"""Integrity checked, immutable speech clips, warmed before serving callers.

This module grants no permission to speak. A cache hit proves stored bytes and
their format, not audible playback, semantic suitability or correct turn taking.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import wave

from app.backchannel_catalog import CATALOG_VERSION, PHRASES
from app.gemini_tts import DEFAULT_MODEL, MODELS, VOICES, request_body

SCHEMA = "backchannel-audio-v1"
PROMPT_VERSION = "gemini-exact-transcript-v1"
SAMPLE_RATE = 16000
MAX_AUDIO_SECONDS = 2.5
MAX_MANIFEST_BYTES = 32 * 1024


class AudioCacheError(ValueError):
    """Fixed error codes; no caller text, credentials or upstream body."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha(value):
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class AudioSpec:
    model: str = DEFAULT_MODEL
    voice: str = "Kore"
    sample_rate: int = SAMPLE_RATE
    channels: int = 1
    sample_width: int = 2
    pcm_format: str = "s16le"
    prompt_version: str = PROMPT_VERSION

    def __post_init__(self):
        if (not isinstance(self.model, str) or self.model not in MODELS
            or not isinstance(self.voice, str) or self.voice not in VOICES
            or type(self.sample_rate) is not int or self.sample_rate != SAMPLE_RATE
            or type(self.channels) is not int or self.channels != 1
            or type(self.sample_width) is not int or self.sample_width != 2
            or self.pcm_format != "s16le" or self.prompt_version != PROMPT_VERSION):
            raise AudioCacheError("backchannel_invalid_audio_spec")


def request_fingerprint(phrase_id, spec):
    if not isinstance(phrase_id, str) or phrase_id not in PHRASES:
        raise AudioCacheError("backchannel_unapproved_phrase")
    return _sha(_json({"schema": SCHEMA, "catalog_version": CATALOG_VERSION,
        "phrase_id": phrase_id, "spec": asdict(spec),
        "request": request_body(PHRASES[phrase_id], spec.sample_rate, spec.voice)}))


def _validate_pcm(pcm, spec):
    if (not isinstance(pcm, bytes) or not pcm or len(pcm) % spec.sample_width
        or len(pcm) > int(spec.sample_rate * spec.sample_width * MAX_AUDIO_SECONDS)
        or not any(pcm)):
        raise AudioCacheError("backchannel_invalid_pcm")
    return round(len(pcm) / (spec.sample_rate * spec.sample_width) * 1000, 6)


def _wav(pcm, spec):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(spec.channels)
        output.setsampwidth(spec.sample_width)
        output.setframerate(spec.sample_rate)
        output.writeframes(pcm)
    return buffer.getvalue()


def _asset(pcm, phrase_id, spec):
    duration = _validate_pcm(pcm, spec)
    request_sha = request_fingerprint(phrase_id, spec)
    pcm_sha = _sha(pcm)
    asset_id = _sha(_json({"request_sha256": request_sha, "pcm_sha256": pcm_sha}))
    wav = _wav(pcm, spec)
    return {"phrase_id": phrase_id, "text": PHRASES[phrase_id],
        "request_sha256": request_sha, "asset_id": asset_id,
        "pcm_sha256": pcm_sha, "wav_sha256": _sha(wav),
        "pcm_bytes": len(pcm), "wav_bytes": len(wav), "duration_ms": duration,
        "pcm_file": f"{asset_id}.pcm", "wav_file": f"{asset_id}.wav"}, wav


class BackchannelAudioCache:
    def __init__(self, directory, spec=None):
        self.directory = Path(directory).expanduser().resolve()
        self.spec = spec or AudioSpec()
        if not isinstance(self.spec, AudioSpec):
            raise AudioCacheError("backchannel_invalid_audio_spec")
        self._loaded = {}
        self.manifest_sha256 = None

    def _path(self, name):
        if not isinstance(name, str) or not re.fullmatch(r"[0-9a-f]{64}\.(?:pcm|wav)", name):
            raise AudioCacheError("backchannel_invalid_asset_path")
        path = self.directory / name
        if path.is_symlink() or path.resolve().parent != self.directory:
            raise AudioCacheError("backchannel_invalid_asset_path")
        return path

    def _read(self, path, maximum):
        try:
            if path.is_symlink() or path.resolve().parent != self.directory or path.stat().st_size > maximum:
                raise AudioCacheError("backchannel_invalid_asset_path")
            with path.open("rb") as handle:
                value = handle.read(maximum + 1)
            if len(value) > maximum:
                raise AudioCacheError("backchannel_audio_limit_exceeded")
            return value
        except OSError:
            raise AudioCacheError("backchannel_asset_unavailable") from None

    def _publish(self, target, value):
        self.directory.mkdir(parents=True, exist_ok=True)
        if target.parent != self.directory or target.is_symlink():
            raise AudioCacheError("backchannel_invalid_asset_path")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(prefix=".backchannel-", suffix=".tmp", dir=self.directory, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                # Link publishes the complete file atomically and cannot replace
                # a previously published object, unlike an ordinary rename.
                os.link(temporary, target)
            except FileExistsError:
                if self._read(target, len(value)) != value:
                    raise AudioCacheError("backchannel_immutable_asset_conflict") from None
        except OSError:
            raise AudioCacheError("backchannel_cache_write_failed") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def store(self, phrase_id, pcm):
        metadata, wav = _asset(pcm, phrase_id, self.spec)
        self._publish(self._path(metadata["pcm_file"]), pcm)
        self._publish(self._path(metadata["wav_file"]), wav)
        return metadata

    def read_pcm(self, asset):
        if (not isinstance(asset, dict) or not isinstance(asset.get("phrase_id"), str)
            or asset["phrase_id"] not in PHRASES):
            raise AudioCacheError("backchannel_invalid_asset")
        maximum = int(SAMPLE_RATE * 2 * MAX_AUDIO_SECONDS)
        pcm = self._read(self._path(asset.get("pcm_file")), maximum)
        expected, wav = _asset(pcm, asset["phrase_id"], self.spec)
        if _json(asset) != _json(expected):
            raise AudioCacheError("backchannel_asset_integrity_failed")
        stored_wav = self._read(self._path(asset["wav_file"]), maximum + 44)
        if stored_wav != wav:
            raise AudioCacheError("backchannel_asset_integrity_failed")
        return pcm

    def read_wav(self, asset):
        return _wav(self.read_pcm(asset), self.spec)

    def write_manifest(self, assets):
        if not isinstance(assets, (list, tuple)) or not assets or len(assets) > len(PHRASES):
            raise AudioCacheError("backchannel_invalid_manifest")
        phrase_ids = [asset.get("phrase_id") if isinstance(asset, dict) else None for asset in assets]
        if any(not isinstance(phrase_id, str) or phrase_id not in PHRASES for phrase_id in phrase_ids):
            raise AudioCacheError("backchannel_invalid_asset")
        if len(set(phrase_ids)) != len(phrase_ids):
            raise AudioCacheError("backchannel_duplicate_phrase")
        for asset in assets:
            self.read_pcm(asset)
        manifest = {"schema": SCHEMA, "catalog_version": CATALOG_VERSION,
            "spec": asdict(self.spec), "assets": sorted(assets, key=lambda asset: asset["phrase_id"]),
            "observation_scope": "stored_audio_bytes_not_audible_playback",
            "listening_approval": "not_recorded"}
        encoded = _json(manifest)
        target = self.directory / ("manifest-" + _sha(encoded) + ".json")
        self._publish(target, encoded)
        return target

    def load_manifest(self, manifest_path):
        supplied = Path(manifest_path).expanduser()
        if supplied.is_symlink():
            raise AudioCacheError("backchannel_invalid_manifest_path")
        path = supplied.resolve()
        if path.parent != self.directory or not re.fullmatch(r"manifest-[0-9a-f]{64}\.json", path.name):
            raise AudioCacheError("backchannel_invalid_manifest_path")
        encoded = self._read(path, MAX_MANIFEST_BYTES)
        if path.name != "manifest-" + _sha(encoded) + ".json":
            raise AudioCacheError("backchannel_manifest_integrity_failed")
        try:
            manifest = json.loads(encoded)
        except (ValueError, UnicodeError):
            raise AudioCacheError("backchannel_invalid_manifest") from None
        if (not isinstance(manifest, dict) or set(manifest) != {"schema", "catalog_version", "spec", "assets", "observation_scope", "listening_approval"}
            or manifest["schema"] != SCHEMA or manifest["catalog_version"] != CATALOG_VERSION
            or _json(manifest["spec"]) != _json(asdict(self.spec))
            or manifest["observation_scope"] != "stored_audio_bytes_not_audible_playback"
            or manifest["listening_approval"] != "not_recorded"
            or not isinstance(manifest["assets"], list) or not 1 <= len(manifest["assets"]) <= len(PHRASES)):
            raise AudioCacheError("backchannel_invalid_manifest")
        loaded = {}
        for asset in manifest["assets"]:
            pcm = self.read_pcm(asset)
            text = asset["text"]
            if text in loaded:
                raise AudioCacheError("backchannel_duplicate_phrase")
            loaded[text] = (pcm, {"cache_asset_id": asset["asset_id"],
                "cache_request_sha256": asset["request_sha256"], "cache_hit": True,
                "audio_duration_ms": asset["duration_ms"], "cached_sample_rate": self.spec.sample_rate})
        # Publish warmed entries only after every object validates. A failed
        # reload cannot partly replace an existing, verified in-memory bank.
        self._loaded = loaded
        self.manifest_sha256 = _sha(encoded)
        return manifest

    def lookup_pcm(self, text, sample_rate, *, model=None, voice=None):
        if (type(sample_rate) is not int or sample_rate != self.spec.sample_rate
            or model is not None and model != self.spec.model
            or voice is not None and voice != self.spec.voice
            or not isinstance(text, str)):
            return None
        item = self._loaded.get(text)
        if item is None:
            return None
        pcm, metadata = item
        return pcm, {**metadata, "cache_manifest_sha256": self.manifest_sha256}
