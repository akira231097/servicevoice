"""Offline webhook execution provenance; no provider calls or local secret reads."""
import importlib
import hashlib
import json
import sqlite3
import time

import dotenv
import httpx
from fastapi.testclient import TestClient
import pytest

from app.gemini_gateway import GatewayError, GatewayLimits, SignatureStore, canonical
from app.tools import TOOL_SCHEMAS


SECRET = "synthetic-webhook-secret-for-provenance"
ASSISTANT = "synthetic-assistant"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr(dotenv, "dotenv_values", lambda *a, **k: {})
    for name, value in {
        "SERVICEVOICE_DB": str(tmp_path / "import.sqlite3"),
        "VAPI_PUBLIC_KEY": "synthetic-public-key",
        "VAPI_ASSISTANT_ID": ASSISTANT,
        "VAPI_WEBHOOK_SECRET": SECRET,
        "VAPI_SERVER_CREDENTIAL_ID": "synthetic-credential",
        "PUBLIC_BASE_URL": "https://example.invalid",
        "VAPI_MODEL_PROFILE": "gemini-3.8-flash-custom",
        "GEMINI_API_KEY": "synthetic-google-key",
        "GEMINI_GATEWAY_SECRET": "synthetic-gateway-secret-at-least-32-characters",
    }.items():
        monkeypatch.setenv(name, value)
    module = importlib.import_module("app.main")
    with TestClient(module.create_app(tmp_path / "provenance.sqlite3")) as result:
        # These cases assert authorization/signatures, not disk latency. Keep
        # terminal results within the same disposable-fixture five-second
        # allowance used by the concurrent correctness exercise. Dedicated
        # harness deadline tests retain the production 1.5-second default.
        result.app.state.harness.timeout_seconds = 5
        yield result


def session(client, call_id="provider-call"):
    response = client.post("/api/sessions", json={"mode": "live"})
    assert response.status_code == 200, response.text
    value = response.json()
    value["call_id"] = call_id
    assert post(client, value).status_code == 200
    return value


def entry(tool_id="tool-one", name="get_dealership_info", args=None):
    return {"id": tool_id, "function": {"name": name, "arguments": {} if args is None else args}}


def post(client, value, calls=None, overrides=None):
    call = {"id": value["call_id"], "assistantId": ASSISTANT,
            "assistantOverrides": {"variableValues": {"session_id": value["id"], "session_token": value["token"]}}}
    if overrides:
        call["assistantOverrides"].update(overrides)
    message = {"type": "tool-calls" if calls is not None else "status-update", "call": call}
    if calls is not None:
        message["toolCallList"] = calls
    return client.post("/webhooks/vapi", json={"message": message}, headers={"authorization": "Bearer " + SECRET})


def issue(client, value, *calls):
    signed = []
    for item in calls:
        function = item["function"]
        arguments = function["arguments"]
        signed.append({"id": item["id"], "type": "function", "function": {
            "name": function["name"], "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments)},
            "extra_content": {"google": {"thought_signature": "synthetic-private-signature"}}})
    client.app.state.gemini_gateway.store.save_group(value["id"], value["call_id"], signed)


def results(response):
    assert response.status_code == 200, response.text
    return [json.loads(item["result"]) for item in response.json()["results"]]


def count(client, table):
    assert table in {"tool_invocations", "tool_results", "events", "appointments"}
    with client.app.state.store.connect() as db:
        return db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]


def test_unissued_tool_rejected_before_admission_or_observation(client):
    value = session(client)
    response = post(client, value, [entry(name="request_handoff", args={"reason": "Please call an advisor"})])
    assert response.status_code == 409 and response.json()["detail"] == "tool_not_issued"
    assert count(client, "tool_invocations") == count(client, "tool_results") == count(client, "events") == 0
    with client.app.state.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM tool_observations").fetchone()[0] == 0


def test_first_event_tool_call_is_protected_before_any_gateway_request(client):
    value = client.post("/api/sessions", json={"mode": "live"}).json()
    value["call_id"] = "first-event-call"
    assert post(client, value, [entry()]).status_code == 409
    assert count(client, "tool_invocations") == 0
    with client.app.state.store.connect() as db:
        assert db.execute("SELECT policy FROM provider_tool_policies WHERE provider_call_id=?", (value["call_id"],)).fetchone()[0] == "gemini-issued"


def test_entire_batch_preflight_prevents_valid_first_tool_effect(client):
    value = session(client)
    first = entry("first", "request_handoff", {"reason": "Need an advisor"})
    issue(client, value, first)
    response = post(client, value, [first, entry("unissued")])
    assert response.status_code == 409
    assert count(client, "tool_invocations") == count(client, "events") == 0


@pytest.mark.parametrize("change", ["tool_id", "name", "arguments", "call"])
def test_issued_identity_cannot_be_reused_for_a_different_command(client, change):
    value = session(client)
    original = entry("issued", "request_handoff", {"reason": "Original reason"})
    issue(client, value, original)
    attempted = entry("issued", "request_handoff", {"reason": "Original reason"})
    if change == "tool_id":
        attempted["id"] = "new-id"
    elif change == "name":
        attempted["function"] = {"name": "get_appointment", "arguments": {}}
    elif change == "arguments":
        attempted["function"]["arguments"]["reason"] = "Changed reason"
    else:
        value = session(client, "another-call")
    assert post(client, value, [attempted]).status_code == 409
    assert count(client, "tool_invocations") == 0


def test_json_formatting_and_pydantic_defaults_do_not_break_issued_tool(client):
    value = session(client)
    command = entry("search", "search_availability", '{ "service" : "oil_change" }')
    issue(client, value, command)
    result = results(post(client, value, [entry("search", "search_availability", {"service": "oil_change"})]))[0]
    assert result["ok"] and result["slots"]
    assert count(client, "tool_invocations") == 1


def test_original_arguments_checked_before_string_normalization(client):
    value = session(client)
    command = entry("handoff", "request_handoff", {"reason": "  Ask an advisor  "})
    issue(client, value, command)
    assert post(client, value, [entry("handoff", "request_handoff", {"reason": "Ask an advisor"})]).status_code == 409
    assert results(post(client, value, [command]))[0]["ok"]


def test_signed_group_can_arrive_individually_or_reordered(client):
    value = session(client)
    first, second = entry("one"), entry("two", "get_appointment")
    issue(client, value, first, second)
    assert all(result["ok"] for result in results(post(client, value, [second, first])))
    assert results(post(client, value, [first]))[0]["ok"]
    assert count(client, "tool_invocations") == 2


@pytest.mark.parametrize("signature_state", ["expired", "deleted"])
def test_exact_normalized_receipt_replay_survives_signature_collection_and_end(client, signature_state):
    value = session(client)
    command = entry("search", "search_availability", {"service": "oil_change"})
    issue(client, value, command)
    original = results(post(client, value, [command]))[0]
    with client.app.state.store.connect() as db:
        if signature_state == "expired":
            db.execute("UPDATE gemini_tool_signatures SET expires=?", (time.time() - 1,))
        else:
            db.execute("DELETE FROM gemini_tool_signatures")
        db.execute("UPDATE sessions SET ended=1 WHERE id=?", (value["id"],))
    normalized = entry("search", "search_availability", {"service": "oil_change", "date": None})
    assert results(post(client, value, [normalized]))[0] == original
    assert count(client, "tool_invocations") == count(client, "events") == 1
    assert post(client, value, [entry("search", "search_availability", {"service": "tires"})]).status_code == 409
    assert post(client, value, [entry("fresh")]).status_code == 409


def test_expired_never_admitted_command_cannot_start(client):
    value = session(client)
    command = entry("expired", "request_handoff", {"reason": "Do not start this"})
    issue(client, value, command)
    with client.app.state.store.connect() as db:
        db.execute("UPDATE gemini_tool_signatures SET expires=?", (time.time() - 1,))
    assert post(client, value, [command]).status_code == 409
    assert count(client, "tool_invocations") == 0


def test_admitted_unknown_operation_can_be_recovered_without_reexecution(client):
    value = session(client)
    command = entry("accepted", "request_handoff", {"reason": "Already admitted"})
    issue(client, value, command)
    normalized = command["function"]["arguments"]
    opid = "op_" + hashlib.sha256((value["id"] + "\0accepted").encode()).hexdigest()[:24]
    fingerprint = hashlib.sha256(canonical({"name": "request_handoff", "args": normalized}).encode()).hexdigest()
    admitted, _ = client.app.state.harness._register(value["id"], "request_handoff", normalized, "accepted", opid, fingerprint)
    assert admitted
    with client.app.state.store.connect() as db:
        db.execute("DELETE FROM gemini_tool_signatures")
        db.execute("UPDATE tool_invocations SET owner='prior-process'")
        db.execute("UPDATE sessions SET ended=1 WHERE id=?", (value["id"],))
    outcome = results(post(client, value, [command]))[0]
    assert outcome["error"] == "operation_unknown" and outcome["operation_id"] == opid
    assert count(client, "tool_invocations") == 1
    assert count(client, "events") == count(client, "tool_results") == 0


def test_get_operation_requires_issuance_and_remains_session_scoped(client):
    value = session(client)
    source = entry("source")
    issue(client, value, source)
    receipt = results(post(client, value, [source]))[0]
    status = entry("status", "get_operation", {"operation_id": receipt["_receipt"]["operation_id"]})
    assert post(client, value, [status]).status_code == 409
    issue(client, value, status)
    assert results(post(client, value, [status]))[0]["ok"]
    other = session(client, "other-call")
    issue(client, other, status)
    assert not results(post(client, other, [status]))[0]["ok"]


@pytest.mark.parametrize("profile", ["baseline", "luna"])
def test_native_profile_path_remains_available_and_pinned(client, monkeypatch, profile):
    monkeypatch.setenv("VAPI_MODEL_PROFILE", profile)
    native = session(client)
    monkeypatch.setenv("VAPI_MODEL_PROFILE", "gemini-3.8-flash-custom")
    assert results(post(client, native, [entry()]))[0]["ok"]
    protected = session(client, "custom-call")
    assert post(client, protected, [entry()]).status_code == 409


def test_custom_policy_cannot_be_downgraded_by_overrides_config_or_restart(client, monkeypatch):
    value = session(client)
    monkeypatch.setenv("VAPI_MODEL_PROFILE", "baseline")
    client.app.state.gemini_gateway.store = SignatureStore(client.app.state.store.path, GatewayLimits())
    response = post(client, value, [entry()], {"model": {"provider": "openai", "model": "gpt-4.1-mini"}})
    assert response.status_code == 409
    assert count(client, "tool_invocations") == 0


def test_policy_store_refuses_cross_session_or_assistant_binding(client):
    value = session(client)
    store = client.app.state.gemini_gateway.store
    for sid, call_id, assistant in [("wrong-session", value["call_id"], ASSISTANT), (value["id"], "other-call", ASSISTANT), (value["id"], value["call_id"], "other-assistant")]:
        with pytest.raises(GatewayError, match="tool_policy_binding"):
            store.pin_call_policy(sid, call_id, assistant, {"VAPI_MODEL_PROFILE": "baseline"})


def test_policy_lookup_failure_is_sanitized_and_does_not_admit_effect(client, monkeypatch):
    value = session(client)
    def fail(*args):
        raise sqlite3.OperationalError("synthetic-private-detail")
    monkeypatch.setattr(client.app.state.gemini_gateway.store, "authorize_tool_batch", fail)
    response = post(client, value, [entry()])
    assert response.status_code == 503 and "synthetic-private-detail" not in response.text
    assert count(client, "tool_invocations") == 0


@pytest.mark.parametrize("profile", [None, "baseline", "luna"])
def test_gateway_rejects_native_or_default_policy_before_google_contact(client, monkeypatch, profile):
    if profile is None:
        monkeypatch.delenv("VAPI_MODEL_PROFILE", raising=False)
        monkeypatch.delenv("VAPI_MODEL", raising=False)
    else:
        monkeypatch.setenv("VAPI_MODEL_PROFILE", profile)
    value = session(client)
    # Even later server configuration changes cannot silently upgrade a call.
    monkeypatch.setenv("VAPI_MODEL_PROFILE", "gemini-3.8-flash-custom")
    contacted = []
    async def forbidden_send(*args, **kwargs):
        contacted.append(True)
        raise AssertionError("Google must not be contacted")
    monkeypatch.setattr(client.app.state.gemini_gateway.client, "send", forbidden_send)
    response = client.post("/llm/chat/completions", headers={
        "authorization": "Bearer synthetic-gateway-secret-at-least-32-characters"}, json={
        "model": "gemini-3.8-flash", "stream": True,
        "messages": [{"role": "user", "content": "What are your hours?"}], "tools": TOOL_SCHEMAS,
        "call": {"id": value["call_id"], "assistantId": ASSISTANT}})
    assert response.status_code == 409 and response.json()["detail"] == "gateway_policy_mismatch"
    assert contacted == []
    # The native profile's normal provider-authenticated tools still work.
    assert results(post(client, value, [entry()]))[0]["ok"]


def test_custom_gateway_keeps_stronger_policy_after_local_profile_changes(client, monkeypatch):
    value = session(client)
    monkeypatch.setenv("VAPI_MODEL_PROFILE", "baseline")
    contacted = []
    async def provider_response(*args, **kwargs):
        contacted.append(True)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=(
            'data: {"model":"gemini-3.8-flash","choices":[{"index":0,"delta":{"content":"Hello."},"finish_reason":null}]}\n\n'
            'data: {"model":"gemini-3.8-flash","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
            'data: [DONE]\n\n').encode())
    monkeypatch.setattr(client.app.state.gemini_gateway.client, "send", provider_response)
    response = client.post("/llm/chat/completions", headers={
        "authorization": "Bearer synthetic-gateway-secret-at-least-32-characters"}, json={
        "model": "gemini-3.8-flash", "stream": True,
        "messages": [{"role": "user", "content": "Hello"}], "tools": TOOL_SCHEMAS,
        "call": {"id": value["call_id"], "assistantId": ASSISTANT}})
    assert response.status_code == 200 and "Hello." in response.text and contacted == [True]
    assert post(client, value, [entry()]).status_code == 409
