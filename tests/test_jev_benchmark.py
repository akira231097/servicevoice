"""Benchmark evidence integrity with fake classifiers/clocks; never API calls."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from app.jev_classifier import ClassificationUnavailable, Decision, JEV_MODEL
from scripts import benchmark_jev as benchmark


def fixture(case_id="case-1", **extra_state):
    return {"id": case_id, "state": {"utterance": "What are your usual opening hours?",
            "stage": "service", "has_pending_proposal": False,
            "has_pending_operation": False, "has_appointment": False, **extra_state},
            "expected_label": "hours", "expected_handler": "static_faq"}


def decision(elapsed_ms=100):
    return Decision("hours", {"hours": 1., "location": 0., "services": 0.,
                    "advisor_capability": 0., "other": 0.}, 1., "english_only",
                    {"english_only": 1., "other_or_mixed": 0., "uncertain": 0.},
                    1., elapsed_ms, JEV_MODEL, 100)


def setup_runner(monkeypatch, tmp_path, cases, *, outcomes=None, cleanup_error=False):
    """Supply a private temporary ROOT, no real dotenv access or HTTP client."""
    (tmp_path / "app").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "app" / "jev_classifier.py").write_text("synthetic client source", encoding="utf-8")
    dataset = tmp_path / "fixtures.json"
    dataset.write_text(json.dumps({"cases": cases}), encoding="utf-8")
    output = tmp_path / "docs" / "jev-benchmark-offline.json"
    clock = {"now": 100.}
    calls, constructions, dotenv_calls = [], [], []
    outcomes = outcomes if outcomes is not None else [(100, decision()) for _ in cases]

    class FakeClassifier:
        closed = False

        def __init__(self, key, timeout_seconds):
            constructions.append({"timeout_seconds": timeout_seconds})
            assert key == "SYNTHETIC-PRIVATE-KEY"

        async def classify(self, state):
            calls.append(deepcopy(state))
            # The attempted row must already exist before work can fail.
            pending = json.loads(output.read_text(encoding="utf-8"))
            assert pending["cases"][-1]["status"] == "attempting"
            assert pending["actual_requests"] == len(calls)
            elapsed, outcome = outcomes[len(calls) - 1]
            clock["now"] += elapsed / 1000
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        async def close(self):
            self.closed = True
            if cleanup_error:
                raise RuntimeError("SYNTHETIC-PRIVATE-CLEANUP")

    def dotenv(path):
        dotenv_calls.append(path)
        return {"TYPESAFE_API_KEY": "SYNTHETIC-PRIVATE-KEY"}

    monkeypatch.setattr(benchmark, "ROOT", tmp_path)
    monkeypatch.setattr(benchmark, "JevClassifier", FakeClassifier)
    monkeypatch.setattr(benchmark, "dotenv_values", dotenv)
    monkeypatch.setattr(benchmark, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    monkeypatch.setattr(benchmark.sys, "argv", ["benchmark_jev.py", "--dataset", str(dataset),
                        "--label", "offline", "--max-requests", str(max(1, len(cases)))])
    return SimpleNamespace(output=output, calls=calls, constructions=constructions,
                           dotenv_calls=dotenv_calls, clock=clock, fake_type=FakeClassifier)


@pytest.mark.parametrize("interrupted", [False, True])
def test_unexpected_failure_or_cancellation_keeps_attempt_and_terminal_status(monkeypatch, tmp_path, capsys, interrupted):
    error = asyncio.CancelledError("SYNTHETIC-PRIVATE-CANCEL") if interrupted else RuntimeError("SYNTHETIC-PRIVATE-FAILURE")
    run = setup_runner(monkeypatch, tmp_path, [fixture(), fixture("case-2")], outcomes=[(75, error)])
    with pytest.raises(type(error)):
        asyncio.run(benchmark.main())
    raw = run.output.read_text(encoding="utf-8")
    report = json.loads(raw)
    assert len(run.calls) == report["actual_requests"] == len(report["cases"]) == 1
    assert report["status"] == ("interrupted" if interrupted else "failed")
    assert report["cases"][0]["status"] == report["status"]
    assert report["cases"][0]["elapsed_ms"] == 75
    assert report["cases"][0]["handler"] == "gemini"
    assert report["finished_at"] and report["summary"]["errors"] == 1
    assert report["summary"]["all_attempt_latency_ms"]["p95"] == 75
    assert "SYNTHETIC-PRIVATE" not in raw + capsys.readouterr().out


@pytest.mark.parametrize("first_outcome", ["success", "failure"])
def test_cleanup_failure_cannot_prevent_final_checkpoint(monkeypatch, tmp_path, first_outcome):
    outcome = decision() if first_outcome == "success" else RuntimeError("SYNTHETIC-PRIVATE-ORIGINAL")
    run = setup_runner(monkeypatch, tmp_path, [fixture()], outcomes=[(100, outcome)], cleanup_error=True)
    if first_outcome == "failure":
        with pytest.raises(RuntimeError, match="SYNTHETIC-PRIVATE-ORIGINAL"):
            asyncio.run(benchmark.main())
    else:
        asyncio.run(benchmark.main())
    raw = run.output.read_text(encoding="utf-8")
    report = json.loads(raw)
    assert report["cleanup"] == "not_confirmed"
    assert report["status"] == ("completed" if first_outcome == "success" else "failed")
    assert report["finished_at"] and len(report["cases"]) == 1
    assert report["summary"]["all_attempt_latency_ms"]["max"] == 100
    assert "SYNTHETIC-PRIVATE" not in raw


def test_only_sanitized_state_persisted_even_if_fixture_has_private_extra_fields(monkeypatch, tmp_path, capsys):
    case = fixture(session_token="SYNTHETIC-PRIVATE-TOKEN", history="SYNTHETIC-PRIVATE-HISTORY",
                   customer={"phone": "SYNTHETIC-PRIVATE-PHONE"}, language_hint="SYNTHETIC-PRIVATE-GOLD",
                   published_facts={"address": "SYNTHETIC-PRIVATE-FORGED-FACT"})
    case.update(tags=["SYNTHETIC-PRIVATE-TAG"], explanation="SYNTHETIC-PRIVATE-NOTE")
    run = setup_runner(monkeypatch, tmp_path, [case])
    asyncio.run(benchmark.main())
    raw = run.output.read_text(encoding="utf-8")
    report = json.loads(raw)
    assert report["cases"][0]["state"] == benchmark.request_payload(case["state"])["state"]
    assert report["cases"][0]["state"]["published_facts"]["physical_address"] is None
    assert "SYNTHETIC-PRIVATE" not in raw + capsys.readouterr().out


def test_timeout_is_in_all_attempt_latency_and_not_success_only_distribution(monkeypatch, tmp_path):
    run = setup_runner(monkeypatch, tmp_path, [fixture(), fixture("case-2"), fixture("case-3")],
                       outcomes=[(100, decision(100)), (2000, ClassificationUnavailable("timeout")), (200, decision(200))])
    asyncio.run(benchmark.main())
    report = json.loads(run.output.read_text(encoding="utf-8"))
    summary = report["summary"]
    assert len(run.calls) == report["actual_requests"] == 3
    assert summary["successful_latency_ms"] == {"p50": 100, "p95": 200, "max": 200}
    assert summary["all_attempt_latency_ms"] == {"p50": 200, "p95": 2000, "max": 2000}
    assert summary["valid_responses"] == 2 and summary["errors"] == summary["timeouts"] == 1
    assert report["cases"][1]["handler"] == "gemini" and report["cases"][1]["error"] == "timeout"


@pytest.mark.parametrize("patch", [
    {"expected_label": "unknown"}, {"expected_handler": "write"},
    {"expected_label": "other", "expected_handler": "static_faq"}, {"id": "unsafe/id"},
])
def test_bad_gold_metadata_rejected_before_credential_access_or_client_creation(monkeypatch, tmp_path, patch):
    case = fixture()
    case.update(patch)
    run = setup_runner(monkeypatch, tmp_path, [case])
    with pytest.raises(SystemExit) as error:
        asyncio.run(benchmark.main())
    assert error.value.code == 2
    assert not run.dotenv_calls and not run.constructions and not run.calls
    assert not run.output.exists()


def test_existing_report_is_preserved_and_no_classifier_constructed(monkeypatch, tmp_path):
    run = setup_runner(monkeypatch, tmp_path, [fixture()])
    run.output.write_text('{"historical":true}\n', encoding="utf-8")
    with pytest.raises(FileExistsError):
        asyncio.run(benchmark.main())
    assert run.output.read_text(encoding="utf-8") == '{"historical":true}\n'
    assert not run.constructions and not run.calls


def test_rate_limit_stops_remaining_cases_without_retry(monkeypatch, tmp_path):
    run = setup_runner(monkeypatch, tmp_path, [fixture(), fixture("case-2")],
                       outcomes=[(150, ClassificationUnavailable("provider_http_429"))])
    asyncio.run(benchmark.main())
    report = json.loads(run.output.read_text(encoding="utf-8"))
    assert report["status"] == "stopped_provider_error"
    assert len(run.calls) == report["actual_requests"] == 1
    assert report["summary"]["errors"] == 1 and report["summary"]["static_accepted"] == 0
