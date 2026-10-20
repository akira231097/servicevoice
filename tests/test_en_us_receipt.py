"""Exact en-US narration through signed gateway history; no provider network."""
import asyncio
import json

import pytest

from app.receipt_narration import NARRATOR_VERSION, candidate, select
from test_receipt_narration import EN, confirm, issue, post, prepare, setup, text_of

EN_US = {**EN, "response_language": "en-US"}
INTAKE = {"service": "oil_change", "customer_name": "Alex Morgan", "phone": "5125550142", "vehicle": "2021 Toyota Camry"}


@pytest.mark.parametrize("mode,zone", [("legacy", "dealership"), ("fused", "America/Chicago"), ("fused", "America/Los_Angeles")])
def test_en_us_proposal_correction_confirmation_keep_exact_receipts_and_signatures(tmp_path, monkeypatch, mode, zone):
    env = setup(tmp_path, monkeypatch)
    context = {**EN_US, "caller_timezone": zone}

    async def scenario():
        try:
            proposal = await prepare(env, context) if mode == "legacy" else await issue(env, "prepare_first_available_booking", {
                **INTAKE, "strategy": "first_available", "presentation": context})
            old = candidate(env.history)
            first = text_of(await post(env))
            assert "2021 Toyota Camry" in first and "Monday, October 19, 2026" in first
            assert "Would you like me to confirm this booking?" in first
            assert env.store.snapshot(env.sid)["appointment"] is None
            slot = env.store.state(env.sid)["selected_slot"]
            env.history.extend([{"role": "assistant", "content": first},
                                {"role": "user", "content": "Correct the vehicle to2022 before I confirm."}])
            corrected = await issue(env, "prepare_booking", {**INTAKE, "vehicle": "2022 Toyota Camry", "slot_id": slot, "presentation": context})
            assert corrected["proposal_id"] != proposal["proposal_id"]
            assert select(env.store, env.sid, env.cid, old).reason == "newer_result"
            revised = text_of(await post(env))
            assert "2022 Toyota Camry" in revised and "2021 Toyota Camry" not in revised
            assert "plus one; five, one, two; five, five, five; zero, one, four, two" in revised
            assert env.store.snapshot(env.sid)["appointment"] is None
            if zone == "America/Los_Angeles":
                assert "9 AM Central Daylight Time" in revised and "7 AM Pacific Daylight Time" in revised
            result = await confirm(env, corrected, context)
            final = text_of(await post(env))
            assert result["appointment"]["reference"] in final and "confirmed" in final
            assert result["appointment"]["vehicle"] == "2022 Toyota Camry"
            assert result["presentation"] == corrected["presentation"] == proposal["presentation"] == context
            assert not env.sent
            rows = env.gateway.latency_snapshot(env.cid)
            assert len(rows) == 3 and NARRATOR_VERSION == "receipt-en-2026-10-20-7"
            assert all(row["route"] == "deterministic_receipt" and row["narrator_version"] == NARRATOR_VERSION for row in rows)
            assert all(row["counts"]["upstream_events"] == 0 for row in rows)
            for message in env.history:
                for tool in message.get("tool_calls", []):
                    arguments = json.loads(tool["function"]["arguments"])
                    if "presentation" in arguments:
                        assert arguments["presentation"] == context
            # Both tags are eligible, but replacing one in a signed invocation
            # still changes its identity. Do not canonicalize away this mismatch.
            def tamper(body):
                function = body["messages"][-2]["tool_calls"][0]["function"]
                arguments = json.loads(function["arguments"])
                arguments["presentation"]["response_language"] = "en"
                function["arguments"] = json.dumps(arguments)
            rejected = await post(env, tamper)
            assert rejected.status_code == 409 and not env.sent
            with env.store.read_connection() as db:
                assert db.execute("SELECT count(*) FROM appointments WHERE session_id=?", (env.sid,)).fetchone()[0] == 1
        finally:
            await env.gateway.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("language", ["en-GB", "en-AU", "en-us", "en-Latn-US", "te", "es"])
def test_other_language_tags_still_use_model_fallback(tmp_path, monkeypatch, language):
    env = setup(tmp_path, monkeypatch)

    async def scenario():
        try:
            proposal = await prepare(env, EN_US)
            await confirm(env, proposal, {**EN_US, "response_language": language})
            assert "Gemini fallback" in text_of(await post(env))
            assert env.gateway.latency_snapshot(env.cid)[-1]["narration_reason"] == "unsupported_language"
            assert len(env.sent) == 1
        finally:
            await env.gateway.close()

    asyncio.run(scenario())


def test_latest_mixed_question_without_metadata_never_inherits_en_us_fast_path(tmp_path, monkeypatch):
    env = setup(tmp_path, monkeypatch)

    async def scenario():
        try:
            proposal = await prepare(env, EN_US)
            await confirm(env, proposal, None)
            assert env.history[-3]["role"] == "user"
            env.history[-3]["content"] = "Yes, confirm. Also, what does warranty cover?"
            assert "Gemini fallback" in text_of(await post(env))
            assert env.gateway.latency_snapshot(env.cid)[-1]["narration_reason"] == "presentation_missing_or_changed"
            assert len(env.sent) == 1
        finally:
            await env.gateway.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("field,value,reason", [
    ("result_language", "en", "presentation_missing_or_changed"),
    ("timezone", "guess", "presentation_invalid"),
    ("timezone", None, "presentation_invalid"),
])
def test_en_us_does_not_bypass_exact_echo_or_timezone_validation(tmp_path, monkeypatch, field, value, reason):
    env = setup(tmp_path, monkeypatch)

    async def scenario():
        try:
            await prepare(env, EN_US)
            if field == "result_language":
                result = json.loads(env.history[-1]["content"])
                result["presentation"]["response_language"] = value
                env.history[-1]["content"] = json.dumps(result)
            else:
                function = env.history[-2]["tool_calls"][0]["function"]
                arguments = json.loads(function["arguments"])
                if value is None:
                    del arguments["presentation"]["caller_timezone"]
                else:
                    arguments["presentation"]["caller_timezone"] = value
                function["arguments"] = json.dumps(arguments)
            decision = candidate(env.history)
            assert decision.reason == reason and decision.text is None
            assert env.store.snapshot(env.sid)["appointment"] is None
        finally:
            await env.gateway.close()

    asyncio.run(scenario())
