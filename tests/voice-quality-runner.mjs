// Offline contract tests. No provider request, microphone, or runtime DB.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { SCENARIOS, PHONE_READBACK_FIXTURES, phoneReadbackGuardForScenario, MODEL, parseOptions, callerPayload, callerCacheKey, replyFlags,
  wav, readBounded, assertInformationalState, assertPhoneReadbackState, assertPhoneReadbackToolMessage, PhoneReadbackGuard,
  providerEvidence, sanitizeGateway, checkpointReport, finalizeEvidence, completionEvidence } from '../scripts/voice_quality_smoke.mjs';

const script=fileURLToPath(new URL('../scripts/voice_quality_smoke.mjs',import.meta.url));
const guard='data:text/javascript,'+encodeURIComponent(`
  import fs from 'node:fs/promises';
  globalThis.fetch=()=>{throw new Error('OFFLINE_NETWORK_FORBIDDEN');};
  globalThis.WebSocket=class{constructor(){throw new Error('OFFLINE_MEDIA_FORBIDDEN');}};
  fs.readFile=async()=>{throw new Error('OFFLINE_FILE_READ_FORBIDDEN');};
`);
const run=(args,target=script)=>spawnSync(process.execPath,['--import',guard,target,...args],{encoding:'utf8',timeout:5000,cwd:os.tmpdir()});

function completion(overrides={}) {
  return completionEvidence({callStarted:1000,observedAt:17500,inputEnd:10000,deadline:50000,
    lastStart:11000,lastStop:13000,lastVoiced:12900,lastText:14500,assistantSpeaking:false,
    pendingPartial:false,captionsSettled:true,assistantReplyPresent:true,callerRecognitionPresent:true,
    firstVoicedPcmRecorded:true,...overrides});
}
test('completion evidence distinguishes the observed 427 ms caption-window deadline deficit from audio activity',()=>{
  // The last voiced timestamp is a fixture, not inferred from the historical report.
  const evidence=completion({inputEnd:86084,deadline:126084,observedAt:126084,
    lastStart:92089,lastStop:113439,lastVoiced:113300,lastText:124011});
  assert.equal(evidence.input_end_ms,85084);assert.equal(evidence.deadline_ms,125084);
  assert.equal(evidence.last_text_activity_ms,123011);assert.equal(evidence.last_stop_ms,112439);
  assert.equal(evidence.last_voiced_ms,112300);assert.equal(evidence.audio_quiet,true);
  assert.equal(evidence.captions_settled,true);assert.equal(evidence.caption_quiet_window_satisfied,false);
  assert.equal(evidence.strict_quiet_predicate_at_observation,false);
  assert.deepEqual(evidence.unmet_conditions,['caption_quiet_window_pending']);
  assert.equal(evidence.time_budget.exhausted_at_observation,true);
  assert.equal(evidence.time_budget.caption_quiet_window_ready_at_ms,125511);
  assert.equal(evidence.time_budget.caption_quiet_window_beyond_deadline_ms,427);
  assert.equal(evidence.time_budget.remaining_ms,0);
  assert.equal(Object.hasOwn(evidence,'passed'),false,'Diagnostics must not supply a replacement verdict');
});
test('settled completion records independent audio/caption conditions and remaining budget',()=>{
  const evidence=completion();
  assert.equal(evidence.audio_quiet,true);assert.equal(evidence.caption_quiet_window_satisfied,true);
  assert.equal(evidence.captions_settled,true);assert.equal(evidence.strict_quiet_predicate_at_observation,true);
  assert.deepEqual(evidence.unmet_conditions,[]);assert.equal(evidence.time_budget.exhausted_at_observation,false);
  assert.equal(evidence.time_budget.remaining_ms,32500);assert.equal(evidence.time_budget.overrun_ms,0);
  assert.equal(evidence.phone_readback_ready,null);assert.equal(evidence.diagnostic_only,true);
});
test('actual active audio is not misclassified as a caption delay',()=>{
  const evidence=completion({assistantSpeaking:true,lastStart:17000});
  assert.equal(evidence.audio_quiet,false);assert.equal(evidence.captions_settled,true);
  assert.equal(evidence.caption_quiet_window_satisfied,true);
  assert.deepEqual(evidence.unmet_conditions,['assistant_audio_active','assistant_speech_stop_not_observed']);
  assert.equal(evidence.time_budget.audio_quiet_window_ready_at_ms,null);
});
test('recognition and persistence failures remain separate from elapsed quiet windows',()=>{
  const evidence=completion({callerRecognitionPresent:false,assistantReplyPresent:false,
    captionsSettled:false,pendingPartial:true,phoneReadbackReady:false});
  assert.equal(evidence.audio_quiet,true);assert.equal(evidence.caption_quiet_window_satisfied,true);
  assert.deepEqual(evidence.unmet_conditions,['assistant_partial_pending','assistant_captions_not_settled',
    'assistant_reply_not_observed','caller_recognition_not_observed','phone_readback_state_not_ready']);
  assert.equal(evidence.strict_quiet_predicate_at_observation,false);
});
test('diagnostic readiness after a deadline retains the missed budget and unrounded observations',()=>{
  const evidence=completion({observedAt:50000.25,lastText:47500.25});
  assert.equal(evidence.last_text_activity_ms,46500.25);assert.equal(evidence.observed_at_ms,49000.25);
  assert.equal(evidence.strict_quiet_predicate_at_observation,true);
  assert.equal(evidence.time_budget.exhausted_at_observation,true);assert.equal(evidence.time_budget.overrun_ms,.25);
  assert.equal(evidence.time_budget.caption_quiet_window_beyond_deadline_ms,.25);
  assert.equal(Object.hasOwn(evidence,'passed'),false);
});

test('default and all explicit dry runs read no credentials and make no provider calls',()=>{
  for(const args of [[],...Object.keys(SCENARIOS).map(name=>['--scenario',name])]){
    const result=run(args);assert.equal(result.status,0,result.stderr);
    const data=JSON.parse(result.stdout);assert.equal(data.mode,'dry_run');assert.equal(data.turns.length,2);
    assert.equal(data.provider_requests_made,0);assert.equal(data.files_written,0);
  }
});
test('phone readback is an explicit two-turn no-consent diagnostic with distinct staged and corrected digits',()=>{
  const turns=SCENARIOS['phone-readback'];assert.equal(turns.length,2);
  assert.match(turns[0].text,/five one two, five five zero, zero one four two/);
  assert.match(turns[0].text,/Alex Morgan/);assert.match(turns[0].text,/Toyota Camry/);
  assert.match(turns[1].text,/five, one, two, five, five, five, zero, one, four, two/);
  assert.ok(turns.every(turn=>/Do not .*book anything\./.test(turn.text)));
  assert.doesNotMatch(turns.map(turn=>turn.text).join(' '),/Yes,|I confirm|go ahead/i);
  assert.deepEqual(parseOptions(['--scenario','phone-readback']),{evaluate:false,scenario:'phone-readback',output:null});
  assert.throws(()=>parseOptions(['--scenario','phone-readback','--evaluate']),/invalid_arguments/);
  assert.deepEqual(parseOptions(['--scenario','phone-readback','--evaluate','--output','diagnostic-01']),{evaluate:true,scenario:'phone-readback',output:'diagnostic-01'});
});

function phoneState(overrides={}){
  return {id:'s',mode:'live',transcript:[],events:[{name:'prepare_first_available_booking',status:'success'}],pending_operations:[],
    service:'oil_change',selected_slot:'slot-one',customer:{name:'Alex Morgan',vehicle:'2021 Toyota Camry',phone:'+15125500142'},
    proposal:{id:'proposal-old',kind:'book',summary:'Synthetic readback'},...overrides};
}
test('phone diagnostic allows proposal tools while informational guards remain strict',()=>{
  const state=phoneState();assert.doesNotThrow(()=>assertPhoneReadbackState(state,'s'));
  assert.throws(()=>assertInformationalState(state,'s'),/unexpected_business_action/);
  for(const name of ['get_dealership_info','begin_service_request','search_availability','prepare_booking','prepare_first_available_booking'])
    assert.doesNotThrow(()=>assertPhoneReadbackState(phoneState({events:[{name,status:'success'}]}),'s'));
  for(const name of ['confirm_action','prepare_reschedule','prepare_cancellation','request_handoff','dismiss_proposal'])
    assert.throws(()=>assertPhoneReadbackState(phoneState({events:[{name,status:'error'}]}),'s'),/forbidden_phone_readback_action/);
  for(const field of ['appointment','handoff'])assert.throws(()=>assertPhoneReadbackState(phoneState({[field]:{}}),'s'),/forbidden_phone_readback_action/);
  assert.throws(()=>assertPhoneReadbackState(phoneState({proposal:{kind:'cancel'}}),'s'),/forbidden_phone_readback_action/);
  assert.throws(()=>assertPhoneReadbackState(state,'other'),/scope/);
});
test('phone diagnostic catches confirmation attempts in provider tool messages before successful results',()=>{
  for(const key of ['toolCalls','toolCallList']){
    assert.throws(()=>assertPhoneReadbackToolMessage({type:'tool-calls',[key]:[{function:{name:'confirm_action'}}]}),/forbidden_phone_readback_tool_attempt/);
    assert.doesNotThrow(()=>assertPhoneReadbackToolMessage({type:'tool-calls',[key]:[{function:{name:'prepare_booking'}}]}));
  }
  assert.throws(()=>assertPhoneReadbackToolMessage({type:'tool-calls',toolWithToolCallList:[{toolCall:{function:{name:'request_handoff'}}}]}),/forbidden_phone_readback_tool_attempt/);
  assert.doesNotThrow(()=>assertPhoneReadbackToolMessage({type:'tool-calls',toolWithToolCallList:[{toolCall:{function:{name:'search_availability'}}}]}));
  assert.throws(()=>assertPhoneReadbackToolMessage({type:'tool-calls'}),/forbidden_phone_readback_tool_attempt/);
});
test('phone readback records a fresh correct replacement in the same slot without claiming supersession or spoken accuracy',()=>{
  const guard=new PhoneReadbackGuard(),initial=phoneState();guard.observe(initial,'s');
  assert.equal(guard.responseReady(initial,['One moment.']),false);
  assert.equal(guard.responseReady(initial,['Your callback number is 512-550-0142.']),true);
  guard.recordTurn(0,initial);
  const replacement=phoneState({proposal:{id:'proposal-new',kind:'book'},customer:{...initial.customer,phone:'+15125550142'}});
  guard.observe(replacement,'s');guard.recordTurn(1,replacement);
  assert.equal(guard.evidence().replacement_state_verified,true);
  assert.equal(guard.evidence().consent_sent,false);
  assert.equal(guard.evidence().original_supersession_verified,false);
  assert.match(guard.evidence().spoken_phone_accuracy,/Pending separate audio review/);
  assert.equal(guard.evidence().snapshot_checks,2);
});
test('phone readback rejects stale IDs, wrong digits, changed slots or intake, and pending operations',()=>{
  const initial=phoneState();
  const replacement=phoneState({proposal:{id:'proposal-new',kind:'book'},customer:{...initial.customer,phone:'+15125550142'}});
  for(const changed of [initial,{...replacement,selected_slot:'other-slot'},{...replacement,customer:{...replacement.customer,phone:'+15125550143'}},
    {...replacement,customer:{...replacement.customer,name:'Different'}},{...replacement,pending_operations:[{id:'op'}]}]){
    const guard=new PhoneReadbackGuard();guard.recordTurn(0,initial);
    assert.throws(()=>guard.recordTurn(1,changed),/phone_readback_(stale_proposal|replacement_mismatch|proposal_incomplete)/);
  }
  assert.throws(()=>new PhoneReadbackGuard().recordTurn(0,replacement),/staged_phone_proposal_not_observed/);
  assert.throws(()=>new PhoneReadbackGuard().recordTurn(2,initial),/phone_readback_turn_limit/);
});

test('callback incident fixture selects only the fixed 0106 to 0176 two-turn no-booking correction',()=>{
  const turns=SCENARIOS['callback-incident'];assert.equal(turns.length,2);
  assert.match(turns[0].text,/plus one, two zero two, five five five, zero one zero six/);
  assert.match(turns[0].text,/oil change at the first available time/);
  assert.match(turns[0].text,/Alex Morgan/);assert.match(turns[0].text,/Toyota Camry/);
  assert.match(turns[1].text,/change only the callback number/);
  assert.match(turns[1].text,/plus one, two, zero, two, five, five, five, zero, one, seven, six/);
  assert.ok(turns.every(turn=>/Do not .*book anything\./.test(turn.text)));
  assert.doesNotMatch(turns.map(turn=>turn.text).join(' '),/Yes,|I confirm|go ahead/i);
  const selected=phoneReadbackGuardForScenario('callback-incident');
  assert.equal(selected.initialPhone,'+12025550106');assert.equal(selected.expectedPhone,'+12025550176');
  assert.deepEqual(PHONE_READBACK_FIXTURES['phone-readback'],{initialPhone:'+15125500142',expectedPhone:'+15125550142'});
  assert.equal(phoneReadbackGuardForScenario('services'),null);
  assert.throws(()=>parseOptions(['--scenario','callback-incident','--evaluate']),/invalid_arguments/);
  assert.deepEqual(parseOptions(['--scenario','callback-incident','--evaluate','--output','callback-synthetic-01']),
    {evaluate:true,scenario:'callback-incident',output:'callback-synthetic-01'});
});

test('phone fixture constructor is bounded, distinct and immutable while preserving original defaults',()=>{
  for(const config of [{initialPhone:'5125500142'},{expectedPhone:'+'+'1'.repeat(16)},
    {initialPhone:'+0123456789'},{expectedPhone:123},{expectedPhone:'+15125500142'}])
    assert.throws(()=>new PhoneReadbackGuard(config),/invalid_phone_readback_fixture/);
  const guard=new PhoneReadbackGuard();
  assert.equal(guard.initialPhone,'+15125500142');assert.equal(guard.expectedPhone,'+15125550142');
  assert.throws(()=>{guard.expectedPhone='+12025550176';},TypeError);
});

function incidentStates(){
  const initial=phoneState({customer:{name:'Alex Morgan',vehicle:'2021 Toyota Camry',phone:'+12025550106'}});
  const replacement=phoneState({proposal:{id:'incident-replacement',kind:'book',summary:'Corrected callback readback'},
    customer:{...initial.customer,phone:'+12025550176'}});
  return {initial,replacement};
}

test('callback incident guard verifies a fresh 0176 replacement without consent or booking authority',()=>{
  const guard=phoneReadbackGuardForScenario('callback-incident'),{initial,replacement}=incidentStates();
  guard.observe(initial,'s');guard.recordTurn(0,initial);
  guard.observe(replacement,'s');guard.recordTurn(1,replacement);guard.verifyFinal(replacement);
  assert.equal(guard.evidence().expected_initial_phone,'+12025550106');
  assert.equal(guard.evidence().expected_corrected_phone,'+12025550176');
  assert.equal(guard.evidence().replacement_state_verified,true);
  assert.equal(guard.evidence().consent_sent,false);assert.equal(guard.evidence().original_supersession_verified,false);
  assert.match(guard.evidence().spoken_phone_accuracy,/Pending separate audio review/);
});

test('callback incident rejects old digits, stale proposals, intake/slot drift and any attempted business commit',()=>{
  const {initial,replacement}=incidentStates();
  const invalid=[initial,{...replacement,customer:{...replacement.customer,phone:'+12025550106'}},
    {...replacement,selected_slot:'different-slot'},{...replacement,customer:{...replacement.customer,name:'Different'}},
    {...replacement,customer:{...replacement.customer,vehicle:'Different vehicle'}},{...replacement,service:'diagnostic'},
    {...replacement,appointment:{status:'confirmed'}},{...replacement,handoff:{status:'callback_requested'}},
    {...replacement,events:[{name:'confirm_action',status:'error'}]}];
  for(const changed of invalid){
    const guard=phoneReadbackGuardForScenario('callback-incident');guard.recordTurn(0,initial);
    assert.throws(()=>{guard.observe(changed,'s');guard.recordTurn(1,changed);},/phone_readback_/);
  }
  const guard=phoneReadbackGuardForScenario('callback-incident');guard.recordTurn(0,initial);guard.recordTurn(1,replacement);
  for(const changed of invalid)assert.throws(()=>guard.verifyFinal(changed),/phone_readback_/);
  assert.throws(()=>guard.verifyFinal({...replacement,pending_operations:[{id:'pending'}]}),/phone_readback_final_state_changed/);
});
test('evaluation requires one valid case and a unique bounded output label',()=>{
  for(const args of [['--evaluate'],['--scenario'],['--scenario','all','--evaluate','--output','safe'],
    ['--output','../escape','--evaluate'],['--output','MixedCase'],['--output','x'.repeat(61)],['--evaluate','--evaluate'],['--unknown']]){
    assert.throws(()=>parseOptions(args),/invalid_arguments/);
    const result=run(args);assert.equal(result.status,2);assert.doesNotMatch(result.stderr,/OFFLINE_/);
  }
  assert.deepEqual(parseOptions(['--evaluate','--scenario','hindi','--output','new-label']),{evaluate:true,scenario:'hindi',output:'new-label'});
});
test('report reservation refuses overwrite before any credential or external access',async()=>{
  const temporary=await fs.mkdtemp(path.join(os.tmpdir(),'voice-quality-cli-'));
  try{
    await fs.mkdir(path.join(temporary,'scripts'));await fs.mkdir(path.join(temporary,'docs'));
    const target=path.join(temporary,'scripts','voice_quality_smoke.mjs');await fs.copyFile(script,target);
    await fs.copyFile(fileURLToPath(new URL('../scripts/voice_regression_checks.mjs',import.meta.url)),path.join(temporary,'scripts','voice_regression_checks.mjs'));
    const report=path.join(temporary,'docs','voice-quality-existing.json');await fs.writeFile(report,'preserved');
    const result=run(['--evaluate','--output','existing'],target);
    assert.equal(result.status,2);assert.match(result.stderr,/EEXIST/);assert.doesNotMatch(result.stderr,/OFFLINE_/);
    assert.equal(await fs.readFile(report,'utf8'),'preserved');
  }finally{
    assert.equal(path.dirname(path.resolve(temporary)),path.resolve(os.tmpdir()));
    assert.ok(path.basename(temporary).startsWith('voice-quality-cli-'));await fs.rm(temporary,{recursive:true,force:true});
  }
});
test('caller v4 language binding is explicit and cache identity changes with language or voice',()=>{
  const turn=SCENARIOS.telugu[0],payload=callerPayload(turn,'synthetic-voice');
  assert.equal(MODEL,'eleven_v4');assert.equal(payload.model_id,MODEL);assert.equal(payload.language_code,'te');
  assert.deepEqual(payload.inputs,[{text:turn.text,voice_id:'synthetic-voice'}]);
  assert.equal(callerCacheKey(turn,'synthetic-voice'),callerCacheKey({...turn},'synthetic-voice'));
  assert.notEqual(callerCacheKey(turn,'synthetic-voice'),callerCacheKey({...turn,language:'hi'},'synthetic-voice'));
  assert.notEqual(callerCacheKey(turn,'synthetic-voice'),callerCacheKey(turn,'other-voice'));
  assert.ok(SCENARIOS.hindi.every(row=>/\p{Script=Devanagari}/u.test(row.text)));
  assert.ok(SCENARIOS.telugu.every(row=>/\p{Script=Telugu}/u.test(row.text)));
});
test('repetition is flagged without editing text or claiming a semantic verdict',()=>{
  const text='Diagnosis diagnosis. Oil change oil change. Tires.';
  const flags=replyFlags(text,'en');assert.equal(flags.advisory_only,true);
  assert.ok(flags.adjacent_repetition_candidates.some(row=>row.phrase==='diagnosis'));
  assert.ok(flags.adjacent_repetition_candidates.some(row=>row.phrase==='oil change'));
  assert.equal(replyFlags('तेल बदलना और ब्रेक की जाँच।','hi').target_script_present,true);
  assert.equal(replyFlags('బ్రేకుల తనిఖీ గురించి చెప్పగలను.','te').target_script_present,true);
  assert.equal(replyFlags('Telugu ni romanized ga cheppagalanu.','te').target_script_present,false);
  assert.equal(replyFlags('I only speak English.','hi').language_restriction_candidate,true);
  assert.equal(Object.hasOwn(flags,'passed'),false);
});
test('informational scenarios reject pending proposals, appointments, handoffs and attempted mutations',()=>{
  const state={id:'s',mode:'live',transcript:[],events:[{name:'get_dealership_info'}]};
  assert.doesNotThrow(()=>assertInformationalState(state,'s'));
  for(const key of ['appointment','proposal','handoff'])assert.throws(()=>assertInformationalState({...state,[key]:{}},'s'),/unexpected_business_action/);
  assert.throws(()=>assertInformationalState({...state,events:[{name:'prepare_first_available_booking',status:'error'}]},'s'),/unexpected_business_action/);
  assert.throws(()=>assertInformationalState({...state,id:'other'},'s'),/session_scope/);
});
test('WAV captures exact PCM once with a valid mono 16 kHz header',()=>{
  const pcm=Buffer.from([0,0,128,0,0,255]);const output=wav(pcm);
  assert.equal(output.toString('ascii',0,4),'RIFF');assert.equal(output.readUInt32LE(4),42);
  assert.equal(output.readUInt16LE(22),1);assert.equal(output.readUInt32LE(24),16000);
  assert.equal(output.readUInt32LE(40),pcm.length);assert.deepEqual(output.subarray(44),pcm);
  assert.throws(()=>wav(Buffer.alloc(3)),/audio_bounds/);
  assert.throws(()=>wav(Buffer.alloc(32000*150+2)),/audio_bounds/);
});
test('bounded response reader rejects oversize data and closes the stream',async()=>{
  let cancelled=false;
  const response=new Response(new ReadableStream({pull(controller){controller.enqueue(new Uint8Array(100));},cancel(){cancelled=true;}}));
  await assert.rejects(readBounded(response,50),/response_bounds/);assert.equal(cancelled,true);
});
test('provider and gateway evidence are call scoped and strip secret/metadata fields',()=>{
  const secret='SYNTHETIC_SECRET',provider={id:'call',assistantId:'assistant',status:'ended',sdp:secret,
    assistantOverrides:{variableValues:{session_id:'session',session_token:secret}},artifact:{messages:[
      {role:'system',message:secret},{role:'tool',message:secret},{role:'bot',message:'Synthetic reply',headers:secret,time:1}]} };
  const safe=providerEvidence(provider,'call','assistant','session');assert.equal(safe.messages.length,1);
  assert.equal(JSON.stringify(safe).includes(secret),false);
  assert.throws(()=>providerEvidence(provider,'different','assistant','session'),/scope/);
  assert.throws(()=>providerEvidence(provider,'call','assistant','different'),/scope/);
  const callKey=createHash('sha256').update('call').digest('hex').slice(0,16);
  const gateway={kind:'gemini_gateway_timing',requests:[{call_key:callKey,route:'gemini',outcome:'completed',prompt:secret,headers:secret,counts:{tool_calls:0,jev_requests:0},milestones_ms:{total:3,first_text:2}}]};
  const safeGateway=sanitizeGateway(gateway,'call');assert.equal(JSON.stringify(safeGateway).includes(secret),false);
  assert.equal(safeGateway.requests[0].tool_calls,0);assert.throws(()=>sanitizeGateway(gateway,'other'),/scope/);
});

test('gateway diagnostic retains exact narration provenance hashes without spoken text or transport data',()=>{
  const row={call_key:createHash('sha256').update('call').digest('hex').slice(0,16),route:'deterministic_receipt',outcome:'completed',
    narrator_version:'receipt-en-2026-10-18-4',source_result_sha256:'a'.repeat(64),output_text_sha256:'b'.repeat(64),
    output_text_source:'verified_receipt',system_prompt_sha256:'c'.repeat(64),output_text:'PRIVATE GENERATED TEXT',headers:{authorization:'PRIVATE TOKEN'}};
  const saved=sanitizeGateway({kind:'gemini_gateway_timing',requests:[row]},'call').requests[0];
  assert.equal(saved.narrator_version,row.narrator_version);assert.equal(saved.source_result_sha256,row.source_result_sha256);
  assert.equal(saved.output_text_sha256,row.output_text_sha256);assert.equal(saved.output_text_source,'verified_receipt');
  assert.equal(saved.system_prompt_sha256,row.system_prompt_sha256);assert.doesNotMatch(JSON.stringify(saved),/PRIVATE|authorization|"output_text"/);
  assert.equal(sanitizeGateway({kind:'gemini_gateway_timing',requests:[{...row,output_text_source:'static_faq'}]},'call').requests[0].output_text_source,'static_faq');
});

test('gateway provenance fields reject malformed hashes, arbitrary source strings and unbounded versions',()=>{
  const base={call_key:createHash('sha256').update('call').digest('hex').slice(0,16)};
  for(const invalid of ['a'.repeat(63),'a'.repeat(65),'g'.repeat(64),123,['a'.repeat(64)],{}]){
    const row=sanitizeGateway({kind:'gemini_gateway_timing',requests:[{...base,source_result_sha256:invalid,output_text_sha256:invalid,system_prompt_sha256:invalid}]},'call').requests[0];
    assert.equal(row.source_result_sha256,null);assert.equal(row.output_text_sha256,null);assert.equal(row.system_prompt_sha256,null);
  }
  for(const invalid of ['PRIVATE TEXT','receipt-en-2026-10-18-'+'1'.repeat(81),{},123]){
    const row=sanitizeGateway({kind:'gemini_gateway_timing',requests:[{...base,narrator_version:invalid,output_text_source:invalid}]},'call').requests[0];
    assert.equal(row.narrator_version,null);assert.equal(row.output_text_source,null);
  }
  assert.throws(()=>sanitizeGateway({kind:'gemini_gateway_timing',requests:[null]},'call'),/scope/);
  assert.throws(()=>sanitizeGateway({kind:'gemini_gateway_timing',requests:[]},'x'.repeat(201)),/scope/);
});

function memoryHandle() {
  return { data:Buffer.alloc(0),syncs:0,closes:0,
    async truncate(length){this.data=this.data.subarray(0,length);},
    async write(buffer,offset,length,position){
      // Force short writes, including UTF-8 split boundaries.
      const count=Math.min(length,17),next=Buffer.alloc(Math.max(this.data.length,position+count));
      this.data.copy(next);buffer.copy(next,position,offset,offset+count);this.data=next;return {bytesWritten:count};
    },
    async writeFile(data){this.data=Buffer.from(data);},
    async sync(){this.syncs++;},async close(){this.closes++;},
  };
}
test('checkpoints preserve safe IDs and successive turns, redact secrets and tolerate short writes',async()=>{
  const handle=memoryHandle(),secret='SECRET-session-capability';
  const report={session_id:'session',call_id:'call',turns:[],private_value:secret};
  await checkpointReport(handle,report,[secret]);
  assert.deepEqual(JSON.parse(handle.data),{session_id:'session',call_id:'call',turns:[],private_value:'[REDACTED]'});
  report.turns.push({assistant:'తెలుగులో సేవలు',caller:'Synthetic turn'});
  await checkpointReport(handle,report,[secret]);
  assert.equal(JSON.parse(handle.data).turns[0].assistant,'తెలుగులో సేవలు');
  assert.equal(JSON.parse(handle.data).call_id,'call');assert.equal(handle.syncs,2);
  assert.equal(handle.data.includes(Buffer.from(secret)),false);
});
test('audio-write failure still persists final JSON and closes both handles',async()=>{
  const handle=memoryHandle(),audio=memoryHandle(),report={status:'completed',transport_and_state_checks_passed:true,call_id:'call'};
  audio.writeFile=async()=>{throw new Error('PRIVATE filesystem failure');};
  await finalizeEvidence({handle,audioHandle:audio,report,pcm:Buffer.from([128,0]),voicedPcmBytes:2,audioFile:'synthetic.wav'});
  const saved=JSON.parse(handle.data);
  assert.equal(saved.call_id,'call');assert.equal(saved.status,'incomplete');assert.equal(saved.transport_and_state_checks_passed,false);
  assert.equal(saved.assistant_audio.failure,'audio_write_failed');assert.equal(saved.assistant_audio.file_written,false);
  assert.equal(saved.assistant_audio.available_for_listening,false);
  assert.equal(handle.data.includes(Buffer.from('PRIVATE')),false);assert.equal(handle.closes,1);assert.equal(audio.closes,1);
});
test('empty PCM and continuous zero PCM cannot claim positive audio evidence',async()=>{
  for(const pcm of [Buffer.alloc(0),Buffer.alloc(640)]){
    const handle=memoryHandle(),audio=memoryHandle(),report={status:'completed',transport_and_state_checks_passed:true};
    await finalizeEvidence({handle,audioHandle:audio,report,pcm,voicedPcmBytes:0,audioFile:'synthetic.wav'});
    const saved=JSON.parse(handle.data);
    assert.equal(saved.assistant_audio.file_written,true);
    assert.equal(saved.assistant_audio.pcm_bytes,pcm.length);
    assert.equal(saved.assistant_audio.audio_signal_observed,false);
    assert.equal(saved.assistant_audio.available_for_listening,false);
    assert.equal(saved.transport_and_state_checks_passed,false);
    assert.equal(saved.assistant_audio.failure,'no_voiced_audio_evidence');
    assert.equal(audio.data.length,44+pcm.length);
  }
});
test('JSON failure still closes report and audio handles without hiding the persistence failure',async()=>{
  const handle=memoryHandle(),audio=memoryHandle(),report={status:'completed',transport_and_state_checks_passed:true};
  handle.write=async()=>{throw new Error('report_write_failure');};
  await assert.rejects(finalizeEvidence({handle,audioHandle:audio,report,pcm:Buffer.from([128,0]),voicedPcmBytes:2,audioFile:'synthetic.wav'}),/report_write_failure/);
  assert.equal(audio.closes,1);assert.equal(handle.closes,1);
});
test('audio-close failure is recorded while final JSON is still saved',async()=>{
  const handle=memoryHandle(),audio=memoryHandle(),report={status:'completed',transport_and_state_checks_passed:true};
  audio.close=async()=>{audio.closes++;throw new Error('PRIVATE close error');};
  await finalizeEvidence({handle,audioHandle:audio,report,pcm:Buffer.from([128,0]),voicedPcmBytes:2,audioFile:'synthetic.wav'});
  const saved=JSON.parse(handle.data);assert.equal(saved.assistant_audio.failure,'audio_close_failed');
  assert.equal(saved.assistant_audio.available_for_listening,false);assert.equal(handle.closes,1);assert.equal(audio.closes,1);
  assert.equal(handle.data.includes(Buffer.from('PRIVATE')),false);
});
