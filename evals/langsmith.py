"""Opt-in LangSmith integration for canonical, synthetic local evaluations.

Default execution validates and writes a preview without reading credentials or
contacting a service. Content-free base tracing is a separate explicit option.
The external-experiment API uses extended retention; uploading requires an
additional cost acknowledgement and is never automatic on the free-plan path.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import uuid

from .schema import COUNTS, HASH, TOOL_NAMES, digest, gate, grade_case, render_case, validate_dataset

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "evals/datasets/servicevoice-golden-v1.json"
ENDPOINTS = {"https://api.smith.langchain.com", "https://eu.api.smith.langchain.com",
             "https://aws.api.smith.langchain.com"}
MAX_RESPONSE = 2 * 1024 * 1024
MAX_PAYLOAD = 2 * 1024 * 1024
SECRET = re.compile(r"\bBearer\s+\S+|\bAIza[\w-]{25,}|\bsk-[\w-]{16,}|\blsv2_[\w-]+|-----BEGIN [A-Z ]*PRIVATE KEY-----", re.I)
URL = re.compile(r"(?:https?://|file://|[A-Z]:[\\/])\S+", re.I)
EMAIL = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d ().-]{7,}\d)(?!\w)")


def need(condition, code):
    if not condition:
        raise ValueError(code)


def timestamp(value):
    need(isinstance(value, str), "invalid_timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("invalid_timestamp") from None
    need(result.tzinfo is not None, "timestamp_requires_timezone")
    return result.astimezone(timezone.utc)


def number(value, *, maximum=86_400_000):
    need(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= maximum, "invalid_duration")
    return float(value)


def safe_text(value, limit=5000):
    need(isinstance(value, str) and len(value) <= limit, "invalid_or_oversized_text")
    # Text export is limited to authored synthetic evidence, with common secret,
    # contact and URL patterns additionally removed. This is not general DLP.
    return PHONE.sub(lambda m: "[phone]" if sum(c.isdigit() for c in m[0]) >= 10 else m[0],
                     EMAIL.sub("[email]", URL.sub("[url_or_path]", SECRET.sub("[credential]", value))))


def _variables(effective_date):
    try:
        day = date.fromisoformat(effective_date)
    except (TypeError, ValueError):
        raise ValueError("invalid_effective_date") from None
    first = day + timedelta(days=1)
    while first.weekday() >= 5:
        first += timedelta(days=1)
    later = first + timedelta(days=1)
    while later.weekday() >= 5:
        later += timedelta(days=1)
    return {"DATE": first.isoformat(), "LATER_DATE": later.isoformat(), "NAME": "Taylor Reed",
            "PHONE": "2025550146", "VEHICLE": "2021 Toyota Camry", "CORRECTED_VEHICLE": "2022 Toyota Camry"}


def validate_report(dataset, report, *, dataset_sha256):
    """Bind every exported row to its authored case, oracle and immutable output.

    Failed mechanical checks are valid evidence and remain failed. Missing rows,
    source drift, modified oracles and malformed records are invalid artifacts.
    """
    validate_dataset(dataset)
    need(isinstance(report, dict) and report.get("schema_version") == 1 and report.get("mode") == "model"
         and report.get("status") == "complete", "completed_model_report_required")
    try:
        run_id = str(uuid.UUID(report.get("run_id")))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("invalid_run_id") from None
    need(run_id == report["run_id"], "canonical_run_uuid_required")
    info = report.get("dataset", {})
    need(isinstance(dataset_sha256, str) and HASH.fullmatch(dataset_sha256) and isinstance(info, dict)
         and info.get("sha256") == dataset_sha256 == info.get("sha256_after"), "dataset_digest_mismatch")
    need(info.get("id") == dataset["dataset_id"] and info.get("version") == dataset["version"], "dataset_identity_mismatch")
    result = gate(report)
    need(result["selected_coverage_complete"] and result["source_and_dataset_stable"], "incomplete_or_changed_evidence")
    start, end = timestamp(report.get("started_at")), timestamp(report.get("completed_at"))
    need(start <= end, "invalid_experiment_time_range")
    config = report.get("configuration")
    need(isinstance(config, dict) and report.get("effective_date_stable") is True, "effective_date_not_stable")
    variables = _variables(config.get("effective_date"))
    need(report.get("template_values") == variables, "noncanonical_synthetic_template_values")
    rows = report.get("cases")
    need(isinstance(rows, list) and 0 < len(rows) <= 40, "select_one_to_forty_case_attempts")
    index = {case["id"]: case for case in dataset["cases"]}
    total_ms = 0
    for row in rows:
        need(row["id"] in index and index[row["id"]]["execution"]["kind"] == "model_text", "noncanonical_model_case")
        case = render_case(index[row["id"]], variables)
        need(row.get("split") == case["split"] and row.get("criticality") == case["criticality"], "case_identity_mismatch")
        need(row.get("inputs") == {"turns": case["turns"]} and row.get("expected") == case["expected"], "case_contract_mismatch")
        output = row.get("outputs")
        need(isinstance(output, dict) and row.get("output_sha256") == digest(output), "output_digest_mismatch")
        turns, tools, states = output.get("turns"), output.get("tools"), output.get("turn_states")
        need(isinstance(turns, list) and len(turns) <= len(case["turns"]) and isinstance(tools, list)
             and len(tools) <= 100 and isinstance(states, list) and len(states) == len(turns), "invalid_output_shape")
        need(row.get("status") in {"completed", "error", "interrupted", "not_run"}, "unknown_case_status")
        for n, turn in enumerate(turns):
            need(isinstance(turn, dict) and turn.get("user") == case["turns"][n], "output_turn_input_mismatch")
            safe_text(turn.get("assistant"), 16000)
            number(turn.get("duration_ms"))
        if "partial_turn" in output:
            partial = output["partial_turn"]
            need(isinstance(partial, dict) and len(turns) < len(case["turns"])
                 and partial.get("user") == case["turns"][len(turns)], "partial_turn_input_mismatch")
            safe_text(partial.get("assistant"), 16000)
        for tool in tools:
            need(isinstance(tool, dict) and tool.get("name") in TOOL_NAMES | {"unknown_tool"}
                 and (tool.get("ok") is None or type(tool.get("ok")) is bool), "invalid_tool_record")
            number(tool.get("duration_ms"))
        recomputed = grade_case(case, states, tools, len(turns), final_state=output.get("state"))
        need(row.get("checks") == recomputed, "mechanical_checks_do_not_match_evidence")
        review = row.get("semantic_review")
        need(isinstance(review, dict) and review.get("status") in {"pending", "passed", "failed"}, "invalid_review_status")
        # Human/judge adjudication lives in bound sidecars. Never accept a
        # modified report status as a review or manufacture a passing score.
        need(review.get("status") == "pending", "semantic_review_requires_separate_bound_sidecar")
        total_ms += number(row.get("duration_ms"))
    need(total_ms <= (end - start).total_seconds() * 1000 + 50, "case_durations_exceed_experiment")
    return rows, start, end


def _state_summary(state):
    if not isinstance(state, dict):
        return {}
    return {key: value for key, value in state.items() if key in COUNTS and type(value) is int and value >= 0}


def build_payload(dataset, report, *, dataset_sha256):
    rows, start, end = validate_report(dataset, report, dataset_sha256=dataset_sha256)
    external = uuid.uuid5(uuid.NAMESPACE_URL, "servicevoice:" + dataset["dataset_id"] + ":" + dataset["version"] + ":" + dataset_sha256)
    payload = {
        "experiment_name": "servicevoice-" + report["run_id"],
        "experiment_description": "Authored synthetic text evaluation; mechanical feedback only. Semantic, voice and release acceptance remain separate.",
        "experiment_start_time": start.isoformat(), "experiment_end_time": end.isoformat(),
        "dataset_id": str(external), "dataset_name": dataset["dataset_id"] + "-" + dataset["version"] + "-" + dataset_sha256[:12],
        "experiment_metadata": {"local_run_id": report["run_id"], "dataset_sha256": dataset_sha256,
                                "report_sha256": digest(report), "source_manifest_sha256": digest(report["source"]["before"]),
                                "release_ready": False, "semantic_review": "pending",
                                "row_timestamps": "Sequential offsets reconstructed from measured case durations; no first-token/audio timing."},
        "results": [],
    }
    cursor = start
    for row in rows:
        duration = number(row["duration_ms"])
        row_end = min(end, cursor + timedelta(milliseconds=duration))
        output = row["outputs"]
        actual = {"status": row["status"], "turns": [{"user": safe_text(t["user"], 16000), "assistant": safe_text(t["assistant"], 16000)} for t in output["turns"]],
                  "tools": [{"name": t["name"], "ok": t["ok"], "duration_ms": t["duration_ms"]} for t in output["tools"]],
                  "state_counts": _state_summary(output.get("state")), "semantic_review": "pending"}
        if "partial_turn" in output:
            actual["partial_turn"] = {key: safe_text(output["partial_turn"][key], 16000) for key in ("user", "assistant")}
        expected = row["expected"]
        item = {"row_id": str(uuid.uuid5(external, row["id"] + ":" + str(row["attempt"]))),
                "inputs": {"turns": [safe_text(t, 16000) for t in row["inputs"]["turns"]]},
                "expected_outputs": {"required_tools": expected["required_tools"], "forbidden_tools": expected["forbidden_tools"],
                                     "semantic_rubric": [safe_text(t, 16000) for t in expected["semantic_rubric"]],
                                     "final_state_counts": _state_summary(expected["final"]), "canonical_oracle_sha256": digest(expected)},
                "actual_outputs": actual,
                "evaluation_scores": [{"key": c["name"], "score": 1 if c["passed"] else 0,
                                       "feedback_source": {"type": "api"}} for c in row["checks"]],
                "start_time": cursor.isoformat(), "end_time": row_end.isoformat(), "run_name": row["id"],
                "run_metadata": {"case_id": row["id"], "attempt": row["attempt"], "split": row["split"],
                                 "criticality": row["criticality"], "output_sha256": row["output_sha256"],
                                 "semantic_review": "pending", "release_ready": False}}
        if row["status"] != "completed":
            item["error"] = "local_case_" + row["status"]
        payload["results"].append(item)
        cursor = row_end
    need(len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()) <= MAX_PAYLOAD, "payload_too_large")
    return payload


def trace_report(dataset, report, telemetry, *, dataset_sha256):
    """Emit content-free base traces; never creates feedback or experiments."""
    rows, start, _ = validate_report(dataset, report, dataset_sha256=dataset_sha256)
    correlation = "eval:" + report["run_id"]
    common = {"run_id": report["run_id"], "dataset_sha256": dataset_sha256, "mode": "model"}
    pending = sum(row["semantic_review"]["status"] == "pending" for row in rows)
    passed = sum(check["passed"] for row in rows for check in row["checks"])
    total = sum(len(row["checks"]) for row in rows)
    accepted = int(telemetry.record_event("eval.run", {**common, "status": "passed" if gate(report)["mechanical_checks_passed"] else "failed",
                   "case_count": len(rows), "check_count": total, "passed_checks": passed, "failed_checks": total - passed,
                   "pending_reviews": pending}, correlation=correlation, start_unix=start.timestamp(),
                   duration_ms=(timestamp(report["completed_at"]) - start).total_seconds() * 1000))
    cursor = start.timestamp()
    for row in rows:
        successes = sum(check["passed"] for check in row["checks"])
        status = "passed" if row["status"] == "completed" and successes == len(row["checks"]) else "failed" if row["status"] == "completed" else "not_run" if row["status"] == "not_run" else "error"
        attrs = {**common, "case_id": row["id"], "status": status, "check_count": len(row["checks"]),
                 "passed_checks": successes, "failed_checks": len(row["checks"]) - successes,
                 "pending_reviews": int(row["semantic_review"]["status"] == "pending")}
        accepted += int(telemetry.record_event("eval.case", attrs, correlation=correlation, start_unix=cursor, duration_ms=row["duration_ms"]))
        cursor += row["duration_ms"] / 1000
    return {"events_planned": len(rows) + 1, "events_accepted": accepted, "content_exported": False,
            "feedback_created": False, "experiment_created": False, "remote_delivery_confirmed": False}


def trace_completion(outcome, status, closed):
    """Interpret post-close counters without equating ingestion and UI visibility."""
    need(isinstance(status, dict), "invalid_telemetry_status")
    reasons = {"disabled", "missing_api_key", "configuration_error", "dependency_unavailable",
               "initialization_failed", "ready", "closed"}
    safe = {"enabled": status.get("enabled") is True,
            "reason": status.get("reason") if status.get("reason") in reasons else "unknown"}
    for field in ("recorded", "exported", "export_failed", "quota_dropped", "filtered", "sampled_out", "budget_errors"):
        value = status.get(field, 0 if field == "budget_errors" else None)
        safe[field] = value if type(value) is int and value >= 0 else None
    planned = outcome["events_planned"]
    complete = (closed is True and safe["enabled"] is True and safe["reason"] == "closed"
                and planned > 0 and outcome["events_accepted"] == planned
                and safe["recorded"] == planned == safe["exported"]
                and all(safe[field] == 0 for field in ("export_failed", "quota_dropped", "budget_errors")))
    return {**outcome, "status": "ingest_accepted" if complete else "export_incomplete",
            "close_completed": closed is True, "telemetry": safe, "ingest_accepted": complete,
            "visibility_verified": False, "remote_delivery_confirmed": False,
            "note": "Exporter success acknowledges ingestion only; verify the project in LangSmith. No quality or release pass follows."}


def upload_settings(env_file=None):
    # Imported/read only after explicit upload. Environment wins for AWS secrets.
    from dotenv import dotenv_values
    values = {key: value for key, value in dotenv_values(env_file or ROOT / ".env", interpolate=False).items()
              if key in {"LANGSMITH_API_KEY", "LANGSMITH_ENDPOINT", "LANGSMITH_WORKSPACE_ID"}}
    key = (os.environ.get("LANGSMITH_API_KEY") or values.get("LANGSMITH_API_KEY") or "").strip()
    endpoint = (os.environ.get("LANGSMITH_ENDPOINT") or values.get("LANGSMITH_ENDPOINT") or "https://aws.api.smith.langchain.com").rstrip("/")
    workspace = (os.environ.get("LANGSMITH_WORKSPACE_ID") or values.get("LANGSMITH_WORKSPACE_ID") or "").strip()
    need(key and len(key) <= 1024 and not any(c.isspace() for c in key), "langsmith_key_missing_or_invalid")
    need(endpoint in ENDPOINTS, "unsupported_langsmith_endpoint")
    if workspace:
        try:
            need(str(uuid.UUID(workspace)) == workspace, "invalid_workspace_id")
        except ValueError:
            raise ValueError("invalid_workspace_id") from None
    return key, endpoint, workspace


async def upload_payload(payload, *, key, endpoint, workspace="", client=None):
    """One bounded POST, no redirects or retries; ambiguity is not success."""
    import asyncio
    import httpx
    need(endpoint in ENDPOINTS, "unsupported_langsmith_endpoint")
    headers = {"x-api-key": key}
    if workspace:
        headers["x-tenant-id"] = workspace
    own = client is None
    client = client or httpx.AsyncClient(timeout=35, follow_redirects=False, trust_env=False)
    try:
        async with asyncio.timeout(35):
            async with client.stream("POST", endpoint + "/api/v1/datasets/upload-experiment", json=payload,
                                     headers=headers, follow_redirects=False) as response:
                need(response.is_success, "langsmith_upload_http_error")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    need(len(raw) <= MAX_RESPONSE, "langsmith_response_too_large")
        data = json.loads(raw)
        need(isinstance(data, dict), "invalid_langsmith_response")
        ids = {}
        for kind in ("experiment", "dataset"):
            item = data.get(kind)
            need(isinstance(item, dict), "invalid_langsmith_response")
            try:
                ids[kind + "_id"] = str(uuid.UUID(item.get("id")))
            except (ValueError, TypeError, AttributeError):
                raise ValueError("invalid_langsmith_response") from None
        return {"status": "uploaded", **ids}
    finally:
        if own:
            await client.aclose()


def write_new(path, value):
    # create-new is the concurrency boundary, including the upload reservation.
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--output", type=Path, help="New preview file; existing files are never overwritten")
    parser.add_argument("--trace-upload", action="store_true", help="Send content-free base traces through the configured bounded exporter")
    parser.add_argument("--upload", action="store_true", help="Upload full synthetic experiment (extended retention may be billed)")
    parser.add_argument("--allow-extended-retention", action="store_true", help="Explicitly acknowledge possible experiment/extended trace charges")
    args = parser.parse_args(argv)
    try:
        need(not (args.upload and args.trace_upload), "choose_one_upload_mode")
        need(not args.upload or args.allow_extended_retention, "experiment_upload_requires_extended_retention_acknowledgement")
        raw = args.dataset.read_bytes()
        dataset, report = json.loads(raw), json.loads(args.report.read_text(encoding="utf-8"))
        dataset_hash = hashlib.sha256(raw).hexdigest()
        payload = build_payload(dataset, report, dataset_sha256=dataset_hash)
        output = args.output or args.report.with_name("langsmith-preview-" + uuid.uuid4().hex + ".json")
        need(output.resolve() not in {args.report.resolve(), args.dataset.resolve()}, "output_cannot_replace_input")
        write_new(output, payload)
        result = {"mode": "dry_run", "rows": len(payload["results"]), "preview": str(output), "provider_calls": 0,
                  "experiment_uploaded": False, "note": "Experiments may use billed extended retention; content-free base tracing is the free-plan path."}
        exit_code = 0
        if args.trace_upload:
            from app.observability import create_telemetry
            from scripts.check_langsmith import load_configuration
            # One loader anchors relative quota files to this checkout, filters
            # named settings and preserves literal credentials without expansion.
            telemetry = create_telemetry(load_configuration(ROOT))
            closed = False
            try:
                outcome = trace_report(dataset, report, telemetry, dataset_sha256=dataset_hash)
            finally:
                closed = telemetry.close(timeout_seconds=2)
            result = {"mode": "base_trace_export", **trace_completion(outcome, telemetry.status(), closed)}
            receipt_path = args.report.with_name("langsmith-base-trace-receipt-" + uuid.uuid4().hex + ".json")
            write_new(receipt_path, {**result, "run_id": report["run_id"], "report_sha256": digest(report),
                                     "dataset_sha256": dataset_hash})
            result["receipt"] = str(receipt_path)
            exit_code = 0 if result["ingest_accepted"] else 2
        if args.upload:
            import asyncio
            key, endpoint, workspace = upload_settings()
            # A local reservation survives a timeout/crash. This API does not
            # document an idempotency key, so blind retries can duplicate runs.
            journal = args.report.with_name("langsmith-experiment-upload.json")
            write_new(journal, {"status": "reserved_outcome_unknown_until_receipt", "run_id": report["run_id"], "payload_sha256": digest(payload),
                                "warning": "Do not retry an ambiguous POST. Inspect LangSmith before deliberately removing this reservation."})
            receipt = asyncio.run(upload_payload(payload, key=key, endpoint=endpoint, workspace=workspace))
            receipt_path = args.report.with_name("langsmith-experiment-receipt.json")
            write_new(receipt_path, {**receipt, "run_id": report["run_id"], "payload_sha256": digest(payload)})
            result = {"mode": "experiment_upload", **receipt, "receipt": str(receipt_path)}
        print(json.dumps(result, allow_nan=False))
        return exit_code
    except Exception:
        print(json.dumps({"status": "invalid_or_ambiguous", "error": "langsmith_export_configuration_artifact_or_upload_error",
                          "note": "No remote success is implied. An upload reservation must be checked before retrying."}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
