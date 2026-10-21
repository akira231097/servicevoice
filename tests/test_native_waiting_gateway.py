"""One native Vapi stream: waiting context is fresh and main never queues behind policy."""
import asyncio
import threading
import json
import sqlite3
import time
from contextlib import closing

import httpx
import pytest

from app.backchannel_policy import BackchannelDecision, BackchannelPolicy
from test_backchannel_gateway import DONE, enable, observed_post, response
from test_gemini_gateway import SIG, continuation, frame, post, request_body, setup, tool_stream
from test_native_waiting_policy import CONFIRM, READBACK, SAFE, answer
from test_pretool_speech_gate import issued, speech


@pytest.mark.parametrize("text,prior,kind,expected", [
    ("Jamie", "May I have your name?", "ordinary_detail", "One moment."),
    ("My name is Jamie", "May I have your name?", "ordinary_detail", "One moment."),
    ("What services do you offer?", "How can I help?", "information_question", "Let me check that."),
    ("Actually the vehicle is a 2016 sedan.", "Which vehicle?", "factual_correction", "One moment."),
])
def test_real_native_policy_releases_context_appropriate_cached_text_in_owned_stream(tmp_path, monkeypatch, text, prior, kind, expected):
    async def scenario():
        release, offered = asyncio.Event(), asyncio.Event()
        class Delayed(httpx.AsyncByteStream):
            async def __aiter__(self):
                await release.wait()
                yield frame({"content": "Exact primary answer."}); yield frame(reason="stop"); yield DONE
        app, gateway, _ = setup(tmp_path, lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Delayed()))
        enable(gateway, monkeypatch)
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=answer(kind))))
        gateway.backchannel_policy = BackchannelPolicy(client=client)
        body = request_body(); body["messages"] = [{"role": "assistant", "content": prior}, {"role": "user", "content": text}]
        task = asyncio.create_task(observed_post(app, body, lambda part: offered.set() if b"<flush />" in part else None))
        try:
            await asyncio.wait_for(offered.wait(), 3)
            assert not task.done()
            release.set(); result = await asyncio.wait_for(task, 2)
            assert speech(result.text) == expected + " <flush /> Exact primary answer."
            timing = gateway.latency_snapshot("call-a")[-1]["backchannel"]
            assert timing["content_kind"] == kind and timing["reason"] == "emitted"
            assert timing["emitted_ms"] is not None and timing["audible_playback"] == "unverified"
        finally:
            release.set(); await asyncio.gather(task, return_exceptions=True)
            await gateway.close(); await client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("change,expected", [({"stage": "confirmation", "active_confirmation": True}, "sensitive_context"),
    ({"has_pending_operation": True}, "sensitive_context"), ({"allow_turn": False}, "turn_not_allowed"),
    ({"stage": "slot"}, "scheduler_changed"), ("failure", "context_unavailable")])
def test_fresh_authoritative_change_revokes_cue_before_stream_yield(tmp_path, monkeypatch, change, expected):
    async def scenario():
        release, suppressed = asyncio.Event(), asyncio.Event()
        class Delayed(httpx.AsyncByteStream):
            async def __aiter__(self):
                await release.wait()
                yield frame({"content": "Exact primary answer."}); yield frame(reason="stop"); yield DONE
        app, gateway, _ = setup(tmp_path, lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Delayed()))
        enable(gateway, monkeypatch); reads = []
        def read(*args):
            reads.append(1)
            if len(reads) == 1:return dict(SAFE)
            if change == "failure":raise RuntimeError("PRIVATE read failure")
            return {**SAFE, **change}
        monkeypatch.setattr(gateway, "_backchannel_context", read)
        original = gateway._backchannel_observation
        def observation(timing, event, elapsed):
            original(timing, event, elapsed)
            if event == "backchannel.suppressed":suppressed.set()
        monkeypatch.setattr(gateway, "_backchannel_observation", observation)
        task = asyncio.create_task(post(app, request_body()))
        try:
            await asyncio.wait_for(suppressed.wait(), 3); release.set()
            result = await asyncio.wait_for(task, 2)
            assert speech(result.text) == "Exact primary answer." and "<flush />" not in result.text
            assert gateway.latency_snapshot("call-a")[-1]["backchannel"]["reason"] == expected
            assert len(reads) == 2 and not gateway.backchannel_budget.calls
        finally:
            release.set(); await asyncio.gather(task, return_exceptions=True); await gateway.close()
    asyncio.run(scenario())


def test_main_ready_during_fresh_read_wins_without_waiting_for_physical_worker(tmp_path, monkeypatch):
    async def scenario():
        release_main, fresh_entered = asyncio.Event(), asyncio.Event()
        release_worker = threading.Event(); reads = []; loop = asyncio.get_running_loop()
        class Delayed(httpx.AsyncByteStream):
            async def __aiter__(self):
                await release_main.wait()
                yield frame({"content": "Exact primary answer."}); yield frame(reason="stop"); yield DONE
        app, gateway, _ = setup(tmp_path, lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Delayed()))
        enable(gateway, monkeypatch)
        def read(*args):
            reads.append(1)
            if len(reads) > 1:
                loop.call_soon_threadsafe(fresh_entered.set)
                assert release_worker.wait(5)
            return dict(SAFE)
        monkeypatch.setattr(gateway, "_backchannel_context", read)
        task = asyncio.create_task(post(app, request_body()))
        try:
            await asyncio.wait_for(fresh_entered.wait(), 3); release_main.set()
            result = await asyncio.wait_for(task, 1)
            assert speech(result.text) == "Exact primary answer." and "<flush />" not in result.text
            assert gateway.latency_snapshot("call-a")[-1]["backchannel"]["reason"] == "primary_ready"
            assert not release_worker.is_set() and not gateway.backchannel_budget.calls
        finally:
            release_worker.set(); release_main.set(); await asyncio.gather(task, return_exceptions=True); await gateway.close()
    asyncio.run(scenario())


def test_budget_capacity_retires_only_authoritatively_ended_or_expired_calls(tmp_path):
    from app.backchannel_budget import BackchannelBudget
    app, gateway, path = setup(tmp_path, lambda r: response())
    gateway.narration.store.read_connection = lambda timeout: closing(sqlite3.connect(path))
    gateway.backchannel_budget = BackchannelBudget(capacity=2, clock=lambda: 10.)
    budget = gateway.backchannel_budget
    assert budget.consume("call-a", 1)[0] and budget.consume("call-b", 1)[0]
    assert budget.consume("call-new", 1) == (False, "budget_capacity")
    with sqlite3.connect(path) as db:db.execute("UPDATE sessions SET ended=1 WHERE provider_call_id='call-b'")
    retired = gateway._backchannel_retired(tuple(budget.calls)); assert retired == ("call-b",)
    budget.retire_confirmed(retired)
    assert budget.consume("call-a", 1) == (False, "turn_already_acknowledged")
    # More than the ledger capacity can pass over the process lifetime without
    # evicting a live call or refunding its same-turn reservation.
    for ordinal in range(6):
        call = "completed-" + str(ordinal)
        assert budget.consume(call, 1)[0]
        with sqlite3.connect(path) as db:
            db.execute("INSERT INTO sessions VALUES (?,?,?,?,?)", ("retired-"+str(ordinal), "cedar-demo", 0,
                time.time()-5*3600, call))
        budget.retire_confirmed(gateway._backchannel_retired(tuple(budget.calls)))
        assert tuple(budget.calls) == ("call-a",)
    asyncio.run(gateway.close())


@pytest.mark.parametrize("kind,expired,expected", [("book", False, True), ("book", True, False), ("reschedule", False, False), ("cancel", False, False)])
def test_confirmation_context_requires_fresh_authoritative_booking_proposal(tmp_path, kind, expired, expected):
    from app.domain import Store
    app, gateway, _ = setup(tmp_path, lambda r: response())
    store = Store(tmp_path / "business.sqlite3"); created = store.create_session("live"); sid = created["id"]
    with store.connect() as db:
        db.execute("UPDATE sessions SET provider_call_id=?,state=? WHERE id=?", ("owned-call", json.dumps({"stage": "confirmation", "proposal_id": "owned-proposal"}), sid))
        db.execute("INSERT INTO proposals VALUES (?,?,?,?,?,?,?,?)", ("owned-proposal", sid, "cedar-demo", kind, "{}", time.time()+(-10 if expired else 60), "pending", None))
    gateway.narration.store = store
    assert gateway._backchannel_context(sid, "owned-call")["booking_confirmation_context"] is expected
    asyncio.run(gateway.close())


def test_book_it_hold_cannot_publish_tool_prose_or_change_signed_authority(tmp_path, monkeypatch):
    async def scenario():
        release, cue = asyncio.Event(), asyncio.Event()
        class Delayed(httpx.AsyncByteStream):
            async def __aiter__(self):
                await release.wait()
                yield frame({"content": "UNVERIFIED BOOKING SUCCESS"}); yield tool_stream()
        app, gateway, _ = setup(tmp_path, lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Delayed()))
        enable(gateway, monkeypatch); monkeypatch.setattr(gateway, "_backchannel_context", lambda *args: dict(CONFIRM))
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=answer("information_question"))))
        gateway.backchannel_policy = BackchannelPolicy(client=client)
        body = request_body(); body["messages"] = [{"role": "assistant", "content": READBACK}, {"role": "user", "content": "Book it."}]
        task = asyncio.create_task(observed_post(app, body, lambda part: cue.set() if b"<flush />" in part else None))
        try:
            await asyncio.wait_for(cue.wait(), 3); release.set(); result = await asyncio.wait_for(task, 2)
            assert speech(result.text) == "One moment. <flush /> "
            assert "UNVERIFIED" not in result.text and "Let me check" not in result.text
            tools = issued(result.text)
            assert len(tools) == 1 and tools[0]["function"] == {"name": "get_appointment", "arguments": "{}"}
            assert gateway.store.restore_group("sid-a", "call-a", tools)[0]["extra_content"]["google"]["thought_signature"] == SIG
        finally:
            release.set(); await asyncio.gather(task, return_exceptions=True); await gateway.close(); await client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("cue_before_tool", [False, True])
def test_signed_tool_followup_reuses_only_caller_context_and_same_turn_budget(tmp_path, monkeypatch, cue_before_tool):
    async def scenario():
        first_release, second_release, cue, suppressed = (asyncio.Event() for _ in range(4))
        calls, classified = [], []
        class Delayed(httpx.AsyncByteStream):
            def __init__(self, release, content):self.release, self.content = release, content
            async def __aiter__(self):
                await self.release.wait(); yield self.content
        def handle(request):
            calls.append(json.loads(request.content))
            if len(calls) == 1 and not cue_before_tool:return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=tool_stream())
            content = tool_stream() if len(calls) == 1 else frame({"content": "Exact primary answer."})+frame(reason="stop")+DONE
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Delayed(first_release if len(calls) == 1 else second_release, content))
        app, gateway, _ = setup(tmp_path, handle); enable(gateway, monkeypatch)
        async def choose(history, key, **kwargs):
            assert kwargs["native_waiting"] is True and history[-1]["role"] == "user"
            assert "PRIVATE_RECEIPT" not in json.dumps(history) and all(row["role"] != "tool" for row in history)
            classified.append(history)
            return BackchannelDecision(phrase_id="moment", reason="eligible_classification")
        monkeypatch.setattr(gateway.backchannel_policy, "choose", choose)
        original = gateway._backchannel_observation
        def observe(timing, event, elapsed):
            original(timing, event, elapsed)
            if event == "backchannel.suppressed":suppressed.set()
        monkeypatch.setattr(gateway, "_backchannel_observation", observe)
        pending = None
        try:
            if cue_before_tool:
                pending = asyncio.create_task(observed_post(app, request_body(), lambda part: cue.set() if b"<flush />" in part else None))
                await asyncio.wait_for(cue.wait(), 3); first_release.set(); first = await asyncio.wait_for(pending, 2)
                assert first.text.count("<flush />") == 1
            else:first = await post(app, request_body()); assert "<flush />" not in first.text
            receipt = {"ok": True, "appointment": None, "note": "PRIVATE_RECEIPT"}
            gateway.observations.record("sid-a", "call-a", "tool-one", receipt)
            body = continuation(); body["messages"][-1]["content"] = json.dumps(receipt)
            cue.clear(); suppressed.clear()
            pending = asyncio.create_task(observed_post(app, body, lambda part: cue.set() if b"<flush />" in part else None))
            await asyncio.wait_for((suppressed if cue_before_tool else cue).wait(), 3)
            second_release.set(); result = await asyncio.wait_for(pending, 2)
            assert speech(result.text) == ("" if cue_before_tool else "One moment. <flush /> ")+"Exact primary answer."
            assert classified and classified[-1][-1]["content"] == "Find my appointment"
            assert "PRIVATE_RECEIPT" in json.dumps(calls[-1]["messages"])
            assert len(calls) == 2
        finally:
            first_release.set(); second_release.set()
            if pending is not None:await asyncio.gather(pending, return_exceptions=True)
            await gateway.close()
    asyncio.run(scenario())
