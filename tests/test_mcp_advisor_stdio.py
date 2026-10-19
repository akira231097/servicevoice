"""Real SDK-to-subprocess protocol checks against disposable SQLite databases."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys

import pytest
pytest.importorskip("mcp", reason="Install requirements-mcp-lock.txt for optional MCP protocol tests.")
from mcp import Client
from app.domain import Store
from app.advisor_queue import AdvisorQueue
from app.tools import execute
from scripts.advisor_mcp_client import content, review_update, server_parameters

ROOT = Path(__file__).resolve().parents[1]


def fixture_case(tmp_path):
    store = Store(tmp_path / "case.sqlite3")
    AdvisorQueue(store)
    session = store.create_session()
    result = execute(store, session["id"], "request_handoff", {"reason": "Synthetic MCP verification"}, "mcp-fixture")
    assert result["ok"]
    return store, session, result["id"]


def original_tables(store):
    with store.connect() as db:
        names = ("sessions", "slots", "proposals", "appointments", "tool_results", "tool_invocations", "events", "transcripts", "handoffs")
        return {name: [tuple(row) for row in db.execute(f"SELECT * FROM {name} ORDER BY rowid")] for name in names}


def test_official_stdio_client_discovers_reads_updates_conflicts_and_recovers_after_restart(tmp_path):
    store, session, case_id = fixture_case(tmp_path)
    before = original_tables(store)
    async def run():
        async with Client(server_parameters(store.path, allow_status_updates=True, operator_label="MCP fixture operator"),
                          read_timeout_seconds=8, cache=None) as client:
            assert client.protocol_version == "2026-07-28"
            discovered = await client.list_tools()
            assert [tool.name for tool in discovered.tools] == ["get_advisor_case", "list_advisor_cases", "update_advisor_case_status"]
            update = next(tool for tool in discovered.tools if tool.name == "update_advisor_case_status")
            assert update.annotations.read_only_hint is False
            assert update.annotations.open_world_hint is False
            assert update.annotations.destructive_hint is True
            assert update.input_schema["additionalProperties"] is False
            assert "operator" not in update.input_schema["properties"]
            scope = json.loads((await client.read_resource("servicevoice://advisor/scope")).contents[0].text)
            assert scope["read_only"] is False and scope["operator_identity_verified"] is False
            listed = content(await client.call_tool("list_advisor_cases", {}))
            assert [row["id"] for row in listed["data"]["items"]] == [case_id]
            initial = content(await client.call_tool("get_advisor_case", {"case_id": case_id}))
            assert initial["data"]["revision"] == 0
            args = {"case_id": case_id, "expected_revision": 0, "status": "in_progress", "note": "Synthetic local review"}
            changed = content(await client.call_tool("update_advisor_case_status", args))
            assert changed["ok"] and changed["data"]["revision"] == 1
            stale = content(await client.call_tool("update_advisor_case_status", args))
            assert stale["ok"] is False and stale["error"] == "advisor_revision_conflict"
            assert len(changed["data"]["audit"]) == 1
        # A new OS subprocess has no memory of the first client but sees its receipt.
        async with Client(server_parameters(store.path), read_timeout_seconds=8, cache=None) as restarted:
            assert restarted.protocol_version == "2026-07-28"
            result = content(await restarted.call_tool("get_advisor_case", {"case_id": case_id}))
            assert result["data"]["revision"] == 1
            assert len(result["data"]["audit"]) == 1
            assert result["data"]["last_operator"] == "MCP fixture operator"
            assert session["token"] not in json.dumps(result)
            assert len((await restarted.list_tools()).tools) == 2
    asyncio.run(run())
    assert original_tables(store) == before


def test_operator_client_decline_does_not_submit_a_write_over_stdio(tmp_path):
    store, _, case_id = fixture_case(tmp_path)
    displayed = []
    async def run():
        async with Client(server_parameters(store.path, allow_status_updates=True, operator_label="Local reviewer"), read_timeout_seconds=8) as client:
            result = await review_update(client, case_id, "resolved", "Draft resolution", "Local reviewer", approve=lambda _: False, display=displayed.append)
            assert result["declined"] is True and result["mutation_submitted"] is False
            case = content(await client.call_tool("get_advisor_case", {"case_id": case_id}))["data"]
            assert case["revision"] == 0 and case["audit"] == []
    asyncio.run(run())
    assert displayed[0]["proposed_local_update"]["expected_revision"] == 0
    assert displayed[0]["proposed_local_update"]["note"] == "Draft resolution"


def test_operator_client_exact_approval_cannot_overwrite_a_concurrent_change(tmp_path):
    store, _, case_id = fixture_case(tmp_path)
    seen = []
    def approve(preview):
        seen.append(preview)
        AdvisorQueue(store).update_case(case_id, 0, "in_progress", "Another reviewer", "Concurrent review")
        return True
    async def run():
        async with Client(server_parameters(store.path, allow_status_updates=True, operator_label="Local reviewer"), read_timeout_seconds=8) as client:
            result = await review_update(client, case_id, "resolved", "Proposed closure", "Local reviewer", approve=approve, display=lambda _: None)
            assert result["error"] == "advisor_revision_conflict"
    asyncio.run(run())
    assert len(seen) == 1 and seen[0]["expected_revision"] == 0
    current = AdvisorQueue(store).get_case(case_id)
    assert current["revision"] == 1 and current["status"] == "in_progress"
    assert current["last_operator"] == "Another reviewer"


def test_client_timeout_does_not_automatically_repeat_an_update():
    from mcp.types import CallToolResult, TextContent
    class ClientStub:
        def __init__(self): self.names = []
        async def call_tool(self, name, args):
            self.names.append(name)
            if name == "get_advisor_case":
                value = {"ok": True, "data": {"revision": 4, "status": "open"}}
                return CallToolResult(content=[TextContent(type="text", text=json.dumps(value))], structured_content=value)
            raise TimeoutError()
    client = ClientStub()
    result = asyncio.run(review_update(client, "case", "resolved", "Note", "Operator", approve=lambda _: True, display=lambda _: None))
    assert result["outcome"] == "unknown"
    assert client.names == ["get_advisor_case", "update_advisor_case_status"]


def test_stdio_startup_rejects_missing_database_without_stdout_pollution_or_creation(tmp_path):
    missing = tmp_path / "missing.sqlite3"
    result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/advisor_mcp_server.py"), "--db", str(missing)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert result.stdout == ""
    assert "no database was created" in result.stderr
    assert not missing.exists()


def test_interactive_client_does_not_accept_piped_approval(tmp_path):
    store, _, case_id = fixture_case(tmp_path)
    result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/advisor_mcp_client.py"), "--db", store.path,
                             "--operator-label", "Fixture", "update", case_id, "--status", "resolved", "--note", "Fixture"],
                            input="APPLY\n", capture_output=True, text=True, timeout=10)
    assert result.returncode == 2 and "interactive terminal" in result.stderr
    assert AdvisorQueue(store).get_case(case_id)["revision"] == 0
