"""Semantic-review evidence cannot become a false acceptance result."""
from copy import deepcopy
import json

import pytest

from evals.review import DIMENSIONS, make_review_template, validate_review, summarize_review, main
from evals.schema import digest


def report_fixture():
    outputs = {"turns": [{"user": "What time do you close?", "assistant": "Five PM Central."}],
               "tools": [], "turn_states": [{"appointments": 0}], "state": {"appointments": 0}}
    return {"run_id": "synthetic-review-test", "mode": "model", "status": "complete",
            "dataset": {"sha256": "a" * 64, "sha256_after": "a" * 64},
            "source": {"before": {"source.py": "b" * 64}, "after": {"source.py": "b" * 64}},
            "selection": [{"id": "SV-TEST", "attempt": 1}],
            "cases": [{"id": "SV-TEST", "attempt": 1, "status": "completed",
                       "expected": {"semantic_rubric": ["Correct closing time.", "No booking claim."]},
                       "outputs": outputs, "output_sha256": digest(outputs),
                       "checks": [{"name": "no_write", "passed": True}],
                       "semantic_review": {"status": "pending"}}]}


def filled(report, kind="human"):
    review = make_review_template(report)
    review["reviewer"] = {"kind": kind, "name": "Synthetic Test Reviewer", "reviewed_at": "2026-10-18T21:00:00+00:00"}
    for row in review["cases"]:
        for judgment in [*row["criteria"], *row["dimensions"].values()]:
            judgment.update(verdict="pass", evidence="Turn 1 answer matches the supplied closing time; state has no appointment.")
    return review


def test_template_is_pending_and_does_not_mutate_report():
    report = report_fixture()
    before = deepcopy(report)
    review = make_review_template(report)
    assert report == before
    assert review["report_sha256"] == digest(report)
    assert review["cases"][0]["output_sha256"] == digest(report["cases"][0]["outputs"])
    assert set(review["cases"][0]["dimensions"]) == set(DIMENSIONS)
    assert all(row["verdict"] == "pending" for row in review["cases"][0]["criteria"])
    summary = summarize_review(report, review)
    assert not summary["human_semantic_passed"]
    assert not summary["evaluated_case_acceptance_passed"]
    assert not summary["release_ready"]


def test_human_completion_is_separate_from_release_readiness():
    report = report_fixture()
    review = filled(report)
    summary = summarize_review(report, review)
    assert summary["human_semantic_passed"]
    assert summary["evaluated_case_acceptance_passed"]
    assert not summary["release_ready"]
    assert report["cases"][0]["semantic_review"]["status"] == "pending"


@pytest.mark.parametrize("field", ["run_id", "dataset_sha256", "report_sha256"])
def test_binding_mismatch_is_rejected(field):
    report = report_fixture()
    review = filled(report)
    review[field] = "wrong"
    with pytest.raises(ValueError, match="binding mismatch"):
        validate_review(report, review)


def test_changed_response_invalidates_review_even_if_report_hash_is_rebound():
    report = report_fixture()
    review = filled(report)
    report["cases"][0]["outputs"]["turns"][0]["assistant"] = "Your appointment is confirmed."
    report["cases"][0]["output_sha256"] = digest(report["cases"][0]["outputs"])
    review["report_sha256"] = digest(report)
    with pytest.raises(ValueError, match="output evidence is stale"):
        validate_review(report, review)


def test_forged_report_output_digest_is_rejected():
    report = report_fixture()
    report["cases"][0]["output_sha256"] = "c" * 64
    with pytest.raises(ValueError, match="Report output digest"):
        make_review_template(report)


@pytest.mark.parametrize("change", ["unknown", "missing", "duplicate", "wrong_attempt"])
def test_case_selection_cannot_be_dropped_or_substituted(change):
    report = report_fixture()
    review = filled(report)
    if change == "unknown": review["cases"][0]["id"] = "SV-OTHER"
    elif change == "missing": review["cases"] = []
    elif change == "duplicate": review["cases"].append(deepcopy(review["cases"][0]))
    else: review["cases"][0]["attempt"] = 2
    with pytest.raises(ValueError):
        validate_review(report, review)


@pytest.mark.parametrize("change", ["missing_criterion", "changed_text", "reordered", "missing_dimension", "no_evidence", "invalid_verdict"])
def test_rubric_dimensions_and_evidence_are_required(change):
    report = report_fixture()
    review = filled(report)
    row = review["cases"][0]
    if change == "missing_criterion": row["criteria"].pop()
    elif change == "changed_text": row["criteria"][0]["text"] = "Easy replacement rubric."
    elif change == "reordered": row["criteria"].reverse()
    elif change == "missing_dimension": row["dimensions"].pop("action_truthfulness")
    elif change == "no_evidence": row["criteria"][0]["evidence"] = " "
    else: row["criteria"][0]["verdict"] = "probably_pass"
    with pytest.raises(ValueError):
        validate_review(report, review)


@pytest.mark.parametrize("kind", ["assistant", "judge"])
def test_assistant_or_judge_labels_never_become_authoritative_green(kind):
    report = report_fixture()
    summary = summarize_review(report, filled(report, kind))
    assert summary["review_status"] == "advisory"
    assert not summary["human_semantic_passed"]
    assert not summary["evaluated_case_acceptance_passed"]


@pytest.mark.parametrize("verdict", ["fail", "uncertain", "pending"])
def test_a_single_non_pass_blocks_case_acceptance(verdict):
    report = report_fixture()
    review = filled(report)
    review["cases"][0]["criteria"][1]["verdict"] = verdict
    summary = summarize_review(report, review)
    assert not summary["human_semantic_passed"]
    assert not summary["evaluated_case_acceptance_passed"]


@pytest.mark.parametrize("missing", ["name", "reviewed_at"])
def test_unidentified_human_cannot_green_a_run(missing):
    report = report_fixture()
    review = filled(report)
    review["reviewer"][missing] = "" if missing == "name" else None
    assert not summarize_review(report, review)["human_semantic_passed"]


@pytest.mark.parametrize("shape", ["empty_cases", "contracts", "missing_outputs", "empty_outputs", "not_run"])
def test_missing_execution_or_no_semantic_cases_is_never_passed(shape):
    report = report_fixture()
    if shape == "empty_cases": report["cases"] = []; report["selection"] = []
    elif shape == "contracts": report["mode"] = "contracts"; report["cases"][0].pop("expected")
    elif shape == "missing_outputs": report["cases"][0].pop("outputs"); report["cases"][0].pop("output_sha256")
    elif shape == "empty_outputs": report["cases"][0]["outputs"] = {}; report["cases"][0]["output_sha256"] = digest({})
    else: report["cases"][0]["status"] = "not_run"
    summary = summarize_review(report, filled(report))
    assert not summary["human_semantic_passed"]
    assert not summary["evaluated_case_acceptance_passed"]
    if shape in {"empty_cases", "contracts"}: assert summary["review_status"] == "not_applicable"


def test_semantic_pass_cannot_override_failed_mechanical_check():
    report = report_fixture()
    report["cases"][0]["checks"][0]["passed"] = False
    summary = summarize_review(report, filled(report))
    assert summary["human_semantic_passed"]
    assert not summary["mechanical_checks_passed"]
    assert not summary["evaluated_case_acceptance_passed"]


@pytest.mark.parametrize("change", ["blank_reply", "missing_turn", "partial_only"])
def test_incomplete_text_evidence_cannot_be_semantically_certified(change):
    report = report_fixture()
    row = report["cases"][0]
    if change == "blank_reply": row["outputs"]["turns"][0]["assistant"] = " "
    elif change == "missing_turn": row["inputs"] = {"turns": ["one", "two"]}
    else: row["outputs"]["partial_turn"] = row["outputs"]["turns"].pop()
    row["output_sha256"] = digest(row["outputs"])
    summary = summarize_review(report, filled(report))
    assert not summary["human_semantic_passed"]
    assert not summary["evaluated_case_acceptance_passed"]


@pytest.mark.parametrize("where", ["kind", "verdict"])
def test_malformed_edit_has_a_controlled_validation_error(where):
    report = report_fixture()
    review = filled(report)
    if where == "kind": review["reviewer"]["kind"] = []
    else: review["cases"][0]["criteria"][0]["verdict"] = {}
    with pytest.raises(ValueError):
        validate_review(report, review)


def test_cli_exclusive_sidecar_preserves_inputs(tmp_path):
    report_path = tmp_path / "result.json"
    review_path = tmp_path / "review.json"
    summary_path = tmp_path / "summary.json"
    report = report_fixture()
    report_path.write_text(json.dumps(report), encoding="utf-8")
    original = report_path.read_bytes()
    assert main(["--report", str(report_path), "--output", str(review_path)]) == 0
    template_bytes = review_path.read_bytes()
    assert main(["--report", str(report_path), "--output", str(review_path)]) == 1
    assert review_path.read_bytes() == template_bytes
    assert main(["--report", str(report_path), "--review", str(review_path), "--output", str(summary_path)]) == 0
    assert not json.loads(summary_path.read_text())["human_semantic_passed"]
    assert report_path.read_bytes() == original
