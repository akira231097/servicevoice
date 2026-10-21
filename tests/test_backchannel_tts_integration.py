"""Candidate-only cache integration; every upstream transport is a local fake."""
import asyncio
import base64
import hashlib
import json
from uuid import uuid4

import httpx
import pytest

from app.backchannel_audio import AudioCacheError, BackchannelAudioCache
from app.backchannel_catalog import PHRASES
from app.diagnostics import Diagnostics, read_call_diagnostics
from app.gemini_tts import DEFAULT_MODEL, GeminiTTS, TTSError
from deploy.tts_server import create_app

TEXT = PHRASES["neutral_ack"]
KEY = "synthetic-provider-key-not-real"
SECRET = "synthetic-speech-secret-not-real-" + "s" * 32
PCM = b"\x20\x03" * 3200


@pytest.fixture
def bank(tmp_path):
    cache = BackchannelAudioCache(tmp_path / "audio")
    asset = cache.store("neutral_ack", PCM)
    manifest = cache.write_manifest([asset])
    return manifest, asset


def settings(bank, **changes):
    return {"GEMINI_API_KEY": KEY, "GEMINI_TTS_BEARER_SECRET": SECRET,
        "GEMINI_TTS_MODEL": DEFAULT_MODEL, "GEMINI_TTS_VOICE": "Kore",
        "SERVICEVOICE_BACKCHANNELS": "on", "SERVICEVOICE_BACKCHANNEL_AUDIO_MANIFEST": str(bank[0]), **changes}


def envelope(text=TEXT, rate=16000, *, call_id=None):
    message = {"type": "voice-request", "text": text, "sampleRate": rate}
    if call_id:
        message["call"] = {"id": call_id}
    return {"message": message}


async def body(response):
    return b"".join([part async for part in response.body_iterator])


def provider_audio(pcm=b"\x01\x00\x02\x00", rate=16000):
    candidate = {"content": {"parts": [{"inlineData": {
        "mimeType": f"audio/L16;rate={rate}", "data": base64.b64encode(pcm).decode()}}]}, "finishReason": "STOP"}
    return b"data: " + json.dumps({"candidates": [candidate]}).encode() + b"\n\n"


def test_candidate_cache_returns_exact_bytes_without_any_provider_request(bank, monkeypatch):
    async def scenario():
        requests, rows = [], []
        def forbidden(request):
            requests.append(request)
            raise AssertionError("a warm exact-text cache hit made a provider request")
        async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
            adapter = GeminiTTS(lambda: settings(bank), client=client)
            await adapter.start()
            monkeypatch.setattr(adapter.backchannel_cache, "_read", lambda *_: pytest.fail("TTS cache hit read disk"))
            response = await adapter.response(envelope(), observer=rows.append)
            assert response.headers["content-type"] == "application/octet-stream"
            assert response.headers["cache-control"] == "no-store"
            assert await body(response) == PCM
            assert not adapter.capacity.locked()
        assert requests == []
        assert [row["event"] for row in rows] == ["tts.request_started", "tts.cache_hit",
            "tts.first_audio_ready", "tts.audio_stream_started", "tts.completed"]
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["off", "rate", "model", "voice", "different_text"])
def test_opt_out_and_configuration_or_exact_text_mismatch_keep_live_google_contract(bank, kind):
    async def scenario():
        values, requests, rows = settings(bank), [], []
        rate = 8000 if kind == "rate" else 16000
        text = TEXT + " " if kind == "different_text" else TEXT
        def provider(request):
            requests.append(request)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=provider_audio(rate=rate))
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            adapter = GeminiTTS(lambda: values, client=client)
            if kind == "off":
                values.update(SERVICEVOICE_BACKCHANNELS="off", SERVICEVOICE_BACKCHANNEL_AUDIO_MANIFEST="missing-is-irrelevant-when-off")
            else:
                await adapter.start()
                if kind == "model":
                    values["GEMINI_TTS_MODEL"] = "gemini-3.8-flash-tts"
                elif kind == "voice":
                    values["GEMINI_TTS_VOICE"] = "Puck"
            assert await body(await adapter.response(envelope(text, rate), observer=rows.append)) == b"\x01\x00\x02\x00"
        assert len(requests) == 1
        request = requests[0]
        assert request.url.host == "generativelanguage.googleapis.com"
        assert request.url.path.endswith(values["GEMINI_TTS_MODEL"] + ":streamGenerateContent")
        assert request.headers["x-goog-api-key"] == KEY
        payload = json.loads(request.content)
        assert payload["contents"][0]["parts"] == [{"text": text}]
        assert payload["generationConfig"]["responseFormat"]["audio"]["sampleRate"] == rate
        assert payload["generationConfig"]["speechConfig"]["voiceConfig"]["voice"] == values["GEMINI_TTS_VOICE"]
        assert KEY not in request.content.decode()
        assert "tts.cache_hit" not in [row["event"] for row in rows]
        assert rows[-1]["event"] == "tts.completed" and rows[-1]["synthesis_source"] == "live_google"
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["missing", "invalid", "wrong_voice", "wrong_model"])
def test_enabled_invalid_manifest_cannot_start_or_request_provider_audio(bank, tmp_path, kind):
    async def scenario():
        values, requests = settings(bank), []
        if kind == "missing":
            values["SERVICEVOICE_BACKCHANNEL_AUDIO_MANIFEST"] = str(tmp_path / ("manifest-" + "0" * 64 + ".json"))
        elif kind == "invalid":
            bank[0].write_bytes(bank[0].read_bytes() + b" ")
        elif kind == "wrong_voice":
            values["GEMINI_TTS_VOICE"] = "Puck"
        else:
            values["GEMINI_TTS_MODEL"] = "gemini-3.8-flash-tts"
        def forbidden(request):
            requests.append(request)
            raise AssertionError("startup validation requested paid audio")
        async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
            adapter = GeminiTTS(lambda: values, client=client)
            with pytest.raises(AudioCacheError):
                await adapter.start()
            assert adapter.backchannel_cache is None and not adapter.resampler_ready
            assert not adapter.capacity.locked()
            assert not requests
    asyncio.run(scenario())


def test_cached_terminal_observer_contains_json_safe_provenance_not_text_or_secrets(bank):
    async def scenario():
        rows = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("unexpected network"))) as client:
            adapter = GeminiTTS(lambda: settings(bank), client=client)
            request = envelope()
            request["private_customer_name"] = "PRIVATE-CUSTOMER-NAME"
            request["private_phone"] = "5125550109"
            assert await body(await adapter.response(request, observer=rows.append)) == PCM
        encoded = json.dumps(rows, sort_keys=True, allow_nan=False)
        assert json.loads(encoded) == rows
        completed = rows[-1]
        asset = bank[1]
        assert completed["event"] == "tts.completed" and completed["cache_hit"] is True
        assert completed["cache_asset_id"] == asset["asset_id"]
        assert completed["cache_request_sha256"] == asset["request_sha256"]
        assert completed["cache_manifest_sha256"] == hashlib.sha256(bank[0].read_bytes()).hexdigest()
        assert completed["audio_bytes"] == len(PCM) and completed["cached_sample_rate"] == 16000
        assert completed["audio_duration_ms"] == asset["duration_ms"] == 200
        assert completed["synthesis_source"] == "pregenerated_cache"
        assert completed["observation_scope"] == "server_bytes_only_not_audible_playback"
        assert completed["provider_call_id"] is None and completed["turn_id"] is None
        for private in (TEXT, KEY, SECRET, "PRIVATE-CUSTOMER-NAME", "5125550109", str(bank[0])):
            assert private not in encoded
    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["before_body", "after_first_chunk"])
def test_cached_cancellation_releases_capacity_without_claiming_completion(bank, phase):
    async def scenario():
        rows = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("unexpected network"))) as client:
            adapter = GeminiTTS(lambda: settings(bank), client=client, max_concurrency=1)
            response = await adapter.response(envelope(), observer=rows.append)
            with pytest.raises(TTSError, match="capacity_exhausted"):
                await adapter.response(envelope())
            if phase == "after_first_chunk":
                assert await anext(response.body_iterator) == PCM
                await response.body_iterator.aclose()
            else:
                async def receive():
                    return {"type": "http.disconnect"}
                async def send(_):
                    raise asyncio.CancelledError()
                with pytest.raises(asyncio.CancelledError):
                    await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
            assert not adapter.capacity.locked()
            assert rows[-1]["event"] == "tts.cancelled"
            assert rows[-1]["failure_phase"] == ("before_first_audio" if phase == "before_body" else "after_first_audio")
            assert not any(row["event"] == "tts.completed" for row in rows)
            assert await body(await adapter.response(envelope())) == PCM
    asyncio.run(scenario())


def test_broken_or_async_observers_cannot_change_cached_audio(bank):
    async def scenario():
        def broken(_):
            raise RuntimeError("synthetic observer error with private text")
        async def accidental_async(_):
            raise AssertionError("async observer must be closed, never awaited")
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("unexpected network"))) as client:
            adapter = GeminiTTS(lambda: settings(bank), client=client, max_concurrency=1)
            for observer in (broken, accidental_async):
                assert await body(await adapter.response(envelope(), observer=observer)) == PCM
                assert not adapter.capacity.locked()
    asyncio.run(scenario())


def test_cached_source_rate_hash_and_duration_survive_content_free_disk_diagnostics(bank, tmp_path):
    async def scenario():
        ledger, call_id = tmp_path / "diagnostics.sqlite3", str(uuid4())
        sink = Diagnostics(ledger)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("unexpected network"))) as upstream:
            values = settings(bank)
            adapter = GeminiTTS(lambda: values, client=upstream)
            app = create_app(values, adapter=adapter, diagnostics=sink)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://offline") as client:
                    response = await client.post("/tts/vapi", headers={"authorization": "Bearer " + SECRET},
                        json=envelope(call_id=call_id))
                    assert response.status_code == 200 and response.content == PCM
        report = read_call_diagnostics(ledger, call_id)
        rows = [row for row in report["events"] if row["event"] in {"tts.cache_hit", "tts.completed"}]
        assert len(rows) == 2
        for row in rows:
            metadata = row["attributes"]
            assert metadata["cache_hit"] is True and metadata["synthesis_source"] == "pregenerated_cache"
            assert metadata["cached_sample_rate"] == 16000
            assert metadata["cache_asset_id"] == bank[1]["asset_id"]
            assert metadata["cache_request_sha256"] == bank[1]["request_sha256"]
            assert metadata["cache_manifest_sha256"] == hashlib.sha256(bank[0].read_bytes()).hexdigest()
            assert metadata["audio_duration_ms"] == 200
            assert metadata["observation_scope"] == "server_bytes_only_not_audible_playback"
        encoded = json.dumps(report, allow_nan=False)
        for private in (TEXT, KEY, SECRET, call_id, str(bank[0])):
            assert private not in encoded and private.encode() not in ledger.read_bytes()
    asyncio.run(scenario())
