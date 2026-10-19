"""Pure, denominator-aware evaluation summaries with explicit timing boundaries.

No network, environment, application imports, file access, model judgment or
release decision. Quantiles use nearest-rank over finite nonnegative numbers.
"""
from __future__ import annotations

from collections import Counter
import math


def finite_nonnegative(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def distribution(values):
    values = list(values)
    valid = sorted(float(value) for value in values if finite_nonnegative(value))
    missing = sum(value is None for value in values)
    def quantile(fraction):
        return valid[max(0, math.ceil(len(valid) * fraction) - 1)] if valid else None
    return {"observations": len(values), "n": len(valid), "missing": missing,
            "invalid": len(values) - len(valid) - missing,
            "quantile_method": "nearest_rank", "p50": quantile(.50),
            "p95": quantile(.95), "p99": quantile(.99),
            "max": valid[-1] if valid else None}


def rate(numerator, denominator):
    if type(numerator) is not int or type(denominator) is not int or not 0 <= numerator <= denominator:
        raise ValueError("invalid_rate_counts")
    return {"numerator": numerator, "denominator": denominator,
            "value": numerator / denominator if denominator else None}


def _rows(value):
    return value if isinstance(value, list) else []


def _object(value):
    return value if isinstance(value, dict) else {}


def _request_outcome(row):
    status = row.get("status") if isinstance(row.get("status"), str) else None
    http = row.get("http_status")
    if type(http) is int and not 200 <= http < 300:
        return "error"
    if status in {"http_error", "timeout", "request_error", "invalid_response_contract", "failed", "error", "cancelled", "interrupted"} or row.get("error"):
        return "error"
    if status in {"response_received", "completed", "succeeded", "success"}:
        return "success"
    return "unknown"


def _outcomes(outcomes):
    counts = Counter(outcomes)
    known = counts["success"] + counts["error"]
    return {"attempts": sum(counts.values()), "successes": counts["success"],
            "errors": counts["error"], "known_outcomes": known,
            "unknown_outcomes": counts["unknown"],
            "error_rate": rate(counts["error"], known),
            "outcome_coverage": rate(known, sum(counts.values()))}


def _usage(requests):
    summary = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        values = [_object(row.get("usage")).get(key) for row in requests]
        known = [value for value in values if type(value) is int and value >= 0]
        summary[key] = {"reported_sum": sum(known) if known else None,
                        "known_requests": len(known), "total_requests": len(values),
                        "complete": bool(values) and len(known) == len(values)}
    return summary


def summarize_tool_expectations(observed_tools, required=(), forbidden=()):
    """Exact-name multiset comparison; it does not grade arguments or consent.

    A repeated required name requires that many observations. Extra calls reduce
    precision; order is separately reported and never inferred from precision.
    Missing names remain unmatched observations in the precision denominator.
    """
    required, forbidden = list(required), list(forbidden)
    if any(not isinstance(name, str) or not name for name in required + forbidden):
        raise ValueError("invalid_reference_tool_name")
    observed_tools = list(observed_tools)
    names = [row.get("name") if isinstance(row, dict) else None for row in observed_tools]
    named = [name for name in names if isinstance(name, str) and name]
    actual, expected = Counter(named), Counter(required)
    matched = sum((actual & expected).values())
    missing = dict(expected - actual)
    extra = dict(actual - expected)
    forbidden_counts = {name: actual[name] for name in sorted(set(forbidden)) if actual[name]}
    return {"observed_calls": len(names), "unnamed_calls": len(names) - len(named),
            "required_calls": len(required), "matched_required_calls": matched,
            "missing_required": missing, "unexpected_named_calls": extra,
            "forbidden_calls": forbidden_counts,
            "required_present": not missing,
            "forbidden_absent": not forbidden_counts,
            "precision": rate(matched, len(names)), "recall": rate(matched, len(required)),
            "exact_name_sequence_match": names == required,
            "scope": "Exact tool names and multiplicity only; no argument, effect, semantic or consent judgment."}


def summarize_cases(cases):
    cases = list(cases)
    valid_cases = [_object(case) for case in cases]
    requests, tools, turns, checks = [], [], [], []
    for case in valid_cases:
        outputs = _object(case.get("outputs"))
        requests.extend(_object(row) for row in _rows(case.get("requests")))
        tools.extend(_object(row) for row in _rows(outputs.get("tools")))
        turns.extend(_object(row) for row in _rows(outputs.get("turns")))
        checks.extend(_object(row) for row in _rows(case.get("checks")))
    statuses = Counter(case.get("status") if isinstance(case.get("status"), str) else "unknown" for case in valid_cases)
    case_success = sum(statuses[name] for name in ("completed", "passed", "succeeded"))
    case_errors = sum(statuses[name] for name in ("failed", "error", "infrastructure_failure", "incomplete_or_failed", "failed_or_incomplete", "interrupted", "timeout"))
    request_summary = _outcomes(_request_outcome(row) for row in requests)
    request_summary["usage"] = _usage(requests)
    request_summary["cost_usd"] = None
    request_summary["cost_note"] = "No price inference; tokens and request caps do not establish billed cost."
    tool_summary = _outcomes("success" if row.get("ok") is True else "error" if row.get("ok") is False else "unknown" for row in tools)
    by_name = {}
    for name in sorted({row.get("name") if isinstance(row.get("name"), str) and row.get("name") else "<missing>" for row in tools}):
        selected = [row for row in tools if (row.get("name") if isinstance(row.get("name"), str) and row.get("name") else "<missing>") == name]
        by_name[name] = {**_outcomes("success" if row.get("ok") is True else "error" if row.get("ok") is False else "unknown" for row in selected),
                         "execution_ms": distribution(row.get("duration_ms") for row in selected)}
    tool_summary["by_name"] = by_name
    passed = sum(row.get("passed") is True for row in checks)
    failed = sum(row.get("passed") is False for row in checks)
    semantic_statuses = Counter()
    for case in valid_cases:
        review = case.get("semantic_review")
        status = review.get("status") if isinstance(review, dict) else review
        semantic_statuses[status if isinstance(status, str) else "unknown"] += 1
    return {
        "schema_version": 1,
        "cases": {"total": len(cases), "malformed_cases": sum(not isinstance(case, dict) for case in cases),
                  "status_counts": dict(statuses), "completed_count": case_success, "error_count": case_errors,
                  "unknown_or_incomplete_count": len(cases) - case_success - case_errors,
                  "error_rate": rate(case_errors, case_success + case_errors),
                  "status_coverage": rate(case_success + case_errors, len(cases))},
        "requests": request_summary, "tools": tool_summary,
        "checks": {"total": len(checks), "passed": passed, "failed": failed,
                   "unknown": len(checks) - passed - failed,
                   "pass_rate": rate(passed, passed + failed),
                   "coverage": rate(passed + failed, len(checks))},
        "semantic_review_status_counts": dict(semantic_statuses),
        "turns": {"observed": len(turns),
                  "nonempty_assistant_replies": sum(isinstance(row.get("assistant"), str) and bool(row["assistant"].strip()) for row in turns),
                  "meaning_or_fluency_pass_rate": None},
        "timings": {
            "units": "milliseconds",
            "case_duration_ms": distribution(row.get("duration_ms") for row in valid_cases),
            "text_turn_completion_ms": distribution(row.get("duration_ms") for row in turns),
            "model_http_full_response_ms": distribution(row.get("elapsed_ms") for row in requests),
            "model_http_successful_response_ms": distribution(row.get("elapsed_ms") for row in requests if _request_outcome(row) == "success"),
            "tool_execution_ms": distribution(row.get("duration_ms") for row in tools),
            "time_to_first_token_ms": None, "time_to_first_audio_ms": None,
            "browser_speech_event_gap_ms": None, "acoustic_response_gap_ms": None,
            "scope": "Text turn duration_ms includes all model rounds and tool waits through the completed text reply. Request elapsed_ms measures full HTTP attempt duration, including errors where supplied. Tool duration_ms measures the supplied tool execution interval. These are not time to first token, browser event timing or acoustic response latency; partial failed turns lack a completed-turn duration."},
        "release_passed": None,
        "limitations": ["Descriptive statistics only; missing outcomes and invalid measurements stay visible and never count as successes.",
                        "Small-sample tail quantiles are observations, not reliable population tail estimates.",
                        "No semantic, human-consent or release decision is inferred from tool success or nonempty prose."]}
