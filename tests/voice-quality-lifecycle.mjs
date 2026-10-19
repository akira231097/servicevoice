// Execute the actual runner end-to-end against a test-only virtual provider.
import assert from 'node:assert/strict';
import {test} from 'node:test';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawnSync} from 'node:child_process';

const fixture=new URL('./fixtures/voice-quality-runtime.mjs',import.meta.url).href;
async function run(mode){
  const temporary=await fs.mkdtemp(path.join(os.tmpdir(),'voice-quality-lifecycle-'));
  try{
    const project=path.join(temporary,'outputs','servicevoice');
    await fs.mkdir(path.join(project,'scripts'),{recursive:true});await fs.mkdir(path.join(project,'docs'));
    for(const filename of ['voice_quality_smoke.mjs','voice_regression_checks.mjs'])await fs.copyFile(fileURLToPath(new URL('../scripts/'+filename,import.meta.url)),path.join(project,'scripts',filename));
    await fs.writeFile(path.join(project,'.env'),'VAPI_API_KEY=synthetic-private-key\nVAPI_ASSISTANT_ID=synthetic-assistant\nELEVENLABS_API_KEY=synthetic-tts-key\nVAPI_ELEVENLABS_VOICE_ID=synthetic-voice\n');
    const reportFile=path.join(project,'docs','voice-quality-fixture.json'),statsFile=path.join(temporary,'mock-stats.json');
    // Do not inherit actual provider credentials into the test child.
    const env=Object.fromEntries(Object.entries(process.env).filter(([key])=>!/KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|VAPI|ELEVENLABS|GEMINI|GOOGLE/i.test(key)));
    Object.assign(env,{VOICE_MOCK_MODE:mode,VOICE_MOCK_REPORT:reportFile,VOICE_MOCK_STATS:statsFile});
    const result=spawnSync(process.execPath,['--import',fixture,path.join(project,'scripts','voice_quality_smoke.mjs'),'--scenario','services','--output','fixture','--evaluate'],{cwd:project,env,encoding:'utf8',timeout:10000});
    assert.equal(result.error,undefined,result.error?.message);
    const report=JSON.parse(await fs.readFile(reportFile,'utf8')),stats=JSON.parse(await fs.readFile(statsFile,'utf8'));
    const audio=await fs.readFile(path.join(project,'docs','voice-quality-fixture-assistant.wav'));
    assert.equal(report.last_checkpoint,'finalized');assert.equal(report.call_id,'synthetic-call');
    assert.equal(report.session_id,'synthetic-session');assert.equal(report.local_session_ended,true);
    assert.equal(stats.session_ended,true);assert.equal(stats.end_control,true);assert.equal(stats.held_timers_at_exit,0);
    assert.deepEqual(stats.checkpoint_at_socket,{call_id:'synthetic-call',last_checkpoint:'call_created'});
    assert.equal(report.provider_requests.create_call,1);assert.equal(report.provider_requests.tts,2);
    assert.equal(stats.requests.filter(row=>row.host==='api.vapi.ai'&&row.path==='/call'&&row.method==='POST').length,1);
    assert.equal(report.provider_ended_verified,true);assert.equal(report.assistant_audio.file_written,true);
    assert.ok(audio.length>44);assert.equal(audio.readUInt32LE(40),report.assistant_audio.pcm_bytes);
    assert.equal(report.semantic_review.status,'pending_human_review');assert.equal(report.semantic_review.audio_listened,false);
    assert.equal(JSON.stringify(report).includes('synthetic-private'),false);assert.equal(JSON.stringify(report).includes('synthetic-tts-key'),false);
    return {result,report,stats};
  }finally{
    assert.equal(path.dirname(path.resolve(temporary)),path.resolve(os.tmpdir()));
    assert.ok(path.basename(temporary).startsWith('voice-quality-lifecycle-'));
    await fs.rm(temporary,{recursive:true,force:true});
  }
}

test('complete runner waits for delayed greeting and answer captions, checkpoints both turns and cleans up',async()=>{
  const {result,report,stats}=await run('complete');assert.equal(result.status,0,result.stderr);
  assert.equal(report.transport_and_state_checks_passed,true);assert.equal(report.status,'completed');assert.equal(report.turns.length,2);
  assert.equal(stats.caller_starts.length,2);assert.ok(stats.caller_starts.every(row=>row.greeting_persisted));
  assert.equal(stats.caller_starts[0].last_checkpoint,'greeting_complete');
  assert.equal(stats.caller_starts[1].last_checkpoint,'turn_1_observed');assert.equal(stats.caller_starts[1].saved_turns,1);
  assert.deepEqual(report.turns.map(row=>row.assistant),[['We offer oil service and brake inspection.'],['Diagnostic inspection checks an unknown problem.']]);
  assert.ok(report.turns.every(row=>row.caller_transcripts.length===1&&row.assistant_quiet_observed));
  assert.ok(report.turns.every(row=>row.completion_evidence.audio_quiet&&row.completion_evidence.captions_settled
    &&row.completion_evidence.caption_quiet_window_satisfied&&row.completion_evidence.unmet_conditions.length===0));
  assert.ok(report.turns.every(row=>row.completion_evidence.deadline_ms-row.completion_evidence.input_end_ms===40000));
  assert.equal(report.maximum_provider_call_seconds,150);
});

test('transport close during an answer retains scope/audio evidence and performs final cleanup',async()=>{
  const {result,report,stats}=await run('transport-close');assert.equal(result.status,1,result.stderr);
  assert.equal(report.transport_and_state_checks_passed,false);assert.equal(report.status,'incomplete');
  assert.equal(report.failure,'call_deadline_or_transport_ended');assert.equal(stats.caller_starts.length,1);
  assert.ok(report.assistant_audio.audio_signal_observed);assert.equal(report.turns.length,1);
});

test('audible answer without its caption cannot pass using the leftover greeting',async()=>{
  const {result,report,stats}=await run('missing-caption');assert.equal(result.status,1,result.stderr);
  assert.equal(report.transport_and_state_checks_passed,false);assert.equal(stats.caller_starts.length,1);
  assert.equal(report.greeting.persisted_complete,true);assert.equal(report.turns.length,1);
  assert.ok(report.assistant_audio.audio_signal_observed);
  assert.equal(report.final_sdk_transcripts.filter(row=>row.role==='assistant').length,1,'Only the greeting was transcribed');
});

test('missing greeting caption stops within the bounded greeting stage without caller input',async()=>{
  const {result,report,stats}=await run('missing-greeting');assert.equal(result.status,1,result.stderr);
  assert.equal(report.transport_and_state_checks_passed,false);assert.equal(report.failure,'greeting_caption_or_audio_not_complete');
  assert.equal(stats.caller_starts.length,0);assert.equal(report.turns.length,0);
});

test('short final plus later partial waits for the full final and its delayed persistence',async()=>{
  const {result,report,stats}=await run('partial-delayed-final');assert.equal(result.status,0,result.stderr);
  assert.equal(report.transport_and_state_checks_passed,true);assert.equal(stats.caller_starts.length,2);
  assert.ok(stats.caller_starts[1].clock>=stats.final_caption_persisted[0]);
  assert.deepEqual(report.turns.map(row=>row.assistant),[['हाँ।','We offer oil service and brake inspection.'],['हाँ।','Diagnostic inspection checks an unknown problem.']]);
  assert.ok(report.turns.every(row=>row.assistant_captions_settled&&!row.pending_assistant_partial));
  assert.equal(report.pending_assistant_partial_at_end,false);
});

test('a partial that never finalizes fails within the turn deadline without sending another turn',async()=>{
  const {result,report,stats}=await run('partial-never-final');assert.equal(result.status,1,result.stderr);
  assert.equal(report.transport_and_state_checks_passed,false);assert.equal(stats.caller_starts.length,1);
  assert.equal(report.failure,'turn_transport_or_recognition_incomplete');
  assert.equal(report.turns[0].assistant_captions_settled,false);assert.equal(report.turns[0].pending_assistant_partial,true);
  assert.equal(report.turns[0].completion_evidence.audio_quiet,true);
  assert.equal(report.turns[0].completion_evidence.time_budget.exhausted_at_observation,true);
  assert.ok(report.turns[0].completion_evidence.unmet_conditions.includes('assistant_partial_pending'));
  assert.ok(report.turns[0].completion_evidence.unmet_conditions.includes('assistant_captions_not_settled'));
  assert.equal(report.pending_assistant_partial_at_end,true);assert.deepEqual(report.turns[0].assistant,['हाँ।']);
});
