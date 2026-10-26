"""Explicit comparison configurations; no environment, credentials or network I/O.

Selection never changes the running assistant. Schema support is distinct from
account availability and from passing our live conversational acceptance tests.
"""
from __future__ import annotations

from copy import deepcopy
import ipaddress
import re
from urllib.parse import urlsplit
from uuid import UUID

MODEL_PROFILES = {
    "baseline": {
        "settings": {"provider": "openai", "model": "gpt-4.1-mini", "temperature": 0.2},
        "support": "documented_native",
        "sources": ["https://docs.vapi.ai/providers/model/openai"],
        "notes": ["Existing default. No live quality/latency winner has been established."],
    },
    "luna": {
        "settings": {"provider": "openai", "model": "gpt-6-luna", "reasoningEffort": "none", "temperature": 0.2, "serviceTier": "default"},
        "support": "documented_native",
        "sources": ["https://docs.vapi.ai/providers/model/openai", "https://developers.openai.com/api/docs/models/gpt-6-luna", "https://developers.openai.com/api/docs/guides/deployment-checklist"],
        "notes": ["Chat Completions function calling requires none reasoning; temperature is valid with none. Standard tier selected explicitly."],
    },
    "gpt-live-1": {
        "settings": {"provider": "openai", "model": "gpt-live-1"},
        "support": "documented_native_beta",
        "sources": ["https://docs.vapi.ai/gpt-live/overview", "https://docs.vapi.ai/gpt-live/configuration"],
        "notes": ["Vapi beta: full-duplex speaker plus a GPT-5.6 reasoner. The classic assistant path cannot express it; use scripts/provision_gpt_live.py.",
                  "Isolated phone line only: requires SERVICEVOICE_PHONE_NATIVE_TOOLS=gpt-live-candidate outside production."],
    },
    "gemini-3.8-flash": {
        "settings": {"provider": "google", "model": "gemini-3.8-flash"},
        "support": "native_unverified_blocked",
        "sources": ["https://docs.vapi.ai/providers/model/gemini"],
        "notes": ["Exact model absent from reviewed native Vapi list. Use the explicit custom adapter profile after its contract is implemented; never substitute Gemini 3.5."],
    },
    "gemini-3.8-flash-custom": {
        "settings": {"provider": "custom-llm", "model": "gemini-3.8-flash", "metadataSendMode": "variable"},
        "support": "experimental_adapter_required",
        "sources": ["https://ai.google.dev/gemini-api/docs/openai", "https://docs.vapi.ai/customization/custom-llm/thegrid"],
        "notes": [
            "Requires an authenticated OpenAI-compatible adapter base URL, not a native Google provider substitution.",
            "Adapter must fix reasoning_effort=low, preserve Gemini thought signatures across streamed tool rounds, and enforce exact model identity.",
            "Vapi call metadata is sent only to our adapter for session binding; the adapter must strip it before calling Google. It must also omit unsupported sampling parameters.",
            "No live call or account access is validated by producing this payload.",
        ],
    },
}

VOICE_PROFILES = {
    "gemini-3.8-flash-lite-tts": {
        "settings": {"provider": "custom-voice", "chunkPlan": {"formatPlan": {"enabled": False}}},
        "support": "documented_custom_adapter",
        "sources": ["https://ai.google.dev/gemini-api/docs/speech-generation",
                    "https://docs.vapi.ai/customization/custom-voices/custom-tts",
                    "https://docs.vapi.ai/assistants/voice-formatting-plan"],
        "notes": ["Latency-first Gemini speech candidate. Our authenticated PCM adapter uses the server-side Gemini API key; no ElevenLabs credential or fallback is selected.",
                  "Requires GEMINI_TTS_MODEL=gemini-3.8-flash-lite-tts on the actual server. Account availability and audible accuracy require live validation."],
    },
    "gemini-3.8-flash-tts": {
        "settings": {"provider": "custom-voice", "chunkPlan": {"formatPlan": {"enabled": False}}},
        "support": "documented_custom_adapter",
        "sources": ["https://ai.google.dev/gemini-api/docs/speech-generation",
                    "https://docs.vapi.ai/customization/custom-voices/custom-tts",
                    "https://docs.vapi.ai/assistants/voice-formatting-plan"],
        "notes": ["Quality-first Gemini speech candidate through the authenticated PCM adapter. No ElevenLabs credential or fallback is selected.",
                  "Requires GEMINI_TTS_MODEL=gemini-3.8-flash-tts on the actual server. Account availability and audible accuracy require live validation."],
    },
    "elliot-v2": {
        "settings": {"provider": "vapi", "voiceId": "Elliot", "version": 2},
        "support": "documented_native",
        "sources": ["https://docs.vapi.ai/providers/voice/vapi"],
        "notes": ["Existing default voice."],
    },
    "eleven-flash-2.5": {
        "settings": {"provider": "11labs", "model": "eleven_flash_v2_5", "voiceId": "sarah"},
        "support": "documented_native",
        "sources": ["https://docs.vapi.ai/providers/voice/elevenlabs"],
        "notes": ["Use the same ElevenLabs voice ID across its two model arms; entitlement and live behavior still need testing."],
    },
    "eleven-v4-turbo": {
        "settings": {"provider": "11labs", "model": "eleven_v4_turbo", "voiceId": "sarah"},
        "support": "documented_native_sdk_snapshot_lags",
        "sources": ["https://docs.vapi.ai/providers/voice/elevenlabs", "https://docs.vapi.ai/whats-new/2026/10/5"],
        "notes": ["Current Vapi provider docs list this exact model; the previously downloaded SDK enum predates that entry. No fallback substitution is configured."],
    },
}

TRANSCRIBER_PROFILES = {
    "soniox-v5-auto": {
        "settings": {"provider": "soniox", "model": "stt-rt-v5", "languages": []},
        "sources": ["https://docs.vapi.ai/providers/transcriber/soniox"],
        "notes": ["An empty languages array requests automatic detection and code-switching across supported languages. Uses the Vapi integration; live entitlement and quality require actual calls."],
    },
    "deepgram-nova3-en": {
        "settings": {"provider": "deepgram", "model": "nova-3", "language": "en"},
        "sources": ["https://docs.vapi.ai/providers/transcriber/deepgram"],
        "notes": ["Legacy configuration, retained for explicit comparison. Fixed recognition locale."],
    },
    "eleven-scribe-v2-auto": {
        "settings": {"provider": "11labs", "model": "scribe_v2_realtime"},
        "sources": ["https://docs.vapi.ai/providers/transcriber/elevenlabs",
                    "https://elevenlabs.io/docs/api-reference/speech-to-text/v-1-speech-to-text-realtime"],
        "notes": ["Provider documentation describes omitted language as automatic prediction; confirm effective adapter behavior with calls. The local account's 2026-10-18 candidate closed with unaccepted_terms, so this profile is not the accepted runtime. No terms are accepted automatically."],
    },
}


def select_transcriber(env, *, apply=False):
    selected = env.get("VAPI_TRANSCRIBER_PROFILE", "").strip() or "soniox-v5-auto"
    if selected not in TRANSCRIBER_PROFILES:
        raise ValueError("Unknown VAPI_TRANSCRIBER_PROFILE; select an explicit documented profile.")
    settings = deepcopy(TRANSCRIBER_PROFILES[selected]["settings"])
    credentials = []
    if settings["provider"] == "11labs":
        credential = _credential_id(env.get("VAPI_ELEVENLABS_CREDENTIAL_ID", "").strip(), "VAPI_ELEVENLABS_CREDENTIAL_ID")
        if apply and not credential:
            raise ValueError("The selected transcriber requires the saved VAPI_ELEVENLABS_CREDENTIAL_ID.")
        if credential:
            credentials.append(credential)
    return settings, credentials


def profile_catalog():
    return deepcopy({"model_profiles": MODEL_PROFILES, "voice_profiles": VOICE_PROFILES,
                     "transcriber_profiles": TRANSCRIBER_PROFILES})


def _credential_id(value, label):
    if not value:
        return None
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{label} must be a saved Vapi credential UUID.") from exc


def _tts_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.hostname.lower() == "localhost"
            or parsed.hostname.lower().endswith((".localhost", ".local"))):
        raise ValueError("VAPI_CUSTOM_TTS_URL must be a public HTTPS endpoint without credentials or query parameters.")
    try:address = ipaddress.ip_address(parsed.hostname)
    except ValueError:pass
    else:
        if not address.is_global:raise ValueError("VAPI_CUSTOM_TTS_URL must not point to a private address.")
    return value


def select_profiles(env, *, model_profile=None, voice_profile=None, apply=False, allow_experimental=False):
    # CLI > named environment profile > supported legacy model alias > default.
    selected_model = model_profile or env.get("VAPI_MODEL_PROFILE", "").strip()
    if not selected_model:
        legacy = env.get("VAPI_MODEL", "").strip()
        selected_model = {"": "baseline", "gpt-4.1-mini": "baseline", "gpt-6-luna": "luna", "gemini-3.8-flash": "gemini-3.8-flash"}.get(legacy)
        if selected_model is None:
            raise ValueError("VAPI_MODEL is not a verified comparison model. Select a documented VAPI_MODEL_PROFILE explicitly.")
    selected_voice = voice_profile or env.get("VAPI_VOICE_PROFILE", "").strip() or "elliot-v2"
    if selected_model not in MODEL_PROFILES or selected_voice not in VOICE_PROFILES:
        raise ValueError("Unknown model or voice profile. Use --list-profiles for exact choices.")
    model_entry, voice_entry = MODEL_PROFILES[selected_model], VOICE_PROFILES[selected_voice]
    if model_entry["support"] == "documented_native_beta":
        raise ValueError("GPT-Live needs a speaker/reasoner assistant. Use scripts/provision_gpt_live.py, not the classic assistant path.")
    if model_entry["support"] == "native_unverified_blocked":
        raise ValueError("Exact Gemini 3.8 Flash native Vapi support is unverified. Use gemini-3.8-flash-custom with an implemented adapter and explicit experimental opt-in; no other Gemini is substituted.")
    model, voice = deepcopy(model_entry["settings"]), deepcopy(voice_entry["settings"])
    credentials = []
    if selected_model == "gemini-3.8-flash-custom":
        if not allow_experimental:
            raise ValueError("The exact Gemini custom path requires --allow-experimental and a tested adapter contract. See integrations/model-comparison.md.")
        custom_url = env.get("VAPI_CUSTOM_LLM_URL", "").strip()
        credential = _credential_id(env.get("VAPI_CUSTOM_LLM_CREDENTIAL_ID", "").strip(), "VAPI_CUSTOM_LLM_CREDENTIAL_ID")
        if apply and (not custom_url or not credential):
            raise ValueError("Set the adapter base VAPI_CUSTOM_LLM_URL and saved VAPI_CUSTOM_LLM_CREDENTIAL_ID before applying the experimental profile.")
        model["url"] = custom_url or "https://REPLACE-WITH-GEMINI-ADAPTER.example/v1"
        credentials.append(credential or "REPLACE-WITH-SAVED-CUSTOM-LLM-CREDENTIAL-ID")
    if voice["provider"] == "11labs":
        voice_id = env.get("VAPI_ELEVENLABS_VOICE_ID", "").strip() or "sarah"
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", voice_id):
            raise ValueError("VAPI_ELEVENLABS_VOICE_ID must be a voice identifier, not a URL or credential.")
        voice["voiceId"] = voice_id
        credential = _credential_id(env.get("VAPI_ELEVENLABS_CREDENTIAL_ID", "").strip(), "VAPI_ELEVENLABS_CREDENTIAL_ID")
        if credential:
            credentials.append(credential)
    if voice["provider"] == "custom-voice":
        tts_url = env.get("VAPI_CUSTOM_TTS_URL", "").strip()
        credential = _credential_id(env.get("VAPI_CUSTOM_TTS_CREDENTIAL_ID", "").strip(), "VAPI_CUSTOM_TTS_CREDENTIAL_ID")
        if apply and (not tts_url or not credential):
            raise ValueError("Set VAPI_CUSTOM_TTS_URL and saved VAPI_CUSTOM_TTS_CREDENTIAL_ID before applying Gemini speech.")
        if apply and env.get("GEMINI_TTS_MODEL", "gemini-3.8-flash-lite-tts") != selected_voice:
            raise ValueError("GEMINI_TTS_MODEL must match the selected Gemini voice profile on the speech server.")
        voice["server"] = {"url": _tts_url(tts_url) if tts_url else "https://REPLACE-WITH-GEMINI-TTS.example/tts/vapi",
                           "credentialId": credential or "REPLACE-WITH-SAVED-CUSTOM-TTS-CREDENTIAL-ID", "timeoutSeconds": 30,
                           "backoffPlan": {"type": "fixed", "maxRetries": 0, "baseDelaySeconds": 1}}
        voice["cachingEnabled"] = False
        credentials.append(credential or "REPLACE-WITH-SAVED-CUSTOM-TTS-CREDENTIAL-ID")
    manifest = {
        "model_profile": selected_model, "voice_profile": selected_voice,
        "model_support": model_entry["support"], "voice_support": voice_entry["support"],
        "sources": model_entry["sources"] + voice_entry["sources"],
        "notes": model_entry["notes"] + voice_entry["notes"],
        "live_comparison_status": "not_measured_by_profile_generation",
    }
    return model, voice, credentials, manifest
