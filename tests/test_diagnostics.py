"""Component evidence must stay isolated, loss-aware, scoped and content-free."""
from contextlib import closing
import hashlib
import json
import sqlite3
import threading
import time
from uuid import uuid4

import pytest

from app import diagnostics as module
from app.diagnostics import (Diagnostics, NullDiagnostics, call_key, canonical, component_catalog,
    component_coverage, configuration_manifest, create_diagnostics, operation_identifier,
    read_call_diagnostics, safe_attributes, turn_identifier)


def test_component_blueprint_covers_three_paths_and_five_questions():
    catalog=component_catalog()
    assert {v["path"] for v in catalog.values()}=={"audio","decision","business","evaluation"}
    for name in ("caller_device","voice_runtime","llm","tool_harness","scheduler"):
        assert name in catalog
    for row in catalog.values():
        assert all(row[k] for k in ("owner","input","output","failure_modes","observation"))
    catalog["llm"]["owner"]="changed"
    assert component_catalog()["llm"]["owner"]!="changed"


def test_generation_attributes_use_the_same_strict_content_free_projection():
    generation = str(uuid4())
    source = {"generation_id": generation, "generation_sequence": 17,
        "generation_sequence_scope": "process_local_http_arrival_only",
        "generation_terminal_state": "superseded_before_release", "checkpoint_rejections": 1,
        "payload_release_scope": "gateway_yield_not_provider_receipt_or_playback",
        "transcript": "private caller words", "Authorization": "secret", "audio": "private"}
    safe = safe_attributes(source)
    assert safe == {k: source[k] for k in ("generation_id", "generation_sequence", "generation_sequence_scope",
        "generation_terminal_state", "checkpoint_rejections", "payload_release_scope")}
    assert safe_attributes({"generation_id": "private text", "predecessor_generation_id": "secret",
        "generation_terminal_state": "private words", "checkpoint_rejections": True}) == {}


def test_configuration_identity_changes_for_owned_source_or_safe_settings_only(tmp_path):
    (tmp_path/"example.py").write_text("x=1")
    base={"SERVICEVOICE_ENVIRONMENT":"production","VAPI_VOICE_PROFILE":"elliot-v2","SERVICEVOICE_RECEIPT_NARRATION":"on"}
    first=configuration_manifest(base,tmp_path)
    assert first==configuration_manifest({**base,"GEMINI_API_KEY":"private-credential","PASSWORD":"private","VAPI_WEBHOOK_SECRET":"private"},tmp_path)
    assert first["id"]!=configuration_manifest({**base,"SERVICEVOICE_RECEIPT_NARRATION":"off"},tmp_path)["id"]
    (tmp_path/"example.py").write_text("x=2")
    assert first["id"]!=configuration_manifest(base,tmp_path)["id"]
    assert first["provider_effective_configuration"]=="unknown"
    assert "private" not in json.dumps(first)


def test_default_manifest_hashes_only_fixed_owned_entrypoints_and_no_environment_files(tmp_path,monkeypatch):
    root=tmp_path/"app";root.mkdir();deploy=tmp_path/"deploy";deploy.mkdir()
    (root/"diagnostics.py").write_text("owned=1")
    (deploy/"server.py").write_text("server=1")
    (deploy/"tts_server.py").write_text("speech=1")
    (deploy/"private.env").write_text("GEMINI_API_KEY=private-one")
    (tmp_path/".env").write_text("GEMINI_API_KEY=private-two")
    monkeypatch.setattr(module,"__file__",str(root/"diagnostics.py"))
    original_read=module.Path.read_bytes;read=[]
    def guarded_read(path):
        read.append(path)
        assert path.suffix==".py" and path.name in {"diagnostics.py","server.py","tts_server.py"}
        return original_read(path)
    monkeypatch.setattr(module.Path,"read_bytes",guarded_read)
    first=configuration_manifest()
    assert {path.name for path in read}=={"diagnostics.py","server.py","tts_server.py"}
    assert first["provider_effective_configuration"]=="unknown"
    (deploy/"private.env").write_text("GEMINI_API_KEY=changed-private")
    (tmp_path/".env").write_text("GEMINI_API_KEY=changed-private-too")
    assert configuration_manifest()["id"]==first["id"]
    (deploy/"server.py").write_text("server=2")
    second=configuration_manifest()
    assert second["id"]!=first["id"]
    (deploy/"tts_server.py").write_text("speech=2")
    assert configuration_manifest()["id"]!=second["id"]
    custom=configuration_manifest(source_root=root)
    (deploy/"tts_server.py").write_text("speech=3")
    assert configuration_manifest(source_root=root)["id"]==custom["id"]


def test_unrecognized_configuration_never_retains_arbitrary_values(tmp_path):
    secret="Bearer PRIVATE_CREDENTIAL sarath@example.com"
    data=configuration_manifest({"VAPI_VOICE_PROFILE":secret,"SERVICEVOICE_ENVIRONMENT":{},"SERVICEVOICE_RECEIPT_NARRATION":[],"SERVICEVOICE_MAX_LIVE_CALLS":"bad-private"},tmp_path)
    assert secret not in json.dumps(data)
    assert "bad-private" not in json.dumps(data)
    assert data["capture_issues"]
    assert data["values"]["VAPI_VOICE_PROFILE"]=="unknown"


def test_breadcrumb_identity_is_stable_scoped_and_separates_operations():
    assert turn_identifier("call-a",2)==turn_identifier("call-a",2)
    assert turn_identifier("call-a",2)!=turn_identifier("call-a",3)
    assert turn_identifier("call-a",2)!=turn_identifier("call-b",2)
    assert turn_identifier("call-a",0) is None
    assert turn_identifier("call-a",True) is None
    assert operation_identifier("sid","tid")=="op_"+hashlib.sha256(b"sid\0tid").hexdigest()[:24]
    assert operation_identifier("sid","tid")!=operation_identifier("sid","other")
    assert call_key({}) is None


def test_attribute_projection_preserves_only_documented_measurements():
    result=safe_attributes({"model":"gemini-3.8-flash-lite-tts","voice":"Kore","audio_bytes":48000,"first_audio_ms":252.3419,
        "http_status":429,"error_code":"tts_upstream_rate_limited","observation_scope":"server_bytes_only_not_audible_playback",
        "input_tokens":True,"output_tokens":-1,"total_tokens":float("nan"),"transcript":"private","arguments":{"phone":"private"},
        "exception":"Bearer private","url":"https://private/?key=secret","provider_call_id":"private","turn_id_status":"not_provided"})
    assert result["first_audio_ms"]==252.342 and result["error_code"]=="tts_upstream_rate_limited"
    assert not any(key in result for key in ("input_tokens","output_tokens","total_tokens","transcript","arguments","exception","url","provider_call_id"))
    assert "private" not in json.dumps(result)


def test_phone_ingress_and_custom_speech_failure_projections_remain_fixed_codes():
    for code in ("tts_resampler_unavailable", "tts_resampling_failed", "tts_upstream_missing_sample_rate",
                 "tts_upstream_wrong_channel_count", "tts_upstream_sample_rate_changed", "tts_upstream_audio_after_completion"):
        projected = safe_attributes({"evidence_source": "verified_phone_ingress", "response_started": True,
                                     "response_complete": False, "error_code": code, "exception": "private"})
        assert projected == {"evidence_source": "verified_phone_ingress", "response_started": True,
                             "response_complete": False, "error_code": code}
    assert safe_attributes({"upstream_http_status": 200}) == {"upstream_http_status": 200}
    assert safe_attributes({"upstream_http_status": 600}) == {}


def test_restart_preserves_request_turn_operation_and_config_without_content(tmp_path):
    path=tmp_path/"diagnostics.sqlite3";call="provider-call-private";request=uuid4().hex
    turn=turn_identifier(call,2);operation=operation_identifier("sid","tool-id")
    sink=Diagnostics(path,{"SERVICEVOICE_ENVIRONMENT":"production"})
    assert sink.record("tool_harness","execution_finished","success",call_id=call,turn_id=turn,request_id=request,operation_id=operation,duration_ms=12,
        attributes={"receipt_present":True,"tool_name":"confirm_action","transcript":"PRIVATE NAME 2025550146","headers":{"authorization":"PRIVATE"}})
    # Verify persisted content without imposing a disk-speed target on shared CI.
    assert sink.close(timeout_seconds=10)
    report=read_call_diagnostics(path,call)
    assert report["coverage"]["state"]=="observed" and report["coverage"]["complete"] is False
    row=report["events"][0]
    assert (row["turn_id"],row["request_id"],row["operation_id"])==(turn,request,operation)
    assert row["config_id"]==report["configuration"]["id"]
    for forbidden in (call,"PRIVATE","2025550146","authorization"):
        assert forbidden not in json.dumps(report)
        assert forbidden.encode() not in path.read_bytes()
    second=Diagnostics(path)
    assert second.close(timeout_seconds=10)
    assert read_call_diagnostics(path,call)["events"][0]==row
    assert "prior_unclean_shutdown_possible_loss" not in read_call_diagnostics(path,call)["coverage"]["reason_codes"]


def test_read_scope_truncation_and_absence_are_explicit(tmp_path):
    path=tmp_path/"ledger.sqlite3";sink=Diagnostics(path)
    for index in range(3):
        assert sink.record("gateway","request_finished","success",call_id="one",start_unix=time.time()+index/1000)
    assert sink.record("gateway","request_finished","failure",call_id="two")
    assert sink.close(timeout_seconds=10)
    first=read_call_diagnostics(path,"one",limit=2)
    assert len(first["events"])==2 and first["coverage"]["truncated"]
    assert first["coverage"]["state"]=="incomplete"
    assert len(read_call_diagnostics(path,"two")["events"])==1
    missing=read_call_diagnostics(path,"missing")
    assert missing["coverage"]["dropped_events"] is None and missing["coverage"]["state"]=="unavailable"
    assert read_call_diagnostics(path,"one",limit=8001)["coverage"]["state"]=="unavailable"
    assert read_call_diagnostics(tmp_path/"absent.sqlite3","one")["coverage"]["state"]=="unavailable"
    assert not (tmp_path/"absent.sqlite3").exists()


def test_queue_pressure_is_nonblocking_and_marks_the_affected_call(tmp_path,monkeypatch):
    entered,release=threading.Event(),threading.Event();original=Diagnostics._write;caller_thread=threading.get_ident()
    def blocked(self,rows,**kwargs):
        assert threading.get_ident()!=caller_thread
        if rows:
            entered.set();assert release.wait(3)
        return original(self,rows,**kwargs)
    monkeypatch.setattr(Diagnostics,"_write",blocked)
    sink=Diagnostics(tmp_path/"ledger.sqlite3",queue_size=1,batch_size=1)
    try:
        assert sink.record("gateway","request_finished","success",call_id="one")
        assert entered.wait(2)
        assert sink.record("gateway","request_finished","success",call_id="one")
        assert not sink.record("gateway","request_finished","success",call_id="lost")
        assert sink.status()["queue_dropped"]==1
        assert sink.snapshot("lost")["coverage"]["dropped_events"]==1
    finally:release.set();assert sink.close(timeout_seconds=10)
    final=read_call_diagnostics(sink.path,"lost")
    assert final["coverage"]["dropped_events"]==1
    assert final["coverage"]["state"]=="incomplete"
    assert read_call_diagnostics(sink.path,"one")["coverage"]["dropped_events"]==0


def test_storage_failure_is_per_call_and_visible_after_recovery(tmp_path,monkeypatch):
    original=Diagnostics._write;failed=threading.Event()
    def fail_once(self,rows,**kwargs):
        if rows and not failed.is_set():
            failed.set();raise sqlite3.OperationalError("PRIVATE synthetic lock")
        return original(self,rows,**kwargs)
    monkeypatch.setattr(Diagnostics,"_write",fail_once)
    sink=Diagnostics(tmp_path/"ledger.sqlite3")
    assert sink.record("gateway","request_finished","success",call_id="lost")
    assert failed.wait(2)
    assert sink.close(timeout_seconds=10)
    report=read_call_diagnostics(sink.path,"lost")
    assert report["coverage"]["dropped_events"]==1
    assert report["coverage"]["state"]=="incomplete"
    assert "PRIVATE" not in json.dumps(report)
    assert sink.status()["storage_dropped"]==1


def test_capacity_retention_pruning_keeps_explicit_call_tombstone(tmp_path):
    sink=Diagnostics(tmp_path/"ledger.sqlite3",max_rows=1,ttl_seconds=60)
    now=time.time()
    assert sink.record("gateway","request_finished","success",call_id="old",start_unix=now-20)
    assert sink.record("gateway","request_finished","success",call_id="new",start_unix=now)
    assert sink.close(timeout_seconds=10)
    old=read_call_diagnostics(sink.path,"old")
    assert old["events"]==[] and old["coverage"]["retention_pruned"]
    assert old["coverage"]["state"]=="incomplete"
    assert len(read_call_diagnostics(sink.path,"new")["events"])==1


def test_unclean_shutdown_marker_and_stale_writer_do_not_claim_complete_capture(tmp_path):
    sink=Diagnostics(tmp_path/"ledger.sqlite3")
    sink.record("gateway","request_finished","success",call_id="one")
    assert sink.close(timeout_seconds=10)
    with sqlite3.connect(sink.path) as db:
        db.execute("UPDATE diagnostic_state SET payload='true' WHERE key='writer_open'")
        db.execute("UPDATE diagnostic_state SET payload=? WHERE key='last_write_unix'",(str(time.time()-200),))
    report=read_call_diagnostics(sink.path,"one")
    assert "diagnostic_writer_stale" in report["coverage"]["reason_codes"]
    restarted=Diagnostics(sink.path);assert restarted.close(timeout_seconds=10)
    report=read_call_diagnostics(sink.path,"one")
    assert "prior_unclean_shutdown_possible_loss" in report["coverage"]["reason_codes"]


@pytest.mark.parametrize("field,value",[("component",{}),("event",["request"]),("outcome",False),("turn_id","PRIVATE"),("request_id","PRIVATE"),("operation_id","PRIVATE"),("duration_ms",float("inf"))])
def test_invalid_records_are_rejected_and_counted(tmp_path,field,value):
    sink=Diagnostics(tmp_path/"ledger.sqlite3")
    args={"component":"gateway","event":"request_finished","outcome":"success","call_id":"one"}
    args[field]=value
    assert not sink.record(**args)
    assert sink.close(timeout_seconds=10)
    assert sink.status()["filtered"]==1
    report=read_call_diagnostics(sink.path,"one")
    assert report["coverage"]["state"]=="incomplete"


def test_modified_sidecar_cannot_export_raw_attributes_or_manifest_values(tmp_path):
    sink=Diagnostics(tmp_path/"ledger.sqlite3")
    sink.record("gateway","request_finished","success",call_id="one")
    assert sink.close(timeout_seconds=10)
    with sqlite3.connect(sink.path) as db:
        eid,payload=db.execute("SELECT event_id,payload FROM diagnostic_events").fetchone();event=json.loads(payload)
        event["attributes"]["transcript"]="PRIVATE"
        _,payload=db.execute("SELECT config_id,payload FROM diagnostic_configs").fetchone();manifest=json.loads(payload)
        manifest["values"]["GEMINI_API_KEY"]="PRIVATE"
        manifest["capture_issues"]=["PRIVATE"]
        manifest.pop("id");cid=hashlib.sha256(canonical(manifest).encode()).hexdigest();manifest["id"]=cid
        event["config_id"]=cid
        db.execute("UPDATE diagnostic_events SET config_id=?,payload=? WHERE event_id=?",(cid,canonical(event),eid))
        db.execute("INSERT INTO diagnostic_configs VALUES (?,?)",(cid,canonical(manifest)))
        db.execute("UPDATE diagnostic_calls SET dropped='PRIVATE'")
    report=read_call_diagnostics(sink.path,"one")
    assert "PRIVATE" not in json.dumps(report)
    assert report["coverage"]["state"]=="incomplete"


def test_component_presence_is_not_equated_with_quality_or_audio_observation(tmp_path):
    sink=Diagnostics(tmp_path/"ledger.sqlite3")
    sink.record("gateway","request_finished","failure",call_id="one")
    assert sink.close(timeout_seconds=10)
    coverage=component_coverage(read_call_diagnostics(sink.path,"one"))
    assert coverage["complete"] is False
    assert coverage["components"]["gateway"]["failed_event_count"]==1
    assert coverage["components"]["caller_device"]["evidence_status"]=="unavailable"
    assert coverage["components"]["stt"]["evidence_status"]=="unavailable"


def test_disabled_sink_has_no_filesystem_side_effect_and_explicit_unknown(tmp_path):
    path=tmp_path/"never"/"ledger.sqlite3"
    sink=create_diagnostics(path,{"SERVICEVOICE_DIAGNOSTICS_ENABLED":"off"})
    assert isinstance(sink,NullDiagnostics)
    assert not sink.record("llm","generation_finished","success",call_id="one")
    assert sink.snapshot("one")["coverage"]["state"]=="unavailable"
    assert sink.close(timeout_seconds=10) and not path.parent.exists()
