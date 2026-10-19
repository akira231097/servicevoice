"""Paired local tool-fusion benchmark; no LLM, audio, network, or provider calls.

Both arms must preserve all dealership facts and available slots. Timings describe
this Python/SQLite harness only. Output bytes are serialized JSON bytes, not tokens.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import domain
from app.domain import Store
from app.harness import ToolHarness, canonical

FIXED_CLOCK = datetime(2026, 10, 17, 18, tzinfo=timezone.utc)
SERVICES = ("oil_change", "tires", "brakes", "diagnostic")


def without_receipt(result):
    return {key: value for key, value in result.items() if key != "_receipt"}


def byte_count(value):
    return len(canonical(value).encode("utf-8"))


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def metrics(values):
    return {"p50": round(statistics.median(values), 4), "p95": round(percentile(values, .95), 4),
            "min": round(min(values), 4), "max": round(max(values), 4)}


async def run_batch(store, harness, arm, session_count, phase):
    sessions = [store.create_session()["id"] for _ in range(session_count)]

    async def scenario(index, sid):
        service = SERVICES[index % len(SERVICES)]
        arguments = {"service": service}
        started = time.perf_counter()
        if arm == "baseline":
            info = await harness.run(sid, "get_dealership_info", {}, f"{phase}-info")
            availability = await harness.run(sid, "search_availability", arguments, f"{phase}-slots")
            results = [info, availability]
            assert all(item.get("ok") and "_receipt" in item for item in results), results
            context = {key: value for key, value in without_receipt(info).items() if key != "ok"}
        else:
            availability = await harness.run(sid, "begin_service_request", arguments, f"{phase}-fused")
            results = [availability]
            assert availability.get("ok") and "_receipt" in availability, availability
            context = availability["context"]
        duration_ms = (time.perf_counter() - started) * 1000
        facts = {"context": context, "slots": availability["slots"],
                 "timezone": availability["timezone"], "message": availability["message"]}
        return {
            "duration_ms": duration_ms,
            "result_bytes_with_receipts": sum(byte_count(result) for result in results),
            "domain_result_bytes_without_receipts": sum(byte_count(without_receipt(result)) for result in results),
            "tool_calls": len(results), "facts": facts,
        }

    started = time.perf_counter()
    results = await asyncio.gather(*(scenario(i, sid) for i, sid in enumerate(sessions)))
    batch_duration_ms = (time.perf_counter() - started) * 1000
    await harness.drain()
    # Validate only after every session's timer ended, so synchronous checks cannot
    # block the event loop and contaminate another in-flight session's measurement.
    for index, (sid, result) in enumerate(zip(sessions, results, strict=True)):
        snapshot = store.snapshot(sid)
        assert snapshot["appointment"] is None
        assert snapshot["proposal"] is None
        assert snapshot["service"] == SERVICES[index % len(SERVICES)]
        assert snapshot["slots"] == result["facts"]["slots"]
        assert len(snapshot["events"]) == result["tool_calls"]
        assert len({event["id"] for event in snapshot["events"]}) == result["tool_calls"]
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM appointments").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM proposals").fetchone()[0] == 0
    return results, batch_duration_ms


async def run_arm(path, arm, sessions, warmup_sessions, workers):
    store = Store(path)
    harness = ToolHarness(store, timeout_seconds=30, max_concurrency=workers)
    await run_batch(store, harness, arm, warmup_sessions, "warmup")
    return await run_batch(store, harness, arm, sessions, "measure")


async def benchmark(args):
    domain.now = lambda: FIXED_CLOCK
    per_arm = {"baseline": [], "fused": []}
    rounds = []
    source_hashes = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
                     for path in ("app/domain.py", "app/tools.py", "app/harness.py", "scripts/benchmark_harness.py")}
    with tempfile.TemporaryDirectory(prefix="servicevoice-benchmark-") as directory:
        for index in range(args.rounds):
            order = ("baseline", "fused") if index % 2 == 0 else ("fused", "baseline")
            outputs, round_record = {}, {"round": index + 1, "order": list(order)}
            for arm in order:
                rows, batch_ms = await run_arm(Path(directory) / f"{index}-{arm}.sqlite3", arm,
                                              args.sessions, args.warmup_sessions, args.workers)
                outputs[arm] = rows
                per_arm[arm].extend(rows)
                round_record[arm] = {
                    "batch_duration_ms": round(batch_ms, 4),
                    "session_duration_ms": metrics([row["duration_ms"] for row in rows]),
                    "tool_calls": sum(row["tool_calls"] for row in rows),
                    "result_bytes_with_receipts": sum(row["result_bytes_with_receipts"] for row in rows),
                    "domain_result_bytes_without_receipts": sum(row["domain_result_bytes_without_receipts"] for row in rows),
                }
            for before, after in zip(outputs["baseline"], outputs["fused"], strict=True):
                assert before["facts"] == after["facts"], "Fusion changed an authoritative fact; refuse efficiency claim"
            round_record["all_context_and_availability_equal"] = True
            rounds.append(round_record)
    final_hashes = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in source_hashes}
    assert source_hashes == final_hashes, "Source changed during measurement; discard this run"
    summary = {}
    for arm, rows in per_arm.items():
        summary[arm] = {
            "measured_sessions": len(rows),
            "session_duration_ms": metrics([row["duration_ms"] for row in rows]),
            "tool_calls_per_session": rows[0]["tool_calls"],
            "mean_result_bytes_with_receipts_per_session": round(statistics.mean(row["result_bytes_with_receipts"] for row in rows), 2),
            "mean_domain_result_bytes_without_receipts_per_session": round(statistics.mean(row["domain_result_bytes_without_receipts"] for row in rows), 2),
        }
    return {
        "benchmark": "servicevoice-local-tool-fusion-v1",
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Python/SQLite tool harness only; no LLM, audio, WebRTC, provider calls, or network",
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "configuration": {"rounds": args.rounds, "concurrent_sessions_per_arm": args.sessions,
                          "warmup_sessions_per_arm": args.warmup_sessions, "max_tool_workers": args.workers,
                          "deadline_seconds": 30, "synthetic_clock_utc": FIXED_CLOCK.isoformat()},
        "source_sha256": source_hashes,
        "correctness": {"all_dealership_context_fields_equal": True, "all_slots_and_availability_facts_equal": True,
                        "appointments_created": 0, "proposals_created": 0,
                        "one_persisted_event_per_actual_tool_call": True,
                        "note": "Read-only workflow at the business level; normal session/trace/result rows are persisted. Mutation integrity is checked by the separate regression suite."},
        "summary": summary,
        "rounds": rounds,
        "limitations": [
            "Local serialized JSON bytes are not model tokens or actual provider/network payload size.",
            "Local durations include scheduler/thread/database contention; they do not measure speech or conversational latency.",
            "Tools called directly through ToolHarness; HTTP and voice-provider overhead are excluded.",
            f"{args.rounds} paired rounds with alternating order are a small machine-specific sample, not a production performance guarantee.",
            "Both arms preserve identical facts; any payload size increase from the fused context wrapper is retained honestly.",
            "This measures an initial read workflow, not task completion under real speech, failures, or a live DMS.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=int, default=20)
    parser.add_argument("--warmup-sessions", type=int, default=20)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/local-harness-benchmark.json")
    args = parser.parse_args()
    if min(args.sessions, args.warmup_sessions, args.rounds, args.workers) < 1:
        parser.error("All counts must be positive")
    report = asyncio.run(benchmark(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve()), "correctness": report["correctness"],
                      "summary": report["summary"]}, indent=2))


if __name__ == "__main__":
    main()
