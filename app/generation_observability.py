"""Content-free projection of provider generation evidence.

Provider turns, model spans, local gateway requests and audio streams have
different identities. This projector never joins them using text, timestamps,
array proximity or matching counts. It observes logs; it does not control calls.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import math
import re
from uuid import UUID, uuid5

from .observability import safe_backchannel_timing, safe_timing_decision_code

VERSION = "generation-observability-v1"
MAX_EVENTS = 10_000
_TURN = re.compile(r"^(?:0|[1-9][0-9]{0,8})$")
_EVENTS = {
    "Turn started": "provider.turn_started",
    "Pipeline cleared": "provider.pipeline_cleared",
    "LLM request started": "provider.model_started",
    "LLM first token received": "provider.model_first_token",
    "LLM response completed": "provider.model_completed",
    "LLM request aborted before completion": "provider.model_abort_label",
    "Bot started speaking": "provider.assistant_speech_started",
    "Bot stopped speaking": "provider.assistant_speech_stopped",
    "User started speaking": "provider.caller_speech_started",
    "User stopped speaking": "provider.caller_speech_stopped",
}
_MODEL_EVENTS = frozenset({"provider.model_started", "provider.model_first_token",
                          "provider.model_completed", "provider.model_abort_label"})
GATEWAY_VERSION = "gateway-generations-v1"
_GATEWAY_ENUMS = {
    "generation_sequence_scope": {"process_local_http_arrival_only"},
    "predecessor_relation": {"queued_successor", "active_or_draining_owner"},
    "supersession_cause": {"newer_local_http_arrival", "gateway_shutdown", "unknown", "predecessor_cleanup_wait_expired"},
    "payload_release_scope": {"gateway_yield_not_provider_receipt_or_playback"},
    "route": {"gemini", "deterministic_receipt", "jev_static_faq"},
    "outcome": {"completed", "tool_emitted", "narrated", "interrupted", "rejected", "state_unavailable", "capacity_rejected", "generation_conflict", "cancelled", "stream_unavailable", "error", "upstream_unavailable", "superseded", "superseded_after_release", "supersession_wait_expired"},
}
_GATEWAY_ENUMS["generation_terminal_state"] = _GATEWAY_ENUMS["outcome"] | {"superseded_before_release", "downstream_disconnected", "cancelled_unknown_cause"}
_GATEWAY_PHASES = ("preflight", "upstream_headers", "first_upstream_event", "first_upstream_text", "first_text", "first_tool_fragment", "upstream_done", "tool_emitted", "total")
_GATEWAY_TIMES = ("superseded_at_ms", "payload_released_at_ms", "downstream_disconnect_at_ms")
_DOT_SEGMENT = re.compile(r"\d{8}T\d{12}Z[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}")


class GenerationEvidenceError(ValueError):
    """Fixed public error codes; never include the offending log content."""


def _bounded_number(value, minimum, maximum):
    return type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum


def project_gateway_timings(rows):
    """Project already call-scoped local rows; never derive IDs for old rows.

    Local HTTP arrival order, emitted SSE IDs and server-side cancellation are
    observations at our gateway. They do not establish provider turn order,
    audible barge-in, TTS packet ownership or carrier/phone packet discard.
    """
    if not isinstance(rows, list) or len(rows) > 5000:
        raise GenerationEvidenceError("invalid_or_oversized_gateway_timings")
    skipped = Counter();unique = {};conflicts = set();duplicates = 0;scopes = set()
    for source in rows:
        if not isinstance(source, dict):
            skipped["invalid_gateway_timing_row"] += 1;continue
        gid = _uuid(source.get("generation_id"))
        if gid is None:
            skipped["persisted_generation_identity_unavailable"] += 1;continue
        key = source.get("call_key")
        if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{16}", key):
            skipped["call_scope_unavailable"] += 1;continue
        started = source.get("started_at_unix")
        phases = source.get("milestones_ms")
        total = phases.get("total") if isinstance(phases, dict) else None
        if not _bounded_number(started, 1577836800, 4102444800) or not _bounded_number(total, 0, 86_400_000):
            skipped["actual_gateway_interval_unavailable"] += 1;continue
        ended = started + total/1000
        if not _bounded_number(ended, started, 4102444800):
            skipped["actual_gateway_interval_unavailable"] += 1;continue
        row = {"generation_id": gid, "request_id": None, "call_key": key,
            "started_at_unix": float(started), "ended_at_unix": float(ended),
            "milestones_ms": {name: float(phases[name]) if _bounded_number(phases.get(name), 0, total) else None for name in _GATEWAY_PHASES},
            "identity_basis": "explicit_persisted_gateway_generation_id", "timing_basis": "server_observed_start_plus_duration"}
        rid = source.get("request_id")
        if isinstance(rid, str) and (re.fullmatch(r"[a-f0-9]{32}", rid) or _uuid(rid)):
            row["request_id"] = rid
        for name in ("predecessor_generation_id", "superseded_by_generation_id"):
            row[name] = _uuid(source.get(name))
        for name, allowed in _GATEWAY_ENUMS.items():
            value = source.get(name)
            row[name] = value if type(value) is str and value in allowed else None
        for name, allowed in (("model", {"gemini-3.8-flash", "gemini-3.5-flash-lite"}), ("reasoning_effort", {"low", "minimal"})):
            if isinstance(source.get(name), str) and source[name] in allowed:
                row[name] = source[name]
        for name in ("narration_reason", "jev_reason", "eof_drain_outcome"):
            # Frozen older review rows omitted these optional fields. Preserve
            # that shape so an export retry cannot change immutable children.
            if name in source:
                row[name] = safe_timing_decision_code(source[name])
        # Preserve observed optional cue evidence through both disk projection
        # and LangSmith's repeated export boundary. Missing legacy evidence
        # remains absent rather than becoming an inferred silent decision.
        cue = safe_backchannel_timing(source.get("backchannel"))
        if cue is not None:
            if "emitted_ms" in cue:
                value = cue["emitted_ms"]
                if _bounded_number(value, 0, total):
                    cue["emitted_ms"] = float(value)
                else:
                    cue.pop("emitted_ms")
            row["backchannel"] = cue
        for name, minimum in (("generation_sequence", 1), ("checkpoint_rejections", 0)):
            value = source.get(name)
            row[name] = value if type(value) is int and minimum <= value <= 100_000_000 else None
        for name in _GATEWAY_TIMES:
            value = source.get(name)
            row[name] = float(value) if _bounded_number(value, 0, total) else None
        for name in ("sse_response_id", "neutral_sse_response_id"):
            value = source.get(name)
            row[name] = value if type(value) is str and re.fullmatch(r"chatcmpl-[a-f0-9]{32}", value) else None
        scopes.add(key)
        if gid in unique:
            if unique[gid] == row:duplicates += 1
            else:conflicts.add(gid)
        else:unique[gid] = row
    if len(scopes) > 1:
        skipped["conflicting_gateway_call_scopes"] += len(unique);unique = {}
    for gid in conflicts:
        unique.pop(gid, None);skipped["conflicting_generation_identity"] += 1
    generations = sorted(unique.values(), key=lambda row: (row["started_at_unix"], row["generation_id"]))
    coverage = {"schema_version": GATEWAY_VERSION, "input_rows": len(rows), "observed_generation_count": len(generations),
        "exact_duplicate_rows_ignored": duplicates, "skipped_row_counts": dict(sorted(skipped.items())),
        "state": "observed_gateway_boundary_only" if generations else "unavailable",
        "complete_generation_visibility": False, "provider_turn_or_tts_generation_join": "unavailable_without_explicit_shared_wire_id",
        "late_phone_packet_discard_verified": False, "audible_interruption_count": None,
        "legacy_generation_ids_inferred": False,
        "interpretation": "Persisted gateway generation IDs only. Sequence is local HTTP arrival order. Payload release is a gateway yield, not provider receipt or phone playback. No historical IDs or provider/TTS joins are inferred."}
    return {"schema_version": GATEWAY_VERSION, "generations": generations, "coverage": coverage}


def build_gateway_generation_runs(projected, *, root_run_id, root_dotted_order, project_id):
    """Create deterministic direct-root children, using actual observed intervals."""
    segments = root_dotted_order.split(".") if isinstance(root_dotted_order, str) else []
    if (_uuid(root_run_id) is None or _uuid(project_id) is None or not segments
        or any(not _DOT_SEGMENT.fullmatch(item) for item in segments) or not segments[-1].endswith(root_run_id)):
        raise GenerationEvidenceError("invalid_gateway_generation_destination")
    if not isinstance(projected, dict) or projected.get("schema_version") != GATEWAY_VERSION:
        raise GenerationEvidenceError("invalid_gateway_generation_projection")
    # Reapply the boundary so arbitrary report text can never enter run outputs.
    safe = project_gateway_timings(projected.get("generations"))
    runs = []
    for row in safe["generations"]:
        run_id = str(uuid5(UUID(root_run_id), GATEWAY_VERSION + ":generation:" + row["generation_id"]))
        start = datetime.fromtimestamp(row["started_at_unix"], timezone.utc)
        end = datetime.fromtimestamp(row["ended_at_unix"], timezone.utc)
        runs.append({"id": run_id, "trace_id": root_run_id, "parent_run_id": root_run_id,
            "session_id": project_id, "name": "voice.gateway.generation", "run_type": "chain", "error": None,
            "start_time": start.isoformat(timespec="microseconds"), "end_time": end.isoformat(timespec="microseconds"),
            "dotted_order": root_dotted_order + "." + start.strftime("%Y%m%dT%H%M%S%fZ") + run_id,
            "inputs": {"generation_id": row["generation_id"], "identity_basis": row["identity_basis"]},
            "outputs": {"observed_generation": row, "provider_model_span_id": None, "tts_generation_id": None,
                "phone_playback_verified": False, "late_phone_packet_discard_verified": False,
                "observation_scope": "gateway_server_boundary_not_phone_playback"},
            "tags": [GATEWAY_VERSION, "gateway-generation-evidence", "server-boundary-not-playback", "not-an-additional-model-call"],
            "extra": {"metadata": {"generation_schema_version": GATEWAY_VERSION,
                "generation_id": row["generation_id"], "timing_basis": row["timing_basis"],
                "parentage_basis": "call_review_root_only_no_inferred_lineage_parent", "capture_policy": "content-free-v1"}}})
    # Preserve the original fixed-schema coverage only by rebuilding its counts
    # at the projection boundary; no caller-provided prose is copied here.
    return {"runs": runs, "coverage": safe["coverage"]}


def _uuid(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = str(UUID(value))
    except (ValueError, AttributeError):
        return None
    return parsed if parsed == value else None


def _turn(value):
    return value if isinstance(value, str) and _TURN.fullmatch(value) else None


def _milliseconds(value):
    # Provider Unix milliseconds, not local monotonic or audible playback time.
    return value if type(value) is int and 1_577_836_800_000 <= value < 4_102_444_800_000 else None


def _counter(value):
    return value if type(value) is int and 0 <= value <= 1_000_000 else None


def _flag(value):
    return "true" if value is True else "false" if value is False else "unknown"


def project_provider_generations(rows, call_id):
    """Project bounded logs using only explicit call/span/turn identities.

    ``body`` is matched against a fixed event allowlist and is never copied.
    Request headers, text, tool arguments, customer fields and audio are ignored.
    A provider's cleanup "abort" after completion is not a failed generation.
    """
    if _uuid(call_id) is None:
        raise GenerationEvidenceError("invalid_call_identity")
    if not isinstance(rows, list) or len(rows) > MAX_EVENTS:
        raise GenerationEvidenceError("invalid_or_oversized_provider_log")
    if any(not isinstance(row, dict) for row in rows):
        raise GenerationEvidenceError("invalid_provider_log_row")
    events, metadata, groups = [], [], defaultdict(list)
    metadata_seen = set()
    flags, metadata_flags, skipped = Counter(), Counter(), Counter()
    for index, source in enumerate(rows):
        attrs = source.get("attributes")
        if not isinstance(attrs, dict):
            skipped["attributes_unavailable"] += 1
            continue
        # Correlation is explicit. Merely residing in this file is not a join.
        cid, trace = attrs.get("callId"), attrs.get("traceId")
        if cid not in (None, call_id) or trace not in (None, call_id):
            skipped["conflicting_call_identity"] += 1
            continue
        if cid != call_id and trace != call_id:
            skipped["unbound_call_identity"] += 1
            continue
        if "wasInterruption" in attrs:
            flags[_flag(attrs["wasInterruption"])] += 1
        name = source.get("body")
        if name == "Custom LLM HTTP request":
            request = attrs.get("request")
            if isinstance(request, dict):
                stamp = _milliseconds(request.get("timestamp"))
                value = request.get("metadata")
                if stamp is not None and isinstance(value, dict) and stamp not in metadata_seen:
                    metadata_seen.add(stamp)
                    reported = value.get("assistantTurnInterrupted")
                    metadata_flags[_flag(reported)] += 1
                    metadata.append({"source_entry_index": index,
                        "provider_request_timestamp_unix_ms": stamp,
                        "provider_num_assistant_turns": _counter(value.get("numAssistantTurns")),
                        "provider_num_user_turns": _counter(value.get("numUserTurns")),
                        "provider_num_model_request_in_turn": _counter(value.get("numModelRequestInTurn")),
                        "provider_assistant_turn_interrupted": reported if type(reported) is bool else None})
        mapped = _EVENTS.get(name) if isinstance(name, str) else None
        if mapped is None:
            continue
        when = _milliseconds(source.get("time"))
        if when is None:
            skipped["relevant_timestamp_unavailable"] += 1
        event = {"source_entry_index": index, "event": mapped,
            "observed_unix_ms": when, "provider_turn_id": _turn(attrs.get("turnId")),
            "provider_previous_turn_id": _turn(attrs.get("previousTurnId")),
            "provider_model_span_id": _uuid(attrs.get("spanId")) if mapped in _MODEL_EVENTS else None,
            "identity_basis": "explicit_provider_fields", "timing_basis": "provider_reported"}
        events.append(event)
        if mapped in _MODEL_EVENTS:
            # A span UUID is a model identity only with the matching trace ID.
            if event["provider_model_span_id"] is not None and trace == call_id:
                groups[event["provider_model_span_id"]].append(event)
            else:
                skipped["model_span_identity_unavailable"] += 1
    generations = []
    for span, group in groups.items():
        selected = {kind: [e for e in group if e["event"] == kind] for kind in _MODEL_EVENTS}
        starts = selected["provider.model_started"]
        first = selected["provider.model_first_token"]
        complete = selected["provider.model_completed"]
        abort = selected["provider.model_abort_label"]
        completed_then_abort = bool(len(starts) == 1 and complete and abort
            and complete[-1]["observed_unix_ms"] is not None
            and abort[-1]["observed_unix_ms"] is not None
            and abort[-1]["observed_unix_ms"] >= complete[-1]["observed_unix_ms"]
            and abort[-1]["source_entry_index"] > complete[-1]["source_entry_index"])
        outcome = ("completed_then_cleanup_abort_label" if completed_then_abort
            else "aborted_without_observed_output" if len(starts) == 1 and abort and not complete and not first
            else "completed" if len(starts) == 1 and complete and not abort
            else "ambiguous_or_incomplete")
        def value(items, key):
            return items[0].get(key) if items else None
        start = value(starts, "observed_unix_ms")
        end = value(complete, "observed_unix_ms") if complete else value(abort, "observed_unix_ms")
        generations.append({"provider_model_span_id": span,
            "provider_turn_id_at_start": value(starts, "provider_turn_id"),
            "provider_turn_id_at_abort": value(abort, "provider_turn_id"),
            "started_unix_ms": start, "first_token_unix_ms": value(first, "observed_unix_ms"),
            "completed_unix_ms": value(complete, "observed_unix_ms"),
            "abort_label_unix_ms": value(abort, "observed_unix_ms"),
            "observed_duration_ms": end - start if type(start) is int and type(end) is int and end >= start else None,
            "classification": outcome, "source_entry_indices": [e["source_entry_index"] for e in group],
            "identity_basis": "explicit_provider_trace_and_span_ids"})
    generations.sort(key=lambda row: (row["started_unix_ms"] or 0, row["provider_model_span_id"]))
    counts = Counter(e["event"] for e in events)
    classifications = Counter(g["classification"] for g in generations)
    return {"schema_version": VERSION, "call_id": call_id,
        "counts": {"provider_turn_starts": counts["provider.turn_started"],
            "provider_pipeline_clears": counts["provider.pipeline_cleared"],
            "provider_model_spans": len(generations),
            "completed_then_cleanup_abort_label": classifications["completed_then_cleanup_abort_label"],
            "aborted_without_observed_output": classifications["aborted_without_observed_output"],
            "ambiguous_or_incomplete_model_spans": classifications["ambiguous_or_incomplete"],
            "provider_assistant_speech_starts": counts["provider.assistant_speech_started"],
            "provider_assistant_speech_stops": counts["provider.assistant_speech_stopped"],
            "provider_caller_speech_starts": counts["provider.caller_speech_started"],
            "provider_caller_speech_stops": counts["provider.caller_speech_stopped"]},
        "provider_flag_evidence": {"pipeline_was_interruption_values": dict(flags),
            "request_assistant_turn_interrupted_values": dict(metadata_flags),
            "scope_reconciliation": "not_established",
            "interpretation": "Different provider flags do not independently prove audible interruption. Their semantics and scopes may differ."},
        "model_generations": generations, "events": events, "provider_request_metadata": metadata,
        "skipped": dict(skipped),
        "coverage": {"provider_turn_is_caller_turn": False,
            "local_gateway_generation_join": "unavailable_without_explicit_shared_wire_id",
            "tts_generation_join": "unavailable_without_explicit_shared_wire_id",
            "rtp_generation_ids_observed": False, "late_phone_packet_discard_verified": False,
            "audible_caller_interruption_count": None, "complete": False},
        "limitations": [
            "Provider turn cycles are not caller utterance counts or independent proof of interruptions.",
            "Aborted without output does not establish whether an actual caller interruption caused it.",
            "A cleanup abort label after completion is not classified as a failed model response.",
            "Local gateway request IDs, provider model spans and TTS request IDs remain separate unless explicitly linked.",
            "No text/time/count heuristic is used to assign audio to a generation.",
            "Vapi/carrier queues, RTP packets and phone playout are outside this backend's demonstrated control.",
        ], "privacy": "Fixed event names, validated IDs, counts and timestamps only; no transcript, headers, audio or customer fields."}


PROVIDER_SPAN_VERSION = "provider-observation-spans-v1"
MAX_PROVIDER_RUNS = 4_000
_PROVIDER_SPAN_EVENTS = frozenset(_EVENTS.values())


class ProviderSpanError(GenerationEvidenceError):
    """Fixed provider-span validation codes; never contain source text."""


def _provider_instant(value):
    return datetime.fromtimestamp(value / 1000, timezone.utc)


def _safe_provider_events(projected):
    """Revalidate cached projections without copying arbitrary keys/prose."""
    if (not isinstance(projected, dict)
            or projected.get("schema_version") != VERSION
            or _uuid(projected.get("call_id")) is None):
        raise ProviderSpanError("invalid_provider_projection")
    raw = projected.get("events")
    if not isinstance(raw, list) or len(raw) > MAX_EVENTS:
        raise ProviderSpanError("invalid_or_oversized_provider_events")
    unique = {}; conflicts = set(); skipped = Counter(); duplicates = 0
    # The historical projector retains a span UUID on an event even when its
    # trace ID was absent. Its model groups alone certify explicit trace+span
    # eligibility. Preserve that distinction rather than reconstructing a
    # previously rejected interval from the UUID and nearby timestamps.
    groups = projected.get("model_generations", [])
    if not isinstance(groups, list) or len(groups) > MAX_EVENTS:
        raise ProviderSpanError("invalid_or_oversized_provider_model_identities")
    bound = set(); binding_count = 0
    for group in groups:
        if not isinstance(group, dict):
            skipped["invalid_provider_model_identity"] += 1; continue
        span, indices = _uuid(group.get("provider_model_span_id")), group.get("source_entry_indices")
        if (span is None or group.get("identity_basis") != "explicit_provider_trace_and_span_ids"
                or not isinstance(indices, list) or any(type(index) is not int or not 0 <= index < MAX_EVENTS for index in indices)):
            skipped["invalid_provider_model_identity"] += 1; continue
        binding_count += len(indices)
        if binding_count > MAX_EVENTS:
            raise ProviderSpanError("invalid_or_oversized_provider_model_identities")
        bound.update((span, index) for index in indices)
    for source in raw:
        if not isinstance(source, dict):
            skipped["invalid_provider_event"] += 1
            continue
        index, kind = source.get("source_entry_index"), source.get("event")
        if (type(index) is not int or not 0 <= index < MAX_EVENTS
                or type(kind) is not str or kind not in _PROVIDER_SPAN_EVENTS):
            skipped["invalid_provider_event"] += 1
            continue
        span = _uuid(source.get("provider_model_span_id")) if kind in _MODEL_EVENTS else None
        eligible = span is not None and ((span, index) in bound or
            "model_generations" not in projected and source.get("provider_model_identity_basis") == "explicit_provider_trace_and_span_ids")
        if span is not None and not eligible:
            skipped["model_span_trace_binding_unavailable"] += 1
        row = {"source_entry_index": index, "event": kind,
            "observed_unix_ms": _milliseconds(source.get("observed_unix_ms")),
            "provider_turn_id": _turn(source.get("provider_turn_id")),
            "provider_previous_turn_id": _turn(source.get("provider_previous_turn_id")),
            "provider_model_span_id": span if eligible else None,
            "provider_model_identity_basis": "explicit_provider_trace_and_span_ids" if eligible else None,
            "identity_basis": "explicit_provider_fields", "timing_basis": "provider_reported"}
        if index in unique:
            if unique[index] != row:
                conflicts.add(index)
            else:
                duplicates += 1
        else:
            unique[index] = row
    for index in conflicts:
        unique.pop(index, None)
    if conflicts:
        skipped["conflicting_source_entry_identity"] += len(conflicts)
    return sorted(unique.values(), key=lambda row: row["source_entry_index"]), dict(skipped), duplicates


def sanitize_provider_projection(projected, expected_call_id):
    """Project stored vendor observations again before the SaaS boundary.

    The authenticated caller supplies the expected call independently. Counts,
    text, headers, purported model intervals and explanatory prose in a cached
    projection never become trusted export fields merely by being persisted.
    """
    rows, skipped, duplicates = _safe_provider_events(projected)
    if _uuid(expected_call_id) is None or projected["call_id"] != expected_call_id:
        raise ProviderSpanError("provider_projection_call_scope_mismatch")
    issues = Counter(skipped)
    prior = projected.get("summary", {})
    if not isinstance(prior, dict):
        prior = {}; issues["invalid_prior_projection_summary_fields"] += 1
    allowed_issues = {"invalid_provider_event", "conflicting_source_entry_identity", "invalid_provider_flag_fields",
        "invalid_provider_model_identity", "model_span_trace_binding_unavailable",
        "invalid_source_projection_skip_fields", "invalid_provider_request_metadata",
        "invalid_provider_request_metadata_fields", "conflicting_provider_request_metadata_identity",
        "invalid_prior_projection_summary_fields"}
    prior_issues = prior.get("rejected_field_counts", {})
    if not isinstance(prior_issues, dict):
        prior_issues = {}; issues["invalid_prior_projection_summary_fields"] += 1
    for name, value in prior_issues.items():
        if type(name) is str and name in allowed_issues and type(value) is int and 0 <= value <= 1_000_000:
            issues[name] += value
        else:
            issues["invalid_prior_projection_summary_fields"] += 1
    def prior_duplicates(key):
        value = prior.get(key, 0)
        if type(value) is int and 0 <= value <= MAX_EVENTS:
            return value
        issues["invalid_prior_projection_summary_fields"] += 1
        return 0
    duplicates += prior_duplicates("exact_duplicate_events_ignored")
    metadata_duplicates = prior_duplicates("exact_duplicate_request_metadata_ignored")
    source_skipped = {}
    raw_skipped = projected.get("skipped", prior.get("source_projection_skipped_counts", {}))
    if not isinstance(raw_skipped, dict):
        raw_skipped = {}; issues["invalid_source_projection_skip_fields"] += 1
    allowed_skips = {"attributes_unavailable", "conflicting_call_identity", "unbound_call_identity",
        "relevant_timestamp_unavailable", "model_span_identity_unavailable"}
    for name, value in raw_skipped.items():
        if (type(name) is str and name in allowed_skips and type(value) is int and 0 <= value <= MAX_EVENTS):
            source_skipped[name] = value
        else:
            issues["invalid_source_projection_skip_fields"] += 1
    raw_flags = projected.get("provider_flag_evidence", {})
    if not isinstance(raw_flags, dict):
        raw_flags = {}; issues["invalid_provider_flag_fields"] += 1
    flags = {}
    for key in ("pipeline_was_interruption_values", "request_assistant_turn_interrupted_values"):
        raw = raw_flags.get(key, {})
        if not isinstance(raw, dict):
            raw = {}; issues["invalid_provider_flag_fields"] += 1
        flags[key] = {}
        for name, value in raw.items():
            if (type(name) is str and name in {"true", "false", "unknown"}
                    and type(value) is int and 0 <= value <= MAX_EVENTS):
                flags[key][name] = value
            else:
                issues["invalid_provider_flag_fields"] += 1
    flags.update(scope_reconciliation="not_established",
        interpretation="Different provider flags do not independently prove audible interruption; their semantics and scopes may differ.")
    raw_metadata = projected.get("provider_request_metadata", [])
    if not isinstance(raw_metadata, list) or len(raw_metadata) > MAX_EVENTS:
        raise ProviderSpanError("invalid_or_oversized_provider_metadata")
    metadata = {}; conflicts = set()
    for source in raw_metadata:
        if not isinstance(source, dict):
            issues["invalid_provider_request_metadata"] += 1; continue
        index = source.get("source_entry_index")
        stamp = _milliseconds(source.get("provider_request_timestamp_unix_ms"))
        if type(index) is not int or not 0 <= index < MAX_EVENTS or stamp is None:
            issues["invalid_provider_request_metadata"] += 1; continue
        row = {"source_entry_index": index, "provider_request_timestamp_unix_ms": stamp}
        for key in ("provider_num_assistant_turns", "provider_num_user_turns", "provider_num_model_request_in_turn"):
            row[key] = _counter(source.get(key))
            if source.get(key) is not None and row[key] is None:
                issues["invalid_provider_request_metadata_fields"] += 1
        value = source.get("provider_assistant_turn_interrupted")
        row["provider_assistant_turn_interrupted"] = value if type(value) is bool else None
        if value is not None and type(value) is not bool:
            issues["invalid_provider_request_metadata_fields"] += 1
        if index in metadata:
            if metadata[index] == row: metadata_duplicates += 1
            else: conflicts.add(index)
        else: metadata[index] = row
    for index in conflicts:
        metadata.pop(index, None)
    if conflicts: issues["conflicting_provider_request_metadata_identity"] += len(conflicts)
    counts = Counter(row["event"] for row in rows)
    return {"schema_version": VERSION, "call_id": expected_call_id, "events": rows,
        "provider_flag_evidence": flags,
        "provider_request_metadata": [metadata[index] for index in sorted(metadata)],
        "summary": {"event_counts": dict(sorted(counts.items())),
            "source_projection_skipped_counts": dict(sorted(source_skipped.items())),
            "explicit_provider_model_span_id_count": len({row["provider_model_span_id"] for row in rows
                if row["provider_model_span_id"] is not None}),
            "exact_duplicate_events_ignored": duplicates,
            "exact_duplicate_request_metadata_ignored": metadata_duplicates,
            "rejected_field_counts": dict(sorted(issues.items()))},
        "coverage": {"state": "provider_reported_partial" if rows else "unavailable", "complete": False,
            "provider_turn_is_caller_turn": False,
            "local_gateway_generation_join": "unavailable_without_explicit_shared_wire_id",
            "tts_generation_join": "unavailable_without_explicit_shared_wire_id",
            "rtp_generation_ids_observed": False, "late_phone_packet_discard_verified": False,
            "audible_caller_interruption_count": None}}


def build_provider_observation_runs(projected, *, root_run_id, root_dotted_order,
                                    project_id, expected_call_id, max_runs=MAX_PROVIDER_RUNS):
    """Build direct-root observations using explicit IDs and recorded times.

    A timestamped event is a point observation: start=end does not claim a
    measured execution duration. Model intervals require unambiguous recorded
    boundaries. Speech starts/stops are never paired using proximity, and no
    provider turn/model identity is joined to local gateway or TTS identities.
    """
    segments = root_dotted_order.split(".") if isinstance(root_dotted_order, str) else []
    if (_uuid(root_run_id) is None or _uuid(project_id) is None or not segments
            or any(not _DOT_SEGMENT.fullmatch(segment) for segment in segments)
            or not segments[-1].endswith(root_run_id)
            or _uuid(expected_call_id) is None
            or type(max_runs) is not int or not 1 <= max_runs <= MAX_PROVIDER_RUNS):
        raise ProviderSpanError("invalid_provider_span_destination_or_limit")
    rows, invalid, duplicates = _safe_provider_events(projected)
    if projected["call_id"] != expected_call_id:
        raise ProviderSpanError("provider_projection_call_scope_mismatch")
    root = UUID(root_run_id); runs = []; skipped = Counter(invalid); groups = defaultdict(list)

    def child(kind, identity, name, start_ms, end_ms, inputs, outputs, metadata):
        rid = str(uuid5(root, PROVIDER_SPAN_VERSION + ":" + kind + ":" + identity))
        start, end = _provider_instant(start_ms), _provider_instant(end_ms)
        return {"id": rid, "trace_id": root_run_id, "parent_run_id": root_run_id,
            "session_id": project_id, "name": name, "run_type": "chain", "error": None,
            "start_time": start.isoformat(timespec="microseconds"),
            "end_time": end.isoformat(timespec="microseconds"),
            "dotted_order": root_dotted_order + "." + start.strftime("%Y%m%dT%H%M%S%fZ") + rid,
            "inputs": inputs, "outputs": {**outputs, "gateway_generation_id": None,
                "tts_generation_id": None, "phone_playback_verified": False,
                "late_phone_packet_discard_verified": False,
                "audible_interruption_count": None},
            "tags": [PROVIDER_SPAN_VERSION, "provider-reported", "not-an-additional-model-call",
                "point-observation-not-duration" if kind == "event" else "explicit-provider-model-interval"],
            "extra": {"metadata": {"record_kind": "provider_" + kind,
                "provider_span_schema_version": PROVIDER_SPAN_VERSION, "capture_policy": "content-free-v1",
                "parentage_basis": "call_review_root_only_no_inferred_causal_parent", **metadata}}}

    for row in rows:
        if row["provider_model_span_id"] is not None:
            groups[row["provider_model_span_id"]].append(row)
        stamp = row["observed_unix_ms"]
        if stamp is None:
            skipped["point_event_timestamp_unavailable"] += 1
            continue
        runs.append(child("event", str(row["source_entry_index"]), "voice." + row["event"], stamp, stamp,
            {"event": row["event"], "source_entry_index": row["source_entry_index"]},
            {"observed_event": row, "timing_basis": "recorded_provider_event_instant_not_execution_duration"},
            {"source_entry_index": row["source_entry_index"], "provider_turn_id": row["provider_turn_id"],
             "provider_model_span_id": row["provider_model_span_id"],
             "timing_basis": "recorded_provider_event_instant_not_execution_duration"}))

    interval_count = 0
    for span, group in sorted(groups.items()):
        selected = {name: [row for row in group if row["event"] == name] for name in _MODEL_EVENTS}
        starts = selected["provider.model_started"]; complete = selected["provider.model_completed"]
        abort = selected["provider.model_abort_label"]; first = selected["provider.model_first_token"]
        if len(starts) != 1 or len(complete) > 1 or len(abort) > 1 or not (complete or abort):
            skipped["model_interval_boundary_ambiguous_or_missing"] += 1
            continue
        start = starts[0]["observed_unix_ms"]
        end_row = complete[0] if complete else abort[0]
        end = end_row["observed_unix_ms"]
        if start is None or end is None or end < start:
            skipped["model_interval_chronology_unavailable"] += 1
            continue
        if (any(row["observed_unix_ms"] is None or not start <= row["observed_unix_ms"] <= end for row in first)
                or (complete and abort and (abort[0]["observed_unix_ms"] is None
                    or abort[0]["observed_unix_ms"] < end
                    or abort[0]["source_entry_index"] <= complete[0]["source_entry_index"]))):
            skipped["model_interval_chronology_unavailable"] += 1
            continue
        classification = ("completed_then_cleanup_abort_label" if complete and abort
            else "completed" if complete else "aborted_without_observed_output" if not first
            else "ambiguous_or_incomplete")
        interval = {"provider_model_span_id": span,
            "provider_turn_id_at_start": starts[0]["provider_turn_id"],
            "provider_turn_id_at_abort": abort[0]["provider_turn_id"] if abort else None,
            "started_unix_ms": start, "completed_unix_ms": complete[0]["observed_unix_ms"] if complete else None,
            "abort_label_unix_ms": abort[0]["observed_unix_ms"] if abort else None,
            "first_token_unix_ms": first[0]["observed_unix_ms"] if len(first) == 1 else None,
            "observed_duration_ms": end - start, "classification": classification,
            "source_entry_indices": [row["source_entry_index"] for row in group],
            "identity_basis": "explicit_provider_trace_and_span_ids",
            "timing_basis": "recorded_provider_model_boundaries"}
        runs.append(child("model_span", span, "voice.provider.model_generation", start, end,
            {"provider_model_span_id": span, "identity_basis": interval["identity_basis"]},
            {"observed_model_interval": interval}, {"provider_model_span_id": span,
                "timing_basis": interval["timing_basis"]}))
        interval_count += 1

    runs.sort(key=lambda run: (run["start_time"], run["id"]))
    omitted = max(0, len(runs) - max_runs)
    runs = runs[:max_runs]
    event_count = sum(run["extra"]["metadata"]["record_kind"] == "provider_event" for run in runs)
    return {"runs": runs, "coverage": {"schema_version": PROVIDER_SPAN_VERSION,
        "input_projected_events": len(projected["events"]), "valid_projected_events": len(rows),
        "exact_duplicate_events_ignored": duplicates, "exported_point_event_spans": event_count,
        "exported_model_interval_spans": len(runs) - event_count,
        "observed_model_intervals_before_export_limit": interval_count,
        "export_limit_omitted_spans": omitted, "skipped_event_counts": dict(sorted(skipped.items())),
        "complete_provider_visibility": False, "provider_turn_is_caller_turn": False,
        "speech_start_stop_pairs_inferred": False, "gateway_or_tts_generation_join": "unavailable_without_explicit_shared_wire_id",
        "audible_interruption_count": None, "late_phone_packet_discard_verified": False,
        "interpretation": "Provider-reported point events and explicit model intervals only. Cleanup abort labels do not establish failed responses; pipeline cycles are not caller utterances. No audio pairing, causal parentage, phone playback or late packet discard is inferred."}}
