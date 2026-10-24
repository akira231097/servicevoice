import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
from uuid import uuid4
import xml.etree.ElementTree as ET

import httpx
import pytest

from app.main import create_app, create_public_app
from app.twilio_bridge import BridgeError, signature
from app.twilio_api import TwilioLane
from app.telephony_ingress import IngressError

ORG = "11111111-1111-4111-8111-111111111111"
PHONE = "22222222-2222-4222-8222-222222222222"
ASSISTANT = "33333333-3333-4333-8333-333333333333"


@pytest.fixture
def lane_app(tmp_path, monkeypatch):
    for key, value in {
        "SERVICEVOICE_TWILIO_LANE": "on", "SERVICEVOICE_ENVIRONMENT": "staging",
        "TWILIO_ACCOUNT_SID": "AC"+"1"*32, "TWILIO_AUTH_TOKEN": "a"*32,
        "TWILIO_API_KEY_SID": "SK"+"2"*32, "TWILIO_API_KEY_SECRET": "b"*32,
        "TWILIO_PHONE_NUMBER": "+14155550123", "SERVICEVOICE_TWILIO_PUBLIC_BASE_URL": "https://lane.example.test/test",
        "SERVICEVOICE_TWILIO_SIP_URI": "sip:"+"c"*32+"@sip.vapi.ai", "SERVICEVOICE_TWILIO_BRIDGE_SECRET": "d"*32,
        "SERVICEVOICE_TELEPHONY_ENABLED": "on", "VAPI_PHONE_ORG_ID": ORG,
        "VAPI_PHONE_NUMBER_ID": PHONE, "VAPI_PHONE_ASSISTANT_ID": ASSISTANT,
        "VAPI_PHONE_WEBHOOK_SECRET": "e"*32, "SERVICEVOICE_TWILIO_SIP_HEADERS_PATH": "call.sip.headers",
        "SERVICEVOICE_LISTENING_CUES": "off", "SERVICEVOICE_LANGSMITH_ENABLED": "off",
    }.items(): monkeypatch.setenv(key, value)
    app = create_app(tmp_path/"business.sqlite3")
    return app


class Provider:
    def __init__(self): self.adds = 0; self.ends = []; self.gate = None
    async def add_vapi(self, row, target):
        self.adds += 1
        if self.gate: await self.gate.wait()
        return {"call_sid": "CA"+"9"*32}
    async def request(self, method, path, data=None):
        return {"status": "completed" if path in self.ends else "in-progress"}
    async def end_conference(self, sid): self.ends.append("/Conferences/"+sid+".json")
    async def end_call(self, sid, **kwargs): self.ends.append("/Calls/"+sid+".json")
    async def close(self): pass


def test_conference_waiting_guard_uses_only_admitted_speaker_state(lane_app, monkeypatch, tmp_path):
    assert lane_app.state.gemini_gateway.waiting_cue_state is None
    monkeypatch.setenv("SERVICEVOICE_LISTENING_CUES", "on")
    app = create_app(tmp_path/"guard.sqlite3")
    lane = app.state.twilio_lane
    gate = app.state.gemini_gateway.waiting_cue_state
    assert gate is not None
    assert gate("fixture", "session") == (None, "speaker_state_unavailable")
    lane.bindings["fixture"] = ("bridge", "session")
    lane.coordinator.register("fixture", "session")
    assert gate("fixture", "other") == (None, "speaker_state_unavailable")
    assert gate("fixture", "session")[1] == "assistant_speaking"
    lane.closing = True
    assert gate("fixture", "session") == (None, "speaker_state_unavailable")


async def post(client, lane, suffix, fields, **kwargs):
    body = urlencode(fields)
    url = lane.config.url(suffix)
    return await client.post("/twilio/"+suffix, content=body, headers={
        "content-type": "application/x-www-form-urlencoded", "x-twilio-signature": signature(lane.config.auth_token, url, fields.items())}, **kwargs)


async def wait_tasks(lane):
    pending = [task for task in lane.tasks if task not in lane.watchdogs.values()]
    if pending: await asyncio.gather(*pending)


def test_full_conference_lifecycle_and_duplicate_callbacks(lane_app):
    async def run():
        lane = lane_app.state.twilio_lane
        provider = lane.client = Provider()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_public_app(lane_app)), base_url="https://untrusted-host.test") as client:
            base = {"AccountSid": lane.config.account_sid, "CallSid": "CA"+"1"*32}
            response = await post(client, lane, "voice", {**base, "To": lane.config.phone_number, "Direction": "inbound"})
            assert response.status_code == 200
            name = ET.fromstring(response.text).find("Dial/Conference").text
            fields = {**base, "FriendlyName": name, "ConferenceSid": "CF"+"2"*32,
                "ParticipantLabel": "caller", "StatusCallbackEvent": "participant-join"}
            assert (await post(client, lane, "conference-status", fields)).status_code == 200
            assert (await post(client, lane, "conference-status", fields)).status_code == 200
            await wait_tasks(lane)
            assert provider.adds == 1
            bridge = lane.store.get(name[3:])
            fields.update(CallSid=bridge["vapi_leg_sid"], ParticipantLabel="vapi")
            await post(client, lane, "conference-status", fields)
            assert lane.store.get(bridge["id"])["state"] == "joined"
            fields.update(CallSid=bridge["caller_sid"], StatusCallbackEvent="participant-leave")
            await post(client, lane, "conference-status", fields)
            await wait_tasks(lane)
            row = lane.store.get(bridge["id"])
            assert row["state"] == "ended" and row["cleanup_verified"] == 1
            fields["StatusCallbackEvent"] = "participant-join"
            await post(client, lane, "conference-status", fields)
            assert lane.store.get(bridge["id"])["state"] == "ended"
            assert provider.adds == 1
        await lane.close()
    asyncio.run(run())


def test_hangup_while_create_participant_is_in_flight(lane_app):
    async def run():
        lane = lane_app.state.twilio_lane
        provider = lane.client = Provider()
        provider.gate = asyncio.Event()
        row = lane.store.allocate("CA"+"1"*32)
        lane.store.claim_dial(row["id"], "CF"+"2"*32, row["caller_sid"])
        task = asyncio.create_task(lane.dial(row["id"]))
        while provider.adds == 0: await asyncio.sleep(0)
        await lane.finish(row["id"])
        provider.gate.set()
        await task
        assert "/Calls/CA"+"9"*32+".json" in provider.ends
        assert lane.store.get(row["id"])["state"] == "ended"
        await lane.close()
    asyncio.run(run())


def test_unknown_create_result_closes_owned_conference_without_redial(lane_app):
    async def run():
        lane = lane_app.state.twilio_lane
        provider = lane.client = Provider()
        async def unknown(*args):
            provider.adds += 1
            raise BridgeError("twilio_unavailable")
        provider.add_vapi = unknown
        row = lane.store.allocate("CA"+"1"*32)
        lane.store.claim_dial(row["id"], "CF"+"2"*32, row["caller_sid"])
        await lane.dial(row["id"])
        assert "/Conferences/CF"+"2"*32+".json" in provider.ends
        assert provider.adds == 1 and lane.store.get(row["id"])["state"] == "ended"
        await lane.close()
    asyncio.run(run())


@pytest.mark.parametrize("fault,expected", [("wrong_account",403), ("wrong_number",403), ("wrong_direction",403),
    ("unsigned",401), ("body_limit",413), ("json",415), ("duplicate",400)])
def test_twilio_boundary_failures(lane_app, fault, expected):
    async def run():
        lane = lane_app.state.twilio_lane
        lane.client = Provider()
        fields = {"AccountSid": lane.config.account_sid, "CallSid": "CA"+"1"*32,
                  "To": lane.config.phone_number, "Direction": "inbound"}
        if fault == "wrong_account": fields["AccountSid"] = "AC"+"9"*32
        if fault == "wrong_number": fields["To"] = "+14155550124"
        if fault == "wrong_direction": fields["Direction"] = "outbound-api"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_public_app(lane_app)), base_url="https://test") as client:
            if fault == "unsigned": response = await client.post("/twilio/voice", data=fields)
            elif fault == "json": response = await client.post("/twilio/voice", json=fields)
            elif fault == "body_limit": response = await client.post("/twilio/voice", content="x="+"a"*17000, headers={"content-type":"application/x-www-form-urlencoded"})
            elif fault == "duplicate":
                pairs = list(fields.items())+[("To", fields["To"])]
                response = await client.post("/twilio/voice", content=urlencode(pairs), headers={"content-type":"application/x-www-form-urlencoded",
                    "x-twilio-signature": signature(lane.config.auth_token,lane.config.url("voice"),pairs)})
            else: response = await post(client,lane,"voice",fields)
            assert response.status_code == expected
            assert lane.store.active() == []
        await lane.close()
    asyncio.run(run())


def test_verified_sip_admission_and_partial_fast_path(lane_app):
    async def run():
        lane = lane_app.state.twilio_lane
        lane.client = Provider()
        row = lane.store.allocate("CA"+"1"*32)
        call = {"id": str(uuid4()), "type": "inboundPhoneCall", "orgId": ORG, "phoneNumberId": PHONE,
                "sip": {"headers": {"x-sv_bridge": row["id"], "x-sv_proof": lane.store.proof(lane.config, row)}}}
        auth = {"authorization": "Bearer "+"e"*32}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_public_app(lane_app)), base_url="https://test") as client:
            response = await client.post("/webhooks/vapi/inbound", headers=auth, json={"message":{"type":"assistant-request","call":call}})
            assert response.status_code == 200, response.text
            call.update(response.json())
            # Tool capacity is deliberately exhausted. Authenticated partials
            # still return without SQLite resolution or scheduler work.
            lane_app.state.telephony_http_capacity = 0
            response = await client.post("/webhooks/vapi/inbound", headers=auth, json={"message":{
                "type":"transcript", "transcriptType":"partial", "role":"user", "transcript":"private fixture", "call":call}})
            assert response.status_code == 200, response.text
            call["phoneNumberId"] = str(uuid4())
            response = await client.post("/webhooks/vapi/inbound", headers=auth, json={"message":{
                "type":"speech-update", "status":"started", "role":"user", "call":call}})
            assert response.status_code == 403
        await lane.close()
    asyncio.run(run())


def test_bridge_hangup_closes_tools_without_provider_end_webhook(lane_app):
    async def run():
        lane=lane_app.state.twilio_lane
        lane.client=Provider()
        row=lane.store.allocate("CA"+"1"*32)
        call={"id":str(uuid4()),"type":"inboundPhoneCall","orgId":ORG,"phoneNumberId":PHONE,
              "sip":{"headers":{"x-sv_bridge":row["id"],"x-sv_proof":lane.store.proof(lane.config,row)}}}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_public_app(lane_app)),base_url="https://test") as client:
            response=await client.post("/webhooks/vapi/inbound",headers={"authorization":"Bearer "+"e"*32},
                json={"message":{"type":"assistant-request","call":call}})
            assert response.status_code==200
            call.update(response.json())
            ingress=lane_app.state.telephony_ingress
            session_id=ingress.resolve(call)
            with pytest.raises(IngressError,match="telephony_binding_conflict"):
                ingress.end_bridge_binding(call["id"],"different-session")
            assert ingress.resolve(call)==session_id
            await lane.finish(row["id"])
            with pytest.raises(IngressError,match="telephony_call_ended"):
                ingress.resolve(call)
            assert ingress.resolve(call,allow_ended=True)==session_id
            await lane.finish(row["id"])
            assert lane.store.get(row["id"])["cleanup_verified"]==1
        await lane.close()
    asyncio.run(run())


def test_hangup_between_admission_and_bridge_binding_retires_session(lane_app,monkeypatch):
    async def run():
        lane=lane_app.state.twilio_lane
        lane.client=Provider()
        row=lane.store.allocate("CA"+"1"*32)
        call={"id":str(uuid4()),"type":"inboundPhoneCall","orgId":ORG,"phoneNumberId":PHONE,
              "sip":{"headers":{"x-sv_bridge":row["id"],"x-sv_proof":lane.store.proof(lane.config,row)}}}
        original=lane.store.bind_session
        def closed(*args):
            lane.store.end(row["id"])
            return original(*args)
        monkeypatch.setattr(lane.store,"bind_session",closed)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_public_app(lane_app)),base_url="https://test") as client:
            response=await client.post("/webhooks/vapi/inbound",headers={"authorization":"Bearer "+"e"*32},
                json={"message":{"type":"assistant-request","call":call}})
            assert response.status_code==409
        with lane_app.state.store.connect() as db:
            session=db.execute("SELECT ended FROM sessions WHERE provider_call_id=?",(call["id"],)).fetchone()
            binding=db.execute("SELECT ended FROM telephony_bindings WHERE provider_call_id=?",(call["id"],)).fetchone()
            assert session["ended"]==1 and binding["ended"] is not None
        await lane.close()
    asyncio.run(run())


def test_disabled_routes_and_no_extra_database(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVICEVOICE_TWILIO_LANE", "off")
    app = create_app(tmp_path/"business.sqlite3")
    assert app.state.twilio_lane is None
    assert not (tmp_path/"business-twilio.sqlite3").exists()
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_public_app(app)), base_url="https://test") as client:
            for path in ("voice", "conference-status", "participant-status", "dial-ended", "caller-status"):
                assert (await client.post("/twilio/"+path)).status_code == 404
            assert (await client.get("/twilio/clip/neutral_ack.wav")).status_code == 404
    asyncio.run(run())


def test_telephone_clip_wraps_exact_warmed_pcm_without_synthesis(lane_app, tmp_path, monkeypatch):
    import hashlib
    import io
    import wave
    from app.backchannel_audio import BackchannelAudioCache
    from test_backchannel_audio import tone
    import app.twilio_api as api
    async def run():
        cache = BackchannelAudioCache(tmp_path/"audio")
        asset = cache.store("neutral_ack", tone(seconds=.67))
        other = cache.store("acknowledge", tone(seconds=.9))
        path = cache.write_manifest([asset, other])
        cache.load_manifest(path)
        monkeypatch.setattr(api, "APPROVED_MANIFEST", cache.manifest_sha256)
        lane = lane_app.state.twilio_lane
        lane.config = replace(lane.config, listening=True)
        lane.client = Provider()
        lane_app.state.gemini_tts.backchannel_cache = cache
        await lane.start({"TYPESAFE_API_KEY":"fixture"})
        with wave.open(io.BytesIO(lane.wav), "rb") as audio:
            assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (8000,1,2)
            assert audio.readframes(audio.getnframes()) == cache.lookup_pcm("Mm-hmm.",8000)[0]
        assert hashlib.sha256(lane.wav).hexdigest() == lane.wav_sha
        await lane.close()
    asyncio.run(run())


def test_listening_clip_selection_is_signed_closed_set_and_requires_joined_caller(lane_app):
    async def run():
        lane=lane_app.state.twilio_lane;lane.client=Provider()
        row=lane.store.allocate("CA"+"1"*32)
        lane.store.claim_dial(row["id"],"CF"+"2"*32,row["caller_sid"])
        with lane.store.db() as db:db.execute("UPDATE twilio_bridges SET state='joined' WHERE id=?",(row["id"],))
        lane.wav=b"neutral";lane.wav_sha="a"*64
        lane.clips={"neutral_ack":(lane.wav,lane.wav_sha),"acknowledge":(b"attention","b"*64)}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_public_app(lane_app)),base_url="https://test") as client:
            suffix="clip/neutral_ack.wav?bridge="+row["id"]+"&cue=acknowledge"
            headers={"x-twilio-signature":signature(lane.config.auth_token,lane.config.url(suffix),[])}
            response=await client.get("/twilio/"+suffix,headers=headers)
            assert response.status_code==200 and response.content==b"attention"
            assert (await client.get("/twilio/"+suffix.replace("cue=acknowledge","cue=neutral_ack"),headers=headers)).status_code==401
            for query in ("cue=booked","cue=acknowledge&cue=neutral_ack"):
                suffix="clip/neutral_ack.wav?bridge="+row["id"]+"&"+query
                headers={"x-twilio-signature":signature(lane.config.auth_token,lane.config.url(suffix),[])}
                assert (await client.get("/twilio/"+suffix,headers=headers)).status_code==404
        await lane.close()
    asyncio.run(run())


def test_synthetic_caller_fixture_is_pinned_and_bounded(tmp_path):
    import hashlib
    import io
    import wave
    from app.twilio_api import load_test_fixture
    path=tmp_path/'fixture.wav'
    with wave.open(str(path),'wb') as audio:
        audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(8000)
        audio.writeframes(b'\x01\x02'*8000)
    expected=path.read_bytes()
    assert load_test_fixture(path,hashlib.sha256(expected).hexdigest())==expected
    with pytest.raises(ValueError):load_test_fixture(path,'0'*64)
    with pytest.raises(ValueError):load_test_fixture('relative.wav','0'*64)
