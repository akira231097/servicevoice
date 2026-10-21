"""Configuration and atomic shared browser/phone reservations; no providers."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.capacity import CapacityConfig,CapacityError,LiveAdmission
from app.domain import Store
from app.telephony_ingress import TelephonyIngress,IngressLimits,IngressError
from test_telephony_ingress import config,message,AUTH,issued_call
from test_telephony_api import factory,BROWSER


def test_profiles_keep_tool_work_bounded_and_raise_only_voice_capacities():
    normal=CapacityConfig.from_mapping({});ten=CapacityConfig.from_mapping({'SERVICEVOICE_CAPACITY_PROFILE':'demo10'})
    assert normal.max_live_calls==normal.gateway_concurrency==normal.phone_workers==4
    assert ten.max_live_calls==ten.gateway_concurrency==ten.phone_workers==10
    assert ten.phone_http_capacity==16 and ten.phone_admissions_per_minute==20
    assert ten.public_limits()['tool_workers']==4 and ten.public_limits()['tool_pending']==64


@pytest.mark.parametrize('key,value',[
 ('CAPACITY_PROFILE','ten'),('CAPACITY_PROFILE',''),('MAX_LIVE_CALLS','0'),('MAX_LIVE_CALLS','11'),
 ('MAX_LIVE_CALLS',True),('MAX_LIVE_CALLS','+10'),('MAX_LIVE_CALLS','010'),('GATEWAY_CONCURRENCY','17'),
 ('GATEWAY_CONCURRENCY','3'),('PHONE_HTTP_CAPACITY','33'),('PHONE_HTTP_CAPACITY','3'),
 ('PHONE_WORKERS','3'),('PHONE_ADMISSIONS_PER_MINUTE','121'),('PHONE_ADMISSIONS_PER_MINUTE','3')])
def test_invalid_configuration_fails_closed(key,value):
    with pytest.raises(ValueError):CapacityConfig.from_mapping({'SERVICEVOICE_'+key:value})


def test_combined_browser_phone_race_admits_exactly_ten_and_replay_uses_no_slot(tmp_path,config):
    store=Store(tmp_path/'mixed.sqlite3');limits=CapacityConfig.from_mapping({'SERVICEVOICE_CAPACITY_PROFILE':'demo10'})
    live=LiveAdmission(store,limits)
    # Keep all16 requests concurrent: this proves the shared atomic ceiling,
    # not16 fsync operations within a hosted Windows runner's one-second limit.
    # Dedicated deadline tests below retain and exercise the production1s bound.
    assert live.deadline_seconds==IngressLimits().deadline_seconds==1
    live.deadline_seconds=5
    phone=TelephonyIngress(store,config,live_admission=live,
        limits=IngressLimits(active_calls=10,workers=16,busy_seconds=.5,deadline_seconds=5))
    barrier=threading.Barrier(16);requests=[message() for _ in range(8)]
    def compete(index):
        barrier.wait(timeout=5)
        try:
            result=live.create_browser_session() if index<8 else phone.admit(requests[index-8],AUTH)
            return index,result
        except (CapacityError,IngressError) as exc:return index,exc.code
    with ThreadPoolExecutor(max_workers=16) as pool:outcomes=list(pool.map(compete,range(16)))
    accepted=[item for item in outcomes if isinstance(item[1],dict)]
    assert len(accepted)==10, {'accepted':len(accepted),
        'rejected':[result for _,result in outcomes if isinstance(result,str)]}
    assert all(result=='live_call_capacity' for _,result in outcomes if isinstance(result,str))
    with store.read_connection() as db:assert live.count(db)==10
    for index,result in accepted:
        if index>=8:assert phone.admit(requests[index-8],AUTH)==result
    with store.read_connection() as db:assert live.count(db)==10
    # Reconstructing the controller does not reset the durable limit.
    with pytest.raises(CapacityError):LiveAdmission(store,limits).create_browser_session()


def test_abandoned_browser_reservation_expires_and_cannot_bind_late(factory,monkeypatch):
    monkeypatch.setenv('SERVICEVOICE_CAPACITY_PROFILE','demo10')
    _,app,_=factory()
    live=app.state.live_admission;moment=live.clock();monkeypatch.setattr(live,'clock',lambda:moment)
    with TestClient(app) as client:
        sessions=[client.post('/api/sessions',json={'mode':'live'}).json() for _ in range(10)]
        assert client.post('/api/sessions',json={'mode':'live'}).status_code==429
        monkeypatch.setattr(live,'clock',lambda:moment+121)
        fresh=client.post('/api/sessions',json={'mode':'live'});assert fresh.status_code==200
        old=sessions[0]
        with pytest.raises(CapacityError,match='live_reservation_expired'):
            app.state.bind_provider_call({'id':'late-call','assistantId':BROWSER,'assistantOverrides':{'variableValues':{'session_id':old['id'],'session_token':old['token']}}})
        with app.state.store.read_connection() as db:
            assert db.execute('SELECT provider_call_id FROM sessions WHERE id=?',(old['id'],)).fetchone()[0] is None


def test_bound_browser_reservation_survives_short_timeout_then_end_releases_slot(factory,monkeypatch):
    monkeypatch.setenv('SERVICEVOICE_CAPACITY_PROFILE','demo10')
    _,app,_=factory();live=app.state.live_admission;moment=live.clock();monkeypatch.setattr(live,'clock',lambda:moment)
    with TestClient(app) as client:
        session=client.post('/api/sessions',json={'mode':'live'}).json()
        call={'id':'bound-call','assistantId':BROWSER,'assistantOverrides':{'variableValues':{'session_id':session['id'],'session_token':session['token']}}}
        assert app.state.bind_provider_call(call)==session['id']
        monkeypatch.setattr(live,'clock',lambda:moment+121)
        with app.state.store.read_connection() as db:assert live.count(db)==1
        assert client.post('/api/sessions/'+session['id']+'/end',headers={'x-session-token':session['token']}).status_code==200
        with app.state.store.read_connection() as db:assert live.count(db)==0


def test_runtime_configuration_is_frozen_and_safe_read_lanes_cover_ten(factory,monkeypatch):
    monkeypatch.setenv('SERVICEVOICE_CAPACITY_PROFILE','demo10')
    _,app,_=factory()
    monkeypatch.setenv('SERVICEVOICE_CAPACITY_PROFILE','default')
    gateway=app.state.gemini_gateway
    assert app.state.capacity.max_live_calls==10 and gateway.limits.concurrency==10
    assert app.state.telephony_http_capacity==16 and app.state.telephony_ingress.limits.active_calls==10
    assert gateway.narration.capacity==gateway.faq.capacity==gateway.faq.connection_capacity==10
    assert app.state.harness.semaphore._value==4 and app.state.harness.max_pending==64
    with TestClient(app) as client:assert client.get('/api/config').json()['capacity_limits']['profile']=='demo10'


def test_queued_phone_abandonment_cannot_admit_after_writer_becomes_free(tmp_path,config):
    store=Store(tmp_path/'abandoned.sqlite3');live=LiveAdmission(store,CapacityConfig())
    phone=TelephonyIngress(store,config,live_admission=live)
    waiting=threading.Event();abandoned=threading.Event();lock=live.writer_lock
    class ObservedLock:
        def acquire(self,**kwargs):
            waiting.set()
            return lock.acquire(**kwargs)
        def release(self):lock.release()
    phone._writer=ObservedLock()
    lock.acquire()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(phone.admit,message(),AUTH,abandoned=abandoned)
        try:
            assert waiting.wait(2)
            abandoned.set()
        finally:lock.release()
        with pytest.raises(IngressError,match='telephony_deadline'):future.result(timeout=2)
    with store.read_connection() as db:
        for table in ('sessions','telephony_bindings','provider_tool_policies'):
            assert db.execute('SELECT count(*) FROM '+table).fetchone()[0]==0


@pytest.mark.parametrize('invalid', ['ended','expired','policy','identity','assistant','terminal'])
def test_readonly_admission_replay_bypasses_writer_but_revalidates(tmp_path,config,invalid):
    store=Store(tmp_path/'replay.sqlite3');moment=[time.time()]
    live=LiveAdmission(store,CapacityConfig(),clock=lambda:moment[0])
    phone=TelephonyIngress(store,config,live_admission=live,clock=lambda:moment[0])
    req=message();original=phone.admit(req,AUTH)
    sid=original['assistantOverrides']['variableValues']['session_id']
    with live.writer_lock:
        assert phone.admit(req,AUTH)==original
        with store.transaction() as db:
            if invalid=='ended':db.execute('UPDATE sessions SET ended=1 WHERE id=?',(sid,))
            elif invalid=='expired':moment[0]+=phone.limits.session_ttl_seconds
            elif invalid=='policy':db.execute("UPDATE provider_tool_policies SET policy='native'")
            elif invalid=='identity':req['call']['phoneNumberId']='44444444-4444-4444-8444-444444444444'
            elif invalid=='assistant':req['assistant']={'id':'44444444-4444-4444-8444-444444444444'}
            else:req['call']['status']='ended'
        with pytest.raises(IngressError) as error:phone.admit(req,AUTH)
        assert error.value.code in {'telephony_call_ended','telephony_call_expired','telephony_binding_conflict',
                                    'telephony_identity_mismatch','telephony_assistant_mismatch'}
    with store.read_connection() as db:assert db.execute('SELECT count(*) FROM sessions').fetchone()[0]==1


def test_browser_writer_wait_is_bounded_and_does_not_insert(tmp_path):
    store=Store(tmp_path/'browser-wait.sqlite3');live=LiveAdmission(store,CapacityConfig())
    live.deadline_seconds=.04
    with live.writer_lock:
        began=time.monotonic()
        with pytest.raises(CapacityError,match='live_admission_deadline'):live.create_browser_session()
        assert time.monotonic()-began<.5
    with store.read_connection() as db:assert db.execute('SELECT count(*) FROM sessions').fetchone()[0]==0
    assert live.create_browser_session()['mode']=='live'


def test_browser_deadline_after_partial_insert_rolls_back(tmp_path,monkeypatch):
    store=Store(tmp_path/'browser-rollback.sqlite3');ticks=[0.0]
    live=LiveAdmission(store,CapacityConfig(),monotonic=lambda:ticks[0])
    original_clock=live.clock
    # created is read only while assembling the INSERT arguments; expiration
    # after that read must be caught before COMMIT and leave no reservation.
    def expire_at_insert():
        ticks[0]=2
        return original_clock()
    original_require=live.require_room
    def room_then_expire(db):
        original_require(db)
        live.clock=expire_at_insert
    monkeypatch.setattr(live,'require_room',room_then_expire)
    with pytest.raises(CapacityError,match='live_admission_deadline'):live.create_browser_session()
    with store.read_connection() as db:assert db.execute('SELECT count(*) FROM sessions').fetchone()[0]==0


def test_phone_read_then_write_keeps_original_worker_budget(tmp_path,config,monkeypatch):
    store=Store(tmp_path/'original-budget.sqlite3');ticks=[0.0]
    phone=TelephonyIngress(store,config,monotonic=lambda:ticks[0])
    transaction=phone._transaction;insert=phone._insert_binding
    @contextmanager
    def slow_read(deadline,**kwargs):
        with transaction(deadline,**kwargs) as value:yield value
        if kwargs.get('write') is False:ticks[0]=.75
    def late_insert(*args):
        insert(*args)
        ticks[0]=1.1
    monkeypatch.setattr(phone,'_transaction',slow_read)
    monkeypatch.setattr(phone,'_insert_binding',late_insert)
    # A six-second HTTP envelope cannot renew the original one-second worker
    # budget when the preliminary read transitions to atomic admission.
    with pytest.raises(IngressError,match='telephony_deadline'):phone.admit(message(),AUTH,deadline=6)
    with store.read_connection() as db:
        for table in ('sessions','telephony_bindings','provider_tool_policies'):
            assert db.execute('SELECT count(*) FROM '+table).fetchone()[0]==0


@pytest.mark.parametrize('callers',[4,10])
def test_offline_caller_scenario_preserves_end_to_end_invariants(tmp_path,callers):
    import asyncio
    from scripts.stress_ten_callers import exercise
    # Sequential caller setup precedes concurrent model/tool/booking checks;
    # this is not simultaneous-admission or hosted-runner disk performance.
    # Dedicated ingress tests retain the unchanged one-second default budget.
    result=asyncio.run(exercise(tmp_path,callers=callers,offline_fixture_deadline=5))
    assert result['passed'] and result['peak_fake_upstream_header_requests']==callers
    assert result['confirmed_appointments']==1 and result['losing_capacity_conflicts']==callers-1
    assert result['exercise_scope']=='offline_correctness_fixture_not_default_deadline_performance'
    assert result['offline_fixture_deadline_seconds']==5
    assert result['production_default_ingress_deadline_seconds']==IngressLimits().deadline_seconds==1
    assert result['production_default_tool_wait_seconds']==1.5
    assert result['fixture_tool_wait_seconds']==30 and result['fixture_signature_writes_serialized'] is True
    assert result['fixture_admission_requests_serialized'] is True
    assert result['admission_setup']=='sequential_fixture_setup'
    assert result['admission_event_retries']==0
    assert all(result['checks'].values())


def test_contended_admission_keeps_default_deadline_failure_visible_but_isolates_correctness_fixture(tmp_path,monkeypatch):
    import asyncio
    from scripts.stress_ten_callers import exercise
    original=TelephonyIngress._insert_binding
    admit=TelephonyIngress.admit;counts={'active':0,'peak':0};lock=threading.Lock()
    def observed_admit(self,*args,**kwargs):
        with lock:
            counts['active']+=1;counts['peak']=max(counts['peak'],counts['active'])
        try:return admit(self,*args,**kwargs)
        finally:
            with lock:counts['active']-=1
    def contended_insert(self,*args,**kwargs):
        # Ten serialized fixture writes exceed one second deterministically,
        # reproducing a slow hosted disk without changing a production timer.
        time.sleep(.14)
        return original(self,*args,**kwargs)
    monkeypatch.setattr(TelephonyIngress,'_insert_binding',contended_insert)
    monkeypatch.setattr(TelephonyIngress,'admit',observed_admit)
    strict={}
    with pytest.raises(AssertionError,match='statuses=.*504.*telephony_deadline'):
        asyncio.run(exercise(tmp_path/'strict',strict,callers=10))
    assert strict['exercise_scope']=='strict_default_deadline_offline_stress'
    assert strict['offline_fixture_deadline_seconds'] is None
    assert strict['production_default_ingress_deadline_seconds']==1
    assert strict['production_default_tool_wait_seconds']==1.5
    assert strict['fixture_tool_wait_seconds'] is None and strict['fixture_signature_writes_serialized'] is False
    assert strict['fixture_admission_requests_serialized'] is False
    assert strict['admission_setup']=='simultaneous_default_deadline_admission'
    assert counts['peak']>1 and counts['active']==0
    assert 504 in strict['admission_statuses'] and 'telephony_deadline' in strict['admission_errors']
    counts['peak']=0
    fixture=asyncio.run(exercise(tmp_path/'fixture',callers=10,offline_fixture_deadline=5))
    assert fixture['admission_statuses']=={'200':10} and fixture['admission_event_retries']==0
    assert fixture['confirmed_appointments']==1 and fixture['losing_capacity_conflicts']==9
    assert fixture['peak_fake_upstream_header_requests']==10 and all(fixture['checks'].values())
    assert fixture['production_default_tool_wait_seconds']==1.5
    assert fixture['fixture_tool_wait_seconds']==30 and fixture['fixture_signature_writes_serialized'] is True
    assert fixture['fixture_admission_requests_serialized'] is True and counts['peak']==1 and counts['active']==0
    assert fixture['admission_setup']=='sequential_fixture_setup'
    assert IngressLimits().deadline_seconds==1


def test_fixture_signature_storage_failure_remains_visible_without_replay(tmp_path,monkeypatch):
    """The CI missing-command symptom must never be treated as tool success."""
    import asyncio
    import sqlite3
    from app.gemini_gateway import SignatureStore
    from scripts.stress_ten_callers import exercise
    calls=[]
    def unavailable(self,*args,**kwargs):
        calls.append(args[1])
        raise sqlite3.OperationalError('synthetic signature writer unavailable')
    monkeypatch.setattr(SignatureStore,'save_group',unavailable)
    progress={}
    with pytest.raises(AssertionError,match='signed synthetic command.*generation_unavailable'):
        asyncio.run(exercise(tmp_path,progress,callers=4,offline_fixture_deadline=5))
    assert 1<=len(calls)<=4 and len(set(calls))==len(calls), 'A failed signed command must never be regenerated or replayed'
    assert progress['last_phase']=='synchronized_gateway'
    assert progress['gateway_command_failures']
    assert all(row['public_errors']==['generation_unavailable'] for row in progress['gateway_command_failures'])
    with Store(tmp_path/'exercise.sqlite3').read_connection() as db:
        assert db.execute('SELECT COUNT(*) FROM appointments').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM tool_results').fetchone()[0]==0


def test_fixture_budget_does_not_retry_or_mask_admission_failure(tmp_path,monkeypatch):
    import asyncio
    from scripts.stress_ten_callers import exercise
    calls=[]
    def unavailable(self,*args,**kwargs):
        calls.append(1)
        raise IngressError('telephony_busy',503)
    monkeypatch.setattr(TelephonyIngress,'_insert_binding',unavailable)
    progress={}
    with pytest.raises(AssertionError,match='statuses=.*503.*telephony_busy'):
        asyncio.run(exercise(tmp_path,progress,callers=10,offline_fixture_deadline=5))
    assert len(calls)==10 and progress['admission_statuses']==[503]*10
    with Store(tmp_path/'exercise.sqlite3').read_connection() as db:
        assert db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM appointments').fetchone()[0]==0


@pytest.mark.parametrize('deadline',[True,0,-1,6,float('nan'),float('inf'),'5'])
def test_offline_fixture_deadline_is_explicit_and_bounded(tmp_path,deadline):
    import asyncio
    from scripts.stress_ten_callers import exercise
    with pytest.raises(ValueError,match='Offline fixture deadline'):
        asyncio.run(exercise(tmp_path,offline_fixture_deadline=deadline))
