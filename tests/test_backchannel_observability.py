"""Acknowledgment timing must never masquerade as a faster substantive answer."""
from copy import deepcopy
from pathlib import Path

from app.diagnostics import safe_attributes
from app.diagnostics import Diagnostics, configuration_manifest
from app.live_eval_evidence import build_evidence, latency, read_snapshot
from app.live_evaluations import public_report, upgrade_v1_metrics
from app.observability import LocalTimingSink, safe_backchannel_timing
from app.live_eval_evidence import read_timings
from test_local_timing import row
from test_live_evaluations import scenario, configuration, judgment


def candidate_timings():
    return [{"outcome":"completed","milestones_ms":{"first_text":4000},
             "backchannel":{"phrase_id":"moment","emitted_ms":550}}]


def test_ack_and_answer_metrics_are_separate_not_zero_latency():
    call={"artifact":{"performanceMetrics":{"turnLatencies":[{"turnLatency":900}],"turnLatencyAverage":900}}}
    metrics=latency(call,candidate_timings())
    assert metrics["turn_avg_ms"]==900
    assert metrics["server_first_ack_p95_ms"]==550
    assert metrics["server_substantive_text_p95_ms"]==4000
    assert metrics["provider_latency_scope"]=="may_include_acknowledgments_not_substantive_answers"
    assert latency(call,[])==latency(call,[{"backchannel":{"emitted_ms":None}}])


def test_fast_ack_cannot_pass_real_call_latency_grade_or_refresh(scenario,monkeypatch):
    store,call=scenario
    call=deepcopy(call)
    call["artifact"]["performanceMetrics"]={"turnLatencyAverage":900,"turnLatencies":[{"turnLatency":900}]}
    package=build_evidence(call,read_snapshot(store.path,call["id"]))
    config=configuration(Path(store.path).parent,store.path)
    report=public_report(package,judgment(package),call,candidate_timings(),config)
    for key in ("latency_within_target","latency_tail_within_target"):
        assert report["checks"][key]["verdict"]=="uncertain"
        assert report["checks"][key]["reason_code"]=="acknowledgment_may_mask_substantive_answer_delay"
    report=deepcopy(report)
    report["review_version"]="live-call-review-v1"
    monkeypatch.setattr("app.live_evaluations.VERSION","live-call-review-v2")
    refreshed=upgrade_v1_metrics(report,call,candidate_timings(),config)
    assert refreshed["checks"]["latency_within_target"]["verdict"]=="uncertain"


def test_persisted_ack_projection_is_idempotent_and_remains_measurable():
    cue={"phrase_id":"moment","reason":"emitted","emitted_ms":550,"classification_ms":158,
         "requests":1,"probability":.999,"language_choice":"cache_compatible",
         "language_probability":1.0,"text":"PRIVATE WORDS"}
    safe=safe_backchannel_timing(cue)
    assert safe_backchannel_timing(safe)==safe
    assert safe["emitted_ms"]==550 and "text" not in safe
    row={**candidate_timings()[0],"backchannel":safe}
    assert latency({},[row])["server_first_ack_p95_ms"]==550


def test_real_timing_database_restart_keeps_ack_and_answer_separate(tmp_path):
    import hashlib
    call_id="synthetic-backchannel-evidence-only"
    path=tmp_path/"gateway.sqlite3"
    sink=LocalTimingSink(path)
    value=row(call_key=hashlib.sha256(call_id.encode()).hexdigest()[:16],
              backchannel={"phrase_id":"moment","reason":"emitted","emitted_ms":550})
    assert sink.record(value) and sink.close()
    restarted=LocalTimingSink(path)
    assert restarted.snapshot()[0]["backchannel"]["emitted_ms"]==550
    assert restarted.close()
    metrics=latency({},read_timings(path,call_id))
    assert metrics["server_first_ack_p95_ms"]==550
    assert metrics["server_substantive_text_p95_ms"]==2100


def test_dropped_timing_row_cannot_hide_explicit_call_scoped_ack(scenario):
    import hashlib
    store,call=scenario
    call=deepcopy(call)
    call["artifact"]["performanceMetrics"]={"turnLatencyAverage":900,"turnLatencies":[{"turnLatency":900}]}
    package=build_evidence(call,read_snapshot(store.path,call["id"]))
    config=configuration(Path(store.path).parent,store.path)
    event={"call_key":hashlib.sha256(call["id"].encode()).hexdigest(),"component":"gateway",
           "event":"backchannel.emitted","outcome":"observed"}
    report=public_report(package,judgment(package),call,[],config,diagnostics={"events":[event]})
    assert report["checks"]["latency_within_target"]["verdict"]=="uncertain"
    assert "server_first_ack_p95_ms" not in report["latency"]
    event["call_key"]="b"*64
    other=public_report(package,judgment(package),call,[],config,diagnostics={"events":[event]})
    assert other["checks"]["latency_within_target"]["verdict"]=="pass"


def test_ack_diagnostic_interval_ends_at_emission_not_in_future(tmp_path):
    import time
    from types import SimpleNamespace
    from app.gemini_gateway import GeminiGateway
    sink=Diagnostics(tmp_path/"diagnostics.sqlite3")
    started=time.time()-1
    timing=SimpleNamespace(row={"generation_id":"c"*32,"request_id":"a"*32,
        "started_at_unix":started,"backchannel":{"phrase_id":"moment","reason":"emitted",
        "classification_ms":150,"probability":1,"confidence":1,"requests":1,"policy_elapsed_ms":152,
        "language_choice":"cache_compatible","language_probability":1,"language_confidence":1}},
        diagnostic_context={"call_id":"synthetic-call"})
    GeminiGateway._backchannel_observation(SimpleNamespace(diagnostics=sink),timing,"backchannel.emitted",550)
    assert sink.close()
    event=sink.snapshot("synthetic-call")["events"][0]
    assert event["start_unix"]==started
    assert event["start_unix"]+event["duration_ms"]/1000<=time.time()
    assert event["duration_ms"]==550
    assert configuration_manifest({"SERVICEVOICE_BACKCHANNELS":"on"})["values"]["SERVICEVOICE_BACKCHANNELS"]=="on"


def test_fixed_backchannel_diagnostics_preserve_gate_evidence_and_drop_secrets():
    safe=safe_attributes({"backchannel_phrase":"moment","backchannel_reason":"cached_language_unverified",
        "backchannel_language":"cache_compatible","backchannel_language_probability":.98,
        "backchannel_probability":.999,"classifier_requests":1,"cached_sample_rate":16000,
        "text":"PRIVATE CALLER WORDS","authorization":"PRIVATE CREDENTIAL"})
    assert safe["backchannel_language_probability"]==.98 and safe["backchannel_probability"]==.999
    assert safe["backchannel_reason"]=="cached_language_unverified" and safe["classifier_requests"]==1
    assert "text" not in safe and "authorization" not in safe


def test_real_docker_build_context_contains_standalone_speech_entrypoint():
    root=Path(__file__).resolve().parents[1]
    assert "COPY deploy/tts_server.py" in (root/"Dockerfile").read_text()
    assert "!deploy/tts_server.py" in (root/".dockerignore").read_text().splitlines()
