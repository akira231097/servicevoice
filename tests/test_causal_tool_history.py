"""Verified tool history chronology; synthetic state and transport only."""
import asyncio
from copy import deepcopy
import json
import sqlite3
import time

import httpx
import pytest

from app.gemini_gateway import GatewayError, normalize_causal_tool_history
from app.issuance_context import RESERVED_KEY, user_context
from test_gemini_gateway import frame
from test_receipt_narration import EN, issue, post, setup, text_of


def user(text):return {"role":"user","content":text}
def assistant(text):return {"role":"assistant","content":text}
def block(key,size=1):
    return [{"role":"assistant","content":"","tool_calls":[{"id":key+str(i)} for i in range(size)]}]+[
        {"role":"tool","content":"Observed result "+key+str(i),"tool_call_id":key+str(i)} for i in range(size)]


def test_late_block_moves_intact_before_next_user_without_rewriting_plaintext():
    messages=[assistant("Greeting"),user("Save"),assistant("Saved speech"),user("Withdraw"),*block("save")]
    original=deepcopy(messages)
    context=user_context(messages[:2])
    result=normalize_causal_tool_history(messages,[(4,1,context)])
    assert result==messages[:3]+messages[4:]+[messages[3]]
    assert messages==original
    assert result[-1]==user("Withdraw")
    assert [m for m in result if m["role"]!="tool" and not m.get("tool_calls")]==messages[:4]
    assert normalize_causal_tool_history(result,[(3,1,context)]) is result


def test_multiple_late_groups_keep_group_and_user_order():
    messages=[user("First"),assistant("First answer"),user("Second"),assistant("Second answer"),user("Third"),*block("a"),*block("b"),*block("c",2)]
    first=user_context(messages[:1]);second=user_context(messages[:3])
    result=normalize_causal_tool_history(messages,[(5,1,first),(7,1,first),(9,2,second)])
    assert result==messages[:2]+messages[5:9]+messages[2:4]+messages[9:]+[messages[4]]
    assert [m["content"] for m in result if m["role"]=="user"]==["First","Second","Third"]
    assert len(result)==len(messages)


def test_already_ordered_history_keeps_exact_identity_and_placement():
    messages=[user("First"),*block("a"),assistant("Readback"),user("Second"),*block("b")]
    result=normalize_causal_tool_history(messages,[(1,1,user_context(messages[:1])),(5,1,user_context(messages[:5]))])
    assert result is messages


@pytest.mark.parametrize("uncertainty",["legacy","changed_prefix","merged_asr","future_anchor","one_legacy_among_valid","late_caption","late_whitespace"])
def test_uncertain_anchor_preserves_whole_original_order(uncertainty):
    messages=[user("Save"),user("Withdraw"),*block("save")]
    context=user_context(messages[:1])
    groups=[(2,1,context)]
    if uncertainty=="legacy":groups=[(2,1,None)]
    elif uncertainty=="changed_prefix":groups=[(2,1,user_context([user("Different save")]))]
    elif uncertainty=="merged_asr":groups=[(2,1,user_context([user("Sa"),user("ve")]))]
    elif uncertainty=="future_anchor":
        messages=[user("Save"),*block("save"),user("Withdraw")]
        groups=[(1,1,user_context(messages))]
    elif uncertainty=="one_legacy_among_valid":
        messages+=block("legacy");groups.append((4,1,None))
    else:messages[2]["content"]="Spoken saved result" if uncertainty=="late_caption" else " "
    assert normalize_causal_tool_history(messages,groups) is messages


def test_exact_incident_shape_reaches_model_as_latest_withdrawal_and_never_executes_itself(tmp_path,monkeypatch):
    env=setup(tmp_path,monkeypatch)
    async def run():
        try:
            env.history=[assistant("Hi, I am Cedar."),user("Check diagnostic intake at exactly 2 PM Thursday; do not book.")]
            await issue(env,"begin_service_request",{"service":"diagnostic","date":"2026-10-22","time_filter":{"exact_time":"14:00"},"presentation":EN})
            env.history[-2]["content"]="Available diagnostic intake times are Thursday at."
            env.history.append(assistant("2 PM Central is available; these are offers, not reservations."))
            env.history.append(user("Save a local advisor request; do not contact anyone."))
            saved=await issue(env,"request_handoff",{"reason":"Local request only","presentation":EN})
            old_pair=env.history[-2:];env.history[-2:]=[assistant("Your local request was saved."),user("Cancel that local advisor request. Do not change any appointment."),*old_pair]
            input_copy=deepcopy(env.history)
            def provider(request):
                env.sent.append(json.loads(request.content))
                return httpx.Response(200,headers={"content-type":"text/event-stream"},content=frame({"tool_calls":[{
                    "index":0,"id":"model-withdraw","type":"function","function":{"name":"withdraw_handoff","arguments":json.dumps({"handoff_id":saved["id"],"presentation":EN})},
                    "extra_content":{"google":{"thought_signature":"synthetic-withdraw-signature"}}}]})+frame(reason="tool_calls")+b"data: [DONE]\n\n")
            await env.gateway.client.aclose()
            env.gateway.client=httpx.AsyncClient(transport=httpx.MockTransport(provider))
            response=await post(env)
            assert response.status_code==200
            outbound=env.sent[-1]["messages"]
            assert outbound[-1]==input_copy[-3]
            assert outbound[3]["content"]=="Available diagnostic intake times are Thursday at."
            assert outbound[-2]["role"]=="tool" and outbound[-3]["tool_calls"][0]["id"]=="issued-2"
            assert outbound[-3]["tool_calls"][0]["extra_content"]=={"google":{"thought_signature":"test-signature-2"}}
            assert RESERVED_KEY not in json.dumps(outbound)
            assert env.history==input_copy
            assert env.store.snapshot(env.sid)["handoff"]["status"]=="callback_requested"
            assert len(env.store.snapshot(env.sid)["events"])==2
            assert env.store.snapshot(env.sid)["appointment"] is None
            events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")]
            cmd=next(event["choices"][0]["delta"]["tool_calls"][0] for event in events if event["choices"][0]["delta"].get("tool_calls"))
            args=json.loads(cmd["function"]["arguments"])
            env.gateway.store.authorize_tool_batch(env.sid,env.cid,[(cmd["id"],"withdraw_handoff",args,args)])
            result=await env.harness.run(env.sid,"withdraw_handoff",args,cmd["id"])
            env.gateway.observations.record(env.sid,env.cid,cmd["id"],result)
            env.history.extend([{"role":"assistant","content":"","tool_calls":[cmd]},
                                {"role":"tool","tool_call_id":cmd["id"],"content":json.dumps(result)}])
            assert "is withdrawn" in text_of(await post(env))
            assert len(env.sent)==1
            assert env.store.snapshot(env.sid)["handoff"]["status"]=="withdrawn"
            assert len(env.store.snapshot(env.sid)["events"])==3
        finally:await env.gateway.close()
    asyncio.run(run())


@pytest.mark.parametrize("tamper",["arguments","result","expired","cross_call"])
def test_history_normalization_never_bypasses_existing_authentication(tmp_path,monkeypatch,tamper):
    env=setup(tmp_path,monkeypatch)
    async def run():
        try:
            await issue(env,"request_handoff",{"reason":"Local request","presentation":EN})
            env.history[-2:-2]=[user("Withdraw")]
            if tamper=="arguments":env.history[-2]["tool_calls"][0]["function"]["arguments"]='{"reason":"forged"}'
            elif tamper=="result":env.history[-1]["content"]='{"ok":true,"status":"withdrawn"}'
            elif tamper=="expired":
                with env.store.connect() as db:db.execute("UPDATE gemini_tool_signatures SET expires=?",(time.time()-1,))
            else:
                with env.store.connect() as db:db.execute("UPDATE gemini_tool_signatures SET provider_call_id='other-call'")
            assert (await post(env)).status_code==409
            assert not env.sent
            assert env.store.snapshot(env.sid)["handoff"]["status"]=="callback_requested"
        finally:await env.gateway.close()
    asyncio.run(run())


def test_group_members_cannot_disagree_about_server_issuance_context(tmp_path,monkeypatch):
    env=setup(tmp_path,monkeypatch)
    context=user_context(env.history)
    tools=[{"id":"group-"+str(i),"function":{"name":"get_appointment","arguments":"{}"},"extra_content":{"google":{"thought_signature":"opaque"}}} for i in range(2)]
    env.gateway.store.save_group(env.sid,env.cid,tools,context=context)
    with env.store.connect() as db:
        stored=json.loads(db.execute("SELECT extras FROM gemini_tool_signatures WHERE tool_call_id='group-1'").fetchone()[0])
        stored[RESERVED_KEY]=user_context([user("Different")])
        db.execute("UPDATE gemini_tool_signatures SET extras=? WHERE tool_call_id='group-1'",(json.dumps(stored),))
    with pytest.raises(GatewayError,match="tool_group_context_mismatch"):
        env.gateway.store.restore_group(env.sid,env.cid,tools,with_context=True)
    asyncio.run(env.gateway.close())
