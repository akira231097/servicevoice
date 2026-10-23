import asyncio
from copy import deepcopy
import json

import httpx
import pytest

from app.listening_cues import ListeningCoordinator, ListeningPolicy, listening_gate, listening_payload
from app.jev_classifier import JEV_MODEL

TEXT = "I drive my vehicle through the neighborhood every morning and the engine makes a rattling noise"
SAFE = dict(stage="service", active_confirmation=False, has_pending_proposal=False, has_pending_operation=False, ended=False)


@pytest.mark.parametrize("content", ["yes", "no", "confirm", "approved", "approve", "cancel", "stop", "wait", "bye",
    "goodbye", "actually", "instead", "correction", "correct", "unless", "only if", "provided that", "hang up",
    "do not", "don't", "emergency", "urgent", "smoke", "fire", "unsafe", "danger", "collision", "crash",
    "injured", "bleeding", "ignore", "system prompt", "developer", "password", "secret", "token", "api key",
    "instructions", "my name", "my address", "phone number", "mobile number", "email", "credit card",
    "debit card", "account number", "social security", "passcode", "verification code", "payment details",
    "1234567890", "someone@example.test", "123 Main Street", "can you help?", "speak in Spanish", "español"])
def test_sensitive_partial_never_reaches_classifier(content):
    assert listening_gate(TEXT+" "+content, "Tell me about the vehicle.", SAFE) != "eligible"


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


def test_long_partial_cues_twice_without_spending_waiting_budget():
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
        assert len(sent) == 2 and p.requests == 2
        assert not hasattr(c, "backchannel_budget")
        await c.close()
    asyncio.run(run())


@pytest.mark.parametrize("race", ["caller_stop", "assistant_start", "correction", "private_append", "tool", "fresh_context", "stale", "end"])
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
