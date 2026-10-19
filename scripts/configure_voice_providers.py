"""Register project-owned Vapi provider credentials; never print credential values.

Read-only by default. --apply registers an ElevenLabs credential and a separate
gateway credential, preserving the private Gemini key exclusively on this server.
There are no automatic retries of credential creation after an ambiguous error.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import secrets
from uuid import UUID

import httpx
from dotenv import dotenv_values, set_key

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    path = ROOT / ".env"
    values = dotenv_values(path)
    required = ("VAPI_API_KEY", "GEMINI_API_KEY", "ELEVENLABS_API_KEY", "PUBLIC_BASE_URL")
    missing = [name for name in required if not (values.get(name) or "").strip()]
    if missing:
        print(json.dumps({"missing": missing, "requests_made": False}))
        return 2
    report = {"mode": "apply" if args.apply else "read_only", "credentials": {}}
    gateway_secret = (values.get("GEMINI_GATEWAY_SECRET") or "").strip()
    if args.apply and not gateway_secret:
        gateway_secret = secrets.token_urlsafe(48)
        set_key(str(path), "GEMINI_GATEWAY_SECRET", gateway_secret)
    if gateway_secret and len(gateway_secret) < 32:
        print("GEMINI_GATEWAY_SECRET must have at least 32 characters; no request made.")
        return 2
    plans = (
        ("VAPI_ELEVENLABS_CREDENTIAL_ID", "11labs", "servicevoice-elevenlabs", values["ELEVENLABS_API_KEY"]),
        ("VAPI_CUSTOM_LLM_CREDENTIAL_ID", "custom-llm", "servicevoice-gemini-gateway", gateway_secret),
    )
    try:
        with httpx.Client(base_url="https://api.vapi.ai", timeout=20, follow_redirects=False,
                          headers={"Authorization": "Bearer " + values["VAPI_API_KEY"]}) as client:
            for setting, provider, name, key in plans:
                saved = (values.get(setting) or "").strip()
                if saved:
                    response = client.get("/credential/" + str(UUID(saved)))
                    report["credentials"][provider] = {"http_status": response.status_code, "created": False}
                    if not response.is_success or response.json().get("provider") != provider:
                        print(json.dumps(report)); return 1
                elif args.apply:
                    response = client.post("/credential", json={"provider": provider, "name": name, "apiKey": key})
                    report["credentials"][provider] = {"http_status": response.status_code, "created": response.is_success}
                    if not response.is_success:
                        print(json.dumps(report)); return 1
                    set_key(str(path), setting, str(UUID(response.json()["id"])))
                else:
                    report["credentials"][provider] = {"configured": False, "created": False}
        if args.apply:
            set_key(str(path), "VAPI_CUSTOM_LLM_URL", values["PUBLIC_BASE_URL"].rstrip("/") + "/llm")
        print(json.dumps(report, indent=2))
        return 0
    except httpx.HTTPError:
        print("Provider connection incomplete. Check Vapi credentials before retrying a create; raw errors are withheld.")
    except (OSError, ValueError, KeyError, TypeError):
        print("Invalid local configuration or provider response. Credential values are withheld.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
