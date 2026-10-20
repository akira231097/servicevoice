"""Runtime observations must preserve cancellation, authority and privacy."""
import asyncio
import json
import threading
import time
from uuid import uuid4

import httpx
import pytest

from app.diagnostic_http import DiagnosticHTTPMiddleware
from app.diagnostics import Diagnostics,turn_identifier
from app.harness import ToolHarness
from test_harness import store,block_execute
from test_gemini_gateway import setup,post,request_body,frame


class Sink:
    def __init__(self):self.events=[]
    def record(self,*args,**kwargs):self.events.append((args,kwargs));return True


def test_asgi_observer_leaves_body_and_exception_unchanged():
    async def run():
        observed=[];sent=[]
        async def application(scope,receive,send):
            assert await receive()=={'type':'http.request','body':b'private-payload'}
            scope['state']['diagnostic_call_id']='call-owned'
            await send({'type':'http.response.start','status':200,'headers':[]})
            await send({'type':'http.response.body','body':b'private-audio','more_body':True})
            raise asyncio.CancelledError()
        app=DiagnosticHTTPMiddleware(application,lambda *a,**k:observed.append((a,k)))
        async def receive():return {'type':'http.request','body':b'private-payload'}
        async def send(value):sent.append(value)
        with pytest.raises(asyncio.CancelledError):
            await app({'type':'http','path':'/tts/vapi'},receive,send)
        assert sent[-1]['body']==b'private-audio'
        [(args,kw)]=observed
        assert args==('tts','http_finished','cancelled')
        assert kw['call_id']=='call-owned' and kw['attributes']['http_status']==200
        assert kw['attributes']['response_complete'] is False
        assert 'private-' not in json.dumps(observed)
    asyncio.run(run())


def test_observer_failure_cannot_change_success_response():
    async def run():
        sent=[]
        async def application(scope,receive,send):
            await send({'type':'http.response.start','status':204,'headers':[]})
            await send({'type':'http.response.body','body':b''})
        def broken(*a,**k):raise OSError('private path')
        async def receive():raise AssertionError('observer read request')
        async def send(value):sent.append(value)
        await DiagnosticHTTPMiddleware(application,broken)({'type':'http','path':'/webhooks/vapi'},receive,send)
        assert sent[0]['status']==204
    asyncio.run(run())


def test_gateway_links_user_turn_and_model_request_without_copying_text(tmp_path):
    app,gateway,_=setup(tmp_path,lambda _:httpx.Response(200,headers={'content-type':'text/event-stream'},
        content=frame({'content':'A synthetic reply.'})+frame(reason='stop')+b'data: [DONE]\n\n'))
    sink=Sink();gateway.diagnostics=sink
    async def run():
        try:
            assert (await post(app,request_body())).status_code==200
            assert {args[0] for args,_ in sink.events}>={'gateway','llm'}
            linked=[kw for args,kw in sink.events if args[0]=='gateway']
            assert linked[0]['call_id']=='call-a'
            assert linked[0]['turn_id']==turn_identifier('call-a',1)
            assert linked[0]['request_id']
            assert 'A synthetic reply' not in json.dumps(sink.events)
        finally:await gateway.close()
    asyncio.run(run())


def test_pending_waiter_still_observes_actual_later_receipt(store,monkeypatch,tmp_path):
    sid=store.create_session()['id'];call_id=str(uuid4())
    started,release,_=block_execute(monkeypatch)
    sink=Sink()
    async def run():
        harness=ToolHarness(store,timeout_seconds=.03)
        context={'call_id':call_id,'turn_id':turn_identifier(call_id,1),'diagnostics':sink}
        try:
            pending=await harness.run(sid,'get_dealership_info',{},'slow-tool',diagnostic_context=context)
            assert pending['error']=='operation_pending'
            assert await asyncio.to_thread(started.wait,2)
            assert not [a for a,k in sink.events if a[0]=='scheduler']
            release.set();await harness.drain()
            [(args,kw)]=[(a,k) for a,k in sink.events if a[0]=='scheduler']
            assert args==('scheduler','receipt_observed','success')
            assert kw['operation_id']==pending['operation_id']
            assert kw['turn_id']==context['turn_id']
            assert kw['duration_ms']==0  # Receipt observation, not invented commit duration.
        finally:release.set();await harness.drain()
    asyncio.run(run())


def test_classifier_failure_survives_successful_model_fallback_and_model_end_is_exact(tmp_path):
    app,gateway,_=setup(tmp_path,lambda _:httpx.Response(500))
    sink=Sink();gateway.diagnostics=sink
    started=time.time()-2
    row={'request_id':uuid4().hex,'started_at_unix':started,'outcome':'completed','route':'gemini',
        'usage':{},'counts':{'jev_requests':1},'jev_reason':'classification_deadline',
        'durations_ms':{'jev_route':150},'milestones_ms':{'total':1800,'upstream_done':1600}}
    gateway._observe_timing(row,'sid',{'call_id':str(uuid4()),'llm_started_at':started+.2,'faq_started_at':started+.01})
    classifier=[(a,k) for a,k in sink.events if a[0]=='faq_router'][0]
    assert classifier[0][2]=='failure' and classifier[1]['attributes']['error_code']=='timeout'
    model=[k for a,k in sink.events if a[0]=='llm'][0]
    assert model['start_unix']==started+.2 and abs(model['duration_ms']-1400)<.01
    asyncio.run(gateway.close())
