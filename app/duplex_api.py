"""Private browser experiment. Cue decisions never enter the booking dispatcher.

The same installer works with a local Store or an authenticated private-console
adapter. Vapi administration credentials stay in the operator's local process.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import asdict
import hashlib
import json
import math
import sqlite3
from pathlib import Path
import time
from urllib.parse import urlsplit

import httpx
from fastapi import Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.concurrency import run_in_threadpool

from .backchannel_audio import BackchannelAudioCache
from .backchannel_catalog import PHRASES
from .duplex_cues import DuplexCuePolicy, DuplexCueSession, cue_state

REQUEST_MAX_AGE_MS = 800
DECISION_REASONS = frozenset({"eligible_listening_ack", "eligible_hold_ack", "invalid_phase",
    "unverified_or_sensitive_context", "main_speaking", "conditional_utterance", "sensitive_utterance",
    "private_contact_detail", "invalid_context", "turn_not_allowed", "sensitive_context", "invalid_messages",
    "not_final_user_turn", "input_bounds", "cached_language_unverified", "short_or_ambiguous",
    "urgent_or_safety", "untrusted_control_content", "prior_context_bounds", "sensitive_prior_turn",
    "credential_missing", "policy_not_ready", "classification_capacity", "provider_unavailable",
    "invalid_response", "response_bounds", "model_mismatch", "policy_closed", "classification_deadline",
    "classifier_silence", "classification_uncertain", "classification_unavailable", "session_closed",
    "invalid_revision", "stale_revision", "phase_already_acknowledged", "call_budget", "cooldown",
    "cue_spacing", "classification_budget", "utterance_classification_budget", "classification_throttled",
    "superseded", "phase_mismatch", "decision_expired", "scheduler_changed", "request_expired"})
EVENT_REASONS = DECISION_REASONS | frozenset({"classifier_deadline", "classifier_unavailable",
    "selection_phase_mismatch", "selection_confidence", "selection_language", "selection_expired_on_arrival",
    "selection_expired_before_playback", "clip_unavailable", "caller_resumed", "caller_started", "partial_revised",
    "final_revised", "caller_stopped", "generation_started", "generation_finished", "sensitive_state_changed",
    "main_audio_ready", "main_announced", "call_ended", "provisional_barge_in", "muted", "sensitive_state", "listening_context"})


class CueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=2000)
    epoch: int = Field(ge=1, le=10000, strict=True)
    revision: int = Field(ge=1, le=100000, strict=True)
    phase: str = Field(pattern="^(listening|waiting)$")


class PlaybackEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: str = Field(pattern="^[a-z_]{1,60}$")
    epoch: int = Field(ge=0, le=10000, strict=True)
    at_ms: float = Field(ge=0, le=360000, allow_inf_nan=False)
    phrase_id: str | None = Field(default=None, pattern="^(neutral_ack|moment|silence)$")
    phase: str | None = Field(default=None, pattern="^(listening|waiting)$")
    reason: str | None = Field(default=None, pattern="^(?:" + "|".join(sorted(EVENT_REASONS)) + ")$")
    blocked: bool | None = Field(default=None, strict=True)
    retry_after_ms: int | None = Field(default=None, ge=1, le=1000, strict=True)

    @model_validator(mode="after")
    def bounded_pacing_retry(self):
        if self.event == "cue_retry_scheduled" or self.retry_after_ms is not None:
            if (self.event != "cue_retry_scheduled" or self.phase != "waiting"
                    or self.reason not in ("classification_throttled", "cue_spacing")
                    or self.retry_after_ms is None):
                raise ValueError("Only a bounded waiting pacing retry may carry a retry delay.")
        return self


def cue_context(snapshot):
    """Authoritative scheduler flags; partial words cannot authorize an action."""
    return {
        "stage": snapshot.get("stage"),
        "active_confirmation": snapshot.get("stage") == "confirmation",
        "has_pending_proposal": bool(snapshot.get("proposal")),
        "has_pending_operation": bool(snapshot.get("pending_operations")),
        "ended": snapshot.get("ended") is not False,
    }


class DuplexControl:
    def __init__(self, settings, authorize, snapshot, *, client=None, policy=None):
        self.settings, self.authorize, self.snapshot = settings, authorize, snapshot
        self.client = client
        self.policy = policy or DuplexCuePolicy(connection_capacity=4)
        self.sessions = {}
        self.lock = asyncio.Lock()
        self.audio = None
        self.events = {}
        self.decisions = {}
        self.clock = time.monotonic
        self.creating = set()
        self.created = set()
        self.calls = {}
        self.stopped = set()

    def config(self):
        values = self.settings()
        assistant = values.get("VAPI_ASSISTANT_ID")
        if (values.get("SERVICEVOICE_DUPLEX") != "on" or not assistant
                or assistant != values.get("VAPI_BACKCHANNEL_ASSISTANT_ID")):
            raise HTTPException(503, "The isolated duplex experiment is disabled.")
        return values

    async def start(self):
        await self.policy.start()
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False)

    async def close(self):
        for session in self.sessions.values():
            session.close()
        await self.policy.close()
        if self.client is not None:
            await self.client.aclose()

    async def authenticated(self, sid, token):
        self.config()
        row = await run_in_threadpool(self.authorize, sid, token)
        if row.get("mode") != "live" or row.get("ended"):
            raise HTTPException(409, "An active isolated live session is required.")
        return row

    async def session(self, sid):
        async with self.lock:
            if sid not in self.sessions:
                if len(self.sessions) >= 64:
                    raise HTTPException(429, "Duplex session capacity reached.")
                self.sessions[sid] = DuplexCueSession(self.policy)
                self.events[sid] = []
                self.decisions[sid] = deque(maxlen=256)
            return self.sessions[sid]

    def reserve_call(self, sid):
        path = self.config().get("SERVICEVOICE_DUPLEX_CALL_LEDGER")
        if not path:
            return
        target = Path(path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(target, timeout=2) as db:
            db.execute("CREATE TABLE IF NOT EXISTS allocations (session_id TEXT PRIMARY KEY, reserved_at REAL NOT NULL)")
            try:
                db.execute("INSERT INTO allocations VALUES (?,?)", (sid, time.time()))
            except sqlite3.IntegrityError:
                raise HTTPException(409, "This session has already reserved a call. Start a new session.") from None

    def assets(self):
        if self.audio is None:
            value = self.config().get("SERVICEVOICE_BACKCHANNEL_AUDIO_MANIFEST")
            if not value:
                raise HTTPException(503, "The approved cue audio bank is unavailable.")
            manifest = Path(value).resolve()
            cache = BackchannelAudioCache(manifest.parent)
            try:
                data = cache.load_manifest(manifest)
                assets = {asset["phrase_id"]: (cache.read_pcm(asset), asset) for asset in data["assets"]
                          if asset["phrase_id"] in ("neutral_ack", "moment")}
                if set(assets) != {"neutral_ack", "moment"}:
                    raise ValueError("incomplete_bank")
            except (OSError, ValueError):
                raise HTTPException(503, "The approved cue audio bank failed validation.") from None
            self.audio = assets
        return self.audio


def install_duplex_api(app, settings, authorize, snapshot, *, client=None, policy=None):
    control = DuplexControl(settings, authorize, snapshot, client=client, policy=policy)
    app.state.duplex = control

    @app.get("/api/duplex/config")
    async def config():
        values = control.config()
        assets = await run_in_threadpool(control.assets)
        return {"enabled": True, "assistant_id": values["VAPI_ASSISTANT_ID"], "sample_rate": 16000,
                "phrases": [{"phrase_id": key, "text": PHRASES[key], "sample_rate": 16000,
                             "sha256": hashlib.sha256(pcm).hexdigest(), "pcm_bytes": len(pcm)}
                            for key, (pcm, _) in assets.items()],
                "scope": "isolated_browser_owned_playback", "phone_enabled": False}

    @app.get("/api/sessions/{sid}/duplex/audio/{phrase}")
    async def audio(sid: str, phrase: str, x_session_token: str = Header(default="")):
        await control.authenticated(sid, x_session_token)
        if phrase not in ("neutral_ack", "moment"):
            raise HTTPException(404)
        assets = await run_in_threadpool(control.assets)
        return Response(assets[phrase][0], media_type="application/octet-stream",
                        headers={"Cache-Control": "no-store", "X-Audio-Sample-Rate": "16000"})

    @app.post("/api/sessions/{sid}/duplex/call")
    async def create_call(sid: str, request: Request, x_session_token: str = Header(default="")):
        row = await control.authenticated(sid, x_session_token)
        values = control.config()
        if not values.get("VAPI_API_KEY"):
            raise HTTPException(503, "Vapi call allocation is unavailable in this operator process.")
        await run_in_threadpool(control.assets)  # Fail before starting a paid call.
        # Reserve bounded local ownership before any durable reservation or paid
        # provider request. A full operator process cannot strand a new call.
        await control.session(sid)
        async with control.lock:
            if sid in control.creating or sid in control.created or row.get("provider_call_id"):
                raise HTTPException(409, "This session already requested a call. Start a new session.")
            control.creating.add(sid)
            # Unknown network outcomes must never allocate a duplicate on retry.
            control.created.add(sid)
        await run_in_threadpool(control.reserve_call, sid)
        payload = {"assistantId": values["VAPI_ASSISTANT_ID"],
                   "assistantOverrides": {"maxDurationSeconds": 300, "variableValues": {
                       "session_id": sid, "session_token": x_session_token,
                       "current_date": values.get("TODAY", ""), "timezone": "America/Chicago",
                       "servicevoice_duplex_playback": "browser-owned-v1"}},
                   "transport": {"provider": "vapi.websocket", "audioFormat": {
                       "format": "pcm_s16le", "container": "raw", "sampleRate": 16000}}}
        try:
            response = await control.client.post("https://api.vapi.ai/call", json=payload,
                headers={"Authorization": "Bearer " + values["VAPI_API_KEY"]})
            if response.status_code != 201 and response.status_code != 200:
                raise HTTPException(502, "The provider rejected isolated call creation.")
            if len(response.content) > 1024 * 1024:
                raise ValueError("response_bounds")
            data = response.json()
            address = data.get("transport", {}).get("websocketCallUrl")
            url = urlsplit(address if isinstance(address, str) else "")
            if (url.scheme != "wss" or not url.hostname or not url.hostname.endswith(".vapi.ai")
                    or url.username or url.password or url.fragment or url.port not in (None, 443)
                    or data.get("assistantId") != values["VAPI_ASSISTANT_ID"]
                    or not isinstance(data.get("id"), str)):
                raise ValueError("invalid_transport")
            control.calls[sid] = data["id"]
            return {"call_id": data["id"], "websocket_url": address, "sample_rate": 16000}
        except httpx.HTTPError:
            raise HTTPException(502, "Call allocation did not complete. Start a new session after checking the provider.") from None
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(502, "The provider returned an unsupported transport.") from None
        finally:
            control.creating.discard(sid)

    @app.post("/api/sessions/{sid}/duplex/stop")
    async def stop_call(sid: str, x_session_token: str = Header(default="")):
        control.config()
        await run_in_threadpool(control.authorize, sid, x_session_token, True)
        if sid in control.sessions:
            control.sessions[sid].close()
        call_id = control.calls.get(sid)
        if not call_id or sid in control.stopped:
            return {"ok": sid in control.stopped, "call_known": bool(call_id)}
        values = control.config()
        headers = {"Authorization": "Bearer " + values["VAPI_API_KEY"]}
        try:
            saved = await control.client.get("https://api.vapi.ai/call/" + call_id, headers=headers)
            if saved.status_code != 200:
                return {"ok": False, "call_known": True}
            data = saved.json()
            if (data.get("id") != call_id or data.get("assistantId") != values["VAPI_ASSISTANT_ID"]):
                return {"ok": False, "call_known": True}
            if data.get("status") == "ended":
                control.stopped.add(sid)
                return {"ok": True, "already_ended": True}
            address = data.get("monitor", {}).get("controlUrl")
            url = urlsplit(address if isinstance(address, str) else "")
            if (url.scheme != "https" or not url.hostname or not url.hostname.endswith(".vapi.ai")
                    or url.username or url.password or url.port not in (None,443) or url.fragment):
                return {"ok": False, "call_known": True}
            stopped = await control.client.post(address, json={"type": "end-call"}, headers=headers)
            if stopped.status_code not in (200,201,202,204):
                return {"ok": False, "call_known": True}
            control.stopped.add(sid)
            return {"ok": True}
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            return {"ok": False, "call_known": True}

    @app.post("/api/sessions/{sid}/duplex/cue")
    async def cue(sid: str, body: CueRequest, x_session_token: str = Header(default="")):
        received_at = control.clock()
        row = await control.authenticated(sid, x_session_token)
        session = await control.session(sid)
        # The private console uses one fresh authenticated-snapshot callback for
        # both roles. Reuse that exact read only when callable identity proves
        # the contract; a distinct authorize callback may return a session row
        # without scheduler state and still requires its separate snapshot.
        before = row if control.authorize is control.snapshot else await run_in_threadpool(
            control.snapshot, sid, x_session_token)
        context = cue_context(before)
        previous = next((item.get("text", "") for item in reversed(before.get("transcript", []))
                         if item.get("role") == "assistant"), "")[-1600:]
        decision = await session.choose(epoch=body.epoch, revision=body.revision, text=body.text,
            phase=body.phase, key=control.config().get("TYPESAFE_API_KEY"), context=context,
            previous_assistant=previous)
        selected_at = control.clock()
        after = await run_in_threadpool(control.snapshot, sid, x_session_token)
        after_context = cue_context(after)
        after_previous = next((item.get("text", "") for item in reversed(after.get("transcript", []))
                               if item.get("role") == "assistant"), "")[-1600:]
        stage_only_change = (context["stage"] in ("service", "slot")
            and after_context["stage"] in ("service", "slot")
            and all(context[key] is False and after_context[key] is False for key in context if key != "stage")
            and after_previous == previous
            and cue_state(body.text, body.phase, after_previous, after_context)[1] == "eligible")
        result = asdict(decision)
        elapsed_ms = max(0, math.ceil((control.clock() - received_at) * 1000))
        # Remaining TTL is measured at response construction, after the fresh
        # authoritative read. The browser anchors this remainder to receipt and
        # separately caps absolute age from its own HTTP-request start.
        remaining = min(decision.expires_in_ms - math.ceil((control.clock() - selected_at) * 1000),
                        REQUEST_MAX_AGE_MS - elapsed_ms)
        result["max_request_age_ms"] = REQUEST_MAX_AGE_MS
        if after_context != context and not stage_only_change:
            session.invalidate(reason="scheduler_changed")
            result.update(phrase_id="silence", reason="scheduler_changed", confidence=0, expires_in_ms=0)
        elif decision.phrase_id != "silence" and remaining <= 0:
            result.update(phrase_id="silence", reason="request_expired", expires_in_ms=0)
        else:
            result["expires_in_ms"] = max(0, remaining) if decision.phrase_id != "silence" else 0
        retry_after = result.get("retry_after_ms")
        if (result["phrase_id"] == "silence" and result["reason"] in
                ("classification_throttled", "cue_spacing") and type(retry_after) is int
                and 1 <= retry_after <= 1000):
            # The hint is remaining pacing at response construction. A retry
            # must be a new authenticated selection with current state; this
            # cannot revive an accepted, expired or uncertain decision.
            result["retry_after_ms"] = max(1, retry_after - max(0,
                math.ceil((control.clock() - selected_at) * 1000)))
        else:
            result.pop("retry_after_ms", None)
        # Never retain utterances, prior turns, tokens, provider bodies or URLs.
        def statistic(value):
            return value if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1 else None
        control.decisions[sid].append({"phase": body.phase, "epoch": body.epoch, "revision": body.revision,
            "phrase_id": result["phrase_id"],
            "reason": result["reason"] if result["reason"] in DECISION_REASONS else "unclassified_result",
            "confidence": statistic(result.get("confidence")), "probability": statistic(result.get("probability")),
            "language_probability": statistic(result.get("language_probability")),
            "request_elapsed_ms": elapsed_ms, "classification_ms": (decision.classification_ms
                if type(decision.classification_ms) in (int, float) and math.isfinite(decision.classification_ms)
                and decision.classification_ms >= 0 else None),
            "expires_in_ms": result["expires_in_ms"], "max_request_age_ms": REQUEST_MAX_AGE_MS,
            "retry_after_ms": result.get("retry_after_ms"),
            "budget": session.budget_state()})
        return result

    @app.post("/api/sessions/{sid}/duplex/event")
    async def event(sid: str, body: PlaybackEvent, x_session_token: str = Header(default="")):
        await control.authenticated(sid, x_session_token)
        session = await control.session(sid)
        if body.event in ("barge_in", "main_started", "ended", "muted"):
            session.invalidate(reason=body.event)
        rows = control.events[sid]
        if len(rows) >= 1000:
            raise HTTPException(429, "The experiment event bound has been reached.")
        rows.append(body.model_dump())
        return {"ok": True}

    @app.get("/api/sessions/{sid}/duplex/evidence")
    async def evidence(sid: str, x_session_token: str = Header(default="")):
        control.config()
        await run_in_threadpool(control.authorize, sid, x_session_token, True)
        return {"events": control.events.get(sid, []), "scope": "browser_reported_playback_events",
                "cue_decisions": list(control.decisions.get(sid, [])),
                "cue_decision_scope": "bounded_server_selection_results_not_proof_of_browser_playback",
                "provider_packet_generation_ids": "unavailable", "human_listening": "not_established"}

    return control
