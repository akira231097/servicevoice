"""A clean final record cannot replace missing provider tool chronology."""
from copy import deepcopy

import pytest

from app.live_eval_evidence import DIMENSIONS, build_evidence, read_snapshot
from app.live_evaluations import public_report
from test_live_evaluations import scenario, configuration, judgment, worker_fixture


@pytest.mark.parametrize("missing_id", ["begin", "prepare", "confirm", "all"])
def test_persisted_tool_missing_from_provider_artifact_is_incomplete_not_invented_failure(scenario, tmp_path, missing_id):
    store, call = scenario
    call = deepcopy(call)
    snapshot = read_snapshot(store.path, call["id"])
    call["artifact"]["messages"] = [
        message for message in call["artifact"]["messages"]
        if not message.get("toolCalls") or missing_id != "all" and message["toolCalls"][0]["id"] != missing_id
    ]
    original_call, original_snapshot = deepcopy(call), deepcopy(snapshot)
    package = build_evidence(call, snapshot)
    assert "persisted_tool_chronology_missing" in package["evidence_issues"]
    assert package["mechanical_checks"]["evidence_complete"]["verdict"] == "uncertain"
    # The database remains mechanically correct; missing chronology is not
    # evidence that a caller refused consent or that the saved details are wrong.
    assert package["mechanical_checks"]["booking_integrity"]["verdict"] == "pass"
    if missing_id in {"confirm", "all"}:
        assert "committed_action_without_observed_confirmation" in package["evidence_issues"]
        assert package["mechanical_checks"]["confirmation_order"]["verdict"] == "uncertain"
    report = public_report(package, judgment(package), call, [], configuration(tmp_path, store.path))
    assert all(report["checks"][dimension]["verdict"] == "uncertain" for dimension in DIMENSIONS)
    assert call == original_call and snapshot == original_snapshot


def test_missing_confirm_chronology_is_unknown_even_when_result_ledger_is_absent(scenario):
    store, call = scenario
    call = deepcopy(call)
    call["artifact"]["messages"] = [m for m in call["artifact"]["messages"] if not m.get("toolCalls")]
    snapshot = read_snapshot(store.path, call["id"])
    snapshot["results"] = []
    snapshot["operations"] = []
    package = build_evidence(call, snapshot)
    assert "committed_action_without_observed_confirmation" in package["evidence_issues"]
    assert package["mechanical_checks"]["confirmation_order"]["verdict"] == "uncertain"
    assert package["mechanical_checks"]["evidence_complete"]["verdict"] == "uncertain"


def test_admitted_attempt_without_result_or_provider_message_is_still_missing_chronology(scenario):
    store, call = scenario
    snapshot = read_snapshot(store.path, call["id"])
    snapshot["operations"].append({"call_id": "unobserved_attempt", "status": "unknown"})
    package = build_evidence(call, snapshot)
    assert "persisted_tool_chronology_missing" in package["evidence_issues"]
    assert package["mechanical_checks"]["booking_integrity"]["verdict"] == "uncertain"


def test_information_only_call_without_any_attempt_does_not_invent_missing_tools(scenario):
    store, call = scenario
    snapshot = read_snapshot(store.path, call["id"])
    for key in ("appointments", "proposals", "results", "operations", "signatures"):
        snapshot[key] = []
    call = deepcopy(call)
    call["artifact"]["messages"] = [
        {"role": "user", "message": "What time do you close on weekdays?"},
        {"role": "bot", "message": "Our weekday hours are in the dealership information."},
    ]
    package = build_evidence(call, snapshot)
    assert not package["evidence_issues"]
    assert package["mechanical_checks"]["evidence_complete"]["verdict"] == "pass"
    assert package["mechanical_checks"]["booking_integrity"]["verdict"] == "not_applicable"
    assert package["mechanical_checks"]["confirmation_order"]["verdict"] == "not_applicable"


def test_unexecuted_generation_signature_does_not_invent_a_scheduler_attempt(scenario):
    store, call = scenario
    snapshot = read_snapshot(store.path, call["id"])
    snapshot["signatures"].append({"tool_call_id": "superseded_unexecuted_tool", "fingerprint": "0" * 64, "user_count": 3})
    package = build_evidence(call, snapshot)
    assert not package["evidence_issues"]
    assert package["mechanical_checks"]["evidence_complete"]["verdict"] == "pass"


def test_complete_provider_history_stays_complete_without_harness_attempt_rows(scenario):
    store, call = scenario
    snapshot = read_snapshot(store.path, call["id"])
    assert not snapshot["operations"]  # Direct Store dispatch fixture, not harness.
    package = build_evidence(call, snapshot)
    assert not package["evidence_issues"]
    assert package["mechanical_checks"]["confirmation_order"]["verdict"] == "pass"


def test_worker_exports_missing_chronology_as_unknown_without_spending_judge_budget(scenario, tmp_path):
    store, call = scenario
    call = deepcopy(call)
    call["artifact"]["messages"] = [m for m in call["artifact"]["messages"] if not m.get("toolCalls")]
    worker, mock, _ = worker_fixture(tmp_path, (store, call))
    assert worker.tick()
    assert mock.judge_requests == 0
    assert worker.outbox.status()["jobs"] == {"complete": 1}
    report = next(iter(mock.runs.values()))["outputs"]
    assert report["checks"]["booking_integrity"]["verdict"] == "pass"
    assert report["checks"]["confirmation_order"]["verdict"] == "uncertain"
    assert all(report["checks"][dimension]["verdict"] == "uncertain" for dimension in DIMENSIONS)
    assert report["needs_review"] is True
