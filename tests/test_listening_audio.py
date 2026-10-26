import hashlib
import io
import json
import wave

import pytest

from app.listening_audio import load_variations, VARIATIONS
from test_listening_cues import setup, TEXT, drain
import asyncio


def test_audio_bank_is_exact_text_hash_and_phone_format(tmp_path):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(8000); audio.writeframes(b'\x01\x00'*4000)
    data = stream.getvalue(); (tmp_path/"uh_huh_1.wav").write_bytes(data)
    row = {"id": "uh_huh_1", "text": "Uh-huh.", "file": "uh_huh_1.wav", "sha256": hashlib.sha256(data).hexdigest(), "duration_ms": 500}
    manifest = {"schema": "listening-audio-v1", "model": "gemini-3.8-flash-lite-tts", "voice": "Kore", "sample_rate": 8000, "assets": [row]}
    path = tmp_path/"manifest.json"; path.write_text(json.dumps(manifest))
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    assert load_variations(path, sha)["uh_huh_1"][0] == data
    (tmp_path/"uh_huh_1.wav").write_bytes(data[:-2])
    with pytest.raises(ValueError, match="listening_clip_changed"): load_variations(path, sha)
    path.write_text('{}')
    with pytest.raises(ValueError, match="listening_manifest_changed"): load_variations(path, sha)


def test_listening_variants_rotate_without_immediate_word_repeats():
    async def run():
        from app.listening_cues import LISTENING_PHRASES
        c, clock, _, sent, _, event, _ = setup()
        c.available_phrases.update(VARIATIONS)
        chosen = []
        for index in range(10):
            # Test rotation after every allowed cadence (up to seven seconds).
            # The separately tested pause preference must not suppress a turn.
            clock.now += 8
            event("transcript", "user", transcriptType="partial", transcript=TEXT+" again"*index)
            await drain(c)
            chosen.append(c.calls["fixture-call"].phrase_id)
        words = [{**LISTENING_PHRASES, **VARIATIONS}[key] for key in chosen]
        assert len(sent) == 10 and len(set(words)) >= 4
        assert all(a != b for a, b in zip(words, words[1:]))
        await c.close()
    asyncio.run(run())
