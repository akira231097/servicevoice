"""Owned browser playback suppresses only the duplicate server cue path."""
import asyncio
import sqlite3

import httpx
import pytest

from test_backchannel_gateway import DONE, enable, response
from test_gemini_gateway import SIG, frame, post, request_body, setup, tool_stream
from test_pretool_speech_gate import issued, speech
from test_receipt_narration import EN, confirm, issue, post as receipt_post, prepare, setup as receipt_setup, text_of


def marker(body, value="browser-owned-v1"):
    body["call"].setdefault("assistantOverrides", {}).setdefault("variableValues", {})[
        "servicevoice_duplex_playback"] = value
    return body


@pytest.mark.parametrize("value,flag,configured,expected_server_path", [
    ("browser-owned-v1", "on", "our-assistant", False),
    (None, "on", "our-assistant", True),
    ("browser-owned-v2", "on", "our-assistant", True),
    ({"browser-owned-v1": True}, "on", "our-assistant", True),
    ("browser-owned-v1", "off", "our-assistant", False),
    ("browser-owned-v1", "on", "another-assistant", False),
])
def test_only_exact_trusted_candidate_marker_removes_server_filler_not_the_primary(
        tmp_path, monkeypatch, value, flag, configured, expected_server_path):
    app, gateway, _ = setup(tmp_path, lambda r: response("The complete primary answer."))
    enable(gateway, monkeypatch, flag=flag, assistant=configured)
    entered = []
    original = gateway._backchannel_stream

    async def observed(*args, **kwargs):
        entered.append(True)
        async for part in original(*args, **kwargs):
            yield part
    monkeypatch.setattr(gateway, "_backchannel_stream", observed)
    body = request_body()
    if value is not None:
        marker(body, value)

    async def run():
        try:
            result = await post(app, body)
            assert result.status_code == 200
            assert speech(result.text) == "The complete primary answer."
            assert bool(entered) is expected_server_path
            assert "<flush />" not in result.text
            assert bool(gateway.latency_snapshot("call-a")[-1].get("backchannel")) is expected_server_path
            assert not gateway.active and not gateway.owners
        finally:
            await gateway.close()
    asyncio.run(run())


def test_browser_owned_playback_keeps_pretool_claim_gate_and_opaque_tool_authority(tmp_path, monkeypatch):
    content = frame({"content": "PRIVATE FALSE BOOKING CLAIM"}) + tool_stream()
    app, gateway, db_path = setup(tmp_path, lambda r: httpx.Response(200,
        headers={"content-type": "text/event-stream"}, content=content))
    enable(gateway, monkeypatch)
    selected = []

    async def choose(*args, **kwargs):
        selected.append(True)
        raise AssertionError("duplicate server selector reached")
    monkeypatch.setattr(gateway.backchannel_policy, "choose", choose)

    async def run():
        try:
            result = await post(app, marker(request_body()))
            assert result.status_code == 200 and speech(result.text) == ""
            assert "PRIVATE" not in result.text and SIG not in result.text and "<flush />" not in result.text
            tools = issued(result.text)
            assert len(tools) == 1 and tools[0]["function"] == {"name": "get_appointment", "arguments": "{}"}
            restored = gateway.store.restore_group("sid-a", "call-a", tools)
            assert restored[0]["extra_content"]["google"]["thought_signature"] == SIG
            with sqlite3.connect(db_path) as db:
                assert db.execute("SELECT count(*) FROM gemini_tool_signatures").fetchone()[0] == 1
            assert not selected and not gateway.active
        finally:
            await gateway.close()
    asyncio.run(run())


def test_browser_marker_cannot_bypass_gateway_auth_or_tool_allowlists(tmp_path, monkeypatch):
    calls = []
    app, gateway, _ = setup(tmp_path, lambda request: calls.append(request) or response())
    enable(gateway, monkeypatch)

    async def run():
        try:
            denied = await post(app, marker(request_body()), auth={"authorization": "Bearer invalid"})
            assert denied.status_code == 401
            body = marker(request_body())
            body["tools"] = [{"type": "function", "function": {"name": "confirm_without_consent"}}]
            rejected = await post(app, body)
            assert rejected.status_code == 422 and not calls and not gateway.active
        finally:
            await gateway.close()
    asyncio.run(run())


def test_browser_playback_marker_keeps_readback_decline_and_single_commit_receipts(tmp_path, monkeypatch):
    env = receipt_setup(tmp_path, monkeypatch)
    env.config.update(SERVICEVOICE_BACKCHANNELS="on", VAPI_BACKCHANNEL_ASSISTANT_ID="test-assistant")
    env.gateway.backchannel_cache_ready = True

    async def run():
        try:
            proposal = await prepare(env, EN)
            readback = await receipt_post(env, marker)
            assert readback.status_code == 200
            assert "2021 Toyota Camry" in text_of(readback) and "confirm this booking?" in text_of(readback)
            assert env.store.snapshot(env.sid)["appointment"] is None
            declined = await issue(env, "confirm_action", {"proposal_id": proposal["proposal_id"],
                "confirmed": False, "presentation": EN})
            assert declined["error"] == "confirmation_required"
            assert env.store.snapshot(env.sid)["appointment"] is None
            result = await confirm(env, proposal, EN)
            assert result["ok"]
            final = await receipt_post(env, marker)
            assert final.status_code == 200 and result["appointment"]["reference"] in text_of(final)
            assert "<flush />" not in final.text and not env.sent
            with env.store.read_connection() as db:
                assert db.execute("SELECT count(*) FROM appointments WHERE session_id=?", (env.sid,)).fetchone()[0] == 1
            assert not env.gateway.active
        finally:
            await env.harness.close()
            await env.gateway.close()
    asyncio.run(run())
