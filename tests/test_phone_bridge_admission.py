"""Disposable main-Store admission controls; provider responses are synthetic."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import threading
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.capacity import CapacityConfig, CapacityError, LiveAdmission
from app.domain import Store, TENANT
from app.phone_bridge_api import PhoneBridgeAdmissions, install_phone_bridge_api
from app.telephony_ingress import IngressError, IngressLimits, TelephonyIngress
from test_telephony_ingress import config, message, AUTH, BROWSER, ORG, PHONE
from test_telephony_api import factory, issue, event, BROWSER_HEADERS, GATEWAY_SECRET
from app.gemini_gateway import MODEL

SECRET = "private-bridge-control-" + "b" * 40
BRIDGE_AUTH = "Bearer " + SECRET


@pytest.fixture
def setup(tmp_path, config):
    store = Store(tmp_path / "main.sqlite3")
    clock = [2_000_000_000.0]
    capacity = LiveAdmission(store, CapacityConfig(), clock=lambda: clock[0])
    ingress = TelephonyIngress(store, config, live_admission=capacity, clock=lambda: clock[0])
    bridge = PhoneBridgeAdmissions(ingress, BROWSER, SECRET)
    return SimpleNamespace(store=store, clock=clock, capacity=capacity, ingress=ingress, bridge=bridge)


def admit(s, call=None):
    call = call or message()["call"]
    return s.bridge.admit(call, s.clock[0] + 300)


def count(s, table):
    with s.store.connect() as db:
        return db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]


def error(code, function, *args, **kwargs):
    with pytest.raises(IngressError) as caught:
        function(*args, **kwargs)
    assert caught.value.code == code


def test_one_canonical_inner_session_no_outer_booking_binding_or_customer_trust(setup):
    s = setup
    outer = message(customer={"number": "+15125550142", "name": "Untrusted provider field"})["call"]
    result = admit(s, outer)
    assert count(s, "sessions") == 1 and count(s, "telephony_bindings") == 0
    session = s.store.authorize(result["id"], result["token"])
    assert session["provider_call_id"] is None
    assert json.loads(session["state"])["customer"] is None
    assert count(s, "appointments") == count(s, "tool_invocations") == 0
    with s.store.connect() as db:
        assert s.capacity.count(db) == 1


def test_malformed_non_ascii_auth_is_safe_rejection_before_state_changes(setup):
    error("phone_bridge_unauthorized", setup.bridge.authenticate, "Bearer \u00e9")
    assert count(setup, "sessions") == 0


def test_duplicate_and_restart_preserve_single_session_capability_and_original_deadline(setup):
    s = setup
    outer = message()["call"]
    first = admit(s, outer)
    s.clock[0] += 10
    restarted = PhoneBridgeAdmissions(s.ingress, BROWSER, SECRET)
    repeated = restarted.admit(outer, s.clock[0] + 300)
    assert repeated["id"] == first["id"] and repeated["token"] == first["token"]
    assert repeated["remaining_duration_ms"] == 290000
    assert count(s, "sessions") == 1
    rotated = PhoneBridgeAdmissions(s.ingress, BROWSER, "different-control-" + "c" * 40)
    error("phone_bridge_capability_changed", rotated.admit, outer, s.clock[0] + 300)


def test_concurrent_browser_and_bridge_share_one_atomic_capacity_slot(setup):
    s = setup
    s.capacity.config = CapacityConfig(max_live_calls=1)
    gate = threading.Barrier(2)
    def run(kind):
        gate.wait()
        try:
            return s.capacity.create_browser_session() if kind == "browser" else admit(s)
        except (CapacityError, IngressError) as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(run, ["browser", "bridge"]))
    assert sum(isinstance(row, dict) for row in outcomes) == 1
    assert "live_call_capacity" in outcomes
    assert count(s, "sessions") == 1


def test_old_phone_and_bridge_share_one_rate_budget_in_both_directions(setup):
    s = setup
    s.ingress.limits = IngressLimits(active_calls=10, admissions_per_window=2)
    s.ingress.admit(message(), AUTH)
    admit(s)
    error("telephony_admission_rate", s.ingress.admit, message(), AUTH)
    error("telephony_admission_rate", admit, s)
    assert count(s, "sessions") == 2


def test_same_outer_call_cannot_enter_both_admission_paths(setup):
    s = setup; outer = message()["call"]
    admit(s, outer)
    error("telephony_binding_conflict", s.ingress.admit, {"type": "assistant-request", "call": outer}, AUTH)
    ordinary = message(); s.ingress.admit(ordinary, AUTH)
    error("phone_bridge_binding_conflict", admit, s, ordinary["call"])
    assert count(s, "sessions") == 2


def test_unknown_allocation_survives_restart_local_end_and_browser_ttl(setup):
    s = setup
    outer = message()["call"]
    session = admit(s, outer)
    assert s.bridge.allocation(outer["id"]) == {"allocation_reserved": True}
    restarted = PhoneBridgeAdmissions(s.ingress, BROWSER, SECRET)
    error("phone_bridge_allocation_already_requested", restarted.allocation, outer["id"])
    with s.store.transaction() as db:
        db.execute("UPDATE sessions SET ended=1 WHERE id=?", (session["id"],))
    s.clock[0] += 5 * 3600
    with s.store.connect() as db:
        assert s.capacity.count(db) == 1
    s.capacity.config = CapacityConfig(max_live_calls=1)
    with pytest.raises(CapacityError, match="live_call_capacity"):
        s.capacity.create_browser_session()
    assert restarted.close(outer["id"], outer_ended=True, inner_ended=True)["closed"] is False


def test_binding_is_exact_once_and_never_overwrites_existing_canonical_provider(setup):
    s = setup
    outer = message()["call"]; session = admit(s, outer); inner = str(uuid4())
    error("phone_bridge_allocation_not_owned", s.bridge.bind, outer["id"], inner)
    s.bridge.allocation(outer["id"])
    assert s.bridge.bind(outer["id"], inner) == s.bridge.bind(outer["id"], inner) == {"bound": True}
    error("phone_bridge_inner_conflict", s.bridge.bind, outer["id"], str(uuid4()))
    error("phone_bridge_inner_conflict", s.bridge.require_live, session["id"], str(uuid4()))
    s.bridge.require_live(session["id"], inner)
    assert s.store.authorize(session["id"], session["token"])["provider_call_id"] is None


def test_outer_end_revokes_booking_session_but_capacity_waits_for_exact_inner_end(setup):
    s = setup
    outer = message()["call"]; session = admit(s, outer); inner = str(uuid4())
    s.bridge.allocation(outer["id"]); s.bridge.bind(outer["id"], inner)
    assert s.bridge.close(outer["id"], outer_ended=True, inner_ended=False) == {"closed": False, "outer_ended": True, "inner_ended": False}
    error("phone_bridge_call_ended", s.bridge.require_live, session["id"], inner)
    with s.store.connect() as db:
        assert db.execute("SELECT ended FROM sessions WHERE id=?", (session["id"],)).fetchone()[0] == 1
        assert s.capacity.count(db) == 1
    assert s.bridge.close(outer["id"], outer_ended=False, inner_ended=True)["closed"] is True
    with s.store.connect() as db:
        assert s.capacity.count(db) == 0
    error("phone_bridge_call_ended", admit, s, outer)


def test_unallocated_close_needs_outer_ended_and_hard_300s_deadline_is_authoritative(setup):
    s = setup; outer = message()["call"]; session = admit(s, outer)
    assert s.bridge.close(outer["id"], outer_ended=False, inner_ended=True)["closed"] is False
    s.clock[0] += 300
    error("phone_bridge_call_ended", s.bridge.require_live, session["id"])
    error("phone_bridge_call_ended", s.bridge.allocation, outer["id"])
    assert s.bridge.close(outer["id"], outer_ended=True, inner_ended=False)["closed"] is True


def test_existing_phone_worker_lane_saturation_blocks_bridge_without_insert(setup):
    s = setup; permits = []
    try:
        for _ in range(s.ingress.limits.workers):
            assert s.ingress._workers.acquire(blocking=False); permits.append(True)
        with pytest.raises(IngressError):
            admit(s)
        assert count(s, "sessions") == 0
    finally:
        for _ in permits:
            s.ingress._workers.release()


def api(s, handler, *, enabled=True):
    app = FastAPI()
    app.state.telephony_http_lock = threading.Lock()
    app.state.telephony_http_capacity = 4
    app.state.telephony_http_pending = set()
    app.state.telephony_http_tasks = set()
    app.state.telephony_http_closing = False
    @app.exception_handler(IngressError)
    async def rejected(request, exc):
        return JSONResponse({"detail": exc.code}, status_code=exc.status)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    values = {"SERVICEVOICE_PHONE_BRIDGE_ADMISSION": "on" if enabled else "off", "VAPI_ASSISTANT_ID": BROWSER,
              "SERVICEVOICE_PHONE_BRIDGE_CONTROL_SECRET": SECRET, "VAPI_API_KEY": "synthetic-not-a-real-key"}
    install_phone_bridge_api(app, s.ingress, lambda: values, client=client)
    return TestClient(app), client


def test_default_off_installs_no_routes_and_auth_fails_before_provider_read(setup):
    calls = []
    def provider(request):
        calls.append(request); raise AssertionError("Unauthorized provider read")
    client, remote = api(setup, provider, enabled=False)
    with client:
        assert client.post("/api/phone-bridge/admit", json={}).status_code == 404
    asyncio.run(remote.aclose())
    client, remote = api(setup, provider)
    with client:
        assert client.post("/api/phone-bridge/admit", json={"outer_call_id": str(uuid4())}).status_code == 401
    asyncio.run(remote.aclose())
    assert not calls


def test_api_independent_provider_identity_private_routes_and_no_token_on_lookup(setup):
    s = setup; outer = message()["call"]; requested = []
    outer["createdAt"] = datetime.fromtimestamp(s.clock[0]-20, timezone.utc).isoformat()
    def provider(request):
        requested.append(str(request.url)); return httpx.Response(200, json=outer)
    client, remote = api(s, provider)
    with client:
        response = client.post("/api/phone-bridge/admit", json={"outer_call_id": outer["id"]}, headers={"Authorization": BRIDGE_AUTH})
        assert response.status_code == 200, response.json()
        session = response.json(); assert session["remaining_duration_ms"] == 280000
        result = client.get("/api/phone-bridge/"+outer["id"], headers={"Authorization": BRIDGE_AUTH})
        assert "token" not in result.json() and "session_id" not in result.json()
        assert client.post("https://untrusted.example/api/phone-bridge/admit", json={"outer_call_id": outer["id"]}, headers={"Authorization": BRIDGE_AUTH}).status_code == 403
        assert requested == ["https://api.vapi.ai/call/"+outer["id"]]
    asyncio.run(remote.aclose())


@pytest.mark.parametrize("change", [
    {"orgId": str(uuid4())}, {"phoneNumberId": str(uuid4())}, {"type": "webCall"},
    {"status": "ended"}, {"createdAt": None}, {"createdAt": "2000-01-01T00:00:00Z"},
])
def test_provider_mismatch_missing_time_or_expired_outer_has_no_session_effect(setup, change):
    s = setup; outer = message()["call"]
    row = {**outer, "createdAt": datetime.fromtimestamp(s.clock[0], timezone.utc).isoformat(), **change}
    client, remote = api(s, lambda request: httpx.Response(200, json=row))
    with client:
        response = client.post("/api/phone-bridge/admit", json={"outer_call_id": outer["id"]}, headers={"Authorization": BRIDGE_AUTH})
        assert response.status_code in (403, 409, 503)
    asyncio.run(remote.aclose())
    assert count(s, "sessions") == count(s, "phone_bridge_admissions") == 0


def test_bridge_http_uses_existing_shared_phone_capacity_before_provider_or_db(setup):
    s = setup; calls = []
    client, remote = api(s, lambda request: calls.append(request))
    client.app.state.telephony_http_capacity = 1
    existing = object(); client.app.state.telephony_http_pending.add(existing)
    with client:
        response = client.post("/api/phone-bridge/admit", json={"outer_call_id": str(uuid4())}, headers={"Authorization": BRIDGE_AUTH})
        assert response.status_code == 503 and response.json()["detail"] == "telephony_http_capacity"
        assert client.app.state.telephony_http_pending == {existing}
    asyncio.run(remote.aclose())
    assert not calls and count(s, "sessions") == 0


def test_api_bind_requires_exact_model_session_and_close_preserves_outer_terminal_if_inner_read_fails(setup):
    s = setup; outer = message()["call"]; session = admit(s, outer); inner = str(uuid4())
    s.bridge.allocation(outer["id"])
    inner_row = {"id": inner, "type": "vapi.websocketCall", "orgId": ORG, "assistantId": BROWSER, "status": "in-progress",
                 "assistantOverrides": {"variableValues": {"session_id": "wrong", "servicevoice_duplex_playback": "browser-owned-v1"}}}
    fail_inner = [False]
    def provider(request):
        if request.url.path.endswith(inner):
            return httpx.Response(503) if fail_inner[0] else httpx.Response(200, json=inner_row)
        return httpx.Response(200, json={**outer, "status": "ended"})
    client, remote = api(s, provider)
    headers = {"Authorization": BRIDGE_AUTH}
    with client:
        path = "/api/phone-bridge/"+outer["id"]
        assert client.post(path+"/bind", json={"inner_call_id": inner}, headers=headers).status_code == 403
        assert s.bridge.lookup(outer["id"])["inner_call_id"] is None
        inner_row["assistantOverrides"]["variableValues"]["session_id"] = session["id"]
        inner_row["assistantOverrides"]["variableValues"]["session_token"] = "wrong capability"
        assert client.post(path+"/bind", json={"inner_call_id": inner}, headers=headers).status_code == 403
        inner_row["assistantOverrides"]["variableValues"]["session_token"] = session["token"]
        inner_row["type"] = "webCall"
        assert client.post(path+"/bind", json={"inner_call_id": inner}, headers=headers).status_code == 403
        assert s.bridge.lookup(outer["id"])["inner_call_id"] is None
        inner_row["type"] = "vapi.websocketCall"
        assert client.post(path+"/bind", json={"inner_call_id": inner}, headers=headers).json() == {"bound": True}
        fail_inner[0] = True
        assert client.post(path+"/close", json={}, headers=headers).status_code == 503
        link = s.bridge.lookup(outer["id"])
        assert link["outer_ended"] == 1 and link["closed"] is None
        with s.store.connect() as db:
            assert db.execute("SELECT ended FROM sessions WHERE id=?", (session["id"],)).fetchone()[0] == 1
            assert s.capacity.count(db) == 1
        fail_inner[0] = False; inner_row["status"] = "ended"
        assert client.post(path+"/close", json={}, headers=headers).json()["closed"] is True
    asyncio.run(remote.aclose())


def bridge_app(factory, monkeypatch):
    monkeypatch.setenv("SERVICEVOICE_PHONE_BRIDGE_ADMISSION", "on")
    monkeypatch.setenv("SERVICEVOICE_PHONE_BRIDGE_CONTROL_SECRET", SECRET)
    monkeypatch.setenv("VAPI_API_KEY", "synthetic-not-a-real-key")
    _, app, public = factory()
    control = app.state.phone_bridge
    outer = message()["call"]
    session = control.admit(outer, control.ingress.clock() + 300)
    inner = str(uuid4())
    call = {"id": inner, "type": "vapi.websocketCall", "assistantId": BROWSER,
            "assistantOverrides": {"variableValues": {"session_id": session["id"], "session_token": session["token"],
                                                       "servicevoice_duplex_playback": "browser-owned-v1"}}}
    return app, public, control, outer, session, call


def test_early_authenticated_provider_event_binds_same_inner_before_create_response(factory, monkeypatch):
    app, public, control, outer, session, call = bridge_app(factory, monkeypatch)
    with TestClient(public) as client:
        # No allocation intent means even authenticated session metadata cannot
        # turn a bridge admission into an unrelated ordinary browser call.
        early = event(call, "status-update", status="in-progress")
        assert client.post("/webhooks/vapi", headers=BROWSER_HEADERS, json=early).status_code == 403
        control.allocation(outer["id"])
        wrong_type={**call,"type":"webCall"}
        assert client.post("/webhooks/vapi",headers=BROWSER_HEADERS,json=event(wrong_type,"status-update",status="in-progress")).status_code==403
        assert control.lookup(outer["id"])["inner_call_id"] is None
        assert client.post("/webhooks/vapi", headers=BROWSER_HEADERS, json=early).status_code == 200
        assert control.lookup(outer["id"])["inner_call_id"] == call["id"]
        assert control.bind(outer["id"], call["id"]) == {"bound": True}
        with app.state.store.connect() as db:
            assert db.execute("SELECT provider_call_id FROM sessions WHERE id=?", (session["id"],)).fetchone()[0] == call["id"]
            assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        different = {**call, "id": str(uuid4())}
        assert client.post("/webhooks/vapi", headers=BROWSER_HEADERS, json=event(different, "status-update", status="in-progress")).status_code == 403
        assert client.post("/api/phone-bridge/admit", json={}, headers={"Authorization": BRIDGE_AUTH}).status_code == 404


def test_outer_end_preserves_exact_accepted_tool_replay_but_denies_new_effect_and_generation(factory, monkeypatch):
    app, public, control, outer, session, call = bridge_app(factory, monkeypatch)
    control.allocation(outer["id"])
    with TestClient(public) as client:
        assert client.post("/webhooks/vapi", headers=BROWSER_HEADERS, json=event(call, "status-update", status="in-progress")).status_code == 200
        issue(app, call, "get_dealership_info", {}, "original")
        def tool(identifier, name="get_dealership_info", arguments=None):
            return client.post("/webhooks/vapi", headers=BROWSER_HEADERS, json=event(call, "tool-calls", toolCallList=[{
                "id": identifier, "function": {"name": name, "arguments": arguments or {}}}]))
        original = tool("original")
        assert original.status_code == 200 and json.loads(original.json()["results"][0]["result"])["ok"]
        control.close(outer["id"], outer_ended=True, inner_ended=False)
        assert tool("original").json() == original.json()
        issue(app, call, "request_handoff", {"reason": "new after carrier end"}, "new")
        rejected = tool("new", "request_handoff", {"reason": "new after carrier end"})
        assert rejected.status_code == 200
        assert json.loads(rejected.json()["results"][0]["result"])["error"] == "session_ended"
        with app.state.store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0] == 0
            assert app.state.live_admission.count(db) == 1
        request = {"model": MODEL, "stream": True, "call": call, "messages": [{"role": "user", "content": "Hello"}]}
        assert client.post("/llm/chat/completions", headers={"Authorization": "Bearer " + GATEWAY_SECRET}, json=request).status_code == 409
        # Identity-bound terminal metadata can still reach the ordinary handler.
        assert client.post("/webhooks/vapi", headers=BROWSER_HEADERS, json=event(call, "status-update", status="ended")).status_code == 200


def test_disabled_bridge_keeps_identity_and_capacity_without_new_models_or_effects(factory, monkeypatch):
    app, public, control, outer, session, call = bridge_app(factory, monkeypatch)
    control.allocation(outer["id"])
    with TestClient(public) as client:
        assert client.post("/webhooks/vapi", headers=BROWSER_HEADERS, json=event(call, "status-update", status="in-progress")).status_code == 200
        issue(app, call, "get_dealership_info", {}, "known")
        def tool(identifier, name="get_dealership_info", arguments=None):
            return client.post("/webhooks/vapi", headers=BROWSER_HEADERS, json=event(call, "tool-calls", toolCallList=[{
                "id": identifier, "function": {"name": name, "arguments": arguments or {}}}]))
        original = tool("known"); assert original.status_code == 200
        # This is the same persistent-row/default-off state a restarted app
        # observes; the read guards must not depend on an enabled controller.
        app.state.phone_bridge = None
        monkeypatch.setenv("SERVICEVOICE_PHONE_BRIDGE_ADMISSION", "off")
        assert tool("known").json() == original.json()
        issue(app, call, "request_handoff", {"reason": "new while disabled"}, "new-disabled")
        rejected = tool("new-disabled", "request_handoff", {"reason": "new while disabled"})
        assert json.loads(rejected.json()["results"][0]["result"])["error"] == "session_ended"
        request = {"model": MODEL, "stream": True, "call": call, "messages": [{"role": "user", "content": "Hello"}]}
        assert client.post("/llm/chat/completions", headers={"Authorization": "Bearer " + GATEWAY_SECRET}, json=request).status_code == 409
        another = {**call, "id": str(uuid4())}
        assert client.post("/webhooks/vapi", headers=BROWSER_HEADERS, json=event(another, "status-update", status="in-progress")).status_code == 403
        assert client.post("/webhooks/vapi", headers=BROWSER_HEADERS, json=event(call, "status-update", status="ended")).status_code == 200
        with app.state.store.connect() as db:
            assert app.state.live_admission.count(db) == 1
            assert db.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0] == 0
