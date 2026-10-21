"""Phone HTTP boundary tests: disposable databases and synthetic credentials only."""
import asyncio
import importlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import dotenv
from fastapi.testclient import TestClient
import httpx
import pytest

from app.gemini_gateway import MODEL

ORG = "11111111-1111-4111-8111-111111111111"
PHONE = "22222222-2222-4222-8222-222222222222"
ASSISTANT = "33333333-3333-4333-8333-333333333333"
BROWSER = "44444444-4444-4444-8444-444444444444"
PHONE_SECRET = "phone-only-credential-" + "a" * 32
BROWSER_SECRET = "browser-only-credential-" + "b" * 32
GATEWAY_SECRET = "gateway-only-credential-" + "c" * 32
PHONE_HEADERS = {"authorization": "Bearer " + PHONE_SECRET}
BROWSER_HEADERS = {"authorization": "Bearer " + BROWSER_SECRET}
PATH = "/webhooks/vapi/inbound"


@pytest.fixture
def factory(tmp_path, monkeypatch):
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr(dotenv, "dotenv_values", lambda *a, **k: {})
    for key, value in {
        "SERVICEVOICE_DB": str(tmp_path / "import.sqlite3"),
        "VAPI_PUBLIC_KEY": "synthetic-public-key", "VAPI_ASSISTANT_ID": BROWSER,
        "VAPI_WEBHOOK_SECRET": BROWSER_SECRET, "VAPI_SERVER_CREDENTIAL_ID": "synthetic-server-credential",
        "PUBLIC_BASE_URL": "https://provider.example.invalid", "VAPI_MODEL_PROFILE": "gemini-3.8-flash-custom",
        "GEMINI_GATEWAY_SECRET": GATEWAY_SECRET, "GEMINI_API_KEY": "synthetic-google-key",
        "SERVICEVOICE_JEV_FAQ": "off", "SERVICEVOICE_RECEIPT_NARRATION": "off",
        "SERVICEVOICE_TELEPHONY_ENABLED": "on", "VAPI_PHONE_WEBHOOK_SECRET": PHONE_SECRET,
        "VAPI_PHONE_ORG_ID": ORG, "VAPI_PHONE_NUMBER_ID": PHONE, "VAPI_PHONE_ASSISTANT_ID": ASSISTANT,
    }.items():
        monkeypatch.setenv(key, value)
    module = importlib.import_module("app.main")
    def make(enabled=True):
        monkeypatch.setenv("SERVICEVOICE_TELEPHONY_ENABLED", "on" if enabled else "off")
        app = module.create_app(tmp_path / (str(uuid4()) + ".sqlite3"))
        return module, app, module.create_public_app(app)
    return make


def admission(**updates):
    return {"message": {"type": "assistant-request", "call": {
        "id": str(uuid4()), "type": "inboundPhoneCall", "orgId": ORG,
        "phoneNumberId": PHONE, **updates,
    }}}


def admit(client, body=None):
    body = body or admission()
    result = client.post(PATH, headers=PHONE_HEADERS, json=body)
    assert result.status_code == 200, result.text
    return {**body["message"]["call"], **result.json()}, result.json()


def event(call, kind="status-update", **extra):
    return {"message": {"type": kind, "call": call, **extra}}


def count(app, table):
    with app.state.store.connect() as db:
        return db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]


def issue(app, call, name, arguments, tool_id):
    sid = call["assistantOverrides"]["variableValues"]["session_id"]
    app.state.gemini_gateway.store.save_group(sid, call["id"], [{
        "id": tool_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)},
        "extra_content": {"google": {"thought_signature": "synthetic-test-signature"}},
    }])


def tool(client, call, name="get_dealership_info", arguments=None, tool_id="tool-one"):
    return client.post(PATH, headers=PHONE_HEADERS, json=event(call, "tool-calls", toolCallList=[{
        "id": tool_id, "function": {"name": name, "arguments": arguments or {}},
    }]))


def test_public_route_and_local_host_exception_keep_console_private(factory):
    _, app, public = factory()
    with TestClient(public) as client:
        call, response = admit(client)
        assert response["assistantId"] == ASSISTANT
        assert set(response["assistantOverrides"]["variableValues"]) == {"session_id"}
        assert client.get("/api/config", headers={"host": "localhost"}).status_code == 404
        assert client.get("/api/docs").status_code == 404
    _, app2, _ = factory()
    with TestClient(app2) as console:
        result = console.post(PATH, headers={**PHONE_HEADERS, "host": "provider.example.invalid"}, json=admission())
        assert result.status_code == 200
        sid = result.json()["assistantOverrides"]["variableValues"]["session_id"]
        assert console.get(f"/api/sessions/{sid}").status_code == 401
        config = console.get("/api/config")
        assert config.json()["telephony_enabled"] is True
        assert PHONE_SECRET not in config.text and ORG not in config.text and ASSISTANT not in config.text


def test_disabled_endpoint_has_no_phone_schema_or_lazy_admission(factory):
    _, app, public = factory(False)
    with TestClient(public) as client:
        assert client.post(PATH, headers=PHONE_HEADERS, json=admission()).status_code == 503
    with app.state.store.connect() as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='telephony_bindings'").fetchone() is None
    assert count(app, "sessions") == 0


@pytest.mark.parametrize("headers", [{}, BROWSER_HEADERS, {"authorization": "Bearer wrong"}])
def test_authentication_rejects_before_reading_stream(factory, headers):
    _, app, public = factory()
    consumed = []
    async def scenario():
        async def body():
            consumed.append(True)
            yield b"{}"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public), base_url="https://example.invalid") as client:
            response = await client.post(PATH, headers=headers, content=body())
            assert response.status_code == 401
        await app.state.shutdown_resources()
    asyncio.run(scenario())
    assert consumed == [] and count(app, "sessions") == 0


@pytest.mark.parametrize("change", [{"orgId": BROWSER}, {"phoneNumberId": BROWSER}, {"assistantId": BROWSER}, {"type": "webCall"}])
def test_authenticated_wrong_origin_is_rejected_without_effect(factory, change):
    _, app, public = factory()
    with TestClient(public) as client:
        assert client.post(PATH, headers=PHONE_HEADERS, json=admission(**change)).status_code == 403
    assert count(app, "sessions") == 0


def test_admission_ignores_preselection_dto_and_caller_capabilities(factory):
    _, app, public = factory()
    req = admission(customer={"number": "+15125550142"}, assistantOverrides={"variableValues": {"session_id": "stolen", "session_token": "stolen"}})
    req["message"]["assistant"] = {"name": "Untrusted DTO", "model": {"provider": "untrusted"}}
    with TestClient(public) as client:
        first = client.post(PATH, headers=PHONE_HEADERS, json=req)
        retry = client.post(PATH, headers=PHONE_HEADERS, json=req)
        assert first.status_code == retry.status_code == 200
        assert first.json() == retry.json()
        assert "stolen" not in first.text and "Untrusted" not in first.text
    assert count(app, "sessions") == count(app, "provider_tool_policies") == 1


def test_non_admission_events_never_create_unknown_calls(factory):
    _, app, public = factory()
    call = {**admission()["message"]["call"], "assistantId": ASSISTANT, "assistantOverrides": {"variableValues": {"session_id": "fake"}}}
    with TestClient(public) as client:
        for kind in ("status-update", "transcript", "end-of-call-report"):
            response = client.post(PATH, headers=PHONE_HEADERS, json=event(call, kind, status="ended"))
            assert response.status_code == 403
        assert tool(client, call).status_code == 403
    assert count(app, "sessions") == count(app, "tool_invocations") == 0


def test_phone_origin_cannot_downgrade_to_browser_webhook_or_binding(factory):
    _, app, public = factory()
    with TestClient(public) as client:
        call, _ = admit(client)
        assert client.post("/webhooks/vapi", headers=BROWSER_HEADERS, json=event(call)).status_code == 403
        disguised = {"id": call["id"], "assistantId": BROWSER}
        assert client.post("/webhooks/vapi", headers=BROWSER_HEADERS, json=event(disguised)).status_code == 403
        assert client.post(PATH, headers=PHONE_HEADERS, json=event({**call, "assistantId": BROWSER})).status_code == 403
        call["assistantOverrides"]["variableValues"]["session_token"] = "caller-added"
        assert client.post(PATH, headers=PHONE_HEADERS, json=event(call)).status_code == 403
    assert count(app, "sessions") == 1


def test_browser_behavior_unchanged_and_phone_secret_cannot_authorize_browser(factory):
    _, app, _ = factory()
    with TestClient(app) as client:
        session = client.post("/api/sessions", json={"mode": "live"}).json()
        call = {"id": "browser-call", "assistantId": BROWSER,
                "assistantOverrides": {"variableValues": {"session_id": session["id"], "session_token": session["token"]}}}
        assert client.post("/webhooks/vapi", headers=BROWSER_HEADERS, json=event(call)).status_code == 200
        assert client.post("/webhooks/vapi", headers=PHONE_HEADERS, json=event(call)).status_code == 401
        assert client.post(PATH, headers=PHONE_HEADERS, json=event(call)).status_code == 422


def test_issued_tool_provenance_observation_and_exact_replay_preserved(factory):
    _, app, public = factory()
    with TestClient(public) as client:
        call, _ = admit(client)
        assert tool(client, call).status_code == 409
        assert count(app, "tool_invocations") == 0
        issue(app, call, "get_dealership_info", {}, "tool-one")
        first = tool(client, call)
        second = tool(client, call)
        assert first.status_code == second.status_code == 200
        assert first.json() == second.json()
        result = json.loads(first.json()["results"][0]["result"])
        assert result["ok"] and result["_receipt"]
        assert count(app, "tool_invocations") == count(app, "tool_results") == count(app, "tool_observations") == 1
        assert tool(client, call, "request_handoff", {"reason": "changed"}).status_code == 409
        assert count(app, "handoffs") == 0


def test_known_terminal_duplicate_expiry_and_post_end_replay(factory):
    _, app, public = factory()
    with TestClient(public) as client:
        call, _ = admit(client)
        issue(app, call, "get_dealership_info", {}, "tool-one")
        initial = tool(client, call).json()
        assert client.post(PATH, headers=PHONE_HEADERS, json=event(call, "status-update", status="ended")).status_code == 200
        assert tool(client, call).json() == initial
        issue(app, call, "get_appointment", {}, "new-after-end")
        refused = tool(client, call, "get_appointment", tool_id="new-after-end")
        assert json.loads(refused.json()["results"][0]["result"])["error"] == "session_ended"
        with app.state.store.connect() as db:
            db.execute("UPDATE sessions SET created=?", (time.time()-15000,))
        assert client.post(PATH, headers=PHONE_HEADERS, json=event(call)).status_code == 409
        assert client.post(PATH, headers=PHONE_HEADERS, json=event(call, "end-of-call-report")).status_code == 200
    assert count(app, "sessions") == 1


def test_configuration_is_frozen_until_restart(factory, monkeypatch):
    _, app, public = factory()
    monkeypatch.setenv("VAPI_PHONE_WEBHOOK_SECRET", "new-secret-" + "z"*40)
    monkeypatch.setenv("SERVICEVOICE_TELEPHONY_ENABLED", "off")
    with TestClient(public) as client:
        assert client.post(PATH, headers=PHONE_HEADERS, json=admission()).status_code == 200
        assert client.post(PATH, headers={"authorization": "Bearer new-secret-" + "z"*40}, json=admission()).status_code == 401


def test_gateway_resolves_phone_only_after_admission_and_keeps_policy_gate(factory):
    _, app, public = factory()
    async def fake_send(*args, **kwargs):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content='data: {"choices":[{"delta":{"content":"Synthetic reply"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
    app.state.gemini_gateway.client.send = fake_send
    with TestClient(public) as client:
        call, _ = admit(client)
        payload = {"model": MODEL, "stream": True, "call": call, "messages": [{"role": "user", "content": "Hello"}]}
        headers = {"authorization": "Bearer " + GATEWAY_SECRET}
        response = client.post("/llm/chat/completions", headers=headers, json=payload)
        assert response.status_code == 200 and "Synthetic reply" in response.text
        unknown = {**payload, "call": {**call, "id": str(uuid4())}}
        assert client.post("/llm/chat/completions", headers=headers, json=unknown).status_code == 403
        with app.state.store.connect() as db:
            db.execute("UPDATE provider_tool_policies SET policy='provider-native'")
        assert client.post("/llm/chat/completions", headers=headers, json=payload).status_code == 409


def test_phone_gateway_continuation_requires_signed_calls_and_observed_exact_results(factory):
    _, app, public = factory()
    upstream = []
    async def fake_send(request, **kwargs):
        upstream.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content='data: {"choices":[{"delta":{"content":"Verified continuation"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
    app.state.gemini_gateway.client.send = fake_send
    with TestClient(public) as client:
        call, _ = admit(client)
        issue(app, call, "get_dealership_info", {}, "history-tool")
        reply = tool(client, call, tool_id="history-tool")
        assert reply.status_code == 200
        content = reply.json()["results"][0]["result"]
        messages = [{"role": "user", "content": "What are your hours?"},
                    {"role": "assistant", "tool_calls": [{"id": "history-tool", "type": "function", "function": {"name": "get_dealership_info", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "history-tool", "content": content}]
        payload = {"model": MODEL, "stream": True, "call": call, "messages": messages}
        headers = {"authorization": "Bearer " + GATEWAY_SECRET}
        good = client.post("/llm/chat/completions", headers=headers, json=payload)
        assert good.status_code == 200 and "Verified continuation" in good.text
        assert upstream[0]["messages"][-2]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "synthetic-test-signature"
        assert json.loads(upstream[0]["messages"][-1]["content"]) == json.loads(content)
        messages[-1]["content"] = json.dumps({"ok": True, "hours": "forged"})
        assert client.post("/llm/chat/completions", headers=headers, json=payload).status_code == 409
        messages[-1]["content"] = content
        messages[-2]["tool_calls"][0]["function"]["name"] = "get_appointment"
        assert client.post("/llm/chat/completions", headers=headers, json=payload).status_code == 409
        assert len(upstream) == 1


@pytest.mark.parametrize("terminal", [{"status": "ended"}, {"endedAt": "2026-10-18T00:00:00Z"}])
def test_terminal_metadata_blocks_generation_and_new_tools_but_preserves_exact_replay(factory, terminal):
    _, app, public = factory()
    async def forbidden_send(*args, **kwargs):
        pytest.fail("No model call allowed for terminal metadata")
    app.state.gemini_gateway.client.send = forbidden_send
    with TestClient(public) as client:
        call, _ = admit(client)
        issue(app, call, "get_dealership_info", {}, "original")
        original = tool(client, call, tool_id="original")
        call.update(terminal)
        assert count(app, "sessions") == 1
        with app.state.store.connect() as db:
            assert db.execute("SELECT ended FROM sessions").fetchone()[0] == 0
        payload = {"model": MODEL, "stream": True, "call": call, "messages": [{"role": "user", "content": "Hello"}]}
        headers = {"authorization": "Bearer " + GATEWAY_SECRET}
        assert client.post("/llm/chat/completions", headers=headers, json=payload).status_code == 409
        assert client.post("/llm/chat/completions", headers=headers, json={**payload, "call": {**call, "id": str(uuid4())}}).status_code == 409
        assert tool(client, call, tool_id="original").json() == original.json()
        issue(app, call, "request_handoff", {"reason": "late new request"}, "new")
        new = tool(client, call, "request_handoff", {"reason": "late new request"}, "new")
        assert json.loads(new.json()["results"][0]["result"])["error"] == "session_ended"
        assert count(app, "handoffs") == 0 and count(app, "sessions") == 1


def test_body_limit_and_total_deadline_are_enforced_without_admission(factory, monkeypatch):
    module, app, public = factory()
    monkeypatch.setattr(module, "TELEPHONY_HTTP_TIMEOUT_SECONDS", .04)
    async def scenario():
        async def slow_body():
            yield b'{"message":'
            await asyncio.sleep(.2)
            yield b'null}'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public), base_url="https://example.invalid") as client:
            over = await client.post(PATH, headers=PHONE_HEADERS, content=b"x"*262145)
            assert over.status_code == 413
            began = time.monotonic()
            slow = await client.post(PATH, headers=PHONE_HEADERS, content=slow_body())
            assert slow.status_code == 504 and time.monotonic()-began < .5
        assert not app.state.telephony_http_pending
        await app.state.shutdown_resources()
    asyncio.run(scenario())
    assert count(app, "sessions") == 0


@pytest.mark.parametrize("cancel_waiter", [False, True])
def test_timeout_or_cancellation_retains_capacity_until_real_worker_completion(factory, monkeypatch, cancel_waiter):
    module, app, public = factory()
    # Cancellation ownership must not race this fixture's artificial timeout.
    # The timeout variant below still exercises the original 80 ms deadline.
    monkeypatch.setattr(module, "TELEPHONY_HTTP_TIMEOUT_SECONDS", 30 if cancel_waiter else .08)
    monkeypatch.setattr(app.state, "telephony_http_capacity", 1)
    entered, release = threading.Event(), threading.Event()
    original = app.state.telephony_ingress.admit
    committed = []
    def slow(*args, **kwargs):
        result = original(*args, **kwargs)
        committed.append(result)
        entered.set()
        assert release.wait(10), "Only the test may release the admitted worker."
        return result
    monkeypatch.setattr(app.state.telephony_ingress, "admit", slow)
    req = admission()
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public), base_url="https://example.invalid") as client:
            request = asyncio.create_task(client.post(PATH, headers=PHONE_HEADERS, json=req))
            assert await asyncio.to_thread(entered.wait, 10)
            if cancel_waiter:
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
            else:
                assert (await request).status_code == 504
            assert len(app.state.telephony_http_pending) == 1
            assert (await client.post(PATH, headers=PHONE_HEADERS, json=req)).status_code == 429
            release.set()
            await asyncio.gather(*list(app.state.telephony_http_tasks))
            await asyncio.sleep(0)
            assert not app.state.telephony_http_pending
            monkeypatch.setattr(app.state.telephony_ingress, "admit", original)
            retry = await client.post(PATH, headers=PHONE_HEADERS, json=req)
            assert retry.status_code == 200 and retry.json() == committed[0]
        await app.state.shutdown_resources()
    try:
        asyncio.run(scenario())
    finally:
        release.set()
    assert count(app, "sessions") == 1


def test_thread_queue_delay_consumes_original_http_budget(factory, monkeypatch):
    module, app, public = factory()
    monkeypatch.setattr(module, "TELEPHONY_HTTP_TIMEOUT_SECONDS", .04)
    occupied, release = threading.Event(), threading.Event()
    def occupy():
        occupied.set()
        assert release.wait(10), "The executor blocker must only release when the test signals it."
    async def scenario():
        loop = asyncio.get_running_loop()
        executor = ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(executor)
        blocking = loop.run_in_executor(None, occupy)
        while not occupied.is_set():await asyncio.sleep(.001)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public), base_url="https://example.invalid") as client:
            response = await client.post(PATH, headers=PHONE_HEADERS, json=admission())
            assert response.status_code == 504
            assert len(app.state.telephony_http_pending) == 1
            release.set()
            await blocking
            await asyncio.gather(*list(app.state.telephony_http_tasks), return_exceptions=True)
            await asyncio.sleep(0)
            assert not app.state.telephony_http_pending
        await app.state.shutdown_resources()
    try:
        asyncio.run(scenario())
    finally:
        release.set()
    assert count(app, "sessions") == 0


@pytest.mark.parametrize("cancel_waiter", [False, True])
def test_premature_timeout_or_cancellation_closes_queued_admission_before_clock_deadline(factory, monkeypatch, cancel_waiter):
    module, app, public = factory()
    # Deterministically exercise the coarse-clock early-timer case: the HTTP
    # waiter finishes while its absolute monotonic budget still has time left.
    monkeypatch.setattr(module, "TELEPHONY_HTTP_TIMEOUT_SECONDS", 5)
    monkeypatch.setattr(app.state, "telephony_http_capacity", 1)
    occupied, release = threading.Event(), threading.Event()
    def occupy():
        occupied.set()
        assert release.wait(10)
    observed = []
    original_admit = app.state.telephony_ingress.admit
    def observe(*args, **kwargs):
        observed.append({"absolute_deadline_still_future": time.monotonic()<kwargs["deadline"],
                         "abandoned": kwargs["abandoned"].is_set()})
        return original_admit(*args, **kwargs)
    monkeypatch.setattr(app.state.telephony_ingress, "admit", observe)
    original_wait = asyncio.wait_for
    injected = []
    async def premature_wait(awaitable, timeout):
        # Body collection uses coroutines. The protected operation uses a shield
        # Future. Cancel only that wrapper, as real wait_for does on timeout.
        if isinstance(awaitable, asyncio.Future) and not injected:
            injected.append(True)
            await asyncio.sleep(0)
            awaitable.cancel()
            if cancel_waiter:raise asyncio.CancelledError()
            raise asyncio.TimeoutError()
        return await original_wait(awaitable, timeout)
    async def scenario():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        blocking = loop.run_in_executor(None, occupy)
        while not occupied.is_set():await asyncio.sleep(.001)
        monkeypatch.setattr(module.asyncio, "wait_for", premature_wait)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public), base_url="https://example.invalid") as client:
            if cancel_waiter:
                with pytest.raises(asyncio.CancelledError):
                    await client.post(PATH, headers=PHONE_HEADERS, json=admission())
            else:
                assert (await client.post(PATH, headers=PHONE_HEADERS, json=admission())).status_code == 504
            assert len(app.state.telephony_http_pending) == 1
            assert (await client.post(PATH, headers=PHONE_HEADERS, json=admission())).status_code == 429
            release.set()
            await blocking
            await asyncio.gather(*list(app.state.telephony_http_tasks), return_exceptions=True)
            await asyncio.sleep(0)
            assert not app.state.telephony_http_pending
        monkeypatch.setattr(module.asyncio, "wait_for", original_wait)
        await app.state.shutdown_resources()
    try:
        asyncio.run(scenario())
    finally:
        release.set()
    assert injected == [True]
    assert observed == [{"absolute_deadline_still_future": True, "abandoned": True}]
    assert count(app, "sessions") == count(app, "provider_tool_policies") == 0


def test_closing_ingress_refuses_new_work(factory):
    _, app, public = factory()
    app.state.telephony_http_closing = True
    with TestClient(public) as client:
        assert client.post(PATH, headers=PHONE_HEADERS, json=admission()).status_code == 503
    assert count(app, "sessions") == 0
