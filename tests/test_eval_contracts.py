"""Contract-runner integrity: real temporary business state, no provider calls."""
import asyncio
from pathlib import Path
import socket

import dotenv
import httpx
import pytest

from evals import contracts


def test_all_contracts_exercise_real_domain_and_pass_with_separate_fixtures(monkeypatch):
    original = contracts.Store
    paths = []

    def forbidden(*args, **kwargs):
        raise AssertionError("Offline contracts attempted credentials or provider access")

    def tracked(path):
        paths.append(Path(path))
        return original(path)

    monkeypatch.setattr(contracts, "Store", tracked)
    monkeypatch.setattr(dotenv, "dotenv_values", forbidden)
    monkeypatch.setattr(dotenv, "load_dotenv", forbidden)
    monkeypatch.setattr(httpx, "Client", forbidden)
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    rows = asyncio.run(contracts.run_contract_suite())
    assert [row["id"] for row in rows] == list(contracts.CONTRACT_IDS)
    assert len(paths) == len(set(paths)) == 12
    assert all(not path.parent.exists() for path in paths)
    assert all(row["passed"] is True and row["checks"] for row in rows), rows
    assert all(set(row) == {"id", "passed", "checks"} for row in rows)
    assert all(type(check["passed"]) is bool for row in rows for check in row["checks"])


def test_missing_time_filter_capability_fails_instead_of_skipping(monkeypatch):
    original = contracts.Fixture.search

    async def without_filter(self, *, sid=None, time_filter=None):
        return await original(self, sid=sid)

    monkeypatch.setattr(contracts, "CONTRACT_IDS", ("exact_time_availability",))
    monkeypatch.setattr(contracts.Fixture, "search", without_filter)
    row = asyncio.run(contracts.run_contract_suite())[0]
    assert row["passed"] is False
    assert any(check["passed"] is False for check in row["checks"])


def test_missing_handoff_withdrawal_fails_instead_of_skipping(monkeypatch):
    original = contracts.Fixture.call

    async def without_withdrawal(self, name, arguments, **kwargs):
        if name == "withdraw_handoff":
            return {"ok": False, "error": "unknown_tool"}
        return await original(self, name, arguments, **kwargs)

    monkeypatch.setattr(contracts, "CONTRACT_IDS", ("handoff_withdrawal_persists",))
    monkeypatch.setattr(contracts.Fixture, "call", without_withdrawal)
    row = asyncio.run(contracts.run_contract_suite())[0]
    assert row["passed"] is False
    assert any(check["passed"] is False for check in row["checks"])


def test_unexpected_errors_are_fixed_codes_and_later_contracts_continue(monkeypatch):
    original = contracts.exercise

    async def faulty(name, fixture):
        if name == "proposal_requires_confirmation":
            raise RuntimeError("PRIVATE-UPSTREAM-ERROR-MUST-NOT-EXPORT")
        await original(name, fixture)

    monkeypatch.setattr(contracts, "CONTRACT_IDS", ("proposal_requires_confirmation", "exact_retry_single_effect"))
    monkeypatch.setattr(contracts, "exercise", faulty)
    rows = asyncio.run(contracts.run_contract_suite())
    assert rows[0] == {"id": "proposal_requires_confirmation", "passed": False, "checks": [], "error": "contract_execution_failed"}
    assert rows[1]["passed"] is True
    assert "PRIVATE" not in repr(rows)


def test_empty_check_list_cannot_pass(monkeypatch):
    async def empty(name, fixture):
        return None

    monkeypatch.setattr(contracts, "CONTRACT_IDS", ("proposal_requires_confirmation",))
    monkeypatch.setattr(contracts, "exercise", empty)
    assert asyncio.run(contracts.run_contract_suite())[0]["passed"] is False


def test_cleanup_failure_cannot_pass_or_expose_error(monkeypatch):
    original = contracts.ToolHarness.close

    async def failed_close(self):
        await original(self)
        raise RuntimeError("PRIVATE-CLEANUP-DETAIL")

    monkeypatch.setattr(contracts, "CONTRACT_IDS", ("proposal_requires_confirmation",))
    monkeypatch.setattr(contracts.ToolHarness, "close", failed_close)
    row = asyncio.run(contracts.run_contract_suite())[0]
    assert row["passed"] is False and row["error"] == "contract_cleanup_failed"
    assert "PRIVATE" not in repr(row)


def test_cancellation_propagates_and_cleans_fixture(monkeypatch):
    paths = []
    original = contracts.Store

    def tracked(path):
        paths.append(Path(path))
        return original(path)

    async def interrupted(name, fixture):
        raise asyncio.CancelledError()

    monkeypatch.setattr(contracts, "CONTRACT_IDS", ("proposal_requires_confirmation",))
    monkeypatch.setattr(contracts, "Store", tracked)
    monkeypatch.setattr(contracts, "exercise", interrupted)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(contracts.run_contract_suite())
    assert len(paths) == 1 and not paths[0].parent.exists()
