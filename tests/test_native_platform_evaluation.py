"""Scoped native evidence is not a scheduler receipt or audio verification."""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from uuid import uuid4

import pytest

from app.gemini_gateway import SignatureStore, GatewayLimits
from app.issuance_context import user_context
from app.live_eval_evidence import DIMENSIONS, VERSION, build_evidence, canonical, read_snapshot
from app.live_evaluations import Worker, feedback_rows, public_report
from app.tools import DEFINITIONS
from evals.live_controls import native_platform_controls
from test_live_evaluations import scenario, configuration, judgment, worker_fixture


def native_call(scenario,*,signed=True,provider_result=True):
    store,call=scenario
    messages=call['artifact']['messages']
    messages.append({'role':'user','message':'That is all I need. Goodbye.'})
    tool={'id':'native_end','type':'function','function':{'name':'endCall','arguments':'{}'}}
    if signed:
        signatures=SignatureStore(store.path,GatewayLimits())
        signatures.save_group(read_snapshot(store.path,call['id'])['session']['id'],call['id'],[tool],
            context=user_context([{'role':'user','content':m['message']}for m in messages if m.get('role')=='user']),allow_end_call=True)
    messages.append({'role':'tool_calls','toolCalls':[tool]})
    if provider_result:
        messages.append({'role':'tool_call_result','name':'endCall','toolCallId':tool['id'],
            'result':'{"ended":true,"note":"PRIVATE PLATFORM RESULT MUST NOT BE EXPORTED"}'})
    call['endedReason']='assistant-ended-call'
    return tool


def test_signed_empty_native_attempt_has_unknown_platform_outcome_not_a_business_receipt(scenario,tmp_path):
    native_call(scenario);store,call=scenario
    package=build_evidence(call,read_snapshot(store.path,call['id']))
    entry=next(v for v in package['evidence'].values()if isinstance(v,dict) and v.get('name')=='endCall')
    assert entry['evidence_scope']=='platform_attempt' and 'server_result' not in entry
    assert entry['platform_evidence']['outcome']=='unknown'
    assert entry['platform_evidence']['authoritative_scheduler_receipt'] is False
    assert entry['platform_evidence']['provider_results'][0]['provenance']=='untrusted_provider_result'
    assert entry['platform_evidence']['provider_ended_by_assistant'] is True
    assert package['mechanical_checks']['evidence_complete']['verdict']=='pass'
    assert not package['evidence_issues'] and 'endCall' not in DEFINITIONS and len(DEFINITIONS)==14
    report=public_report(package,judgment(package),call,[],configuration(tmp_path,store.path))
    public_native=next(v for v in report['evidence_index'].values()if v.get('tool')=='endCall')
    assert public_native=={'tool':'endCall','sequence':len(call['artifact']['messages'])-2,
        'evidence_scope':'platform_attempt','issuance_verified':True,'platform_outcome':'unknown','scheduler_receipt':False}
    assert report['booking']['appointment_count']==1
    assert report['evidence_coverage']['spoken_audio_verified'] is None
    assert 'PRIVATE PLATFORM RESULT' not in canonical(report)+canonical(list(feedback_rows(str(uuid4()),report)))


def test_signed_native_without_provider_result_is_still_only_a_verified_attempt(scenario):
    native_call(scenario,provider_result=False);store,call=scenario
    package=build_evidence(call,read_snapshot(store.path,call['id']))
    entry=next(v for v in package['evidence'].values()if isinstance(v,dict) and v.get('name')=='endCall')
    assert entry['platform_evidence']['provider_results']==[]
    assert entry['platform_evidence']['outcome']=='unknown'
    assert package['mechanical_checks']['evidence_complete']['verdict']=='pass'


@pytest.mark.parametrize('corruption',['unsigned','nonempty_args_even_matching_fingerprint','mixed_batch','wrong_caller_anchor',
    'missing_caller_anchor','wrong_fingerprint','wrong_call_scope','wrong_session_scope','repeated_attempt'])
def test_native_exemption_never_accepts_forgery_mixed_batch_or_stale_caller_context(scenario,tmp_path,corruption):
    tool=native_call(scenario,signed=corruption!='unsigned');store,call=scenario
    if corruption=='nonempty_args_even_matching_fingerprint':
        tool['function']['arguments']='{"reason":"goodbye"}'
    elif corruption=='mixed_batch':
        call['artifact']['messages'][-2]['toolCalls'].append(deepcopy(call['artifact']['messages'][2]['toolCalls'][0]))
    elif corruption=='repeated_attempt':
        call['artifact']['messages'].append({'role':'tool_calls','toolCalls':[deepcopy(tool)]})
    if corruption=='wrong_call_scope':
        with store.connect() as db:db.execute('UPDATE gemini_tool_signatures SET provider_call_id=? WHERE tool_call_id=?',(str(uuid4()),tool['id']))
    if corruption=='wrong_session_scope':
        with store.connect() as db:db.execute('UPDATE gemini_tool_signatures SET session_id=? WHERE tool_call_id=?',('different_session',tool['id']))
    snapshot=read_snapshot(store.path,call['id'])
    signature=next((s for s in snapshot['signatures']if s['tool_call_id']==tool['id']),None)
    if corruption=='nonempty_args_even_matching_fingerprint':
        signature['fingerprint']=hashlib.sha256(canonical({'name':'endCall','arguments':{'reason':'goodbye'}}).encode()).hexdigest()
    elif corruption=='wrong_caller_anchor':signature['user_count']-=1
    elif corruption=='missing_caller_anchor':signature['user_count']=None
    elif corruption=='wrong_fingerprint':signature['fingerprint']='0'*64
    package=build_evidence(call,snapshot)
    assert package['mechanical_checks']['evidence_complete']['verdict']=='uncertain'
    assert any('native_platform_attempt' in i for i in package['evidence_issues'])
    report=public_report(package,judgment(package),call,[],configuration(tmp_path,store.path))
    assert all(report['checks'][d]['verdict']=='uncertain' for d in DIMENSIONS)


def test_signed_native_call_can_be_reviewed_without_dispatching_another_business_action(scenario,tmp_path):
    native_call(scenario);worker,mock,_=worker_fixture(tmp_path,scenario)
    assert worker.tick() and worker.outbox.status()['jobs']=={'complete':1}
    assert mock.judge_requests==1
    report=next(iter(mock.runs.values()))['outputs']
    assert report['checks']['booking_integrity']['verdict']=='pass'
    assert report['review_version']==VERSION=='live-call-review-v6'


def test_unsigned_native_success_cannot_consume_judge_budget_or_receive_clean_passes(scenario,tmp_path):
    native_call(scenario,signed=False);worker,mock,_=worker_fixture(tmp_path,scenario)
    assert worker.tick() and worker.outbox.status()['jobs']=={'complete':1}
    assert mock.judge_requests==0
    report=next(iter(mock.runs.values()))['outputs']
    assert all(report['checks'][d]['verdict']=='uncertain' for d in DIMENSIONS)


def test_pending_v4_review_is_published_readably_with_its_frozen_grades_and_no_new_judge(scenario,tmp_path):
    worker,mock,now=worker_fixture(tmp_path,scenario);assert worker.tick()
    # A v4 review whose export was still pending at upgrade time (a completed one
    # is never requeued) is published under the current contract.
    with closing(worker.outbox.db()) as db,db:
        report=json.loads(db.execute('SELECT report FROM jobs').fetchone()[0]);report['review_version']='live-call-review-v4'
        report['checks']['conversation_quality']={'verdict':'fail','issue_code':'unnecessary_repetition','evidence_refs':['turn:2','invented:9'],'explanation':'frozen'}
        report['review_process']['judge_prompt_revision']='2026-10-19-historical-1'
        db.execute("UPDATE jobs SET report=?,state='pending',lease=0,next_attempt=0,completed=NULL",(canonical(report),))
    mock.runs.clear();mock.feedback.clear();now[0]+=901
    restarted=Worker(worker.config,client=worker.client,clock=lambda:now[0])
    assert restarted.tick()
    assert mock.judge_requests==1 and restarted.outbox.status()['jobs']=={'complete':1}
    published=next(iter(mock.runs.values()))['outputs']
    assert published['review_version']=='live-call-review-v6' and published['supersedes_review_version']=='live-call-review-v4'
    assert published['review_process']['semantic_grades_reused_from']=='live-call-review-v4'
    assert published['review_process']['judge_prompt_revision']=='2026-10-19-historical-1'
    # The frozen fail survives; a reference the rebuilt package no longer holds is dropped.
    assert published['checks']['conversation_quality']['verdict']=='fail'
    assert published['checks']['conversation_quality']['issue_code']=='unnecessary_repetition'
    assert published['checks']['conversation_quality']['evidence_refs']==['turn:2']
    assert published['needs_review'] is True and '1 Summary' in published
    with closing(restarted.outbox.db()) as db:
        assert json.loads(db.execute('SELECT report FROM jobs').fetchone()[0])['review_version']=='live-call-review-v6'
        assert db.execute('SELECT judge_attempts FROM jobs').fetchone()[0]==1
    assert not Worker(worker.config,client=worker.client,clock=lambda:now[0]).tick() and mock.judge_requests==1


def test_four_authored_native_controls_are_explicit_offline_expectations_not_model_or_audio_results(scenario):
    store,call=scenario;snapshot=read_snapshot(store.path,call['id'])
    original_call,original_snapshot=deepcopy(call),deepcopy(snapshot)
    controls=native_platform_controls(call,snapshot)
    assert call==original_call and snapshot==original_snapshot
    assert {c['id']for c in controls}=={'signed_explicit_finish','signed_attempt_despite_wait','unsigned_provider_success','signed_customer_termination'}
    assert all(c['source']=='authored_offline_native_control_not_a_live_result'for c in controls)
    assert all(c['audio_verification_expected']=='unknown'for c in controls)
    for control in controls:
        complete=control['package']['mechanical_checks']['evidence_complete']['verdict']
        assert complete==('pass'if control['signed_attempt_expected']else'uncertain')
        native=next(v for v in control['package']['evidence'].values()if isinstance(v,dict)and v.get('name')=='endCall')
        if control['signed_attempt_expected']:
            assert native['platform_evidence']['outcome']=='unknown'
            assert native['platform_evidence']['provider_ended_by_assistant']==control['provider_ended_by_assistant_expected']
