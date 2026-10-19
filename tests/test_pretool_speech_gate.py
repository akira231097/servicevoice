"""A tool generation may issue commands, never pre-claim their outcome."""
import asyncio
from contextlib import closing
import json
import sqlite3
import time
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest

from app.domain import Store
from app.gemini_gateway import install_gateway, sse
from app.harness import ToolHarness
from app.issuance_context import decode_extras, user_context
from app.tools import DEFINITIONS, execute
from test_gemini_gateway import KEY, SECRET, SIG, frame, post, request_body, setup

CLAIM="The local advisor request has been withdrawn. Any appointment is unchanged."
DONE=b"data: [DONE]\n\n"

def command(name="get_appointment",arguments="{}"):
    return {"index":0,"id":"tool-speech-gate","type":"function","function":{"name":name,"arguments":arguments},
            "extra_content":{"google":{"thought_signature":SIG,"additional_provider_metadata":"preserve exactly"}}}

def events(data):
    return [json.loads(line[6:]) for line in data.splitlines() if line.startswith("data: {")]

def speech(data):
    return "".join(event["choices"][0]["delta"].get("content","") for event in events(data) if "choices" in event)

def issued(data):
    return [tool for event in events(data) if "choices" in event for tool in event["choices"][0]["delta"].get("tool_calls",[])]

def persisted_count(path):
    with closing(sqlite3.connect(path)) as db:
        return db.execute("SELECT COUNT(*) FROM gemini_tool_signatures").fetchone()[0]

@pytest.mark.parametrize("layout",["same_frame","prose_then_tool","tool_then_prose"])
def test_tool_generation_drops_all_freeform_prose_but_keeps_command_and_signature(tmp_path,layout):
    if layout=="same_frame":chunks=[frame({"content":CLAIM,"tool_calls":[command()]})]
    elif layout=="prose_then_tool":chunks=[frame({"content":CLAIM}),frame({"tool_calls":[command()]})]
    else:chunks=[frame({"tool_calls":[command()]}),frame({"content":CLAIM})]
    content=b"".join([*chunks,frame(reason="tool_calls"),DONE])
    app,gateway,path=setup(tmp_path,lambda _:httpx.Response(200,headers={"content-type":"text/event-stream"},content=content))
    async def run():
        try:
            response=await post(app,request_body())
            assert response.status_code==200 and speech(response.text)==""
            assert CLAIM not in response.text and SIG not in response.text
            calls=issued(response.text)
            assert len(calls)==1 and calls[0]["function"]==command()["function"]
            assert [event["choices"][0]["finish_reason"] for event in events(response.text) if "choices" in event][-1]=="tool_calls"
            assert response.text.endswith("data: [DONE]\n\n") and persisted_count(path)==1
            restored=gateway.store.restore_group("sid-a","call-a",calls)
            assert restored[0]["extra_content"]==command()["extra_content"]
            with closing(sqlite3.connect(path)) as db:
                stored=json.loads(db.execute("SELECT extras FROM gemini_tool_signatures").fetchone()[0])
            assert decode_extras(stored)[1]==user_context(request_body()["messages"])
            assert not gateway.active
        finally:await gateway.close()
    asyncio.run(run())

@pytest.mark.parametrize("failure",["provider_error","bad_json","malformed_tool","done_before_finish","finish_without_done","incomplete_tool","length_finish"])
def test_upstream_failure_never_releases_buffered_claim_or_command(tmp_path,failure):
    chunks=[frame({"content":CLAIM})]
    if failure=="provider_error":chunks += [sse({"error":{"message":"PRIVATE UPSTREAM DETAILS"}}),DONE]
    elif failure=="bad_json":chunks += [b'data: {invalid}\n\n',DONE]
    elif failure=="malformed_tool":chunks += [frame({"tool_calls":[command(arguments="{")]}),frame(reason="tool_calls"),DONE]
    elif failure=="done_before_finish":chunks += [DONE]
    elif failure=="finish_without_done":chunks += [frame(reason="stop")]
    elif failure=="incomplete_tool":chunks += [frame({"tool_calls":[{"index":0,"function":{"name":"get_appointment","arguments":"{}"}}]}),frame(reason="tool_calls"),DONE]
    else:chunks += [frame(reason="length"),DONE]
    app,gateway,path=setup(tmp_path,lambda _:httpx.Response(200,headers={"content-type":"text/event-stream"},content=b"".join(chunks)))
    async def run():
        try:
            response=await post(app,request_body())
            assert "generation_unavailable" in response.text
            assert speech(response.text)=="" and issued(response.text)==[]
            assert CLAIM not in response.text and "PRIVATE" not in response.text and "[DONE]" not in response.text
            assert persisted_count(path)==0 and not gateway.active
        finally:await gateway.close()
    asyncio.run(run())

def test_transport_read_error_after_text_does_not_leak_partial_prose(tmp_path):
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield frame({"content":CLAIM})
            raise httpx.ReadError("PRIVATE TRANSPORT ERROR")
    app,gateway,path=setup(tmp_path,lambda _:httpx.Response(200,headers={"content-type":"text/event-stream"},stream=BrokenStream()))
    async def run():
        try:
            response=await post(app,request_body())
            assert speech(response.text)=="" and "generation_unavailable" in response.text
            assert CLAIM not in response.text and "PRIVATE" not in response.text and "[DONE]" not in response.text
            assert persisted_count(path)==0 and not gateway.active
        finally:await gateway.close()
    asyncio.run(run())

@pytest.mark.parametrize("later_tool",[True,False])
def test_delayed_finish_or_tool_cannot_release_an_early_text_chunk(tmp_path,later_tool):
    app,gateway,path=setup(tmp_path,lambda _:httpx.Response(500))
    async def run():
        reached_pause=asyncio.Event();release=asyncio.Event()
        class PausedStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield frame({"content":"First unchanged sentence. "})
                # This event is reached only when relay asks for more upstream
                # data instead of yielding that premature prose downstream.
                reached_pause.set()
                await release.wait()
                if later_tool:yield frame({"tool_calls":[command()]})
                else:yield frame({"content":"Second sentence తెలుగు."})
                yield frame(reason="tool_calls" if later_tool else "stop")
                yield DONE
        class Request:
            async def is_disconnected(self):return False
        response=httpx.Response(200,headers={"content-type":"text/event-stream"},stream=PausedStream())
        gateway.active.add("call-a")
        relay=gateway._relay(Request(),response,"sid-a","call-a",time.monotonic(),issuance_context=user_context(request_body()["messages"]))
        first=asyncio.create_task(anext(relay));pause=asyncio.create_task(reached_pause.wait())
        try:
            done,_=await asyncio.wait((first,pause),timeout=2,return_when=asyncio.FIRST_COMPLETED)
            assert pause in done and first not in done, "Prose escaped before generation type/completion was known"
            assert persisted_count(path)==0
            release.set()
            body=(await asyncio.wait_for(first,2))+b"".join([part async for part in relay])
            if later_tool:
                assert speech(body.decode())=="" and len(issued(body.decode()))==1
                assert persisted_count(path)==1
            else:
                assert speech(body.decode())=="First unchanged sentence. Second sentence తెలుగు."
                assert issued(body.decode())==[] and persisted_count(path)==0
            assert body.endswith(DONE) and response.is_closed and not gateway.active
        finally:
            release.set()
            for task in (first,pause):
                if not task.done():task.cancel()
            await asyncio.gather(first,pause,return_exceptions=True)
            await relay.aclose()
            await gateway.close()
    asyncio.run(run())

def test_text_only_combines_exact_unicode_and_whitespace_without_changing_speech(tmp_path):
    parts=["  Hello",", caller.\n","నా వాహనం", " — one answer.  "]
    content=b"".join([*(frame({"content":part}) for part in parts),frame(reason="stop"),DONE])
    app,gateway,path=setup(tmp_path,lambda _:httpx.Response(200,headers={"content-type":"text/event-stream"},content=content))
    async def run():
        try:
            response=await post(app,request_body())
            assert speech(response.text)=="".join(parts)
            assert len([event for event in events(response.text) if event.get("choices",[{}])[0].get("delta",{}).get("content")])==1
            assert issued(response.text)==[] and persisted_count(path)==0
            assert response.text.endswith("data: [DONE]\n\n")
        finally:await gateway.close()
    asyncio.run(run())

def test_emitting_withdrawal_command_does_not_execute_it_and_normal_receipt_replay_stays_exact(tmp_path):
    store=Store(tmp_path/'business.sqlite3')
    owner=store.create_session('live');sid=owner['id'];cid='call-a'
    with store.connect() as db:db.execute('UPDATE sessions SET provider_call_id=? WHERE id=?',(cid,sid))
    saved=execute(store,sid,'request_handoff',{'reason':'Synthetic local request'},'original-request')
    arguments={'handoff_id':saved['id']}
    cmd=command('withdraw_handoff',json.dumps(arguments))
    content=frame({'content':CLAIM,'tool_calls':[cmd]})+frame(reason='tool_calls')+DONE
    app=FastAPI();app.state.store=store
    provider=httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,headers={'content-type':'text/event-stream'},content=content)))
    gateway=install_gateway(app,lambda:{'GEMINI_GATEWAY_SECRET':SECRET,'GEMINI_API_KEY':KEY},lambda *args:sid,client=provider)
    gateway.store.pin_call_policy(sid,cid,'our-assistant',{'VAPI_MODEL_PROFILE':'gemini-3.8-flash-custom'})
    async def run():
        harness=ToolHarness(store)
        try:
            body=request_body();body['messages']=[{'role':'user','content':'Withdraw my local advisor request.'}]
            response=await post(app,body)
            calls=issued(response.text)
            assert len(calls)==1 and speech(response.text)==''
            assert store.snapshot(sid)['handoff']['status']=='callback_requested'
            normalized=DEFINITIONS['withdraw_handoff'][0].model_validate(arguments).model_dump()
            gateway.store.authorize_tool_batch(sid,cid,[(cmd['id'],'withdraw_handoff',normalized,arguments)])
            first=await harness.run(sid,'withdraw_handoff',arguments,cmd['id'])
            second=await harness.run(sid,'withdraw_handoff',arguments,cmd['id'])
            assert first==second and first['status']=='withdrawn' and '_receipt' in first
            gateway.observations.record(sid,cid,cmd['id'],first)
            assert gateway.store.restore_group(sid,cid,calls)[0]['extra_content']==cmd['extra_content']
            snapshot=store.snapshot(sid)
            assert snapshot['handoff']['status']=='withdrawn' and snapshot['appointment'] is None and snapshot['proposal'] is None
            assert len([event for event in snapshot['events'] if event['name']=='withdraw_handoff'])==1
        finally:
            await harness.close()
            await gateway.close()
            await provider.aclose()
    asyncio.run(run())
