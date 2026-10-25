"""Bounded Gemini speech adapter for Vapi's custom-voice PCM contract.

Only approved text becomes speech; this adapter owns no conversation, tools or
booking logic. Streamed bytes are not proof of audible playback or caller receipt.
The observer receives fixed diagnostic fields, never text, audio or credentials.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import inspect
import json
import re
import time
from uuid import UUID, uuid4

import httpx
from starlette.responses import StreamingResponse

MODELS = frozenset({"gemini-3.8-flash-lite-tts", "gemini-3.8-flash-tts"})
DEFAULT_MODEL = "gemini-3.8-flash-lite-tts"
SAMPLE_RATES = frozenset({8000, 16000, 22050, 24000, 44100})
VOICES = frozenset({"Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede", "Callirrhoe", "Autonoe", "Enceladus", "Iapetus", "Umbriel", "Algieba", "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar", "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi", "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat"})
API_BASE = "https://generativelanguage.googleapis.com/v1beta/models/"
MAX_TEXT_CHARACTERS = 4000
MAX_EVENT_BYTES = 2 * 1024 * 1024
MAX_AUDIO_SECONDS = 90
ATOMIC_FAREWELL = "Thank you for calling. Goodbye."


class TTSError(RuntimeError):
    """Fixed safe codes; upstream response bodies never become exceptions/logs."""
    def __init__(self, code, status_code=502):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def voice_request(payload):
    message = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(message, dict) or message.get("type") != "voice-request":
        raise TTSError("tts_invalid_message", 400)
    text, rate = message.get("text"), message.get("sampleRate")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARACTERS:
        raise TTSError("tts_invalid_text", 400)
    if type(rate) is not int or rate not in SAMPLE_RATES:
        raise TTSError("tts_unsupported_sample_rate", 400)
    # Vapi's documented minimum voice envelope has no call or turn identifier.
    # If the authenticated request additionally provides a call UUID, retain it
    # explicitly as provider-reported evidence. Never infer a call from its text.
    call = message.get("call")
    call_id = None
    if isinstance(call, dict):
        try:call_id = str(UUID(call.get("id", "")))
        except (ValueError, TypeError, AttributeError):pass
    return text, rate, call_id


def request_body(text, sample_rate, voice="Kore"):
    """Gemini 3.8 separates exact transcript from optional performance metadata."""
    return {"contents": [{"role": "user", "parts": [{"text": text}]}],
        "generationConfig": {"responseModalities": ["AUDIO"],
            "responseFormat": {"audio": {"mimeType": "AUDIO_L16", "sampleRate": sample_rate}},
            "speechConfig": {"voiceConfig": {"voice": voice}}}}


async def sse_events(response):
    """Parse bounded SSE without buffering an entire generated audio response."""
    pending, data, size = b"", [], 0
    # Do not set chunk_size: HTTPX would coalesce small provider chunks and
    # postpone the first playable audio while waiting to fill that buffer.
    async for block in response.aiter_bytes():
        pending += block
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            line = line.rstrip(b"\r")
            if len(line) > MAX_EVENT_BYTES:
                raise TTSError("tts_upstream_event_too_large")
            if not line:
                if data:
                    yield b"\n".join(data)
                data, size = [], 0
            elif line.startswith(b"data:"):
                value = line[5:].lstrip(b" ")
                size += len(value)
                if size > MAX_EVENT_BYTES:raise TTSError("tts_upstream_event_too_large")
                data.append(value)
        if len(pending) > MAX_EVENT_BYTES:raise TTSError("tts_upstream_event_too_large")
    if pending:
        line = pending.rstrip(b"\r")
        if line.startswith(b"data:"):
            value = line[5:].lstrip(b" ")
            size += len(value)
            if size > MAX_EVENT_BYTES:raise TTSError("tts_upstream_event_too_large")
            data.append(value)
        elif line.strip() and not line.startswith(b":"):
            raise TTSError("tts_upstream_invalid_sse")
    if data:yield b"\n".join(data)


class PCMRateConverter:
    """Stateful bandlimited PCM conversion; never relabel upstream sample rates.

    Gemini currently returns 24kHz PCM even when another rate is requested.
    libsoxr carries its filter state between SSE events and flushes exactly once.
    Imports are lazy so native-rate speech remains independent of conversion.
    """
    def __init__(self, source_rate, target_rate):
        self.source_rate, self.target_rate = source_rate, target_rate
        self.converter, self.np = None, None
        if source_rate != target_rate:
            try:
                import numpy as np
                import soxr
                self.np = np
                self.converter = soxr.ResampleStream(source_rate, target_rate,
                    num_channels=1, dtype="int16", quality="HQ")
            except (ImportError, OSError):
                raise TTSError("tts_resampler_unavailable", 503) from None

    def convert(self, audio, *, last=False):
        if self.converter is None:return audio
        samples = self.np.frombuffer(audio, dtype="<i2").astype(self.np.int16, copy=False)
        try:
            return self.converter.resample_chunk(samples, last=last).astype("<i2", copy=False).tobytes()
        except Exception:
            raise TTSError("tts_resampling_failed") from None


async def pcm_chunks(response, sample_rate, *, metadata=None):
    total, dangling, received, stopped, converter = 0, b"", False, False, None
    async for event in sse_events(response):
        if event == b"[DONE]":break
        try:value = json.loads(event)
        except (ValueError, UnicodeError):raise TTSError("tts_upstream_invalid_json") from None
        if not isinstance(value, dict) or value.get("error"):
            raise TTSError("tts_upstream_error_event")
        if isinstance(value.get("promptFeedback"), dict) and value["promptFeedback"].get("blockReason"):
            raise TTSError("tts_upstream_blocked")
        candidates = value.get("candidates", [])
        if not isinstance(candidates, list) or len(candidates)>1:
            raise TTSError("tts_upstream_invalid_audio")
        for candidate in candidates:
            if not isinstance(candidate, dict):raise TTSError("tts_upstream_invalid_audio")
            finish = candidate.get("finishReason")
            if finish not in (None, "STOP"):
                raise TTSError("tts_upstream_incomplete_generation")
            content = candidate.get("content", {})
            if not isinstance(content, dict):raise TTSError("tts_upstream_invalid_audio")
            parts = content.get("parts", [])
            if not isinstance(parts, list):raise TTSError("tts_upstream_invalid_audio")
            for part in parts:
                if not isinstance(part, dict):raise TTSError("tts_upstream_invalid_audio")
                # Google's normal terminal STOP frame includes an empty text
                # part. Accept only that exact metadata after real PCM; any
                # nonempty text or early empty text is a contract violation.
                if "text" in part and not (part["text"]=="" and finish=="STOP" and received and "inlineData" not in part):
                    raise TTSError("tts_upstream_unexpected_text")
                inline = part.get("inlineData")
                if inline is None:continue
                if stopped:raise TTSError("tts_upstream_audio_after_completion")
                if not isinstance(inline, dict):raise TTSError("tts_upstream_invalid_audio")
                mime = inline.get("mimeType", "")
                if not isinstance(mime, str) or mime.split(";", 1)[0].strip().lower() not in {"audio/l16", "audio/pcm"}:
                    raise TTSError("tts_upstream_wrong_audio_format")
                declared_rate = re.search(r"(?:^|;)\s*rate\s*=\s*(\d+)(?:;|$)", mime, flags=re.I)
                if not declared_rate:raise TTSError("tts_upstream_missing_sample_rate")
                source_rate = int(declared_rate[1])
                if source_rate not in SAMPLE_RATES | {48000}:
                    raise TTSError("tts_upstream_wrong_sample_rate")
                channels = re.search(r"(?:^|;)\s*channels\s*=\s*(\d+)(?:;|$)", mime, flags=re.I)
                if channels and int(channels[1])!=1:raise TTSError("tts_upstream_wrong_channel_count")
                if converter is None:
                    converter = PCMRateConverter(source_rate, sample_rate)
                    if metadata is not None:
                        metadata.update(upstream_sample_rate=source_rate, resampled=source_rate!=sample_rate,
                            resampler="soxr_hq" if source_rate!=sample_rate else "none")
                elif source_rate != converter.source_rate:
                    raise TTSError("tts_upstream_sample_rate_changed")
                raw = inline.get("data")
                if not isinstance(raw, str) or not raw or len(raw) > MAX_EVENT_BYTES:
                    raise TTSError("tts_upstream_invalid_audio")
                try:audio = base64.b64decode(raw, validate=True)
                except (ValueError, binascii.Error):raise TTSError("tts_upstream_invalid_audio") from None
                total += len(audio)
                if total > source_rate * 2 * MAX_AUDIO_SECONDS:
                    raise TTSError("tts_audio_limit_exceeded")
                audio = dangling + audio
                dangling = audio[-1:] if len(audio) % 2 else b""
                audio = audio[:-1] if dangling else audio
                if audio:
                    if not received and audio.startswith(b"RIFF"):
                        raise TTSError("tts_upstream_unexpected_wav_header")
                    received = True
                    converted = converter.convert(audio)
                    if converted:yield converted
            if finish=="STOP":stopped=True
    if dangling:raise TTSError("tts_upstream_incomplete_pcm_sample")
    if not received:raise TTSError("tts_upstream_empty_audio")
    if not stopped:raise TTSError("tts_upstream_incomplete_generation")
    tail = converter.convert(b"", last=True)
    if tail:yield tail


class GeminiTTS:
    def __init__(self, settings, *, client=None, max_concurrency=4, timeout_seconds=25):
        if type(max_concurrency) is not int or not 1 <= max_concurrency <= 16:
            raise ValueError("TTS concurrency must be between one and sixteen.")
        if type(timeout_seconds) not in (int,float) or not 1 <= timeout_seconds <= 60:
            raise ValueError("TTS deadline must be between one and sixty seconds.")
        self.settings, self.client, self.owns_client = settings, client, client is None
        self.capacity, self.timeout = asyncio.Semaphore(max_concurrency), timeout_seconds
        self.client_lock = asyncio.Lock()
        self.resampler_ready = False
        self.backchannel_cache = None

    async def start(self):
        async with self.client_lock:
            settings = self.settings()
            if settings.get("SERVICEVOICE_BACKCHANNELS") == "on" and self.backchannel_cache is None:
                def warm_cache():
                    from pathlib import Path
                    from .backchannel_audio import AudioSpec, BackchannelAudioCache
                    path = Path(settings.get("SERVICEVOICE_BACKCHANNEL_AUDIO_MANIFEST", ""))
                    cache = BackchannelAudioCache(path.parent, AudioSpec(
                        model=settings.get("GEMINI_TTS_MODEL", DEFAULT_MODEL), voice=settings.get("GEMINI_TTS_VOICE", "Kore")))
                    cache.load_manifest(path)
                    return cache
                self.backchannel_cache = await asyncio.to_thread(warm_cache)
            if not self.resampler_ready:
                # Local imports/filter initialization belong in app readiness,
                # before the first caller. Missing conversion dependencies fail
                # startup rather than producing mislabeled telephone audio.
                PCMRateConverter(24000, 8000)
                self.resampler_ready = True
            if self.client is None:
                self.client = httpx.AsyncClient(timeout=httpx.Timeout(10, connect=3, pool=1),
                    limits=httpx.Limits(max_connections=8, max_keepalive_connections=8, keepalive_expiry=60),
                    follow_redirects=False, trust_env=False)

    async def close(self):
        async with self.client_lock:
            if self.client is not None and self.owns_client:
                await self.client.aclose()
                self.client = None

    async def response(self, payload, *, observer=None):
        text, rate, call_id = voice_request(payload)
        settings = self.settings()
        model = settings.get("GEMINI_TTS_MODEL", DEFAULT_MODEL)
        voice = settings.get("GEMINI_TTS_VOICE", "Kore")
        key = settings.get("GEMINI_API_KEY", "")
        if model not in MODELS or voice not in VOICES:
            raise TTSError("tts_invalid_configuration", 503)
        if not isinstance(key, str) or not key.strip():raise TTSError("tts_not_configured", 503)
        if self.capacity.locked():raise TTSError("tts_capacity_exhausted", 503)
        await self.capacity.acquire()
        started, request_id, written, first_ms = time.perf_counter(), str(uuid4()), 0, None
        started_at_unix=time.time()
        upstream, released, terminal = None, False, False
        audio_metadata = {}

        def observe(event, **attributes):
            nonlocal terminal
            if event in {"tts.completed", "tts.failed", "tts.cancelled"}:terminal = True
            if observer is None:return
            record = {"event": event, "request_id": request_id, "provider": "google", "model": model,
                "voice": voice, "sample_rate": rate, "input_characters": len(text),
                "provider_call_id": call_id, "provider_call_id_status": "provider_reported" if call_id else "not_provided",
                "turn_id": None, "turn_id_status": "not_provided", "audio_bytes": written,
                "started_at_unix":started_at_unix,
                "elapsed_ms": round((time.perf_counter()-started)*1000,3), **audio_metadata, **attributes}
            try:
                result = observer(record)
                # Observers must enqueue synchronously; never await telemetry in
                # the audio path. Close an accidental coroutine rather than leak it.
                if inspect.iscoroutine(result):result.close()
            except Exception:pass

        async def release():
            nonlocal released
            if released:return
            released = True
            if not terminal:
                observe("tts.cancelled", failure_phase="after_first_audio" if written else "before_first_audio", error_code="tts_transport_closed")
            try:
                if upstream is not None:
                    try:await upstream.aclose()
                    except Exception:pass  # Cleanup cannot change the observed outcome.
            finally:self.capacity.release()

        def mapped(exc):
            if isinstance(exc, TTSError):return exc
            if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
                return TTSError("tts_deadline_exceeded", 504)
            return TTSError("tts_upstream_connection_failed")

        observe("tts.request_started")
        try:
            async with asyncio.timeout(self.timeout):
                await self.start()
                hit = (self.backchannel_cache.lookup_pcm(text, rate, model=model, voice=voice)
                       if settings.get("SERVICEVOICE_BACKCHANNELS") == "on" and self.backchannel_cache is not None else None)
                if hit is not None:
                    pcm, metadata = hit
                    audio_metadata.update(metadata, synthesis_source="pregenerated_cache")
                    observe("tts.cache_hit", observation_scope="server_bytes_only_not_audible_playback")
                    async def cached_audio():yield pcm
                    audio = cached_audio()
                else:
                    audio_metadata["synthesis_source"] = "live_google"
                    request = self.client.build_request("POST", API_BASE+model+":streamGenerateContent?alt=sse",
                        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                        json=request_body(text, rate, voice))
                    upstream = await self.client.send(request, stream=True)
                    observe("tts.upstream_connected", upstream_http_status=upstream.status_code)
                    if upstream.status_code != 200:
                        code = "tts_upstream_rate_limited" if upstream.status_code == 429 else "tts_upstream_rejected"
                        raise TTSError(code, 503 if upstream.status_code == 429 else 502)
                    if "text/event-stream" not in upstream.headers.get("content-type", "").lower():
                        raise TTSError("tts_upstream_wrong_content_type")
                    audio = pcm_chunks(upstream, rate, metadata=audio_metadata)
                if (settings.get("SERVICEVOICE_TTS_ATOMIC_FAREWELL") == "on"
                        and text.strip() == ATOMIC_FAREWELL):
                    # Vapi's blocking endCall can resolve on the first tiny
                    # streamed segment, disconnecting before subsequent speech.
                    # This exact fixed farewell is bounded and fully validated
                    # before releasing any bytes; ordinary replies still stream.
                    chunks=[];size=0
                    async for chunk in audio:
                        size+=len(chunk)
                        if size>rate*2*8:raise TTSError("tts_audio_limit_exceeded")
                        chunks.append(chunk)
                    if not size:raise TTSError("tts_upstream_empty_audio")
                    pcm=b"".join(chunks)
                    await audio.aclose()
                    async def complete_farewell():yield pcm
                    audio=complete_farewell()
                    audio_metadata["synthesis_source"]="buffered_farewell"
                first = await anext(audio)
                first_ms = round((time.perf_counter()-started)*1000, 3)
                observe("tts.first_audio_ready", first_audio_ms=first_ms)
        except (asyncio.CancelledError, GeneratorExit):
            observe("tts.cancelled", failure_phase="before_first_audio", error_code="tts_client_cancelled")
            await release()
            raise
        except Exception as exc:
            error = mapped(exc)
            observe("tts.failed", failure_phase="before_first_audio", error_code=error.code)
            await release()
            raise error from None

        async def stream():
            nonlocal written
            try:
                remaining = self.timeout - (time.perf_counter()-started)
                if remaining <= 0:raise TTSError("tts_deadline_exceeded", 504)
                async with asyncio.timeout(remaining):
                    written += len(first)
                    observe("tts.audio_stream_started", first_audio_ms=first_ms,
                            observation_scope="server_bytes_only_not_audible_playback")
                    yield first
                    async for chunk in audio:
                        written += len(chunk)
                        yield chunk
                    observe("tts.completed", first_audio_ms=first_ms,
                            audio_duration_ms=round(written/(rate*2)*1000,3),
                            observation_scope="server_bytes_only_not_audible_playback")
            except (asyncio.CancelledError, GeneratorExit):
                observe("tts.cancelled", failure_phase="after_first_audio" if written else "before_first_audio", error_code="tts_client_cancelled")
                raise
            except Exception as exc:
                error = mapped(exc)
                observe("tts.failed", failure_phase="after_first_audio" if written else "before_first_audio", error_code=error.code)
                # Ending normally would misclassify a truncated utterance as a
                # successful synthesis. Abort; never retry/replay partial speech.
                raise error from None
            finally:
                try:await audio.aclose()
                finally:await release()

        class ClosingResponse(StreamingResponse):
            async def __call__(self, scope, receive, send):
                try:
                    return await super().__call__(scope, receive, send)
                finally:
                    # Also release if ASGI disconnects before the body iterator
                    # ever starts (its generator finally would not run then).
                    try:await audio.aclose()
                    finally:await release()

        return ClosingResponse(stream(), media_type="application/octet-stream",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                     "X-ServiceVoice-TTS-Request-Id": request_id})
