"""Release acceptance must bind actual source, repetitions, and human evidence."""
from copy import deepcopy

import pytest

from evals.release_gate import evaluate_text_gate
from evals.review import make_review_template
from evals.schema import digest


def evidence():
    outputs={"turns":[{"user":"Book the corrected appointment.","assistant":"The corrected appointment is saved."}],
             "tools":[],"state":{"appointments":1}}
    report={"run_id":"synthetic-release-test","mode":"model","status":"complete",
        "configuration":{"transport":"streaming_gateway","model":"synthetic-test-model"},
        "dataset":{"sha256":"a"*64,"sha256_after":"a"*64},
        "source":{"before":{"source.py":"b"*64},"after":{"source.py":"b"*64}},
        "selection":[{"id":"SV-CRITICAL","attempt":i} for i in range(1,4)],"cases":[]}
    for i in range(1,4):
        report["cases"].append({"id":"SV-CRITICAL","attempt":i,"status":"completed",
            "inputs":{"turns":["Book the corrected appointment."]},"expected":{"semantic_rubric":["The corrected booking is saved after assent."]},
            "outputs":deepcopy(outputs),"output_sha256":digest(outputs),
            "checks":[{"name":"confirmation_required","passed":True}],"semantic_review":{"status":"pending"}})
    return report,human_review(report)


def human_review(report):
    review=make_review_template(report)
    review["reviewer"]={"kind":"human","name":"Synthetic Test Reviewer","reviewed_at":"2026-10-20T18:00:00+00:00"}
    for row in review["cases"]:
        for item in [*row["criteria"],*row["dimensions"].values()]:
            item.update(verdict="pass",evidence="Synthetic fixture: recorded turn and final state match the required result.")
    return review


def run(report,review,**changes):
    policy={"required_cases":["SV-CRITICAL"],"expected_source":{"source.py":"b"*64},
        "expected_dataset_sha256":"a"*64,"expected_configuration_sha256":digest(report["configuration"]),"min_attempts":3}
    policy.update(changes)
    return evaluate_text_gate(report,review,**policy)


def test_repeated_current_gateway_and_bound_human_review_pass_only_the_text_stage():
    report,review=evidence();result=run(report,review)
    assert result["text_acceptance_passed"] and result["blockers"]==[]
    assert result["production_release_ready"] is False and result["remaining_production_evidence"]


@pytest.mark.parametrize("verdict",["fail","uncertain","pending"])
def test_one_bad_confirmation_or_task_grade_blocks_acceptance(verdict):
    report,review=evidence();review["cases"][1]["dimensions"]["consent_and_corrections"]["verdict"]=verdict
    assert not run(report,review)["text_acceptance_passed"]


def test_known_workflow_failure_cannot_be_overridden_by_human_or_judge_pass():
    report,_=evidence();report["cases"][1]["checks"][0]["passed"]=False
    result=run(report,human_review(report))
    assert "mechanical_checks_failed_or_incomplete" in result["blockers"]


@pytest.mark.parametrize("change",["single_trial","missing_case","nonconsecutive","duplicate"])
def test_best_of_or_incomplete_trial_selection_is_not_release_evidence(change):
    report,_=evidence()
    if change=="single_trial":report["cases"]=report["cases"][:1];report["selection"]=report["selection"][:1]
    elif change=="missing_case":
        for row in [*report["cases"],*report["selection"]]:row["id"]="OTHER"
    elif change=="nonconsecutive":report["cases"][-1]["attempt"]=9;report["selection"][-1]["attempt"]=9
    else:report["cases"].append(deepcopy(report["cases"][0]))
    result=run(report,human_review(report)) if change!="duplicate" else run(report,{})
    assert not result["text_acceptance_passed"]


def test_stable_but_old_source_is_not_current_candidate_proof():
    report,review=evidence()
    result=run(report,review,expected_source={"source.py":"c"*64})
    assert "candidate_source_does_not_match_executed_source" in result["blockers"]


def test_old_settings_or_dataset_cannot_be_substituted():
    report,review=evidence()
    assert not run(report,review,expected_configuration_sha256="d"*64)["text_acceptance_passed"]
    assert not run(report,review,expected_dataset_sha256="e"*64)["text_acceptance_passed"]


def test_direct_model_success_does_not_exercise_routing_and_harness():
    report,_=evidence();report["configuration"]["transport"]="direct_nonstreaming_model_http"
    result=run(report,human_review(report))
    assert "full_gateway_was_not_exercised" in result["blockers"]


def test_automated_judge_is_not_independent_human_acceptance():
    report,review=evidence();review["reviewer"]["kind"]="judge"
    assert "human_semantic_review_failed_or_incomplete" in run(report,review)["blockers"]


def test_stale_review_binding_is_rejected_even_if_its_verdicts_are_green():
    report,review=evidence();report["cases"][0]["outputs"]["turns"][0]["assistant"]="Different answer."
    assert "human_review_binding_or_evidence_invalid" in run(report,review)["blockers"]


@pytest.mark.parametrize("field",["source","dataset","cases","configuration"])
def test_malformed_evidence_is_blocked_without_becoming_a_pass(field):
    report,review=evidence();configuration=digest(report['configuration']);report[field]=None
    result=run(report,review,expected_configuration_sha256=configuration)
    assert not result['text_acceptance_passed'] and result['blockers']


@pytest.mark.parametrize("policy",[{"min_attempts":True},{"min_attempts":0},{"required_cases":[]},
    {"required_cases":["A","A"]},{"expected_source":{}},{"expected_configuration_sha256":"unknown"}])
def test_invalid_or_weakened_gate_policy_cannot_produce_success(policy):
    report,review=evidence()
    with pytest.raises(ValueError):run(report,review,**policy)
