"""Authored offline controls for provider-observation child spans."""
import copy
from datetime import datetime, timezone
import json
from uuid import uuid4

import pytest

from app.generation_observability import (ProviderSpanError, build_provider_observation_runs,
    project_provider_generations, sanitize_provider_projection)


BASE = 1_792_537_910_000
CALL = str(uuid4())


def destination():
    root = str(uuid4())
    return {"root_run_id": root, "root_dotted_order": "20261020T202510000000Z" + root,
        "project_id": str(uuid4()), "expected_call_id": CALL}


def event(index, kind, stamp, span=None, turn="17"):
    return {"source_entry_index": index, "event": "provider." + kind,
        "observed_unix_ms": stamp, "provider_turn_id": turn,
        "provider_previous_turn_id": "16", "provider_model_span_id": span,
        "provider_model_identity_basis":"explicit_provider_trace_and_span_ids" if span else None,
        "identity_basis": "explicit_provider_fields", "timing_basis": "provider_reported"}


def projection(events):
    return {"schema_version": "generation-observability-v1", "call_id": CALL, "events": events}


def test_actual_provider_projector_output_exports_without_raw_text_or_headers():
    span=str(uuid4());private="PRIVATE CALLER TEXT AND CREDENTIALS"
    rows=[{"body":body,"time":BASE+offset,"attributes":{"callId":CALL,"traceId":CALL,
        "spanId":span,"turnId":"17","request":{"headers":{"Authorization":private}},
        "transcript":private}} for body,offset in (("LLM request started",0),("LLM response completed",100))]
    projected=project_provider_generations(rows,CALL)
    result=build_provider_observation_runs(projected,**destination())
    assert result["coverage"]["exported_point_event_spans"]==2
    assert result["coverage"]["exported_model_interval_spans"]==1
    assert result["coverage"]["skipped_event_counts"]=={}
    assert private not in json.dumps(result)


@pytest.mark.parametrize("remove_legacy_groups",[False,True])
def test_call_only_model_span_uuid_never_recovers_a_trace_link_rejected_by_the_projector(remove_legacy_groups):
    span=str(uuid4())
    raw=[{"time":BASE+offset,"body":body,"attributes":{"callId":CALL,"spanId":span}}
        for body,offset in (("LLM request started",0),("LLM response completed",100))]
    projected=project_provider_generations(raw,CALL)
    assert projected["model_generations"]==[]
    assert all(row["provider_model_span_id"]==span for row in projected["events"])
    if remove_legacy_groups:projected.pop("model_generations")
    safe=sanitize_provider_projection(projected,CALL)
    assert all(row["provider_model_span_id"] is None for row in safe["events"])
    assert safe["summary"]["rejected_field_counts"]=={"model_span_trace_binding_unavailable":2}
    assert sanitize_provider_projection(json.loads(json.dumps(safe)),CALL)==safe
    for value in (projected,safe):
        result=build_provider_observation_runs(value,**destination())
        assert result["coverage"]["exported_model_interval_spans"]==0
        assert result["coverage"]["exported_point_event_spans"]==2


def test_only_explicitly_trace_bound_indices_in_a_shared_span_can_form_model_interval():
    span=str(uuid4())
    raw=[{"time":BASE,"body":"LLM request started","attributes":{"callId":CALL,"traceId":CALL,"spanId":span}},
        {"time":BASE+100,"body":"LLM response completed","attributes":{"callId":CALL,"spanId":span}}]
    projected=project_provider_generations(raw,CALL)
    assert projected["model_generations"][0]["source_entry_indices"]==[0]
    safe=sanitize_provider_projection(projected,CALL)
    assert [row["provider_model_span_id"] for row in safe["events"]]==[span,None]
    assert build_provider_observation_runs(safe,**destination())["coverage"]["exported_model_interval_spans"]==0


def test_valid_explicit_trace_span_eligibility_survives_frozen_projection_without_model_generation_prose():
    span=str(uuid4())
    raw=[{"time":BASE+offset,"body":body,"attributes":{"callId":CALL,"traceId":CALL,"spanId":span}}
        for body,offset in (("LLM request started",0),("LLM response completed",100))]
    safe=sanitize_provider_projection(project_provider_generations(raw,CALL),CALL)
    assert "model_generations" not in safe
    assert all(row["provider_model_identity_basis"]=="explicit_provider_trace_and_span_ids" for row in safe["events"])
    assert sanitize_provider_projection(json.loads(json.dumps(safe)),CALL)==safe
    assert build_provider_observation_runs(safe,**destination())["coverage"]["exported_model_interval_spans"]==1


def test_public_projection_boundary_retains_only_fixed_events_flags_and_numeric_metadata():
    private="PRIVATE TEXT HEADERS CREDENTIALS"
    source=projection([event(1,"pipeline_cleared",BASE)])
    source.update(transcript=private,headers={"Authorization":private},model_generations=[{"private":private}],
        coverage={"interpretation":private},summary={"private":private},provider_flag_evidence={
            "pipeline_was_interruption_values":{"false":172,private:1,"true":private},
            "request_assistant_turn_interrupted_values":{"true":21},"interpretation":private},
        provider_request_metadata=[{"source_entry_index":2,"provider_request_timestamp_unix_ms":BASE+1,
            "provider_num_assistant_turns":12,"provider_num_user_turns":13,"provider_num_model_request_in_turn":1,
            "provider_assistant_turn_interrupted":True,"headers":private}])
    safe=sanitize_provider_projection(source,CALL)
    assert private not in json.dumps(safe)
    assert "model_generations" not in safe and "headers" not in safe
    assert safe["summary"]["rejected_field_counts"]=={
        "invalid_provider_flag_fields":2,"invalid_provider_model_identity":1}
    assert safe["provider_flag_evidence"]["pipeline_was_interruption_values"]=={"false":172}
    assert safe["provider_flag_evidence"]["request_assistant_turn_interrupted_values"]=={"true":21}
    assert safe["provider_flag_evidence"]["scope_reconciliation"]=="not_established"
    assert safe["provider_request_metadata"][0]["provider_num_user_turns"]==13
    assert safe["coverage"]["audible_caller_interruption_count"] is None
    assert build_provider_observation_runs(safe,**destination())["coverage"]["exported_point_event_spans"]==1


def test_public_projection_rejects_call_mismatch_without_echoing_any_source():
    with pytest.raises(ProviderSpanError,match="provider_projection_call_scope_mismatch"):
        sanitize_provider_projection(projection([]),str(uuid4()))


def test_public_projection_event_and_metadata_conflicts_remain_explicit_without_choosing_truth():
    original=event(1,"caller_speech_started",BASE);changed={**original,"observed_unix_ms":BASE+1}
    metadata={"source_entry_index":2,"provider_request_timestamp_unix_ms":BASE+2,"provider_num_user_turns":1}
    source=projection([original,original,changed]);source["provider_request_metadata"]=[metadata,metadata,{**metadata,"provider_num_user_turns":2}]
    safe=sanitize_provider_projection(source,CALL)
    assert safe["events"]==[] and safe["provider_request_metadata"]==[]
    assert safe["summary"]["exact_duplicate_events_ignored"]==1
    assert safe["summary"]["exact_duplicate_request_metadata_ignored"]==1
    assert safe["summary"]["rejected_field_counts"]=={
        "conflicting_source_entry_identity":1,"conflicting_provider_request_metadata_identity":1}
    assert safe["coverage"]["state"]=="unavailable"


def test_public_projection_invalid_metadata_values_become_unknown_and_oversized_metadata_rejects():
    source=projection([]);source["provider_request_metadata"]=[{"source_entry_index":0,
        "provider_request_timestamp_unix_ms":BASE,"provider_num_user_turns":True,
        "provider_assistant_turn_interrupted":"PRIVATE"}]
    safe=sanitize_provider_projection(source,CALL)
    assert safe["provider_request_metadata"][0]["provider_num_user_turns"] is None
    assert safe["provider_request_metadata"][0]["provider_assistant_turn_interrupted"] is None
    assert "PRIVATE" not in json.dumps(safe)
    source["provider_request_metadata"]=[{}]*10001
    with pytest.raises(ProviderSpanError,match="invalid_or_oversized_provider_metadata"):
        sanitize_provider_projection(source,CALL)


def test_public_projection_preserves_only_fixed_bounded_source_skip_counts():
    source=projection([]);source["skipped"]={"conflicting_call_identity":2,"unbound_call_identity":3,
        "attributes_unavailable":True,"PRIVATE SOURCE":5}
    safe=sanitize_provider_projection(source,CALL)
    assert safe["summary"]["source_projection_skipped_counts"]=={
        "conflicting_call_identity":2,"unbound_call_identity":3}
    assert safe["summary"]["rejected_field_counts"]=={"invalid_source_projection_skip_fields":2}
    assert "PRIVATE SOURCE" not in json.dumps(safe)


def test_public_projection_frozen_round_trip_preserves_known_filtering_duplicate_and_source_loss():
    original=event(1,"caller_speech_started",BASE);changed={**original,"observed_unix_ms":BASE+1}
    source=projection([original,original,changed,event(2,"caller_speech_stopped",BASE+100)])
    source["skipped"]={"unbound_call_identity":3}
    metadata={"source_entry_index":3,"provider_request_timestamp_unix_ms":BASE+50,"provider_num_user_turns":"PRIVATE"}
    source["provider_request_metadata"]=[metadata,metadata]
    safe=sanitize_provider_projection(source,CALL)
    cached=json.loads(json.dumps(safe))
    assert sanitize_provider_projection(cached,CALL)==safe
    assert safe["summary"]["exact_duplicate_events_ignored"]==1
    assert safe["summary"]["exact_duplicate_request_metadata_ignored"]==1
    assert safe["summary"]["source_projection_skipped_counts"]=={"unbound_call_identity":3}
    assert safe["summary"]["rejected_field_counts"]["conflicting_source_entry_identity"]==1
    assert "PRIVATE" not in json.dumps(safe)


def test_public_projection_prior_loss_counters_accept_only_fixed_codes_and_bounded_integers():
    source=projection([]);source["summary"]={"rejected_field_counts":{
        "invalid_provider_event":2,"PRIVATE SECRET":3,"invalid_provider_flag_fields":True},
        "exact_duplicate_events_ignored":"PRIVATE SECRET"}
    safe=sanitize_provider_projection(source,CALL)
    assert safe["summary"]["rejected_field_counts"]=={
        "invalid_provider_event":2,"invalid_prior_projection_summary_fields":3}
    assert safe["summary"]["exact_duplicate_events_ignored"]==0
    assert "PRIVATE SECRET" not in json.dumps(safe)
    assert sanitize_provider_projection(json.loads(json.dumps(safe)),CALL)==safe


def test_exact_point_event_and_model_interval_are_deterministic_and_direct_root():
    span = str(uuid4()); target = destination()
    source = projection([event(7, "model_started", BASE, span),
        event(8, "model_first_token", BASE + 150, span), event(9, "model_completed", BASE + 430, span)])
    result = build_provider_observation_runs(source, **target)
    assert result == build_provider_observation_runs(json.loads(json.dumps(source)), **target)
    assert len(result["runs"]) == 4
    model = next(run for run in result["runs"] if run["name"] == "voice.provider.model_generation")
    assert model["start_time"] == datetime.fromtimestamp(BASE / 1000, timezone.utc).isoformat(timespec="microseconds")
    assert model["end_time"] == datetime.fromtimestamp((BASE + 430) / 1000, timezone.utc).isoformat(timespec="microseconds")
    assert model["outputs"]["observed_model_interval"]["observed_duration_ms"] == 430
    assert model["error"] is None and model["run_type"] == "chain"
    for run in result["runs"]:
        assert run["parent_run_id"] == run["trace_id"] == target["root_run_id"]
        assert run["outputs"]["tts_generation_id"] is run["outputs"]["gateway_generation_id"] is None
        assert run["outputs"]["phone_playback_verified"] is False
        assert run["outputs"]["late_phone_packet_discard_verified"] is False
    points = [run for run in result["runs"] if run["extra"]["metadata"]["record_kind"] == "provider_event"]
    assert all(run["start_time"] == run["end_time"] for run in points)
    assert all("point-observation-not-duration" in run["tags"] for run in points)


def test_completed_then_cleanup_abort_has_completed_interval_and_no_failure():
    span = str(uuid4())
    source = projection([event(1, "model_started", BASE, span), event(2, "model_completed", BASE + 100, span),
        event(3, "model_abort_label", BASE + 240, span)])
    result = build_provider_observation_runs(source, **destination())
    model = next(run for run in result["runs"] if run["name"] == "voice.provider.model_generation")
    evidence = model["outputs"]["observed_model_interval"]
    assert evidence["classification"] == "completed_then_cleanup_abort_label"
    assert evidence["observed_duration_ms"] == 100
    assert evidence["abort_label_unix_ms"] == BASE + 240 and model["error"] is None


def test_abort_without_observed_output_does_not_claim_caller_interruption():
    span = str(uuid4())
    result = build_provider_observation_runs(projection([
        event(1, "model_started", BASE, span), event(2, "model_abort_label", BASE + 100, span)]), **destination())
    model = next(run for run in result["runs"] if run["name"] == "voice.provider.model_generation")
    assert model["outputs"]["observed_model_interval"]["classification"] == "aborted_without_observed_output"
    assert model["outputs"]["audible_interruption_count"] is None and model["error"] is None


@pytest.mark.parametrize("rows", [
    [event(1, "assistant_speech_started", BASE), event(2, "caller_speech_stopped", BASE + 20)],
    [event(1, "assistant_speech_stopped", BASE)],
    [event(1, "turn_started", BASE), event(2, "pipeline_cleared", BASE + 20)],
])
def test_unmatched_speech_and_pipeline_events_never_invent_intervals_or_caller_turns(rows):
    result = build_provider_observation_runs(projection(rows), **destination())
    assert len(result["runs"]) == len(rows)
    assert result["coverage"]["exported_model_interval_spans"] == 0
    assert result["coverage"]["speech_start_stop_pairs_inferred"] is False
    assert result["coverage"]["provider_turn_is_caller_turn"] is False
    assert all(run["start_time"] == run["end_time"] for run in result["runs"])


@pytest.mark.parametrize("defect", ["no_start", "multiple_start", "reversed", "missing_time", "late_first_token", "abort_before_completion"])
def test_missing_or_contradictory_model_boundaries_keep_points_without_fabricated_execution(defect):
    span = str(uuid4()); rows = [event(1, "model_started", BASE, span), event(2, "model_completed", BASE + 100, span)]
    if defect == "no_start": rows.pop(0)
    elif defect == "multiple_start": rows.append(event(3, "model_started", BASE + 1, span))
    elif defect == "reversed": rows[1]["observed_unix_ms"] = BASE - 1
    elif defect == "missing_time": rows[0]["observed_unix_ms"] = None
    elif defect == "late_first_token": rows.append(event(3, "model_first_token", BASE + 101, span))
    elif defect == "abort_before_completion": rows.append(event(3, "model_abort_label", BASE + 99, span))
    result = build_provider_observation_runs(projection(rows), **destination())
    assert result["coverage"]["exported_model_interval_spans"] == 0
    assert result["coverage"]["skipped_event_counts"]


def test_unknown_private_content_is_not_exported_even_from_a_modified_cached_projection():
    span = str(uuid4()); private = "PRIVATE CUSTOMER TOKEN SENTENCE"
    source = projection([event(1, "model_started", BASE, span), event(2, "model_completed", BASE + 100, span)])
    source.update(headers={"Authorization": private}, transcript=private, coverage={"interpretation": private},
        model_generations=[{"classification": private}])
    for row in source["events"]:
        row.update(headers=private, identity_basis=private, timing_basis=private,
            provider_turn_id=private, provider_previous_turn_id=private)
    result = build_provider_observation_runs(source, **destination())
    assert private not in json.dumps(result)
    assert all(run["outputs"].get("observed_event", {}).get("provider_turn_id") is None for run in result["runs"])


def test_exact_duplicates_deduplicate_and_conflicting_source_identity_rejects_both():
    original = event(1, "caller_speech_started", BASE)
    duplicate = copy.deepcopy(original)
    result = build_provider_observation_runs(projection([original, duplicate]), **destination())
    assert len(result["runs"]) == 1 and result["coverage"]["exact_duplicate_events_ignored"] == 1
    duplicate["observed_unix_ms"] += 1
    result = build_provider_observation_runs(projection([original, duplicate]), **destination())
    assert result["runs"] == []
    assert result["coverage"]["skipped_event_counts"] == {"conflicting_source_entry_identity": 1}


def test_export_limit_is_explicit_and_ids_do_not_depend_on_selected_run_count():
    source = projection([event(index, "pipeline_cleared", BASE + index) for index in range(5)])
    target = destination(); full = build_provider_observation_runs(source, **target)
    bounded = build_provider_observation_runs(source, **target, max_runs=2)
    assert bounded["runs"] == full["runs"][:2]
    assert bounded["coverage"]["export_limit_omitted_spans"] == 3
    assert bounded["coverage"]["complete_provider_visibility"] is False


@pytest.mark.parametrize("defect", ["root", "project", "dotted", "limit", "projection_version", "call", "different_call", "oversized"])
def test_invalid_destination_projection_or_bounds_fail_with_fixed_codes_only(defect):
    target = destination(); source = projection([])
    if defect == "root": target["root_run_id"] = "PRIVATE"
    elif defect == "project": target["project_id"] = "PRIVATE"
    elif defect == "dotted": target["root_dotted_order"] += ".PRIVATE"
    elif defect == "limit": target["max_runs"] = True
    elif defect == "projection_version": source["schema_version"] = "PRIVATE"
    elif defect == "call": source["call_id"] = "PRIVATE"
    elif defect == "different_call": source["call_id"] = str(uuid4())
    elif defect == "oversized": source["events"] = [{}] * 10001
    with pytest.raises(ProviderSpanError) as error:
        build_provider_observation_runs(source, **target)
    assert "PRIVATE" not in str(error.value)
