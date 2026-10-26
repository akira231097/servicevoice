"""Verified combined-service speech skips no safeguards and no selected service."""
import asyncio
import pytest

from app.receipt_narration import candidate, select
from test_receipt_narration import EN, issue, post, setup, text_of, confirm


SCOPE = {"service": "diagnostic", "services": ["diagnostic", "brakes"], "presentation": EN}


async def proposal(env):
    found = await issue(env, "begin_service_request", SCOPE)
    offers = text_of(await post(env))
    assert "Diagnostic appointment" in offers and "Brake inspection" in offers
    assert "one 60-minute intake visit" in offers
    return await issue(env, "prepare_booking", {**SCOPE, "slot_id": found["slots"][0]["id"],
        "customer_name": "Alex Morgan", "phone": "5125550142", "vehicle": "2022 Toyota Camry"})


def test_complete_scope_readback_and_single_commit(tmp_path, monkeypatch):
    env = setup(tmp_path, monkeypatch)
    async def run():
        try:
            prepared = await proposal(env)
            text = text_of(await post(env))
            for expected in ("Diagnostic appointment", "Brake inspection", "One 60-minute intake visit", "2022 Toyota Camry", "Alex Morgan", "Callback five one two, five five five, zero one four two", "confirm this booking?"):
                assert expected in text
            assert env.store.snapshot(env.sid)["appointment"] is None
            result = await confirm(env, prepared)
            text = text_of(await post(env))
            assert "Diagnostic appointment" in text and "Brake inspection" in text
            assert "appointment appointment" not in text
            assert result["appointment"]["reference"] in text
            with env.store.read_connection() as db:
                assert db.execute("SELECT COUNT(*) FROM appointments").fetchone()[0] == 1
                assert db.execute("SELECT COUNT(*) FROM appointment_services").fetchone()[0] == 2
            assert not env.sent
        finally:
            await env.gateway.close()
    asyncio.run(run())


def test_service_only_correction_reads_every_target_service_without_saving_early(tmp_path, monkeypatch):
    env = setup(tmp_path, monkeypatch)
    async def run():
        try:
            saved = await confirm(env, await proposal(env))
            before = saved["appointment"]
            change = await issue(env, "prepare_reschedule", {"slot_id": before["slot_id"], "services": ["brakes", "tires"], "presentation": EN})
            text = text_of(await post(env))
            assert "Brake inspection" in text and "Tire inspection & rotation" in text
            assert "Diagnostic appointment" not in text and "Service and vehicle stay the same" not in text
            assert "confirm this change?" in text
            assert env.store.snapshot(env.sid)["appointment"] == before
            result = await confirm(env, change)
            assert result["appointment"]["services"] == ["brakes", "tires"]
            assert result["appointment"]["reference"] == before["reference"]
            assert result["appointment"]["id"] == before["id"]
            assert not env.sent
        finally:
            await env.gateway.close()
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["prepare_reschedule", "prepare_cancellation"])
def test_changed_child_scope_prevents_stale_readback(tmp_path, monkeypatch, kind):
    env = setup(tmp_path, monkeypatch)
    async def run():
        try:
            saved = await confirm(env, await proposal(env))
            before = saved["appointment"]
            args = {"slot_id": before["slot_id"], "services": ["brakes"], "presentation": EN} if kind == "prepare_reschedule" else {"presentation": EN}
            await issue(env, kind, args)
            prepared = candidate(env.history)
            with env.store.connect() as db:
                db.execute("DELETE FROM appointment_services WHERE appointment_id=? AND service='brakes'", (before["id"],))
            result = select(env.store, env.sid, env.cid, prepared)
            assert result.text is None and result.reason == "appointment_changed"
        finally:
            await env.gateway.close()
    asyncio.run(run())


def test_unresolved_question_keeps_model_path_for_combined_receipt(tmp_path, monkeypatch):
    env = setup(tmp_path, monkeypatch)
    async def run():
        try:
            search = await issue(env, "begin_service_request", SCOPE)
            await issue(env, "prepare_booking", {**SCOPE, "presentation": None, "slot_id": search["slots"][0]["id"],
                "customer_name": "Alex Morgan", "phone": "5125550142", "vehicle": "2022 Toyota Camry"})
            assert "Gemini fallback" in text_of(await post(env))
            assert len(env.sent) == 1 and env.store.snapshot(env.sid)["appointment"] is None
        finally:
            await env.gateway.close()
    asyncio.run(run())
