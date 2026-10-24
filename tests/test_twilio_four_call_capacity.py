"""Four separate callers share bounded pools, not a four-person conference."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading
from uuid import uuid4
import httpx
import pytest

from app.main import create_app, create_public_app
from app.twilio_bridge import BridgeError, BridgeStore
from app.listening_cues import ListeningPolicy
from app.jev_classifier import JEV_MODEL
from test_listening_cues import SAFE, TEXT, choice, setup, drain
from test_twilio_api import lane_app, post, Provider, ORG, PHONE


def test_eight_racing_callers_admit_exactly_four_across_store_instances(tmp_path):
    path=tmp_path/'bridges.sqlite3';BridgeStore(path)
    stores=[BridgeStore(path) for _ in range(8)];barrier=threading.Barrier(8)
    def admit(index):
        barrier.wait()
        try:return stores[index].allocate('CA'+f'{index:032x}')
        except BridgeError as error:return error.code
    with ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(admit,range(8)))
    assert sum(isinstance(row,dict) for row in results)==4
    assert results.count('bridge_capacity')==4
    assert len(BridgeStore(path).active())==4


def test_four_authenticated_calls_keep_distinct_sessions_and_fifth_is_refused(lane_app):
    async def run():
        lane=lane_app.state.twilio_lane;lane.config=replace(lane.config,listening=True)
        lane.client=Provider();lane.started=True
        calls=[]
        for index in range(4):
            row=lane.store.allocate('CA'+f'{index:032x}')
            calls.append({'id':str(uuid4()),'type':'inboundPhoneCall','orgId':ORG,'phoneNumberId':PHONE,
                'sip':{'headers':{'x-sv_bridge':row['id'],'x-sv_proof':lane.store.proof(lane.config,row)}}})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_public_app(lane_app)),base_url='https://test') as client:
            responses=await asyncio.gather(*(client.post('/webhooks/vapi/inbound',headers={'authorization':'Bearer '+'e'*32},
                json={'message':{'type':'assistant-request','call':call}}) for call in calls))
            assert [r.status_code for r in responses]==[200]*4
            for call,response in zip(calls,responses):call.update(response.json())
            sessions=[lane_app.state.telephony_ingress.resolve(call) for call in calls]
            assert len(set(sessions))==4 and len(lane.coordinator.calls)==4
            for call,session in zip(calls,sessions):
                assert lane.bindings[call['id']][1]==session
                assert lane.coordinator.calls[call['id']].session_id==session
            fifth=await post(client,lane,'voice',{'AccountSid':lane.config.account_sid,'CallSid':'CA'+'f'*32,
                'To':lane.config.phone_number,'Direction':'inbound'})
            assert fifth.status_code==200 and 'unavailable' in fifth.text and '<Hangup' in fifth.text
            assert len(lane.store.active())==4
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=lane_app),base_url='http://127.0.0.1') as client:
            readiness=(await client.get('/api/twilio/readiness')).json()
            assert readiness['max_concurrent_calls']==readiness['application_call_capacity']==readiness['listening_classifier_capacity']==4
            assert readiness['prototype_call_limit']==60
        await lane.close()
    asyncio.run(run())


def test_four_simultaneous_listening_decisions_do_not_starve_third_and_fourth():
    async def run():
        entered=asyncio.Event();release=asyncio.Event();calls=[]
        async def handler(request):
            calls.append(request)
            if len(calls)==4:entered.set()
            await release.wait()
            return httpx.Response(200,json={'model':JEV_MODEL,'answers':{
                'content':choice(('ordinary','excluded'),'ordinary',.99),
                'language':choice(('english','other','uncertain'),'english',.995)}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            policy=ListeningPolicy(client=client,connection_capacity=4)
            tasks=[asyncio.create_task(policy.choose(TEXT,'Tell me more.',SAFE,'fixture')) for _ in range(4)]
            try:
                await asyncio.wait_for(entered.wait(),.2)
                assert await policy.choose(TEXT,'Tell me more.',SAFE,'fixture')=='policy_unavailable'
            finally:release.set()
            assert await asyncio.gather(*tasks)==['approved']*4
            assert policy.active==0
    asyncio.run(run())


def test_interrupting_one_listener_does_not_cancel_other_three():
    async def run():
        c,clock,policy,sent,_,_,_=setup();policy.block=asyncio.Event()
        for index in range(1,4):
            assert c.register(f'call-{index}',f'session-{index}')
            state=c.calls[f'call-{index}'];state.assistant=False;state.assistant_stopped=0
            state.caller=True;state.caller_started=0
        assert not c.register('fifth','session-5')
        ids=list(c.calls)
        for cid in ids:c.event(cid,{'type':'transcript','timestamp':clock.wall()*1000,'role':'user',
            'transcriptType':'partial','transcript':TEXT})
        for _ in range(10):
            await asyncio.sleep(0)
            if policy.requests==4:break
        assert policy.requests==4
        c.retire(ids[0]);policy.block.set();await drain(c)
        assert set(sent)==set(ids[1:]) and len(sent)==3
        assert c.register('replacement','replacement-session')
        await c.close()
    asyncio.run(run())


def test_lifetime_quota_is_separate_bounded_and_durable(tmp_path):
    now=[1000];store=BridgeStore(tmp_path/'bridge.sqlite3',clock=lambda:now[0])
    for index in range(60):
        now[0]+=61;row=store.allocate('CA'+f'{index:032x}');store.end(row['id'],verified=True)
    assert store.active()==[]
    with pytest.raises(BridgeError,match='bridge_test_budget'):
        BridgeStore(store.path,clock=lambda:now[0]).allocate('CA'+'f'*32)


def test_lane_refuses_application_capacity_below_four(lane_app,monkeypatch,tmp_path):
    monkeypatch.setenv('SERVICEVOICE_MAX_LIVE_CALLS','2')
    with pytest.raises(ValueError,match='four live calls'):create_app(tmp_path/'undersized.sqlite3')
