"""Failure-oriented post-call worker checks. No live provider/network writes."""
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
from uuid import uuid4

import httpx
import pytest

from app.domain import Store
from app.gemini_gateway import SignatureStore, GatewayLimits
from app.issuance_context import user_context
from app.live_eval_evidence import (DIMENSIONS, VERSION, EvidenceError, Redactor, build_evidence,
    canonical, integrity, latency, read_snapshot)
from app import live_evaluations
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
    reschedule['kind']='reschedule';reschedule['payload']={'slot_id':'later','appointment_id':reschedule['result']['appointment']['id']}
    reschedule['result']['action']='reschedule'
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
        self.run_posts=[];self.fail_child_once=False;self.corrupt_child_field=None
        self.hide_child_readback_once=False
        self.hide_feedback_readback_once=False
        self.batch_requests=[];self.trace_queries=[];self.feedback_queries=[];self.rate_limit_batch_once=False
        self.request_paths=[]
        self.queues={};self.queued={};self.queue_posts=[];self.queue_adds=[];self.fail_queue=False
    def read_run(self,rid):
        body=deepcopy(self.runs.get(rid))
        if not isinstance(body,dict):return body
        if body.get('parent_run_id') and self.hide_child_readback_once:
            self.hide_child_readback_once=False;return None
        if body.get('parent_run_id') and self.corrupt_child_field:
            field=self.corrupt_child_field
            if field=='non_object':body=[]
            elif field=='equivalent_timezone':
                body['start_time']=datetime.fromisoformat(body['start_time']).isoformat().replace('+00:00','Z')
                body['end_time']=datetime.fromisoformat(body['end_time']).isoformat().replace('+00:00','Z')
            elif field=='vendor_utc_naive':
                body['start_time']=datetime.fromisoformat(body['start_time']).replace(tzinfo=None).isoformat()
                body['end_time']=datetime.fromisoformat(body['end_time']).replace(tzinfo=None).isoformat()
            elif field=='vendor_shifted_microsecond':
                from datetime import timedelta
                body['start_time']=(datetime.fromisoformat(body['start_time'])+timedelta(microseconds=1)).replace(tzinfo=None).isoformat()
            else:body[field]={'corrupted':True} if field=='outputs' else 'corrupted'
        return body
    def __call__(self,request):
        path=request.url.path
        self.request_paths.append((request.method,path))
        def result(body,status=200):return httpx.Response(status,json=body)
        if request.url.host=='api.vapi.ai':self.calls+=1;return result(self.call)
        if request.url.host=='generativelanguage.googleapis.com':
            self.judge_requests+=1
            package=json.loads(json.loads(request.content)['messages'][1]['content'].split('\n',1)[1])
            value=judgment(package)
            if self.bad_judge:value['hallucination_free']['evidence_refs']=['bad']
            return result({'model':'gemini-3.8-flash','choices':[{'finish_reason':'stop','message':{'content':canonical(value)}}]})
        if path=='/sessions':return result([{'id':self.project,'tenant_id':self.workspace,'name':'servicevoice-live-calls'}])
        if path=='/api/v1/runs/query':
            query=json.loads(request.content);self.trace_queries.append(query)
            assert request.method=='POST' and query['session']==[self.project] and query['limit']==100
            assert set(query)=={'session','trace','limit','select'}
            assert {'id','session_id','trace_id','start_time','end_time','outputs'}<=set(query['select'])
            rows=[]
            for rid,value in self.runs.items():
                if value.get('trace_id')==query['trace']:
                    body=self.read_run(rid)
                    if body is not None:rows.append(body)
            return result({'runs':rows,'cursors':{'next':None,'prev':None}})
        if path=='/api/v1/runs/batch':
            batch=json.loads(request.content);self.batch_requests.append(batch)
            assert request.method=='POST' and set(batch)=={'post','patch'}
            assert batch['patch']==[] and 1<=len(batch['post'])<=100
            if self.rate_limit_batch_once:
                self.rate_limit_batch_once=False
                return httpx.Response(429,json={},headers={'Retry-After':'120'} if getattr(self,'rate_limit_header',True) else {})
            for body in batch['post']:
                self.run_posts.append(body['id']);self.runs[body['id']]=body
            if self.fail_child_once:self.fail_child_once=False;return result({},503)
            return result({},202)
        if path=='/runs':
            body=json.loads(request.content);self.run_posts.append(body['id']);self.runs[body['id']]=body
            # An ambiguous acknowledgement after durable acceptance must retry
            # the same child identity, never create another model judgment.
            if body.get('parent_run_id') and self.fail_child_once:
                self.fail_child_once=False;return result({},503)
            return result({},202)
        if path.startswith('/runs/'):
            rid=path.rsplit('/',1)[1]
            if request.method=='PATCH' and rid in self.runs:self.runs[rid].update(json.loads(request.content))
            body=self.read_run(rid)
            return result(body or {},200 if body is not None else 404)
        if path=='/feedback':
            if request.method=='GET':
                self.feedback_queries.append(dict(request.url.params))
                assert request.url.params['limit']=='100'
                rows=[deepcopy(v)for v in self.feedback.values()if v['run_id']==request.url.params['run']]
                if rows and self.hide_feedback_readback_once:
                    self.hide_feedback_readback_once=False;rows=rows[1:]
                offset=int(request.url.params['offset'])
                return result(rows[offset:offset+100])
            if self.fail_feedback_once:self.fail_feedback_once=False;return result({},503)
            body=json.loads(request.content);assert body['extend_trace_retention'] is False
            self.feedback[body['id']]=body;return result(body)
        if path.startswith('/feedback/'):
            fid=path.rsplit('/',1)[1]
            if fid in self.feedback and self.hide_feedback_readback_once:
                self.hide_feedback_readback_once=False;return result({},404)
            return result(self.feedback.get(fid,{}),200 if fid in self.feedback else 404)
        if path=='/annotation-queues':
            if request.method=='GET':
                return result([q for q in self.queues.values() if q['name']==request.url.params.get('name')])
            body=json.loads(request.content);self.queue_posts.append(body)
            # LangSmith refuses a queue bound to a project without a reference dataset (400).
            assert body['rubric_items'] and body['rubric_instructions'] and 'session_ids' not in body
            self.queues[body['id']]={'id':body['id'],'name':body['name']};return result(self.queues[body['id']],201)
        if path.startswith('/annotation-queues/') and path.endswith('/queues'):
            return result([self.queues[q] for q in self.queued.get(path.split('/')[2],())])
        if path.startswith('/annotation-queues/') and path.endswith('/runs'):
            assert request.method=='POST'
            if self.fail_queue:return result({},503)
            for rid in json.loads(request.content):self.queued.setdefault(rid,set()).add(path.split('/')[2]);self.queue_adds.append(rid)
            return result({},200)
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


def test_accepted_child_temporarily_missing_retries_frozen_review_after_fifteen_seconds(scenario,tmp_path):
    record_export_diagnostics(scenario);worker,mock,now=worker_fixture(tmp_path,scenario)
    worker.outbox.discover(worker.config.source)
    with closing(worker.outbox.db()) as db,db:
        db.execute('UPDATE jobs SET attempts=5')
    mock.hide_child_readback_once=True
    assert worker.tick()
    with closing(worker.outbox.db()) as db:
        pending=dict(db.execute('SELECT * FROM jobs').fetchone())
        budget=db.execute('SELECT SUM(used) FROM budgets').fetchone()[0]
    assert pending['attempts']==6 and pending['failures']==1
    assert pending['next_attempt']==now[0]+15
    assert pending['last_error']=='component_span_readback_ingestion_pending'
    assert pending['report'] and mock.judge_requests==1 and budget==1
    assert not mock.feedback
    # Every readable child (one per caller turn plus the pending tool) has been
    # posted before the first delayed readback can interrupt export. No
    # one-child-per-retry serialization.
    children=[r for r in mock.runs.values() if r.get('parent_run_id')]
    assert len(children)==2 and sorted(r['name'][:7] for r in children)==['tool · ','turn 02']
    frozen=deepcopy(mock.runs)
    now[0]+=14;assert not worker.tick()
    now[0]+=1;assert worker.tick()
    assert worker.outbox.status()['jobs']=={'complete':1} and mock.judge_requests==1
    assert all(mock.runs[k]==value for k,value in frozen.items())
    assert len(mock.run_posts)==len(set(mock.run_posts))
    with closing(worker.outbox.db()) as db:
        assert db.execute('SELECT judge_attempts FROM jobs').fetchone()[0]==1
        assert db.execute('SELECT SUM(used) FROM budgets').fetchone()[0]==1


def test_all_feedback_is_created_before_a_temporarily_missing_first_readback(scenario,tmp_path):
    worker,mock,now=worker_fixture(tmp_path,scenario)
    mock.hide_feedback_readback_once=True
    assert worker.tick() and worker.outbox.status()['jobs']=={'pending':1}
    report=next(iter(mock.runs.values()))['outputs']
    expected=list(feedback_rows(str(uuid4()),report))
    assert len(mock.feedback)==len(expected)>6
    frozen=deepcopy(mock.feedback)
    assert mock.judge_requests==1
    now[0]+=15
    assert worker.tick() and worker.outbox.status()['jobs']=={'complete':1}
    assert mock.judge_requests==1 and mock.feedback==frozen


def test_hundreds_of_component_children_use_bounded_official_batches_and_two_trace_queries(scenario,tmp_path):
    from app.diagnostics import Diagnostics
    store,call=scenario;source=Path(store.path)
    sink=Diagnostics(source.with_name(source.stem+'-diagnostics.sqlite3'))
    start=datetime.fromisoformat(call['startedAt']).timestamp()
    for index in range(176):
        request=uuid4().hex
        assert sink.record('tts','tts.request_started','started',call_id=call['id'],request_id=request,start_unix=start+10+index*.01,duration_ms=0)
        assert sink.record('tts','tts.completed','success',call_id=call['id'],request_id=request,start_unix=start+10+index*.01,duration_ms=5,
            attributes={'cache_hit':index%2==0,'input_characters':12})
    assert sink.close()
    worker,mock,_=worker_fixture(tmp_path,scenario)
    assert worker.tick() and worker.outbox.status()['jobs']=={'complete':1}
    # One speech-synthesis group and 176 synthesis rows; the untimed caller turns stay in the root.
    assert [len(b['post'])for b in mock.batch_requests]==[100,77]
    assert all(b['patch']==[]for b in mock.batch_requests)
    assert len(mock.trace_queries)==2 and len(mock.feedback_queries)==2
    root=next(r for r in mock.runs.values()if r['name']=='voice.call.review')
    assert all(q['session']==[mock.project] and q['trace']==root['id']for q in mock.trace_queries)
    child_ids={r['id']for r in mock.runs.values()if r.get('parent_run_id')}
    assert len(child_ids)==177
    group=next(r for r in mock.runs.values() if r['name'].startswith('speech synthesis'))
    assert group['outputs']['cached_cue_clips']==88 and root['outputs']['trace_export']['tts']==176
    assert sum(1 for r in mock.runs.values() if r['name']=='tts · cached cue clip')==88
    assert not any(path=='/runs/'+child for _,path in mock.request_paths for child in child_ids)
    assert not any(path.startswith('/feedback/')for _,path in mock.request_paths)
    assert mock.judge_requests==1


def test_batch_rate_limit_honors_numeric_retry_after_without_another_judge_or_budget_attempt(scenario,tmp_path):
    record_export_diagnostics(scenario);worker,mock,now=worker_fixture(tmp_path,scenario)
    mock.rate_limit_batch_once=True;assert worker.tick()
    with closing(worker.outbox.db()) as db:
        pending=dict(db.execute('SELECT * FROM jobs').fetchone())
    assert pending['last_error']=='export_http_429' and pending['next_attempt']==now[0]+120
    assert pending['report'] and not mock.feedback and mock.judge_requests==1
    assert pending['failures']==0  # provider throttling is a wait, never a step towards dead-lettering
    now[0]+=119;assert not worker.tick()
    now[0]+=1;assert worker.tick()
    assert worker.outbox.status()['jobs']=={'complete':1} and mock.judge_requests==1
    assert len(mock.run_posts)==len(set(mock.run_posts))
    with closing(worker.outbox.db()) as db:
        assert db.execute('SELECT judge_attempts FROM jobs').fetchone()[0]==1
        assert db.execute('SELECT SUM(used) FROM budgets').fetchone()[0]==1


def test_job_dead_lettered_only_by_throttling_is_requeued_on_restart(scenario,tmp_path):
    worker,mock,now=worker_fixture(tmp_path,scenario);worker.outbox.discover(worker.config.source)
    call=mock.call['id']
    with closing(worker.outbox.db()) as db,db:
        db.execute("UPDATE jobs SET state='deadletter',failures=8,last_error='export_http_429' WHERE call_id=?",(call,))
    assert 'review_deadletters_require_operator_action' in worker.outbox.status()['alerts']
    restarted=Worker(worker.config,client=worker.client,clock=lambda:now[0])
    assert restarted.outbox.status()['jobs']=={'pending':1} and restarted.outbox.status()['alerts']==[]
    assert restarted.tick() and restarted.outbox.status()['jobs']=={'complete':1}
    # Any other dead-letter reason still needs an operator.
    with closing(restarted.outbox.db()) as db,db:
        db.execute("UPDATE jobs SET state='deadletter',failures=8,last_error='provider_call_identity_mismatch' WHERE call_id=?",(call,))
    again=Worker(worker.config,client=worker.client,clock=lambda:now[0])
    assert again.outbox.status()['jobs']=={'deadletter':1}
    # A readback mismatch stays with the operator whatever contract the stored
    # review belongs to; nothing is relabeled or republished on restart.
    with closing(again.outbox.db()) as db,db:
        report=json.loads(db.execute('SELECT report FROM jobs').fetchone()[0]);report['review_version']='live-call-review-v5'
        db.execute("UPDATE jobs SET report=?,last_error='run_readback_mismatch_or_ingestion_pending' WHERE call_id=?",(canonical(report),call))
    assert Worker(worker.config,client=worker.client,clock=lambda:now[0]).outbox.status()['jobs']=={'deadletter':1}
    # A 429-only dead letter of an earlier contract is requeued and published under
    # this contract with its frozen grades and no new model request.
    with closing(again.outbox.db()) as db,db:
        db.execute("UPDATE jobs SET last_error='export_http_429' WHERE call_id=?",(call,))
    republished=Worker(worker.config,client=worker.client,clock=lambda:now[0])
    assert republished.outbox.status()['jobs']=={'pending':1}
    mock.runs.clear();mock.feedback.clear()
    assert republished.tick() and republished.outbox.status()['jobs']=={'complete':1} and mock.judge_requests==1
    assert next(iter(mock.runs.values()))['outputs']['review_process']['semantic_grades_reused_from']=='live-call-review-v5'


def test_unexpected_scoped_forensic_children_are_not_counted_as_expected_component_evidence(scenario,tmp_path):
    record_export_diagnostics(scenario);worker,mock,now=worker_fixture(tmp_path,scenario)
    mock.fail_child_once=True;worker.tick()
    root=next(r for r in mock.runs.values()if r['name']=='voice.call.review')
    extra=deepcopy(next(r for r in mock.runs.values()if r.get('parent_run_id')))
    extra['id']=str(uuid4());extra['name']='human.forensic.annotation'
    mock.runs[extra['id']]=extra
    now[0]+=901;worker.tick()
    assert worker.outbox.status()['jobs']=={'complete':1} and mock.judge_requests==1
    assert root['outputs']['trace_export']['turns']==1 and root['outputs']['trace_export']['tools']==1
    assert root['outputs']['trace_export']['exported_runs']==2


def test_real_export_failures_back_off_by_failed_ticks_not_lifetime_poll_attempts(scenario,tmp_path):
    worker,mock,now=worker_fixture(tmp_path,scenario)
    worker.outbox.discover(worker.config.source)
    with closing(worker.outbox.db()) as db,db:db.execute('UPDATE jobs SET attempts=25')
    for delay in (30,60):
        mock.fail_feedback_once=True;assert worker.tick()
        with closing(worker.outbox.db()) as db:
            pending=dict(db.execute('SELECT * FROM jobs').fetchone())
        assert pending['next_attempt']==now[0]+delay
        now[0]+=delay
    assert worker.tick() and worker.outbox.status()['jobs']=={'complete':1}
    assert mock.judge_requests==1


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


def test_complete_matching_reference_can_pass_text_check_with_audio_coverage_still_unknown(scenario,tmp_path):
    store,call=scenario
    package=build_evidence(call,read_snapshot(store.path,call['id']))
    report=public_report(package,judgment(package),call,[],configuration(tmp_path,store.path))
    assert report['checks']['reference_consistency']['verdict']=='pass'
    assert report['checks']['booking_accuracy']['verdict']=='pass'
    assert report['needs_review'] is False
    assert report['evidence_coverage']['spoken_audio_verified'] is None
    feedback={r['key']:r for r in feedback_rows(str(uuid4()),report)}
    assert 'spoken_audio_verified' not in feedback and feedback['reference_consistency']['score']==1
    assert 'never hears audio' in report['1 Summary']['what_to_check'][0]


@pytest.mark.parametrize('spoken',['Your reference is C. SV. 3F2DFB5.','Your appointment is confirmed.'])
def test_returned_reference_without_complete_assistant_literal_requires_text_review_not_audio_failure(scenario,tmp_path,spoken):
    store,call=scenario
    call['artifact']['messages'][-1]['message']=spoken
    package=build_evidence(call,read_snapshot(store.path,call['id']))
    report=public_report(package,judgment(package),call,[],configuration(tmp_path,store.path))
    assert package['mechanical_checks']['reference_consistency']['verdict']=='uncertain'
    assert package['mechanical_checks']['reference_consistency']['reason_code']=='returned_reference_missing_complete_literal_requires_review'
    assert report['checks']['booking_accuracy']['verdict']=='uncertain'
    assert report['checks']['booking_integrity']['verdict']=='pass'
    assert report['needs_review'] is True
    assert report['evidence_coverage']['spoken_audio_verified'] is None


def test_no_booking_reference_obligation_is_not_counted_as_reference_success(scenario):
    store,call=scenario
    call['artifact']['messages']=[{'role':'user','message':'What are your hours?'},
        {'role':'bot','message':'Monday through Friday, eight to five Central.'}]
    package=build_evidence(call,read_snapshot(store.path,call['id']))
    assert package['mechanical_checks']['reference_consistency']['verdict']=='not_applicable'


def test_reference_uncertainty_does_not_replace_an_existing_semantic_failure(scenario,tmp_path):
    store,call=scenario;call['artifact']['messages'][-1]['message']='Confirmed.'
    package=build_evidence(call,read_snapshot(store.path,call['id']))
    report=public_report(package,judgment(package,'fail'),call,[],configuration(tmp_path,store.path))
    assert report['checks']['booking_accuracy']['verdict']=='fail'


@pytest.mark.parametrize('reason,expected',[
    ('assistant-ended-call',True),('assistant-said-end-call-phrase',True),
    ('customer-ended-call',False),('silence-timed-out',None),('new-unrecognized-reason',None),(None,None),
    ([],None),({},None),
])
def test_provider_termination_is_separate_coverage_and_never_a_goodbye_quality_grade(scenario,tmp_path,reason,expected):
    store,call=scenario;call['endedReason']=reason
    package=build_evidence(call,read_snapshot(store.path,call['id']))
    report=public_report(package,judgment(package),call,[],configuration(tmp_path,store.path))
    assert report['evidence_coverage']['provider_ended_by_assistant'] is expected
    assert report['needs_review'] is False
    assert 'provider_ended_by_assistant' not in report['checks'] and 'assistant_hung_up' not in report['checks']
    feedback={r['key']:r for r in feedback_rows(str(uuid4()),report)}
    assert feedback['assistant_hung_up']['score']==(int(expected) if expected is not None else None)
    assert 'goodbye' in feedback['assistant_hung_up']['comment']


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
    assert report['checks']['first_response_within_target']['verdict']=='uncertain'
    assert report['checks']['first_response_within_target']['reason_code']=='no_measured_response_turns'
    assert report['checks']['answer_within_target']['verdict']=='uncertain'
    assert report['slow_call'] is False and report['needs_review'] is False


def test_zero_summary_cannot_hide_positive_measured_turns():
    call={'artifact':{'performanceMetrics':{'turnLatencyAverage':0,'turnLatencies':[{'turnLatency':3000},{'turnLatency':5000}]}}}
    assert latency(call)['turn_avg_ms']==4000


@pytest.mark.parametrize('previous_version',['live-call-review-v1','live-call-review-v2','live-call-review-v3'])
def test_new_review_contract_rejudges_older_cached_review_without_relabeling_it(scenario,tmp_path,previous_version):
    worker,mock,now=worker_fixture(tmp_path,scenario);worker.tick()
    with closing(worker.outbox.db()) as db,db:
        cached=json.loads(db.execute('SELECT report FROM jobs').fetchone()[0]);cached['review_version']=previous_version
        if previous_version!='live-call-review-v3':
            cached['checks'].pop('task_completion');cached['checks'].pop('conversation_quality')
        cached.pop('evidence_coverage',None)
        # Only a review still pending at upgrade time is assessed again; a completed one stays frozen.
        db.execute("UPDATE jobs SET report=?,state='pending',lease=0,next_attempt=0,completed=NULL",(canonical(cached),))
    mock.runs.clear();mock.feedback.clear()
    now[0]+=901
    restarted=Worker(worker.config,client=worker.client,clock=lambda:now[0]);restarted.tick()
    assert mock.judge_requests==2 and restarted.outbox.status()['jobs']=={'complete':1}
    report=next(iter(mock.runs.values()))['outputs']
    assert report['review_version']==VERSION and report['supersedes_review_version']==previous_version
    assert {'task_completion','conversation_quality'}<=set(report['checks'])
    assert report['evidence_coverage']['spoken_audio_verified'] is None
    with closing(restarted.outbox.db()) as db:
        assert db.execute('SELECT judge_attempts FROM jobs').fetchone()[0]==2
        assert db.execute('SELECT SUM(used) FROM budgets').fetchone()[0]==2


@pytest.mark.parametrize('previous_version',['live-call-review-v2','live-call-review-v3'])
def test_old_review_with_exhausted_attempts_cannot_manufacture_new_dimension_passes(scenario,tmp_path,previous_version):
    worker,mock,now=worker_fixture(tmp_path,scenario);worker.tick()
    with closing(worker.outbox.db()) as db,db:
        cached=json.loads(db.execute('SELECT report FROM jobs').fetchone()[0]);cached['review_version']=previous_version
        db.execute("UPDATE jobs SET report=?,judge_attempts=3,state='pending',lease=0,next_attempt=0,completed=NULL",(canonical(cached),))
    mock.runs.clear();mock.feedback.clear();now[0]+=901
    restarted=Worker(worker.config,client=worker.client,clock=lambda:now[0]);restarted.tick()
    report=next(iter(mock.runs.values()))['outputs']
    assert mock.judge_requests==1
    assert all(report['checks'][d]['verdict']=='uncertain' for d in DIMENSIONS)
    assert report['review_process']['semantic_status']=='judge_attempt_limit_exhausted'


def test_budget_exhaustion_reason_and_true_coverage_denominators_are_visible(scenario,tmp_path):
    worker,mock,_=worker_fixture(tmp_path,scenario);worker.config=replace(worker.config,monthly_limit=1)
    worker.outbox.discover(worker.config.source);worker.outbox.reserve_judge(mock.call['id'],1)
    worker.tick()
    report=next(iter(mock.runs.values()))['outputs'];status=worker.outbox.status(1)
    assert report['review_process']['semantic_status']=='monthly_judge_budget_exhausted'
    assert status['coverage']['known_ended_calls']==1
    assert status['coverage']['completed_reviews']==1
    assert status['coverage']['semantically_assessed_calls']==0
    assert status['coverage']['completed_with_unknown_semantics']==1
    assert status['coverage']['completed_review_fraction']==1
    assert status['coverage']['semantic_assessment_fraction']==0
    # A spent allowance is an operator warning, not an unhealthy worker: reviews
    # continue with explicit unknown semantics.
    assert 'monthly_judge_budget_exhausted' in status['warnings'] and status['alerts']==[]


def test_successful_review_records_lag_and_does_not_claim_provider_audio_visibility(scenario,tmp_path):
    worker,mock,_=worker_fixture(tmp_path,scenario);worker.tick()
    status=worker.outbox.status(200);report=next(iter(mock.runs.values()))['outputs']
    assert status['coverage']['semantically_assessed_calls']==1
    assert status['coverage']['fully_scored_calls']==1
    assert status['average_review_lag_seconds']>=0
    assert status['max_review_lag_seconds']>=status['average_review_lag_seconds']
    assert report['diagnostics']['coverage']['complete'] is False
    assert report['diagnostics']['configuration']['provider_effective_configuration']=='unknown'


def test_worker_exports_only_call_scoped_diagnostic_breadcrumbs_and_keeps_unknown_audio(scenario,tmp_path):
    from app.diagnostics import Diagnostics, operation_identifier, turn_identifier
    store,call=scenario
    worker,mock,_=worker_fixture(tmp_path,scenario)
    source=Path(store.path);path=source.with_name(source.stem+'-diagnostics.sqlite3')
    turn=turn_identifier(call['id'],2);operation=operation_identifier('session','confirm')
    sink=Diagnostics(path,{'SERVICEVOICE_ENVIRONMENT':'production'})
    assert sink.record('tool_harness','execution_finished','success',call_id=call['id'],turn_id=turn,
        operation_id=operation,duration_ms=12,attributes={'receipt_present':True,'tool_name':'confirm_action',
            'transcript':'PRIVATE CUSTOMER 5125550142','exception':'PRIVATE API KEY'})
    assert sink.record('gateway','request_finished','failure',call_id=str(uuid4()),duration_ms=50)
    assert sink.close()
    assert worker.tick()
    with closing(worker.outbox.db()) as db:
        report=json.loads(db.execute('SELECT report FROM jobs').fetchone()[0])
    snapshot=report['diagnostics']
    assert len(snapshot['events'])==1
    assert snapshot['events'][0]['turn_id']==turn and snapshot['events'][0]['operation_id']==operation
    assert snapshot['events'][0]['config_id']==snapshot['configuration']['id']
    assert snapshot['configuration']['provenance']=='local_requested_settings_and_owned_source'
    assert snapshot['configuration']['provider_effective_configuration']=='unknown'
    assert snapshot['components']['components']['tool_harness']['evidence_status']=='observed'
    assert snapshot['components']['components']['caller_device']['evidence_status']=='unavailable'
    assert snapshot['coverage']['complete'] is False and snapshot['components']['complete'] is False
    for private in (call['id'],'PRIVATE CUSTOMER','5125550142','PRIVATE API KEY'):
        assert private not in canonical(snapshot)


def record_export_diagnostics(scenario):
    from app.diagnostics import Diagnostics, operation_identifier, turn_identifier
    store,call=scenario;source=Path(store.path)
    path=source.with_name(source.stem+'-diagnostics.sqlite3')
    session=read_snapshot(store.path,call['id'])['session']['id']
    turn=turn_identifier(call['id'],2);operation=operation_identifier(session,'confirm')
    request=str(uuid4());started=datetime.fromisoformat(call['startedAt']).timestamp()
    sink=Diagnostics(path,{'SERVICEVOICE_ENVIRONMENT':'production'})
    assert sink.record('gateway','request_finished','success',call_id=call['id'],turn_id=turn,request_id=request,
        start_unix=started+10,duration_ms=20,attributes={'http_status':200,'transcript':'PRIVATE SOURCE'})
    assert sink.record('tool_harness','execution_finished','pending',call_id=call['id'],turn_id=turn,
        operation_id=operation,start_unix=started+11,duration_ms=50,attributes={'receipt_present':False})
    # An observation after the call ends is a point event, not a fabricated
    # duration of the scheduler write. Explicit same-operation evidence joins it.
    ended=datetime.fromisoformat(call['endedAt']).timestamp()
    assert sink.record('scheduler','receipt_observed','success',call_id=call['id'],turn_id=turn,
        operation_id=operation,start_unix=ended+1,duration_ms=0,
        attributes={'receipt_present':True,'evidence_source':'tool_receipt'})
    # A breadcrumb without measured duration stays in the root report, with
    # skipped timing coverage rather than an invented child interval.
    assert sink.record('voice_runtime','call_ended','success',call_id=call['id'],start_unix=ended)
    assert sink.close()
    return path


def test_real_diagnostic_ledger_exports_verified_children_and_late_receipt_without_private_text(scenario,tmp_path):
    record_export_diagnostics(scenario);worker,mock,_=worker_fixture(tmp_path,scenario)
    assert worker.tick() and worker.outbox.status()['jobs']=={'complete':1}
    root=next(r for r in mock.runs.values() if r['name']=='voice.call.review')
    children=[r for r in mock.runs.values() if r.get('parent_run_id')]
    turns=[r for r in children if r['extra']['metadata']['record_kind']=='turn']
    # The scenario artifact carries no speech timings, so only the turn with a
    # timed tool event gets a row; the first turn stays in the root timeline.
    assert len(turns)==1 and len(children)==2
    assert root['outputs']['trace_export']['turns']==1 and root['outputs']['trace_export']['tools']==1
    assert root['outputs']['trace_export']['omitted']==1 and len(root['outputs']['3 Turn-by-turn timeline']['turns'])==2
    # The pending confirm tool joins its turn through the explicit operation id;
    # the late scheduler receipt marks it as received, never as a fabricated duration.
    tool=next(r for r in children if r['extra']['metadata']['record_kind']=='tool')
    second=next(r for r in turns if r['extra']['metadata']['turn']==2)
    assert tool['parent_run_id']==second['id'] and tool['name'].startswith('tool · confirm_action (ok, 0.05 s)')
    assert tool['outputs']['receipt_present'] is True and tool['run_type']=='tool'
    assert tool['outputs']['ok'] is True
    assert all(r['trace_id']==root['id'] for r in children)
    assert all(r['dotted_order'].startswith(root['dotted_order']+'.') for r in children)
    assert 'PRIVATE SOURCE' not in canonical(children) and 'PRIVATE SOURCE' not in canonical(root)
    assert scenario[1]['id'] not in canonical(children)
    assert mock.judge_requests==1
    # Every recorded non-webhook event is still listed in the root review.
    kept={(e['component'],e['event']) for e in root['outputs']['diagnostics']['events']}
    assert kept=={('gateway','request_finished'),('tool_harness','execution_finished'),('scheduler','receipt_observed'),('voice_runtime','call_ended')}
    assert root['outputs']['diagnostics']['webhook_deliveries']['deliveries']==0


def test_ambiguous_child_upload_retries_same_frozen_ids_without_another_judge_or_new_ledger_events(scenario,tmp_path):
    from app.diagnostics import Diagnostics
    path=record_export_diagnostics(scenario);worker,mock,now=worker_fixture(tmp_path,scenario)
    mock.fail_child_once=True;worker.tick()
    assert worker.outbox.status()['jobs']=={'pending':1} and mock.judge_requests==1 and not mock.feedback
    frozen=deepcopy(mock.runs)
    sink=Diagnostics(path)
    assert sink.record('tts','tts.failed','failure',call_id=scenario[1]['id'],duration_ms=1,
        attributes={'error_code':'tts_upstream_rate_limited'})
    assert sink.close()
    now[0]+=901;worker.tick()
    assert worker.outbox.status()['jobs']=={'complete':1} and mock.judge_requests==1
    assert len(mock.run_posts)==len(set(mock.run_posts))==len(mock.runs)
    assert all(mock.runs[k]==value for k,value in frozen.items())
    assert not any(r['name'].endswith('tts.failed') for r in mock.runs.values())
    assert len(mock.feedback)>0


@pytest.mark.parametrize('field',['outputs','start_time','end_time','run_type','error','non_object','vendor_shifted_microsecond'])
def test_corrupted_component_readback_blocks_completion_and_feedback_until_reconciled(scenario,tmp_path,field):
    record_export_diagnostics(scenario);worker,mock,now=worker_fixture(tmp_path,scenario)
    mock.corrupt_child_field=field
    assert worker.tick()
    assert worker.outbox.status()['jobs']=={'pending':1}
    assert 'component_span_readback_mismatch_or_ingestion_pending' in worker.outbox.status()['pending_reasons']
    assert mock.judge_requests==1 and not mock.feedback
    mock.corrupt_child_field=None;now[0]+=901;worker.tick()
    assert worker.outbox.status()['jobs']=={'complete':1} and mock.judge_requests==1


@pytest.mark.parametrize('serialization',['equivalent_timezone','vendor_utc_naive'])
def test_equivalent_component_timestamps_reconcile_across_iso_serializations(scenario,tmp_path,serialization):
    record_export_diagnostics(scenario);worker,mock,_=worker_fixture(tmp_path,scenario)
    mock.corrupt_child_field=serialization;worker.tick()
    assert worker.outbox.status()['jobs']=={'complete':1}


@pytest.mark.parametrize('actual',[
    '2026-10-20T20:25:52.453571Z',
    '2026-10-20T20:25:52.453571+00:00',
    '2026-10-20T20:25:52.453571',
    '2026-10-20T15:25:52.453571-05:00',
])
def test_official_readback_timestamp_serializations_preserve_the_exact_instant(actual):
    assert LangSmith.same_time(actual,'2026-10-20T20:25:52.453571+00:00')


@pytest.mark.parametrize('actual,expected',[
    ('2026-10-20T20:25:52.453572','2026-10-20T20:25:52.453571+00:00'),
    ('2026-10-20T20:25:52.453571','2026-10-20T20:25:52.453571'),
    ('2026-10-20T20:25:52.453571Z','2026-10-20T20:25:52.453571'),
    ('2026-10-20T20:25:52.453571-05:00','2026-10-20T20:25:52.453571+00:00'),
    ('2026-10-20','2026-10-20T00:00:00+00:00'),
    ('2026-10-20T20:25:52.4535710','2026-10-20T20:25:52.453571+00:00'),
    ('2026-02-30T20:25:52.453571','2026-10-20T20:25:52.453571+00:00'),
    ('2026-10-20T20:25:52.453571+00:99','2026-10-20T18:46:52.453571+00:00'),
    (None,'2026-10-20T20:25:52.453571+00:00'),
])
def test_timestamp_readback_never_invents_expected_timezone_or_rounds_a_real_shift(actual,expected):
    assert not LangSmith.same_time(actual,expected)


def test_authoritative_uncertainty_is_not_counted_as_a_fully_scored_semantic_review(scenario,tmp_path):
    store,call=scenario
    with store.connect() as db:db.execute('DELETE FROM gemini_tool_signatures')
    worker,mock,_=worker_fixture(tmp_path,scenario);worker.tick()
    root=next(r for r in mock.runs.values() if r['name']=='voice.call.review')
    assert root['outputs']['checks']['confirmation_compliance']['verdict']=='uncertain'
    assert root['outputs']['review_process']['semantic_status']=='assessed_with_uncertainty'
    coverage=worker.outbox.status()['coverage']
    assert coverage['semantically_assessed_calls']==1 and coverage['fully_scored_calls']==0
    assert coverage['completed_with_unknown_semantics']==1


def test_persistent_dependency_failure_deadletters_and_requeue_preserves_judge_limits(scenario,tmp_path):
    worker,mock,now=worker_fixture(tmp_path,scenario)
    worker.config=replace(worker.config,max_failures=2)
    original=worker.client
    worker.client=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(503,json={})))
    for _ in range(2):worker.tick();now[0]+=901
    status=worker.outbox.status(200)
    assert status['jobs']=={'deadletter':1} and status['coverage']['deadletter_calls']==1
    assert 'review_deadletters_require_operator_action' in status['alerts']
    assert not worker.tick()
    assert worker.outbox.requeue_deadletter(mock.call['id'])
    worker.client.close();worker.client=original
    worker.tick()
    assert worker.outbox.status()['jobs']=={'complete':1} and mock.judge_requests==1


def test_active_call_waiting_does_not_consume_persistent_failure_budget(scenario,tmp_path):
    worker,mock,now=worker_fixture(tmp_path,scenario);mock.call['status']='in-progress'
    worker.config=replace(worker.config,max_failures=1)
    for _ in range(5):worker.tick();now[0]+=901
    status=worker.outbox.status(200)
    assert status['jobs']=={'pending':1} and status['coverage']['known_active_calls']==1
    assert status['coverage']['known_ended_calls']==0
    assert status['oldest_pending_review_age_seconds'] is None
    with closing(worker.outbox.db()) as db:assert db.execute('SELECT failures FROM jobs').fetchone()[0]==0


def test_oldest_pending_ended_call_and_unavailable_identity_are_separate(scenario,tmp_path):
    worker,mock,now=worker_fixture(tmp_path,scenario);worker.outbox.discover(worker.config.source)
    worker.outbox.mark_eligibility(mock.call['id'],True,now[0]-700)
    missing=str(uuid4());worker.outbox.discover_provider_calls([(missing,now[0]-900)])
    worker.outbox.unavailable(missing)
    status=worker.outbox.status(200)
    assert status['oldest_pending_review_age_seconds']==700
    assert status['coverage']['discovered_calls']==2
    assert status['coverage']['known_ended_calls']==1
    assert status['coverage']['eligibility_unknown_calls']==1
    assert status['coverage']['provider_records_unavailable']==1
    assert 'review_backlog_older_than_ten_minutes' in status['alerts']


def test_legacy_four_dimension_judgment_cannot_pass_new_rubric(scenario):
    package=build_evidence(scenario[1],read_snapshot(scenario[0].path,scenario[1]['id']))
    value=judgment(package);value.pop('task_completion');value.pop('conversation_quality')
    with pytest.raises(EvidenceError,match='judge_invalid_dimensions'):parse_judgment(value,package)


def test_rate_limit_without_retry_after_is_a_wait_that_pauses_every_claim(scenario,tmp_path):
    record_export_diagnostics(scenario);worker,mock,now=worker_fixture(tmp_path,scenario)
    mock.rate_limit_batch_once=True;mock.rate_limit_header=False;assert worker.tick()
    with closing(worker.outbox.db()) as db:
        pending=dict(db.execute('SELECT * FROM jobs').fetchone())
    assert pending['state']=='pending' and pending['last_error']=='export_http_429' and pending['failures']==0
    assert pending['next_attempt']==now[0]+60 and mock.judge_requests==1
    status=worker.outbox.status(200)
    assert status['warnings']==['langsmith_rate_limited'] and status['alerts']==[] and status['rate_limit_wait_seconds']==60
    # Another due job is not claimed while the wait lasts: the queue stops asking.
    with closing(worker.outbox.db()) as db,db:
        db.execute("INSERT INTO jobs(call_id,session_id,created,next_attempt,eligible_ended,ended) VALUES (?,'',?,0,1,?)",(str(uuid4()),now[0],now[0]-100))
    assert worker.outbox.claim() is None
    now[0]+=60
    assert worker.outbox.claim() is not None and worker.outbox.status(200)['warnings']==[]


def test_review_older_than_the_langsmith_window_is_dead_lettered_at_once(scenario,tmp_path):
    worker,mock,now=worker_fixture(tmp_path,scenario)
    now[0]+=24*3600  # the call started a day ago; LangSmith would refuse its run
    assert worker.tick()
    with closing(worker.outbox.db()) as db:
        job=dict(db.execute('SELECT * FROM jobs').fetchone())
    assert job['state']=='deadletter' and job['last_error']=='langsmith_run_window_expired'
    assert mock.judge_requests==0 and not mock.runs and not mock.feedback
    assert 'review_deadletters_require_operator_action' in worker.outbox.status(200)['alerts']
    with closing(worker.outbox.db()) as db:
        assert db.execute('SELECT SUM(used) FROM budgets').fetchone()[0] is None


def test_main_writes_a_heartbeat_before_the_first_review(tmp_path,monkeypatch):
    events=[]
    class FakeWorker:
        def __init__(self,config):self.config=config;self.client=httpx.Client()
        def tick(self):events.append('tick');return False
        def heartbeat(self,error=None):events.append(('heartbeat',error))
    config=configuration(tmp_path,tmp_path/'source.sqlite3')
    monkeypatch.setattr(live_evaluations.Config,'from_env',classmethod(lambda cls,env=None:config))
    monkeypatch.setattr(live_evaluations,'Worker',FakeWorker)
    monkeypatch.setattr(live_evaluations.os,'umask',lambda _:0o077)
    monkeypatch.setattr(sys,'argv',['reviewer','--once'])
    assert live_evaluations.main()==0
    assert events==[('heartbeat',None),'tick',('heartbeat',None)]
