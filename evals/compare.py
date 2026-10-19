"""Compare matched local experiments without hiding missing or failed attempts."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .schema import digest, gate
from .metrics import summarize_cases


def compare_runs(baseline, candidate):
    reasons = []
    for key in ("mode",):
        if baseline.get(key) != candidate.get(key):
            reasons.append("different_" + key)
    if baseline.get("dataset", {}).get("sha256") != candidate.get("dataset", {}).get("sha256"):
        reasons.append("different_dataset")
    if baseline.get("configuration", {}).get("effective_date") != candidate.get("configuration", {}).get("effective_date"):
        reasons.append("different_effective_date")
    if baseline.get("template_values") != candidate.get("template_values"):
        reasons.append("different_rendered_inputs")
    selections = []
    for report in (baseline, candidate):
        selections.append(sorted((s["id"], s["attempt"]) for s in report.get("selection", [])))
        evidence_gate = gate(report)
        if not evidence_gate["source_and_dataset_stable"]:
            reasons.append("unstable_source_or_dataset")
        if not evidence_gate["selected_coverage_complete"]:
            reasons.append("incomplete_or_duplicate_case_coverage")
    if selections[0] != selections[1]:
        reasons.append("different_trial_selection")
    old = {(c["id"], c["attempt"]): c for c in baseline.get("cases", [])}
    new = {(c["id"], c["attempt"]): c for c in candidate.get("cases", [])}
    changes = []
    for identity in sorted(set(old) & set(new)):
        before, after = old[identity], new[identity]
        if before.get("inputs") != after.get("inputs") or before.get("expected") != after.get("expected"):
            reasons.append("different_case_inputs_or_oracles")
        before_checks = {c["name"]: c.get("passed") for c in before.get("checks", [])}
        after_checks = {c["name"]: c.get("passed") for c in after.get("checks", [])}
        missing = sorted(set(before_checks) - set(after_checks))
        regressions = sorted(k for k in before_checks if before_checks[k] is True and after_checks.get(k) is not True)
        changes.append({"id": identity[0], "attempt": identity[1], "baseline_status": before.get("status"),
                        "candidate_status": after.get("status"), "new_failed_or_missing_checks": regressions,
                        "missing_checks": missing, "candidate_failed_checks": sorted(k for k, v in after_checks.items() if v is not True)})
    return {"schema_version": 1, "baseline_run_id": baseline.get("run_id"), "candidate_run_id": candidate.get("run_id"),
            "baseline_report_sha256": digest(baseline), "candidate_report_sha256": digest(candidate),
            "comparable": not reasons, "incomparability_reasons": sorted(set(reasons)),
            "case_changes": changes, "baseline_metrics": summarize_cases(baseline.get("cases", [])),
            "candidate_metrics": summarize_cases(candidate.get("cases", [])),
            "candidate_mechanical_gate": gate(candidate), "release_ready": False,
            "limitations": ["Latency summaries are descriptive; small samples do not establish statistical improvement.",
                            "Model, prompt and source changes are permitted comparisons; dataset, date and case inputs must match.",
                            "No automatic semantic or acoustic improvement claim follows from these metrics."]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = compare_runs(json.loads(args.baseline.read_text(encoding="utf-8")), json.loads(args.candidate.read_text(encoding="utf-8")))
    with args.output.open("x", encoding="utf-8", newline="\n") as f:
        json.dump(result, f, indent=2, ensure_ascii=False, allow_nan=False)
        f.write("\n")
    print(json.dumps({"comparable": result["comparable"], "output": str(args.output)}))
    return 0 if result["comparable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
