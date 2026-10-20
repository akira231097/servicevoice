import json
from uuid import uuid4

import pytest

from app.generation_observability import (MAX_EVENTS, GenerationEvidenceError,
    project_provider_generations)

CALL = "00000000-0000-4000-8000-000000000001"
SPAN = "00000000-0000-4000-8000-000000000002"
BASE = 1_792_527_910_000


def event(body, ms=0, *, call=CALL, span=SPAN, turn="17", **attrs):
    return {"body": body, "time": BASE + ms,
        "attributes": {"callId": call, "traceId": call,
            "spanId": span, "turnId": turn, **attrs}}


def test_completed_then_cleanup_abort_is_not_failed_generation():
    result = project_provider_generations([
        event("LLM request started"), event("LLM first token received", 20),
        event("LLM response completed", 30),
        event("LLM request aborted before completion", 30),
    ], CALL)
    row = result["model_generations"][0]
    assert row["classification"] == "completed_then_cleanup_abort_label"
    assert row["observed_duration_ms"] == 30
    assert result["counts"]["aborted_without_observed_output"] == 0


def test_abort_without_output_is_not_called_an_audible_interruption():
    result = project_provider_generations([
        event("LLM request started", turn="17"),
        event("LLM request aborted before completion", 100, turn="18"),
    ], CALL)
    row = result["model_generations"][0]
    assert row["classification"] == "aborted_without_observed_output"
    assert (row["provider_turn_id_at_start"], row["provider_turn_id_at_abort"]) == ("17", "18")
    assert result["coverage"]["audible_caller_interruption_count"] is None


def test_spans_share_turn_without_becoming_one_generation():
    result = project_provider_generations([
        event("LLM request started"), event("LLM response completed", 10),
        event("LLM request started", 11, span=str(uuid4())),
    ], CALL)
    assert result["counts"]["provider_model_spans"] == 2
    assert {r["provider_turn_id_at_start"] for r in result["model_generations"]} == {"17"}


def test_reordered_abort_label_does_not_become_proven_cleanup():
    result = project_provider_generations([
        event("LLM request started"), event("LLM response completed", 30),
        event("LLM request aborted before completion", 20),
    ], CALL)
    assert result["model_generations"][0]["classification"] == "ambiguous_or_incomplete"


def test_provider_flag_scopes_remain_unreconciled():
    result = project_provider_generations([
        event("Pipeline cleared", wasInterruption=False),
        event("Custom LLM HTTP request", request={"timestamp": BASE,
            "metadata": {"assistantTurnInterrupted": True, "numUserTurns": 1}}),
    ], CALL)
    flags = result["provider_flag_evidence"]
    assert flags["pipeline_was_interruption_values"] == {"false": 1}
    assert flags["request_assistant_turn_interrupted_values"] == {"true": 1}
    assert flags["scope_reconciliation"] == "not_established"


def test_duplicate_provider_http_log_entries_do_not_double_count_request_metadata():
    row = event("Custom LLM HTTP request", request={"timestamp": BASE,
        "metadata": {"assistantTurnInterrupted": True}})
    result = project_provider_generations([row, row], CALL)
    assert len(result["provider_request_metadata"]) == 1


def test_content_headers_customer_data_and_arbitrary_log_names_never_exported():
    marker = "PRIVATE_SECRET_DO_NOT_EXPORT"
    rows = [event("LLM request started", messages=marker, requestHeaders={"authorization": marker},
        customer={"number": marker}), event(marker),
        event("Custom LLM HTTP request", request={"timestamp": BASE, "headers": {"x-key": marker},
            "messages": marker, "metadata": {"numUserTurns": marker, "assistantTurnInterrupted": marker}})]
    result = project_provider_generations(rows, CALL)
    assert marker not in json.dumps(result)
    assert result["provider_request_metadata"][0]["provider_num_user_turns"] is None
    assert result["provider_request_metadata"][0]["provider_assistant_turn_interrupted"] is None


def test_other_call_and_unbound_rows_cannot_attach_to_requested_call():
    result = project_provider_generations([
        event("LLM request started", call=str(uuid4())),
        {"body": "LLM request started", "time": BASE, "attributes": {"spanId": SPAN}},
    ], CALL)
    assert result["model_generations"] == []
    assert result["skipped"] == {"conflicting_call_identity": 1, "unbound_call_identity": 1}


def test_missing_span_is_not_guessed_from_timestamps_or_turn():
    result = project_provider_generations([
        event("LLM request started", span=None), event("LLM response completed", 20, span=None),
    ], CALL)
    assert result["model_generations"] == []
    assert result["skipped"]["model_span_identity_unavailable"] == 2
    assert result["coverage"]["tts_generation_join"].startswith("unavailable")


@pytest.mark.parametrize("value", [True, -1, None, "1792527910000", 4_102_444_800_000])
def test_invalid_timestamp_never_creates_a_fake_duration(value):
    row = event("LLM request started");row["time"] = value
    result = project_provider_generations([row, event("LLM response completed", 20)], CALL)
    assert result["model_generations"][0]["observed_duration_ms"] is None


@pytest.mark.parametrize("rows,call,code", [
    ([], "bad-call", "invalid_call_identity"),
    ({}, CALL, "invalid_or_oversized_provider_log"),
    ([{}] * (MAX_EVENTS + 1), CALL, "invalid_or_oversized_provider_log"),
    (["private body"], CALL, "invalid_provider_log_row"),
])
def test_input_bounds_return_only_fixed_error_codes(rows, call, code):
    with pytest.raises(GenerationEvidenceError, match="^" + code + "$"):
        project_provider_generations(rows, call)
