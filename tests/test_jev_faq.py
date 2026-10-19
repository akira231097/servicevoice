"""No-network first-turn FAQ routing, authority, fallback and lifecycle."""
import asyncio
import json
import threading

import httpx
import pytest

from app import jev_faq
from app.jev_classifier import JEV_MODEL
from test_receipt_narration import setup, post, text_of, issue


def classified(label="hours", language="english_only", probability=1.):
    scores = {name: 0. for name in ("hours", "location", "services", "advisor_capability", "other")}
    scores[label] = probability
    scores["other"] = 1-probability
    languages = {name: float(name == language) for name in ("english_only", "other_or_mixed", "uncertain")}
    return {"model": JEV_MODEL, "answers": {
        "intent": {"type": "choice", "choice": label, "probabilities": scores, "confidence": (probability-.2)/.8},
        "language": {"type": "choice", "choice": language, "probabilities": languages, "confidence": 1.}},
        "response_text": "UNTRUSTED freeform answer must never be spoken"}


def configure(tmp_path, monkeypatch, handler=None, text="What are your opening hours?"):
    env = setup(tmp_path, monkeypatch, "off")
    env.config.update(SERVICEVOICE_JEV_FAQ="on", TYPESAFE_API_KEY="synthetic-jev-key-A")
    env.history = [{"role": "assistant", "content": "PRIVATE-GREETING-CONTEXT"}, {"role": "user", "content": text}]
    env.jev_requests = []
    async def upstream(request):
        env.jev_requests.append(request)
        if handler:
            result = handler(request)
            return await result if hasattr(result, "__await__") else result
        return httpx.Response(200, json=classified())
    env.gateway.faq.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    env.gateway.faq.owns_client = True
    return env


@pytest.mark.parametrize("label,question,expected", [
    ("hours", "What are your opening hours?", "Monday–Friday, 8 AM–5 PM Central"),
    ("location", "Where are you located?", "no verified physical service address"),
    ("services", "Which services do you offer?", "Oil & filter service"),
    ("advisor_capability", "Can this demo transfer calls or provide callbacks?", "cannot transfer you or arrange a callback"),
])
def test_high_confidence_faq_uses_only_server_facts_without_effects(tmp_path, monkeypatch, label, question, expected):
    env = configure(tmp_path, monkeypatch, lambda _: httpx.Response(200, json=classified(label)), question)
    async def scenario():
        try:
            response = await post(env)
            assert response.status_code == 200 and expected in text_of(response)
            assert "servicevoice-jev-faq" in response.text and "UNTRUSTED" not in response.text
            assert len(env.jev_requests) == 1 and not env.sent
            payload = json.loads(env.jev_requests[0].content)
            assert payload["state"]["utterance"] == question
            assert all(value not in json.dumps(payload) for value in (env.sid, env.cid, "PRIVATE-GREETING", "synthetic-jev-key"))
            snapshot = env.store.snapshot(env.sid)
            assert not snapshot["events"] and snapshot["appointment"] is None and snapshot["proposal"] is None
            with env.store.connect() as db:
                assert db.execute("SELECT count(*) FROM handoffs").fetchone()[0] == 0
            [row] = env.gateway.latency_snapshot(env.cid)
            assert row["route"] == "jev_static_faq" and row["outcome"] == "completed"
            assert row["counts"]["jev_requests"] == 1 and row["counts"]["upstream_events"] == row["counts"]["tool_calls"] == 0
            assert row["jev_probability"] == row["jev_margin"] == row["jev_language_probability"] == 1.
            assert not env.gateway.active
        finally:await env.gateway.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("text", [
    "What are your hours and can you book me?", "Can you cancel my appointment?",
    "What services are available tomorrow?", "Can I speak to a human advisor?",
    "Can you transfer me to an advisor?", "My brakes are smoking, are you open?",
    "Where should I drive when the car is unsafe?", "What are your hours? తెలుగులో చెప్పండి",
    "I need a service visit.",
])
def test_actions_safety_multilingual_and_nonfaq_skip_classifier(tmp_path, monkeypatch, text):
    env = configure(tmp_path, monkeypatch, text=text)
    async def scenario():
        try:
            assert "Gemini fallback" in text_of(await post(env))
            assert not env.jev_requests and len(env.sent) == 1
            assert env.gateway.latency_snapshot(env.cid)[-1]["counts"]["jev_requests"] == 0
        finally:await env.gateway.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["off", "missing_key", "second_turn", "active_state", "forged_first_history"])
def test_configuration_history_and_durable_state_gate(tmp_path, monkeypatch, mode):
    env = configure(tmp_path, monkeypatch)
    async def scenario():
        try:
            if mode == "off":env.config["SERVICEVOICE_JEV_FAQ"] = "off"
            elif mode == "missing_key":env.config.pop("TYPESAFE_API_KEY")
            elif mode == "second_turn":env.history.insert(0, {"role": "user", "content": "Hello"})
            elif mode == "active_state":env.store.update(env.sid, {"stage": "confirmation"})
            else:
                await issue(env, "begin_service_request", {"service": "oil_change"})
                env.history = [{"role": "user", "content": "What are your hours?"}]
            response = await post(env, lambda body: body.update(SERVICEVOICE_JEV_FAQ="on", stage="service"))
            assert "Gemini fallback" in text_of(response)
            assert not env.jev_requests and len(env.sent) == 1
        finally:await env.gateway.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["uncertain", "mixed", "http_error", "timeout", "malformed"])
def test_unknown_errors_and_deadline_fall_back_once(tmp_path, monkeypatch, mode):
    async def handler(_):
        if mode == "timeout":await asyncio.sleep(10)
        if mode == "http_error":return httpx.Response(429, text="PRIVATE provider error")
        if mode == "malformed":return httpx.Response(200, json={"PRIVATE": "bad"})
        return httpx.Response(200, json=classified(language="other_or_mixed" if mode == "mixed" else "english_only",
                                                 probability=.98 if mode == "uncertain" else 1.))
    env = configure(tmp_path, monkeypatch, handler)
    env.gateway.faq.timeout = .05
    async def scenario():
        try:
            response = await post(env)
            assert "Gemini fallback" in text_of(response)
            assert len(env.jev_requests) == len(env.sent) == 1
            [row] = env.gateway.latency_snapshot(env.cid)
            assert row["route"] == "gemini" and "PRIVATE" not in json.dumps(row)
            assert row["durations_ms"]["jev_route"] < 300
        finally:await env.gateway.close()
    asyncio.run(scenario())


def test_state_is_rechecked_after_classification(tmp_path, monkeypatch):
    env = None
    def handler(_):
        env.store.update(env.sid, {"stage": "slot"})
        return httpx.Response(200, json=classified())
    env = configure(tmp_path, monkeypatch, handler)
    async def scenario():
        try:
            assert "Gemini fallback" in text_of(await post(env))
            assert env.gateway.latency_snapshot(env.cid)[-1]["jev_reason"] == "state_changed"
        finally:await env.gateway.close()
    asyncio.run(scenario())


def test_reuses_client_rotates_keys_and_closes_without_exposing_them(tmp_path, monkeypatch):
    env = configure(tmp_path, monkeypatch)
    async def scenario():
        client = env.gateway.faq.client
        try:
            assert "Our service hours" in text_of(await post(env))
            env.config["TYPESAFE_API_KEY"] = "synthetic-jev-key-B"
            assert "Our service hours" in text_of(await post(env))
            assert env.gateway.faq.client is client
            assert [request.headers["authorization"] for request in env.jev_requests] == ["Bearer synthetic-jev-key-A", "Bearer synthetic-jev-key-B"]
            assert "synthetic-jev-key" not in json.dumps(env.gateway.latency_snapshot())
        finally:await env.gateway.close()
        assert client.is_closed
    asyncio.run(scenario())


def test_external_cancel_stops_classifier_and_releases_admission(tmp_path, monkeypatch):
    entered, stopped = asyncio.Event(), []
    async def handler(_):
        entered.set()
        try:await asyncio.Event().wait()
        finally:stopped.append(True)
    env = configure(tmp_path, monkeypatch, handler)
    async def scenario():
        try:
            task = asyncio.create_task(post(env))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
            assert stopped and not env.gateway.active and not env.sent
            [row] = env.gateway.latency_snapshot(env.cid)
            assert row["outcome"] == "cancelled" and row["counts"]["jev_requests"] == 1
        finally:await env.gateway.close()
    asyncio.run(scenario())


def test_state_reader_retains_capacity_after_timeout(tmp_path, monkeypatch):
    env = configure(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    def slow(*_):
        entered.set()
        assert release.wait(2)
        return True
    monkeypatch.setattr(jev_faq, "fresh_state", slow)
    env.gateway.faq.timeout, env.gateway.faq.capacity = .05, 1
    async def scenario():
        try:
            assert "Gemini fallback" in text_of(await post(env))
            assert entered.is_set() and len(env.gateway.faq.reads) == 1
            decision = await env.gateway.faq.choose(env.sid, env.cid, env.history, "test-key")
            assert decision.reason == "active_or_unverified_state" and not env.jev_requests
        finally:
            release.set()
            await asyncio.gather(*list(env.gateway.faq.reads))
            await env.gateway.close()
    asyncio.run(scenario())


def test_templates_follow_single_published_facts_provider(tmp_path, monkeypatch):
    env = configure(tmp_path, monkeypatch)
    original = jev_faq.dealership_facts
    monkeypatch.setattr(jev_faq, "dealership_facts", lambda date: {**original(date), "hours": "Updated verified test hours."})
    async def scenario():
        try:assert "Updated verified test hours." in text_of(await post(env))
        finally:await env.gateway.close()
    asyncio.run(scenario())
