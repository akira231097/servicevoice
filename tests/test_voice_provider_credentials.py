"""Registration plans protect provider keys and keep auth scopes separate."""
import json
from uuid import UUID

import httpx
import pytest

from scripts import configure_voice_providers as configure


def test_gemini_plan_shares_only_endpoint_auth_never_google_or_elevenlabs_keys():
    values={"GEMINI_GATEWAY_SECRET":"gateway-"+"g"*40,"GEMINI_TTS_BEARER_SECRET":"speech-"+"s"*40,
            "GEMINI_API_KEY":"PRIVATE-GOOGLE-KEY","ELEVENLABS_API_KEY":"PRIVATE-ELEVEN-KEY"}
    plans=configure.credential_plans(values,"gemini-3.8-flash-lite-tts")
    assert [(p[1],p[2]) for p in plans]==[("custom-llm","gateway"),("custom-credential","speech")]
    speech=plans[1][3]
    assert speech["authenticationPlan"]=={"type":"bearer","token":values["GEMINI_TTS_BEARER_SECRET"],
        "headerName":"Authorization","bearerPrefixEnabled":True}
    assert "PRIVATE" not in json.dumps(plans)


def test_read_only_plan_generates_and_persists_no_secrets():
    def forbidden(*args):raise AssertionError("read-only cannot persist")
    plans=configure.credential_plans({},"gemini-3.8-flash-lite-tts",save_secret=forbidden)
    assert plans[0][3]["apiKey"]=="" and plans[1][3]["authenticationPlan"]["token"]==""


def test_new_apply_credentials_receive_two_distinct_persisted_secrets():
    written={}
    plans=configure.credential_plans({},"gemini-3.8-flash-lite-tts",apply=True,save_secret=written.__setitem__)
    assert len(written)==2 and len(set(written.values()))==2
    assert all(len(value)>=32 for value in written.values())
    assert plans[1][3]["authenticationPlan"]["token"]==written["GEMINI_TTS_BEARER_SECRET"]


@pytest.mark.parametrize("values", [
    {"VAPI_CUSTOM_LLM_CREDENTIAL_ID":"b3e52f63-ef71-40c5-97d8-adf8a472b0e2"},
    {"GEMINI_GATEWAY_SECRET":"g"*40,"VAPI_CUSTOM_TTS_CREDENTIAL_ID":"b3e52f63-ef71-40c5-97d8-adf8a472b0e2"},
    {"GEMINI_GATEWAY_SECRET":"short"},
    {"GEMINI_GATEWAY_SECRET":"g"*40,"GEMINI_TTS_BEARER_SECRET":"g"*40}])
def test_bad_or_missing_endpoint_secrets_do_not_silently_rotate_existing_credentials(values):
    with pytest.raises(ValueError):
        configure.credential_plans(values,"gemini-3.8-flash-lite-tts",apply=True,save_secret=lambda *args:None)


def test_default_gemini_read_only_main_needs_no_elevenlabs_key_or_external_request(tmp_path,monkeypatch,capsys):
    monkeypatch.setattr(configure,"dotenv_values",lambda path:{"VAPI_API_KEY":"vapi-test","GEMINI_API_KEY":"google-test","PUBLIC_BASE_URL":"https://voice.example"})
    def forbidden(*args,**kwargs):raise AssertionError("no missing credential should be fetched or created")
    real_client=httpx.Client
    monkeypatch.setattr(configure.httpx,"Client",lambda **kwargs:real_client(**kwargs,transport=httpx.MockTransport(forbidden)))
    assert configure.main(["--env-file",str(tmp_path/"private.env")])==0
    output=capsys.readouterr().out
    assert '"provider": "custom-credential"' in output and "google-test" not in output and "vapi-test" not in output


def test_apply_never_automatically_retries_ambiguous_credential_create(tmp_path,monkeypatch,capsys):
    values={"VAPI_API_KEY":"vapi-test","GEMINI_API_KEY":"google-test","PUBLIC_BASE_URL":"https://voice.example",
        "GEMINI_GATEWAY_SECRET":"g"*40,"GEMINI_TTS_BEARER_SECRET":"s"*40}
    monkeypatch.setattr(configure,"dotenv_values",lambda path:values)
    monkeypatch.setattr(configure,"set_key",lambda *args,**kwargs:None)
    calls=[]
    def lost_response(request):
        calls.append(request)
        raise httpx.ReadTimeout("PRIVATE-CREDENTIAL",request=request)
    real_client=httpx.Client
    monkeypatch.setattr(configure.httpx,"Client",lambda **kwargs:real_client(**kwargs,transport=httpx.MockTransport(lost_response)))
    assert configure.main(["--apply","--env-file",str(tmp_path/"private.env")])==1
    assert len(calls)==1 and "PRIVATE-CREDENTIAL" not in capsys.readouterr().out
