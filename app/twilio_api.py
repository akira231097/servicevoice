"""Experimental HTTP integration for a separate Twilio phone line.

Only dedicated phone admission can register a listening session. Partial events
use that in-memory binding, not the scheduler worker pool. Disabled announcement
routes return inert silence; no production environment can enable this prototype.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qsl
import wave
from collections import Counter, deque

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response

from .listening_cues import CueNotSent, ListeningCoordinator, ListeningPolicy, information_question, listening_gate, neutral_listening_only, LISTENING_PHRASES
from .telephony_ingress import IngressError, IngressConfig, TelephonyIngress
from .phone_ingress_router import PhoneIngressRouter
from .twilio_media_health import OpeningMedia
from .twilio_limits import MAX_CONCURRENT_CALLS, MAX_PROTOTYPE_CALLS
from .listening_audio import load_variations
from .twilio_test_capture import install_capture, CAPTURE_PATH
from .twilio_bridge import (BridgeError, BridgeStore, TwilioClient, TwilioConfig,
    conference_twiml, direct_twiml, unavailable_twiml, verify_signature, sid, proof_header_paths, flag)

APPROVED_MANIFEST = "2eaa539728bb43ae6e33d61f7e359f7d34419d4ec443c66b6ea953efd15ca467"
TWILIO_PATHS = frozenset({"/twilio/voice", "/twilio/conference-status", "/twilio/participant-status",
    "/twilio/dial-ended", "/twilio/caller-status", "/twilio/clip/{phrase_id}.wav", "/twilio/tester/fixture.wav", CAPTURE_PATH})
FAST_EVENTS = frozenset({"speech-update", "user-interrupted"})
TERMINAL_CALLS = frozenset({"completed", "canceled", "failed", "busy", "no-answer"})
CLIP_REFUSALS = frozenset({"recovery_disabled", "clip_unavailable", "invalid_selection", "bridge_unavailable",
    "ticket_missing", "binding_mismatch", "ticket_expired", "speaker_priority", "no_neutral_clip",
    "clip_internal_error", "lane_unavailable", "question_finished", "caller_turn_finished"})


class ClipRefusal(Exception):
    pass


def silent_announcement():
    """A refused announcement must never trigger Twilio's spoken HTTP error."""
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(8000)
        audio.writeframes(bytes(640*2))  # 80 ms of valid PCM silence.
    return Response(stream.getvalue(), media_type="audio/wav", headers={"Cache-Control": "no-store"})


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
        self.ended_bindings = {}  # Bounded no-op receipts, never live admission.
        self.proofs = {}
        self.watchdogs = {}
        self.ending = set()
        self.locks = {}
        self.dialing = set()
        self.sweeper = None
        self.wav = None
        self.wav_sha = None
        self.clips = {}
        self.announcements = {}
        self.media = {}
        self.recovery_announcements = {}
        self.caller_left = set()
        self.cleanup_retry = {}
        self.media_alerted = set()
        self.clip_outcomes = deque(maxlen=100)
        self.clip_refusals = Counter()
        self.clip_refusal_alert = False
        self.ingress = getattr(app.state.telephony_ingress, "isolated", app.state.telephony_ingress)
        self.phone_identity = self.ingress.config
        self.started = False
        self.observed_proof_paths = ()
        self.fixture = None
        self.test_capture = None
        self.coordinator = ListeningCoordinator(ListeningPolicy(connection_capacity=MAX_CONCURRENT_CALLS), "",
            self.context, self.dispatch, self.observe)

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self._done)
        return task

    def _done(self, task):
        self.tasks.discard(task)
        if not task.cancelled():
            if task.exception() is not None:
                self.observe("twilio-background", "background_failure", "unavailable")

    def lock(self, bridge_id):
        # The durable prototype's lifetime admission quota bounds this registry.
        return self.locks.setdefault(bridge_id, asyncio.Lock())

    async def _sweep(self):
        delay = 2
        while not self.closing:
            await asyncio.sleep(delay)
            try:
                await self.sweep_once()
                delay = 2
            except Exception:
                delay = min(30, delay*2)
                self.observe("twilio-background", "background_failure", "unavailable")

    async def sweep_once(self):
        for row in await asyncio.to_thread(self.store.active):
            try:
                if row["state"] == "ended":
                    if self.coordinator.clock() >= self.cleanup_retry.get(row["id"], (0, 0))[1]:
                        await self.finish(row["id"])
                elif row["state"] == "joined":
                    await self.recover_opening(row["id"])
            except Exception:
                # One unavailable bridge must not prevent other cleanup work.
                self.observe(row["provider_call_id"] or row["id"], "background_failure", "unavailable")

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
            if settings.get("SERVICEVOICE_LISTENING_AUDIO_MANIFEST"):
                self.clips.update(await asyncio.to_thread(load_variations,
                    settings["SERVICEVOICE_LISTENING_AUDIO_MANIFEST"], settings.get("SERVICEVOICE_LISTENING_AUDIO_SHA256")))
            self.coordinator.available_phrases = set(self.clips)
            await self.coordinator.policy.start()
        # Restart cannot reopen rooms with stale speaker state or fresh budgets.
        for row in await asyncio.to_thread(self.store.active):
            await self.finish(row["id"])
        self.started = True
        self.sweeper = asyncio.create_task(self._sweep())

    async def close(self):
        self.closing = True
        if self.sweeper:
            self.sweeper.cancel()
            await asyncio.gather(self.sweeper, return_exceptions=True)
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
        if not self.owns_message(message):
            return
        if not self.config.sip_headers_path:
            self.observed_proof_paths = proof_header_paths(message)
        try:
            bridge_id = self.store.accept_proof(self.config, message)
        except BridgeError as exc:
            raise IngressError(exc.code, exc.status) from None
        self.proofs[message["call"]["id"]] = bridge_id

    async def admitted(self, message, result):
        if not self.owns_message(message):
            return
        call_id = message["call"]["id"]
        bridge_id = self.proofs.pop(call_id, None)
        if bridge_id is None:
            raise IngressError("bridge_proof_missing", 403)
        session_id = result["assistantOverrides"]["variableValues"]["session_id"]
        async with self.lock(bridge_id):
            try:
                await asyncio.to_thread(self.store.bind_session, bridge_id, call_id, session_id)
                if self.closing:
                    raise BridgeError("bridge_closing", 503)
            except BridgeError:
                await asyncio.to_thread(self.app.state.telephony_ingress.end_bridge_binding, call_id, session_id)
                self.coordinator.retire(call_id)
                self.bindings.pop(call_id, None)
                raise
            self.bindings[call_id] = (bridge_id, session_id)
            self.media.setdefault(bridge_id, OpeningMedia(self.coordinator.clock()))
            if self.config.topology == "direct_sip":
                with self.store.db() as db:
                    db.execute("UPDATE twilio_bridges SET state='joined' WHERE id=? AND state='waiting'", (bridge_id,))
            if self.config.listening and self.config.topology == "conference":
                if not self.coordinator.register(call_id, session_id):
                    self.observe(call_id, "listening_unavailable", "capacity")
            self.observe(call_id, "call_admitted", "approved")

    def fast_event(self, message):
        kind = message.get("type")
        if not isinstance(kind, str):
            return False
        fast = kind in FAST_EVENTS or kind == "transcript" and message.get("transcriptType") == "partial"
        if not fast:
            return False
        if not self.owns_message(message) and (message.get("call") or {}).get("id") not in self.bindings:
            return False
        call = message.get("call")
        ingress = self.ingress
        call_id = ingress._identity(call, message.get("assistant"), assistant_required=True)
        binding = self.bindings.get(call_id)
        overrides = call.get("assistantOverrides")
        if binding is None and self.ended_bindings.get(call_id) is not None:
            if (isinstance(overrides, dict)
                    and overrides.get("variableValues") == {"session_id": self.ended_bindings[call_id]}):
                # Vapi may finish a transcript after the room has closed. The
                # HTTP route already authenticated the request; _identity above
                # checks org, phone and assistant. Never restore listening or
                # enqueue scheduler work for this exact retired binding.
                return True
        if binding is None or not isinstance(overrides, dict) or overrides.get("variableValues") != {"session_id": binding[1]}:
            raise IngressError("bridge_event_not_admitted", 403)
        self.event(call_id, message)
        return True

    def event(self, call_id, message):
        if call_id in self.bindings:
            media = self.media.get(self.bindings[call_id][0])
            if media:
                had_words = media.words
                media.provider(message, self.coordinator.clock())
                if media.words and not had_words:
                    self.observe(call_id, "caller_words_observed", "approved")
            self.coordinator.event(call_id, message)

    async def recover_opening(self, bridge_id):
        """Observe only: VAD without ASR is not proof of a broken audio path.

        Automatic reconnect is disabled until we have positive media evidence
        and a recoverable handover. In particular, never detach or hang up a
        possibly working call because a diagnostic or provider request fails.
        """
        async with self.lock(bridge_id):
            row = await asyncio.to_thread(self.store.get, bridge_id)
            media = self.media.get(bridge_id)
            if (not row or row["state"] != "joined" or self.closing or bridge_id in self.caller_left
                    or bridge_id in self.media_alerted or media is None or not media.suspicion(self.coordinator.clock())):
                return
            self.media_alerted.add(bridge_id)
            self.observe(row["provider_call_id"], "media_suspected", "caller_energy_without_words")

    def owns_message(self, message):
        call = message.get("call") if isinstance(message, dict) else None
        identity = self.phone_identity
        return (isinstance(call, dict) and identity is not None
                and call.get("orgId") == identity.organization_id
                and call.get("phoneNumberId") == identity.phone_number_id)

    async def before_answer(self, call_id, session_id):
        state = self.coordinator.calls.get(call_id)
        if state is None or state.session_id != session_id:
            return
        state.answer_ready = True
        state.answer_ready_at = self.coordinator.wall()
        state.clip_pending_until = 0
        # Give ready answer text to synthesis immediately. Invalidating tickets
        # prevents any unfetched cue from starting, but is not cancellation of
        # a clip Twilio already fetched. Keep that estimate for cue gating only.
        for ticket, item in list(self.announcements.items()):
            if item["call_id"] == call_id:
                self.announcements.pop(ticket, None)
        if state.clip_until > self.coordinator.clock():
            self.observe(call_id, "answer_released", "fetched_clip_active")

    def waiting_status(self, call_id, session_id, follow_up=False):
        binding = self.bindings.get(call_id)
        if self.closing or binding is None or binding[1] != session_id:
            return None, "speaker_state_unavailable"
        stamp=self.coordinator.refundable_waiting(call_id,session_id)
        if stamp is not None:
            refunded,_=self.app.state.gemini_gateway.backchannel_budget.refund(call_id,stamp,preserve_phrase=True)
            if refunded:self.coordinator.reclaim_waiting(call_id,session_id,stamp)
        return self.coordinator.waiting_status(call_id, session_id, follow_up)

    def commit_waiting(self, call_id, session_id, follow_up=False):
        if self.waiting_status(call_id, session_id, follow_up)[1] != "eligible":
            return False
        return self.coordinator.commit_waiting(call_id, session_id, follow_up)

    def fallback_ready(self, call_id, session_id, text):
        return self.coordinator.fallback_ready(call_id, session_id, text)

    async def context(self, session_id, call_id):
        return await asyncio.to_thread(self.app.state.gemini_gateway._backchannel_context, session_id, call_id)

    def observe(self, call_id, event, reason, phrase_id=None):
        component = "listening_cue" if event in {"decision", "suppressed", "dispatched", "clip_fetched", "clip_silenced", "announcement_end", "announcement_fail"} else "twilio_bridge"
        state = self.coordinator.calls.get(call_id)
        phrase_id = phrase_id or (state.phrase_id if state else "neutral_ack")
        self.app.state.diagnose(component, event, "observed", call_id=call_id,
            attributes={"listening_reason": reason, "backchannel_phrase": phrase_id if component == "listening_cue" else "silence"})

    def clip_outcome(self, call_id, reason=None):
        self.clip_outcomes.append(reason is not None)
        if reason:
            self.clip_refusals[reason] += 1
        elevated = len(self.clip_outcomes) >= 10 and sum(self.clip_outcomes) >= 2 and sum(self.clip_outcomes)/len(self.clip_outcomes) > .05
        if elevated and not self.clip_refusal_alert:
            self.app.state.diagnose("twilio_bridge", "clip_refusal_alert", "failed", call_id=call_id,
                attributes={"listening_reason": "excess_clip_refusals"})
        self.clip_refusal_alert = elevated

    def neutral_clip(self, state):
        rotation = ("neutral_ack", "uh_huh_1", "neutral_ack", "uh_huh_2")
        previous = state.last_delivered_phrase
        choices = [(i, rotation[i % 4]) for i in range(state.neutral_cursor, state.neutral_cursor+4)
            if rotation[i % 4] in self.clips and rotation[i % 4] != previous]
        # Prefer different words, while alternating the two Uh-huh recordings.
        varied = [(i, key) for i, key in choices if not (key.startswith("uh_huh_") and previous.startswith("uh_huh_"))]
        choices = varied or choices
        if not choices:
            return None
        index, selection = choices[0]
        state.neutral_cursor = index+1
        return selection

    async def dispatch(self, call_id):
        binding = self.bindings.get(call_id)
        if not binding or self.closing:
            raise CueNotSent("bridge_ended")
        row = await asyncio.to_thread(self.store.get, binding[0])
        context = await self.context(binding[1], call_id)
        state = self.coordinator.calls.get(call_id)
        if (not row or row["state"] != "joined" or state is None or state.assistant or state.answer_ready
                or not self.coordinator.listening_window(state)
                or self.coordinator.clock()-state.assistant_stopped < 1.5
                or listening_gate(state.text, state.previous, context) != "eligible"):
            raise CueNotSent("cue_superseded")
        # The HTTP deadline is not remote cancellation. A late clip fetch is
        # separately rejected below, and the guard follows its expected end.
        now = self.coordinator.clock()
        self.announcements = {key: item for key, item in self.announcements.items() if item["expires"] > now}
        ticket = secrets.token_hex(16)
        self.announcements[ticket] = {"call_id": call_id, "bridge_id": row["id"],
            "epoch": state.epoch, "expires": now+.75, "phrase_id": state.phrase_id}
        state.clip_pending_until = now+.75
        return await self.client.announce(row, state.phrase_id, ticket=ticket)

    async def dial(self, bridge_id):
        row = await asyncio.to_thread(self.store.get, bridge_id)
        self.dialing.add(bridge_id)
        failed = False
        try:
            result = await self.client.add_vapi(row, self.store.target(self.config, row))
            leg = result.get("call_sid")
            await asyncio.to_thread(self.store.set_leg, bridge_id, leg)
            fresh = await asyncio.to_thread(self.store.get, bridge_id)
            if fresh["state"] == "ended":
                await self.client.end_call(leg)
        except Exception as exc:
            failed = True
            if isinstance(exc, BridgeError) and exc.definitive:
                await asyncio.to_thread(self.store.resolve_dial, bridge_id)
            self.observe(row["provider_call_id"] or bridge_id, "dial_failed", "unavailable")
        finally:
            self.dialing.discard(bridge_id)
        fresh = await asyncio.to_thread(self.store.get, bridge_id)
        if failed or fresh["state"] == "ended":
            await self.finish(bridge_id, apology=failed and fresh["state"] != "ended")

    def watch(self, row):
        if row["id"] not in self.watchdogs:
            self.watchdogs[row["id"]] = self.spawn(self._watch(row["id"]))

    async def _watch(self, bridge_id):
        await asyncio.sleep(15)
        row = await asyncio.to_thread(self.store.get, bridge_id)
        if row and row["state"] not in {"joined", "ended"}:
            await self.finish(bridge_id, apology=True)

    async def finish(self, bridge_id, *, apology=False):
        async with self.lock(bridge_id):
            try:
                await self._finish(bridge_id, apology=apology)
            finally:
                attempts = self.cleanup_retry.get(bridge_id, (0, 0))[0]+1
                self.cleanup_retry[bridge_id] = (attempts, self.coordinator.clock()+min(30, 2**min(attempts, 5)))
                row = await asyncio.to_thread(self.store.get, bridge_id)
                if row is None or row["cleanup_verified"]:
                    self.cleanup_retry.pop(bridge_id, None)

    async def _finish(self, bridge_id, *, apology=False):
        self.ending.add(bridge_id)
        try:
            row = await asyncio.to_thread(self.store.get, bridge_id)
            if not row:
                return
            await asyncio.to_thread(self.store.end, bridge_id)
            # Admission may have committed its identity after the first read.
            row = await asyncio.to_thread(self.store.get, bridge_id)
            call_id = row["provider_call_id"]
            if call_id:
                if row["state"] == "ended" and row["session_id"]:
                    self.ended_bindings[call_id] = row["session_id"]
                    while len(self.ended_bindings) > MAX_PROTOTYPE_CALLS:
                        self.ended_bindings.pop(next(iter(self.ended_bindings)))
                self.coordinator.retire(call_id)
                self.bindings.pop(call_id, None)
                self.proofs.pop(call_id, None)
            self.media.pop(bridge_id, None)
            verified = not row["dial_pending"]
            local_clean = True
            if call_id and row["session_id"]:
                try:
                    await asyncio.to_thread(self.app.state.telephony_ingress.end_bridge_binding, call_id, row["session_id"])
                except IngressError:
                    # Keep ending provider legs even when local storage needs reconciliation.
                    verified = False
                    local_clean = False
            # Ending the room prevents a late in-flight participant creation
            # from joining. The returned leg is also ended by dial() after readback.
            if apology:
                await asyncio.to_thread(self.store.apology, bridge_id, time.time()+8)
                try:
                    await self.client.end_call(row["caller_sid"], apology=True)
                    return  # <Say><Hangup> owns the caller until it completes.
                except BridgeError as exc:
                    if not exc.definitive:
                        return
                    await asyncio.to_thread(self.store.apology, bridge_id, 0)
            elif row["apology_until"] > time.time():
                caller = await self.client.request("GET", f"/Calls/{row['caller_sid']}.json")
                if caller.get("status") not in TERMINAL_CALLS:
                    return
            if row["dial_pending"] and bridge_id not in self.dialing:
                try:
                    pending = await self.client.pending_leg(row)
                    if pending:
                        await asyncio.to_thread(self.store.set_leg, bridge_id, pending)
                        row = await asyncio.to_thread(self.store.get, bridge_id)
                        verified = local_clean
                except (BridgeError, AttributeError):
                    verified = False
            room_ended = False
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
                    if kind == "conference":
                        room_ended = value.get("status") == "completed"
                except BridgeError:
                    verified = False
            if (row["dial_pending"] and bridge_id not in self.dialing and room_ended
                    and time.time()-(row["ended"] or time.time()) >= 15):
                try:
                    if await self.client.dial_quiescent(row):
                        await asyncio.to_thread(self.store.resolve_dial, bridge_id)
                        # Retry from a new row; do not reuse verification from
                        # a snapshot whose pending dial has just changed.
                        return
                except (BridgeError, AttributeError):
                    pass
            # This proves Twilio legs only. Vapi terminal evidence is captured by
            # its normal authenticated end webhook and the reviewer separately.
            await asyncio.to_thread(self.store.end, bridge_id, verified=verified)
            self.observe(call_id or bridge_id, "cleanup_verified" if verified else "cleanup_pending", "twilio_legs_only")
        finally:
            self.ending.discard(bridge_id)


def install_twilio(app, settings):
    config = TwilioConfig.from_mapping(settings)
    keys = ("VAPI_TWILIO_ORG_ID", "VAPI_TWILIO_PHONE_NUMBER_ID", "VAPI_TWILIO_ASSISTANT_ID", "VAPI_TWILIO_WEBHOOK_SECRET")
    if config and any(settings.get(k) for k in keys):
        identity = IngressConfig(*(settings.get(k) for k in keys))
        primary = app.state.telephony_ingress
        if primary.config is None:
            raise ValueError("Primary phone ingress must be configured.")
        if identity.webhook_secret == primary.config.webhook_secret:
            raise ValueError("Additional isolated phone requires its own credential.")
        isolated = TelephonyIngress(app.state.store, identity, limits=primary.limits,
            live_admission=app.state.live_admission)
        app.state.telephony_ingress = PhoneIngressRouter(primary, isolated, app.state.store)
    lane = TwilioLane(app, config) if config else None
    app.state.twilio_lane = lane
    install_capture(app,lane)
    if lane:
        if app.state.capacity.max_live_calls < MAX_CONCURRENT_CALLS:
            raise ValueError("Twilio requires application capacity for four live calls.")
        if app.state.telephony_ingress.config is None:
            raise ValueError("Twilio requires dedicated authenticated phone admission.")
        lane.ingress.admission_guard = lane.admission_guard
        if config.listening and config.topology == "conference":
            app.state.gemini_gateway.waiting_cue_state = lane.waiting_status
            app.state.gemini_gateway.closing_ready = lane.coordinator.closing_ready
            app.state.gemini_gateway.closing_status = lane.coordinator.closing_status
            app.state.gemini_gateway.waiting_neutral_only = lane.coordinator.waiting_neutral_only
            app.state.gemini_gateway.waiting_cue_commit = lane.commit_waiting
            app.state.gemini_gateway.waiting_cue_assistant = lane.phone_identity.assistant_id
            app.state.gemini_gateway.before_answer = lane.before_answer
            app.state.gemini_gateway.waiting_fallback_ready = lane.fallback_ready
            app.state.gemini_gateway.waiting_content_finished = lane.coordinator.mark_waiting_finished
            app.state.gemini_gateway.waiting_cue_track = lane.coordinator.track_waiting

    @app.exception_handler(BridgeError)
    async def bridge_error(request, exc):
        return JSONResponse({"error": exc.code}, status_code=exc.status)

    async def authenticated(request):
        if lane is None:
            raise HTTPException(404)
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
        request.state.twilio_clip_authenticated = True
        if lane.closing or lane.client is None:
            raise HTTPException(503, "Isolated lane is unavailable.")
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
        if await asyncio.to_thread(lane.store.retired_leg, row["id"], call):
            return {"ok": True}
        if event == "participant-leave" and call == row["caller_sid"] or event == "conference-end":
            lane.caller_left.add(row["id"])
        if event in {"participant-speech-start", "participant-speech-stop"}:
            media = lane.media.get(row["id"])
            role = "caller" if call == row["caller_sid"] else "agent" if call == row["vapi_leg_sid"] else None
            if media and role:
                media.conference(role, event.rsplit("-", 1)[1], fields.get("SequenceNumber"), lane.coordinator.clock())
                lane.observe(row["provider_call_id"] or row["id"],
                    "speaker_started" if event.endswith("start") else "speaker_stopped", role)
            return {"ok": True}
        if row["state"] == "recovering" and call == row["vapi_leg_sid"]:
            return {"ok": True}
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
        if row and (await asyncio.to_thread(lane.store.retired_leg, row["id"], fields.get("CallSid"))
                or request.query_params.get("epoch") not in (None, str(row["proof_expiry"]))):
            return {"ok": True}
        if not row or not sid(fields.get("CallSid"), "CA") or row["vapi_leg_sid"] not in (None, fields["CallSid"]):
            raise HTTPException(403, "Unknown participant.")
        if row["state"] == "recovering":
            return {"ok": True}
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
            lane.caller_left.add(row["id"])
            lane.spawn(lane.finish(row["id"]))
        return Response("<Response><Hangup /></Response>", media_type="application/xml")

    async def approved_clip(phrase_id: str, request: Request):
        await authenticated(request)
        request.state.twilio_clip_authenticated = True
        if phrase_id == "neutral_ack" and request.query_params.get("recovery") == "1":
            raise ClipRefusal("recovery_disabled")
        if phrase_id != "neutral_ack" or lane.wav is None:
            raise ClipRefusal("clip_unavailable")
        # Keep the existing edge route. Twilio signs the full query, including
        # the closed-set clip selection; arbitrary text/URLs are never accepted.
        selection = request.query_params.get("cue", "neutral_ack")
        if len(request.query_params.getlist("cue")) > 1 or selection not in lane.clips:
            raise ClipRefusal("invalid_selection")
        row = await asyncio.to_thread(lane.store.get, request.query_params.get("bridge", ""))
        if row is None or row["state"] != "joined":
            raise ClipRefusal("bridge_unavailable")
        ticket = request.query_params.get("ticket", "")
        item = lane.announcements.pop(ticket, None)
        state = lane.coordinator.calls.get(row["provider_call_id"])
        now = lane.coordinator.clock()
        if len(request.query_params.getlist("ticket")) != 1 or item is None:
            raise ClipRefusal("ticket_missing")
        if (state is None or item["bridge_id"] != row["id"] or item["call_id"] != row["provider_call_id"]
                or item["phrase_id"] != selection):
            raise ClipRefusal("binding_mismatch")
        if item["expires"] <= now:
            raise ClipRefusal("ticket_expired")
        if state.closed or state.assistant or state.answer_ready:
            raise ClipRefusal("speaker_priority")
        if lane.coordinator.waiting_content_finished(state):
            raise ClipRefusal("caller_turn_finished")
        if not state.caller and information_question(state.text):
            raise ClipRefusal("question_finished")
        # A caller may begin a pause during the short provider fetch interval.
        # Keep approved attention, but replace an agreement-sounding token with
        # a closed neutral clip rather than reject ordinary speaker-state churn.
        if neutral_listening_only(state):
            selection = lane.neutral_clip(state)
            if selection is None:
                raise ClipRefusal("no_neutral_clip")
            state.phrase_id = selection
        elif selection == state.last_delivered_phrase:
            selection = lane.neutral_clip(state)
            if selection is None:
                raise ClipRefusal("no_neutral_clip")
            state.phrase_id = selection
        wav, sha = lane.clips[selection]
        with wave.open(io.BytesIO(wav), "rb") as audio:
            duration = audio.getnframes()/audio.getframerate()
        state.clip_pending_until = 0
        state.clip_until = now+duration+.25
        state.last_delivered_phrase = selection
        lane.clip_outcome(row["provider_call_id"])
        lane.observe(row["provider_call_id"] or row["id"], "clip_fetched", "accepted_unverified", selection)
        return Response(wav, media_type="audio/wav", headers={
            "Cache-Control": "no-store", "ETag": '"' + sha + '"'})

    @app.get("/twilio/clip/{phrase_id}.wav")
    async def clip(phrase_id: str, request: Request):
        try:
            return await approved_clip(phrase_id, request)
        except Exception as exc:
            # Authentication/ownership still gate all real audio and mutations.
            # Even unavailable, stale, replayed or invalid requests get inert
            # media rather than an error Twilio could speak into a live call.
            if lane is not None and getattr(request.state, "twilio_clip_authenticated", False):
                bridge = request.query_params.get("bridge", "")
                bound = next((cid for cid, value in lane.bindings.items() if value[0] == bridge), None)
                requested = request.query_params.get("cue", "neutral_ack")
                reason = str(exc) if isinstance(exc, ClipRefusal) and str(exc) in CLIP_REFUSALS else "clip_internal_error"
                if isinstance(exc, HTTPException) and exc.status_code == 503:
                    reason = "lane_unavailable"
                try:
                    lane.clip_outcome(bound or "twilio-clip", reason)
                    lane.observe(bound or "twilio-clip", "clip_silenced", reason, requested if requested in lane.clips else "silence")
                except Exception:
                    pass  # Even a diagnostic failure must return silent media.
            return silent_announcement()

    @app.get("/api/twilio/readiness")
    async def local_readiness():
        if lane is None:
            return {"enabled": False}
        return {"enabled": True, "prototype": True, "started": lane.started,
                "max_concurrent_calls": MAX_CONCURRENT_CALLS, "prototype_call_limit": MAX_PROTOTYPE_CALLS,
                "application_call_capacity": app.state.capacity.max_live_calls,
                "listening_classifier_capacity": lane.coordinator.policy.connection_capacity,
                "observed_proof_paths": lane.observed_proof_paths,
                "sip_header_path_configured": bool(config.sip_headers_path),
                "clip_sha256": lane.wav_sha, "listening_enabled": config.listening,
                "automatic_opening_recovery": False, "media_health_mode": "observe_only",
                "clip_refusal_alert": lane.clip_refusal_alert, "clip_refusals": dict(lane.clip_refusals),
                "clip_fetch_window": len(lane.clip_outcomes), "clip_silenced_window": sum(lane.clip_outcomes)}

    @app.get("/twilio/tester/fixture.wav")
    async def test_fixture(request: Request):
        await authenticated(request)
        if lane.fixture is None:
            raise HTTPException(404)
        return Response(lane.fixture, media_type="audio/wav", headers={"Cache-Control":"private, max-age=86400"})

    return lane
