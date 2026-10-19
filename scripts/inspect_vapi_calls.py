"""Read scoped Vapi call evidence without persisting conversations or credentials.

Only main() loads project configuration or makes a request. Importable helpers
support offline tests. The API response exists in memory only; the saved report
is constructed from an explicit allowlist, never by deleting sensitive fields.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "docs" / "live-call-evidence.json"
API_BASE = "https://api.vapi.ai"
SDK_SOURCE = "https://github.com/VapiAI/client-sdk-web/blob/main/api.ts"
MAX_RESPONSE_BYTES = 12 * 1024 * 1024
TOOL_NAMES = frozenset({
    "get_dealership_info", "begin_service_request", "search_availability", "prepare_first_available_booking", "prepare_booking",
    "confirm_action", "dismiss_proposal", "get_appointment", "prepare_reschedule",
    "prepare_cancellation", "request_handoff", "withdraw_handoff", "get_session_context", "get_operation",
})
TURN_FIELDS = ("modelLatency", "voiceLatency", "transcriberLatency", "endpointingLatency", "turnLatency")
AVERAGE_FIELDS = tuple(field + "Average" for field in TURN_FIELDS)
TRANSPORT_FIELDS = ("fromTransportLatencyAverage", "toTransportLatencyAverage")
COUNT_FIELDS = ("numUserInterrupted", "numAssistantInterrupted")
COST_FIELDS = ("transport", "stt", "llm", "tts", "vapi", "chat", "total")
USAGE_FIELDS = ("llmPromptTokens", "llmCompletionTokens", "llmCachedPromptTokens", "ttsCharacters")
ANALYSIS_FIELDS = ("summary", "structuredData", "successEvaluation", "structuredOutput")
CALL_TYPES = {"webCall", "vapi.websocketCall", "inboundPhoneCall", "outboundPhoneCall"}
CALL_STATUSES = {"scheduled", "queued", "ringing", "in-progress", "forwarding", "ended", "not-found", "deletion-failed"}


def numeric(value):
    try:
        return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def enum_value(value, allowed, fallback=None):
    return value if isinstance(value, str) and value in allowed else fallback


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, OverflowError):
        return None


def iso(value):
    parsed = timestamp(value)
    return parsed.isoformat().replace("+00:00", "Z") if parsed else None


def identifier(value):
    try:
        return str(UUID(value)) if isinstance(value, str) else None
    except ValueError:
        return None


def safe_reason(value):
    # endedMessage is deliberately excluded: it can contain arbitrary payloads.
    return value if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9.\-]{0,179}", value) else None


def performance(call):
    artifact = call.get("artifact")
    metrics = artifact.get("performanceMetrics") if isinstance(artifact, dict) else None
    if not isinstance(metrics, dict):
        return {"available": False, "missing": ["artifact.performanceMetrics"]}
    values = {key: numeric(metrics.get(key)) for key in AVERAGE_FIELDS + TRANSPORT_FIELDS + COUNT_FIELDS}
    turns = metrics.get("turnLatencies")
    return {
        "available": True, "values": values,
        "missing": [key for key, value in values.items() if value is None],
        "turn_latencies": [
            {key: numeric(turn.get(key)) for key in TURN_FIELDS}
            for turn in turns if isinstance(turn, dict)
        ] if isinstance(turns, list) else None,
    }


def provider_tools(call):
    artifact = call.get("artifact")
    messages = artifact.get("messages") if isinstance(artifact, dict) else None
    source = "artifact.messages"
    if not isinstance(messages, list):
        messages, source = call.get("messages"), "messages"
    if not isinstance(messages, list):
        return {"available": False, "missing": ["artifact.messages or messages"]}
    calls, results, unnamed = {}, {}, 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        tool_calls = message.get("toolCalls")
        for tool in tool_calls if isinstance(tool_calls, list) else []:
            if not isinstance(tool, dict):
                continue
            tool_id = tool.get("id")
            function = tool.get("function")
            name = function.get("name") if isinstance(function, dict) else None
            if isinstance(tool_id, str) and tool_id:
                calls[tool_id] = enum_value(name, TOOL_NAMES, "other")
            else:
                unnamed += 1
        tool_id = message.get("toolCallId")
        if not isinstance(tool_id, str) or "result" not in message:
            continue
        result = message.get("result")
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except (ValueError, RecursionError):
                result = None
        ok = result.get("ok") if isinstance(result, dict) else None
        outcome = "ok_true" if ok is True else "ok_false" if ok is False else "unclassified"
        # Conflicting repeated artifacts are not silently counted as success.
        if tool_id in results and results[tool_id] != outcome:
            results[tool_id] = "conflicting_results"
        else:
            results[tool_id] = outcome
    outcomes = Counter(results.values())
    return {
        "available": True, "source": source,
        "unique_tool_call_ids": len(calls), "calls_without_id": unnamed,
        "calls_by_name": dict(sorted(Counter(calls.values()).items())),
        "unique_result_ids": len(results),
        "result_counts": {key: outcomes[key] for key in ("ok_true", "ok_false", "unclassified", "conflicting_results")},
        "call_ids_without_result": len(set(calls) - set(results)),
        "result_ids_without_call": len(set(results) - set(calls)),
        "interpretation": "Explicit tool-result ok flags only; tool success is not proof of a booking or caller consent.",
    }


def local_evidence(db_path, call_id):
    """Read operational columns only; never select customer, state or transcript."""
    if not db_path.is_file():
        return {"available": False, "reason": "database_missing"}
    try:
        with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")  # All counters describe the same read snapshot.
            sessions = db.execute(
                "SELECT id FROM sessions WHERE provider_call_id=? AND tenant=?",
                (call_id, "cedar-demo"),
            ).fetchall()
            if len(sessions) != 1:
                return {"available": True, "session_binding": "not_found" if not sessions else "ambiguous", "appointment_evidence": "unavailable"}
            sid = sessions[0][0]
            rows = db.execute(
                "SELECT name,status,COUNT(*) FROM events WHERE session_id=? GROUP BY name,status", (sid,)
            ).fetchall()
            counts = Counter()
            for name, status, count in rows:
                counts[(name if name in TOOL_NAMES else "other", status if status in {"success", "error"} else "other")] += count
            appointments = Counter()
            for status, count in db.execute("SELECT status,COUNT(*) FROM appointments WHERE session_id=? AND tenant=? GROUP BY status", (sid, "cedar-demo")):
                appointments[status if status in {"confirmed", "cancelled"} else "other"] += count
            commits = Counter()
            for kind, count in db.execute("SELECT kind,COUNT(*) FROM proposals WHERE session_id=? AND tenant=? AND state='committed' GROUP BY kind", (sid, "cedar-demo")):
                commits[kind if kind in {"book", "reschedule", "cancel"} else "other"] += count
            return {
                "available": True, "session_binding": "matched_provider_call_id",
                "tool_events": [{"name": name, "status": status, "count": count} for (name, status), count in sorted(counts.items())],
                "tool_event_total": sum(counts.values()),
                "tool_success_count": sum(count for (_, status), count in counts.items() if status == "success"),
                "tool_error_count": sum(count for (_, status), count in counts.items() if status == "error"),
                "appointments_by_current_status": dict(sorted(appointments.items())),
                "appointment_count": sum(appointments.values()),
                "committed_actions": dict(sorted(commits.items())),
                "appointment_evidence": "persisted_rows_observed" if appointments else "no_appointment_rows_observed",
                "interpretation": "Local mock-scheduler snapshot, not a real DMS booking, spoken accuracy or independently verified consent. Events exclude cached replays and pre-dispatch rejections.",
            }
    except sqlite3.Error:
        return {"available": False, "reason": "database_unreadable_or_schema_incompatible"}


def sanitize_call(call, db_path):
    started, ended = timestamp(call.get("startedAt")), timestamp(call.get("endedAt"))
    duration = (ended - started).total_seconds() if started and ended and ended >= started else None
    breakdown = call.get("costBreakdown")
    breakdown = breakdown if isinstance(breakdown, dict) else {}
    analysis = breakdown.get("analysisCostBreakdown")
    analysis = analysis if isinstance(analysis, dict) else {}
    provider = {
        "id": identifier(call.get("id")),
        "type": enum_value(call.get("type"), CALL_TYPES),
        "status": enum_value(call.get("status"), CALL_STATUSES),
        "ended_reason": safe_reason(call.get("endedReason")),
        "created_at": iso(call.get("createdAt")), "updated_at": iso(call.get("updatedAt")),
        "started_at": iso(call.get("startedAt")), "ended_at": iso(call.get("endedAt")),
        "duration_seconds": duration, "cost_usd": numeric(call.get("cost")),
    }
    return {
        "provider": provider,
        "missing_provider_fields": [key for key, value in provider.items() if value is None],
        "transport_evidence": {
            "provider_call_record_exists": True,
            "provider_start_observed": bool(started) or provider["status"] in {"in-progress", "forwarding"},
            "basis": "startedAt" if started else "active_status" if provider["status"] in {"in-progress", "forwarding"} else "start_not_observed",
            "audible_audio_verified": None,
            "interpretation": "Provider start metadata does not prove microphone capture, audible reply or completed business task.",
        },
        "cost_breakdown_usd": {key: numeric(breakdown.get(key)) for key in COST_FIELDS},
        "usage": {key: numeric(breakdown.get(key)) for key in USAGE_FIELDS},
        "analysis_cost_breakdown_usd": {key: numeric(analysis.get(key)) for key in ANALYSIS_FIELDS},
        "performance": performance(call), "provider_tools": provider_tools(call),
        "local_scheduler": local_evidence(db_path, call["id"]),
    }


def build_report(raw_calls, assistant_id, since, limit, latest, db_path):
    """Return None for no exact in-window calls. Never reinterpret absence as pass."""
    selected, dropped = [], 0
    for call in raw_calls:
        if not isinstance(call, dict) or call.get("assistantId") != assistant_id:
            dropped += 1
            continue
        created = timestamp(call.get("createdAt"))
        if not identifier(call.get("id")) or not created or created < since:
            dropped += 1
            continue
        selected.append(call)
    selected.sort(key=lambda call: timestamp(call["createdAt"]), reverse=True)
    if latest:
        selected = selected[:1]
    if not selected:
        return None
    return {
        "schema_version": 1,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "evidence_kind": "observed_provider_records_and_local_mock_scheduler",
        "scope": {
            "assistant_id": assistant_id, "created_at_since": since.isoformat(),
            "request_limit": limit, "returned_count": len(raw_calls), "reported_count": len(selected),
            "discarded_out_of_scope_or_invalid_count": dropped,
            "possibly_truncated": len(raw_calls) >= limit,
            "selection": "newest_in_returned_batch" if latest else "all_matching_in_returned_batch",
            "ordering_note": "Locally sorted by createdAt. GET /call schema does not promise a sort order; a limit-sized batch may omit newer/other calls. Narrow --since if needed.",
        },
        "calls": [sanitize_call(call, db_path) for call in selected],
        "metric_definitions": {
            "source": SDK_SOURCE,
            "provider_location": "Call.artifact.performanceMetrics",
            "modelLatency": "Provider time to first model token.",
            "voiceLatency": "Provider voice latency from model output.",
            "transcriberLatency": "Provider transcription latency from user speech.",
            "endpointingLatency": "Provider endpointing latency.",
            "turnLatency": "Provider whole-turn latency; not independently recorded acoustic EOS-to-audible time.",
            "Average": "Provider-reported call average; not a p95 across turns or calls.",
            "latency_units": "Raw provider values preserved. Inspected SDK explicitly specifies milliseconds only for fromTransportLatencyAverage/toTransportLatencyAverage; other latency units are not asserted or converted here.",
            "numUserInterrupted": "Count of times assistant interrupted user.",
            "numAssistantInterrupted": "Count of times user interrupted assistant.",
            "missing": "null or available=false means missing, not zero and not failure.",
            "browser_metrics": "Not imported: separately exported browser event gaps are event proxies, not acoustic measurements.",
        },
        "limitations": [
            "No aggregate pass/fail, production-grade certification or latency-target claim is inferred.",
            "In-progress or recently ended calls may lack final costs, artifacts and metrics; rerun after processing.",
            "Tool success counts include reads/proposals; only correlated local committed actions and appointment rows evidence a mock booking.",
            "Payloads, keys, transcripts, summaries, tool arguments, SDP, recordings, URLs and customer details are not saved.",
        ],
        "sources": ["https://docs.vapi.ai/api-reference/calls/list", "https://docs.vapi.ai/api-reference/calls/get", SDK_SOURCE],
    }


def fetch_calls(client, assistant_id, since, limit):
    params = {"assistantId": assistant_id, "limit": limit, "createdAtGe": since.isoformat()}
    with client.stream("GET", API_BASE + "/call", params=params) as response:
        response.raise_for_status()
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError("Response exceeds the bounded inspection size. Narrow the time window/limit.")
    payload = json.loads(body)
    if not isinstance(payload, list):
        raise ValueError("Unexpected call-list response shape; no report was written.")
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", help="Inclusive createdAt cutoff, ISO 8601 with timezone. Default: past 24 hours.")
    parser.add_argument("--limit", type=int, default=20, help="Bounded provider batch: 1-100, default 20.")
    parser.add_argument("--latest", action="store_true", help="Report only the newest call in the returned bounded batch.")
    parser.add_argument("--db", type=Path, help="Optional existing local scheduler DB; otherwise SERVICEVOICE_DB/project default.")
    args = parser.parse_args(argv)
    since = timestamp(args.since) if args.since else datetime.now(timezone.utc) - timedelta(hours=24)
    if since is None or not 1 <= args.limit <= 100:
        parser.error("Use --since with a timezone and --limit between 1 and 100.")

    from dotenv import load_dotenv
    import httpx
    load_dotenv(ROOT / ".env", override=False)
    key = os.getenv("VAPI_API_KEY", "").strip()
    assistant_id = os.getenv("VAPI_ASSISTANT_ID", "").strip()
    if not key or identifier(assistant_id) != assistant_id:
        print("Set VAPI_API_KEY and a canonical VAPI_ASSISTANT_ID locally; no request made.", file=sys.stderr)
        return 2
    configured = args.db or Path(os.getenv("SERVICEVOICE_DB", "../../work/runtime/servicevoice.sqlite3"))
    db_path = configured if configured.is_absolute() else ROOT / configured
    try:
        with httpx.Client(headers={"Authorization": "Bearer " + key}, timeout=20, follow_redirects=False) as client:
            calls = fetch_calls(client, assistant_id, since, args.limit)
        report = build_report(calls, assistant_id, since, args.limit, args.latest, db_path)
        del calls
        if report is None:
            print("No matching calls in this returned batch/window. Report unchanged; no evidence of success inferred.")
            return 0
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        temporary = REPORT.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
        temporary.replace(REPORT)
        print(f"Saved sanitized evidence for {len(report['calls'])} call(s): docs/live-call-evidence.json")
        if report["scope"]["possibly_truncated"]:
            print("Batch limit reached; this is a bounded sample. Narrow --since to inspect a smaller window.")
        return 0
    except httpx.HTTPStatusError as exc:
        print(f"Vapi returned HTTP {exc.response.status_code}; report unchanged. Response content withheld.", file=sys.stderr)
    except (httpx.HTTPError, ValueError, OSError, RecursionError):
        print("Inspection failed; report unchanged. Check local configuration, network, date window and DB access. Raw error withheld.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
