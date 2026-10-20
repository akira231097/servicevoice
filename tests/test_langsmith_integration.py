"""Application/telemetry failure boundaries; all data and transports are local."""
import asyncio
from datetime import datetime, timezone
import importlib
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import dotenv
import httpx
import pytest

from runtime_sqlite import ensure_sqlite
ensure_sqlite()
from app import domain
from app.domain import Store
from app.harness import ToolHarness
from app import harness as harness_module
from app.observability import NullTelemetry, TelemetryConfig, create_telemetry
from test_gemini_gateway import setup, post, request_body, frame, KEY, SIG


class Spy:
    def __init__(self, fail=False):
        self.events = []
        self.closed = []
        self.fail = fail

    def record_event(self, name, attrs, **options):
        self.events.append((name, attrs, options))
        if self.fail:
            raise RuntimeError("private-monitoring-failure")
        return True

    def close(self, timeout_seconds=2.0):
        self.closed.append((timeout_seconds, threading.get_ident()))
        return True

    def status(self):
        return {"enabled": True, "reason": "ready", "recorded": len(self.events), "exported": 0,
                "export_failed": 0, "quota_dropped": 0, "filtered": 0, "sampled_out": 0}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(domain, "now", lambda: datetime(2026, 10, 18, 18, tzinfo=timezone.utc))
    return Store(tmp_path / "synthetic.sqlite3")


async def booking(harness, sid):
    options = await harness.run(sid, "search_availability", {"service": "oil_change"}, "search-private-id")
    proposal = await harness.run(sid, "prepare_booking", {"service": "oil_change", "slot_id": options["slots"][0]["id"],
        "customer_name": "Private Test Customer", "phone": "2025550146", "vehicle": "2022 Test Vehicle"}, "prepare-private-id")
    assert proposal["ok"] is True
    arguments = {"proposal_id": proposal["proposal_id"], "confirmed": True}
    result = await harness.run(sid, "confirm_action", arguments, "commit-private-id")
    return result, arguments


def active_count(store):
    with store.connect() as db:
        return db.execute("SELECT count(*) FROM appointments WHERE status='confirmed'").fetchone()[0]


def test_tool_hook_exposes_fixed_operational_facts_without_arguments_or_results(store):
    sid = store.create_session()["id"]
    sink = Spy()
    async def run():
        harness = ToolHarness(store)
        harness.telemetry = sink
        try:
            result, _ = await booking(harness, sid)
            assert result["ok"] is True and active_count(store) == 1
            assert len(sink.events) == 3
            for name, attrs, timing in sink.events:
                assert name == "tool.execution"
                assert set(attrs) == {"tool_name", "ok", "error_code", "receipt_present", "admitted"}
                assert attrs["ok"] is True and attrs["receipt_present"] is True
                assert timing["correlation"] == sid
                assert timing["start_unix"] > 0 and timing["duration_ms"] >= 0
            text = json.dumps([attrs for _, attrs, _ in sink.events])
            for private in (sid, "Private Test Customer", "2025550146", "2022 Test Vehicle", "prepare-private-id", result["_receipt"]["operation_id"]):
                assert private not in text
        finally:
            await harness.close()
    asyncio.run(run())


def test_raising_monitor_cannot_change_committed_booking_or_retry_receipt(store):
    sid = store.create_session()["id"]
    async def run():
        harness = ToolHarness(store)
        harness.telemetry = Spy(fail=True)
        try:
            first, arguments = await booking(harness, sid)
            retry = await harness.run(sid, "confirm_action", arguments, "commit-private-id")
            assert first["ok"] is True and first == retry
            assert first["appointment"]["status"] == "confirmed"
            assert "_receipt" in first and active_count(store) == 1
            with store.connect() as db:
                assert db.execute("SELECT count(*) FROM tool_results WHERE session_id=? AND call_id='commit-private-id'", (sid,)).fetchone()[0] == 1
        finally:
            await harness.close()
    asyncio.run(run())


def test_cancelled_waiter_does_not_turn_accepted_booking_into_failed_write_telemetry(store, monkeypatch):
    sid = store.create_session()["id"]
    sink = Spy()
    entered, release = threading.Event(), threading.Event()
    original = harness_module.execute
    def delayed(store, session_id, name, arguments, call_id):
        if name == "confirm_action":
            entered.set()
            assert release.wait(3), "Accepted test write was never released"
        return original(store, session_id, name, arguments, call_id)
    monkeypatch.setattr(harness_module, "execute", delayed)
    async def run():
        harness = ToolHarness(store, timeout_seconds=2)
        harness.telemetry = sink
        waiter = None
        try:
            options = await harness.run(sid, "search_availability", {"service": "oil_change"}, "cancel-search")
            proposal = await harness.run(sid, "prepare_booking", {"service": "oil_change", "slot_id": options["slots"][0]["id"],
                "customer_name": "Synthetic Customer", "phone": "2025550146", "vehicle": "2022 Test Vehicle"}, "cancel-prepare")
            arguments = {"proposal_id": proposal["proposal_id"], "confirmed": True}
            waiter = asyncio.create_task(harness.run(sid, "confirm_action", arguments, "accepted-then-cancelled"))
            assert await asyncio.to_thread(entered.wait, 1)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            event = sink.events[-1][1]
            assert event["tool_name"] == "confirm_action" and event["error_code"] == "waiter_interrupted"
            assert event["ok"] is None, "A cancelled waiter has no authoritative write outcome"
            assert event["receipt_present"] is False
            release.set()
            await harness.drain()
            assert active_count(store) == 1
            reconciled = await harness.run(sid, "confirm_action", arguments, "accepted-then-cancelled")
            assert reconciled["ok"] is True and "_receipt" in reconciled
            assert sink.events[-1][1]["ok"] is True and active_count(store) == 1
        finally:
            release.set()
            if waiter is not None:
                await asyncio.gather(waiter, return_exceptions=True)
            await harness.close()
    asyncio.run(run())


def test_untrusted_tool_name_and_invalid_args_never_enter_hook_attributes(store):
    sid = store.create_session()["id"]
    sink = Spy()
    async def run():
        harness = ToolHarness(store)
        harness.telemetry = sink
        try:
            unknown = await harness.run(sid, "private-customer-in-tool-name", {"secret": "private-argument"}, "private-call-id")
            invalid = await harness.run(sid, "confirm_action", {"secret": "private-argument"}, "private-call-id-2")
            assert unknown["error"] == "unknown_tool" and invalid["error"] == "invalid_arguments"
            assert sink.events[0][1]["tool_name"] == "unknown_tool"
            assert "private" not in json.dumps([row[1] for row in sink.events])
            assert active_count(store) == 0
        finally:
            await harness.close()
    asyncio.run(run())


@pytest.mark.parametrize("monitor_fails", [False, True])
def test_gateway_observer_failure_preserves_validated_stream_and_private_data(tmp_path, monitor_fails):
    private_reply = "PRIVATE-RESPONSE-CONTENT"
    app, gateway, _ = setup(tmp_path, lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"},
        content=frame({"content": private_reply}) + frame(reason="stop") + b"data: [DONE]\n\n"))
    sink = Spy(fail=monitor_fails)
    gateway.telemetry = sink
    async def run():
        try:
            response = await post(app, request_body())
            assert response.status_code == 200 and private_reply in response.text
            assert response.text.endswith("data: [DONE]\n\n")
            assert not gateway.active
            [timing] = gateway.latency_snapshot("call-a")
            assert timing["outcome"] == "completed"
            assert len(sink.events) == 1
            name, attrs, options = sink.events[0]
            assert name == "gateway.request" and attrs["outcome"] == "completed"
            assert attrs["route"] == "gemini" and attrs["model"] == "gemini-3.8-flash"
            assert attrs["total_ms"] >= attrs["first_text_ms"] >= attrs["upstream_done_ms"]
            assert options["correlation"] == "sid-a"
            encoded = json.dumps(attrs)
            for private in (private_reply, KEY, SIG, "call-a", "sid-a", "private-session-capability", "private-metadata", "+15555551234", "private-monitoring-failure"):
                assert private not in encoded
        finally:
            await gateway.close()
    asyncio.run(run())


def test_gateway_provider_failure_remains_failure_even_when_monitor_raises(tmp_path):
    def failure(_): raise httpx.ConnectError("private-provider-error-body")
    app, gateway, _ = setup(tmp_path, failure)
    gateway.telemetry = Spy(fail=True)
    async def run():
        try:
            response = await post(app, request_body())
            assert response.status_code == 502
            assert "private-provider-error-body" not in response.text
            [row] = gateway.latency_snapshot("call-a")
            assert row["outcome"] == "upstream_unavailable"
            assert gateway.telemetry.events[0][1]["outcome"] == "upstream_unavailable"
            assert not gateway.active
        finally:
            await gateway.close()
    asyncio.run(run())


@pytest.fixture
def app_setup(tmp_path, monkeypatch):
    # Main's existing import-time dotenv hook and its startup settings lookup are
    # both intercepted. Every process setting is synthetic or disabled here.
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr(dotenv, "dotenv_values", lambda *a, **k: {})
    monkeypatch.setenv("SERVICEVOICE_DB", str(tmp_path / "import.sqlite3"))
    monkeypatch.setenv("SERVICEVOICE_LANGSMITH_ENABLED", "off")
    monkeypatch.setenv("SERVICEVOICE_JEV_FAQ", "off")
    monkeypatch.setenv("TYPESAFE_API_KEY", "")
    monkeypatch.setenv("LANGSMITH_API_KEY", "synthetic-not-a-real-key")
    module = importlib.import_module("app.main")
    app = module.create_app(tmp_path / "console.sqlite3")
    return module, app


def test_two_listener_startup_and_duplicate_calls_construct_one_provider(app_setup, monkeypatch):
    module, app = app_setup
    sink, builds = Spy(), []
    def build(config):
        builds.append(config)
        return sink
    monkeypatch.setattr(module, "create_telemetry", build)
    monkeypatch.setattr(app.state.gemini_gateway, "start", AsyncMock())
    public = module.create_public_app(app)
    async def run():
        async with app.router.lifespan_context(app):
            async with public.router.lifespan_context(public):
                await asyncio.gather(*(app.state.startup_resources() for _ in range(8)))
                assert len(builds) == 1
                assert app.state.telemetry is sink
                assert app.state.harness.telemetry is sink
                assert app.state.gemini_gateway.telemetry is sink
        assert sink.closed
        assert all(timeout == 2 for timeout, _ in sink.closed)
        assert all(thread_id != threading.get_ident() for _, thread_id in sink.closed)
    asyncio.run(run())


def test_observability_status_is_local_only_and_absent_on_public_listener(app_setup, monkeypatch):
    module, app = app_setup
    sink = Spy()
    monkeypatch.setattr(module, "create_telemetry", lambda config: sink)
    async def run():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                response = await client.get("/api/observability-status")
                assert response.status_code == 200
                status=response.json()
                diagnostic_status=status.pop("diagnostics")
                assert status == sink.status()
                assert diagnostic_status["enabled"] and diagnostic_status["config_id"]
                assert response.headers["cache-control"] == "no-store"
                assert "synthetic-not-a-real-key" not in response.text
                assert (await client.get("/api/observability-status", headers={"host": "external.example"})).status_code == 403
                assert (await client.get("/api/observability-status", headers={"origin": "https://external.example"})).status_code == 403
            public = module.create_public_app(app)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public), base_url="http://testserver") as client:
                for host in ("localhost", "127.0.0.1", "external.example"):
                    assert (await client.get("/api/observability-status", headers={"host": host})).status_code == 404
    asyncio.run(run())


def test_provider_creation_failure_falls_back_without_blocking_app(app_setup, monkeypatch):
    module, app = app_setup
    def fail(config): raise RuntimeError("private-setup-error")
    monkeypatch.setattr(module, "create_telemetry", fail)
    async def run():
        async with app.router.lifespan_context(app):
            assert isinstance(app.state.telemetry, NullTelemetry)
            sid = app.state.store.create_session()["id"]
            result, _ = await booking(app.state.harness, sid)
            assert result["ok"] is True and active_count(app.state.store) == 1
    asyncio.run(run())


def test_actual_booking_hook_to_otel_pipeline_exports_no_private_values(store, tmp_path):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    exporter = InMemorySpanExporter()
    sink = create_telemetry(TelemetryConfig(enabled=True, api_key="synthetic-key-for-test-only",
        budget_path=str(tmp_path / "telemetry-budget.sqlite3")), exporter=exporter)
    assert sink.status()["reason"] == "ready"
    sid = store.create_session()["id"]
    async def run():
        harness = ToolHarness(store)
        harness.telemetry = sink
        try:
            result, _ = await booking(harness, sid)
            assert result["ok"] is True and active_count(store) == 1
        finally:
            await harness.close()
    try:
        asyncio.run(run())
    finally:
        assert sink.close(timeout_seconds=2)
    spans = exporter.get_finished_spans()
    assert len(spans) == 3
    text = json.dumps([{"name": span.name, "attributes": dict(span.attributes), "resource": dict(span.resource.attributes)} for span in spans])
    for value in (sid, "Private Test Customer", "2025550146", "2022 Test Vehicle", "synthetic-key-for-test-only", "search-private-id", "prepare-private-id", "commit-private-id"):
        assert value not in text
    assert all(span.name == "tool.execution" for span in spans)
    assert all(span.attributes["langsmith.metadata.capture_policy"] == "content-free-v1" for span in spans)
