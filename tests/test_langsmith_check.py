"""Check command validation, redaction and failed-ingestion handling; no API calls."""
from dataclasses import replace
import json
from pathlib import Path

import pytest

from app.observability import TelemetryConfig
from scripts import check_langsmith as check

SECRET = "not-a-real-langsmith-api-key-secret"


def config(tmp_path, **overrides):
    return replace(TelemetryConfig(api_key=SECRET, budget_path=str(tmp_path / "budget.sqlite3")), **overrides)


class Sink:
    def __init__(self, status=None, *, admitted=True, closed=True):
        self.values = {"reason": "closed", "recorded": 1, "exported": 1, "export_failed": 0,
                       "quota_dropped": 0, "filtered": 0, "sampled_out": 0, "budget_errors": 0}
        self.values.update(status or {})
        self.admitted, self.closed = admitted, closed
        self.events = []
        self.timeouts = []

    def record_event(self, name, attributes, *, correlation):
        self.events.append((name, attributes, correlation))
        return self.admitted

    def close(self, *, timeout_seconds):
        self.timeouts.append(timeout_seconds)
        return self.closed

    def status(self):
        return self.values


def test_default_check_has_no_sink_files_or_network(tmp_path):
    report, code = check.check_configuration(config(tmp_path), sink_factory=lambda _: pytest.fail("Created network sink"))
    assert code == 0
    assert report["config_reason"] == "ready"
    assert report["agent_tracing_enabled"] is False
    assert "ingest_accepted" not in report
    assert not (tmp_path / "budget.sqlite3").exists()
    assert SECRET not in json.dumps(report)


@pytest.mark.parametrize("changes,reason", [
    ({"api_key": ""}, "missing_api_key"),
    ({"endpoint": "https://private-key@evil.example"}, "configuration_error"),
    ({"workspace_id": "private-invalid-workspace"}, "configuration_error"),
    ({"project": "private-customer@email.example"}, "configuration_error"),
])
def test_invalid_configuration_is_fixed_reason_only(tmp_path, changes, reason):
    report, code = check.check_configuration(config(tmp_path, **changes), send_test=True,
                                             sink_factory=lambda _: pytest.fail("Created network sink"))
    assert code == 2
    assert report["config_reason"] == reason
    encoded = json.dumps(report)
    assert SECRET not in encoded and "private" not in encoded


def test_send_is_one_content_free_span_and_forces_sampling(tmp_path):
    sink = Sink()
    candidates = []
    def factory(candidate):
        candidates.append(candidate)
        return sink
    report, code = check.check_configuration(config(tmp_path, sample_rate=0), send_test=True, sink_factory=factory)
    assert code == 0 and report["ingest_accepted"] is True
    assert candidates[0].enabled is True and candidates[0].sample_rate == 1.0
    assert len(sink.events) == 1
    name, attributes, correlation = sink.events[0]
    assert name == "setup.check" and attributes == {"mode": "setup", "status": "ready"}
    assert correlation.startswith("synthetic-setup-") and len(correlation) == len("synthetic-setup-") + 32
    assert sink.timeouts == [3.0]
    encoded = json.dumps(report)
    assert correlation not in encoded and SECRET not in encoded
    assert "dashboard" not in report and "billing" not in report


@pytest.mark.parametrize("status,admitted,closed", [
    ({"exported": 0}, True, True),
    ({"export_failed": 1}, True, True),
    ({"quota_dropped": 1}, True, True),
    ({"budget_errors": 1}, True, True),
    ({"filtered": 1}, True, True),
    ({"sampled_out": 1}, True, True),
    ({"recorded": 0}, True, True),
    ({"recorded": 2, "exported": 2}, True, True),
    ({}, False, True),
    ({}, True, False),
    ({"reason": "dependency_unavailable", "recorded": 0, "exported": 0}, False, True),
    ({"reason": "unknown-untrusted-status"}, True, True),
])
def test_partial_or_failed_export_cannot_pass(tmp_path, status, admitted, closed):
    report, code = check.check_configuration(config(tmp_path), send_test=True,
                                             sink_factory=lambda _: Sink(status, admitted=admitted, closed=closed))
    assert code == 1 and report["ingest_accepted"] is False


def test_sink_raw_status_and_errors_are_never_echoed(tmp_path):
    class Broken(Sink):
        def record_event(self, *args, **kwargs): raise RuntimeError(SECRET)
        def status(self): return {"reason": SECRET, "recorded": SECRET, "error": SECRET}
    sink = Broken()
    report, code = check.check_configuration(config(tmp_path), send_test=True, sink_factory=lambda _: sink)
    assert code == 1 and report["export_reason"] == "check_failed"
    assert sink.timeouts == [3.0]
    assert SECRET not in json.dumps(report)


def test_load_only_named_values_env_precedence_and_relative_budget(tmp_path, monkeypatch):
    import dotenv
    calls = []
    def fake_values(path, **kwargs):
        calls.append((path, kwargs))
        return {"LANGSMITH_API_KEY": SECRET, "LANGSMITH_PROJECT": "file-project",
                "GEMINI_API_KEY": "unrelated-secret", "SERVICEVOICE_LANGSMITH_BUDGET_PATH": "state/budget.sqlite3"}
    monkeypatch.setattr(dotenv, "dotenv_values", fake_values)
    actual = check.load_configuration(tmp_path, {"LANGSMITH_PROJECT": "env-project"})
    assert actual.api_key == SECRET and actual.project == "env-project"
    assert actual.budget_path == str((tmp_path / "state/budget.sqlite3").resolve())
    assert calls == [(tmp_path / ".env", {"interpolate": False})]
    assert not (tmp_path / "state").exists()


def test_output_is_new_file_and_no_secret_or_correlation(tmp_path, monkeypatch, capsys):
    destination = tmp_path / "check.json"
    monkeypatch.setattr(check, "load_configuration", lambda: config(tmp_path))
    monkeypatch.setattr(check, "create_telemetry", lambda _: Sink())
    assert check.main(["--send-test", "--output", str(destination)]) == 0
    result = json.loads(destination.read_text())
    assert result["ingest_accepted"] is True
    assert SECRET not in destination.read_text() and "correlation" not in destination.read_text()
    assert SECRET not in capsys.readouterr().out
    original = destination.read_bytes()
    monkeypatch.setattr(check, "load_configuration", lambda: pytest.fail("Read config after failed output reservation"))
    assert check.main(["--send-test", "--output", str(destination)]) == 2
    assert destination.read_bytes() == original


def test_load_exception_prints_fixed_error(tmp_path, monkeypatch, capsys):
    def fail(): raise RuntimeError(SECRET)
    monkeypatch.setattr(check, "load_configuration", fail)
    assert check.main([]) == 2
    assert json.loads(capsys.readouterr().out) == {"schema_version": 1, "config_reason": "configuration_load_failed"}
