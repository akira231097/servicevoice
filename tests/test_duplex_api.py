"""Offline HTTP boundaries for the isolated browser transport; real Store auth."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import JSONResponse
import httpx
import pytest

from app.backchannel_audio import BackchannelAudioCache
from app.backchannel_policy import BackchannelDecision
from app.domain import DomainError, Store
from app.duplex_api import install_duplex_api
from app.duplex_cues import DuplexCuePolicy

ASSISTANT = "isolated-assistant-fixture"
VAPI_KEY = "synthetic-provider-credential"
JEV_KEY = "synthetic-classifier-credential"


class Policy:
    def __init__(self, callback=None):
        self.calls, self.callback, self.closed = [], callback, False

    async def start(self):
        return True

    async def close(self):
        self.closed = True

    async def choose(self, text, key, **options):
        self.calls.append((text, key, options))
        if self.callback:
            value = self.callback(text, key, options)
            if hasattr(value, "__await__"):
                await value
        return BackchannelDecision(phrase_id="neutral_ack" if options["phase"] == "listening" else "moment",
            reason="eligible_fixture", confidence=.99, language_choice="cache_compatible", requests=1)


@asynccontextmanager
async def harness(tmp_path, *, values=None, policy=None, provider=None, phrases=("neutral_ack", "moment")):
    cache = BackchannelAudioCache(tmp_path / "audio")
    pcm = b"\x01\x00" * 1600
    assets = [cache.store(phrase, pcm) for phrase in phrases]
    manifest = cache.write_manifest(assets)
    settings = {"SERVICEVOICE_DUPLEX": "on", "VAPI_ASSISTANT_ID": ASSISTANT,
        "VAPI_BACKCHANNEL_ASSISTANT_ID": ASSISTANT, "VAPI_API_KEY": VAPI_KEY,
        "TYPESAFE_API_KEY": JEV_KEY, "SERVICEVOICE_BACKCHANNEL_AUDIO_MANIFEST": str(manifest),
        "TODAY": "2026-10-20", **(values or {})}
    store = Store(tmp_path / "scheduler.sqlite3")
    first, second = store.create_session("live"), store.create_session("live")
    calls = []

    async def handler(request):
        calls.append(request)
        if provider:
            answer = provider(request)
            return await answer if hasattr(answer, "__await__") else answer
        return httpx.Response(201, json={"id": "isolated-call-fixture", "assistantId": ASSISTANT,
            "transport": {"websocketCallUrl": "wss://phone-call-websocket.aws-us-west-2-backend-production1.vapi.ai/fixture"},
            "unnecessary_private_provider_field": VAPI_KEY})

    app = FastAPI()

    @app.exception_handler(DomainError)
    async def domain_error(request, error):
        return JSONResponse({"error": error.code}, status_code=401 if error.code == "unauthorized" else 409)

    def snapshot(sid, token):
        store.authorize(sid, token, True)
        return store.snapshot(sid)

    policy = policy or Policy()
    provider_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    control = install_duplex_api(app, lambda: settings, store.authorize, snapshot,
        client=provider_client, policy=policy)
    await control.start()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        try:
            yield SimpleNamespace(client=client, control=control, store=store, first=first, second=second,
                settings=settings, calls=calls, policy=policy, manifest=manifest, assets=assets, pcm=pcm)
        finally:
            await control.close()


def headers(session):
    return {"x-session-token": session["token"]}


def path(session, suffix):
    return f"/api/sessions/{session['id']}/duplex/{suffix}"


def cue(**changes):
    return {"text": "I need routine service for my vehicle", "phase": "listening", "epoch": 1, "revision": 1, **changes}


def end_session(store, sid):
    with store.transaction() as db:
        db.execute("UPDATE sessions SET ended=1 WHERE id=?", (sid,))


def test_real_store_auth_rejects_wrong_owner_unknown_missing_and_ended_sessions_without_provider_work(tmp_path):
    async def run():
        async with harness(tmp_path) as h:
            for suffix, method, body in [("call", "post", None), ("cue", "post", cue()),
                ("audio/neutral_ack", "get", None), ("event", "post", {"event": "cue_started", "epoch": 1, "at_ms": 1})]:
                for target, token in [(h.first, h.second["token"]), (h.first, ""), ({"id": "unknown"}, h.first["token"])]:
                    response = await h.client.request(method, path(target, suffix), headers={"x-session-token": token}, json=body)
                    assert response.status_code == 401, response.text
            end_session(h.store, h.first["id"])
            response = await h.client.post(path(h.first, "call"), headers=headers(h.first))
            assert response.status_code == 409
            assert h.calls == [] and h.policy.calls == [] and h.control.sessions == {}
    asyncio.run(run())


def test_rehearsal_session_cannot_allocate_live_transport_or_request_a_cue(tmp_path):
    async def run():
        async with harness(tmp_path) as h:
            rehearsal = h.store.create_session("rehearsal")
            response = await h.client.post(path(rehearsal, "call"), headers=headers(rehearsal))
            assert response.status_code == 409
            response = await h.client.post(path(rehearsal, "cue"), headers=headers(rehearsal), json=cue())
            assert response.status_code == 409
            assert not h.calls and not h.policy.calls
    asyncio.run(run())


@pytest.mark.parametrize("overrides", [{"SERVICEVOICE_DUPLEX": "off"}, {"VAPI_BACKCHANNEL_ASSISTANT_ID": "accepted-baseline"},
    {"VAPI_ASSISTANT_ID": ""}])
def test_enable_flag_and_exact_isolated_assistant_are_required(tmp_path, overrides):
    async def run():
        async with harness(tmp_path, values=overrides) as h:
            response = await h.client.get("/api/duplex/config")
            assert response.status_code == 503
            response = await h.client.post(path(h.first, "call"), headers=headers(h.first))
            assert response.status_code == 503 and not h.calls
    asyncio.run(run())


def test_config_and_cached_audio_return_only_validated_geometry_hashes_and_scoped_bytes(tmp_path):
    async def run():
        async with harness(tmp_path) as h:
            response = await h.client.get("/api/duplex/config")
            assert response.status_code == 200, response.text
            config = response.json()
            assert config["sample_rate"] == 16000 and config["phone_enabled"] is False
            assert {item["phrase_id"] for item in config["phrases"]} == {"neutral_ack", "moment"}
            assert all(item["sha256"] == hashlib.sha256(h.pcm).hexdigest() and item["pcm_bytes"] == len(h.pcm)
                and item["sample_rate"] == 16000 for item in config["phrases"])
            assert not any(secret in response.text for secret in (VAPI_KEY, JEV_KEY, h.first["token"], str(tmp_path)))
            audio = await h.client.get(path(h.first, "audio/neutral_ack"), headers=headers(h.first))
            assert audio.status_code == 200 and audio.content == h.pcm
            assert audio.headers["x-audio-sample-rate"] == "16000"
            assert audio.headers["cache-control"] == "no-store"
            assert (await h.client.get(path(h.first, "audio/checking"), headers=headers(h.first))).status_code == 404
            assert not h.calls
    asyncio.run(run())


@pytest.mark.parametrize("damage", ["manifest", "pcm", "missing_phrase"])
def test_audio_integrity_failure_prevents_config_and_paid_call_allocation(tmp_path, damage):
    async def run():
        async with harness(tmp_path, phrases=("neutral_ack",) if damage == "missing_phrase" else ("neutral_ack", "moment")) as h:
            if damage == "manifest":
                h.manifest.write_bytes(h.manifest.read_bytes() + b" ")
            elif damage == "pcm":
                target = h.manifest.parent / h.assets[0]["pcm_file"]
                target.write_bytes(b"\x02\x00" * 1600)
            response = await h.client.get("/api/duplex/config")
            assert response.status_code == 503
            response = await h.client.post(path(h.first, "call"), headers=headers(h.first))
            assert response.status_code == 503 and not h.calls
            assert str(tmp_path) not in response.text
    asyncio.run(run())


def test_call_allocation_ignores_client_override_fields_and_pins_candidate_transport_and_duration(tmp_path):
    async def run():
        async with harness(tmp_path) as h:
            response = await h.client.post(path(h.first, "call"), headers=headers(h.first), json={
                "assistantId": "accepted-baseline", "sampleRate": 48000, "maxDurationSeconds": 9999,
                "transport": {"provider": "phone"}})
            assert response.status_code == 200, response.text
            assert set(response.json()) == {"call_id", "websocket_url", "sample_rate"}
            assert response.json()["sample_rate"] == 16000
            assert VAPI_KEY not in response.text and JEV_KEY not in response.text and h.first["token"] not in response.text
            request = h.calls[0]
            assert request.url == "https://api.vapi.ai/call"
            assert request.headers["authorization"] == "Bearer " + VAPI_KEY
            payload = json.loads(request.content)
            assert payload["assistantId"] == ASSISTANT
            assert payload["transport"] == {"provider": "vapi.websocket", "audioFormat": {
                "format": "pcm_s16le", "container": "raw", "sampleRate": 16000}}
            assert payload["assistantOverrides"]["maxDurationSeconds"] == 300
            assert payload["assistantOverrides"]["variableValues"] == {"session_id": h.first["id"],
                "session_token": h.first["token"], "current_date": "2026-10-20", "timezone": "America/Chicago",
                "servicevoice_duplex_playback": "browser-owned-v1"}
            assert (await h.client.post(path(h.first, "call"), headers=headers(h.first))).status_code == 409
            assert len(h.calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize("key", [None, ""])
def test_missing_vapi_key_never_contacts_provider(tmp_path, key):
    async def run():
        async with harness(tmp_path, values={"VAPI_API_KEY": key}) as h:
            response = await h.client.post(path(h.first, "call"), headers=headers(h.first))
            assert response.status_code == 503 and not h.calls
    asyncio.run(run())


def test_unknown_network_outcome_is_never_replayed_and_session_does_not_claim_a_call(tmp_path):
    async def run():
        def timeout(request):
            raise httpx.ReadTimeout("private synthetic upstream body", request=request)
        async with harness(tmp_path, provider=timeout) as h:
            response = await h.client.post(path(h.first, "call"), headers=headers(h.first))
            assert response.status_code == 502 and "private synthetic" not in response.text
            assert (await h.client.post(path(h.first, "call"), headers=headers(h.first))).status_code == 409
            assert len(h.calls) == 1 and h.first["id"] in h.control.sessions and not h.control.creating
            assert not h.control.calls, "Local preflight ownership is not evidence of a provider call."
            assert h.first["id"] in h.control.created
    asyncio.run(run())


def test_full_local_session_capacity_prevents_paid_call_and_durable_reservation(tmp_path):
    async def run():
        ledger = tmp_path / "uncreated-call-ledger.sqlite3"
        async with harness(tmp_path, values={"SERVICEVOICE_DUPLEX_CALL_LEDGER": str(ledger)}) as h:
            for ordinal in range(64):
                await h.control.session("other-owned-session-" + str(ordinal))
            response = await h.client.post(path(h.first, "call"), headers=headers(h.first))
            assert response.status_code == 429
            assert h.calls == [] and not ledger.exists()
            assert len(h.control.sessions) == 64 and h.first["id"] not in h.control.sessions
            assert not h.control.calls and not h.control.created and not h.control.creating
    asyncio.run(run())


def test_concurrent_duplicate_call_request_is_rejected_while_the_original_is_in_flight(tmp_path):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def held(request):
            entered.set(); await release.wait()
            return httpx.Response(503, text="synthetic unavailable")
        async with harness(tmp_path, provider=held) as h:
            first = asyncio.create_task(h.client.post(path(h.first, "call"), headers=headers(h.first)))
            await entered.wait()
            second = await h.client.post(path(h.first, "call"), headers=headers(h.first))
            assert second.status_code == 409 and len(h.calls) == 1
            release.set(); assert (await first).status_code == 502
    asyncio.run(run())


@pytest.mark.parametrize("address", ["ws://backend.vapi.ai/call", "wss://vapi.ai.attacker.invalid/call",
    "wss://attacker-vapi.ai/call", "wss://user:password@backend.vapi.ai/call", "wss://backend.vapi.ai:8443/call",
    "wss://backend.vapi.ai/call#fragment"])
def test_provider_transport_is_restricted_to_credential_free_tls_vapi_endpoints(tmp_path, address):
    async def run():
        async with harness(tmp_path, provider=lambda request: httpx.Response(201, json={"id": "call",
            "assistantId": ASSISTANT, "transport": {"websocketCallUrl": address}})) as h:
            response = await h.client.post(path(h.first, "call"), headers=headers(h.first))
            assert response.status_code == 502 and "password" not in response.text
    asyncio.run(run())


def test_provider_cannot_silently_return_a_different_assistant(tmp_path):
    async def run():
        async with harness(tmp_path, provider=lambda request: httpx.Response(201, json={"id": "call",
            "assistantId": "accepted-baseline", "transport": {"websocketCallUrl": "wss://backend.vapi.ai/call"}})) as h:
            assert (await h.client.post(path(h.first, "call"), headers=headers(h.first))).status_code == 502
    asyncio.run(run())


def test_cues_are_call_isolated_and_read_only_against_the_real_scheduler(tmp_path):
    async def run():
        async with harness(tmp_path) as h:
            before = h.store.snapshot(h.first["id"])
            first = await h.client.post(path(h.first, "cue"), headers=headers(h.first), json=cue())
            second = await h.client.post(path(h.second, "cue"), headers=headers(h.second), json=cue())
            assert first.status_code == second.status_code == 200
            assert first.json()["phrase_id"] == second.json()["phrase_id"] == "neutral_ack"
            assert first.json()["epoch"] == first.json()["revision"] == 1
            assert 0 < first.json()["expires_in_ms"] <= 800
            assert h.control.sessions[h.first["id"]] is not h.control.sessions[h.second["id"]]
            assert h.store.snapshot(h.first["id"]) == before
            assert not h.calls
            with h.store.connect() as db:
                assert db.execute("SELECT count(*) FROM appointments").fetchone()[0] == 0
                assert db.execute("SELECT count(*) FROM tool_invocations").fetchone()[0] == 0
    asyncio.run(run())


def test_scheduler_becoming_protected_during_classification_suppresses_a_previously_valid_result(tmp_path):
    async def run():
        async with harness(tmp_path) as h:
            h.policy.callback = lambda *_: h.store.update(h.first["id"], {"stage": "confirmation"})
            response = await h.client.post(path(h.first, "cue"), headers=headers(h.first), json=cue())
            assert response.status_code == 200, response.text
            assert response.json()["phrase_id"] == "silence"
            assert response.json()["reason"] == "scheduler_changed"
            assert response.json()["expires_in_ms"] == 0
            assert not h.calls
    asyncio.run(run())


@pytest.mark.parametrize("after,allowed", [
    ({"stage": "slot"}, True), ({"stage": "details"}, False),
    ({"stage": "confirmation"}, False), ({"stage": "slot", "proposal": {"id": "fixture"}}, False),
    ({"stage": "slot", "pending_operations": [{"id": "fixture", "status": "running"}]}, False),
    ({"stage": "slot", "ended": True}, False),
    ({"stage": "slot", "transcript": [{"role": "assistant", "text": "Please confirm the booking readback."}]}, False),
])
def test_safe_stage_only_transition_can_preserve_hold_but_other_scheduler_changes_cannot(tmp_path, after, allowed):
    async def run():
        async with harness(tmp_path) as h:
            original_snapshot = h.control.snapshot
            changed = []
            def snapshot(sid, token):
                return {**original_snapshot(sid, token), **(after if changed else {})}
            h.control.snapshot = snapshot
            h.policy.callback = lambda *_: changed.append(True)
            response = await h.client.post(path(h.first, "cue"), headers=headers(h.first), json=cue(phase="waiting"))
            assert response.status_code == 200
            assert response.json()["phrase_id"] == ("moment" if allowed else "silence")
            if not allowed:
                assert response.json()["reason"] == "scheduler_changed"
    asyncio.run(run())


def test_fixed_content_free_client_dispositions_are_accepted_but_arbitrary_reason_text_is_rejected(tmp_path):
    async def run():
        async with harness(tmp_path) as h:
            for kind, reason, blocked in (("cue_suppressed", "selection_expired_on_arrival", None),
                    ("cue_canceled", "caller_resumed", None), ("cue_gate_changed", "sensitive_state", False)):
                body = {"event": kind, "epoch": 1, "at_ms": 100, "phase": "waiting", "reason": reason}
                if blocked is not None:
                    body["blocked"] = blocked
                response = await h.client.post(path(h.first, "event"), headers=headers(h.first), json=body)
                assert response.status_code == 200
            rejected = await h.client.post(path(h.first, "event"), headers=headers(h.first), json={
                "event": "cue_suppressed", "epoch": 1, "at_ms": 100, "reason": "PRIVATE_CALLER_WORDS"})
            assert rejected.status_code == 422
            evidence = await h.client.get(path(h.first, "evidence"), headers=headers(h.first))
            assert len(evidence.json()["events"]) == 3
            assert "PRIVATE_CALLER_WORDS" not in evidence.text
    asyncio.run(run())


@pytest.mark.parametrize("identical_callback", [False, True])
@pytest.mark.parametrize("change", ["none", "confirmation", "ended"])
def test_cue_reuses_only_identical_authenticated_snapshot_and_always_revalidates(tmp_path, identical_callback, change):
    async def run():
        async with harness(tmp_path) as h:
            reads = []

            def fresh_snapshot(sid, token):
                reads.append("snapshot")
                h.store.authorize(sid, token, True)
                return h.store.snapshot(sid)

            def distinct_authorize(sid, token):
                reads.append("authorize")
                # This intentionally returns only a Store session row. Reusing
                # it as scheduler state would incorrectly suppress safe cues.
                return h.store.authorize(sid, token)

            h.control.snapshot = fresh_snapshot
            h.control.authorize = fresh_snapshot if identical_callback else distinct_authorize

            def during_classification(*_):
                if change == "confirmation":
                    h.store.update(h.first["id"], {"stage": "confirmation"})
                elif change == "ended":
                    end_session(h.store, h.first["id"])
            h.policy.callback = during_classification
            response = await h.client.post(path(h.first, "cue"), headers=headers(h.first), json=cue())
            assert response.status_code == 200, response.text
            assert reads == (["snapshot", "snapshot"] if identical_callback else
                ["authorize", "snapshot", "snapshot"])
            assert len(h.policy.calls) == 1
            if change == "none":
                assert response.json()["phrase_id"] == "neutral_ack"
            else:
                assert response.json()["phrase_id"] == "silence"
                assert response.json()["reason"] == "scheduler_changed"
                assert response.json()["expires_in_ms"] == 0
            assert not h.calls
    asyncio.run(run())


def test_identical_authenticated_snapshot_failure_stops_before_classifier_or_session_creation(tmp_path):
    async def run():
        async with harness(tmp_path) as h:
            reads = []

            def fresh_snapshot(sid, token):
                reads.append(sid)
                h.store.authorize(sid, token, True)
                return h.store.snapshot(sid)
            h.control.authorize = h.control.snapshot = fresh_snapshot
            response = await h.client.post(path(h.first, "cue"), headers=headers(h.second), json=cue())
            assert response.status_code == 401
            assert reads == [h.first["id"]]
            assert not h.policy.calls and not h.calls and not h.control.sessions
    asyncio.run(run())


@pytest.mark.parametrize("after_read_seconds,expired", [(.25, False), (.75, True)])
def test_request_ttl_includes_authorization_and_fresh_read_and_is_remaining_at_response(tmp_path, after_read_seconds, expired):
    from test_duplex_cues import Clock

    async def run():
        async with harness(tmp_path) as h:
            clock, reads = Clock(), []
            h.control.clock = clock
            session = await h.control.session(h.first["id"])
            session.clock = clock

            def fresh_snapshot(sid, token):
                clock.now += .2 if not reads else after_read_seconds
                reads.append(True)
                h.store.authorize(sid, token, True)
                return h.store.snapshot(sid)
            h.control.authorize = h.control.snapshot = fresh_snapshot
            def classification(*_):
                clock.now += .15
            h.policy.callback = classification
            response = await h.client.post(path(h.first, "cue"), headers=headers(h.first), json=cue())
            assert response.status_code == 200
            value = response.json()
            assert value["max_request_age_ms"] == 800
            if expired:
                assert value["phrase_id"] == "silence" and value["reason"] == "request_expired"
                assert value["expires_in_ms"] == 0
            else:
                assert value["phrase_id"] == "neutral_ack"
                # 200 ms auth +150 ms policy +250 ms revalidation leaves200 ms
                # of the absolute800 ms request lifetime, not the old650 ms.
                assert 198 <= value["expires_in_ms"] <= 200
            assert len(reads) == 2 and len(h.policy.calls) == 1
            row = h.control.decisions[h.first["id"]][-1]
            assert row["expires_in_ms"] == value["expires_in_ms"]
            assert row["max_request_age_ms"] == 800
            assert row["request_elapsed_ms"] >= 600
    asyncio.run(run())


@pytest.mark.parametrize("guard", ["classification_throttled", "cue_spacing"])
def test_pacing_retry_hint_is_remaining_after_authoritative_read_and_requires_new_selection(tmp_path, guard):
    from test_duplex_cues import Clock

    async def run():
        async with harness(tmp_path) as h:
            clock = Clock()
            h.control.clock = clock
            session = await h.control.session(h.first["id"])
            session.clock = clock
            if guard == "classification_throttled":
                session.last_attempt_at = 0
            else:
                session.last_cue_at = 0
            clock.now = .1
            reads = []

            def fresh_snapshot(sid, token):
                h.store.authorize(sid, token, True)
                if len(reads) % 2:
                    clock.now += .05
                reads.append(True)
                return h.store.snapshot(sid)
            h.control.authorize = h.control.snapshot = fresh_snapshot
            response = await h.client.post(path(h.first, "cue"), headers=headers(h.first),
                json=cue(phase="waiting"))
            value = response.json()
            assert value["phrase_id"] == "silence" and value["reason"] == guard
            assert value["requests"] == 0 and value["expires_in_ms"] == 0
            assert 149 <= value["retry_after_ms"] <= 151
            assert h.control.decisions[h.first["id"]][-1]["retry_after_ms"] == value["retry_after_ms"]
            assert not h.policy.calls and not session.reservations
            clock.now += value["retry_after_ms"] / 1000 + .001
            accepted = await h.client.post(path(h.first, "cue"), headers=headers(h.first),
                json=cue(phase="waiting", revision=2))
            assert accepted.json()["phrase_id"] == "moment"
            assert "retry_after_ms" not in accepted.json()
            assert session.count == session.classifications == len(h.policy.calls) == 1
    asyncio.run(run())


def test_changed_authoritative_state_revokes_pacing_retry_hint(tmp_path):
    from test_duplex_cues import Clock

    async def run():
        async with harness(tmp_path) as h:
            clock, reads = Clock(), []
            h.control.clock = clock
            session = await h.control.session(h.first["id"])
            session.clock = clock
            session.last_attempt_at = 0

            def fresh_snapshot(sid, token):
                h.store.authorize(sid, token, True)
                snapshot = h.store.snapshot(sid)
                if reads:
                    snapshot["stage"] = "confirmation"
                reads.append(True)
                return snapshot
            h.control.authorize = h.control.snapshot = fresh_snapshot
            response = await h.client.post(path(h.first, "cue"), headers=headers(h.first),
                json=cue(phase="waiting"))
            assert response.json()["reason"] == "scheduler_changed"
            assert "retry_after_ms" not in response.json()
            assert h.control.decisions[h.first["id"]][-1]["retry_after_ms"] is None
            assert not h.policy.calls and not session.reservations
    asyncio.run(run())


def test_waiting_pacing_retry_event_is_bounded_and_content_free(tmp_path):
    async def run():
        async with harness(tmp_path) as h:
            body = {"event": "cue_retry_scheduled", "epoch": 1, "at_ms": 10,
                "phase": "waiting", "reason": "cue_spacing", "retry_after_ms": 150}
            response = await h.client.post(path(h.first, "event"), headers=headers(h.first), json=body)
            assert response.status_code == 200
            saved = h.control.events[h.first["id"]][-1]
            assert saved["retry_after_ms"] == 150 and saved["phase"] == "waiting"
            for changes in ({"retry_after_ms": 0}, {"retry_after_ms": 1001}, {"retry_after_ms": True},
                    {"retry_after_ms": None}, {"phase": "listening"},
                    {"reason": "classification_uncertain"}, {"event": "cue_suppressed"}):
                rejected = await h.client.post(path(h.first, "event"), headers=headers(h.first),
                    json={**body, **changes})
                assert rejected.status_code == 422
            assert len(h.control.events[h.first["id"]]) == 1
    asyncio.run(run())


def test_decision_evidence_is_bounded_content_free_and_owner_scoped(tmp_path):
    async def run():
        async with harness(tmp_path) as h:
            for revision in range(1, 261):
                response = await h.client.post(path(h.first, "cue"), headers=headers(h.first),
                    json=cue(text="PRIVATE_UTTERANCE_FIXTURE", phase="waiting", revision=revision))
                assert response.status_code == 200
            forbidden = await h.client.get(path(h.first, "evidence"), headers=headers(h.second))
            assert forbidden.status_code == 401
            response = await h.client.get(path(h.first, "evidence"), headers=headers(h.first))
            assert response.status_code == 200
            rows = response.json()["cue_decisions"]
            assert len(rows) == 256 and rows[0]["revision"] == 5 and rows[-1]["revision"] == 260
            assert rows[-1]["phase"] == "waiting" and rows[-1]["reason"] == "phase_already_acknowledged"
            assert rows[-1]["budget"]["lifetime_cue_reservations"] == 1
            assert all(secret not in response.text for secret in ("PRIVATE_UTTERANCE_FIXTURE", JEV_KEY,
                VAPI_KEY, h.first["token"], h.second["token"], "https://", "previous_assistant"))
            assert not h.calls
    asyncio.run(run())


def test_barge_in_event_cancels_an_inflight_selection_without_waiting_for_the_classifier(tmp_path):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def blocked(*_):
            entered.set(); await release.wait()
        async with harness(tmp_path, policy=Policy(blocked)) as h:
            task = asyncio.create_task(h.client.post(path(h.first, "cue"), headers=headers(h.first), json=cue()))
            await entered.wait()
            event = await h.client.post(path(h.first, "event"), headers=headers(h.first), json={
                "event": "barge_in", "epoch": 2, "at_ms": 100})
            assert event.status_code == 200
            response = await asyncio.wait_for(task, 1)
            assert response.status_code == 200 and response.json()["phrase_id"] == "silence"
            assert response.json()["reason"] == "superseded"
    asyncio.run(run())


def test_missing_classifier_key_cannot_issue_a_provider_request(tmp_path):
    async def run():
        classifier_calls = []
        def forbidden(request):
            classifier_calls.append(request)
            pytest.fail("missing classifier key issued a request")
        client = httpx.AsyncClient(transport=httpx.MockTransport(forbidden))
        policy = DuplexCuePolicy(client=client)
        try:
            async with harness(tmp_path, values={"TYPESAFE_API_KEY": ""}, policy=policy) as h:
                response = await h.client.post(path(h.first, "cue"), headers=headers(h.first), json=cue())
                assert response.status_code == 200
                assert response.json()["phrase_id"] == "silence" and response.json()["reason"] == "credential_missing"
                assert not classifier_calls and not h.calls
        finally:
            await client.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("changes", [{"epoch": True}, {"revision": 0}, {"phase": "thinking"},
    {"text": "x" * 2001}, {"text": "ordinary words", "operation": "confirm_action"}])
def test_cue_request_bounds_reject_invalid_control_or_arbitrary_action_fields(tmp_path, changes):
    async def run():
        async with harness(tmp_path) as h:
            response = await h.client.post(path(h.first, "cue"), headers=headers(h.first), json=cue(**changes))
            assert response.status_code == 422 and not h.policy.calls and not h.calls
    asyncio.run(run())


def test_playback_events_and_evidence_are_bounded_content_free_and_owned_by_the_exact_session(tmp_path):
    async def run():
        async with harness(tmp_path) as h:
            response = await h.client.post(path(h.first, "event"), headers=headers(h.first), json={
                "event": "cue_started", "epoch": 1, "at_ms": 123, "phrase_id": "neutral_ack"})
            assert response.status_code == 200
            rejected = await h.client.post(path(h.first, "event"), headers=headers(h.first), json={
                "event": "cue_started", "epoch": 1, "at_ms": 124, "text": "sensitive transcript"})
            assert rejected.status_code == 422
            denied = await h.client.get(path(h.first, "evidence"), headers=headers(h.second))
            assert denied.status_code == 401
            empty = await h.client.get(path(h.second, "evidence"), headers=headers(h.second))
            assert empty.json()["events"] == []
            end_session(h.store, h.first["id"])
            evidence = await h.client.get(path(h.first, "evidence"), headers=headers(h.first))
            assert evidence.status_code == 200 and len(evidence.json()["events"]) == 1
            assert evidence.json()["provider_packet_generation_ids"] == "unavailable"
            assert evidence.json()["human_listening"] == "not_established"
            assert not any(secret in evidence.text for secret in (VAPI_KEY, JEV_KEY, h.first["token"], "sensitive transcript"))
            await h.control.session(h.second["id"])
            h.control.events[h.second["id"]] = [{"event": "test", "epoch": 0, "at_ms": 0}] * 1000
            bounded = await h.client.post(path(h.second, "event"), headers=headers(h.second), json={
                "event": "cue_ended", "epoch": 1, "at_ms": 200})
            assert bounded.status_code == 429 and len(h.control.events[h.second["id"]]) == 1000
    asyncio.run(run())


def test_scoped_stop_accepts_ended_session_but_cannot_end_another_sessions_call_and_is_idempotent(tmp_path):
    async def run():
        def provider(request):
            if request.method == "GET":
                return httpx.Response(200, json={"id": "isolated-call-fixture", "assistantId": ASSISTANT,
                    "status": "in-progress", "monitor": {"controlUrl": "https://backend.vapi.ai/control/fixture"}})
            if request.url.path == "/control/fixture":
                assert json.loads(request.content) == {"type": "end-call"}
                return httpx.Response(204)
            return httpx.Response(201, json={"id": "isolated-call-fixture", "assistantId": ASSISTANT,
                "transport": {"websocketCallUrl": "wss://backend.vapi.ai/fixture"}})
        async with harness(tmp_path, provider=provider) as h:
            await h.client.post(path(h.first, "call"), headers=headers(h.first))
            denied = await h.client.post(path(h.first, "stop"), headers=headers(h.second))
            assert denied.status_code == 401 and len(h.calls) == 1
            unbound = await h.client.post(path(h.second, "stop"), headers=headers(h.second), json={"call_id": "isolated-call-fixture"})
            assert unbound.json()["call_known"] is False and len(h.calls) == 1
            end_session(h.store, h.first["id"])
            stopped = await h.client.post(path(h.first, "stop"), headers=headers(h.first), json={"call_id": "unrelated-call"})
            assert stopped.status_code == 200 and stopped.json()["ok"] is True
            assert [request.url.path for request in h.calls] == ["/call", "/call/isolated-call-fixture", "/control/fixture"]
            assert (await h.client.post(path(h.first, "stop"), headers=headers(h.first))).json()["ok"] is True
            assert len(h.calls) == 3
            assert VAPI_KEY not in stopped.text and h.first["token"] not in stopped.text
    asyncio.run(run())


@pytest.mark.parametrize("address", ["http://backend.vapi.ai/control", "https://vapi.ai.attacker.invalid/control",
    "https://user:password@backend.vapi.ai/control", "https://backend.vapi.ai:444/control"])
def test_stop_rejects_untrusted_monitor_control_urls_without_forwarding_private_provider_credentials(tmp_path, address):
    async def run():
        def provider(request):
            assert request.method == "GET"
            return httpx.Response(200, json={"id": "isolated-call-fixture", "assistantId": ASSISTANT,
                "monitor": {"controlUrl": address}})
        async with harness(tmp_path, provider=provider) as h:
            h.control.calls[h.first["id"]] = "isolated-call-fixture"
            response = await h.client.post(path(h.first, "stop"), headers=headers(h.first))
            assert response.status_code == 200 and response.json()["ok"] is False
            assert len(h.calls) == 1 and h.calls[0].url.host == "api.vapi.ai"
    asyncio.run(run())


def test_durable_allocation_reservation_survives_lost_inmemory_state_after_unknown_provider_outcome(tmp_path):
    async def run():
        def timeout(request):
            raise httpx.ReadTimeout("fixture unknown outcome", request=request)
        async with harness(tmp_path, values={"SERVICEVOICE_DUPLEX_CALL_LEDGER": str(tmp_path / "allocations.sqlite3")}, provider=timeout) as h:
            first = await h.client.post(path(h.first, "call"), headers=headers(h.first))
            assert first.status_code == 502
            h.control.created.clear(); h.control.creating.clear()
            repeated = await h.client.post(path(h.first, "call"), headers=headers(h.first))
            assert repeated.status_code == 409 and len(h.calls) == 1
    asyncio.run(run())
