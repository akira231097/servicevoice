"""Explicit model rendering decisions without new effect or consent authority.

These tests verify schema delivery and real local signed-receipt behavior.
They do not establish whether a remote model follows the schema or measure audio.
"""
import asyncio
from copy import deepcopy
import json

import pytest

from app.gemini_gateway import tool_definitions
from app.receipt_narration import SUPPORTED, candidate, select
from app.tools import DEFINITIONS, SYSTEM_PROMPT, TOOL_SCHEMAS
from scripts.provision_vapi import normalize_tools
from test_receipt_narration import EN, PACIFIC, confirm, issue, post, prepare, setup, text_of


@pytest.mark.parametrize("name", sorted(SUPPORTED))
def test_provider_requires_object_or_null_but_backend_keeps_legacy_optional(name):
    model = DEFINITIONS[name][0]
    raw = model.model_json_schema()
    assert "presentation" not in raw.get("required", [])
    schemas = {tool["function"]["name"]: tool["function"]["parameters"] for tool in TOOL_SCHEMAS}
    public = schemas[name]
    assert public["required"].count("presentation") == 1
    field = public["properties"]["presentation"]
    assert field["type"] == ["object", "null"] and "default" not in field
    assert field["additionalProperties"] is False
    assert set(field["required"]) == {"response_scope", "response_language", "caller_timezone"}
    assert field["properties"]["response_scope"]["enum"] == ["receipt_only"]
    # Language is an unrestricted validated tag, never an English-only enum.
    assert "enum" not in field["properties"]["response_language"]
    explicit_decisions={"presentation"}
    if name in {"begin_service_request","search_availability","prepare_first_available_booking","prepare_booking","prepare_reschedule"}:
        explicit_decisions.add("services")
        assert "services" not in raw.get("required", [])
        assert public["required"].count("services")==1
        assert public["properties"]["services"]["type"]==["array","null"]
        assert "default" not in public["properties"]["services"]
    assert set(public.get("required", [])) - explicit_decisions == set(raw.get("required", []))
    assert set(public["properties"]) == set(raw["properties"])


def test_same_contract_reaches_vapi_and_authoritative_gateway_without_caller_defaults():
    supplied = deepcopy(TOOL_SCHEMAS)
    for tool in supplied:
        parameters = tool["function"]["parameters"]
        if "presentation" in parameters["properties"]:
            parameters["required"].remove("presentation")
            parameters["properties"]["presentation"]["default"] = EN
    assert tool_definitions(supplied) == TOOL_SCHEMAS
    assert [tool["function"] for tool in normalize_tools(TOOL_SCHEMAS)] == [tool["function"] for tool in TOOL_SCHEMAS]
    presentable = {name for name, (model, _) in DEFINITIONS.items() if "presentation" in model.model_fields}
    assert presentable == SUPPORTED
    for tool in TOOL_SCHEMAS:
        if tool["function"]["name"] not in presentable:
            assert "presentation" not in tool["function"]["parameters"].get("required", [])
    assert SYSTEM_PROMPT.count('<presentation_metadata>') == 1
    assert "Set presentation to null" in SYSTEM_PROMPT
    assert "Omit presentation" not in SYSTEM_PROMPT


def test_explicit_null_replays_old_omitted_arguments_without_repeating_effect(tmp_path, monkeypatch):
    env = setup(tmp_path, monkeypatch)
    async def scenario():
        try:
            proposal = await prepare(env, None)
            args = {"proposal_id": proposal["proposal_id"], "confirmed": True}
            first = await env.harness.run(env.sid, "confirm_action", args, "legacy-confirm")
            repeated = await env.harness.run(env.sid, "confirm_action", {**args, "presentation": None}, "legacy-confirm")
            assert first == repeated and first["ok"] and "presentation" not in first
            with env.store.read_connection() as db:
                assert db.execute("SELECT count(*) FROM appointments WHERE session_id=?", (env.sid,)).fetchone()[0] == 1
                assert db.execute("SELECT count(*) FROM tool_results WHERE session_id=? AND call_id='legacy-confirm'", (env.sid,)).fetchone()[0] == 1
            assert DEFINITIONS["confirm_action"][0].model_validate({**args, "presentation": None}).model_dump() == args
        finally:
            await env.harness.close()
            await env.gateway.close()
    asyncio.run(scenario())


def test_signed_availability_correction_and_later_confirmation_omit_narration_rounds(tmp_path, monkeypatch):
    env = setup(tmp_path, monkeypatch)
    async def scenario():
        try:
            async def speak():
                response = await post(env)
                assert response.status_code == 200
                text = text_of(response)
                env.history.append({"role": "assistant", "content": text})
                return text

            await issue(env, "begin_service_request", {"service": "diagnostic", "date": "2026-10-22", "presentation": EN})
            assert "offers, not reservations" in await speak()
            env.history.append({"role": "user", "content": "What about exactly two PM that day, in dealership time?"})
            offers = await issue(env, "search_availability", {"service": "diagnostic", "date": "2026-10-22",
                "time_filter": {"exact_time": "14:00"}, "presentation": EN})
            assert "2 PM Central Daylight Time" in await speak()
            env.history.append({"role": "user", "content": "Use that time. Alex Morgan, 5125500142, 2021 Toyota Camry. Read it back before booking."})
            args = {"service": "diagnostic", "slot_id": offers["slots"][0]["id"], "customer_name": "Alex Morgan",
                    "phone": "5125500142", "vehicle": "2021 Toyota Camry", "presentation": EN}
            old = await issue(env, "prepare_booking", args)
            old_candidate = candidate(env.history)
            assert "confirm this booking?" in await speak()
            assert env.store.snapshot(env.sid)["appointment"] is None
            env.history.append({"role": "user", "content": "Correct the callback to 5125550142 and the vehicle to 2022 Toyota Camry. Please also show my Pacific equivalent."})
            corrected = await issue(env, "prepare_booking", {**args, "slot_id": old["slot_id"], "phone": "5125550142",
                "vehicle": "2022 Toyota Camry", "presentation": PACIFIC})
            assert select(env.store, env.sid, env.cid, old_candidate).text is None
            readback = await speak()
            for expected in ("Alex Morgan", "2022 Toyota Camry", "five, five, five; zero, one, four, two",
                             "2 PM Central Daylight Time", "12 PM Pacific Daylight Time", "confirm this booking?"):
                assert expected in readback
            assert "2021 Toyota Camry" not in readback
            assert env.store.snapshot(env.sid)["appointment"] is None
            result = await confirm(env, corrected, PACIFIC)
            final = await speak()
            assert result["appointment"]["reference"] in final
            assert result["appointment"]["vehicle"] == "2022 Toyota Camry"
            assert not env.sent, "All five verified receipt continuations must skip a second model generation"
            rows = env.gateway.latency_snapshot(env.cid)
            assert len(rows) == 5 and all(row["route"] == "deterministic_receipt" for row in rows)
            assert all(row["counts"]["upstream_events"] == 0 for row in rows)
            with env.store.read_connection() as db:
                assert db.execute("SELECT count(*) FROM appointments WHERE session_id=?", (env.sid,)).fetchone()[0] == 1
                customer = json.loads(db.execute("SELECT customer FROM appointments WHERE session_id=?", (env.sid,)).fetchone()[0])
                assert customer["phone"] == "+15125550142"
                states = {row["id"]: row["state"] for row in db.execute("SELECT id,state FROM proposals WHERE session_id=?", (env.sid,))}
                assert states[old["proposal_id"]] == "superseded" and states[corrected["proposal_id"]] == "committed"
        finally:
            await env.harness.close()
            await env.gateway.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("presentation,user_text,reason", [
    (None, "Yes, book it. Also, does the warranty pay for it?", "presentation_missing_or_changed"),
    ({**EN, "response_language": "es"}, "Sí, confirma la reserva y responde en español.", "unsupported_language"),
    ({**EN, "response_language": "hi"}, "हाँ, बुक करें और हिंदी में जवाब दें।", "unsupported_language"),
])
def test_current_explicit_decision_never_inherits_previous_english_receipt(tmp_path, monkeypatch, presentation, user_text, reason):
    env = setup(tmp_path, monkeypatch)
    async def scenario():
        try:
            proposal = await prepare(env, EN)
            readback = text_of(await post(env))
            env.history.extend([{"role": "assistant", "content": readback}, {"role": "user", "content": user_text}])
            result = await issue(env, "confirm_action", {"proposal_id": proposal["proposal_id"], "confirmed": True, "presentation": presentation})
            assert result["ok"]
            assert text_of(await post(env)) == "Gemini fallback"
            assert env.gateway.latency_snapshot(env.cid)[-1]["narration_reason"] == reason
            assert len(env.sent) == 1
            # Fallback receives the complete latest request; no preferences or
            # already-committed business result are rewritten to force the route.
            assert any(message.get("content") == user_text for message in env.sent[0]["messages"])
            echoed = result.get("presentation")
            assert echoed == presentation
        finally:
            await env.harness.close()
            await env.gateway.close()
    asyncio.run(scenario())
