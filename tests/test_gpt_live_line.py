"""Isolated GPT-Live phone line: native tool policy, consent check and caps.

Disposable databases and synthetic credentials only; no provider requests.
"""
from datetime import date, datetime
import hashlib
import hmac
import importlib
import json
from pathlib import Path
import time
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
    assert model["reasoner"]["reasoningEffort"] == "none"  # "low" measured about 1.5 s slower per lookup
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
    # Vapi fills today's dealership-local date when the call starts; the lookup stays as the fallback.
    assert 'The current dealership-local date is {{"now" | date: "%A, %Y-%m-%d", "America/Chicago"}}' in reasoner
    assert "{{current_date}}" not in reasoner and "{{timezone}}" not in reasoner
    assert "If missing or unresolved, use get_dealership_info" in reasoner and "<trusted_dealership_facts>" in reasoner
    assert '"today"' not in reasoner  # a date saved with the assistant would go stale
    assert "callback number with its digits grouped" in reasoner  # every readback must let the server verify it
    assert "caller_number" in reasoner and "join the letters" in reasoner
    assert "spell it in the booking readback" in reasoner and "spelled letter by letter" in model["speaker"]["instructions"]
    assert "caller_id_guidance" in reasoner and "a name from caller ID" in model["speaker"]["instructions"]
    speaker = model["speaker"]["instructions"]
    for section in ("Facts you can share without checking:", "Backchannel policy:", "Interruption policy:",
                    "Do not delegate to the backend when:", "While work is running:", "Names and numbers:", "Results:"):
        assert section in speaker
    assert "thanks you" in speaker  # pleasantries are answered without a reasoner round trip
    luna = gpt_live.assistant_payload(server_url="https://edge.example/gpt-live/webhooks/vapi/inbound",
                                      server_credential_id=SERVER_CREDENTIAL, reasoner_model="gpt-5.6-luna",
                                      reasoning_effort="none", local_date=date(2026, 10, 25))
    assert luna["model"]["reasoner"]["model"] == "gpt-5.6-luna" and luna["model"]["reasoner"]["reasoningEffort"] == "none"
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
    # Live call 2026-10-25: natural speech says "your" instead of the caller's name.
    "I have a proposed oil and filter service for your 2019 Honda Civic on Monday, October 26 at 9 a.m. "
    "Central Daylight Time. Your callback number is four zero five, five five five, zero one two three. "
    "Would you like me to go ahead?",
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


def admit(client, caller=None):
    body = {"message": {"type": "assistant-request", "call": {"id": str(uuid4()), "type": "inboundPhoneCall",
                                                             "orgId": ORG, "phoneNumberId": PHONE,
                                                             **({"customer": {"number": caller}} if caller else {})}}}
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
        {"role": "tool_calls", "message": "", "secondsFromStart": 3.5,
         "toolCalls": [{"id": "d1", "type": "function", "function": {"name": "delegate", "arguments": "{}"}}]},
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
    assert package["evidence_issues"] == []
    tools = [value for key, value in package["evidence"].items() if key.startswith("tool:")]
    assert tools[0]["name"] == "delegate" and tools[0]["evidence_scope"] == "provider_delegation"
    assert tools[1]["issuance"] == "provider_native" and tools[1]["user_count"] == 1
    assert tools[2]["platform_evidence"]["provider_ended_by_assistant"] is True
    gateway = dict(snapshot, tool_policy="gemini-issued")
    assert "unverified_native_platform_attempt" in build_evidence(call, gateway)["evidence_issues"]


def test_native_line_offers_the_calling_number_only_through_session_context(native_app):
    app, client = native_app()
    with client:
        call, _ = admit(client, caller="+14055550199")
        context = run_tool(client, call, "get_session_context", {}, "context-1")
        assert context["caller_number"] == "+14055550199" and "not identity verification" in context["caller_number_note"]
        unknown, _ = admit(client, caller="anonymous")
        assert "caller_number" not in run_tool(client, unknown, "get_session_context", {}, "context-2")


def test_gateway_issued_phone_line_never_records_the_calling_number(native_app):
    app, client = native_app(SERVICEVOICE_PHONE_NATIVE_TOOLS="", VAPI_MODEL_PROFILE="gemini-3.8-flash-custom",
                             SERVICEVOICE_ENVIRONMENT="production")
    with client:
        _, admitted = admit(client, caller="+14055550199")
        sid = admitted["assistantOverrides"]["variableValues"]["session_id"]
        with app.state.store.connect() as db:
            state = json.loads(db.execute("SELECT state FROM sessions WHERE id=?", (sid,)).fetchone()[0])
        assert "caller_number" not in state


def test_lifetime_cap_refuses_politely_without_a_session(native_app):
    app, client = native_app(SERVICEVOICE_PHONE_LIFETIME_ADMISSIONS="1")
    with client:
        admit(client)
        _, second = admit(client)
        assert second == CAP_REFUSAL
        with app.state.store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM telephony_bindings").fetchone()[0] == 1

# --- caller-ID name step (the Twilio lane's caller_context, unchanged) ----------

CALLER_KEY = "caller-context-test-key-" + "k" * 24
TEST_LINE = "+14055550177"
OWNER = "+12175550142"
ROOT = Path(__file__).resolve().parents[1]


def caller_policy(tmp_path, mode="personalize", entries=((TEST_LINE, "owned_test", "Jordan Rivera"),)):
    allow = []
    for number, label, fixture in entries:
        entry = {"digest": hmac.new(CALLER_KEY.encode(), number.encode(), hashlib.sha256).hexdigest(), "label": label}
        if fixture:
            entry["fixture_name"] = fixture
        allow.append(entry)
    path = tmp_path / "caller-policy.json"
    path.write_text(json.dumps({"schema": "servicevoice-caller-context-v1", "mode": mode, "allow": allow}))
    return {"SERVICEVOICE_TWILIO_CALLER_CONTEXT_POLICY": str(path), "SERVICEVOICE_TWILIO_CALLER_CONTEXT_KEY": CALLER_KEY}


def test_caller_id_name_rides_on_tool_results_and_is_never_stored(native_app, tmp_path):
    from app.diagnostics import read_review_diagnostics
    from app.live_eval_evidence import caller_id_offer
    app, client = native_app(**caller_policy(tmp_path))
    with client:
        call, _ = admit(client, caller=TEST_LINE)
        context = run_tool(client, call, "get_session_context", {}, "context-1", {"messages": []})
        guidance = context["caller_id_guidance"]
        assert "\"Should I put this under Jordan Rivera, at the number you're calling from?\"" in guidance
        assert "405-555-0177" in guidance and "not identity verification" in guidance
        assert context["caller_number"] == TEST_LINE
        # Every result carries it, so the backend needs no extra tool round trip.
        offers = run_tool(client, call, "begin_service_request", {"service": "oil_change"}, "search-1", {"messages": []})
        assert "Jordan Rivera" in offers["caller_id_guidance"]
        with app.state.store.connect() as db:
            stored = [row[0] for row in db.execute("SELECT result FROM tool_results")]
        assert stored and not any("Jordan" in row or "caller_id_guidance" in row for row in stored)
    store = Path(app.state.store.path)
    for path in store.parent.glob(store.name + "*"):
        assert b"Jordan" not in path.read_bytes(), path.name  # the booking database never holds the name
    diagnostics = read_review_diagnostics(store, call["id"])
    assert caller_id_offer(diagnostics) == "name_and_number"
    codes = [(row["event"], row["attributes"]) for row in diagnostics["events"] if row["event"] in ("caller_lookup", "caller_hint")]
    assert ("caller_lookup", {"caller_lookup_outcome": "fixture", "caller_context_mode": "personalize",
                              "caller_verstat": "absent"}) in codes
    raw = json.dumps(diagnostics)
    assert "Jordan" not in raw and "405-555-0177" not in raw and TEST_LINE not in raw


def test_caller_id_guidance_follows_what_was_actually_said(native_app, tmp_path):
    app, client = native_app(**caller_policy(tmp_path))
    with client:
        call, _ = admit(client, caller=TEST_LINE)
        asked = [bot("Should I put this under Jordan Rivera, at the number you're calling from?", 1_000, 4_000),
                 user("No, it's for my wife, Dana Lee.", 4_500)]
        guidance = run_tool(client, call, "get_session_context", {}, "context-2", {"messages": asked})["caller_id_guidance"]
        assert "already asked about the caller ID name" in guidance and "Jordan" not in guidance
        prepared = [*asked, {"role": "tool_calls", "message": "", "toolCalls": [
            {"id": "prep-9", "type": "function", "function": {"name": "prepare_booking", "arguments": "{}"}}]}]
        reminder = run_tool(client, call, "get_dealership_info", {}, "info-1", {"messages": prepared})["caller_id_guidance"]
        assert reminder.startswith("If you use a confirmed name, say it in full exactly as confirmed")


def test_a_number_already_offered_never_reopens_the_name_step(native_app, tmp_path):
    app, client = native_app(**caller_policy(tmp_path))
    with client:
        call, _ = admit(client, caller=TEST_LINE)
        offered = [bot("May I have your name? I can use the number you're calling from.", 1_000, 4_000)]
        guidance = run_tool(client, call, "get_session_context", {}, "context-6", {"messages": offered})["caller_id_guidance"]
        assert "already offered the number they are calling from" in guidance and "Jordan" not in guidance


@pytest.mark.parametrize("mode,caller", [("personalize", OWNER), ("off", TEST_LINE), ("shadow", TEST_LINE)])
def test_no_caller_id_guidance_unless_allowlisted_and_personalizing(native_app, tmp_path, mode, caller):
    app, client = native_app(**caller_policy(tmp_path, mode=mode))
    with client:
        call, _ = admit(client, caller=caller)
        context = run_tool(client, call, "get_session_context", {}, "context-3", {"messages": []})
        assert "caller_id_guidance" not in context and context["caller_number"] == caller


def test_caller_name_lookup_never_delays_the_call_and_ends_with_it(native_app, tmp_path, monkeypatch):
    import asyncio
    from app import main as main_module
    from app.caller_context import LookupResult
    gate = {}

    class HeldLookup:
        def __init__(self, *args, **kwargs):
            pass

        async def lookup(self, number, seconds):
            gate["loop"], gate["event"] = asyncio.get_running_loop(), asyncio.Event()
            await gate["event"].wait()
            return LookupResult("found", "Pat Morgan")

        async def close(self):
            pass

    monkeypatch.setattr(main_module, "CallerNameLookup", HeldLookup)
    app, client = native_app(**caller_policy(tmp_path, entries=((OWNER, "owner", None),)))
    with client:
        started = time.monotonic()
        call, _ = admit(client, caller=OWNER)
        assert time.monotonic() - started < 2  # admission never waits for the lookup
        assert "caller_id_guidance" not in run_tool(client, call, "get_session_context", {}, "context-4", {"messages": []})
        gate["loop"].call_soon_threadsafe(gate["event"].set)
        deadline = time.monotonic() + 5
        while app.state.caller_context.calls[call["id"]].outcome == "pending" and time.monotonic() < deadline:
            time.sleep(0.02)
        guidance = run_tool(client, call, "get_session_context", {}, "context-5", {"messages": []})["caller_id_guidance"]
        assert "Should I put this under Pat Morgan" in guidance
        held, _ = admit(client, caller=OWNER)  # a second call whose lookup never finishes
        for ended in (call, held):
            response = client.post(PATH, headers=PHONE_HEADERS, json={"message": {"type": "end-of-call-report", "call": ended}})
            assert response.status_code == 200, response.text
        assert app.state.caller_context.calls == {}


def test_reviewer_learns_caller_id_reached_the_assistant_without_values():
    from app.live_eval_evidence import build_evidence, caller_id_offer
    def rows(*states, component="webhook"):
        return {"events": [{"component": component, "event": "caller_hint", "attributes": {"caller_hint_state": s}}
                           for s in states]}
    assert caller_id_offer(rows("number_only")) == caller_id_offer(rows("number_offered")) == "number_only"
    assert caller_id_offer(rows("details_saved", "name_asked")) == "name_and_number"
    assert caller_id_offer(rows("name_and_number", component="twilio_bridge")) == "name_and_number"
    assert caller_id_offer(rows("number_only", component="gateway")) is None and caller_id_offer(None) is None
    call = {"artifact": {"messages": [{"role": "bot", "message": "Hi, this is Cedar.", "secondsFromStart": 0.5},
                                      {"role": "user", "message": "Hours?", "secondsFromStart": 2}]}}
    snapshot = {"session": {"id": "sess_1", "created": 1_793_000_000.0, "ended": 1}, "appointments": [], "proposals": [],
                "results": [], "operations": [], "events": [], "handoffs": [], "signatures": [],
                "tool_policy": "provider-native", "caller_id": "name_and_number"}
    item = build_evidence(call, snapshot)["evidence"]["caller_id"]
    assert item["available_to_assistant"] == "name_and_number"
    assert "does not contain the caller-ID values" in item["provenance"]
    assert "caller_id" not in build_evidence(call, dict(snapshot, caller_id=None))["evidence"]


def test_caller_context_settings_are_checked_and_only_used_on_the_gpt_live_line(native_app, tmp_path):
    with pytest.raises(ValueError):
        native_app(SERVICEVOICE_TWILIO_CALLER_CONTEXT_POLICY="relative/policy.json",
                   SERVICEVOICE_TWILIO_CALLER_CONTEXT_KEY=CALLER_KEY)
    app, _ = native_app(SERVICEVOICE_PHONE_NATIVE_TOOLS="", VAPI_MODEL_PROFILE="gemini-3.8-flash-custom",
                        SERVICEVOICE_ENVIRONMENT="production", **caller_policy(tmp_path))
    assert app.state.caller_source is None


def test_compose_reviewer_health_check_fits_its_cpu_cap_and_caller_policy_is_read_only():
    text = (ROOT / "deploy" / "lightsail" / "compose.gpt-live.yaml").read_text()
    business, reviewer = text.split("  gpt-live-reviewer:")
    assert "cpus: 0.15" in reviewer and "timeout: 30s" in reviewer and "interval: 60s" in reviewer
    assert ":/run/caller-context:ro" in business
