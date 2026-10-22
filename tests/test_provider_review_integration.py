"""Automatic provider evidence export, cache stability and immutable readback."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import hashlib
import json
from uuid import uuid4

import httpx
import pytest

from app import live_evaluations as module
from app.generation_observability import project_provider_generations
from app.live_eval_evidence import build_evidence, canonical, read_snapshot
from app.live_evaluations import Config, Worker, feedback_rows, public_report
from test_live_evaluations import scenario, configuration, judgment, ProviderMock

HOST='00000000000000000000000000000000.r2.cloudflarestorage.com'


def capture(call):
    cid=call['id'];span=str(uuid4());at=int(datetime.fromisoformat(call['startedAt']).timestamp()*1000)
    raw=[{'time':at+offset,'body':name,'attributes':{'callId':cid,'traceId':cid,'spanId':span,'turn':1,
         'headers':{'Authorization':'PRIVATE ADMIN KEY'},'message':'PRIVATE CUSTOMER TEXT'}}
         for offset,name in [(0,'LLM request started'),(50,'LLM first token received'),
             (100,'LLM response completed'),(110,'LLM request aborted before completion'),
             (120,'Bot started speaking'),(130,'Bot stopped speaking')]]
    return {'capture_revision':'provider-storage-capture-v1','status':'observed','reason_code':'provider_log_projected',
        'network_requests':1,'http_status':200,'storage_profile':'vapi_r2_calllogs_v1','artifact_field':'presignedLogUrl',
        'call_key':hashlib.sha256(cid.encode()).hexdigest(),'wire_bytes':100,'expanded_bytes':1000,
        'source_rows':len(raw),'wire_sha256':'a'*64,'projection':project_provider_generations(raw,cid),
        'replace_existing_report':False,'private_url':'https://secret.invalid/?signature=PRIVATE QUERY'}


def worker(tmp_path,scenario):
    store,call=scenario
    c=replace(configuration(tmp_path,store.path),organization=call['orgId'],provider_log_hosts=(HOST,))
    mock=ProviderMock(call,c.workspace);clock=[__import__('time').time()]
    instance=Worker(c,client=httpx.Client(transport=httpx.MockTransport(mock)),clock=lambda:clock[0])
    return instance,mock,clock


def test_actual_worker_exports_provider_events_and_model_interval_without_private_text_or_false_abort_failure(scenario,tmp_path,monkeypatch):
    instance,mock,_=worker(tmp_path,scenario);calls=[];result=capture(scenario[1])
    def observe(call,**kwargs):
        calls.append(kwargs);assert kwargs['expected_call_id']==call['id']
        assert kwargs['expected_org_id']==call['orgId'] and kwargs['trusted_hosts']==(HOST,)
        return deepcopy(result)
    monkeypatch.setattr(module,'capture_provider_log_sync',observe)
    assert instance.tick() and instance.outbox.status()['jobs']=={'complete':1}
    root=next(r for r in mock.runs.values() if r['name']=='voice.call.review')
    children=[r for r in mock.runs.values() if r.get('parent_run_id') and r['extra']['metadata']['record_kind'] in ('provider_model','group') and 'vapi' in r['name']]
    assert len(calls)==1 and mock.judge_requests==1 and len(children)==2
    group=next(r for r in children if r['extra']['metadata']['record_kind']=='group')
    interval=next(r for r in children if r['extra']['metadata']['record_kind']=='provider_model')
    assert interval['parent_run_id']==group['id'] and group['parent_run_id']==root['id']
    assert interval['outputs']['classification']=='completed_then_cleanup_abort_label' and interval['error'] is None
    assert interval['name'].startswith('vapi llm · completed (cleanup abort label afterwards)')
    assert interval['outputs']['gateway_generation_id'] is None
    assert group['outputs']['event_counts']['provider.model_started']==1 and len(group['outputs']['timeline'])==6
    assert root['outputs']['trace_export']['provider_model_spans']==1
    exported=root['outputs']['provider_observations']
    assert exported['status']=='observed' and 'events' not in exported['projection'] and exported['projection']['summary']['event_counts']['provider.model_started']==1
    assert 'PRIVATE' not in canonical(mock.runs) and 'secret.invalid' not in canonical(mock.runs)
    feedback={r['key']:r for r in mock.feedback.values()}
    assert feedback['coverage_provider_log']['score']==1


def test_cached_export_retry_and_completed_restart_never_refetch_or_rejudge(scenario,tmp_path,monkeypatch):
    instance,mock,clock=worker(tmp_path,scenario);seen=[];result=capture(scenario[1])
    monkeypatch.setattr(module,'capture_provider_log_sync',lambda *a,**k:seen.append(True) or deepcopy(result))
    mock.fail_feedback_once=True;assert instance.tick()
    frozen=deepcopy(mock.runs)
    monkeypatch.setattr(module,'capture_provider_log_sync',lambda *a,**k:pytest.fail('Frozen review refetched logs'))
    clock[0]+=901;assert instance.tick()
    assert mock.runs==frozen and len(seen)==1 and mock.judge_requests==1
    assert instance.outbox.status()['jobs']=={'complete':1}
    assert not instance.tick()


def test_old_frozen_v5_report_never_gets_new_provider_fields_or_fetch(scenario,tmp_path,monkeypatch):
    instance,mock,clock=worker(tmp_path,scenario);store,call=scenario
    package=build_evidence(call,read_snapshot(store.path,call['id']))
    old=public_report(package,judgment(package),call,[],instance.config)
    assert 'provider_observations' not in old
    instance.outbox.discover(store.path);assert instance.outbox.claim()
    instance.outbox.save_report(call['id'],old);instance.outbox.retry(call['id'],'authored_export_retry',0)
    monkeypatch.setattr(module,'capture_provider_log_sync',lambda *a,**k:pytest.fail('Legacy frozen review refetched'))
    assert instance.tick() and instance.outbox.status()['jobs']=={'complete':1}
    root=next(r for r in mock.runs.values() if r['name']=='voice.call.review')
    assert 'provider_observations' not in root['outputs'] and 'provider_observation_export' not in root['outputs']
    assert mock.judge_requests==0


@pytest.mark.parametrize('state,reason', [('disabled','provider_log_hosts_unconfigured'),('unavailable','provider_log_network_unavailable')])
def test_missing_provider_log_is_unknown_coverage_and_does_not_change_booking_grade(scenario,tmp_path,monkeypatch,state,reason):
    instance,mock,_=worker(tmp_path,scenario)
    result={'capture_revision':'provider-storage-capture-v1','status':state,'reason_code':reason,
        'network_requests':0,'http_status':None,'projection':None,'replace_existing_report':False}
    monkeypatch.setattr(module,'capture_provider_log_sync',lambda *a,**k:result)
    assert instance.tick() and instance.outbox.status()['jobs']=={'complete':1}
    root=next(r for r in mock.runs.values() if r['name']=='voice.call.review')
    assert root['outputs']['checks']['booking_integrity']['verdict']=='pass'
    assert root['outputs']['provider_observations']['status']==state
    assert root['outputs']['trace_export']['provider_model_spans']==0
    feedback={r['key']:r for r in mock.feedback.values()}
    assert feedback['coverage_provider_log']['score'] is None


@pytest.mark.parametrize('field',['tags','extra'])
def test_provider_child_label_or_metadata_mismatch_prevents_completed_export(scenario,tmp_path,monkeypatch,field):
    instance,mock,clock=worker(tmp_path,scenario);result=capture(scenario[1])
    monkeypatch.setattr(module,'capture_provider_log_sync',lambda *a,**k:deepcopy(result))
    mock.corrupt_child_field=field;assert instance.tick()
    assert instance.outbox.status()['jobs']=={'pending':1} and mock.judge_requests==1
    mock.corrupt_child_field=None;clock[0]+=901;assert instance.tick()
    assert instance.outbox.status()['jobs']=={'complete':1} and mock.judge_requests==1


def test_provider_allowlist_is_explicit_and_requires_bound_organization(tmp_path):
    c=configuration(tmp_path,tmp_path/'source.sqlite3')
    assert c.provider_log_hosts==()
    with pytest.raises(ValueError):replace(c,provider_log_hosts=(HOST,)).validate()
    for hosts in [('attacker.example',),(HOST,HOST),('http://'+HOST,),('',)]:
        with pytest.raises(ValueError):replace(c,organization=str(uuid4()),provider_log_hosts=hosts).validate()


def test_cached_capture_unknown_fields_cannot_reach_root_export(scenario,tmp_path,monkeypatch):
    instance,mock,_=worker(tmp_path,scenario);store,call=scenario
    package=build_evidence(call,read_snapshot(store.path,call['id']))
    report=public_report(package,judgment(package),call,[],instance.config,provider_capture=capture(call))
    report['provider_observations']['private_url']='https://secret.invalid/PRIVATE'
    report['provider_observations']['projection']['raw_headers']='PRIVATE KEY'
    instance.outbox.discover(store.path);assert instance.outbox.claim()
    instance.outbox.save_report(call['id'],report);instance.outbox.retry(call['id'],'authored_cached_boundary',0)
    monkeypatch.setattr(module,'capture_provider_log_sync',lambda *a,**k:pytest.fail('Cached review refetched'))
    assert instance.tick() and instance.outbox.status()['jobs']=={'complete':1}
    assert 'PRIVATE' not in canonical(mock.runs) and 'secret.invalid' not in canonical(mock.runs)
    assert mock.judge_requests==0
