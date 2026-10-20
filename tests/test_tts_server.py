"""Standalone speech deployment boundaries; all upstream audio is fictional."""
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import httpx
import pytest
from starlette.responses import StreamingResponse

from app.diagnostics import Diagnostics, NullDiagnostics, read_call_diagnostics
from app.gemini_tts import GeminiTTS, TTSError
from app.http_boundary import MAX_WEBHOOK_BYTES
from deploy.tts_server import CONCURRENCY, create_app

VALUES={'GEMINI_API_KEY':'fictional-provider-key-123456','GEMINI_TTS_BEARER_SECRET':'fictional-speech-secret-'+('x'*40)}
AUTH={'authorization':'Bearer '+VALUES['GEMINI_TTS_BEARER_SECRET']}


def envelope(text='Your booking reference is CSV-12345678.',call=None):
    message={'type':'voice-request','text':text,'sampleRate':16000}
    if call is not None:message['call']={'id':call}
    return {'message':message}


class Adapter:
    def __init__(self,error=None):self.started=0;self.closed=0;self.requests=0;self.error=error
    async def start(self):self.started+=1
    async def close(self):self.closed+=1
    async def response(self,payload,**kwargs):
        self.requests+=1
        if self.error:raise self.error
        async def pcm():yield b'\x01\x00\x02\x00'
        return StreamingResponse(pcm(),media_type='application/octet-stream')


def test_lifecycle_prewarms_locally_and_health_never_claims_credentials_quota_or_playback_verified():
    async def run():
        adapter=Adapter();app=create_app(VALUES,adapter=adapter,diagnostics=NullDiagnostics())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://sidecar') as client:
            assert (await client.get('/healthz')).status_code==503
            async with app.router.lifespan_context(app):
                assert adapter.started==1 and adapter.requests==0
                health=await client.get('/healthz')
                assert health.status_code==200 and health.json()['max_concurrency']==4
                assert health.json()['upstream_credential_and_quota']=='not_checked'
                assert health.json()['caller_playback']=='unknown' and adapter.requests==0
            assert adapter.closed==1 and (await client.get('/healthz')).status_code==503
    asyncio.run(run())


def test_only_two_explicit_routes_exist_and_speech_auth_precedes_body_reads():
    async def run():
        adapter=Adapter();app=create_app(VALUES,adapter=adapter,diagnostics=NullDiagnostics());reads=[]
        async def forbidden_body():
            reads.append(True);raise AssertionError('Unauthenticated body must not be consumed.')
            yield b''
        async with app.router.lifespan_context(app):
            assert {(r.path,tuple(sorted(r.methods))) for r in app.routes}=={('/healthz',('GET',)),('/tts/vapi',('POST',))}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://sidecar') as client:
                assert (await client.post('/tts/vapi',content=forbidden_body())).status_code==401
                assert (await client.post('/tts/vapi',headers={'authorization':'Bearer gateway-secret'},content=b'bad')).status_code==401
                assert (await client.post('/tts/vapi',headers=[*AUTH.items(),*AUTH.items()],content=b'bad')).status_code==401
                for route in ('/','/docs','/redoc','/openapi.json','/api/sessions','/webhooks/vapi','/llm/chat/completions'):
                    assert (await client.get(route)).status_code==404
        assert reads==[] and adapter.requests==0
    asyncio.run(run())


@pytest.mark.parametrize('changes',[{'GEMINI_API_KEY':''},{'GEMINI_TTS_BEARER_SECRET':'short'},
    {'GEMINI_TTS_BEARER_SECRET':'has whitespace '+('x'*40)},{'GEMINI_TTS_MODEL':'unexpected-model'},
    {'GEMINI_TTS_VOICE':'unexpected-voice'},{'GEMINI_API_KEY':'x'*40,'GEMINI_TTS_BEARER_SECRET':'x'*40}])
def test_invalid_configuration_fails_before_client_start_without_secret_disclosure(changes):
    async def run():
        values={**VALUES,**changes};adapter=Adapter();app=create_app(values,adapter=adapter,diagnostics=NullDiagnostics())
        with pytest.raises(RuntimeError,match='Standalone speech configuration') as error:
            async with app.router.lifespan_context(app):pass
        assert VALUES['GEMINI_API_KEY'] not in str(error.value)
        assert adapter.started==0 and not app.state.ready
    asyncio.run(run())


@pytest.mark.parametrize('kind,status',[('invalid_json',400),('declared_large',413),('streamed_large',413),('invalid_envelope',400)])
def test_authenticated_payload_bounds_and_invalid_requests_never_reach_provider(kind,status):
    async def run():
        adapter=Adapter();app=create_app(VALUES,adapter=adapter,diagnostics=NullDiagnostics())
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://sidecar') as client:
                if kind=='invalid_json':reply=await client.post('/tts/vapi',headers=AUTH,content=b'not-json')
                elif kind=='invalid_envelope':reply=await client.post('/tts/vapi',headers=AUTH,json={'message':{}})
                elif kind=='declared_large':reply=await client.post('/tts/vapi',headers={**AUTH,'content-length':str(MAX_WEBHOOK_BYTES+1)},content=b'{}')
                else:
                    async def chunks():
                        yield b'x'*(MAX_WEBHOOK_BYTES//2)
                        yield b'x'*(MAX_WEBHOOK_BYTES//2+1)
                    reply=await client.post('/tts/vapi',headers=AUTH,content=chunks())
                assert reply.status_code==status
        assert adapter.requests==0
    asyncio.run(run())


def test_startup_pool_and_resampler_failure_keeps_unready_and_closes_resources():
    class Fails(Adapter):
        async def start(self):raise TTSError('tts_resampler_unavailable',503)
    async def run():
        adapter=Fails();app=create_app(VALUES,adapter=adapter,diagnostics=NullDiagnostics())
        with pytest.raises(TTSError):
            async with app.router.lifespan_context(app):pass
        assert adapter.closed==1 and not app.state.ready
    asyncio.run(run())


def test_failed_sentence_is_a_content_free_fingerprint_with_unknown_playback(tmp_path):
    async def run():
        call=str(uuid4());text='PRIVATE CUSTOMER 5125550142 needs the corrected booking.'
        ledger=tmp_path/'tts.sqlite3';diagnostics=Diagnostics(ledger)
        adapter=Adapter(TTSError('tts_capacity_exhausted',503))
        app=create_app(VALUES,adapter=adapter,diagnostics=diagnostics)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://sidecar') as client:
                reply=await client.post('/tts/vapi',headers=AUTH,json=envelope(text,call))
                assert reply.status_code==503 and reply.json()=={'detail':'tts_capacity_exhausted'}
        report=read_call_diagnostics(ledger,call)
        failure=next(row for row in report['events'] if row['event']=='tts.failed')
        assert failure['attributes']['input_sha256']==hashlib.sha256(text.encode()).hexdigest()
        assert failure['attributes']['error_code']=='tts_capacity_exhausted'
        assert failure['attributes']['failure_phase']=='admission'
        assert failure['attributes']['observation_scope']=='server_bytes_only_not_audible_playback'
        assert report['coverage']['complete'] is False
        for private in (text,'5125550142',call,VALUES['GEMINI_API_KEY'],VALUES['GEMINI_TTS_BEARER_SECRET']):
            assert private not in json.dumps(report) and private.encode() not in ledger.read_bytes()
    asyncio.run(run())


def audio_event(pcm=b'\x01\x00\x02\x00'):
    value={'candidates':[{'content':{'parts':[{'inlineData':{'mimeType':'audio/L16;rate=16000',
        'data':base64.b64encode(pcm).decode()}}]}}]}
    return b'data: '+json.dumps(value).encode()+b'\n\n'


STOP=b'data: {"candidates":[{"finishReason":"STOP"}]}\n\n'


class GatedAudio(httpx.AsyncByteStream):
    def __init__(self,gate=None):self.gate=gate;self.closed=False
    async def __aiter__(self):
        yield audio_event()
        if self.gate is not None:await self.gate.wait()
        yield STOP
    async def aclose(self):self.closed=True


def test_four_parallel_synthesis_streams_succeed_fifth_is_bounded_and_no_provider_prewarm_call():
    async def run():
        gate=asyncio.Event();entered=asyncio.Event();requests=[];streams=[]
        def provider(request):
            requests.append(request);stream=GatedAudio(gate);streams.append(stream)
            if len(requests)==4:entered.set()
            return httpx.Response(200,headers={'content-type':'text/event-stream'},stream=stream)
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as upstream:
            adapter=GeminiTTS(lambda:VALUES,client=upstream,max_concurrency=CONCURRENCY)
            app=create_app(VALUES,adapter=adapter,diagnostics=NullDiagnostics())
            async with app.router.lifespan_context(app):
                assert len(requests)==0 and adapter.resampler_ready
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://sidecar') as client:
                    active=[asyncio.create_task(client.post('/tts/vapi',headers=AUTH,json=envelope())) for _ in range(4)]
                    try:
                        await asyncio.wait_for(entered.wait(),2)
                        fifth=await client.post('/tts/vapi',headers=AUTH,json=envelope())
                        assert fifth.status_code==503 and fifth.json()['detail']=='tts_capacity_exhausted'
                        assert len(requests)==4
                    finally:gate.set()
                    replies=await asyncio.wait_for(asyncio.gather(*active),2)
                    assert all(r.status_code==200 and r.content==b'\x01\x00\x02\x00' for r in replies)
                    assert all(s.closed for s in streams) and not adapter.capacity.locked()
                    assert len(requests)==4
    asyncio.run(run())


def test_successful_speech_preserves_adapter_timing_and_hides_unrelated_body_content(tmp_path):
    async def run():
        ledger=tmp_path/'tts.sqlite3';sink=Diagnostics(ledger);call=str(uuid4())
        stream=GatedAudio()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,
            headers={'content-type':'text/event-stream'},stream=stream))) as upstream:
            adapter=GeminiTTS(lambda:VALUES,client=upstream);app=create_app(VALUES,adapter=adapter,diagnostics=sink)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://sidecar') as client:
                    reply=await client.post('/tts/vapi',headers=AUTH,json=envelope(call=call))
                    assert reply.status_code==200 and reply.headers['content-type']=='application/octet-stream'
                    assert reply.headers['cache-control']=='no-store'
        report=read_call_diagnostics(ledger,call)
        synthesis=[r for r in report['events'] if r['event'].startswith('tts.')]
        assert {r['event'] for r in synthesis}>={'tts.request_started','tts.first_audio_ready','tts.completed'}
        assert len({r['start_unix'] for r in synthesis})==1
        assert all(r['duration_ms'] is not None and r['duration_ms']>=0 for r in synthesis)
        complete=next(r for r in synthesis if r['event']=='tts.completed')
        assert complete['outcome']=='success' and complete['attributes']['audio_bytes']==4
        assert complete['attributes']['observation_scope']=='server_bytes_only_not_audible_playback'
        assert complete['attributes']['provider_call_id_status']=='provider_reported'
        assert report['configuration']['provider_effective_configuration']=='unknown'
        assert 'CSV-12345678' not in json.dumps(report)
    asyncio.run(run())


def test_observer_failure_cannot_modify_audio_or_speech_outcome():
    class BrokenSink(NullDiagnostics):
        def record(self,*a,**kw):raise RuntimeError('fictional diagnostic outage')
    async def run():
        adapter=Adapter();app=create_app(VALUES,adapter=adapter,diagnostics=BrokenSink())
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://sidecar') as client:
                reply=await client.post('/tts/vapi',headers=AUTH,json=envelope())
                assert reply.status_code==200 and reply.content==b'\x01\x00\x02\x00'
    asyncio.run(run())


def test_partial_audio_failure_is_not_silently_completed_or_retried(tmp_path):
    class TruncatedAudio(httpx.AsyncByteStream):
        async def __aiter__(self):yield audio_event()  # Missing authoritative STOP.
        async def aclose(self):self.closed=True
    async def run():
        ledger=tmp_path/'tts.sqlite3';sink=Diagnostics(ledger);call=str(uuid4());requests=[];stream=TruncatedAudio()
        def provider(request):
            requests.append(request)
            return httpx.Response(200,headers={'content-type':'text/event-stream'},stream=stream)
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as upstream:
            adapter=GeminiTTS(lambda:VALUES,client=upstream);app=create_app(VALUES,adapter=adapter,diagnostics=sink)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://sidecar') as client:
                    with pytest.raises(TTSError,match='tts_upstream_incomplete_generation'):
                        await client.post('/tts/vapi',headers=AUTH,json=envelope(call=call))
            assert len(requests)==1 and stream.closed and not adapter.capacity.locked()
        report=read_call_diagnostics(ledger,call)
        assert not any(row['event']=='tts.completed' for row in report['events'])
        failed=next(row for row in report['events'] if row['event']=='tts.failed')
        assert failed['attributes']['failure_phase']=='after_first_audio'
        assert failed['attributes']['error_code']=='tts_upstream_incomplete_generation'
        assert failed['attributes']['input_sha256']==hashlib.sha256(envelope()['message']['text'].encode()).hexdigest()
        http=next(row for row in report['events'] if row['event']=='http_finished')
        assert http['outcome']=='failure' and http['attributes']['response_complete'] is False
    asyncio.run(run())


def test_standalone_factory_and_startup_never_import_conversation_or_business_controller():
    code='''
import asyncio,sys
from deploy.tts_server import create_app
from app.diagnostics import NullDiagnostics
class Adapter:
    async def start(self):pass
    async def close(self):pass
app=create_app({'GEMINI_API_KEY':'fictional-provider-key-123456','GEMINI_TTS_BEARER_SECRET':'x'*40},adapter=Adapter(),diagnostics=NullDiagnostics())
async def check():
    async with app.router.lifespan_context(app):
        for name in ('app.main','app.domain','app.gemini_gateway','app.harness','app.tools'):
            assert name not in sys.modules,name
asyncio.run(check())
'''
    result=subprocess.run([sys.executable,'-c',code],cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
