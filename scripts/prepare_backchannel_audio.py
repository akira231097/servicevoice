"""Plan approved acknowledgment clips; explicit --generate calls Google's TTS.

Generated files stay under the ignored .local directory by default. The output
is evidence of complete synthesis and stored geometry, not a listening approval.
No API keys are printed, copied into the manifest or sent to a research site.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.backchannel_audio import AudioCacheError, AudioSpec, BackchannelAudioCache, request_fingerprint
from app.backchannel_catalog import CATALOG_VERSION, PHRASES
from app.backchannel_pcm import PCMProcessingError, RMS_THRESHOLD, processing_version, trim_edge_silence
from app.gemini_tts import GeminiTTS, TTSError


def plan(phrase_ids, spec, *, rms_threshold=RMS_THRESHOLD):
    return {"mode": "plan_only", "provider_requests": 0, "catalog_version": CATALOG_VERSION,
        "model": spec.model, "voice": spec.voice, "sample_rate": spec.sample_rate,
        "channels": 1, "pcm_format": "s16le", "name_interpolation": False,
        "processing_version": processing_version(rms_threshold), "rms_threshold_pcm16": rms_threshold,
        "phrases": [{"phrase_id": phrase_id, "text": PHRASES[phrase_id],
            "request_sha256": request_fingerprint(phrase_id, spec)} for phrase_id in phrase_ids]}


async def generate(phrase_ids, cache, key, *, adapter_factory=GeminiTTS, rms_threshold=RMS_THRESHOLD):
    """Bounded sequential synthesis; only terminal-complete PCM is published."""
    version = processing_version(rms_threshold)
    if not isinstance(key, str) or not key.strip():
        raise AudioCacheError("backchannel_gemini_key_missing")
    settings = {"GEMINI_API_KEY": key, "GEMINI_TTS_MODEL": cache.spec.model, "GEMINI_TTS_VOICE": cache.spec.voice}
    adapter = adapter_factory(lambda: settings)
    assets, diagnostics = [], []
    try:
        await adapter.start()
        for phrase_id in phrase_ids:
            if phrase_id not in PHRASES:
                raise AudioCacheError("backchannel_unapproved_phrase")
            observations = []
            started = time.perf_counter()
            response = await adapter.response({"message": {"type": "voice-request",
                "text": PHRASES[phrase_id], "sampleRate": cache.spec.sample_rate}}, observer=observations.append)
            iterator = response.body_iterator
            pcm = bytearray()
            try:
                async for block in iterator:
                    pcm.extend(block)
                    if len(pcm) > cache.spec.sample_rate * 2 * 2.5:
                        raise AudioCacheError("backchannel_clip_too_long")
            finally:
                await iterator.aclose()
            completed = [item for item in observations if item.get("event") == "tts.completed"]
            if len(completed) != 1 or any(item.get("event") in {"tts.failed", "tts.cancelled"} for item in observations):
                raise AudioCacheError("backchannel_incomplete_synthesis")
            processed, processing = trim_edge_silence(bytes(pcm), cache.spec.sample_rate, rms_threshold=rms_threshold)
            asset = cache.store(phrase_id, processed)
            assets.append(asset)
            diagnostics.append({"phrase_id": phrase_id, "duration_ms": asset["duration_ms"],
                "generation_elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "upstream_sample_rate": completed[0].get("upstream_sample_rate"),
                "resampled": completed[0].get("resampled"),
                "resampler": completed[0].get("resampler"),
                "complete_synthesis_observed": True, "processing": processing})
    finally:
        await adapter.close()
    target = cache.write_manifest(assets)
    cache.load_manifest(target)
    return {"mode": "generated_candidate_not_listening_approved", "provider_requests": len(phrase_ids),
        "manifest": str(target), "manifest_sha256": cache.manifest_sha256,
        "processing_version": version,
        "listening_approval": "not_recorded", "phrases": diagnostics}


def process_manifest(source_manifest, cache, *, rms_threshold=RMS_THRESHOLD):
    """Process existing verified clips locally; preserve original immutable files."""
    version = processing_version(rms_threshold)
    source_path = Path(source_manifest)
    source = BackchannelAudioCache(source_path.parent, cache.spec)
    manifest = source.load_manifest(source_path)
    assets, diagnostics = [], []
    for old_asset in manifest["assets"]:
        pcm = source.read_pcm(old_asset)
        processed, processing = trim_edge_silence(pcm, cache.spec.sample_rate, rms_threshold=rms_threshold)
        new_asset = cache.store(old_asset["phrase_id"], processed)
        assets.append(new_asset)
        diagnostics.append({"phrase_id": old_asset["phrase_id"], "duration_ms": new_asset["duration_ms"],
            "source_asset_id": old_asset["asset_id"], "output_asset_id": new_asset["asset_id"],
            "processing": processing, "source_evidence": "validated_existing_cache_bytes",
            "new_synthesis_observed": False})
    target = cache.write_manifest(assets)
    cache.load_manifest(target)
    return {"mode": "processed_existing_candidate_not_listening_approved", "provider_requests": 0,
        "source_manifest_sha256": source.manifest_sha256, "manifest": str(target),
        "manifest_sha256": cache.manifest_sha256, "processing_version": version,
        "listening_approval": "not_recorded", "phrases": diagnostics}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".local" / "backchannel-audio")
    parser.add_argument("--phrase", action="append", choices=list(PHRASES), dest="phrases")
    parser.add_argument("--rms-threshold", type=int, choices=(5, 40), default=RMS_THRESHOLD,
        help="5 is conservative; explicit40 is an experimental noise trim requiring listening before use.")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--generate", action="store_true", help="Make at most one paid Google synthesis request per selected phrase.")
    modes.add_argument("--trim-manifest", type=Path, help="Process an existing manifest locally, with zero provider requests.")
    parser.add_argument("--env-file", type=Path, help="Optional local credentials file, read only when --generate is supplied.")
    args = parser.parse_args(argv)
    phrase_ids = args.phrases or list(PHRASES)
    if len(set(phrase_ids)) != len(phrase_ids):
        parser.error("Each approved phrase may be selected only once.")
    spec = AudioSpec()
    if args.trim_manifest:
        if args.phrases:
            parser.error("--trim-manifest processes the complete existing bank; do not also select --phrase.")
        try:
            report = process_manifest(args.trim_manifest, BackchannelAudioCache(args.output_dir, spec), rms_threshold=args.rms_threshold)
        except (AudioCacheError, PCMProcessingError) as error:
            print(json.dumps({"status": "failed", "error_code": str(error), "manifest_ready": False}))
            return 1
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if not args.generate:
        print(json.dumps(plan(phrase_ids, spec, rms_threshold=args.rms_threshold), ensure_ascii=False, indent=2))
        return 0
    values = {}
    if args.env_file:
        from dotenv import dotenv_values
        values = dotenv_values(args.env_file)
    key = values.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY")
    try:
        report = asyncio.run(generate(phrase_ids, BackchannelAudioCache(args.output_dir, spec), key, rms_threshold=args.rms_threshold))
    except (AudioCacheError, PCMProcessingError, TTSError) as error:
        print(json.dumps({"status": "failed", "error_code": str(error), "manifest_ready": False}))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
