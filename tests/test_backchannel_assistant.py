"""Offline candidate boundaries; no keys, network, calls or number mutations."""
from copy import deepcopy
import json

import pytest

from scripts.prepare_backchannel_assistant import (
    ALLOWED_PATHS, CREATE_FIELDS, EXPERIMENT, READ_ONLY_FIELDS, TAG,
    build_candidate, candidate_audit, main, verify_saved,
)

GATEWAY = "https://candidate.example/ack/llm"
TTS = "https://candidate.example/ack/tts/vapi"
WEBHOOK = "https://candidate.example/ack/webhooks/vapi/inbound"


def source():
    return {
        "id": "11111111-1111-4111-8111-111111111111", "orgId": "saved-owner",
        "createdAt": "2026-10-20T12:00:00Z", "updatedAt": "2026-10-20T13:00:00Z",
        "isServerUrlSecretSet": True, "latestVersion": {"id": "saved-version"},
        "name": "Accepted assistant",
        "model": {"provider": "custom-llm", "url": "https://accepted.example/llm",
                  "model": "gemini-3.8-flash", "maxTokens": 600, "temperature": .2,
                  "messages": [{"role": "system", "content": "Exact guard: read back, then require explicit confirmation."}],
                  "tools": [{"type": "function", "function": {"name": "confirm_action", "parameters": {"type": "object"}}},
                            {"type": "endCall"}], "metadataSendMode": "off"},
        "voice": {"provider": "custom-voice", "server": {"url": "https://accepted.example/tts/vapi",
                  "credentialId": "speech-saved-id", "timeoutSeconds": 30,
                  "backoffPlan": {"type": "fixed", "maxRetries": 0, "baseDelaySeconds": 1}},
                  "cachingEnabled": False, "chunkPlan": {"enabled": False, "minCharacters": 30,
                      "punctuationBoundaries": [".", "!", "?"]}},
        "transcriber": {"provider": "soniox", "model": "stt-rt-v5", "language": "auto"},
        "server": {"url": "https://accepted.example/webhooks/vapi/inbound", "credentialId": "webhook-saved-id"},
        "credentialIds": ["speech-saved-id", "model-saved-id", "webhook-saved-id"],
        "firstMessage": "Exact accepted greeting.", "firstMessageMode": "assistant-speaks-first",
        "firstMessageInterruptionsEnabled": True,
        "startSpeakingPlan": {"waitSeconds": .1},
        "stopSpeakingPlan": {"numWords": 0, "voiceSeconds": .2, "backoffSeconds": .7},
        "endCallMessage": "Exact accepted farewell.", "endCallPhrases": [],
        "maxDurationSeconds": 300, "voicemailDetection": "off",
        "artifactPlan": {"recordingEnabled": False},
        "serverMessages": ["tool-calls", "transcript", "status-update", "end-of-call-report"],
        "clientMessages": ["transcript", "speech-update", "user-interrupted"],
        "metadata": {"deployment": "accepted-value"},
    }


def test_candidate_preserves_reasoner_stt_tools_confirmations_voice_and_lifecycle_exactly():
    original = source()
    before = deepcopy(original)
    candidate = build_candidate(original, GATEWAY, TTS, webhook_url=WEBHOOK)
    assert original == before
    assert not (set(candidate) & READ_ONLY_FIELDS)
    expected = {key: deepcopy(value) for key, value in original.items() if key in CREATE_FIELDS}
    expected["name"] = candidate["name"]
    expected["model"]["url"] = GATEWAY
    expected["voice"]["server"]["url"] = TTS
    expected["voice"]["chunkPlan"]["enabled"] = True
    expected["server"]["url"] = WEBHOOK
    expected["metadata"][TAG] = EXPERIMENT
    assert candidate == expected
    assert candidate["model"]["messages"] == original["model"]["messages"]
    assert candidate["model"]["tools"] == original["model"]["tools"]
    assert candidate["transcriber"] == original["transcriber"]
    audit = candidate_audit(original, candidate)
    assert audit["only_allowed_changes"] and audit["credential_ids_preserved"]
    assert set(audit["changed_paths"]) <= ALLOWED_PATHS
    assert audit["provider_mutations"] == 0 and not audit["number_mapping_changed"]


def test_optional_webhook_is_preserved_and_missing_chunk_plan_metadata_are_added_only_as_needed():
    original = source()
    original["voice"].pop("chunkPlan")
    original.pop("metadata")
    candidate = build_candidate(original, GATEWAY, TTS)
    assert candidate["server"] == original["server"]
    assert candidate["voice"]["chunkPlan"] == {"enabled": True}
    assert candidate["metadata"] == {TAG: EXPERIMENT}
    assert candidate_audit(original, candidate)["only_allowed_changes"]


@pytest.mark.parametrize("change", [
    lambda payload: payload.update(newUnknownField={"controls": "behavior"}),
    lambda payload: payload["model"].update(provider="google"),
    lambda payload: payload["voice"].update(provider="11labs"),
    lambda payload: payload["voice"].update(server=None),
    lambda payload: payload["model"].update(messages=[]),
    lambda payload: payload["model"].update(tools=[]),
    lambda payload: payload.update(transcriber=None),
    lambda payload: payload["voice"].update(chunkPlan=None),
    lambda payload: payload.update(metadata=[]),
    lambda payload: payload["metadata"].update({TAG: "another-experiment"}),
    lambda payload: payload.update(credentials=[{"provider": "google", "apiKey": "SYNTHETIC-NOT-A-REAL-KEY"}]),
    lambda payload: payload["voice"]["server"].update(headers={"Authorization": "Bearer SYNTHETIC"}),
])
def test_unsupported_or_credential_bearing_sources_fail_closed(change):
    original = source()
    change(original)
    with pytest.raises(ValueError):
        build_candidate(original, GATEWAY, TTS)


@pytest.mark.parametrize("url", [
    "http://candidate.example/llm", "https://localhost/llm", "https://127.0.0.1/llm",
    "https://10.0.0.1/llm", "https://user:pass@candidate.example/llm",
    "https://candidate.example/llm?secret=SYNTHETIC", "https://candidate.example/llm#fragment",
    " https://candidate.example/llm", "https://candidate.example:99999/llm",
])
def test_candidate_endpoint_validation_rejects_unsafe_or_unreachable_forms(url):
    with pytest.raises(ValueError):
        build_candidate(source(), url, TTS)
    with pytest.raises(ValueError):
        build_candidate(source(), GATEWAY, url)
    with pytest.raises(ValueError):
        build_candidate(source(), GATEWAY, TTS, webhook_url=url)


@pytest.mark.parametrize("change", [
    lambda payload: payload["model"].update(temperature=.9),
    lambda payload: payload["model"]["tools"].pop(),
    lambda payload: payload["model"]["messages"][0].update(content="different confirmation policy"),
    lambda payload: payload["transcriber"].update(provider="google"),
    lambda payload: payload["voice"]["chunkPlan"].update(minCharacters=999),
    lambda payload: payload.update(stopSpeakingPlan={"numWords": 9}),
    lambda payload: payload.update(maxDurationSeconds=600),
    lambda payload: payload.update(newProviderDefault=True),
])
def test_readback_detects_changed_safeguards_and_unreviewed_provider_defaults(change):
    plan = build_candidate(source(), GATEWAY, TTS, webhook_url=WEBHOOK)
    saved = deepcopy(plan)
    saved.update(id="22222222-2222-4222-8222-222222222222", orgId="saved-owner")
    assert verify_saved(plan, saved)["saved_writable_exactly_matches_plan"]
    change(saved)
    if "newProviderDefault" in saved:
        with pytest.raises(ValueError, match="unknown_assistant_fields"):
            verify_saved(plan, saved)
    else:
        audit = verify_saved(plan, saved)
        assert not audit["saved_writable_exactly_matches_plan"] and audit["differences_requiring_review"]


def test_cli_is_offline_default_and_preserves_immutable_rollback_without_echoing_payload(tmp_path, monkeypatch, capsys):
    import socket
    monkeypatch.setattr(socket, "create_connection", lambda *_args, **_kwargs: pytest.fail("unexpected network"))
    original = source()
    original["model"]["messages"][0]["content"] += " SYNTHETIC-PRIVATE-SENTINEL"
    source_path, directory = tmp_path / "source.json", tmp_path / "private-plan"
    source_path.write_text(json.dumps(original), encoding="utf-8")
    arguments = ["--source", str(source_path), "--gateway-url", GATEWAY, "--tts-url", TTS,
                 "--webhook-url", WEBHOOK, "--output-directory", str(directory)]
    assert main(arguments) == 0
    printed = capsys.readouterr().out
    assert "SYNTHETIC-PRIVATE-SENTINEL" not in printed and "saved-owner" not in printed
    assert json.loads(printed)["provider_mutations"] == 0
    assert json.loads((directory / "source-rollback.json").read_text()) == original
    expected = build_candidate(original, GATEWAY, TTS, webhook_url=WEBHOOK)
    assert json.loads((directory / "candidate-create-plan.json").read_text()) == expected
    assert main(arguments) == 1  # A later attempt cannot overwrite the rollback.
    assert json.loads((directory / "source-rollback.json").read_text()) == original


def test_cli_failure_does_not_echo_inline_key_or_malformed_url(tmp_path, capsys):
    original = source()
    original["credentials"] = [{"provider": "google", "apiKey": "SYNTHETIC-PRIVATE-SENTINEL"}]
    source_path = tmp_path / "source.json"
    source_path.write_text(json.dumps(original), encoding="utf-8")
    assert main(["--source", str(source_path), "--gateway-url", GATEWAY, "--tts-url", TTS]) == 1
    assert "SYNTHETIC-PRIVATE-SENTINEL" not in capsys.readouterr().out
