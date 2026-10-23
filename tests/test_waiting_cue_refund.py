"""A waiting cue Vapi never played must not silence the real post-turn request.

Observed on 2026-10-21: Vapi starts speculative model requests while the caller
is still speaking and cancels or replaces them as speech continues, discarding
their output. The gateway had spent its one-per-turn cue inside those discarded
requests, so the actual answer arrived after several seconds of silence. A refund
applies only to cancelled or superseded generations that released no answer text
or tool command; completed answers and upstream failures keep their consumption.
The refund keeps the one-second spacing from the unplayed emission, so these
coordination tests drive the budget clock explicitly, like the budget tests do.
"""
import asyncio
from uuid import uuid4

import httpx

import app.gemini_gateway as module
from app.backchannel_budget import BackchannelBudget
from app.backchannel_catalog import PHRASES
from app.diagnostics import EVENTS, safe_attributes
from app.generation_observability import project_gateway_timings
from app.live_eval_evidence import latency
from app.observability import safe_backchannel_timing
from test_backchannel_gateway import DONE, enable, observed_post
from test_backchannel_observability import candidate_timings
from test_gemini_gateway import frame, request_body, setup
from test_local_timing import row
from test_pretool_speech_gate import speech


def test_budget_refund_restores_turn_and_window_but_keeps_cooldown_anchor():
    ticks = [1.]; budget = BackchannelBudget(clock=lambda: ticks[0])
    assert budget.consume("owned", 1, "moment") == (True, "emitted")
    stamp = budget.receipt("owned")
    assert stamp == 1 and budget.consume("owned", 1) == (False, "turn_already_acknowledged")
    assert budget.refund("owned", stamp) == (True, "refunded")
    assert budget.refund("owned", stamp) == (False, "not_refundable")
    ticks[0] = 1.5
    assert budget.consume("owned", 1) == (False, "cooldown")  # spacing still counts from the unplayed cue
    ticks[0] = 2.1
    assert budget.consume("owned", 1, "one_sec") == (True, "emitted")
    assert budget.previous_phrase("owned") == "one_sec"
    ticks[0] = 4
    assert budget.consume("owned", 2, "checking") == (True, "emitted")
    assert budget.refund("owned", budget.receipt("owned")) == (True, "refunded")
    assert budget.previous_phrase("owned") == "one_sec" and budget.calls["owned"][0] == 1
    ticks[0] = 6
    assert budget.consume("owned", 2, "taking_a_look") == (True, "emitted")


def test_budget_refund_does_not_count_unplayed_cues_in_the_rolling_window():
    ticks = [0.]; budget = BackchannelBudget(clock=lambda: ticks[0])
    for ordinal in range(1, 5):
        ticks[0] = ordinal * 2
        assert budget.consume("owned", ordinal, "moment") == (True, "emitted")
    assert budget.refund("owned", budget.receipt("owned")) == (True, "refunded")
    ticks[0] = 10
    assert budget.consume("owned", 4, "one_sec") == (True, "emitted")  # the fourth turn is open again
    ticks[0] = 12
    assert budget.consume("owned", 5) == (False, "call_budget")


def test_budget_refund_rejects_stale_retired_or_unknown_receipts():
    ticks = [1.]; budget = BackchannelBudget(clock=lambda: ticks[0])
    assert budget.refund("unknown", 1) == (False, "not_refundable")
    assert budget.consume("owned", 1, "moment") == (True, "emitted")
    first = budget.receipt("owned")
    ticks[0] = 3
    assert budget.consume("owned", 2, "one_sec") == (True, "emitted")
    assert budget.refund("owned", first) == (False, "not_refundable")  # a newer consumption owns the row
    assert budget.calls["owned"][0] == 2
    budget.retire_confirmed(["owned"])
    assert budget.receipt("owned") is None and budget.refund("owned", 3) == (False, "not_refundable")


class Diagnostics:
    def __init__(self):self.events = []

    def record(self, component, event, outcome, **kwargs):
        self.events.append((component, event, outcome, kwargs.get("attributes") or {}));return True


def blocked_stream(release, text="Exact primary answer."):
    class Blocked(httpx.AsyncByteStream):
        async def __aiter__(self):
            await release.wait()
            yield frame({"content": text}); yield frame(reason="stop"); yield DONE
    return Blocked()


def streaming(release):
    return lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=blocked_stream(release))


def cue_observer(cue):
    return lambda part: cue.set() if b"<flush />" in part else None


def clocked_budget(gateway):
    clock = [10.]
    gateway.backchannel_budget = BackchannelBudget(clock=lambda: clock[0])
    return clock


def test_cue_spent_on_a_request_vapi_cancelled_returns_for_the_real_post_turn_request(tmp_path, monkeypatch):
    async def scenario():
        release, cue = asyncio.Event(), asyncio.Event()
        app, gateway, _ = setup(tmp_path, streaming(release))
        enable(gateway, monkeypatch)
        clock = clocked_budget(gateway)
        gateway.diagnostics = Diagnostics()
        speculative = asyncio.create_task(observed_post(app, request_body(), cue_observer(cue)))
        try:
            await asyncio.wait_for(cue.wait(), 3)
            assert gateway.backchannel_budget.calls["call-a"][0] == 1
            speculative.cancel()  # Vapi abandons the speculative request before any answer exists.
            await asyncio.gather(speculative, return_exceptions=True)
            async with asyncio.timeout(3):
                while gateway.active or gateway.owners:await asyncio.sleep(.001)
            first = gateway.latency_snapshot("call-a")[-1]
            assert first["backchannel"]["reason"] == "emitted" and first["backchannel"]["refunded"] is True
            assert first["backchannel"]["refund_reason"] == "generation_cancelled_before_answer"
            assert first["milestones_ms"]["first_text"] is None and first["outcome"] == "cancelled"
            assert gateway.backchannel_budget.calls["call-a"][0] == 0
            refunded = [event for event in gateway.diagnostics.events if event[1] == "backchannel.refunded"]
            assert len(refunded) == 1 and refunded[0][3]["backchannel_reason"] == "generation_cancelled_before_answer"
            assert refunded[0][3]["backchannel_refunded"] is True and refunded[0][3]["backchannel_refund_reason"] == "generation_cancelled_before_answer"
            cue.clear()
            clock[0] += .5
            early = asyncio.create_task(observed_post(app, request_body(), cue_observer(cue)))
            # Spacing from the unplayed emission still holds: a request this soon stays silent.
            with_cue = await asyncio.wait([asyncio.ensure_future(cue.wait())], timeout=1.6)
            assert not with_cue[0]
            release.set()
            quiet = await asyncio.wait_for(early, 3)
            assert speech(quiet.text) == "Exact primary answer."
            assert gateway.latency_snapshot("call-a")[-1]["backchannel"]["reason"] == "cooldown"
            release.clear(); cue.clear()
            clock[0] += 2
            real = asyncio.create_task(observed_post(app, request_body(), cue_observer(cue)))
            await asyncio.wait_for(cue.wait(), 3)
            release.set()
            result = await asyncio.wait_for(real, 3)
            assert speech(result.text) == PHRASES["moment"] + " <flush />Exact primary answer."
            second = gateway.latency_snapshot("call-a")[-1]
            assert second["backchannel"]["reason"] == "emitted" and "refunded" not in second["backchannel"]
            assert second["outcome"] == "completed" and not gateway.active and not gateway.owners
        finally:
            release.set()
            if not speculative.done():speculative.cancel()
            await asyncio.gather(speculative, return_exceptions=True)
            await gateway.close()
    asyncio.run(scenario())


def test_cue_spent_on_a_superseded_request_returns_for_its_replacement(tmp_path, monkeypatch):
    async def scenario():
        release, cue = asyncio.Event(), asyncio.Event()
        count = 0
        def handler(request):
            nonlocal count
            count += 1
            return streaming(release)(request)
        app, gateway, _ = setup(tmp_path, handler, module.GatewayLimits(concurrency=1, cleanup_seconds=.5, supersession_seconds=2))
        enable(gateway, monkeypatch)
        clock = clocked_budget(gateway)
        gateway.diagnostics = Diagnostics()
        old = asyncio.create_task(observed_post(app, request_body(), cue_observer(cue)))
        try:
            await asyncio.wait_for(cue.wait(), 3)
            cue.clear()
            clock[0] += 2
            newer = asyncio.create_task(observed_post(app, request_body(), cue_observer(cue)))
            await asyncio.wait_for(cue.wait(), 4)  # the replacement earned the same turn's cue again
            release.set()
            latest = await asyncio.wait_for(newer, 3)
            previous = await asyncio.wait_for(old, 3)
            assert speech(latest.text) == PHRASES["moment"] + " <flush />Exact primary answer."
            assert speech(previous.text) == PHRASES["moment"] + " <flush />"  # offered, then a neutral close
            rows = gateway.latency_snapshot("call-a")
            superseded = [item for item in rows if item["backchannel"].get("refunded")]
            assert len(superseded) == 1 and superseded[0]["backchannel"]["refund_reason"] == "generation_superseded_before_answer"
            assert superseded[0]["milestones_ms"]["first_text"] is None
            assert rows[-1]["backchannel"]["reason"] == "emitted" and "refunded" not in rows[-1]["backchannel"]
            assert [event[3]["backchannel_reason"] for event in gateway.diagnostics.events if event[1] == "backchannel.refunded"] == ["generation_superseded_before_answer"]
            assert count == 2 and not gateway.active and not gateway.owners and not gateway.successors
        finally:
            release.set()
            if not old.done():old.cancel()
            await asyncio.gather(old, return_exceptions=True)
            await gateway.close()
    asyncio.run(scenario())


def test_completed_answer_and_upstream_failure_keep_their_cue_consumption(tmp_path, monkeypatch):
    async def scenario():
        first_release, second_release, third_release = (asyncio.Event() for _ in range(3))
        cue, suppressed = asyncio.Event(), asyncio.Event()
        count = 0
        async def handler(request):
            nonlocal count
            count += 1
            if count == 1:return streaming(first_release)(request)
            if count == 2:
                await second_release.wait()
                return httpx.Response(500)
            return streaming(third_release)(request)
        app, gateway, _ = setup(tmp_path, handler)
        enable(gateway, monkeypatch)
        clock = clocked_budget(gateway)
        original = gateway._backchannel_observation
        def observe(timing, event, elapsed):
            original(timing, event, elapsed)
            if event == "backchannel.suppressed":suppressed.set()
        monkeypatch.setattr(gateway, "_backchannel_observation", observe)
        pending = None
        try:
            pending = asyncio.create_task(observed_post(app, request_body(), cue_observer(cue)))
            await asyncio.wait_for(cue.wait(), 3); first_release.set()
            completed = await asyncio.wait_for(pending, 3)
            assert speech(completed.text) == PHRASES["moment"] + " <flush />Exact primary answer."
            assert "refunded" not in gateway.latency_snapshot("call-a")[-1]["backchannel"]
            body = request_body()
            body["messages"].extend([{"role": "assistant", "content": "Tell me more."}, {"role": "user", "content": "My car is a Toyota sedan."}])
            clock[0] += 5; cue.clear()
            pending = asyncio.create_task(observed_post(app, body, cue_observer(cue)))
            await asyncio.wait_for(cue.wait(), 3); second_release.set()
            failed = await asyncio.wait_for(pending, 3)
            # The second caller turn rotates to the next neutral phrase; the cue is still offered before the failure.
            assert "generation_unavailable" in failed.text and speech(failed.text) == PHRASES["one_sec"] + " <flush />"
            failed_row = gateway.latency_snapshot("call-a")[-1]
            assert failed_row["outcome"] == "upstream_unavailable" and "refunded" not in failed_row["backchannel"]
            assert gateway.backchannel_budget.calls["call-a"][0] == 2
            clock[0] += 5; cue.clear(); suppressed.clear()
            pending = asyncio.create_task(observed_post(app, body, cue_observer(cue)))
            await asyncio.wait_for(suppressed.wait(), 3); third_release.set()
            retried = await asyncio.wait_for(pending, 3)
            assert speech(retried.text) == "Exact primary answer."
            assert gateway.latency_snapshot("call-a")[-1]["backchannel"]["reason"] == "turn_already_acknowledged"
            assert not gateway.active and not gateway.owners
        finally:
            for event in (first_release, second_release, third_release):event.set()
            if pending is not None:await asyncio.gather(pending, return_exceptions=True)
            await gateway.close()
    asyncio.run(scenario())


def test_refund_evidence_survives_projection_and_is_excluded_from_acknowledgment_latency():
    cue = {"phrase_id": "moment", "reason": "emitted", "emitted_ms": 584, "refunded": True,
           "refund_reason": "generation_cancelled_before_answer", "text": "PRIVATE"}
    safe = safe_backchannel_timing(cue)
    assert safe["refunded"] is True and safe["refund_reason"] == "generation_cancelled_before_answer"
    assert safe["emitted_ms"] == 584 and "PRIVATE" not in repr(safe) and safe_backchannel_timing(safe) == safe
    metrics = latency({}, [{**candidate_timings()[0], "backchannel": safe}])
    assert "acknowledgment_observed" not in metrics and "server_first_ack_p95_ms" not in metrics
    assert latency({}, candidate_timings())["server_first_ack_p95_ms"] == 550
    projected = project_gateway_timings([row(generation_id=str(uuid4()), outcome="cancelled", backchannel=cue)])
    assert projected["generations"][0]["backchannel"]["refunded"] is True
    assert projected["generations"][0]["backchannel"]["refund_reason"] == "generation_cancelled_before_answer"
    assert "PRIVATE" not in repr(projected)
    assert "backchannel.refunded" in EVENTS
    attributes = safe_attributes({"backchannel_refunded": True, "backchannel_refund_reason": "generation_superseded_before_answer",
                                  "backchannel_reason": "generation_cancelled_before_answer", "backchannel_note": "PRIVATE"})
    assert attributes["backchannel_refunded"] is True and attributes["backchannel_refund_reason"] == "generation_superseded_before_answer"
    assert attributes["backchannel_reason"] == "generation_cancelled_before_answer" and "PRIVATE" not in repr(attributes)
