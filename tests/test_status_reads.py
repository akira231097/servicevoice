"""Outcome reads stay bounded without misclassifying an accepted mutation.

These tests use disposable databases and controlled local worker delays only.
"""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time

import pytest

from app import domain
from app import harness as harness_module
from app.domain import Store
from app.harness import ToolHarness
from app.tools import execute as real_execute


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(domain, "now", lambda: datetime(2026, 10, 17, 18, tzinfo=timezone.utc))
    return Store(tmp_path/"status.sqlite3")


def prepared(store):
    sid = store.create_session()["id"]
    offers = real_execute(store, sid, "search_availability", {"service": "oil_change"}, "search")
    proposal = real_execute(store, sid, "prepare_booking", {
        "service": "oil_change", "slot_id": offers["slots"][0]["id"], "customer_name": "Alex Morgan",
        "phone": "5125550142", "vehicle": "2021 Toyota Camry"}, "prepare")
    return sid, {"proposal_id": proposal["proposal_id"], "confirmed": True}


def verify_receipt(result):
    raw = {key: value for key, value in result.items() if key != "_receipt"}
    encoded = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert result["_receipt"]["result_sha256"] == hashlib.sha256(encoded.encode()).hexdigest()


async def settle_reads(harness):
    reads = list(harness.status_reads.values())
    if reads:
        await asyncio.gather(*(asyncio.shield(read) for read in reads), return_exceptions=True)
    await asyncio.sleep(0)  # Allow completion callbacks to release bookkeeping.


@pytest.fixture
def completed_status_setup(complete_pending_tool_results_async):
    """Finish the real fixture commit before strict status-read tests begin."""
    async def commit(harness, sid, arguments):
        original = await harness.run(sid, "confirm_action", arguments, "commit")
        preserved = json.dumps(original, sort_keys=True)
        [completed] = await complete_pending_tool_results_async(harness, sid, [original])
        assert json.dumps(original, sort_keys=True) == preserved
        assert completed.get("ok") is True and "_receipt" in completed, {
            "initial_ok": original.get("ok"), "initial_error": original.get("error"),
            "completed_ok": completed.get("ok"), "completed_error": completed.get("error"),
            "completed_reason": completed.get("reason"), "receipt_present": "_receipt" in completed,
        }
        verify_receipt(completed)
        return completed
    return commit


def test_timed_out_duplicate_polls_keep_one_worker_and_later_read_fresh_receipt(
        store, monkeypatch, completed_status_setup):
    sid, arguments = prepared(store)
    started, release = threading.Event(), threading.Event()
    calls = []

    async def scenario():
        harness = ToolHarness(store, status_timeout_seconds=.04)
        committed = await completed_status_setup(harness, sid, arguments)
        opid = committed["_receipt"]["operation_id"]
        original = harness._lookup_status

        def slow(*args):
            calls.append(args)
            started.set()
            assert release.wait(3), "Test did not release status worker"
            return original(*args)

        monkeypatch.setattr(harness, "_lookup_status", slow)
        try:
            began = time.perf_counter()
            results = await asyncio.gather(*(harness.operation(sid, opid) for _ in range(20)))
            assert time.perf_counter()-began < .75
            assert started.is_set()
            assert all(result["error"] == "status_unavailable" and result["reason"] == "deadline" for result in results)
            assert all("_receipt" not in result for result in results)
            assert len(calls) == len(harness.status_reads) == 1
            repeated = await asyncio.gather(*(harness.operation(sid, opid) for _ in range(20)))
            assert all(result["error"] == "status_unavailable" for result in repeated)
            assert len(calls) == 1, "Timeout cannot free capacity while the worker is still running"
            release.set()
            await settle_reads(harness)
            assert await harness.operation(sid, opid) == committed
            assert len(calls) == 2, "Completed status reads are not an indefinitely stale cache"
            with store.connect() as db:
                assert db.execute("SELECT count(*) FROM appointments").fetchone()[0] == 1
                assert db.execute("SELECT count(*) FROM tool_results").fetchone()[0] == 3
        finally:
            release.set()
            await settle_reads(harness)
            await harness.close()

    asyncio.run(scenario())


def test_canceled_status_waiter_does_not_release_running_capacity_or_starve_effects(
        store, monkeypatch, completed_status_setup):
    sid, arguments = prepared(store)
    release, both_started = threading.Event(), threading.Event()
    calls = []
    lock = threading.Lock()

    async def scenario():
        harness = ToolHarness(store, status_timeout_seconds=1, max_status_reads=2)
        committed = await completed_status_setup(harness, sid, arguments)
        opid = committed["_receipt"]["operation_id"]
        original = harness._lookup_status

        def slow(*args):
            with lock:
                calls.append(args)
                if len(calls) == 2:
                    both_started.set()
            assert release.wait(3), "Test did not release status workers"
            return original(*args)

        monkeypatch.setattr(harness, "_lookup_status", slow)
        waiters = []
        try:
            waiters = [asyncio.create_task(harness.operation(sid, key)) for key in (opid, "op_absent")]
            assert await asyncio.to_thread(both_started.wait, 1)
            waiters[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiters[0]
            assert len(harness.status_reads) == 2
            overloaded = await harness.operation(sid, "op_third")
            assert overloaded["error"] == "status_unavailable"
            assert overloaded["reason"] == "capacity"
            assert len(calls) == 2
            duplicate = asyncio.create_task(harness.operation(sid, opid))
            waiters.append(duplicate)
            await asyncio.sleep(0)
            assert not duplicate.done()
            assert len(calls) == 2
            # Status work has a distinct bound, not the effect semaphore/backlog.
            effect = await asyncio.wait_for(harness.run(sid, "get_dealership_info", {}, "unrelated-effect"), 1)
            assert effect["ok"]
            verify_receipt(effect)
            await harness.close()
            assert harness.closing
            release.set()
            await settle_reads(harness)
            assert await duplicate == committed
            assert (await harness.operation(sid, "op_third"))["error"] == "operation_missing"
            assert await harness.operation(sid, opid) == committed, "Status remains readable after effect shutdown"
        finally:
            release.set()
            await settle_reads(harness)
            await asyncio.gather(*waiters, return_exceptions=True)
            await harness.close()

    asyncio.run(scenario())


def test_actual_exclusive_database_lock_yields_unavailable_then_the_same_receipt(store, completed_status_setup):
    sid, arguments = prepared(store)

    async def scenario():
        harness = ToolHarness(store)
        committed = await completed_status_setup(harness, sid, arguments)
        opid = committed["_receipt"]["operation_id"]
        await harness.drain()
        blocker = sqlite3.connect(store.path, isolation_level=None, timeout=.2)
        try:
            blocker.execute("PRAGMA locking_mode=EXCLUSIVE")
            blocker.execute("BEGIN EXCLUSIVE")
            blocker.execute("UPDATE sessions SET mode=mode WHERE id=?", (sid,))
            # Prove the fixture blocks readers; BEGIN IMMEDIATE in WAL alone
            # does not test this failure mode because readers can proceed.
            with pytest.raises(sqlite3.OperationalError) as locked:
                with store.read_connection(.001) as db:
                    db.execute("SELECT count(*) FROM sessions").fetchone()
            assert locked.value.sqlite_errorcode & 0xff in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
            began = time.perf_counter()
            unavailable = await harness.operation(sid, opid)
            assert time.perf_counter()-began < .85
            assert unavailable["error"] == "status_unavailable"
            assert "_receipt" not in unavailable
        finally:
            blocker.rollback()
            blocker.close()
        await settle_reads(harness)
        assert await harness.operation(sid, opid) == committed
        with store.connect() as db:
            assert db.execute("SELECT status FROM tool_invocations WHERE id=?", (opid,)).fetchone()[0] == "completed"
            assert db.execute("SELECT count(*) FROM appointments").fetchone()[0] == 1
        await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("unavailable_mode", ["read_error", "deadline", "capacity"])
def test_exception_after_commit_and_unavailable_status_never_marks_failed(store, monkeypatch, unavailable_mode):
    sid, arguments = prepared(store)
    release, started = threading.Event(), threading.Event()
    effects = []

    def commit_then_raise(*args):
        result = real_execute(*args)
        effects.append(result)
        raise OSError("Controlled response loss after a durable commit")

    monkeypatch.setattr(harness_module, "execute", commit_then_raise)

    async def scenario():
        harness = ToolHarness(store, status_timeout_seconds=.04, max_status_reads=1)
        original = harness._lookup_status
        occupier = None

        def unavailable(*args):
            if unavailable_mode == "read_error":
                raise sqlite3.OperationalError("Controlled read failure")
            started.set()
            assert release.wait(3), "Test did not release read worker"
            return original(*args)

        monkeypatch.setattr(harness, "_lookup_status", unavailable)
        try:
            if unavailable_mode == "capacity":
                occupier = asyncio.create_task(harness.operation(sid, "op_occupies-read-lane"))
                assert await asyncio.to_thread(started.wait, 1)
            uncertain = await harness.run(sid, "confirm_action", arguments, "commit-lost-response")
            assert uncertain["error"] == "operation_unknown"
            assert "_receipt" not in uncertain
            opid = uncertain["operation_id"]
            with store.connect() as db:
                assert db.execute("SELECT status FROM tool_invocations WHERE id=?", (opid,)).fetchone()[0] != "failed"
                assert db.execute("SELECT count(*) FROM appointments").fetchone()[0] == 1
                assert db.execute("SELECT count(*) FROM tool_results WHERE call_id='commit-lost-response'").fetchone()[0] == 1
            release.set()
            await settle_reads(harness)
            monkeypatch.setattr(harness, "_lookup_status", original)
            recovered = await harness.operation(sid, opid)
            assert recovered["ok"]
            verify_receipt(recovered)
            assert {key: value for key, value in recovered.items() if key != "_receipt"} == effects[0]
            assert await harness.run(sid, "confirm_action", arguments, "commit-lost-response") == recovered
            assert len(effects) == 1
        finally:
            release.set()
            await settle_reads(harness)
            if occupier is not None:
                await occupier
            await harness.close()

    asyncio.run(scenario())


def test_status_singleflight_key_preserves_session_ownership(store, monkeypatch, completed_status_setup):
    sid, arguments = prepared(store)
    other = store.create_session()["id"]
    release, started = threading.Event(), threading.Event()
    reads = []
    lock = threading.Lock()

    async def scenario():
        harness = ToolHarness(store, status_timeout_seconds=1, max_status_reads=2)
        committed = await completed_status_setup(harness, sid, arguments)
        opid = committed["_receipt"]["operation_id"]
        original = harness._lookup_status

        def slow(*args):
            with lock:
                reads.append(args)
                if len(reads) == 2:
                    started.set()
            assert release.wait(3)
            return original(*args)

        monkeypatch.setattr(harness, "_lookup_status", slow)
        try:
            one = asyncio.create_task(harness.operation(sid, opid))
            two = asyncio.create_task(harness.operation(other, opid))
            assert await asyncio.to_thread(started.wait, 1)
            release.set()
            authorized, foreign = await asyncio.gather(one, two)
            assert authorized == committed
            assert foreign["error"] == "operation_missing"
            assert "appointment" not in foreign
            assert len(reads) == 2
        finally:
            release.set()
            await settle_reads(harness)
            await harness.close()

    asyncio.run(scenario())


def test_status_setup_reconciles_the_same_pending_commit_without_resubmission(
        store, monkeypatch, complete_pending_tool_results_async):
    sid, arguments = prepared(store)
    release = threading.Event()

    async def scenario():
        harness = ToolHarness(store)
        original_execute = harness._execute_registered

        def gated(*args, **kwargs):
            assert release.wait(30), "Only the test may release accepted setup work."
            return original_execute(*args, **kwargs)

        monkeypatch.setattr(harness, "_execute_registered", gated)
        try:
            initial = await harness.run(sid, "confirm_action", arguments, "commit")
            assert initial.get("error") == "operation_pending", {
                "ok": initial.get("ok"), "error": initial.get("error"), "receipt_present": "_receipt" in initial,
            }
            preserved = json.dumps(initial, sort_keys=True)
            with store.connect() as db:
                assert db.execute("SELECT count(*) FROM tool_invocations").fetchone()[0] == 1
                assert db.execute("SELECT count(*) FROM appointments").fetchone()[0] == 0
            release.set()
            [committed] = await complete_pending_tool_results_async(harness, sid, [initial])
            assert committed.get("ok") is True and "_receipt" in committed, {
                "ok": committed.get("ok"), "error": committed.get("error"), "reason": committed.get("reason"),
                "receipt_present": "_receipt" in committed,
            }
            verify_receipt(committed)
            assert committed["_receipt"]["operation_id"] == initial["operation_id"]
            assert json.dumps(initial, sort_keys=True) == preserved
            assert await harness.run(sid, "confirm_action", arguments, "commit") == committed
            with store.connect() as db:
                assert db.execute("SELECT count(*) FROM tool_invocations").fetchone()[0] == 1
                assert db.execute("SELECT count(*) FROM appointments").fetchone()[0] == 1
                assert db.execute("SELECT count(*) FROM tool_results WHERE call_id='commit'").fetchone()[0] == 1
        finally:
            release.set()
            await harness.close()

    asyncio.run(scenario())


def test_status_connection_is_read_only_and_never_creates_a_missing_database(store, tmp_path):
    with store.read_connection() as db:
        assert db.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            db.execute("UPDATE sessions SET ended=1")
    absent = Store.__new__(Store)
    absent.path = str(tmp_path/"never-created.sqlite3")
    with pytest.raises(sqlite3.OperationalError):
        with absent.read_connection():
            pass
    assert not Path(absent.path).exists()


@pytest.mark.parametrize("options", [
    {"status_timeout_seconds": 0}, {"status_timeout_seconds": -1},
    {"status_timeout_seconds": float("nan")}, {"status_timeout_seconds": float("inf")},
    {"status_timeout_seconds": True}, {"status_timeout_seconds": "1"},
    {"max_status_reads": 0}, {"max_status_reads": -1}, {"max_status_reads": True}, {"max_status_reads": 1.5},
])
def test_status_limits_are_positive_finite_bounded_configuration(store, options):
    with pytest.raises(ValueError):
        ToolHarness(store, **options)
