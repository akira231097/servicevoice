// Pure completion/greeting checks: no provider, microphone, credentials or runtime DB.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import fs from 'node:fs/promises';
import path from 'node:path';
import vm from 'node:vm';
import {fileURLToPath} from 'node:url';
import {parseEnv} from 'node:util';
import {createHash} from 'node:crypto';
import { AssistantCaptionTracker, faqHoursReady, persistedGreetingComplete, waitForPersistedGreeting } from '../scripts/voice_regression_checks.mjs';
import {collectGatewayTiming,sanitizeGatewayTiming} from '../scripts/gateway_timing_evidence.mjs';

const greeting = [{role:'assistant',text:"Hi, I'm Cedar. This is an AI demo. How can I help with your service visit?"}];

test('short final cannot settle a newer partial; a final must also reach persistent captions',()=>{
  const tracker=new AssistantCaptionTracker();
  const event=(transcriptType,transcript)=>({type:'transcript',role:'assistant',transcriptType,transcript});
  tracker.observe(event('final','हाँ।'));
  const saved=[{role:'assistant',text:'हाँ।'}];assert.equal(tracker.settled(saved),true);
  assert.equal(tracker.observe(event('partial','ब्रेक की जाँच में')),true);
  assert.equal(tracker.settled(saved),false);assert.equal(tracker.pendingPartial,true);
  tracker.observe(event('final','ब्रेक की जाँच में पैड देखे जाते हैं।'));
  assert.equal(tracker.pendingPartial,false);assert.equal(tracker.settled(saved),false);
  saved.push({role:'assistant',text:'ब्रेक की जाँच में पैड देखे जाते हैं।'});assert.equal(tracker.settled(saved),true);
});

test('caption matching preserves repeated finals and ignores unrelated or malformed events',()=>{
  const tracker=new AssistantCaptionTracker(),final={type:'transcript',role:'assistant',transcriptType:'final',transcript:'Yes.'};
  tracker.observe(final);tracker.observe(final);
  assert.equal(tracker.settled([{role:'assistant',text:'Yes.'}]),false);
  assert.equal(tracker.settled([{role:'assistant',text:'Yes.'},{role:'assistant',text:'Yes.'}]),true);
  for(const message of [{...final,role:'user',transcriptType:'partial'},{...final,transcript:null},{...final,transcript:'  '},{...final,transcriptType:'unknown'}])assert.equal(tracker.observe(message),false);
  assert.equal(tracker.pendingPartial,false);
});

test('weekday closing cue accepts only five PM equivalents and retains Central/evidence checks',()=>{
  for (const time of ['5 PM','5pm','5:00 PM','5:00 p.m.','five PM','five in the afternoon','5 in the evening']) {
    assert.equal(faqHoursReady(`We close at ${time} Central.`,true),true,time);
    assert.equal(faqHoursReady(`We close at ${time} Central.`,false,[{name:'get_dealership_info'}]),true,time);
    assert.equal(faqHoursReady(`We close at ${time} Central.`,false,[]),false,time);
    assert.equal(faqHoursReady(`We close at ${time} Eastern.`,true),false,time);
  }
  for (const time of ['5:30 PM','5:01 PM','15:00 PM','25 PM','5 AM','5:00 AM','5:00:30 PM'])
    assert.equal(faqHoursReady(`We close at ${time} Central.`,true),false,time);
  assert.equal(faqHoursReady('Our service hours are Monday to Friday, 8:00 AM to 5:00 PM Central, closed Saturday and Sunday.',true),true);
});

test('persisted greeting requires the assistant closing cue, including split captions',()=>{
  assert.equal(persistedGreetingComplete(greeting),true);
  assert.equal(persistedGreetingComplete([{role:'assistant',text:'How can I help with your'},{role:'assistant',text:'service visit?'}]),true);
  assert.equal(persistedGreetingComplete([]),false);
  assert.equal(persistedGreetingComplete([{role:'assistant',text:"Hi, I'm Cedar. This is an AI demo."}]),false);
  assert.equal(persistedGreetingComplete([{role:'user',text:'How can I help with your service visit?'}]),false);
});

test('already quiet audio waits for late persisted greeting before returning the baseline',async()=>{
  let clock=0,reads=0;
  const state=await waitForPersistedGreeting({snapshot:async()=>{reads++;return {transcript:clock>=300?greeting:[]};},
    quiet:()=>true,active:()=>{},now:()=>clock,sleep:async ms=>{clock+=ms;},callStarted:0});
  assert.equal(clock,300);assert.equal(reads,4);assert.deepEqual(state.transcript,greeting);
  const after=[...state.transcript,{role:'user',text:'A question'},{role:'assistant',text:'A reply'}];
  assert.deepEqual(after.slice(state.transcript.length).filter(row=>row.role==='assistant').map(row=>row.text),['A reply']);
});

test('persisted greeting alone still waits for the existing audio-quiet condition',async()=>{
  let clock=0;
  await waitForPersistedGreeting({snapshot:async()=>({transcript:greeting}),quiet:after=>after===10&&clock>=400,
    active:()=>{},now:()=>clock,sleep:async ms=>{clock+=ms;},callStarted:10});
  assert.equal(clock,400);
});

test('missing captions or audio fail after the same bounded 20 seconds',async()=>{
  for(const [transcript,audioQuiet] of [[[],true],[greeting,false]]) {
    let clock=0;
    await assert.rejects(waitForPersistedGreeting({snapshot:async()=>({transcript}),quiet:()=>audioQuiet,
      active:()=>{},now:()=>clock,sleep:async ms=>{clock+=ms;},callStarted:0}),/greeting_caption_or_audio_not_complete/);
    assert.equal(clock,20000);
  }
});

test('a terminated call rejects before any further greeting snapshot',async()=>{
  let reads=0;
  await assert.rejects(waitForPersistedGreeting({snapshot:async()=>{reads++;return {transcript:greeting};},quiet:()=>true,
    active:()=>{throw new Error('call_ended');},now:()=>0,sleep:async()=>{},callStarted:0}),/call_ended/);
  assert.equal(reads,0);
});

function gatewayFixture(){
  return {kind:'gemini_gateway_timing',session_id:'session-fixture',provider_call_id:'call-fixture',requests:[{
    call_key:createHash('sha256').update('call-fixture').digest('hex').slice(0,16),request_id:'a'.repeat(32),route:'gemini',
    outcome:'completed',narration_reason:'issuance_context_changed',narrator_version:'receipt-en-2026-10-18-5',
    source_result_sha256:'b'.repeat(64),output_text_sha256:'c'.repeat(64),output_text_source:'verified_receipt',
    counts:{tool_calls:0},milestones_ms:{first_text:140,total:150},headers:{authorization:'PRIVATE-HEADER'},
    output_text:'PRIVATE-OUTPUT',transcript:'PRIVATE-TRANSCRIPT'}]};
}

test('gateway cleanup evidence requires exact session/call plus every call hash and excludes arbitrary payload fields',()=>{
  const value=gatewayFixture(),safe=sanitizeGatewayTiming(value,'session-fixture','call-fixture');
  assert.equal(safe.requests[0].narration_reason,'issuance_context_changed');
  assert.equal(safe.requests[0].narrator_version,'receipt-en-2026-10-18-5');
  assert.equal(safe.requests[0].output_text_sha256,'c'.repeat(64));
  assert.equal(safe.requests[0].counts.tool_calls,0);assert.doesNotMatch(JSON.stringify(safe),/PRIVATE-/);
  for(const changed of [{...value,session_id:'other'},{...value,provider_call_id:'other'},
    {...value,requests:[{...value.requests[0],call_key:'d'.repeat(16)}]}])
    assert.throws(()=>sanitizeGatewayTiming(changed,'session-fixture','call-fixture'),/gateway_scope_mismatch/);
  assert.throws(()=>sanitizeGatewayTiming({...value,requests:Array(257).fill(value.requests[0])},'session-fixture','call-fixture'),/bounds/);
});

test('gateway failure evidence keeps neutral supersession distinct from success without allowing arbitrary outcome text',()=>{
  const value=gatewayFixture();
  const outcomes=['superseded','superseded_after_release','supersession_wait_expired','PRIVATE-CALLER-TEXT'];
  value.requests=outcomes.map(outcome=>({...value.requests[0],outcome,narrator_version:'receipt-en-2026-10-18-6'}));
  const safe=sanitizeGatewayTiming(value,'session-fixture','call-fixture');
  assert.deepEqual(safe.requests.map(row=>row.outcome),[...outcomes.slice(0,3),null]);
  assert.ok(safe.requests.every(row=>row.narrator_version==='receipt-en-2026-10-18-6'));
  assert.doesNotMatch(JSON.stringify(safe),/PRIVATE-/);
});

test('gateway collector uses cleanup deadline mode, capability header only, and a 1MiB body limit',async()=>{
  const calls=[];
  const request=async(...args)=>{calls.push(args);return new Response(JSON.stringify(gatewayFixture()));};
  const safe=await collectGatewayTiming(request,{id:'session-fixture',token:'PRIVATE-CAPABILITY'},'call-fixture');
  assert.equal(calls.length,1);assert.equal(calls[0][2],true);
  assert.equal(calls[0][1].headers['x-session-token'],'PRIVATE-CAPABILITY');
  assert.doesNotMatch(calls[0][0],/PRIVATE-CAPABILITY/);assert.doesNotMatch(JSON.stringify(safe),/PRIVATE-CAPABILITY/);
  await assert.rejects(collectGatewayTiming(async()=>new Response('x'.repeat(1024*1024+1)),
    {id:'session-fixture',token:'PRIVATE-CAPABILITY'},'call-fixture'),/gateway_evidence_bounds/);
});

async function failedConversation(gateway){
  const sourceUrl=new URL('../scripts/voice_conversation_eval.mjs',import.meta.url);
  const source=await fs.readFile(sourceUrl,'utf8');
  const requests=[];let saved;
  const fakeFs={async open(){return {async close(){}};},async mkdir(){},
    async readFile(file){
      if(String(file).endsWith('.env'))return 'VAPI_API_KEY=PRIVATE-KEY\nVAPI_ASSISTANT_ID=assistant-fixture\nELEVENLABS_API_KEY=PRIVATE-TTS\nVAPI_ELEVENLABS_VOICE_ID=voice-fixture';
      if(String(file).endsWith('.pcm'))return Buffer.alloc(640,1);
      if(String(file).endsWith('voice_conversation_eval.mjs'))return Buffer.from(source);
      throw Error('unexpected_offline_read');
    },async writeFile(file,data){assert.match(String(file),/\.json$/);saved=JSON.parse(data);},
  };
  const fetch=async(url,options={})=>{
    const target=new URL(url);requests.push({path:target.pathname,options});
    const json=value=>new Response(JSON.stringify(value));
    if(target.pathname==='/api/config')return json({prompt_version:'2026-10-18-gemini-agentic-15',today:'2026-10-18',timezone:'America/Chicago'});
    if(target.pathname==='/api/sessions')return json({id:'session-fixture',token:'PRIVATE-CAPABILITY'});
    if(target.pathname==='/assistant/assistant-fixture')return json({model:{messages:[{content:'Synthetic prompt'}],tools:[]}});
    if(target.pathname==='/call')return json({id:'call-fixture',transport:{websocketCallUrl:'wss://fixture.vapi.ai/synthetic'}});
    if(target.pathname==='/api/sessions/session-fixture/end')return json({ended:true});
    if(target.pathname==='/api/sessions/session-fixture/gateway-timing'){
      assert.equal(options.signal.aborted,false,'Cleanup must ignore the expired voice deadline');
      return json(gateway);
    }
    if(target.pathname==='/call/call-fixture')return json({id:'call-fixture',assistantId:'assistant-fixture',status:'ended',artifact:{messages:[{role:'bot',message:'Synthetic greeting'}]}});
    throw Error('unexpected_offline_request');
  };
  class OfflineSocket {static OPEN=1;static CONNECTING=0;constructor(){throw Error('forced_transport_failure');}}
  const process={argv:['node',fileURLToPath(sourceUrl),'repairs','offline-fixture'],exitCode:null};
  const context={fs:fakeFs,path,fileURLToPath,parseEnv,createHash,AssistantCaptionTracker,faqHoursReady,waitForPersistedGreeting,collectGatewayTiming,
    fetch,WebSocket:OfflineSocket,Buffer,URL,AbortController,AbortSignal,performance,process,console:{log(){}},
    setTimeout(callback,ms){if(ms===145000)callback();return 1;},clearTimeout(){},clearInterval(){},setInterval(){throw Error('unexpected_timer');}};
  const body=source.replace(/^import .*;\r?\n/gm,'').replaceAll('import.meta.url',JSON.stringify(sourceUrl.href));
  await vm.runInNewContext('(async()=>{'+body+'\n})()',context);
  assert.ok(saved);assert.equal(process.exitCode,1);assert.equal(saved.failure,'forced_transport_failure');
  assert.equal(saved.transport_and_state_checks_passed,false);assert.equal(saved.local_session_ended,true);
  const end=requests.findIndex(row=>row.path.endsWith('/end')),timing=requests.findIndex(row=>row.path.endsWith('/gateway-timing'));
  assert.ok(end>=0&&timing>end);assert.equal(requests.filter(row=>row.path.endsWith('/gateway-timing')).length,1);
  assert.equal(requests[timing].options.headers['x-session-token'],'PRIVATE-CAPABILITY');
  assert.doesNotMatch(JSON.stringify(saved),/PRIVATE-CAPABILITY|PRIVATE-KEY|PRIVATE-TTS|PRIVATE-HEADER|PRIVATE-OUTPUT|PRIVATE-TRANSCRIPT/);
  return saved;
}

test('actual failed conversation runner still collects sanitized gateway evidence after ending its session',async()=>{
  const saved=await failedConversation(gatewayFixture());
  assert.equal(saved.gateway_timing_available,true);assert.equal(saved.gateway_timing_collected_after_session_end,true);
  assert.equal(saved.gateway_timing.requests[0].narration_reason,'issuance_context_changed');
  assert.equal(saved.gateway_timing.provider_call_id,'call-fixture');
});

test('actual failed conversation runner refuses a foreign gateway identity without losing the original failure',async()=>{
  const saved=await failedConversation({...gatewayFixture(),session_id:'other-session'});
  assert.equal(saved.gateway_timing_available,false);assert.equal(saved.gateway_timing_failure,'gateway_scope_mismatch');
  assert.equal(saved.gateway_timing,undefined);assert.equal(saved.failure,'forced_transport_failure');
});
