"""Durable allocation and owned stopping of synthetic duplex provider calls."""
import asyncio
from contextlib import asynccontextmanager
import json
import sqlite3

from fastapi import FastAPI
from fastapi.responses import JSONResponse
import httpx
import pytest

from app.domain import DomainError
from app.duplex_api import install_duplex_api
from test_duplex_api import ASSISTANT, VAPI_KEY, Policy, end_session, harness, headers, path

CALL = "isolated-call-fixture"
CONTROL = "https://phone-call-control.aws-us-west-2-backend-production1.vapi.ai/control/fixture"


def allocation():
    return httpx.Response(201, json={"id": CALL, "assistantId": ASSISTANT,
        "transport": {"websocketCallUrl": "wss://backend.vapi.ai/fixture"}})


@asynccontextmanager
async def restarted(h, provider):
    app = FastAPI()

    @app.exception_handler(DomainError)
    async def domain_error(request, error):
        return JSONResponse({"error": error.code}, status_code=401 if error.code == "unauthorized" else 409)

    def snapshot(sid, token):
        h.store.authorize(sid, token, True)
        return h.store.snapshot(sid)
    provider_client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    control = install_duplex_api(app, lambda: h.settings, h.store.authorize, snapshot,
        client=provider_client, policy=Policy())
    await control.start()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://restarted") as client:
        try:
            yield client, control
        finally:
            await control.close()


@pytest.mark.parametrize("outcome", ["success", "timeout", "rejected"])
def test_durable_call_reservation_survives_a_new_controller_for_every_network_outcome(tmp_path, outcome):
    async def run():
        def provider(request):
            if outcome == "timeout":
                raise httpx.ReadTimeout("PRIVATE upstream response", request=request)
            return allocation() if outcome == "success" else httpx.Response(503, text="PRIVATE rejected")
        ledger = tmp_path / "allocations.sqlite3"
        async with harness(tmp_path, provider=provider,
                           values={"SERVICEVOICE_DUPLEX_CALL_LEDGER": str(ledger)}) as h:
            first = await h.client.post(path(h.first, "call"), headers=headers(h.first))
            assert first.status_code == (200 if outcome == "success" else 502)
            assert "PRIVATE" not in first.text
            after_restart = []

            def unexpected(request):
                after_restart.append(request)
                return allocation()
            async with restarted(h, unexpected) as (client, control):
                duplicate = await client.post(path(h.first, "call"), headers=headers(h.first))
                assert duplicate.status_code == 409
                assert not after_restart and not control.calls
            with sqlite3.connect(ledger) as db:
                assert db.execute("SELECT session_id FROM allocations").fetchall() == [(h.first["id"],)]
            assert len(h.calls) == 1
    asyncio.run(run())


def test_separate_operator_controllers_share_the_same_atomic_allocation_reservation(tmp_path):
    async def run():
        async with harness(tmp_path, values={"SERVICEVOICE_DUPLEX_CALL_LEDGER": str(tmp_path / "ledger.sqlite3")}) as h:
            calls = []

            def provider(request):
                calls.append(request)
                return allocation()
            async with restarted(h, provider) as (first, _):
                async with restarted(h, provider) as (second, _):
                    answers = await asyncio.gather(
                        first.post(path(h.first, "call"), headers=headers(h.first)),
                        second.post(path(h.first, "call"), headers=headers(h.first)))
                    assert sorted(answer.status_code for answer in answers) == [200, 409]
                    assert len(calls) == 1
    asyncio.run(run())


def test_stop_uses_only_the_session_owned_call_and_is_idempotent_after_confirmation(tmp_path):
    async def run():
        def provider(request):
            if str(request.url) == "https://api.vapi.ai/call":
                return allocation()
            if request.method == "GET":
                assert str(request.url) == "https://api.vapi.ai/call/" + CALL
                return httpx.Response(200, json={"id": CALL, "assistantId": ASSISTANT,
                    "status": "in-progress", "monitor": {"controlUrl": CONTROL}})
            assert str(request.url) == CONTROL and json.loads(request.content) == {"type": "end-call"}
            return httpx.Response(202)
        async with harness(tmp_path, provider=provider) as h:
            assert (await h.client.post(path(h.first, "call"), headers=headers(h.first))).status_code == 200
            denied = await h.client.post(path(h.first, "stop"), headers=headers(h.second))
            assert denied.status_code == 401 and len(h.calls) == 1
            unknown = await h.client.post(path(h.second, "stop"), headers=headers(h.second),
                json={"call_id": CALL, "control_url": CONTROL})
            assert unknown.json() == {"ok": False, "call_known": False} and len(h.calls) == 1
            stopped = await h.client.post(path(h.first, "stop"), headers=headers(h.first),
                json={"call_id": "arbitrary-other-call", "control_url": "https://attacker.invalid"})
            assert stopped.json() == {"ok": True} and len(h.calls) == 3
            assert h.control.sessions[h.first["id"]].closed
            assert all(request.headers["authorization"] == "Bearer " + VAPI_KEY for request in h.calls)
            repeated = await h.client.post(path(h.first, "stop"), headers=headers(h.first))
            assert repeated.json() == {"ok": True, "call_known": True} and len(h.calls) == 3
            assert VAPI_KEY not in stopped.text and h.first["token"] not in stopped.text
    asyncio.run(run())


def test_stop_remains_available_for_an_ended_local_session_and_skips_already_ended_provider_call(tmp_path):
    async def run():
        def provider(request):
            if request.method == "POST":
                return allocation()
            return httpx.Response(200, json={"id": CALL, "assistantId": ASSISTANT, "status": "ended"})
        async with harness(tmp_path, provider=provider) as h:
            assert (await h.client.post(path(h.first, "call"), headers=headers(h.first))).status_code == 200
            end_session(h.store, h.first["id"])
            stopped = await h.client.post(path(h.first, "stop"), headers=headers(h.first))
            assert stopped.json() == {"ok": True, "already_ended": True} and len(h.calls) == 2
    asyncio.run(run())


@pytest.mark.parametrize("address", ["http://backend.vapi.ai/control", "https://vapi.ai.attacker.invalid/control",
    "https://attacker-vapi.ai/control", "https://user:password@backend.vapi.ai/control",
    "https://backend.vapi.ai:8443/control", "https://backend.vapi.ai/control#fragment"])
def test_stop_rejects_untrusted_live_control_addresses_without_forwarding_a_key(tmp_path, address):
    async def run():
        def provider(request):
            if request.method == "POST":
                assert str(request.url) == "https://api.vapi.ai/call", "untrusted control address contacted"
                return allocation()
            return httpx.Response(200, json={"id": CALL, "assistantId": ASSISTANT,
                "status": "in-progress", "monitor": {"controlUrl": address}})
        async with harness(tmp_path, provider=provider) as h:
            assert (await h.client.post(path(h.first, "call"), headers=headers(h.first))).status_code == 200
            stopped = await h.client.post(path(h.first, "stop"), headers=headers(h.first))
            assert stopped.json() == {"ok": False, "call_known": True} and len(h.calls) == 2
            assert "password" not in stopped.text and VAPI_KEY not in stopped.text
    asyncio.run(run())


def test_unknown_stop_outcome_is_not_reported_as_success_and_can_be_reconciled_without_reallocation(tmp_path):
    async def run():
        applied = False

        def provider(request):
            nonlocal applied
            if str(request.url) == "https://api.vapi.ai/call":
                return allocation()
            if request.method == "GET":
                return httpx.Response(200, json={"id": CALL, "assistantId": ASSISTANT,
                    "status": "ended" if applied else "in-progress", "monitor": {"controlUrl": CONTROL}})
            applied = True
            raise httpx.ReadTimeout("PRIVATE stop receipt", request=request)
        async with harness(tmp_path, provider=provider) as h:
            assert (await h.client.post(path(h.first, "call"), headers=headers(h.first))).status_code == 200
            first = await h.client.post(path(h.first, "stop"), headers=headers(h.first))
            assert first.json() == {"ok": False, "call_known": True}
            assert h.first["id"] not in h.control.stopped
            second = await h.client.post(path(h.first, "stop"), headers=headers(h.first))
            assert second.json() == {"ok": True, "already_ended": True}
            assert sum(request.method == "POST" and str(request.url) == "https://api.vapi.ai/call" for request in h.calls) == 1
            assert len(h.calls) == 4 and "PRIVATE" not in first.text
    asyncio.run(run())
