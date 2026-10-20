"""Public TTS accepts only its dedicated credential; console data stays private."""
import asyncio
import importlib

import dotenv
import httpx
import pytest
from starlette.responses import StreamingResponse

from app.gemini_tts import TTSError


@pytest.fixture
def application(tmp_path,monkeypatch):
    monkeypatch.setattr(dotenv,'load_dotenv',lambda *a,**k:False)
    monkeypatch.setattr(dotenv,'dotenv_values',lambda *a,**k:{})
    monkeypatch.setenv('SERVICEVOICE_DB',str(tmp_path/'ambient.sqlite3'))
    monkeypatch.setenv('GEMINI_TTS_BEARER_SECRET','speech-secret-'+('x'*40))
    monkeypatch.setenv('SERVICEVOICE_TELEPHONY_ENABLED','off')
    module=importlib.import_module('app.main');console=module.create_app(tmp_path/'app.sqlite3')
    return console,module.create_public_app(console)


def test_separate_tts_auth_is_required_before_provider_or_payload_work(application):
    console,public=application
    async def forbidden(*a,**k):raise AssertionError('provider called without speech auth')
    console.state.gemini_tts.response=forbidden
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public),base_url='http://testserver') as c:
            assert (await c.post('/tts/vapi',content=b'not even json')).status_code==401
            assert (await c.post('/tts/vapi',headers={'authorization':'Bearer gateway-secret'},json={})).status_code==401
            assert (await c.get('/api/observability-status')).status_code==404
            assert (await c.get('/api/sessions/other/diagnostics')).status_code==404
        await console.state.gemini_gateway.close();await console.state.gemini_tts.close()
    asyncio.run(run())


def test_upstream_speech_error_retains_explicit_status_without_details(application):
    console,public=application
    async def unavailable(*a,**k):raise TTSError('tts_deadline_exceeded',504)
    console.state.gemini_tts.response=unavailable
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public),base_url='http://testserver') as c:
            r=await c.post('/tts/vapi',headers={'authorization':'Bearer speech-secret-'+('x'*40)},json={'message':{}})
            assert r.status_code==504 and r.json()=={'detail':'tts_deadline_exceeded'}
        await console.state.gemini_gateway.close();await console.state.gemini_tts.close()
    asyncio.run(run())
