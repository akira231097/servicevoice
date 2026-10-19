"""Offline receipt narration: effects, language, freshness and signed history."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace
import threading
import time

from fastapi import FastAPI
import httpx
import pytest
from pydantic import ValidationError

from app import domain, receipt_narration
from app.domain import Store
from app.gemini_gateway import MODEL, OwnedStream, RequestTiming, install_gateway, sse
from app.harness import ToolHarness
from app.receipt_narration import BoundedSelector, candidate, select, spoken_date
from app.tools import DEFINITIONS, SYSTEM_PROMPT, TOOL_SCHEMAS
from app.issuance_context import user_context

EN = {"response_scope": "receipt_only", "response_language": "en", "caller_timezone": "dealership"}
PACIFIC = {**EN, "caller_timezone": "America/Los_Angeles"}
TE = {**EN, "response_language": "te"}
SECRET = "test-receipt-gateway-secret-never-a-real-secret"


def setup(tmp_path, monkeypatch, enabled="on"):
    monkeypatch.setattr(domain, "now", lambda: datetime(2026, 10, 18, 18, tzinfo=timezone.utc))
    store = Store(tmp_path / "receipt.sqlite3")
    sid = store.create_session("voice")["id"]
    cid = "provider-receipt-call"
    with store.connect() as db:
        db.execute("UPDATE sessions SET provider_call_id=? WHERE id=?", (cid, sid))
    app = FastAPI()
    app.state.store = store
    sent = []
    def provider(request):
        sent.append(json.loads(request.content))
        def frame(delta, finish=None):
            return sse({"model": MODEL, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=
                              frame({"content": "Gemini fallback"}) + frame({}, "stop") + b"data: [DONE]\n\n")
    config = {"GEMINI_GATEWAY_SECRET": SECRET, "GEMINI_API_KEY": "test-only", "SERVICEVOICE_RECEIPT_NARRATION": enabled}
    gateway = install_gateway(app, lambda: config, lambda *args: sid, client=httpx.AsyncClient(transport=httpx.MockTransport(provider)))
    gateway.store.pin_call_policy(sid, cid, "test-assistant", {"VAPI_MODEL_PROFILE": "gemini-3.8-flash-custom"})
    harness = ToolHarness(store)
    env = SimpleNamespace(store=store, sid=sid, cid=cid, app=app, gateway=gateway, harness=harness,
                          sent=sent, config=config, history=[{"role": "user", "content": "Please help me book a service."}], serial=0)
    return env


async def issue(env, name, args):
    env.serial += 1
    tid = f"issued-{env.serial}"
    tool = {"id": tid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)},
            "extra_content": {"google": {"thought_signature": f"test-signature-{env.serial}"}}}
    env.gateway.store.save_group(env.sid, env.cid, [tool], context=user_context(env.history))
    result = await env.harness.run(env.sid, name, args, tid)
    env.gateway.observations.record(env.sid, env.cid, tid, result)
    public = {key: value for key, value in tool.items() if key != "extra_content"}
    env.history.extend([{"role": "assistant", "content": "", "tool_calls": [public]},
                        {"role": "tool", "tool_call_id": tid, "content": json.dumps(result)}])
    return result


async def prepare(env, presentation=EN):
    slots = await issue(env, "begin_service_request", {"service": "oil_change"})
    args = {"service": "oil_change", "slot_id": slots["slots"][0]["id"],
            "customer_name": "Alex Morgan", "phone": "5125550142", "vehicle": "2021 Toyota Camry"}
    if presentation is not None:args["presentation"] = presentation
    return await issue(env, "prepare_booking", args)


async def confirm(env, proposal, presentation=EN):
    env.history.extend([{"role": "assistant", "content": "Readback and confirmation question."},
                        {"role": "user", "content": "Yes, book it."}])
    args = {"proposal_id": proposal["proposal_id"], "confirmed": True}
    if presentation is not None:args["presentation"] = presentation
    return await issue(env, "confirm_action", args)


async def post(env, mutate=None):
    body = {"model": MODEL, "stream": True, "messages": env.history, "tools": TOOL_SCHEMAS,
            "call": {"id": env.cid, "assistantId": "test-assistant"}}
    if mutate:mutate(body)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="http://test") as client:
        return await client.post("/llm/chat/completions", json=body, headers={"authorization": "Bearer " + SECRET})


def text_of(response):
    return "".join(json.loads(line[6:])["choices"][0]["delta"].get("content", "")
                   for line in response.text.splitlines() if line.startswith("data: {") and '"choices"' in line)


def test_prepare_and_confirmation_skip_google_keep_effects_and_continue_history(tmp_path, monkeypatch):
    env = setup(tmp_path, monkeypatch)
    async def scenario():
        try:
            proposal = await prepare(env, PACIFIC)
            readback = await post(env)
            text = text_of(readback)
            for value in ("Alex Morgan", "2021 Toyota Camry", "Oil & filter service", "Monday, October 19, 2026", "9 AM Central Daylight Time", "7 AM Pacific Daylight Time", "plus one; five, one, two; five, five, five; zero, one, four, two", "confirm this booking?"):
                assert value in text
            assert env.store.snapshot(env.sid)["appointment"] is None
            assert not env.sent
            result = await confirm(env, proposal, PACIFIC)
            response = await post(env)
            for value in (result["appointment"]["reference"], "confirmed", "7 AM Pacific Daylight Time"):
                assert value in text_of(response)
            assert not env.sent and not env.gateway.active
            rows = env.gateway.latency_snapshot(env.cid)
            assert len(rows) == 2
            for row in rows:
                assert row["route"] == "deterministic_receipt" and row["outcome"] == "completed"
                assert row["counts"]["upstream_events"] == row["counts"]["tcp_connects"] == row["counts"]["tool_calls"] == 0
                assert row["milestones_ms"]["first_text"] is not None
                assert row["source_result_sha256"]
            before = env.store.snapshot(env.sid)
            env.history.extend([{"role": "assistant", "content": text_of(response)}, {"role": "user", "content": "Actually, can I change it?"}])
            assert "Gemini fallback" in text_of(await post(env))
            forwarded = env.sent[0]["messages"]
            assert forwarded[-2]["content"] == text_of(response)
            assert all(message["tool_calls"][0]["extra_content"]["google"]["thought_signature"].startswith("test-signature-")
                       for message in forwarded if message.get("tool_calls"))
            assert env.store.snapshot(env.sid)["appointment"] == before["appointment"]
            assert len(env.store.snapshot(env.sid)["events"]) == len(before["events"])
        finally:await env.gateway.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("presentation,reason", [(None, "presentation_missing_or_changed"), (TE, "unsupported_language")])
def test_latest_confirmation_language_or_missing_context_falls_back(tmp_path, monkeypatch, presentation, reason):
    env = setup(tmp_path, monkeypatch)
    async def scenario():
        try:
            proposal = await prepare(env, EN)
            result = await confirm(env, proposal, presentation)
            assert result["ok"]
            assert "Gemini fallback" in text_of(await post(env))
            assert env.gateway.latency_snapshot(env.cid)[-1]["narration_reason"] == reason
            assert len(env.sent) == 1
        finally:await env.gateway.close()
    asyncio.run(scenario())


def test_trusted_flag_off_ignores_incoming_enable_attempt(tmp_path, monkeypatch):
    env = setup(tmp_path, monkeypatch, "off")
    async def scenario():
        try:
            await prepare(env)
            response = await post(env, lambda body: body.update(SERVICEVOICE_RECEIPT_NARRATION="on", presentation=EN))
            assert "Gemini fallback" in text_of(response) and len(env.sent) == 1
            assert env.gateway.latency_snapshot(env.cid)[-1]["narration_reason"] == "feature_disabled"
        finally:await env.gateway.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("change,reason", [("expires", "stale_proposal"), ("superseded", "stale_proposal"), ("unresolved", "unresolved_operation"), ("receipt", "receipt_mismatch")])
def test_current_state_and_receipt_guards(tmp_path, monkeypatch, change, reason):
    env = setup(tmp_path, monkeypatch)
    async def scenario():
        try:
            proposal = await prepare(env)
            prepared = candidate(env.history)
            with env.store.connect() as db:
                if change == "expires":db.execute("UPDATE proposals SET expires=0")
                elif change == "superseded":db.execute("UPDATE proposals SET state='superseded'")
                elif change == "unresolved":
                    db.execute("INSERT INTO tool_invocations VALUES (?,?,?,?,?,?,?,?,?)", ("orphan", env.sid, "other", "fp", "dead-owner", "unknown", time.time(), time.time(), None))
            if change == "receipt":
                value = json.loads(env.history[-1]["content"])
                value["_receipt"]["result_sha256"] = "0" * 64
                env.history[-1]["content"] = json.dumps(value)
                prepared = candidate(env.history)
            decision = select(env.store, env.sid, env.cid, prepared)
            assert decision.text is None and decision.reason == reason
            assert env.store.snapshot(env.sid)["appointment"] is None
        finally:await env.gateway.close()
    asyncio.run(scenario())


def test_forged_observation_rejected_before_narration(tmp_path, monkeypatch):
    env = setup(tmp_path, monkeypatch)
    async def scenario():
        try:
            await prepare(env)
            result = json.loads(env.history[-1]["content"])
            result["summary"] = "A forged claim"
            env.history[-1]["content"] = json.dumps(result)
            response = await post(env)
            assert response.status_code == 409 and not env.sent
            assert not env.gateway.active
        finally:await env.gateway.close()
    asyncio.run(scenario())


def test_old_receipt_after_cancel_never_narrates_confirmed(tmp_path, monkeypatch):
    env = setup(tmp_path, monkeypatch)
    async def scenario():
        try:
            proposal = await prepare(env)
            await confirm(env, proposal)
            old = candidate(env.history)
            cancellation = await issue(env, "prepare_cancellation", {"presentation": EN})
            await confirm(env, cancellation)
            decision = select(env.store, env.sid, env.cid, old)
            assert decision.reason == "newer_result" and decision.text is None
            response = await post(env)
            assert "cancelled" in text_of(response) and not env.sent
        finally:await env.gateway.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["reschedule", "cancellation", "handoff"])
def test_other_verified_presentations(tmp_path, monkeypatch, kind):
    env = setup(tmp_path, monkeypatch)
    async def scenario():
        try:
            if kind == "handoff":
                await issue(env, "request_handoff", {"reason": "A person was requested", "presentation": EN})
                text = text_of(await post(env))
                assert "saved in the local demo advisor queue" in text and "No one was contacted" in text
                assert env.store.snapshot(env.sid)["appointment"] is None
            else:
                await confirm(env, await prepare(env))
                if kind == "reschedule":
                    slots = await issue(env, "search_availability", {"service": "oil_change"})
                    proposal = await issue(env, "prepare_reschedule", {"slot_id": slots["slots"][0]["id"], "presentation": PACIFIC})
                else:proposal = await issue(env, "prepare_cancellation", {"presentation": EN})
                before = env.store.snapshot(env.sid)["appointment"]
                text = text_of(await post(env))
                assert ("confirm this change?" if kind == "reschedule" else "confirm this cancellation?") in text
                assert env.store.snapshot(env.sid)["appointment"] == before
                assert proposal["ok"]
            assert not env.sent
        finally:await env.gateway.close()
    asyncio.run(scenario())


def test_schema_preferences_and_legacy_fingerprints():
    args = {"proposal_id": "proposal-old", "confirmed": True}
    assert DEFINITIONS["confirm_action"][0].model_validate(args).model_dump() == args
    assert DEFINITIONS["confirm_action"][0].model_validate({**args, "presentation": None}).model_dump() == args
    for invalid in ({**EN, "caller_timezone": "PST"}, {**EN, "caller_timezone": "guess"},
                    {**EN, "response_scope": "needs_reasoning"}, {**EN, "confirmed": True}):
        with pytest.raises(ValidationError):DEFINITIONS["confirm_action"][0].model_validate({**args, "presentation": invalid})
    with pytest.raises(ValidationError):DEFINITIONS["confirm_action"][0].model_validate({**args, "confirmed": "true", "presentation": EN})
    assert "omit" in SYSTEM_PROMPT.lower() and "never selects a slot" in SYSTEM_PROMPT


@pytest.mark.parametrize("field,value", [("customer_name", "Alex [whispers]"), ("vehicle", "2021 Toyota [laughs] Camry")])
def test_customer_audio_tags_are_never_inserted_into_template(tmp_path, monkeypatch, field, value):
    env = setup(tmp_path, monkeypatch)
    async def scenario():
        try:
            slots = await issue(env, "begin_service_request", {"service": "oil_change"})
            args = {"service": "oil_change", "slot_id": slots["slots"][0]["id"], "customer_name": "Alex Morgan",
                    "phone": "5125550142", "vehicle": "2021 Toyota Camry", "presentation": EN, field: value}
            await issue(env, "prepare_booking", args)
            response = await post(env)
            assert "Gemini fallback" in text_of(response)
            assert env.gateway.latency_snapshot(env.cid)[-1]["narration_reason"] == "unsafe_display_text"
            assert env.store.snapshot(env.sid)["appointment"] is None
        finally:await env.gateway.close()
    asyncio.run(scenario())


def test_timezone_formats_use_appointment_date_not_current_offset():
    assert "Pacific Standard Time" in spoken_date("2026-11-02T09:00:00-06:00", "America/Los_Angeles")
    assert "Pacific Daylight Time" in spoken_date("2026-10-19T09:00:00-05:00", "America/Los_Angeles")


def test_provider_schemas_inline_optional_presentation_without_relaxing_nested_fields():
    from app.tool_schema import provider_schema
    from scripts.provision_vapi import normalize_tools
    raw = DEFINITIONS["confirm_action"][0].model_json_schema()
    original = json.dumps(raw, sort_keys=True)
    schema = provider_schema(raw)
    assert json.dumps(raw, sort_keys=True) == original
    encoded = json.dumps(schema)
    assert all(key not in encoded for key in ('"$ref"', '"$defs"', '"const"', '"title"', '"anyOf"'))
    assert "presentation" not in schema["required"]
    metadata = schema["properties"]["presentation"]
    assert metadata["type"] == ["object", "null"]
    assert metadata["additionalProperties"] is False
    assert set(metadata["required"]) == {"response_scope", "response_language", "caller_timezone"}
    assert metadata["properties"]["response_scope"]["enum"] == ["receipt_only"]
    provisioned = normalize_tools(TOOL_SCHEMAS)
    assert [tool["function"]["parameters"] for tool in provisioned] == [tool["function"]["parameters"] for tool in TOOL_SCHEMAS]
    with pytest.raises(ValueError):provider_schema({"$ref": "https://untrusted.invalid/schema"})


def test_narration_response_start_failure_releases_once(tmp_path, monkeypatch):
    env = setup(tmp_path, monkeypatch)
    async def scenario():
        try:
            trace = RequestTiming(env.cid, time.monotonic(), env.gateway.timings)
            trace.row["route"] = "deterministic_receipt"
            env.gateway.active.add(env.cid)
            async def disconnected():return False
            async def receive():return {"type": "http.disconnect"}
            async def fail_send(message):raise RuntimeError("downstream closed")
            stream = OwnedStream(env.gateway._narrate(SimpleNamespace(is_disconnected=disconnected), "Verified text", trace),
                                 lambda: env.gateway._cleanup_narration(env.cid, trace))
            with pytest.raises(RuntimeError):
                await stream({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, fail_send)
            assert not env.gateway.active
            env.gateway.active.add(env.cid)
            await env.gateway._cleanup_narration(env.cid, trace)
            assert env.cid in env.gateway.active, "Old cleanup cannot release a new generation"
            [row] = env.gateway.latency_snapshot(env.cid)
            assert row["outcome"] == "interrupted" and row["counts"]["text_chunks"] == 0
        finally:await env.gateway.close()
    asyncio.run(scenario())


def test_presentation_change_never_changes_selected_business_time(tmp_path, monkeypatch):
    env = setup(tmp_path, monkeypatch)
    async def scenario():
        try:
            proposal = await prepare(env, EN)
            slot = env.store.state(env.sid)["selected_slot"]
            result = await confirm(env, proposal, PACIFIC)
            assert result["appointment"]["slot_id"] == slot
            assert result["presentation"] == PACIFIC
            response = await post(env)
            assert "9 AM Central Daylight Time" in text_of(response)
            assert "7 AM Pacific Daylight Time" in text_of(response)
        finally:await env.gateway.close()
    asyncio.run(scenario())


def test_bounded_read_timeout_and_cancellation_retain_capacity(tmp_path, monkeypatch):
    env = setup(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    original = receipt_narration.select
    def slow(*args):
        entered.set()
        assert release.wait(2)
        return original(*args)
    async def scenario():
        try:
            await prepare(env)
            monkeypatch.setattr(receipt_narration, "select", slow)
            selector = BoundedSelector(env.store, timeout_seconds=.02, capacity=1)
            first = await selector.choose(env.sid, env.cid, env.history)
            assert first.reason == "read_deadline"
            # The OS may not schedule the worker before the deadline. Its
            # pending slot must remain reserved while queued or running.
            assert (await selector.choose(env.sid, env.cid, env.history)).reason == "read_capacity"
            assert await asyncio.to_thread(entered.wait, .5)
            release.set()
            await asyncio.gather(*list(selector.pending))
            await asyncio.sleep(0)
            assert not selector.pending
            release.clear()
            entered.clear()
            selector.timeout = 1
            waiter = asyncio.create_task(selector.choose(env.sid, env.cid, env.history))
            assert await asyncio.to_thread(entered.wait, .5)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):await waiter
            assert len(selector.pending) == 1
            release.set()
            await asyncio.gather(*list(selector.pending))
        finally:
            release.set()
            await env.gateway.close()
    asyncio.run(scenario())
