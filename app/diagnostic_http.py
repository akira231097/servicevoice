"""Observe ASGI completion without a task-group or request/body interception."""
import asyncio
import time
from uuid import uuid4


class DiagnosticHTTPMiddleware:
    def __init__(self,app,observer):
        self.app,self.observer=app,observer

    async def __call__(self,scope,receive,send):
        component={"/webhooks/vapi":"webhook","/webhooks/vapi/inbound":"webhook",
            "/llm/chat/completions":"gateway","/tts/vapi":"tts"}.get(scope.get("path"))
        if scope.get("type")!="http" or component is None:
            return await self.app(scope,receive,send)
        started=time.time();clock_started=time.perf_counter();request_id=uuid4().hex
        state=scope.setdefault("state",{});state["diagnostic_request_id"]=request_id
        status=None;complete=False;cancelled=False
        async def observe_send(message):
            nonlocal status,complete
            await send(message)
            if message["type"]=="http.response.start":status=message["status"]
            if message["type"]=="http.response.body" and not message.get("more_body",False):complete=True
        try:
            return await self.app(scope,receive,observe_send)
        except asyncio.CancelledError:
            cancelled=True
            raise
        finally:
            outcome="cancelled" if cancelled else "success" if status is not None and status<400 and complete else "failure"
            try:
                self.observer(component,"http_finished",outcome,
                    call_id=state.get("diagnostic_call_id","unbound_"+request_id),request_id=request_id,
                    start_unix=started,duration_ms=max(0,(time.perf_counter()-clock_started)*1000),
                    attributes={"http_status":status,"response_started":status is not None,"response_complete":complete,
                        "evidence_source":"server_http_headers"})
            except Exception:pass
