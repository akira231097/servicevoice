"""Actual persisted generation identity flows to the same call-review trace."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from app.live_eval_evidence import build_evidence, canonical, read_snapshot
from app.live_evaluations import public_report
from test_live_evaluations import scenario, configuration, judgment, worker_fixture


def row(call,*,legacy=False):
    generation=str(uuid4())
    result={'request_id':generation.replace('-',''),'call_key':hashlib.sha256(call['id'].encode()).hexdigest()[:16],
        'started_at_unix':datetime.fromisoformat(call['startedAt']).timestamp()+10,
        'milestones_ms':{'preflight':3,'first_text':50,'total':100},
        'route':'gemini','outcome':'completed',
        'transcript':'PRIVATE TRANSCRIPT MUST NEVER LEAVE TIMING STORE',
        'request_headers':{'Authorization':'Bearer PRIVATE KEY'}}
    if not legacy:
        result.update(generation_id=generation,generation_sequence=1,
            generation_sequence_scope='process_local_http_arrival_only',generation_terminal_state='completed',
            payload_release_scope='gateway_yield_not_provider_receipt_or_playback',payload_released_at_ms=50,
            sse_response_id='chatcmpl-'+generation.replace('-',''))
    return result


def save(store,timing):
    source=Path(store.path);path=source.with_name(source.stem+'-gateway-timings.sqlite3')
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS gateway_timings(request_id TEXT PRIMARY KEY,call_key TEXT,started REAL,payload TEXT)')
        db.execute('INSERT INTO gateway_timings VALUES (?,?,?,?)',(timing['request_id'],timing['call_key'],timing['started_at_unix'],canonical(timing)))


def test_persisted_generation_exports_as_same_review_root_child_without_model_audio_join_or_private_text(scenario,tmp_path):
    store,call=scenario;timing=row(call);save(store,timing)
    worker,mock,_=worker_fixture(tmp_path,scenario)
    assert worker.tick() and worker.outbox.status()['jobs']=={'complete':1}
    root=next(r for r in mock.runs.values()if r['name']=='voice.call.review')
    generation=next(r for r in mock.runs.values()if r.get('parent_run_id') and r['extra']['metadata']['record_kind']=='generation')
    # Without a diagnostics turn identifier the request is listed, not guessed into a turn.
    group=next(r for r in mock.runs.values()if r['name'].startswith('gateway requests without a recorded turn'))
    assert generation['parent_run_id']==group['id'] and group['parent_run_id']==generation['trace_id']==root['id']
    assert generation['inputs']['generation_id']==timing['generation_id'] and generation['run_type']=='llm'
    assert generation['name']=='gemini · answer, first text after 0.05 s'
    assert generation['outputs']['first_text_ms']==50 and generation['outputs']['total_ms']==100
    assert datetime.fromisoformat(generation['end_time'])>datetime.fromisoformat(generation['start_time'])
    assert root['outputs']['gateway_generations']['coverage']['observed_generation_count']==1
    assert root['outputs']['trace_export']['generations']==1 and root['outputs']['trace_export']['turns']==0
    assert root['outputs']['review_trace']['join_basis']['provider_model_spans_joined_to_generations'] is False
    assert mock.judge_requests==1
    assert 'PRIVATE' not in canonical(root)+canonical(generation)


def test_legacy_rows_keep_original_missing_identity_coverage_without_inferred_generation_children(scenario,tmp_path):
    store,call=scenario;save(store,row(call,legacy=True))
    worker,mock,_=worker_fixture(tmp_path,scenario)
    assert worker.tick() and worker.outbox.status()['jobs']=={'complete':1}
    report=next(r for r in mock.runs.values()if r['name']=='voice.call.review')['outputs']
    coverage=report['gateway_generations']['coverage']
    assert coverage['state']=='unavailable' and coverage['input_rows']==1
    assert coverage['skipped_row_counts']=={'persisted_generation_identity_unavailable':1}
    assert coverage['legacy_generation_ids_inferred'] is False
    # The request is still shown by its persisted request id; no generation id is invented.
    legacy=next(r for r in mock.runs.values() if r.get('parent_run_id') and r['extra']['metadata']['record_kind']=='generation')
    assert legacy['inputs']['generation_id'] is None and legacy['extra']['metadata']['generation_id'] is None
    assert report['needs_review'] is False  # Unknown lineage is separate coverage.


def test_frozen_generation_projection_and_run_ids_survive_export_retry_without_another_judge(scenario,tmp_path):
    store,call=scenario;save(store,row(call));worker,mock,now=worker_fixture(tmp_path,scenario)
    mock.fail_feedback_once=True;worker.tick()
    frozen=deepcopy(mock.runs)
    save(store,row(call))  # Later evidence cannot silently change a frozen review.
    now[0]+=901;worker.tick()
    assert worker.outbox.status()['jobs']=={'complete':1} and mock.judge_requests==1
    assert all(mock.runs[key]==value for key,value in frozen.items())
    assert len([r for r in mock.runs.values()if r.get('parent_run_id') and r['extra']['metadata']['record_kind']=='generation'])==1
    assert len(mock.run_posts)==len(set(mock.run_posts))


def test_generation_payload_from_another_call_never_attaches_to_this_review(scenario,tmp_path):
    store,call=scenario;timing=row(call);timing['call_key']='0'*16
    package=build_evidence(call,read_snapshot(store.path,call['id']))
    report=public_report(package,judgment(package),call,[timing],configuration(tmp_path,store.path))
    assert report['gateway_generations']['generations']==[]
    assert report['gateway_generations']['coverage']['skipped_row_counts']=={'review_call_scope_unavailable_or_mismatch':1}
    assert timing['generation_id'] not in canonical(report)
