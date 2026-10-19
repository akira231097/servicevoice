"""Ten-caller optional read paths: fake network, real signed/local state."""
import asyncio
from types import SimpleNamespace
import threading

import httpx
import pytest

from app import jev_faq,receipt_narration
from test_telephony_api import factory
from test_jev_faq import classified
from test_receipt_narration import issue,EN


def callers(app):
    store=app.state.store;gateway=app.state.gemini_gateway
    values=[]
    for index in range(10):
        sid=store.create_session('live')['id'];cid=f'capacity-optional-{index}'
        with store.transaction() as db:db.execute('UPDATE sessions SET provider_call_id=? WHERE id=?',(cid,sid))
        gateway.store.pin_call_policy(sid,cid,'synthetic-assistant',{'VAPI_MODEL_PROFILE':'gemini-3.8-flash-custom'})
        values.append(SimpleNamespace(store=store,sid=sid,cid=cid,gateway=gateway,harness=app.state.harness,
                                     history=[{'role':'user','content':'What are your service hours?'}],serial=0))
    return values


@pytest.mark.parametrize('slow',[False,True])
def test_ten_simultaneous_faq_classifications_use_profile_pool_and_bounded_fallback(factory,monkeypatch,slow):
    monkeypatch.setenv('SERVICEVOICE_CAPACITY_PROFILE','demo10')
    _,app,_=factory();gateway=app.state.gemini_gateway;values=callers(app)
    original_client=httpx.AsyncClient;built=[]
    async def scenario():
        entered=0;peak=0;active=0;all_entered=asyncio.Event();closed=0
        async def fake_classifier(request):
            nonlocal entered,peak,active,closed
            active+=1;peak=max(peak,active);entered+=1
            if entered==10:all_entered.set()
            try:
                await asyncio.wait_for(all_entered.wait(),1)
                if slow:await asyncio.Event().wait()
                return httpx.Response(200,json=classified())
            finally:
                active-=1;closed+=1
        def build(*args,**kwargs):
            limits=kwargs['limits'];built.append((limits.max_connections,limits.max_keepalive_connections))
            return original_client(*args,transport=httpx.MockTransport(fake_classifier),**kwargs)
        monkeypatch.setattr(jev_faq.httpx,'AsyncClient',build)
        try:
            assert await gateway.faq.start()
            decisions=await asyncio.gather(*(gateway.faq.choose(v.sid,v.cid,v.history,'synthetic-key') for v in values))
            assert built==[(10,10)] and entered==peak==closed==10
            assert all(d.requests==1 for d in decisions)
            if slow:
                assert all(d.reason=='classification_deadline' and d.text is None for d in decisions)
                assert max(d.elapsed_ms for d in decisions)<700
            else:
                assert all(d.reason=='verified_static_faq' and 'service hours' in d.text for d in decisions)
            assert not gateway.faq.reads
            with app.state.store.read_connection() as db:
                assert db.execute('SELECT count(*) FROM appointments').fetchone()[0]==0
                assert db.execute('SELECT count(*) FROM tool_results').fetchone()[0]==0
        finally:await app.state.shutdown_resources()
    asyncio.run(scenario())


def test_ten_overlapping_receipt_readers_verify_actual_owned_fresh_proposals(factory,monkeypatch):
    monkeypatch.setenv('SERVICEVOICE_CAPACITY_PROFILE','demo10')
    _,app,_=factory();values=callers(app);gateway=app.state.gemini_gateway
    original_select=receipt_narration.select
    barrier=threading.Barrier(10);lock=threading.Lock();active=0;peak=0
    def simultaneous_select(*args):
        nonlocal active,peak
        with lock:active+=1;peak=max(peak,active)
        try:
            barrier.wait(timeout=2)
            return original_select(*args)
        finally:
            with lock:active-=1
    async def scenario():
        try:
            for index,env in enumerate(values):
                env.history=[{'role':'user','content':f'First available oil service for Caller {index}.'}]
                result=await issue(env,'prepare_first_available_booking',{'strategy':'first_available',
                    'service':'oil_change','customer_name':f'Caller {index}','phone':'5125550142',
                    'vehicle':'2021 Toyota Camry','presentation':EN})
                assert result['ok']
            monkeypatch.setattr(receipt_narration,'select',simultaneous_select)
            decisions=await asyncio.gather(*(gateway.narration.choose(v.sid,v.cid,v.history) for v in values))
            assert peak==10 and active==0
            for index,decision in enumerate(decisions):
                assert decision.text and f'Caller {index}' in decision.text
                assert 'confirm this booking' in decision.text
                assert decision.result_sha256
            assert not gateway.narration.pending
            with app.state.store.read_connection() as db:
                assert db.execute('SELECT count(*) FROM appointments').fetchone()[0]==0
                assert db.execute("SELECT count(*) FROM proposals WHERE state='pending'").fetchone()[0]==10
        finally:await app.state.shutdown_resources()
    asyncio.run(scenario())
