"""Regression for Vapi moving an old tool/result tail after a new caller turn."""
import asyncio
import json

from test_receipt_narration import EN, issue, post, setup, text_of


def test_reordered_old_save_receipt_cannot_answer_new_withdrawal_request(tmp_path, monkeypatch):
    env = setup(tmp_path, monkeypatch)
    async def scenario():
        try:
            result = await issue(env, "request_handoff", {"reason": "Please save a local advisor request", "presentation": EN})
            assert "was saved" in text_of(await post(env))
            # Actual incident shape: the previous signed pair appears after a
            # later caller instruction although its durable result is unchanged.
            env.history[-2:-2] = [{"role": "user", "content": "Cancel that local advisor request. Do not change any appointment."}]
            response = await post(env)
            assert text_of(response) == "Gemini fallback"
            assert env.gateway.latency_snapshot(env.cid)[-1]["narration_reason"] == "not_immediate_tool_result"
            assert env.sent[-1]["messages"][-1]["content"].startswith("Cancel that local advisor request")
            assert env.sent[-1]["messages"][-2]["role"] == "tool"
            assert env.store.snapshot(env.sid)["handoff"]["status"] == "callback_requested"
            assert len(env.store.snapshot(env.sid)["events"]) == 1
            # A genuinely new withdrawal result is eligible in the new context.
            await issue(env, "withdraw_handoff", {"handoff_id": result["id"], "presentation": EN})
            assert "is withdrawn" in text_of(await post(env))
            assert len(env.sent) == 1
        finally: await env.gateway.close()
    asyncio.run(scenario())


def test_identical_receipt_continuation_retry_preserves_text_and_does_not_repeat_effect(tmp_path, monkeypatch):
    env = setup(tmp_path, monkeypatch)
    async def scenario():
        try:
            await issue(env, "request_handoff", {"reason": "Please ask an advisor", "presentation": EN})
            first = text_of(await post(env))
            second = text_of(await post(env))
            assert first == second and not env.sent
            assert len(env.store.snapshot(env.sid)["events"]) == 1
        finally: await env.gateway.close()
    asyncio.run(scenario())


def test_legacy_opaque_signature_restores_but_does_not_enable_unanchored_narration(tmp_path, monkeypatch):
    env = setup(tmp_path, monkeypatch)
    async def scenario():
        try:
            await issue(env, "request_handoff", {"reason": "Please ask an advisor", "presentation": EN})
            with env.store.connect() as db:
                db.execute("UPDATE gemini_tool_signatures SET extras=? WHERE provider_call_id=?", (json.dumps({"google": {"thought_signature": "test-signature-1"}}), env.cid))
            assert text_of(await post(env)) == "Gemini fallback"
            row = env.gateway.latency_snapshot(env.cid)[-1]
            assert row["narration_reason"] == "issuance_context_missing"
            actual = env.sent[-1]["messages"][-2]["tool_calls"][0]["extra_content"]
            assert actual == {"google": {"thought_signature": "test-signature-1"}}
        finally: await env.gateway.close()
    asyncio.run(scenario())
