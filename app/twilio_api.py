"""Experimental HTTP integration for a separate Twilio phone line.

Only dedicated phone admission can register a listening session. Partial events
use that in-memory binding, not the scheduler worker pool. No route is live with
the feature disabled, and no production environment can enable this prototype.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
from pathlib import Path
from urllib.parse import parse_qsl
import wave

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response

from .listening_cues import ListeningCoordinator, ListeningPolicy, listening_gate, LISTENING_PHRASES
from .telephony_ingress import IngressError
from .twilio_test_capture import install_capture, CAPTURE_PATH
from .twilio_bridge import (BridgeError, BridgeStore, TwilioClient, TwilioConfig,
    conference_twiml, direct_twiml, unavailable_twiml, verify_signature, sid, proof_header_paths, flag)

APPROVED_MANIFEST = "2eaa539728bb43ae6e33d61f7e359f7d34419d4ec443c66b6ea953efd15ca467"
TWILIO_PATHS = frozenset({"/twilio/voice", "/twilio/conference-status", "/twilio/participant-status",
    "/twilio/dial-ended", "/twilio/caller-status", "/twilio/clip/{phrase_id}.wav", "/twilio/tester/fixture.wav", CAPTURE_PATH})
FAST_EVENTS = frozenset({"speech-update", "user-interrupted"})
TERMINAL_CALLS = frozenset({"completed", "canceled", "failed", "busy", "no-answer"})


def load_test_fixture(path, expected):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.stat().st_size > 2*1024*1024:
        raise ValueError("Invalid isolated test fixture.")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("Isolated test fixture changed.")
    with wave.open(io.BytesIO(data), "rb") as audio:
        if (audio.getnchannels() != 1 or audio.getsampwidth() != 2 or audio.getframerate() not in {8000,16000}
                or not 0 < audio.getnframes()/audio.getframerate() <= 30):
            raise ValueError("Unsupported isolated test fixture format.")
    return data


class TwilioLane:
    def __init__(self, app, config):
        self.app, self.config = app, config
        path = Path(app.state.store.path)
        self.store = BridgeStore(path.with_name(path.stem + "-twilio.sqlite3"))
        self.client = None
        self.closing = False
        self.tasks = set()
        self.bindings = {}
        self.proofs = {}
        self.watchdogs = {}
        self.ending = set()
        self.wav = None
        self.wav_sha = None
        self.clips = {}
        self.started = False
        self.observed_proof_paths = ()
        self.fixture = None
        self.test_capture = None
        self.coordinator = ListeningCoordinator(ListeningPolicy(connection_capacity=2), "",
            self.context, self.dispatch, self.observe)

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self._done)
        return task

    def _done(self, task):
        self.tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def start(self, settings):
        if self.started:
            return
        if self.client is None:
            self.client = TwilioClient(self.config)
        self.coordinator.key = settings.get("TYPESAFE_API_KEY", "")
        if flag(settings, "SERVICEVOICE_TWILIO_TESTER"):
            self.fixture = await asyncio.to_thread(load_test_fixture,
                settings.get("SERVICEVOICE_TWILIO_TESTER_FIXTURE", ""),
                settings.get("SERVICEVOICE_TWILIO_TESTER_FIXTURE_SHA256", ""))
        if self.config.listening:
            cache = self.app.state.gemini_tts.backchannel_cache
            if cache is None or cache.manifest_sha256 != APPROVED_MANIFEST:
                raise ValueError("Listening cues require the approved audio bank.")
            for phrase_id, text in LISTENING_PHRASES.items():
                item = cache.lookup_pcm(text, 8000)
                if item is None:
                    raise ValueError("Missing approved telephone cue.")
                pcm, metadata = item
                if not 0 < len(pcm)/16000 <= 1.5:
                    raise ValueError("Listening clip exceeds its audio budget.")
                stream = io.BytesIO()
                with wave.open(stream, "wb") as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(8000)
                    output.writeframes(pcm)
                wav = stream.getvalue()
                self.clips[phrase_id] = (wav, hashlib.sha256(wav).hexdigest())
            self.wav, self.wav_sha = self.clips["neutral_ack"]
            await self.coordinator.policy.start()
        # Restart cannot reopen rooms with stale speaker state or fresh budgets.
        for row in await asyncio.to_thread(self.store.active):
            await self.finish(row["id"])
        self.started = True

    async def close(self):
        self.closing = True
        if self.test_capture:
            await self.test_capture.close()
        for task in self.watchdogs.values():
            task.cancel()
        await self.coordinator.close()
        if self.client:
            for row in await asyncio.to_thread(self.store.active):
                await self.finish(row["id"])
        if self.tasks:
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        if self.client:
            await self.client.close()

    def admission_guard(self, message):
        if not self.config.sip_headers_path:
            self.observed_proof_paths = proof_header_paths(message)
        try:
            bridge_id = self.store.accept_proof(self.config, message)
        except BridgeError as exc:
            raise IngressError(exc.code, exc.status) from None
        self.proofs[message["call"]["id"]] = bridge_id

    async def admitted(self, message, result):
        call_id = message["call"]["id"]
        bridge_id = self.proofs.pop(call_id, None)
        if bridge_id is None:
            raise IngressError("bridge_proof_missing", 403)
        session_id = result["assistantOverrides"]["variableValues"]["session_id"]
        try:
            await asyncio.to_thread(self.store.bind_session, bridge_id, call_id, session_id)
        except BridgeError:
            # Hang-up can win after admission commits but before bridge binding.
            await asyncio.to_thread(self.app.state.telephony_ingress.end_bridge_binding, call_id, session_id)
            raise
        if self.closing:
            await self.finish(bridge_id)
            raise IngressError("bridge_closing", 503)
        self.bindings[call_id] = (bridge_id, session_id)
        if self.config.topology == "direct_sip":
            with self.store.db() as db:
                db.execute("UPDATE twilio_bridges SET state='joined' WHERE id=? AND state='waiting'", (bridge_id,))
        if self.config.listening and self.config.topology == "conference":
            self.coordinator.register(call_id, session_id)
        self.observe(call_id, "call_admitted", "approved")

    def fast_event(self, message):
        kind = message.get("type")
        if not isinstance(kind, str):
            return False
        fast = kind in FAST_EVENTS or kind == "transcript" and message.get("transcriptType") == "partial"
        if not fast:
            return False
        call = message.get("call")
        ingress = self.app.state.telephony_ingress
        call_id = ingress._identity(call, message.get("assistant"), assistant_required=True)
        binding = self.bindings.get(call_id)
        overrides = call.get("assistantOverrides")
        if binding is None or not isinstance(overrides, dict) or overrides.get("variableValues") != {"session_id": binding[1]}:
            raise IngressError("bridge_event_not_admitted", 403)
        self.coordinator.event(call_id, message)
        return True

    def event(self, call_id, message):
        if call_id in self.bindings:
            self.coordinator.event(call_id, message)

    def waiting_status(self, call_id, session_id):
        binding = self.bindings.get(call_id)
        if self.closing or binding is None or binding[1] != session_id:
            return None, "speaker_state_unavailable"
        return self.coordinator.waiting_status(call_id, session_id)

    async def context(self, session_id, call_id):
        return await asyncio.to_thread(self.app.state.gemini_gateway._backchannel_context, session_id, call_id)

    def observe(self, call_id, event, reason, phrase_id=None):
        component = "listening_cue" if event in {"decision", "suppressed", "dispatched", "clip_fetched", "announcement_end", "announcement_fail"} else "twilio_bridge"
        state = self.coordinator.calls.get(call_id)
        phrase_id = phrase_id or (state.phrase_id if state else "neutral_ack")
        self.app.state.diagnose(component, event, "observed", call_id=call_id,
            attributes={"listening_reason": reason, "backchannel_phrase": phrase_id if component == "listening_cue" else "silence"})

    async def dispatch(self, call_id):
        binding = self.bindings.get(call_id)
        if not binding or self.closing:
            raise BridgeError("bridge_ended")
        row = await asyncio.to_thread(self.store.get, binding[0])
        context = await self.context(binding[1], call_id)
        state = self.coordinator.calls.get(call_id)
        if (not row or row["state"] != "joined" or state is None or not state.caller or state.assistant
                or self.coordinator.clock()-state.received > .3
                or self.coordinator.clock()-state.assistant_stopped < 1.5
                or listening_gate(state.text, state.previous, context) != "eligible"):
            raise BridgeError("cue_superseded")
        return await self.client.announce(row, state.phrase_id)

    async def dial(self, bridge_id):
        row = await asyncio.to_thread(self.store.get, bridge_id)
        try:
            result = await self.client.add_vapi(row, self.store.target(self.config, row))
            leg = result.get("call_sid")
            await asyncio.to_thread(self.store.set_leg, bridge_id, leg)
            fresh = await asyncio.to_thread(self.store.get, bridge_id)
            if fresh["state"] == "ended":
                await self.client.end_call(leg)
                await self.finish(bridge_id)
        except Exception:
            await self.finish(bridge_id, apology=True)

    def watch(self, row):
        if row["id"] not in self.watchdogs:
            self.watchdogs[row["id"]] = self.spawn(self._watch(row["id"]))

    async def _watch(self, bridge_id):
        await asyncio.sleep(15)
        row = await asyncio.to_thread(self.store.get, bridge_id)
        if row and row["state"] not in {"joined", "ended"}:
            await self.finish(bridge_id, apology=True)

    async def finish(self, bridge_id, *, apology=False):
        if bridge_id in self.ending:
            return
        self.ending.add(bridge_id)
        try:
            row = await asyncio.to_thread(self.store.get, bridge_id)
            if not row:
                return
            await asyncio.to_thread(self.store.end, bridge_id)
            call_id = row["provider_call_id"]
            if call_id:
                self.coordinator.retire(call_id)
                self.bindings.pop(call_id, None)
                self.proofs.pop(call_id, None)
            verified = not row["dial_pending"]
            if call_id and row["session_id"]:
                try:
                    await asyncio.to_thread(self.app.state.telephony_ingress.end_bridge_binding, call_id, row["session_id"])
                except IngressError:
                    # Keep ending provider legs even when local storage needs reconciliation.
                    verified = False
            # Ending the room prevents a late in-flight participant creation
            # from joining. The returned leg is also ended by dial() after readback.
            if apology:
                try:
                    await self.client.end_call(row["caller_sid"], apology=True)
                except BridgeError:
                    pass
            targets = [("call", row["caller_sid"]), ("call", row["vapi_leg_sid"])]
            if row["conference_sid"]:
                targets.insert(0, ("conference", row["conference_sid"]))
            for kind, target in targets:
                if not target:
                    continue
                plural = "Conferences" if kind == "conference" else "Calls"
                try:
                    value = await self.client.request("GET", f"/{plural}/{target}.json")
                    terminal = {"completed"} if kind == "conference" else TERMINAL_CALLS
                    if value.get("status") not in terminal:
                        if kind == "conference":
                            await self.client.end_conference(target)
                        else:
                            await self.client.end_call(target)
                        value = await self.client.request("GET", f"/{plural}/{target}.json")
                    verified = verified and value.get("status") in terminal
                except BridgeError:
                    verified = False
            # This proves Twilio legs only. Vapi terminal evidence is captured by
            # its normal authenticated end webhook and the reviewer separately.
            await asyncio.to_thread(self.store.end, bridge_id, verified=verified)
            self.observe(call_id or bridge_id, "cleanup_verified" if verified else "cleanup_pending", "twilio_legs_only")
        finally:
            self.ending.discard(bridge_id)


def install_twilio(app, settings):
    config = TwilioConfig.from_mapping(settings)
    lane = TwilioLane(app, config) if config else None
    app.state.twilio_lane = lane
    install_capture(app,lane)
    if lane:
        if app.state.telephony_ingress.config is None:
            raise ValueError("Twilio requires dedicated authenticated phone admission.")
        app.state.telephony_ingress.admission_guard = lane.admission_guard
        if config.listening and config.topology == "conference":
            app.state.gemini_gateway.waiting_cue_state = lane.waiting_status

    @app.exception_handler(BridgeError)
    async def bridge_error(request, exc):
        return JSONResponse({"error": exc.code}, status_code=exc.status)

    async def authenticated(request):
        if lane is None:
            raise HTTPException(404)
        if lane.closing or lane.client is None:
            raise HTTPException(503, "Isolated lane is unavailable.")
        suffix = request.url.path.removeprefix("/twilio/")
        try:
            query = request.scope.get("query_string", b"").decode("ascii")
        except UnicodeError:
            raise HTTPException(400) from None
        url = config.url(suffix) + ("?" + query if query else "")
        pairs = []
        if request.method == "POST":
            if request.headers.get("content-type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
                raise HTTPException(415)
            body = bytearray()
            try:
                async with asyncio.timeout(2):
                    async for chunk in request.stream():
                        body.extend(chunk)
                        if len(body) > 16384:
                            raise HTTPException(413)
                pairs = parse_qsl(body.decode("utf-8"), keep_blank_values=True, max_num_fields=80, errors="strict")
            except (UnicodeError, ValueError):
                raise HTTPException(400, "Invalid form.") from None
            except TimeoutError:
                raise HTTPException(408) from None
        if not verify_signature(config, url, pairs, request.headers.get("x-twilio-signature", "")):
            raise HTTPException(401, "Invalid Twilio signature.")
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise HTTPException(400, "Ambiguous form fields.")
        if request.method == "POST" and fields.get("AccountSid") != config.account_sid:
            raise HTTPException(403, "Wrong Twilio account.")
        return fields

    @app.post("/twilio/voice")
    async def voice(request: Request):
        fields = await authenticated(request)
        if fields.get("To") != config.phone_number or fields.get("Direction") != "inbound":
            raise HTTPException(403, "Wrong isolated line.")
        try:
            row = await asyncio.to_thread(lane.store.allocate, fields.get("CallSid"))
        except BridgeError as exc:
            if exc.code not in {"bridge_capacity", "bridge_rate_limit", "bridge_test_budget"}:
                raise
            return Response(unavailable_twiml(), media_type="application/xml")
        if row["state"] == "ended":
            return Response("<Response><Hangup /></Response>", media_type="application/xml")
        lane.watch(row)
        result = (conference_twiml(config, row["id"]) if config.topology == "conference"
                  else direct_twiml(config, row["id"], lane.store.target(config, row)))
        return Response(result, media_type="application/xml")

    @app.post("/twilio/conference-status")
    async def conference_status(request: Request):
        fields = await authenticated(request)
        name = fields.get("FriendlyName", "")
        row = await asyncio.to_thread(lane.store.get, name[3:] if name.startswith("sv-") else "")
        if not row or not sid(fields.get("ConferenceSid"), "CF"):
            raise HTTPException(403, "Unknown conference.")
        conference, call, event = fields["ConferenceSid"], fields.get("CallSid"), fields.get("StatusCallbackEvent")
        if row["conference_sid"] not in (None, conference):
            raise HTTPException(403, "Wrong conference.")
        if event == "participant-join" and call == row["caller_sid"] and fields.get("ParticipantLabel") == "caller":
            if await asyncio.to_thread(lane.store.claim_dial, row["id"], conference, call):
                lane.spawn(lane.dial(row["id"]))
        elif (event == "participant-join" and fields.get("ParticipantLabel") == "vapi"
                and sid(call, "CA") and call != row["caller_sid"] and row["vapi_leg_sid"] in (None, call)):
            await asyncio.to_thread(lane.store.set_leg, row["id"], call)
            await asyncio.to_thread(lane.store.joined, row["id"])
            if row["state"] == "ended":
                lane.spawn(lane.finish(row["id"]))
        elif event == "conference-end" or event == "participant-leave" and call in {row["caller_sid"], row["vapi_leg_sid"]}:
            lane.spawn(lane.finish(row["id"]))
        elif event in {"announcement-end", "announcement-fail"} and call == row["caller_sid"]:
            lane.observe(row["provider_call_id"] or row["id"], event.replace("-", "_"), "accepted_unverified")
        return {"ok": True}

    @app.post("/twilio/participant-status")
    async def participant_status(request: Request):
        fields = await authenticated(request)
        row = await asyncio.to_thread(lane.store.get, request.query_params.get("bridge", ""))
        if not row or not sid(fields.get("CallSid"), "CA") or row["vapi_leg_sid"] not in (None, fields["CallSid"]):
            raise HTTPException(403, "Unknown participant.")
        # This signed per-bridge callback may precede create-participant's reply.
        await asyncio.to_thread(lane.store.set_leg, row["id"], fields["CallSid"])
        if fields.get("CallStatus") == "in-progress":
            await asyncio.to_thread(lane.store.joined, row["id"])
        if row["state"] == "ended" or fields.get("CallStatus") in TERMINAL_CALLS:
            lane.spawn(lane.finish(row["id"]))
        return {"ok": True}

    @app.post("/twilio/dial-ended")
    @app.post("/twilio/caller-status")
    async def caller_status(request: Request):
        fields = await authenticated(request)
        rows = await asyncio.to_thread(lane.store.active)
        row = next((r for r in rows if r["caller_sid"] == fields.get("CallSid")), None)
        if row and (request.url.path.endswith("dial-ended") or fields.get("CallStatus") in TERMINAL_CALLS):
            lane.spawn(lane.finish(row["id"]))
        return Response("<Response><Hangup /></Response>", media_type="application/xml")

    @app.get("/twilio/clip/{phrase_id}.wav")
    async def clip(phrase_id: str, request: Request):
        await authenticated(request)
        if phrase_id != "neutral_ack" or lane.wav is None:
            raise HTTPException(404)
        # Keep the existing edge route. Twilio signs the full query, including
        # the closed-set clip selection; arbitrary text/URLs are never accepted.
        selection = request.query_params.get("cue", "neutral_ack")
        if len(request.query_params.getlist("cue")) > 1 or selection not in lane.clips:
            raise HTTPException(404)
        row = await asyncio.to_thread(lane.store.get, request.query_params.get("bridge", ""))
        if row is None or row["state"] != "joined":
            raise HTTPException(404)
        lane.observe(row["provider_call_id"] or row["id"], "clip_fetched", "accepted_unverified", selection)
        wav, sha = lane.clips[selection]
        return Response(wav, media_type="audio/wav", headers={
            "Cache-Control": "private, max-age=86400", "ETag": '"' + sha + '"'})

    @app.get("/api/twilio/readiness")
    async def local_readiness():
        if lane is None:
            return {"enabled": False}
        return {"enabled": True, "prototype": True, "started": lane.started,
                "observed_proof_paths": lane.observed_proof_paths,
                "sip_header_path_configured": bool(config.sip_headers_path),
                "clip_sha256": lane.wav_sha, "listening_enabled": config.listening}

    @app.get("/twilio/tester/fixture.wav")
    async def test_fixture(request: Request):
        await authenticated(request)
        if lane.fixture is None:
            raise HTTPException(404)
        return Response(lane.fixture, media_type="audio/wav", headers={"Cache-Control":"private, max-age=86400"})

    return lane
