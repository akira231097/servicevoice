"""Preview a saved Vapi assistant; make changes only with --apply.

Run from any directory with the project's Python environment. No phone numbers
or calls are created. Authentication stays on the saved assistant, not in the
browser's assistant overrides.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
API_BASE = "https://api.vapi.ai"
from app.voice_profiles import MODEL_PROFILES, VOICE_PROFILES, profile_catalog, select_profiles, select_transcriber
from app.gemini_gateway import MAX_OUTPUT_TOKENS
from app.tool_schema import provider_schema
from app.call_lifecycle import CLOSING_POLICY, enabled as closing_enabled, native_tool


def normalize_tools(schemas: list[dict]) -> list[dict]:
    """Accept OpenAI nested or realtime flat schemas; emit Vapi function tools."""
    normalized, names = [], set()
    for original in schemas:
        function = original.get("function", original)
        name = function.get("name", "")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name):
            raise ValueError("Every tool needs a valid function name.")
        if name in names:
            raise ValueError(f"Duplicate tool name: {name}")
        names.add(name)
        parameters = function.get("parameters")
        if not isinstance(parameters, dict) or parameters.get("type") != "object":
            raise ValueError(f"Tool {name} needs an object JSON parameter schema.")
        normalized.append({
            "type": "function",
            "async": False,
            # An omitted request-start uses Vapi's random filler. Empty content
            # suppresses it; do not delay tool execution behind spoken text.
            # This improves speech behavior, not guaranteed response latency.
            "messages": [
                {"type": "request-start", "content": "", "blocking": False},
                {"type": "request-response-delayed", "content": "I'm still waiting for the result.",
                 "timingMilliseconds": 5000},
            ],
            "function": {
                "name": name,
                "description": function.get("description", ""),
                "parameters": provider_schema(copy.deepcopy(parameters)),
            },
        })
    if not normalized:
        raise ValueError("TOOL_SCHEMAS is empty; refusing to provision a tool-less booking agent.")
    return normalized


def validate_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("PUBLIC_BASE_URL must be an HTTPS URL without credentials, query, or fragment.")
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise ValueError("PUBLIC_BASE_URL must be reachable by Vapi, not a local hostname.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValueError("PUBLIC_BASE_URL must not contain a private or loopback IP address.")
    return value.rstrip("/")


def assistant_payload(prompt: str, schemas: list[dict], env: dict, *, apply: bool,
                      model_profile=None, voice_profile=None, allow_experimental=False) -> dict:
    base_url = env.get("PUBLIC_BASE_URL", "").strip()
    credential_id = env.get("VAPI_SERVER_CREDENTIAL_ID", "").strip()
    if apply and (not base_url or not credential_id):
        raise ValueError("Set PUBLIC_BASE_URL and VAPI_SERVER_CREDENTIAL_ID before --apply.")
    if base_url:
        base_url = validate_base_url(base_url)
    if credential_id:
        try:
            UUID(credential_id)
        except ValueError as exc:
            raise ValueError("VAPI_SERVER_CREDENTIAL_ID must be the saved credential UUID.") from exc
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("SYSTEM_PROMPT must be a nonempty string.")
    model, voice, credentials, _ = select_profiles(env, model_profile=model_profile, voice_profile=voice_profile,
                                                  apply=apply, allow_experimental=allow_experimental)
    native_closing=closing_enabled(env)
    if native_closing and model.get("provider")!="custom-llm":
        raise ValueError("Native end-call opt-in requires the guarded custom Gemini gateway.")
    if native_closing:prompt += "\n" + CLOSING_POLICY
    transcriber, transcription_credentials = select_transcriber(env, apply=apply)
    credentials = list(dict.fromkeys([*credentials, *transcription_credentials]))
    if "url" in model:
        model["url"] = validate_base_url(model["url"])
    payload = {
        "name": "ServiceVoice - dealership service demo",
        "firstMessage": "Hi, I'm Cedar. This is an AI demo for Cedar Service, a fictional dealership. How can I help with your service visit?",
        "firstMessageMode": "assistant-speaks-first",
        "firstMessageInterruptionsEnabled": True,
        "model": {
            **model,
            "maxTokens": MAX_OUTPUT_TOKENS if model.get("provider") == "custom-llm" else 600,
            "messages": [{"role": "system", "content": prompt}],
            "tools": normalize_tools(schemas) + ([native_tool()] if native_closing else []),
        },
        "transcriber": transcriber,
        "voice": voice,
        "server": {
            "url": (base_url or "https://REPLACE-WITH-PUBLIC-HOST.example") + "/webhooks/vapi",
            "credentialId": credential_id or "REPLACE-WITH-SAVED-CREDENTIAL-ID",
            "timeoutSeconds": 8,
        },
        "clientMessages": ["transcript", "speech-update", "status-update", "tool-calls", "user-interrupted"],
        "serverMessages": ["tool-calls", "status-update", "end-of-call-report", "transcript"],
        "artifactPlan": {"recordingEnabled": False},
        "maxDurationSeconds": 300,
        "backgroundSound": "off",
    }
    if credentials:
        # This is an allowlist for ALL call credentials, including server auth.
        # Omitting the webhook credential causes Vapi-originated requests to
        # arrive unsigned even though server.credentialId is configured.
        payload["credentialIds"] = list(dict.fromkeys([
            *credentials, credential_id or "REPLACE-WITH-SAVED-CREDENTIAL-ID",
        ]))
    return payload


def redact(value, secrets: list[str]):
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if re.sub(r"[^a-z]", "", key.lower()) in {
                "secret", "token", "password", "apikey", "authorization",
                "authtoken", "accesstoken", "refreshtoken", "webhooksecret", "sessiontoken",
            }
            else redact(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in sorted(secrets, key=len, reverse=True):
            if secret:
                value = value.replace(secret, "[REDACTED]")
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Create/update the saved assistant in Vapi.")
    parser.add_argument("--create-new", action="store_true", help="Create a new assistant even if an ID is configured.")
    parser.add_argument("--model-profile", choices=tuple(MODEL_PROFILES), help="Model profile; overrides VAPI_MODEL_PROFILE.")
    parser.add_argument("--voice-profile", choices=tuple(VOICE_PROFILES), help="Voice profile; overrides VAPI_VOICE_PROFILE.")
    parser.add_argument("--allow-experimental", action="store_true", help="Explicitly select the unverified Gemini custom-adapter path.")
    parser.add_argument("--list-profiles", action="store_true", help="List documented and blocked profiles without reading configuration.")
    parser.add_argument("--offline", action="store_true", help="Preview with empty configuration: do not read .env or process credentials; cannot apply.")
    parser.add_argument("--output", type=Path, default=ROOT / ".local" / "vapi-assistant.preview.json",
                        help="Path for the redacted configuration preview.")
    args = parser.parse_args(argv)
    if args.list_profiles:
        print(json.dumps(profile_catalog(), indent=2))
        return 0
    if args.offline and args.apply:
        parser.error("--offline cannot be combined with --apply.")

    try:
        if not args.offline:
            from dotenv import load_dotenv
            load_dotenv(ROOT / ".env", override=False)
        from app.tools import SYSTEM_PROMPT, TOOL_SCHEMAS
        env = {} if args.offline else dict(os.environ)
        options = {"apply": args.apply, "model_profile": args.model_profile, "voice_profile": args.voice_profile,
                   "allow_experimental": args.allow_experimental}
        payload = assistant_payload(SYSTEM_PROMPT, TOOL_SCHEMAS, env, **options)
        _, _, _, manifest = select_profiles(env, **options)
        manifest["transcriber_profile"] = env.get("VAPI_TRANSCRIBER_PROFILE", "").strip() or "soniox-v5-auto"
        manifest["transcriber"] = payload["transcriber"]
        manifest["sources"] += profile_catalog()["transcriber_profiles"][manifest["transcriber_profile"]]["sources"]
        secrets = [value for key, value in env.items() if re.search(r"(?:KEY|TOKEN|SECRET|PASSWORD)$", key, re.I) and value]
        preview = redact(payload, secrets)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(preview, indent=2) + "\n", encoding="utf-8")
        manifest["preview_sha256"] = hashlib.sha256(json.dumps(preview, sort_keys=True).encode()).hexdigest()
        args.output.with_suffix(".meta.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        print(f"Redacted assistant preview: {args.output.resolve()}")
        print(f"Profiles: {manifest['model_profile']} / {manifest['voice_profile']}; metadata saved alongside preview.")
        print(f"Configured {len([tool for tool in payload['model']['tools'] if tool['type']=='function'])} business tools; recording is disabled.")
        if not args.apply:
            print("Dry run complete. No external requests were made.")
            return 0

        private_key = env.get("VAPI_API_KEY", "").strip()
        webhook_secret = env.get("VAPI_WEBHOOK_SECRET", "").strip()
        if not private_key:
            raise ValueError("Set VAPI_API_KEY in .env before --apply.")
        if len(webhook_secret) < 32:
            raise ValueError("VAPI_WEBHOOK_SECRET must contain at least 32 characters and match the saved Vapi credential.")
        assistant_id = "" if args.create_new else env.get("VAPI_ASSISTANT_ID", "").strip()
        if assistant_id:
            try:
                UUID(assistant_id)
            except ValueError as exc:
                raise ValueError("VAPI_ASSISTANT_ID must be a saved assistant UUID.") from exc
        import httpx
        endpoint = f"{API_BASE}/assistant" + (f"/{assistant_id}" if assistant_id else "")
        method = "PATCH" if assistant_id else "POST"
        try:
            # No automatic retries: a timeout after POST may have created the resource.
            with httpx.Client(timeout=30.0, follow_redirects=False) as client:
                response = client.request(method, endpoint, json=payload,
                                          headers={"Authorization": f"Bearer {private_key}"})
        except httpx.RequestError:
            print("Vapi request did not complete. Inspect the Vapi dashboard before retrying; a create may have succeeded.", file=sys.stderr)
            return 1
        if not response.is_success:
            print(f"Vapi rejected the request (HTTP {response.status_code}).", file=sys.stderr)
            try:
                issue=response.json().get("message","No validation detail provided.")
                if isinstance(issue,(str,list)):
                    print("Validation detail: "+json.dumps(redact(issue,secrets))[:2400],file=sys.stderr)
            except (ValueError,AttributeError):
                print("No structured validation detail available.",file=sys.stderr)
            return 1
        try:
            saved_id = str(UUID(response.json()["id"]))
        except (ValueError, KeyError, TypeError):
            print("Vapi returned success without a valid assistant ID. Inspect the dashboard before retrying.", file=sys.stderr)
            return 1
        try:
            from dotenv import set_key
            set_key(str(ROOT / ".env"), "VAPI_ASSISTANT_ID", saved_id, quote_mode="always")
        except OSError:
            print(f"Vapi saved assistant {saved_id}, but .env could not be updated. Copy this nonsecret ID manually.", file=sys.stderr)
            return 1
        print("Saved assistant configured successfully; VAPI_ASSISTANT_ID was updated in .env.")
        print("Restart the app and run the live browser acceptance scenarios. Voice and PSTN remain unverified until tested.")
        return 0
    except (ImportError, ValueError, OSError) as exc:
        # Only configuration errors constructed above are echoed. No network bodies or keys.
        if isinstance(exc, ImportError):
            print("Install requirements and ensure app.tools exports SYSTEM_PROMPT and TOOL_SCHEMAS.", file=sys.stderr)
        elif isinstance(exc, OSError):
            print("Could not write the local configuration preview. Check the output directory permissions.", file=sys.stderr)
        else:
            print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
