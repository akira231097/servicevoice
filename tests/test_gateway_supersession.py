"""Deterministic offline generation ownership; no network, real calls or effects."""
import asyncio
import json
import sqlite3
import threading
import time
from contextlib import suppress

import httpx
import pytest
from starlette.requests import Request
from fastapi import HTTPException

import app.gemini_gateway as module
from app.gemini_gateway import GatewayLimits
from test_gemini_gateway import SECRET, SIG, continuation, frame, post, request_body, setup, tool_stream


def reply(text="Latest"):
    return httpx.Response(200, headers={"content-type":"text/event-stream"},
                          content=frame({"content":text})+frame(reason="stop")+b"data: [DONE]\n\n")


def neutral(response):
    assert response.status_code == 200
    assert response.text.endswith("data: [DONE]\n\n")
    events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")]
    assert len(events)==1 and events[0]["choices"][0]["delta"]=={}
    assert events[0]["choices"][0]["finish_reason"]=="stop"
    assert SIG not in response.text and "error" not in response.text


async def until(predicate, seconds=2):
    async with asyncio.timeout(seconds):
        while not predicate():await asyncio.sleep(.001)


def direct_request(body):
    encoded=json.dumps(body).encode()
    sent=False
    async def receive():
        nonlocal sent
        if not sent:
            sent=True
            return {"type":"http.request","body":encoded,"more_body":False}
        await asyncio.Event().wait()
    return Request({"type":"http","method":"POST","path":"/llm/chat/completions",
                    "headers":[(b"authorization",("Bearer "+SECRET).encode())]},receive)


def test_headers_superseded_are_neutral_and_latest_runs_at_capacity(tmp_path):
    async def run():
        entered,cancelled=asyncio.Event(),asyncio.Event()
        count=0
        async def handler(request):
            nonlocal count
            count+=1
            if count==1:
                entered.set()
                try:await asyncio.Event().wait()
                except asyncio.CancelledError:cancelled.set();raise
            return reply()
        app,gateway,_=setup(tmp_path,handler,GatewayLimits(concurrency=1))
        try:
            first=asyncio.create_task(post(app,request_body()))
            await entered.wait()
            assert (await post(app,request_body("call-b"))).status_code==429
            latest=await asyncio.wait_for(post(app,request_body()),2)
            neutral(await first)
            assert cancelled.is_set() and "Latest" in latest.text and count==2
            assert not gateway.active and not gateway.owners and not gateway.successors
            assert any(row["outcome"]=="superseded" for row in gateway.timings)
        finally:await gateway.close()
    asyncio.run(run())


def test_buffered_prose_and_tools_never_escape_on_supersession(tmp_path):
    async def run():
        buffered,closed=asyncio.Event(),asyncio.Event()
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield frame({"content":"Obsolete success claim"})
                yield tool_stream().split(b"data: [DONE]")[0]
                buffered.set()
                await asyncio.Event().wait()
            async def aclose(self):closed.set()
        count=0
        async def handler(request):
            nonlocal count
            count+=1
            return httpx.Response(200,headers={"content-type":"text/event-stream"},stream=Stream()) if count==1 else reply()
        app,gateway,path=setup(tmp_path,handler)
        try:
            old=asyncio.create_task(post(app,request_body()))
            await buffered.wait()
            new=await asyncio.wait_for(post(app,request_body()),2)
            neutral(await old)
            assert closed.is_set() and "Latest" in new.text
            with sqlite3.connect(path) as db:
                assert db.execute("SELECT COUNT(*) FROM gemini_tool_signatures").fetchone()[0]==0
            assert not gateway.active
        finally:await gateway.close()
    asyncio.run(run())


def test_three_generations_keep_original_cleanup_barrier(tmp_path):
    async def run():
        entered,cancelled,release=asyncio.Event(),asyncio.Event(),asyncio.Event()
        count=0
        async def handler(request):
            nonlocal count
            count+=1
            if count==1:
                entered.set()
                try:await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set();await release.wait()
            return reply()
        app,gateway,_=setup(tmp_path,handler,GatewayLimits(concurrency=1))
        tasks=[]
        try:
            tasks.append(asyncio.create_task(post(app,request_body())))
            await entered.wait()
            original=gateway.owners["call-a"]
            tasks.append(asyncio.create_task(post(app,request_body())))
            await cancelled.wait()
            tasks.append(asyncio.create_task(post(app,request_body())))
            neutral(await asyncio.wait_for(tasks[1],1))
            assert count==1 and gateway.owners["call-a"] is original and not original.released.is_set()
            assert (await post(app,request_body("call-b"))).status_code==429
            release.set()
            neutral(await tasks[0])
            assert "Latest" in (await tasks[2]).text and count==2
            assert not gateway.active
        finally:
            release.set()
            await asyncio.gather(*tasks,return_exceptions=True)
            await gateway.close()
    asyncio.run(run())


def test_slow_close_keeps_physical_lease_after_wait_timeout(tmp_path):
    async def run():
        buffered,closing,release=asyncio.Event(),asyncio.Event(),asyncio.Event()
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                buffered.set()
                await asyncio.Event().wait()
                yield b""
            async def aclose(self):closing.set();await release.wait()
        count=0
        async def handler(request):
            nonlocal count
            count+=1
            return httpx.Response(200,headers={"content-type":"text/event-stream"},stream=Stream()) if count==1 else reply()
        app,gateway,_=setup(tmp_path,handler,GatewayLimits(concurrency=1,cleanup_seconds=.02,supersession_seconds=.05))
        old=None
        try:
            old=asyncio.create_task(post(app,request_body()))
            await buffered.wait()
            original=gateway.owners["call-a"]
            neutral(await post(app,request_body()))
            await closing.wait()
            assert count==1 and not original.released.is_set() and gateway.active=={"call-a"}
            assert (await post(app,request_body("call-b"))).status_code==429
            release.set()
            neutral(await old)
            await until(lambda:not gateway.active)
            assert "Latest" in (await post(app,request_body())).text
            assert any(row["outcome"]=="supersession_wait_expired" for row in gateway.timings)
        finally:
            release.set()
            if old:await old
            await gateway.close()
    asyncio.run(run())


def test_older_slow_preflight_never_replaces_newer_completed_request(tmp_path,monkeypatch):
    entered,release=threading.Event(),threading.Event()
    original=module.prepare_request
    def slow(body,*args):
        if body["messages"][0]["content"]=="older":
            entered.set()
            assert release.wait(3)
        return original(body,*args)
    monkeypatch.setattr(module,"prepare_request",slow)
    async def run():
        requests=[]
        def handler(request):requests.append(request);return reply()
        app,gateway,_=setup(tmp_path,handler)
        old=None
        try:
            body=request_body();body["messages"][0]["content"]="older"
            old=asyncio.create_task(post(app,body))
            await until(entered.is_set)
            assert "Latest" in (await post(app,request_body())).text
            release.set()
            neutral(await old)
            assert len(requests)==1 and not gateway.active
        finally:
            release.set()
            if old:await old
            await gateway.close()
    asyncio.run(run())


def test_invalid_preflight_does_not_cancel_running_owner(tmp_path):
    async def run():
        entered,release=asyncio.Event(),asyncio.Event()
        async def handler(request):entered.set();await release.wait();return reply()
        app,gateway,_=setup(tmp_path,handler)
        try:
            old=asyncio.create_task(post(app,request_body()));await entered.wait()
            owner=gateway.owners["call-a"]
            malformed=request_body();malformed["messages"][0]["role"]="developer"
            assert (await post(app,malformed)).status_code==422
            assert gateway.owners["call-a"] is owner and not owner.superseded.is_set()
            release.set();assert "Latest" in (await old).text
        finally:release.set();await gateway.close()
    asyncio.run(run())


@pytest.mark.parametrize("ending",["session","shutdown","waiter_cancel"])
def test_waiting_successor_rechecks_liveness_and_cancellation(tmp_path,ending):
    async def run():
        entered,cancelled,release=asyncio.Event(),asyncio.Event(),asyncio.Event()
        count=0
        async def handler(request):
            nonlocal count
            count+=1;entered.set()
            try:await asyncio.Event().wait()
            except asyncio.CancelledError:cancelled.set();await release.wait()
            return reply()
        app,gateway,path=setup(tmp_path,handler)
        tasks=[]
        try:
            tasks.append(asyncio.create_task(post(app,request_body())));await entered.wait()
            tasks.append(asyncio.create_task(post(app,request_body())));await cancelled.wait()
            if ending=="session":
                with sqlite3.connect(path) as db:db.execute("UPDATE sessions SET ended=1 WHERE id='sid-a'")
            elif ending=="shutdown":gateway.closing=True
            else:
                tasks[1].cancel()
                with pytest.raises(asyncio.CancelledError):await tasks[1]
                assert gateway.active=={"call-a"} and not gateway.successors
            release.set();neutral(await tasks[0])
            if ending=="session":assert (await tasks[1]).status_code==409
            elif ending=="shutdown":neutral(await tasks[1])
            assert count==1 and not gateway.active
        finally:
            release.set();await asyncio.gather(*tasks,return_exceptions=True);await gateway.close()
    asyncio.run(run())


def test_unstarted_stream_is_closed_and_cannot_release_new_owner(tmp_path):
    async def run():
        entered,release=asyncio.Event(),asyncio.Event()
        count=0
        async def handler(request):
            nonlocal count
            count+=1
            if count==2:entered.set();await release.wait()
            return reply("Old" if count==1 else "Latest")
        app,gateway,_=setup(tmp_path,handler)
        old=await gateway.handle(direct_request(request_body()))
        original=gateway.owners["call-a"]
        new=asyncio.create_task(post(app,request_body()))
        try:
            await entered.wait()
            current=gateway.owners["call-a"]
            assert current is not original and original.released.is_set()
            text=b"".join([part async for part in old.body_iterator]).decode()
            neutral(httpx.Response(200,text=text))
            await old.cleanup()
            assert gateway.owners["call-a"] is current and gateway.active=={"call-a"}
            release.set();assert "Latest" in (await new).text
        finally:release.set();await new;await old.cleanup();await gateway.close()
    asyncio.run(run())


def test_already_emitted_signed_tool_remains_valid_for_continuation(tmp_path):
    async def run():
        sent=[]
        def handler(request):
            sent.append(json.loads(request.content))
            return httpx.Response(200,headers={"content-type":"text/event-stream"},content=tool_stream()) if len(sent)==1 else reply()
        app,gateway,path=setup(tmp_path,handler)
        old=await gateway.handle(direct_request(request_body()))
        try:
            emitted=await anext(old.body_iterator)
            assert b'"tool_calls"' in emitted and SIG.encode() not in emitted
            gateway.observations.record("sid-a","call-a","tool-one",{"ok":True,"appointment":None})
            new=await post(app,continuation())
            assert "Latest" in new.text
            assert sent[1]["messages"][2]["tool_calls"][0]["extra_content"]["google"]["thought_signature"]==SIG
            rest=b"".join([part async for part in old.body_iterator])
            assert b'"delta":{}' in rest and b"[DONE]" in rest and b'"id":"tool-one"' not in rest
            with sqlite3.connect(path) as db:assert db.execute("SELECT COUNT(*) FROM gemini_tool_signatures").fetchone()[0]==1
            assert any(row["outcome"]=="superseded_after_release" for row in gateway.timings)
        finally:await old.cleanup();await gateway.close()
    asyncio.run(run())


@pytest.mark.parametrize("failure",[sqlite3.OperationalError("private-db-detail"),RuntimeError("private-detail")])
def test_post_admission_state_error_releases_owner(tmp_path,monkeypatch,failure):
    app,gateway,_=setup(tmp_path,lambda _:pytest.fail("no provider call"))
    original=gateway.store.check_live
    count=0
    def check(*args):
        nonlocal count
        count+=1
        if count==2:raise failure
        return original(*args)
    monkeypatch.setattr(gateway.store,"check_live",check)
    async def run():
        try:
            if isinstance(failure,sqlite3.Error):
                result=await post(app,request_body());assert result.status_code==503 and "private" not in result.text
            else:
                with pytest.raises(RuntimeError):await post(app,request_body())
            assert not gateway.active and not gateway.owners
        finally:await gateway.close()
    asyncio.run(run())


@pytest.mark.parametrize("transferred",[False,True])
def test_cancellation_during_admission_finally_has_no_orphan_owner_or_successor(tmp_path,monkeypatch,transferred):
    async def run():
        entered,cancelled,release,joining=asyncio.Event(),asyncio.Event(),asyncio.Event(),asyncio.Event()
        requests=0
        async def handler(request):
            nonlocal requests
            requests+=1
            if requests==1:
                entered.set()
                try:await asyncio.Event().wait()
                except asyncio.CancelledError:cancelled.set();await release.wait()
            return reply()
        app,gateway,_=setup(tmp_path,handler,GatewayLimits(supersession_seconds=.04 if not transferred else 2))
        original=gateway._watch_disconnect
        async def watch(request,owner,stop,generation=None):
            if generation and generation.sequence==2:
                try:await asyncio.Event().wait()
                except asyncio.CancelledError:
                    joining.set()
                    await asyncio.Event().wait()
            else:await original(request,owner,stop,generation)
        monkeypatch.setattr(gateway,"_watch_disconnect",watch)
        tasks=[]
        try:
            tasks.append(asyncio.create_task(post(app,request_body())));await entered.wait()
            tasks.append(asyncio.create_task(post(app,request_body())));await cancelled.wait()
            if transferred:release.set()
            await asyncio.wait_for(joining.wait(),1)
            if transferred:
                tasks.append(asyncio.create_task(post(app,request_body())))
                neutral(await asyncio.wait_for(tasks[1],1))
                assert "Latest" in (await tasks[2]).text
            else:
                tasks[1].cancel()
                # Its bounded wait already expired, so this is obsolete neutral.
                neutral(await tasks[1])
                assert not gateway.successors and gateway.active=={"call-a"}
                release.set()
            neutral(await tasks[0])
            assert not gateway.active and not gateway.owners and not gateway.successors
            assert requests==(2 if transferred else 1)
        finally:
            release.set()
            for task in tasks:
                if not task.done():task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True);await gateway.close()
    asyncio.run(run())


def test_supersession_during_signature_persistence_suppresses_command_but_keeps_record(tmp_path,monkeypatch):
    saved,release=threading.Event(),threading.Event()
    async def run():
        requests=0
        def handler(request):
            nonlocal requests
            requests+=1
            return httpx.Response(200,headers={"content-type":"text/event-stream"},content=tool_stream()) if requests==1 else reply()
        app,gateway,path=setup(tmp_path,handler)
        original=gateway.store.save_group
        def save(*args,**kwargs):
            original(*args,**kwargs);saved.set();assert release.wait(3)
        monkeypatch.setattr(gateway.store,"save_group",save)
        old=None
        try:
            old=asyncio.create_task(post(app,request_body()));await until(saved.is_set)
            assert "Latest" in (await post(app,request_body())).text
            release.set();neutral(await old)
            with sqlite3.connect(path) as db:assert db.execute("SELECT COUNT(*) FROM gemini_tool_signatures").fetchone()[0]==1
            assert not gateway.active
        finally:
            release.set()
            if old:await old
            await gateway.close()
    asyncio.run(run())


def test_four_actual_upstreams_bound_replacement_and_other_call_fairness(tmp_path):
    async def run():
        release=asyncio.Event();active=0;maximum=0;seen={}
        async def handler(request):
            nonlocal active,maximum
            key=json.loads(request.content)["messages"][-1]["content"]
            seen[key]=seen.get(key,0)+1
            active+=1;maximum=max(maximum,active)
            try:
                if key!="call-a" or seen[key]==1:await release.wait()
                return reply()
            finally:active-=1
        app,gateway,path=setup(tmp_path,handler,GatewayLimits(concurrency=4))
        with sqlite3.connect(path) as db:
            db.executemany("INSERT INTO sessions VALUES (?,?,?,?,?)",[("sid-"+letter,"cedar-demo",0,time.time(),"call-"+letter) for letter in "cde"])
        def bind(call,assistant=None):
            cid=call.get("id")
            if cid not in {"call-"+letter for letter in "abcde"}:raise HTTPException(403,"Invalid binding")
            sid="sid-"+cid[-1]
            gateway.store.pin_call_policy(sid,cid,"our-assistant",{"VAPI_MODEL_PROFILE":"gemini-3.8-flash-custom"})
            return sid
        gateway.bind_call=bind
        def body(cid):
            value=request_body(cid);value["messages"][-1]["content"]=cid;return value
        tasks=[]
        try:
            tasks=[asyncio.create_task(post(app,body("call-"+letter))) for letter in "abcd"]
            await until(lambda:active==4)
            assert (await post(app,body("call-e"))).status_code==429
            assert "Latest" in (await post(app,body("call-a"))).text
            neutral(await tasks[0])
            assert maximum==4 and active==3
            release.set();assert all(result.status_code==200 for result in await asyncio.gather(*tasks[1:]))
            assert not gateway.active and maximum==4
        finally:release.set();await asyncio.gather(*tasks,return_exceptions=True);await gateway.close()
    asyncio.run(run())
