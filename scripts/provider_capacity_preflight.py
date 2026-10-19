"""Read-only provider checks; never start calls, buy capacity or print credentials.

Account quotas not exposed by these endpoints remain explicitly unverified.
Use --env-file to select a private configuration file. Output contains counts,
HTTP status codes and selected capability fields, never raw provider responses.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import UUID

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def request_json(client, url, headers):
    try:
        response = client.get(url, headers=headers)
        try:
            payload = response.json() if response.is_success else None
        except ValueError:
            payload = None
        return response.status_code, payload
    except httpx.HTTPError:
        return "network_error", None


def rows(payload):
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for field in ("results", "data", "items"):
            if isinstance(payload.get(field), list):
                return rows(payload[field])
        return [payload]
    return []


def numeric_fields(payload, fields):
    """Read only documented/explicit quota names, not arbitrary response values."""
    found = {}
    for item in rows(payload):
        for container in (item, item.get("subscriptionLimits"), item.get("subscription"), item.get("limits")):
            if not isinstance(container, dict):
                continue
            for field in fields:
                value = container.get(field)
                if type(value) in (int, float) and value >= 0:
                    found[field] = value
    return found


def check(values, client):
    report = {"read_only": True, "calls_started": 0, "purchases": 0,
              "target_simultaneous_calls": 10, "vapi": {}, "elevenlabs": {}, "gemini": {}}
    private = values.get("VAPI_API_KEY")
    if private:
        headers = {"Authorization": "Bearer " + private}
        for path, label in (("/org", "organization"), ("/phone-number", "phone_numbers")):
            code, payload = request_json(client, "https://api.vapi.ai" + path, headers)
            result = {"http_status": code}
            if code == 200 and payload is not None:
                result["count"] = len(rows(payload))
                if label == "organization":
                    result["reported_limits"] = numeric_fields(payload, (
                        "concurrencyLimit", "remainingConcurrentCalls", "concurrencyCounter", "concurrency"))
                else:
                    result["vapi_managed_count"] = sum(r.get("provider") == "vapi" for r in rows(payload))
                    result["unassigned_assistant_count"] = sum(not r.get("assistantId") for r in rows(payload))
            report["vapi"][label] = result
        for key, label in (("VAPI_ASSISTANT_ID", "browser_assistant"),
                           ("VAPI_PHONE_ASSISTANT_ID", "phone_assistant")):
            configured = values.get(key)
            if not configured:
                report["vapi"][label] = {"configured": False}
                continue
            try:
                identifier = str(UUID(configured))
            except (ValueError, TypeError, AttributeError):
                report["vapi"][label] = {"configuration_valid": False}
                continue
            code, payload = request_json(client, "https://api.vapi.ai/assistant/" + identifier, headers)
            result = {"http_status": code}
            if code == 200 and isinstance(payload, dict):
                result.update({"tool_count": len(payload.get("model", {}).get("tools", [])),
                               "recording_enabled": payload.get("artifactPlan", {}).get("recordingEnabled"),
                               "server_configured": bool(payload.get("server", {}).get("url"))})
            report["vapi"][label] = result
    else:
        report["vapi"] = {"key_present": False}
    if values.get("ELEVENLABS_API_KEY"):
        code, payload = request_json(client, "https://api.elevenlabs.io/v1/user/subscription",
                                     {"xi-api-key": values["ELEVENLABS_API_KEY"]})
        report["elevenlabs"] = {"http_status": code, "reported_limits": numeric_fields(payload, (
            "max_concurrent_requests", "max_concurrent_tts_requests", "max_concurrent_dialogue_sessions"))}
    else:
        report["elevenlabs"] = {"key_present": False}
    if values.get("GEMINI_API_KEY"):
        code, payload = request_json(client,
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash",
            {"x-goog-api-key": values["GEMINI_API_KEY"]})
        report["gemini"] = {"model_lookup_http_status": code,
                            "generate_content_listed": isinstance(payload, dict) and
                            "generateContent" in payload.get("supportedGenerationMethods", [])}
    else:
        report["gemini"] = {"key_present": False}
    report["still_required"] = [
        "Confirm actual Vapi subscription supports at least 10 simultaneous calls; legacy entitlement may differ.",
        "Verify Gemini project RPM/TPM and actual speech-provider concurrency for the selected voice integration.",
        "Verify deployed HTTPS and authenticated phone routing, then run staged real calls up to 10.",
    ]
    report["ten_real_calls_verified"] = False
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not args.env_file.is_file():
        print("Private environment file is unavailable. No requests made.")
        return 2
    values = dotenv_values(args.env_file)
    with httpx.Client(timeout=15, follow_redirects=False) as client:
        report = check(values, client)
    output = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
