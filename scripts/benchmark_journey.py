"""Fixed offline 2x2 experiment: complete booking, move and cancellation journeys.

No credentials, server, network, audio or live scheduler. Defaults are a protocol,
not tuning knobs: four balanced blocks, 1/20 concurrency, 20 warmups and 20 measured
journeys, fused/two-read starts, four effect workers, normal 1.5-second deadline.
Run --validate-only before the single measured experiment to check the workload.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import platform
import sqlite3
import statistics
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import domain
from app.domain import Store
from app.harness import ToolHarness, canonical

FIXED_CLOCK = datetime(2026, 10, 17, 18, tzinfo=timezone.utc)
TRACE_KEY = ContextVar("journey_benchmark_operation", default=None)
VARIANTS = {
    "reference_lane_off_reg4": (False, 4),
    "current_lane_on_reg1": (True, 1),
    "lane_on_reg4": (True, 4),
    "lane_off_reg1": (False, 1),
}
# Balanced Latin square: each variant occupies each position exactly once.
ORDERS = ((0, 1, 3, 2), (1, 2, 0, 3), (2, 3, 1, 0), (3, 0, 2, 1))
STAGES = ("booking", "move", "cancel", "journey", "confirm_booking", "confirm_move", "confirm_cancel")
SOURCE_PATHS = ("app/domain.py", "app/tools.py", "app/harness.py", "scripts/benchmark_journey.py", "runtime_sqlite.py")
PROTOCOL = {
    "blocks": 4, "concurrency": [1, 20], "warmup_journeys": 20, "measured_journeys": 20,
    "starts": ["fused", "two_reads"], "workers": 4, "max_pending": 64,
    "request_deadline_seconds": 1.5, "status_poll_seconds": .1, "completion_cap_seconds": 30,
    "completion_wait_policy": "One write; poll the same operation ID for pending/unavailable status, honoring retry_after_ms within one async completion deadline. Unknown/failed outcomes are not retried.",
    "synthetic_clock_utc": FIXED_CLOCK.isoformat(),
    "promotion_gates": {
        "correctness": "All recorded journeys and invariant/replay checks pass; normal regression suite also required.",
        "non_regression": "Every stage p50/p95 at both loads and start modes <= max(reference*1.10, reference+5ms).",
        "pending_rate": "No increase over reference for either load/start cell; no failed or unknown completion.",
        "meaningful_gain": "At least 15% lower whole-journey p95 at concurrency20 for BOTH start modes.",
        "choice": "Report all variants. Among eligible candidates prefer lowest mean 20-session journey p95, then fewer coordination layers.",
        "scope": "Local machine-specific engineering comparison, not a voice latency SLO or statistical production guarantee.",
    },
}


def metrics(values):
    ordered = sorted(values)
    if not ordered:
        return {"count": 0}
    return {"count": len(values), "p50": round(statistics.median(values), 4),
            "p95": round(ordered[max(0, math.ceil(len(ordered)*.95)-1)], 4),
            "mean": round(statistics.mean(values), 4), "min": round(ordered[0], 4), "max": round(ordered[-1], 4)}


class Recorder:
    def __init__(self):
        self.enabled = False
        self.lock = threading.Lock()
        self.rows = defaultdict(lambda: defaultdict(lambda: [0.0, 0]))
        self.connections = 0

    def add(self, phase, seconds):
        key = TRACE_KEY.get()
        if self.enabled and key:
            with self.lock:
                row = self.rows[key][phase]
                row[0] += seconds*1000
                row[1] += 1

    def open_connection(self):
        with self.lock:
            self.connections += 1

    def connections_at_close(self):
        with self.lock:
            return self.connections

    def closed_connection(self):
        with self.lock:
            self.connections -= 1

    def clear(self):
        with self.lock:
            self.rows.clear()

    def phases(self, key):
        with self.lock:
            return {name: {"total_ms": round(value[0], 5), "count": value[1]}
                    for name, value in self.rows.get(key, {}).items()}


class TimedWriter:
    def __init__(self, recorder, enabled):
        self.recorder = recorder
        self.lock = threading.RLock() if enabled else nullcontext()

    def __enter__(self):
        started = time.perf_counter()
        result = self.lock.__enter__()
        self.recorder.add("writer_wait", time.perf_counter()-started)
        return result

    def __exit__(self, *args):
        return self.lock.__exit__(*args)


class TimedConnection:
    def __init__(self, db, recorder):
        self.db, self.recorder = db, recorder
        self.began = None

    def __getattr__(self, name):
        return getattr(self.db, name)

    def execute(self, sql, *args):
        if sql == "BEGIN IMMEDIATE":
            started = time.perf_counter()
            try:
                return self.db.execute(sql, *args)
            finally:
                self.recorder.add("sqlite_begin_wait", time.perf_counter()-started)
                self.began = time.perf_counter()
        return self.db.execute(sql, *args)

    def commit(self):
        if self.began is not None:
            self.recorder.add("transaction_body", time.perf_counter()-self.began)
        started = time.perf_counter()
        try:
            return self.db.commit()
        finally:
            self.recorder.add("sqlite_commit", time.perf_counter()-started)


class TimedStore(Store):
    def __init__(self, path, recorder, writer_lane):
        self.recorder = recorder
        self._writer_lock = TimedWriter(recorder, writer_lane)
        super().__init__(path)

    @contextmanager
    def transaction(self):
        # The production Store now relies on SQLite exclusion. Retain explicit
        # test-local coordination to make both historical variants reproducible.
        with self._writer_lock:
            with super().transaction() as db:
                yield db

    @contextmanager
    def connect(self):
        started = time.perf_counter()
        closing_started, tracked_at_close = None, None
        try:
            with super().connect() as db:
                self.recorder.open_connection()
                self.recorder.add("db_open_and_pragma", time.perf_counter()-started)
                try:
                    yield TimedConnection(db, self.recorder)
                finally:
                    tracked_at_close = self.recorder.connections_at_close()
                    closing_started = time.perf_counter()
        finally:
            if closing_started is not None:
                duration = time.perf_counter()-closing_started
                self.recorder.closed_connection()
                self.recorder.add("db_close", duration)
                if tracked_at_close == 1:
                    self.recorder.add("db_close_one_tracked_connection", duration)


class TimedGate:
    def __init__(self, gate, recorder, name):
        self.gate, self.recorder, self.name = gate, recorder, name

    async def __aenter__(self):
        started = time.perf_counter()
        result = await self.gate.__aenter__()
        self.recorder.add(self.name, time.perf_counter()-started)
        return result

    async def __aexit__(self, *args):
        return await self.gate.__aexit__(*args)


class TimedHarness(ToolHarness):
    def __init__(self, store, recorder, registrars):
        super().__init__(store, timeout_seconds=1.5, max_concurrency=4, max_pending=64, max_registrars=registrars)
        self.recorder = recorder
        self.admission_lock = TimedGate(self.admission_lock, recorder, "admission_queue")
        self.semaphore = TimedGate(self.semaphore, recorder, "effect_queue")

    async def run(self, sid, name, arguments, call_id):
        token = TRACE_KEY.set((sid, call_id))
        try:
            return await super().run(sid, name, arguments, call_id)
        finally:
            TRACE_KEY.reset(token)

    def _register(self, *args):
        started = time.perf_counter()
        try:
            return super()._register(*args)
        finally:
            self.recorder.add("register_worker", time.perf_counter()-started)

    def _execute_registered(self, *args):
        started = time.perf_counter()
        try:
            return super()._execute_registered(*args)
        finally:
            self.recorder.add("effect_worker", time.perf_counter()-started)

    def _lookup(self, *args):
        started = time.perf_counter()
        try:
            return super()._lookup(*args)
        finally:
            self.recorder.add("receipt_lookup", time.perf_counter()-started)


def receipt_ok(result):
    raw = {key: value for key, value in result.items() if key != "_receipt"}
    assert result["_receipt"]["result_sha256"] == hashlib.sha256(canonical(raw).encode()).hexdigest()


class CompletionDeadline(TimeoutError):
    """The benchmark stopped waiting; the accepted action may still complete."""

    def __init__(self, operation_id, last_result, polls):
        self.operation_id = operation_id
        self.last_result = last_result
        self.status_polls = polls
        super().__init__(f"Completion wait expired: operation_id={operation_id!r}, "
                         f"last_status={last_result.get('error', 'unobserved')!r}, polls={polls}; "
                         "outcome unverified, no write was repeated")


async def run_until_settled(harness, sid, name, arguments, call_id, *,
                            completion_cap_seconds=None, clock=time.perf_counter, sleep=asyncio.sleep):
    """Bound the complete wait, including status reads, without reissuing effects.

    Canceling this waiter does not cancel accepted work shielded by ToolHarness.
    The caller still checks the terminal result and authoritative receipt.
    """
    cap = PROTOCOL["completion_cap_seconds"] if completion_cap_seconds is None else completion_cap_seconds
    if type(cap) not in (int, float) or not math.isfinite(cap) or cap <= 0:
        raise ValueError("Completion wait must be positive and finite")
    started = clock()
    deadline = started + cap
    result, operation_id, polls, unavailable = {}, None, 0, 0
    try:
        # Unlike the old between-polls check, this also bounds a slow status
        # await. It is a cooperative wait budget, not cancellation of an effect.
        async with asyncio.timeout(cap):
            result = await harness.run(sid, name, arguments, call_id)
            first_ms = (clock() - started) * 1000
            was_pending = result.get("error") == "operation_pending"
            while result.get("error") in {"operation_pending", "status_unavailable"}:
                observed_id = result.get("operation_id")
                if not isinstance(observed_id, str) or not observed_id:
                    raise AssertionError("Transient status omitted its operation ID")
                if operation_id is None:
                    operation_id = observed_id
                elif observed_id != operation_id:
                    raise AssertionError("Status response changed the operation ID")
                delay = PROTOCOL["status_poll_seconds"]
                if result["error"] == "status_unavailable":
                    unavailable += 1
                    retry_ms = result.get("retry_after_ms")
                    if type(retry_ms) in (int, float) and math.isfinite(retry_ms) and retry_ms >= 0:
                        delay = max(delay, retry_ms / 1000)
                remaining = deadline - clock()
                if remaining <= 0:
                    raise CompletionDeadline(operation_id, result, polls)
                await sleep(min(delay, remaining))
                if clock() >= deadline:
                    raise CompletionDeadline(operation_id, result, polls)
                polls += 1
                result = await harness.run(sid, "get_operation", {"operation_id": operation_id},
                                           f"{call_id}-poll-{polls}")
            if clock() >= deadline:
                raise CompletionDeadline(operation_id, result, polls)
    except TimeoutError as error:
        if isinstance(error, CompletionDeadline):
            raise
        raise CompletionDeadline(operation_id, result, polls) from error
    if operation_id is not None and "_receipt" in result:
        assert result["_receipt"]["operation_id"] == operation_id, "Receipt belongs to another operation"
    return {"result": result, "duration_ms": (clock() - started) * 1000,
            "first_response_ms": first_ms, "pending": was_pending,
            "status_polls": polls, "status_unavailable_count": unavailable}


def assigned_slots(index):
    # Two callers per weekday, with four nonoverlapping intended slots per day.
    # Both chosen slots remain within the first three offers under any ordering
    # of these two callers' book/move/cancel steps. No capacity inflation.
    day = FIXED_CLOCK.date()
    weekdays = []
    for offset in range(22):
        candidate = day + timedelta(days=offset)
        if candidate.weekday() < 5:
            weekdays.append(candidate.isoformat())
    chosen_day = weekdays[index//2]
    initial_hour, moved_hour = ((9, 11) if index % 2 == 0 else (10, 13))
    return chosen_day, f"slot_{chosen_day}_{initial_hour}", f"slot_{chosen_day}_{moved_hour}"


async def run_batch(store, harness, recorder, start_mode, concurrency, phase, count=20):
    sessions = [store.create_session()["id"] for _ in range(count)]
    gate = asyncio.Semaphore(concurrency)
    recorder.clear()
    recorder.enabled = True

    async def journey(index, sid):
        async with gate:
            day, initial_slot, moved_slot = assigned_slots(index)
            service = ("oil_change", "tires", "brakes", "diagnostic")[index % 4]
            tools, results, timings = [], {}, {}

            async def call(name, arguments, label):
                call_id = f"{phase}-{label}"
                observed = await run_until_settled(harness, sid, name, arguments, call_id)
                result = observed.pop("result")
                if not result.get("ok") or "_receipt" not in result:
                    raise AssertionError({"phase": label, "result": result})
                tools.append({"name": name, "label": label, "call_id": call_id,
                              **observed,
                              "result_bytes": len(canonical(result).encode())})
                results[label] = result
                return result

            began = time.perf_counter()
            if start_mode == "two_reads":
                await call("get_dealership_info", {}, "info")
                await call("search_availability", {"service": service, "date": day}, "initial-read")
            else:
                await call("begin_service_request", {"service": service, "date": day}, "initial-read")
            booking = await call("prepare_booking", {"service": service, "slot_id": initial_slot,
                "customer_name": "Synthetic Caller", "phone": "5125550142", "vehicle": "2021 Toyota Camry"}, "prepare-book")
            booked = await call("confirm_action", {"proposal_id": booking["proposal_id"], "confirmed": True}, "confirm-book")
            timings["booking"] = (time.perf_counter()-began)*1000
            stage = time.perf_counter()
            await call("search_availability", {"service": service, "date": day}, "move-read")
            move = await call("prepare_reschedule", {"slot_id": moved_slot}, "prepare-move")
            moved = await call("confirm_action", {"proposal_id": move["proposal_id"], "confirmed": True}, "confirm-move")
            timings["move"] = (time.perf_counter()-stage)*1000
            stage = time.perf_counter()
            cancel = await call("prepare_cancellation", {}, "prepare-cancel")
            cancelled = await call("confirm_action", {"proposal_id": cancel["proposal_id"], "confirmed": True}, "confirm-cancel")
            timings["cancel"] = (time.perf_counter()-stage)*1000
            timings["journey"] = (time.perf_counter()-began)*1000
            for name, label in (("confirm_booking", "confirm-book"), ("confirm_move", "confirm-move"), ("confirm_cancel", "confirm-cancel")):
                timings[name] = next(row["duration_ms"] for row in tools if row["label"] == label)
            return {"index": index, "sid": sid, "stage_ms": timings, "tools": tools, "results": results,
                    "initial_slot": initial_slot, "moved_slot": moved_slot,
                    "replay_arguments": {"proposal_id": booking["proposal_id"], "confirmed": True}}

    started = time.perf_counter()
    rows = await asyncio.gather(*(journey(index, sid) for index, sid in enumerate(sessions)))
    batch_ms = (time.perf_counter()-started)*1000
    await harness.drain()
    recorder.enabled = False
    # All expensive assertions/readbacks occur after every measured timer ends.
    contexts = []
    for row in rows:
        sid, results = row["sid"], row["results"]
        expected_calls = 8 if start_mode == "fused" else 9
        assert len(row["tools"]) == expected_calls
        for tool in row["tools"]:
            tool["phase_ms"] = recorder.phases((sid, tool["call_id"]))
            tool["status_poll_phase_ms"] = [recorder.phases((sid, f"{tool['call_id']}-poll-{index}"))
                                            for index in range(1, tool["status_polls"]+1)]
            receipt_ok(results[tool["label"]])
        initial, moved, cancelled = (results[name]["appointment"] for name in ("confirm-book", "confirm-move", "confirm-cancel"))
        assert initial["id"] == moved["id"] == cancelled["id"]
        assert initial["slot_id"] == row["initial_slot"]
        assert moved["slot_id"] == cancelled["slot_id"] == row["moved_slot"]
        assert initial["status"] == moved["status"] == "confirmed"
        assert cancelled["status"] == "cancelled"
        for read in ("initial-read", "move-read"):
            assert results[read]["timezone"] == "America/Chicago"
            availability = results[read]
            assert availability["returned_count"] == len(availability["slots"])
            assert availability["total_matching"] >= availability["returned_count"]
            assert availability["truncated"] is (availability["total_matching"] > availability["returned_count"])
            assert availability["complete"] is (not availability["truncated"])
            expected_scope = ("Only the first three matches are shown; an omitted time is not proof of unavailability."
                if availability["truncated"] else "These are all matches for the stated query within the displayed search horizon.")
            assert availability["message"] == "Available times are offers, not reservations. " + expected_scope
            for slot in results[read]["slots"]:
                assert set(slot) == {"id", "start", "label", "remaining"}
                assert slot["label"] == domain.label(slot["start"])
                assert slot["remaining"] == 1
        context = results["initial-read"]["context"] if start_mode == "fused" else {
            key: value for key, value in results["info"].items() if key not in ("ok", "_receipt")}
        contexts.append(context)
        replay = await harness.run(sid, "confirm_action", row["replay_arguments"], f"{phase}-confirm-book")
        assert replay == results["confirm-book"], "Exact historical receipt must remain immutable after cancellation"
        conflict = await harness.run(sid, "confirm_action", {**row["replay_arguments"], "confirmed": False}, f"{phase}-confirm-book")
        assert conflict.get("error") == "idempotency_conflict"
        snapshot = store.snapshot(sid)
        assert snapshot["appointment"] == cancelled
        assert snapshot["proposal"] is None
        assert len(snapshot["events"]) == expected_calls
        with store.connect() as db:
            assert db.execute("SELECT count(*) FROM appointments WHERE session_id=?", (sid,)).fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM proposals WHERE session_id=? AND state='committed'", (sid,)).fetchone()[0] == 3
            assert db.execute("SELECT count(*) FROM tool_results WHERE session_id=?", (sid,)).fetchone()[0] == expected_calls
        row.pop("results")
        row.pop("replay_arguments")
        row.pop("sid")
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM appointments WHERE status='confirmed'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM tool_invocations WHERE status NOT IN ('completed','rejected')").fetchone()[0] == 0
    assert all(context == contexts[0] for context in contexts)
    return rows, batch_ms, contexts[0]


async def run_arm(path, variant, mode, concurrency, validate_only=False):
    recorder = Recorder()
    lane, registrars = VARIANTS[variant]
    store = TimedStore(path, recorder, lane)
    harness = TimedHarness(store, recorder, registrars)
    with store.connect() as db:
        settings = {name: db.execute(f"PRAGMA {name}").fetchone()[0]
                    for name in ("journal_mode", "synchronous", "wal_autocheckpoint")}
    assert settings["journal_mode"] == "wal"
    assert settings["synchronous"] == 2, "Never weaken FULL durability to improve timings"
    try:
        await run_batch(store, harness, recorder, mode, concurrency, "warmup", 2 if validate_only else 20)
        rows, batch_ms, context = await run_batch(store, harness, recorder, mode, concurrency, "measure", 2 if validate_only else 20)
        return {"variant": variant, "start_mode": mode, "concurrency": concurrency,
                "batch_ms": round(batch_ms, 4), "sqlite_settings": settings, "sessions": rows}, context
    finally:
        recorder.enabled = False
        await harness.close()


async def validate_atomic_contention(path, variant):
    """Untimed preflight: coordination knobs cannot replace SQLite authority."""
    recorder = Recorder()
    lane, registrars = VARIANTS[variant]
    store = TimedStore(path, recorder, lane)
    harness = TimedHarness(store, recorder, registrars)
    sessions = [store.create_session()["id"] for _ in range(2)]
    try:
        offers = await asyncio.gather(*(harness.run(sid, "search_availability", {"service": "oil_change"}, "race-search") for sid in sessions))
        slot = offers[0]["slots"][0]["id"]
        assert offers[1]["slots"][0]["id"] == slot
        proposals = await asyncio.gather(*(harness.run(sid, "prepare_booking", {
            "service": "oil_change", "slot_id": slot, "customer_name": "Synthetic Caller",
            "phone": "5125550142", "vehicle": "2021 Toyota Camry"}, "race-prepare") for sid in sessions))
        arguments = [{"proposal_id": proposal["proposal_id"], "confirmed": True} for proposal in proposals]
        results = await asyncio.gather(*(harness.run(sid, "confirm_action", args, "race-confirm")
                                         for sid, args in zip(sessions, arguments, strict=True)))
        for result in results:
            receipt_ok(result)
        assert sum(result["ok"] is True for result in results) == 1
        assert sum(result.get("error") == "slot_unavailable" for result in results) == 1
        with store.connect() as db:
            assert db.execute("SELECT count(*) FROM appointments WHERE status='confirmed'").fetchone()[0] == 1
        winner = next(index for index, result in enumerate(results) if result["ok"])
        foreign = await harness.run(sessions[1-winner], "confirm_action", arguments[winner], "foreign-proposal")
        assert foreign.get("error") == "proposal_missing"
        await harness.close()
        recovered = TimedHarness(store, recorder, registrars)
        try:
            assert await recovered.run(sessions[winner], "confirm_action", arguments[winner], "race-confirm") == results[winner]
            assert await recovered.operation(sessions[winner], results[winner]["_receipt"]["operation_id"]) == results[winner]
            with store.connect() as db:
                assert db.execute("SELECT count(*) FROM appointments").fetchone()[0] == 1
        finally:
            await recovered.close()
    finally:
        await harness.close()


def summarize(records):
    grouped = defaultdict(list)
    for record in records:
        grouped[(record["variant"], record["start_mode"], record["concurrency"])].extend(record["sessions"])
    summary = []
    for (variant, mode, concurrency), rows in grouped.items():
        tools = [tool for row in rows for tool in row["tools"]]
        phase_names = sorted({name for tool in tools for name in tool["phase_ms"]})
        summary.append({"variant": variant, "start_mode": mode, "concurrency": concurrency,
                        "stages_ms": {stage: metrics([row["stage_ms"][stage] for row in rows]) for stage in STAGES},
                        "tool_count": len(tools), "tool_ms": metrics([tool["duration_ms"] for tool in tools]),
                        "pending_count": sum(tool["pending"] for tool in tools),
                        "pending_rate": sum(tool["pending"] for tool in tools)/len(tools),
                        "status_poll_count": sum(tool["status_polls"] for tool in tools),
                        "status_unavailable_count": sum(tool["status_unavailable_count"] for tool in tools),
                        "phase_total_per_tool_ms": {name: metrics([tool["phase_ms"].get(name, {"total_ms": 0})["total_ms"] for tool in tools]) for name in phase_names}})
    return summary


def gates(summary):
    by_cell = {(row["variant"], row["start_mode"], row["concurrency"]): row for row in summary}
    decisions = []
    for variant in VARIANTS:
        if variant.startswith("reference"):
            continue
        violations, improvements = [], {}
        for mode in PROTOCOL["starts"]:
            for concurrency in PROTOCOL["concurrency"]:
                reference = by_cell[("reference_lane_off_reg4", mode, concurrency)]
                candidate = by_cell[(variant, mode, concurrency)]
                for stage in STAGES:
                    for statistic in ("p50", "p95"):
                        before = reference["stages_ms"][stage][statistic]
                        after = candidate["stages_ms"][stage][statistic]
                        if after > max(before*1.1, before+5):
                            violations.append({"cell": [mode, concurrency, stage, statistic], "reference_ms": before, "candidate_ms": after})
                if candidate["pending_rate"] > reference["pending_rate"]:
                    violations.append({"cell": [mode, concurrency, "pending_rate"], "reference": reference["pending_rate"], "candidate": candidate["pending_rate"]})
                if concurrency == 20:
                    improvement = 1-candidate["stages_ms"]["journey"]["p95"]/reference["stages_ms"]["journey"]["p95"]
                    improvements[mode] = improvement
                    if improvement < .15:
                        violations.append({"cell": [mode, 20, "meaningful_gain"], "observed_fraction": improvement, "required_fraction": .15})
        decisions.append({"variant": variant, "eligible": not violations, "journey_p95_improvement_at20": improvements, "violations": violations})
    return decisions


async def benchmark(validate_only):
    assert sqlite3.sqlite_version == "3.54.1", "This fixed experiment requires the verified workspace-private SQLite3.54.1 runtime"
    domain.now = lambda: FIXED_CLOCK
    hashes = {path: hashlib.sha256((ROOT/path).read_bytes()).hexdigest() for path in SOURCE_PATHS}
    records, contexts = [], []
    names = list(VARIANTS)
    with tempfile.TemporaryDirectory(prefix="servicevoice-journey-") as directory:
        if validate_only:
            for variant in names:
                await validate_atomic_contention(Path(directory)/f"race-{variant}.sqlite3", variant)
                print(f"Validation: {variant}: final-slot race, session ownership and replay recovery passed", flush=True)
        for block, order in enumerate(ORDERS[:1] if validate_only else ORDERS):
            profiles = [(mode, load) for mode in PROTOCOL["starts"] for load in PROTOCOL["concurrency"]]
            if block % 2:
                profiles.reverse()
            for mode, concurrency in profiles:
                if validate_only and concurrency == 1:
                    continue
                for position, index in enumerate(order):
                    variant = names[index]
                    path = Path(directory)/f"block{block}-{mode}-{concurrency}-{variant}.sqlite3"
                    record, context = await run_arm(path, variant, mode, concurrency, validate_only)
                    record.update(block=block+1, position=position+1)
                    records.append(record)
                    contexts.append(context)
                    print(f"{'Validation' if validate_only else 'Measured'} block {block+1}: {mode}, concurrency {concurrency}, {variant}: correctness passed", flush=True)
    assert all(context == contexts[0] for context in contexts), "Dealer facts differ across modes/variants"
    final_hashes = {path: hashlib.sha256((ROOT/path).read_bytes()).hexdigest() for path in SOURCE_PATHS}
    assert hashes == final_hashes, "Source changed; discard measured comparison"
    if validate_only:
        return None
    summary = summarize(records)
    return {"benchmark": "servicevoice-local-journey-2x2-v2", "outcome": "completed", "observed_at_utc": datetime.now(timezone.utc).isoformat(),
            "scope": "Local tool execution only: no speech, model, HTTP, network, or actual DMS",
            "protocol": PROTOCOL, "environment": {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
                "platform": platform.platform(), "workspace_sqlite_dll_sha256": hashlib.sha256((ROOT/".local/sqlite/sqlite3.dll").read_bytes()).hexdigest()},
            "source_sha256": hashes, "variants": VARIANTS,
            "correctness": {"all_journeys_completed": True, "one_appointment_per_session": True,
                "appointment_identity_preserved_on_move": True, "all_final_appointments_cancelled": True,
                "all_capacity_released": True, "complete_dealer_context_equal": True,
                "availability_schema_and_selected_slots_preserved": True, "every_receipt_verified": True,
                "exact_replay_immutable_after_cancel": True, "changed_request_idempotency_conflict": True,
                "one_event_and_result_per_actual_tool_call": True, "three_committed_proposals_per_session": True},
            "summary": summary, "promotion_evaluation": gates(summary), "raw_blocks": records,
            "limitations": [
                "Full instrumentation is identical in all variants but adds overhead; results do not replace uninstrumented or voice measurements.",
                "Four balanced blocks are a small sample. Sessions in one batch share a DB and are correlated; pooled quantiles are not confidence intervals.",
                "No speech/think-time/staggered arrivals: journeys intentionally stress sequential tool scheduling.",
                "Status polling follows the normal deadline; all poll count and wall-time overhead remains included.",
                "Nested phase timings overlap and must not be added together as independent components.",
                "One tracked connection at close is an application-level observation, not proof that SQLite ran a checkpoint.",
                "Availability lists can legitimately differ with interleaving; schema, selected offered slots, identity, capacity and complete static context are validated.",
                "The verified workspace-private SQLite3.54.1 runtime addresses the earlier WAL-reset version issue; it does not establish production DMS or deployment guarantees.",
            ]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT/"docs/harness-journey-benchmark.json")
    args = parser.parse_args()
    if not args.validate_only and args.output.exists():
        parser.error("Output already exists; preserve it and explicitly choose a fresh filename for a separately authorized experiment")
    try:
        report = asyncio.run(benchmark(args.validate_only))
    except Exception as error:
        if not args.validate_only:
            # Preserve a failed attempt as evidence; never silently overwrite it
            # or rerun until an independently identified defect is corrected.
            args.output.write_text(json.dumps({"benchmark": "servicevoice-local-journey-2x2-v2",
                "outcome": "failed", "observed_at_utc": datetime.now(timezone.utc).isoformat(),
                "protocol": PROTOCOL, "error": repr(error), "sqlite": sqlite3.sqlite_version}, indent=2)+"\n", encoding="utf-8")
        raise
    if report is not None:
        args.output.write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
        print(json.dumps({"output": str(args.output), "correctness": report["correctness"], "promotion_evaluation": report["promotion_evaluation"]}, indent=2))


if __name__ == "__main__":
    main()
