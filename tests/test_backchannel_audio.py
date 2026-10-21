"""Stored PCM geometry and immutable exact-text cache; no paid provider calls."""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import hashlib
import io
import json
import math
import struct
import wave

import httpx
import pytest

from app.backchannel_audio import AudioCacheError, AudioSpec, BackchannelAudioCache, request_fingerprint
from app.backchannel_catalog import PHRASES
from app.gemini_tts import GeminiTTS, TTSError
from scripts.prepare_backchannel_audio import generate, main


def tone(rate=16000, seconds=.25):
    return b"".join(struct.pack("<h", round(4000 * math.sin(2 * math.pi * 440 * n / rate)))
                    for n in range(round(rate * seconds)))


def seeded(tmp_path, phrase_id="neutral_ack"):
    cache = BackchannelAudioCache(tmp_path)
    pcm = tone()
    asset = cache.store(phrase_id, pcm)
    target = cache.write_manifest([asset])
    return cache, pcm, asset, target


def test_warmed_exact_text_lookup_owns_no_disk_io_and_never_splices_names(tmp_path, monkeypatch):
    cache, pcm, asset, target = seeded(tmp_path)
    manifest = cache.load_manifest(target)
    assert manifest["listening_approval"] == "not_recorded"
    assert manifest["observation_scope"] == "stored_audio_bytes_not_audible_playback"
    monkeypatch.setattr(cache, "_read", lambda *_: pytest.fail("live cache lookup performed disk work"))
    result, metadata = cache.lookup_pcm(PHRASES["neutral_ack"], 16000, model=cache.spec.model, voice="Kore")
    assert result == pcm and metadata["cache_asset_id"] == asset["asset_id"]
    assert metadata["cache_manifest_sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert metadata["cache_text_normalized"] is False
    for text in ("Mm-hmm. Alex", "Mm-hmm.\u00a0", "mm-hmm.", "I booked it.", "Let me check that, Sarath."):
        assert cache.lookup_pcm(text, 16000) is None
    padded, padded_metadata = cache.lookup_pcm(" \tMm-hmm. \r\n",16000)
    assert padded == pcm and padded_metadata["cache_text_normalized"] is True
    assert cache.lookup_pcm(PHRASES["neutral_ack"], 12345) is None
    assert cache.lookup_pcm(PHRASES["neutral_ack"], True) is None
    assert cache.lookup_pcm(PHRASES["neutral_ack"], 16000, voice="Zephyr") is None
    assert cache.lookup_pcm(PHRASES["neutral_ack"], 16000, model="different-model") is None


def test_cache_ids_bind_request_voice_rate_and_actual_audio_bytes(tmp_path):
    cache, pcm, asset, target = seeded(tmp_path)
    assert request_fingerprint("neutral_ack", AudioSpec(voice="Puck")) != asset["request_sha256"]
    assert request_fingerprint("moment", cache.spec) != asset["request_sha256"]
    changed = cache.store("neutral_ack", tone(seconds=.3))
    assert changed["asset_id"] != asset["asset_id"]
    assert target.read_bytes() == cache.write_manifest([asset]).read_bytes()
    assert cache.read_pcm(asset) == pcm
    assert json.loads(target.read_bytes())["spec"] == asdict(cache.spec)
    assert set(path.suffix for path in tmp_path.iterdir()) == {".pcm", ".wav", ".json"}


@pytest.mark.parametrize("rate", [8000, 16000, 22050, 24000, 44100])
def test_phone_and_browser_cache_rates_preserve_pitch_duration_and_no_request_work(tmp_path, monkeypatch, rate):
    import numpy as np
    cache, pcm, asset, target = seeded(tmp_path)
    cache.load_manifest(target)
    monkeypatch.setattr(cache, "_read", lambda *_: pytest.fail("request disk work"))
    monkeypatch.setattr("app.backchannel_audio.PCMRateConverter",
        lambda *_: pytest.fail("request conversion work"))
    audio, evidence = cache.lookup_pcm(PHRASES["neutral_ack"], rate)
    samples = np.frombuffer(audio, dtype="<i2").astype(float)
    assert len(samples) == pytest.approx(rate * .25, abs=1)
    spectrum = np.abs(np.fft.rfft(samples))
    frequency = np.fft.rfftfreq(len(samples), 1 / rate)[int(np.argmax(spectrum))]
    assert frequency == pytest.approx(440, abs=4)
    assert np.sqrt(np.mean(samples ** 2)) == pytest.approx(4000 / math.sqrt(2), rel=.03)
    assert evidence["cached_sample_rate"] == rate
    assert evidence["upstream_sample_rate"] == 16000
    assert evidence["resampled"] is (rate != 16000)
    assert evidence["cache_asset_id"] == asset["asset_id"]
    if rate == 16000:
        assert audio == pcm


def test_failed_rate_warmup_preserves_whole_previous_bank(tmp_path, monkeypatch):
    from app import backchannel_audio
    cache, _, _, target = seeded(tmp_path)
    cache.load_manifest(target)
    previous = {rate: cache.lookup_pcm(PHRASES["neutral_ack"], rate)
                for rate in (8000, 16000, 22050, 24000, 44100)}
    original = backchannel_audio.PCMRateConverter
    def failing(source, target):
        if target == 24000:
            raise TTSError("tts_resampling_failed")
        return original(source, target)
    monkeypatch.setattr(backchannel_audio, "PCMRateConverter", failing)
    with pytest.raises(AudioCacheError, match="cache_resampling_failed"):
        cache.load_manifest(target)
    assert all(cache.lookup_pcm(PHRASES["neutral_ack"], rate) == hit
               for rate, hit in previous.items())


def test_parallel_publication_is_complete_idempotent_and_cannot_clobber(tmp_path):
    cache = BackchannelAudioCache(tmp_path)
    pcm = tone()
    with ThreadPoolExecutor(max_workers=8) as pool:
        assets = list(pool.map(lambda _: cache.store("neutral_ack", pcm), range(16)))
    assert all(asset == assets[0] for asset in assets)
    assert not list(tmp_path.glob(".backchannel-*.tmp"))
    path = tmp_path / assets[0]["pcm_file"]
    path.write_bytes(b"altered")
    with pytest.raises(AudioCacheError, match="immutable_asset_conflict"):
        cache.store("neutral_ack", pcm)
    assert path.read_bytes() == b"altered"


def test_failed_atomic_publication_leaves_no_partial_audio(tmp_path, monkeypatch):
    def unavailable(*_):
        raise OSError("synthetic filesystem failure")
    monkeypatch.setattr("app.backchannel_audio.os.link", unavailable)
    with pytest.raises(AudioCacheError, match="cache_write_failed"):
        BackchannelAudioCache(tmp_path).store("neutral_ack", tone())
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("pcm", [b"", b"\x01", b"\x00\x00" * 2000, b"\x01\x00" * 40001, bytearray(b"\x01\x00")],
                         ids=["empty", "unaligned", "silence", "overlong", "mutable"])
def test_invalid_unaligned_silent_or_long_pcm_is_never_published(tmp_path, pcm):
    with pytest.raises(AudioCacheError, match="invalid_pcm"):
        BackchannelAudioCache(tmp_path).store("neutral_ack", pcm)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("values", [{"sample_rate": 8000}, {"sample_rate": True}, {"channels": 2},
    {"channels": True}, {"sample_width": 1}, {"pcm_format": "float32"}, {"model": "arbitrary-host/model"},
    {"voice": "unknown"}, {"prompt_version": "old-style-prompt"}])
def test_audio_geometry_and_model_configuration_are_pinned(values):
    with pytest.raises(AudioCacheError, match="invalid_audio_spec"):
        AudioSpec(**values)


def test_wave_header_declares_actual_samples_and_natural_duration(tmp_path):
    cache, pcm, asset, _ = seeded(tmp_path)
    with wave.open(io.BytesIO(cache.read_wav(asset)), "rb") as audio:
        assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (1, 2, 16000)
        assert audio.getnframes() / audio.getframerate() == .25
        assert audio.readframes(audio.getnframes()) == pcm


def test_corrupt_audio_or_manifest_cannot_partly_replace_warmed_cache(tmp_path):
    cache, pcm, asset, target = seeded(tmp_path)
    cache.load_manifest(target)
    path = tmp_path / asset["pcm_file"]
    damaged = bytearray(pcm)
    damaged[10] ^= 1
    path.write_bytes(damaged)
    with pytest.raises(AudioCacheError, match="asset_integrity_failed"):
        cache.load_manifest(target)
    assert cache.lookup_pcm(PHRASES["neutral_ack"], 16000)[0] == pcm
    target.write_bytes(target.read_bytes() + b" ")
    with pytest.raises(AudioCacheError, match="manifest_integrity_failed"):
        cache.load_manifest(target)
    assert cache.lookup_pcm(PHRASES["neutral_ack"], 16000)[0] == pcm


def test_prompt_or_spec_revision_invalidates_existing_manifest(tmp_path, monkeypatch):
    cache, _, _, target = seeded(tmp_path)
    with pytest.raises(AudioCacheError, match="invalid_manifest"):
        BackchannelAudioCache(tmp_path, AudioSpec(voice="Puck")).load_manifest(target)
    from app import backchannel_audio
    original = backchannel_audio.request_body
    monkeypatch.setattr(backchannel_audio, "request_body", lambda *args: {**original(*args), "new_style": "changed"})
    with pytest.raises(AudioCacheError, match="asset_integrity_failed"):
        cache.load_manifest(target)


def test_unsafe_asset_paths_extra_fields_and_duplicate_phrases_fail_closed(tmp_path):
    cache, _, asset, _ = seeded(tmp_path)
    with pytest.raises(AudioCacheError, match="invalid_asset_path"):
        cache.read_pcm({**asset, "pcm_file": "../outside.pcm"})
    with pytest.raises(AudioCacheError, match="asset_integrity_failed"):
        cache.read_pcm({**asset, "unapproved": "field"})
    with pytest.raises(AudioCacheError, match="duplicate_phrase"):
        cache.write_manifest([asset, asset])
    with pytest.raises(AudioCacheError, match="unapproved_phrase"):
        cache.store("../outside", tone())
    with pytest.raises(AudioCacheError, match="invalid_manifest_path"):
        cache.load_manifest(tmp_path / "manifest.json")


def provider_event(pcm, rate=24000, *, stop=False):
    parts = [{"inlineData": {"mimeType": f"audio/L16;rate={rate};channels=1", "data": base64.b64encode(pcm).decode()}}]
    candidate = {"content": {"parts": parts}}
    if stop:
        candidate["finishReason"] = "STOP"
    return b"data: " + json.dumps({"candidates": [candidate]}).encode() + b"\n\n"


def test_pregeneration_reuses_complete_24k_to_16k_conversion_and_publishes_no_key(tmp_path):
    key = "synthetic-key-not-real"
    requests = []
    pcm24 = tone(24000, seconds=1)

    async def scenario():
        def provider(request):
            requests.append(request)
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                content=provider_event(pcm24[:24000]) + provider_event(pcm24[24000:], stop=True))
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            cache = BackchannelAudioCache(tmp_path)
            report = await generate(["neutral_ack"], cache, key,
                adapter_factory=lambda settings: GeminiTTS(settings, client=client))
        assert report["provider_requests"] == 1 and report["listening_approval"] == "not_recorded"
        row = report["phrases"][0]
        assert row["duration_ms"] == 1000 and row["upstream_sample_rate"] == 24000
        assert row["resampled"] is True and row["resampler"] == "soxr_hq"
        pcm, _ = cache.lookup_pcm(PHRASES["neutral_ack"], 16000)
        assert len(pcm) == 32000
        asset = cache.load_manifest(report["manifest"])["assets"][0]
        with wave.open(io.BytesIO(cache.read_wav(asset)), "rb") as audio:
            assert audio.getframerate() == 16000 and audio.getnframes() == 16000
        assert all(key.encode() not in path.read_bytes() for path in tmp_path.iterdir())
    asyncio.run(scenario())
    assert len(requests) == 1 and requests[0].url.host == "generativelanguage.googleapis.com"
    assert requests[0].headers["x-goog-api-key"] == key
    assert json.loads(requests[0].content)["contents"][0]["parts"][0]["text"] == PHRASES["neutral_ack"]


def test_incomplete_synthesis_never_becomes_a_ready_manifest(tmp_path):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200,
            headers={"content-type": "text/event-stream"}, content=provider_event(tone())))) as client:
            with pytest.raises(TTSError, match="tts_upstream_incomplete_generation"):
                await generate(["neutral_ack"], BackchannelAudioCache(tmp_path), "synthetic",
                    adapter_factory=lambda settings: GeminiTTS(settings, client=client))
        assert not list(tmp_path.glob("manifest-*.json"))
        assert not list(tmp_path.glob("*.pcm"))
    asyncio.run(scenario())


def test_default_cli_only_plans_without_reading_keys_or_creating_output(tmp_path, monkeypatch, capsys):
    import dotenv
    monkeypatch.setattr(dotenv, "dotenv_values", lambda *_: pytest.fail("plan read credential file"))
    assert main(["--output-dir", str(tmp_path / "unused"), "--env-file", str(tmp_path / "not-read.env")]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["provider_requests"] == 0 and report["name_interpolation"] is False
    assert len(report["phrases"]) == len(PHRASES)
    assert not (tmp_path / "unused").exists()


def test_generate_without_a_key_fails_without_creating_assets(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert main(["--generate", "--output-dir", str(tmp_path / "unused")]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report == {"status": "failed", "error_code": "backchannel_gemini_key_missing", "manifest_ready": False}
    assert not (tmp_path / "unused").exists()
