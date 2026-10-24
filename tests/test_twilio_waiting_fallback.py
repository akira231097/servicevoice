from dataclasses import replace
import asyncio

from app.backchannel_policy import BackchannelDecision, guarded_waiting_fallback
from app.listening_cues import listening_gate, substantive_assistant_text
from test_listening_cues import setup, SAFE, TEXT, drain


def test_uncertain_fallback_is_not_a_provider_error_or_sensitive_action_override():
    decision = BackchannelDecision(reason="classification_uncertain", probability=.64,
        language_choice="cache_compatible", language_probability=.995,
        content_kind="ordinary_detail", content_probability=.95)
    state = {"neutral_confirmation": False, "requested_name": False}
    assert guarded_waiting_fallback(decision, state, SAFE)
    for reason in ("provider_unavailable", "classifier_silence", "invalid_response", "classification_deadline"):
        assert not guarded_waiting_fallback(replace(decision, reason=reason), state, SAFE)
    for field in ("active_confirmation", "has_pending_proposal", "has_pending_operation", "ended"):
        assert not guarded_waiting_fallback(decision, state, {**SAFE, field: True})
    assert not guarded_waiting_fallback(decision, {**state, "requested_name": True}, SAFE)
    assert not guarded_waiting_fallback(replace(decision, content_kind="other"), state, SAFE)


def test_fallback_needs_matching_recent_final_and_real_caller_stop():
    async def run():
        c, clock, _, _, _, event, _ = setup()
        event("speech-update", "user", status="stopped")
        clock.now += .8
        assert not c.fallback_ready("fixture-call", "session", TEXT)  # quiet, no words
        event("transcript", "user", transcriptType="partial", transcript=TEXT)
        clock.now += .8
        assert not c.fallback_ready("fixture-call", "session", TEXT)  # unfinished ASR
        event("transcript", "user", transcriptType="final", transcript=TEXT)
        assert c.fallback_ready("fixture-call", "session", TEXT)
        assert not c.fallback_ready("fixture-call", "session", "a different request")
        clock.now += 4
        assert not c.fallback_ready("fixture-call", "session", TEXT)  # idle silence
        await c.close()
    asyncio.run(run())


def test_interrupted_waiting_cue_does_not_reopen_budget_even_with_split_asr_turn():
    async def run():
        c, clock, _, _, _, event, _ = setup()
        event("speech-update", "user", status="stopped")
        clock.now += .8
        assert c.commit_waiting("fixture-call", "session")
        event("speech-update", "assistant", status="started")
        for text in ("Just", "Just a", "Just a sec."):
            clock.now += .05
            event("transcript", "assistant", transcriptType="partial", transcript=text)
        event("user-interrupted")
        clock.now += .1
        event("speech-update", "user", status="started")
        event("speech-update", "assistant", status="stopped")
        clock.now += 2.3
        event("speech-update", "user", status="stopped")
        clock.now += .8
        assert c.waiting_status("fixture-call", "session")[1] == "turn_already_acknowledged"
        assert not c.commit_waiting("fixture-call", "session")
        event("transcript", "assistant", transcriptType="final", transcript="We can inspect your brakes.")
        clock.now += .1
        event("speech-update", "user", status="started")
        clock.now += 1
        event("speech-update", "user", status="stopped")
        clock.now += .8
        assert c.commit_waiting("fixture-call", "session")
        await c.close()
    asyncio.run(run())


def test_partial_cue_prefix_is_not_a_substantive_answer():
    for text in ("One", "One mo", "ONE MOMENT", "Just a sec", "One moment. One sec."):
        assert not substantive_assistant_text(text)
    assert substantive_assistant_text("One moment. We can inspect that.")


def test_current_clause_releases_ordinary_words_but_keeps_real_vetoes():
    for text in ("The brakes squeal really loudly when I stop", "Actually my car rattles in the morning",
            "Can you help? My engine rattles loudly every morning when I start the car.",
            "My name is Alex. The steering wheel shakes when I drive on the highway."):
        assert listening_gate(text, "Tell me more.", SAFE) == "eligible"
    for text in ("Stop talking. My engine rattles loudly every morning when I start the car.",
            "My phone number is five one two. And it", "Cancel the appointment. My car rattles every morning."):
        assert listening_gate(text, "Tell me more.", SAFE) != "eligible"


def test_listening_opportunity_in_short_pause_but_not_idle_silence():
    async def run():
        c, clock, _, sent, _, event, _ = setup()
        # Move the description just before warm-up; pause opens the next chance.
        clock.now = 2.9
        event("transcript", "user", transcriptType="partial", transcript=TEXT)
        clock.now = 3
        event("speech-update", "user", status="stopped")
        clock.now = 3.4
        await drain(c)
        assert len(sent) == 1
        clock.now += 6
        assert not c.listening_window(c.calls["fixture-call"])
        await c.close()
    asyncio.run(run())


def test_fallback_stream_is_neutral_and_only_emits_with_completed_turn_evidence(tmp_path, monkeypatch):
    async def run():
        import httpx
        from test_gemini_gateway import setup as gateway_setup, request_body, frame
        from test_backchannel_gateway import enable, observed_post, DONE
        from test_pretool_speech_gate import speech
        release, cue = asyncio.Event(), asyncio.Event()
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                await release.wait()
                yield frame({"content": "We can inspect that."}); yield frame(reason="stop"); yield DONE
        app, gateway, _ = gateway_setup(tmp_path, lambda r:httpx.Response(200,
            headers={"content-type":"text/event-stream"},stream=Stream()))
        enable(gateway, monkeypatch)
        config = gateway.config_loader()
        config["SERVICEVOICE_TWILIO_WAITING_FALLBACK"] = "on"
        gateway.config_loader = lambda: config
        gateway.waiting_cue_assistant = "our-assistant"
        gateway.waiting_cue_state = lambda *args: ("ticket", "eligible")
        gateway.waiting_cue_commit = lambda *args: True
        async def choose(*args, **kwargs):
            return BackchannelDecision(reason="classification_uncertain", probability=.64,
                language_choice="cache_compatible", language_probability=.995,
                content_kind="ordinary_detail", content_probability=.95)
        monkeypatch.setattr(gateway.backchannel_policy, "choose", choose)
        gateway.waiting_fallback_ready = lambda *args: True
        task = asyncio.create_task(observed_post(app, request_body(), lambda part:cue.set() if b"<flush />" in part else None))
        try:
            await asyncio.wait_for(cue.wait(), 3)
            release.set(); response = await asyncio.wait_for(task, 3)
            assert speech(response.text) == "One sec. <flush />We can inspect that."
            assert gateway.latency_snapshot("call-a")[-1]["backchannel"]["guarded_fallback"] is True
        finally:
            release.set()
            if not task.done(): task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await gateway.close()
    asyncio.run(run())
