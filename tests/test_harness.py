"""Independent runtime contracts for bounded tools and durable mutation receipts.

All slow effects are controlled local fakes. No voice vendor or external DMS is called.
"""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app import domain
from app import harness as harness_module
from app.domain import Store
from app.harness import ToolHarness
from app.tools import execute as real_execute


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(domain, "now", lambda: datetime(2026, 10, 17, 18, tzinfo=timezone.utc))
    return Store(tmp_path / "harness.sqlite3")


def pending_booking(store):
    sid = store.create_session()["id"]
    options = real_execute(store, sid, "search_availability", {"service": "oil_change"}, "initial-search")
    proposal = real_execute(store, sid, "prepare_booking", {
        "service": "oil_change", "slot_id": options["slots"][0]["id"],
        "customer_name": "Alex Morgan", "phone": "5125550142", "vehicle": "2021 Toyota Camry",
    }, "initial-prepare")
    assert proposal["ok"]
    return sid, {"proposal_id": proposal["proposal_id"], "confirmed": True}


def active_count(store):
    with store.connect() as db:
        return db.execute("SELECT count(*) FROM appointments WHERE status='confirmed'").fetchone()[0]


def block_execute(monkeypatch):
    started, release = threading.Event(), threading.Event()
    calls = []

    def slow(store, sid, name, arguments, call_id):
        calls.append((sid, name, call_id))
        started.set()
        if not release.wait(timeout=3):
            raise RuntimeError("Test did not release its controlled effect")
        return real_execute(store, sid, name, arguments, call_id)

    monkeypatch.setattr(harness_module, "execute", slow)
    return started, release, calls


def assert_receipt(result):
    receipt = result["_receipt"]
    assert isinstance(receipt["operation_id"], str) and receipt["operation_id"]
    canonical = json.dumps({k: v for k, v in result.items() if k != "_receipt"},
                           sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert receipt["result_sha256"] == hashlib.sha256(canonical.encode()).hexdigest()
    return receipt["operation_id"]


def test_timeout_is_pending_and_eventual_committed_result_has_verifiable_receipt(store, monkeypatch):
    sid, arguments = pending_booking(store)
    _, release, calls = block_execute(monkeypatch)

    async def scenario():
        harness = ToolHarness(store, timeout_seconds=0.03, max_concurrency=2)
        try:
            result = await harness.run(sid, "confirm_action", arguments, "slow-commit")
            assert result["ok"] is False
            assert result["error"] == "operation_pending"
            assert active_count(store) == 0
            release.set()
            await harness.drain()
            completed = await harness.operation(sid, result["operation_id"])
            assert completed["ok"] is True
            assert completed["appointment"]["status"] == "confirmed"
            assert assert_receipt(completed) == result["operation_id"]
            assert active_count(store) == 1
            assert len(calls) == 1
        finally:
            release.set()
            await harness.drain()

    asyncio.run(scenario())


def test_concurrent_duplicate_requests_execute_once_and_return_identical_receipt(store, monkeypatch):
    sid, arguments = pending_booking(store)
    started, release, calls = block_execute(monkeypatch)

    async def scenario():
        harness = ToolHarness(store, timeout_seconds=1, max_concurrency=2)
        try:
            first = asyncio.create_task(harness.run(sid, "confirm_action", arguments, "same-id"))
            second = asyncio.create_task(harness.run(sid, "confirm_action", arguments, "same-id"))
            assert await asyncio.to_thread(started.wait, 1)
            release.set()
            one, two = await asyncio.gather(first, second)
            assert one == two
            assert one["ok"]
            assert_receipt(one)
            assert len(calls) == 1
            assert active_count(store) == 1
        finally:
            release.set()
            await harness.drain()

    asyncio.run(scenario())


def test_changed_payload_cannot_attach_to_inflight_operation(store, monkeypatch):
    sid, arguments = pending_booking(store)
    _, release, calls = block_execute(monkeypatch)

    async def scenario():
        harness = ToolHarness(store, timeout_seconds=0.03)
        try:
            pending = await harness.run(sid, "confirm_action", arguments, "same-id")
            assert pending["error"] == "operation_pending"
            changed = await harness.run(sid, "confirm_action", {**arguments, "confirmed": False}, "same-id")
            assert changed["ok"] is False
            assert changed["error"] == "idempotency_conflict"
            release.set()
            await harness.drain()
            assert len(calls) == 1
            assert active_count(store) == 1
        finally:
            release.set()
            await harness.drain()

    asyncio.run(scenario())


def test_receipt_lookup_is_scoped_to_its_session(store):
    sid, arguments = pending_booking(store)
    stranger = store.create_session()["id"]

    async def scenario():
        harness = ToolHarness(store)
        result = await harness.run(sid, "confirm_action", arguments, "owned-commit")
        operation_id = assert_receipt(result)
        denied = await harness.operation(stranger, operation_id)
        assert denied["ok"] is False
        assert denied["error"] == "operation_missing"
        assert result["appointment"]["reference"] not in json.dumps(denied)
        assert await harness.operation(sid, operation_id) == result
        await harness.drain()

    asyncio.run(scenario())


def test_failed_business_operation_never_becomes_a_successful_booking(store):
    sid, arguments = pending_booking(store)

    async def scenario():
        harness = ToolHarness(store)
        denied = await harness.run(sid, "confirm_action", {**arguments, "confirmed": False}, "declined")
        assert denied["ok"] is False
        assert denied["error"] == "confirmation_required"
        operation_id = assert_receipt(denied)
        lookup = await harness.operation(sid, operation_id)
        assert lookup == denied
        assert active_count(store) == 0
        await harness.drain()

    asyncio.run(scenario())


def test_unexpected_worker_exception_never_reports_success_or_reexecutes(store, monkeypatch):
    sid = store.create_session()["id"]
    attempts = []

    def broken(*args):
        attempts.append(1)
        raise RuntimeError("synthetic dependency failure")

    monkeypatch.setattr(harness_module, "execute", broken)

    async def scenario():
        harness = ToolHarness(store)
        failed = await harness.run(sid, "get_dealership_info", {}, "failed-read")
        assert failed["ok"] is False
        assert failed["error"] == "operation_failed"
        repeated = await harness.run(sid, "get_dealership_info", {}, "failed-read")
        assert repeated["ok"] is False
        assert len(attempts) == 1
        assert active_count(store) == 0
        await harness.drain()

    asyncio.run(scenario())


def test_unpersisted_worker_success_cannot_produce_a_success_receipt(store, monkeypatch):
    sid, arguments = pending_booking(store)
    monkeypatch.setattr(harness_module, "execute", lambda *args: {
        "ok": True, "appointment": {"reference": "FABRICATED", "status": "confirmed"},
    })

    async def scenario():
        harness = ToolHarness(store)
        result = await harness.run(sid, "confirm_action", arguments, "unpersisted-success")
        assert result["ok"] is False
        assert result["error"] == "operation_failed"
        assert "_receipt" not in result
        assert active_count(store) == 0
        await harness.drain()

    asyncio.run(scenario())


def test_exception_after_commit_recovers_authoritative_result_instead_of_false_failure(store, monkeypatch):
    sid, arguments = pending_booking(store)

    def commit_then_raise(*args):
        real_execute(*args)
        raise RuntimeError("Transport failed after the effect was committed")

    monkeypatch.setattr(harness_module, "execute", commit_then_raise)

    async def scenario():
        harness = ToolHarness(store)
        result = await harness.run(sid, "confirm_action", arguments, "committed-before-error")
        assert result["ok"] is True
        operation_id = assert_receipt(result)
        assert await harness.operation(sid, operation_id) == result
        assert active_count(store) == 1
        assert store.snapshot(sid)["pending_operations"] == []
        await harness.drain()

    asyncio.run(scenario())


def test_pending_admission_is_not_reported_missing_while_database_is_write_locked(store):
    sid = store.create_session()["id"]

    async def scenario():
        harness = ToolHarness(store, timeout_seconds=0.03)
        blocker = sqlite3.connect(store.path, isolation_level=None)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            pending = await harness.run(sid, "get_dealership_info", {}, "awaiting-journal")
            assert pending["error"] == "operation_pending"
            status = await asyncio.wait_for(harness.operation(sid, pending["operation_id"]), timeout=0.5)
            assert status["ok"] is False
            assert status["error"] == "operation_pending"
        finally:
            blocker.rollback()
            blocker.close()
            await harness.drain()
        completed = await harness.operation(sid, pending["operation_id"])
        assert completed["ok"] is True
        assert_receipt(completed)

    asyncio.run(scenario())


def test_concurrency_budget_bounds_worker_count_without_serializing_every_read(store, monkeypatch):
    sid = store.create_session()["id"]
    lock = threading.Lock()
    counters = {"active": 0, "peak": 0}

    def delayed(*args):
        with lock:
            counters["active"] += 1
            counters["peak"] = max(counters["peak"], counters["active"])
        try:
            time.sleep(0.04)  # Simulated I/O, deliberately outside the SQLite transaction.
            return real_execute(*args)
        finally:
            with lock:
                counters["active"] -= 1

    monkeypatch.setattr(harness_module, "execute", delayed)

    async def scenario():
        harness = ToolHarness(store, timeout_seconds=2, max_concurrency=2)
        results = await asyncio.gather(*[
            harness.run(sid, "get_dealership_info", {}, f"read-{i}") for i in range(6)
        ])
        assert all(result["ok"] for result in results)
        assert counters["peak"] == 2
        assert counters["active"] == 0
        await harness.drain()

    asyncio.run(scenario())


def test_wait_deadline_includes_time_in_admission_queue(store, monkeypatch):
    sid = store.create_session()["id"]
    started, release, calls = block_execute(monkeypatch)

    async def scenario():
        harness = ToolHarness(store, timeout_seconds=0.03, max_concurrency=1)
        try:
            first = await harness.run(sid, "get_dealership_info", {}, "occupies-worker")
            assert first["error"] == "operation_pending"
            # Admission can outlast the short caller deadline on a busy host.
            # Establish worker occupancy before testing the next request's wait.
            assert await asyncio.to_thread(started.wait, 2)
            queued = await asyncio.wait_for(
                harness.run(sid, "get_dealership_info", {}, "queued-read"), timeout=0.5,
            )
            assert queued["error"] == "operation_pending"
            assert len(calls) == 1
            release.set()
            await harness.drain()
            assert (await harness.operation(sid, queued["operation_id"]))["ok"]
            assert len(calls) == 2
        finally:
            release.set()
            await harness.drain()

    asyncio.run(scenario())


def test_finite_backlog_rejects_new_work_but_attaches_exact_duplicates(store, monkeypatch):
    sid = store.create_session()["id"]
    _, release, calls = block_execute(monkeypatch)

    async def scenario():
        harness = ToolHarness(store, timeout_seconds=0.03, max_concurrency=2, max_pending=2)
        try:
            accepted = await asyncio.gather(*[
                harness.run(sid, "get_dealership_info", {}, call_id) for call_id in ("active-1", "active-2")
            ])
            assert all(result["error"] == "operation_pending" for result in accepted)
            rejected = await harness.run(sid, "get_dealership_info", {}, "over-limit")
            assert rejected["error"] == "harness_overloaded"
            assert rejected["admitted"] is False
            with store.connect() as db:
                assert db.execute("SELECT count(*) FROM tool_invocations WHERE call_id='over-limit'").fetchone()[0] == 0
                assert db.execute("SELECT count(*) FROM tool_results WHERE call_id='over-limit'").fetchone()[0] == 0
            duplicate = await harness.run(sid, "get_dealership_info", {}, "active-1")
            assert duplicate["error"] == "operation_pending"
            assert duplicate["operation_id"] == accepted[0]["operation_id"]
            assert (await harness.operation(sid, duplicate["operation_id"]))["error"] == "operation_pending"
            release.set()
            await harness.drain()
            assert len(calls) == 2
            retried = await harness.run(sid, "get_dealership_info", {}, "over-limit")
            await harness.drain()
            if retried.get("error") == "operation_pending":
                retried = await harness.operation(sid, retried["operation_id"])
            assert retried["ok"] is True
            assert_receipt(retried)
            assert len(calls) == 3
        finally:
            release.set()
            await harness.drain()

    asyncio.run(scenario())


def test_admission_queue_is_serial_while_deadlines_and_status_reads_remain_responsive(store, monkeypatch):
    sid = store.create_session()["id"]
    release = threading.Event()
    registrar_entered = threading.Event()
    entered = []

    async def scenario():
        harness = ToolHarness(store, timeout_seconds=0.03, max_concurrency=2, max_pending=4, max_registrars=1)
        original_register = harness._register

        def slow_register(*args):
            entered.append(1)
            registrar_entered.set()
            if not release.wait(timeout=3):
                raise RuntimeError("Test admission was not released")
            return original_register(*args)

        monkeypatch.setattr(harness, "_register", slow_register)
        try:
            results = await asyncio.wait_for(asyncio.gather(*[
                harness.run(sid, "get_dealership_info", {}, f"queued-{i}") for i in range(3)
            ]), timeout=1)
            assert all(result["error"] == "operation_pending" for result in results)
            # A 30 ms caller deadline may elapse before the worker gets CPU.
            # Observe entry before checking exclusivity; keep the response
            # deadline and single-registrar assertion unchanged.
            assert await asyncio.to_thread(registrar_entered.wait, 1), "Registrar never entered"
            assert len(entered) == 1, "Only one registrar may occupy the writer/threadpool wait"
            for result in results:
                status = await asyncio.wait_for(harness.operation(sid, result["operation_id"]), timeout=.5)
                assert status["error"] == "operation_pending"
            release.set()
            await harness.drain()
            assert len(entered) == 3
            completed = await asyncio.gather(*(harness.operation(sid, result["operation_id"]) for result in results))
            assert all(result["ok"] for result in completed)
        finally:
            release.set()
            await harness.drain()

    asyncio.run(scenario())


def test_configurable_registrars_are_bounded_and_do_not_block_status_reads(store, monkeypatch):
    sid = store.create_session()["id"]
    entered, peak = 0, 0
    lock, release, four_started = threading.Lock(), threading.Event(), threading.Event()

    async def scenario():
        harness = ToolHarness(store, timeout_seconds=.03, max_registrars=4, max_pending=8)
        original = harness._register

        def blocked(*args):
            nonlocal entered, peak
            with lock:
                entered += 1
                peak = max(peak, entered)
                if entered == 4:
                    four_started.set()
            try:
                if not release.wait(3):
                    raise RuntimeError("Test registrar was not released")
                return original(*args)
            finally:
                with lock:
                    entered -= 1

        monkeypatch.setattr(harness, "_register", blocked)
        try:
            results = await asyncio.gather(*[
                harness.run(sid, "get_dealership_info", {}, f"registrar-{index}") for index in range(8)
            ])
            assert await asyncio.to_thread(four_started.wait, 1)
            assert peak == 4
            assert all(result["error"] == "operation_pending" for result in results)
            assert (await asyncio.wait_for(harness.operation(sid, results[-1]["operation_id"]), .5))["error"] == "operation_pending"
            release.set()
            await harness.close()
            assert peak == 4
            assert entered == 0
            # This checks registrar completion, not admission beyond the
            # separately bounded status-read lane.
            completed = [await harness.operation(sid, result["operation_id"]) for result in results]
            assert all(result["ok"] for result in completed)
        finally:
            release.set()
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("capacity", [0, -1, True, 1.5, "4"])
def test_invalid_registrar_capacity_is_rejected(store, capacity):
    with pytest.raises(ValueError):
        ToolHarness(store, max_registrars=capacity)


def test_close_stops_admission_but_finishes_accepted_work_and_keeps_status_readable(store, monkeypatch):
    sid, arguments = pending_booking(store)
    started, release, calls = block_execute(monkeypatch)

    async def scenario():
        harness = ToolHarness(store, timeout_seconds=0.03, max_concurrency=1)
        closer = None
        try:
            first = await harness.run(sid, "confirm_action", arguments, "accepted-commit")
            assert first["error"] == "operation_pending"
            assert await asyncio.to_thread(started.wait, 1)
            queued = await harness.run(sid, "get_dealership_info", {}, "accepted-queued")
            assert queued["error"] == "operation_pending"
            closer = asyncio.create_task(harness.close())
            await asyncio.sleep(0)
            assert harness.closing
            assert not closer.done()

            rejected = await harness.run(sid, "confirm_action", arguments, "late-commit")
            assert rejected["error"] == "harness_closed"
            assert rejected["admitted"] is False
            with store.connect() as db:
                assert db.execute("SELECT count(*) FROM tool_invocations WHERE call_id='late-commit'").fetchone()[0] == 0
                assert db.execute("SELECT count(*) FROM tool_results WHERE call_id='late-commit'").fetchone()[0] == 0
            duplicate = await harness.run(sid, "confirm_action", arguments, "accepted-commit")
            assert duplicate["error"] == "operation_pending"
            assert duplicate["operation_id"] == first["operation_id"]
            status = await harness.run(sid, "get_operation", {"operation_id": first["operation_id"]}, "status-during-close")
            assert status["error"] == "operation_pending"
            assert not closer.done()

            release.set()
            await asyncio.wait_for(closer, 2)
            assert not any(not task.done() for _, _, task in harness.inflight.values())
            assert len(calls) == 2
            assert active_count(store) == 1
            completed = await harness.operation(sid, first["operation_id"])
            assert completed["ok"]
            assert_receipt(completed)
            assert (await harness.operation(sid, queued["operation_id"]))["ok"]
            assert (await harness.run(sid, "get_dealership_info", {}, "after-close"))["error"] == "harness_closed"
            await harness.close()  # Repeated shutdown is harmless.
        finally:
            release.set()
            await harness.close()
            if closer is not None:
                await closer

    asyncio.run(scenario())


def test_canceled_close_waiter_does_not_cancel_effect_or_reopen_admission(store, monkeypatch):
    sid, arguments = pending_booking(store)
    started, release, calls = block_execute(monkeypatch)

    async def scenario():
        harness = ToolHarness(store, timeout_seconds=0.03)
        try:
            accepted = await harness.run(sid, "confirm_action", arguments, "close-cancel")
            assert accepted["error"] == "operation_pending"
            assert await asyncio.to_thread(started.wait, 1)
            closer = asyncio.create_task(harness.close())
            await asyncio.sleep(0)
            closer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await closer
            assert harness.closing
            assert (await harness.run(sid, "get_dealership_info", {}, "late"))["admitted"] is False
            release.set()
            await harness.close()
            assert (await harness.operation(sid, accepted["operation_id"]))["ok"]
            assert len(calls) == active_count(store) == 1
        finally:
            release.set()
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("name", [[], {}, None, 42])
def test_non_string_tool_name_is_rejected_without_admission(store, name):
    sid = store.create_session()["id"]

    async def scenario():
        harness = ToolHarness(store)
        result = await harness.run(sid, name, {}, "invalid-tool")
        assert result["error"] == "unknown_tool"
        assert harness.inflight == {}
        with store.connect() as db:
            assert db.execute("SELECT count(*) FROM tool_invocations").fetchone()[0] == 0
        await harness.close()

    asyncio.run(scenario())


def test_durable_result_can_be_recovered_by_fresh_harness_without_reexecution(store, monkeypatch):
    sid, arguments = pending_booking(store)
    calls = []

    def counted(*args):
        calls.append(1)
        return real_execute(*args)

    monkeypatch.setattr(harness_module, "execute", counted)

    async def scenario():
        original = ToolHarness(store)
        result = await original.run(sid, "confirm_action", arguments, "durable-commit")
        operation_id = assert_receipt(result)
        await original.drain()
        restarted = ToolHarness(Store(store.path))
        assert await restarted.operation(sid, operation_id) == result
        assert await restarted.run(sid, "confirm_action", arguments, "durable-commit") == result
        assert len(calls) == 1
        assert active_count(store) == 1
        await restarted.drain()

    asyncio.run(scenario())


def test_fresh_harness_does_not_blindly_retry_unresolved_invocation(store, monkeypatch):
    sid, arguments = pending_booking(store)
    started, release, calls = block_execute(monkeypatch)

    async def scenario():
        original = ToolHarness(store, timeout_seconds=0.03)
        # The original has a deliberately short transport deadline; the recovery
        # assertion must not depend on a fresh SQLite lookup finishing within 30ms.
        restarted = ToolHarness(store, timeout_seconds=1)
        try:
            pending = await original.run(sid, "confirm_action", arguments, "unknown-commit")
            assert pending["error"] == "operation_pending"
            assert await asyncio.to_thread(started.wait, 1)
            unknown = await restarted.operation(sid, pending["operation_id"])
            assert unknown["ok"] is False
            assert unknown["error"] == "operation_unknown"
            repeated = await restarted.run(sid, "confirm_action", arguments, "unknown-commit")
            assert repeated["ok"] is False
            assert repeated["error"] == "operation_unknown"
            assert len(calls) == 1
            release.set()
            await original.drain()
            final = await restarted.operation(sid, pending["operation_id"])
            assert final["ok"] is True
            assert_receipt(final)
            assert active_count(store) == 1
        finally:
            release.set()
            await original.drain()
            await restarted.drain()

    asyncio.run(scenario())


def test_cancelling_http_waiter_does_not_cancel_or_duplicate_accepted_effect(store, monkeypatch):
    sid, arguments = pending_booking(store)
    started, release, calls = block_execute(monkeypatch)

    async def scenario():
        harness = ToolHarness(store, timeout_seconds=2)
        try:
            waiter = asyncio.create_task(harness.run(sid, "confirm_action", arguments, "cancelled-waiter"))
            assert await asyncio.to_thread(started.wait, 1)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            release.set()
            await harness.drain()
            recovered = await harness.run(sid, "confirm_action", arguments, "cancelled-waiter")
            assert recovered["ok"] is True
            assert_receipt(recovered)
            assert active_count(store) == 1
            assert len(calls) == 1
        finally:
            release.set()
            await harness.drain()

    asyncio.run(scenario())


def test_invalid_arguments_rejected_before_effect_execution(store, monkeypatch):
    sid = store.create_session()["id"]
    calls = []

    def should_not_run(*args):
        calls.append(1)
        raise AssertionError("Invalid arguments must never reach the worker")

    monkeypatch.setattr(harness_module, "execute", should_not_run)

    async def scenario():
        harness = ToolHarness(store)
        result = await harness.run(sid, "confirm_action", {"proposal_id": "fake", "confirmed": "true"}, "invalid")
        assert result["ok"] is False
        assert result["error"] == "invalid_arguments"
        assert not calls
        await harness.drain()

    asyncio.run(scenario())


def test_fused_begin_matches_available_slots_without_preparing_or_committing(store):
    sid = store.create_session()["id"]
    expected = real_execute(store, sid, "search_availability", {"service": "oil_change"}, "comparison-read")

    async def scenario():
        harness = ToolHarness(store)
        fused = await harness.run(sid, "begin_service_request", {"service": "oil_change"}, "fused-begin")
        assert fused["ok"] is True
        assert fused["slots"] == expected["slots"]
        assert "America/Chicago" in json.dumps(fused)
        assert store.snapshot(sid)["proposal"] is None
        assert active_count(store) == 0
        assert_receipt(fused)
        await harness.drain()

    asyncio.run(scenario())


def test_fused_begin_invalidates_previous_pending_proposal_but_never_confirms_it(store):
    sid, arguments = pending_booking(store)

    async def scenario():
        harness = ToolHarness(store)
        fused = await harness.run(sid, "begin_service_request", {"service": "brakes"}, "new-intent")
        assert fused["ok"] is True
        stale = await harness.run(sid, "confirm_action", arguments, "stale-confirm")
        assert stale["ok"] is False
        assert stale["error"] == "stale_proposal"
        assert active_count(store) == 0
        await harness.drain()

    asyncio.run(scenario())


CRASH_CHILD = r'''
import asyncio
from datetime import datetime, timezone
import json
import os
import sys
from app import domain
from app import harness as runtime
from app.domain import Store
from app.tools import execute as domain_execute

data = json.loads(sys.stdin.read())
domain.now = lambda: datetime(2026, 10, 17, 18, tzinfo=timezone.utc)
store = Store(data["database"])

def child_effect(*args):
    if data["scenario"] == "committed":
        domain_execute(*args)
        os._exit(91)
    os._exit(92)

runtime.execute = child_effect

async def main():
    harness = runtime.ToolHarness(store, timeout_seconds=5)
    if data["scenario"] == "queued":
        mark = harness._mark
        def exit_before_running(sid, opid, status, detail=None):
            if status == "running":
                os._exit(92)
            return mark(sid, opid, status, detail)
        harness._mark = exit_before_running
    await harness.run(data["session_id"], "confirm_action", data["arguments"], data["call_id"])
    raise AssertionError("Disposable test child should have exited at the injected crash")

asyncio.run(main())
'''


def run_crash_child(store, sid, arguments, scenario, call_id):
    # This process exists solely for this test and touches only its temporary database.
    completed = subprocess.run(
        [sys.executable, "-c", CRASH_CHILD],
        input=json.dumps({"database": store.path, "session_id": sid, "arguments": arguments,
                          "scenario": scenario, "call_id": call_id}),
        cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, timeout=12,
    )
    assert completed.returncode == (91 if scenario == "committed" else 92), completed.stderr
    with store.connect() as db:
        row = db.execute("SELECT * FROM tool_invocations WHERE session_id=? AND call_id=?", (sid, call_id)).fetchone()
    assert row is not None
    return dict(row)


def test_actual_process_exit_after_commit_recovers_one_authoritative_booking(store, monkeypatch):
    sid, arguments = pending_booking(store)
    row = run_crash_child(store, sid, arguments, "committed", "child-committed")
    assert row["status"] == "running", "The child must exit before journal completion"
    assert active_count(store) == 1
    executed = []

    def no_reexecution(*args):
        executed.append(1)
        raise AssertionError("A committed child result must be reconciled, never executed again")

    monkeypatch.setattr(harness_module, "execute", no_reexecution)

    async def scenario():
        restarted = ToolHarness(Store(store.path))
        recovered = await restarted.operation(sid, row["id"])
        assert recovered["ok"] is True
        assert recovered["appointment"]["status"] == "confirmed"
        assert assert_receipt(recovered) == row["id"]
        assert await restarted.run(sid, "confirm_action", arguments, "child-committed") == recovered
        assert active_count(store) == 1
        assert executed == []
        assert store.snapshot(sid)["pending_operations"] == []
        await restarted.drain()

    asyncio.run(scenario())


@pytest.mark.parametrize("crash_phase", ["queued", "running"])
def test_actual_orphaned_process_invocation_stays_unknown_without_reexecution(store, monkeypatch, crash_phase):
    sid, arguments = pending_booking(store)
    call_id = "child-orphan-" + crash_phase
    row = run_crash_child(store, sid, arguments, crash_phase, call_id)
    assert row["status"] == crash_phase
    assert active_count(store) == 0
    executed = []

    def no_reexecution(*args):
        executed.append(1)
        raise AssertionError("Unknown work cannot be blindly reexecuted")

    monkeypatch.setattr(harness_module, "execute", no_reexecution)

    async def scenario():
        restarted = ToolHarness(Store(store.path))
        unknown = await restarted.operation(sid, row["id"])
        assert unknown["ok"] is False
        assert unknown["error"] == "operation_unknown"
        retry = await restarted.run(sid, "confirm_action", arguments, call_id)
        assert retry["ok"] is False
        assert retry["error"] == "operation_unknown"
        assert executed == []
        assert active_count(store) == 0
        await restarted.drain()

    asyncio.run(scenario())
