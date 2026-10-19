import math

import pytest

from evals.metrics import distribution, rate, summarize_cases, summarize_tool_expectations


def test_quantiles_use_nearest_rank_and_keep_missing_invalid_visible():
    result = distribution([None, True, -1, math.nan, math.inf, "7", *range(1, 101)])
    assert result == {"observations": 106, "n": 100, "missing": 1, "invalid": 5,
                      "quantile_method": "nearest_rank", "p50": 50.0, "p95": 95.0,
                      "p99": 99.0, "max": 100.0}


def test_empty_metrics_are_unknown_not_zero_success_or_zero_latency():
    result = summarize_cases([])
    assert result["cases"]["error_rate"]["value"] is None
    assert result["requests"]["error_rate"]["value"] is None
    assert result["requests"]["usage"]["total_tokens"]["reported_sum"] is None
    assert result["timings"]["model_http_full_response_ms"]["p50"] is None
    assert result["release_passed"] is None


def test_errors_and_unknown_outcomes_have_explicit_denominators():
    result = summarize_cases([
        {"status": "completed", "duration_ms": 500, "requests": [
            {"status": "response_received", "http_status": 200, "elapsed_ms": 100, "usage": {"total_tokens": 12}},
            {"status": "timeout", "elapsed_ms": 35000},
            {"status": "started", "elapsed_ms": None}],
         "outputs": {"tools": [{"name": "search", "ok": True, "duration_ms": 3}, {"name": "confirm", "ok": False, "duration_ms": 4}, {"name": "unknown"}],
                     "turns": [{"user": "Synthetic request", "assistant": "Synthetic answer"}]},
         "checks": [{"passed": True}, {"passed": False}, {}], "semantic_review": "pending"},
        {"status": "not_run"},
    ])
    assert result["requests"]["error_rate"] == {"numerator": 1, "denominator": 2, "value": .5}
    assert result["requests"]["outcome_coverage"]["value"] == 2 / 3
    assert result["tools"]["error_rate"]["value"] == .5
    assert result["tools"]["unknown_outcomes"] == 1
    assert result["checks"]["pass_rate"]["value"] == .5
    assert result["checks"]["unknown"] == 1
    assert result["cases"]["status_coverage"]["value"] == .5
    assert result["timings"]["model_http_full_response_ms"]["max"] == 35000
    assert result["timings"]["model_http_successful_response_ms"]["max"] == 100
    assert result["requests"]["usage"]["total_tokens"] == {"reported_sum": 12, "known_requests": 1, "total_requests": 3, "complete": False}


def test_full_http_and_tool_measurements_never_become_audio_or_semantic_evidence():
    result = summarize_cases([{"status": "completed", "requests": [{"status": "response_received", "elapsed_ms": 1}],
                               "outputs": {"turns": [{"assistant": "False booking claim."}], "tools": [{"name": "search", "ok": True, "duration_ms": 2}]}}])
    assert result["timings"]["time_to_first_token_ms"] is None
    assert result["timings"]["time_to_first_audio_ms"] is None
    assert result["timings"]["acoustic_response_gap_ms"] is None
    assert result["turns"]["meaning_or_fluency_pass_rate"] is None
    assert result["release_passed"] is None
    assert result["requests"]["cost_usd"] is None


def test_complete_text_turn_duration_includes_round_trip_chain_without_inventing_partial_duration():
    result = summarize_cases([{"outputs": {"turns": [{"user": "Question", "assistant": "Answer", "duration_ms": 4000}],
                                           "partial_turn": {"user": "Interrupted", "assistant": ""}},
                               "requests": [{"status": "response_received", "elapsed_ms": 1500},
                                            {"status": "response_received", "elapsed_ms": 2000}]}])
    assert result["timings"]["text_turn_completion_ms"]["n"] == 1
    assert result["timings"]["text_turn_completion_ms"]["p50"] == 4000
    assert result["timings"]["model_http_full_response_ms"]["max"] == 2000
    assert result["timings"]["time_to_first_audio_ms"] is None


def test_conflicting_http_status_and_response_contract_errors_are_errors():
    result = summarize_cases([{"requests": [{"status": "response_received", "http_status": 500, "elapsed_ms": 8},
                                           {"status": "invalid_response_contract", "http_status": 200, "elapsed_ms": 10}]}])
    assert result["requests"]["errors"] == 2
    assert result["requests"]["successes"] == 0
    assert result["timings"]["model_http_successful_response_ms"]["n"] == 0


def test_repeated_attempts_remain_in_denominator_and_success_cannot_hide_failure():
    attempts = [{"request_index": 1, "status": "timeout", "elapsed_ms": 30000},
                {"request_index": 1, "status": "response_received", "elapsed_ms": 100}]
    result = summarize_cases([{"requests": attempts}])
    assert result["requests"]["attempts"] == 2
    assert result["requests"]["error_rate"]["value"] == .5
    assert result["timings"]["model_http_full_response_ms"]["max"] == 30000


def test_measured_zero_is_valid_but_nonfinite_or_nonintegral_token_usage_is_unknown():
    result = summarize_cases([{"requests": [
        {"status": "response_received", "elapsed_ms": 0, "usage": {"prompt_tokens": 0, "completion_tokens": 1.5, "total_tokens": float("nan")}},
        {"status": [], "elapsed_ms": 10 ** 1000, "usage": {"total_tokens": False}}]}])
    assert result["timings"]["model_http_full_response_ms"]["n"] == 1
    assert result["timings"]["model_http_full_response_ms"]["p50"] == 0
    assert result["timings"]["model_http_full_response_ms"]["invalid"] == 1
    assert result["requests"]["usage"]["prompt_tokens"]["reported_sum"] == 0
    assert result["requests"]["usage"]["completion_tokens"]["reported_sum"] is None
    assert result["requests"]["usage"]["total_tokens"]["reported_sum"] is None


def test_malformed_values_do_not_coerce_into_success_or_token_usage():
    result = summarize_cases([None, {"status": [], "duration_ms": False, "requests": [None, {"usage": {"total_tokens": True}}],
                                    "outputs": {"tools": [None, {"name": "search", "ok": "true", "duration_ms": "2"}]},
                                    "checks": [{"passed": "true"}, None]}])
    assert result["cases"]["malformed_cases"] == 1
    assert result["requests"]["known_outcomes"] == 0
    assert result["requests"]["usage"]["total_tokens"]["reported_sum"] is None
    assert result["tools"]["known_outcomes"] == 0
    assert result["checks"]["unknown"] == 2


def test_tool_multiset_precision_recall_keeps_repeats_and_forbidden_calls():
    result = summarize_tool_expectations([{"name": "search"}, {"name": "search"}, {"name": "confirm"}, {}],
                                         required=["search", "prepare"], forbidden=["confirm"])
    assert result["precision"] == {"numerator": 1, "denominator": 4, "value": .25}
    assert result["recall"]["value"] == .5
    assert result["missing_required"] == {"prepare": 1}
    assert result["unexpected_named_calls"] == {"search": 1, "confirm": 1}
    assert result["forbidden_calls"] == {"confirm": 1}
    assert result["exact_name_sequence_match"] is False
    assert result["required_present"] is False and result["forbidden_absent"] is False


def test_perfect_name_counts_do_not_establish_correct_order():
    result = summarize_tool_expectations([{"name": "confirm"}, {"name": "prepare"}], required=["prepare", "confirm"])
    assert result["precision"]["value"] == result["recall"]["value"] == 1
    assert result["exact_name_sequence_match"] is False


def test_no_reference_tools_leaves_recall_unknown():
    result = summarize_tool_expectations([])
    assert result["precision"]["value"] is None and result["recall"]["value"] is None
    assert result["required_present"] is True


@pytest.mark.parametrize("numerator,denominator", [(True, 1), (1, 0), (-1, 2), (1, 1.0)])
def test_invalid_rate_counts_rejected(numerator, denominator):
    with pytest.raises(ValueError, match="invalid_rate_counts"):
        rate(numerator, denominator)
