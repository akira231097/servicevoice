"""Native LangSmith experiment payloads with evidence-bound synthetic feedback.

Pure builders here perform no network calls. Upload is a separate explicit step
using the existing bounded, reserved-outcome experiment API integration. Judge
scores are advisory and never substitute for authoritative scheduler checks.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
import json
import re
import uuid

from .langsmith import build_payload, safe_text
from .schema import digest
from .review import DIMENSIONS
from .run import reference_readback_evidence
from .judge import clean as clean_evidence

EVALUATOR_VERSION = "servicevoice-native-grounding-2026-10-19-1"


def need(ok, code):
    if not ok:
        raise ValueError(code)


def feedback(key, score=None, *, value=None, comment="", model=False):
    need(re.fullmatch(r"[a-z][a-z0-9_.]{0,140}", key), "invalid_feedback_key")
    need(score is None or type(score) in (int, float) and score in {0, 1}, "invalid_binary_score")
    return {"key": key, "score": score, "value": value, "comment": safe_text(comment, 12000),
            "feedback_source": {"type": "model" if model else "api"},
            "extra": {"evaluator_version": EVALUATOR_VERSION,
                      "authority": "advisory_same_model_judge" if model else "recorded_execution_contract"}}


def code_feedback(case):
    """Recorded-state and literal-reference checks, not a general semantic judge."""
    output = case.get("outputs") or {}
    complete = case.get("status") == "completed" and len(output.get("turns", [])) == len(case["inputs"]["turns"])
    checks = case.get("checks") or []
    result = [feedback("code_execution_complete", int(complete)),
              feedback("code_mechanical_contract", int(complete and bool(checks) and all(c["passed"] is True for c in checks)))]
    confirmation = [c for c in checks if c["name"].startswith("confirmation_requires_prior_turn_proposal.")]
    result.append(feedback("code_confirmation_order", int(all(c["passed"] is True for c in confirmation)) if complete else 0,
        value="checked" if confirmation else "no_confirmation_attempt",
        comment="Checks that every observed confirm command refers to a proposal from an earlier user turn. This alone does not establish the meaning or audibility of assent."))
    references = reference_readback_evidence(output)
    mentions = [r for r in references if (r.get("candidate_count") or 0) > 0]
    bad = [r for r in mentions if r.get("status") != "match"]
    result.append(feedback("code_no_fabricated_reference", int(complete and not bad),
        value="matched" if mentions and not bad else "unverifiable_or_mismatch" if bad else "no_reference_spoken",
        comment="Literal CSV reference tokens must match the latest preceding returned-reference digest. No reference mention is not proof that a required full reference was read back. " + json.dumps(bad, ensure_ascii=False)))
    side_effects = [c for c in checks if c["name"].startswith(("turn_", "final.", "forbidden_tool_absent.", "arguments."))]
    result.append(feedback("code_state_and_arguments", int(complete and bool(side_effects) and all(c["passed"] is True for c in side_effects)),
        comment="Compares recorded scheduler checkpoints and requested arguments to this authored case's frozen oracle. " +
            json.dumps([c for c in side_effects if c["passed"] is not True], ensure_ascii=False, default=str)))
    return result


def bind_judge(report, review, result):
    need(review.get("run_id") == result.get("run_id") == report.get("run_id"), "judge_run_mismatch")
    need(review.get("report_sha256") == digest(report), "judge_review_digest_mismatch")
    need(review.get("dataset_sha256") == report["dataset"]["sha256"], "judge_review_dataset_mismatch")
    need(result.get("report_sha256") == digest(report), "judge_report_digest_mismatch")
    need(result.get("dataset_sha256") == report["dataset"]["sha256"], "judge_dataset_mismatch")
    index = {(c["id"], c["attempt"]): c for c in review["cases"]}
    states = {(c["id"], c["attempt"]): c for c in result["cases"]}
    return index, states


def judge_feedback(case, index, states):
    identity = case["id"], case["attempt"]
    reviewed, state = index.get(identity), states.get(identity)
    if state is not None:
        need(state.get("output_sha256") == case.get("output_sha256"), "judge_case_output_mismatch")
    available = bool(reviewed and state and state.get("status") == "judged_advisory")
    result = [feedback("judge_review_available", int(available), model=True,
                       comment="Same-model advisory review; calibration results are separate. Missing judgment never passes.")]
    for name in DIMENSIONS:
        item = reviewed["dimensions"][name] if available else None
        verdict = item["verdict"] if item else "unavailable"
        result.append(feedback("judge_" + name, int(verdict == "pass"), value=verdict, model=True,
            comment=(item["evidence"] if item else "No valid judgment; inspect judge-result.json.") +
            " Advisory score: 1=pass, 0=fail/uncertain/unavailable. Inspect categorical verdict; this is not human approval."))
    return result


def redact_tree(value):
    if isinstance(value, dict):
        return {str(k): redact_tree(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_tree(v) for v in value]
    return safe_text(value, 24000) if isinstance(value, str) else value


def build_native_payload(dataset, report, review, judge_result, *, dataset_sha256, label):
    need(re.fullmatch(r"servicevoice-(?:v17|v18)-[a-z0-9-]{1,50}", label), "invalid_experiment_label")
    payload = build_payload(dataset, report, dataset_sha256=dataset_sha256)
    judge_index, states = bind_judge(report, review, judge_result)
    payload["experiment_name"] = label
    payload["dataset_description"] = "Authored synthetic v18 acceptance cases. Structured reference facts, scheduler oracles and human-review rubrics; no caller data. Earlier challenge families are exposed, not a blind holdout."
    payload["experiment_description"] = "Real Gemini 3.8 Flash through the production streaming gateway in disposable schedulers. Code invariants and evidence-bound advisory hallucination/semantic judging. No carrier or audio evaluation."
    payload["experiment_metadata"].update({"prompt_version": report["configuration"]["prompt_version"],
        "model": report["configuration"]["model"], "transport": report["configuration"]["transport"],
        "evaluator_version": EVALUATOR_VERSION, "judge_model": judge_result["model_requested"],
        "judging_authority": "advisory_same_model", "synthetic_only": True,
        "configuration": report["configuration"], "judge_source_report_sha256": digest(report)})
    payload["experiment_metadata"]["semantic_review"] = "advisory_judged; no independent human acceptance"
    all_scores = []
    for case, row in zip(report["cases"], payload["results"], strict=True):
        scores = code_feedback(case) + judge_feedback(case, judge_index, states)
        row["evaluation_scores"].extend(scores)
        row["expected_outputs"]["trusted_dealership_facts"] = redact_tree(report["configuration"]["trusted_dealership_facts"])
        row["expected_outputs"]["oracle"] = redact_tree(case["expected"])
        row["actual_outputs"].update({
            "tool_evidence": redact_tree(clean_evidence(case["outputs"].get("tools", []))),
            "turn_states": redact_tree(clean_evidence(case["outputs"].get("turn_states", []))),
            "reference_readback": reference_readback_evidence(case["outputs"]),
            "check_results": redact_tree(case["checks"]),
            "semantic_review": "advisory_same_model; see judge_* feedback",
        })
        generations = case["outputs"].get("gateway_generations", [])
        routes = Counter((g.get("gateway") or {}).get("route", "unknown") for g in generations)
        row["actual_outputs"]["routing_summary"] = dict(routes)
        row["actual_outputs"]["turn_timings_ms"] = [t["duration_ms"] for t in case["outputs"].get("turns", [])]
        row["run_metadata"].update({"prompt_version": report["configuration"]["prompt_version"],
            "evaluator_version": EVALUATOR_VERSION, "family": case["family"],
            "source_split": case["split"], "transport": report["configuration"]["transport"],
            "mechanical_pass": all(c["passed"] is True for c in case["checks"]),
            "semantic_review": "advisory_same_model", "release_ready": False})
        all_scores.extend(scores)
    aggregates = {}
    for key in sorted({s["key"] for s in all_scores}):
        values = [s["score"] for s in all_scores if s["key"] == key and s["score"] is not None]
        aggregates[key] = {"pass": sum(v == 1 for v in values), "total": len(values)}
    payload["experiment_metadata"]["feedback_counts"] = aggregates
    # All comparison examples are stable by dataset/case/attempt, not by run UUID.
    need(len({r["row_id"] for r in payload["results"]}) == len(payload["results"]), "duplicate_native_examples")
    serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    need(len(serialized.encode()) <= 2 * 1024 * 1024, "native_payload_too_large")
    need(not re.search(r"\bAIza[\w-]{25,}|\blsv2_[\w-]+|-----BEGIN [A-Z ]*PRIVATE KEY-----", serialized), "native_payload_credential_pattern")
    return payload


def build_control_payload(dataset, report, *, label):
    """Native judge-sensitivity experiment; not an agent quality score."""
    need(report.get("dataset_sha256") == digest(dataset), "control_dataset_digest_mismatch")
    need(len(dataset["cases"]) == len(report["cases"]) > 0, "control_coverage_incomplete")
    identity = uuid.uuid5(uuid.NAMESPACE_URL, dataset["dataset_id"] + ":" + digest(dataset))
    rows=[]
    for control, actual in zip(dataset["cases"], report["cases"], strict=True):
        need(control["id"] == actual["id"] and control["expected_verdict"] == actual["expected_verdict"], "control_identity_mismatch")
        matched = actual.get("status") == "judged" and actual.get("actual_verdict") == control["expected_verdict"]
        need(actual.get("matched") is matched, "control_score_does_not_match_evidence")
        rows.append({"row_id":str(uuid.uuid5(identity,control["id"])),
            "inputs":redact_tree(control["package"]["evidence"]["turn:1"]),
            "expected_outputs":{"target_dimension":control["target_dimension"],"expected_verdict":control["expected_verdict"],
                "trusted_facts":redact_tree(control["package"]["evidence"]["facts"]),
                "recorded_tools":redact_tree(control["package"]["evidence"]["tool:1"])},
            "actual_outputs":{"actual_verdict":actual["actual_verdict"],"judgment":redact_tree(actual.get("judgment")),
                "source":"Authored answer supplied to the evaluator; not generated by the agent."},
            "evaluation_scores":[feedback("control_label_match",int(matched),value=actual["actual_verdict"],
                comment="Agreement with the explicit expected label for this known-fact control. This is a judge-sensitivity test, not agent performance.")],
            "start_time":actual["started_at"],"end_time":actual["completed_at"],"run_name":control["id"],
            "run_metadata":{"synthetic_only":True,"control_not_agent_output":True,"evaluator_version":EVALUATOR_VERSION}})
    return {"experiment_name":label,"experiment_description":"Known-fact positive/negative controls for the same-model advisory judge. Includes fabricated prices, address, hours, callback delivery, warranty claims, instruction injection and Spanish examples.",
        "dataset_id":str(identity),"dataset_name":dataset["dataset_id"]+"-"+dataset["version"]+"-"+digest(dataset)[:12],
        "dataset_description":"Deliberately authored answers with explicit expected labels. No caller or agent-run data.",
        "results":rows,"experiment_start_time":report["started_at"],"experiment_end_time":report["completed_at"],
        "experiment_metadata":{"synthetic_only":True,"control_not_agent_output":True,"evaluator_version":EVALUATOR_VERSION,
            "expected_labels_sent_to_judge":False,"known_controls_matched":report["matched"],"known_controls_total":report["total"],
            "limitations":report["limitations"],"judge_model":report["model"],"release_ready":False}}
