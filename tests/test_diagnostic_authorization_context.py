"""Optional diagnostic joins must never weaken or delay tool authorization.

These tests use disposable local databases and no voice/model provider.
"""
import asyncio
from contextlib import closing
from types import SimpleNamespace

import pytest

from app.domain import Store
from app.gemini_gateway import GatewayError, GatewayLimits, SignatureStore
from app.harness import ToolHarness
from app.issuance_context import user_context


@pytest.fixture
def realm(tmp_path):
    business = Store(tmp_path / "evidence.sqlite3")
    sid = business.create_session()["id"]
    signatures = SignatureStore(business.path, GatewayLimits())
    signatures.pin_call_policy(sid, "synthetic-call", "synthetic-assistant",
                               {"VAPI_MODEL_PROFILE": "gemini-3.8-flash-custom"})
    return SimpleNamespace(business=business, signatures=signatures, sid=sid,
                           cid="synthetic-call")


def issue(realm, tool_id="issued", ordinal=3):
    tool = {"id": tool_id, "function": {"name": "get_dealership_info", "arguments": "{}"},
            "extra_content": {"google": {"thought_signature": "synthetic-signature"}}}
    context = user_context([{"role": "user", "content": "Synthetic question"}] * ordinal)
    realm.signatures.save_group(realm.sid, realm.cid, [tool], context=context)
    return (tool_id, "get_dealership_info", {}, {})


def test_authorized_batch_returns_each_issued_ordinal_even_when_reordered(realm):
    first = issue(realm, "first", 2)
    second = issue(realm, "second", 5)
    assert realm.signatures.authorize_tool_batch(realm.sid, realm.cid, [second, first]) == {
        "first": {"user_count": 2}, "second": {"user_count": 5}}


@pytest.mark.parametrize("extras", [
    "not-json", "[]", "null", "true", "{}",
    '{"__servicevoice_issuance_context":[]}',
    '{"__servicevoice_issuance_context":{"user_count":true}}',
    '{"__servicevoice_issuance_context":{"user_count":"3"}}',
    '{"__servicevoice_issuance_context":{"user_count":0}}',
    '{"__servicevoice_issuance_context":{"user_count":-1}}',
    '{"__servicevoice_issuance_context":{"user_count":100001}}',
    "[" * 5000 + "0" + "]" * 5000,
], ids=["non-json", "list", "null", "boolean", "missing", "context-list", "boolean-ordinal",
        "string-ordinal", "zero-ordinal", "negative-ordinal", "oversized-ordinal", "deeply-nested"])
def test_unreadable_or_invalid_optional_context_does_not_reject_authorized_tool(realm, extras):
    command = issue(realm)
    with closing(realm.signatures.connect()) as db:
        db.execute("UPDATE gemini_tool_signatures SET extras=?", (extras,))
        db.commit()
    assert realm.signatures.authorize_tool_batch(realm.sid, realm.cid, [command]) == {}
    with realm.business.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM tool_invocations").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM tool_results").fetchone()[0] == 0


@pytest.mark.parametrize("mutation,expected", [
    ("expired", "tool_not_issued"),
    ("fingerprint", "tool_issue_mismatch"),
    ("wrong_call", "tool_not_issued"),
    ("wrong_session", "tool_not_issued"),
])
def test_valid_diagnostic_context_never_bypasses_issuance_checks(realm, mutation, expected):
    command = issue(realm)
    statements = {
        "expired": "UPDATE gemini_tool_signatures SET expires=0",
        "fingerprint": "UPDATE gemini_tool_signatures SET fingerprint='incorrect'",
        "wrong_call": "UPDATE gemini_tool_signatures SET provider_call_id='other-call'",
        "wrong_session": "UPDATE gemini_tool_signatures SET session_id='other-session'",
    }
    with closing(realm.signatures.connect()) as db:
        db.execute(statements[mutation])
        db.commit()
    with pytest.raises(GatewayError) as caught:
        realm.signatures.authorize_tool_batch(realm.sid, realm.cid, [command])
    assert caught.value.code == expected


def test_context_projection_reuses_existing_authorization_connection_and_select(realm, monkeypatch):
    command = issue(realm)
    original = realm.signatures.connect
    connections, statements = [], []
    def tracked_connect():
        db = original()
        db.set_trace_callback(statements.append)
        connections.append(db)
        return db
    def forbidden_second_context_read(*args, **kwargs):
        raise AssertionError("Optional context must not add a second lookup")
    monkeypatch.setattr(realm.signatures, "connect", tracked_connect)
    monkeypatch.setattr(realm.signatures, "diagnostic_tool_context", forbidden_second_context_read)
    assert realm.signatures.authorize_tool_batch(realm.sid, realm.cid, [command]) == {
        "issued": {"user_count": 3}}
    assert len(connections) == 1
    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    # Policy, existing invocation, existing receipt, and original signature read.
    assert len(selects) == 4
    assert sum("FROM gemini_tool_signatures" in s for s in selects) == 1
    assert not any(s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for s in statements)


def test_missing_replay_context_keeps_existing_receipt_recoverable_without_reexecution(realm, monkeypatch):
    command = issue(realm)
    async def scenario():
        harness = ToolHarness(realm.business)
        try:
            assert realm.signatures.authorize_tool_batch(realm.sid, realm.cid, [command])
            first = await harness.run(realm.sid, command[1], command[2], command[0])
            assert first["ok"] and first["_receipt"]
            with closing(realm.signatures.connect()) as db:
                db.execute("DELETE FROM gemini_tool_signatures")
                db.commit()
            def forbidden_execution(*args, **kwargs):
                raise AssertionError("Replay must return the committed result")
            monkeypatch.setattr("app.harness.execute", forbidden_execution)
            assert realm.signatures.authorize_tool_batch(realm.sid, realm.cid, [command]) == {}
            replay = await harness.run(realm.sid, command[1], command[2], command[0])
            assert replay == first
            with realm.business.connect() as db:
                assert db.execute("SELECT COUNT(*) FROM tool_invocations").fetchone()[0] == 1
                assert db.execute("SELECT COUNT(*) FROM tool_results").fetchone()[0] == 1
        finally:
            await harness.close()
    asyncio.run(scenario())
