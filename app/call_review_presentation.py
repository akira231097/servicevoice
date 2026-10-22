"""Human-readable presentation of an automatic call review.

Pure functions only: no network, scheduler access, model request or file I/O.
Every input is already content-free (numbers, fixed codes, tool names and the
owned waiting-cue phrases). This module arranges that evidence so a person can
read what happened on the call, step by step, and why each check passed,
failed or stayed uncertain.

Joins between sources use explicit identifiers: the gateway's caller-turn
ordinal, request/generation IDs, operation IDs and TTS request IDs. Provider
artifact timings (caller/assistant speech segments, per-turn stage latencies)
are attached to a gateway turn by ordinal only when both sources count the same
number of caller turns, and the report states that basis. Nothing here claims
audible playback, pronunciation or what the caller actually heard.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
import re
from uuid import UUID, uuid5

VERSION = "call-review-presentation-v1"
TRACE_VERSION = "review-trace-v6"
MAX_TURNS = 200
PHRASE_TEXT = {
    "neutral_ack": "Mm-hmm.", "acknowledge": "I hear you.", "checking": "Let me check that.",
    "moment": "One moment.", "one_sec": "One sec.", "just_a_sec": "Just a sec.",
    "let_me_see": "Let me see.", "taking_a_look": "Let me take a look.", "working_on_it": "Working on it.",
}
TOOL_NAMES = frozenset({
    "get_dealership_info", "begin_service_request", "search_availability", "prepare_first_available_booking",
    "prepare_booking", "confirm_action", "dismiss_proposal", "get_appointment", "get_session_context",
    "get_operation", "prepare_reschedule", "prepare_cancellation", "request_handoff", "withdraw_handoff", "endCall",
})
CHECK_LABELS = {
    "hallucination_free": "Stuck to the dealership facts and tool results",
    "booking_accuracy": "Saved booking matches what the caller asked for",
    "confirmation_compliance": "Read the proposal back and got a yes before saving",
    "action_truthfulness": "Told the truth about what it did",
    "task_completion": "Finished the caller's goal or agreed a clear next step",
    "conversation_quality": "Stayed relevant, clear and responsive to corrections",
    "booking_integrity": "Saved records match the committed receipts (code check)",
    "confirmation_order": "Confirmation came on a later turn than the readback (code check)",
    "evidence_complete": "Enough stored evidence to judge the call",
    "reference_consistency": "Spoken booking reference matches the saved reference",
    "first_response_within_target": "Caller heard a response quickly (Vapi stage sum)",
    "answer_within_target": "The real answer started quickly on the server",
}
CHECK_GROUPS = (
    ("Conversation (Gemini reviewer, advisory)", ("hallucination_free", "action_truthfulness", "task_completion", "conversation_quality")),
    ("Booking (code checks win over the reviewer)", ("booking_integrity", "confirmation_order", "booking_accuracy", "confirmation_compliance", "reference_consistency")),
    ("Speed", ("first_response_within_target", "answer_within_target")),
    ("Evidence", ("evidence_complete",)),
)
LATENCY_CHECKS = ("first_response_within_target", "answer_within_target")
VERDICT_WORDS = {"pass": "PASS", "fail": "FAIL", "uncertain": "UNCERTAIN", "not_applicable": "N/A"}
CODE_LABELS = {
    "none": "no issue found",
    "no_saved_booking": "no booking was saved on this call",
    "saved_records_match_committed_receipts": "saved appointment matches its receipt",
    "later_user_turn_required_not_proof_of_assent": "no booking change was attempted",
    "text_and_tool_receipts_present": "transcript and tool receipts are present",
    "incomplete_provider_or_receipt_evidence": "some provider or receipt evidence is missing",
    "scheduler_evidence_unavailable": "the scheduler had no session for this call",
    "literal_reference_tokens_only_not_audio": "no booking reference was spoken",
    "unverified_literal_reference_requires_review": "a spoken reference does not match a saved one; listen to the recording",
    "returned_reference_missing_complete_literal_requires_review": "a reference was issued but not spoken in full; listen to the recording",
    "incomplete_evidence": "the reviewer did not have enough evidence",
    "incomplete_evidence_prevents_pass": "missing evidence blocks a pass",
    "failed_authoritative_booking_integrity": "the code check on saved records failed",
    "failed_authoritative_confirmation_order": "the code check on confirmation order failed",
    "missing_authoritative_booking_integrity": "saved-record evidence is incomplete",
    "missing_authoritative_confirmation_order": "confirmation-order evidence is incomplete",
    "reference_text_requires_review_not_proof_of_audio_error": "the spoken reference needs a human check",
    "unresolved_operations": "a scheduler operation was still unresolved",
    "committed_action_missing_receipt": "a committed action has no receipt",
    "proposal_receipt_mismatch": "the receipt differs from the proposal",
    "appointment_commit_mismatch": "appointments and commits do not match",
    "saved_booking_receipt_mismatch": "the saved booking differs from its receipt",
    "saved_customer_proposal_mismatch": "saved customer details differ from the proposal",
    "unsupported_price": "quoted an unsupported price", "unsupported_hours": "stated unsupported hours",
    "unsupported_location": "stated an unsupported location", "unsupported_availability": "claimed unsupported availability",
    "unsupported_policy": "stated an unsupported policy", "false_booking_claim": "claimed a booking that was not saved",
    "booking_detail_mismatch": "booking details differ from what the caller asked", "missing_readback": "no readback before saving",
    "missing_explicit_assent": "no explicit yes after the readback", "stale_confirmation": "confirmed an outdated proposal",
    "false_callback_claim": "claimed a callback or transfer that did not happen", "task_incomplete": "the caller's goal was not finished",
    "unnecessary_repetition": "repeated itself unnecessarily", "ignored_correction": "ignored a correction", "other": "other issue",
    "first_response_target_met": "average and slow-end first responses within target",
    "first_response_average_over_target": "average first response slower than target",
    "first_response_tail_over_target": "slowest responses beyond 1.5x target",
    "no_measured_response_turns": "Vapi reported no measured response turns",
    "server_answer_target_met": "answers started within target on the server",
    "server_answer_tail_over_target": "slow-end answers started after the target",
    "no_completed_server_generations": "no completed server generations to measure",
    "emitted": "cue played", "primary_ready": "answer ready within the head start",
    "policy_not_ready": "cue policy not ready", "context_unavailable": "call context unavailable",
    "turn_already_acknowledged": "already cued this turn", "cooldown": "too soon after the last cue",
    "call_budget": "cue budget for this call used up", "not_eligible": "turn not eligible for a cue",
    "sensitive_context": "sensitive context", "sensitive_utterance": "sensitive wording",
    "sensitive_prior_turn": "sensitive previous turn", "private_detail": "caller gave private details",
    "private_prior_turn": "previous turn had private details", "caller_still_formulating": "caller still formulating",
    "cached_language_unverified": "language not verified for the cached clips", "short_or_ambiguous": "too short or ambiguous",
    "urgent_or_safety": "urgent or safety wording", "classifier_timeout": "classifier timed out",
    "classifier_unavailable": "classifier unavailable", "low_confidence": "classifier not confident",
    "invalid_context": "scheduler stage not eligible", "classification_uncertain": "classifier unsure",
    "classification_deadline": "classifier deadline passed", "scheduler_changed": "scheduler state changed",
    "generation_cancelled_before_answer": "request cancelled before any answer; cue refunded",
    "generation_superseded_before_answer": "request replaced before any answer; cue refunded",
    "completed_then_cleanup_abort_label": "completed (cleanup abort label afterwards)",
    "aborted_without_observed_output": "aborted before any output", "ambiguous_or_incomplete": "ambiguous or incomplete",
    "completed": "completed", "cancelled": "cancelled", "superseded": "replaced by a newer request",
}
ENDED_LABELS = {
    "assistant-ended-call": "assistant hung up after the goodbye", "assistant-said-end-call-phrase": "assistant hung up after the goodbye",
    "customer-ended-call": "caller hung up", "exceeded-max-duration": "maximum call length reached",
    "silence-timed-out": "silence timeout", "voicemail": "voicemail detected",
}
ENDED_TAGS = {
    "assistant-ended-call": "assistant-hung-up", "assistant-said-end-call-phrase": "assistant-hung-up",
    "customer-ended-call": "caller-hung-up", "exceeded-max-duration": "max-duration",
}
CHANNEL_LABELS = {"inboundPhoneCall": "Inbound phone call", "outboundPhoneCall": "Outbound phone call",
                  "webCall": "Browser call", "vapi.websocketCall": "Synthetic websocket call"}
NOISE_EVENTS = frozenset({("webhook", "http_finished"), ("webhook", "call_bound")})
_UUID = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")
_REQUEST = re.compile(r"^(?:[a-f0-9]{32}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})$")
_OPERATION = re.compile(r"^op_[a-f0-9]{24}$")
_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,99}$")
_DOT_SEGMENT = re.compile(r"\d{8}T\d{12}Z[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}")
_SILENT_OUTCOMES = ("cancelled", "superseded", "superseded_after_release", "interrupted", "supersession_wait_expired")
_PROVIDER_MODEL_EVENTS = frozenset({"provider.model_started", "provider.model_first_token", "provider.model_completed", "provider.model_abort_label"})
_ANSWERED_OUTCOMES = ("completed", "tool_emitted", "narrated")


def number(value, low=0, high=86_400_000):
    """A finite number inside explicit bounds, else None. Never raises."""
    return float(value) if type(value) in (int, float) and math.isfinite(value) and low <= value <= high else None


def code(value):
    return value if type(value) is str and _CODE.fullmatch(value) else None


def label(value):
    """Short English for a fixed code; unknown or unsafe values stay generic."""
    safe = code(value)
    if safe is None:
        return "unknown"
    return CODE_LABELS.get(safe, safe.replace("_", " ").replace(".", " "))


def clock(seconds):
    value = number(seconds, 0, 86_400)
    if value is None:
        return "?"
    minutes, rest = divmod(value, 60)
    return f"{int(minutes)}:{rest:04.1f}"


def seconds_text(milliseconds):
    value = number(milliseconds)
    return "?" if value is None else f"{value / 1000:.2f} s"


def duration_text(seconds):
    value = number(seconds, 0, 86_400)
    if value is None:
        return "unknown length"
    minutes, rest = divmod(int(round(value)), 60)
    return f"{minutes} min {rest} s" if minutes else f"{rest} s"


def percentile(values, fraction=.95):
    rows = sorted(v for v in values if number(v) is not None)
    return round(rows[max(0, math.ceil(len(rows) * fraction) - 1)], 3) if rows else None


def average(values):
    rows = [v for v in values if number(v) is not None]
    return round(sum(rows) / len(rows), 3) if rows else None


def call_started(call):
    for key in ("startedAt", "createdAt"):
        value = call.get(key) if isinstance(call, dict) else None
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    return parsed.timestamp()
            except ValueError:
                pass
    return None


def call_duration_seconds(call):
    start = call_started(call)
    ended = call.get("endedAt") if isinstance(call, dict) else None
    if start is None or not isinstance(ended, str):
        return None
    try:
        end = datetime.fromisoformat(ended.replace("Z", "+00:00"))
    except ValueError:
        return None
    if end.tzinfo is None:
        return None
    value = number(end.timestamp() - start, 0, 86_400)
    return round(value, 3) if value is not None else None


def artifact_turns(call):
    """Caller and assistant speech segments from the provider artifact: timings only.

    One row per caller utterance, in artifact order. Text is never copied.
    Times are seconds from the provider's call start; durations come from the
    provider's own millisecond stamps.
    """
    artifact = call.get("artifact") if isinstance(call, dict) else None
    messages = (artifact or {}).get("messages") if isinstance(artifact, dict) else None
    if messages is None:
        messages = call.get("messages", []) if isinstance(call, dict) else []
    rows, current = [], None
    for item in messages if isinstance(messages, list) else []:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        spoken = isinstance(item.get("message"), str) and item["message"].strip()
        if role == "user" and spoken:
            if len(rows) >= MAX_TURNS:
                break
            current = {"ordinal": len(rows) + 1, "caller_start_s": number(item.get("secondsFromStart"), 0, 86_400),
                       "caller_duration_s": _segment_seconds(item), "asr_confidence": number(item.get("confidence"), 0, 1),
                       "assistant_start_s": None, "assistant_end_s": None, "assistant_segments": 0}
            rows.append(current)
        elif role in ("bot", "assistant") and spoken and current is not None:
            start = number(item.get("secondsFromStart"), 0, 86_400)
            length = _segment_seconds(item)
            if start is not None and current["assistant_start_s"] is None:
                current["assistant_start_s"] = start
            if start is not None and length is not None:
                current["assistant_end_s"] = max(current["assistant_end_s"] or 0, start + length)
            current["assistant_segments"] += 1
    return rows


def _segment_seconds(item):
    start = number(item.get("time"), 1_577_836_800_000, 4_102_444_800_000)
    end = number(item.get("endTime"), 1_577_836_800_000, 4_102_444_800_000)
    if start is not None and end is not None and end >= start:
        return round((end - start) / 1000, 3)
    length = number(item.get("duration"), 0, 3_600_000)
    return round(length / 1000, 3) if length is not None else None


def provider_turn_latencies(call):
    artifact = call.get("artifact") if isinstance(call, dict) else None
    metrics = (artifact or {}).get("performanceMetrics") if isinstance(artifact, dict) else None
    rows = metrics.get("turnLatencies") if isinstance(metrics, dict) else None
    result = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict):
            result.append({key: number(row.get(key)) for key in ("turnLatency", "modelLatency", "voiceLatency", "transcriberLatency", "endpointingLatency")})
    return result[:MAX_TURNS]


def interruptions(call):
    artifact = call.get("artifact") if isinstance(call, dict) else None
    metrics = (artifact or {}).get("performanceMetrics") if isinstance(artifact, dict) else None
    result = {}
    if not isinstance(metrics, dict):
        return result
    for source, target in (("numAssistantInterrupted", "caller_interrupted_assistant"), ("numUserInterrupted", "assistant_interrupted_caller")):
        value = metrics.get(source)
        if type(value) is int and 0 <= value <= 10_000:
            result[target] = value
    return result


def call_cost(call):
    """Provider-reported cost split; self-hosted Gemini and speech are billed elsewhere."""
    breakdown = call.get("costBreakdown") if isinstance(call, dict) else None
    result = {}
    if isinstance(breakdown, dict):
        for key, target in (("total", "total_usd"), ("stt", "speech_recognition_usd"), ("llm", "model_usd"), ("tts", "voice_usd"), ("vapi", "platform_usd"), ("transport", "transport_usd")):
            value = number(breakdown.get(key), 0, 10_000)
            if value is not None:
                result[target] = round(value, 4)
        for key, target in (("llmPromptTokens", "provider_prompt_tokens"), ("llmCompletionTokens", "provider_completion_tokens"), ("ttsCharacters", "voice_characters")):
            value = breakdown.get(key)
            if type(value) is int and 0 <= value <= 1_000_000_000:
                result[target] = value
    total = number(call.get("cost"), 0, 10_000) if isinstance(call, dict) else None
    if total is not None and "total_usd" not in result:
        result["total_usd"] = round(total, 4)
    return result


def generation_rows(timings):
    """Per-generation gateway facts from persisted timing rows, keyed by request id."""
    rows = {}
    for source in timings if isinstance(timings, list) else []:
        if not isinstance(source, dict):
            continue
        request = source.get("request_id")
        if not isinstance(request, str) or not _REQUEST.fullmatch(request) or request in rows:
            continue
        phases = source.get("milestones_ms") if isinstance(source.get("milestones_ms"), dict) else {}
        total = number(phases.get("total"))
        started = number(source.get("started_at_unix"), 1_577_836_800, 4_102_444_800)
        if total is None or started is None:
            continue
        usage = source.get("usage") if isinstance(source.get("usage"), dict) else {}
        route = source.get("route") if source.get("route") in ("gemini", "deterministic_receipt", "jev_static_faq") else "unknown"
        generation = source.get("generation_id")
        rows[request] = {
            "request_id": request, "generation_id": generation if isinstance(generation, str) and _UUID.fullmatch(generation) else None,
            "started_at_unix": started, "total_ms": total, "route": route, "outcome": code(source.get("outcome")) or "unknown",
            "first_text_ms": number(phases.get("first_text")), "tool_emitted_ms": number(phases.get("tool_emitted")),
            "upstream_headers_ms": number(phases.get("upstream_headers")), "preflight_ms": number(phases.get("preflight")),
            "input_tokens": usage.get("input_tokens") if type(usage.get("input_tokens")) is int else None,
            "output_tokens": usage.get("output_tokens") if type(usage.get("output_tokens")) is int else None,
            "total_tokens": usage.get("total_tokens") if type(usage.get("total_tokens")) is int else None,
            "sequence": source.get("generation_sequence") if type(source.get("generation_sequence")) is int else None,
            "cue": _cue(source.get("backchannel") if isinstance(source.get("backchannel"), dict) else None),
        }
    return rows


def _cue(cue):
    if cue is None:
        return None
    phrase = cue.get("phrase_id") if cue.get("phrase_id") in PHRASE_TEXT or cue.get("phrase_id") == "silence" else None
    emitted = number(cue.get("emitted_ms"))
    reason = code(cue.get("reason"))
    if phrase is None and emitted is None and reason is None:
        return None
    return {"phrase_id": phrase, "text": PHRASE_TEXT.get(phrase), "emitted_ms": emitted, "reason": reason,
            "follow_up": cue.get("follow_up") is True, "refunded": cue.get("refunded") is True,
            "refund_reason": code(cue.get("refund_reason")), "classifier_ms": number(cue.get("classification_ms")),
            "played": phrase is not None and phrase != "silence" and emitted is not None and cue.get("refunded") is not True}


def answer_ms(generation):
    """Server milliseconds until substantive content: first text or the emitted tool."""
    candidates = [v for v in (generation.get("first_text_ms"), generation.get("tool_emitted_ms")) if v is not None]
    return min(candidates) if candidates else None


def answer_milestones(timings):
    """Answer milestones of completed generations from raw timing rows, in ms."""
    values = []
    for row in timings if isinstance(timings, list) else []:
        if not isinstance(row, dict) or row.get("outcome") not in _ANSWERED_OUTCOMES:
            continue
        phases = row.get("milestones_ms") if isinstance(row.get("milestones_ms"), dict) else {}
        value = answer_ms({"first_text_ms": number(phases.get("first_text")), "tool_emitted_ms": number(phases.get("tool_emitted"))})
        if value is not None:
            values.append(value)
    return values


def diagnostic_events(diagnostics, call_key):
    events = diagnostics.get("events") if isinstance(diagnostics, dict) else None
    rows = []
    for row in events if isinstance(events, list) else []:
        if not isinstance(row, dict) or row.get("call_key") != call_key:
            continue
        attributes = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
        request = row.get("request_id")
        operation = row.get("operation_id")
        rows.append({"component": code(row.get("component")), "event": code(row.get("event")), "outcome": code(row.get("outcome")),
                     "turn_id": row.get("turn_id") if isinstance(row.get("turn_id"), str) else None,
                     "request_id": request if isinstance(request, str) and _REQUEST.fullmatch(request) else None,
                     "operation_id": operation if isinstance(operation, str) and _OPERATION.fullmatch(operation) else None,
                     "start_unix": number(row.get("start_unix"), 1_577_836_800, 4_102_444_800),
                     "duration_ms": number(row.get("duration_ms")), "attributes": attributes})
    return rows


def condense_diagnostics(diagnostics, call_key):
    """Keep every non-noise event; summarize webhook deliveries as counts.

    The export used to carry up to 2,000 raw events per call, most of them one
    row per provider webhook delivery. Those counts stay visible; only their
    individual rows leave the exported root payload.
    """
    if not isinstance(diagnostics, dict):
        return diagnostics
    events = diagnostics.get("events") if isinstance(diagnostics.get("events"), list) else []
    kept, webhook, tally = [], {"deliveries": 0, "by_event": {}, "by_outcome": {}, "http_status": {}, "by_event_type": {}}, {}
    for row in events:
        if isinstance(row, dict):
            key = f"{row.get('component')}.{row.get('event')}.{row.get('outcome')}"
            tally[key] = tally.get(key, 0) + 1
        if isinstance(row, dict) and row.get("call_key") == call_key and (row.get("component"), row.get("event")) in NOISE_EVENTS:
            webhook["deliveries"] += 1
            webhook["by_event"][row["event"]] = webhook["by_event"].get(row["event"], 0) + 1
            outcome = code(row.get("outcome")) or "unknown"
            webhook["by_outcome"][outcome] = webhook["by_outcome"].get(outcome, 0) + 1
            attributes = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
            status = attributes.get("http_status")
            if type(status) is int:
                webhook["http_status"][str(status)] = webhook["http_status"].get(str(status), 0) + 1
            kind = attributes.get("event_type")
            if isinstance(kind, str) and len(kind) <= 40:
                webhook["by_event_type"][kind] = webhook["by_event_type"].get(kind, 0) + 1
            continue
        kept.append(row)
    result = {**diagnostics, "events": kept, "webhook_deliveries": webhook, "event_counts": dict(sorted(tally.items())),
              "export_note": "Webhook delivery rows are summarized in webhook_deliveries; every other recorded event is listed. event_counts include the summarized rows."}
    coverage = result.get("coverage")
    if isinstance(coverage, dict):
        result["coverage"] = {**coverage, "exported_event_count": len(kept), "summarized_webhook_events": webhook["deliveries"]}
    return result


def build_timeline(call, package, timings, diagnostics, *, call_key, turn_ids, operation_ids=None, provider=None):
    """Join evidence per caller turn using explicit identifiers.

    ``turn_ids`` maps the gateway turn ordinal to its recorded turn identifier.
    ``operation_ids`` maps an evidence reference (``tool:N``) to the harness
    operation id. The result is content-free and is persisted with the review
    as ``review_trace`` so an export retry rebuilds identical children.
    """
    package = package if isinstance(package, dict) else {}
    evidence = package.get("evidence") if isinstance(package.get("evidence"), dict) else {}
    artifact = artifact_turns(call)
    latencies = provider_turn_latencies(call)
    events = diagnostic_events(diagnostics, call_key)
    generations = generation_rows(timings)
    ordinal_of_turn = {turn_id: ordinal for ordinal, turn_id in (turn_ids or {}).items()}
    request_turn, tool_events, receipt_events = {}, {}, {}
    for row in events:
        ordinal = ordinal_of_turn.get(row["turn_id"])
        if row["component"] == "gateway" and row["event"] == "request_finished" and row["request_id"] and ordinal:
            request_turn[row["request_id"]] = ordinal
        elif row["component"] == "tool_harness" and row["event"] == "execution_finished" and row["operation_id"]:
            tool_events[row["operation_id"]] = row
        elif row["component"] == "scheduler" and row["event"] == "receipt_observed" and row["operation_id"]:
            receipt_events[row["operation_id"]] = row
    tool_rows = []
    for ref, item in evidence.items():
        if not re.fullmatch(r"tool:\d+", ref) or not isinstance(item, dict):
            continue
        name = item.get("name") if item.get("name") in TOOL_NAMES else "unknown_tool"
        ordinal = item.get("user_count") if type(item.get("user_count")) is int and 1 <= item["user_count"] <= MAX_TURNS else None
        result = item.get("server_result") if isinstance(item.get("server_result"), dict) else None
        row = {"ref": ref, "name": name, "turn": ordinal, "issuance_verified": item.get("gateway_issuance_verified") is True,
               "ok": result.get("ok") if result is not None and type(result.get("ok")) is bool else None,
               "error_code": code(result.get("error")) if result is not None else None,
               "scope": item.get("evidence_scope") if item.get("evidence_scope") in ("business_operation", "platform_attempt", "unverified_platform_attempt") else None,
               "operation_id": None, "duration_ms": None, "receipt_present": None, "start_unix": None}
        operation = (operation_ids or {}).get(ref)
        if isinstance(operation, str) and _OPERATION.fullmatch(operation):
            row["operation_id"] = operation
            observed = tool_events.get(operation)
            if observed:
                row["duration_ms"] = observed["duration_ms"]
                row["start_unix"] = observed["start_unix"]
                row["receipt_present"] = (observed["attributes"].get("receipt_present") is True) or (operation in receipt_events)
                if row["turn"] is None:
                    row["turn"] = ordinal_of_turn.get(observed["turn_id"])
            elif operation in receipt_events:
                row["receipt_present"] = True
        tool_rows.append(row)
    gateway_turns = set(request_turn.values()) | {row["turn"] for row in tool_rows if row["turn"]}
    gateway_count = max(gateway_turns) if gateway_turns else 0
    artifact_count = len(artifact)
    artifact_matched = artifact_count > 0 and (gateway_count == 0 or artifact_count == gateway_count)
    latency_matched = bool(latencies) and len(latencies) == artifact_count
    total = max(gateway_count, artifact_count)
    generations_by_turn, unassigned = {}, []
    for request, generation in generations.items():
        ordinal = request_turn.get(request)
        if ordinal:
            generations_by_turn.setdefault(ordinal, []).append(generation)
        else:
            unassigned.append(generation)
    start_epoch = call_started(call)
    turns = []
    for ordinal in range(1, min(total, MAX_TURNS) + 1):
        rows = sorted(generations_by_turn.get(ordinal, []), key=lambda g: (g["started_at_unix"], g["request_id"]))
        answered = [g for g in rows if g["outcome"] in _ANSWERED_OUTCOMES and answer_ms(g) is not None]
        final = answered[-1] if answered else (rows[-1] if rows else None)
        cues = [g["cue"] for g in rows if g.get("cue")]
        played = [c for c in cues if c["played"]]
        tools = [t for t in tool_rows if t["turn"] == ordinal]
        turn = {"turn": ordinal, "generations": rows, "gateway_generations": len(rows),
                "speculative_cancelled": sum(1 for g in rows if g["outcome"] in _SILENT_OUTCOMES and answer_ms(g) is None),
                "route": final["route"] if final else None, "outcome": final["outcome"] if final else None,
                "answer_after_ms": answer_ms(final) if final else None, "model_total_ms": final["total_ms"] if final else None,
                "answered_with": None, "tokens": sum(g["total_tokens"] for g in rows if g["total_tokens"]) or None,
                "cue": played[0] if played else None, "cue_follow_up": any(c["follow_up"] for c in played),
                "cue_suppressed_reason": None if played else next((c["reason"] for c in cues if c["reason"] not in (None, "emitted")), None),
                "tools": tools, "caller_start_s": None, "caller_duration_s": None, "asr_confidence": None,
                "assistant_start_s": None, "assistant_duration_s": None, "first_response_ms": None, "stage_ms": None,
                "start_unix": rows[0]["started_at_unix"] if rows else None, "end_unix": None}
        if final is not None:
            if final["tool_emitted_ms"] is not None and (final["first_text_ms"] is None or final["tool_emitted_ms"] <= final["first_text_ms"]):
                names = [t["name"] for t in tools]
                turn["answered_with"] = "tool call " + ", ".join(names) if names else "tool call"
            elif final["route"] == "deterministic_receipt":
                turn["answered_with"] = "verified receipt readback"
            elif final["route"] == "jev_static_faq":
                turn["answered_with"] = "static FAQ answer"
            elif final["first_text_ms"] is not None:
                turn["answered_with"] = "spoken answer"
            else:
                turn["answered_with"] = label(final["outcome"])
        ends = [g["started_at_unix"] + g["total_ms"] / 1000 for g in rows]
        ends += [t["start_unix"] + (t["duration_ms"] or 0) / 1000 for t in tools if t["start_unix"] is not None]
        starts = [t["start_unix"] for t in tools if t["start_unix"] is not None] + ([turn["start_unix"]] if turn["start_unix"] is not None else [])
        turn["start_unix"] = min(starts) if starts else None
        if artifact_matched and ordinal <= artifact_count:
            row = artifact[ordinal - 1]
            spoke = row["assistant_start_s"] is not None and row["assistant_end_s"] is not None and row["assistant_end_s"] >= row["assistant_start_s"]
            turn.update(caller_start_s=row["caller_start_s"], caller_duration_s=row["caller_duration_s"], asr_confidence=row["asr_confidence"],
                        assistant_start_s=row["assistant_start_s"],
                        assistant_duration_s=round(row["assistant_end_s"] - row["assistant_start_s"], 3) if spoke else None)
            if start_epoch is not None and row["caller_start_s"] is not None:
                caller_at = start_epoch + row["caller_start_s"]
                turn["start_unix"] = min(turn["start_unix"], caller_at) if turn["start_unix"] is not None else caller_at
            if start_epoch is not None and row["assistant_end_s"] is not None:
                ends.append(start_epoch + row["assistant_end_s"])
            if latency_matched:
                stage = latencies[ordinal - 1]
                turn["first_response_ms"] = stage["turnLatency"]
                turn["stage_ms"] = {"speech_recognition": stage["transcriberLatency"], "end_of_speech": stage["endpointingLatency"],
                                    "model_first_token": stage["modelLatency"], "voice": stage["voiceLatency"]}
        turn["end_unix"] = max(ends) if ends else None
        turn["line"] = describe_turn(turn)
        turns.append(turn)
    return {"schema_version": VERSION, "start_epoch": start_epoch, "turns": turns, "tools": tool_rows,
            "unassigned_generations": sorted(unassigned, key=lambda g: (g["started_at_unix"], g["request_id"])),
            "tts_requests": tts_requests(events), "provider": provider_summary(provider, start_epoch),
            "interruptions": interruptions(call),
            "join_basis": {"gateway_turn_ordinal": "caller message ordinal as counted by the gateway (explicit turn identifiers)",
                           "artifact_rows_attached_by_ordinal": artifact_matched,
                           "artifact_caller_turns": artifact_count, "gateway_caller_turns": gateway_count,
                           "vapi_turn_latencies_attached_by_ordinal": latency_matched and artifact_matched,
                           "tts_requests_joined_to_turns": False, "provider_model_spans_joined_to_generations": False,
                           "note": "Provider rows attach to a gateway turn only when both count the same caller turns; TTS and provider model spans are listed on their own."}}


def describe_turn(turn):
    parts = [f"Turn {turn['turn']}"]
    if turn["caller_start_s"] is not None:
        spoke = f"caller spoke at {clock(turn['caller_start_s'])}"
        if turn["caller_duration_s"] is not None:
            spoke += f" for {turn['caller_duration_s']:.1f} s"
        if turn["asr_confidence"] is not None and turn["asr_confidence"] < .8:
            spoke += f" (low recognition confidence {turn['asr_confidence']:.2f})"
        parts.append(spoke)
    cue = turn["cue"]
    if cue:
        parts.append(f"cue \"{cue['text']}\" at {seconds_text(cue['emitted_ms'])}" + (" (follow-up)" if cue["follow_up"] else ""))
    elif turn["cue_suppressed_reason"]:
        parts.append("no cue: " + label(turn["cue_suppressed_reason"]))
    if turn["answered_with"]:
        when = seconds_text(turn["answer_after_ms"]) if turn["answer_after_ms"] is not None else "?"
        parts.append(f"{turn['answered_with']} after {when} on the server")
    elif turn["gateway_generations"] == 0:
        parts.append("no gateway request recorded for this turn")
    for tool in turn["tools"]:
        status = "ok" if tool["ok"] else ("pending" if tool["error_code"] == "operation_pending" else (label(tool["error_code"]) if tool["error_code"] else "?"))
        extra = f" {tool['duration_ms'] / 1000:.2f} s" if tool["duration_ms"] is not None else ""
        parts.append(f"tool {tool['name']} {status}{extra}")
    if turn["speculative_cancelled"]:
        parts.append(f"{turn['speculative_cancelled']} speculative request(s) cancelled while the caller kept talking")
    if turn["first_response_ms"] is not None:
        parts.append(f"first audio {seconds_text(turn['first_response_ms'])} after the caller stopped (Vapi)")
    if turn["assistant_duration_s"] is not None:
        parts.append(f"assistant spoke for {turn['assistant_duration_s']:.1f} s")
    return " · ".join(parts)


def tts_requests(events):
    """Speech-synthesis requests observed by the separate speech ledger."""
    rows = {}
    for row in events:
        if row["component"] != "tts" or not row["request_id"] or not row["event"]:
            continue
        item = rows.setdefault(row["request_id"], {"request_id": row["request_id"], "start_unix": None, "first_audio_ms": None,
                                                     "elapsed_ms": None, "outcome": None, "cache_hit": None, "characters": None, "audio_ms": None, "source": None})
        attributes = row["attributes"]
        if row["event"] in ("tts.request_started", "tts.started", "tts.request") and row["start_unix"] is not None:
            item["start_unix"] = row["start_unix"]
        if row["event"] in ("tts.first_audio_ready", "tts.first_audio"):
            item["first_audio_ms"] = number(attributes.get("first_audio_ms")) if number(attributes.get("first_audio_ms")) is not None else row["duration_ms"]
        if row["event"] in ("tts.completed", "tts.failed", "tts.cancelled"):
            item["outcome"] = {"tts.completed": "completed", "tts.failed": "failed", "tts.cancelled": "cancelled"}[row["event"]]
            item["elapsed_ms"] = row["duration_ms"]
            if item["start_unix"] is None and row["start_unix"] is not None:
                item["start_unix"] = row["start_unix"]
            for key, target in (("input_characters", "characters"), ("audio_duration_ms", "audio_ms")):
                value = attributes.get(key)
                if type(value) in (int, float) and number(value) is not None:
                    item[target] = value
            if type(attributes.get("cache_hit")) is bool:
                item["cache_hit"] = attributes["cache_hit"]
            if attributes.get("synthesis_source") in ("pregenerated_cache", "live_google"):
                item["source"] = attributes["synthesis_source"]
        if row["event"] == "tts.cache_hit":
            item["cache_hit"] = True
    return sorted((r for r in rows.values() if r["start_unix"] is not None), key=lambda r: (r["start_unix"], r["request_id"]))


def provider_summary(provider, start_epoch):
    """Compact view of Vapi's own event log: counts, a short timeline and model spans."""
    projection = provider.get("projection") if isinstance(provider, dict) else None
    status = provider.get("status") if isinstance(provider, dict) and isinstance(provider.get("status"), str) else "unavailable"
    if not isinstance(projection, dict):
        return {"status": status, "event_counts": {}, "timeline": [], "model_spans": [], "interruption_flags": None}
    timeline, groups = [], {}
    for row in projection.get("events", []) if isinstance(projection.get("events"), list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("event"), str):
            continue
        stamp = row.get("observed_unix_ms")
        span = row.get("provider_model_span_id")
        if isinstance(span, str) and _UUID.fullmatch(span) and row["event"] in _PROVIDER_MODEL_EVENTS:
            groups.setdefault(span, []).append(row)
        if type(stamp) is not int or start_epoch is None:
            continue
        delta = stamp / 1000 - start_epoch
        timeline.append({"at": clock(max(delta, 0.0)) if delta > -1 else "?", "event": row["event"].replace("provider.", "").replace("_", " ")})
    # Model intervals need one recorded start and a completion or abort with
    # sane chronology, exactly as the forensic projector required; a cleanup
    # abort after completion stays a completed request.
    spans = []
    for span, group in groups.items():
        starts = [r for r in group if r["event"] == "provider.model_started"]
        complete = [r for r in group if r["event"] == "provider.model_completed"]
        abort = [r for r in group if r["event"] == "provider.model_abort_label"]
        first = [r for r in group if r["event"] == "provider.model_first_token"]
        if len(starts) != 1 or len(complete) > 1 or len(abort) > 1 or not (complete or abort):
            continue
        start = starts[0].get("observed_unix_ms")
        end = (complete[0] if complete else abort[0]).get("observed_unix_ms")
        if type(start) is not int or type(end) is not int or end < start:
            continue
        token = first[0].get("observed_unix_ms") if len(first) == 1 else None
        classification = ("completed_then_cleanup_abort_label" if complete and abort else "completed" if complete
                          else "aborted_without_observed_output" if not first else "ambiguous_or_incomplete")
        spans.append({"span_id": span, "started_unix_ms": start, "first_token_ms": (token - start) if type(token) is int and token >= start else None,
                      "duration_ms": float(end - start), "classification": classification})
    summary = projection.get("summary") if isinstance(projection.get("summary"), dict) else {}
    counts = summary.get("event_counts") if isinstance(summary.get("event_counts"), dict) else {}
    flags = projection.get("provider_flag_evidence") if isinstance(projection.get("provider_flag_evidence"), dict) else None
    return {"status": status, "event_counts": {k: v for k, v in counts.items() if isinstance(k, str) and type(v) is int},
            "timeline": timeline[:400], "model_spans": sorted(spans, key=lambda s: (s["started_unix_ms"], s["span_id"]))[:200],
            "interruption_flags": {k: v for k, v in flags.items() if k in ("pipeline_was_interruption_values", "request_assistant_turn_interrupted_values")} if flags else None}


def cue_summary(timeline):
    turns = timeline.get("turns", [])
    played = [t["cue"] for t in turns if t.get("cue")]
    all_generations = [g for t in turns for g in t.get("generations", [])] + list(timeline.get("unassigned_generations", []))
    refunded = sum(1 for g in all_generations if g.get("cue") and g["cue"]["refunded"])
    reasons, phrases = {}, {}
    for turn in turns:
        if not turn.get("cue") and turn.get("cue_suppressed_reason"):
            key = label(turn["cue_suppressed_reason"])
            reasons[key] = reasons.get(key, 0) + 1
    for cue in played:
        phrases[cue["text"]] = phrases.get(cue["text"], 0) + 1
    return {"turns_with_a_cue": len(played), "caller_turns": len(turns),
            "coverage": round(len(played) / len(turns), 2) if turns else None,
            "follow_up_cues": sum(1 for t in turns if t.get("cue_follow_up")),
            "cue_start_p95_ms": percentile([c["emitted_ms"] for c in played]),
            "cue_start_avg_ms": average([c["emitted_ms"] for c in played]),
            "phrases_used": phrases, "turns_without_a_cue_by_reason": reasons, "refunded_speculative_cues": refunded,
            "meaning": "A cue is a short prerecorded phrase such as 'One moment.' played while the answer is prepared. Silence is deliberate on private-detail, goodbye and plain-approval turns."}


def latency_summary(latency, timeline, targets):
    turns = timeline.get("turns", [])
    answers = [t["answer_after_ms"] for t in turns if t.get("answer_after_ms") is not None]
    if not answers:
        answers = [answer_ms(g) for g in timeline.get("unassigned_generations", []) if g.get("outcome") in _ANSWERED_OUTCOMES and answer_ms(g) is not None]
    # The measured section always agrees with the checks: fall back to the raw-row metrics.
    answer_avg = average(answers) if answers else latency.get("server_answer_avg_ms")
    answer_p95 = percentile(answers) if answers else latency.get("server_answer_p95_ms")
    first = latency.get("turn_avg_ms")
    lines = []
    if first is not None:
        lines.append(f"Callers heard a response {seconds_text(first)} on average after they stopped talking (Vapi stage sum: speech recognition + end-of-speech detection + model first token + voice).")
    if latency.get("turn_p95_ms") is not None:
        lines.append(f"The slowest responses took about {seconds_text(latency['turn_p95_ms'])} (p95).")
    if answer_p95 is not None:
        lines.append(f"On the server, the real answer or tool call started {seconds_text(answer_avg)} after the request on average, {seconds_text(answer_p95)} at p95; speech synthesis and transport add to what the caller hears.")
    if latency.get("server_first_ack_p95_ms") is not None:
        lines.append(f"Waiting cues started within {seconds_text(latency['server_first_ack_p95_ms'])} (p95) on the server.")
    stages = {k: latency.get(k) for k in ("transcriber_avg_ms", "endpointing_avg_ms", "model_avg_ms", "voice_avg_ms") if latency.get(k) is not None}
    if stages:
        order = [("transcriber_avg_ms", "speech recognition"), ("endpointing_avg_ms", "end-of-speech detection"), ("model_avg_ms", "model first token"), ("voice_avg_ms", "voice")]
        lines.append("Where the first response goes (Vapi averages): " + ", ".join(f"{name} {seconds_text(stages[key])}" for key, name in order if key in stages) + ".")
    if not lines:
        lines.append("No latency measurements were available for this call.")
    return {"summary": lines, "first_response_avg_ms": first, "first_response_p95_ms": latency.get("turn_p95_ms"),
            "measured_turns": latency.get("measured_turns"), "answer_avg_ms": answer_avg, "answer_p95_ms": answer_p95,
            "cue_start_p95_ms": latency.get("server_first_ack_p95_ms"), "vapi_stage_averages_ms": stages,
            "targets_ms": targets, "interruptions": timeline.get("interruptions") or {},
            "definitions": {"first_response": "Vapi-reported time from the caller's end of speech to the start of assistant audio; with waiting cues this is usually the cue.",
                            "answer": "Server time from the gateway request to the first substantive text or tool call; excludes speech recognition, synthesis and transport.",
                            "cue_start": "Server time from the gateway request to the cue audio being yielded; not phone playback."}}


def scorecard(checks, evidence_index):
    groups = []
    for title, keys in CHECK_GROUPS:
        rows = []
        for key in keys:
            item = checks.get(key)
            if not isinstance(item, dict):
                continue
            verdict = item.get("verdict") if item.get("verdict") in VERDICT_WORDS else "uncertain"
            reason = item.get("issue_code") if item.get("issue_code") not in (None, "") else item.get("reason_code")
            refs = []
            for ref in item.get("evidence_refs", []) if isinstance(item.get("evidence_refs"), list) else []:
                if isinstance(ref, str) and re.fullmatch(r"(?:turn|tool):\d+|state|facts", ref):
                    where = evidence_index.get(ref) if isinstance(evidence_index, dict) else None
                    at = where.get("at") if isinstance(where, dict) else None
                    refs.append(f"{ref} ({at})" if at else ref)
            rows.append({"check": key, "question": CHECK_LABELS.get(key, key), "result": VERDICT_WORDS[verdict],
                         "why": label(reason) if reason else "", "evidence": refs[:8]})
        groups.append({"group": title, "checks": rows})
    return groups


def headline(call_summary, booking, duration_s, needs_review, slow_call, ended_reason):
    channel = CHANNEL_LABELS.get(call_summary.get("channel"), "Call")
    turns = call_summary.get("caller_utterances")
    count = booking.get("appointment_count")
    if booking.get("scheduler_evidence_available") is False:
        booked = "scheduler had no session for this call"
    elif count:
        booked = f"{count} appointment{'s' if count != 1 else ''} saved"
    else:
        booked = "no booking saved"
    verdict = "needs a human look" if needs_review else "checks clear"
    if slow_call:
        verdict += ", slow"
    return f"{channel} · {duration_text(duration_s)} · {turns if turns is not None else '?'} caller turns · {booked} · ended: {ended_label(ended_reason)} · {verdict}"


def ended_label(ended_reason):
    if not isinstance(ended_reason, str):
        return "unknown"
    return ENDED_LABELS.get(ended_reason, ended_reason.replace("-", " "))


def summary_lines(checks, latency, timeline, booking, cues, ended_reason, interruptions_count):
    lines = []
    failed = [k for k, v in checks.items() if isinstance(v, dict) and v.get("verdict") == "fail"]
    unsure = [k for k, v in checks.items() if isinstance(v, dict) and v.get("verdict") == "uncertain"]
    if failed:
        lines.append("Failed: " + "; ".join(f"{CHECK_LABELS.get(k, k)} ({label(checks[k].get('issue_code') or checks[k].get('reason_code'))})" for k in failed) + ".")
    if unsure:
        lines.append("Needs a human look: " + "; ".join(f"{CHECK_LABELS.get(k, k)} ({label(checks[k].get('issue_code') or checks[k].get('reason_code'))})" for k in unsure) + ".")
    if not failed and not unsure:
        lines.append("Every automatic check passed or did not apply.")
    counted = {}
    for tool in timeline.get("tools", []):
        counted[tool["name"]] = counted.get(tool["name"], 0) + 1
    if counted:
        lines.append("Tools used: " + ", ".join(f"{name} x{n}" if n > 1 else name for name, n in counted.items()) + ".")
    if booking.get("appointment_count"):
        lines.append(f"{booking['appointment_count']} appointment(s) saved with {booking.get('committed_actions') or 0} committed action(s).")
    if cues.get("caller_turns"):
        lines.append(f"Waiting cues played on {cues['turns_with_a_cue']} of {cues['caller_turns']} caller turns" + (f", {cues['follow_up_cues']} follow-up(s)" if cues.get("follow_up_cues") else "") + ".")
    if latency.get("turn_avg_ms") is not None:
        lines.append(f"Average first response {seconds_text(latency['turn_avg_ms'])}, slowest {seconds_text(latency.get('turn_p95_ms'))}.")
    if interruptions_count:
        lines.append(f"The caller interrupted the assistant {interruptions_count} time(s) (Vapi count).")
    lines.append("Ended: " + ended_label(ended_reason) + ".")
    return lines


def what_to_check(checks, timeline, evidence_index):
    """Concrete pointers for a human reviewer, derived from the verdicts."""
    items = []
    for key, item in checks.items():
        if not isinstance(item, dict) or item.get("verdict") not in ("fail", "uncertain"):
            continue
        if key in LATENCY_CHECKS and item.get("verdict") == "uncertain":
            continue  # An unmeasured speed check is not something a listener can resolve.
        refs = [r for r in item.get("evidence_refs", []) if isinstance(r, str) and re.fullmatch(r"(?:turn|tool):\d+", r)]
        where = ", ".join(f"{r} at {evidence_index[r]['at']}" if isinstance(evidence_index.get(r), dict) and evidence_index[r].get("at") else r for r in refs[:4])
        items.append(f"{CHECK_LABELS.get(key, key)}: {label(item.get('issue_code') or item.get('reason_code'))}" + (f" (look at {where})" if where else ""))
    low = [t for t in timeline.get("turns", []) if t.get("asr_confidence") is not None and t["asr_confidence"] < .8]
    for turn in low[:5]:
        items.append(f"Turn {turn['turn']} at {clock(turn['caller_start_s'])}: speech recognition confidence {turn['asr_confidence']:.2f}; the caller may have been misheard.")
    slow = [t for t in timeline.get("turns", []) if t.get("first_response_ms") is not None and t["first_response_ms"] > 4000]
    for turn in slow[:5]:
        items.append(f"Turn {turn['turn']}: first audio only after {seconds_text(turn['first_response_ms'])}; listen for an awkward pause.")
    if not items:
        items.append("Nothing flagged. Spot-check one recording for naturalness; the automatic review never hears audio.")
    return items


def build_overview(*, checks, latency, timeline, booking, call_summary, evidence_index, duration_s, needs_review, slow_call, ended_reason, targets, cost, review_process):
    cues = cue_summary(timeline)
    interrupted = (timeline.get("interruptions") or {}).get("caller_interrupted_assistant")
    start_epoch = timeline.get("start_epoch")
    tts = timeline.get("tts_requests", [])
    return {
        "1 Summary": {"headline": headline(call_summary, booking, duration_s, needs_review, slow_call, ended_reason),
                      "lines": summary_lines(checks, latency, timeline, booking, cues, ended_reason, interrupted),
                      "what_to_check": what_to_check(checks, timeline, evidence_index),
                      "review_basis": "Stored transcript, tool receipts, scheduler records and server timings. No audio was heard by this review.",
                      "semantic_review": review_process.get("semantic_status") if isinstance(review_process, dict) else None},
        "2 Scorecard": {"groups": scorecard(checks, evidence_index),
                        "legend": "PASS / FAIL come from evidence; UNCERTAIN means a human should look; N/A means the situation did not occur. Code checks on saved records override the advisory reviewer."},
        "3 Turn-by-turn timeline": {"turns": [t["line"] for t in timeline.get("turns", [])],
                                    "details": [_turn_details(t) for t in timeline.get("turns", [])],
                                    "generations_without_a_turn": [{"started_at": clock(g["started_at_unix"] - start_epoch) if start_epoch is not None else None, "route": g["route"], "outcome": g["outcome"],
                                                                    "total_ms": g["total_ms"], "answer_after_ms": answer_ms(g), "cue": g["cue"]["text"] if g.get("cue") and g["cue"]["played"] else None}
                                                                   for g in timeline.get("unassigned_generations", [])][:50],
                                    "speech_synthesis": {"requests": len(tts), "cached_cue_clips": sum(1 for r in tts if r.get("cache_hit")),
                                                         "first_audio_p95_ms": percentile([r["first_audio_ms"] for r in tts if r.get("first_audio_ms") is not None]),
                                                         "failed": sum(1 for r in tts if r.get("outcome") == "failed")},
                                    "join_basis": timeline.get("join_basis")},
        "4 Latency": latency_summary(latency, timeline, targets),
        "5 Waiting cues": cues,
        "6 Booking and actions": {**{k: booking.get(k) for k in ("scheduler_evidence_available", "appointment_count", "committed_actions", "unresolved_operations")},
                                  "tools": [{"turn": t["turn"], "name": t["name"], "ok": t["ok"], "error": label(t["error_code"]) if t["error_code"] and t["error_code"] != "none" else None,
                                             "duration_ms": t["duration_ms"], "receipt_present": t["receipt_present"], "issuance_verified": t["issuance_verified"]}
                                            for t in timeline.get("tools", [])],
                                  "provider_cost": cost},
        "7 How to read this review": {
            "steps": ["Read 1 Summary, then every FAIL or UNCERTAIN row in 2 Scorecard.",
                      "Follow the evidence references (turn:N at m:ss) into 3 Turn-by-turn timeline and, if a recording exists, listen at that time.",
                      "Expand the trace children: one 'turn NN' row per caller turn with its cue, Gemini request and tool calls; speech synthesis and Vapi's own events are listed separately.",
                      "Feedback rows: checks score 1/0 (blank = uncertain or n/a); needs_review and slow_call are flags (1 = look); metric_* rows are measurements, not scores."],
            "what_this_cannot_prove": ["What the caller actually heard, pronunciation or naturalness.",
                                       "That a cue or answer reached the phone: server times are not playback.",
                                       "Consent beyond the stored text and turn order."]},
    }


def _turn_details(turn):
    return {"turn": turn["turn"], "caller_spoke_at": clock(turn["caller_start_s"]) if turn.get("caller_start_s") is not None else None,
            "caller_spoke_s": turn.get("caller_duration_s"), "asr_confidence": turn.get("asr_confidence"),
            "cue": {"text": turn["cue"]["text"], "at_ms": turn["cue"]["emitted_ms"], "follow_up": turn["cue"]["follow_up"]} if turn.get("cue") else None,
            "no_cue_because": label(turn["cue_suppressed_reason"]) if turn.get("cue_suppressed_reason") and not turn.get("cue") else None,
            "answered_with": turn.get("answered_with"), "answer_after_ms": turn.get("answer_after_ms"), "model_total_ms": turn.get("model_total_ms"),
            "tools": [f"{t['name']} {'ok' if t['ok'] else (t['error_code'] or '?')}" for t in turn.get("tools", [])],
            "speculative_requests_cancelled": turn.get("speculative_cancelled"), "tokens": turn.get("tokens"),
            "first_response_ms": turn.get("first_response_ms"), "stage_ms": turn.get("stage_ms"),
            "assistant_spoke_at": clock(turn["assistant_start_s"]) if turn.get("assistant_start_s") is not None else None,
            "assistant_spoke_s": turn.get("assistant_duration_s")}


def markdown_report(overview, *, call_id=None, started=None):
    """A document a person can read top to bottom; also rendered by evals.call_report."""
    summary = overview["1 Summary"]
    lines = [f"# Call review — {started or 'time unknown'}", "", f"**{summary['headline']}**", ""]
    lines += [f"- {line}" for line in summary["lines"]]
    lines += ["", "## What to check", ""] + [f"- {item}" for item in summary["what_to_check"]]
    lines += ["", "## Scorecard", "", "| Check | Result | Why | Evidence |", "|---|---|---|---|"]
    for group in overview["2 Scorecard"]["groups"]:
        lines.append(f"| **{group['group']}** | | | |")
        for row in group["checks"]:
            lines.append(f"| {row['question']} | {row['result']} | {row['why']} | {', '.join(row['evidence'])} |")
    lines += ["", "## Turn by turn", ""]
    turns = overview["3 Turn-by-turn timeline"]["turns"]
    lines += [f"{n + 1}. {line}" for n, line in enumerate(turns)] or ["No caller turns were observed."]
    synthesis = overview["3 Turn-by-turn timeline"]["speech_synthesis"]
    if synthesis["requests"]:
        lines += ["", f"Speech synthesis: {synthesis['requests']} requests, {synthesis['cached_cue_clips']} served from the cue cache, first audio p95 {seconds_text(synthesis['first_audio_p95_ms'])}, {synthesis['failed']} failed."]
    lines += ["", "## Latency", ""] + [f"- {line}" for line in overview["4 Latency"]["summary"]]
    cues = overview["5 Waiting cues"]
    lines += ["", "## Waiting cues", "", f"- Played on {cues['turns_with_a_cue']} of {cues['caller_turns']} caller turns" + (f" (cue start p95 {seconds_text(cues['cue_start_p95_ms'])})" if cues.get("cue_start_p95_ms") is not None else "") + "."]
    if cues["phrases_used"]:
        lines.append("- Phrases: " + ", ".join(f"\"{text}\" x{n}" for text, n in cues["phrases_used"].items()) + ".")
    if cues["turns_without_a_cue_by_reason"]:
        lines.append("- Silent turns: " + ", ".join(f"{reason} x{n}" for reason, n in cues["turns_without_a_cue_by_reason"].items()) + ".")
    booking = overview["6 Booking and actions"]
    lines += ["", "## Booking and actions", "", f"- Appointments saved: {booking.get('appointment_count')} · committed actions: {booking.get('committed_actions')} · unresolved operations: {booking.get('unresolved_operations')}."]
    for tool in booking["tools"]:
        lines.append(f"- Turn {tool['turn'] or '?'}: {tool['name']} — {'ok' if tool['ok'] else (tool['error'] or 'unknown')}" + (f", {tool['duration_ms'] / 1000:.2f} s" if tool.get("duration_ms") is not None else "") + (", receipt saved" if tool.get("receipt_present") else ""))
    cost = booking.get("provider_cost") or {}
    if cost.get("total_usd") is not None:
        lines.append(f"- Provider cost: ${cost['total_usd']:.4f} (platform ${cost.get('platform_usd', 0):.4f}, speech recognition ${cost.get('speech_recognition_usd', 0):.4f}).")
    lines += ["", "## Limits of this review", ""] + [f"- {item}" for item in overview["7 How to read this review"]["what_this_cannot_prove"]]
    if call_id:
        lines += ["", f"Call `{call_id}`."]
    return "\n".join(lines) + "\n"


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="microseconds")


def _segment(start, run_id):
    return datetime.fromtimestamp(start, timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + run_id


def build_review_children(trace, *, root_run_id, root_dotted_order, project_id, max_children=600):
    """Readable trace children under the call review, built from explicit identities.

    One ``turn NN`` row per caller turn, each with its cue, Gemini requests and
    tool calls; then one group for speech synthesis and one for Vapi's own
    model spans. Run ids are deterministic (UUID5 under the root), so a retried
    export posts identical payloads from the persisted ``review_trace``.
    Timestamps are recorded server or provider times; a group's interval is the
    envelope of its rows, not a measured stage.
    """
    root = root_run_id if isinstance(root_run_id, str) and _UUID.fullmatch(root_run_id) else None
    project = project_id if isinstance(project_id, str) and _UUID.fullmatch(project_id) else None
    segments = root_dotted_order.split(".") if isinstance(root_dotted_order, str) else []
    if root is None or project is None or not segments or any(not _DOT_SEGMENT.fullmatch(s) for s in segments) or not segments[-1].endswith(root):
        raise ValueError("invalid_review_child_destination")
    trace = trace if isinstance(trace, dict) else {}
    namespace = UUID(root)
    runs, counts = [], {"turns": 0, "generations": 0, "cues": 0, "tools": 0, "tts": 0, "provider_model_spans": 0, "omitted": 0}

    def child(kind, identity, name, start, end, inputs, outputs, parent=None, run_type="chain", tags=(), metadata=None):
        start = number(start, 1_577_836_800, 4_102_444_800)
        end = number(end, 1_577_836_800, 4_102_444_800)
        if start is None:
            counts["omitted"] += 1
            return None
        end = max(end if end is not None else start, start)
        run_id = str(uuid5(namespace, TRACE_VERSION + ":" + kind + ":" + str(identity)))
        parent_run = parent or {"id": root, "dotted_order": root_dotted_order}
        body = {"id": run_id, "trace_id": root, "parent_run_id": parent_run["id"], "session_id": project, "name": name[:200], "run_type": run_type, "error": None,
                "start_time": _iso(start), "end_time": _iso(end), "dotted_order": parent_run["dotted_order"] + "." + _segment(start, run_id),
                "inputs": inputs, "outputs": outputs, "tags": [TRACE_VERSION, *tags],
                "extra": {"metadata": {"record_kind": kind, "trace_schema_version": TRACE_VERSION, "capture_policy": "content-free-v1",
                                       "timing_basis": "recorded_server_or_provider_times_not_phone_playback", **(metadata or {})}}}
        runs.append(body)
        return body

    for turn in trace.get("turns", []) if isinstance(trace.get("turns"), list) else []:
        if not isinstance(turn, dict) or type(turn.get("turn")) is not int or turn.get("start_unix") is None:
            counts["omitted"] += 1
            continue
        tools = [t for t in turn.get("tools", []) if isinstance(t, dict)]
        names = [t["name"] for t in tools]
        booked = any(t.get("ok") for t in tools if t.get("name") == "confirm_action")
        suffix = ("booked" if booked else f"tool {names[0]}" + (f" +{len(names) - 1}" if len(names) > 1 else "") if names
                  else "answer" if turn.get("answer_after_ms") is not None else "no answer" if turn.get("gateway_generations") else "no gateway request")
        group = child("turn", turn["turn"], f"turn {turn['turn']:02d} · {suffix}", turn["start_unix"], turn.get("end_unix"),
                      {"turn": turn["turn"], "caller_spoke_at": clock(turn["caller_start_s"]) if turn.get("caller_start_s") is not None else None,
                       "caller_spoke_s": turn.get("caller_duration_s"), "asr_confidence": turn.get("asr_confidence")},
                      {"what_happened": turn.get("line"), "first_response_ms": turn.get("first_response_ms"), "answer_after_ms": turn.get("answer_after_ms"),
                       "cue": turn["cue"]["text"] if turn.get("cue") else None,
                       "no_cue_because": label(turn["cue_suppressed_reason"]) if turn.get("cue_suppressed_reason") and not turn.get("cue") else None,
                       "tools": [f"{t['name']} {'ok' if t.get('ok') else (t.get('error_code') or '?')}" for t in tools], "tokens": turn.get("tokens"),
                       "assistant_spoke_s": turn.get("assistant_duration_s"), "stage_ms": turn.get("stage_ms")},
                      tags=["caller-turn"], metadata={"turn": turn["turn"], "grouping_only": True})
        if group is None:
            continue
        counts["turns"] += 1
        for generation in sorted((g for g in turn.get("generations", []) if isinstance(g, dict)), key=lambda g: (g.get("started_at_unix") or 0, str(g.get("request_id")))):
            _generation_children(child, generation, group, counts)
        for tool in tools:
            if tool.get("start_unix") is None:
                continue
            status = "ok" if tool.get("ok") else ("pending" if tool.get("error_code") == "operation_pending" else (tool.get("error_code") or "unknown"))
            if child("tool", tool.get("operation_id") or tool.get("ref"), f"tool · {tool['name']} ({status}, {seconds_text(tool.get('duration_ms'))})",
                     tool["start_unix"], tool["start_unix"] + (tool.get("duration_ms") or 0) / 1000,
                     {"tool": tool["name"], "turn": turn["turn"]},
                     {"ok": tool.get("ok"), "error": tool.get("error_code"), "duration_ms": tool.get("duration_ms"), "receipt_present": tool.get("receipt_present"),
                      "issuance_verified": tool.get("issuance_verified"), "scope": tool.get("scope")},
                     parent=group, run_type="tool", tags=["tool"], metadata={"operation_id": tool.get("operation_id")}) is not None:
                counts["tools"] += 1
    unassigned = [g for g in trace.get("unassigned_generations", []) if isinstance(g, dict) and number(g.get("started_at_unix"), 1_577_836_800, 4_102_444_800) is not None] if isinstance(trace.get("unassigned_generations"), list) else []
    if unassigned:
        start = min(g["started_at_unix"] for g in unassigned)
        end = max(g["started_at_unix"] + (number(g.get("total_ms")) or 0) / 1000 for g in unassigned)
        group = child("group", "unassigned", f"gateway requests without a recorded turn · {len(unassigned)}", start, end,
                      {"count": len(unassigned)}, {"why": "The diagnostics ledger had no turn identifier for these requests (ledger pruned, disabled or recorded before the overlay)."},
                      tags=["grouping-only"], metadata={"grouping_only": True})
        for generation in sorted(unassigned, key=lambda g: (g["started_at_unix"], str(g.get("request_id")))):
            _generation_children(child, generation, group, counts)
    tts = [r for r in trace.get("tts_requests", []) if isinstance(r, dict) and number(r.get("start_unix"), 1_577_836_800, 4_102_444_800) is not None] if isinstance(trace.get("tts_requests"), list) else []
    if tts:
        start = min(r["start_unix"] for r in tts)
        end = max(r["start_unix"] + (number(r.get("elapsed_ms")) or 0) / 1000 for r in tts)
        group = child("group", "tts", f"speech synthesis · {len(tts)} requests", start, end, {"requests": len(tts)},
                      {"cached_cue_clips": sum(1 for r in tts if r.get("cache_hit")), "failed": sum(1 for r in tts if r.get("outcome") == "failed"),
                       "first_audio_p95_ms": percentile([r["first_audio_ms"] for r in tts if r.get("first_audio_ms") is not None]),
                       "note": "Requests from Vapi to the Gemini speech sidecar. They carry a call id but no turn id, so they are listed here rather than under a turn."},
                      tags=["grouping-only", "tts"], metadata={"grouping_only": True})
        for request in tts:
            if not isinstance(request.get("request_id"), str) or not _REQUEST.fullmatch(request["request_id"]):
                counts["omitted"] += 1
                continue
            name = "tts · cached cue clip" if request.get("cache_hit") else f"tts · {request.get('outcome') or 'started'}" + (f" (first audio {seconds_text(request['first_audio_ms'])})" if request.get("first_audio_ms") is not None else "")
            if child("tts", request["request_id"], name, request["start_unix"], request["start_unix"] + (number(request.get("elapsed_ms")) or 0) / 1000,
                     {"request_id": request["request_id"]},
                     {"outcome": request.get("outcome"), "first_audio_ms": request.get("first_audio_ms"), "elapsed_ms": request.get("elapsed_ms"),
                      "characters": request.get("characters"), "audio_ms": request.get("audio_ms"), "cache_hit": request.get("cache_hit"), "source": request.get("source"),
                      "scope": "server bytes, not audible playback"},
                     parent=group, tags=["tts"]) is not None:
                counts["tts"] += 1
    provider = trace.get("provider") if isinstance(trace.get("provider"), dict) else {}
    spans = [s for s in provider.get("model_spans", []) if isinstance(s, dict) and isinstance(s.get("span_id"), str) and _UUID.fullmatch(s["span_id"]) and type(s.get("started_unix_ms")) is int] if isinstance(provider.get("model_spans"), list) else []
    if spans:
        start = min(s["started_unix_ms"] for s in spans) / 1000
        end = max(s["started_unix_ms"] + (number(s.get("duration_ms")) or 0) for s in spans) / 1000
        group = child("group", "provider", f"vapi · {len(spans)} model requests as Vapi saw them", start, end,
                      {"model_requests": len(spans)},
                      {"event_counts": provider.get("event_counts"), "interruption_flags": provider.get("interruption_flags"),
                       "timeline": (provider.get("timeline") or [])[:400],
                       "note": "Vapi's own log of LLM requests and speech start/stop events. Not joined to gateway generations: there is no shared identifier."},
                      tags=["grouping-only", "provider-reported"], metadata={"grouping_only": True})
        for span in spans:
            if child("provider_model", span["span_id"], f"vapi llm · {label(span.get('classification'))}" + (f" ({seconds_text(span['duration_ms'])})" if number(span.get("duration_ms")) is not None else ""),
                     span["started_unix_ms"] / 1000, (span["started_unix_ms"] + (number(span.get("duration_ms")) or 0)) / 1000,
                     {"provider_model_span_id": span["span_id"]},
                     {"classification": span.get("classification"), "first_token_ms": span.get("first_token_ms"), "duration_ms": span.get("duration_ms"), "gateway_generation_id": None},
                     parent=group, tags=["provider-reported"]) is not None:
                counts["provider_model_spans"] += 1
    if len(runs) > max_children:
        counts["omitted"] += len(runs) - max_children
        runs = runs[:max_children]
    return {"runs": runs, "coverage": {"schema_version": TRACE_VERSION, **counts, "exported_runs": len(runs),
                                       "interpretation": "Readable per-turn trace from explicit identifiers; group rows are envelopes, not measured stages. Nothing here proves phone playback."}}


def _generation_children(child, generation, group, counts):
    start = number(generation.get("started_at_unix"), 1_577_836_800, 4_102_444_800)
    total = number(generation.get("total_ms"))
    request = generation.get("request_id")
    if start is None or total is None or not isinstance(request, str) or not _REQUEST.fullmatch(request):
        counts["omitted"] += 1
        return
    end = start + total / 1000
    cue = generation.get("cue") if isinstance(generation.get("cue"), dict) else None
    if cue and cue.get("played") and number(cue.get("emitted_ms")) is not None:
        if child("cue", request, f"cue · {cue.get('text')} ({seconds_text(cue['emitted_ms'])})" + (" follow-up" if cue.get("follow_up") else ""),
                 start, start + cue["emitted_ms"] / 1000, {"phrase": cue.get("phrase_id")},
                 {"text": cue.get("text"), "yielded_after_ms": cue["emitted_ms"], "classifier_ms": cue.get("classifier_ms"), "follow_up": cue.get("follow_up"), "scope": "gateway yield, not audible playback"},
                 parent=group, tags=["waiting-cue"]) is not None:
            counts["cues"] += 1
    route = generation.get("route") if generation.get("route") in ("gemini", "deterministic_receipt", "jev_static_faq") else "unknown"
    outcome = code(generation.get("outcome")) or "unknown"
    first_text, tool_emitted = number(generation.get("first_text_ms")), number(generation.get("tool_emitted_ms"))
    answer = answer_ms({"first_text_ms": first_text, "tool_emitted_ms": tool_emitted})
    if route == "deterministic_receipt":
        name = "receipt readback · verified receipt, no model"
    elif route == "jev_static_faq":
        name = "static faq · Jev classifier, no model"
    elif outcome in _SILENT_OUTCOMES and answer is None:
        name = f"gemini · speculative request cancelled ({seconds_text(total)})"
    elif tool_emitted is not None and (first_text is None or tool_emitted <= first_text):
        name = f"gemini · tool call after {seconds_text(tool_emitted)}"
    elif first_text is not None:
        name = f"gemini · answer, first text after {seconds_text(first_text)}"
    else:
        name = f"gemini · {label(outcome)}"
    outputs = {"route": route, "outcome": outcome, "first_text_ms": first_text, "tool_emitted_ms": tool_emitted,
               "upstream_headers_ms": number(generation.get("upstream_headers_ms")), "total_ms": total,
               "cue": cue.get("text") if cue and cue.get("played") else None,
               "cue_decision": label(cue.get("reason")) if cue and not cue.get("played") and cue.get("reason") else None,
               "refunded_cue": cue.get("text") if cue and cue.get("refunded") else None}
    tokens = generation.get("total_tokens")
    if type(tokens) is int:
        outputs["usage_metadata"] = {"input_tokens": generation.get("input_tokens") if type(generation.get("input_tokens")) is int else 0,
                                     "output_tokens": generation.get("output_tokens") if type(generation.get("output_tokens")) is int else 0, "total_tokens": tokens}
    generation_id = generation.get("generation_id") if isinstance(generation.get("generation_id"), str) and _UUID.fullmatch(generation["generation_id"]) else None
    if child("generation", request, name, start, end, {"generation_id": generation_id, "route": route}, outputs, parent=group,
             run_type="llm" if route == "gemini" else "chain", tags=["gateway-generation", route],
             metadata={"generation_id": generation_id, "model": "gemini-3.8-flash" if route == "gemini" else None, "not_an_additional_model_call": True}) is not None:
        counts["generations"] += 1


def review_tags(report):
    tags = ["real-call", "automatic-review", report.get("review_version", ""), "needs-review" if report.get("needs_review") else "checks-clear"]
    if report.get("slow_call"):
        tags.append("slow")
    booking = report.get("booking") or {}
    tags.append("booked" if booking.get("appointment_count") else "no-booking")
    ended = (report.get("call_summary") or {}).get("ended_reason")
    tags.append(ENDED_TAGS.get(ended, "ended-other"))
    return [t for t in tags if t]


def review_metadata(report, *, call_id, environment, latency_target_ms):
    summary = report.get("call_summary") or {}
    booking = report.get("booking") or {}
    cost = ((report.get("6 Booking and actions") or {}).get("provider_cost") or {})
    return {"review_version": report.get("review_version"), "environment": environment, "call_id": call_id,
            "judge_model": (report.get("review_process") or {}).get("judge_model"), "needs_review": bool(report.get("needs_review")),
            "slow_call": bool(report.get("slow_call")), "latency_target_ms": latency_target_ms,
            "ended_reason": summary.get("ended_reason"), "channel": summary.get("channel"), "caller_turns": summary.get("caller_utterances"),
            "bookings": booking.get("appointment_count"), "duration_seconds": summary.get("duration_seconds"),
            "cost_usd": cost.get("total_usd")}


def feedback_rows(run_id, report):
    """Feedback a person can scan: checks score 1/0, flags, coverage, metric_* values."""
    checks = report.get("checks") or {}
    for key, item in checks.items():
        if not isinstance(item, dict):
            continue
        verdict = item.get("verdict")
        reason = item.get("issue_code") if item.get("issue_code") not in (None, "") else item.get("reason_code", "")
        yield {"key": key, "score": 1 if verdict == "pass" else 0 if verdict == "fail" else None, "value": verdict,
               "comment": f"{CHECK_LABELS.get(key, key)}. {VERDICT_WORDS.get(verdict, 'UNCERTAIN')}: {label(reason)}." + (" Evidence: " + ", ".join(str(r) for r in item.get("evidence_refs", [])[:8]) if item.get("evidence_refs") else "")}
    yield {"key": "needs_review", "score": int(bool(report.get("needs_review"))), "value": "review" if report.get("needs_review") else "clear",
           "comment": "1 = a conversation, booking or evidence check failed or stayed uncertain; a human should look. Speed is reported separately as slow_call."}
    yield {"key": "slow_call", "score": int(bool(report.get("slow_call"))), "value": "slow" if report.get("slow_call") else "within_target",
           "comment": "1 = first response or server answer missed its target. See the Latency section for which one."}
    process = report.get("review_process") or {}
    semantic = process.get("semantic_status") in ("assessed", "assessed_with_uncertainty")
    yield {"key": "coverage_semantic_review", "score": int(semantic), "value": process.get("semantic_status"),
           "comment": "Coverage, not quality: 1 = the Gemini reviewer graded this call; 0 = it could not (budget, evidence or judge outage)."}
    capture = report.get("provider_observations")
    if isinstance(capture, dict):
        yield {"key": "coverage_provider_log", "score": 1 if capture.get("status") == "observed" else None, "value": capture.get("status", "unavailable"),
               "comment": "Coverage, not quality: 1 = Vapi's call log was captured and projected."}
    ended = (report.get("evidence_coverage") or {}).get("provider_ended_by_assistant")
    yield {"key": "assistant_hung_up", "score": int(ended) if type(ended) is bool else None, "value": ended if type(ended) is bool else "unknown",
           "comment": "Coverage, not quality: Vapi's end reason says the assistant ended the call (1) or the caller did (0). Does not prove the goodbye finished playing."}
    latency = report.get("latency") or {}
    cues = report.get("5 Waiting cues") or {}
    summary = report.get("call_summary") or {}
    booking = report.get("booking") or {}
    actions = report.get("6 Booking and actions") or {}
    cost = actions.get("provider_cost") or {}
    speed = report.get("4 Latency") or {}
    details = (report.get("3 Turn-by-turn timeline") or {}).get("details")
    details = [d for d in details if isinstance(d, dict)] if isinstance(details, list) else []
    confidences = [d["asr_confidence"] for d in details if number(d.get("asr_confidence"), 0, 1) is not None]
    metrics = [
        ("metric_call_seconds", summary.get("duration_seconds"), "Call length in seconds."),
        ("metric_caller_turns", summary.get("caller_utterances"), "Caller utterances in the stored transcript."),
        ("metric_bookings", booking.get("appointment_count"), "Appointments saved on this call."),
        ("metric_cost_usd", cost.get("total_usd"), "Vapi-reported provider cost in USD; self-hosted Gemini and speech costs are not included."),
        ("metric_first_response_avg_ms", latency.get("turn_avg_ms"), "Average ms from the caller's end of speech to assistant audio (Vapi stage sum); with cues this is usually the cue."),
        ("metric_first_response_p95_ms", latency.get("turn_p95_ms"), "Slow end (p95) of the same measurement."),
        ("metric_answer_p95_ms", speed.get("answer_p95_ms"), "Server ms until the real answer or tool call, p95; excludes recognition, synthesis and transport."),
        ("metric_cue_p95_ms", latency.get("server_first_ack_p95_ms"), "Server ms until a waiting cue was yielded, p95."),
        ("metric_cues_played", cues.get("turns_with_a_cue"), "Caller turns on which a waiting cue played."),
        ("metric_cue_follow_ups", cues.get("follow_up_cues"), "Follow-up cues played while a tool result was answered."),
        ("metric_model_avg_ms", latency.get("model_avg_ms"), "Vapi average model time to first token."),
        ("metric_transcriber_avg_ms", latency.get("transcriber_avg_ms"), "Vapi average speech recognition time."),
        ("metric_voice_avg_ms", latency.get("voice_avg_ms"), "Vapi average voice generation time."),
        ("metric_endpointing_avg_ms", latency.get("endpointing_avg_ms"), "Vapi average end-of-speech detection time."),
        ("metric_tool_calls", len(actions.get("tools") or []) if isinstance(actions.get("tools"), list) else None, "Business tool calls observed."),
        ("metric_tokens_total", sum(d["tokens"] for d in details if type(d.get("tokens")) is int) or None, "Gemini tokens across this call's gateway generations."),
        ("metric_caller_interruptions", (speed.get("interruptions") or {}).get("caller_interrupted_assistant"), "Times the caller interrupted the assistant, as counted by Vapi."),
        ("metric_asr_confidence_min", min(confidences) if confidences else None, "Lowest speech-recognition confidence on any caller turn (0 to 1)."),
    ]
    for key, value, comment in metrics:
        if type(value) in (int, float) and math.isfinite(value):
            yield {"key": key, "score": value, "value": value, "comment": "Measurement, not a score. " + comment}
