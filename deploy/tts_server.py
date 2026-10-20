"""Standalone candidate speech service; no conversation or scheduler imports.

Run separately from the unchanged production reasoning/tools image. This service
accepts approved text, returns bounded PCM, and records content-free synthesis
evidence. Generated/server-sent bytes never establish what a caller heard.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import hashlib
import os
from pathlib import Path
import re
import secrets
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.diagnostic_http import DiagnosticHTTPMiddleware
from app.diagnostics import NullDiagnostics, create_diagnostics, safe_attributes
from app.gemini_tts import DEFAULT_MODEL, MODELS, VOICES, GeminiTTS, TTSError, voice_request
from app.http_boundary import read_webhook_json

CONCURRENCY=4
_ATTRIBUTES=("model","voice","sample_rate","input_characters","audio_bytes","error_code","failure_phase",
    "provider","provider_call_id_status","turn_id_status","first_audio_ms","upstream_http_status",
    "upstream_sample_rate","resampled","resampler","audio_duration_ms","observation_scope")


def _validate(values):
    secret=values.get('GEMINI_TTS_BEARER_SECRET','');key=values.get('GEMINI_API_KEY','')
    if (not isinstance(secret,str) or not 32<=len(secret)<=512
            or any(ord(c)<33 or ord(c)>126 for c in secret)
            or not isinstance(key,str) or not 16<=len(key)<=512 or any(ord(c)<33 or ord(c)>126 for c in key)
            or secret==key
            or type(values.get('GEMINI_TTS_MODEL',DEFAULT_MODEL)) is not str
            or values.get('GEMINI_TTS_MODEL',DEFAULT_MODEL) not in MODELS
            or type(values.get('GEMINI_TTS_VOICE','Kore')) is not str
            or values.get('GEMINI_TTS_VOICE','Kore') not in VOICES):
        raise RuntimeError('Standalone speech configuration is incomplete or invalid.')


def create_app(env=None,*,adapter=None,diagnostics=None):
    """Isolated factory; configuration is frozen for this process's lifetime.

    Environment injection is for deployment/tests. No ambient dotenv, assistant
    changes, calls, model generations, database writes or network checks occur
    while constructing the app. Local pool/filter prewarm occurs at startup.
    """
    values=dict(os.environ if env is None else env)
    # Diagnostic configuration is an allowlisted projection, never all secrets
    # or unrelated production controller settings inherited from the process.
    diagnostic_settings={key:values[key] for key in ('SERVICEVOICE_ENVIRONMENT',
        'SERVICEVOICE_DIAGNOSTICS_ENABLED','GEMINI_TTS_MODEL','GEMINI_TTS_VOICE') if key in values}
    service=adapter if adapter is not None else GeminiTTS(lambda:values,max_concurrency=CONCURRENCY)
    initial=diagnostics if diagnostics is not None else NullDiagnostics('not_started')

    @asynccontextmanager
    async def lifespan(app):
        _validate(values)
        try:
            await service.start()  # Local HTTP pool + resampling prewarm; no paid speech request.
            if diagnostics is None:
                path=Path(values.get('SERVICEVOICE_TTS_DIAGNOSTICS_PATH','/data/tts-diagnostics.sqlite3'))
                app.state.diagnostics=await asyncio.to_thread(create_diagnostics,path,diagnostic_settings)
            app.state.ready=True
            yield
        finally:
            app.state.ready=False
            try:await service.close()
            finally:
                try:await asyncio.wait_for(asyncio.to_thread(app.state.diagnostics.close,2),timeout=2.5)
                except Exception:pass

    app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)
    app.state.ready=False;app.state.gemini_tts=service;app.state.diagnostics=initial

    def record(component,event,outcome,**fields):
        try:return app.state.diagnostics.record(component,event,outcome,**fields)
        except Exception:return False  # Evidence outages cannot change speech delivery.

    app.add_middleware(DiagnosticHTTPMiddleware,observer=record)

    @app.get('/healthz')
    async def health():
        return JSONResponse({'status':'ready' if app.state.ready else 'not_ready',
            'service':'servicevoice-tts','max_concurrency':CONCURRENCY,
            'upstream_credential_and_quota':'not_checked','caller_playback':'unknown'},
            status_code=200 if app.state.ready else 503,headers={'Cache-Control':'no-store'})

    @app.post('/tts/vapi')
    async def synthesize(request:Request):
        secret=values.get('GEMINI_TTS_BEARER_SECRET','')
        if not isinstance(secret,str) or not 32<=len(secret)<=512:
            raise HTTPException(503,'Speech is not configured.')
        authorizations=request.headers.getlist('authorization')
        if len(authorizations)!=1 or not secrets.compare_digest(authorizations[0].encode(),('Bearer '+secret).encode()):
            raise HTTPException(401,'Invalid speech authentication.')
        if not app.state.ready:raise HTTPException(503,'Speech is not ready.')
        payload=await read_webhook_json(request)  # Authentication precedes all body reads.
        call_id=None;fingerprint=None;terminal_observed=False
        try:
            text,_,call_id=voice_request(payload)
            if call_id:request.state.diagnostic_call_id=call_id
            fingerprint=hashlib.sha256(text.encode()).hexdigest()
            def observe(row):
                nonlocal terminal_observed
                # GeminiTTS emits only fixed diagnostic values. Re-project again
                # at the disk boundary; never retain text, audio or exceptions.
                event=row.get('event')
                if event in {'tts.completed','tts.failed','tts.cancelled'}:terminal_observed=True
                outcome=('failure' if event=='tts.failed' else 'cancelled' if event=='tts.cancelled'
                    else 'success' if event=='tts.completed' else 'started')
                request_id=row.get('request_id')
                start=row.get('started_at_unix')
                attributes=safe_attributes({key:row[key] for key in _ATTRIBUTES if key in row})
                attributes['input_sha256']=fingerprint
                record('tts',event,outcome,call_id=call_id or 'unbound_'+str(request_id),request_id=request_id,
                    start_unix=start,duration_ms=row.get('elapsed_ms') if start is not None else None,
                    attributes=attributes)
            return await service.response(payload,observer=observe)
        except TTSError as exc:
            # The adapter emits fixed codes. Never return upstream bodies or
            # raw exception text, including for an unexpected adapter failure.
            code=exc.code if isinstance(exc.code,str) and re.fullmatch(r'tts_[a-z0-9_]{1,100}',exc.code) else 'tts_unavailable'
            status=exc.status_code if type(exc.status_code) is int and 400<=exc.status_code<=599 else 502
            if not terminal_observed:
                request_id=getattr(request.state,'diagnostic_request_id',None)
                attributes={'error_code':code,'failure_phase':'validation' if status==400 else 'admission',
                    'observation_scope':'server_bytes_only_not_audible_playback'}
                if fingerprint is not None:attributes['input_sha256']=fingerprint
                record('tts','tts.failed','failure',call_id=call_id or 'unbound_'+str(request_id),request_id=request_id,
                    start_unix=time.time(),duration_ms=0,attributes=safe_attributes(attributes))
            raise HTTPException(status,code) from None

    return app


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',default='0.0.0.0')
    parser.add_argument('--port',type=int,default=8000)
    args=parser.parse_args(argv)
    if not 1<=args.port<=65535:parser.error('Port must be between one and 65535.')
    import uvicorn
    uvicorn.run(create_app(),host=args.host,port=args.port,workers=1,proxy_headers=False,
        access_log=False,server_header=False)


if __name__=='__main__':main()
