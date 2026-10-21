"""The loopback operator console forwards only explicit candidate routes."""
import asyncio
from contextlib import contextmanager
import json
from types import SimpleNamespace

import httpx
import pytest

from scripts.serve_duplex import create_console


CANDIDATE = "isolated-console-assistant"
VAPI_KEY = "synthetic-private-vapi-key-never-real"
JEV_KEY = "synthetic-private-classifier-key-never-real"
TOKEN = "synthetic-session-capability"
SID = "synthetic-owned-session"


@contextmanager
def console_fixture(*, changes=None, values=None, handler=None):
    config = {"vapi_assistant_id": CANDIDATE, "live_voice_available": True,
              "duplex_browser_cue_control": "browser-owned-v1", "today": "2026-10-20",
              **(changes or {})}
    local = {"VAPI_API_KEY": VAPI_KEY, "TYPESAFE_API_KEY": JEV_KEY,
             "VAPI_BACKCHANNEL_ASSISTANT_ID": CANDIDATE,
             "VAPI_ASSISTANT_ID": "accepted-baseline-must-not-be-selected", **(values or {})}
    calls = []
    state = {"id": SID, "mode": "live", "ended": False, "stage": "service",
             "proposal": None, "pending_operations": []}

    def dispatch(request):
        calls.append(request)
        if request.url.path == "/api/config":
            return httpx.Response(200, json=config)
        if handler:
            answer = handler(request)
            if answer is not None:
                return answer
        if request.url.path == "/api/sessions" and request.method == "POST":
            return httpx.Response(200, json={**state, "token": TOKEN})
        if request.url.path in ("/api/sessions/" + SID, "/api/sessions/" + SID + "/end"):
            if request.headers.get("x-session-token") != TOKEN:
                return httpx.Response(401, json={"private_body": VAPI_KEY})
            if request.method == "POST":
                state["ended"] = True
            return httpx.Response(200, json=dict(state))
        return httpx.Response(404, json={"detail": "not found"})

    remote = httpx.Client(base_url="http://127.0.0.1:8775", transport=httpx.MockTransport(dispatch))
    try:
        app = create_console(local, "http://127.0.0.1:8775", upstream=remote)
        yield SimpleNamespace(app=app, calls=calls, remote=remote, local=local, config=config, state=state)
    finally:
        remote.close()


def test_console_pins_candidate_and_exposes_no_operator_credentials():
    async def run():
        with console_fixture() as h:
            assert h.app.state.duplex.settings()["VAPI_ASSISTANT_ID"] == CANDIDATE
            assert h.app.state.duplex.settings()["SERVICEVOICE_DUPLEX"] == "on"
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url="http://127.0.0.1:8785") as client:
                response = await client.get("/api/config")
                assert response.status_code == 200 and response.json() == h.config
                assert not any(value in response.text for value in (VAPI_KEY, JEV_KEY, TOKEN, "accepted-baseline"))
                assert response.headers["cache-control"] == "no-store"
                assert response.headers["referrer-policy"] == "no-referrer"
                assert response.headers["x-content-type-options"] == "nosniff"
                assert "access-control-allow-origin" not in response.headers
            assert len(h.calls) == 1 and h.calls[0].url.path == "/api/config"
    asyncio.run(run())


@pytest.mark.parametrize("changes,values", [
    ({"vapi_assistant_id": "accepted-baseline"}, None),
    ({"live_voice_available": False}, None),
    ({"live_voice_available": None}, None),
    ({"duplex_browser_cue_control": None}, None),
    ({"duplex_browser_cue_control": "browser-owned-v2"}, None),
    ({"duplex_browser_cue_control": True}, None),
    (None, {"VAPI_BACKCHANNEL_ASSISTANT_ID": ""}),
])
def test_startup_requires_exact_candidate_and_loaded_gateway_control_version(changes, values):
    with pytest.raises(ValueError):
        with console_fixture(changes=changes, values=values):
            pytest.fail("mismatched candidate console started")


@pytest.mark.parametrize("address", ["https://127.0.0.1:8775", "http://localhost:8775",
    "http://127.0.0.1.attacker.invalid:8775", "http://attacker.invalid", "http://127.0.0.1:8775/api/config",
    "http://user:password@127.0.0.1:8775", "http://127.0.0.1:8775/?target=other", "http://127.0.0.1:8775/#fragment"])
def test_console_upstream_cannot_be_replaced_with_an_arbitrary_address(address):
    calls = []
    with httpx.Client(base_url="http://127.0.0.1:8775", transport=httpx.MockTransport(
            lambda request: calls.append(request) or httpx.Response(200, json={}))) as remote:
        with pytest.raises(ValueError):
            create_console({"VAPI_BACKCHANNEL_ASSISTANT_ID": CANDIDATE}, address, upstream=remote)
    assert calls == []


@pytest.mark.parametrize("headers", [
    {"origin": "https://attacker.invalid"}, {"origin": "null"},
    {"origin": "http://127.0.0.1:8775"}, {"origin": "http://localhost:8785"},
    {"host": "attacker.invalid"}, {"host": "127.0.0.1.attacker.invalid"},
    {"host": "public.example", "origin": "http://public.example"},
])
def test_cross_origin_or_non_loopback_hosts_are_rejected_before_session_or_call_work(headers):
    async def run():
        with console_fixture() as h:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url="http://127.0.0.1:8785") as client:
                for target in ("/api/sessions", "/api/sessions/" + SID + "/duplex/call"):
                    response = await client.post(target, headers={"x-session-token": TOKEN, **headers})
                    assert response.status_code == 403
                    assert VAPI_KEY not in response.text and JEV_KEY not in response.text
            assert len(h.calls) == 1 and not h.app.state.duplex.created and not h.app.state.duplex.sessions
    asyncio.run(run())


def test_session_creation_ignores_client_modes_and_session_snapshot_forwards_only_its_capability():
    async def run():
        with console_fixture() as h:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url="http://127.0.0.1:8785") as client:
                made = await client.post("/api/sessions", headers={"origin": "http://127.0.0.1:8785"},
                    json={"mode": "admin", "url": "https://attacker.invalid", "assistantId": "accepted-baseline"})
                assert made.status_code == 200 and made.json()["token"] == TOKEN
                assert json.loads(h.calls[-1].content) == {"mode": "live"}
                snapshot = await client.get("/api/sessions/" + SID + "?url=https://attacker.invalid", headers={
                    "x-session-token": TOKEN, "authorization": "Bearer client-injected-admin-token"})
                assert snapshot.status_code == 200 and snapshot.json()["id"] == SID
                request = h.calls[-1]
                assert str(request.url) == "http://127.0.0.1:8775/api/sessions/" + SID
                assert request.headers["x-session-token"] == TOKEN and "authorization" not in request.headers
                assert VAPI_KEY not in str(request.headers) and JEV_KEY not in str(request.headers)
    asyncio.run(run())


def test_failed_session_authorization_hides_upstream_body_and_prevents_end_side_effects():
    async def run():
        with console_fixture() as h:
            session = await h.app.state.duplex.session(SID)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url="http://127.0.0.1:8785") as client:
                response = await client.post("/api/sessions/" + SID + "/end", headers={"x-session-token": "other-session-token"})
                assert response.status_code == 401
                assert VAPI_KEY not in response.text and "private_body" not in response.text
                assert not session.closed and not h.state["ended"]
            assert h.calls[-1].method == "GET" and len(h.calls) == 2
    asyncio.run(run())


def test_owned_end_authorizes_before_forwarding_and_closes_only_the_owned_cue_session():
    async def run():
        with console_fixture() as h:
            owned = await h.app.state.duplex.session(SID)
            other = await h.app.state.duplex.session("other-owned-session")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url="http://127.0.0.1:8785") as client:
                response = await client.post("/api/sessions/" + SID + "/end", headers={"x-session-token": TOKEN},
                    json={"sid": "other-owned-session", "url": "https://attacker.invalid"})
                assert response.status_code == 200 and response.json()["ended"] is True
                assert owned.closed and not other.closed
                assert [(request.method, request.url.path) for request in h.calls[-2:]] == [
                    ("GET", "/api/sessions/" + SID), ("POST", "/api/sessions/" + SID + "/end")]
                assert all(request.headers.get("x-session-token") == TOKEN for request in h.calls[-2:])
                after = await client.get("/api/sessions/" + SID, headers={"x-session-token": TOKEN})
                assert after.status_code == 200 and after.json()["ended"] is True
    asyncio.run(run())


@pytest.mark.parametrize("target,method", [
    ("/api/proxy?url=https://attacker.invalid", "GET"),
    ("/api/sessions/" + SID + "/admin", "POST"),
    ("/api/sessions/https:%2F%2Fattacker.invalid", "GET"),
    ("/api/sessions/..", "GET"),
    ("http://attacker.invalid/api/sessions/" + SID, "GET"),
])
def test_console_has_no_general_proxy_or_arbitrary_upstream_route(target, method):
    async def run():
        with console_fixture() as h:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url="http://127.0.0.1:8785") as client:
                answer = await client.request(method, target, headers={"x-session-token": TOKEN})
                assert answer.status_code in (403, 404, 405), answer.text
            assert len(h.calls) == 1
    asyncio.run(run())
