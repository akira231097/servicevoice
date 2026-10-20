"""Register project-owned Vapi provider credentials; never print credential values.

Read-only by default. --apply registers the selected voice authentication and a
separate gateway credential. Gemini speech keeps the Google key on our server;
Vapi receives a dedicated authentication token for that endpoint, never the key.
There are no automatic retries of credential creation after an ambiguous error.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import secrets
import sys
from uuid import UUID

import httpx
from dotenv import dotenv_values, set_key

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.voice_profiles import VOICE_PROFILES
from scripts.provision_vapi import validate_base_url


def credential_plans(values, voice_profile, *, apply=False, save_secret=None):
    """Return scoped requests. Existing credentials cannot silently rotate tokens."""
    plans = []
    def secret_for(setting, credential_setting):
        secret = (values.get(setting) or "").strip()
        if apply and not secret:
            if values.get(credential_setting):
                raise ValueError("A saved credential exists but its matching local secret is missing.")
            secret = secrets.token_urlsafe(48)
            if save_secret is None:raise ValueError("Secret persistence is required before registration.")
            save_secret(setting, secret)
        if secret and len(secret) < 32:raise ValueError("Endpoint authentication secrets must be at least 32 characters.")
        return secret
    gateway = secret_for("GEMINI_GATEWAY_SECRET", "VAPI_CUSTOM_LLM_CREDENTIAL_ID")
    plans.append(("VAPI_CUSTOM_LLM_CREDENTIAL_ID", "custom-llm", "gateway", {
        "provider":"custom-llm", "name":"servicevoice-gemini-gateway", "apiKey":gateway}))
    if voice_profile.startswith("gemini-"):
        tts = secret_for("GEMINI_TTS_BEARER_SECRET", "VAPI_CUSTOM_TTS_CREDENTIAL_ID")
        if tts and gateway and tts == gateway:raise ValueError("Speech and gateway authentication must use separate secrets.")
        plans.append(("VAPI_CUSTOM_TTS_CREDENTIAL_ID", "custom-credential", "speech", {
            "provider":"custom-credential", "name":"servicevoice-gemini-speech",
            "authenticationPlan":{"type":"bearer", "token":tts,
                "headerName":"Authorization", "bearerPrefixEnabled":True}}))
    elif VOICE_PROFILES[voice_profile]["settings"]["provider"] == "11labs":
        key = (values.get("ELEVENLABS_API_KEY") or "").strip()
        if not key:raise ValueError("The selected ElevenLabs profile requires its provider key.")
        plans.append(("VAPI_ELEVENLABS_CREDENTIAL_ID", "11labs", "speech", {
            "provider":"11labs", "name":"servicevoice-elevenlabs", "apiKey":key}))
    return plans


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--env-file", type=Path, default=ROOT/".env")
    parser.add_argument("--voice-profile", choices=tuple(VOICE_PROFILES))
    args = parser.parse_args(argv)
    path = args.env_file
    values = dotenv_values(path)
    required = ("VAPI_API_KEY", "GEMINI_API_KEY", "PUBLIC_BASE_URL")
    missing = [name for name in required if not (values.get(name) or "").strip()]
    if missing:
        print(json.dumps({"missing": missing, "requests_made": False}))
        return 2
    report = {"mode": "apply" if args.apply else "read_only", "credentials": {}}
    try:
        profile=args.voice_profile or values.get("VAPI_VOICE_PROFILE") or "gemini-3.8-flash-lite-tts"
        if profile not in VOICE_PROFILES:raise ValueError("Unknown voice profile.")
        base=validate_base_url(values["PUBLIC_BASE_URL"])
        plans=credential_plans(values,profile,apply=args.apply,
            save_secret=lambda setting,secret:set_key(str(path),setting,secret))
        with httpx.Client(base_url="https://api.vapi.ai", timeout=20, follow_redirects=False,
                          trust_env=False,headers={"Authorization": "Bearer " + values["VAPI_API_KEY"]}) as client:
            for setting, provider, purpose, payload in plans:
                saved = (values.get(setting) or "").strip()
                if saved:
                    response = client.get("/credential/" + str(UUID(saved)))
                    report["credentials"][purpose] = {"provider":provider,"http_status": response.status_code, "created": False}
                    if not response.is_success or response.json().get("provider") != provider:
                        print(json.dumps(report)); return 1
                elif args.apply:
                    response = client.post("/credential", json=payload)
                    report["credentials"][purpose] = {"provider":provider,"http_status": response.status_code, "created": response.is_success}
                    if not response.is_success:
                        print(json.dumps(report)); return 1
                    set_key(str(path), setting, str(UUID(response.json()["id"])))
                else:
                    report["credentials"][purpose] = {"provider":provider,"configured": False, "created": False}
        if args.apply:
            set_key(str(path), "VAPI_CUSTOM_LLM_URL", base + "/llm")
            if profile.startswith("gemini-"):
                set_key(str(path), "VAPI_CUSTOM_TTS_URL", base + "/tts/vapi")
                set_key(str(path), "GEMINI_TTS_MODEL", profile)
            set_key(str(path), "VAPI_VOICE_PROFILE", profile)
        print(json.dumps(report, indent=2))
        return 0
    except httpx.HTTPError:
        print("Provider connection incomplete. Check Vapi credentials before retrying a create; raw errors are withheld.")
    except (OSError, ValueError, KeyError, TypeError):
        print("Invalid local configuration or provider response. Credential values are withheld.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
