"""Local experiment runner. Default is offline; model requires explicit --execute.

Every run gets a fresh directory. Model tests use synthetic cases and disposable
databases. No Vapi calls, production scheduler writes, or telemetry uploads.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import uuid

from .schema import digest, gate, grade_case, render_case, select_cases, validate_dataset

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "evals/datasets/servicevoice-golden-v1.json"


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def file_sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_manifest():
    paths = [*ROOT.glob("app/**/*.py"), *ROOT.glob("evals/**/*.py"),
             ROOT / "runtime_sqlite.py", ROOT / "scripts/evaluate_gemini.py",
             ROOT / "scripts/evaluate_voice_quality.py", ROOT / "requirements.txt"]
    return {str(p.relative_to(ROOT)).replace("\\", "/"): file_sha(p) for p in sorted(paths) if p.is_file()}


def write_json(path, value, *, exclusive=False):
    with path.open("x" if exclusive else "w", encoding="utf-8", newline="\n") as f:
        json.dump(value, f, indent=2, ensure_ascii=False, allow_nan=False)
        f.write("\n")


def checkpoint(directory, report):
    # The working checkpoint can change; completed evidence is exclusively created.
    temp = directory / "checkpoint.tmp"
    write_json(temp, report)
    temp.replace(directory / "checkpoint.json")


def new_run(dataset, dataset_path, mode, selection, output_root):
    rid = str(uuid.uuid4())
    directory = output_root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + rid)
    directory.mkdir(parents=True, exist_ok=False)
    report = {"schema_version": 1, "run_id": rid, "mode": mode, "started_at": utcnow(),
              "completed_at": None, "status": "running", "selection": selection,
              "dataset": {"id": dataset["dataset_id"], "version": dataset["version"],
                          "sha256": file_sha(dataset_path), "review_status": dataset["review_status"]},
              "source": {"before": source_manifest()}, "cases": [],
              "limitations": ["Authored acceptance corpus, not a published benchmark score or representative production sample.",
                              "No acoustic/ASR/TTS, Vapi, gateway/JEV routing, CRM/DMS or deployment claim follows from a text run.",
                              "Pending semantic review and missing layers never count as passed.",
                              "HTTP durations are full model responses, not time to first token or first audible response."]}
    checkpoint(directory, report)
    return directory, report


def render_report(report):
    e = html.escape
    cards = []
    for case in report["cases"]:
        failing = [x for x in case.get("checks", []) if x.get("passed") is not True]
        title = f'{case["id"]} · attempt {case["attempt"]} · {case["status"]}'
        cards.append(f'<details><summary>{e(title)} · {len(failing)} failed checks</summary>')
        if failing:
            cards.append('<pre>' + e(json.dumps(failing, indent=2, ensure_ascii=False)) + '</pre>')
        for turn in case.get("outputs", {}).get("turns", []):
            cards.append('<p><b>Caller:</b> ' + e(turn.get("user", "")) + '</p><p><b>Assistant:</b> ' + e(turn.get("assistant", "")) + '</p>')
        if case.get("outputs", {}).get("tools"):
            cards.append('<details><summary>Tool evidence and checkpoints</summary><pre>' + e(json.dumps(case["outputs"], indent=2, ensure_ascii=False)) + '</pre></details>')
        cards.append('<p>Semantic review: ' + e(case.get("semantic_review", {}).get("status", "not_applicable")) + '</p></details>')
    body = f'''<!doctype html><html lang="en"><meta charset="utf-8"><title>ServiceVoice evaluation</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><style>
body{{font:16px/1.55 system-ui;background:#f6f8fb;color:#162539;max-width:1100px;margin:40px auto;padding:0 24px}}h1{{font-size:32px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#edf1f6;padding:16px;border-radius:8px}}details{{background:white;border:1px solid #d6dfe9;padding:16px;margin:14px 0;border-radius:8px}}summary{{cursor:pointer;font-weight:600}}.note{{border-left:4px solid #b17608;padding:12px 20px;background:#fff5dc}}a{{color:#125c9e}}</style>
<h1>ServiceVoice evaluation</h1><p>{e(report["mode"])} · {e(report["run_id"])} · {e(report["started_at"])}</p>
<p class="note"><b>Scope: {e(report["gate"]["execution_result"])}.</b> This is one evaluation layer. Release readiness is not established. See the separate semantic review template and remaining voice checks.</p>
<p><a href="result.json">Complete evidence</a> · <a href="review-template.json">Semantic review template</a></p>
<h2>Gates</h2><pre>{e(json.dumps(report["gate"], indent=2))}</pre>
<h2>Metrics</h2><pre>{e(json.dumps(report.get("metrics", {}), indent=2))}</pre>
<h2>Cases</h2>{''.join(cards)}<h2>Limits</h2><ul>{''.join('<li>'+e(x)+'</li>' for x in report["limitations"])}</ul></html>'''
    return body


def finish(directory, report, dataset_path):
    report["completed_at"] = utcnow()
    report["status"] = "complete"
    report["source"]["after"] = source_manifest()
    report["dataset"]["sha256_after"] = file_sha(dataset_path)
    report["gate"] = gate(report)
    from .metrics import summarize_cases
    report["metrics"] = summarize_cases(report["cases"])
    report["reliability"] = reliability(report["cases"], report["selection"])
    # Build all companion artifacts before publishing a result commit marker.
    # A missing reviewer module, bad template, or failed companion write cannot
    # leave a completed result.json that implies the handoff was finalized.
    staged = []
    try:
        from .review import make_review_template
        payloads = {
            "review-template.json": json.dumps(make_review_template(report), indent=2, ensure_ascii=False, allow_nan=False) + "\n",
            "report.html": render_report(report),
            "result.json": json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        }
        for name, payload in payloads.items():
            temporary = directory / (".finalize-" + uuid.uuid4().hex + ".tmp")
            staged.append((temporary, directory / name))
            with temporary.open("x", encoding="utf-8", newline="\n") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
        for temporary, destination in staged:
            os.link(temporary, destination)  # Atomic create-new; never overwrite.
        checkpoint(directory, report)
    except Exception:
        report["status"] = "finalization_failed"
        report["finalization_error"] = "artifact_finalization_failed"
        checkpoint(directory, report)
        raise ValueError("artifact_finalization_failed") from None
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)
    print(json.dumps({"run_id": report["run_id"], "report": str(directory / "report.html"),
                      "execution_result": report["gate"]["execution_result"],
                      "release_ready": False, "cases": len(report["cases"])}))
    return 0 if report["gate"]["mechanical_checks_passed"] else 1


def reliability(cases, selection):
    from math import comb
    grouped = {}
    for selected in selection:
        grouped.setdefault(selected["id"], []).append(selected["attempt"])
    rows = []
    for cid, expected_attempts in grouped.items():
        selected_rows = [c for c in cases if c.get("id") == cid]
        successes = sum(c.get("status") == "completed" and bool(c.get("checks")) and all(x.get("passed") is True for x in c["checks"]) for c in selected_rows)
        complete = Counter(c.get("attempt") for c in selected_rows) == Counter(expected_attempts)
        n = len(expected_attempts)
        rows.append({"id": cid, "planned_trials": n, "observed_trials": len(selected_rows),
                     "mechanical_successes": successes, "coverage_complete": complete,
                     "mechanical_pass_power_k": {str(k): (comb(successes, k) / comb(n, k) if complete and successes >= k else 0.0 if complete else None) for k in (1, 2, 3) if n >= k}})
    return {"label": "Mechanical reliability only; semantic/task success remains ungraded.",
            "estimator": "C(successes,k)/C(trials,k); all planned attempts included; no best-of-k selection.",
            "cases": rows}


def variables_for(day):
    first = day + timedelta(days=1)
    while first.weekday() >= 5:
        first += timedelta(days=1)
    later = first + timedelta(days=1)
    while later.weekday() >= 5:
        later += timedelta(days=1)
    return {"DATE": first.isoformat(), "LATER_DATE": later.isoformat(), "NAME": "Taylor Reed",
            "PHONE": "2025550146", "VEHICLE": "2021 Toyota Camry", "CORRECTED_VEHICLE": "2022 Toyota Camry"}


def state_evidence(conversation, tools):
    store, sid = conversation.store, conversation.sid
    snapshot = store.snapshot(sid)
    with store.connect() as db:
        counts = {table: db.execute(f"SELECT COUNT(*) FROM {table} WHERE session_id=?", (sid,)).fetchone()[0]
                  for table in ("appointments", "proposals", "handoffs")}
        row = db.execute("SELECT payload FROM proposals WHERE session_id=? AND state='pending' ORDER BY rowid DESC LIMIT 1", (sid,)).fetchone()
        pending = json.loads(row["payload"]) if row else {}
        counts["pending_proposals"] = db.execute("SELECT COUNT(*) FROM proposals WHERE session_id=? AND state='pending'", (sid,)).fetchone()[0]
        booked = db.execute("SELECT customer FROM appointments WHERE session_id=? ORDER BY rowid DESC LIMIT 1", (sid,)).fetchone()
        customer = pending.get("customer") or (json.loads(booked["customer"]) if booked else {})
    appointment = snapshot.get("appointment") or {}
    counts.update({"confirm_calls": sum(t.get("name") == "confirm_action" for t in tools),
                   "appointment_status": appointment.get("status"),
                   "handoff_status": (snapshot.get("handoff") or {}).get("status"),
                   "service": pending.get("service") or appointment.get("service"),
                   "vehicle": pending.get("customer", {}).get("vehicle") or appointment.get("vehicle"),
                   "customer_name": customer.get("name"), "phone": customer.get("phone"),
                   "appointment_start": appointment.get("start"),
                   "proposal_summary": pending.get("summary")})
    return counts


class TraceSanitizer:
    """Only synthetic task facts; opaque local object IDs get stable aliases."""
    FIELDS = {"service", "date", "customer_name", "phone", "vehicle", "confirmed", "reason",
              "slot_id", "proposal_id", "operation_id", "handoff_id", "time_filter", "exact_time",
              "start_time", "end_time", "start_inclusive", "end_inclusive", "ok", "error", "status",
              "summary", "message", "slots", "start", "label", "remaining", "available", "query",
              "complete", "result_count", "matching_count", "returned_count", "has_more", "limit",
              "total_matching", "truncated", "horizon_start", "horizon_end", "timezone",
              "appointment", "service_name", "customer", "name", "reference", "kind", "id"}
    def __init__(self):
        self.ids = {}

    def clean(self, value, key="", depth=0):
        if depth > 7:
            return "[depth limit]"
        if isinstance(value, dict):
            return {k: self.clean(v, k, depth + 1) for k, v in value.items() if k in self.FIELDS}
        if isinstance(value, list):
            return [self.clean(v, key, depth + 1) for v in value[:20]]
        if isinstance(value, str):
            if key in {"id", "slot_id", "proposal_id", "operation_id", "handoff_id", "reference"}:
                if value not in self.ids:
                    self.ids[value] = f"local_object_{len(self.ids) + 1}"
                return self.ids[value]
            return value[:5000]
        return value if value is None or type(value) in {int, bool, float} else None


REFERENCE = re.compile(r"CSV-[0-9A-F]{8}")
REFERENCE_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])CSV-[0-9A-F]{8}(?![A-Za-z0-9_-])", re.I)
REFERENCE_HASH = re.compile(r"[a-f0-9]{64}")


def result_reference_sha256(result):
    """Retain equality evidence for a valid synthetic tool reference, not its ID."""
    if not isinstance(result, dict) or result.get("ok") is not True:
        return None
    appointment = result.get("appointment")
    reference = appointment.get("reference") if isinstance(appointment, dict) else None
    if not isinstance(reference, str) or REFERENCE.fullmatch(reference) is None:
        return None
    return hashlib.sha256(reference.encode("utf-8")).hexdigest()


def reference_readback_evidence(outputs):
    """Advisory literal equality, never a semantic, consent or acoustic verdict.

    Only appointment results available by the end of that caller turn supply
    the expected digest. Legacy reports, invalid hashes and missing evidence
    stay unverifiable. Quoted/negated codes still need contextual review; spoken
    letter names and translated formatting are outside this narrow matcher.
    Turn-level evidence cannot prove speech followed tool completion.
    """
    if not isinstance(outputs, dict):
        return []
    turns, tools = outputs.get("turns"), outputs.get("tools")
    if not isinstance(turns, list) or len(turns) > 30 or not isinstance(tools, list) or len(tools) > 100:
        return []
    evidence = []
    for number, turn in enumerate(turns, 1):
        expected, source = None, None
        for index, tool in enumerate(tools, 1):
            if not isinstance(tool, dict) or tool.get("ok") is not True:
                continue
            user_turn, result = tool.get("user_turn"), tool.get("result")
            if type(user_turn) is not int or not 1 <= user_turn <= number or not isinstance(result, dict) or "appointment" not in result:
                continue
            # A later result without an available digest invalidates the old
            # comparison target. Do not borrow it from a future turn or alias.
            source = "tool:" + str(index)
            candidate = tool.get("result_reference_sha256")
            appointment = result.get("appointment")
            expected = (candidate if isinstance(appointment, dict) and isinstance(candidate, str)
                        and REFERENCE_HASH.fullmatch(candidate) is not None else None)
        text = turn.get("assistant") if isinstance(turn, dict) else None
        readable = isinstance(text, str) and len(text) <= 16000
        candidates = REFERENCE_TOKEN.findall(text) if readable else []
        bounded = readable and len(candidates) <= 32
        matching = (sum(hashlib.sha256(value.upper().encode("utf-8")).hexdigest() == expected
                        for value in candidates) if bounded and expected else None)
        evidence.append({"user_turn": number, "method": "canonical-reference-sha256-v1",
                         "source_tool": source, "expected_reference_available": expected is not None,
                         "candidate_count": len(candidates) if bounded else None,
                         "matching_candidates": matching,
                         "mismatching_candidates": len(candidates) - matching if matching is not None else None,
                         "status": "unverifiable" if expected is None or not bounded else
                                   "not_mentioned" if not candidates else
                                   "match" if matching == len(candidates) else "mismatch"})
    return evidence


async def execute_case(case, attempt, client, key, budget, directory, report, *, transport="direct", jev_key=None):
    from runtime_sqlite import ensure_sqlite
    ensure_sqlite()
    from app.domain import Store
    from scripts.evaluate_gemini import Conversation
    from scripts.evaluate_voice_quality import RecordingClient, repetition_flags
    row = {"id": case["id"], "attempt": attempt, "family": case["family"], "category": case["category"],
           "split": case["split"], "criticality": case["criticality"], "status": "running",
           "inputs": {"turns": case["turns"]}, "expected": case["expected"],
           "outputs": {"turns": [], "tools": [], "turn_states": [], "state": {}},
           "requests": [], "checks": [], "semantic_review": {"status": "pending", "reviewer_kind": None}}
    report["cases"].append(row)
    checkpoint(directory, report)
    if case["execution"]["kind"] != "model_text":
        row["status"] = "not_run"
        row["reason"] = "requires_" + case["execution"]["kind"]
        return row
    started = time.perf_counter()
    interrupted = None
    with tempfile.TemporaryDirectory(prefix="servicevoice-golden-") as tmp:
        recording = RecordingClient(client, row["requests"], lambda: checkpoint(directory, report))
        if transport == "gateway":
            from .gateway_transport import GatewayConversation
            conversation = GatewayConversation(recording, Store(Path(tmp) / "case.sqlite3"), key, budget, jev_key=jev_key)
        else:
            conversation = Conversation(recording, Store(Path(tmp) / "case.sqlite3"), key, budget)
        recording.turn_number = lambda: len(conversation.turns)
        row["prompt_context"] = conversation.prompt_metadata
        sanitizer = TraceSanitizer()
        original = conversation.harness.run
        async def traced(sid, name, arguments, tool_id):
            from app.tools import DEFINITIONS
            event = {"user_turn": len(conversation.turns), "name": name if isinstance(name, str) and name in DEFINITIONS else "unknown_tool", "arguments": sanitizer.clean(arguments), "ok": None}
            row["outputs"]["tools"].append(event)
            begin = time.perf_counter()
            try:
                result = await original(sid, name, arguments, tool_id)
                event["ok"] = result.get("ok") is True
                event["error"] = result.get("error")
                reference_hash = result_reference_sha256(result)
                if reference_hash is not None:
                    event["result_reference_sha256"] = reference_hash
                event["result"] = sanitizer.clean(result)
                return result
            finally:
                event["duration_ms"] = round((time.perf_counter() - begin) * 1000, 3)
                checkpoint(directory, report)
        conversation.harness.run = traced
        try:
            async with asyncio.timeout(180):
                for text in case["turns"]:
                    begin = time.perf_counter()
                    turn = await conversation.say(text)
                    row["outputs"]["turns"].append({"user": text, "assistant": turn["assistant"],
                                                   "duration_ms": round((time.perf_counter() - begin) * 1000, 2),
                                                   "repetition_advisory": repetition_flags(turn["assistant"])})
                    row["outputs"]["turn_states"].append(state_evidence(conversation, row["outputs"]["tools"]))
                    checkpoint(directory, report)
            row["status"] = "completed"
        except Exception as exc:
            row["status"] = "error"
            from scripts.evaluate_gemini import EvaluationFailure
            from httpx import TimeoutException
            row["error_code"] = "timeout" if isinstance(exc, (TimeoutError, TimeoutException)) else "request_budget_exhausted" if isinstance(exc, EvaluationFailure) and str(exc) == "Global model-request cap reached." else "model_or_tool_execution_failed"
        except BaseException as exc:
            row["status"] = "interrupted"
            row["error_code"] = "execution_interrupted"
            interrupted = exc
        finally:
            # Preserve partial text and request attempts before cleanup can fail.
            if len(conversation.turns) > len(row["outputs"]["turns"]):
                partial = conversation.turns[-1]
                row["outputs"]["partial_turn"] = {"user": partial["user"], "assistant": partial["assistant"]}
            checkpoint(directory, report)
            try:
                if transport == "gateway":
                    await conversation.close()
                else:
                    await conversation.harness.close()
            except Exception:
                row["status"] = "error"
                row["error_code"] = "harness_cleanup_failed"
            except BaseException as exc:
                row["status"] = "interrupted"
                row["error_code"] = "cleanup_interrupted"
                interrupted = interrupted or exc
            try:
                row["outputs"]["state"] = state_evidence(conversation, row["outputs"]["tools"])
            except Exception:
                row["status"] = "error"
                row["error_code"] = "final_state_unavailable"
            row["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
            if transport == "gateway":
                row["outputs"]["gateway_generations"] = conversation.generations
            checkpoint(directory, report)
    row["checks"] = grade_case(case, row["outputs"]["turn_states"], row["outputs"]["tools"], len(row["outputs"]["turns"]), final_state=row["outputs"]["state"])
    row["outputs"]["reference_readback"] = reference_readback_evidence(row["outputs"])
    row["output_sha256"] = digest(row["outputs"])
    checkpoint(directory, report)
    if interrupted is not None:
        raise interrupted
    print(json.dumps({"case": row["id"], "attempt": attempt, "status": row["status"],
                      "failed_checks": [c["name"] for c in row["checks"] if not c["passed"]]}), flush=True)
    return row


async def run_model(args, dataset, selected):
    from runtime_sqlite import ensure_sqlite
    ensure_sqlite()
    from app.domain import today, dealership_facts
    from app.tools import SYSTEM_PROMPT, SYSTEM_PROMPT_VERSION
    from scripts.evaluate_gemini import MODEL
    import httpx
    from dotenv import dotenv_values
    # Only the explicit model command reads the existing provider credential.
    local_values = dotenv_values(ROOT / ".env", interpolate=False)
    key = (os.environ.get("GEMINI_API_KEY") or local_values.get("GEMINI_API_KEY") or "").strip()
    if not key:
        raise ValueError("GEMINI_API_KEY missing; no requests made")
    selection = [{"id": c["id"], "attempt": n} for n in range(1, args.repetitions + 1) for c in selected]
    directory, report = new_run(dataset, args.dataset, "model", selection, args.output_root)
    effective_day = today()
    transport = getattr(args, "transport", "direct")
    if transport == "gateway":
        report["limitations"][1] = "Exercises the streaming gateway, Jev/receipt routing and disposable scheduler; no Vapi, acoustic, CRM/DMS or running deployment acceptance."
    jev_key = (os.environ.get("TYPESAFE_API_KEY") or local_values.get("TYPESAFE_API_KEY") or "").strip() if transport == "gateway" else None
    report["configuration"] = {"model": MODEL, "reasoning_effort": "low", "prompt_version": SYSTEM_PROMPT_VERSION,
                               "base_prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
                               "effective_date": effective_day.isoformat(), "timezone": "America/Chicago",
                               "trusted_dealership_facts": dealership_facts(effective_day),
                               "max_requests": args.max_requests, "repetitions": args.repetitions,
                               "concurrency": 1, "transport": "streaming_gateway" if transport == "gateway" else "direct_nonstreaming_model_http",
                               "jev_faq_enabled": bool(jev_key), "receipt_narration_enabled": transport == "gateway",
                               "temperature": "provider_default", "usd_cost": None}
    report["coverage"] = {"selected_ids": [c["id"] for c in selected], "unselected_ids": [c["id"] for c in dataset["cases"] if c not in selected]}
    budget = [args.max_requests]
    variables = variables_for(effective_day)
    report["template_values"] = variables
    async with httpx.AsyncClient(timeout=35, follow_redirects=False, trust_env=False) as client:
        for item in selection:
            raw = next(c for c in selected if c["id"] == item["id"])
            case = render_case(raw, variables)
            await execute_case(case, item["attempt"], client, key, budget, directory, report, transport=transport, jev_key=jev_key)
    report["model_requests_consumed"] = args.max_requests - budget[0]
    report["effective_date_stable"] = today() == effective_day
    if not report["effective_date_stable"]:
        report["source"]["before"]["effective_date_changed"] = True
    return finish(directory, report, args.dataset)


async def run_contracts(args, dataset):
    from .contracts import CONTRACT_IDS, run_contract_suite
    directory, report = new_run(dataset, args.dataset, "contracts", [{"id": cid, "attempt": 1} for cid in CONTRACT_IDS], args.output_root)
    for item in await run_contract_suite():
        report["cases"].append({**item, "attempt": 1, "status": "completed" if item["passed"] else "error",
                                "semantic_review": {"status": "not_applicable"}})
    return finish(directory, report, args.dataset)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", choices=["validate", "contracts", "model"], default="contracts")
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--output-root", type=Path, default=ROOT / "docs/evals/runs")
    parser.add_argument("--split", choices=["regression", "dev", "holdout"], default="regression")
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--max-requests", type=int, default=30)
    parser.add_argument("--transport", choices=("direct", "gateway"), default="direct",
                        help="Gateway exercises the actual streaming policy, receipt renderer and optional Jev in a disposable database")
    parser.add_argument("--execute", action="store_true", help="Explicitly enable paid model requests; never voice calls")
    args = parser.parse_args(argv)
    if not 1 <= args.repetitions <= 10 or not 1 <= args.max_requests <= 300:
        parser.error("Repetitions 1..10, model request cap 1..300")
    try:
        dataset = validate_dataset(json.loads(args.dataset.read_text(encoding="utf-8")))
        selected = select_cases(dataset, args.split, args.case_ids)
        if args.mode == "validate" or (args.mode == "model" and not args.execute):
            print(json.dumps({"schema": "valid", "dataset": dataset["dataset_id"], "cases": len(dataset["cases"]),
                              "selected": [c["id"] for c in selected], "mode": "plan_only", "provider_calls": 0,
                              "note": "Validation/plan only; no behavioral or semantic test has passed."}))
            return 0
        if args.mode == "contracts":
            return asyncio.run(run_contracts(args, dataset))
        return asyncio.run(run_model(args, dataset, selected))
    except (ValueError, OSError) as exc:
        # No provider exception bodies reach here in the model loop.
        print(json.dumps({"error": "configuration_or_artifact_error", "type": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
