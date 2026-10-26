"""Reproduce the handset failure: Vapi retained only the first parallel tool."""
import asyncio
import json
import sqlite3
import httpx
import pytest
from test_gemini_gateway import setup, post, request_body, frame, SIG
from app.gemini_gateway import SERIAL_TOOL_CLARIFICATION
from app.gemini_gateway import sse


def batch(prefix, count):
    tools=[{'index':i,'id':f'{prefix}-{i}','type':'function',
        'function':{'name':'get_dealership_info','arguments':'{}'},
        **({'extra_content':{'google':{'thought_signature':SIG}}} if i==0 else {})} for i in range(count)]
    return frame({'tool_calls':tools})+frame(reason='tool_calls')+b'data: [DONE]\n\n'


@pytest.mark.parametrize('retry_count', [1,2])
def test_isolated_parallel_candidate_is_never_issued_as_a_partial_group(tmp_path,retry_count):
    seen=[]
    def handler(request):
        body=json.loads(request.content);seen.append(body)
        content=batch('discarded',2) if len(seen)==1 else batch('retry',retry_count) if len(seen)==2 else frame({'content':'The request can continue.'})+frame(reason='stop')+b'data: [DONE]\n\n'
        tokens=120 if len(seen)==1 else 60
        content=content.replace(b'data: [DONE]',sse({'choices':[],'usage':{'total_tokens':tokens}})+b'data: [DONE]')
        return httpx.Response(200,headers={'content-type':'text/event-stream'},content=content)
    app,gateway,path=setup(tmp_path,handler);gateway.waiting_cue_assistant='our-assistant'
    async def run():
        try:
            response=await post(app,request_body())
            assert response.status_code==200 and 'discarded-0' not in response.text and 'discarded-1' not in response.text
            assert len(seen)==2 and 'at most ONE' in seen[0]['messages'][0]['content']
            assert 'NONE of that candidate' in seen[1]['messages'][0]['content']
            with sqlite3.connect(path) as db:
                rows=db.execute('SELECT tool_call_id,group_size FROM gemini_tool_signatures').fetchall()
            if retry_count==1:
                assert rows==[('retry-0',1)]
                body=request_body();body['messages'].extend([
                    {'role':'assistant','content':'','tool_calls':[{'id':'retry-0','type':'function','function':{'name':'get_dealership_info','arguments':'{}'}}]},
                    {'role':'tool','tool_call_id':'retry-0','content':'{"ok":true}'}])
                gateway.observations.record('sid-a','call-a','retry-0',{'ok':True})
                follow=await post(app,body)
                assert follow.status_code==200 and 'request can continue' in follow.text
            else:
                assert rows==[] and SERIAL_TOOL_CLARIFICATION in response.text
                assert 'tool_calls' not in response.text and '"finish_reason":"stop"' in response.text
            assert not gateway.active
            assert gateway.timings[0]['counts']['serial_tool_replans']==1
            assert gateway.timings[0]['usage']['total_tokens']==180
        finally:await gateway.close()
    asyncio.run(run())


def test_other_assistant_keeps_complete_parallel_group_policy(tmp_path):
    seen=[]
    def handler(request):
        seen.append(json.loads(request.content));return httpx.Response(200,headers={'content-type':'text/event-stream'},content=batch('parallel',2))
    app,gateway,path=setup(tmp_path,handler);gateway.waiting_cue_assistant='different-assistant'
    async def run():
        try:
            response=await post(app,request_body())
            assert len(seen)==1 and 'at most ONE' not in seen[0]['messages'][0]['content']
            assert 'parallel-0' in response.text and 'parallel-1' in response.text
            with sqlite3.connect(path) as db:assert db.execute('SELECT group_size FROM gemini_tool_signatures').fetchall()==[(2,),(2,)]
        finally:await gateway.close()
    asyncio.run(run())


def test_interrupt_during_private_replan_releases_capacity_and_issues_nothing(tmp_path):
    async def run():
        entered=asyncio.Event();calls=[]
        async def handler(request):
            calls.append(request)
            if len(calls)==2:
                entered.set();await asyncio.Event().wait()
            content=batch('private',2) if len(calls)==1 else frame({'content':'Still here.'})+frame(reason='stop')+b'data: [DONE]\n\n'
            return httpx.Response(200,headers={'content-type':'text/event-stream'},content=content)
        app,gateway,path=setup(tmp_path,handler);gateway.waiting_cue_assistant='our-assistant'
        try:
            task=asyncio.create_task(post(app,request_body()))
            await asyncio.wait_for(entered.wait(),2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
            assert not gateway.active
            with sqlite3.connect(path) as db:assert db.execute('SELECT COUNT(*) FROM gemini_tool_signatures').fetchone()[0]==0
            response=await post(app,request_body())
            assert response.status_code==200 and 'Still here.' in response.text and not gateway.active
        finally:await gateway.close()
    asyncio.run(run())
