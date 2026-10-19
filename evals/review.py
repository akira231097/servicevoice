"""Local, evidence-bound semantic review sidecars; no network or model judge.

The original experiment report is immutable input. Review verdicts are separate
from mechanical checks, and assistant/judge reviews are always advisory here.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import re

from .schema import digest, gate


DIMENSIONS = (
    "factual_grounding", "action_truthfulness", "task_completion",
    "consent_and_corrections", "conversation_quality",
)
VERDICTS = {"pending", "pass", "fail", "uncertain"}
KINDS = {"human", "assistant", "judge"}
HASH = re.compile(r"[0-9a-f]{64}")


def _need(condition, message):
    if not condition:
        raise ValueError(message)


def _identity(report):
    _need(isinstance(report, dict), "Report must be an object")
    _need(isinstance(report.get("run_id"), str) and bool(report["run_id"]), "Report run identity missing")
    dataset = report.get("dataset")
    _need(isinstance(dataset, dict) and isinstance(dataset.get("sha256"), str)
          and HASH.fullmatch(dataset["sha256"]) is not None, "Report dataset digest missing")
    return report["run_id"], dataset["sha256"], digest(report)


def _eligible(report):
    """No semantic claim is generated for contract-only cases without a rubric."""
    cases = report.get("cases")
    _need(isinstance(cases, list), "Report cases must be a list")
    selected, seen = [], set()
    for case in cases:
        _need(isinstance(case, dict), "Invalid report case")
        cid, attempt = case.get("id"), case.get("attempt")
        _need(isinstance(cid, str) and bool(cid) and type(attempt) is int and attempt > 0,
              "Invalid case identity")
        key = (cid, attempt)
        _need(key not in seen, "Duplicate report case identity")
        seen.add(key)
        expected = case.get("expected")
        if expected is None:
            continue
        _need(isinstance(expected, dict), "Invalid report expectations")
        rubric = expected.get("semantic_rubric")
        _need(isinstance(rubric, list) and bool(rubric)
              and all(isinstance(text, str) and bool(text) for text in rubric),
              "Missing or invalid semantic rubric")
        # Missing outputs can still receive a review template. They can never
        # satisfy the evidence prerequisite for a semantic pass.
        actual_digest = digest(case.get("outputs"))
        _need(case.get("output_sha256", actual_digest) == actual_digest,
              "Report output digest does not match its evidence")
        selected.append(case)
    return selected


def _judgment():
    return {"verdict": "pending", "evidence": ""}


def make_review_template(report):
    """Build an unsigned, unreviewed sidecar without changing the report."""
    run_id, dataset_hash, report_hash = _identity(report)
    cases = []
    for case in _eligible(report):
        cases.append({
            "id": case["id"], "attempt": case["attempt"],
            "output_sha256": digest(case.get("outputs")),
            "criteria": [
                {"id": f"criterion_{index:02d}", "text": text, **_judgment()}
                for index, text in enumerate(case["expected"]["semantic_rubric"], 1)
            ],
            "dimensions": {dimension: _judgment() for dimension in DIMENSIONS},
            "notes": "",
        })
    return {
        "schema_version": 1, "review_type": "servicevoice_semantic_review",
        "run_id": run_id, "dataset_sha256": dataset_hash,
        "report_sha256": report_hash,
        "reviewer": {"kind": "human", "name": "", "reviewed_at": None},
        "instructions": [
            "All judgments start pending. Read saved inputs, outputs, tool results and state before grading.",
            "For pass, fail or uncertain, cite a turn, tool result or state fact and explain the judgment.",
            "Do not infer audible behavior from text, captions or a correct stored value.",
            "Review every criterion and dimension. Missing evidence is uncertain, never pass.",
            "Record the actual reviewer kind. Assistant and judge reviews remain advisory.",
            "Reviewer identity is self-declared local metadata, not cryptographically verified attribution.",
            "Keep the original experiment report unchanged; save this as a separate local file.",
        ],
        "cases": cases,
    }


def _validate_judgment(judgment):
    _need(isinstance(judgment, dict), "Invalid judgment")
    _need(isinstance(judgment.get("verdict"), str) and judgment["verdict"] in VERDICTS, "Unknown review verdict")
    evidence = judgment.get("evidence")
    _need(isinstance(evidence, str) and len(evidence) <= 8000, "Invalid judgment evidence")
    if judgment["verdict"] != "pending":
        _need(bool(evidence.strip()), "A graded judgment requires evidence")


def validate_review(report, review):
    """Raise ValueError for stale, incomplete-contract or mismatched sidecars.

    Pending judgments are valid drafts. They never imply a semantic pass.
    """
    run_id, dataset_hash, report_hash = _identity(report)
    eligible = _eligible(report)
    _need(isinstance(review, dict), "Review must be an object")
    _need(type(review.get("schema_version")) is int and review["schema_version"] == 1
          and review.get("review_type") == "servicevoice_semantic_review", "Unsupported review schema")
    for field, value in (("run_id", run_id), ("dataset_sha256", dataset_hash), ("report_sha256", report_hash)):
        _need(review.get(field) == value, "Review binding mismatch: " + field)
    reviewer = review.get("reviewer")
    _need(isinstance(reviewer, dict) and set(reviewer) == {"kind", "name", "reviewed_at"}, "Invalid reviewer contract")
    _need(isinstance(reviewer["kind"], str) and reviewer["kind"] in KINDS, "Unknown reviewer kind")
    _need(isinstance(reviewer["name"], str) and len(reviewer["name"]) <= 200, "Invalid reviewer name")
    reviewed_at = reviewer["reviewed_at"]
    if reviewed_at is not None:
        _need(isinstance(reviewed_at, str), "Invalid review timestamp")
        try:
            stamp = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("Invalid review timestamp") from None
        _need(stamp.tzinfo is not None, "Review timestamp requires timezone")
    rows = review.get("cases")
    _need(isinstance(rows, list) and len(rows) == len(eligible), "Review case coverage mismatch")
    source = {(case["id"], case["attempt"]): case for case in eligible}
    seen = set()
    for row in rows:
        _need(isinstance(row, dict), "Invalid review case")
        _need(isinstance(row.get("id"), str) and type(row.get("attempt")) is int,
              "Invalid review case identity")
        key = (row["id"], row["attempt"])
        _need(key in source and key not in seen, "Unknown or duplicate review case")
        seen.add(key)
        case = source[key]
        _need(row.get("output_sha256") == digest(case.get("outputs")), "Review output evidence is stale")
        rubric = case["expected"]["semantic_rubric"]
        criteria = row.get("criteria")
        _need(isinstance(criteria, list) and len(criteria) == len(rubric), "Review criterion coverage mismatch")
        for index, (criterion, text) in enumerate(zip(criteria, rubric), 1):
            _need(isinstance(criterion, dict) and set(criterion) == {"id", "text", "verdict", "evidence"},
                  "Invalid criterion contract")
            _need(criterion["id"] == f"criterion_{index:02d}" and criterion["text"] == text,
                  "Review criterion differs from frozen rubric")
            _validate_judgment(criterion)
        dimensions = row.get("dimensions")
        _need(isinstance(dimensions, dict) and set(dimensions) == set(DIMENSIONS), "Review dimension coverage mismatch")
        for judgment in dimensions.values():
            _need(isinstance(judgment, dict) and set(judgment) == {"verdict", "evidence"}, "Invalid dimension contract")
            _validate_judgment(judgment)
        _need(isinstance(row.get("notes"), str) and len(row["notes"]) <= 10000, "Invalid case notes")
    return review


def summarize_review(report, review):
    """Summarize separately; neither a judge nor this helper edits run results."""
    validate_review(report, review)
    eligible = _eligible(report)
    source = {(case["id"], case["attempt"]): case for case in eligible}
    reviewer = review["reviewer"]
    identified = bool(reviewer["name"].strip()) and reviewer["reviewed_at"] is not None
    human = reviewer["kind"] == "human" and identified
    rows, counts = [], Counter()
    dimension_counts = {dimension: Counter() for dimension in DIMENSIONS}
    for row in review["cases"]:
        case = source[(row["id"], row["attempt"])]
        verdicts = [criterion["verdict"] for criterion in row["criteria"]]
        verdicts += [judgment["verdict"] for judgment in row["dimensions"].values()]
        counts.update(verdicts)
        for dimension, judgment in row["dimensions"].items():
            dimension_counts[dimension][judgment["verdict"]] += 1
        outputs = case.get("outputs")
        turns = outputs.get("turns") if isinstance(outputs, dict) else None
        observed = (case.get("status") == "completed" and isinstance(turns, list) and bool(turns)
                    and all(isinstance(turn, dict)
                            and isinstance(turn.get("user"), str) and bool(turn["user"].strip())
                            and isinstance(turn.get("assistant"), str) and bool(turn["assistant"].strip())
                            for turn in turns))
        input_turns = (case.get("inputs") or {}).get("turns")
        if isinstance(input_turns, list):
            observed = observed and len(turns or []) == len(input_turns)
        if "fail" in verdicts:
            status = "failed"
        elif "pending" in verdicts:
            status = "pending"
        elif "uncertain" in verdicts or not observed:
            status = "insufficient_evidence"
        else:
            status = "passed"
        mechanical = bool(case.get("checks")) and all(check.get("passed") is True for check in case["checks"])
        rows.append({"id": row["id"], "attempt": row["attempt"],
                     "review_verdict": status, "human_semantic_passed": human and status == "passed",
                     "completed_output_evidence_present": observed,
                     "mechanical_checks_passed": mechanical})
    semantic_passed = bool(rows) and human and all(row["human_semantic_passed"] for row in rows)
    mechanical_gate = gate(report)["mechanical_checks_passed"]
    status = ("not_applicable" if not rows else "advisory" if reviewer["kind"] != "human"
              else "passed" if semantic_passed else "failed" if any(row["review_verdict"] == "failed" for row in rows)
              else "pending_or_insufficient_evidence")
    return {
        "schema_version": 1, "scope": "semantic_review_sidecar_only",
        "run_id": report["run_id"], "dataset_sha256": report["dataset"]["sha256"],
        "report_sha256": digest(report), "review_sha256": digest(review),
        "review_status": status, "reviewer_kind": reviewer["kind"],
        "reviewer_identified": identified, "human_semantic_passed": bool(semantic_passed),
        "case_count": len(rows), "judgment_counts": dict(counts),
        "dimension_verdict_counts": {name: dict(values) for name, values in dimension_counts.items()},
        "mechanical_checks_passed": mechanical_gate,
        "evaluated_case_acceptance_passed": bool(semantic_passed and mechanical_gate),
        "release_ready": False, "cases": rows,
        "limitations": [
            "Human identity is self-declared local review metadata, not independently authenticated.",
            "Assistant and model-judge reviews are advisory; no automatic semantic pass is permitted.",
            "A semantic pass does not repair failed mechanical checks or certify untested audio, routing, deployment or integration layers.",
            "This sidecar preserves the original experiment report unchanged.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Create or validate a local semantic-review sidecar.")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--review", type=Path, help="Filled sidecar to validate; omit to create a pending template.")
    parser.add_argument("--output", required=True, type=Path, help="New output path; existing files are never replaced.")
    args = parser.parse_args(argv)
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        if args.review is None:
            result = make_review_template(report)
        else:
            review = json.loads(args.review.read_text(encoding="utf-8"))
            result = summarize_review(report, review)
        # Never create intermediate directories or rewrite an input/evidence file.
        with args.output.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
    except (ValueError, OSError) as exc:
        if isinstance(exc, ValueError):
            print("Review validation failed: " + str(exc))
        else:
            print("Review file operation failed; use readable inputs and a new output filename.")
        return 1
    print(json.dumps({"output": str(args.output), "kind": "summary" if args.review else "pending_template",
                      "release_ready": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
