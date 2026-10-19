"""Profile compatibility and privacy checks; no credentials or provider calls."""
import copy
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("profile_provisioner", ROOT / "scripts" / "provision_vapi.py")
provisioner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provisioner)
from app.tools import SYSTEM_PROMPT, TOOL_SCHEMAS
from app.voice_profiles import profile_catalog, select_profiles

CREDENTIAL = "11111111-1111-4111-8111-111111111111"
ELEVEN_CREDENTIAL = "22222222-2222-4222-8222-222222222222"
WEBHOOK_CREDENTIAL = "33333333-3333-4333-8333-333333333333"


def payload(env=None, **options):
    return provisioner.assistant_payload(SYSTEM_PROMPT, TOOL_SCHEMAS, env or {}, apply=False, **options)


def test_default_configuration_uses_automatic_recognition():
    result = payload()
    assert result["model"]["provider"] == "openai"
    assert result["model"]["model"] == "gpt-4.1-mini"
    assert result["model"]["temperature"] == 0.2
    assert "reasoningEffort" not in result["model"]
    assert result["voice"] == {"provider": "vapi", "voiceId": "Elliot", "version": 2}
    assert result["transcriber"] == {"provider": "soniox", "model": "stt-rt-v5", "languages": []}
    assert result["maxDurationSeconds"] == 300
    assert result["artifactPlan"]["recordingEnabled"] is False


def test_all_function_tools_suppress_immediate_filler_without_speaking_a_success():
    original_schemas = copy.deepcopy(TOOL_SCHEMAS)
    result = payload()
    tools = result["model"]["tools"]
    names = {tool["function"]["name"] for tool in tools}
    assert {"get_dealership_info", "prepare_booking", "confirm_action", "request_handoff"} <= names
    for tool in tools:
        assert tool["type"] == "function" and tool["async"] is False
        assert tool["messages"] == [
            {"type": "request-start", "content": "", "blocking": False},
            {"type": "request-response-delayed", "content": "I'm still waiting for the result.",
             "timingMilliseconds": 5000},
        ]
        # There must be no fixed success message: only actual tool results may
        # authorize a spoken booking confirmation or local handoff status.
        assert not any(message["type"] == "request-complete" for message in tool["messages"])
    assert TOOL_SCHEMAS == original_schemas
    assert "startSpeakingPlan" not in result and "stopSpeakingPlan" not in result
    assert result["transcriber"] == {"provider": "soniox", "model": "stt-rt-v5", "languages": []}


def test_luna_uses_none_for_tools_with_standard_tier():
    result = payload(model_profile="luna")
    model = result["model"]
    assert model["model"] == "gpt-6-luna"
    assert model["reasoningEffort"] == "none"
    assert model["temperature"] == 0.2
    assert model["serviceTier"] == "default"
    assert model["tools"] == payload()["model"]["tools"]
    assert model["messages"] == payload()["model"]["messages"]


def test_auto_transcription_omits_language_and_scopes_saved_credential():
    env = {"VAPI_TRANSCRIBER_PROFILE": "eleven-scribe-v2-auto",
           "VAPI_ELEVENLABS_CREDENTIAL_ID": ELEVEN_CREDENTIAL,
           "VAPI_SERVER_CREDENTIAL_ID": WEBHOOK_CREDENTIAL}
    result = payload(env)
    assert result["transcriber"] == {"provider": "11labs", "model": "scribe_v2_realtime"}
    assert result["credentialIds"] == [ELEVEN_CREDENTIAL, WEBHOOK_CREDENTIAL]
    assert "language" not in result["transcriber"] and "secondaryLanguages" not in result["transcriber"]
    # The same saved key can serve both speech directions without duplicate IDs.
    both = payload(env, voice_profile="eleven-v4-turbo")
    assert both["credentialIds"] == [ELEVEN_CREDENTIAL, WEBHOOK_CREDENTIAL]


def test_soniox_detection_has_no_language_whitelist_or_new_key_requirement():
    env = {"VAPI_TRANSCRIBER_PROFILE": "soniox-v5-auto",
           "VAPI_ELEVENLABS_CREDENTIAL_ID": ELEVEN_CREDENTIAL,
           "VAPI_SERVER_CREDENTIAL_ID": WEBHOOK_CREDENTIAL}
    result = payload(env, voice_profile="eleven-v4-turbo")
    assert result["transcriber"] == {"provider": "soniox", "model": "stt-rt-v5", "languages": []}
    assert "language" not in result["transcriber"]
    assert result["credentialIds"] == [ELEVEN_CREDENTIAL, WEBHOOK_CREDENTIAL]


def test_transcriber_selection_rejects_unknown_or_uncredentialed_apply():
    from app.voice_profiles import select_transcriber
    with pytest.raises(ValueError, match="Unknown"):
        select_transcriber({"VAPI_TRANSCRIBER_PROFILE": "invented"})
    with pytest.raises(ValueError, match="saved"):
        select_transcriber({"VAPI_TRANSCRIBER_PROFILE": "eleven-scribe-v2-auto"}, apply=True)
    with pytest.raises(ValueError, match="UUID"):
        select_transcriber({"VAPI_TRANSCRIBER_PROFILE": "eleven-scribe-v2-auto",
                            "VAPI_ELEVENLABS_CREDENTIAL_ID": "not-a-key"})


def test_transcriber_return_is_detached_from_profile_registry():
    from app.voice_profiles import select_transcriber
    env = {"VAPI_TRANSCRIBER_PROFILE": "eleven-scribe-v2-auto"}
    settings, _ = select_transcriber(env)
    settings["language"] = "forced"
    assert "language" not in select_transcriber(env)[0]


@pytest.mark.parametrize("profile,exact", [("eleven-flash-2.5", "eleven_flash_v2_5"), ("eleven-v4-turbo", "eleven_v4_turbo")])
def test_eleven_exact_models_and_scoped_credentials(profile, exact):
    result = payload({"VAPI_ELEVENLABS_VOICE_ID": "sarah", "VAPI_ELEVENLABS_CREDENTIAL_ID": ELEVEN_CREDENTIAL,
                      "VAPI_SERVER_CREDENTIAL_ID": WEBHOOK_CREDENTIAL,
                      "ELEVENLABS_API_KEY": "SYNTHETIC-PRIVATE-KEY"}, voice_profile=profile)
    assert result["voice"] == {"provider": "11labs", "model": exact, "voiceId": "sarah"}
    assert result["credentialIds"] == [ELEVEN_CREDENTIAL, WEBHOOK_CREDENTIAL]
    assert "SYNTHETIC-PRIVATE-KEY" not in json.dumps(result)


@pytest.mark.parametrize("env,options", [
    ({"VAPI_MODEL_PROFILE": "invented"}, {}),
    ({"VAPI_MODEL": "gpt-unverified"}, {}),
    ({"VAPI_VOICE_PROFILE": "invented"}, {}),
    ({}, {"model_profile": "gemini-3.8-flash"}),
    ({}, {"model_profile": "gemini-3.8-flash-custom"}),
    ({"VAPI_ELEVENLABS_VOICE_ID": "https://example.com/secret"}, {"voice_profile": "eleven-v4-turbo"}),
    ({"VAPI_ELEVENLABS_CREDENTIAL_ID": "not-a-uuid"}, {"voice_profile": "eleven-v4-turbo"}),
])
def test_unsupported_or_unsafe_profiles_rejected(env, options):
    with pytest.raises(ValueError):
        payload(env, **options)


def test_cli_selection_precedes_named_env_and_legacy_alias():
    result = payload({"VAPI_MODEL_PROFILE": "luna", "VAPI_MODEL": "gpt-unverified", "VAPI_VOICE_PROFILE": "eleven-v4-turbo"},
                     model_profile="baseline", voice_profile="elliot-v2")
    assert result["model"]["model"] == "gpt-4.1-mini"
    assert result["voice"]["provider"] == "vapi"
    assert payload({"VAPI_MODEL": "gpt-6-luna"})["model"]["reasoningEffort"] == "none"


def test_custom_gemini_exact_model_metadata_and_no_invented_fields():
    result = payload({"VAPI_CUSTOM_LLM_URL": "https://adapter.example/llm", "VAPI_CUSTOM_LLM_CREDENTIAL_ID": CREDENTIAL,
                      "VAPI_SERVER_CREDENTIAL_ID": WEBHOOK_CREDENTIAL,
                      "GEMINI_API_KEY": "SYNTHETIC-GOOGLE-KEY"}, model_profile="gemini-3.8-flash-custom", allow_experimental=True)
    model = result["model"]
    assert model["model"] == "gemini-3.8-flash"
    assert model["provider"] == "custom-llm"
    assert model["url"] == "https://adapter.example/llm"
    assert model["metadataSendMode"] == "variable"
    assert "reasoningEffort" not in model and "thinkingLevel" not in model and "temperature" not in model
    assert result["credentialIds"] == [CREDENTIAL, WEBHOOK_CREDENTIAL]
    assert "SYNTHETIC-GOOGLE-KEY" not in json.dumps(result)
    assert model["tools"] == payload()["model"]["tools"]


def test_explicit_call_credentials_include_webhook_and_deduplicate():
    env = {"VAPI_CUSTOM_LLM_URL": "https://adapter.example/llm", "VAPI_CUSTOM_LLM_CREDENTIAL_ID": CREDENTIAL,
           "VAPI_SERVER_CREDENTIAL_ID": WEBHOOK_CREDENTIAL, "VAPI_ELEVENLABS_CREDENTIAL_ID": ELEVEN_CREDENTIAL}
    result = payload(env, model_profile="gemini-3.8-flash-custom", voice_profile="eleven-v4-turbo", allow_experimental=True)
    assert result["credentialIds"] == [CREDENTIAL, ELEVEN_CREDENTIAL, WEBHOOK_CREDENTIAL]
    assert result["server"]["credentialId"] in result["credentialIds"]
    # A reused synthetic ID should appear once, never as duplicate allowlist data.
    env["VAPI_SERVER_CREDENTIAL_ID"] = CREDENTIAL
    result = payload(env, model_profile="gemini-3.8-flash-custom", voice_profile="eleven-v4-turbo", allow_experimental=True)
    assert result["credentialIds"] == [CREDENTIAL, ELEVEN_CREDENTIAL]


def test_custom_apply_requires_adapter_configuration():
    with pytest.raises(ValueError, match="adapter base"):
        select_profiles({}, model_profile="gemini-3.8-flash-custom", allow_experimental=True, apply=True)


def test_custom_url_rejects_credentials_and_private_hosts():
    for url in ("https://user:secret@adapter.example/llm", "https://127.0.0.1/llm", "http://adapter.example/llm"):
        with pytest.raises(ValueError):
            payload({"VAPI_CUSTOM_LLM_URL": url}, model_profile="gemini-3.8-flash-custom", allow_experimental=True)


def test_profile_selection_cannot_mutate_registry_or_tool_schemas():
    schemas = copy.deepcopy(TOOL_SCHEMAS)
    catalog = profile_catalog()
    catalog["model_profiles"]["luna"]["settings"]["reasoningEffort"] = "high"
    result = payload(model_profile="luna")
    result["model"]["tools"][0]["function"]["parameters"]["properties"].clear()
    assert TOOL_SCHEMAS == schemas
    assert payload(model_profile="luna")["model"]["reasoningEffort"] == "none"


def test_redaction_preserves_shape_and_removes_nested_credentials():
    result = provisioner.redact({"apiKey": "unknown", "session_token": "unknown", "model": {
        "tools": [{"function": {"name": "book", "description": "SYNTHETIC-KEY-LONG"}}],
        "text": "Bearer SYNTHETIC-KEY-LONG", "headers": {"Authorization": "unknown"},
    }}, ["SYNTHETIC-KEY", "SYNTHETIC-KEY-LONG"])
    encoded = json.dumps(result)
    assert "unknown" not in encoded and "SYNTHETIC" not in encoded and "-LONG" not in encoded
    assert result["model"]["tools"][0]["function"]["name"] == "book"


def test_offline_cli_reads_no_env_file_and_writes_redacted_profile_metadata(tmp_path, monkeypatch):
    import dotenv
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline mode must never read or write .env")
    monkeypatch.setattr(dotenv, "load_dotenv", forbidden)
    monkeypatch.setattr(dotenv, "set_key", forbidden)
    monkeypatch.setenv("GEMINI_API_KEY", "SYNTHETIC-ENV-SECRET")
    monkeypatch.setenv("VAPI_MODEL_PROFILE", "invented")
    path = tmp_path / "preview.json"
    assert provisioner.main(["--offline", "--model-profile", "luna", "--output", str(path)]) == 0
    assert json.loads(path.read_text())["model"]["model"] == "gpt-6-luna"
    manifest = json.loads(path.with_suffix(".meta.json").read_text())
    assert manifest["model_profile"] == "luna" and len(manifest["preview_sha256"]) == 64
    assert "SYNTHETIC-ENV-SECRET" not in path.read_text()


def test_offline_cannot_apply():
    with pytest.raises(SystemExit) as exc:
        provisioner.main(["--offline", "--apply"])
    assert exc.value.code == 2
