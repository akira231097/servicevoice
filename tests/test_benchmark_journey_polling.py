"""The offline journey runner must wait honestly without repeating a mutation."""
import asyncio
from datetime import datetime, timezone
import time

import pytest

from app import domain
from app.domain import Store
from app.harness import ToolHarness
from app.tools import execute
from scripts.benchmark_journey import CompletionDeadline, receipt_ok, run_until_settled


class Clock:
    def __init__(self):
        self.value = 0.0
        self.sleeps = []

    def now(self):
        return self.value

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


def pending(opid="op-1"):
    return {"ok": False, "error": "operation_pending", "operation_id": opid}


def unavailable(opid="op-1", retry_after_ms=250):
    return {"ok": False, "error": "status_unavailable", "operation_id": opid,
            "reason": "capacity", "retry_after_ms": retry_after_ms}


class StatusSequence:
    def __init__(self, statuses):
        self.statuses = iter(statuses)
        self.calls = []

    async def run(self, sid, name, arguments, call_id):
        self.calls.append((sid, name, arguments, call_id))
        return pending() if name != "get_operation" else next(self.statuses)


def test_unavailable_then_pending_recovers_real_receipt_with_one_booking(tmp_path, monkeypatch):
    monkeypatch.setattr(domain, "now", lambda: datetime(2026, 10, 17, 18, tzinfo=timezone.utc))
    store = Store(tmp_path / "booking.sqlite3")
    sid = store.create_session()["id"]
    offers = execute(store, sid, "search_availability", {"service": "oil_change"}, "search")
    proposal = execute(store, sid, "prepare_booking", {
        "service": "oil_change", "slot_id": offers["slots"][0]["id"],
        "customer_name": "Synthetic Caller", "phone": "5125550142", "vehicle": "2021 Toyota Camry",
    }, "prepare")
    arguments = {"proposal_id": proposal["proposal_id"], "confirmed": True}

    async def scenario():
        real = ToolHarness(store)
        clock, calls, committed = Clock(), [], None

        class WithheldStatus:
            async def run(self, session_id, name, args, call_id):
                nonlocal committed
                calls.append((session_id, name, args, call_id))
                if name != "get_operation":
                    committed = await real.run(session_id, name, args, call_id)
                    return pending(committed["_receipt"]["operation_id"])
                if len(calls) == 2:
                    return unavailable(args["operation_id"])
                if len(calls) == 3:
                    return pending(args["operation_id"])
                return await real.run(session_id, name, args, call_id)

        try:
            observed = await run_until_settled(WithheldStatus(), sid, "confirm_action", arguments, "commit",
                                               clock=clock.now, sleep=clock.sleep)
            assert observed["result"] == committed
            receipt_ok(observed["result"])
            assert observed["pending"] is True
            assert observed["status_polls"] == 3
            assert observed["status_unavailable_count"] == 1
            assert clock.sleeps == [.1, .25, .1]
            assert observed["duration_ms"] == pytest.approx(450)
            assert [call[1] for call in calls] == ["confirm_action"] + ["get_operation"] * 3
            assert all(call[0] == sid and call[2] == {"operation_id": committed["_receipt"]["operation_id"]}
                       for call in calls[1:])
            assert [call[3] for call in calls[1:]] == ["commit-poll-1", "commit-poll-2", "commit-poll-3"]
            with store.connect() as db:
                assert db.execute("SELECT count(*) FROM appointments").fetchone()[0] == 1
                assert db.execute("SELECT count(*) FROM tool_results WHERE call_id='commit'").fetchone()[0] == 1
        finally:
            await real.close()

    asyncio.run(scenario())


def test_unavailable_only_expires_once_without_repeating_write():
    async def scenario():
        clock = Clock()
        harness = StatusSequence([unavailable()] * 10)
        with pytest.raises(CompletionDeadline) as captured:
            await run_until_settled(harness, "session", "confirm_action", {}, "commit",
                                   completion_cap_seconds=.7, clock=clock.now, sleep=clock.sleep)
        assert clock.value == pytest.approx(.7)
        assert clock.sleeps == pytest.approx([.1, .25, .25, .1])
        assert captured.value.operation_id == "op-1"
        assert captured.value.last_result["error"] == "status_unavailable"
        assert captured.value.status_polls == 3
        assert "outcome unverified" in str(captured.value)
        assert [call[1] for call in harness.calls] == ["confirm_action"] + ["get_operation"] * 3
        assert all(call[2] == {"operation_id": "op-1"} for call in harness.calls[1:])

    asyncio.run(scenario())


def test_retry_hint_longer_than_remaining_budget_never_causes_early_poll():
    async def scenario():
        clock = Clock()
        harness = StatusSequence([unavailable(retry_after_ms=10_000)])
        with pytest.raises(CompletionDeadline):
            await run_until_settled(harness, "session", "confirm_action", {}, "commit",
                                   completion_cap_seconds=.3, clock=clock.now, sleep=clock.sleep)
        assert clock.sleeps == pytest.approx([.1, .2])
        assert len(harness.calls) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("terminal", ["operation_unknown", "operation_failed", "operation_missing"])
def test_unknown_failed_and_missing_are_distinct_terminal_outcomes_not_retries(terminal):
    async def scenario():
        clock = Clock()
        result = {"ok": False, "error": terminal, "operation_id": "op-1"}
        harness = StatusSequence([result])
        observed = await run_until_settled(harness, "session", "confirm_action", {}, "commit",
                                         clock=clock.now, sleep=clock.sleep)
        assert observed["result"] == result
        assert "_receipt" not in observed["result"]
        assert len(harness.calls) == 2

    asyncio.run(scenario())


def test_slow_status_await_is_inside_overall_deadline():
    async def scenario():
        calls, read_cancelled = [], asyncio.Event()

        class HangingRead:
            async def run(self, sid, name, arguments, call_id):
                calls.append(name)
                if name != "get_operation":
                    return pending()
                try:
                    await asyncio.Event().wait()
                finally:
                    read_cancelled.set()

        started = time.perf_counter()
        with pytest.raises(CompletionDeadline) as captured:
            await run_until_settled(HangingRead(), "session", "confirm_action", {}, "commit",
                                   completion_cap_seconds=.15)
        assert time.perf_counter() - started < 1
        assert read_cancelled.is_set()
        assert calls == ["confirm_action", "get_operation"]
        assert captured.value.last_result["error"] == "operation_pending"

    asyncio.run(scenario())


@pytest.mark.parametrize("retry_ms", [None, "250", True, -1, float("nan"), float("inf")])
def test_invalid_retry_hint_uses_existing_poll_floor(retry_ms):
    async def scenario():
        clock = Clock()
        terminal = {"ok": False, "error": "operation_unknown", "operation_id": "op-1"}
        harness = StatusSequence([unavailable(retry_after_ms=retry_ms), terminal])
        observed = await run_until_settled(harness, "session", "confirm_action", {}, "commit",
                                         clock=clock.now, sleep=clock.sleep)
        assert clock.sleeps == [.1, .1]
        assert observed["status_unavailable_count"] == 1
        assert observed["result"] == terminal

    asyncio.run(scenario())


def test_status_cannot_redirect_polling_to_another_operation():
    async def scenario():
        clock = Clock()
        harness = StatusSequence([unavailable(opid="other-operation")])
        with pytest.raises(AssertionError, match="changed the operation ID"):
            await run_until_settled(harness, "session", "confirm_action", {}, "commit",
                                   clock=clock.now, sleep=clock.sleep)
        assert len(harness.calls) == 2
        assert harness.calls[-1][2] == {"operation_id": "op-1"}

    asyncio.run(scenario())
