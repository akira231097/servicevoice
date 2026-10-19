"""Optional diagnostic lifetime must remain separate from booking persistence."""
import importlib
from pathlib import Path
import sqlite3
import time

import dotenv
from fastapi.testclient import TestClient

from app.gemini_gateway import RequestTiming
from app.observability import NullLocalTimingSink


def application(tmp_path, monkeypatch):
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr(dotenv, "dotenv_values", lambda *a, **k: {})
    monkeypatch.setenv("SERVICEVOICE_DB", str(tmp_path / "ambient-test.sqlite3"))
    monkeypatch.setenv("SERVICEVOICE_TIMING_PERSISTENCE", "on")
    module = importlib.import_module("app.main")
    return module, module.create_app(tmp_path / "booking.sqlite3")


def test_lifespan_flushes_only_content_free_timing_to_a_companion_database(tmp_path, monkeypatch):
    _, app = application(tmp_path, monkeypatch)
    with TestClient(app):
        gateway = app.state.gemini_gateway
        timing = RequestTiming("private-call-id", time.monotonic(), gateway.timings,
                               gateway._observe_timing, "private-session-id")
        timing.outcome = "completed"
        timing.finish()
        assert gateway.timing_sink is app.state.local_timings
        assert app.state.local_timings.status()["recorded"] == 1
    path = tmp_path / "booking-gateway-timings.sqlite3"
    assert path.exists() and Path(app.state.store.path) != path
    rows = app.state.local_timings.snapshot()
    assert len(rows) == 1 and rows[0]["request_id"] == timing.row["request_id"]
    assert "private-call-id" not in str(rows) and "private-session-id" not in str(rows)
    with sqlite3.connect(app.state.store.path) as database:
        assert not database.execute("SELECT 1 FROM sqlite_master WHERE name='gateway_timings'").fetchone()
        assert database.execute("SELECT count(*) FROM appointments").fetchone()[0] == 0


def test_diagnostic_storage_initialization_failure_does_not_prevent_application_start(tmp_path, monkeypatch):
    module, app = application(tmp_path, monkeypatch)
    def fail(_):
        raise OSError("private-diagnostic-path")
    monkeypatch.setattr(module, "create_local_timing_sink", fail)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert isinstance(app.state.local_timings, NullLocalTimingSink)


def test_timing_storage_remains_opt_in_for_local_sessions(tmp_path, monkeypatch):
    _, app = application(tmp_path, monkeypatch)
    monkeypatch.setenv("SERVICEVOICE_TIMING_PERSISTENCE", "off")
    with TestClient(app):
        assert isinstance(app.state.local_timings, NullLocalTimingSink)
    assert not (tmp_path / "booking-gateway-timings.sqlite3").exists()
