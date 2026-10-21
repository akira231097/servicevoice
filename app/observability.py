"""Opt-in, content-free LangSmith OTLP export outside the booking write path.

No dotenv loading, global tracer installation, automatic instrumentation or SDK
decorators. Each event is an independent root span, grouped by a keyed thread
pseudonym in LangSmith metadata. Project addressing remains distinct from caller
conversation grouping. The exporter worker reserves a persistent monthly span budget before
network I/O. This is a local allowance, not a guarantee about account billing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import closing
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import queue
import sqlite3
import threading
import time
from typing import Mapping
from uuid import UUID


OFFICIAL_ENDPOINTS = frozenset({
    "https://aws.api.smith.langchain.com",
    "https://api.smith.langchain.com",
    "https://eu.api.smith.langchain.com",
    "https://apac.api.smith.langchain.com",
})
EVENT_NAMES = frozenset({"gateway.request", "tool.execution", "eval.run", "eval.case", "setup.check"})
TOOLS = frozenset({
    "get_dealership_info", "begin_service_request", "search_availability",
    "prepare_first_available_booking", "prepare_booking", "confirm_action",
    "dismiss_proposal", "get_appointment", "get_session_context", "get_operation",
    "prepare_reschedule", "prepare_cancellation", "request_handoff", "withdraw_handoff", "unknown_tool",
})
ERROR_CODES = frozenset({
    "none", "unknown", "unknown_tool", "invalid_arguments", "idempotency_conflict",
    "unauthorized", "session_ended", "operation_failed", "operation_pending",
    "operation_unknown", "operation_missing", "status_unavailable", "harness_closed",
    "harness_overloaded", "handoff_withdrawn", "historical_proposal", "not_found",
    "proposal_not_found", "proposal_expired", "stale_proposal", "slot_unavailable",
    "capacity_unavailable", "invalid_slot", "invalid_date", "no_appointment",
    "invalid_service", "invalid_vehicle", "invalid_phone", "invalid_name",
    "validation_error", "timeout", "cancelled", "internal_error", "receipt_missing", "waiter_interrupted",
})
_ENUMS = {
    "route": frozenset({"gemini", "deterministic_receipt", "jev_static_faq"}),
    "outcome": frozenset({"completed", "interrupted", "rejected", "state_unavailable",
        "capacity_rejected", "generation_conflict", "cancelled", "stream_unavailable", "error",
        "upstream_unavailable", "superseded", "superseded_after_release", "supersession_wait_expired"}),
    "status": frozenset({"passed", "failed", "pending", "error", "incomplete", "not_run", "completed", "ready"}),
    "mode": frozenset({"model", "contracts", "validate", "smoke", "trace_smoke", "setup"}),
    "model": frozenset({"gemini-3.8-flash", "gemini-3.5-flash", "jev-gem"}),
    "tool_name": TOOLS,
    "error_code": ERROR_CODES,
}
_BOOLS = frozenset({"ok", "receipt_present", "admitted"})
_COUNTS = frozenset({
    "tool_calls", "text_chunks", "history_messages", "tcp_connects", "tls_handshakes",
    "history_tool_results", "upstream_events", "text_utf8_bytes", "eof_tail_bytes", "jev_requests",
    "check_count", "passed_checks", "failed_checks", "case_count", "pending_reviews",
    "request_count", "input_tokens", "output_tokens", "total_tokens",
    "cached_input_tokens", "reasoning_tokens",
})
_DURATIONS = frozenset({
    "duration_ms", "total_ms", "first_text_ms", "upstream_headers_ms", "first_upstream_event_ms",
    "first_tool_fragment_ms", "upstream_done_ms", "tool_emitted_ms", "preflight_ms",
    "tool_signature_persist_ms", "upstream_eof_drain_ms", "jev_route_ms", "jev_classification_ms",
    "first_upstream_text_ms", "validated_text_hold_ms",
})
_HASHES = frozenset({"prompt_sha256", "dataset_sha256", "source_sha256", "output_sha256"})
_SLUG = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,79}$")
_PROJECT = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
_HASH = re.compile(r"^[a-f0-9]{64}$")
_PROMPT = re.compile(r"^\d{4}-\d{2}-\d{2}-gemini-agentic-\d{1,3}$")
_NARRATOR = re.compile(r"^receipt-en-\d{4}-\d{2}-\d{2}-\d{1,3}$")
_RESPONSE_LIMIT_BYTES = 64 * 1024
_EXPORT_DEADLINE_SECONDS = 2.0

_GENERATION_UUID_KEYS = ("generation_id", "predecessor_generation_id", "superseded_by_generation_id")
_GENERATION_ENUMS = {
    "generation_sequence_scope": frozenset({"process_local_http_arrival_only"}),
    "predecessor_relation": frozenset({"queued_successor", "active_or_draining_owner"}),
    "supersession_cause": frozenset({"newer_local_http_arrival", "gateway_shutdown", "unknown", "predecessor_cleanup_wait_expired"}),
    "payload_release_scope": frozenset({"gateway_yield_not_provider_receipt_or_playback"}),
    "generation_terminal_state": _ENUMS["outcome"] | frozenset({
        "superseded_before_release", "downstream_disconnected", "cancelled_unknown_cause"}),
}
_GENERATION_TIMES = ("superseded_at_ms", "payload_released_at_ms", "downstream_disconnect_at_ms")
_SSE_RESPONSE_ID = re.compile(r"^chatcmpl-[a-f0-9]{32}$")


def _generation_fields(values):
    """Strict content-free local lineage, never inferred provider/TTS linkage."""
    safe={}
    for key in _GENERATION_UUID_KEYS:
        value=values.get(key)
        if type(value) is str:
            try:
                if str(UUID(value))==value:safe[key]=value
            except ValueError:pass
    for key,allowed in _GENERATION_ENUMS.items():
        value=values.get(key)
        if type(value) is str and value in allowed:safe[key]=value
    for key,minimum in (("generation_sequence",1),("checkpoint_rejections",0)):
        value=values.get(key)
        if type(value) is int and minimum<=value<=100_000_000:safe[key]=value
    for key in _GENERATION_TIMES:
        value=values.get(key)
        if type(value) in (int,float) and math.isfinite(value) and 0<=value<=86_400_000:
            safe[key]=round(float(value),3)
    for key in ("sse_response_id","neutral_sse_response_id"):
        value=values.get(key)
        if type(value) is str and _SSE_RESPONSE_ID.fullmatch(value):safe[key]=value
    return safe


@dataclass(frozen=True)
class TelemetryConfig:
    enabled: bool = False
    api_key: str = field(default="", repr=False)
    endpoint: str = "https://aws.api.smith.langchain.com"
    project: str = "servicevoice-development"
    workspace_id: str = ""
    environment: str = "local"
    sample_rate: float = 1.0
    monthly_span_limit: int = 1000
    budget_path: str = "work/runtime/langsmith-budget.sqlite3"
    config_error: bool = False

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "TelemetryConfig":
        """Parse only named settings. Never read files or reveal invalid values."""
        env = os.environ if environ is None else environ
        enabled = str(env.get("SERVICEVOICE_LANGSMITH_ENABLED", "false")).strip().lower()
        invalid = enabled not in {"true", "on", "1", "false", "off", "0", ""}
        try:
            sample_rate = float(env.get("SERVICEVOICE_LANGSMITH_SAMPLE_RATE", "1"))
            monthly = int(env.get("SERVICEVOICE_LANGSMITH_MONTHLY_SPAN_LIMIT", "1000"))
        except (ValueError, TypeError):
            sample_rate, monthly, invalid = 1.0, 1000, True
        return cls(
            enabled=enabled in {"true", "on", "1"},
            api_key=str(env.get("LANGSMITH_API_KEY", "")).strip(),
            endpoint=str(env.get("LANGSMITH_ENDPOINT", "https://aws.api.smith.langchain.com")),
            project=str(env.get("LANGSMITH_PROJECT", "servicevoice-development")),
            workspace_id=str(env.get("LANGSMITH_WORKSPACE_ID", "")),
            environment=str(env.get("SERVICEVOICE_ENVIRONMENT", "local")),
            sample_rate=sample_rate,
            monthly_span_limit=monthly,
            budget_path=str(env.get("SERVICEVOICE_LANGSMITH_BUDGET_PATH", "work/runtime/langsmith-budget.sqlite3")),
            config_error=invalid,
        )

    def reason(self) -> str:
        if self.config_error:
            return "configuration_error"
        if not self.enabled:
            return "disabled"
        if not self.api_key:
            return "missing_api_key"
        if (
            self.endpoint not in OFFICIAL_ENDPOINTS
            or not _PROJECT.fullmatch(self.project)
            or self.environment not in {"local", "development", "staging", "production"}
            or not math.isfinite(self.sample_rate)
            or not 0 <= self.sample_rate <= 1
            or type(self.monthly_span_limit) is not int
            or not 1 <= self.monthly_span_limit <= 1000
            or not 16 <= len(self.api_key) <= 512
            or any(ord(char) < 33 or ord(char) > 126 for char in self.api_key)
            or not self.budget_path
        ):
            return "configuration_error"
        if self.workspace_id:
            try:
                if str(UUID(self.workspace_id)) != self.workspace_id:
                    return "configuration_error"
            except (ValueError, TypeError, AttributeError):
                return "configuration_error"
        return "ready"


class NullTelemetry:
    """A disabled sink has no provider, worker, filesystem or network side effects."""
    def __init__(self, reason: str = "disabled"):
        self.reason = reason

    def record_event(self, name, attributes, *, correlation, start_unix=None, duration_ms=None) -> bool:
        return False

    def status(self) -> dict:
        return {"enabled": False, "reason": self.reason, "recorded": 0, "exported": 0,
            "export_failed": 0, "quota_dropped": 0, "filtered": 0, "sampled_out": 0}

    def close(self, timeout_seconds: float = 2.0) -> bool:
        return True


def safe_attributes(attributes: Mapping) -> dict:
    """Whitelist keys AND shapes. No prompts, arguments, values or raw errors."""
    if not isinstance(attributes, Mapping):
        return {}
    safe = {}
    for key, values in _ENUMS.items():
        value = attributes.get(key)
        if type(value) is str and value in values:
            safe[key] = value
    for key in _BOOLS:
        value = attributes.get(key)
        if type(value) is bool:
            safe[key] = value
    for key in _COUNTS:
        value = attributes.get(key)
        if type(value) is int and 0 <= value <= 100_000_000:
            safe[key] = value
    for key in _DURATIONS:
        value = attributes.get(key)
        if type(value) in {int, float} and math.isfinite(value) and 0 <= value <= 86_400_000:
            safe[key] = round(float(value), 3)
    for key in _HASHES:
        value = attributes.get(key)
        if type(value) is str and _HASH.fullmatch(value):
            safe[key] = value
    for key, pattern in (("case_id", _SLUG), ("prompt_version", _PROMPT), ("narrator_version", _NARRATOR)):
        value = attributes.get(key)
        if type(value) is str and pattern.fullmatch(value):
            safe[key] = value
    run_id = attributes.get("run_id")
    if type(run_id) is str:
        try:
            if str(UUID(run_id)) == run_id:
                safe["run_id"] = run_id
        except ValueError:
            pass
    safe.update(_generation_fields(attributes))
    return safe


class _Budget:
    """Disk I/O is confined to the BatchSpanProcessor export worker."""
    def __init__(self, path: str, limit: int):
        self.path, self.limit = path, limit

    def reserve(self, requested: int, now: float | None = None) -> int:
        month = time.strftime("%Y-%m", time.gmtime(time.time() if now is None else now))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=0.05)) as connection, connection:
            connection.execute("PRAGMA busy_timeout=50")
            connection.execute("CREATE TABLE IF NOT EXISTS telemetry_budget (month TEXT PRIMARY KEY, spans INTEGER NOT NULL CHECK(spans>=0))")
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute("SELECT spans FROM telemetry_budget WHERE month=?", (month,)).fetchone()
            used = current[0] if current else 0
            admitted = min(requested, max(0, self.limit - used))
            connection.execute("INSERT INTO telemetry_budget(month,spans) VALUES(?,?) ON CONFLICT(month) DO UPDATE SET spans=excluded.spans", (month, used + admitted))
            # Only one counter per UTC month; retain counters across clock skew.
            # No payloads or customer identifiers are persisted in this file.
            return admitted


class _ContentFreeOTLPExporter:
    """Pinned OTel protobuf encoding with a deliberately small HTTP boundary.

    The general OTLP exporter merges and may log ambient OTEL header settings.
    This adapter neither reads those settings nor retains HTTP response bodies
    after bounded protocol validation.
    There are no transport retries: failed batches still consume their allowance.
    A successful result means HTTP acceptance, not confirmed dashboard visibility.
    """
    def __init__(self, config: TelemetryConfig):
        import requests
        from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse
        self._encode_spans = encode_spans
        self._response_class = ExportTraceServiceResponse
        self._session = requests.Session()
        self._session.trust_env = False
        self._endpoint = config.endpoint + "/otel/v1/traces"
        self._headers = {"x-api-key": config.api_key, "langsmith-project": config.project,
            "content-type": "application/x-protobuf", "user-agent": "servicevoice-otel/1",
            "accept-encoding": "identity"}
        if config.workspace_id:
            self._headers["x-tenant-id"] = config.workspace_id
        self._certificate = requests.certs.where()

    def export(self, spans):
        from opentelemetry.sdk.trace.export import SpanExportResult
        try:
            payload = self._encode_spans(spans).SerializeToString()
            if len(payload) > 256 * 1024:
                return SpanExportResult.FAILURE
            deadline = time.monotonic() + _EXPORT_DEADLINE_SECONDS
            response = self._session.request("POST", self._endpoint,
                headers=self._headers, data=payload, timeout=(1.0, 0.5),
                allow_redirects=False, stream=True, verify=self._certificate)
            try:
                accepted = self._accepted_response(response, deadline)
            finally:
                response.close()
            return SpanExportResult.SUCCESS if accepted else SpanExportResult.FAILURE
        except Exception:
            # No raw exception, URL, body, reason or headers reach SDK logging.
            return SpanExportResult.FAILURE

    def _accepted_response(self, response, deadline: float) -> bool:
        """Validate OTLP acknowledgement without logging its diagnostic text.

        OTLP partial rejection also uses HTTP 200. Never report a whole batch as
        accepted when that acknowledgement reports rejected spans. Partial
        batches are not retried, and already reserved quota is retained.
        """
        if response.status_code != 200 or time.monotonic() > deadline:
            return False
        encoding = response.headers.get("Content-Encoding", "").strip().lower()
        if encoding not in {"", "identity"}:
            return False
        declared = response.headers.get("Content-Length")
        if declared is not None:
            if len(declared) > 12 or not declared.isdecimal() or int(declared) > _RESPONSE_LIMIT_BYTES:
                return False
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        body = bytearray()
        # Small reads let the absolute deadline be checked even for a server
        # sending one byte at a time below the per-read socket timeout. DNS is
        # not governed by Requests' socket timeout; shutdown wait remains bounded.
        for chunk in response.iter_content(chunk_size=1):
            if time.monotonic() > deadline:
                return False
            if not chunk:
                continue
            if content_type != "application/x-protobuf" or type(chunk) is not bytes:
                return False
            if len(body) + len(chunk) > _RESPONSE_LIMIT_BYTES:
                return False
            body.extend(chunk)
        if time.monotonic() > deadline:
            return False
        # Empty protobuf is the valid full-success acknowledgement, including
        # servers omitting Content-Type for that zero-length response.
        acknowledgement = self._response_class.FromString(bytes(body))
        if acknowledgement.HasField("partial_success"):
            return acknowledgement.partial_success.rejected_spans == 0
        return True

    def shutdown(self):
        self._session.close()

    def force_flush(self, timeout_millis=1000):
        return True


class Telemetry:
    """An explicitly constructed private provider with bounded best-effort export."""
    def __init__(self, config: TelemetryConfig, exporter=None):
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider, SpanLimits
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExportResult
        from opentelemetry.sdk.trace.sampling import ALWAYS_ON
        from opentelemetry.metrics import NoOpMeterProvider
        from opentelemetry.sdk.trace.id_generator import RandomIdGenerator

        self._lock = threading.Lock()
        self._stats = {"recorded": 0, "exported": 0, "export_failed": 0,
            "quota_dropped": 0, "filtered": 0, "sampled_out": 0, "budget_errors": 0}
        self._closed = False
        self._closer = None
        self._config = config
        self._key = hmac.digest(config.api_key.encode("ascii"), b"servicevoice-telemetry-pseudonym-v1", "sha256")
        self._budget = _Budget(config.budget_path, config.monthly_span_limit)
        if exporter is None:
            exporter = _ContentFreeOTLPExporter(config)

        owner = self

        class BudgetedExporter:
            def export(self, spans):
                try:
                    admitted = owner._budget.reserve(len(spans))
                except Exception:
                    owner._increment("budget_errors")
                    owner._increment("quota_dropped", len(spans))
                    return SpanExportResult.FAILURE
                owner._increment("quota_dropped", len(spans) - admitted)
                if not admitted:
                    return SpanExportResult.SUCCESS
                try:
                    result = exporter.export(spans[:admitted])
                except Exception:
                    result = SpanExportResult.FAILURE
                owner._increment("exported" if result == SpanExportResult.SUCCESS else "export_failed", admitted)
                return result

            def shutdown(self):
                try:
                    exporter.shutdown()
                except Exception:
                    owner._increment("export_failed")

            def force_flush(self, timeout_millis=1000):
                return True

        # Resource(...) deliberately avoids Resource.create() environment detectors.
        self._provider = TracerProvider(
            sampler=ALWAYS_ON, resource=Resource({"service.name": "servicevoice",
                "deployment.environment.name": config.environment}),
            shutdown_on_exit=False, id_generator=RandomIdGenerator(),
            span_limits=SpanLimits(max_attributes=96, max_events=0, max_links=0,
                max_attribute_length=128),
        )
        self._provider.add_span_processor(BatchSpanProcessor(
            BudgetedExporter(), max_queue_size=256, schedule_delay_millis=500,
            max_export_batch_size=32, export_timeout_millis=2500,
            meter_provider=NoOpMeterProvider(),
        ))
        self._tracer = self._provider.get_tracer("servicevoice.content_free", "1")

    def _increment(self, key: str, count: int = 1):
        with self._lock:
            self._stats[key] += count

    def record_event(self, name, attributes, *, correlation, start_unix=None, duration_ms=None) -> bool:
        """Enqueue a bounded span. Never performs disk/network I/O or raises to caller."""
        try:
            if self._closed or name not in EVENT_NAMES or type(correlation) is not str or not 1 <= len(correlation) <= 512:
                self._increment("filtered")
                return False
            digest = hmac.digest(self._key, correlation.encode("utf-8"), "sha256")
            if int.from_bytes(digest[:8], "big") / 2**64 >= self._config.sample_rate:
                self._increment("sampled_out")
                return False
            attributes = safe_attributes(attributes)
            model_route = name == "gateway.request" and attributes.get("route") == "gemini"
            if name == "gateway.request" and not model_route:
                # Receipt narration and static FAQ routing do not call Gemini.
                # Do not mix their durations/tokens into LLM latency or costs.
                for key in ("model", "input_tokens", "output_tokens", "total_tokens", "cached_input_tokens", "reasoning_tokens"):
                    attributes.pop(key, None)
            pseudonym = digest.hex()[:32]
            safe = {"langsmith.metadata." + key: value for key, value in attributes.items()}
            safe.update({"langsmith.span.kind": "llm" if model_route else "tool" if name == "tool.execution" else "chain",
                # LangSmith's reserved trace.session_id addresses a PROJECT,
                # not a caller conversation. A pseudonym there causes project
                # lookup failures. Metadata thread_id is the conversation key.
                "langsmith.metadata.thread_id": pseudonym,
                "langsmith.trace.session_name": self._config.project,
                "langsmith.metadata.capture_policy": "content-free-v1",
                "langsmith.metadata.environment": self._config.environment})
            if model_route and "model" in attributes:
                safe["gen_ai.request.model"] = attributes["model"]
                safe["gen_ai.system"] = "google"
            for key in ("input_tokens", "output_tokens", "total_tokens"):
                if key in attributes:
                    safe["gen_ai.usage." + key] = attributes[key]
            for key, otel_key in (("cached_input_tokens", "gen_ai.usage.cache_read.input_tokens"),
                                  ("reasoning_tokens", "gen_ai.usage.reasoning.output_tokens")):
                if key in attributes:
                    safe[otel_key] = attributes[key]
            now = time.time()
            start = now if start_unix is None else start_unix
            duration = 0.001 if duration_ms is None else duration_ms
            if type(start) not in {int, float} or not math.isfinite(start) or not 1577836800 <= start <= now + 5:
                self._increment("filtered")
                return False
            if type(duration) not in {int, float} or not math.isfinite(duration) or not 0 <= duration <= 86400000:
                self._increment("filtered")
                return False
            from opentelemetry.context import Context
            span = self._tracer.start_span(name, context=Context(), attributes=safe, start_time=int(start * 1e9))
            span.end(end_time=int((start + duration / 1000) * 1e9))
            self._increment("recorded")
            return True
        except Exception:
            self._increment("filtered")
            return False

    def status(self) -> dict:
        with self._lock:
            return {"enabled": True, "reason": "closed" if self._closed else "ready",
                "monthly_span_limit": self._config.monthly_span_limit,
                "sample_rate": self._config.sample_rate, "budget_scope": "shared-budget-file",
                **self._stats}

    def close(self, timeout_seconds: float = 2.0) -> bool:
        """Call off the event loop. Bounds caller wait even for a faulty exporter."""
        timeout = min(3.0, max(0.0, float(timeout_seconds)))
        with self._lock:
            self._closed = True
            if self._closer is None:
                def finish():
                    try:
                        self._provider.force_flush(timeout_millis=max(1, int(timeout * 900)))
                    finally:
                        self._provider.shutdown()
                self._closer = threading.Thread(target=finish, name="servicevoice-telemetry-close", daemon=True)
                self._closer.start()
            closer = self._closer
        closer.join(timeout)
        return not closer.is_alive()


def create_telemetry(config: TelemetryConfig, *, exporter=None):
    try:
        reason = config.reason()
    except Exception:
        return NullTelemetry("configuration_error")
    if reason != "ready":
        return NullTelemetry(reason)
    try:
        return Telemetry(config, exporter=exporter)
    except ImportError:
        return NullTelemetry("dependency_unavailable")
    except Exception:
        return NullTelemetry("initialization_failed")


# Durable local diagnostics use a companion database, never the booking writer.
# These are server-owned timing facts, not a general log or transcript store.
_TIMING_CODES = frozenset("""feature_disabled not_immediate_tool_result tool_batch_or_history_shape
unsupported_tool confirmation_not_asserted unsuccessful_result presentation_missing_or_changed
unsupported_language receipt_missing receipt_mismatch presentation_invalid inactive_session
durable_result_mismatch newer_result unresolved_operation stale_proposal proposal_mismatch
unsafe_display_text unsupported_phone_format appointment_changed handoff_changed verified_receipt
state_unavailable read_capacity read_deadline not_first_user_turn input_or_language_bounds
action_or_complex_request not_simple_faq_candidate eligible_text credential_missing router_closed
classifier_not_ready active_or_unverified_state state_changed unknown_label verified_static_faq
classification_deadline classifier_timeout classifier_transport_error classifier_model_mismatch
classifier_invalid_response classifier_response_bounds classifier_inconsistent_confidence
classifier_unavailable cancelled interrupted rejected capacity_rejected generation_conflict
superseded superseded_after_release supersession_wait_expired upstream_unavailable completed
stream_unavailable not_attempted budget_exhausted size_limit eof timeout read_error
active_or_uncertain_state non_ascii_language language_uncertain intent_uncertain candidate_only
availability_changed availability_incomplete issuance_context_missing issuance_context_changed""".split())
_TIMING_MILESTONES = frozenset({"preflight", "upstream_headers", "first_upstream_event",
    "first_upstream_text", "first_text", "first_tool_fragment", "upstream_done", "tool_emitted", "total"})
_TIMING_DURATIONS = frozenset({"tool_signature_persist", "upstream_eof_drain", "jev_route",
                              "jev_classification", "validated_text_hold"})
_TIMING_COUNTS = frozenset({"history_messages", "history_tool_results", "upstream_events", "text_chunks",
                          "text_utf8_bytes", "tool_calls", "tcp_connects", "tls_handshakes", "eof_tail_bytes", "jev_requests"})
_TIMING_USAGE = frozenset({"input_tokens", "output_tokens", "total_tokens", "cached_input_tokens", "reasoning_tokens"})


def safe_timing_decision_code(value):
    """Export a recorded fixed decision code; unknown/free text stays absent."""
    return value if type(value) is str and value in _TIMING_CODES else None


_SHORT_HASH = re.compile(r"^[a-f0-9]{16}$")
_REQUEST_ID = re.compile(r"^[a-f0-9]{32}$")
_FAQ_VERSION = re.compile(r"^jev-static-faq-\d{4}-\d{2}-\d{2}-\d{1,3}$")


def safe_backchannel_timing(cue):
    """Idempotent, content-free projection shared by disk and trace exports."""
    if type(cue) is not dict:
        return None
    from .diagnostics import safe_attributes
    fields = {"phrase_id":"backchannel_phrase", "reason":"backchannel_reason",
        "classification_ms":"classifier_ms", "emitted_ms":"first_text_ms",
        "probability":"backchannel_probability", "confidence":"backchannel_confidence",
        "requests":"classifier_requests", "policy_elapsed_ms":"policy_elapsed_ms",
        "language_choice":"backchannel_language", "language_probability":"backchannel_language_probability",
        "language_confidence":"backchannel_language_confidence"}
    attributes = safe_attributes({target:cue.get(source) for source,target in fields.items()})
    return {**{source:attributes[target] for source,target in fields.items() if target in attributes},
        "evidence_scope":"gateway_yield_not_audible_playback"}


def safe_timing_row(row):
    """A fixed projection; unknown data and unreported token counts stay absent/null."""
    if type(row) is not dict:
        return None
    request_id, call_key, started = row.get("request_id"), row.get("call_key"), row.get("started_at_unix")
    if (type(request_id) is not str or not _REQUEST_ID.fullmatch(request_id)
            or type(call_key) is not str or not _SHORT_HASH.fullmatch(call_key)
            or type(started) not in (int, float) or not math.isfinite(started)
            or not 1577836800 <= started <= time.time() + 5
            or type(row.get("route")) is not str or row["route"] not in _ENUMS["route"]
            or type(row.get("outcome")) is not str or row["outcome"] not in _ENUMS["outcome"]):
        return None
    safe = {"request_id": request_id, "call_key": call_key, "started_at_unix": started,
            "route": row["route"], "outcome": row["outcome"]}
    for key in ("narration_reason", "jev_reason", "eof_drain_outcome"):
        safe[key] = safe_timing_decision_code(row.get(key))
    for key in ("system_prompt_sha256", "source_result_sha256", "output_text_sha256"):
        value = row.get(key)
        safe[key] = value if type(value) is str and _HASH.fullmatch(value) else None
    for key, pattern in (("narrator_version", _NARRATOR), ("jev_faq_version", _FAQ_VERSION)):
        value = row.get(key)
        safe[key] = value if type(value) is str and pattern.fullmatch(value) else None
    source = row.get("output_text_source")
    safe["output_text_source"] = source if type(source) is str and source in {"verified_receipt", "static_faq"} else None
    safe["jev_model"] = row.get("jev_model") if row.get("jev_model") == "jev-1.13.0" else None
    for key in ("jev_probability", "jev_margin", "jev_language_probability"):
        value = row.get(key)
        safe[key] = value if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1 else None
    for field, keys in (("milestones_ms", _TIMING_MILESTONES), ("durations_ms", _TIMING_DURATIONS)):
        values = row.get(field)
        values = values if type(values) is dict else {}
        safe[field] = {}
        for key in sorted(keys):
            value = values.get(key)
            safe[field][key] = round(float(value), 3) if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 86_400_000 else None
    if safe["milestones_ms"]["total"] is None:
        return None
    for field, keys in (("counts", _TIMING_COUNTS), ("usage", _TIMING_USAGE)):
        values = row.get(field)
        values = values if type(values) is dict else {}
        safe[field] = {key: value if type(value := values.get(key)) is int and 0 <= value <= 100_000_000 else None for key in sorted(keys)}
    safe.update(_generation_fields(row))
    cue = safe_backchannel_timing(row.get("backchannel"))
    if cue is not None:
        safe["backchannel"] = cue
    return safe


class NullLocalTimingSink:
    def __init__(self, reason="disabled"):
        self.reason = reason

    def record(self, row):
        return False

    def snapshot(self, call_key=None, limit=256):
        return []

    def status(self):
        return {"enabled": False, "reason": self.reason, "recorded": 0, "persisted": 0,
                "queue_dropped": 0, "storage_dropped": 0, "filtered": 0, "read_errors": 0, "queued": 0}

    def close(self, timeout_seconds=2.0):
        return True


class LocalTimingSink:
    """Best-effort bounded metadata persistence, independent from business effects.

    record() only projects and enqueues. SQLite, pruning and directory creation
    run on one daemon worker. No network, logger, ambient instrumentation or key.
    snapshot() is an explicit disk read: callers must run it off the event loop.
    close() bounds the caller wait, not a pathological OS filesystem operation.
    """
    def __init__(self, path, *, queue_size=256, batch_size=32, max_rows=4096, ttl_seconds=7*86400):
        self.path = Path(path)
        if (not str(path) or type(queue_size) is not int or not 1 <= queue_size <= 256
                or type(batch_size) is not int or not 1 <= batch_size <= min(32, queue_size)
                or type(max_rows) is not int or not 1 <= max_rows <= 4096
                or type(ttl_seconds) not in (int, float) or not math.isfinite(ttl_seconds)
                or not 0 < ttl_seconds <= 7*86400):
            raise ValueError("invalid_local_timing_configuration")
        self.batch_size, self.max_rows, self.ttl_seconds = batch_size, max_rows, ttl_seconds
        self._queue = queue.Queue(maxsize=queue_size)
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._reason = "initializing"
        self._stats = {key: 0 for key in ("recorded", "persisted", "queue_dropped", "storage_dropped", "filtered", "read_errors")}
        self._worker = threading.Thread(target=self._work, name="servicevoice-local-timing", daemon=True)
        self._worker.start()

    def _increment(self, key, count=1):
        with self._lock:
            self._stats[key] += count

    def record(self, row):
        try:
            safe = safe_timing_row(row)
            if safe is None:
                self._increment("filtered")
                return False
            # Shared lock makes close-vs-enqueue deterministic; no I/O under it.
            with self._lock:
                if self._closed.is_set():
                    self._stats["filtered"] += 1
                    return False
                try:
                    self._queue.put_nowait(safe)
                except queue.Full:
                    self._stats["queue_dropped"] += 1
                    return False
                self._stats["recorded"] += 1
                return True
        except Exception:
            self._increment("filtered")
            return False

    def _write(self, rows):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - self.ttl_seconds
        with closing(sqlite3.connect(self.path, timeout=.025)) as connection, connection:
            connection.execute("PRAGMA busy_timeout=25")
            # A companion file only; rollback journaling prevents an unbounded
            # WAL when a diagnostic reader is held open. Hard DB cap64MiB.
            connection.execute("PRAGMA journal_mode=DELETE")
            page_size = connection.execute("PRAGMA page_size").fetchone()[0]
            maximum_pages = 64*1024**2 // page_size
            actual_pages = connection.execute(f"PRAGMA max_page_count={maximum_pages}").fetchone()[0]
            if actual_pages > maximum_pages:
                raise ValueError("timing_database_size_bound")
            connection.execute("CREATE TABLE IF NOT EXISTS gateway_timings (request_id TEXT PRIMARY KEY, call_key TEXT NOT NULL, started REAL NOT NULL, payload TEXT NOT NULL)")
            connection.execute("CREATE INDEX IF NOT EXISTS gateway_timing_call ON gateway_timings(call_key,started)")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM gateway_timings WHERE started<?", (cutoff,))
            accepted = 0
            for row in rows:
                if row["started_at_unix"] < cutoff:
                    continue
                payload = json.dumps(row, separators=(",", ":"), allow_nan=False)
                if len(payload.encode()) > 16*1024:
                    continue
                # Request IDs are immutable; replay never rewrites timing facts.
                cursor = connection.execute("INSERT OR IGNORE INTO gateway_timings VALUES (?,?,?,?)",
                    (row["request_id"], row["call_key"], row["started_at_unix"], payload))
                accepted += cursor.rowcount
            connection.execute("DELETE FROM gateway_timings WHERE request_id IN (SELECT request_id FROM gateway_timings ORDER BY started DESC,request_id DESC LIMIT -1 OFFSET ?)", (self.max_rows,))
        return accepted

    def _work(self):
        try:
            self._write([])
            with self._lock: self._reason = "ready"
        except Exception:
            with self._lock: self._reason = "storage_unavailable"
        last_prune = time.monotonic()
        while not self._closed.is_set() or not self._queue.empty():
            try:
                first = self._queue.get(timeout=.5)
            except queue.Empty:
                if time.monotonic() - last_prune >= 60:
                    try: self._write([])
                    except Exception:
                        with self._lock: self._reason = "storage_unavailable"
                    last_prune = time.monotonic()
                continue
            rows = [first]
            while len(rows) < self.batch_size:
                try: rows.append(self._queue.get_nowait())
                except queue.Empty: break
            try:
                count = self._write(rows)
                with self._lock:
                    self._stats["persisted"] += count
                    self._reason = "ready"
            except Exception:
                self._increment("storage_dropped", len(rows))
                with self._lock: self._reason = "storage_unavailable"
            finally:
                for _ in rows: self._queue.task_done()

    def snapshot(self, call_key=None, limit=256):
        if (call_key is not None and (type(call_key) is not str or not _SHORT_HASH.fullmatch(call_key))
                or type(limit) is not int or not 1 <= limit <= 256):
            return []
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri()+"?mode=ro", uri=True, timeout=.025)) as connection:
                cutoff = time.time() - self.ttl_seconds
                if call_key is None:
                    rows = connection.execute("SELECT payload FROM gateway_timings WHERE started>=? ORDER BY started DESC,request_id DESC LIMIT ?", (cutoff,limit)).fetchall()
                else:
                    rows = connection.execute("SELECT payload FROM gateway_timings WHERE started>=? AND call_key=? ORDER BY started DESC,request_id DESC LIMIT ?", (cutoff,call_key,limit)).fetchall()
            return [safe for (payload,) in reversed(rows) if (safe := safe_timing_row(json.loads(payload))) is not None]
        except Exception:
            self._increment("read_errors")
            return []

    def status(self):
        with self._lock:
            return {"enabled": True, "reason": "closed" if self._closed.is_set() else self._reason,
                    "queued": self._queue.qsize(), "max_rows": self.max_rows,
                    "ttl_seconds": self.ttl_seconds, **self._stats}

    def close(self, timeout_seconds=2.0):
        try: timeout = min(2.0, max(0.0, float(timeout_seconds)))
        except (TypeError,ValueError): timeout = 0
        with self._lock: self._closed.set()
        self._worker.join(timeout)
        return not self._worker.is_alive()


def create_local_timing_sink(path):
    try:
        return LocalTimingSink(path)
    except Exception:
        return NullLocalTimingSink("initialization_failed")
