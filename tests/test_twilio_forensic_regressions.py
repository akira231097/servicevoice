"""Replay the independent review's failures with corrected expectations."""
import asyncio
from dataclasses import replace
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest

from app.main import create_app, create_public_app
from app.twilio_bridge import BridgeError, TwilioClient
from test_twilio_api import lane_app, ORG, PHONE, ASSISTANT, Provider

CALLER, LEG, CONF = "CA"+"1"*32, "CA"+"9"*32, "CF"+"2"*32


class FakeTwilio:
    def __init__(self):
        self.log, self.calls, self.conferences = [], {}, {}
        self.gate = None

    async def handler(self, request):
        tail = request.url.path.split("/Accounts/", 1)[1].split("/", 1)[1]
        body = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.log.append((request.method, tail, body))
        if request.method == "GET" and self.gate is not None:
            await self.gate.wait()
        parts = tail.removesuffix(".json").split("/")
        if parts[0] == "Conferences" and len(parts) == 4:
            return httpx.Response(404, json={"code": 20404})
        if parts[0] == "Conferences" and len(parts) == 3:
            if self.conferences.get(parts[1]) == "completed":
                return httpx.Response(404, json={"code": 20404})
            self.calls[LEG] = "queued"
            return httpx.Response(201, json={"call_sid": LEG})
        if parts[0] == "Conferences" and len(parts) == 2:
            if request.method == "POST": self.conferences[parts[1]] = "completed"
            return httpx.Response(200, json={"status": self.conferences.get(parts[1], "in-progress")})
        if parts[0] == "Calls" and len(parts) == 2:
            if request.method == "POST" and body.get("Status") == "completed":
                self.calls[parts[1]] = "completed"
            # Updating TwiML starts the apology; it does NOT end the call.
            return httpx.Response(200, json={"status": self.calls.get(parts[1], "in-progress")})
        return httpx.Response(200, json={"calls": [], "next_page_uri": None})


def attach(lane, fake):
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    lane.client = TwilioClient(lane.config, client=client)
    return client


def test_apology_owns_call_until_twiml_finishes(lane_app):
    async def run():
        lane = lane_app.state.twilio_lane
        fake = FakeTwilio()
        async with attach(lane, fake):
            row = lane.store.allocate(CALLER)
            lane.store.claim_dial(row["id"], CONF, CALLER)
            lane.store.set_leg(row["id"], LEG)
            await lane.finish(row["id"], apology=True)
            await lane.finish(row["id"])
            posts = [b for m, p, b in fake.log if m == "POST" and p == f"Calls/{CALLER}.json"]
            assert len(posts) == 1 and "Twiml" in posts[0]
            assert lane.store.get(row["id"])["cleanup_verified"] == 0
            fake.calls[CALLER] = "completed"
            await lane.finish(row["id"])
            assert lane.store.get(row["id"])["cleanup_verified"] == 1
            await lane.close()
    asyncio.run(run())


def test_two_definite_dial_failures_do_not_lock_line(lane_app):
    async def run():
        lane = lane_app.state.twilio_lane
        fake = FakeTwilio()
        async with attach(lane, fake):
            for index in range(2):
                caller, conf = "CA"+str(index+1)*32, "CF"+str(index+3)*32
                row = lane.store.allocate(caller)
                lane.store.claim_dial(row["id"], conf, caller)
                fake.conferences[conf] = "completed"
                await lane.dial(row["id"])
                assert lane.store.get(row["id"])["dial_pending"] == 0
                fake.calls[caller] = "completed"
                await lane.finish(row["id"])
            assert lane.store.active() == []
            assert lane.store.allocate("CA"+"7"*32)
            await lane.close()
    asyncio.run(run())


def test_cleanup_requests_serialize_instead_of_dropping_late_dial(lane_app):
    async def run():
        lane = lane_app.state.twilio_lane
        fake = FakeTwilio()
        fake.gate = asyncio.Event()
        async with attach(lane, fake):
            row = lane.store.allocate(CALLER)
            lane.store.claim_dial(row["id"], CONF, CALLER)
            first = asyncio.create_task(lane.finish(row["id"]))
            while not any(m == "GET" for m, _, _ in fake.log): await asyncio.sleep(0)
            late = asyncio.create_task(lane.dial(row["id"]))
            while lane.store.get(row["id"])["dial_pending"]: await asyncio.sleep(0)
            assert not first.done()
            fake.gate.set()
            await asyncio.gather(first, late)
            assert lane.store.get(row["id"])["cleanup_verified"] == 1
            await lane.close()
    asyncio.run(run())


def test_full_listening_registry_keeps_phone_call_available(lane_app):
    async def run():
        lane = lane_app.state.twilio_lane
        lane.config = replace(lane.config, listening=True)
        lane.client = Provider()
        lane.coordinator.register("old-1", "s1")
        lane.coordinator.register("old-2", "s2")
        row = lane.store.allocate(CALLER)
        call = {"id": str(uuid4()), "type": "inboundPhoneCall", "orgId": ORG, "phoneNumberId": PHONE,
            "sip": {"headers": {"x-sv_bridge": row["id"], "x-sv_proof": lane.store.proof(lane.config, row)}}}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_public_app(lane_app)), base_url="https://test") as client:
            response = await client.post("/webhooks/vapi/inbound", headers={"authorization": "Bearer "+"e"*32},
                json={"message": {"type": "assistant-request", "call": call}})
            assert response.status_code == 200
            call.update(response.json())
            assert lane_app.state.telephony_ingress.resolve(call)
            assert call["id"] not in lane.coordinator.calls
        await lane.close()
    asyncio.run(run())


def test_announcement_deadline_is_ambiguous_and_never_retried(lane_app):
    async def run():
        calls = []
        async def slow(request):
            calls.append(request)
            await asyncio.sleep(2)
            return httpx.Response(200, json={})
        lane = lane_app.state.twilio_lane
        async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as http:
            provider = TwilioClient(lane.config, client=http)
            with pytest.raises(BridgeError, match="announcement_outcome_unknown") as error:
                await provider.announce({"id": "fixture", "conference_sid": CONF, "caller_sid": CALLER})
            assert error.value.definitive is False and len(calls) == 1
    asyncio.run(run())


def test_additional_phone_scope_preserves_primary_admission_and_waiting(lane_app, monkeypatch, tmp_path):
    isolated_phone, isolated_assistant = str(uuid4()), str(uuid4())
    for key, value in {"VAPI_TWILIO_ORG_ID": ORG, "VAPI_TWILIO_PHONE_NUMBER_ID": isolated_phone,
            "VAPI_TWILIO_ASSISTANT_ID": isolated_assistant, "VAPI_TWILIO_WEBHOOK_SECRET": "z"*32,
            "SERVICEVOICE_LISTENING_CUES": "on", "VAPI_BACKCHANNEL_ASSISTANT_ID": ASSISTANT}.items():
        monkeypatch.setenv(key, value)
    app = create_app(tmp_path/"cohost.sqlite3")
    async def run():
        lane = app.state.twilio_lane
        lane.client = Provider()
        lane.closing = True
        call = {"id": str(uuid4()), "type": "inboundPhoneCall", "orgId": ORG, "phoneNumberId": PHONE}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_public_app(app)), base_url="https://test") as client:
            response = await client.post("/webhooks/vapi/inbound", headers={"authorization": "Bearer "+"e"*32},
                json={"message": {"type": "assistant-request", "call": call}})
            assert response.status_code == 200 and response.json()["assistantId"] == ASSISTANT
            call.update(response.json())
            assert app.state.telephony_ingress.resolve(call)
            assert call["id"] not in lane.bindings
            from types import SimpleNamespace
            assert not app.state.gemini_gateway._waiting_applies(SimpleNamespace(diagnostic_context={"assistant_id": ASSISTANT}))
            assert app.state.gemini_gateway._waiting_applies(SimpleNamespace(diagnostic_context={"assistant_id": isolated_assistant}))
            # The primary credential cannot bypass the isolated proof/secret.
            wrong = {**call, "id": str(uuid4()), "phoneNumberId": isolated_phone, "assistantId": isolated_assistant}
            denied = await client.post("/webhooks/vapi/inbound", headers={"authorization": "Bearer "+"e"*32},
                json={"message": {"type": "assistant-request", "call": wrong}})
            assert denied.status_code == 401
        await lane.close()
    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["redial", "caller_left", "words_resumed", "detach_failed"])
def test_opening_recovery_is_once_and_never_replays_business_work(lane_app, outcome):
    async def run():
        lane = lane_app.state.twilio_lane
        class RecoveryProvider(Provider):
            async def recovery_announcement(self, row, ticket):
                if outcome == "caller_left": lane.caller_left.add(row["id"])
                if outcome == "words_resumed": lane.media[row["id"]].words = True
            async def detach_agent(self, row):
                if outcome == "detach_failed": raise BridgeError("twilio_unavailable")
                self.ends.append("/Calls/"+row["vapi_leg_sid"]+".json")
            async def add_vapi(self, row, target):
                self.adds += 1
                return {"call_sid": "CA"+"8"*32}
        provider = lane.client = RecoveryProvider()
        row = lane.store.allocate(CALLER)
        lane.store.claim_dial(row["id"], CONF, CALLER); lane.store.set_leg(row["id"], LEG); lane.store.joined(row["id"])
        call = {"id": str(uuid4()), "type": "inboundPhoneCall", "orgId": ORG, "phoneNumberId": PHONE,
            "sip": {"headers": {"x-sv_bridge": row["id"], "x-sv_proof": lane.store.proof(lane.config, row)}}}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_public_app(lane_app)), base_url="https://test") as client:
            response = await client.post("/webhooks/vapi/inbound", headers={"authorization": "Bearer "+"e"*32},
                json={"message": {"type": "assistant-request", "call": call}})
            assert response.status_code == 200
            old_session = response.json()["assistantOverrides"]["variableValues"]["session_id"]
        media = lane.media[row["id"]]
        media.greeting = lane.coordinator.clock()-9
        media.caller_bursts = 2; media.vapi_user_bursts = 1
        await lane.recover_opening(row["id"])
        from test_twilio_api import wait_tasks
        await wait_tasks(lane)
        fresh = lane.store.get(row["id"])
        assert fresh["recovery_count"] == 1
        if outcome == "redial":
            assert provider.adds == 1 and fresh["vapi_leg_sid"] == "CA"+"8"*32
            assert lane.store.retired_leg(row["id"], LEG)
            assert call["id"] not in lane.bindings
            with lane_app.state.store.connect() as db:
                assert db.execute("SELECT ended FROM sessions WHERE id=?", (old_session,)).fetchone()[0] == 1
        elif outcome == "words_resumed":
            assert fresh["state"] == "joined" and provider.adds == 0
            assert call["id"] in lane.bindings
        else:
            assert fresh["state"] == "ended" and provider.adds == 0
        await lane.recover_opening(row["id"])
        assert provider.adds == (1 if outcome == "redial" else 0)
        await lane.close()
    asyncio.run(run())


def test_answer_cancels_unfetched_cue_and_waits_only_for_fetched_audio(lane_app):
    async def run():
        lane = lane_app.state.twilio_lane
        lane.coordinator.register("call", "session")
        state = lane.coordinator.calls["call"]
        lane.announcements["stale"] = {"call_id": "call"}
        state.clip_pending_until = lane.coordinator.clock()+.75
        await lane.before_answer("call", "session")
        assert not lane.announcements and state.clip_pending_until == 0 and state.answer_ready
        state.clip_until = lane.coordinator.clock()+.1
        task = asyncio.create_task(lane.before_answer("call", "session"))
        await asyncio.sleep(.01)
        assert not task.done()
        await task
        assert lane.coordinator.clock() >= state.clip_until
        await lane.coordinator.close()
    asyncio.run(run())
