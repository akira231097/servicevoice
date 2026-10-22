"""Offline multi-ledger review evidence; no cloud, models or provider calls."""
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

import pytest

from app import diagnostics as module
from app.diagnostics import (Diagnostics, REVIEW_DIAGNOSTICS_REVISION, canonical,
    read_call_diagnostics, read_review_diagnostics)
from app.live_evaluations import Worker
from test_live_evaluations import scenario, worker_fixture


def ledger(path,call_id,component,*,marker='business',start=None):
    owned=path.parent/('owned-'+marker);owned.mkdir(exist_ok=True)
    (owned/'fixture.py').write_text('authored_source='+repr(marker))
    sink=Diagnostics(path,{'SERVICEVOICE_ENVIRONMENT':'production'},source_root=owned)
    assert sink.record(component,'request_finished','success',call_id=call_id,
        start_unix=time.time()-10 if start is None else start,duration_ms=5,
        request_id=str(uuid4()),attributes={'transcript':'PRIVATE CANARY','headers':'PRIVATE SECRET'})
    assert sink.record(component,'request_finished','failure',call_id='other-call-private',duration_ms=1)
    assert sink.close()
    return read_call_diagnostics(path,call_id)


def fixture_paths(tmp_path):
    source=tmp_path/'scheduler.sqlite3'
    return source,source.with_name('scheduler-diagnostics.sqlite3'),tmp_path/'speech-diagnostics.sqlite3'


def copy_scoped_event(source,destination,call_id,*,conflict=False):
    key=module.call_key(call_id)
    with closing(sqlite3.connect(source)) as db:
        row=list(db.execute('SELECT * FROM diagnostic_events WHERE call_key=?',(key,)).fetchone())
        config=db.execute('SELECT * FROM diagnostic_configs WHERE config_id=?',(row[3],)).fetchone()
    if conflict:
        payload=json.loads(row[4]);payload['outcome']='failure';row[4]=canonical(payload)
    with closing(sqlite3.connect(destination)) as db,db:
        db.execute('DELETE FROM diagnostic_events WHERE call_key=?',(key,))
        db.execute('INSERT OR IGNORE INTO diagnostic_configs VALUES (?,?)',config)
        db.execute('INSERT INTO diagnostic_events VALUES (?,?,?,?,?)',row)


def test_independent_call_scoped_union_retains_roles_primary_configuration_and_privacy(tmp_path):
    source,business,speech=fixture_paths(tmp_path);call=str(uuid4())
    first=ledger(business,call,'gateway');second=ledger(speech,call,'tts',marker='speech')
    before={path:path.read_bytes() for path in (business,speech)}
    report=read_review_diagnostics(source,call,speech)
    assert report==read_review_diagnostics(source,call,speech)
    assert {row['component'] for row in report['events']}=={'gateway','tts'}
    assert report['configuration']==first['configuration']!=second['configuration']
    assert report['configuration_scope']=='business_runtime_only'
    assert report['coverage_revision']==REVIEW_DIAGNOSTICS_REVISION
    assert {record['id'] for record in report['configurations']}=={first['configuration']['id'],second['configuration']['id']}
    assert [entry['role'] for entry in report['sources']]==['business_runtime','configured_runtime']
    assert report['coverage']['state']=='observed' and report['coverage']['complete'] is False
    for entry in report['sources']:
        assert entry['coverage']['state']=='observed' and entry['exported_event_count']==1
    for row in report['events']:
        assert row['source_roles']==['business_runtime' if row['component']=='gateway' else 'configured_runtime']
    serialized=canonical(report)
    for forbidden in (str(tmp_path),str(business),str(speech),call,'other-call-private','PRIVATE CANARY','PRIVATE SECRET'):
        assert forbidden not in serialized
    assert all(path.read_bytes()==value for path,value in before.items())


@pytest.mark.parametrize('configured',[False,True])
def test_original_single_ledger_and_duplicate_path_are_read_once(tmp_path,monkeypatch,configured):
    source,business,_=fixture_paths(tmp_path);call=str(uuid4());original=ledger(business,call,'gateway')
    read=module.read_call_diagnostics;seen=[]
    def spy(path,*args):seen.append(path);return read(path,*args)
    monkeypatch.setattr(module,'read_call_diagnostics',spy)
    alias=business.parent/'.'/business.name if configured else None
    report=read_review_diagnostics(source,call,alias)
    assert len(seen)==len(report['sources'])==1
    assert report['configuration']==original['configuration']
    assert {key:value for key,value in report['events'][0].items() if key!='source_roles'}==original['events'][0]
    assert report['sources'][0]['also_configured'] is configured


@pytest.mark.parametrize('missing_role',['business','speech'])
@pytest.mark.parametrize('broken',[False,True])
def test_unavailable_ledger_never_discards_valid_sibling_or_substitutes_speech_config(tmp_path,missing_role,broken):
    source,business,speech=fixture_paths(tmp_path);call=str(uuid4())
    valid_path=speech if missing_role=='business' else business
    invalid_path=business if missing_role=='business' else speech
    ledger(valid_path,call,'tts' if missing_role=='business' else 'gateway')
    if broken:invalid_path.write_bytes(b'authored corrupt SQLite fixture')
    report=read_review_diagnostics(source,call,speech)
    assert len(report['events'])==1
    assert report['coverage']['state']=='incomplete'
    assert 'diagnostic_source_unavailable' in report['coverage']['reason_codes']
    assert (report['configuration'] is None)==(missing_role=='business')
    assert len(report['configurations'])==1
    if not broken:assert not invalid_path.exists()


@pytest.mark.parametrize('conflict',[False,True])
def test_duplicate_event_ids_are_deduplicated_or_rejected_without_choosing_truth(tmp_path,conflict):
    source,business,speech=fixture_paths(tmp_path);call=str(uuid4())
    ledger(business,call,'gateway');ledger(speech,call,'tts',marker='speech')
    copy_scoped_event(business,speech,call,conflict=conflict)
    report=read_review_diagnostics(source,call,speech)
    if conflict:
        assert report['events']==[] and report['coverage']['conflicting_event_id_count']==1
        assert report['coverage']['state']=='incomplete'
        assert 'conflicting_event_identity' in report['coverage']['reason_codes']
    else:
        assert len(report['events'])==1 and report['coverage']['exact_duplicate_events_ignored']==1
        assert report['events'][0]['source_roles']==['business_runtime','configured_runtime']


def test_conflicting_projected_configuration_identity_is_explicit_and_rejects_its_events(tmp_path,monkeypatch):
    source,business,speech=fixture_paths(tmp_path);call=str(uuid4())
    first=ledger(business,call,'gateway');second=deepcopy(first)
    second['configurations'][0]['source_sha256']='f'*64
    second['configuration']=second['configurations'][0]
    read=module.read_call_diagnostics
    monkeypatch.setattr(module,'read_call_diagnostics',lambda path,*args:second if path==speech.resolve() else read(path,*args))
    report=read_review_diagnostics(source,call,speech)
    assert report['events']==[] and report['configuration'] is None and report['configurations']==[]
    assert report['coverage']['state']=='incomplete'
    assert report['coverage']['conflicting_configuration_id_count']==1
    assert 'conflicting_configuration_identity' in report['coverage']['reason_codes']
    assert all(entry['configuration_ids'] and not entry['configurations'] for entry in report['sources'])


def test_combined_export_bound_and_each_source_loss_remain_explicit(tmp_path):
    source,business,speech=fixture_paths(tmp_path);call=str(uuid4());start=time.time()-10
    for index,path in enumerate((business,speech)):
        report=ledger(path,call,'gateway' if index==0 else 'tts',marker=str(index),start=start)
        template=report['events'][0]
        with closing(sqlite3.connect(path)) as db,db:
            for offset in range(1,1001):
                row={**template,'event_id':str(uuid4()),'start_unix':start+offset/100000}
                db.execute('INSERT INTO diagnostic_events VALUES (?,?,?,?,?)',
                    (row['event_id'],row['call_key'],row['start_unix'],row['config_id'],canonical(row)))
    assert len(read_review_diagnostics(source,call,speech)['events'])==2002  # default union cap is 6,000
    merged=read_review_diagnostics(source,call,speech,limit=2000)
    assert len(merged['events'])==merged['coverage']['event_count']==2000
    assert merged['coverage']['read_event_count']==2002 and merged['coverage']['union_events_omitted']==2
    assert merged['coverage']['omitted_webhook_delivery_events']==0
    assert merged['coverage']['truncated'] is True and merged['coverage']['state']=='incomplete'
    assert 'combined_event_limit_exceeded' in merged['coverage']['reason_codes']
    with closing(sqlite3.connect(speech)) as db,db:
        db.execute('UPDATE diagnostic_calls SET dropped=3 WHERE call_key=?',(module.call_key(call),))
    lost=read_review_diagnostics(source,call,speech,limit=4)
    assert lost['coverage']['dropped_events']==3
    assert lost['sources'][1]['coverage']['dropped_events']==3
    assert {'events_dropped','diagnostic_source_incomplete'}<=set(lost['coverage']['reason_codes'])
    assert len(lost['events'])==4 and lost['coverage']['truncated'] is True


def test_one_reader_failure_isolated_from_other_source(tmp_path,monkeypatch):
    source,business,speech=fixture_paths(tmp_path);call=str(uuid4());ledger(business,call,'gateway')
    read=module.read_call_diagnostics
    def fail_one(path,*args):
        if path==speech.resolve():raise sqlite3.OperationalError('PRIVATE ERROR')
        return read(path,*args)
    monkeypatch.setattr(module,'read_call_diagnostics',fail_one)
    merged=read_review_diagnostics(source,call,speech)
    assert len(merged['events'])==1 and merged['coverage']['state']=='incomplete'
    assert 'PRIVATE ERROR' not in canonical(merged)


def test_payload_call_key_must_match_selected_sql_scope_before_event_or_configuration_union(tmp_path):
    source,business,speech=fixture_paths(tmp_path);call=str(uuid4())
    original=ledger(business,call,'gateway');ledger(speech,call,'tts',marker='speech')
    with closing(sqlite3.connect(business)) as db,db:
        # The indexed scope still matches the requested call, but its persisted
        # payload claims a different call. SQL selection alone is insufficient.
        eid,payload=db.execute('SELECT event_id,payload FROM diagnostic_events WHERE call_key=?',(module.call_key(call),)).fetchone()
        row=json.loads(payload);row['call_key']='f'*64
        db.execute('UPDATE diagnostic_events SET payload=? WHERE event_id=?',(canonical(row),eid))
    merged=read_review_diagnostics(source,call,speech)
    assert [row['component'] for row in merged['events']]==['tts']
    assert merged['configuration'] is None
    assert original['configuration']['id'] not in {record['id'] for record in merged['configurations']}
    assert merged['coverage']['state']=='incomplete' and merged['coverage']['rejected_call_scope_events']==1
    assert 'call_scope_mismatch' in merged['coverage']['reason_codes']
    assert merged['sources'][0]['rejected_call_scope_events']==1
    assert merged['sources'][0]['configuration_ids']==[]
    assert 'f'*64 not in canonical(merged)


def test_worker_automatically_exports_both_real_ledgers_with_verified_children_and_keeps_history(scenario,tmp_path):
    store,call=scenario;source=Path(store.path)
    business=source.with_name(source.stem+'-diagnostics.sqlite3');speech=tmp_path/'speech-ledger.sqlite3'
    ledger(business,call['id'],'tool_harness');ledger(speech,call['id'],'tts',marker='speech')
    worker,mock,clock=worker_fixture(tmp_path,scenario)
    config=replace(worker.config,diagnostics_path=speech)
    worker=Worker(config,client=worker.client,clock=lambda:clock[0])
    source_before=source.read_bytes()
    assert worker.tick() and worker.outbox.status()['jobs']=={'complete':1}
    root=next(run for run in mock.runs.values() if run['name']=='voice.call.review')
    report=root['outputs']
    # Both ledgers reach the exported review with their source roles; events
    # that carry no turn, operation or synthesis identity stay listed in the
    # root rather than being guessed into a turn.
    assert {(e['component'],e['event'],tuple(e['source_roles'])) for e in report['diagnostics']['events']}=={
        ('tool_harness','request_finished',('business_runtime',)),('tts','request_finished',('configured_runtime',))}
    assert {entry['role'] for entry in report['diagnostics']['sources']}=={'business_runtime','configured_runtime'}
    assert report['diagnostics']['event_counts']=={'tool_harness.request_finished.success':1,'tts.request_finished.success':1}
    assert report['trace_export']['tts']==0 and report['trace_export']['tools']==0 and report['trace_export']['turns']==0
    assert report['trace_export']['omitted']==2 and len(report['3 Turn-by-turn timeline']['turns'])==2  # untimed turns stay in the root
    assert report['review_process']['diagnostics_coverage_revision']==REVIEW_DIAGNOSTICS_REVISION
    assert report['review_process']['diagnostics_source_sha256']==hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
    assert 'PRIVATE CANARY' not in canonical(mock.runs) and 'PRIVATE SECRET' not in canonical(mock.runs)
    assert source.read_bytes()==source_before and mock.judge_requests==1
    frozen=deepcopy(mock.runs)
    restarted=Worker(config,client=worker.client,clock=lambda:clock[0])
    assert not restarted.tick() and mock.judge_requests==1 and mock.runs==frozen
