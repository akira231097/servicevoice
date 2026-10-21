"""Both speech entrypoints retain cache evidence without claiming audible playback."""
import asyncio
from contextlib import AsyncExitStack
import hashlib
import importlib
import json
from uuid import uuid4

import dotenv
import httpx
import pytest

from app.backchannel_audio import BackchannelAudioCache
from app.backchannel_catalog import PHRASES
from app.diagnostics import Diagnostics, read_call_diagnostics
from app.gemini_tts import GeminiTTS
from deploy.tts_server import create_app as create_standalone_app


@pytest.mark.parametrize("deployment", ["integrated", "standalone"])
def test_real_cached_pcm_keeps_timing_and_lineage_in_both_public_ledgers(tmp_path, monkeypatch, deployment):
    # This is synthetic PCM, not a recording or evidence of human listening.
    pcm = b"\x01\x00\xff\xff" * 2000
    cache = BackchannelAudioCache(tmp_path / "audio")
    asset = cache.store("moment", pcm)
    manifest = cache.write_manifest([asset])
    values = {
        "GEMINI_API_KEY": "fictional-provider-key-123456",
        "GEMINI_TTS_BEARER_SECRET": "fictional-speech-secret-" + "x" * 40,
        "SERVICEVOICE_BACKCHANNELS": "on",
        "SERVICEVOICE_BACKCHANNEL_AUDIO_MANIFEST": str(manifest),
    }
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr(dotenv, "dotenv_values", lambda *a, **k: {})
    monkeypatch.setenv("SERVICEVOICE_DB", str(tmp_path / "ambient.sqlite3"))
    monkeypatch.setenv("SERVICEVOICE_TELEPHONY_ENABLED", "off")
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    call = str(uuid4())
    ledger = tmp_path / "diagnostics.sqlite3"
    sink = Diagnostics(ledger)
    requests = []

    def forbidden_upstream(request):
        requests.append(request)
        raise AssertionError("A warmed cache hit must not call a speech provider.")

    async def run():
        async with AsyncExitStack() as resources:
            resources.callback(sink.close, 2)
            upstream = await resources.enter_async_context(httpx.AsyncClient(transport=httpx.MockTransport(forbidden_upstream)))
            adapter = GeminiTTS(lambda: values, client=upstream)
            if deployment == "integrated":
                module = importlib.import_module("app.main")
                console = module.create_app(tmp_path / "app.sqlite3")
                console.state.gemini_tts = adapter
                console.state.diagnostics = sink
                app = module.create_public_app(console)
                await adapter.start()
                resources.push_async_callback(adapter.close)
                resources.push_async_callback(console.state.gemini_gateway.close)
            else:
                app = create_standalone_app(values, adapter=adapter, diagnostics=sink)
                await resources.enter_async_context(app.router.lifespan_context(app))
            client = await resources.enter_async_context(httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"))
            reply = await client.post("/tts/vapi", headers={
                "authorization": "Bearer " + values["GEMINI_TTS_BEARER_SECRET"]}, json={
                "message": {"type": "voice-request", "text": PHRASES["moment"]+" ",
                    "sampleRate": 16000, "call": {"id": call}},
                "unrelated_private_text": "PRIVATE CUSTOMER 5125550142"})
            assert reply.status_code == 200
            assert reply.content == pcm
            assert reply.headers["content-type"] == "application/octet-stream"
            assert reply.headers["cache-control"] == "no-store"
            assert not adapter.capacity.locked()

    asyncio.run(run())
    assert requests == []
    report = read_call_diagnostics(ledger, call)
    events = {row["event"]: row for row in report["events"] if row["event"].startswith("tts.")}
    assert set(events) == {"tts.request_started", "tts.cache_hit", "tts.first_audio_ready",
        "tts.audio_stream_started", "tts.completed"}
    assert len({row["request_id"] for row in events.values()}) == 1
    assert len({row["start_unix"] for row in events.values()}) == 1
    assert all(row["duration_ms"] is not None and row["duration_ms"] >= 0 for row in events.values())
    for name, row in events.items():
        assert row["outcome"] == ("success" if name == "tts.completed" else "started")
        if name != "tts.request_started":
            assert {key: row["attributes"][key] for key in (
                "cache_hit", "cache_asset_id", "cache_request_sha256", "cache_manifest_sha256",
                "cached_sample_rate", "synthesis_source")} == {
                "cache_hit": True, "cache_asset_id": asset["asset_id"],
                "cache_request_sha256": asset["request_sha256"],
                "cache_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                "cached_sample_rate": 16000, "synthesis_source": "pregenerated_cache"}
    complete = events["tts.completed"]["attributes"]
    assert complete["cache_text_normalized"] is True
    assert complete["audio_bytes"] == len(pcm)
    assert complete["audio_duration_ms"] == len(pcm) / (16000 * 2) * 1000
    assert complete["observation_scope"] == "server_bytes_only_not_audible_playback"
    assert report["coverage"]["complete"] is False
    exported = json.dumps(report)
    for private in ("PRIVATE CUSTOMER", "5125550142", call,
        values["GEMINI_API_KEY"], values["GEMINI_TTS_BEARER_SECRET"]):
        assert private not in exported and private.encode() not in ledger.read_bytes()
