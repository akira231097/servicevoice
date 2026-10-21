"""Reference equality uses real returned evidence, without exposing opaque IDs."""
from copy import deepcopy
import hashlib
import json

import pytest

from evals import judge, run
from evals.schema import digest
from test_eval_runner import execute, reply
from test_eval_judge import fixture as judge_fixture
from test_eval_langsmith import fixture as export_fixture, build


REFERENCE = "CSV-A1B2C3D4"
OTHER = "CSV-01234567"


def hashed(value):
    return hashlib.sha256(value.encode()).hexdigest()


def tool(reference=REFERENCE, *, turn=1):
    return {"name": "get_appointment", "ok": True, "user_turn": turn,
            "result_reference_sha256": hashed(reference),
            "result": {"ok": True, "appointment": {"reference": "local_object_1"}}}


def output(text, tools=None):
    return {"turns": [{"assistant": text}], "tools": [tool()] if tools is None else tools}


@pytest.mark.parametrize("reference", ["", "CSV-123", "CSV-ABCDEFGH", "csv-a1b2c3d4",
                                        REFERENCE + "EXTRA", "private-customer-reference", None, 123])
def test_only_exact_bounded_server_reference_is_hashed(reference):
    assert run.result_reference_sha256({"ok": True, "appointment": {"reference": reference}}) is None


def test_failed_or_unstructured_result_cannot_supply_reference_evidence():
    for result in (None, [], {"ok": False, "appointment": {"reference": REFERENCE}},
                   {"ok": 1, "appointment": {"reference": REFERENCE}}, {"ok": True, "appointment": None}):
        assert run.result_reference_sha256(result) is None
    assert run.result_reference_sha256({"ok": True, "appointment": {"reference": REFERENCE}}) == hashed(REFERENCE)


@pytest.mark.parametrize("text,status,matching,mismatching", [
    ("Your reference is " + REFERENCE + ".", "match", 1, 0),
    ("Your reference is " + REFERENCE.lower() + ".", "match", 1, 0),
    ("Your reference is " + OTHER + ".", "mismatch", 0, 1),
    ("Not " + OTHER + "; the reference is " + REFERENCE + ".", "mismatch", 1, 1),
    ("The appointment is confirmed.", "not_mentioned", 0, 0),
    ("Your reference is " + REFERENCE + "EXTRA.", "not_mentioned", 0, 0),
    ("C S V dash A one B two C three D four.", "not_mentioned", 0, 0),
])
def test_literal_comparison_does_not_claim_to_grade_semantics(text, status, matching, mismatching):
    [row] = run.reference_readback_evidence(output(text))
    assert row["status"] == status and row["matching_candidates"] == matching
    assert row["mismatching_candidates"] == mismatching and row["source_tool"] == "tool:1"
    assert REFERENCE not in json.dumps(row) and hashed(REFERENCE) not in json.dumps(row)


def test_future_turn_result_cannot_verify_an_earlier_claim():
    evidence = output("Your reference is " + REFERENCE, [tool(turn=2)])
    evidence["turns"].append({"assistant": "Your reference is " + REFERENCE})
    rows = run.reference_readback_evidence(evidence)
    assert rows[0]["status"] == "unverifiable" and rows[0]["source_tool"] is None
    assert rows[1]["status"] == "match" and rows[1]["source_tool"] == "tool:1"


def test_latest_authoritative_appointment_invalidates_previous_comparison():
    [row] = run.reference_readback_evidence(output("Your reference is " + REFERENCE,
                                                [tool(), tool(OTHER)]))
    assert row["status"] == "mismatch" and row["source_tool"] == "tool:2"
    for replacement in (None, {"reference": "local_object_2"}):
        later = {"user_turn": 1, "ok": True, "result": {"appointment": replacement}}
        [row] = run.reference_readback_evidence(output("Your reference is " + REFERENCE, [tool(), later]))
        assert row["status"] == "unverifiable" and not row["expected_reference_available"]


@pytest.mark.parametrize("invalid_hash", [None, "", "f" * 63, "F" * 64, "private-secret", 123])
def test_legacy_or_invalid_hash_never_becomes_matching_evidence(invalid_hash):
    item = tool()
    if invalid_hash is None:
        item.pop("result_reference_sha256")
    else:
        item["result_reference_sha256"] = invalid_hash
    [row] = run.reference_readback_evidence(output(REFERENCE, [item]))
    assert row["status"] == "unverifiable" and row["matching_candidates"] is None


def test_failed_result_and_boolean_user_turn_cannot_override_known_reference():
    failed, invalid_turn = tool(OTHER), tool(OTHER)
    failed["ok"] = False
    invalid_turn["user_turn"] = True
    [row] = run.reference_readback_evidence(output(REFERENCE, [tool(), failed, invalid_turn]))
    assert row["status"] == "match" and row["source_tool"] == "tool:1"


@pytest.mark.parametrize("text", [REFERENCE + " " * 16000, (REFERENCE + " ") * 33, None])
def test_oversized_or_unsupported_text_is_unverifiable(text):
    [row] = run.reference_readback_evidence(output(text))
    assert row["status"] == "unverifiable" and row["matching_candidates"] is None


@pytest.mark.parametrize("wrong_reference", [False, True])
def test_mock_model_books_real_disposable_appointment_and_reference_is_checked(tmp_path, wrong_reference, monkeypatch):
    from scripts.evaluate_gemini import Conversation
    original_init = Conversation.__init__

    def reference_fixture(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        # Reference equality needs a completed disposable receipt, not a CI
        # disk-speed benchmark. Production keeps its strict 1.5-second waiter.
        self.harness.timeout_seconds = 30

    monkeypatch.setattr(Conversation, "__init__", reference_fixture)
    case = {"id": "synthetic-reference", "family": "receipt", "category": "confirmation", "split": "regression",
            "criticality": "critical", "execution": {"kind": "model_text"},
            "turns": ["Prepare the first available oil change for Taylor Reed, 2025550146, 2021 Toyota Camry. Ask before booking.",
                      "Yes, confirm that exact appointment."],
            "expected": {"turn_checks": [{"after_turn": 1, "appointments": 0}, {"after_turn": 2, "appointments": 1}],
                         "final": {"appointments": 1}, "required_tools": ["prepare_first_available_booking", "confirm_action"],
                         "forbidden_tools": [], "semantic_rubric": ["State the complete returned appointment reference."]}}
    requests, actual_references = [], []
    def provider(request):
        body = json.loads(request.content)
        requests.append(body)
        n = len(requests)
        if n == 1:
            return reply(tool="prepare_first_available_booking", arguments={"strategy": "first_available", "service": "oil_change",
                "customer_name": "Taylor Reed", "phone": "2025550146", "vehicle": "2021 Toyota Camry"}, index=n)
        results = [json.loads(m["content"]) for m in body["messages"] if m["role"] == "tool"]
        if n == 2:
            return reply(results[-1]["summary"] + " Would you like me to confirm it?")
        if n == 3:
            return reply(tool="confirm_action", arguments={"proposal_id": results[0]["proposal_id"], "confirmed": True}, index=n)
        reference = results[-1]["appointment"]["reference"]
        actual_references.append(reference)
        spoken = reference[:-1] + ("0" if reference[-1] != "0" else "1") if wrong_reference else reference
        return reply("Your appointment is confirmed. Your reference is " + spoken + ".")
    row, _, used = execute(tmp_path, provider, raw_case=case)
    diagnostic = {"status": row["status"], "error_code": row.get("error_code"), "requests": used,
                  "tool_errors": [item.get("error") for item in row["outputs"]["tools"]]}
    assert row["status"] == "completed" and used == 4 and all(c["passed"] for c in row["checks"]), diagnostic
    event = row["outputs"]["tools"][-1]
    assert event["result_reference_sha256"] == hashed(actual_references[0])
    assert event["result"]["appointment"]["reference"].startswith("local_object_")
    assert actual_references[0] not in json.dumps(row["outputs"]["tools"])
    assert row["outputs"]["reference_readback"][-1]["status"] == ("mismatch" if wrong_reference else "match")
    assert row["semantic_review"]["status"] == "pending"
    assert row["output_sha256"] == digest(row["outputs"])


def test_judge_recomputes_bound_facts_and_drops_reference_digest():
    _, _, report = judge_fixture()
    case = report["cases"][0]
    case["outputs"]["turns"][0]["assistant"] = "Your reference is " + OTHER
    case["outputs"]["tools"] = [tool()]
    case["outputs"]["reference_readback"] = [{"status": "match", "private": "DO-NOT-SEND"}]
    package = judge.build_evidence(report, case)
    fact = package["evidence"]["reference_readback:turn:1"]
    assert fact["status"] == "mismatch" and fact["mismatching_candidates"] == 1
    assert "result_reference_sha256" not in package["evidence"]["tool:1"]
    assert hashed(REFERENCE) not in json.dumps(package) and "DO-NOT-SEND" not in json.dumps(package)
    legacy = deepcopy(case)
    legacy["outputs"]["tools"][0].pop("result_reference_sha256")
    assert judge.build_evidence(report, legacy)["evidence"]["reference_readback:turn:1"]["status"] == "unverifiable"


def test_export_still_accepts_optional_evidence_and_does_not_export_its_digest():
    dataset, report, _, sha = export_fixture()
    row = report["cases"][0]
    row["outputs"]["tools"][0]["result_reference_sha256"] = hashed(REFERENCE)
    row["outputs"]["reference_readback"] = run.reference_readback_evidence(row["outputs"])
    row["output_sha256"] = digest(row["outputs"])
    payload = build(dataset, report, sha)
    assert "result_reference_sha256" not in json.dumps(payload)
    assert hashed(REFERENCE) not in json.dumps(payload)
