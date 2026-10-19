"""Real OTel pipeline tests with in-memory exporters; never contact LangSmith."""
from dataclasses import replace
import json
import sqlite3
import threading
import time

import pytest

from app.observability import (
    TelemetryConfig, NullTelemetry, create_telemetry, safe_attributes, _Budget,
)


def drain_telemetry(sink):
    """Wait for test-owned workers while their HTTP mocks remain installed.

    This is test teardown, not a change to the application's bounded close.
    The stalled-exporter test below still verifies its 10 ms caller budget.
    """
    stop = time.monotonic() + 10
    while time.monotonic() < stop:
        if sink.close(timeout_seconds=0.25):
            return True
    return False


@pytest.fixture(autouse=True)
def finish_test_telemetry_workers(monkeypatch):
    original = create_telemetry
    owned = []
    def tracked(*args, **kwargs):
        sink = original(*args, **kwargs)
        owned.append(sink)
        return sink
    monkeypatch.setitem(globals(), "create_telemetry", tracked)
    yield
    # Teardown precedes monkeypatch.undo, preventing late exports from using
    # the next parametrized case's mocked transport.
    for sink in owned:
        assert drain_telemetry(sink), "Test-owned telemetry worker did not stop"


def config(tmp_path, **changes):
    return replace(TelemetryConfig(enabled=True, api_key="test-secret-api-key-not-a-real-key",
        budget_path=str(tmp_path / "budget.sqlite3")), **changes)


def make_sink(tmp_path, **changes):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    exporter = InMemorySpanExporter()
    sink = create_telemetry(config(tmp_path, **changes), exporter=exporter)
    assert sink.status()["reason"] == "ready"
    return sink, exporter


@pytest.mark.parametrize("env,reason", [
    ({}, "disabled"),
    ({"SERVICEVOICE_LANGSMITH_ENABLED": "off", "LANGSMITH_API_KEY": "secret"}, "disabled"),
    ({"SERVICEVOICE_LANGSMITH_ENABLED": "on"}, "missing_api_key"),
    ({"SERVICEVOICE_LANGSMITH_ENABLED": "maybe"}, "configuration_error"),
])
def test_disabled_and_missing_key_have_no_side_effects(monkeypatch, env, reason):
    import builtins
    real_import = builtins.__import__
    def forbid_import(name, *args, **kwargs):
        assert not name.startswith("opentelemetry"), "Disabled sink imported an SDK"
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", forbid_import)
    monkeypatch.setattr(threading.Thread, "start", lambda *a, **k: pytest.fail("started a thread"))
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: pytest.fail("opened database"))
    sink = create_telemetry(TelemetryConfig.from_env(env))
    assert isinstance(sink, NullTelemetry)
    assert sink.status()["reason"] == reason
    assert not sink.record_event("setup.check", {}, correlation="example")
    assert drain_telemetry(sink)


@pytest.mark.parametrize("field,value", [
    ("endpoint", "http://api.smith.langchain.com"),
    ("endpoint", "https://api.smith.langchain.com.evil.example"),
    ("endpoint", "https://key@api.smith.langchain.com"),
    ("endpoint", "https://api.smith.langchain.com/otel"),
    ("endpoint", "https://api.smith.langchain.com?forward=private"),
    ("project", "customer@email.example"),
    ("workspace_id", "arbitrary-tenant"),
    ("environment", "customer-name"),
    ("sample_rate", float("nan")),
    ("sample_rate", 1.1),
    ("monthly_span_limit", 1001),
    ("monthly_span_limit", 0),
    ("api_key", "value\r\nx-injected: header"),
])
def test_invalid_configuration_fails_closed(tmp_path, field, value):
    sink = create_telemetry(config(tmp_path, **{field: value}))
    assert sink.status()["reason"] == "configuration_error"
    assert not (tmp_path / "budget.sqlite3").exists()


def test_config_repr_and_status_never_reveal_key(tmp_path):
    cfg = config(tmp_path)
    assert cfg.api_key not in repr(cfg)
    sink, _ = make_sink(tmp_path)
    assert cfg.api_key not in json.dumps(sink.status())
    sink.close()


def test_real_spans_have_only_safe_attributes_and_pseudonyms(tmp_path):
    from opentelemetry import trace
    before = trace.get_tracer_provider()
    sink, exporter = make_sink(tmp_path)
    assert trace.get_tracer_provider() is before
    secret = "customer-private-token+2025550146@example.com"
    sink.record_event("tool.execution", {
        "tool_name": "confirm_action", "ok": True, "error_code": "none",
        "receipt_present": True, "customer_name": secret, "phone": secret,
        "prompt": secret, "arguments": {"private": secret}, "exception": secret,
        "model": secret, "status": secret, "error": secret,
        "duration_ms": 42.25, "check_count": 3,
    }, correlation=secret)
    assert drain_telemetry(sink)
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "tool.execution"
    assert span.parent is None
    attrs = dict(span.attributes)
    assert attrs["langsmith.metadata.tool_name"] == "confirm_action"
    assert attrs["langsmith.metadata.ok"] is True
    assert attrs["langsmith.metadata.duration_ms"] == 42.25
    assert len(attrs["langsmith.metadata.thread_id"]) == 32
    assert "langsmith.trace.session_id" not in attrs
    assert attrs["langsmith.trace.session_name"] == "servicevoice-development"
    encoded = json.dumps(attrs)
    assert secret not in encoded
    assert config(tmp_path).api_key not in encoded
    assert not span.events
    assert not span.links
    assert sink.status()["exported"] == 1


def test_no_ambient_resource_or_parent_capture(tmp_path, monkeypatch):
    from opentelemetry import trace
    from opentelemetry.trace import SpanContext, NonRecordingSpan, TraceFlags
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "secret=private-user,service.name=wrong")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "private-customer")
    sink, exporter = make_sink(tmp_path)
    parent = NonRecordingSpan(SpanContext(17, 19, False, TraceFlags(1)))
    with trace.use_span(parent):
        sink.record_event("gateway.request", {"model": "gemini-3.8-flash"}, correlation="case")
    sink.close()
    span = exporter.get_finished_spans()[0]
    assert span.parent is None
    assert span.context.trace_id != 17
    assert dict(span.resource.attributes) == {
        "service.name": "servicevoice", "deployment.environment.name": "local"}


def test_sampling_is_consistent_for_correlated_events(tmp_path):
    sink, exporter = make_sink(tmp_path, sample_rate=0.5)
    decisions = {}
    for i in range(40):
        correlation = f"synthetic-session-{i}"
        decisions[i] = [sink.record_event(name, {}, correlation=correlation)
            for name in ("gateway.request", "tool.execution", "eval.case")]
    sink.close()
    assert all(len(set(items)) == 1 for items in decisions.values())
    assert any(items[0] for items in decisions.values())
    assert any(not items[0] for items in decisions.values())
    spans = exporter.get_finished_spans()
    assert len(spans) == sum(sum(items) for items in decisions.values())


def test_zero_sampling_never_opens_quota_file(tmp_path):
    sink, exporter = make_sink(tmp_path, sample_rate=0)
    assert not sink.record_event("setup.check", {}, correlation="synthetic")
    sink.close()
    assert exporter.get_finished_spans() == ()
    assert not (tmp_path / "budget.sqlite3").exists()


def test_persistent_quota_limits_combined_process_instances(tmp_path):
    first, exporter1 = make_sink(tmp_path, monthly_span_limit=3)
    for _ in range(5):
        assert first.record_event("setup.check", {}, correlation="case")
    first.close()
    second, exporter2 = make_sink(tmp_path, monthly_span_limit=3)
    second.record_event("setup.check", {}, correlation="case")
    second.close()
    assert len(exporter1.get_finished_spans()) == 3
    assert exporter2.get_finished_spans() == ()
    assert first.status()["quota_dropped"] == 2
    assert second.status()["quota_dropped"] == 1
    with sqlite3.connect(tmp_path / "budget.sqlite3") as connection:
        rows = connection.execute("SELECT month, spans FROM telemetry_budget").fetchall()
    assert rows == [(time.strftime("%Y-%m", time.gmtime()), 3)]


def test_budget_failure_prevents_export_without_touching_caller(tmp_path):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("fixed", encoding="utf-8")
    sink, exporter = make_sink(tmp_path, budget_path=str(blocked / "quota.db"))
    assert sink.record_event("setup.check", {}, correlation="case")
    assert drain_telemetry(sink)
    assert not exporter.get_finished_spans()
    assert sink.status()["budget_errors"] == 1
    assert sink.status()["quota_dropped"] == 1


def test_disk_budget_runs_off_recording_thread(tmp_path, monkeypatch):
    caller = threading.get_ident()
    actual = _Budget.reserve
    observed = []
    def reserve(self, count, now=None):
        observed.append(threading.get_ident())
        assert threading.get_ident() != caller
        return actual(self, count, now)
    monkeypatch.setattr(_Budget, "reserve", reserve)
    sink, _ = make_sink(tmp_path)
    sink.record_event("setup.check", {}, correlation="case")
    sink.close()
    assert observed


def test_failed_exports_consume_reserved_budget_and_never_raise(tmp_path):
    class Failure:
        def export(self, spans):
            raise RuntimeError("do not export raw private-error-message")
        def shutdown(self):
            raise RuntimeError("private-shutdown-message")
    sink = create_telemetry(config(tmp_path, monthly_span_limit=1), exporter=Failure())
    assert sink.record_event("setup.check", {}, correlation="case")
    assert drain_telemetry(sink)
    assert sink.status()["export_failed"] >= 1
    following, exporter = make_sink(tmp_path, monthly_span_limit=1)
    following.record_event("setup.check", {}, correlation="case")
    following.close()
    assert not exporter.get_finished_spans()


def test_stalled_exporter_cannot_block_recording_and_queue_is_bounded(tmp_path):
    started, release = threading.Event(), threading.Event()
    seen = []
    from opentelemetry.sdk.trace.export import SpanExportResult
    class Stalled:
        def export(self, spans):
            started.set()
            release.wait(5)
            seen.extend(spans)
            return SpanExportResult.SUCCESS
        def shutdown(self):
            return None
    sink = create_telemetry(config(tmp_path), exporter=Stalled())
    try:
        for _ in range(32):
            sink.record_event("setup.check", {}, correlation="case")
        assert started.wait(2)
        begin = time.monotonic()
        for _ in range(1000):
            sink.record_event("setup.check", {}, correlation="case")
        assert time.monotonic() - begin < 1.0
        begin = time.monotonic()
        assert not sink.close(timeout_seconds=0.01)
        assert time.monotonic() - begin < 0.2
    finally:
        release.set()
        sink.close(timeout_seconds=2)
    assert len(seen) <= 32 + 256


def test_all_unsupported_attributes_and_invalid_shapes_are_filtered():
    assert safe_attributes({
        "ok": 1, "tool_calls": True, "duration_ms": float("nan"),
        "first_text_ms": -1, "status": "phone2025550146", "case_id": "private@example.com",
        "run_id": "not-a-uuid", "prompt_sha256": "not-a-hash", "prompt_version": "api-secret",
        "raw": "sensitive", "model": "customer-name",
    }) == {}


def test_invalid_events_times_and_post_close_are_harmless(tmp_path):
    sink, exporter = make_sink(tmp_path)
    assert not sink.record_event("private-prompt", {}, correlation="case")
    assert not sink.record_event("setup.check", {}, correlation="")
    assert not sink.record_event("setup.check", {}, correlation="case", start_unix=float("inf"))
    assert not sink.record_event("setup.check", {}, correlation="case", duration_ms=-1)
    sink.close()
    assert not sink.record_event("setup.check", {}, correlation="case")
    assert not exporter.get_finished_spans()


@pytest.mark.parametrize("http_status,exported", [(200, 1), (302, 0), (401, 0)])
def test_real_otlp_serialization_has_fixed_endpoint_no_content_or_redirects(tmp_path, monkeypatch, http_status, exported, caplog):
    import requests
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest, ExportTraceServiceResponse
    observed = []
    sensitive = "private-response-and-customer-content"
    def request(self, *args, **kwargs):
        if args:
            kwargs["method"], kwargs["url"] = args
        observed.append(kwargs)
        assert self.trust_env is False
        response = requests.Response()
        response.status_code = http_status
        response.reason = sensitive
        acknowledgement = ExportTraceServiceResponse()
        acknowledgement.partial_success.error_message = sensitive
        response._content = acknowledgement.SerializeToString() if http_status == 200 else sensitive.encode()
        response._content_consumed = True
        response.headers["Content-Type"] = "application/x-protobuf"
        response.headers["Location"] = "https://evil.example/secret"
        return response
    monkeypatch.setattr(requests.Session, "request", request)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://evil.example")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "injected=private-header")
    sink = create_telemetry(config(tmp_path, workspace_id="8c185029-1999-4afe-8ada-e6ff36839982"))
    assert sink.status()["reason"] == "ready"
    sink.record_event("setup.check", {"status": "ready", "prompt": sensitive}, correlation=sensitive)
    assert drain_telemetry(sink)
    assert len(observed) == 1
    request = observed[0]
    assert request["url"] == "https://aws.api.smith.langchain.com/otel/v1/traces"
    assert request["allow_redirects"] is False
    assert request["stream"] is True
    assert request["timeout"] == (1.0, 0.5)
    assert request["headers"]["accept-encoding"] == "identity"
    assert request["headers"]["x-api-key"] == config(tmp_path).api_key
    assert request["headers"]["langsmith-project"] == "servicevoice-development"
    assert request["headers"]["x-tenant-id"] == "8c185029-1999-4afe-8ada-e6ff36839982"
    assert "injected" not in request["headers"]
    body = ExportTraceServiceRequest.FromString(request["data"])
    encoded = str(body)
    assert sensitive not in encoded
    assert config(tmp_path).api_key not in encoded
    assert "setup.check" in encoded
    assert sensitive not in caplog.text
    assert sink.status()["exported"] == exported
    assert sink.status()["export_failed"] == 1 - exported


def test_transport_exception_is_sanitized_before_sdk_logging(tmp_path, monkeypatch, caplog):
    import requests
    def request(*args, **kwargs):
        raise RuntimeError("private-error-with-api-token-and-customer")
    monkeypatch.setattr(requests.Session, "request", request)
    sink = create_telemetry(config(tmp_path))
    sink.record_event("setup.check", {}, correlation="case")
    assert drain_telemetry(sink)
    assert "private-error-with-api-token-and-customer" not in caplog.text
    assert not caplog.text
    assert sink.status()["export_failed"] == 1


def test_malformed_ambient_otel_secrets_are_neither_parsed_nor_logged(tmp_path, monkeypatch, caplog):
    import requests
    sensitive = "ambient-secret-header-that-is-not-valid-syntax"
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", sensitive)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", sensitive)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_CERTIFICATE", "not-a-real-private-file")
    observed = []
    def request(self, *args, **kwargs):
        observed.append(kwargs)
        assert sensitive not in json.dumps(kwargs["headers"])
        assert kwargs["verify"] != "not-a-real-private-file"
        response = requests.Response()
        response.status_code = 200
        response._content = b""
        response._content_consumed = True
        return response
    monkeypatch.setattr(requests.Session, "request", request)
    sink = create_telemetry(config(tmp_path))
    assert sink.record_event("setup.check", {}, correlation="case")
    assert drain_telemetry(sink)
    assert len(observed) == 1
    assert sensitive not in caplog.text
    assert sink.status()["exported"] == 1


def test_wrong_direct_config_types_fail_closed(tmp_path):
    for changes in ({"sample_rate": "not-a-number"}, {"endpoint": []}, {"project": 19}, {"monthly_span_limit": None}):
        sink = create_telemetry(config(tmp_path, **changes))
        assert isinstance(sink, NullTelemetry)
        assert sink.status()["reason"] == "configuration_error"


@pytest.mark.parametrize("route,kind", [
    ("gemini", "llm"), ("deterministic_receipt", "chain"), ("jev_static_faq", "chain"),
    (None, "chain"),
])
def test_fast_routes_do_not_impersonate_gemini_or_its_token_usage(tmp_path, route, kind):
    sink, exporter = make_sink(tmp_path)
    assert sink.record_event("gateway.request", {
        "route": route, "model": "gemini-3.8-flash", "input_tokens": 20,
        "output_tokens": 30, "total_tokens": 50, "first_text_ms": 37.25,
    }, correlation="synthetic-route")
    assert drain_telemetry(sink)
    attrs = dict(exporter.get_finished_spans()[0].attributes)
    assert attrs["langsmith.span.kind"] == kind
    assert attrs["langsmith.metadata.first_text_ms"] == 37.25
    if route == "gemini":
        assert attrs["gen_ai.request.model"] == "gemini-3.8-flash"
        assert attrs["gen_ai.system"] == "google"
        assert attrs["gen_ai.usage.total_tokens"] == 50
    else:
        assert "langsmith.metadata.model" not in attrs
        assert "langsmith.metadata.total_tokens" not in attrs
        assert not any(key.startswith("gen_ai.") for key in attrs)


@pytest.mark.parametrize("kind,expected", [
    ("empty", True), ("empty_without_type", True), ("warning", True),
    ("partial", False), ("negative_rejection", False), ("excess_rejection", False),
    ("malformed", False), ("wrong_type", False), ("missing_type", False),
    ("oversized", False), ("oversized_declared", False), ("invalid_length", False),
    ("compressed", False), ("non_200_success", False),
])
def test_otlp_acknowledgements_are_bounded_validated_and_never_retried(tmp_path, monkeypatch, caplog, kind, expected):
    import requests
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse
    sensitive = "private-provider-error-message-never-export-or-log"
    acknowledgement = ExportTraceServiceResponse()
    body = b""
    content_type = "application/x-protobuf"
    extra_headers = {}
    status = 200
    if kind in {"warning", "partial", "negative_rejection", "excess_rejection"}:
        acknowledgement.partial_success.error_message = sensitive
        acknowledgement.partial_success.rejected_spans = {
            "warning": 0, "partial": 1, "negative_rejection": -1, "excess_rejection": 9,
        }[kind]
        body = acknowledgement.SerializeToString()
    elif kind == "malformed":
        body = b"\xff"
    elif kind in {"wrong_type", "missing_type"}:
        body = b"{}"
        content_type = "application/json" if kind == "wrong_type" else ""
    elif kind == "empty_without_type":
        content_type = ""
    elif kind == "oversized":
        body = b"x" * (64 * 1024 + 1)
    elif kind == "oversized_declared":
        extra_headers["Content-Length"] = str(64 * 1024 + 1)
    elif kind == "invalid_length":
        extra_headers["Content-Length"] = "-1"
    elif kind == "compressed":
        extra_headers["Content-Encoding"] = "gzip"
    elif kind == "non_200_success":
        status = 204
    observed = []
    closed = []
    def request(self, *args, **kwargs):
        observed.append(kwargs)
        response = requests.Response()
        response.status_code = status
        response._content = body
        response._content_consumed = True
        if content_type:
            response.headers["Content-Type"] = content_type
        response.headers.update(extra_headers)
        response.close = lambda: closed.append(True)
        return response
    monkeypatch.setattr(requests.Session, "request", request)
    sink = create_telemetry(config(tmp_path))
    sink.record_event("setup.check", {}, correlation="case")
    assert drain_telemetry(sink)
    assert len(observed) == 1
    assert closed == [True]
    assert sink.status()["exported"] == int(expected)
    assert sink.status()["export_failed"] == int(not expected)
    assert sensitive not in caplog.text
    assert sensitive not in json.dumps(sink.status())
    # A rejected or malformed acknowledgement still consumes the reservation.
    with sqlite3.connect(tmp_path / "budget.sqlite3") as connection:
        assert connection.execute("SELECT SUM(spans) FROM telemetry_budget").fetchone()[0] == 1


def test_response_slow_drip_uses_small_reads_and_absolute_deadline(tmp_path, monkeypatch):
    import requests
    import app.observability as module
    monkeypatch.setattr(module, "_EXPORT_DEADLINE_SECONDS", 0.01)
    observed, closed = [], []
    def request(self, *args, **kwargs):
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "application/x-protobuf"
        def chunks(chunk_size):
            observed.append(chunk_size)
            time.sleep(0.02)
            yield b"\x00"
            pytest.fail("Read continued past absolute deadline")
        response.iter_content = chunks
        response.close = lambda: closed.append(True)
        return response
    monkeypatch.setattr(requests.Session, "request", request)
    sink = create_telemetry(config(tmp_path))
    sink.record_event("setup.check", {}, correlation="case")
    assert drain_telemetry(sink)
    assert observed == [1]
    assert closed == [True]
    assert sink.status()["export_failed"] == 1


def test_conversation_threads_do_not_override_langsmith_project_addressing(tmp_path):
    sink, exporter = make_sink(tmp_path, project="servicevoice-staging")
    assert sink.record_event("gateway.request", {"route": "gemini"}, correlation="private-session-a")
    assert sink.record_event("tool.execution", {"tool_name": "confirm_action"}, correlation="private-session-a")
    assert sink.record_event("gateway.request", {"route": "gemini"}, correlation="private-session-b")
    assert drain_telemetry(sink)
    spans = exporter.get_finished_spans()
    attributes = [dict(span.attributes) for span in spans]
    threads = [row["langsmith.metadata.thread_id"] for row in attributes]
    assert threads[0] == threads[1]
    assert threads[0] != threads[2]
    assert all(len(value) == 32 for value in threads)
    assert all(row["langsmith.trace.session_name"] == "servicevoice-staging" for row in attributes)
    assert all("langsmith.trace.session_id" not in row for row in attributes)
    assert all(span.parent is None for span in spans)
    assert len({span.context.trace_id for span in spans}) == 3
    assert "private-session" not in json.dumps(attributes)


def test_budget_month_rollover_and_clock_skew_cannot_restore_old_allowance(tmp_path):
    budget = _Budget(str(tmp_path / "quota.db"), 2)
    assert budget.reserve(3, now=1767225600) == 2  # January 2026
    assert budget.reserve(3, now=1769904000) == 2  # February 2026
    assert budget.reserve(3, now=1767225600) == 0


def test_budget_admission_is_atomic_across_concurrent_workers(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    budget = _Budget(str(tmp_path / "quota.db"), 10)
    budget.reserve(0)
    def reserve():
        try:
            return budget.reserve(3)
        except sqlite3.OperationalError as error:
            # The exporter deliberately drops optional traces on a busy quota
            # database. Contention must not bypass the cap or lose accounting.
            assert error.sqlite_errorcode & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
            return 0
    with ThreadPoolExecutor(max_workers=5) as executor:
        reservations = list(executor.map(lambda _: reserve(), range(15)))
    assert all(0 <= count <= 3 for count in reservations)
    assert sum(reservations) <= 10
    with sqlite3.connect(tmp_path / "quota.db") as connection:
        assert connection.execute("SELECT SUM(spans) FROM telemetry_budget").fetchone()[0] == sum(reservations)
    assert budget.reserve(10) == 10 - sum(reservations)
    assert budget.reserve(1) == 0
