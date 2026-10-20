"""Local ownership evidence only; no caller intent, vendor or audio inference."""
import asyncio
import json
import sqlite3
import threading
import time
from types import SimpleNamespace
from uuid import UUID,uuid4

import httpx
import pytest

import app.gemini_gateway as module
from app.gemini_gateway import Generation, RequestTiming
from app.observability import LocalTimingSink, safe_attributes, safe_timing_row
from test_gemini_gateway import SIG, continuation, frame, post, request_body, setup, tool_stream
from test_gateway_supersession import direct_request, neutral, reply, until


def assert_identity(row):
    assert UUID(row["generation_id"]).hex==row["request_id"]
    assert row["generation_sequence_scope"]=="process_local_http_arrival_only"
    assert row["payload_release_scope"]=="gateway_yield_not_provider_receipt_or_playback"


def test_stable_generation_and_existing_sse_mapping_persist_without_content(tmp_path):
    async def run():
        app,gateway,_=setup(tmp_path,lambda r:reply("PRIVATE_CALLER_CONTENT"))
        sink=LocalTimingSink(tmp_path/'timing.sqlite3');gateway.timing_sink=sink
        try:
            first=await post(app,request_body());second=await post(app,request_body())
            rows=gateway.latency_snapshot('call-a')
            assert len(rows)==2 and rows[0]["generation_id"]!=rows[1]["generation_id"]
            assert [r["generation_sequence"] for r in rows]==[1,2]
            for row,response in zip(rows,[first,second]):
                assert_identity(row)
                assert row["generation_terminal_state"]=="completed"
                ids={json.loads(line[6:])["id"] for line in response.text.splitlines() if line.startswith('data: {')}
                assert ids=={row["sse_response_id"]}
                assert 0<=row["payload_released_at_ms"]<=row["milestones_ms"]["total"]
            assert sink.close()
            persisted=sink.snapshot()
            assert {r["generation_id"] for r in persisted}=={r["generation_id"] for r in rows}
            assert all(r["sse_response_id"] for r in persisted)
            assert "PRIVATE_CALLER_CONTENT" not in json.dumps(persisted)
            assert "call-a" not in json.dumps(persisted)
        finally:sink.close();await gateway.close()
    asyncio.run(run())


def test_late_old_fragments_after_cancellation_cannot_release_or_issue_tool(tmp_path):
    async def run():
        buffered=asyncio.Event();closed=asyncio.Event();requests=[]
        class LateStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield frame({"content":"OLD_PRIVATE_SUCCESS_CLAIM"})
                buffered.set()
                try:await asyncio.Event().wait()
                except asyncio.CancelledError:
                    # Model transport ignores cancellation and sends a late
                    # complete signed tool. Ownership checkpoints still gate it.
                    yield tool_stream()
            async def aclose(self):closed.set()
        async def handler(request):
            requests.append(request)
            return httpx.Response(200,headers={"content-type":"text/event-stream"},stream=LateStream()) if len(requests)==1 else reply()
        app,gateway,path=setup(tmp_path,handler)
        old=None
        try:
            old=asyncio.create_task(post(app,request_body()))
            await asyncio.wait_for(buffered.wait(),2)
            newest=await asyncio.wait_for(post(app,request_body()),2)
            neutral(await old)
            assert "Latest" in newest.text and closed.is_set()
            rows=gateway.latency_snapshot('call-a')
            prior=next(r for r in rows if r["generation_sequence"]==1)
            latest=next(r for r in rows if r["generation_sequence"]==2)
            assert prior["generation_terminal_state"]=="superseded_before_release"
            assert prior["superseded_by_generation_id"]==latest["generation_id"]
            assert latest["predecessor_generation_id"]==prior["generation_id"]
            assert prior["supersession_cause"]=="newer_local_http_arrival"
            assert prior["superseded_at_ms"] is not None
            assert prior["payload_released_at_ms"] is None and prior["checkpoint_rejections"]>=1
            assert "OLD_PRIVATE_SUCCESS_CLAIM" not in json.dumps(rows)
            with sqlite3.connect(path) as db:
                assert db.execute('SELECT COUNT(*) FROM gemini_tool_signatures').fetchone()[0]==0
        finally:
            if old and not old.done():old.cancel()
            if old:await asyncio.gather(old,return_exceptions=True)
            await gateway.close()
    asyncio.run(run())


def test_replacement_after_release_retains_attempt_and_explicit_lineage(tmp_path):
    async def run():
        requests=[]
        def handler(request):
            requests.append(request)
            return httpx.Response(200,headers={"content-type":"text/event-stream"},content=tool_stream()) if len(requests)==1 else reply()
        app,gateway,path=setup(tmp_path,handler)
        old=await gateway.handle(direct_request(request_body()))
        try:
            offered=await anext(old.body_iterator)
            assert b'tool_calls' in offered
            gateway.observations.record('sid-a','call-a','tool-one',{"ok":True,"appointment":None})
            assert "Latest" in (await post(app,continuation())).text
            remaining=b''.join([part async for part in old.body_iterator])
            assert b'tool-one' not in remaining
            rows=gateway.latency_snapshot('call-a')
            prior=next(r for r in rows if r['generation_sequence']==1)
            latest=next(r for r in rows if r['generation_sequence']==2)
            assert prior['generation_terminal_state']=='superseded_after_release'
            assert prior['payload_released_at_ms'] is not None
            assert prior['superseded_by_generation_id']==latest['generation_id']
            assert latest['predecessor_generation_id']==prior['generation_id']
            assert prior['sse_response_id'].encode() in offered
            with sqlite3.connect(path) as db:
                assert db.execute('SELECT COUNT(*) FROM gemini_tool_signatures').fetchone()[0]==1
        finally:await old.cleanup();await gateway.close()
    asyncio.run(run())


def test_delayed_old_preflight_links_newer_completed_local_request(tmp_path,monkeypatch):
    entered,release=threading.Event(),threading.Event();original=module.prepare_request
    def slow(value,*args,**kwargs):
        if value['messages'][0]['content']=='older-private':
            entered.set();assert release.wait(3)
        return original(value,*args,**kwargs)
    monkeypatch.setattr(module,'prepare_request',slow)
    async def run():
        app,gateway,_=setup(tmp_path,lambda r:reply())
        old=None
        try:
            value=request_body();value['messages'][0]['content']='older-private'
            old=asyncio.create_task(post(app,value));await until(entered.is_set)
            assert 'Latest' in (await post(app,request_body())).text
            release.set();neutral(await old)
            rows=gateway.latency_snapshot('call-a')
            prior=next(r for r in rows if r['generation_sequence']==1)
            latest=next(r for r in rows if r['generation_sequence']==2)
            assert prior['generation_terminal_state']=='superseded_before_release'
            assert prior['superseded_by_generation_id']==latest['generation_id']
            assert latest['predecessor_generation_id'] is None
            assert prior['predecessor_generation_id'] is None
        finally:
            release.set()
            if old:await old
            await gateway.close()
    asyncio.run(run())


@pytest.mark.parametrize('disconnected',[False,True])
def test_unidentified_cancellation_and_observed_transport_disconnect_are_distinct(disconnected):
    saved=[];timing=RequestTiming('private-call',time.monotonic(),saved)
    generation=Generation('private-call',1,timing);timing.generation=generation
    timing.outcome='cancelled'
    if disconnected:
        generation.disconnected=True;timing.note_time('downstream_disconnect_at_ms')
    timing.finish();[row]=saved
    assert row['generation_terminal_state']==('downstream_disconnected' if disconnected else 'cancelled_unknown_cause')
    assert row['downstream_disconnect_at_ms'] is not None if disconnected else row['downstream_disconnect_at_ms'] is None
    assert 'caller_interruption' not in json.dumps(row)


def test_local_lineage_projection_rejects_arbitrary_content_and_noncanonical_shapes(tmp_path):
    timing=RequestTiming('call-private',time.monotonic(),[]);timing.outcome='completed';timing.finish()
    value=timing.row
    value.update(generation_sequence=True,predecessor_generation_id='PRIVATE_SECRET',
        superseded_by_generation_id=str(uuid4()).upper(),supersession_cause='PRIVATE_SECRET',
        superseded_at_ms=float('nan'),payload_released_at_ms=-1,
        checkpoint_rejections=10**12,sse_response_id='https://PRIVATE_SECRET',
        neutral_sse_response_id='chatcmpl-PRIVATE_SECRET',provider_generation_id='PRIVATE_SECRET')
    projected=safe_timing_row(value)
    assert projected['generation_id']==value['generation_id']
    for field in ('generation_sequence','predecessor_generation_id','superseded_by_generation_id',
            'supersession_cause','superseded_at_ms','payload_released_at_ms','checkpoint_rejections',
            'sse_response_id','neutral_sse_response_id','provider_generation_id'):
        assert field not in projected
    assert 'PRIVATE_SECRET' not in json.dumps(projected)
    telemetry=safe_attributes(value)
    assert telemetry['generation_id']==value['generation_id']
    assert 'PRIVATE_SECRET' not in json.dumps(telemetry)
    assert 'provider_generation_id' not in telemetry


def test_gateway_lineage_metadata_reaches_local_langsmith_exporter_without_wire_inference(tmp_path):
    from test_observability import make_sink,drain_telemetry
    sink,exporter=make_sink(tmp_path)
    generation=str(uuid4());predecessor=str(uuid4())
    try:
        assert sink.record_event('gateway.request',{'route':'gemini','outcome':'completed',
            'generation_id':generation,'generation_sequence':2,
            'generation_sequence_scope':'process_local_http_arrival_only',
            'predecessor_generation_id':predecessor,'predecessor_relation':'active_or_draining_owner',
            'generation_terminal_state':'completed','payload_released_at_ms':12.5,
            'payload_release_scope':'gateway_yield_not_provider_receipt_or_playback',
            'provider_generation_id':'PRIVATE_SECRET','tts_generation_id':'PRIVATE_SECRET'},correlation='private-call')
    finally:assert drain_telemetry(sink)
    [span]=exporter.get_finished_spans()
    assert span.attributes['langsmith.metadata.generation_id']==generation
    assert span.attributes['langsmith.metadata.predecessor_generation_id']==predecessor
    assert span.attributes['langsmith.metadata.generation_sequence']==2
    assert 'PRIVATE_SECRET' not in json.dumps(dict(span.attributes))
    assert 'provider_generation_id' not in span.attributes and 'tts_generation_id' not in span.attributes


def test_gateway_component_ledger_persists_generation_scope_and_exact_sse_mapping(tmp_path):
    from app.diagnostics import Diagnostics,read_call_diagnostics
    async def run():
        app,gateway,_=setup(tmp_path,lambda r:reply('PRIVATE_SPOKEN_TEXT'))
        sink=Diagnostics(tmp_path/'components.sqlite3');gateway.diagnostics=sink
        try:
            response=await post(app,request_body())
            [timing]=gateway.latency_snapshot('call-a')
            assert sink.close()
            rows=read_call_diagnostics(sink.path,'call-a')['events']
            [row]=[item for item in rows if item['component']=='gateway']
            attributes=row['attributes']
            assert row['request_id']==timing['request_id']
            assert attributes['generation_id']==timing['generation_id']
            assert attributes['generation_sequence_scope']=='process_local_http_arrival_only'
            assert attributes['payload_release_scope']=='gateway_yield_not_provider_receipt_or_playback'
            assert attributes['sse_response_id']==timing['sse_response_id']
            assert timing['sse_response_id'] in response.text
            assert 'PRIVATE_SPOKEN_TEXT' not in json.dumps(rows)
            assert 'provider_generation_id' not in attributes and 'tts_generation_id' not in attributes
        finally:sink.close();await gateway.close()
    asyncio.run(run())
