"""Fail-closed acceptance gate for repeated, current-source gateway evaluations.

This command performs no provider calls, deployment or LangSmith writes. It
checks existing immutable experiment reports and evidence-bound human reviews.
A passing text gate never certifies unheard audio or an untested phone release.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

from .review import summarize_review
from .schema import digest, gate, select_cases, validate_dataset

HASH=re.compile(r"[0-9a-f]{64}")


def evaluate_text_gate(report,review,*,required_cases,expected_source,
                       expected_dataset_sha256,expected_configuration_sha256,min_attempts=3):
    """Validate all required trials; neither a best-of-N sample nor judge label wins.

    expected_source must come from the candidate source tree, not the report.
    Configuration here means the text runner's recorded settings; it does not
    imply the live Vapi assistant used those settings or validate its voice.
    """
    if (not isinstance(required_cases,list) or not required_cases
            or any(not isinstance(c,str) or not c for c in required_cases)
            or len(set(required_cases))!=len(required_cases)
            or type(min_attempts) is not int or not 1<=min_attempts<=20
            or not isinstance(expected_source,dict) or not expected_source
            or any(not isinstance(k,str) or not isinstance(v,str) or HASH.fullmatch(v) is None for k,v in expected_source.items())
            or not all(isinstance(v,str) and HASH.fullmatch(v) for v in (expected_dataset_sha256,expected_configuration_sha256))):
        raise ValueError("invalid_release_gate_policy")
    if not isinstance(report,dict) or not isinstance(review,dict):raise ValueError("invalid_release_evidence")
    blockers=[]
    mechanical=gate(report)
    if report.get("mode")!="model" or report.get("status")!="complete":blockers.append("model_execution_incomplete")
    if not mechanical["mechanical_checks_passed"]:blockers.append("mechanical_checks_failed_or_incomplete")
    source=report.get("source") if isinstance(report.get("source"),dict) else {}
    if source.get("before")!=expected_source or source.get("after")!=expected_source:
        blockers.append("candidate_source_does_not_match_executed_source")
    dataset=report.get("dataset") if isinstance(report.get("dataset"),dict) else {}
    if dataset.get("sha256")!=expected_dataset_sha256:
        blockers.append("candidate_dataset_does_not_match_executed_dataset")
    configuration=report.get("configuration",{})
    if not isinstance(configuration,dict) or configuration.get("transport")!="streaming_gateway":
        blockers.append("full_gateway_was_not_exercised")
    if digest(configuration)!=expected_configuration_sha256:
        blockers.append("candidate_text_configuration_does_not_match")
    identities=Counter()
    for row in report.get("cases",[]) if isinstance(report.get("cases"),list) else []:
        if isinstance(row,dict) and isinstance(row.get("id"),str) and type(row.get("attempt")) is int:
            identities[(row["id"],row["attempt"])]+=1
    trials=[]
    for cid in required_cases:
        attempts=sorted(attempt for (name,attempt),count in identities.items() if name==cid and count==1)
        enough=len(attempts)>=min_attempts and attempts==list(range(1,len(attempts)+1))
        if not enough:blockers.append("missing_or_nonconsecutive_required_trials")
        trials.append({"case_id":cid,"observed_attempts":attempts,"minimum_attempts":min_attempts,"coverage_satisfied":enough})
    try:
        semantic=summarize_review(report,review)
        if not semantic["human_semantic_passed"]:blockers.append("human_semantic_review_failed_or_incomplete")
        if not semantic["evaluated_case_acceptance_passed"]:blockers.append("case_acceptance_not_established")
        semantic_summary={k:semantic[k] for k in ("review_status","reviewer_kind","reviewer_identified","case_count","judgment_counts")}
    except (ValueError,KeyError,TypeError):
        blockers.append("human_review_binding_or_evidence_invalid")
        semantic_summary={"review_status":"invalid_or_unavailable"}
    return {"schema_version":1,"gate":"repeated_current_source_gateway_text_acceptance",
        "text_acceptance_passed":not blockers,"blockers":sorted(set(blockers)),"required_trials":trials,
        "report_sha256":digest(report),"review_sha256":digest(review),
        "expected_source_manifest_sha256":digest(expected_source),"expected_dataset_sha256":expected_dataset_sha256,
        "expected_text_configuration_sha256":expected_configuration_sha256,"semantic_review":semantic_summary,
        "production_release_ready":False,
        "remaining_production_evidence":["Effective deployed configuration and release fingerprint verified on the actual call path.",
            "Representative repeated voice and phone journeys, real audio and interruption acceptance.",
            "Call-to-turn-to-operation diagnostic coverage and live evaluator health readback.",
            "Independent business-policy and judge calibration review; real downstream integration acceptance if used."],
        "limitations":["Human identity in the supplied review is self-declared, not authenticated by this local tool.",
            "A text pass covers only the listed dataset, exact candidate source, settings and trials.",
            "This gate neither deploys nor changes the original report, grades, scheduler or provider settings."]}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report",type=Path,required=True)
    parser.add_argument("--review",type=Path,required=True,help="Filled evidence-bound human semantic review sidecar.")
    parser.add_argument("--dataset",type=Path,required=True)
    parser.add_argument("--expected-configuration-sha256",required=True,help="Digest of the intended text runner configuration, frozen before executing the candidate.")
    parser.add_argument("--min-attempts",type=int,default=3)
    parser.add_argument("--output",type=Path,required=True,help="New result path; existing files are never overwritten.")
    args=parser.parse_args(argv)
    from .run import source_manifest
    raw=args.dataset.read_bytes();dataset=validate_dataset(json.loads(raw))
    report=json.loads(args.report.read_text(encoding="utf-8"));review=json.loads(args.review.read_text(encoding="utf-8"))
    result=evaluate_text_gate(report,review,required_cases=[c["id"] for c in select_cases(dataset)],
        expected_source=source_manifest(),expected_dataset_sha256=hashlib.sha256(raw).hexdigest(),
        expected_configuration_sha256=args.expected_configuration_sha256,min_attempts=args.min_attempts)
    with args.output.open("x",encoding="utf-8",newline="\n") as f:
        json.dump(result,f,indent=2,ensure_ascii=False,allow_nan=False);f.write("\n")
    print(json.dumps({"text_acceptance_passed":result["text_acceptance_passed"],"blockers":result["blockers"],
        "production_release_ready":False,"output":str(args.output.resolve())}))
    return 0 if result["text_acceptance_passed"] else 1


if __name__=="__main__":raise SystemExit(main())
