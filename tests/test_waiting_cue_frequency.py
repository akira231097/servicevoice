"""More frequent, still neutral waiting cues: the 2026-10-22 production call review.

A 3m49s inbound call on the refund release played three cues in thirteen caller
turns. The per-generation records showed four silences caused by the scheduler's
`date` stage (a search with no slots for the requested day) being unknown to the
policy, a lookup turn whose answer generation stayed silent for 3.5 s after the
tool-call generation had spent the turn's only cue, ordinary scheduling turns at
0.91-0.96 fit rejected only because the subject answer was unsure which routine
kind applied, and a plain "yes, that's correct" approval of a booking readback
that the verified-confirmation exception did not recognize. These tests pin the
corrections; business authority, phrase identity and refunds are unchanged.
"""
import asyncio

import httpx
import pytest

from app.backchannel_budget import BackchannelBudget
from app.backchannel_catalog import PHRASES
from app.backchannel_policy import BackchannelDecision, waiting_phrase, waiting_state
from app.diagnostics import safe_attributes
from app.live_eval_evidence import latency
from app.observability import safe_backchannel_timing
from test_backchannel_gateway import enable, observed_post
from test_backchannel_observability import candidate_timings
from test_gemini_gateway import continuation, request_body, setup, tool_stream
from test_native_waiting_policy import CONFIRM, READBACK, SAFE, messages
from test_pretool_speech_gate import speech
from test_waiting_cue_refund import Diagnostics, clocked_budget, cue_observer, streaming


@pytest.mark.parametrize("stage,eligible", [("service", True), ("slot", True), ("date", True), ("details", True),
                                            ("confirmation", False), ("complete", False), ("handoff", False), ("searching", False)])
def test_no_slot_date_stage_is_ordinary_scheduling_context(stage, eligible):
    state, reason = waiting_state(messages("Is Friday morning open instead?", "Nothing is open on Thursday."), {**SAFE, "stage": stage})
    assert (state is not None) is eligible
    if stage == "searching":assert reason == "invalid_context"


def test_budget_grants_one_spaced_neutral_follow_up_per_turn_after_a_tool_result():
    ticks = [10.]; budget = BackchannelBudget(clock=lambda: ticks[0])
    assert budget.consume("owned", 1, "checking") == (True, "emitted")
    assert budget.consume("owned", 1, "one_sec", follow_up=True) == (False, "cooldown")
    ticks[0] = 12.4
    assert budget.consume("owned", 1, "one_sec", follow_up=True) == (False, "cooldown")  # under 2.5 s after the turn's cue
    assert budget.consume("owned", 1, "one_sec") == (False, "turn_already_acknowledged")  # not a tool-result request
    ticks[0] = 12.6
    assert budget.consume("owned", 1, "one_sec", follow_up=True) == (True, "emitted")
    assert budget.previous_phrase("owned") == "one_sec" and budget.calls["owned"][4] == 1
    ticks[0] = 20
    assert budget.consume("owned", 1, "just_a_sec", follow_up=True) == (False, "turn_already_acknowledged")  # one follow-up per turn
    assert budget.consume("owned", 2, "moment", follow_up=True) == (True, "emitted")  # a later turn's first cue may itself follow a tool result
    assert budget.calls["owned"][0] == 2 and budget.calls["owned"][4] is None


def test_follow_up_cues_count_toward_the_rolling_window_cap():
    ticks = [10.]; budget = BackchannelBudget(clock=lambda: ticks[0])
    assert budget.consume("owned", 1, "checking") == (True, "emitted")
    ticks[0] = 12.6; assert budget.consume("owned", 1, "one_sec", follow_up=True) == (True, "emitted")
    ticks[0] = 14; assert budget.consume("owned", 2, "moment") == (True, "emitted")
    ticks[0] = 16.6; assert budget.consume("owned", 2, "one_sec", follow_up=True) == (True, "emitted")
    ticks[0] = 18; assert budget.consume("owned", 3, "just_a_sec") == (False, "call_budget")
    ticks[0] = 25.1; assert budget.consume("owned", 3, "just_a_sec") == (True, "emitted")


def test_refunded_follow_up_restores_the_turn_mark_but_keeps_its_spacing():
    ticks = [10.]; budget = BackchannelBudget(clock=lambda: ticks[0])
    assert budget.consume("owned", 1, "checking") == (True, "emitted")
    ticks[0] = 13
    assert budget.consume("owned", 1, "one_sec", follow_up=True) == (True, "emitted")
    stamp = budget.receipt("owned")
    assert budget.refund("owned", stamp) == (True, "refunded")
    assert budget.calls["owned"][0] == 1 and budget.calls["owned"][4] is None and budget.previous_phrase("owned") == "checking"
    ticks[0] = 13.5
    assert budget.consume("owned", 1, "one_sec", follow_up=True) == (False, "cooldown")  # spacing counts from the unplayed emission
    ticks[0] = 15.6
    assert budget.consume("owned", 1, "one_sec", follow_up=True) == (True, "emitted")


def test_follow_up_phrase_is_neutral_and_differs_from_the_turn_phrase():
    decision = BackchannelDecision(probability=1., content_kind="information_question", content_probability=1., content_confidence=1.)
    assert waiting_phrase(decision, 1) == "checking"
    assert waiting_phrase(decision, 1, "checking", neutral_only=True) == "moment"
    assert waiting_phrase(decision, 1, "moment", neutral_only=True) == "one_sec"
    assert waiting_phrase(decision, 2, "one_sec", neutral_only=True) == "just_a_sec"


@pytest.mark.parametrize("text", ["Yes, that's correct.", "That's right.", "Correct.", "Sounds good.", "Yes, that works.", "Confirm.",
                                  "Okay, go ahead.", "Yes, go ahead and book it.", "Please confirm the appointment.", "Yes, confirm it, please.",
                                  "Yes", "Book it.", "Yes, please book it.", "Please confirm the booking."])
def test_plain_unconditional_readback_approvals_qualify_for_the_neutral_confirmation_hold(text):
    state, reason = waiting_state(messages(text, READBACK), CONFIRM)
    assert state is not None and state["neutral_confirmation"] is True, reason
    assert "555" not in repr(state) and "Jamie" not in repr(state)


@pytest.mark.parametrize("text", ["Yes, but can we do 3 PM?", "Yes, and add a tire rotation.", "Correct, if it's free.", "Yes, yes, book it.",
                                  "No, that's wrong.", "Go ahead and cancel it.", "Confirm the cancellation.", "That's correct for Tuesday.",
                                  "Sounds good, call me later.", "Actually, yes."])
def test_approvals_with_any_added_clause_stay_outside_the_confirmation_hold(text):
    assert waiting_state(messages(text, READBACK), CONFIRM)[0] is None


def blocked_tool_stream(release):
    class Blocked(httpx.AsyncByteStream):
        async def __aiter__(self):
            await release.wait()
            yield tool_stream()
    return Blocked()


def test_lookup_turn_receives_a_spaced_neutral_follow_up_cue_while_its_tool_result_is_answered(tmp_path, monkeypatch):
    async def scenario():
        tool_release, answer_release, cue = asyncio.Event(), asyncio.Event(), asyncio.Event()
        count = 0
        def handler(request):
            nonlocal count
            count += 1
            if count == 1:
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=blocked_tool_stream(tool_release))
            return streaming(answer_release)(request)
        app, gateway, _ = setup(tmp_path, handler)
        enable(gateway, monkeypatch)
        clock = clocked_budget(gateway)
        gateway.diagnostics = Diagnostics()
        pending = None
        try:
            pending = asyncio.create_task(observed_post(app, request_body(), cue_observer(cue)))
            await asyncio.wait_for(cue.wait(), 3); tool_release.set()
            tool = await asyncio.wait_for(pending, 3)
            assert tool.status_code == 200 and "get_appointment" in tool.text and tool.text.count("<flush />") == 1
            first = gateway.latency_snapshot("call-a")[-1]
            assert first["backchannel"]["reason"] == "emitted" and first["backchannel"]["follow_up"] is False
            assert first["backchannel"]["phrase_id"] == "moment" and first["milestones_ms"]["tool_emitted"] is not None
            gateway.observations.record("sid-a", "call-a", "tool-one", {"ok": True, "appointment": None})
            cue.clear(); clock[0] += 3
            pending = asyncio.create_task(observed_post(app, continuation(), cue_observer(cue)))
            await asyncio.wait_for(cue.wait(), 3); answer_release.set()
            result = await asyncio.wait_for(pending, 3)
            assert speech(result.text) == PHRASES["one_sec"] + " <flush />Exact primary answer."
            second = gateway.latency_snapshot("call-a")[-1]
            assert second["backchannel"]["reason"] == "emitted" and second["backchannel"]["follow_up"] is True
            assert second["backchannel"]["phrase_id"] == "one_sec" and "refunded" not in second["backchannel"]
            emitted = [event for event in gateway.diagnostics.events if event[1] == "backchannel.emitted"]
            assert [event[3]["backchannel_follow_up"] for event in emitted] == [False, True]
            assert [event[3]["backchannel_phrase"] for event in emitted] == ["moment", "one_sec"]
            assert gateway.backchannel_budget.calls["call-a"][0] == 1 and gateway.backchannel_budget.calls["call-a"][4] == 1
            assert count == 2 and not gateway.active and not gateway.owners
        finally:
            tool_release.set(); answer_release.set()
            if pending is not None:await asyncio.gather(pending, return_exceptions=True)
            await gateway.close()
    asyncio.run(scenario())


def test_follow_up_evidence_is_content_free_and_counted_by_the_evaluator():
    cue = {"phrase_id": "one_sec", "reason": "emitted", "emitted_ms": 610, "follow_up": True, "text": "PRIVATE"}
    safe = safe_backchannel_timing(cue)
    assert safe["follow_up"] is True and safe["emitted_ms"] == 610 and "PRIVATE" not in repr(safe)
    assert safe_backchannel_timing(safe) == safe and "follow_up" not in safe_backchannel_timing({"reason": "emitted", "emitted_ms": 550})
    base = candidate_timings()[0]
    rows = [base, {**base, "backchannel": {**base["backchannel"], "follow_up": True}}]
    metrics = latency({}, rows)
    assert metrics["server_acknowledgments_emitted"] == 2 and metrics["server_follow_up_acknowledgments"] == 1
    assert "server_follow_up_acknowledgments" not in latency({}, candidate_timings())
    attributes = safe_attributes({"backchannel_follow_up": True, "backchannel_reason": "emitted", "backchannel_note": "PRIVATE"})
    assert attributes["backchannel_follow_up"] is True and "PRIVATE" not in repr(attributes)
