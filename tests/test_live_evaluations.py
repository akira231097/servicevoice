"""Failure-oriented post-call worker checks. No live provider/network writes."""
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

import httpx
import pytest

from app.domain import Store
from app.gemini_gateway import SignatureStore, GatewayLimits
from app.issuance_context import user_context
from app.live_eval_evidence import (DIMENSIONS, EvidenceError, Redactor, build_evidence,
    canonical, integrity, latency, read_snapshot)
from app.live_evaluations import (Config, LangSmith, Outbox, Worker, feedback_rows,
    parse_judgment, public_report)


@pytest.fixture
def scenario(tmp_path):
    store=Store(tmp_path/'source.sqlite3')
    session=store.create_session('live');sid=session['id'];cid=str(uuid4())
    created=time.time()-600
    with store.connect() as db:
        db.execute('UPDATE sessions SET provider_call_id=?,created=?,ended=1 WHERE id=?',(cid,created,sid))
    availability=store.dispatch(sid,'begin_service_request',{'service':'oil_change'},'begin')
    slot=availability['slots'][0]['id']
    args={'service':'oil_change','slot_id':slot,'customer_name':'Alex Morgan','phone':'5125550142','vehicle':'2022 Toyota Camry'}
    prepared=store.dispatch(sid,'prepare_booking',args,'prepare')
    confirm={'proposal_id':prepared['proposal_id'],'confirmed':True}
    saved=store.dispatch(sid,'confirm_action',confirm,'confirm')
    signatures=SignatureStore(store.path,GatewayLimits())
    def tool(tid,name,arguments,count):
        value={'id':tid,'type':'function','function':{'name':name,'arguments':canonical(arguments)}}
        signatures.save_group(sid,cid,[value],context=user_context([{'role':'user','content':'u'+str(i)} for i in range(count)]))
        return {'role':'tool_calls','toolCalls':[value]}
    call={'id':cid,'status':'ended','orgId':str(uuid4()),'createdAt':datetime.fromtimestamp(created,timezone.utc).isoformat(),'startedAt':datetime.fromtimestamp(created,timezone.utc).isoformat(),
          'endedAt':datetime.fromtimestamp(created+120,timezone.utc).isoformat(),
          'artifact':{'messages':[
              {'role':'system','message':'PRIVATE PROMPT MUST NEVER BE EXPORTED'},
              {'role':'user','message':'Oil change please. My name is Alex Morgan, 5125550142, 2022 Toyota Camry.'},
              tool('begin','begin_service_request',{'service':'oil_change'},1),tool('prepare','prepare_booking',args,1),
              {'role':'bot','message':prepared['summary']+' Shall I book it?'},
              {'role':'user','message':'Yes, please book that.'},tool('confirm','confirm_action',confirm,2),
              {'role':'bot','message':'Booked. Your reference is '+saved['appointment']['reference']}],
              'performanceMetrics':{'turnLatencyAverage':1500,'modelLatencyAverage':900,'voiceLatencyAverage':200,
                  'endpointingLatencyAverage':300,'turnLatencies':[{'turnLatency':1200},{'turnLatency':1800}]}}}
    return store,call


def configuration(tmp_path,source):
    return Config(source=Path(source),outbox=tmp_path/'evaluation'/'outbox.sqlite3',vapi_key='v'*32,
        gemini_key='g'*32,langsmith_key='l'*32,workspace=str(uuid4()))


def judgment(package,verdict='pass'):
    return {d:{'verdict':verdict,'evidence_refs':['state'],'issue_code':'none' if verdict in {'pass','not_applicable'} else 'other','reason':'Evidence-based advisory review.'} for d in DIMENSIONS}


def test_source_is_readonly_and_customer_names_consistently_aliased(scenario):
    store,call=scenario
    before=Path(store.path).read_bytes()
    snapshot=read_snapshot(store.path,call['id']);package=build_evidence(call,snapshot)
    serialized=canonical(package)
    assert 'Alex Morgan' not in serialized and '5125550142' not in serialized
    assert '[customer_1]' in serialized and '[phone_1]' in serialized
    assert 'PRIVATE PROMPT' not in serialized and 'thought_signature' not in serialized
    assert Path(store.path).read_bytes()==before
    assert package['mechanical_checks']['booking_integrity']['verdict']=='pass'
    assert package['mechanical_checks']['confirmation_order']['verdict']=='pass'


@pytest.mark.parametrize('field,value',[('slot_id','wrong_slot'),('reference','CSV-11111111'),('service','brakes'),('status','cancelled')])
def test_changed_saved_booking_detected(scenario,field,value):
    snapshot=read_snapshot(scenario[0].path,scenario[1]['id'])
    snapshot['appointments'][0][field]=value
    assert integrity(snapshot)['verdict']=='fail'


def test_changed_callback_detected_even_if_spoken_receipt_omits_it(scenario):
    snapshot=read_snapshot(scenario[0].path,scenario[1]['id'])
    snapshot['appointments'][0]['customer']['phone']='+12175550111'
    assert integrity(snapshot)['reason_code']=='saved_customer_proposal_mismatch'


def test_no_booking_not_counted_as_booking_success(scenario):
    snapshot=read_snapshot(scenario[0].path,scenario[1]['id'])
    snapshot['appointments']=[];snapshot['proposals']=[]
    assert integrity(snapshot)['verdict']=='not_applicable'


def test_missing_commit_receipt_and_pending_effect_never_pass(scenario):
    snapshot=read_snapshot(scenario[0].path,scenario[1]['id'])
    snapshot['proposals'][0]['result']=None
    assert integrity(snapshot)['verdict']=='fail'
    snapshot['operations'].append({'call_id':'pending','status':'unknown'})
    assert integrity(snapshot)['verdict']=='uncertain'


def test_duplicate_receipt_for_historical_action_does_not_change_current_state(scenario):
    store,call=scenario
    snapshot=read_snapshot(store.path,call['id'])
    booking=snapshot['proposals'][0]
    reschedule=deepcopy(booking)
    reschedule['kind']='reschedule';reschedule['payload']={'slot_id':'later'}
    reschedule['result']['appointment']['slot_id']='later'
    snapshot['proposals'].append(reschedule);snapshot['appointments'][0]['slot_id']='later'
    assert integrity(snapshot)['verdict']=='pass'


def test_confirm_in_same_user_turn_fails_despite_successful_save(scenario):
    store,call=scenario
    snapshot=read_snapshot(store.path,call['id'])
    for sig in snapshot['signatures']:sig['user_count']=1
    package=build_evidence(call,snapshot)
    assert package['mechanical_checks']['confirmation_order']['verdict']=='fail'
    report=public_report(package,judgment(package),call,[],configuration(Path(store.path).parent,store.path))
    assert report['checks']['confirmation_compliance']['verdict']=='fail'


def test_missing_issuance_anchor_is_uncertain_not_proof_of_consent(scenario):
    snapshot=read_snapshot(scenario[0].path,scenario[1]['id']);snapshot['signatures']=[]
    package=build_evidence(scenario[1],snapshot)
    assert package['mechanical_checks']['confirmation_order']['verdict']=='uncertain'


def test_no_caller_transcript_cannot_receive_clean_semantic_scores(scenario,tmp_path):
    store,call=scenario
    call['artifact']['messages']=[m for m in call['artifact']['messages'] if m['role']!='user']
    package=build_evidence(call,read_snapshot(store.path,call['id']))
    report=public_report(package,judgment(package),call,[],configuration(tmp_path,store.path))
    assert all(report['checks'][d]['verdict']=='uncertain' for d in DIMENSIONS)
    assert report['needs_review']


def test_conflicting_repeated_tool_arguments_make_evidence_incomplete(scenario):
    store,call=scenario
    duplicate=deepcopy(call['artifact']['messages'][3]);duplicate['toolCalls'][0]['function']['arguments']='{"service":"brakes"}'
    call['artifact']['messages'].append(duplicate)
    package=build_evidence(call,read_snapshot(store.path,call['id']))
    assert 'conflicting_tool_identity' in package['evidence_issues']


def test_model_cannot_fabricate_evidence_references(scenario):
    package=build_evidence(scenario[1],read_snapshot(scenario[0].path,scenario[1]['id']))
    value=judgment(package);value['hallucination_free']['evidence_refs']=['invented:1']
    with pytest.raises(EvidenceError):parse_judgment(value,package)


def test_export_boundary_ignores_judge_prose_and_source_text(scenario,tmp_path):
    store,call=scenario
    package=build_evidence(call,read_snapshot(store.path,call['id']))
    value=judgment(package)
    for v in value.values():v['reason']='Alex Morgan 5125550142 api-key-secret raw transcript'
    report=public_report(package,value,call,[],configuration(tmp_path,store.path))
    exported=canonical(report)+canonical(list(feedback_rows(str(uuid4()),report)))
    for forbidden in ['Alex','5125550142','api-key-secret','raw transcript','CSV-','PRIVATE PROMPT']:assert forbidden not in exported


def test_unknown_scores_are_null_not_zero_or_pass(scenario,tmp_path):
    store,call=scenario
    package=build_evidence(call,read_snapshot(store.path,call['id']))
    report=public_report(package,{},call,[],configuration(tmp_path,store.path))
    feedback={r['key']:r for r in feedback_rows(str(uuid4()),report)}
    assert feedback['hallucination_free']['score'] is None
    assert feedback['hallucination_free']['value']=='uncertain'
    assert feedback['needs_review']['score']==1


def test_latency_missing_is_unknown_and_p95_is_measured(scenario):
    assert latency({})=={'interpretation':'Vapi-reported milliseconds; server first text excludes speech and transport. No acoustic calibration.'}
    result=latency(scenario[1],[{'outcome':'completed','milestones_ms':{'first_text':500}}])
    assert result['turn_p95_ms']==1800 and result['server_first_text_p95_ms']==500


def test_redactor_does_not_merge_different_corrected_phone_numbers():
    redactor=Redactor({})
    assert redactor.text('5125550142')==redactor.text('+1 (512) 555-0142')
    assert redactor.text('5125550143')!=redactor.text('5125550142')


@pytest.mark.parametrize('field,value',[('endpoint','https://attacker.example'),('monthly_limit',1001),('project','bad/path'),('outbox',None)])
def test_invalid_destination_or_budget_refused(tmp_path,field,value):
    c=configuration(tmp_path,tmp_path/'source.sqlite3')
    if field=='outbox':value=c.source
    with pytest.raises(ValueError):replace(c,**{field:value}).validate()


def test_durable_claim_and_monthly_budget_survive_restart(scenario,tmp_path):
    now=[time.time()]
    out=Outbox(tmp_path/'outbox.sqlite3',clock=lambda:now[0]);out.discover(scenario[0].path)
    first=out.claim();assert first
    second=Outbox(out.path,clock=lambda:now[0]);assert second.claim() is None
    assert out.reserve_judge(first['call_id'],1)
    assert not second.reserve_judge(first['call_id'],1)
    now[0]+=301
    assert second.claim()['call_id']==first['call_id']


class ProviderMock:
    def __init__(self,call,workspace):
        self.call=call;self.workspace=workspace;self.project=str(uuid4())
        self.runs={};self.feedback={};self.judge_requests=0;self.calls=0;self.fail_feedback_once=False;self.bad_judge=False
    def __call__(self,request):
        path=request.url.path
        def result(body,status=200):return httpx.Response(status,json=body)
        if request.url.host=='api.vapi.ai':self.calls+=1;return result(self.call)
        if request.url.host=='generativelanguage.googleapis.com':
            self.judge_requests+=1
            package=json.loads(json.loads(request.content)['messages'][1]['content'].split('\n',1)[1])
            value=judgment(package)
            if self.bad_judge:value['hallucination_free']['evidence_refs']=['bad']
            return result({'model':'gemini-3.8-flash','choices':[{'finish_reason':'stop','message':{'content':canonical(value)}}]})
        if path=='/sessions':return result([{'id':self.project,'tenant_id':self.workspace,'name':'servicevoice-live-calls'}])
        if path=='/runs':
            body=json.loads(request.content);self.runs[body['id']]=body;return result({},202)
        if path.startswith('/runs/'):
            rid=path.rsplit('/',1)[1]
            if request.method=='PATCH' and rid in self.runs:self.runs[rid].update(json.loads(request.content))
            return result(self.runs.get(rid,{}),200 if rid in self.runs else 404)
        if path=='/feedback':
            if self.fail_feedback_once:self.fail_feedback_once=False;return result({},503)
            body=json.loads(request.content);assert body['extend_trace_retention'] is False
            self.feedback[body['id']]=body;return result(body)
        if path.startswith('/feedback/'):
            fid=path.rsplit('/',1)[1];return result(self.feedback.get(fid,{}),200 if fid in self.feedback else 404)
        raise AssertionError(str(request.url))


def worker_fixture(tmp_path,scenario):
    store,call=scenario;c=configuration(tmp_path,store.path);now=[time.time()]
    mock=ProviderMock(call,c.workspace)
    client=httpx.Client(transport=httpx.MockTransport(mock))
    worker=Worker(c,client=client,clock=lambda:now[0])
    return worker,mock,now


def test_call_is_automatically_reviewed_once_with_verified_feedback(scenario,tmp_path):
    worker,mock,_=worker_fixture(tmp_path,scenario)
    assert worker.tick()
    assert worker.outbox.status()['jobs']=={'complete':1}
    assert mock.judge_requests==1 and len(mock.runs)==1 and len(mock.feedback)>=10
    assert not worker.tick() and mock.judge_requests==1


def test_upload_outage_retries_cached_review_without_another_model_call(scenario,tmp_path):
    worker,mock,now=worker_fixture(tmp_path,scenario);mock.fail_feedback_once=True
    worker.tick();assert worker.outbox.status()['jobs']=={'pending':1}
    now[0]+=901;worker.tick()
    assert worker.outbox.status()['jobs']=={'complete':1}
    assert mock.judge_requests==1 and len(mock.runs)==1


def test_active_call_is_never_judged(scenario,tmp_path):
    worker,mock,_=worker_fixture(tmp_path,scenario);mock.call['status']='in-progress'
    worker.tick()
    assert mock.judge_requests==0 and not mock.runs


def test_wrong_provider_identity_never_reaches_judge_or_langsmith(scenario,tmp_path):
    worker,mock,_=worker_fixture(tmp_path,scenario);mock.call['id']=str(uuid4())
    worker.tick()
    assert mock.judge_requests==0 and not mock.runs
    assert 'provider_call_identity_mismatch' in worker.outbox.status()['pending_reasons']


def test_persistently_invalid_judge_publishes_uncertain_instead_of_fabricated_pass(scenario,tmp_path):
    worker,mock,now=worker_fixture(tmp_path,scenario);mock.bad_judge=True
    for _ in range(3):worker.tick();now[0]+=901
    assert mock.judge_requests==3
    assert worker.outbox.status()['jobs']=={'complete':1}
    report=next(iter(mock.runs.values()))['outputs']
    assert report['checks']['hallucination_free']['verdict']=='uncertain'


def test_no_transcript_uses_no_judge_budget_and_remains_visible(scenario,tmp_path):
    worker,mock,_=worker_fixture(tmp_path,scenario);mock.call['artifact']['messages']=[]
    worker.tick()
    assert mock.judge_requests==0 and len(mock.runs)==1
    assert next(iter(mock.runs.values()))['outputs']['needs_review']


def test_existing_run_payload_drift_does_not_receive_wrong_feedback(scenario,tmp_path):
    worker,mock,now=worker_fixture(tmp_path,scenario);mock.fail_feedback_once=True
    worker.tick()
    next(iter(mock.runs.values()))['outputs']={'changed':True}
    now[0]+=901;worker.tick()
    assert not mock.feedback
    assert 'run_readback_mismatch_or_ingestion_pending' in worker.outbox.status()['pending_reasons']


def test_missing_provider_record_is_not_retried_forever_or_scored_as_pass(scenario,tmp_path):
    worker,mock,now=worker_fixture(tmp_path,scenario)
    original=worker.client
    worker.client=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(404,json={})))
    for _ in range(3):worker.tick();now[0]+=901
    assert worker.outbox.status()['jobs']=={'unavailable':1}
    assert mock.judge_requests==0 and not mock.runs
    assert not worker.tick()
    original.close();worker.client.close()


def test_coverage_preserves_immutable_snapshots_and_never_adds_call_feedback(scenario,tmp_path):
    worker,mock,now=worker_fixture(tmp_path,scenario)
    worker.langsmith.coverage(worker.outbox.status(),now[0])
    worker.langsmith.coverage({'jobs':{'complete':5},'pending_reasons':{}},now[0]+100)
    assert len(mock.runs)==2 and not mock.feedback
    worker.langsmith.coverage({'jobs':{'complete':5},'pending_reasons':{}},now[0]+101)
    assert len(mock.runs)==2
    row=list(mock.runs.values())[-1]
    assert row['name']=='voice.review.coverage' and row['outputs']['jobs']=={'complete':5}


def test_budget_exhaustion_still_exports_an_explicit_unavailable_review(scenario,tmp_path):
    worker,mock,_=worker_fixture(tmp_path,scenario)
    worker.outbox.discover(worker.config.source)
    worker.config=replace(worker.config,monthly_limit=1)
    assert worker.outbox.reserve_judge(mock.call['id'],1)
    worker.tick()
    assert mock.judge_requests==0 and len(mock.runs)==1
    assert next(iter(mock.runs.values()))['outputs']['checks']['hallucination_free']['verdict']=='uncertain'


def test_unverified_reference_is_flagged_without_assuming_it_was_asserted(scenario):
    store,call=scenario
    call['artifact']['messages'].append({'role':'bot','message':'Did you say CSV-11111111?'})
    package=build_evidence(call,read_snapshot(store.path,call['id']))
    assert package['mechanical_checks']['reference_consistency']['verdict']=='uncertain'


def test_service_labels_are_not_replaced_as_customer_names():
    value={'facts':{'name':'Cedar','services':[{'name':'Oil & filter service'}]},'customer':{'name':'Alex Morgan','phone':'5125550142'}}
    safe=Redactor(value).value(value)
    assert safe['facts']==value['facts'] and safe['customer']['name']=='[customer_1]'


def test_prebinding_failure_is_discovered_and_published_without_inventing_booking_state(scenario,tmp_path):
    worker,mock,_=worker_fixture(tmp_path,scenario)
    assistant=str(uuid4());worker.config=replace(worker.config,assistant_id=assistant)
    mock.call['assistantId']=assistant
    with scenario[0].connect() as db:db.execute('UPDATE sessions SET provider_call_id=NULL')
    mock.call['artifact']['messages']=[{'role':'bot','message':'Hello, how can I help?'}]
    def handler(request):
        if request.url.host=='api.vapi.ai' and request.url.path=='/call':return httpx.Response(200,json=[mock.call])
        return mock(request)
    worker.client=httpx.Client(transport=httpx.MockTransport(handler))
    worker.tick()
    assert worker.outbox.status()['jobs']=={'complete':1}
    assert mock.judge_requests==0
    report=next(iter(mock.runs.values()))['outputs']
    assert report['booking']['appointment_count'] is None
    assert not report['booking']['scheduler_evidence_available']
    assert all(report['checks'][d]['verdict']=='uncertain' for d in DIMENSIONS)


def test_provider_list_cannot_import_another_assistants_calls(scenario,tmp_path):
    worker,mock,_=worker_fixture(tmp_path,scenario)
    worker.config=replace(worker.config,assistant_id=str(uuid4()))
    mock.call['assistantId']=str(uuid4())
    worker.client=httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200,json=[mock.call])))
    with pytest.raises(EvidenceError,match='scope_mismatch'):worker.discover_provider()
    assert worker.outbox.status()['jobs']=={}


def test_full_unordered_provider_window_is_partitioned_instead_of_silently_truncated(scenario,tmp_path):
    worker,mock,now=worker_fixture(tmp_path,scenario);assistant=str(uuid4())
    worker.config=replace(worker.config,assistant_id=assistant)
    calls=[]
    since=float(worker.outbox.setting('capture_since'))
    for i in range(21):
        call=deepcopy(mock.call);call['id']=str(uuid4());call['assistantId']=assistant
        call['createdAt']=datetime.fromtimestamp(since+1000+i*100,timezone.utc).isoformat();calls.append(call)
    def handler(request):
        start=datetime.fromisoformat(request.url.params['createdAtGe']).timestamp()
        end=datetime.fromisoformat(request.url.params['createdAtLe']).timestamp()
        rows=[c for c in reversed(calls) if start<=datetime.fromisoformat(c['createdAt']).timestamp()<=end]
        return httpx.Response(200,json=rows[:20])
    worker.client=httpx.Client(transport=httpx.MockTransport(handler))
    worker.discover_provider()
    assert worker.outbox.status()['jobs']=={'pending':21}


def test_late_backend_binding_upgrades_orphan_job_without_replaying_it(scenario,tmp_path):
    store,call=scenario;out=Outbox(tmp_path/'queue.sqlite3')
    out.discover_provider_calls([(call['id'],time.time()-600)])
    out.discover(store.path)
    with closing(out.db()) as db:row=db.execute('SELECT session_id FROM jobs').fetchone()
    assert row[0]==read_snapshot(store.path,call['id'])['session']['id']


def test_default_zero_averages_without_response_turns_are_not_instant_replies(scenario,tmp_path):
    store,call=scenario
    call['artifact']['performanceMetrics']={'turnLatencies':[],'turnLatencyAverage':0,'voiceLatencyAverage':0,'modelLatencyAverage':0}
    metrics=latency(call)
    assert 'turn_avg_ms' not in metrics and 'model_avg_ms' not in metrics
    package=build_evidence(call,read_snapshot(store.path,call['id']))
    report=public_report(package,judgment(package),call,[],configuration(tmp_path,store.path))
    assert report['checks']['latency_within_target']['verdict']=='uncertain'
    assert report['checks']['latency_tail_within_target']['verdict']=='uncertain'


def test_zero_summary_cannot_hide_positive_measured_turns():
    call={'artifact':{'performanceMetrics':{'turnLatencyAverage':0,'turnLatencies':[{'turnLatency':3000},{'turnLatency':5000}]}}}
    assert latency(call)['turn_avg_ms']==4000


def test_latency_revision_reuses_semantic_grades_without_another_judge_call(scenario,tmp_path):
    worker,mock,now=worker_fixture(tmp_path,scenario);worker.tick()
    with closing(worker.outbox.db()) as db,db:
        cached=json.loads(db.execute('SELECT report FROM jobs').fetchone()[0]);cached['review_version']='live-call-review-v1'
        db.execute('UPDATE jobs SET report=?',(canonical(cached),))
    mock.runs.clear();mock.feedback.clear()
    now[0]+=901
    restarted=Worker(worker.config,client=worker.client,clock=lambda:now[0]);restarted.tick()
    assert mock.judge_requests==1 and restarted.outbox.status()['jobs']=={'complete':1}
    report=next(iter(mock.runs.values()))['outputs']
    assert report['review_version']=='live-call-review-v2' and report['semantic_review_version']=='live-call-review-v1'
