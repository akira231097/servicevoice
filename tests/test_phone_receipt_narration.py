"""Exact phone presentation through signed receipt history; no acoustic claim."""
import asyncio
import json
import re

import pytest

from app.receipt_narration import NARRATOR_VERSION, candidate, select, spoken_phone
from test_receipt_narration import EN, issue, post, setup, text_of

WORDS = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
         "six": "6", "seven": "7", "eight": "8", "nine": "9"}
INTAKE = {"service": "oil_change", "customer_name": "Alex Morgan", "vehicle": "2021 Toyota Camry", "presentation": EN}


def decode_phone_span(span):
    """A North American readback omits the implied +1; anything else says "plus"."""
    international = span.startswith("plus ")
    words = re.findall(r"[a-z]+", span.removeprefix("plus "))
    assert words and all(word in WORDS for word in words)
    assert not re.search(r"[0-9]", span)
    return ("+" if international else "+1") + "".join(WORDS[word] for word in words)


def callback_span(text):
    return text.split("Callback ", 1)[1].split(". Would you like me to confirm this booking?", 1)[0]


@pytest.mark.parametrize("phone", [
    "+15125550142", "+15125500142", "+10123456789", "+10000000000", "+11111111111",
    "+442071838750", "+919876543210",
])
def test_each_digit_round_trips_without_dropping_country_repeats_or_zeros(phone):
    rendered = spoken_phone(phone)
    assert decode_phone_span(rendered) == phone
    assert ";" not in rendered
    if phone.startswith("+1") and len(phone) == 12:
        # Spoken the way people say a US number: no "plus one", three runs of digits.
        assert not rendered.startswith("plus")
        assert [len(group.split(" ")) for group in rendered.split(", ")] == [3, 3, 4]
    else:
        assert rendered.startswith("plus ") and "," not in rendered  # Do not guess other countries' groups.
        assert len(re.findall(r"[a-z]+", rendered)) - 1 == len(phone) - 1


@pytest.mark.parametrize("phone", [None, "15125550142", "++15125550142", "+1 5125550142", "+15125550142 ext 5",
                                   "+1５１２５５５０１４２", "+1234567890123456", "+01234567890"])
def test_unsupported_shapes_are_not_silently_repaired(phone):
    assert spoken_phone(phone) is None


@pytest.mark.parametrize("phone", ["5125550142", "5125500142", "0123456789", "0000000000"])
def test_signed_fused_readback_preserves_phone_and_immutable_result(tmp_path, monkeypatch, phone):
    env = setup(tmp_path, monkeypatch)

    async def scenario():
        try:
            result = await issue(env, "prepare_first_available_booking", {**INTAKE, "phone": phone, "strategy": "first_available"})
            assert result["ok"]
            before = env.store.snapshot(env.sid)
            history_before = json.dumps(env.history, sort_keys=True)
            with env.store.read_connection() as db:
                receipt_before = db.execute("SELECT result FROM tool_results WHERE session_id=? ORDER BY rowid DESC LIMIT 1", (env.sid,)).fetchone()[0]
            rendered = text_of(await post(env))
            assert decode_phone_span(callback_span(rendered)) == "+1" + phone
            assert env.store.snapshot(env.sid) == before
            assert env.store.snapshot(env.sid)["appointment"] is None
            assert json.dumps(env.history, sort_keys=True) == history_before
            with env.store.read_connection() as db:
                assert db.execute("SELECT result FROM tool_results WHERE session_id=? ORDER BY rowid DESC LIMIT 1", (env.sid,)).fetchone()[0] == receipt_before
            timing = env.gateway.latency_snapshot(env.cid)[-1]
            assert timing["route"] == "deterministic_receipt" and timing["narrator_version"] == NARRATOR_VERSION
            assert not env.sent
        finally:
            await env.gateway.close()

    asyncio.run(scenario())


def test_signed_phone_correction_reads_fresh_repeated_digits_without_booking(tmp_path, monkeypatch):
    env = setup(tmp_path, monkeypatch)

    async def scenario():
        try:
            original = await issue(env, "prepare_first_available_booking", {**INTAKE, "phone": "5125500142", "strategy": "first_available"})
            original_candidate = candidate(env.history)
            initial_readback = text_of(await post(env))
            assert decode_phone_span(callback_span(initial_readback)) == "+15125500142"
            slot = env.store.state(env.sid)["selected_slot"]
            env.history.extend([{"role": "assistant", "content": initial_readback},
                                {"role": "user", "content": "Correct only my callback number to5125550142. Read it back before booking."}])
            corrected = await issue(env, "prepare_booking", {**INTAKE, "phone": "5125550142", "slot_id": slot})
            assert corrected["proposal_id"] != original["proposal_id"]
            assert select(env.store, env.sid, env.cid, original_candidate).reason == "newer_result"
            final_readback = text_of(await post(env))
            assert decode_phone_span(callback_span(final_readback)) == "+15125550142"
            with env.store.read_connection() as db:
                rows = db.execute("SELECT id,state,payload FROM proposals WHERE session_id=? ORDER BY rowid", (env.sid,)).fetchall()
                assert [(row["id"], row["state"]) for row in rows] == [(original["proposal_id"], "superseded"), (corrected["proposal_id"], "pending")]
                assert [json.loads(row["payload"])["customer"]["phone"] for row in rows] == ["+15125500142", "+15125550142"]
                assert db.execute("SELECT count(*) FROM appointments WHERE session_id=?", (env.sid,)).fetchone()[0] == 0
            assert not env.sent
        finally:
            await env.gateway.close()

    asyncio.run(scenario())


def test_domain_accepted_unicode_digits_fall_back_without_storage_rewrite(tmp_path, monkeypatch):
    env = setup(tmp_path, monkeypatch)

    async def scenario():
        try:
            raw_phone = "５１２５５５０１４２"
            result = await issue(env, "prepare_first_available_booking", {**INTAKE, "phone": raw_phone, "strategy": "first_available"})
            assert result["ok"]
            assert env.store.state(env.sid)["customer"]["phone"] == "+1" + raw_phone
            response = await post(env)
            assert "Gemini fallback" in text_of(response)
            assert env.gateway.latency_snapshot(env.cid)[-1]["narration_reason"] == "unsupported_phone_format"
            assert env.store.state(env.sid)["customer"]["phone"] == "+1" + raw_phone
            assert env.store.snapshot(env.sid)["appointment"] is None
        finally:
            await env.gateway.close()

    asyncio.run(scenario())
