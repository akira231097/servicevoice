"""Real gateway/ASGI coordination with synthetic models, keys and audio readiness."""
import asyncio
import json
import sqlite3
import time

import httpx
import pytest

import app.gemini_gateway as module
from app.backchannel_budget import BackchannelBudget
from app.backchannel_catalog import PHRASES
from app.backchannel_policy import BackchannelDecision
from test_gemini_gateway import KEY, SECRET, SIG, frame, post, request_body, setup, tool_stream
from test_pretool_speech_gate import events, issued, speech

DONE = b"data: [DONE]\n\n"


def response(text="Exact primary answer."):
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
        content=frame({"content": text})+frame(reason="stop")+DONE)


def enable(gateway, monkeypatch, *, flag="on", assistant="our-assistant", cache_ready=True):
    gateway.config_loader = lambda: {"GEMINI_GATEWAY_SECRET": SECRET, "GEMINI_API_KEY": KEY,
        "SERVICEVOICE_BACKCHANNELS": flag, "VAPI_BACKCHANNEL_ASSISTANT_ID": assistant,
        "TYPESAFE_API_KEY": "synthetic-classifier-key"}
    gateway.backchannel_cache_ready = cache_ready
    monkeypatch.setattr(gateway, "_backchannel_context", lambda *args: {
        "allow_turn": True, "stage": "service", "active_confirmation": False,
        "has_pending_proposal": False, "has_pending_operation": False, "ended": False})
    # These tests exercise coordination, not a shared runner's worker-start
    # speed. Leave the independent policy enough time to finish its tiny read.
    monkeypatch.setattr(module, "ACK_DELAY_SECONDS", 1, raising=False)
    async def choose(*args, **kwargs):
        return BackchannelDecision(phrase_id="moment", reason="eligible_classification", classification_ms=1,
            probability=1, confidence=1, requests=1)
    monkeypatch.setattr(gateway.backchannel_policy, "choose", choose)


async def observed_post(app, body, observer):
    async def observed(scope, receive, send):
        async def capture(message):
            if message["type"] == "http.response.body":observer(message.get("body", b""))
            await send(message)
        await app(scope, receive, capture)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=observed), base_url="http://testserver") as client:
        return await client.post("/llm/chat/completions", json=body, headers={"authorization": "Bearer " + SECRET})


def test_delayed_primary_releases_one_flushed_prefix_then_exact_answer_with_same_response_id(tmp_path, monkeypatch):
    async def scenario():
        entered, release, cue = asyncio.Event(), asyncio.Event(), asyncio.Event()
        bodies = []
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                entered.set()
                await release.wait()
                yield frame({"content": "Exact primary "})
                yield frame({"content": "answer, complete."})
                yield frame(reason="stop")
                yield DONE
        app, gateway, _ = setup(tmp_path, lambda r: httpx.Response(200,
            headers={"content-type": "text/event-stream"}, stream=Stream()))
        enable(gateway, monkeypatch)
        def observer(body):
            bodies.append(body)
            if b"<flush />" in body:cue.set()
        pending = asyncio.create_task(observed_post(app, request_body(), observer))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await asyncio.wait_for(cue.wait(), 2)
            assert not pending.done(), "acknowledgment must precede the blocked substantive answer"
            assert b"Exact primary" not in b"".join(bodies)
            release.set()
            result = await asyncio.wait_for(pending, 2)
            assert result.status_code == 200
            assert speech(result.text) == PHRASES["moment"]+" <flush />Exact primary answer, complete."
            assert len({event["id"] for event in events(result.text) if "choices" in event}) == 1
            assert result.text.count("<flush />") == 1 and result.text.endswith(DONE.decode())
            row = gateway.latency_snapshot("call-a")[-1]
            assert row["backchannel"]["emitted_ms"] is not None
            assert row["backchannel"]["audible_playback"] == "unverified"
            assert not gateway.active and not gateway.owners and not gateway.successors
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
            await gateway.close()
    asyncio.run(scenario())


def test_fast_primary_wins_and_no_acknowledgment_is_released(tmp_path, monkeypatch):
    app, gateway, _ = setup(tmp_path, lambda r: response())
    enable(gateway, monkeypatch)
    # This tests ordering, not machine performance: leave the primary ample time.
    monkeypatch.setattr(module, "ACK_DELAY_SECONDS", 10, raising=False)
    async def scenario():
        try:
            result = await post(app, request_body())
            assert speech(result.text) == "Exact primary answer." and "<flush />" not in result.text
            assert gateway.latency_snapshot("call-a")[-1]["backchannel"]["reason"] == "primary_ready"
            assert not gateway.active
        finally:await gateway.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("flag,assistant,ready", [("off", "our-assistant", True), ("on", "another-assistant", True),
    ("on", "", True), ("on", "our-assistant", False)])
def test_disabled_or_non_candidate_assistant_keeps_existing_primary_path(tmp_path, monkeypatch, flag, assistant, ready):
    app, gateway, _ = setup(tmp_path, lambda r: response())
    enable(gateway, monkeypatch, flag=flag, assistant=assistant, cache_ready=ready)
    async def forbidden(*args, **kwargs):pytest.fail("inactive candidate must not request classification")
    monkeypatch.setattr(gateway.backchannel_policy, "choose", forbidden)
    async def scenario():
        try:
            result = await post(app, request_body())
            assert speech(result.text) == "Exact primary answer." and "<flush />" not in result.text
            assert "backchannel" not in gateway.latency_snapshot("call-a")[-1]
            assert not gateway.active
        finally:await gateway.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["exception", "slow", "silence", "checking"])
def test_classifier_failure_or_slow_result_never_fails_or_delays_primary(tmp_path, monkeypatch, failure):
    calls = []
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                await release.wait()
                yield frame({"content": "Exact primary answer."})
                yield frame(reason="stop")
                yield DONE
        app, gateway, _ = setup(tmp_path, lambda r: httpx.Response(200,
            headers={"content-type": "text/event-stream"}, stream=Stream()))
        enable(gateway, monkeypatch)
        monkeypatch.setattr(module, "ACK_DELAY_SECONDS", 10, raising=False)
        async def choose(*args, **kwargs):
            calls.append(1)
            entered.set()
            if failure == "exception":raise RuntimeError("PRIVATE classifier failure")
            if failure == "slow":await asyncio.Event().wait()
            return BackchannelDecision(phrase_id="checking" if failure == "checking" else "silence")
        monkeypatch.setattr(gateway.backchannel_policy, "choose", choose)
        pending = asyncio.create_task(post(app, request_body()))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            release.set()
            result = await asyncio.wait_for(pending, 2)
            assert speech(result.text) == "Exact primary answer."
            assert "<flush />" not in result.text and "PRIVATE" not in result.text
            assert calls == [1] and not gateway.active and not gateway.owners
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
            await gateway.close()
    asyncio.run(scenario())


def test_delayed_tool_prose_stays_discarded_and_exact_signature_authority_is_preserved(tmp_path, monkeypatch):
    async def scenario():
        buffered, release, cue = asyncio.Event(), asyncio.Event(), asyncio.Event()
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield frame({"content": "PRIVATE UNVERIFIED BOOKING CLAIM"})
                buffered.set()
                await release.wait()
                yield tool_stream()
        app, gateway, path = setup(tmp_path, lambda r: httpx.Response(200,
            headers={"content-type": "text/event-stream"}, stream=Stream()))
        enable(gateway, monkeypatch)
        pending = asyncio.create_task(observed_post(app, request_body(),
            lambda body: cue.set() if b"<flush />" in body else None))
        try:
            await asyncio.wait_for(buffered.wait(), 2)
            await asyncio.wait_for(cue.wait(), 2)
            release.set()
            result = await asyncio.wait_for(pending, 2)
            assert speech(result.text) == PHRASES["moment"]+" <flush />"
            assert "PRIVATE" not in result.text and "Let me check" not in result.text and SIG not in result.text
            tools = issued(result.text)
            assert len(tools) == 1 and tools[0]["function"] == {"name": "get_appointment", "arguments": "{}"}
            restored = gateway.store.restore_group("sid-a", "call-a", tools)
            assert restored[0]["extra_content"]["google"]["thought_signature"] == SIG
            with sqlite3.connect(path) as db:
                assert db.execute("SELECT COUNT(*) FROM gemini_tool_signatures").fetchone()[0] == 1
            assert len({event["id"] for event in events(result.text) if "choices" in event}) == 1
            assert not gateway.active
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
            await gateway.close()
    asyncio.run(scenario())


def test_new_generation_cancels_obsolete_pending_ack_and_primary_and_releases_lease(tmp_path, monkeypatch):
    async def scenario():
        entered, closed, policy_entered, policy_cancelled = (asyncio.Event() for _ in range(4))
        count = 0
        class Old(httpx.AsyncByteStream):
            async def __aiter__(self):
                entered.set()
                await asyncio.Event().wait()
                yield frame({"content": "OBSOLETE ANSWER"})
            async def aclose(self):closed.set()
        def handler(request):
            nonlocal count
            count += 1
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Old()) if count == 1 else response("Latest answer.")
        app, gateway, _ = setup(tmp_path, handler)
        enable(gateway, monkeypatch)
        monkeypatch.setattr(module, "ACK_DELAY_SECONDS", 10, raising=False)
        decisions = 0
        async def choose(*args, **kwargs):
            nonlocal decisions
            decisions += 1
            if decisions == 1:
                policy_entered.set()
                try:await asyncio.Event().wait()
                finally:policy_cancelled.set()
            return BackchannelDecision(phrase_id="moment")
        monkeypatch.setattr(gateway.backchannel_policy, "choose", choose)
        old = asyncio.create_task(post(app, request_body()))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await asyncio.wait_for(policy_entered.wait(), 2)
            latest = await asyncio.wait_for(post(app, request_body()), 2)
            previous = await asyncio.wait_for(old, 2)
            assert speech(latest.text) == "Latest answer."
            assert "OBSOLETE" not in previous.text and "<flush />" not in previous.text
            assert closed.is_set() and policy_cancelled.is_set() and count == 2
            assert not gateway.active and not gateway.owners and not gateway.successors
        finally:
            if not old.done():old.cancel()
            await asyncio.gather(old, return_exceptions=True)
            await gateway.close()
    asyncio.run(scenario())


def test_budget_cooldown_and_same_turn_cannot_repeat_ack_but_primary_always_completes(tmp_path, monkeypatch):
    async def scenario():
        release, consumed = asyncio.Event(), asyncio.Event()
        class Delayed(httpx.AsyncByteStream):
            async def __aiter__(self):
                await release.wait()
                yield frame({"content": "Exact primary answer."})
                yield frame(reason="stop")
                yield DONE
        app, gateway, _ = setup(tmp_path, lambda r: httpx.Response(200,
            headers={"content-type": "text/event-stream"}, stream=Delayed()))
        enable(gateway, monkeypatch)
        # A functional budget test must not race cold worker scheduling against
        # a 20ms test timer. Keep the primary blocked until budget evaluation.
        monkeypatch.setattr(module, "ACK_DELAY_SECONDS", 1)
        clock = [10.]
        class ObservedBudget(BackchannelBudget):
            def consume(self, *args, **kwargs):
                result = super().consume(*args, **kwargs)
                consumed.set()
                return result
        gateway.backchannel_budget = ObservedBudget(cooldown_seconds=15, per_call=2, window_seconds=1000, clock=lambda: clock[0])
        async def one(body):
            release.clear()
            consumed.clear()
            pending = asyncio.create_task(post(app, body))
            try:
                await asyncio.wait_for(consumed.wait(), 3)
            finally:release.set()
            return await asyncio.wait_for(pending, 2)
        body = request_body()
        try:
            first = await one(body)
            assert first.text.count("<flush />") == 1
            same = await one(body)
            assert "<flush />" not in same.text and speech(same.text) == "Exact primary answer."
            assert gateway.latency_snapshot("call-a")[-1]["backchannel"]["reason"] == "turn_already_acknowledged"
            body["messages"].extend([{"role": "assistant", "content": "Tell me more."}, {"role": "user", "content": "My car is a Toyota sedan."}])
            cooldown = await one(body)
            assert "<flush />" not in cooldown.text and speech(cooldown.text) == "Exact primary answer."
            assert gateway.latency_snapshot("call-a")[-1]["backchannel"]["reason"] == "cooldown"
            clock[0] += 16
            second = await one(body)
            assert second.text.count("<flush />") == 1
            clock[0] += 16
            body["messages"].extend([{"role": "assistant", "content": "Anything about the vehicle?"}, {"role": "user", "content": "It has a standard gas engine."}])
            exhausted = await one(body)
            assert "<flush />" not in exhausted.text and speech(exhausted.text) == "Exact primary answer."
            assert gateway.latency_snapshot("call-a")[-1]["backchannel"]["reason"] == "call_budget"
            assert not gateway.active and not gateway.owners
        finally:await gateway.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("interrupt_after_cue", [False, True])
def test_cancelled_header_worker_retains_physical_lease_until_cleanup_really_finishes(tmp_path, monkeypatch, interrupt_after_cue):
    async def scenario():
        entered, cancelled, release, cue = (asyncio.Event() for _ in range(4))
        count, cancellations = 0, 0
        async def handler(request):
            nonlocal count, cancellations
            count += 1
            if count == 1:
                entered.set()
                try:await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancellations += 1
                    cancelled.set()
                    try:await release.wait()
                    except asyncio.CancelledError:
                        cancellations += 1
                        raise
                return response("OBSOLETE provider response")
            return response("Recovered primary answer.")
        app, gateway, _ = setup(tmp_path, handler,
            module.GatewayLimits(concurrency=1, cleanup_seconds=.02, supersession_seconds=.05))
        enable(gateway, monkeypatch)
        monkeypatch.setattr(module, "ACK_DELAY_SECONDS", 1 if interrupt_after_cue else .03)
        original, offered = None, []
        def observe(body):
            # This is the actual old response's ASGI-send boundary. Scheduling
            # may permit a valid cue before the successor reaches admission.
            # It must never permit another cue/content after supersession.
            offered.append((original is not None and original.superseded.is_set(), body))
            if b"<flush />" in body:cue.set()
        old = asyncio.create_task(observed_post(app, request_body(), observe))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            original = gateway.owners["call-a"]
            if interrupt_after_cue:
                await asyncio.wait_for(cue.wait(), 2)
                assert original.payload_emitted and not original.task.done()
            successor = asyncio.create_task(post(app, request_body()))
            await asyncio.wait_for(cancelled.wait(), 2)
            newer = await asyncio.wait_for(successor, 2)
            await asyncio.sleep(.08)  # First cancellation remains held beyond cue/cleanup budgets.
            assert speech(newer.text) == "" and "<flush />" not in newer.text
            assert count == 1 and cancellations == 1
            assert gateway.owners["call-a"] is original and not original.released.is_set()
            assert gateway.active == {"call-a"}
            assert (await post(app, request_body("call-b"))).status_code == 429
            release.set()
            previous = await asyncio.wait_for(old, 2)
            assert "OBSOLETE" not in previous.text and issued(previous.text) == []
            assert previous.text.count("<flush />") <= 1
            assert speech(previous.text) in ("", PHRASES["moment"]+" <flush />")
            after_supersession = b"".join(body for superseded, body in offered if superseded)
            assert b"<flush />" not in after_supersession and b"OBSOLETE" not in after_supersession
            assert after_supersession.endswith(DONE)
            # An already offered prefix and its neutral closing frame belong
            # to the same SSE response, even though the generation is obsolete.
            assert len({event["id"] for event in events(previous.text) if "choices" in event}) == 1
            async with asyncio.timeout(2):
                while gateway.active:await asyncio.sleep(.001)
            recovered = await post(app, request_body())
            assert speech(recovered.text) == "Recovered primary answer."
            assert count == 2 and cancellations == 1
            assert not gateway.active and not gateway.owners and not gateway.successors
        finally:
            release.set()
            if not old.done():old.cancel()
            await asyncio.gather(old, return_exceptions=True)
            await gateway.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["http500", "timeout", "wrong_content_type"])
def test_fast_header_failure_remains_an_error_instead_of_neutral_success(tmp_path, monkeypatch, failure):
    def handler(request):
        if failure == "timeout":raise httpx.ReadTimeout("PRIVATE synthetic provider error")
        if failure == "http500":return httpx.Response(500)
        return httpx.Response(200, headers={"content-type": "application/json"}, json={})
    app, gateway, _ = setup(tmp_path, handler)
    enable(gateway, monkeypatch)
    monkeypatch.setattr(module, "ACK_DELAY_SECONDS", 10)
    async def scenario():
        try:
            result = await post(app, request_body())
            assert result.status_code == 200  # Candidate already owns an SSE response.
            assert "generation_unavailable" in result.text and "PRIVATE" not in result.text
            assert DONE.decode() not in result.text and '"finish_reason":"stop"' not in result.text
            assert speech(result.text) == "" and issued(result.text) == []
            row = gateway.latency_snapshot("call-a")[-1]
            assert row["outcome"] == "upstream_unavailable" and row["supersession_cause"] is None
            assert not gateway.active and not gateway.owners and not gateway.successors
        finally:await gateway.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["http500", "timeout", "wrong_content_type", "incomplete_generation", "body_error"])
def test_upstream_failure_after_offered_cue_never_becomes_a_false_success(tmp_path, monkeypatch, failure):
    async def scenario():
        entered, release, cue = asyncio.Event(), asyncio.Event(), asyncio.Event()
        offered = []
        class BrokenBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield frame({"content": "PRIVATE unvalidated provider content"})
                if failure == "body_error":raise httpx.ReadError("PRIVATE synthetic body error")
                # Missing stop/tool finish before DONE must remain a failure.
                yield DONE
        async def handler(request):
            entered.set()
            await release.wait()
            if failure == "timeout":raise httpx.ReadTimeout("PRIVATE synthetic provider error")
            if failure == "http500":return httpx.Response(500)
            if failure == "wrong_content_type":
                return httpx.Response(200, headers={"content-type": "application/json"}, json={})
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=BrokenBody())
        app, gateway, _ = setup(tmp_path, handler)
        enable(gateway, monkeypatch)
        def observe(body):
            offered.append(body)
            if b"<flush />" in body:cue.set()
        pending = asyncio.create_task(observed_post(app, request_body(), observe))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await asyncio.wait_for(cue.wait(), 2)
            assert not pending.done() and b"PRIVATE" not in b"".join(offered)
            release.set()
            result = await asyncio.wait_for(pending, 2)
            assert "generation_unavailable" in result.text and "PRIVATE" not in result.text
            assert speech(result.text) == PHRASES["moment"]+" <flush />"
            assert result.text.count("<flush />") == 1 and issued(result.text) == []
            assert DONE.decode() not in result.text and '"finish_reason":"stop"' not in result.text
            assert not gateway.active and not gateway.owners and not gateway.successors
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
            await gateway.close()
    asyncio.run(scenario())
