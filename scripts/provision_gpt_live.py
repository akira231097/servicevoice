"""Preview or create the isolated GPT-Live assistant; never edits another assistant.

Default is a read-only preview. --create-server-credential saves the candidate's
phone webhook secret as a Vapi bearer credential. --apply creates a NEW
assistant; --update ASSISTANT_ID changes only an assistant this script created
(name prefix "ServiceVoice GPT-Live", GPT-Live model) and never a production ID.
No credential values or provider response bodies are printed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from uuid import UUID

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import gpt_live  # noqa: E402

API = "https://api.vapi.ai"


def _uuid(value):
    try:
        return str(UUID(value)) if value else None
    except (TypeError, ValueError):
        return None


def openai_credential(client):
    rows = client.get("/credential")
    rows.raise_for_status()
    items = rows.json() if isinstance(rows.json(), list) else rows.json().get("results", [])
    found = [item["id"] for item in items if isinstance(item, dict) and item.get("provider") == "openai"]
    if len(found) > 1:
        raise ValueError("More than one OpenAI credential is saved in Vapi; name the one to use with --openai-credential-id.")
    return found[0] if found else None


def protected_ids(env):
    return {value for value in (env.get("VAPI_ASSISTANT_ID"), env.get("VAPI_PHONE_ASSISTANT_ID"),
                                env.get("VAPI_BACKCHANNEL_ASSISTANT_ID"), env.get("VAPI_EVAL_ASSISTANT_ID")) if value}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True, help="Private file holding VAPI_API_KEY and the production IDs to protect.")
    parser.add_argument("--secret-file", type=Path, help="Private file holding GPT_LIVE_PHONE_WEBHOOK_SECRET (candidate side).")
    parser.add_argument("--server-url", required=True)
    parser.add_argument("--server-credential-id")
    parser.add_argument("--openai-credential-id")
    parser.add_argument("--voice", default="gleam")
    parser.add_argument("--reasoner-model", default="gpt-5.6-terra", choices=gpt_live.REASONER_MODELS)
    parser.add_argument("--reasoning-effort", default="low", choices=gpt_live.REASONING_EFFORTS)
    parser.add_argument("--allow-vapi-billing", action="store_true", help="Proceed without an OpenAI credential (Vapi balance pays GPT-Live).")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--create-server-credential", action="store_true")
    action.add_argument("--apply", action="store_true")
    action.add_argument("--update")
    args = parser.parse_args(argv)
    env = dotenv_values(args.env_file, interpolate=False)
    key = env.get("VAPI_API_KEY") or env.get("VAPI_PRIVATE_KEY")
    if not key:
        print("VAPI_API_KEY is missing from the env file.")
        return 2
    with httpx.Client(base_url=API, headers={"Authorization": "Bearer " + key}, timeout=30,
                      trust_env=False, follow_redirects=False) as client:
        if args.create_server_credential:
            secret = (dotenv_values(args.secret_file, interpolate=False).get("GPT_LIVE_PHONE_WEBHOOK_SECRET") or "") if args.secret_file else ""
            if not 32 <= len(secret) <= 512:
                print("A 32-512 character GPT_LIVE_PHONE_WEBHOOK_SECRET is required in --secret-file.")
                return 2
            # Deliberately no retry: a create can succeed before a timeout.
            created = client.post("/credential", json={
                "provider": "custom-credential", "name": "servicevoice-gpt-live-webhook",
                "authenticationPlan": {"type": "bearer", "token": secret, "headerName": "Authorization",
                                       "bearerPrefixEnabled": True}})
            print(json.dumps({"server_credential_http_status": created.status_code,
                              "server_credential_id": _uuid(created.json().get("id")) if created.is_success else None}))
            return 0 if created.is_success else 1
        server_credential = _uuid(args.server_credential_id)
        if not server_credential:
            print("--server-credential-id must be the saved webhook credential UUID.")
            return 2
        openai_id = _uuid(args.openai_credential_id) or openai_credential(client)
        if not openai_id and not args.allow_vapi_billing:
            print("No OpenAI credential is connected in Vapi (Integrations -> OpenAI). Connect it, or pass --allow-vapi-billing.")
            return 2
        payload = gpt_live.assistant_payload(server_url=args.server_url, server_credential_id=server_credential,
                                             openai_credential_id=openai_id, voice_id=args.voice,
                                             reasoner_model=args.reasoner_model, reasoning_effort=args.reasoning_effort)
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        summary = {"name": payload["name"], "model": payload["model"]["model"], "reasoner": payload["model"]["reasoner"]["model"],
                   "reasoning_effort": payload["model"]["reasoner"]["reasoningEffort"],
                   "voice": payload["voice"]["voiceId"], "tools": len(payload["model"]["tools"]),
                   "server_url": payload["server"]["url"], "openai_credential": bool(openai_id),
                   "prompt_version": gpt_live.PROMPT_VERSION, "payload_sha256": digest}
        if not (args.apply or args.update):
            print(json.dumps({"preview": summary}, indent=2))
            return 0
        if args.apply:
            existing = client.get("/assistant", params={"limit": 100})
            existing.raise_for_status()
            if any(item.get("name") == payload["name"] for item in existing.json()):
                print("An assistant with this name already exists; use --update with its ID.")
                return 1
            created = client.post("/assistant", json=payload)
            print(json.dumps({"created_http_status": created.status_code,
                              "assistant_id": _uuid(created.json().get("id")) if created.is_success else None, **summary}))
            return 0 if created.is_success else 1
        target = _uuid(args.update)
        if not target or target in protected_ids(env):
            print("Refused: not a valid isolated GPT-Live assistant ID.")
            return 2
        current = client.get(f"/assistant/{target}")
        current.raise_for_status()
        body = current.json()
        if not str(body.get("name", "")).startswith(gpt_live.NAME_PREFIX) or (body.get("model") or {}).get("model") != gpt_live.MODEL:
            print("Refused: the target is not an assistant created by this script.")
            return 2
        updated = client.patch(f"/assistant/{target}", json=payload)
        print(json.dumps({"updated_http_status": updated.status_code, "assistant_id": target, **summary}))
        return 0 if updated.is_success else 1


if __name__ == "__main__":
    raise SystemExit(main())
