import asyncio
from copy import deepcopy
import json

import httpx
import pytest

from app.listening_cues import ListeningCoordinator, ListeningPolicy, listening_gate, listening_payload
from app.jev_classifier import JEV_MODEL

TEXT = "I drive my vehicle through the neighborhood every morning and the engine makes a rattling noise"
SAFE = dict(stage="service", active_confirmation=False, has_pending_proposal=False, has_pending_operation=False, ended=False)


@pytest.mark.parametrize("content", ["confirm", "approved", "approve", "cancel", "stop", "wait", "bye",
    "goodbye", "actually", "instead", "correction", "correct", "unless", "only if", "provided that", "hang up",
    "do not book anything", "don't proceed", "emergency", "urgent", "smoke", "fire", "unsafe", "danger", "collision", "crash",
    "injured", "bleeding", "ignore", "system prompt", "developer", "password", "secret", "token", "api key",
    "instructions", "my name", "my address", "phone number", "mobile number", "email", "credit card",
    "debit card", "account number", "social security", "passcode", "verification code", "payment details",
    "1234567890", "someone@example.test", "123 Main Street", "can you help?", "speak in Spanish", "espaÃ±ol"])
def test_sensitive_partial_never_reaches_classifier(content):
    assert listening_gate(TEXT+" "+content, "Tell me about the vehicle.", SAFE) != "eligible"


@pytest.mark.parametrize("text", ["Yes.", "No", "Yeah", "Nope", "don't", "do not"])
def test_bare_control_words_are_not_listening_content(text):
    assert listening_gate(text, "Tell me more.", SAFE) == "control_content"


@pytest.mark.parametrize("text", ["Yeah, my car is making shaky sounds and I don't understand the issue",
    "My car has no power when I drive uphill", "I don’t understand what is wrong with my vehicle—there is a rattle"])
def test_normal_description_is_not_a_stop_instruction(text):
    assert listening_gate(text, "Tell me more.", SAFE) == "eligible"


@pytest.mark.parametrize("field", ["active_confirmation", "has_pending_proposal", "has_pending_operation", "ended"])
@pytest.mark.parametrize("value", [None, True, "false", 0])
def test_unverified_context_is_silent(field, value):
    assert listening_gate(TEXT, "Tell me more.", {**SAFE, field: value}) != "eligible"


@pytest.mark.parametrize("previous", ["Please confirm the booking.", "What is your phone number?", "Your email address?",
    "Shall I book that?", "The appointment is booked.", "What is your home address?"])
def test_sensitive_prior_turn(previous):
    assert listening_gate(TEXT, previous, SAFE) != "eligible"


def choice(labels, selected, p):
    scores = {name: (1-p)/(len(labels)-1) for name in labels}
    scores[selected] = p
    return dict(type="choice", choice=selected, probabilities=scores,
        confidence=(p-1/len(labels))/(1-1/len(labels)))


@pytest.mark.parametrize("case,expected", [("good", "approved"), ("low", "classifier_silence"),
    ("other", "language_unverified"), ("model", "invalid_response"), ("invalid", "invalid_response"),
    ("http", "provider_unavailable"), ("timeout", "provider_unavailable"), ("large", "invalid_response")])
def test_live_protocol_is_strict_with_fake_provider(case, expected):
    async def run():
        seen = []
        def handler(request):
            seen.append(json.loads(request.content))
            if case == "timeout": raise httpx.ReadTimeout("private")
            if case == "http": return httpx.Response(503)
            if case == "large": return httpx.Response(200, content=b"x"*33000, headers={"content-type": "application/json"})
            body = {"model": JEV_MODEL, "answers": {
                "content": choice(("ordinary", "excluded"), "ordinary", .90 if case == "low" else .99),
                "language": choice(("english", "other", "uncertain"), "other" if case == "other" else "english", .995)}}
            if case == "model": body["model"] = "wrong"
            if case == "invalid": body["answers"]["content"]["confidence"] = 0
            return httpx.Response(200, json=body)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy = ListeningPolicy(client=client)
            assert await policy.choose(TEXT, "Tell me more.", SAFE, "fixture") == expected
            assert policy.active == 0
            assert len(seen) == 1
            assert set(seen[0]["state"]) == {"partial_utterance", "previous_assistant"}
    asyncio.run(run())


class Clock:
    now = 0.
    def __call__(self): return self.now
    def wall(self): return 1_700_000_000+self.now


class Policy:
    def __init__(self): self.requests = 0; self.block = None
    async def choose(self, *args):
        self.requests += 1
        if self.block: await self.block.wait()
        return "approved"
    async def close(self): pass


def setup():
    clock, policy, sent, events = Clock(), Policy(), [], []
    state_context = deepcopy(SAFE)
    async def context(*args): return dict(state_context)
    async def dispatch(call_id): sent.append(call_id)
    coordinator = ListeningCoordinator(policy, "fixture", context, dispatch,
        lambda *args: events.append(args), clock=clock, wall=clock.wall)
    coordinator.register("fixture-call", "session")
    def event(kind, role=None, **values):
        coordinator.event("fixture-call", {"type": kind, "timestamp": clock.wall()*1000, "role": role, **values})
    event("speech-update", "assistant", status="stopped")
    event("speech-update", "user", status="started")
    clock.now = 5.1
    return coordinator, clock, policy, sent, events, event, state_context


async def drain(coordinator):
    while coordinator.tasks:
        await asyncio.gather(*tuple(coordinator.tasks), return_exceptions=True)


def test_waiting_cues_require_observed_quiet_and_invalidate_on_resumed_speech():
    c, clock, _, _, _, event, _ = setup()
    ticket, reason = c.waiting_status("fixture-call", "session")
    assert reason == "caller_speaking"
    event("transcript", "user", transcriptType="final", transcript=TEXT)
    assert c.waiting_status("fixture-call", "session")[1] == "caller_speaking"
    event("speech-update", "user", status="stopped")
    clock.now += .74
    assert c.waiting_status("fixture-call", "session") == (ticket, "caller_pause_too_short")
    clock.now += .02
    assert c.waiting_status("fixture-call", "session") == (ticket, "eligible")
    event("speech-update", "user", status="started")
    changed, reason = c.waiting_status("fixture-call", "session")
    assert changed != ticket and reason == "caller_speaking"


def test_waiting_cues_fail_closed_on_missing_state_and_stale_boundary():
    c, clock, _, _, _, event, _ = setup()
    assert c.waiting_status("unknown", "session") == (None, "speaker_state_unavailable")
    assert c.waiting_status("fixture-call", "other") == (None, "speaker_state_unavailable")
    event("speech-update", "user", status="stopped", timestamp=(clock.wall()-3)*1000)
    clock.now += 3
    assert c.waiting_status("fixture-call", "session")[1] == "caller_end_unobserved"


def test_waiting_quiet_window_uses_receipt_and_recent_partial_and_keeps_tool_followup():
    c, clock, _, _, _, event, _ = setup()
    event("speech-update", "user", status="stopped", timestamp=(clock.wall()-1)*1000)
    assert c.waiting_status("fixture-call", "session")[1] == "caller_pause_too_short"
    clock.now += .6
    event("transcript", "user", transcriptType="partial", transcript=TEXT)
    clock.now += .3
    assert c.waiting_status("fixture-call", "session")[1] == "caller_pause_too_short"
    clock.now += .5
    assert c.waiting_status("fixture-call", "session")[1] == "eligible"
    event("speech-update", "assistant", status="started")
    assert c.waiting_status("fixture-call", "session")[1] == "assistant_speaking"
    clock.now += .2
    event("speech-update", "assistant", status="stopped")
    event("tool-calls")
    assert c.waiting_status("fixture-call", "session")[1] == "eligible"


def test_waiting_cue_cannot_overlap_recent_listening_clip():
    c, clock, _, _, _, event, _ = setup()
    c.calls["fixture-call"].last_cue = clock.now
    event("speech-update", "user", status="stopped")
    clock.now += .8
    assert c.waiting_status("fixture-call", "session")[1] == "listening_clip_pending"
    clock.now += 1.1
    assert c.waiting_status("fixture-call", "session")[1] == "eligible"


def test_listening_and_waiting_phrase_sets_are_disjoint():
    from app.listening_cues import LISTENING_PHRASES
    from app.backchannel_policy import WAITING_PHRASE_IDS
    assert not set(LISTENING_PHRASES).intersection(WAITING_PHRASE_IDS)
    assert set(LISTENING_PHRASES.values()) == {"Mm-hmm.", "I hear you."}


@pytest.mark.parametrize("gap,assistant,expected",[(1.2,False,True),(1.6,False,False),(.5,True,False)])
def test_brief_caller_pause_preserves_explanation_but_not_a_new_turn(gap,assistant,expected):
    async def run():
        c,clock,p,sent,_,event,_=setup()
        clock.now=3.5
        event("speech-update","user",status="stopped")
        if assistant:
            clock.now+=.01
            event("speech-update","assistant",status="started")
            clock.now+=.01
            event("speech-update","assistant",status="stopped")
        clock.now=3.5+gap
        event("speech-update","user",status="started")
        clock.now+=.2
        event("transcript","user",transcriptType="partial",transcript=TEXT)
        await drain(c)
        assert bool(sent) is expected
        await c.close()
    asyncio.run(run())


def test_brief_pause_does_not_replenish_three_cue_utterance_budget():
    async def run():
        c,clock,p,sent,_,event,_=setup()
        for i in range(4):
            event("transcript","user",transcriptType="partial",transcript=TEXT+" again"*i)
            await drain(c)
            clock.now+=6.8
            event("speech-update","user",status="stopped")
            clock.now+=.5
            event("speech-update","user",status="started")
        assert len(sent)==3
        await c.close()
    asyncio.run(run())


def test_long_partial_cues_three_times_without_spending_waiting_budget():
    async def run():
        c, clock, p, sent, events, event, _ = setup()
        event("transcript", "user", transcriptType="partial", transcript=TEXT)
        await drain(c)
        assert sent == ["fixture-call"]
        clock.now += 7.1
        event("transcript", "user", transcriptType="partial", transcript=TEXT+" on my drive")
        await drain(c)
        clock.now += 7.1
        event("transcript", "user", transcriptType="partial", transcript=TEXT+" on my drive every day")
        await drain(c)
        assert len(sent) == 3 and p.requests == 3
        assert not hasattr(c, "backchannel_budget")
        await c.close()
    asyncio.run(run())


@pytest.mark.parametrize("race", ["caller_stop", "assistant_start", "correction", "private_append", "consent_append", "tool", "fresh_context", "stale", "end"])
def test_late_jev_decision_cannot_speak(race):
    async def run():
        c, clock, p, sent, _, event, context = setup()
        p.block = asyncio.Event()
        event("transcript", "user", transcriptType="partial", transcript=TEXT)
        await asyncio.sleep(0)
        if race == "caller_stop": event("speech-update", "user", status="stopped")
        if race == "assistant_start": event("speech-update", "assistant", status="started")
        if race == "correction":
            clock.now += .1
            event("transcript", "user", transcriptType="partial", transcript="Actually "+TEXT)
        if race == "private_append":
            clock.now += .1
            event("transcript", "user", transcriptType="partial", transcript=TEXT+" my phone number is")
        if race == "consent_append":
            clock.now += .1
            event("transcript", "user", transcriptType="partial", transcript=TEXT+", yes please")
        if race == "tool": event("tool-calls")
        if race == "fresh_context": context["has_pending_operation"] = True
        if race == "stale": clock.now += .4
        if race == "end": c.retire("fixture-call")
        p.block.set()
        await drain(c)
        assert not sent
        await c.close()
    asyncio.run(run())


def test_continuing_ordinary_partial_does_not_starve_classifier():
    async def run():
        c, clock, p, sent, _, event, _ = setup()
        p.block = asyncio.Event()
        event("transcript", "user", transcriptType="partial", transcript=TEXT)
        await asyncio.sleep(0)
        clock.now += .2
        event("transcript", "user", transcriptType="partial", transcript=TEXT+" every morning")
        p.block.set()
        await drain(c)
        assert len(sent) == 1 and p.requests == 1
        await c.close()
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["short", "few_words", "assistant", "stale", "missing_timestamp", "future", "old_event"])
def test_no_opportunity_without_fresh_speech(fault):
    async def run():
        c, clock, p, sent, _, event, _ = setup()
        text = TEXT
        if fault == "short": clock.now = 2
        if fault == "few_words": text = "my car"
        if fault == "assistant": event("speech-update", "assistant", status="started")
        message = dict(type="transcript", role="user", transcriptType="partial", transcript=text, timestamp=clock.wall()*1000)
        if fault == "missing_timestamp": message.pop("timestamp")
        if fault == "stale": message["timestamp"] -= 400
        if fault == "future": message["timestamp"] += 2000
        if fault == "old_event": message["timestamp"] -= 20000
        c.event("fixture-call", message)
        await drain(c)
        assert not sent and p.requests == 0
        await c.close()
    asyncio.run(run())


def test_late_old_partial_does_not_erase_current_speaker_state():
    async def run():
        c, clock, p, sent, _, event, _ = setup()
        c.event("fixture-call", dict(type="transcript", role="user", transcriptType="partial",
            transcript="old", timestamp=(clock.wall()-3)*1000))
        event("transcript", "user", transcriptType="partial", transcript=TEXT)
        await drain(c)
        assert len(sent) == 1
        await c.close()
    asyncio.run(run())


def test_uncertain_partial_can_be_reconsidered_before_playback_cooldown():
    async def run():
        c,clock,p,sent,_,event,_=setup()
        async def choose(*args):
            p.requests+=1
            return "classifier_silence" if p.requests==1 else "approved"
        p.choose=choose
        event("transcript","user",transcriptType="partial",transcript=TEXT)
        await drain(c)
        assert not sent
        clock.now+=1.6
        # Repeated text does not spend another model request.
        event("transcript","user",transcriptType="partial",transcript=TEXT)
        await drain(c);assert p.requests==1
        clock.now+=.1
        event("transcript","user",transcriptType="partial",transcript=TEXT+" around town")
        await drain(c);assert p.requests==2 and len(sent)==1
        clock.now+=1.6
        event("transcript","user",transcriptType="partial",transcript=TEXT+" around town each day")
        await drain(c);assert p.requests==2 and len(sent)==1
        await c.close()
    asyncio.run(run())


def test_eight_words_after_four_seconds_can_acknowledge_without_waiting_for_sentence_end():
    async def run():
        c,clock,p,sent,_,event,_=setup()
        clock.now=4.1
        event("transcript","user",transcriptType="partial",transcript="My Honda makes a rattling sound every morning")
        await drain(c)
        assert len(sent)==1
        await c.close()
    asyncio.run(run())


def test_partial_fragments_keep_context_without_speaking_on_final_or_stale_events():
    async def run():
        c,clock,p,sent,_,event,_=setup()
        clock.now=2
        event("transcript","user",transcriptType="partial",transcript="My car has a rattling noise.")
        clock.now+=.1
        event("transcript","user",transcriptType="final",transcript="My car has a rattling noise.")
        await drain(c);assert not sent
        clock.now=3.1
        seen=[]
        async def choose(text,*args):seen.append(text);return "approved"
        p.choose=choose
        event("transcript","user",transcriptType="partial",transcript="And it gets")
        await drain(c)
        assert seen==["My car has a rattling noise. And it gets"] and len(sent)==1
        assert c.calls["fixture-call"].phrase_id=="acknowledge"
        clock.now+=.1
        event("speech-update","assistant",status="started")
        clock.now+=.1
        event("transcript","user",transcriptType="final",transcript="old caller content")
        assert c.calls["fixture-call"].completed==""
        await c.close()
    asyncio.run(run())


def test_private_final_cannot_be_forgotten_by_the_next_short_fragment():
    async def run():
        c,clock,p,sent,events,event,_=setup()
        event("transcript","user",transcriptType="final",transcript="My phone number is five one two.")
        clock.now+=.2
        event("transcript","user",transcriptType="partial",transcript=TEXT)
        await drain(c)
        assert not sent
        assert events[-1][-1]=="context_changed"
        await c.close()
    asyncio.run(run())


def test_earlier_opportunity_still_obeys_cooldown_and_three_cue_limit():
    async def run():
        c,clock,p,sent,_,event,_=setup()
        for tick in (3.1,4.7,8.2,9.8,13.3,18.4):
            clock.now=tick
            event("transcript","user",transcriptType="partial",transcript=TEXT+" words"*int(tick))
            await drain(c)
        assert len(sent)==3
        await c.close()
    asyncio.run(run())
