/** Test-only preload. No real fetch/WebSocket, microphone or runtime database. */
import fs from 'node:fs';
import { createHash } from 'node:crypto';

const mode = process.env.VOICE_MOCK_MODE;
if (!['complete','transport-close','missing-caption','missing-greeting','partial-delayed-final','partial-never-final'].includes(mode)) throw new Error('mock_mode_required');
const nativeTimeout = globalThis.setTimeout;
const nativeClear = globalThis.clearTimeout;
let clock = 0, socket, ended = false, callerTurns = 0;
const schedule = [], transcript = [], heldTimers = new Set();
const stats = {mode,requests:[],caller_starts:[],final_caption_persisted:[],checkpoint_at_socket:null,session_ended:false,end_control:false};
const greeting = "Hi, I'm Cedar. This is an AI demo. How can I help with your service visit?";
const replies = ['We offer oil service and brake inspection.','Diagnostic inspection checks an unknown problem.'];
const pcm = Buffer.alloc(640);for(let index=0;index<pcm.length;index+=2)pcm.writeInt16LE(600,index);
const callId = 'synthetic-call', sessionId = 'synthetic-session', assistantId = 'synthetic-assistant';
const json = value => new Response(JSON.stringify(value),{headers:{'Content-Type':'application/json'}});
const enqueue = (delay, action) => {schedule.push({at:clock+delay,action});schedule.sort((a,b)=>a.at-b.at);};
const tick = () => {while(schedule.length&&schedule[0].at<=clock)schedule.shift().action();};
const readCheckpoint = () => JSON.parse(fs.readFileSync(process.env.VOICE_MOCK_REPORT,'utf8'));

Object.defineProperty(globalThis,'performance',{value:{now:()=>clock},configurable:true});
globalThis.setTimeout = (action,ms=0,...args) => {
  if(ms>=1000){const timer={kind:'long-timeout'};heldTimers.add(timer);return timer;}
  return nativeTimeout(()=>{clock+=Math.max(0,ms);tick();action(...args);},0);
};
globalThis.clearTimeout = timer => {if(!heldTimers.delete(timer))nativeClear(timer);};
// The test drives audio frames explicitly. Do not create a real wall-clock pump.
globalThis.setInterval = () => {const timer={kind:'interval'};heldTimers.add(timer);return timer;};
globalThis.clearInterval = timer => heldTimers.delete(timer);

function message(value){socket.dispatchEvent(new MessageEvent('message',{data:JSON.stringify(value)}));}
function caption(role,text){transcript.push({role,text});message({type:'transcript',transcriptType:'final',role,transcript:text});}
function audio(){socket.dispatchEvent(new MessageEvent('message',{data:pcm.buffer.slice(pcm.byteOffset,pcm.byteOffset+pcm.byteLength)}));}
function speaking(status){message({type:'speech-update',role:'assistant',status});}

class MockSocket extends EventTarget {
  static CONNECTING=0;static OPEN=1;static CLOSED=3;
  readyState=0;bufferedAmount=0;
  constructor(url){
    super();if(String(url)!=='wss://synthetic.vapi.ai/mock')throw new Error('unexpected_mock_websocket');
    socket=this;
    const checkpoint=readCheckpoint();stats.checkpoint_at_socket={call_id:checkpoint.call_id,last_checkpoint:checkpoint.last_checkpoint};
    queueMicrotask(()=>{
      this.readyState=1;this.dispatchEvent(new Event('open'));
      clock=1;speaking('started');audio();clock=2;speaking('stopped');
      // Audio is quiet well before the persisted greeting becomes visible.
      if(mode!=='missing-greeting')enqueue(3200,()=>caption('assistant',greeting));
    });
  }
  send(value){
    if(this.readyState!==1)throw new Error('mock_socket_closed');
    if(typeof value==='string'){
      if(JSON.parse(value).type!=='end-call')throw new Error('unexpected_mock_control');
      stats.end_control=true;return;
    }
    const frame=Buffer.from(value);
    if(!frame.some(byte=>byte!==0))return;
    const index=callerTurns++;
    if(index>=2)throw new Error('unexpected_extra_caller_turn');
    const checkpoint=readCheckpoint();
    stats.caller_starts.push({index,clock,greeting_persisted:transcript.some(row=>row.text===greeting),
      last_checkpoint:checkpoint.last_checkpoint,saved_call_id:checkpoint.call_id,
      saved_turns:checkpoint.turns.length});
    enqueue(500,()=>{
      caption('user',index===0?'What services do you offer?':'Which checks an unknown problem?');
      speaking('started');audio();
      if(mode==='transport-close'){ended=true;this.close();return;}
      enqueue(200,()=>speaking('stopped'));
      if(mode==='complete')enqueue(3500,()=>caption('assistant',replies[index]));
      else if(mode==='missing-caption')enqueue(31000,()=>{ended=true;this.close();});
      else if(mode.startsWith('partial-')){
        enqueue(500,()=>caption('assistant','हाँ।'));
        enqueue(1000,()=>message({type:'transcript',role:'assistant',transcriptType:'partial',transcript:'ब्रेक की जाँच में'}));
        enqueue(3500,()=>message({type:'transcript',role:'assistant',transcriptType:'partial',transcript:'ब्रेक की जाँच में पैड देखे जाते हैं।'}));
        if(mode==='partial-delayed-final'){
          enqueue(9000,()=>message({type:'transcript',role:'assistant',transcriptType:'final',transcript:replies[index]}));
          enqueue(13000,()=>{transcript.push({role:'assistant',text:replies[index]});stats.final_caption_persisted.push(clock);});
        }
      }
    });
  }
  close(){if(this.readyState===3)return;this.readyState=3;this.dispatchEvent(new Event('close'));}
}
globalThis.WebSocket=MockSocket;

globalThis.fetch=async (input,options={})=>{
  const url=new URL(String(input)),method=options.method||'GET';
  stats.requests.push({host:url.hostname,path:url.pathname,method});
  if(url.hostname==='api.elevenlabs.io'&&url.pathname==='/v1/text-to-dialogue'&&method==='POST')return new Response(pcm);
  if(url.hostname==='api.vapi.ai'&&url.pathname==='/assistant/'+assistantId&&method==='GET')return json({id:assistantId,model:{model:'gemini-3.8-flash'},voice:{model:'eleven_v4_turbo'},transcriber:{provider:'soniox',model:'stt-rt-v5'}});
  if(url.hostname==='api.vapi.ai'&&url.pathname==='/call'&&method==='POST')return json({id:callId,transport:{websocketCallUrl:'wss://synthetic.vapi.ai/mock'},monitor:{controlUrl:'https://synthetic.vapi.ai/control'}});
  if(url.hostname==='api.vapi.ai'&&url.pathname==='/call/'+callId&&method==='GET')return json({id:callId,assistantId,status:'ended',endedReason:ended?'silence-timed-out':'customer-ended-call',cost:0,
    assistantOverrides:{variableValues:{session_id:sessionId}},artifact:{messages:transcript.map(row=>({role:row.role==='assistant'?'bot':'user',message:row.text,time:1,endTime:2}))}});
  if(url.hostname==='synthetic.vapi.ai'&&url.pathname==='/control'&&method==='POST'){stats.end_control=true;return json({});}
  if(url.origin!=='http://127.0.0.1:8765')throw new Error('unmocked_network_forbidden');
  if(url.pathname==='/api/config'&&method==='GET')return json({live_voice_available:true,vapi_assistant_id:assistantId,vapi_public_key:'synthetic-public-key',prompt_version:'2026-10-18-gemini-agentic-12',today:'2026-10-18',timezone:'America/Chicago'});
  if(url.pathname==='/api/sessions'&&method==='POST')return json({id:sessionId,token:'synthetic-private-capability'});
  if(options.headers?.['x-session-token']!=='synthetic-private-capability')throw new Error('mock_session_capability_missing');
  if(url.pathname===`/api/sessions/${sessionId}`&&method==='GET')return json({id:sessionId,mode:'live',transcript:[...transcript],events:[],pending_operations:[]});
  if(url.pathname===`/api/sessions/${sessionId}/end`&&method==='POST'){stats.session_ended=true;return json({});}
  if(url.pathname===`/api/sessions/${sessionId}/gateway-timing`&&method==='GET')return json({kind:'gemini_gateway_timing',requests:[{call_key:createHash('sha256').update(callId).digest('hex').slice(0,16),route:'gemini',outcome:'completed',counts:{tool_calls:0,jev_requests:0},milestones_ms:{first_text:1,total:2}}]});
  throw new Error('unmocked_request_forbidden');
};
process.on('exit',()=>{stats.held_timers_at_exit=heldTimers.size;fs.writeFileSync(process.env.VOICE_MOCK_STATS,JSON.stringify(stats));});
