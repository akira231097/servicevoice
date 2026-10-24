import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import time
from urllib.parse import parse_qs
from uuid import uuid4
import xml.etree.ElementTree as ET

import httpx
import pytest

from app.twilio_bridge import (BridgeError, BridgeStore, TwilioClient, TwilioConfig,
    conference_twiml, direct_twiml, signature, verify_signature, proof_header_paths)


def config(**kwargs):
    return replace(TwilioConfig("AC"+"1"*32, "a"*32, "SK"+"2"*32, "b"*32,
        "+14155550123", "https://lane.example.test/test", "sip:"+"c"*32+"@sip.vapi.ai", "d"*32,
        sip_headers_path="call.sip.headers"), **kwargs)


def test_documented_signature_vector():
    fields = {"CallSid": "CA1234567890ABCDE", "Caller": "+14158675310", "Digits": "1234",
              "From": "+14158675310", "To": "+18005551212"}
    assert signature("12345", "https://example.com/myapp.php?foo=1&bar=2", fields.items()) == "L/OH5YylLD5NRKLltdqwSvS0BnU="


@pytest.mark.parametrize("mutate", ["url", "field", "extra", "signature", "token"])
def test_signature_tampering(mutate):
    cfg = config()
    url, pairs = cfg.url("voice"), [("To", cfg.phone_number), ("FutureField", " a ")]
    signed = signature(cfg.auth_token, url, pairs)
    if mutate == "url": url += "/"
    if mutate == "field": pairs[0] = ("To", "+14155550124")
    if mutate == "extra": pairs.append(("unknown", "1"))
    if mutate == "signature": signed = "wrong"
    if mutate == "token": cfg = replace(cfg, auth_token="z"*32)
    assert not verify_signature(cfg, url, pairs, signed)


def test_duplicate_value_signature_matches_provider_algorithm():
    assert signature("token", "https://a", [("a", "z"), ("a", "b"), ("a", "z")]) == signature("token", "https://a", [("a", "b"), ("a", "z")])


@pytest.mark.parametrize("kwargs", [dict(public_base="http://a"), dict(public_base="https://a/?x=1"),
    dict(public_base="https://x:y@a"), dict(sip_uri="sip:known@sip.vapi.ai"), dict(topology="other"),
    dict(jitter="large"), dict(auth_token=""), dict(phone_number="+4412345"), dict(api_key_sid="AC"+"a"*32)])
def test_invalid_enabled_configuration(kwargs):
    with pytest.raises(ValueError): config(**kwargs)


def test_disabled_requires_no_credentials_and_production_is_refused():
    assert TwilioConfig.from_mapping({}) is None
    with pytest.raises(ValueError, match="isolated"):
        TwilioConfig.from_mapping({"SERVICEVOICE_TWILIO_LANE": "on", "SERVICEVOICE_ENVIRONMENT": "production"})


def test_twiml_has_private_room_and_direct_baseline():
    cfg = config()
    conf = ET.fromstring(conference_twiml(cfg, "a"*32)).find("Dial/Conference")
    assert conf.text == "sv-"+"a"*32
    assert conf.attrib["maxParticipants"] == "2"
    assert conf.attrib["jitterBufferSize"] == "small"
    assert conf.attrib["endConferenceOnExit"] == "true"
    target = cfg.sip_uri+"?x-a=1&x-b=2"
    assert ET.fromstring(direct_twiml(cfg, "a"*32, target)).find("Dial/Sip").text == target


def proof_message(store, cfg, row, call_id=None):
    return {"call": {"id": call_id or str(uuid4()), "sip": {"headers": {
        "X-SV_Bridge": row["id"], "X-SV_Proof": store.proof(cfg, row)}}}}


def test_duplicate_join_waits_for_short_write_lock_and_dials_once(tmp_path):
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    row = store.allocate("CA" + "1" * 32)
    with ThreadPoolExecutor(max_workers=2) as pool:
        with store.db() as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            claims = [pool.submit(store.claim_dial, row["id"], "CF" + "2" * 32,
                                  row["caller_sid"]) for _ in range(2)]
            time.sleep(.2)
            assert all(not claim.done() for claim in claims)
        assert sorted(claim.result(timeout=3) for claim in claims) == [False, True]
    assert store.get(row["id"])["dial_pending"] == 1


def test_bridge_duplicate_admission_capacity_and_restart(tmp_path):
    store = BridgeStore(tmp_path/"bridges.db")
    first = store.allocate("CA"+"1"*32)
    assert store.allocate(first["caller_sid"]) == first
    store.allocate("CA"+"2"*32)
    store.allocate("CA"+"3"*32)
    store.allocate("CA"+"4"*32)
    store.end(first["id"])
    with pytest.raises(BridgeError, match="capacity"):
        BridgeStore(store.path).allocate("CA"+"5"*32)
    store.end(first["id"], verified=True)
    assert store.allocate("CA"+"5"*32)


def test_dial_claim_is_once_and_terminal_state_never_reopens(tmp_path):
    store = BridgeStore(tmp_path/"bridges.db")
    row = store.allocate("CA"+"1"*32)
    assert not store.claim_dial(row["id"], "CF"+"2"*32, "CA"+"3"*32)
    assert store.claim_dial(row["id"], "CF"+"2"*32, row["caller_sid"])
    assert not store.claim_dial(row["id"], "CF"+"2"*32, row["caller_sid"])
    store.end(row["id"])
    store.joined(row["id"])
    assert store.get(row["id"])["state"] == "ended"
    assert not store.claim_dial(row["id"], "CF"+"2"*32, row["caller_sid"])


def test_proof_retries_same_call_but_rejects_replay(tmp_path):
    now = [1000]
    store = BridgeStore(tmp_path/"bridges.db", clock=lambda: now[0])
    cfg, row = config(), store.allocate("CA"+"1"*32)
    message = proof_message(store, cfg, row)
    assert store.accept_proof(cfg, message) == row["id"]
    now[0] += 90
    assert store.accept_proof(cfg, message) == row["id"]
    message["call"]["id"] = str(uuid4())
    with pytest.raises(BridgeError, match="replayed"):
        store.accept_proof(cfg, message)


@pytest.mark.parametrize("fault", ["missing", "wrong", "expired", "ended", "schema", "duplicate", "wrong_call"])
def test_proof_failures(tmp_path, fault):
    now = [1000]
    store = BridgeStore(tmp_path/"bridges.db", clock=lambda: now[0])
    cfg, row = config(), store.allocate("CA"+"1"*32)
    message = proof_message(store, cfg, row)
    if fault == "missing": message["call"]["sip"]["headers"].pop("X-SV_Proof")
    if fault == "wrong": message["call"]["sip"]["headers"]["X-SV_Proof"] = "wrong"
    if fault == "expired": now[0] += 60
    if fault == "ended": store.end(row["id"])
    if fault == "schema": cfg = replace(cfg, sip_headers_path="")
    if fault == "duplicate": message["call"]["sip"]["headers"]["x-sv_bridge"] = row["id"]
    if fault == "wrong_call": message["call"]["id"] = "random"
    with pytest.raises(BridgeError): store.accept_proof(cfg, message)


def test_rest_shapes_and_no_retry(tmp_path):
    async def run():
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(201, json={"call_sid": "CA"+"2"*32})
        cfg = config()
        row = {"id": "a"*32, "conference_sid": "CF"+"1"*32, "caller_sid": "CA"+"1"*32}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            api = TwilioClient(cfg, client=client)
            await api.add_vapi(row, "sip:fixture@sip.vapi.ai")
            await api.announce(row)
            await api.announce(row,"acknowledge")
            with pytest.raises(BridgeError,match="cue_not_approved"):
                await api.announce(row,"booked")
        fields = parse_qs(requests[0].content.decode())
        assert fields["StatusCallbackEvent"] == ["initiated", "ringing", "answered", "completed"]
        assert fields["EndConferenceOnExit"] == ["true"]
        assert fields["JitterBufferSize"] == ["small"]
        assert requests[1].url.path.endswith(row["caller_sid"]+".json")
        assert "AnnounceUrl" in parse_qs(requests[1].content.decode())
        assert len(requests)==3
        assert parse_qs(requests[2].content.decode())["AnnounceUrl"][0].endswith("&cue=acknowledge")
        attempts = []
        def fail(request):
            attempts.append(request)
            raise httpx.ReadTimeout("private-error")
        async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
            with pytest.raises(BridgeError, match="twilio_unavailable"):
                await TwilioClient(cfg, client=client).announce(row)
        assert len(attempts) == 1
    asyncio.run(run())


def test_schema_discovery_exposes_only_proof_container_path():
    message = {"call":{"sip":{"headers":{"x-sv_bridge":"private-bridge", "x-sv_proof":"private-proof"}},
                       "customer":{"number":"private-phone"}}, "transcript":"private-words"}
    assert proof_header_paths(message) == ("call.sip.headers",)
    assert "private" not in repr(proof_header_paths(message))


def test_unresolved_dial_never_frees_test_capacity(tmp_path):
    store = BridgeStore(tmp_path/"bridges.db")
    row = store.allocate("CA"+"1"*32)
    store.claim_dial(row["id"], "CF"+"2"*32, row["caller_sid"])
    store.end(row["id"], verified=True)
    assert store.get(row["id"])["cleanup_verified"] == 0
    store.set_leg(row["id"], "CA"+"3"*32)
    store.end(row["id"], verified=True)
    assert store.get(row["id"])["cleanup_verified"] == 1


def test_explicit_pinned_variable_path_accepts_documented_prefix_mapping(tmp_path):
    store = BridgeStore(tmp_path/"bridges.db")
    cfg = config(sip_headers_path="call.assistantOverrides.variableValues")
    row = store.allocate("CA"+"1"*32)
    message = {"call":{"id":str(uuid4()), "assistantOverrides":{"variableValues":{
        "sv_bridge":row["id"], "sv_proof":store.proof(cfg,row)}}}}
    assert proof_header_paths(message) == (cfg.sip_headers_path,)
    assert store.accept_proof(cfg,message) == row["id"]
