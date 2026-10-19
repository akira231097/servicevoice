from copy import deepcopy

from evals.compare import compare_runs


def report():
    return {"run_id": "test", "mode": "model", "dataset": {"sha256": "a"*64, "sha256_after": "a"*64},
            "source": {"before": {"app.py": "a"*64}, "after": {"app.py": "a"*64}},
            "configuration": {"effective_date": "2026-10-18"}, "template_values": {"DATE": "2026-10-19"},
            "selection": [{"id": "a", "attempt": 1}],
            "cases": [{"id": "a", "attempt": 1, "status": "completed", "checks": [{"name": "consent", "passed": True}], "inputs": {"turns": ["hello"]}}]}


def test_comparison_allows_new_source_but_not_new_dataset():
    old, new = report(), report()
    new["source"] = {"before": {"app.py": "b"*64}, "after": {"app.py": "b"*64}}
    assert compare_runs(old, new)["comparable"]
    new["dataset"] = {"sha256": "b"*64, "sha256_after": "b"*64}
    assert not compare_runs(old, new)["comparable"]


def test_missing_assertion_is_visible_regression():
    old, new = report(), report()
    new["cases"][0]["checks"] = []
    result = compare_runs(old, new)
    assert result["case_changes"][0]["new_failed_or_missing_checks"] == ["consent"]
    assert not result["candidate_mechanical_gate"]["mechanical_checks_passed"]


def test_missing_trial_and_duplicate_never_comparable():
    old, new = report(), report()
    new["cases"] = []
    assert not compare_runs(old, new)["comparable"]
    new = report()
    new["cases"].append(deepcopy(new["cases"][0]))
    assert not compare_runs(old, new)["comparable"]


def test_dates_or_case_inputs_cannot_silently_differ():
    for field in ("date", "inputs"):
        old, new = report(), report()
        if field == "date":
            new["configuration"]["effective_date"] = "2026-10-19"
        else:
            new["cases"][0]["inputs"]["turns"] = ["yes book"]
        assert not compare_runs(old, new)["comparable"]


def test_semantics_never_inferred_from_all_green_checks():
    result = compare_runs(report(), report())
    assert result["comparable"]
    assert not result["release_ready"]
