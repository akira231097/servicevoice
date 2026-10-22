"""The readable call review must stay content-free, honest about joins and stable on retry."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import time
from uuid import uuid4

import pytest

from app import call_review_presentation as presentation
from app.call_review_presentation import (artifact_turns, build_review_children, build_timeline, call_cost,
    condense_diagnostics, feedback_rows, interruptions, markdown_report, review_metadata, review_tags)
from app.diagnostics import Diagnostics, operation_identifier, read_review_diagnostics, turn_identifier
from app.live_eval_evidence import build_evidence, canonical, read_snapshot
from app.live_evaluations import Worker, public_report, reuse_cached_grades
from test_live_evaluations import scenario, configuration, judgment, worker_fixture

ROOT='4aa3c2fa-a6a2-4a3f-a2e0-f19b58dd7534'
PROJECT='aed7dd11-c235-46a1-982f-a1454c4fbf40'
ORDER='20261020T170000000000Z'+ROOT


def artifact_call(call,*,turns=2):
    """Provider-shaped message timings in their real units: epoch ms, ms durations, seconds offsets."""
    call=deepcopy(call);start=datetime.fromisoformat(call['startedAt']).timestamp()
    messages=[m for m in call['artifact']['messages'] if m.get('role')!='user']
    users=[m for m in call['artifact']['messages'] if m.get('role')=='user']
    rebuilt=[];offset=5.0
    for index,user in enumerate(users[:turns]):
        at=start+offset
        rebuilt.append({**user,'time':int(at*1000),'endTime':int(at*1000)+1800,'duration':1800,'secondsFromStart':offset,'confidence':[.97,.67][index%2]})
        bot=next((m for m in messages if m.get('role')=='bot'),None)
        if bot is not None:
            messages.remove(bot)
            rebuilt.append({**bot,'time':int(at*1000)+3000,'endTime':int(at*1000)+9000,'duration':6000,'secondsFromStart':offset+3})
        offset+=20
    call['artifact']['messages']=[m for m in call['artifact']['messages'] if m.get('role')=='system']+rebuilt+[m for m in messages if m.get('role')!='bot']
    call['artifact']['performanceMetrics']={'turnLatencyAverage':1500,'modelLatencyAverage':900,'transcriberLatencyAverage':400,'voiceLatencyAverage':150,'endpointingLatencyAverage':3,
        'numAssistantInterrupted':2,'turnLatencies':[{'turnLatency':1200+i*600,'modelLatency':800,'voiceLatency':150,'transcriberLatency':400,'endpointingLatency':3} for i in range(turns)]}
    call['costBreakdown']={'total':.27,'stt':.02,'llm':0,'tts':0,'vapi':.25,'llmPromptTokens':4000,'llmCompletionTokens':40,'ttsCharacters':300}
    call['cost']=.27;call['endedReason']='customer-ended-call';call['type']='inboundPhoneCall'
    return call


def timing_row(call,*,ordinal_offset=10,route='gemini',outcome='completed',first_text=2600,tool=None,cue=None,tokens=1200):
    generation=str(uuid4())
    row={'request_id':generation.replace('-',''),'generation_id':generation,'call_key':hashlib.sha256(call['id'].encode()).hexdigest()[:16],
        'started_at_unix':datetime.fromisoformat(call['startedAt']).timestamp()+ordinal_offset,
        'milestones_ms':{'preflight':3,'upstream_headers':400,'first_text':first_text,'tool_emitted':tool,'total':max(v for v in (first_text,tool,300) if v is not None)+10},
        'route':route,'outcome':outcome,'usage':{'input_tokens':tokens,'output_tokens':40,'total_tokens':tokens+40},
        'transcript':'PRIVATE TRANSCRIPT','prompt':'PRIVATE PROMPT'}
    if cue:row['backchannel']=cue
    return row


def ledger_for(scenario,rows,*,tts=0):
    """Record gateway events (with turn/request identity), the confirm tool and TTS requests."""
    store,call=scenario;source=__import__('pathlib').Path(store.path)
    session=read_snapshot(store.path,call['id'])['session']['id']
    sink=Diagnostics(source.with_name(source.stem+'-diagnostics.sqlite3'))
    for ordinal,row in rows:
        assert sink.record('gateway','request_finished','success',call_id=call['id'],turn_id=turn_identifier(call['id'],ordinal),
            request_id=row['request_id'],start_unix=row['started_at_unix'],duration_ms=row['milestones_ms']['total'],
            attributes={'generation_id':row['generation_id'],'user_ordinal':ordinal,'route':row['route']})
    operation=operation_identifier(session,'confirm')
    assert sink.record('tool_harness','execution_finished','success',call_id=call['id'],turn_id=turn_identifier(call['id'],2),
        operation_id=operation,start_unix=datetime.fromisoformat(call['startedAt']).timestamp()+27,duration_ms=48,
        attributes={'tool_name':'confirm_action','receipt_present':True,'error_code':'none'})
    start=datetime.fromisoformat(call['startedAt']).timestamp()
    for index in range(tts):
        request=uuid4().hex
        assert sink.record('tts','tts.request_started','started',call_id=call['id'],request_id=request,start_unix=start+8+index,duration_ms=0)
        assert sink.record('tts','tts.first_audio_ready','started',call_id=call['id'],request_id=request,start_unix=start+8+index,duration_ms=210,attributes={'first_audio_ms':210})
        assert sink.record('tts','tts.completed','success',call_id=call['id'],request_id=request,start_unix=start+8+index,duration_ms=900,
            attributes={'cache_hit':index==0,'input_characters':40,'audio_duration_ms':1500,'synthesis_source':'pregenerated_cache' if index==0 else 'live_google'})
    for index in range(30):
        assert sink.record('webhook','http_finished','success',call_id=call['id'],request_id=uuid4().hex,start_unix=start+1+index/10,duration_ms=2,attributes={'http_status':200})
        assert sink.record('webhook','call_bound','success',call_id=call['id'],request_id=uuid4().hex,start_unix=start+1+index/10,attributes={'event_type':'speech-update'})
    assert sink.close()
    return read_review_diagnostics(source,call['id'])


def test_artifact_units_cost_and_interruptions_are_read_in_provider_units(scenario):
    call=artifact_call(scenario[1])
    turns=artifact_turns(call)
    assert [t['ordinal'] for t in turns]==[1,2]
    assert turns[0]['caller_start_s']==5.0 and turns[0]['caller_duration_s']==1.8 and turns[0]['asr_confidence']==.97
    assert turns[0]['assistant_start_s']==8.0 and turns[0]['assistant_end_s']==14.0
    assert interruptions(call)=={'caller_interrupted_assistant':2}
    assert call_cost(call)=={'total_usd':.27,'speech_recognition_usd':.02,'model_usd':0,'voice_usd':0,'platform_usd':.25,'provider_prompt_tokens':4000,'provider_completion_tokens':40,'voice_characters':300}
    assert 'PRIVATE' not in canonical(turns)


def test_timeline_joins_by_explicit_identity_and_states_its_basis(scenario,tmp_path):
    store,call=scenario;call=artifact_call(call)
    first=timing_row(call,ordinal_offset=6,cue={'phrase_id':'checking','reason':'emitted','emitted_ms':570,'classification_ms':150,'follow_up':False})
    speculative=timing_row(call,ordinal_offset=25,outcome='cancelled',first_text=None,cue={'phrase_id':'moment','reason':'emitted','emitted_ms':560,'refunded':True,'refund_reason':'generation_cancelled_before_answer'})
    second=timing_row(call,ordinal_offset=26,first_text=None,tool=1900,cue={'phrase_id':'silence','reason':'private_detail'})
    diagnostics=ledger_for((store,call),[(1,first),(2,speculative),(2,second)],tts=3)
    tool_ids={};package=build_evidence(call,read_snapshot(store.path,call['id']),tool_ids=tool_ids)
    session=read_snapshot(store.path,call['id'])['session']['id']
    operation_ids={ref:operation_identifier(session,tid) for ref,tid in tool_ids.items()}
    timeline=build_timeline(call,package,[first,speculative,second],diagnostics,call_key=hashlib.sha256(call['id'].encode()).hexdigest(),
        turn_ids={k:turn_identifier(call['id'],k) for k in range(1,10)},operation_ids=operation_ids)
    one,two=timeline['turns']
    assert one['cue']['text']=='Let me check that.' and one['answer_after_ms']==2600 and one['answered_with']=='spoken answer'
    assert one['first_response_ms']==1200 and one['stage_ms']['speech_recognition']==400 and one['caller_start_s']==5.0
    assert two['speculative_cancelled']==1 and two['answered_with']=='tool call confirm_action' and two['answer_after_ms']==1900
    assert two['cue'] is None and two['cue_suppressed_reason']=='private_detail'
    confirm=next(t for t in two['tools'] if t['name']=='confirm_action')
    assert confirm['duration_ms']==48 and confirm['receipt_present'] is True and confirm['operation_id']==operation_ids['tool:3']
    assert 'caller gave private details' in two['line'] and 'tool confirm_action ok 0.05 s' in two['line']
    assert '1 speculative request(s) cancelled' in two['line'] and 'first audio 1.80 s' in two['line']
    assert timeline['join_basis']['artifact_rows_attached_by_ordinal'] is True
    assert timeline['join_basis']['vapi_turn_latencies_attached_by_ordinal'] is True
    assert len(timeline['tts_requests'])==3 and timeline['tts_requests'][0]['cache_hit'] is True and timeline['tts_requests'][1]['first_audio_ms']==210
    assert timeline['interruptions']=={'caller_interrupted_assistant':2}
    assert 'PRIVATE' not in canonical(timeline)
    # A different caller-turn count means artifact rows are listed, not attached.
    mismatched=artifact_call(call,turns=1)
    other=build_timeline(mismatched,package,[first,speculative,second],diagnostics,call_key=hashlib.sha256(call['id'].encode()).hexdigest(),
        turn_ids={k:turn_identifier(call['id'],k) for k in range(1,10)},operation_ids=operation_ids)
    assert other['join_basis']['artifact_rows_attached_by_ordinal'] is False
    assert all(t['caller_start_s'] is None and t['first_response_ms'] is None for t in other['turns'])


def test_condensed_diagnostics_keep_every_non_webhook_event_and_count_the_rest(scenario):
    store,call=scenario;row=timing_row(call)
    diagnostics=ledger_for(scenario,[(1,row)],tts=1)
    key=hashlib.sha256(call['id'].encode()).hexdigest()
    condensed=condense_diagnostics(diagnostics,key)
    assert condensed['webhook_deliveries']['deliveries']==60 and condensed['webhook_deliveries']['by_event']=={'http_finished':30,'call_bound':30}
    assert condensed['webhook_deliveries']['http_status']=={'200':30} and condensed['webhook_deliveries']['by_event_type']=={'speech-update':30}
    assert {(e['component'],e['event']) for e in condensed['events']}=={('gateway','request_finished'),('tool_harness','execution_finished'),
        ('tts','tts.request_started'),('tts','tts.first_audio_ready'),('tts','tts.completed')}
    assert condensed['event_counts']['webhook.http_finished.success']==30 and condensed['coverage']['summarized_webhook_events']==60
    assert condensed['coverage']['event_count']==len(diagnostics['events'])


def test_union_cap_drops_webhook_delivery_rows_before_business_evidence(scenario):
    store,call=scenario;row=timing_row(call);source=__import__('pathlib').Path(store.path)
    ledger_for(scenario,[(1,row)],tts=1)
    merged=read_review_diagnostics(source,call['id'],limit=8)
    kept=[(e['component'],e['event']) for e in merged['events']]
    assert len(kept)==8 and kept.count(('webhook','http_finished'))+kept.count(('webhook','call_bound'))==3
    assert {('gateway','request_finished'),('tool_harness','execution_finished'),('tts','tts.request_started'),('tts','tts.first_audio_ready'),('tts','tts.completed')}<=set(kept)
    assert merged['events']==sorted(merged['events'],key=lambda e:(e['start_unix'],e['event_id']))
    assert merged['coverage']['truncated'] is True and 'read_limit_exceeded' in merged['coverage']['reason_codes']
    assert merged['sources'][0]['coverage']['omitted_webhook_delivery_events']==57
    assert merged['coverage']['omission_order']=='webhook_delivery_rows_first_then_chronological'
    # Five hundred union rows: the business ledger is only truncated, never silently trimmed of evidence.
    assert len(read_review_diagnostics(source,call['id'],limit=500)['events'])==65


def test_public_report_has_readable_sections_markdown_and_persisted_trace(scenario,tmp_path):
    store,call=scenario;call=artifact_call(call)
    first=timing_row(call,ordinal_offset=6,cue={'phrase_id':'checking','reason':'emitted','emitted_ms':570,'classification_ms':150})
    second=timing_row(call,ordinal_offset=26,first_text=None,tool=1900)
    diagnostics=ledger_for((store,call),[(1,first),(2,second)],tts=2)
    tool_ids={};package=build_evidence(call,read_snapshot(store.path,call['id']),tool_ids=tool_ids)
    session=read_snapshot(store.path,call['id'])['session']['id']
    report=public_report(package,judgment(package),call,[first,second],configuration(tmp_path,store.path),diagnostics=diagnostics,
        operation_ids={ref:operation_identifier(session,tid) for ref,tid in tool_ids.items()})
    assert report['presentation_status']=='ready'
    summary=report['1 Summary']
    assert summary['headline'].startswith('Inbound phone call · 2 min 0 s · 2 caller turns · 1 appointment saved · ended: caller hung up')
    assert summary['headline'].endswith('checks clear')
    assert any(line.startswith('Tools used: begin_service_request, prepare_booking, confirm_action') for line in summary['lines'])
    assert any('Waiting cues played on 1 of 2 caller turns' in line for line in summary['lines'])
    assert any('interrupted the assistant 2 time' in line for line in summary['lines'])
    groups={g['group']:{row['check']:row for row in g['checks']} for g in report['2 Scorecard']['groups']}
    assert groups['Speed']['first_response_within_target']['result']=='PASS' and groups['Speed']['answer_within_target']['result']=='PASS'
    assert groups['Booking (code checks win over the reviewer)']['booking_integrity']['why']=='saved appointment matches its receipt'
    timeline=report['3 Turn-by-turn timeline']
    assert timeline['turns'][0].startswith('Turn 1 · caller spoke at 0:05.0 for 1.8 s · cue "Let me check that." at 0.57 s · spoken answer after 2.60 s on the server')
    assert timeline['details'][1]['tools']==['confirm_action ok'] and timeline['details'][1]['asr_confidence']==.67
    assert timeline['speech_synthesis']=={'requests':2,'cached_cue_clips':1,'first_audio_p95_ms':210,'failed':0}
    assert report['4 Latency']['answer_p95_ms']==2600 and report['4 Latency']['first_response_avg_ms']==1500
    assert report['5 Waiting cues']['turns_with_a_cue']==1 and report['5 Waiting cues']['phrases_used']=={'Let me check that.':1}
    assert report['6 Booking and actions']['provider_cost']['total_usd']==.27 and report['6 Booking and actions']['appointment_count']==1
    assert report['call_summary']['duration_seconds']==120.0 and report['slow_call'] is False
    assert report['latency_targets_ms']['server_answer_p95']==3000
    assert report['evidence_index']['turn:1']['at']=='0:05.0'
    markdown=report['0 Report (markdown)']
    assert markdown.startswith('# Call review — ') and '| Stuck to the dealership facts and tool results | PASS |' in markdown
    assert '1. Turn 1 · caller spoke at 0:05.0' in markdown and 'Provider cost: $0.2700' in markdown
    trace=report['review_trace']
    assert [t['turn'] for t in trace['turns']]==[1,2] and trace['turns'][0]['generations'][0]['request_id']==first['request_id']
    assert report['diagnostics']['webhook_deliveries']['deliveries']==60
    exported=canonical(report)+canonical(list(feedback_rows(str(uuid4()),report)))
    for forbidden in ('PRIVATE','Alex Morgan','5125550142','CSV-'):
        assert forbidden not in exported
    rows={r['key']:r for r in feedback_rows(str(uuid4()),report)}
    assert rows['needs_review']['score']==0 and rows['slow_call']['score']==0 and rows['coverage_semantic_review']['score']==1
    assert rows['metric_call_seconds']['score']==120.0 and rows['metric_bookings']['score']==1 and rows['metric_cues_played']['score']==1
    assert rows['metric_cost_usd']['score']==.27 and rows['metric_caller_interruptions']['score']==2 and rows['metric_asr_confidence_min']['score']==.67
    assert rows['metric_answer_p95_ms']['score']==2600 and rows['metric_tokens_total']['score']==2480 and rows['metric_tool_calls']['score']==3
    assert all(r['comment'].startswith('Measurement, not a score.') for k,r in rows.items() if k.startswith('metric_'))
    assert set(review_tags(report))=={'real-call','automatic-review','live-call-review-v6','checks-clear','booked','caller-hung-up'}
    metadata=review_metadata(report,call_id=call['id'],environment='production',latency_target_ms=2000)
    assert metadata['bookings']==1 and metadata['ended_reason']=='customer-ended-call' and metadata['cost_usd']==.27


def test_children_are_deterministic_readable_and_bounded(scenario,tmp_path):
    store,call=scenario;call=artifact_call(call)
    first=timing_row(call,ordinal_offset=6,cue={'phrase_id':'checking','reason':'emitted','emitted_ms':570,'classification_ms':150})
    second=timing_row(call,ordinal_offset=26,first_text=None,tool=1900)
    orphan=timing_row(call,ordinal_offset=40,route='deterministic_receipt',first_text=30)
    diagnostics=ledger_for((store,call),[(1,first),(2,second)],tts=2)
    tool_ids={};package=build_evidence(call,read_snapshot(store.path,call['id']),tool_ids=tool_ids)
    session=read_snapshot(store.path,call['id'])['session']['id']
    report=public_report(package,judgment(package),call,[first,second,orphan],configuration(tmp_path,store.path),diagnostics=diagnostics,
        operation_ids={ref:operation_identifier(session,tid) for ref,tid in tool_ids.items()})
    plan=build_review_children(report['review_trace'],root_run_id=ROOT,root_dotted_order=ORDER,project_id=PROJECT)
    again=build_review_children(json.loads(canonical(report['review_trace'])),root_run_id=ROOT,root_dotted_order=ORDER,project_id=PROJECT)
    assert plan==again
    names=[r['name'] for r in plan['runs']]
    assert names[:3]==['turn 01 · tool begin_service_request +1','cue · Let me check that. (0.57 s)','gemini · answer, first text after 2.60 s']
    assert 'turn 02 · booked' in names and 'gemini · tool call after 1.90 s' in names and 'tool · confirm_action (ok, 0.05 s)' in names
    assert 'gateway requests without a recorded turn · 1' in names and 'receipt readback · verified receipt, no model' in names
    assert 'speech synthesis · 2 requests' in names and 'tts · cached cue clip' in names
    assert plan['coverage']=={'schema_version':'review-trace-v6','turns':2,'generations':3,'cues':1,'tools':1,'tts':2,'provider_model_spans':0,'omitted':0,'exported_runs':11,
        'interpretation':plan['coverage']['interpretation']}
    by_name={r['name']:r for r in plan['runs']}
    cue=by_name['cue · Let me check that. (0.57 s)'];generation=by_name['gemini · answer, first text after 2.60 s']
    assert cue['parent_run_id']==by_name['turn 01 · tool begin_service_request +1']['id'] and generation['run_type']=='llm'
    assert (datetime.fromisoformat(cue['end_time'])-datetime.fromisoformat(cue['start_time'])).total_seconds()==pytest.approx(.57)
    assert generation['outputs']['usage_metadata']=={'input_tokens':1200,'output_tokens':40,'total_tokens':1240}
    for run in plan['runs']:
        assert run['trace_id']==ROOT and run['session_id']==PROJECT and run['dotted_order'].startswith(ORDER+'.')
        parent=by_name.get(next((n for n,r in by_name.items() if r['id']==run['parent_run_id']),''))
        assert parent is None or run['dotted_order'].startswith(parent['dotted_order']+'.')
    assert 'PRIVATE' not in canonical(plan)
    capped=build_review_children(report['review_trace'],root_run_id=ROOT,root_dotted_order=ORDER,project_id=PROJECT,max_children=4)
    assert len(capped['runs'])==4 and capped['coverage']['omitted']==7
    with pytest.raises(ValueError,match='invalid_review_child_destination'):
        build_review_children(report['review_trace'],root_run_id='bad',root_dotted_order=ORDER,project_id=PROJECT)


def test_children_never_invent_timing_and_markdown_handles_empty_calls(scenario,tmp_path):
    store,call=scenario
    package=build_evidence(call,read_snapshot(store.path,call['id']))
    report=public_report(package,{},call,[],configuration(tmp_path,store.path))
    assert report['presentation_status']=='ready' and report['3 Turn-by-turn timeline']['turns']==[
        'Turn 1 · no gateway request recorded for this turn · tool begin_service_request ok · tool prepare_booking ok · first audio 1.20 s after the caller stopped (Vapi)',
        'Turn 2 · no gateway request recorded for this turn · tool confirm_action ok · first audio 1.80 s after the caller stopped (Vapi)']
    assert report['4 Latency']['summary'][0].startswith('Callers heard a response 1.50 s on average')
    plan=build_review_children(report['review_trace'],root_run_id=ROOT,root_dotted_order=ORDER,project_id=PROJECT)
    assert plan['coverage']['turns']==0 and plan['coverage']['omitted']==2 and plan['runs']==[]
    assert 'Needs a human look' in report['0 Report (markdown)']
    trace=deepcopy(report['review_trace']);trace['turns'][0]['start_unix']='not a time';trace['tts_requests']=[{'request_id':'x','start_unix':None}]
    assert build_review_children(trace,root_run_id=ROOT,root_dotted_order=ORDER,project_id=PROJECT)['runs']==[]
    assert build_review_children(None,root_run_id=ROOT,root_dotted_order=ORDER,project_id=PROJECT)['coverage']['exported_runs']==0
    assert markdown_report(report)  # Reusable without a call id or start time.


def test_reused_grades_keep_frozen_verdicts_and_drop_unknown_references(scenario,tmp_path):
    store,call=scenario
    package=build_evidence(call,read_snapshot(store.path,call['id']))
    cached=public_report(package,judgment(package,'fail'),call,[],configuration(tmp_path,store.path))
    cached['checks']['hallucination_free']['evidence_refs']=['turn:1','invented:1']
    cached['checks']['task_completion']['issue_code']='not_a_code'
    graded,status=reuse_cached_grades(cached,package)
    assert status=='assessed' and graded['hallucination_free']['evidence_refs']==['turn:1']
    assert graded['hallucination_free']['verdict']=='fail' and graded['task_completion']['issue_code']=='other'
    assert all(v['reason'] for v in graded.values())
    unjudged=deepcopy(cached);unjudged['review_process']['semantic_status']='judge_attempt_limit_exhausted'
    assert reuse_cached_grades(unjudged,package)==({},'judge_attempt_limit_exhausted')
    assert reuse_cached_grades({'checks':{}},package)==({},'semantic_review_unavailable')


def test_flagged_call_is_queued_once_for_human_listening_and_queue_outage_is_not_a_failure(scenario,tmp_path):
    store,call=scenario
    call['artifact']['messages']=[m for m in call['artifact']['messages'] if m.get('role')!='user']  # no caller text: every grade uncertain
    worker,mock,now=worker_fixture(tmp_path,(store,call))
    assert worker.tick() and worker.outbox.status()['jobs']=={'complete':1}
    root=next(r for r in mock.runs.values() if r['name']=='voice.call.review')
    assert root['outputs']['needs_review'] is True
    assert len(mock.queue_posts)==1 and mock.queue_posts[0]['name']=='ServiceVoice calls to review'
    assert [k['feedback_key'] for k in mock.queue_posts[0]['rubric_items']]==['human_sounded_natural','human_cues_well_timed','human_heard_correctly','human_booking_correct','human_overall_acceptable']
    assert mock.queue_adds==[root['id']]
    status=worker.outbox.status()
    assert status['human_review_queue']['queued']==1 and status['human_review_queue']['queue_unavailable']==0
    with closing(worker.outbox.db()) as db:assert db.execute('SELECT queued FROM jobs').fetchone()[0]=='queued'
    assert not worker.tick() and mock.queue_adds==[root['id']]
    # A queue outage after a durable finish is recorded, never retried as a review failure.
    other=deepcopy(call);other['id']=str(uuid4())
    with store.connect() as db:db.execute('UPDATE sessions SET provider_call_id=?',(other['id'],))
    outage,mock2,_=worker_fixture(tmp_path/'second',(store,other));mock2.fail_queue=True
    assert outage.tick() and outage.outbox.status()['jobs']=={'complete':1}
    assert outage.outbox.status()['human_review_queue']['queue_unavailable']==1 and mock2.judge_requests==0
    assert not outage.tick()


def test_clear_call_is_not_queued_and_disabled_queue_is_silent(scenario,tmp_path):
    worker,mock,_=worker_fixture(tmp_path,scenario)
    assert worker.tick() and next(iter(mock.runs.values()))['outputs']['needs_review'] is False
    assert not mock.queue_posts and not mock.queue_adds
    assert not any(p.startswith('/annotation-queues') for _,p in mock.request_paths)


def test_readable_sections_sort_first_and_remain_free_of_private_text(scenario,tmp_path):
    worker,mock,_=worker_fixture(tmp_path,scenario);mock.call['endedReason']='customer-ended-call'
    assert worker.tick()
    root=next(r for r in mock.runs.values() if r['name']=='voice.call.review')
    keys=list(json.loads(canonical(root['outputs'])))
    assert keys[:9]==['0 Report (markdown)','1 Summary','2 Scorecard','3 Turn-by-turn timeline','4 Latency','5 Waiting cues','6 Booking and actions','7 How to read this review','booking']
    assert root['inputs']['headline']==root['outputs']['1 Summary']['headline']
    assert root['extra']['metadata']['bookings']==1 and root['extra']['metadata']['ended_reason']=='customer-ended-call'
    assert set(root['tags'])>={'live-call-review-v6','checks-clear','booked','caller-hung-up'}
    serialized=canonical(root)+canonical(list(mock.feedback.values()))
    for forbidden in ('Alex Morgan','5125550142','PRIVATE PROMPT','CSV-'):
        assert forbidden not in serialized


def test_status_runs_are_rare_and_labelled(scenario,tmp_path):
    worker,mock,now=worker_fixture(tmp_path,scenario)
    worker.heartbeat();first=len(mock.runs)
    now[0]+=1000;worker.heartbeat()
    assert len(mock.runs)==first  # a quarter hour of unchanged status is not a new trace
    now[0]+=21000;worker.heartbeat()
    assert len(mock.runs)==first  # the six-hour heartbeat reuses the same-day fingerprint for an unchanged status
    worker.outbox.set_setting('provider_discovery_error','provider_discovery_incomplete');now[0]+=20;worker.heartbeat()
    assert len(mock.runs)==first+1  # a changed status is a new immutable row
    status=next(r for r in mock.runs.values() if r['name']=='voice.review.coverage')
    assert 'reviewer-status' in status['tags'] and 'not a call' in status['inputs']['scope']
