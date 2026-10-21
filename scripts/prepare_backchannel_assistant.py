"""Prepare an isolated Vapi acknowledgment candidate without making requests.

This helper reads only an explicitly supplied assistant snapshot. It never loads
an API key, creates an assistant, changes a number, or starts a call. Review the
private candidate/rollback files before a separate deployment procedure.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = "jev-backchannels-v1"
TAG = "servicevoice_experiment"
CANDIDATE_NAME = "ServiceVoice - Jev acknowledgment test"
# Verified official CreateAssistantDto snapshot, not every field returned by GET.
# https://github.com/VapiAI/server-sdk-typescript/blob/ce0b348412130447f18e77363de7a97ca12f244b/src/api/types/CreateAssistantDto.ts
CREATE_FIELDS = frozenset({
    "analysisPlan", "artifactPlan", "backgroundSound", "backgroundSpeechDenoisingPlan",
    "clientMessages", "compliancePlan", "credentialIds", "credentials", "endCallMessage",
    "endCallPhrases", "firstMessage", "firstMessageInterruptionsEnabled", "firstMessageMode",
    "hooks", "keypadInputPlan", "maxDurationSeconds", "metadata", "model",
    "modelOutputInMessagesEnabled", "monitorPlan", "name", "observabilityPlan", "server",
    "serverMessages", "startSpeakingPlan", "stopSpeakingPlan", "transcriber",
    "transportConfigurations", "voice", "voicemailDetection", "voicemailMessage",
})
READ_ONLY_FIELDS = frozenset({
    "id", "orgId", "createdAt", "updatedAt", "isServerUrlSecretSet", "latestVersion",
})
ALLOWED_PATHS = frozenset({
    "/name", "/model/url", "/voice/server/url", "/server/url",
    "/voice/chunkPlan", "/voice/chunkPlan/enabled", "/metadata", "/metadata/" + TAG,
})
_SECRET_FIELDS = frozenset({
    "apikey", "secret", "token", "password", "authorization", "authtoken",
    "accesstoken", "refreshtoken", "webhooksecret", "sessiontoken",
})


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def changed_paths(left, right, path=""):
    if isinstance(left, dict) and isinstance(right, dict):
        result = []
        for key in sorted(set(left) | set(right)):
            current = path + "/" + key.replace("~", "~0").replace("/", "~1")
            if key not in left or key not in right:
                result.append(current)
            else:
                result.extend(changed_paths(left[key], right[key], current))
        return result
    return [] if left == right else [path or "/"]


def _no_inline_secrets(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("assistant_dictionary_keys_invalid")
            normalized = re.sub(r"[^a-z]", "", key.lower())
            if normalized in _SECRET_FIELDS and item not in (None, ""):
                raise ValueError("inline_credentials_require_saved_credential_ids")
            if key == "credentials" and item not in (None, []):
                raise ValueError("inline_credentials_require_saved_credential_ids")
            _no_inline_secrets(item)
    elif isinstance(value, list):
        for item in value:
            _no_inline_secrets(item)


def writable_snapshot(payload):
    if not isinstance(payload, dict):
        raise ValueError("assistant_snapshot_must_be_object")
    if set(payload) - CREATE_FIELDS - READ_ONLY_FIELDS:
        raise ValueError("unknown_assistant_fields_require_schema_review")
    # JSON compatibility and finite values are checked without printing payloads.
    canonical(payload)
    _no_inline_secrets(payload)
    return {key: copy.deepcopy(value) for key, value in payload.items() if key in CREATE_FIELDS}


def _public_https(value):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("candidate_endpoint_requires_public_https")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or any(ord(char) < 33 for char in value)):
        raise ValueError("candidate_endpoint_requires_public_https")
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise ValueError("candidate_endpoint_requires_public_https")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValueError("candidate_endpoint_requires_public_https")
    # Validate the port now; urlsplit.port raises on malformed/out-of-range ports.
    try:
        parsed.port
    except ValueError:
        raise ValueError("candidate_endpoint_requires_public_https") from None
    return value.rstrip("/")


def build_candidate(payload, gateway_url, tts_url, *, webhook_url=None):
    """Return a create DTO; alter only isolated endpoints, name, tag and flush.

    Prompts, reasoner, Soniox, tools, native end-call, credentials, confirmations,
    recording and interruption plans remain exact copies. The backend's feature
    flag is separate environment configuration, never a prompt instruction.
    """
    original = writable_snapshot(payload)
    candidate = copy.deepcopy(original)
    model, voice = candidate.get("model"), candidate.get("voice")
    if not isinstance(model, dict) or model.get("provider") != "custom-llm":
        raise ValueError("candidate_requires_existing_custom_llm")
    if not isinstance(voice, dict) or voice.get("provider") != "custom-voice":
        raise ValueError("candidate_requires_existing_custom_voice")
    if not isinstance(voice.get("server"), dict):
        raise ValueError("candidate_requires_existing_voice_server")
    if not isinstance(model.get("messages"), list) or not model["messages"]:
        raise ValueError("candidate_requires_existing_prompt")
    if not isinstance(model.get("tools"), list) or not model["tools"]:
        raise ValueError("candidate_requires_existing_booking_tools")
    if not isinstance(candidate.get("transcriber"), dict):
        raise ValueError("candidate_requires_existing_transcriber")
    chunk = voice.get("chunkPlan", {})
    metadata = candidate.get("metadata", {})
    if not isinstance(chunk, dict) or not isinstance(metadata, dict):
        raise ValueError("candidate_chunk_plan_or_metadata_invalid")
    if TAG in metadata and metadata[TAG] != EXPERIMENT:
        raise ValueError("candidate_experiment_tag_collision")
    candidate["name"] = CANDIDATE_NAME
    model["url"] = _public_https(gateway_url)
    voice["server"]["url"] = _public_https(tts_url)
    voice["chunkPlan"] = {**chunk, "enabled": True}
    candidate["metadata"] = {**metadata, TAG: EXPERIMENT}
    if webhook_url is not None:
        if not isinstance(candidate.get("server"), dict):
            raise ValueError("candidate_requires_existing_webhook_server")
        candidate["server"]["url"] = _public_https(webhook_url)
    differences = changed_paths(original, candidate)
    if set(differences) - ALLOWED_PATHS:
        raise ValueError("candidate_has_unapproved_configuration_changes")
    return candidate


def candidate_audit(source, candidate):
    original = writable_snapshot(source)
    differences = changed_paths(original, candidate)
    return {
        "schema_version": EXPERIMENT,
        "source_sha256": sha256(source),
        "candidate_sha256": sha256(candidate),
        "changed_paths": differences,
        "only_allowed_changes": not bool(set(differences) - ALLOWED_PATHS),
        "excluded_read_only_fields": sorted(set(source) & READ_ONLY_FIELDS),
        "source_writable_fields": sorted(original),
        "provider_mutations": 0,
        "number_mapping_changed": False,
        "backend_feature_flag_is_not_in_prompt": True,
        "remote_create_and_readback_required": True,
        "credential_ids_preserved": original.get("credentialIds") == candidate.get("credentialIds"),
        "saved_configuration_is_not_effective_playback_proof": True,
    }


def verify_saved(plan, saved):
    actual = writable_snapshot(saved)
    differences = changed_paths(plan, actual)
    return {
        "saved_writable_exactly_matches_plan": not differences,
        "differences_requiring_review": differences,
        "provider_added_defaults_are_not_silently_accepted": True,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Private saved assistant JSON; no API read.")
    parser.add_argument("--gateway-url", required=True, help="Isolated custom-LLM base URL, normally ending /llm.")
    parser.add_argument("--tts-url", required=True, help="Isolated custom voice endpoint.")
    parser.add_argument("--webhook-url", help="Isolated authenticated booking webhook; otherwise preserved.")
    parser.add_argument("--output-directory", type=Path, help="New private directory; default is ignored .local/.")
    args = parser.parse_args(argv)
    try:
        if args.source.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("assistant_snapshot_oversized")
        source = json.loads(args.source.read_text(encoding="utf-8-sig"))
        candidate = build_candidate(source, args.gateway_url, args.tts_url, webhook_url=args.webhook_url)
        audit = candidate_audit(source, candidate)
        directory = args.output_directory or ROOT / ".local" / (
            "backchannel-plan-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
        directory.mkdir(parents=True, exist_ok=False)
        for name, value in (("source-rollback.json", source), ("candidate-create-plan.json", candidate),
                            ("clone-audit.json", audit)):
            path = directory / name
            with path.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.write("\n")
            path.chmod(0o600)
        print(json.dumps({"status": "dry_run_complete", "provider_mutations": 0,
                          "number_mapping_changed": False, "only_allowed_changes": audit["only_allowed_changes"],
                          "candidate_sha256": audit["candidate_sha256"]}))
        return 0
    except Exception:
        # A malformed snapshot/URL may contain a secret. Do not echo it or a
        # library exception, and never load ambient credentials for diagnostics.
        print(json.dumps({"status": "offline_candidate_prepare_failed", "provider_mutations": 0}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
