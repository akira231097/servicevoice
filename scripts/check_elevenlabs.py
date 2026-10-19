"""Small paired TTS integration probe; not a conversational latency benchmark."""
import json
from pathlib import Path
import time

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
SENTENCE = "I can help with your service visit. Which day works best for you?"


def main():
    values = dotenv_values(ROOT / ".env")
    key = (values.get("ELEVENLABS_API_KEY") or "").strip()
    voice = (values.get("VAPI_ELEVENLABS_VOICE_ID") or "").strip()
    if not key or not voice:
        print("Set the local ElevenLabs key and selected voice ID first."); return 2
    report = {"kind": "direct_elevenlabs_streaming_probe", "text": SENTENCE, "voice_id": voice,
              "samples": [], "limitations": ["Two samples per model, no statistical latency claim.",
                  "Includes local network and provider HTTP time; excludes Vapi, STT, LLM, endpointing and playback.",
                  "Audio quality requires listening; HTTP success does not prove subjective naturalness."]}
    order = ("eleven_flash_v2_5", "eleven_v4_turbo", "eleven_v4_turbo", "eleven_flash_v2_5")
    with httpx.Client(timeout=25, follow_redirects=False) as client:
        for model in order:
            started, first, audio = time.perf_counter(), None, bytearray()
            item = {"model": model}
            try:
                with client.stream("POST", f"https://api.elevenlabs.io/v1/text-to-speech/{voice}/stream",
                                   params={"output_format": "mp3_44100_128"}, headers={"xi-api-key": key},
                                   json={"text": SENTENCE, "model_id": model}) as response:
                    item["http_status"] = response.status_code
                    if response.is_success:
                        for chunk in response.iter_bytes():
                            if chunk and first is None: first = time.perf_counter()
                            audio.extend(chunk)
                            if len(audio) > 2*1024*1024: raise ValueError("Audio size limit")
                item.update(first_audio_byte_ms=round((first-started)*1000, 2) if first else None,
                            complete_ms=round((time.perf_counter()-started)*1000, 2), audio_bytes=len(audio))
                if audio and item["http_status"] == 200:
                    destination = ROOT/"docs/audio"/(model+".mp3")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if not destination.exists(): destination.write_bytes(audio)
            except (httpx.HTTPError, ValueError):
                item["error"] = "network_or_response_limit; raw details withheld"
            report["samples"].append(item)
            print(json.dumps(item))
    (ROOT/"docs/elevenlabs-streaming-probe.json").write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
    return 0 if all(sample.get("http_status") == 200 and sample.get("audio_bytes", 0) > 0 for sample in report["samples"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
