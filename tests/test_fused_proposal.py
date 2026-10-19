"""Fused proposal equivalence, legacy replay and signed narration continuity."""
import asyncio
import hashlib
import json

import httpx
import pytest
from pydantic import ValidationError

from app.gemini_gateway import GatewayLimits, MODEL
from app.harness import ToolHarness
from app.tools import DEFINITIONS, MAX_TOOL_BATCH, TOOL_SCHEMAS, execute
from scripts.inspect_vapi_calls import TOOL_NAMES
from scripts.evaluate_forensic_regressions import SCHEDULING_TOOLS, SUPPORTED_PROMPT_VERSIONS
from test_receipt_narration import setup, issue, post, text_of, EN, PACIFIC, TE, confirm

FUSED = "prepare_first_available_booking"
INTAKE = {"strategy": "first_available", "service": "oil_change", "customer_name": "Alex Morgan",
          "phone": "5125550142", "vehicle": "2021 Toyota Camry"}


def test_fused_matches_legacy_proposal_without_reserving(tmp_path, monkeypatch):
    env = setup(tmp_path, monkeypatch)
    legacy_sid = env.store.create_session()["id"]
    offered = execute(env.store, legacy_sid, "begin_service_request", {"service": "oil_change"}, "legacy-search")
    legacy = execute(env.store, legacy_sid, "prepare_booking", {
        **{key: value for key, value in INTAKE.items() if key != "strategy"},
        "slot_id": offered["slots"][0]["id"], "presentation": PACIFIC}, "legacy-prepare")
    fused = execute(env.store, env.sid, FUSED, {**INTAKE, "presentation": PACIFIC}, "fused-prepare")
    try:
        assert fused["ok"]
        assert {key: value for key, value in fused.items() if key not in ("proposal_id", "expires_at")} == {
            key: value for key, value in legacy.items() if key not in ("proposal_id", "expires_at")}
        with env.store.read_connection() as db:
            payloads = [json.loads(db.execute("SELECT payload FROM proposals WHERE id=?", (result["proposal_id"],)).fetchone()[0]) for result in (legacy, fused)]
            assert payloads[0] == payloads[1]
            assert db.execute("SELECT COUNT(*) FROM appointments").fetchone()[0] == 0
            assert env.store.availability(db, "oil_change")[0]["remaining"] == offered["slots"][0]["remaining"]
        left, right = env.store.state(legacy_sid), env.store.state(env.sid)
        assert {key: value for key, value in left.items() if key != "proposal_id"} == {key: value for key, value in right.items() if key != "proposal_id"}
    finally:asyncio.run(env.gateway.close())


@pytest.mark.parametrize("change", [{"strategy": None}, {"strategy": "first_available_on_monday"},
                                  {"date": "2026-10-19"}, {"slot_id": "slot_2026-10-19_9"},
                                  {"day": "Monday"}, {"time_window": "morning"}, {"confirmed": True}])
def test_strict_selection_contract_has_no_date_slot_or_commit_shortcut(change):
    with pytest.raises(ValidationError):DEFINITIONS[FUSED][0].model_validate({**INTAKE, **change})
    with pytest.raises(ValidationError):DEFINITIONS[FUSED][0].model_validate({key: value for key, value in INTAKE.items() if key != "strategy"})


def test_catalog_fourteen_preserves_execution_batch_twelve():
    assert len(TOOL_SCHEMAS) == 14
    assert MAX_TOOL_BATCH == GatewayLimits().tool_calls == 12
    assert FUSED in TOOL_NAMES and FUSED in SCHEDULING_TOOLS
    assert "2026-10-18-gemini-agentic-8" in SUPPORTED_PROMPT_VERSIONS
    parameters = next(tool["function"]["parameters"] for tool in TOOL_SCHEMAS if tool["function"]["name"] == FUSED)
    assert parameters["properties"]["strategy"]["enum"] == ["first_available"]
    assert "strategy" in parameters["required"] and parameters["additionalProperties"] is False


def test_replay_and_restart_preserve_same_proposal_receipt(tmp_path, monkeypatch):
    env = setup(tmp_path, monkeypatch)
    async def scenario():
        try:
            first = await env.harness.run(env.sid, FUSED, INTAKE, "fused-once")
            second = await ToolHarness(env.store).run(env.sid, FUSED, INTAKE, "fused-once")
            assert first == second and first["ok"] and "_receipt" in first
            changed = await env.harness.run(env.sid, FUSED, {**INTAKE, "vehicle": "2022 Toyota Camry"}, "fused-once")
            assert changed["error"] == "idempotency_conflict"
            new_id = await env.harness.run(env.sid, FUSED, INTAKE, "fused-new-id")
            assert new_id["error"] == "first_available_not_fresh"
            with env.store.read_connection() as db:
                assert db.execute("SELECT count(*) FROM proposals WHERE session_id=?", (env.sid,)).fetchone()[0] == 1
                assert db.execute("SELECT count(*) FROM appointments").fetchone()[0] == 0
        finally:await env.gateway.close()
    asyncio.run(scenario())


def test_legacy_normalization_and_cached_fingerprint_remain_valid(tmp_path, monkeypatch):
    env = setup(tmp_path, monkeypatch)
    args = {"service": "oil_change", "slot_id": "slot_2026-10-19_9", "customer_name": "Alex Morgan",
            "phone": "5125550142", "vehicle": "2021 Toyota Camry"}
    assert DEFINITIONS["prepare_booking"][0].model_validate(args).model_dump() == args
    fingerprint = hashlib.sha256(json.dumps({"name": "prepare_booking", "args": args}, sort_keys=True).encode()).hexdigest()
    historical = {"ok": True, "proposal_id": "proposal-legacy", "summary": "Legacy preserved result", "expires_at": "2026-10-19T00:00:00+00:00", "instruction": "Read back before confirming."}
    with env.store.transaction() as db:
        db.execute("INSERT INTO tool_results VALUES (?,?,?,?)", (env.sid, "legacy-tool-id", fingerprint, json.dumps(historical)))
    async def scenario():
        try:
            result = await env.harness.run(env.sid, "prepare_booking", args, "legacy-tool-id")
            assert {key: value for key, value in result.items() if key != "_receipt"} == historical
            assert env.store.snapshot(env.sid)["proposal"] is None
        finally:await env.gateway.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("presentation", [EN, PACIFIC, TE, None])
def test_signed_fused_result_narration_and_next_generation_history(tmp_path, monkeypatch, presentation):
    env = setup(tmp_path, monkeypatch)
    async def scenario():
        try:
            args = {**INTAKE}
            if presentation is not None:args["presentation"] = presentation
            proposal = await issue(env, FUSED, args)
            assert proposal["ok"]
            # The same exact emitted call is authorized by the existing
            # signature gate; changed presentation/business args cannot borrow it.
            tool = env.history[-2]["tool_calls"][0]
            normalized = DEFINITIONS[FUSED][0].model_validate(args).model_dump()
            env.gateway.store.authorize_tool_batch(env.sid, env.cid, [(tool["id"], FUSED, normalized, args)])
            response = await post(env)
            optimized = presentation in (EN, PACIFIC)
            assert env.gateway.latency_snapshot(env.cid)[-1]["route"] == ("deterministic_receipt" if optimized else "gemini")
            if optimized:
                assert "Would you like me to confirm this booking?" in text_of(response)
                assert "Alex Morgan" in text_of(response)
                if presentation == PACIFIC:assert "7 AM Pacific Daylight Time" in text_of(response)
                assert not env.sent
            assert env.store.snapshot(env.sid)["appointment"] is None
            confirmed = await confirm(env, proposal, presentation)
            assert confirmed["ok"]
            final = await post(env)
            if optimized:
                assert confirmed["appointment"]["reference"] in text_of(final)
                assert not env.sent
            env.history.extend([{"role": "assistant", "content": text_of(final)}, {"role": "user", "content": "What can I change?"}])
            await post(env)
            forwarded = env.sent[-1]["messages"]
            fused_calls = [call for message in forwarded for call in message.get("tool_calls", []) if call["function"]["name"] == FUSED]
            assert len(fused_calls) == 1 and fused_calls[0]["extra_content"]["google"]["thought_signature"] == "test-signature-1"
            with env.store.read_connection() as db:
                assert db.execute("SELECT count(*) FROM appointments WHERE session_id=?", (env.sid,)).fetchone()[0] == 1
        finally:await env.gateway.close()
    asyncio.run(scenario())
