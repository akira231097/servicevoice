"""Availability/withdrawal speech only follows current, observed tool receipts."""
import asyncio
import hashlib
import json

import pytest

from app.receipt_narration import candidate, select
from app.domain import TENANT
from test_receipt_narration import EN, TE, confirm, issue, post, prepare, setup, text_of


@pytest.mark.parametrize("name", ["begin_service_request", "search_availability"])
def test_verified_availability_skips_second_model_without_proposal_or_reservation(tmp_path, monkeypatch, name):
    env = setup(tmp_path, monkeypatch)
    async def scenario():
        try:
            result = await issue(env, name, {"service": "diagnostic", "date": "2026-10-22", "presentation": EN})
            assert result["truncated"] is True and result["total_matching"] > 3
            response = await post(env)
            text = text_of(response)
            assert "9 AM" in text and "10 AM" in text and "There are more matching times" in text
            assert "offers, not reservations" in text and "confirmed" not in text
            assert not env.sent
            state = env.store.snapshot(env.sid)
            assert not state["proposal"] and not state["appointment"]
            row = env.gateway.latency_snapshot(env.cid)[-1]
            assert row["route"] == "deterministic_receipt" and row["counts"]["upstream_events"] == 0
            assert row["output_text_sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
            assert row["output_text_source"] == "verified_receipt"
            assert text not in json.dumps(row)
        finally: await env.gateway.close()
    asyncio.run(scenario())


def test_exact_afternoon_query_checks_whole_inventory_not_first_three(tmp_path, monkeypatch):
    env = setup(tmp_path, monkeypatch)
    async def scenario():
        try:
            result = await issue(env, "search_availability", {"service": "diagnostic", "date": "2026-10-22",
                "time_filter": {"exact_time": "14:00"}, "presentation": EN})
            assert result["complete"] and result["total_matching"] == 1
            text = text_of(await post(env))
            assert "2 PM" in text and "9 AM" not in text and "not available" not in text
            assert not env.sent
        finally: await env.gateway.close()
    asyncio.run(scenario())


def test_verified_zero_matches_is_scoped_and_does_not_silently_broaden(tmp_path, monkeypatch):
    env = setup(tmp_path, monkeypatch)
    async def scenario():
        try:
            result = await issue(env, "search_availability", {"service": "diagnostic", "date": "2026-10-22",
                "time_filter": {"exact_time": "14:15"}, "presentation": EN})
            assert result["complete"] and result["total_matching"] == 0
            text = text_of(await post(env))
            assert "requested search" in text and "different date or time?" in text
            assert not env.sent and not env.store.snapshot(env.sid)["slots"]
        finally: await env.gateway.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["capacity", "query", "slots", "proposal", "stage", "newer"])
def test_availability_rechecks_current_state_before_speaking(tmp_path, monkeypatch, change):
    env = setup(tmp_path, monkeypatch)
    async def scenario():
        try:
            result = await issue(env, "search_availability", {"service": "diagnostic", "date": "2026-10-22", "presentation": EN})
            prepared = candidate(env.history)
            other_sid = env.store.create_session("live")["id"] if change == "capacity" else None
            with env.store.connect() as db:
                if change == "capacity":
                    db.execute("INSERT INTO appointments VALUES (?,?,?,?,?,?,?,?)", ("apt_other", "CSV-OTHER", other_sid, TENANT,
                        "diagnostic", result["slots"][0]["id"], json.dumps({"name": "Another caller", "phone": "+15125550144", "vehicle": "2022 Toyota Camry"}), "confirmed"))
                elif change == "query": env.store.update(env.sid, {"availability_query": {}}, db)
                elif change == "slots": env.store.update(env.sid, {"slots": []}, db)
                elif change == "proposal": env.store.update(env.sid, {"proposal_id": "changed"}, db)
                elif change == "stage": env.store.update(env.sid, {"stage": "complete"}, db)
            if change == "newer": await issue(env, "get_session_context", {})
            decision = select(env.store, env.sid, env.cid, prepared)
            assert decision.text is None
            assert decision.reason == ("newer_result" if change == "newer" else "availability_changed")
        finally: await env.gateway.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("presentation", [None, TE])
def test_unclassified_or_other_language_uses_normal_multilingual_model(tmp_path, monkeypatch, presentation):
    env = setup(tmp_path, monkeypatch)
    async def scenario():
        try:
            args = {"service": "diagnostic"}
            if presentation is not None: args["presentation"] = presentation
            await issue(env, "search_availability", args)
            assert "Gemini fallback" in text_of(await post(env))
            assert len(env.sent) == 1
        finally: await env.gateway.close()
    asyncio.run(scenario())


def test_withdrawal_receipt_is_required_and_retains_booking_proposal(tmp_path, monkeypatch):
    env = setup(tmp_path, monkeypatch)
    async def scenario():
        try:
            handoff = await issue(env, "request_handoff", {"reason": "Caller wants an advisor", "presentation": EN})
            proposal = await prepare(env)
            before = env.store.snapshot(env.sid)
            result = await issue(env, "withdraw_handoff", {"handoff_id": handoff["id"], "presentation": EN})
            assert result["ok"] and result["status"] == "withdrawn"
            text = text_of(await post(env))
            assert text == "Your local advisor request is withdrawn. No appointment was changed."
            assert not env.sent
            after = env.store.snapshot(env.sid)
            assert after["proposal"] == before["proposal"] and after["proposal"]["id"] == proposal["proposal_id"]
            assert after["appointment"] is None and after["handoff"]["status"] == "withdrawn"
        finally: await env.gateway.close()
    asyncio.run(scenario())


def test_failed_or_stale_withdrawal_never_gets_success_narration(tmp_path, monkeypatch):
    env = setup(tmp_path, monkeypatch)
    async def scenario():
        try:
            failed = await issue(env, "withdraw_handoff", {"handoff_id": "handoff_unknown", "presentation": EN})
            assert failed["ok"] is False
            assert "Gemini fallback" in text_of(await post(env))
            assert env.gateway.latency_snapshot(env.cid)[-1]["narration_reason"] == "unsuccessful_result"
            saved = await issue(env, "request_handoff", {"reason": "Please ask an advisor", "presentation": EN})
            historical = candidate(env.history)
            await issue(env, "withdraw_handoff", {"handoff_id": saved["id"], "presentation": EN})
            assert select(env.store, env.sid, env.cid, historical).text is None
            withdrawn = candidate(env.history)
            with env.store.connect() as db:
                db.execute("UPDATE handoffs SET status='callback_requested' WHERE id=?", (saved["id"],))
            assert select(env.store, env.sid, env.cid, withdrawn).reason == "handoff_changed"
        finally: await env.gateway.close()
    asyncio.run(scenario())


def test_negative_availability_is_not_reused_after_another_session_releases_capacity(tmp_path, monkeypatch):
    env = setup(tmp_path, monkeypatch)
    async def scenario():
        try:
            other_sid = env.store.create_session("live")["id"]
            with env.store.connect() as db:
                slot = env.store.availability_result(db, "diagnostic", "2026-10-22", {"exact_time": "14:00"})["slots"][0]
                db.execute("INSERT INTO appointments VALUES (?,?,?,?,?,?,?,?)", ("apt_other", "CSV-OTHER", other_sid, TENANT,
                    "diagnostic", slot["id"], json.dumps({"name": "Other caller", "phone": "+15125550144", "vehicle": "2022 Toyota Camry"}), "confirmed"))
            result = await issue(env, "search_availability", {"service": "diagnostic", "date": "2026-10-22",
                "time_filter": {"exact_time": "14:00"}, "presentation": EN})
            assert result["complete"] and result["total_matching"] == 0
            prepared = candidate(env.history)
            with env.store.connect() as db: db.execute("UPDATE appointments SET status='cancelled' WHERE id='apt_other'")
            assert select(env.store, env.sid, env.cid, prepared).reason == "availability_changed"
        finally: await env.gateway.close()
    asyncio.run(scenario())


def test_withdrawing_advisor_request_after_booking_does_not_change_appointment(tmp_path, monkeypatch):
    env = setup(tmp_path, monkeypatch)
    async def scenario():
        try:
            saved = await issue(env, "request_handoff", {"reason": "Please ask an advisor"})
            proposal = await prepare(env)
            appointment = (await confirm(env, proposal))["appointment"]
            await issue(env, "withdraw_handoff", {"handoff_id": saved["id"], "presentation": EN})
            assert "is withdrawn" in text_of(await post(env))
            state = env.store.snapshot(env.sid)
            assert state["appointment"] == appointment and state["stage"] == "complete"
            assert not env.sent
        finally: await env.gateway.close()
    asyncio.run(scenario())
