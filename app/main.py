from __future__ import annotations

import hashlib
import asyncio
import json
import os
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .domain import DomainError, SERVICES, Store, today
from .rehearsal import respond
from .tools import DEFINITIONS, SYSTEM_PROMPT_VERSION, MAX_TOOL_BATCH
from .harness import ToolHarness
from .http_boundary import read_webhook_json
from .gemini_gateway import GatewayError, GatewayLimits, install_gateway
from .tool_observations import ObservationError
from .receipt_narration import NARRATOR_VERSION
from .jev_faq import FAQ_VERSION
from .jev_classifier import JEV_MODEL
from .advisor_api import install_advisor_api
from .telephony_ingress import IngressConfig, IngressError, IngressLimits, TelephonyIngress
from .capacity import CapacityConfig, CapacityError, LiveAdmission
from .observability import NullTelemetry, TelemetryConfig, create_telemetry, NullLocalTimingSink, create_local_timing_sink
from .diagnostics import NullDiagnostics, create_diagnostics, operation_identifier, turn_identifier
from .gemini_tts import GeminiTTS, TTSError
from .diagnostic_http import DiagnosticHTTPMiddleware
from .twilio_api import install_twilio, TWILIO_PATHS
from .twilio_bridge import BridgeError

ROOT=Path(__file__).resolve().parent.parent
load_dotenv(ROOT/".env")
TELEPHONY_HTTP_TIMEOUT_SECONDS=6.0

class Message(BaseModel):
    model_config=ConfigDict(extra="forbid")
    text:str=Field(min_length=1,max_length=2000)

class NewSession(BaseModel):
    mode:str="rehearsal"

class ToolRequest(BaseModel):
    model_config=ConfigDict(extra="forbid")
    name:str=Field(max_length=100)
    arguments:dict
    call_id:str=Field(min_length=1,max_length=200)

def _tool_diagnostic_context(diagnostics,call_id,tool_id,contexts):
    """Use existing authorization metadata only; no extra database/network read."""
    try:
        value=contexts.get(tool_id,{}) if isinstance(contexts,dict) else {}
        ordinal=value.get("user_count") if isinstance(value,dict) else None
        return {"call_id":call_id,"turn_id":turn_identifier(call_id,ordinal),"diagnostics":diagnostics}
    except Exception:
        return None  # Missing diagnostic linkage cannot change an accepted effect.

def create_app(db_path=None):
    from dotenv import dotenv_values
    capacity=CapacityConfig.from_mapping({**dotenv_values(ROOT/".env"),**os.environ})
    configured=Path(db_path or os.getenv("SERVICEVOICE_DB","../../work/runtime/servicevoice.sqlite3"))
    store=Store(configured if configured.is_absolute() else ROOT/configured)
    async def startup_resources():
        async with app.state.telemetry_lock:
            if not app.state.telemetry_initialized:
                try:
                    def initialize_telemetry():
                        config=TelemetryConfig.from_env(settings())
                        budget_path=Path(config.budget_path)
                        if not budget_path.is_absolute():
                            config=replace(config,budget_path=str(ROOT/budget_path))
                        return create_telemetry(config)
                    telemetry=await run_in_threadpool(initialize_telemetry)
                except Exception:
                    telemetry=NullTelemetry()
                app.state.telemetry=telemetry
                app.state.harness.telemetry=telemetry
                app.state.gemini_gateway.telemetry=telemetry
                timing_sink=NullLocalTimingSink()
                if settings().get("SERVICEVOICE_TIMING_PERSISTENCE", "off") == "on":
                    try:
                        booking_path=Path(store.path)
                        timing_path=booking_path.with_name(booking_path.stem+"-gateway-timings.sqlite3")
                        timing_sink=await run_in_threadpool(create_local_timing_sink,timing_path)
                    except Exception:
                        pass  # Optional diagnostic storage never prevents startup.
                app.state.local_timings=timing_sink
                app.state.gemini_gateway.timing_sink=timing_sink
                diagnostic_path=Path(store.path).with_name(Path(store.path).stem+"-diagnostics.sqlite3")
                try:
                    diagnostics=await run_in_threadpool(create_diagnostics,diagnostic_path,settings())
                except Exception:
                    diagnostics=NullDiagnostics("initialization_failed")
                app.state.diagnostics=diagnostics
                app.state.gemini_gateway.diagnostics=diagnostics
                app.state.telemetry_initialized=True
        await app.state.gemini_gateway.start()
        await app.state.gemini_tts.start()
        if app.state.twilio_lane is not None:
            await app.state.twilio_lane.start(settings())
    async def shutdown_resources():
        app.state.telephony_http_closing=True
        pending=list(app.state.telephony_http_tasks)
        if pending:
            try:await asyncio.wait_for(asyncio.shield(asyncio.gather(*pending,return_exceptions=True)),timeout=6)
            except asyncio.TimeoutError:pass  # Do not cancel accepted synchronous effects.
        if app.state.twilio_lane is not None:
            await app.state.twilio_lane.close()
        try:
            await app.state.gemini_gateway.close()
        finally:
            await app.state.gemini_tts.close()
            try:await asyncio.wait_for(asyncio.shield(app.state.harness.close()),timeout=5)
            except asyncio.TimeoutError:pass  # Durable journal reconciles unfinished accepted operations.
            finally:
                try:await asyncio.wait_for(asyncio.to_thread(app.state.local_timings.close,2),timeout=2.5)
                except Exception:pass
                try:await asyncio.wait_for(asyncio.to_thread(app.state.diagnostics.close,2),timeout=2.5)
                except Exception:pass
                # Exporter shutdown belongs off the event loop, after accepted
                # effects have had their drain opportunity. Never fail shutdown.
                try:await asyncio.wait_for(asyncio.to_thread(app.state.telemetry.close,2),timeout=2.5)
                except Exception:pass
    @asynccontextmanager
    async def lifespan(application):
        try:
            await startup_resources()
            yield
        finally:
            await shutdown_resources()
    app=FastAPI(title="ServiceVoice",version="1.1.0",docs_url="/api/docs",redoc_url=None,lifespan=lifespan)
    app.state.store=store
    app.state.capacity=capacity
    app.state.live_admission=LiveAdmission(store,capacity)
    app.state.harness=ToolHarness(store)
    app.state.telemetry=NullTelemetry()
    app.state.local_timings=NullLocalTimingSink()
    app.state.diagnostics=NullDiagnostics()
    app.state.telemetry_initialized=False
    app.state.telemetry_lock=asyncio.Lock()
    app.state.shutdown_resources=shutdown_resources
    app.state.startup_resources=startup_resources
    app.state.session_locks=defaultdict(threading.Lock)
    app.state.rate=defaultdict(deque)
    app.state.rate_lock=threading.Lock()

    def diagnose(component,event,outcome,*,call_id,**kwargs):
        # Observation never controls admission, receipts or booking execution.
        try:return app.state.diagnostics.record(component,event,outcome,call_id=call_id,**kwargs)
        except Exception:return False
    app.state.diagnose=diagnose

    app.add_middleware(DiagnosticHTTPMiddleware,observer=diagnose)

    @app.middleware("http")
    async def local_console_boundary(request:Request,call_next):
        # Defense in depth. The public listener below has no operator-console routes at all.
        if request.url.path not in ("/webhooks/vapi","/webhooks/vapi/inbound","/llm/chat/completions","/tts/vapi") and not request.url.path.startswith("/twilio/"):
            if request.url.hostname not in ("localhost","127.0.0.1","::1","testserver"):
                return JSONResponse({"detail":"The operator console is local-only."},status_code=403)
            origin=request.headers.get("origin")
            if origin and origin.rstrip("/")!=str(request.base_url).rstrip("/"):
                return JSONResponse({"detail":"Cross-origin requests are not allowed."},status_code=403)
        try:length=int(request.headers.get("content-length","0") or "0")
        except ValueError:return JSONResponse({"detail":"Invalid content length."},status_code=400)
        if length>262144:
            return JSONResponse({"detail":"Request too large."},status_code=413)
        response=await call_next(request)
        response.headers["Cache-Control"]="no-store"
        response.headers["X-Content-Type-Options"]="nosniff"
        response.headers["Referrer-Policy"]="no-referrer"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request,exc):
        status=401 if exc.code=="unauthorized" else 409
        return JSONResponse({"detail":exc.message,"error":exc.code},status_code=status)

    @app.exception_handler(IngressError)
    async def ingress_error(request,exc):
        return JSONResponse({"detail":exc.code,"error":exc.code},status_code=exc.status)

    @app.exception_handler(CapacityError)
    async def capacity_error(request,exc):
        return JSONResponse({"detail":exc.code,"error":exc.code},status_code=exc.status)

    def auth(sid,token,allow_ended=False):
        return store.authorize(sid,token,allow_ended)

    def settings():
        from dotenv import dotenv_values
        return {**dotenv_values(ROOT/".env"),**os.environ}

    app.state.gemini_tts=GeminiTTS(settings)

    @app.post("/tts/vapi")
    async def synthesize_voice(request:Request):
        values=await run_in_threadpool(settings)
        secret=values.get("GEMINI_TTS_BEARER_SECRET") or ""
        supplied=request.headers.get("authorization","")
        if not isinstance(secret,str) or len(secret)<32:
            raise HTTPException(503,"Gemini speech is not configured.")
        if not secrets.compare_digest(supplied.encode(),("Bearer "+secret).encode()):
            raise HTTPException(401,"Invalid speech authentication.")
        body=await read_webhook_json(request)
        def observe(event):
            call_id=event.get("provider_call_id")
            if call_id:request.state.diagnostic_call_id=call_id
            diagnose("tts",event.get("event","tts.unknown"),
                "failure" if event.get("event")=="tts.failed" else "cancelled" if event.get("event")=="tts.cancelled" else "success" if event.get("event")=="tts.completed" else "started",
                call_id=call_id or "unbound_"+event["request_id"],request_id=event["request_id"],
                start_unix=event.get("started_at_unix"),duration_ms=event.get("elapsed_ms"),attributes={k:event[k] for k in (
                    "model","voice","sample_rate","input_characters","audio_bytes","error_code","failure_phase",
                    "provider","provider_call_id_status","turn_id_status","first_audio_ms","upstream_http_status",
                    "upstream_sample_rate","resampled","resampler","observation_scope","audio_duration_ms",
                    "cache_hit","cache_text_normalized","cache_asset_id","cache_request_sha256","cache_manifest_sha256","cached_sample_rate","synthesis_source") if k in event})
        try:return await app.state.gemini_tts.response(body,observer=observe)
        except TTSError as exc:
            raise HTTPException(getattr(exc,"status_code",502),getattr(exc,"code","speech_unavailable")) from None

    def config():
        # Reload only the explicit file; API keys never enter returned data.
        values=settings()
        needed=("VAPI_PUBLIC_KEY","VAPI_ASSISTANT_ID","VAPI_WEBHOOK_SECRET","VAPI_SERVER_CREDENTIAL_ID","PUBLIC_BASE_URL")
        missing=[key for key in needed if not values.get(key)]
        if values.get("PUBLIC_BASE_URL") and not str(values["PUBLIC_BASE_URL"]).startswith("https://"):
            missing.append("PUBLIC_BASE_URL must use HTTPS")
        return {"name":"ServiceVoice","dealership":"Cedar Service (fictional)","timezone":"America/Chicago","today":today().isoformat(),
                "prompt_version":SYSTEM_PROMPT_VERSION,
                "capacity_limits":capacity.public_limits(),
                "telephony_enabled":app.state.telephony_ingress.config is not None,
                "receipt_narration_enabled":values.get("SERVICEVOICE_RECEIPT_NARRATION")=="on",
                "narrator_version":NARRATOR_VERSION,
                "jev_faq_enabled":values.get("SERVICEVOICE_JEV_FAQ")=="on",
                "jev_faq_ready":app.state.gemini_gateway.faq.ready,
                "jev_pool_initialization_ms":app.state.gemini_gateway.faq.initialization_ms,
                "jev_faq_version":FAQ_VERSION,"jev_model":JEV_MODEL,
                "services":list(SERVICES.values()),"voice_provider":"Vapi","live_voice_available":not missing,"voice_max_duration_seconds":300,
                "live_voice_missing":missing,"vapi_public_key":values.get("VAPI_PUBLIC_KEY") or "",
                "vapi_assistant_id":values.get("VAPI_ASSISTANT_ID") or "",
                "limitations":["Fictional dealership and transactional demo scheduler.","Rehearsal uses scripted text, not an AI model.","Advisor requests stay in a local queue; no real transfer or callback.","Vapi voice needs account credits and a reachable authenticated HTTPS webhook."]}

    @app.get("/api/config")
    def get_config():return config()

    @app.get("/health")
    def health():return {"status":"ok","scheduler":"sqlite-demo","provider":"vapi"}

    @app.get("/api/observability-status")
    def observability_status():
        # This diagnostic is intentionally absent from the public listener.
        return {**app.state.telemetry.status(),"diagnostics":app.state.diagnostics.status()}

    @app.get("/api/sessions/{sid}/diagnostics")
    async def session_diagnostics(sid:str,x_session_token:str=Header(default="")):
        session=await run_in_threadpool(auth,sid,x_session_token,True)
        call_id=session["provider_call_id"]
        if not call_id:return {"events":[],"coverage":{"status":"unbound","complete":False}}
        return await run_in_threadpool(app.state.diagnostics.snapshot,call_id)

    @app.get("/")
    def index():return FileResponse(ROOT/"app/static/index.html")

    @app.post("/api/sessions")
    def new_session(body:NewSession,request:Request):
        if body.mode not in ("rehearsal","live"):
            raise HTTPException(422,"Mode must be rehearsal or live.")
        if body.mode=="live" and not config()["live_voice_available"]:
            raise HTTPException(503,"Vapi is not configured. Use rehearsal or complete the local .env setup.")
        with app.state.rate_lock:
            bucket=app.state.rate[request.client.host if request.client else "local"]
            while bucket and bucket[0]<time.time()-3600:bucket.popleft()
            if len(bucket)>=100:raise HTTPException(429,"Too many new sessions. Try again later.")
            bucket.append(time.time())
        return app.state.live_admission.create_browser_session() if body.mode=="live" else store.create_session(body.mode)

    @app.get("/api/sessions/{sid}")
    def get_session(sid:str,x_session_token:str=Header(default="")):
        auth(sid,x_session_token,True)
        return store.snapshot(sid)

    @app.get("/api/sessions/{sid}/gateway-timing")
    async def gateway_timing(sid:str,x_session_token:str=Header(default="")):
        session=await run_in_threadpool(auth,sid,x_session_token,True)
        call_id=session["provider_call_id"]
        # A session with no bound call must never request the internal all-call snapshot.
        rows=app.state.gemini_gateway.latency_snapshot(call_id) if call_id else []
        return {"kind":"gemini_gateway_timing","session_id":sid,"provider_call_id":call_id,"capacity":256,
                "scope":"Server request/stream observations; not acoustic latency or booking proof. Finalized requests only; process-memory retention.",
                "requests":rows}

    @app.post("/api/sessions/{sid}/message")
    def message(sid:str,body:Message,x_session_token:str=Header(default="")):
        session=auth(sid,x_session_token)
        if session["mode"]!="rehearsal":raise HTTPException(409,"Use the microphone in a live session.")
        with app.state.session_locks[sid]:
            store.transcript(sid,"user",body.text)
            reply=respond(store,sid,body.text)
            store.transcript(sid,"assistant",reply)
        return store.snapshot(sid)

    @app.post("/api/sessions/{sid}/tools")
    async def tool(sid:str,body:ToolRequest,x_session_token:str=Header(default="")):
        # Local debug endpoint uses the same dispatcher; never exposed through the public tunnel.
        await run_in_threadpool(auth,sid,x_session_token)
        return await app.state.harness.run(sid,body.name,body.arguments,body.call_id)

    @app.get("/api/sessions/{sid}/operations/{operation_id}")
    async def operation(sid:str,operation_id:str,x_session_token:str=Header(default="")):
        await run_in_threadpool(auth,sid,x_session_token,True)
        return await app.state.harness.operation(sid,operation_id)

    @app.post("/api/sessions/{sid}/end")
    def end_session(sid:str,x_session_token:str=Header(default="")):
        auth(sid,x_session_token,True)
        with store.connect() as db:db.execute("UPDATE sessions SET ended=1 WHERE id=?",(sid,))
        return {"ok":True}

    def validate_tool_batch(message):
        calls=message.get("toolCallList")
        if not isinstance(calls,list) or not 1<=len(calls)<=MAX_TOOL_BATCH:
            raise HTTPException(422,"Invalid tool list.")
        validated=[]; seen=set()
        for item in calls:
            if not isinstance(item,dict):raise HTTPException(422,"Invalid tool call.")
            tid=item.get("id"); function=item.get("function")
            if not isinstance(tid,str) or not 1<=len(tid)<=200:
                raise HTTPException(422,"Missing tool call ID.")
            if tid in seen:raise HTTPException(422,"Duplicate tool call ID within batch.")
            seen.add(tid)
            if not isinstance(function,dict):raise HTTPException(422,"Invalid function object.")
            name=function.get("name")
            if not isinstance(name,str) or not 1<=len(name)<=100 or name not in DEFINITIONS:
                raise HTTPException(422,"Unsupported function name.")
            arguments=function.get("arguments",{})
            try:
                if isinstance(arguments,str):arguments=json.loads(arguments)
                if not isinstance(arguments,dict):raise ValueError()
                normalized=DEFINITIONS[name][0].model_validate(arguments).model_dump()
            except (ValueError,TypeError,RecursionError,ValidationError):
                raise HTTPException(422,"Invalid function arguments.")
            validated.append((tid,name,normalized,arguments))
        return validated

    def require_unexpired(session):
        if time.time()-session["created"]>4*3600:
            raise DomainError("session_expired","Session expired. Start a new session.")
        if session["mode"]!="live":
            raise HTTPException(403,"Voice call needs a live session.")

    def resolve_call(call_id,sid,token):
        # Every SQLite operation in this routine runs in a worker thread.
        with store.connect() as db:
            bound=db.execute("SELECT * FROM sessions WHERE provider_call_id=?",(call_id,)).fetchone()
            # Preserve the phone/browser separation even after phone ingress is
            # disabled on a later restart, or a payload omits phone metadata.
            phone_table=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='telephony_bindings'").fetchone()
            if phone_table and db.execute("SELECT 1 FROM telephony_bindings WHERE provider_call_id=?",(call_id,)).fetchone():
                raise HTTPException(403,"Phone calls require their dedicated ingress binding.")
        if bound:
            require_unexpired(bound)
            if sid is not None and sid!=bound["id"]:
                raise HTTPException(403,"Conflicting session binding.")
            if token is not None:auth(bound["id"],token,True)
            return bound["id"]
        if not isinstance(sid,str) or not isinstance(token,str):
            raise HTTPException(403,"Unknown call. Start from the local browser demo.")
        session=auth(sid,token)
        require_unexpired(session)
        try:
            with store.transaction() as db:
                # Recheck after acquiring the write lock: /end or another
                # provider binding may have committed while we were waiting.
                current=db.execute("SELECT * FROM sessions WHERE id=?",(sid,)).fetchone()
                require_unexpired(current)
                app.state.live_admission.require_browser_reservation(current)
                if current["ended"]:raise DomainError("session_ended","This session has ended.")
                if current["provider_call_id"] and current["provider_call_id"]!=call_id:
                    raise HTTPException(409,"Session already belongs to another call.")
                other=db.execute("SELECT id FROM sessions WHERE provider_call_id=?",(call_id,)).fetchone()
                if other and other["id"]!=sid:raise HTTPException(409,"Call already belongs to another session.")
                db.execute("UPDATE sessions SET provider_call_id=? WHERE id=?",(call_id,sid))
        except sqlite3.IntegrityError:
            raise HTTPException(409,"Call already belongs to another session.")
        return sid

    def bind_provider_call(call,assistant=None):
        """Synchronous shared boundary; async routes must run it in a thread."""
        if not isinstance(call,dict):raise HTTPException(422,"Invalid call object.")
        call_id=call.get("id")
        if not isinstance(call_id,str) or not 1<=len(call_id)<=200:raise HTTPException(422,"Missing call ID.")
        if assistant is None:assistant={}
        if not isinstance(assistant,dict):raise HTTPException(422,"Invalid assistant object.")
        ingress=app.state.telephony_ingress
        selected=(call.get("assistantId"),assistant.get("id"))
        phone_identity=(call.get("type") in ("inboundPhoneCall","outboundPhoneCall")
                        or call.get("phoneNumberId") is not None
                        or (ingress.config is not None and ingress.config.assistant_id in selected))
        if phone_identity:
            # This is resolution only. Unknown calls can only be admitted by the
            # separately authenticated assistant-request endpoint below.
            return ingress.resolve(call,assistant,allow_ended=True)
        values=settings()
        expected=values.get("VAPI_ASSISTANT_ID") or ""
        assistant_id=call.get("assistantId") or assistant.get("id")
        if not expected or assistant_id!=expected:raise HTTPException(403,"Unexpected assistant.")
        overrides=call.get("assistantOverrides",{})
        if not isinstance(overrides,dict):raise HTTPException(422,"Invalid assistant overrides.")
        variables=overrides.get("variableValues",{})
        if not isinstance(variables,dict):raise HTTPException(422,"Invalid session variables.")
        sid=variables.get("session_id");token=variables.get("session_token")
        if sid is not None and (not isinstance(sid,str) or not 1<=len(sid)<=100):
            raise HTTPException(422,"Invalid session binding.")
        if token is not None and (not isinstance(token,str) or not 1<=len(token)<=256):
            raise HTTPException(422,"Invalid session binding.")
        sid=resolve_call(call_id,sid,token)
        try:
            app.state.gemini_gateway.store.pin_call_policy(sid,call_id,assistant_id,values)
        except GatewayError as exc:
            raise HTTPException(exc.status,exc.code) from None
        except sqlite3.Error:
            raise HTTPException(503,"Call execution policy unavailable.") from None
        return sid

    app.state.bind_provider_call=bind_provider_call
    app.state.config_loader=settings
    def bind_model_call(call,assistant=None):
        # A delayed terminal webhook must not allow a new generation when the
        # authenticated provider payload already says the call has ended.
        if isinstance(call,dict) and (call.get("status")=="ended" or call.get("endedAt") is not None):
            raise HTTPException(409,"Call has ended. No new generation is allowed.")
        return bind_provider_call(call,assistant)
    install_gateway(app, settings, bind_model_call,limits=GatewayLimits(concurrency=capacity.gateway_concurrency))
    # Dedicated phone identity/credentials are frozen until process restart.
    app.state.telephony_ingress=TelephonyIngress(store,IngressConfig.from_mapping(settings()),
        limits=IngressLimits(active_calls=capacity.max_live_calls,workers=capacity.phone_workers,
                             admissions_per_window=capacity.phone_admissions_per_minute),
        live_admission=app.state.live_admission)
    app.state.telephony_http_capacity=capacity.phone_http_capacity
    app.state.telephony_http_pending=set()
    app.state.telephony_http_lock=threading.Lock()
    app.state.telephony_http_tasks=set()
    app.state.telephony_http_closing=False
    install_twilio(app, settings())

    def tool_may_continue(sid,tid,terminal_metadata=False):
        with store.connect() as db:
            session=db.execute("SELECT * FROM sessions WHERE id=?",(sid,)).fetchone()
            require_unexpired(session)
            if not session["ended"] and not terminal_metadata:return True
            # Existing work may finish and exact retries must remain observable.
            # A new ID after end is refused; nothing cancels accepted effects.
            cached=db.execute("SELECT 1 FROM tool_results WHERE session_id=? AND call_id=?",(sid,tid)).fetchone()
            admitted=db.execute("SELECT 1 FROM tool_invocations WHERE session_id=? AND call_id=?",(sid,tid)).fetchone()
            return bool(cached or admitted)

    def save_transcript(sid,call_id,message):
        role=message.get("role"); text=message.get("transcript")
        if role not in ("user","assistant") or not isinstance(text,str):return
        identity_fields={key:message[key] for key in ("id","eventId","timestamp","turn") if message.get(key) is not None}
        # Without provider identity, identical words can be separate utterances.
        # Do not manufacture a dedupe key from the transcript text alone.
        event_id=None
        if identity_fields:
            identity=json.dumps({"call":call_id,"role":role,"text":text,**identity_fields},sort_keys=True)
            event_id=hashlib.sha256(identity.encode()).hexdigest()
        store.transcript(sid,role,text,event_id)

    def mark_ended(sid):
        with store.connect() as db:db.execute("UPDATE sessions SET ended=1 WHERE id=?",(sid,))

    async def process_webhook(message, *, phone=False, deadline=None, abandoned=None, diagnostic_request=None):
        if not isinstance(message,dict):raise HTTPException(422,"Missing message.")
        event_type=message.get("type")
        if not isinstance(event_type,str) or not 1<=len(event_type)<=100:
            raise HTTPException(422,"Invalid event type.")
        call=message.get("call")
        # Validate the ENTIRE batch before binding or executing its first effect.
        calls=validate_tool_batch(message) if event_type=="tool-calls" else []
        if phone:
            sid=await asyncio.to_thread(app.state.telephony_ingress.resolve,call,message.get("assistant"),allow_ended=True,deadline=deadline,abandoned=abandoned)
        else:
            # Browser webhook credentials never authorize phone-origin events.
            if isinstance(call,dict) and (call.get("type") in ("inboundPhoneCall","outboundPhoneCall") or call.get("phoneNumberId") is not None):
                raise HTTPException(403,"Phone calls require their dedicated webhook.")
            ingress=app.state.telephony_ingress
            if ingress.config is not None and isinstance(call,dict) and call.get("assistantId")==ingress.config.assistant_id:
                raise HTTPException(403,"Phone calls require their dedicated webhook.")
            # bind_provider_call alone permits phone resolution for the Gemini
            # gateway. Reject both assistant identity locations on this route.
            assistant=message.get("assistant")
            if ingress.config is not None and isinstance(assistant,dict) and assistant.get("id")==ingress.config.assistant_id:
                raise HTTPException(403,"Phone calls require their dedicated webhook.")
            sid=await run_in_threadpool(bind_provider_call,call,assistant)
        call_id=call["id"]
        if phone and app.state.twilio_lane is not None:
            app.state.twilio_lane.event(call_id, message)
        if diagnostic_request is not None:diagnostic_request.state.diagnostic_call_id=call_id
        diagnose("webhook","call_bound","success",call_id=call_id,
            request_id=getattr(getattr(diagnostic_request,"state",None),"diagnostic_request_id",None),
            attributes={"evidence_source":"verified_session_binding",
                "event_type":event_type if event_type in {"tool-calls","transcript","status-update","end-of-call-report","assistant-request","speech-update"} else "unknown"})
        if event_type=="tool-calls":
            # Provider auth is not execution provenance. Reject an entire batch
            # before any tool runs if the custom gateway did not issue it.
            try:
                diagnostic_contexts=await run_in_threadpool(app.state.gemini_gateway.store.authorize_tool_batch,sid,call_id,calls)
            except GatewayError as exc:
                raise HTTPException(exc.status,exc.code) from None
            except sqlite3.Error:
                raise HTTPException(503,"Tool authorization evidence unavailable.") from None
            results=[]
            for tid,name,arguments,_original in calls:
                terminal_metadata=phone and (call.get("status")=="ended" or call.get("endedAt") is not None)
                may_continue=await run_in_threadpool(tool_may_continue,sid,tid,terminal_metadata)
                # Do not begin a fresh effect after slow preflight exhausted the
                # phone HTTP budget. Already accepted effects remain shielded.
                if phone and ((abandoned is not None and abandoned.is_set()) or (deadline is not None and time.monotonic()>=deadline)):
                    raise HTTPException(504,"Phone webhook deadline exceeded. Retry the same event and call ID to reconcile.")
                if not may_continue:
                    result={"ok":False,"error":"session_ended","message":"Call ended. No new action was taken."}
                else:
                    context=_tool_diagnostic_context(app.state.diagnostics,call_id,tid,diagnostic_contexts)
                    result=await app.state.harness.run(sid,name,arguments,tid,diagnostic_context=context)
                try:
                    await run_in_threadpool(app.state.gemini_gateway.observations.record,sid,call_id,tid,result)
                except (ObservationError,sqlite3.Error):
                    # Accepted effects remain committed; an exact retry can
                    # recover their receipt. Never return unverifiable success.
                    raise HTTPException(503,"Tool response evidence could not be saved. Retry the original tool ID.")
                results.append({"toolCallId":tid,"result":json.dumps(result)})
            return {"results":results}
        if event_type=="transcript" and message.get("transcriptType")=="final":
            await run_in_threadpool(save_transcript,sid,call_id,message)
            diagnose("stt" if message.get("role")=="user" else "voice_runtime","final_transcript_received","success",call_id=call_id,
                attributes={"transcript_role":message.get("role"),"transcript_final":True,"evidence_source":"provider_transcript_only"})
        if event_type=="end-of-call-report" or (event_type=="status-update" and message.get("status")=="ended"):
            await run_in_threadpool(mark_ended,sid)
            diagnose("voice_runtime","call_ended","success",call_id=call_id,attributes={"evidence_source":"provider_terminal_event"})
        return {"received":True}

    @app.post("/webhooks/vapi")
    async def webhook(request:Request):
        values=await run_in_threadpool(settings)
        secret=values.get("VAPI_WEBHOOK_SECRET") or ""
        supplied=request.headers.get("authorization","")
        if not secret or not secrets.compare_digest(supplied.encode("utf-8"),("Bearer "+secret).encode("utf-8")):
            raise HTTPException(401,"Invalid webhook authentication.")
        body=await read_webhook_json(request)
        message=body.get("message") if isinstance(body,dict) else None
        return await process_webhook(message,diagnostic_request=request)

    @app.post("/webhooks/vapi/inbound")
    async def inbound_webhook(request:Request):
        deadline=time.monotonic()+TELEPHONY_HTTP_TIMEOUT_SECONDS
        abandoned=threading.Event()
        ingress=app.state.telephony_ingress
        authorization=request.headers.get("authorization","")
        ingress.authenticate(authorization)  # No DB/body read before authentication.
        # Authenticated listening traffic has its own in-memory admission path;
        # hundreds of partials cannot occupy the slots used by scheduling tools.
        lane=app.state.twilio_lane
        early_body=None
        if lane is not None:
            if lane.closing:
                raise HTTPException(503,"Isolated lane is closing.")
            try:
                early_body=await asyncio.wait_for(read_webhook_json(request),timeout=2)
            except TimeoutError:
                raise HTTPException(408,"Webhook body deadline exceeded.") from None
            early_message=early_body.get("message") if isinstance(early_body,dict) else None
            if isinstance(early_message,dict) and lane.fast_event(early_message):
                return {"ok":True}
        marker=object()
        with app.state.telephony_http_lock:
            if app.state.telephony_http_closing:
                raise HTTPException(503,"Phone ingress is closing.")
            if len(app.state.telephony_http_pending)>=app.state.telephony_http_capacity:
                raise HTTPException(429,"Phone webhook capacity unavailable. Retry the same event.")
            app.state.telephony_http_pending.add(marker)
        operation=None
        def release(completed=None):
            with app.state.telephony_http_lock:
                app.state.telephony_http_pending.discard(marker)
            if completed is not None and not completed.cancelled():
                completed.exception()  # Consume late failure after timed-out caller.
            if completed is not None:
                app.state.telephony_http_tasks.discard(completed)
        try:
            body=early_body if early_body is not None else await asyncio.wait_for(read_webhook_json(request),timeout=max(0,deadline-time.monotonic()))
            message=body.get("message") if isinstance(body,dict) else None
            if not isinstance(message,dict):raise HTTPException(422,"Missing message.")
            event_type=message.get("type")
            async def observe_phone_effect(effect,event):
                result=await asyncio.to_thread(effect,message,authorization,deadline=deadline,abandoned=abandoned)
                # Identity is diagnostic evidence only after ingress validation
                # succeeds, including work finishing after its HTTP waiter left.
                verified_call_id=message["call"]["id"]
                if lane is not None:
                    if event == "call_bound":
                        await lane.admitted(message, result)
                    elif verified_call_id in lane.bindings:
                        lane.spawn(lane.finish(lane.bindings[verified_call_id][0]))
                request.state.diagnostic_call_id=verified_call_id
                diagnose("voice_runtime",event,"success",call_id=verified_call_id,
                    attributes={"evidence_source":"verified_phone_ingress"})
                return result
            if event_type=="assistant-request":
                coroutine=observe_phone_effect(ingress.admit,"call_bound")
            elif event_type=="end-of-call-report" or (event_type=="status-update" and message.get("status")=="ended"):
                coroutine=observe_phone_effect(ingress.end,"call_ended")
            else:
                coroutine=process_webhook(message,phone=True,deadline=deadline,abandoned=abandoned,diagnostic_request=request)
            operation=asyncio.create_task(coroutine)
            app.state.telephony_http_tasks.add(operation)
            operation.add_done_callback(release)
            # A timed-out/disconnected waiter must not cancel accepted DB work or
            # release its capacity while its actual thread is still running.
            return await asyncio.wait_for(asyncio.shield(operation),timeout=max(0,deadline-time.monotonic()))
        except asyncio.TimeoutError:
            abandoned.set()
            raise HTTPException(504,"Phone webhook deadline exceeded. Retry the same event and call ID to reconcile.") from None
        except asyncio.CancelledError:
            abandoned.set()
            raise
        finally:
            if operation is None:
                release()

    install_advisor_api(app,store,ROOT/"app/static")
    app.mount("/static",StaticFiles(directory=ROOT/"app/static"),name="static")
    return app

def create_public_app(console):
    """Authenticated provider routes only; never expose operator-console routes."""
    @asynccontextmanager
    async def lifespan(application):
        try:
            await console.state.startup_resources()
            yield
        finally:
            await console.state.shutdown_resources()
    public=FastAPI(docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)
    public.add_middleware(DiagnosticHTTPMiddleware,observer=console.state.diagnose)
    for route in console.routes:
        if getattr(route,"path",None) in ("/webhooks/vapi","/webhooks/vapi/inbound","/llm/chat/completions","/tts/vapi") or getattr(route,"path",None) in TWILIO_PATHS:public.router.routes.append(route)
    public.add_exception_handler(DomainError,console.exception_handlers[DomainError])
    public.add_exception_handler(IngressError,console.exception_handlers[IngressError])
    public.add_exception_handler(CapacityError,console.exception_handlers[CapacityError])
    public.add_exception_handler(BridgeError,console.exception_handlers[BridgeError])
    return public

app=create_app()
public_app=create_public_app(app)
