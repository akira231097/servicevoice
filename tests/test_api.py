"""HTTP authorization and webhook-contract tests; credentials and calls are synthetic."""
import importlib
import asyncio
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone

import dotenv
import pytest
import httpx
from fastapi.testclient import TestClient

from app import domain

TEST_SECRET = "test-only-webhook-secret-not-a-real-credential"
ASSISTANT = "test-assistant"


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Prevent dotenv from opening any local secret file, including at module import.
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setattr(dotenv, "dotenv_values", lambda *args, **kwargs: {})
    values = {
        "SERVICEVOICE_DB": str(tmp_path / "import.sqlite3"),
        "VAPI_PUBLIC_KEY": "test-public-key",
        "VAPI_ASSISTANT_ID": ASSISTANT,
        "VAPI_WEBHOOK_SECRET": TEST_SECRET,
        "VAPI_SERVER_CREDENTIAL_ID": "test-server-credential-id",
        "PUBLIC_BASE_URL": "https://test-webhook.example.invalid",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(domain, "now", lambda: datetime(2026, 10, 17, 18, tzinfo=timezone.utc))
    module = importlib.import_module("app.main")
    app = module.create_app(tmp_path / "test.sqlite3")
    with TestClient(app) as test_client:
        yield test_client


def new_session(client, mode="rehearsal"):
    response = client.post("/api/sessions", json={"mode": mode})
    assert response.status_code == 200, response.text
    return response.json()


def headers(session):
    return {"x-session-token": session["token"]}


def tool(client, session, name, arguments=None, call_id="test-tool"):
    return client.post(f"/api/sessions/{session['id']}/tools", headers=headers(session), json={
        "name": name, "arguments": arguments or {}, "call_id": call_id,
    })


def payload(session=None, call_id="provider-call-1", event_type="status-update", **extra):
    call = {"id": call_id, "assistantId": ASSISTANT}
    if session:
        call["assistantOverrides"] = {"variableValues": {
            "session_id": session["id"], "session_token": session["token"],
        }}
    return {"message": {"type": event_type, "call": call, **extra}}


def webhook(client, body, authorized=True, **headers_extra):
    request_headers = {"host": "test-webhook.example.invalid", **headers_extra}
    if authorized:
        request_headers["authorization"] = "Bearer " + TEST_SECRET
    return client.post("/webhooks/vapi", headers=request_headers, json=body)


def webhook_tool(client, name, arguments, call_id="tool-1", session=None):
    response = webhook(client, payload(session, event_type="tool-calls", toolCallList=[{
        "id": call_id, "function": {"name": name, "arguments": arguments},
    }]))
    assert response.status_code == 200, response.text
    item = response.json()["results"][0]
    assert item["toolCallId"] == call_id
    return json.loads(item["result"])


def test_session_requires_its_own_token_for_reads_and_mutations(client):
    owner = new_session(client)
    other = new_session(client)
    endpoint = f"/api/sessions/{owner['id']}"
    assert client.get(endpoint).status_code == 401
    assert client.get(endpoint, headers=headers(other)).status_code == 401
    assert client.get(endpoint, headers=headers(owner)).status_code == 200
    denied = client.post(endpoint + "/tools", headers=headers(other), json={
        "name": "get_appointment", "arguments": {}, "call_id": "attempt",
    })
    assert denied.status_code == 401


def test_public_host_cannot_read_or_mutate_operator_console(client):
    for path in ("/", "/api/config", "/health", "/api/docs", "/static/index.html"):
        assert client.get(path, headers={"host": "public.example.invalid"}).status_code == 403
    assert client.post("/api/sessions", headers={"host": "public.example.invalid"}, json={}).status_code == 403


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "public.example.invalid"])
def test_separate_public_listener_has_no_console_even_with_spoofed_host(client, host):
    module = importlib.import_module("app.main")
    public = module.create_public_app(client.app)
    with TestClient(public) as remote:
        for path in ("/", "/api/config", "/health", "/api/docs", "/docs", "/openapi.json", "/static/index.html"):
            assert remote.get(path, headers={"host": host}).status_code == 404
        assert remote.post("/api/sessions", headers={"host": host}, json={}).status_code == 404
        assert remote.post("/webhooks/vapi", headers={"host": host}, json={}).status_code == 401


def test_separate_public_listener_accepts_only_authenticated_known_call(client):
    module = importlib.import_module("app.main")
    live = new_session(client, "live")
    with TestClient(module.create_public_app(client.app)) as remote:
        response = remote.post("/webhooks/vapi", headers={
            "host": "public.example.invalid", "authorization": "Bearer " + TEST_SECRET,
        }, json=payload(live))
        assert response.status_code == 200
        assert response.json() == {"received": True}


def test_cross_origin_operator_mutation_is_rejected(client):
    response = client.post("/api/sessions", headers={"origin": "https://attacker.example.invalid"}, json={})
    assert response.status_code == 403


def test_public_configuration_never_returns_webhook_secret(client):
    response = client.get("/api/config")
    assert response.status_code == 200
    assert response.json()["live_voice_available"] is True
    assert TEST_SECRET not in response.text
    assert "test-server-credential-id" not in response.text


def test_live_session_disabled_when_required_configuration_missing(client, monkeypatch):
    monkeypatch.delenv("VAPI_WEBHOOK_SECRET")
    assert client.get("/api/config").json()["live_voice_available"] is False
    assert client.post("/api/sessions", json={"mode": "live"}).status_code == 503
    assert new_session(client)["mode"] == "rehearsal"


def test_missing_and_extra_tool_envelope_fields_rejected(client):
    session = new_session(client)
    endpoint = f"/api/sessions/{session['id']}/tools"
    assert client.post(endpoint, headers=headers(session), json={"name": "get_appointment"}).status_code == 422
    response = client.post(endpoint, headers=headers(session), json={
        "name": "get_appointment", "arguments": {}, "call_id": "one", "tenant": "other",
    })
    assert response.status_code == 422


def test_wrong_tool_argument_type_is_rejected_without_action(client):
    session = new_session(client)
    response = tool(client, session, "confirm_action", {"proposal_id": "fake", "confirmed": "true"})
    assert response.status_code == 200
    assert response.json()["error"] == "invalid_arguments"
    assert client.get(f"/api/sessions/{session['id']}", headers=headers(session)).json()["appointment"] is None


def test_unsigned_and_wrong_secret_webhooks_rejected_before_binding(client):
    session = new_session(client, "live")
    body = payload(session)
    assert webhook(client, body, authorized=False).status_code == 401
    assert webhook(client, body, authorized=False, authorization="Bearer incorrect").status_code == 401
    with client.app.state.store.connect() as db:
        assert db.execute("SELECT provider_call_id FROM sessions WHERE id=?", (session["id"],)).fetchone()[0] is None


def test_non_ascii_authorization_header_is_rejected_without_server_error(client):
    response = client.post("/webhooks/vapi", headers=[(b"authorization", b"Bearer \xff")], json={})
    assert response.status_code == 401


def test_authenticated_webhook_binds_call_and_replays_exact_tool_result(client):
    session = new_session(client, "live")
    first = webhook_tool(client, "search_availability", {"service": "oil_change"}, "availability-1", session)
    replay = webhook_tool(client, "search_availability", {"service": "oil_change"}, "availability-1")
    assert first == replay
    assert first["slots"]
    with client.app.state.store.connect() as db:
        assert db.execute("SELECT provider_call_id FROM sessions WHERE id=?", (session["id"],)).fetchone()[0] == "provider-call-1"
        assert db.execute("SELECT count(*) FROM events WHERE session_id=?", (session["id"],)).fetchone()[0] == 1


def test_changed_arguments_on_duplicate_webhook_tool_id_conflict(client):
    session = new_session(client, "live")
    assert webhook_tool(client, "search_availability", {"service": "oil_change"}, "availability-1", session)["ok"]
    changed = webhook_tool(client, "search_availability", {"service": "brakes"}, "availability-1")
    assert changed["error"] == "idempotency_conflict"


def test_webhook_booking_duplicate_and_after_end_retry_do_not_create_another(client):
    session = new_session(client, "live")
    slots = webhook_tool(client, "search_availability", {"service": "oil_change"}, "search-1", session)["slots"]
    proposal = webhook_tool(client, "prepare_booking", {
        "service": "oil_change", "slot_id": slots[0]["id"], "customer_name": "Alex Morgan",
        "phone": "5125550142", "vehicle": "2021 Toyota Camry",
    }, "prepare-1")
    args = {"proposal_id": proposal["proposal_id"], "confirmed": True}
    result = webhook_tool(client, "confirm_action", args, "commit-1")
    assert result["ok"]
    assert webhook_tool(client, "confirm_action", args, "commit-1") == result
    assert webhook(client, payload(event_type="end-of-call-report")).status_code == 200
    assert webhook_tool(client, "confirm_action", args, "commit-1") == result
    assert webhook_tool(client, "prepare_cancellation", {}, "new-after-end")["error"] == "session_ended"
    with client.app.state.store.connect() as db:
        assert db.execute("SELECT count(*) FROM appointments").fetchone()[0] == 1


def test_observation_failure_after_booking_commit_recovers_on_exact_webhook_retry(client, monkeypatch):
    session = new_session(client, "live")
    slots = webhook_tool(client, "search_availability", {"service": "oil_change"}, "search-1", session)["slots"]
    proposal = webhook_tool(client, "prepare_booking", {
        "service": "oil_change", "slot_id": slots[0]["id"], "customer_name": "Alex Morgan",
        "phone": "5125550142", "vehicle": "2021 Toyota Camry",
    }, "prepare-1")
    args = {"proposal_id": proposal["proposal_id"], "confirmed": True}
    body = payload(event_type="tool-calls", toolCallList=[{
        "id": "commit-with-observation-failure", "function": {"name": "confirm_action", "arguments": args},
    }])
    observations = client.app.state.gemini_gateway.observations
    original_record = observations.record
    attempted_results = []

    def fail_first_record(sid, provider_call_id, tool_call_id, result):
        assert (sid, provider_call_id, tool_call_id) == (session["id"], "provider-call-1", "commit-with-observation-failure")
        attempted_results.append(result)
        if len(attempted_results) == 1:
            # Fault is after the effect AND its authoritative receipt committed.
            with client.app.state.store.connect() as db:
                assert db.execute("SELECT COUNT(*) FROM appointments WHERE session_id=? AND status='confirmed'", (sid,)).fetchone()[0] == 1
                assert db.execute("SELECT COUNT(*) FROM tool_results WHERE session_id=? AND call_id=?", (sid, tool_call_id)).fetchone()[0] == 1
            raise sqlite3.OperationalError("synthetic observation storage failure")
        return original_record(sid, provider_call_id, tool_call_id, result)

    monkeypatch.setattr(observations, "record", fail_first_record)
    failed = webhook(client, body)
    assert failed.status_code == 503
    assert "synthetic observation storage failure" not in failed.text
    with client.app.state.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM tool_observations WHERE tool_call_id='commit-with-observation-failure'").fetchone()[0] == 0

    retried = webhook(client, body)
    assert retried.status_code == 200, retried.text
    item = retried.json()["results"][0]
    result = json.loads(item["result"])
    assert result["ok"] is True
    assert result == attempted_results[0] == attempted_results[1]
    assert observations.lookup(session["id"], "provider-call-1", item["toolCallId"], item["result"]) == result
    with client.app.state.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM appointments WHERE session_id=?", (session["id"],)).fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM tool_results WHERE session_id=? AND call_id=?", (session["id"], item["toolCallId"])).fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM tool_observations WHERE session_id=? AND provider_call_id=? AND tool_call_id=?", (session["id"], "provider-call-1", item["toolCallId"])).fetchone()[0] == 1


def test_wrong_assistant_unknown_call_and_rehearsal_binding_are_rejected(client):
    live = new_session(client, "live")
    wrong = payload(live)
    wrong["message"]["call"]["assistantId"] = "unconfigured-assistant"
    assert webhook(client, wrong).status_code == 403
    assert webhook(client, payload()).status_code == 403
    assert webhook(client, payload(new_session(client))).status_code == 403


def test_call_binding_rejects_incorrect_session_token_and_second_call(client):
    live = new_session(client, "live")
    bad = payload({**live, "token": "incorrect"})
    assert webhook(client, bad).status_code == 401
    assert webhook(client, payload(live)).status_code == 200
    assert webhook(client, payload(live, call_id="second-call")).status_code == 409


def test_duplicate_final_transcript_events_stored_once(client):
    live = new_session(client, "live")
    event = payload(live, event_type="transcript", transcriptType="final", role="user", transcript="Oil change please", timestamp=123, turn=1)
    assert webhook(client, event).status_code == 200
    assert webhook(client, event).status_code == 200
    snapshot = client.get(f"/api/sessions/{live['id']}", headers=headers(live)).json()
    assert snapshot["transcript"] == [{"role": "user", "text": "Oil change please"}]


def test_ended_session_cannot_accept_new_local_mutation_but_can_read(client):
    session = new_session(client)
    endpoint = f"/api/sessions/{session['id']}"
    assert client.post(endpoint + "/end", headers=headers(session)).status_code == 200
    assert tool(client, session, "search_availability", {"service": "oil_change"}).status_code == 409
    assert client.get(endpoint, headers=headers(session)).json()["ended"] is True


@pytest.mark.parametrize("body", [[], {}, {"message": []}, {"message": {"call": {}}}])
def test_malformed_top_level_webhooks_return_client_error(client, body):
    assert webhook(client, body).status_code == 422


@pytest.mark.parametrize("field,value", [("call", []), ("call", "bad"), ("assistant", "bad")])
def test_malformed_nested_webhook_shapes_return_client_error(client, field, value):
    live = new_session(client, "live")
    body = payload(live)
    if field == "assistant":
        body["message"]["call"].pop("assistantId")
    body["message"][field] = value
    assert webhook(client, body).status_code == 422


def test_malformed_tool_function_shape_returns_client_error(client):
    live = new_session(client, "live")
    body = payload(live, event_type="tool-calls", toolCallList=[{"id": "one", "function": "bad"}])
    assert webhook(client, body).status_code == 422


@pytest.mark.parametrize("invalid_function", [
    {"name": [], "arguments": {}},
    {"name": {}, "arguments": {}},
    {"name": None, "arguments": {}},
    {"name": "get_appointment", "arguments": "["},
    {"name": "get_appointment", "arguments": []},
    {"name": "get_appointment", "arguments": {"unexpected": True}},
])
def test_entire_tool_batch_validated_before_booking_or_binding(client, invalid_function):
    live = new_session(client, "live")
    slots = tool(client, live, "search_availability", {"service": "oil_change"}, "search").json()["slots"]
    proposal = tool(client, live, "prepare_booking", {
        "service": "oil_change", "slot_id": slots[0]["id"], "customer_name": "Synthetic Test",
        "phone": "5125550100", "vehicle": "2021 Toyota Camry",
    }, "prepare").json()
    response = webhook(client, payload(live, event_type="tool-calls", toolCallList=[
        {"id": "would-book", "function": {"name": "confirm_action", "arguments": {
            "proposal_id": proposal["proposal_id"], "confirmed": True,
        }}},
        {"id": "malformed-second", "function": invalid_function},
    ]))
    assert response.status_code == 422
    with client.app.state.store.connect() as db:
        assert db.execute("SELECT count(*) FROM appointments").fetchone()[0] == 0
        assert db.execute("SELECT provider_call_id FROM sessions WHERE id=?", (live["id"],)).fetchone()[0] is None
        assert db.execute("SELECT count(*) FROM tool_invocations WHERE call_id='would-book'").fetchone()[0] == 0


def test_bound_call_expiry_cannot_bypass_local_session_lifetime(client):
    live = new_session(client, "live")
    assert webhook(client, payload(live)).status_code == 200
    with client.app.state.store.connect() as db:
        db.execute("UPDATE sessions SET created=? WHERE id=?", (time.time() - 4 * 3600 - 1, live["id"]))
    response = webhook(client, payload(event_type="tool-calls", toolCallList=[{
        "id": "expired-new-action", "function": {"name": "request_handoff", "arguments": {"reason": "Synthetic request"}},
    }]))
    assert response.status_code == 409
    assert response.json()["error"] == "session_expired"
    with client.app.state.store.connect() as db:
        assert db.execute("SELECT count(*) FROM handoffs").fetchone()[0] == 0


def test_bound_call_rejects_explicit_conflicting_session_capability(client):
    first = new_session(client, "live")
    other = new_session(client, "live")
    assert webhook(client, payload(first)).status_code == 200
    assert webhook(client, payload(other)).status_code == 403
    assert webhook(client, payload({**first, "token": "wrong"})).status_code == 401


def test_identical_final_utterances_without_provider_identity_are_not_collapsed(client):
    live = new_session(client, "live")
    event = payload(live, event_type="transcript", transcriptType="final", role="user", transcript="Yes")
    assert webhook(client, event).status_code == 200
    assert webhook(client, event).status_code == 200
    snapshot = client.get(f"/api/sessions/{live['id']}", headers=headers(live)).json()
    assert snapshot["transcript"] == [{"role": "user", "text": "Yes"}] * 2


def test_public_listener_caps_chunked_body_before_binding(client):
    module = importlib.import_module("app.main")
    public = module.create_public_app(client.app)
    consumed = []

    async def scenario():
        async def chunks():
            for index in range(6):
                consumed.append(index)
                yield b" " * 65536
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public), base_url="https://public.example.invalid") as remote:
            response = await remote.post("/webhooks/vapi", headers={"authorization": "Bearer " + TEST_SECRET}, content=chunks())
            assert response.status_code == 413
    asyncio.run(scenario())
    assert consumed == [0, 1, 2, 3, 4], "reader must stop before consuming the unbounded remainder"


def test_public_listener_body_deadline_bounds_slow_stream(client, monkeypatch):
    module = importlib.import_module("app.main")
    boundary = importlib.import_module("app.http_boundary")
    monkeypatch.setattr(boundary, "WEBHOOK_BODY_TIMEOUT_SECONDS", 0.03)
    public = module.create_public_app(client.app)

    async def scenario():
        async def chunks():
            yield b"{"
            await asyncio.sleep(0.3)
            yield b"}"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public), base_url="https://public.example.invalid") as remote:
            response = await remote.post("/webhooks/vapi", headers={"authorization": "Bearer " + TEST_SECRET}, content=chunks())
            assert response.status_code == 408
    asyncio.run(scenario())


def test_contended_transcript_write_does_not_block_async_request_loop(client):
    live = new_session(client, "live")
    assert webhook(client, payload(live)).status_code == 200
    store = client.app.state.store
    locked = threading.Event()
    release = threading.Event()

    def hold_writer():
        db = sqlite3.connect(store.path, isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE")
            locked.set()
            release.wait(2)
            db.rollback()
        finally:
            db.close()

    writer = threading.Thread(target=hold_writer)
    writer.start()
    assert locked.wait(1)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://testserver") as remote:
            body = payload(event_type="transcript", transcriptType="final", role="user", transcript="Synthetic contended write")
            request = asyncio.create_task(remote.post("/webhooks/vapi", headers={"authorization": "Bearer " + TEST_SECRET}, json=body))
            start = time.perf_counter()
            await asyncio.sleep(0.05)
            elapsed = time.perf_counter() - start
            release.set()
            assert elapsed < 0.5, "a SQLite write wait must not occupy the event-loop thread"
            assert (await request).status_code == 200
    try:
        asyncio.run(scenario())
    finally:
        release.set()
        writer.join(3)
