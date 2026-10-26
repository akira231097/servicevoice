"""Offline control/provenance contracts; these tests do not grade a live model."""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest

from app.live_eval_evidence import DIMENSIONS, VERSION
from app.live_evaluations import (JUDGE_PROMPT_REVISION, JUDGE_PROMPT_SHA256,
                                  JUDGE_SYSTEM, parse_judgment, public_report)
from evals.vehicle_grounding_controls import SOURCE, vehicle_grounding_controls


EXPECTED = {
    "unverified_manual_assertion": "fail",
    "caller_confirms_manual": "pass",
    "question_not_vehicle_evidence": "fail",
    "explicit_specification_limit": "pass",
    "ambiguous_preference_interpretation": "uncertain",
}


def authored_judgment(control):
    # Schema probe only. These values are authored, never presented as a model's result.
    target = control["expected"]
    value = {dimension: {"verdict": "not_applicable", "issue_code": "none",
                         "evidence_refs": ["state"], "reason": "Authored schema probe only."}
             for dimension in DIMENSIONS}
    value["hallucination_free"] = {"verdict": target,
        "issue_code": "none" if target == "pass" else "incomplete_evidence" if target == "uncertain" else "other",
        "evidence_refs": ["facts", "turn:1", "turn:2"], "reason": "Authored schema probe only."}
    return value


def test_five_distinct_target_controls_have_explicit_authored_labels():
    controls = vehicle_grounding_controls()
    assert len(controls) == len({c["id"] for c in controls}) == 5
    assert {c["id"]: c["expected"] for c in controls} == EXPECTED
    assert all(c["source"] == SOURCE and c["dimension"] == "hallucination_free" for c in controls)
    assert all(c["label_basis"] for c in controls)


def test_each_control_has_its_own_empty_business_state_and_no_verified_vehicle_source():
    controls = vehicle_grounding_controls()
    references = []
    for control in controls:
        package = control["package"]
        assert package["rubric_version"] == VERSION
        assert package["dimensions"] == list(DIMENSIONS)
        state = package["evidence"]["state"]
        assert state["scheduler_evidence_available"] is True  # An explicitly authored empty fixture.
        for key in ("appointments", "proposals", "handoffs", "pending_operations"):
            assert state[key] == []
        assert not any(key.startswith("tool:") for key in package["evidence"])
        reference = package["evidence"]["facts"]["vehicle_specification_reference"]
        assert all(reference[key] is None for key in (
            "verified_vin", "verified_trim", "verified_transmission", "verified_engine", "verified_equipment"))
        references.append(reference)
        assert package["mechanical_checks"]["booking_integrity"]["verdict"] == "not_applicable"
        assert package["mechanical_checks"]["confirmation_order"]["verdict"] == "not_applicable"
    assert all(reference == references[0] for reference in references)
    controls[0]["package"]["evidence"]["facts"]["vehicle_specification_reference"]["verified_trim"] = "changed"
    assert controls[1]["package"]["evidence"]["facts"]["vehicle_specification_reference"]["verified_trim"] is None
    assert vehicle_grounding_controls()[0]["package"]["evidence"]["facts"]["vehicle_specification_reference"]["verified_trim"] is None


def test_labels_and_rationales_are_not_leaked_to_judge_evidence():
    forbidden = {"expected", "label_basis", "expected_verdict", "gold_label"}
    def keys(value):
        if isinstance(value, dict):
            for key, child in value.items():
                yield key
                yield from keys(child)
        elif isinstance(value, list):
            for child in value:
                yield from keys(child)
    for control in vehicle_grounding_controls():
        assert not forbidden.intersection(keys(control["package"]))
        encoded = json.dumps(control["package"], ensure_ascii=False)
        assert control["label_basis"] not in encoded
        assert "Authored text" in encoded


@pytest.mark.parametrize("control", vehicle_grounding_controls(), ids=lambda control: control["id"])
def test_packages_match_the_actual_six_dimension_judgment_parser(control):
    judgment = authored_judgment(control)
    assert parse_judgment(judgment, control["package"]) == judgment
    assert set(judgment) == set(DIMENSIONS)
    assert set(judgment["hallucination_free"]["evidence_refs"]) <= set(control["package"]["evidence"])


def test_prompt_revision_digest_and_exported_provenance_match_without_caller_text():
    assert JUDGE_PROMPT_REVISION == "2026-10-25-caller-id-4"
    assert JUDGE_PROMPT_SHA256 == hashlib.sha256(JUDGE_SYSTEM.encode("utf-8")).hexdigest()
    control = vehicle_grounding_controls()[0]
    original = deepcopy(control["package"])
    report = public_report(control["package"], authored_judgment(control),
                           {"id": "authored-control", "type": "webCall", "endedReason": "customer-ended-call"},
                           [], SimpleNamespace(latency_target_ms=2000))
    assert control["package"] == original
    process = report["review_process"]
    assert process["judge_prompt_revision"] == JUDGE_PROMPT_REVISION
    assert process["judge_prompt_sha256"] == JUDGE_PROMPT_SHA256
    assert process["advisory"] is True
    assert process["human_calibration"] == "not_established_by_this_review"
    assert "Your 2022 Nissan Altima has a manual transmission" not in json.dumps(report)
    assert report["evidence_coverage"]["spoken_audio_verified"] is None
