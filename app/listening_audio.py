"""Integrity-checked, local-only variations for the independent listening lane."""
import hashlib
import io
import json
from pathlib import Path
import re
import wave

VARIATIONS = {f"{name}_{take}": text for name, text in
    (("uh_huh", "Uh-huh."), ("yeah", "Yeah."), ("okay", "Okay."), ("right", "Right.")) for take in (1, 2)}
ROTATION = ("uh_huh_1", "acknowledge", "right_1", "okay_1", "uh_huh_2", "yeah_1", "neutral_ack", "right_2", "okay_2", "yeah_2")


def load_variations(path, expected_sha):
    path = Path(path)
    if (not path.is_absolute() or path.is_symlink() or not path.is_file()
            or not re.fullmatch(r"[a-f0-9]{64}", expected_sha or "") or path.stat().st_size > 32768):
        raise ValueError("invalid_listening_manifest")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError("listening_manifest_changed")
    manifest = json.loads(raw)
    if (manifest.get("schema"), manifest.get("model"), manifest.get("voice"), manifest.get("sample_rate")) != (
            "listening-audio-v1", "gemini-3.8-flash-lite-tts", "Kore", 8000):
        raise ValueError("unsupported_listening_bank")
    rows = manifest.get("assets")
    if not isinstance(rows, list) or not 1 <= len(rows) <= len(VARIATIONS):
        raise ValueError("listening_bank_bounds")
    result = {}
    for row in rows:
        ident = row.get("id") if isinstance(row, dict) else None
        if ident not in VARIATIONS or ident in result or row.get("text") != VARIATIONS[ident] or row.get("file") != ident+".wav":
            raise ValueError("invalid_listening_clip")
        source = path.parent/(ident+".wav")
        if source.is_symlink() or not source.is_file() or not 44 < source.stat().st_size <= 25000:
            raise ValueError("listening_clip_bounds")
        wav = source.read_bytes();sha = hashlib.sha256(wav).hexdigest()
        if sha != row.get("sha256"): raise ValueError("listening_clip_changed")
        with wave.open(io.BytesIO(wav), "rb") as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 8000, "NONE"):
                raise ValueError("invalid_listening_audio")
            duration = audio.getnframes()/8
            pcm = audio.readframes(audio.getnframes())
            if not 100 <= duration <= 1500 or duration != row.get("duration_ms") or len(pcm) != audio.getnframes()*2 or not any(pcm):
                raise ValueError("invalid_listening_duration")
        result[ident] = (wav, sha)
    return result
