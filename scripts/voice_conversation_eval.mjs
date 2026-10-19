/** Bounded synthetic voice regression: FAQ, advisor or verified repair conversation.
 * No human microphone or phone. Spends credits only when executed.
 * Reuses exact PCM by phrase hash for matched runs; preserves every report.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseEnv } from 'node:util';
import { createHash } from 'node:crypto';
import { AssistantCaptionTracker, faqHoursReady, waitForPersistedGreeting } from './voice_regression_checks.mjs';
import { collectGatewayTiming } from './gateway_timing_evidence.mjs';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const [scenario, label] = process.argv.slice(2);
if (!['faq', 'advisor', 'repairs'].includes(scenario) || !/^[a-z0-9-]{1,60}$/.test(label || '') || process.argv.length !== 4)
  throw new Error('Usage: node scripts/voice_conversation_eval.mjs faq|advisor|repairs unique-label');
const output = path.join(root, 'docs', `voice-regression-${scenario}-${label}.json`);
const reservation = await fs.open(output, 'wx'); await reservation.close();
const env = parseEnv(await fs.readFile(path.join(root, '.env'), 'utf8'));
const local = 'http://127.0.0.1:8765';
const phrases = scenario === 'faq' ? ['What time does your service department close on weekdays?'] : scenario === 'repairs' ? [
  'Can you check a diagnostic intake at 2 PM Central this coming Thursday? Only check that exact time; do not book anything.',
  'Please save a request in the local demo advisor queue. I understand this will not contact anyone or arrange a callback.',
  'Cancel that local advisor request. Do not change any appointment.',
] : [
  'Where exactly are you located? Do you have a physical service address?',
  'My car stereo stopped working. I do not want an appointment yet. Can you troubleshoot it for me here?',
  'Please save a local request for a human adviser. I understand this demo will not actually call me or transfer me.',
];
const report = { kind: 'bounded_synthetic_voice_regression', scenario, label, started_at: new Date().toISOString(),
  microphone_used: false, telephone_used: false, maximum_provider_call_seconds: 150, caller_inputs: [], turns: [],
  limitations: ['Synthetic PCM and transcript/provider events; not human microphone, audibility or acoustic latency.',
    'Automatic transport checks do not replace semantic review of the saved assistant replies.',
    'Client audio gaps include trailing caller PCM and transport delivery; provider transcript gaps are separate.'] };
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const now = () => performance.now();
const life = new AbortController();
let call, session, socket, timer, silenceTimer, inputPlaying = false, assistantSpeaking = false, lastStop = 0, lastStart = 0;
let lastVoiced = 0, lastText = 0, voicedBytes = 0, callStarted = 0, transportEnded = false, socketFault = false;
let trustedStaticFacts = false;
const finals = [], audioTimes = [];
const assistantCaptions = new AssistantCaptionTracker();
const hash = value => createHash('sha256').update(value).digest('hex');
const filler = text => /^(give me a moment|one moment|1 moment|this will just take a sec|i'm still waiting for the result)[.!?\s]*$/i.test(text.trim());
function responseReady(index, replies, state) {
  const text = replies.join(' ').toLowerCase();
  if (!replies.some(t => !filler(t))) return false;
  // Conservative completion cues for this fixed exercise, not a general judge.
  // Preserve the full transcript for semantic review even when a cue fails.
  if (scenario === 'faq') return faqHoursReady(text, trustedStaticFacts, state.events);
  if (scenario === 'repairs') {
    if (index === 0) return state.availability_query?.time_filter?.exact_time === '14:00'
      && state.slots?.length === 1 && /\b(available|offer|opening)\b/.test(text);
    if (index === 1) return state.handoff?.status === 'callback_requested' && /\b(local|locally|queue)\b/.test(text)
      && /\b(no\s*(one|1|body)|nobody|not\s+contacted|haven't\s+contacted)\b/.test(text);
    return state.handoff?.status === 'withdrawn' && /\b(withdrawn|removed|cancelled|canceled)\b/.test(text);
  }
  if (index === 0) return /\b(fictional|demo)\b/.test(text) && /\b(address|location|physical)\b/.test(text);
  if (index === 1) return /\b(local|queue)\b/.test(text) && /\b(callback|call back|transfer)\b/.test(text);
  return Boolean(state.handoff) && /\b(local|locally|queue)\b/.test(text)
    && /\b(no\s*(one|1|body)|nobody|not\s+contacted|haven't\s+contacted)\b/.test(text);
}
async function request(url, options = {}, cleanup = false) {
  let response;
  try {
    response = await fetch(url, { ...options, redirect: 'error', signal: cleanup ? AbortSignal.timeout(5000)
      : AbortSignal.any([life.signal, AbortSignal.timeout(20000)]) });
  } catch (error) {
    const parsed = new URL(url);
    const stage = parsed.hostname === 'api.vapi.ai' ? (parsed.pathname.startsWith('/assistant/') ? 'vapi_assistant_read' : options.method === 'POST' ? 'vapi_call_create' : 'vapi_call_read')
      : parsed.hostname === 'api.elevenlabs.io' ? 'caller_audio_generation' : 'local_api';
    const code = error.cause?.code;
    report.network_failures ||= [];
    if (report.network_failures.length < 8) report.network_failures.push({ stage,
      name: ['TimeoutError','AbortError','TypeError'].includes(error.name) ? error.name : 'request_error',
      code: ['UND_ERR_CONNECT_TIMEOUT','ETIMEDOUT','ECONNREFUSED','EAI_AGAIN','ENOTFOUND'].includes(code) ? code : null });
    throw error;
  }
  if (!response.ok) throw new Error(`HTTP_${response.status}`);
  return response;
}
function active() {
  if (life.signal.aborted || transportEnded || socketFault) throw new Error('call_deadline_or_transport_ended');
}
async function snapshot() {
  const state = await (await request(`${local}/api/sessions/${session.id}`, { headers: { 'x-session-token': session.token } })).json();
  if (state.id !== session.id || state.mode !== 'live') throw new Error('session_scope_mismatch');
  return state;
}
function quiet(after) {
  return !assistantSpeaking && !assistantCaptions.pendingPartial && lastStop > after && lastStop >= lastStart && lastVoiced > after
    && now() - Math.max(lastStop, lastVoiced, lastText) >= 2500;
}
try {
  for (const key of ['VAPI_API_KEY','VAPI_ASSISTANT_ID','ELEVENLABS_API_KEY','VAPI_ELEVENLABS_VOICE_ID'])
    if (!env[key]) throw new Error('configuration_missing');
  const cache = path.resolve(root, '../../work/runtime/voice-eval-pcm'); await fs.mkdir(cache, { recursive: true });
  const clips = [];
  for (const text of phrases) {
    const cacheKey = hash(JSON.stringify([text,env.VAPI_ELEVENLABS_VOICE_ID,'eleven_flash_v2_5','pcm_16000']));
    const file = path.join(cache, cacheKey + '.pcm');
    let pcm;
    try { pcm = await fs.readFile(file); }
    catch (error) {
      if (error.code !== 'ENOENT') throw error;
      const tts = await request(`https://api.elevenlabs.io/v1/text-to-speech/${encodeURIComponent(env.VAPI_ELEVENLABS_VOICE_ID)}?output_format=pcm_16000`, {
        method: 'POST', headers: { 'xi-api-key': env.ELEVENLABS_API_KEY, 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, model_id: 'eleven_flash_v2_5' }),
      });
      pcm = Buffer.from(await tts.arrayBuffer());
      if (!pcm.length || pcm.length > 32_000 * 30 || pcm.length % 2) throw new Error('invalid_caller_pcm');
      await fs.writeFile(file, pcm, { flag: 'wx' });
    }
    if (!pcm.length || pcm.length > 32_000 * 30 || pcm.length % 2) throw new Error('invalid_cached_caller_pcm');
    clips.push(pcm); report.caller_inputs.push({ text, pcm_sha256: hash(pcm), duration_seconds: pcm.length / 32_000 });
  }
  const config = await (await request(local+'/api/config')).json();
  report.running_prompt_version = config.prompt_version || null;
  report.runtime_optimization_flags = {receipt_narration_enabled: config.receipt_narration_enabled === true,
    jev_faq_enabled: config.jev_faq_enabled === true, narrator_version: config.narrator_version || null};
  report.runner_sha256 = hash(await fs.readFile(fileURLToPath(import.meta.url)));
  report.dealership_date = config.today;
  trustedStaticFacts = ['2026-10-18-gemini-agentic-6','2026-10-18-gemini-agentic-7','2026-10-18-gemini-agentic-8','2026-10-18-gemini-agentic-9','2026-10-18-gemini-agentic-10','2026-10-18-gemini-agentic-11','2026-10-18-gemini-agentic-12','2026-10-18-gemini-agentic-13','2026-10-18-gemini-agentic-14','2026-10-18-gemini-agentic-15'].includes(config.prompt_version);
  report.static_facts_mode = trustedStaticFacts;
  session = await (await request(local+'/api/sessions', { method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:'live'}) })).json();
  report.session_id = session.id;
  const saved = await (await request('https://api.vapi.ai/assistant/'+env.VAPI_ASSISTANT_ID, { headers:{ Authorization:'Bearer '+env.VAPI_API_KEY } })).json();
  report.assistant_configuration_sha256 = hash(JSON.stringify(saved));
  report.prompt_sha256 = hash(saved.model.messages[0].content);
  report.tool_start_messages = saved.model.tools.map(t => ({ name:t.function.name, messages:t.messages || null }));
  call = await (await request('https://api.vapi.ai/call', {method:'POST',headers:{Authorization:'Bearer '+env.VAPI_API_KEY,'Content-Type':'application/json'},body:JSON.stringify({
    assistantId: env.VAPI_ASSISTANT_ID,
    assistantOverrides:{maxDurationSeconds:150,variableValues:{session_id:session.id,session_token:session.token,current_date:config.today,timezone:config.timezone}},
    transport:{provider:'vapi.websocket',audioFormat:{format:'pcm_s16le',container:'raw',sampleRate:16000}},
  })})).json();
  report.call_id = call.id; callStarted = now();
  timer = setTimeout(() => life.abort(), 145_000);
  const ws = new URL(call.transport?.websocketCallUrl);
  if (ws.protocol !== 'wss:' || !ws.hostname.endsWith('.vapi.ai')) throw new Error('unexpected_transport_url');
  socket = new WebSocket(ws); socket.binaryType = 'arraybuffer';
  socket.addEventListener('error', () => { socketFault=true; });
  socket.addEventListener('close', () => { transportEnded=true; });
  socket.addEventListener('message', event => {
    if (typeof event.data !== 'string') {
      const samples=new DataView(event.data);let peak=0;
      for(let i=0;i+1<samples.byteLength;i+=2)peak=Math.max(peak,Math.abs(samples.getInt16(i,true)));
      if(peak>128){lastVoiced=now();voicedBytes+=samples.byteLength;if(audioTimes.length<10_000)audioTimes.push(lastVoiced);}
      return;
    }
    try {
      const m=JSON.parse(event.data);
      if(assistantCaptions.observe(m))lastText=now();
      if(m.type==='speech-update' && m.role==='assistant'){
        if(m.status==='started'){assistantSpeaking=true;lastStart=now();}
        if(m.status==='stopped'){assistantSpeaking=false;lastStop=now();}
      }
      if(m.type==='transcript' && m.transcriptType==='final' && ['assistant','user'].includes(m.role)){
        lastText=now();finals.push({role:m.role,text:m.transcript,at:lastText});
      }
    } catch { /* No raw provider text is logged on parse failure. */ }
  });
  await new Promise((resolve,reject)=>{
    const timeout=setTimeout(()=>reject(new Error('websocket_open_timeout')),15000);
    socket.addEventListener('open',()=>{clearTimeout(timeout);resolve();},{once:true});
    socket.addEventListener('error',()=>{clearTimeout(timeout);reject(new Error('websocket_open_failed'));},{once:true});
  });
  silenceTimer=setInterval(()=>{
    if(!inputPlaying && socket.readyState===WebSocket.OPEN && socket.bufferedAmount<128_000)socket.send(new Uint8Array(640));
  },20);
  const greeting = await waitForPersistedGreeting({snapshot, quiet, active, now, sleep:pause, callStarted,
    captionsSettled:transcript=>assistantCaptions.settled(transcript)});
  report.greeting = {persisted_complete:true,audio_quiet_observed:true,
    transcript_rows:greeting.transcript.length,completed_ms:Math.round(now()-callStarted)};
  for(let index=0;index<clips.length;index++){
    active();const pcm=clips[index];const before=await snapshot();const baseline=before.transcript.length;
    const inputStart=now(), beforeVoice=voicedBytes;inputPlaying=true;
    try {
      for(let offset=0;offset<pcm.length;offset+=640){
        active();if(socket.readyState!==WebSocket.OPEN || socket.bufferedAmount>128_000)throw new Error('input_transport_unavailable');
        socket.send(pcm.subarray(offset,Math.min(offset+640,pcm.length)));
        await pause(Math.max(0,inputStart+Math.min(offset+640,pcm.length)/32-now()));
      }
    } finally {inputPlaying=false;}
    const inputEnd=now(), until=Math.min(now()+35_000,callStarted+140_000);let state, replies=[];
    while(now()<until){
      active();state=await snapshot();replies=state.transcript.slice(baseline).filter(t=>t.role==='assistant').map(t=>t.text);
      if(responseReady(index,replies,state) && quiet(inputEnd) && assistantCaptions.settled(state.transcript))break;
      await pause(250);
    }
    const turn={input:phrases[index],input_end_ms:Math.round(inputEnd-callStarted),assistant:replies,
      caller_transcripts:state.transcript.slice(baseline).filter(t=>t.role==='user').map(t=>t.text),
      first_voiced_pcm_after_input_ms:audioTimes.find(t=>t>inputEnd)-inputEnd,
      voiced_bytes_after_input:voicedBytes-beforeVoice,assistant_finished:quiet(inputEnd),
      assistant_captions_settled:assistantCaptions.settled(state.transcript),pending_assistant_partial:assistantCaptions.pendingPartial,
      tool_names:state.events.map(e=>e.name),appointment_present:Boolean(state.appointment),handoff_present:Boolean(state.handoff),
      ...(scenario==='repairs'?{handoff_status:state.handoff?.status||null,availability_query:state.availability_query||null}: {})};
    if(!Number.isFinite(turn.first_voiced_pcm_after_input_ms))turn.first_voiced_pcm_after_input_ms=null;
    report.turns.push(turn);
    if(!turn.assistant_finished || !turn.assistant_captions_settled || !responseReady(index,replies,state) || !turn.caller_transcripts.length || !turn.voiced_bytes_after_input)
      throw new Error('turn_transport_or_completion_check_failed');
    if(state.appointment)throw new Error('unexpected_appointment');
  }
  const final=await snapshot();
  report.local_tool_names=final.events.map(e=>e.name);
  report.local_handoff_present=Boolean(final.handoff);
  report.local_appointment_present=Boolean(final.appointment);
  report.transport_and_state_checks_passed=scenario==='faq'?(trustedStaticFacts || report.local_tool_names.includes('get_dealership_info'))&&!final.appointment:Boolean(final.handoff)&&!final.appointment;
  if(scenario==='repairs')report.transport_and_state_checks_passed=final.handoff?.status==='withdrawn'&&!final.appointment&&!final.proposal
    && report.local_tool_names.includes('withdraw_handoff') && !final.pending_operations?.length;
} catch(error){
  const value=String(error.message||'unknown');report.failure=/^[a-zA-Z0-9_]+$/.test(value)?value:'request_or_transport_failure';
  report.transport_and_state_checks_passed=false;
} finally {
  clearInterval(silenceTimer);clearTimeout(timer);
  if(socket?.readyState===WebSocket.OPEN){
    try{socket.send(JSON.stringify({type:'end-call'}));report.end_control_sent=true;}catch{report.end_control_failed=true;}
    try{socket.close();}catch{report.socket_close_failed=true;}
  }else if(socket?.readyState===WebSocket.CONNECTING){
    try{socket.close();}catch{report.socket_close_failed=true;}
  }
  if(session){try{await request(`${local}/api/sessions/${session.id}/end`,{method:'POST',headers:{'x-session-token':session.token}},true);report.local_session_ended=true;}catch{report.local_session_ended=false;}}
  if(session&&call){
    try{
      report.gateway_timing=await collectGatewayTiming(request,session,call.id,local);
      report.gateway_timing_available=true;
      report.gateway_timing_collected_after_session_end=report.local_session_ended===true;
    }catch(error){
      report.gateway_timing_available=false;
      report.gateway_timing_failure=['gateway_scope_mismatch','gateway_evidence_bounds'].includes(error?.message)?error.message:'collection_unavailable';
    }
  }
  if(scenario==='faq'&&trustedStaticFacts&&report.transport_and_state_checks_passed===true){
    report.single_request_static_answer=report.gateway_timing?.requests?.length===1
      &&report.gateway_timing.requests[0].outcome==='completed'
      &&report.gateway_timing.requests[0].counts.tool_calls===0&&report.local_tool_names?.length===0;
    report.response_routes=report.gateway_timing?.requests?.map(row=>row.route)||[];
    report.single_generation_static_answer=report.single_request_static_answer&&report.response_routes[0]==='gemini';
  }
  if(call){
    try {
      for(let attempt=0;attempt<5;attempt++){
        const record=await(await request('https://api.vapi.ai/call/'+call.id,{headers:{Authorization:'Bearer '+env.VAPI_API_KEY}},true)).json();
        if(record.id!==call.id || record.assistantId!==env.VAPI_ASSISTANT_ID)throw new Error('provider_scope_mismatch');
        if(record.status==='ended' && (record.artifact?.messages||record.messages)?.length){
          report.provider={status:record.status,ended_reason:record.endedReason,started_at:record.startedAt,ended_at:record.endedAt,cost_usd:record.cost};
          report.provider_messages=(record.artifact?.messages||record.messages).filter(m=>['user','bot'].includes(m.role)).map(m=>({role:m.role,text:m.message,time:m.time,endTime:m.endTime,secondsFromStart:m.secondsFromStart}));break;
        }
        await pause(1000);
      }
    }catch{report.provider_readback_failed=true;}
  }
  report.finished_at=new Date().toISOString();
  report.pending_assistant_partial_at_end=assistantCaptions.pendingPartial;
  report.assistant_caption_event_counts={partial:assistantCaptions.partialCount,final:assistantCaptions.finalCaptions.length};
  let serialized=JSON.stringify(report,null,2)+'\n';
  for(const [key,value] of Object.entries(env))if(value && /KEY|TOKEN|SECRET|PASSWORD/.test(key))serialized=serialized.replaceAll(value,'[REDACTED]');
  if(session?.token)serialized=serialized.replaceAll(session.token,'[REDACTED]');
  await fs.writeFile(output,serialized);
  console.log(JSON.stringify({output,passed:report.transport_and_state_checks_passed,failure:report.failure,call_id:report.call_id,turns:report.turns}));
}
process.exitCode=report.transport_and_state_checks_passed?0:1;
