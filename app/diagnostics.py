"""Bounded component evidence, independent from scheduling and network export.

All disk writes run on a separate thread and a separate SQLite file. The ledger
contains schema descriptions, IDs, fixed codes and measurements, never messages,
tool arguments, credentials or raw exceptions. An observed event is not proof
that a vendor's hidden implementation or the caller's audio was correct.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from contextlib import closing
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import queue
import re
import sqlite3
import threading
import time
from uuid import UUID, uuid4

from .observability import _generation_fields

VERSION = "component-diagnostics-v1"
REVIEW_DIAGNOSTICS_REVISION = "call-diagnostic-sources-v1"
# One row per provider webhook delivery; dropped first when a read overflows.
NOISE_EVENTS = frozenset({("webhook", "http_finished"), ("webhook", "call_bound")})
MAX_LEDGER_ROWS = 20_000
_HASH = re.compile(r"^[a-f0-9]{64}$")
_ID = re.compile(r"^(?:[a-f0-9]{32}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})$")
_TURN = re.compile(r"^turn_[a-f0-9]{24}$")
_OPERATION = re.compile(r"^op_[a-f0-9]{24}$")


def _component(owner, entering, leaving, failures, observation, path):
    return {"owner": owner, "input": entering, "output": leaving,
            "failure_modes": failures, "observation": observation, "path": path}


COMPONENTS = {
    "twilio_bridge": _component("Twilio isolated lane", "Authenticated conference lifecycle", "Owned caller and SIP legs", ["join_failed", "cleanup_pending"], "Signed callbacks and REST terminal readbacks; Vapi end observed separately", "audio"),
    "listening_cue": _component("ServiceVoice Jev and Twilio", "Fresh partial and observed speaker state", "Caller participant announcement", ["stale_partial", "provider_unavailable", "speaker_priority"], "Selection, dispatch and clip fetch; acoustic playback and overlap require captured audio", "audio"),
    "caller_device": _component("Caller device and carrier", "Microphone sound frames", "Received and played sound", ["muted_microphone", "packet_loss", "playback_failure"], "Controlled endpoint audio capture; not visible to this backend", "audio"),
    "voice_runtime": _component("Vapi and carrier", "Phone or browser audio session", "Audio transport, call events and transcripts", ["connection_failure", "call_drop", "transport_delay"], "Vapi call status, end reason and provider timing", "audio"),
    "stt": _component("Configured speech recognition provider via Vapi", "Caller audio", "Transcript events", ["misrecognized_words", "misrecognized_digits", "missing_speech"], "Provider transcript; independent audio comparison needed for accuracy", "audio"),
    "endpointing": _component("Vapi voice runtime", "Speech and silence boundaries", "Turn-end or interruption signals", ["premature_turn_end", "missed_interruption", "long_silence"], "Provider stage timing; controlled audio test for correctness", "audio"),
    "gateway": _component("ServiceVoice", "Authenticated call and conversation envelope", "Validated streaming model request or guarded narration", ["authentication_rejected", "capacity_rejected", "invalid_history", "superseded_generation"], "Gateway request event, phase timings and final outcome", "decision"),
    "llm": _component("Google Gemini through ServiceVoice", "Validated prompt, history and tool schemas", "Text fragments or proposed tool calls", ["upstream_timeout", "invalid_tool_call", "unsupported_claim", "incomplete_task"], "Request timings, token counts and post-call evidence review; no hidden reasoning capture", "decision"),
    "faq_router": _component("ServiceVoice and TypeSafe", "Eligible static-information question", "Classified label or model fallback", ["classifier_timeout", "unknown_label", "incorrect_route"], "Eligibility/fallback code, timing and route outcome", "decision"),
    "receipt_renderer": _component("ServiceVoice", "Verified committed tool receipt", "Approved spoken readback text", ["stale_receipt", "receipt_mismatch", "unsafe_display"], "Verification code and source/output hashes; audio requires separate evidence", "decision"),
    "webhook": _component("ServiceVoice; Vapi delivers events", "Authenticated HTTP event envelope", "HTTP acknowledgement or tool results", ["authentication_rejected", "invalid_event", "deadline_exceeded", "event_delivery_failure"], "HTTP status, authenticated event kind, timing and correlation IDs", "business"),
    "tool_harness": _component("ServiceVoice", "Authorized normalized tool request", "Pending, unknown, rejected or verified receipt", ["overloaded", "operation_pending", "operation_unknown", "idempotency_conflict"], "Operation ID, tool outcome and receipt-present flag", "business"),
    "scheduler": _component("ServiceVoice SQLite demo scheduler", "Validated session-scoped read/write request", "Committed business result and receipt", ["slot_unavailable", "write_lock", "commit_without_response", "missing_receipt"], "Durable operation journal, committed result and post-call state reconciliation", "business"),
    "tts": _component("Configured speech synthesis provider via Vapi", "Approved response text", "Synthesized audio", ["synthesis_timeout", "quota_exhausted", "invalid_audio", "pronunciation_error"], "Provider status/timing or custom synthesis events; playback correctness needs audio", "audio"),
    "evaluator": _component("ServiceVoice reviewer; Gemini and LangSmith", "Final transcript, trusted facts and scheduler evidence", "Advisory grades, component coverage and numeric timing", ["missing_evidence", "judge_unavailable", "budget_exhausted", "export_failure"], "Review status, model attempts, queue lag, grade evidence and export readback", "evaluation"),
}

EVENTS = frozenset({"backchannel.selected", "backchannel.emitted", "backchannel.suppressed", "backchannel.refunded", "tts.cache_hit", "request", "response", "admission", "validation", "generation", "classification", "narration", "execution", "commit", "status", "transcript", "call_started", "call_ended", "synthesis", "review", "export", "reconciliation",
    "request_finished", "generation_finished", "classification_finished", "render_finished", "http_finished", "final_transcript_received", "call_bound", "execution_finished", "receipt_observed",
    "http_response_headers", "tts.request_started", "tts.upstream_connected", "tts.first_audio_ready", "tts.audio_stream_started",
    "tts.started", "tts.first_audio", "tts.completed", "tts.failed", "tts.cancelled", "tts.request", "tts.response", "tts.error"})
EVENTS |= frozenset({"decision", "suppressed", "dispatched", "clip_fetched", "clip_silenced", "announcement_end", "announcement_fail", "call_admitted", "cleanup_verified", "cleanup_pending"})
OUTCOMES = frozenset({"started", "completed", "success", "failure", "rejected", "failed", "pending", "unknown", "unavailable", "cancelled", "superseded", "interrupted", "observed", "not_applicable"})
EVENTS |= frozenset({"background_failure", "dial_failed", "listening_unavailable"})
_ENUM_ATTRIBUTES = {
    "backchannel_language": frozenset({"cache_compatible","not_compatible","uncertain"}),
    "backchannel_phrase": frozenset({"silence","neutral_ack","acknowledge","moment","one_sec","just_a_sec","let_me_see","checking","taking_a_look"}),
    "backchannel_content_kind": frozenset({"information_question","scheduling_request","ordinary_detail","factual_correction","other"}),
    "backchannel_reason": frozenset({"emitted","primary_ready","policy_not_ready","context_unavailable","turn_already_acknowledged","cooldown","call_budget","budget_capacity","not_eligible","sensitive_context","sensitive_utterance","sensitive_prior_turn","cached_language_unverified","short_or_ambiguous","urgent_or_safety","untrusted_control_content","classifier_timeout","classifier_unavailable","low_confidence","selected","silence","invalid_context","turn_not_allowed","invalid_messages","not_final_user_turn","input_bounds","prior_context_bounds","credential_missing","classification_capacity","provider_unavailable","invalid_response","response_bounds","model_mismatch","policy_closed","classification_deadline","classifier_silence","classification_uncertain","eligible_classification","classification_unavailable"}),
    "synthesis_source": frozenset({"pregenerated_cache","live_google"}),
    "error_code": frozenset({"none", "unknown", "invalid_arguments", "unauthorized", "authentication_rejected", "capacity_rejected", "invalid_history", "invalid_event", "deadline_exceeded", "timeout", "upstream_unavailable", "state_unavailable", "operation_pending", "operation_unknown", "operation_missing", "operation_failed", "idempotency_conflict", "harness_overloaded", "harness_closed", "session_ended", "slot_unavailable", "receipt_missing", "receipt_mismatch", "validation_error", "internal_error", "cancelled", "waiter_interrupted", "budget_exhausted", "missing_evidence", "export_failure", "quota_exhausted", "invalid_audio", "transport_error",
        "tts_resampler_unavailable", "tts_resampling_failed", "tts_upstream_missing_sample_rate", "tts_upstream_wrong_channel_count", "tts_upstream_sample_rate_changed", "tts_upstream_audio_after_completion",
        "classifier_timeout", "classifier_unavailable", "classifier_rate_limited", "classification_deadline",
        "tts_unavailable",
        "tts_invalid_message", "tts_invalid_text", "tts_unsupported_sample_rate", "tts_upstream_event_too_large", "tts_upstream_invalid_sse", "tts_upstream_invalid_json", "tts_upstream_error_event", "tts_upstream_blocked", "tts_upstream_invalid_audio", "tts_upstream_incomplete_generation", "tts_upstream_unexpected_text", "tts_upstream_wrong_audio_format", "tts_upstream_wrong_sample_rate", "tts_audio_limit_exceeded", "tts_upstream_unexpected_wav_header", "tts_upstream_incomplete_pcm_sample", "tts_upstream_empty_audio", "tts_invalid_configuration", "tts_not_configured", "tts_capacity_exhausted", "tts_transport_closed", "tts_deadline_exceeded", "tts_upstream_connection_failed", "tts_upstream_rate_limited", "tts_upstream_rejected", "tts_upstream_wrong_content_type", "tts_client_cancelled"}),
    "route": frozenset({"gemini", "deterministic_receipt", "jev_static_faq"}),
    "event_type": frozenset({"tool-calls", "transcript", "status-update", "end-of-call-report", "assistant-request", "speech-update", "unknown"}),
    "speaker": frozenset({"user", "assistant", "unknown"}),
    "transcript_role": frozenset({"user", "assistant", "unknown"}),
    "turn_basis": frozenset({"user_message_ordinal", "provider_event_id", "unknown"}),
    "evidence_basis": frozenset({"server_observed", "provider_reported", "post_call_inferred", "unavailable"}),
    "evidence_source": frozenset({"server_observed", "provider_reported", "post_call_inferred", "unavailable", "local", "gateway", "webhook", "tool_receipt", "custom_tts", "local_runtime", "server_http_headers", "verified_session_binding", "verified_phone_ingress", "provider_transcript_only", "provider_terminal_event"}),
    "failure_phase": frozenset({"authentication", "admission", "preflight", "request", "connect", "upstream", "headers", "body", "generation", "stream", "validation", "synthesis", "audio_decode", "codec", "export", "storage", "unknown", "before_first_audio", "after_first_audio"}),
    "model": frozenset({"gemini-3.8-flash", "gemini-3.8-flash-lite-tts", "gemini-3.8-flash-tts", "gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts", "gemini-2.5-pro-preview-tts", "gemini-2.5-flash-tts", "gemini-2.5-pro-tts", "jev-1.13.0"}),
    "voice": frozenset("Zephyr Puck Charon Kore Fenrir Leda Orus Aoede Callirrhoe Autonoe Enceladus Iapetus Umbriel Algieba Despina Erinome Algenib Rasalgethi Laomedeia Achernar Alnilam Schedar Gacrux Pulcherrima Achird Zubenelgenubi Vindemiatrix Sadachbia Sadaltager Sulafat".split()),
    "provider": frozenset({"google", "vapi", "soniox", "typesafe"}),
    "provider_call_id_status": frozenset({"provider_reported", "not_provided"}),
    "turn_id_status": frozenset({"not_provided", "user_message_ordinal", "provider_reported"}),
    "observation_scope": frozenset({"server_bytes_only_not_audible_playback"}),
    "resampler": frozenset({"soxr_hq", "none"}),
    "tool_name": frozenset({"get_dealership_info", "begin_service_request", "search_availability", "prepare_first_available_booking", "prepare_booking", "confirm_action", "dismiss_proposal", "get_appointment", "get_session_context", "get_operation", "prepare_reschedule", "prepare_cancellation", "request_handoff", "withdraw_handoff", "unknown_tool"}),
}
_ENUM_ATTRIBUTES["backchannel_reason"] |= frozenset({"scheduler_changed", "private_detail", "private_prior_turn", "caller_still_formulating"})
_ENUM_ATTRIBUTES["backchannel_reason"] |= frozenset({"speaker_state_unavailable", "caller_speaking",
    "assistant_speaking", "caller_end_unobserved", "caller_pause_too_short", "caller_resumed", "listening_clip_pending"})
# A cue Vapi discarded with a cancelled or replaced request is refunded; the
# refund cause is recorded beside the original emission reason, never over it.
_ENUM_ATTRIBUTES["backchannel_refund_reason"] = frozenset({"generation_cancelled_before_answer", "generation_superseded_before_answer"})
_ENUM_ATTRIBUTES["backchannel_reason"] |= _ENUM_ATTRIBUTES["backchannel_refund_reason"]
_ENUM_ATTRIBUTES["listening_reason"] = frozenset({"approved", "accepted_unverified", "twilio_legs_only",
    "sensitive_context", "input_bounds", "language_unverified", "control_content", "sensitive_content",
    "private_content", "sensitive_prior_turn", "question", "policy_unavailable", "provider_unavailable",
    "invalid_response", "classifier_silence", "speaker_priority", "speech_too_short", "stale_partial",
    "insufficient_words", "cue_budget", "cue_cooldown", "superseded", "context_changed", "unavailable"})
_ENUM_ATTRIBUTES["listening_reason"] |= frozenset({"capacity", "silent_refusal"})
EVENTS |= frozenset({"answer_released"})
_ENUM_ATTRIBUTES["listening_reason"] |= frozenset({"fetched_clip_active", "not_dispatched"})
EVENTS |= frozenset({"clip_refusal_alert"})
_ENUM_ATTRIBUTES["listening_reason"] |= frozenset({"prefer_pause", "excess_clip_refusals", "recovery_disabled",
    "clip_unavailable", "invalid_selection", "bridge_unavailable", "ticket_missing", "binding_mismatch",
    "ticket_expired", "no_neutral_clip", "clip_internal_error", "lane_unavailable"})
_ENUM_ATTRIBUTES["model"] |= frozenset({"gemini-3.5-flash-lite"})
_ENUM_ATTRIBUTES["backchannel_phrase"] |= frozenset({f"{name}_{take}" for name in ("uh_huh", "yeah", "okay", "right") for take in (1, 2)})
EVENTS |= frozenset({"caller_words_observed", "media_suspected", "media_recovery_started", "media_recovery_aborted",
    "media_recovery_failed", "media_recovery_redial", "speaker_started", "speaker_stopped"})
_ENUM_ATTRIBUTES["listening_reason"] |= frozenset({"caller_energy_without_words", "opening_only", "words_resumed", "caller", "agent"})
_ENUM_ATTRIBUTES["backchannel_reason"] |= frozenset({"fallback_turn_unverified"})
_COUNTS = frozenset({"classifier_requests","cached_sample_rate","input_bytes", "output_bytes", "input_messages", "input_characters", "output_chunks", "tool_calls", "user_ordinal", "http_status", "upstream_http_status", "attempt", "input_tokens", "output_tokens", "total_tokens", "sample_rate_ppm", "sample_rate", "upstream_sample_rate", "response_audio_bytes", "audio_bytes", "caller_utterances", "assistant_utterances"})
_FLAGS = frozenset({"backchannel_refunded","backchannel_follow_up","cache_text_normalized","cache_hit","ok", "receipt_present", "admitted", "authenticated", "partial", "replayed", "terminal", "audio_available", "transcript_final", "resampled", "response_started", "response_complete"})
_FLAGS |= frozenset({"backchannel_guarded_fallback"})
_DURATIONS = frozenset({"policy_elapsed_ms","classifier_ms","queue_ms", "first_byte_ms", "first_text_ms", "first_audio_ms", "upstream_ms", "commit_ms", "preflight_ms", "audio_duration_ms"})
_HASHES = frozenset({"cache_asset_id","cache_request_sha256","cache_manifest_sha256","input_sha256", "output_sha256", "prompt_sha256", "receipt_sha256", "provider_config_sha256", "source_result_sha256", "output_text_sha256"})


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def call_key(call_id):
    if type(call_id) is not str or not 1 <= len(call_id) <= 512 or any(ord(c) < 32 for c in call_id):
        return None
    return hashlib.sha256(call_id.encode()).hexdigest()


def turn_identifier(call_id, user_ordinal):
    """Stable request-history ordinal, not an independently measured audio turn."""
    key = call_key(call_id)
    if key is None or type(user_ordinal) is not int or not 1 <= user_ordinal <= 100_000:
        return None
    return "turn_" + hashlib.sha256((key+":"+str(user_ordinal)).encode()).hexdigest()[:24]


def operation_identifier(session_id, tool_call_id):
    if any(type(v) is not str or not 1 <= len(v) <= 512 for v in (session_id, tool_call_id)):
        return None
    return "op_" + hashlib.sha256((session_id+"\0"+tool_call_id).encode()).hexdigest()[:24]


def component_catalog():
    return deepcopy(COMPONENTS)


def safe_attributes(attributes):
    if not isinstance(attributes, Mapping):
        return {}
    result = {}
    for key in ("backchannel_probability","backchannel_confidence","backchannel_language_probability","backchannel_language_confidence","backchannel_content_probability","backchannel_content_confidence"):
        value=attributes.get(key)
        if type(value) in (int,float) and math.isfinite(value) and 0<=value<=1:result[key]=value
    for key, choices in _ENUM_ATTRIBUTES.items():
        value = attributes.get(key)
        if type(value) is str and value in choices:
            result[key] = value
    for key in _COUNTS:
        value = attributes.get(key)
        if type(value) is int and 0 <= value <= 100_000_000 and (key not in {"http_status", "upstream_http_status"} or 100 <= value <= 599):
            result[key] = value
    for key in _FLAGS:
        if type(attributes.get(key)) is bool:
            result[key] = attributes[key]
    for key in _DURATIONS:
        value = attributes.get(key)
        if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 86_400_000:
            result[key] = round(float(value), 3)
    for key in _HASHES:
        value = attributes.get(key)
        if type(value) is str and _HASH.fullmatch(value):
            result[key] = value
    result.update(_generation_fields(attributes))
    return result


def configuration_manifest(settings=None, source_root=None):
    """Hash owned source and allowlisted configuration; never hash a dotenv file.

    Provider state is not inferred from requested local settings. The actual
    published Vapi assistant and third-party internal state require independent
    observations, represented separately by evidence events.
    """
    settings = settings if isinstance(settings, Mapping) else {}
    root = Path(source_root) if source_root is not None else Path(__file__).parent
    source = {}
    for path in sorted(root.glob("*.py")):
        if path.is_file():
            source[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    if source_root is None:
        # Fixed owned entrypoints affect boundary/privacy behavior too. Never
        # enumerate deployment directories or read runtime environment files.
        for name in ("server.py", "tts_server.py"):
            path = root.parent / "deploy" / name
            if path.is_file():
                source["deploy/" + name] = hashlib.sha256(path.read_bytes()).hexdigest()
    known = {
        "SERVICEVOICE_ENVIRONMENT": {"local", "development", "staging", "production"},
        "SERVICEVOICE_CAPACITY_PROFILE": {"default", "ten-call", "ten-call-demo"},
        "GEMINI_TTS_MODEL": {"gemini-3.8-flash-lite-tts", "gemini-3.8-flash-tts"},
        "GEMINI_TTS_VOICE": set(_ENUM_ATTRIBUTES["voice"]),
    }
    defaults={"SERVICEVOICE_ENVIRONMENT":"local","SERVICEVOICE_CAPACITY_PROFILE":"default",
              "GEMINI_TTS_MODEL":"gemini-3.8-flash-lite-tts","GEMINI_TTS_VOICE":"Kore"}
    try:
        from .voice_profiles import MODEL_PROFILES, VOICE_PROFILES, TRANSCRIBER_PROFILES
        known.update(VAPI_MODEL_PROFILE=set(MODEL_PROFILES), VAPI_VOICE_PROFILE=set(VOICE_PROFILES), VAPI_TRANSCRIBER_PROFILE=set(TRANSCRIBER_PROFILES))
    except (ImportError, AttributeError):
        # A catalog import issue is visible as unknown capture, never raw text.
        pass
    values, issues = {}, []
    for key, choices in known.items():
        value = settings.get(key,defaults.get(key))
        if isinstance(value, str) and value in choices:
            values[key] = value
        elif value is not None:
            values[key] = "unknown"
            issues.append("unrecognized_" + key.lower())
    for key in ("SERVICEVOICE_RECEIPT_NARRATION", "SERVICEVOICE_JEV_FAQ", "SERVICEVOICE_TELEPHONY_ENABLED", "SERVICEVOICE_TIMING_PERSISTENCE", "SERVICEVOICE_NATIVE_END_CALL", "SERVICEVOICE_DIAGNOSTICS_ENABLED", "SERVICEVOICE_BACKCHANNELS"):
        value = settings.get(key)
        if value is not None:
            values[key] = value if isinstance(value,str) and value in {"on", "off"} else "unknown"
            if values[key] == "unknown": issues.append("unrecognized_" + key.lower())
    for key in ("SERVICEVOICE_MAX_LIVE_CALLS", "SERVICEVOICE_GATEWAY_CONCURRENCY", "SERVICEVOICE_PHONE_HTTP_CAPACITY"):
        value = settings.get(key)
        if value is not None:
            try: integer = int(value)
            except (ValueError, TypeError): integer = 0
            values[key] = integer if 1 <= integer <= 100 else "unknown"
            if values[key] == "unknown": issues.append("unrecognized_" + key.lower())
    record = {"schema_version": VERSION, "values": values, "source_sha256": hashlib.sha256(canonical(source).encode()).hexdigest(),
              "provenance": "local_requested_settings_and_owned_source", "provider_effective_configuration": "unknown", "capture_issues": issues}
    return {"id": hashlib.sha256(canonical(record).encode()).hexdigest(), **record}


def _event(config_id, component, event, outcome, *, call_id, turn_id=None, request_id=None, operation_id=None, start_unix=None, duration_ms=None, attributes=None):
    key = call_key(call_id)
    start = time.time() if start_unix is None else start_unix
    if (type(component) is not str or component not in COMPONENTS or type(event) is not str or event not in EVENTS or type(outcome) is not str or outcome not in OUTCOMES or key is None
            or type(start) not in (int, float) or not math.isfinite(start) or not 1577836800 <= start <= time.time()+5
            or turn_id is not None and (type(turn_id) is not str or not _TURN.fullmatch(turn_id))
            or request_id is not None and (type(request_id) is not str or not _ID.fullmatch(request_id))
            or operation_id is not None and (type(operation_id) is not str or not _OPERATION.fullmatch(operation_id))):
        return None
    if duration_ms is not None and (type(duration_ms) not in (int, float) or not math.isfinite(duration_ms) or not 0 <= duration_ms <= 86_400_000):
        return None
    return {"event_id": str(uuid4()), "config_id": config_id, "call_key": key,
            "component": component, "event": event, "outcome": outcome, "turn_id": turn_id,
            "request_id": request_id, "operation_id": operation_id, "start_unix": float(start),
            "duration_ms": round(float(duration_ms), 3) if duration_ms is not None else None,
            "attributes": safe_attributes(attributes)}


def _empty(reason, key=None):
    return {"schema_version": VERSION, "call_key": key, "configuration": None, "configurations": [], "events": [],
            "coverage": {"state": "unavailable", "complete": False, "event_count": 0, "dropped_events": None,
                         "truncated": False, "retention_pruned": False, "reason_codes": [reason]}}


def _project_event(row):
    """Revalidate persisted evidence; a malformed sidecar is not trusted code."""
    if not isinstance(row, dict) or type(row.get("config_id")) is not str or not _HASH.fullmatch(row["config_id"]) or type(row.get("call_key")) is not str or not _HASH.fullmatch(row["call_key"]):
        return None
    # Reuse validation without trying to reverse the call hash.
    projected = _event(row["config_id"], row.get("component"), row.get("event"), row.get("outcome"), call_id="validation",
        turn_id=row.get("turn_id"), request_id=row.get("request_id"), operation_id=row.get("operation_id"), start_unix=row.get("start_unix"), duration_ms=row.get("duration_ms"), attributes=row.get("attributes"))
    eid = row.get("event_id")
    if projected is None or type(eid) is not str or not _ID.fullmatch(eid): return None
    projected.update(event_id=eid, call_key=row["call_key"])
    return projected


def _project_manifest(record,cid):
    if not isinstance(record,dict) or record.get("id")!=cid or not _HASH.fullmatch(cid): return None
    expected=hashlib.sha256(canonical({k:v for k,v in record.items() if k!="id"}).encode()).hexdigest()
    if expected!=cid:return None
    # Re-project a potentially modified sidecar before its content reaches SaaS.
    raw_values=record.get("values",{})
    raw_values=raw_values if isinstance(raw_values,dict) else {}
    values={k:v for k,v in configuration_manifest(raw_values,source_root=Path(__file__).parent/"__no_sources__")["values"].items() if k in raw_values}
    source=record.get("source_sha256")
    if type(source) is not str or not _HASH.fullmatch(source):return None
    return {"id":cid,"schema_version":VERSION,"values":values,"source_sha256":source,
        "provenance":"local_requested_settings_and_owned_source","provider_effective_configuration":"unknown",
        "capture_issues":["configuration_capture_incomplete"] if record.get("capture_issues") else []}


def read_call_diagnostics(path, call_id, limit=1000):
    """Read-only export helper. Invoke outside a live event-loop response path."""
    key = call_key(call_id)
    if key is None or type(limit) is not int or not 1 <= limit <= 8000:
        return _empty("invalid_read_scope", key)
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro", uri=True, timeout=.05)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            # The ledger holds at most 20,000 rows in total; read this call's rows
            # completely so that a webhook flood can be dropped first on overflow.
            raw = db.execute("SELECT payload FROM diagnostic_events WHERE call_key=? ORDER BY started,event_id LIMIT ?", (key,MAX_LEDGER_ROWS+1)).fetchall()
            counts = db.execute("SELECT dropped,pruned FROM diagnostic_calls WHERE call_key=?", (key,)).fetchone()
            state = {k: json.loads(v) for k,v in db.execute("SELECT key,payload FROM diagnostic_state")}
            configs = {k: json.loads(v) for k,v in db.execute("SELECT config_id,payload FROM diagnostic_configs")}
        events = []
        invalid = 0
        for (payload,) in raw[:MAX_LEDGER_ROWS]:
            item = _project_event(json.loads(payload))
            if item is not None: events.append(item)
            else: invalid += 1
        omitted_noise = 0
        if len(events) > limit:
            kept = [row for row in events if (row["component"],row["event"]) not in NOISE_EVENTS]
            noise = [row for row in events if (row["component"],row["event"]) in NOISE_EVENTS]
            chosen = kept[:limit] if len(kept) >= limit else kept + noise[:limit-len(kept)]
            omitted_noise = len(noise) - sum(1 for row in chosen if (row["component"],row["event"]) in NOISE_EVENTS)
            events = sorted(chosen, key=lambda row: (row["start_unix"],row["event_id"]))
        if not events and counts is None:
            return _empty("no_call_evidence",key)
        configuration_ids = list(dict.fromkeys(e["config_id"] for e in events))
        manifests = []
        for cid in configuration_ids:
            record = configs.get(cid)
            projected=_project_manifest(record,cid)
            if projected is not None:manifests.append(projected)
            else:invalid+=1
        dropped, pruned = counts if counts is not None else (0,0)
        if type(dropped) is not int or not 0<=dropped<=100_000_000:
            dropped=None;invalid+=1
        if type(pruned) is not int or not 0<=pruned<=100_000_000:
            pruned=None;invalid+=1
        reasons = []
        if dropped: reasons.append("events_dropped")
        if pruned: reasons.append("retention_or_capacity_pruned")
        if len(raw)>limit: reasons.append("read_limit_exceeded")
        if invalid: reasons.append("invalid_persisted_evidence")
        if state.get("unclean_shutdowns",0): reasons.append("prior_unclean_shutdown_possible_loss")
        if state.get("unattributed_drops",0): reasons.append("unattributed_evidence_loss")
        if state.get("write_in_progress",False): reasons.append("writer_has_unflushed_events")
        last_write=state.get("last_write_unix")
        if type(last_write) not in (int,float) or not math.isfinite(last_write):last_write=None
        if state.get("writer_open",False) and (last_write is None or time.time()-last_write>180):reasons.append("diagnostic_writer_stale")
        return {"schema_version": VERSION, "call_key": key, "configuration": manifests[-1] if manifests else None,
            "configurations": manifests, "events": events,
            "coverage": {"state": "incomplete" if reasons else "observed", "complete": False,
                "event_count":len(events), "dropped_events": dropped, "truncated":len(raw)>limit,
                "omitted_webhook_delivery_events":omitted_noise,
                "retention_pruned":bool(pruned), "reason_codes":reasons,
                "writer_heartbeat_unix":last_write,
                "interpretation":"Observed application events only; not proof of complete vendor/audio visibility."}}
    except (OSError,sqlite3.Error,ValueError,TypeError,KeyError):
        return _empty("diagnostics_unavailable",key)


read_diagnostics = read_call_diagnostics


def read_review_diagnostics(source, call_id, diagnostics_path=None, limit=6000):
    """Read one call from the business ledger and a distinct configured ledger.

    Paths are trusted worker configuration, never event content, and are not
    exported. The fixed source roles describe where evidence was captured, not
    an inferred audio/generation relationship. A missing or damaged sibling
    cannot remove valid evidence. The union is capped at ``limit`` events (6,000
    by default, 8,000 at most). When it overflows, per-delivery webhook rows are
    dropped first so a long call keeps its gateway, tool, scheduler and speech
    evidence; the omitted count stays visible in coverage.
    """
    key=call_key(call_id)
    if key is None or type(limit) is not int or not 1 <= limit <= 8000:
        return {**_empty("invalid_read_scope",key),"coverage_revision":REVIEW_DIAGNOSTICS_REVISION,"sources":[]}
    business=Path(source).with_name(Path(source).stem+"-diagnostics.sqlite3")
    specs=[("business_runtime","scheduler_derived_sidecar",business)]
    if diagnostics_path is not None:
        specs.append(("configured_runtime","explicit_diagnostics_path",Path(diagnostics_path)))
    distinct={};sources=[];events={};event_sources={};conflicting_events=set();configs={};conflicting_configs=set()
    exact_duplicates=0;read_count=0
    for role,provenance,path in specs:
        try:
            resolved=path.resolve()
        except (OSError,ValueError,RuntimeError):
            resolved=None
        if resolved is not None and resolved in distinct:
            distinct[resolved]["also_configured"]=True
            continue
        try:
            snapshot=read_call_diagnostics(resolved,call_id,limit) if resolved is not None else _empty("diagnostics_unavailable",key)
        except (OSError,sqlite3.Error,ValueError,TypeError,KeyError):
            snapshot=_empty("diagnostics_unavailable",key)
        raw_rows=snapshot["events"]
        rows=[row for row in raw_rows if snapshot.get("call_key")==key and row.get("call_key")==key]
        rejected_scope=len(raw_rows)-len(rows)
        source_coverage=deepcopy(snapshot["coverage"])
        if rejected_scope:
            source_coverage.update(state="incomplete",complete=False,
                rejected_call_scope_events=rejected_scope,
                reason_codes=sorted(set(source_coverage["reason_codes"])|{"call_scope_mismatch"}))
        used_configs={row["config_id"] for row in rows}
        scoped_configs=[record for record in snapshot.get("configurations",[]) if record["id"] in used_configs]
        preferred=snapshot.get("configuration")
        if preferred is not None and preferred["id"] not in used_configs:preferred=None
        if preferred is None and scoped_configs:preferred=scoped_configs[-1]
        entry={"role":role,"provenance":provenance,"also_configured":role=="configured_runtime",
            "coverage":source_coverage,"configuration":preferred,
            "configurations":scoped_configs,"read_event_count":len(raw_rows),
            "scoped_event_count":len(rows),"rejected_call_scope_events":rejected_scope}
        sources.append(entry)
        if resolved is not None:distinct[resolved]=entry
        read_count+=len(raw_rows)
        for manifest in entry["configurations"]:
            cid=manifest["id"]
            if cid in configs and configs[cid]!=manifest:conflicting_configs.add(cid)
            else:configs[cid]=manifest
        for row in rows:
            eid=row["event_id"]
            if eid in events:
                if events[eid]!=row:conflicting_events.add(eid)
                else:exact_duplicates+=1
            else:events[eid]=row
            event_sources.setdefault(eid,set()).add(role)
    # Never choose one of two contradictory persisted identities as truth.
    for cid in conflicting_configs:configs.pop(cid,None)
    rejected_config_events={eid for eid,row in events.items() if row["config_id"] in conflicting_configs}
    for eid in conflicting_events|rejected_config_events:events.pop(eid,None)
    ordered=sorted(events.values(),key=lambda row:(row["start_unix"],row["event_id"]))
    omitted=max(0,len(ordered)-limit)
    if omitted:
        # Keep every non-noise event; webhook delivery rows yield first, then time order.
        kept=[row for row in ordered if (row["component"],row["event"]) not in NOISE_EVENTS]
        noise=[row for row in ordered if (row["component"],row["event"]) in NOISE_EVENTS]
        selected=sorted((kept+noise[:max(0,limit-len(kept))])[:limit] if len(kept)<=limit else kept[:limit],key=lambda row:(row["start_unix"],row["event_id"]))
    else:selected=ordered[:limit]
    for row in selected:
        row["source_roles"]=sorted(event_sources[row["event_id"]])
    for entry in sources:
        entry["exported_event_count"]=sum(entry["role"] in row["source_roles"] for row in selected)
        entry["configuration_ids"]=[record["id"] for record in entry["configurations"]]
        # A conflicting identity is retained as an ID and explicit loss code,
        # never as a selected supposedly authoritative configuration record.
        entry["configurations"]=[record for record in entry["configurations"] if record["id"] not in conflicting_configs]
        if entry["configuration"] and entry["configuration"]["id"] in conflicting_configs:entry["configuration"]=None
    reasons=set()
    states=[entry["coverage"]["state"] for entry in sources]
    if "unavailable" in states:reasons.add("diagnostic_source_unavailable")
    if "incomplete" in states:reasons.add("diagnostic_source_incomplete")
    if conflicting_events:reasons.add("conflicting_event_identity")
    if conflicting_configs:reasons.add("conflicting_configuration_identity")
    if omitted:reasons.add("combined_event_limit_exceeded")
    omitted_noise=sum(1 for row in ordered if (row["component"],row["event"]) in NOISE_EVENTS)-sum(1 for row in selected if (row["component"],row["event"]) in NOISE_EVENTS)
    for entry in sources:reasons.update(entry["coverage"]["reason_codes"])
    primary=sources[0].get("configuration") if sources else None
    dropped=[entry["coverage"].get("dropped_events") for entry in sources]
    state="observed" if not reasons else "incomplete" if any(value in {"observed","incomplete"} for value in states) else "unavailable"
    return {"schema_version":VERSION,"coverage_revision":REVIEW_DIAGNOSTICS_REVISION,"call_key":key,
        "configuration":primary,"configuration_scope":"business_runtime_only",
        "configurations":[configs[cid] for cid in sorted(configs)],"sources":sources,"events":selected,
        "coverage":{"state":state,"complete":False,"event_count":len(selected),"source_count":len(sources),
            "read_event_count":read_count,"exact_duplicate_events_ignored":exact_duplicates,
            "conflicting_event_id_count":len(conflicting_events),"conflicting_configuration_id_count":len(conflicting_configs),
            "rejected_call_scope_events":sum(entry["rejected_call_scope_events"] for entry in sources),
            "events_rejected_due_to_configuration_conflict":len(rejected_config_events),"union_events_omitted":omitted,
            "omitted_webhook_delivery_events":omitted_noise,"omission_order":"webhook_delivery_rows_first_then_chronological",
            "dropped_events":sum(dropped) if all(type(value) is int for value in dropped) else None,
            "truncated":bool(omitted or any(entry["coverage"].get("truncated") for entry in sources)),
            "retention_pruned":any(entry["coverage"].get("retention_pruned") for entry in sources),
            "reason_codes":sorted(reasons),
            "interpretation":"Independent call-scoped ledgers; primary configuration is business runtime only. Source roles and timestamps do not establish audio playback, TTS-to-generation lineage or causal joins."}}


def component_coverage(snapshot):
    events = snapshot.get("events",[]) if isinstance(snapshot,dict) else []
    result = {}
    for name,definition in COMPONENTS.items():
        rows = [r for r in events if r.get("component")==name]
        result[name] = {**deepcopy(definition), "evidence_status": "observed" if rows else "unavailable",
            "event_count":len(rows), "failed_event_count":sum(r.get("outcome") in {"failed","failure","rejected","unavailable"} for r in rows),
            "unknown_reason":None if rows else "no_component_events", "claims": ["Event outcome and measurement only; component correctness is not implied."] if rows else []}
    return {"state":"partial" if events else "unavailable", "complete":False, "components":result,
        "interpretation":"Vendor-reported timing and server observations do not prove caller audio, recognition or playback correctness."}


class NullDiagnostics:
    def __init__(self, reason="disabled"): self.reason = reason
    def record(self, component, event, outcome, **kwargs): return False
    def snapshot(self, call_id, limit=1000): return _empty(self.reason,call_key(call_id))
    def status(self): return {"enabled":False,"reason":self.reason,"recorded":0,"persisted":0,"queue_dropped":0,"storage_dropped":0,"filtered":0,"queued":0}
    def close(self, timeout_seconds=2.0): return True


class Diagnostics:
    def __init__(self,path,settings=None,*,queue_size=512,batch_size=32,max_rows=20_000,ttl_seconds=7*86400,source_root=None):
        if (not str(path) or type(queue_size) is not int or not 1 <= queue_size <= 1024
                or type(batch_size) is not int or not 1 <= batch_size <= min(queue_size,64)
                or type(max_rows) is not int or not 1 <= max_rows <= 100_000
                or type(ttl_seconds) not in (int,float) or not math.isfinite(ttl_seconds) or not 0 < ttl_seconds <= 30*86400):
            raise ValueError("invalid_diagnostics_configuration")
        self.path,self.manifest = Path(path),configuration_manifest(settings,source_root)
        self.batch_size,self.max_rows,self.ttl_seconds = batch_size,max_rows,float(ttl_seconds)
        self._queue,self._lock,self._closed = queue.Queue(maxsize=queue_size),threading.Lock(),threading.Event()
        self._stats = {k:0 for k in ("recorded","persisted","queue_dropped","storage_dropped","filtered","read_errors")}
        self._losses,self._loss_limit,self._unattributed,self._reason = {},4096,0,"initializing"
        self._worker=threading.Thread(target=self._work,name="servicevoice-diagnostics",daemon=True)
        self._worker.start()

    def _loss(self,key,kind,count=1):
        with self._lock:
            self._stats[kind] += count
            if key is not None and (key in self._losses or len(self._losses)<self._loss_limit):
                self._losses[key]=self._losses.get(key,0)+count
            else:self._unattributed+=count

    def record(self,component,event,outcome,*,call_id,turn_id=None,request_id=None,operation_id=None,start_unix=None,duration_ms=None,attributes=None):
        try:
            item=_event(self.manifest["id"],component,event,outcome,call_id=call_id,turn_id=turn_id,request_id=request_id,operation_id=operation_id,start_unix=start_unix,duration_ms=duration_ms,attributes=attributes)
            if item is None:
                self._loss(call_key(call_id),"filtered");return False
            with self._lock:
                if self._closed.is_set():
                    self._stats["filtered"]+=1
                    key=item["call_key"]
                    if key in self._losses or len(self._losses)<self._loss_limit:self._losses[key]=self._losses.get(key,0)+1
                    else:self._unattributed+=1
                    return False
                try:self._queue.put_nowait(item)
                except queue.Full:
                    # Attribute per-call loss without any synchronous disk I/O.
                    self._stats["queue_dropped"]+=1
                    key=item["call_key"]
                    if key in self._losses or len(self._losses)<self._loss_limit:self._losses[key]=self._losses.get(key,0)+1
                    else:self._unattributed+=1
                    return False
                self._stats["recorded"]+=1
                return True
        except Exception:
            self._loss(call_key(call_id),"filtered");return False

    def _connect(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        db=sqlite3.connect(self.path,timeout=.05)
        db.execute("PRAGMA busy_timeout=50")
        db.execute("PRAGMA journal_mode=DELETE")
        page=db.execute("PRAGMA page_size").fetchone()[0]
        if db.execute(f"PRAGMA max_page_count={64*1024**2//page}").fetchone()[0]>64*1024**2//page:raise ValueError("diagnostics_size_bound")
        db.executescript("""CREATE TABLE IF NOT EXISTS diagnostic_events(event_id TEXT PRIMARY KEY,call_key TEXT NOT NULL,started REAL NOT NULL,config_id TEXT NOT NULL,payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS diagnostic_event_call ON diagnostic_events(call_key,started);
CREATE INDEX IF NOT EXISTS diagnostic_event_order ON diagnostic_events(started,event_id);
CREATE TABLE IF NOT EXISTS diagnostic_configs(config_id TEXT PRIMARY KEY,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS diagnostic_calls(call_key TEXT PRIMARY KEY,updated REAL NOT NULL,dropped INTEGER NOT NULL DEFAULT 0,pruned INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS diagnostic_state(key TEXT PRIMARY KEY,payload TEXT NOT NULL);""")
        return db

    def _write(self,rows,*,initialize=False,terminal=False):
        with self._lock:
            losses,self._losses=self._losses,{}
            unattributed,self._unattributed=self._unattributed,0
        try:
            with closing(self._connect()) as db,db:
                def state(key,value):db.execute("INSERT OR REPLACE INTO diagnostic_state VALUES (?,?)",(key,canonical(value)))
                def prior(key,default=0):
                    row=db.execute("SELECT payload FROM diagnostic_state WHERE key=?",(key,)).fetchone()
                    return json.loads(row[0]) if row else default
                if initialize:
                    if prior("writer_open",False):state("unclean_shutdowns",prior("unclean_shutdowns")+1)
                    state("writer_open",True)
                db.execute("INSERT OR IGNORE INTO diagnostic_configs VALUES (?,?)",(self.manifest["id"],canonical(self.manifest)))
                now=time.time(); cutoff=now-self.ttl_seconds
                for key,count in losses.items():
                    db.execute("INSERT INTO diagnostic_calls VALUES (?,?,?,0) ON CONFLICT(call_key) DO UPDATE SET updated=excluded.updated,dropped=dropped+excluded.dropped",(key,now,count))
                if unattributed:state("unattributed_drops",prior("unattributed_drops")+unattributed)
                for row in rows:
                    db.execute("INSERT OR IGNORE INTO diagnostic_events VALUES (?,?,?,?,?)",(row["event_id"],row["call_key"],row["start_unix"],row["config_id"],canonical(row)))
                    db.execute("INSERT INTO diagnostic_calls VALUES (?,?,0,0) ON CONFLICT(call_key) DO UPDATE SET updated=excluded.updated",(row["call_key"],now))
                doomed=dict(db.execute("SELECT call_key,count(*) FROM diagnostic_events WHERE started<? GROUP BY call_key",(cutoff,)).fetchall())
                db.execute("DELETE FROM diagnostic_events WHERE started<?",(cutoff,))
                over=db.execute("SELECT event_id,call_key FROM diagnostic_events ORDER BY started DESC,event_id DESC LIMIT -1 OFFSET ?",(self.max_rows,)).fetchall()
                for key,count in Counter(key for _,key in over).items():doomed[key]=doomed.get(key,0)+count
                db.executemany("DELETE FROM diagnostic_events WHERE event_id=?",[(eid,) for eid,_ in over])
                for key,count in doomed.items():db.execute("UPDATE diagnostic_calls SET pruned=pruned+?,updated=? WHERE call_key=?",(count,now,key))
                # Bound call tombstones and immutable config history as well.
                db.execute("DELETE FROM diagnostic_calls WHERE call_key NOT IN (SELECT call_key FROM diagnostic_events) AND updated<?",(cutoff,))
                db.execute("DELETE FROM diagnostic_calls WHERE call_key IN (SELECT call_key FROM diagnostic_calls ORDER BY updated DESC LIMIT -1 OFFSET 100000)")
                db.execute("DELETE FROM diagnostic_configs WHERE config_id NOT IN (SELECT DISTINCT config_id FROM diagnostic_events) AND config_id<>?",(self.manifest["id"],))
                state("writer_open",not terminal)
                state("last_write_unix",now)
            return len(rows)
        except Exception:
            with self._lock:
                for key,count in losses.items():
                    if key in self._losses or len(self._losses)<self._loss_limit:self._losses[key]=self._losses.get(key,0)+count
                    else:self._unattributed+=count
                self._unattributed+=unattributed
            raise

    def _work(self):
        try:
            self._write([],initialize=True)
            with self._lock:self._reason="ready"
        except Exception:
            with self._lock:self._reason="storage_unavailable"
        last_write=time.monotonic()
        while not self._closed.is_set() or not self._queue.empty():
            rows=[]
            try:rows.append(self._queue.get(timeout=.25))
            except queue.Empty:pass
            while len(rows)<self.batch_size:
                try:rows.append(self._queue.get_nowait())
                except queue.Empty:break
            with self._lock:has_losses=bool(self._losses or self._unattributed)
            if not rows and not has_losses and time.monotonic()-last_write<30:continue
            try:
                accepted=self._write(rows)
                last_write=time.monotonic()
                with self._lock:self._stats["persisted"]+=accepted;self._reason="ready"
            except Exception:
                for row in rows:self._loss(row["call_key"],"storage_dropped")
                with self._lock:self._reason="storage_unavailable"
            finally:
                for _ in rows:self._queue.task_done()
        try:self._write([],terminal=True)
        except Exception:pass

    def snapshot(self,call_id,limit=1000):
        value=read_call_diagnostics(self.path,call_id,limit)
        with self._lock:
            lost=self._losses.get(call_key(call_id),0)
            pending=self._queue.qsize()
            unattributed=self._unattributed
        if lost or pending or unattributed:
            coverage=value["coverage"]
            coverage["state"]="incomplete"
            if lost:
                coverage["dropped_events"]=(coverage["dropped_events"] or 0)+lost
                coverage["reason_codes"].append("events_dropped_pending_persistence")
            if pending:coverage["reason_codes"].append("writer_has_unflushed_events")
            if unattributed:coverage["reason_codes"].append("unattributed_evidence_loss")
        return value

    def status(self):
        with self._lock:return {"enabled":True,"reason":"closed" if self._closed.is_set() else self._reason,"schema_version":VERSION,"config_id":self.manifest["id"],"queued":self._queue.qsize(),"max_rows":self.max_rows,"ttl_seconds":self.ttl_seconds,**self._stats}

    def close(self,timeout_seconds=2.0):
        try: timeout=min(3.0,max(0.0,float(timeout_seconds)))
        except (ValueError,TypeError):timeout=0
        self._closed.set();self._worker.join(timeout)
        return not self._worker.is_alive()


def create_diagnostics(path,settings=None):
    values=settings if isinstance(settings,Mapping) else {}
    if values.get("SERVICEVOICE_DIAGNOSTICS_ENABLED","on") not in {"on","true","1"}:
        return NullDiagnostics("disabled")
    try:return Diagnostics(path,values)
    except Exception:return NullDiagnostics("initialization_failed")
