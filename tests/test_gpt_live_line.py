"""Isolated GPT-Live phone line: native tool policy, consent check and caps.

Disposable databases and synthetic credentials only; no provider requests.
"""
from datetime import date, datetime
import importlib
import json
from uuid import uuid4

import dotenv
from fastapi.testclient import TestClient
import pytest

from app import gpt_live, native_consent
from app.domain import TZ
from app.gemini_gateway import configured_tool_policy
from app.telephony_ingress import CAP_REFUSAL, IngressConfig
from app.tools import TOOL_SCHEMAS
from app.voice_profiles import select_profiles

ORG = "11111111-1111-4111-8111-111111111111"
PHONE = "22222222-2222-4222-8222-222222222222"
ASSISTANT = "33333333-3333-4333-8333-333333333333"
BROWSER = "44444444-4444-4444-8444-444444444444"
SERVER_CREDENTIAL = "55555555-5555-4555-8555-555555555555"
OPENAI_CREDENTIAL = "66666666-6666-4666-8666-666666666666"
PHONE_SECRET = "phone-only-credential-" + "a" * 32
PHONE_HEADERS = {"authorization": "Bearer " + PHONE_SECRET}
PATH = "/webhooks/vapi/inbound"
NATIVE = {"VAPI_MODEL_PROFILE": "gpt-live-1", "SERVICEVOICE_PHONE_NATIVE_TOOLS": "gpt-live-candidate",
          "SERVICEVOICE_ENVIRONMENT": "staging"}


def mapping(**updates):
    values = {"SERVICEVOICE_TELEPHONY_ENABLED": "on", "VAPI_PHONE_ORG_ID": ORG, "VAPI_PHONE_NUMBER_ID": PHONE,
              "VAPI_PHONE_ASSISTANT_ID": ASSISTANT, "VAPI_PHONE_WEBHOOK_SECRET": PHONE_SECRET,
              "VAPI_ASSISTANT_ID": BROWSER, "VAPI_MODEL_PROFILE": "gemini-3.8-flash-custom"}
    values.update(updates)
    return values


def test_default_phone_policy_stays_gateway_issued():
    assert IngressConfig.from_mapping(mapping()).tool_policy == "gemini-issued"


@pytest.mark.parametrize("updates", [
    {**NATIVE, "SERVICEVOICE_ENVIRONMENT": "production"},
    {**NATIVE, "SERVICEVOICE_ENVIRONMENT": ""},
    {k: v for k, v in NATIVE.items() if k != "SERVICEVOICE_ENVIRONMENT"},
    {**NATIVE, "VAPI_MODEL_PROFILE": "gemini-3.8-flash-custom"},
    {**NATIVE, "SERVICEVOICE_PHONE_NATIVE_TOOLS": "on"},
    {"VAPI_MODEL_PROFILE": "gpt-live-1", "SERVICEVOICE_ENVIRONMENT": "staging"},
    {**NATIVE, "SERVICEVOICE_PHONE_DAILY_ADMISSIONS": "0"},
    {**NATIVE, "SERVICEVOICE_PHONE_LIFETIME_ADMISSIONS": "many"},
])
def test_native_phone_tools_fail_closed(updates):
    with pytest.raises(ValueError):
        IngressConfig.from_mapping(mapping(**updates))


def test_native_phone_tools_only_for_isolated_gpt_live_line():
    config = IngressConfig.from_mapping(mapping(**NATIVE, SERVICEVOICE_PHONE_DAILY_ADMISSIONS="15",
                                                SERVICEVOICE_PHONE_LIFETIME_ADMISSIONS="60"))
    assert (config.tool_policy, config.daily_admissions, config.lifetime_admissions) == ("provider-native", 15, 60)


def test_profile_mapping_and_classic_path_refusal():
    assert configured_tool_policy({"VAPI_MODEL_PROFILE": "gpt-live-1"}) == "provider-native"
    assert configured_tool_policy({"VAPI_MODEL_PROFILE": "gemini-3.8-flash-custom"}) == "gemini-issued"
    with pytest.raises(ValueError, match="provision_gpt_live"):
        select_profiles({"VAPI_MODEL_PROFILE": "gpt-live-1"})


def test_assistant_payload_shape_and_guards():
    payload = gpt_live.assistant_payload(server_url="https://edge.example/gpt-live/webhooks/vapi/inbound",
                                         server_credential_id=SERVER_CREDENTIAL, openai_credential_id=OPENAI_CREDENTIAL,
                                         local_date=date(2026, 10, 25))
    model = payload["model"]
    assert (model["provider"], model["model"]) == ("openai", "gpt-live-1")
    assert model["reasoner"]["model"] == "gpt-5.6-terra" and model["speaker"]["personalityPacks"] == ["eager-listener"]
    names = [tool["function"]["name"] for tool in model["tools"] if tool["type"] == "function"]
    assert names == [schema["function"]["name"] for schema in TOOL_SCHEMAS]
    assert all(tool["async"] is False for tool in model["tools"] if tool["type"] == "function")
    assert {"type": "endCall"} in model["tools"]
    assert payload["credentialIds"] == [SERVER_CREDENTIAL, OPENAI_CREDENTIAL]
    assert payload["voice"] == {"provider": "openai", "voiceId": "gleam"}
    assert payload["maxDurationSeconds"] == 600 and payload["artifactPlan"]["recordingEnabled"] is True
    assert 'transcript[transcriptType="final"]' in payload["serverMessages"] and "tool-calls" in payload["serverMessages"]
    assert [hook["options"]["timeoutSeconds"] for hook in payload["hooks"]] == [8, 20, 45]
    reasoner = model["reasoner"]["instructions"]
    assert "{{" not in reasoner and "get_dealership_info" in reasoner and "<trusted_dealership_facts>" in reasoner
    assert '"today"' not in reasoner  # the reasoner fetches the date per call
    assert "callback number with its digits grouped" in reasoner  # every readback must let the server verify it
    assert "recorded" in model["speaker"]["instructions"] and "recorded" in payload["firstMessage"]
    with pytest.raises(ValueError):
        gpt_live.assistant_payload(server_url="https://edge.example/webhooks/vapi", server_credential_id=SERVER_CREDENTIAL)
    with pytest.raises(ValueError):
        gpt_live.assistant_payload(server_url="https://edge.example/gpt-live/webhooks/vapi/inbound",
                                   server_credential_id=SERVER_CREDENTIAL, voice_id="Kore")
    with pytest.raises(ValueError):
        gpt_live.assistant_payload(server_url="https://edge.example/gpt-live/webhooks/vapi/inbound",
                                   server_credential_id=SERVER_CREDENTIAL, name="Production assistant")


# --- consent check -----------------------------------------------------------

START = "2026-10-26T14:00:00+00:00"  # Monday 26 October, 9:00 AM Central
PROPOSAL = {"id": "proposal_1", "kind": "book", "expires": 1_800_000_300.0}
FACTS = {"start": START, "services": ["oil_change"], "customer": {"name": "Sam Lee", "phone": "+14055550123"}}
PREPARED = {"role": "tool_call_result", "toolCallId": "prep", "name": "prepare_booking",
            "result": json.dumps({"ok": True, "proposal_id": "proposal_1"}), "time": 1_000, "secondsFromStart": 1}
READBACK = "I have an oil change for Sam on Monday, October 26th at 9 a.m., callback number ending in 0 1 2 3. Shall I book it?"


def bot(text, start, end):
    return {"role": "bot", "message": text, "time": start, "endTime": end, "secondsFromStart": start / 1000}


def user(text, start, end=None):
    return {"role": "user", "message": text, "time": start, "endTime": end or start + 700, "secondsFromStart": start / 1000}


def check(*messages, facts=FACTS, proposal=PROPOSAL):
    return native_consent.assess({"messages": [PREPARED, *messages]}, proposal, facts, today=date(2026, 10, 25))


def test_verified_after_full_readback_and_later_yes():
    ok, evidence = check(bot(READBACK, 2_000, 9_000), user("Yes, please.", 9_400))
    assert ok, evidence
    assert evidence["anchor"] == "tool_result" and evidence["assent"] == "explicit_yes"
    assert all(evidence["facts"].values())


@pytest.mark.parametrize("readback", [
    "Booking your oil change, Sam Lee, Monday at nine in the morning; we'll call 405 555 0123. Sound good?",
    "That's an oil and filter service on October 26 at 9:00 AM for Sam, callback 4055550123. Go ahead?",
])
def test_paraphrased_readbacks_pass(readback):
    ok, evidence = check(bot(readback, 2_000, 9_000), user("Yeah that works.", 9_200))
    assert ok, evidence


@pytest.mark.parametrize("readback,missing", [
    ("I have an oil change for Sam on Monday at 9. Shall I book it?", ["callback_number"]),
    ("I have an oil change for Sam, callback ending 0123. Shall I book it?", ["day", "time"]),
    ("I have an oil change for Sam on Monday at 9 PM, callback ending 0123. Book it?", ["time"]),
    ("I have a brake inspection for Sam on Monday at 9, callback ending 0123. Book it?", ["services"]),
])
def test_incomplete_or_wrong_readback_is_refused(readback, missing):
    ok, evidence = check(bot(readback, 2_000, 9_000), user("Yes.", 9_400))
    assert not ok and evidence["missing_facts"] == missing
    assert "callback" in native_consent.refusal(evidence)["message"]


@pytest.mark.parametrize("answer,assent", [
    ("Mm-hmm.", "backchannel"),
    ("Yes, but make it Tuesday.", "negated_or_changed"),
    ("No, wait.", "negated_or_changed"),
    ("Is that the morning?", "question"),
    ("Thanks.", "not_assent"),
])
def test_answers_that_are_not_explicit_yes(answer, assent):
    ok, evidence = check(bot(READBACK, 2_000, 9_000), user(answer, 9_400))
    assert not ok and evidence["assent"] == assent


def test_yes_spoken_over_the_readback_is_refused():
    ok, evidence = check(bot(READBACK, 2_000, 9_000), user("Yes.", 4_000))
    assert not ok and evidence["assent"] == "during_readback"


def test_later_change_withdraws_an_earlier_yes_but_thanks_does_not():
    ok, evidence = check(bot(READBACK, 2_000, 9_000), user("Yes.", 9_400), bot("Great.", 10_000, 10_500),
                         user("Actually, wait.", 11_000))
    assert not ok and evidence["assent"] == "withdrawn_later"
    ok, evidence = check(bot(READBACK, 2_000, 9_000), user("Yes.", 9_400), bot("Booking it now.", 10_000, 11_000),
                         user("Thanks.", 11_500))
    assert ok, evidence


def test_missing_transcript_or_anchor_is_refused():
    assert native_consent.assess(None, PROPOSAL, FACTS)[1]["reasons"] == ["transcript_unavailable"]
    no_anchor = {"messages": [bot(READBACK, 2_000, 9_000), user("Yes.", 9_400)]}
    assert native_consent.assess(no_anchor, PROPOSAL, FACTS)[1]["reasons"] == ["readback_anchor_missing"]
    timed = dict(PROPOSAL, expires=(2_000 / 1000) + 300 - 1)  # created one second before the readback
    ok, evidence = native_consent.assess(no_anchor, timed, FACTS, today=date(2026, 10, 25))
    assert ok and evidence["anchor"] == "proposal_time"
    assert native_consent.assess({"messages": []}, PROPOSAL, None)[1]["reasons"] == ["proposal_facts_unavailable"]


def test_time_and_digit_parsing():
    start = datetime.fromisoformat(START).astimezone(TZ)
    assert native_consent.mentions_time("at nine o'clock", start)
    assert native_consent.mentions_time("9 in the morning", start)
    assert not native_consent.mentions_time("call 4 0 5 5 5 5 0 9 2 3", start)
    assert not native_consent.mentions_time("9 PM", start)
    assert native_consent.mentions_day("tomorrow", start, date(2026, 10, 25))


# --- phone path ----------------------------------------------------------------

@pytest.fixture
def native_app(tmp_path, monkeypatch):
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr(dotenv, "dotenv_values", lambda *a, **k: {})
    def make(**extra):
        for key, value in {**mapping(**NATIVE), "SERVICEVOICE_DB": str(tmp_path / "import.sqlite3"),
                           "VAPI_PUBLIC_KEY": "synthetic-public-key", "VAPI_WEBHOOK_SECRET": "browser-only-" + "b" * 32,
                           "VAPI_SERVER_CREDENTIAL_ID": "synthetic-server-credential",
                           "PUBLIC_BASE_URL": "https://provider.example.invalid",
                           "GEMINI_GATEWAY_SECRET": "gateway-only-" + "c" * 32, "GEMINI_API_KEY": "synthetic-google-key",
                           "SERVICEVOICE_JEV_FAQ": "off", "SERVICEVOICE_RECEIPT_NARRATION": "off", **extra}.items():
            monkeypatch.setenv(key, value)
        module = importlib.import_module("app.main")
        app = module.create_app(tmp_path / (str(uuid4()) + ".sqlite3"))
        return app, TestClient(module.create_public_app(app))
    return make


def admit(client):
    body = {"message": {"type": "assistant-request", "call": {"id": str(uuid4()), "type": "inboundPhoneCall",
                                                             "orgId": ORG, "phoneNumberId": PHONE}}}
    response = client.post(PATH, headers=PHONE_HEADERS, json=body)
    assert response.status_code == 200, response.text
    return {**body["message"]["call"], **response.json()}, response.json()


def run_tool(client, call, name, arguments, tool_id, artifact=None):
    message = {"type": "tool-calls", "call": call,
               "toolCallList": [{"id": tool_id, "function": {"name": name, "arguments": arguments}}]}
    if artifact is not None:
        message["artifact"] = artifact
    response = client.post(PATH, headers=PHONE_HEADERS, json={"message": message})
    assert response.status_code == 200, response.text
    return json.loads(response.json()["results"][0]["result"])


def test_native_phone_booking_requires_verified_readback(native_app):
    app, client = native_app()
    with client:
        call, admitted = admit(client)
        sid = admitted["assistantOverrides"]["variableValues"]["session_id"]
        assert app.state.gemini_gateway.store.call_policy(sid, call["id"]) == "provider-native"
        offers = run_tool(client, call, "begin_service_request", {"service": "oil_change"}, "search-1")
        slot = offers["slots"][0]
        prepared = run_tool(client, call, "prepare_booking", {"service": "oil_change", "slot_id": slot["id"],
                            "customer_name": "Sam Lee", "phone": "4055550123", "vehicle": "2019 Honda Civic"}, "prep-1")
        confirm = {"proposal_id": prepared["proposal_id"], "confirmed": True}
        refused = run_tool(client, call, "confirm_action", confirm, "confirm-1")
        assert refused["error"] == "confirmation_not_verified"
        with app.state.store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM appointments").fetchone()[0] == 0
        start = datetime.fromisoformat(slot["start"]).astimezone(TZ)
        readback = (f"I have an oil change for Sam on {start:%A}, {start:%B} {start.day}, at {start.hour % 12 or 12}"
                    f"{'' if start.minute == 0 else f':{start.minute:02d}'} {'AM' if start.hour < 12 else 'PM'}, "
                    "callback ending in 0123. Shall I book it?")
        artifact = {"messages": [
            {"role": "tool_call_result", "toolCallId": "prep-1", "name": "prepare_booking",
             "result": json.dumps(prepared), "time": 1_000, "secondsFromStart": 1},
            bot(readback, 2_000, 9_000), user("Yes, go ahead.", 9_300)]}
        booked = run_tool(client, call, "confirm_action", confirm, "confirm-2", artifact)
        assert booked.get("ok") is True and booked["appointment"]["reference"], booked
        with app.state.store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM appointments").fetchone()[0] == 1


def test_reviewer_uses_provider_ordinals_for_native_calls():
    from app.live_eval_evidence import build_evidence
    messages = [
        {"role": "bot", "message": "Hi, this is Cedar.", "secondsFromStart": 0.5},
        {"role": "user", "message": "What are your hours?", "secondsFromStart": 3},
        {"role": "tool_calls", "message": "", "secondsFromStart": 4,
         "toolCalls": [{"id": "t1", "type": "function", "function": {"name": "get_dealership_info", "arguments": "{}"}}]},
        {"role": "tool_call_result", "toolCallId": "t1", "name": "get_dealership_info", "result": "{}", "secondsFromStart": 4.2},
        {"role": "bot", "message": "We're open weekdays from 8 to 5.", "secondsFromStart": 5},
        {"role": "user", "message": "Thanks, bye.", "secondsFromStart": 8},
        {"role": "tool_calls", "message": "", "secondsFromStart": 9,
         "toolCalls": [{"id": "t2", "type": "function", "function": {"name": "endCall", "arguments": "{}"}}]},
    ]
    snapshot = {"session": {"id": "sess_1", "created": 1_793_000_000.0, "ended": 1}, "appointments": [], "proposals": [],
                "results": [{"call_id": "t1", "result": {"ok": True}}], "operations": [], "events": [], "handoffs": [],
                "signatures": [], "tool_policy": "provider-native"}
    call = {"artifact": {"messages": messages}, "endedReason": "assistant-ended-call"}
    package = build_evidence(call, snapshot)
    assert "unverified_native_platform_attempt" not in package["evidence_issues"]
    tools = [value for key, value in package["evidence"].items() if key.startswith("tool:")]
    assert tools[0]["issuance"] == "provider_native" and tools[0]["user_count"] == 1
    assert tools[1]["platform_evidence"]["provider_ended_by_assistant"] is True
    gateway = dict(snapshot, tool_policy="gemini-issued")
    assert "unverified_native_platform_attempt" in build_evidence(call, gateway)["evidence_issues"]


def test_lifetime_cap_refuses_politely_without_a_session(native_app):
    app, client = native_app(SERVICEVOICE_PHONE_LIFETIME_ADMISSIONS="1")
    with client:
        admit(client)
        _, second = admit(client)
        assert second == CAP_REFUSAL
        with app.state.store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM telephony_bindings").fetchone()[0] == 1
