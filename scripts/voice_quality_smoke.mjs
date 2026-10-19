/** Bounded synthetic language/repetition regression. Default is an offline plan.
 * Explicit --evaluate creates ONE paid Vapi WebSocket call; no user microphone.
 * Example: node scripts/voice_quality_smoke.mjs --scenario telugu --output v10-te-01 --evaluate
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseEnv } from 'node:util';
import { createHash } from 'node:crypto';
import { AssistantCaptionTracker, waitForPersistedGreeting } from './voice_regression_checks.mjs';

export const SCENARIOS = {
  'language-query': [
    { language:'en', reply_language:'hi', text:'Can you help me in Hindi?' },
    { language:'hi', reply_language:'hi', text:'कृपया हिंदी में बताइए कि आप कौन सी वाहन सेवाएँ देते हैं। अभी कोई अपॉइंटमेंट बुक मत कीजिए।' },
  ],
  services: [
    { language:'en', reply_language:'en', text:'What services do you offer? Please explain them once, and do not book anything.' },
    { language:'en', reply_language:'en', text:'Which of those is for checking an unknown problem? I am only asking about services, not booking.' },
  ],
  hindi: [
    { language:'hi', reply_language:'hi', text:'कृपया हिंदी में बताइए कि आपके यहाँ कौन सी वाहन सेवाएँ उपलब्ध हैं। अभी बुकिंग नहीं करनी है।' },
    { language:'hi', reply_language:'hi', text:'मेरा मतलब तेल बदलना नहीं, ब्रेक की जाँच है। केवल सेवा समझाइए, अपॉइंटमेंट बुक मत कीजिए।' },
  ],
  telugu: [
    { language:'te', reply_language:'te', text:'దయచేసి తెలుగులో చెప్పండి, మీ దగ్గర ఏ వాహన సేవలు ఉన్నాయి? ఇప్పుడు అపాయింట్‌మెంట్ బుక్ చేయవద్దు.' },
    { language:'te', reply_language:'te', text:'ఆయిల్ మార్పు కాదు, బ్రేకుల తనిఖీ గురించి అడుగుతున్నాను. సేవ గురించి మాత్రమే చెప్పండి, బుకింగ్ చేయవద్దు.' },
  ],
  'phone-readback': [
    {language:'en',reply_language:'en',text:"I'd like an oil change at the first available time. My name is Alex Morgan. My callback number is five one two, five five zero, zero one four two. I drive a two zero two one Toyota Camry. Please read back all the details. Do not book anything."},
    {language:'en',reply_language:'en',text:'The callback number is wrong. Please change only the callback number to five, one, two, five, five, five, zero, one, four, two. Read back all the corrected details. Do not confirm or book anything.'},
  ],
  'callback-incident': [
    {language:'en',reply_language:'en',text:"I'd like an oil change at the first available time. My name is Alex Morgan. My callback number is plus one, two zero two, five five five, zero one zero six. I drive a two zero two one Toyota Camry. Please read back all the details. Do not book anything."},
    {language:'en',reply_language:'en',text:'The callback number is wrong. Please change only the callback number to plus one, two, zero, two, five, five, five, zero, one, seven, six. Read back all the corrected details. Do not confirm or book anything.'},
  ],
};
export const PHONE_READBACK_FIXTURES = Object.freeze({
  'phone-readback': Object.freeze({initialPhone:'+15125500142',expectedPhone:'+15125550142'}),
  'callback-incident': Object.freeze({initialPhone:'+12025550106',expectedPhone:'+12025550176'}),
});
export const MODEL = 'eleven_v4';
const FORMAT = 'pcm_16000';
const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const LOCAL = 'http://127.0.0.1:8765';
const MAX_PCM_BYTES = 32000 * 150;
const READ_TOOLS = new Set(['get_dealership_info','get_appointment','get_session_context','get_operation']);
const PROPOSAL_TOOLS = new Set([...READ_TOOLS,'begin_service_request','search_availability','prepare_booking','prepare_first_available_booking']);
const hash = value => createHash('sha256').update(value).digest('hex');
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const safeCode = value => typeof value === 'string' && /^[a-zA-Z0-9_.-]{1,100}$/.test(value) ? value : null;
const nonnegative = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;

// Observation only: this does not choose a deadline, accept a turn, or retry it.
// Keep audio silence separate from delayed ASR captions and their quiet window.
export function completionEvidence({callStarted,observedAt,inputEnd,deadline,lastStart,lastStop,lastVoiced,lastText,
  assistantSpeaking,pendingPartial,captionsSettled,assistantReplyPresent,callerRecognitionPresent,
  firstVoicedPcmRecorded,stateSnapshotAvailable=true,phoneReadbackReady=null}) {
  const windowMs=2500;
  const freshStop=lastStop>inputEnd&&lastStop>=lastStart;
  const freshVoice=lastVoiced>inputEnd;
  const audioWindowReadyAt=Math.max(lastStop,lastVoiced)+windowMs;
  const captionWindowReadyAt=lastText+windowMs;
  const audioQuiet=!assistantSpeaking&&freshStop&&freshVoice&&observedAt>=audioWindowReadyAt;
  const captionQuiet=observedAt>=captionWindowReadyAt;
  const strictQuiet=audioQuiet&&!pendingPartial&&captionQuiet;
  const reasons=[];
  if(assistantSpeaking)reasons.push('assistant_audio_active');
  if(!freshStop)reasons.push('assistant_speech_stop_not_observed');
  if(!freshVoice)reasons.push('voiced_pcm_not_observed');
  if(!assistantSpeaking&&freshStop&&freshVoice&&!audioQuiet)reasons.push('audio_quiet_window_pending');
  if(pendingPartial)reasons.push('assistant_partial_pending');
  if(!captionsSettled)reasons.push('assistant_captions_not_settled');
  if(!captionQuiet)reasons.push('caption_quiet_window_pending');
  if(!assistantReplyPresent)reasons.push('assistant_reply_not_observed');
  if(!callerRecognitionPresent)reasons.push('caller_recognition_not_observed');
  if(!firstVoicedPcmRecorded)reasons.push('first_voiced_pcm_timing_missing');
  if(phoneReadbackReady===false)reasons.push('phone_readback_state_not_ready');
  const relative=value=>Number.isFinite(value)&&value>0 ? value-callStarted : null;
  return {
    version:'voice-completion-observation-1',diagnostic_only:true,
    observed_at_ms:observedAt-callStarted,input_end_ms:inputEnd-callStarted,deadline_ms:deadline-callStarted,
    last_start_ms:relative(lastStart),last_stop_ms:relative(lastStop),last_voiced_ms:relative(lastVoiced),
    last_text_activity_ms:relative(lastText),quiet_window_ms:windowMs,
    audio_quiet:audioQuiet,caption_quiet_window_satisfied:captionQuiet,captions_settled:captionsSettled,
    pending_assistant_partial:pendingPartial,assistant_speaking:assistantSpeaking,
    fresh_assistant_speech_stop:freshStop,fresh_voiced_pcm:freshVoice,
    assistant_reply_present:assistantReplyPresent,caller_recognition_present:callerRecognitionPresent,
    first_voiced_pcm_recorded:firstVoicedPcmRecorded,state_snapshot_available:stateSnapshotAvailable,
    phone_readback_ready:phoneReadbackReady,strict_quiet_predicate_at_observation:strictQuiet,
    unmet_conditions:reasons,
    time_budget:{exhausted_at_observation:observedAt>=deadline,remaining_ms:Math.max(0,deadline-observedAt),
      overrun_ms:Math.max(0,observedAt-deadline),
      audio_quiet_window_ready_at_ms:freshStop&&freshVoice?audioWindowReadyAt-callStarted:null,
      caption_quiet_window_ready_at_ms:lastText>0?relative(captionWindowReadyAt):null,
      caption_quiet_window_beyond_deadline_ms:Math.max(0,captionWindowReadyAt-deadline)},
    interpretation:'Timestamps are client observations relative to call start. last_text_activity includes observed assistant partial/final and user final captions. These diagnostics do not replace the original strict turn predicate or establish acoustic/semantic completion.'
  };
}

export function parseOptions(args) {
  const options = { evaluate:false, scenario:'services', output:null };
  const seen = new Set();
  for (let i=0;i<args.length;i++) {
    const flag=args[i];
    if (seen.has(flag) || !['--evaluate','--scenario','--output'].includes(flag)) throw new Error('invalid_arguments');
    seen.add(flag);
    if (flag==='--evaluate') options.evaluate=true;
    else if (flag==='--scenario') options.scenario=args[++i];
    else options.output=args[++i];
  }
  if (!Object.hasOwn(SCENARIOS,options.scenario) || (options.output!==null && !/^[a-z0-9][a-z0-9-]{0,59}$/.test(options.output || ''))
      || (options.evaluate && !options.output)) throw new Error('invalid_arguments');
  return options;
}
export function callerCacheKey(turn, voiceId) {
  return hash(JSON.stringify([turn.text,voiceId,MODEL,FORMAT,turn.language]));
}
export function callerPayload(turn, voiceId) {
  return { inputs:[{text:turn.text,voice_id:voiceId}], model_id:MODEL, language_code:turn.language };
}
export function replyFlags(text, expectedLanguage) {
  const tokens=(String(text).normalize('NFKC').toLowerCase().match(/[\p{L}\p{M}]+/gu)||[]).slice(0,2000);
  const repeats=[];
  for (let width=1;width<=4;width++) for (let i=0;i+2*width<=tokens.length;i++) {
    if (tokens.slice(i,i+width).join(' ')===tokens.slice(i+width,i+2*width).join(' ')) {
      const phrase=tokens.slice(i,i+width).join(' ');
      if (!repeats.some(item=>item.phrase===phrase) && repeats.length<12) repeats.push({phrase:phrase.slice(0,100),words:width});
    }
  }
  const script = expectedLanguage==='hi' ? /\p{Script=Devanagari}/u : expectedLanguage==='te' ? /\p{Script=Telugu}/u : /[A-Za-z]/;
  return { adjacent_repetition_candidates:repeats,
    expected_reply_language:expectedLanguage, target_script_present:script.test(String(text)),
    language_restriction_candidate:/\b(?:only (?:speak|support|understand)|(?:can(?:not|'t)|do not|don't) (?:speak|support|understand))\b/i.test(text),
    advisory_only:true,
    interpretation:'Text/script heuristics only. Repetition may be legitimate; absent script may reflect romanized captions. No automatic language, acoustic or semantic pass.' };
}
export function wav(pcm) {
  if (!Buffer.isBuffer(pcm) || pcm.length>MAX_PCM_BYTES || pcm.length%2) throw new Error('audio_bounds');
  const header=Buffer.alloc(44);
  header.write('RIFF',0);header.writeUInt32LE(pcm.length+36,4);header.write('WAVEfmt ',8);
  header.writeUInt32LE(16,16);header.writeUInt16LE(1,20);header.writeUInt16LE(1,22);
  header.writeUInt32LE(16000,24);header.writeUInt32LE(32000,28);header.writeUInt16LE(2,32);header.writeUInt16LE(16,34);
  header.write('data',36);header.writeUInt32LE(pcm.length,40);
  return Buffer.concat([header,pcm]);
}
export async function readBounded(response, maximum) {
  const chunks=[];let bytes=0;
  for await (const chunk of response.body) {
    bytes+=chunk.length;
    if (bytes>maximum) throw new Error('response_bounds');
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}
export function assertInformationalState(value, sid) {
  if (value?.id!==sid || value.mode!=='live' || !Array.isArray(value.transcript) || !Array.isArray(value.events)) throw new Error('session_scope_or_shape');
  if (value.appointment || value.proposal || value.handoff || value.events.some(event=>!READ_TOOLS.has(event.name))) throw new Error('unexpected_business_action');
  if (value.transcript.length>2000 || value.events.length>256) throw new Error('session_evidence_bounds');
}
export function assertPhoneReadbackState(value, sid) {
  if(value?.id!==sid||value.mode!=='live'||!Array.isArray(value.transcript)||!Array.isArray(value.events))throw new Error('session_scope_or_shape');
  if(value.transcript.length>2000||value.events.length>256)throw new Error('session_evidence_bounds');
  if(value.appointment||value.handoff||(value.proposal&&value.proposal.kind!=='book')
      ||value.events.some(event=>!PROPOSAL_TOOLS.has(event.name)))throw new Error('forbidden_phone_readback_action');
}
export function assertPhoneReadbackToolMessage(message) {
  if(message?.type!=='tool-calls')return;
  const calls=Array.isArray(message.toolWithToolCallList)?message.toolWithToolCallList.map(row=>row?.toolCall)
    :message.toolCallList??message.toolCalls;
  if(!Array.isArray(calls)||calls.length>16||calls.some(call=>!PROPOSAL_TOOLS.has(call?.function?.name)))throw new Error('forbidden_phone_readback_tool_attempt');
}
export class PhoneReadbackGuard {
  initial=null;
  replacement=null;
  snapshotChecks=0;
  constructor({initialPhone='+15125500142',expectedPhone='+15125550142'}={}) {
    if(typeof initialPhone!=='string'||typeof expectedPhone!=='string'||!/^\+[1-9][0-9]{7,14}$/.test(initialPhone)
        ||!/^\+[1-9][0-9]{7,14}$/.test(expectedPhone)||initialPhone===expectedPhone)throw new Error('invalid_phone_readback_fixture');
    Object.defineProperties(this,{initialPhone:{value:initialPhone,enumerable:true},expectedPhone:{value:expectedPhone,enumerable:true}});
  }
  observe(value,sid) {assertPhoneReadbackState(value,sid);this.snapshotChecks++;}
  responseReady(value,replies) {
    return value?.proposal?.kind==='book'&&typeof value.proposal.id==='string'&&typeof value.selected_slot==='string'
      &&!value.pending_operations?.length&&/\b(callback|phone|number)\b/i.test(replies.join(' '));
  }
  recordTurn(index,value) {
    if(value?.proposal?.kind!=='book'||!value.proposal.id||!value.selected_slot||value.pending_operations?.length)
      throw new Error('phone_readback_proposal_incomplete');
    const observed={proposal_id:value.proposal.id,phone:value.customer?.phone,slot_id:value.selected_slot,
      customer_name:value.customer?.name,vehicle:value.customer?.vehicle,service:value.service,summary:value.proposal.summary};
    if(index===0){
      if(this.initial||observed.phone!==this.initialPhone||observed.service!=='oil_change')throw new Error('staged_phone_proposal_not_observed');
      this.initial=observed;
    }else if(index===1){
      if(!this.initial||observed.proposal_id===this.initial.proposal_id)throw new Error('phone_readback_stale_proposal');
      if(observed.phone!==this.expectedPhone||observed.slot_id!==this.initial.slot_id||observed.customer_name!==this.initial.customer_name
          ||observed.vehicle!==this.initial.vehicle||observed.service!==this.initial.service)throw new Error('phone_readback_replacement_mismatch');
      this.replacement=observed;
    }else throw new Error('phone_readback_turn_limit');
    return observed;
  }
  verifyFinal(value) {
    assertPhoneReadbackState(value,value?.id);
    if(!this.replacement||value.proposal?.id!==this.replacement.proposal_id||value.customer?.phone!==this.expectedPhone
        ||value.selected_slot!==this.replacement.slot_id||value.customer?.name!==this.replacement.customer_name
        ||value.customer?.vehicle!==this.replacement.vehicle||value.service!==this.replacement.service||value.pending_operations?.length)
      throw new Error('phone_readback_final_state_changed');
  }
  evidence() {
    return {expected_initial_phone:this.initialPhone,expected_corrected_phone:this.expectedPhone,
      initial:this.initial,replacement:this.replacement,snapshot_checks:this.snapshotChecks,
      replacement_state_verified:Boolean(this.initial&&this.replacement),consent_sent:false,
      original_supersession_verified:false,
      supersession_scope:'Snapshot shows the fresh pending replacement, not the historical old proposal row. Independent read-only database inspection is required for old-row supersession.',
      spoken_phone_accuracy:'Pending separate audio review; neither captions nor stored state establish what was spoken.'};
  }
}
export function phoneReadbackGuardForScenario(scenario) {
  return Object.hasOwn(PHONE_READBACK_FIXTURES,scenario)?new PhoneReadbackGuard(PHONE_READBACK_FIXTURES[scenario]):null;
}
export function providerEvidence(value, cid, assistantId, sid) {
  if (value?.id!==cid || value.assistantId!==assistantId
      || (value.assistantOverrides?.variableValues?.session_id!==undefined && value.assistantOverrides.variableValues.session_id!==sid)) throw new Error('provider_scope_mismatch');
  const messages=value.artifact?.messages || value.messages || [];
  if (!Array.isArray(messages) || messages.length>2000) throw new Error('provider_evidence_bounds');
  return { status:safeCode(value.status),ended_reason:safeCode(value.endedReason),cost_usd:nonnegative(value.cost),
    messages:messages.filter(row=>['user','bot','assistant'].includes(row?.role)).map(row=>({
      role:row.role==='user'?'user':'assistant',text:typeof row.message==='string'?row.message.slice(0,8000):'',
      time:nonnegative(row.time),endTime:nonnegative(row.endTime),secondsFromStart:nonnegative(row.secondsFromStart),
    })) };
}
export function sanitizeGateway(value, callId) {
  if (value?.kind!=='gemini_gateway_timing' || !Array.isArray(value.requests) || value.requests.length>256) throw new Error('gateway_evidence_bounds');
  if (typeof callId!=='string' || !callId.length || callId.length>200 || value.requests.some(row=>!row||row.call_key!==hash(callId).slice(0,16))) throw new Error('gateway_scope_mismatch');
  const sha256=value=>typeof value==='string'&&/^[a-f0-9]{64}$/.test(value)?value:null;
  return { requests:value.requests.map(row=>({route:safeCode(row.route),outcome:safeCode(row.outcome),
    total_ms:nonnegative(row.milestones_ms?.total),first_text_ms:nonnegative(row.milestones_ms?.first_text),
    tool_calls:nonnegative(row.counts?.tool_calls),jev_requests:nonnegative(row.counts?.jev_requests),
    system_prompt_sha256:sha256(row.system_prompt_sha256),
    source_result_sha256:sha256(row.source_result_sha256),output_text_sha256:sha256(row.output_text_sha256),
    output_text_source:['verified_receipt','static_faq'].includes(row.output_text_source)?row.output_text_source:null,
    narrator_version:typeof row.narrator_version==='string'&&row.narrator_version.length<=80
      &&/^receipt-en-\d{4}-\d{2}-\d{2}-\d+$/.test(row.narrator_version)?row.narrator_version:null,
  })) };
}

export async function checkpointReport(handle, report, secrets=[]) {
  let serialized=JSON.stringify(report,null,2)+'\n';
  for(const value of secrets)if(typeof value==='string'&&value.length)serialized=serialized.replaceAll(value,'[REDACTED]');
  const data=Buffer.from(serialized);
  await handle.truncate(0);
  let offset=0;
  while(offset<data.length){
    const {bytesWritten}=await handle.write(data,offset,data.length-offset,offset);
    if(!Number.isInteger(bytesWritten)||bytesWritten<=0)throw new Error('report_write_stalled');
    offset+=bytesWritten;
  }
  await handle.sync();
}

export async function finalizeEvidence({handle,audioHandle,report,pcm,voicedPcmBytes,audioFile,secrets=[]}) {
  const signalObserved=Number.isSafeInteger(voicedPcmBytes)&&voicedPcmBytes>0&&voicedPcmBytes<=pcm.length;
  report.assistant_audio={file:audioFile,file_written:false,available_for_listening:false,
    pcm_bytes:pcm.length,voiced_pcm_bytes:voicedPcmBytes,audio_signal_observed:signalObserved,
    sample_rate:16000,channels:1,seconds:pcm.length/32000,
    scope:'Remote synthetic call PCM in arrival order, including greeting/silence. No microphone recording; not yet listened to or independently transcribed.'};
  try{
    try{
      const audio=wav(pcm);await audioHandle.writeFile(audio);await audioHandle.sync();
      report.assistant_audio.file_written=true;
      report.assistant_audio.sha256=hash(audio);
      report.assistant_audio.available_for_listening=signalObserved;
      if(!signalObserved){report.assistant_audio.failure='no_voiced_audio_evidence';report.status='incomplete';report.transport_and_state_checks_passed=false;}
    }catch{
      report.assistant_audio.failure='audio_write_failed';report.status='incomplete';report.transport_and_state_checks_passed=false;
    }finally{
      try{await audioHandle.close();}catch{
        report.assistant_audio.failure='audio_close_failed';report.assistant_audio.available_for_listening=false;
        report.status='incomplete';report.transport_and_state_checks_passed=false;
      }
    }
  }finally{
    report.finished_at=new Date().toISOString();
    try{await checkpointReport(handle,report,secrets);}finally{await handle.close();}
  }
}

export async function evaluate(options) {
  const phoneGuard=phoneReadbackGuardForScenario(options.scenario);
  const basename=`voice-quality-${options.output}`;
  // Both destinations are reserved before credentials or provider work.
  const handle=await fs.open(path.join(ROOT,'docs',basename+'.json'),'wx');
  let audioHandle;
  try { audioHandle=await fs.open(path.join(ROOT,'docs',basename+'-assistant.wav'),'wx'); }
  catch (error) { try{await checkpointReport(handle,{status:'not_started',failure:error.code==='EEXIST'?'audio_output_exists':'audio_output_unavailable'});}finally{await handle.close();}throw error; }
  const report={kind:'synthetic_voice_quality_regression',scenario:options.scenario,started_at:new Date().toISOString(),
    status:'running',synthetic_inputs_only:true,microphone_used:false,telephone_used:false,
    maximum_provider_call_seconds:150,maximum_user_turns:2,maximum_tts_requests:2,automatic_retries:false,
    provider_requests:{tts:0,create_call:0,read_assistant:0,read_call:0},caller_audio:[],turns:[],
    transport_and_state_checks_passed:false,semantic_review:{status:'pending_human_review',audio_listened:false,independent_audio_transcription:false},
    limitations:['Synthetic caller audio is generated, not a native-speaker validated pronunciation fixture.',
      'Saved assistant WAV contains only the remote stream of this explicitly synthetic call; no human microphone is captured.',
      'Text repetition/script flags are advisory. Review WAV and both provider/caller transcripts for actual language and duplicated audio.',
      'Quiet completion requires speech-stop, 2.5 seconds without voiced PCM/caption activity, no pending observed assistant partial caption, and persistence of observed final captions. Unseen provider events and semantic completion are not independently verified.',
      phoneGuard?'No booking consent is supplied. Only read/search/booking-proposal preparation is permitted; any confirmation attempt, appointment or advisor request fails this diagnostic.'
        :'No booking consent is supplied. These cases must produce no appointment, proposal, or advisor request.',
      'Timing is client-observed PCM/event timing, not independent acoustic latency.',
      'Reported Vapi cost is not total test cost: separately billed model/voice usage and synthetic caller TTS can add charges.'],
    caller_tts_source:'https://elevenlabs.io/docs/api-reference/text-to-dialogue/convert',
    caller_model_source:'https://elevenlabs.io/docs/overview/models'};
  if(phoneGuard){report.diagnostic_only=true;report.phone_readback=phoneGuard.evidence();report.limitations.push('Tool-event/state guards detect forbidden behavior and end the call; they are not a separate server-side authorization policy.');}
  let env={},config,session,call,socket,timer,silenceTimer,callStarted=0,inputPlaying=false,ended=false,fault=false;
  let businessGuardFailure=null;
  let assistantSpeaking=false,lastStart=0,lastStop=0,lastVoiced=0,lastText=0,pcmBytes=0,voicedPcmBytes=0,messageCount=0;
  const chunks=[],events=[],finals=[],life=new AbortController();
  const assistantCaptions=new AssistantCaptionTracker();
  const now=()=>performance.now();
  const secrets=()=>[...Object.entries(env).filter(([key,value])=>typeof value==='string'&&value.length>=8&&/KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL/.test(key)).map(([,value])=>value),session?.token].filter(Boolean);
  const checkpoint=async stage=>{report.last_checkpoint=stage;report.checkpoint_at=new Date().toISOString();await checkpointReport(handle,report,secrets());};
  const active=()=>{if(businessGuardFailure)throw new Error(businessGuardFailure);if(life.signal.aborted || ended || fault)throw new Error('call_deadline_or_transport_ended');};
  const request=async(url,options={},cleanup=false,timeout=20000)=>{
    const response=await fetch(url,{...options,redirect:'error',signal:cleanup?AbortSignal.timeout(4000):AbortSignal.any([life.signal,AbortSignal.timeout(timeout)])});
    if(!response.ok)throw new Error(`HTTP_${response.status}`);return response;
  };
  const json=async(url,options={},cleanup=false,maximum=512*1024)=>JSON.parse((await readBounded(await request(url,options,cleanup),maximum)).toString('utf8'));
  const sessionHeaders=()=>({'x-session-token':session.token});
  const snapshot=async(cleanup=false)=>{
    const value=await json(`${LOCAL}/api/sessions/${session.id}`,{headers:sessionHeaders()},cleanup);
    if(phoneGuard){phoneGuard.observe(value,session.id);report.phone_readback=phoneGuard.evidence();}
    else assertInformationalState(value,session.id);
    return value;
  };
  const quiet=after=>!assistantSpeaking && !assistantCaptions.pendingPartial && lastStop>after && lastStop>=lastStart && lastVoiced>after
    && now()-Math.max(lastStop,lastVoiced,lastText)>=2500;
  const endSocket=()=>{
    clearInterval(silenceTimer);
    if(socket?.readyState===WebSocket.OPEN){try{socket.send(JSON.stringify({type:'end-call'}));report.end_control_sent=true;}catch{}try{socket.close();}catch{}}
    else if(socket?.readyState===WebSocket.CONNECTING){try{socket.close();}catch{}}
  };
  try {
    await checkpoint('initialized');
    env={...parseEnv(await fs.readFile(path.join(ROOT,'.env'),'utf8')),...process.env};
    for(const key of ['VAPI_API_KEY','VAPI_ASSISTANT_ID','ELEVENLABS_API_KEY','VAPI_ELEVENLABS_VOICE_ID'])if(!env[key])throw new Error('configuration_missing');
    report.runner_sha256=hash(await fs.readFile(fileURLToPath(import.meta.url)));
    const cache=path.resolve(ROOT,'../../work/runtime/voice-quality-pcm');await fs.mkdir(cache,{recursive:true});
    const clips=[];
    for(const turn of SCENARIOS[options.scenario]){
      const cacheKey=callerCacheKey(turn,env.VAPI_ELEVENLABS_VOICE_ID),file=path.join(cache,cacheKey+'.pcm');
      let pcm,source='cache';
      try { const stat=await fs.stat(file);if(!stat.isFile()||stat.size>32000*30||stat.size<2||stat.size%2)throw new Error('caller_pcm_bounds');pcm=await fs.readFile(file); }
      catch(error){
        if(error.code!=='ENOENT')throw error;
        source='generated';report.provider_requests.tts++;
        await checkpoint('caller_audio_requested');
        const response=await request('https://api.elevenlabs.io/v1/text-to-dialogue?output_format=pcm_16000',{
          method:'POST',headers:{'xi-api-key':env.ELEVENLABS_API_KEY,'Content-Type':'application/json'},
          body:JSON.stringify(callerPayload(turn,env.VAPI_ELEVENLABS_VOICE_ID)),
        },false,45000);
        pcm=await readBounded(response,32000*30);
        if(!pcm.length||pcm.length%2)throw new Error('caller_pcm_bounds');
        await fs.writeFile(file,pcm,{flag:'wx'});
      }
      clips.push(pcm);report.caller_audio.push({language:turn.language,requested_reply_language:turn.reply_language,
        text:turn.text,source,model:MODEL,format:FORMAT,phrase_sha256:hash(turn.text),cache_key_sha256:cacheKey,pcm_sha256:hash(pcm),duration_seconds:pcm.length/32000});
    }
    config=await json(LOCAL+'/api/config',{},false,64000);
    if(!config.live_voice_available||config.vapi_assistant_id!==env.VAPI_ASSISTANT_ID)throw new Error('live_configuration_mismatch');
    report.runtime={prompt_version:safeCode(config.prompt_version),receipt_narration_enabled:config.receipt_narration_enabled===true,jev_faq_enabled:config.jev_faq_enabled===true};
    report.provider_requests.read_assistant++;
    const saved=await json('https://api.vapi.ai/assistant/'+env.VAPI_ASSISTANT_ID,{headers:{Authorization:'Bearer '+env.VAPI_API_KEY}},false,1024*1024);
    if(saved.id!==env.VAPI_ASSISTANT_ID)throw new Error('assistant_scope_mismatch');
    report.saved_profile={model:safeCode(saved.model?.model),voice_model:safeCode(saved.voice?.model),
      transcriber_provider:safeCode(saved.transcriber?.provider),transcriber_model:safeCode(saved.transcriber?.model),transcriber_language:safeCode(saved.transcriber?.language)};
    session=await json(LOCAL+'/api/sessions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:'live'})});
    report.session_id=session.id;
    report.provider_requests.create_call++;
    await checkpoint('call_creation_requested');
    call=await json('https://api.vapi.ai/call',{method:'POST',headers:{Authorization:'Bearer '+env.VAPI_API_KEY,'Content-Type':'application/json'},body:JSON.stringify({
      assistantId:env.VAPI_ASSISTANT_ID,assistantOverrides:{maxDurationSeconds:150,variableValues:{session_id:session.id,session_token:session.token,current_date:config.today,timezone:config.timezone}},
      transport:{provider:'vapi.websocket',audioFormat:{format:'pcm_s16le',container:'raw',sampleRate:16000}},
    })});
    report.call_id=call.id;callStarted=now();timer=setTimeout(()=>{life.abort();endSocket();},145000);
    await checkpoint('call_created');active();
    const ws=new URL(call.transport?.websocketCallUrl);
    if(ws.protocol!=='wss:'||!ws.hostname.endsWith('.vapi.ai'))throw new Error('unexpected_transport_url');
    socket=new WebSocket(ws);socket.binaryType='arraybuffer';
    socket.addEventListener('error',()=>{fault=true;});socket.addEventListener('close',()=>{ended=true;});
    socket.addEventListener('message',event=>{
      if(typeof event.data!=='string'){
        const pcm=Buffer.from(event.data);
        if(pcm.length%2||pcmBytes+pcm.length>MAX_PCM_BYTES){fault=true;endSocket();return;}
        pcmBytes+=pcm.length;chunks.push(pcm);let peak=0;for(let i=0;i<pcm.length;i+=2)peak=Math.max(peak,Math.abs(pcm.readInt16LE(i)));
        if(peak>128){voicedPcmBytes+=pcm.length;lastVoiced=now();const turn=report.turns.at(-1);if(!inputPlaying&&turn?.input_end_ms!==undefined&&turn.first_voiced_pcm_after_input_ms===undefined)turn.first_voiced_pcm_after_input_ms=lastVoiced-callStarted-turn.input_end_ms;}
        return;
      }
      if(++messageCount>20000||event.data.length>256*1024){fault=true;endSocket();return;}
      try{
        const message=JSON.parse(event.data);
        if(phoneGuard){try{assertPhoneReadbackToolMessage(message);}catch(error){businessGuardFailure=safeCode(error.message)||'forbidden_phone_readback_tool_attempt';endSocket();return;}}
        if(assistantCaptions.observe(message))lastText=now();
        if(message.type==='speech-update'&&message.role==='assistant'){
          if(message.status==='started'){assistantSpeaking=true;lastStart=now();}
          if(message.status==='stopped'){assistantSpeaking=false;lastStop=now();}
          if(events.length<1000)events.push({type:'assistant-speech',status:safeCode(message.status),observed_ms:Math.round(now()-callStarted)});
        }
        if(message.type==='transcript'&&message.transcriptType==='final'&&['user','assistant'].includes(message.role)&&typeof message.transcript==='string'){
          lastText=now();if(finals.length<2000)finals.push({role:message.role,text:message.transcript.slice(0,8000),observed_ms:Math.round(lastText-callStarted)});
        }
        if(message.type==='status-update'&&message.status==='ended')ended=true;
      }catch{}
    });
    await new Promise((resolve,reject)=>{
      const timeout=setTimeout(()=>reject(new Error('websocket_open_timeout')),12000);
      socket.addEventListener('open',()=>{clearTimeout(timeout);resolve();},{once:true});
      socket.addEventListener('error',()=>{clearTimeout(timeout);reject(new Error('websocket_open_failed'));},{once:true});
    });
    silenceTimer=setInterval(()=>{
      if(inputPlaying||socket.readyState!==WebSocket.OPEN)return;
      if(socket.bufferedAmount>128000){fault=true;endSocket();return;}
      try{socket.send(new Uint8Array(640));}catch{fault=true;endSocket();}
    },20);
    const greeting=await waitForPersistedGreeting({snapshot,quiet,active,now,sleep:wait,callStarted,
      captionsSettled:transcript=>assistantCaptions.settled(transcript)});
    report.greeting={persisted_complete:true,audio_quiet_observed:true,
      transcript_rows:greeting.transcript.length,completed_ms:Math.round(now()-callStarted)};
    await checkpoint('greeting_complete');
    for(let index=0;index<clips.length;index++){
      active();const before=await snapshot(),baseline=before.transcript.length,pcm=clips[index],start=now();
      const turn={index,requested_reply_language:SCENARIOS[options.scenario][index].reply_language,input_started_ms:Math.round(start-callStarted)};
      report.turns.push(turn);inputPlaying=true;
      try{for(let offset=0;offset<pcm.length;offset+=640){
        active();if(socket.readyState!==WebSocket.OPEN||socket.bufferedAmount>128000)throw new Error('input_transport_unavailable');
        socket.send(pcm.subarray(offset,Math.min(offset+640,pcm.length)));
        await wait(Math.max(0,start+Math.min(offset+640,pcm.length)/32-now()));
      }}finally{inputPlaying=false;}
      const inputEnd=now();turn.input_end_ms=Math.round(inputEnd-callStarted);
      const deadline=Math.min(now()+40000,callStarted+140000);let state,replies=[];
      while(now()<deadline){
        active();state=await snapshot();replies=state.transcript.slice(baseline).filter(row=>row.role==='assistant').map(row=>row.text);
        if(replies.some(text=>text.trim())&&quiet(inputEnd)&&assistantCaptions.settled(state.transcript)
            &&(!phoneGuard||phoneGuard.responseReady(state,replies)))break;
        await wait(250);
      }
      turn.assistant=replies;turn.caller_transcripts=(state?.transcript||[]).slice(baseline).filter(row=>row.role==='user').map(row=>row.text);
      turn.assistant_quiet_observed=quiet(inputEnd);
      turn.assistant_captions_settled=assistantCaptions.settled(state?.transcript);
      turn.pending_assistant_partial=assistantCaptions.pendingPartial;
      turn.completion_evidence=completionEvidence({callStarted,observedAt:now(),inputEnd,deadline,
        lastStart,lastStop,lastVoiced,lastText,assistantSpeaking,pendingPartial:assistantCaptions.pendingPartial,
        captionsSettled:turn.assistant_captions_settled,assistantReplyPresent:replies.some(text=>text.trim()),
        callerRecognitionPresent:turn.caller_transcripts.some(text=>text.trim()),
        firstVoicedPcmRecorded:turn.first_voiced_pcm_after_input_ms!==undefined,stateSnapshotAvailable:state!==undefined,
        phoneReadbackReady:phoneGuard?phoneGuard.responseReady(state,replies):null});
      turn.advisory_flags=replyFlags(replies.join(' '),turn.requested_reply_language);
      await checkpoint(`turn_${index+1}_observed`);
      if(!turn.assistant_quiet_observed||!turn.assistant_captions_settled||!replies.length||!turn.caller_transcripts.length||turn.first_voiced_pcm_after_input_ms===undefined)throw new Error('turn_transport_or_recognition_incomplete');
      if(phoneGuard){
        if(!phoneGuard.responseReady(state,replies))throw new Error('phone_readback_response_incomplete');
        turn.proposal_observation=phoneGuard.recordTurn(index,state);report.phone_readback=phoneGuard.evidence();
        await checkpoint(`phone_readback_${index+1}_recorded`);
      }
    }
    const final=await snapshot();
    if(phoneGuard){
      phoneGuard.verifyFinal(final);
      report.final_tool_names=final.events.map(event=>event.name);
    }else report.final_read_tool_names=final.events.map(event=>event.name);
    report.transport_and_state_checks_passed=true;report.status='completed';
  }catch(error){report.status='incomplete';report.failure=safeCode(error?.message)||'request_or_transport_failed';}
  finally{
    clearTimeout(timer);clearInterval(silenceTimer);endSocket();
    if(call&&!report.end_control_sent&&call.monitor?.controlUrl){try{
      const control=new URL(call.monitor.controlUrl);
      if(control.protocol!=='https:'||!control.hostname.endsWith('.vapi.ai'))throw new Error('unexpected_control_url');
      await request(control,{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+config.vapi_public_key},body:JSON.stringify({type:'end-call'})},true);
      report.end_control_sent=true;
    }catch{report.end_control_failed=true;}}
    if(session){
      try{await request(`${LOCAL}/api/sessions/${session.id}/end`,{method:'POST',headers:sessionHeaders()},true);report.local_session_ended=true;
        const final=await snapshot(true);report.pending_operations_at_end=final.pending_operations?.length||0;
      }catch{report.final_state_unverified=true;report.transport_and_state_checks_passed=false;}
      try{report.gateway_timing=sanitizeGateway(await json(`${LOCAL}/api/sessions/${session.id}/gateway-timing`,{headers:sessionHeaders()},true,1024*1024),call?.id);}catch{report.gateway_timing_available=false;}
    }
    if(call){try{
      report.provider_requests.read_call++;
      report.provider=providerEvidence(await json('https://api.vapi.ai/call/'+encodeURIComponent(call.id),{headers:{Authorization:'Bearer '+env.VAPI_API_KEY}},true,12*1024*1024),call.id,env.VAPI_ASSISTANT_ID,session.id);
    }catch{report.provider_evidence_unavailable=true;}}
    if(!report.local_session_ended||report.pending_operations_at_end>0||(call&&!report.end_control_sent))report.transport_and_state_checks_passed=false;
    if(phoneGuard){report.phone_readback=phoneGuard.evidence();if(businessGuardFailure){report.failure=businessGuardFailure;report.transport_and_state_checks_passed=false;report.status='incomplete';}}
    report.provider_ended_verified=report.provider?.status==='ended';
    report.events=events;report.final_sdk_transcripts=finals;
    report.pending_assistant_partial_at_end=assistantCaptions.pendingPartial;
    report.assistant_caption_event_counts={partial:assistantCaptions.partialCount,final:assistantCaptions.finalCaptions.length};
    report.last_checkpoint='finalized';
    await finalizeEvidence({handle,audioHandle,report,pcm:Buffer.concat(chunks),voicedPcmBytes,audioFile:basename+'-assistant.wav',secrets:secrets()});
    console.log(JSON.stringify({output:`docs/${basename}.json`,status:report.status,transport_and_state_checks_passed:report.transport_and_state_checks_passed,
      semantic_review:report.semantic_review.status,provider_requests:report.provider_requests}));
  }
  return report.transport_and_state_checks_passed?0:1;
}

if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url)){
  try{
    const options=parseOptions(process.argv.slice(2));
    if(!options.evaluate)console.log(JSON.stringify({mode:'dry_run',scenario:options.scenario,turns:SCENARIOS[options.scenario],caller_model:MODEL,
      maximum_provider_calls:1,maximum_tts_requests:2,provider_requests_made:0,files_written:0,
      instruction:'Add --evaluate and --output unique-label only when ready to run one paid synthetic call.'},null,2));
    else process.exitCode=await evaluate(options);
  }catch(error){console.error(safeCode(error?.code)||safeCode(error?.message)||'evaluation_failed');process.exitCode=2;}
}
