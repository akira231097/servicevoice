// Exercise the actual renderer with a minimal offline DOM, no browser/provider.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import fs from 'node:fs/promises';
import vm from 'node:vm';
import { createHash, webcrypto } from 'node:crypto';

class Element {
  constructor(tag = '') { this.tagName = tag; this.children = []; this.dataset = {}; this._text = ''; this.listeners = {}; this.classList = { toggle() {}, add() {} }; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(' '); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; this._text = ''; }
  closest() { return this; }
  querySelectorAll() { return []; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  setAttribute() {}
}
async function renderer(fetchFn = () => { throw new Error('Network forbidden'); }) {
  const source = await fs.readFile(new URL('../app/static/app.js', import.meta.url), 'utf8');
  const isolated = source.replace(/^import .*;\r?\n/gm, '').replace(/^void initialize\(\);\s*$/m, '');
  const elements = new Map();
  const requests = [], downloads = [], blobs = new Map(), timers = new Map(), windowListeners = {};
  let timerId = 0;
  const document = {
    getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); },
    createElement(tag) { const element = new Element(tag); element.click = () => downloads.push({filename: element.download, blob: blobs.get(element.href)}); return element; },
    createTextNode(text) { const element = new Element(); element.textContent = text; return element; },
    querySelectorAll() { return []; },
  };
  const context = vm.createContext({ document, window:{addEventListener(name, callback) { windowListeners[name] = callback; }},
    SnapshotChannel:class { bind() {} },
    fetch(...args) { requests.push(args); return fetchFn(...args); }, AbortController, Blob, TextEncoder, crypto:webcrypto,
    URL:{createObjectURL(blob) { const id = 'blob:' + blobs.size; blobs.set(id, blob); return id; }, revokeObjectURL(id) { blobs.delete(id); }},
    setTimeout(callback, ms) { timers.set(++timerId, {callback, ms}); return timerId; }, clearTimeout(id) { timers.delete(id); }, clearInterval() {},
  });
  vm.runInContext(isolated, context);
  return { context, elements, requests, downloads, timers, windowListeners };
}
test('saved handoff renderer states local storage and no actual callback or transfer', async () => {
  const { context, elements } = await renderer();
  context.fixture = { handoff:{reason:'Synthetic stereo question', status:'callback_requested', message:'Historical backend wording'} };
  vm.runInContext('state.mode="live"; renderOutcome(fixture)', context);
  assert.equal(elements.get('outcome-label').textContent, 'LOCAL ADVISOR REQUEST');
  const text = elements.get('outcome').textContent;
  assert.match(text, /Saved locally/);
  assert.match(text, /No one has been contacted/);
  assert.match(text, /cannot transfer you or arrange a callback/);
  assert.doesNotMatch(text, /Callback requested|Historical backend wording/);
});
test('fused preparation activity uses a readable label and does not imply booking completion', async () => {
  const { context, elements } = await renderer();
  context.fixture = { events:[{id:1,name:'prepare_first_available_booking',status:'success',elapsed_ms:4,at:'2026-10-18T18:00:00Z'}] };
  vm.runInContext('renderActivity(fixture)', context);
  const summary = elements.get('activity').children[0].children[0].textContent;
  assert.match(summary, /Earliest appointment prepared/);
  assert.doesNotMatch(summary, /prepare_first_available_booking|confirmed|booked/);
});

const appointment = { reference:'CSV-SYNTHETIC', status:'confirmed', service_name:'Oil and filter service', label:'Monday at 1 PM Central', customer_name:'Synthetic customer', vehicle:'2021 Toyota Camry' };
const savedHandoff = {status:'callback_requested', reason:'Synthetic question'};
for (const status of ['confirmed', 'cancelled']) test(`a ${status} appointment stays primary with an existing advisor request`, async () => {
  const {context, elements} = await renderer();
  context.fixture = {appointment:{...appointment,status}, handoff:savedHandoff};
  vm.runInContext('state.mode="live"; renderOutcome(fixture)', context);
  assert.equal(elements.get('outcome-label').textContent, status === 'cancelled' ? 'CANCELLATION SAVED' : 'VERIFIED RESULT');
  const outcome = elements.get('outcome');
  assert.match(outcome.children[1].textContent, status === 'cancelled' ? /Appointment cancelled/ : /on the calendar/);
  assert.match(outcome.textContent, /CSV-SYNTHETIC/);
  assert.match(outcome.textContent, /Saved locally/);
  assert.match(outcome.textContent, /No one has been contacted/);
});

test('withdrawn queue history is truthful and cannot mask a later appointment', async () => {
  const {context, elements} = await renderer();
  context.fixture = {appointment, handoff:{status:'withdrawn',reason:'<img src=x onerror=alert(1)>'}};
  vm.runInContext('state.mode="live"; renderOutcome(fixture)', context);
  assert.equal(elements.get('outcome-label').textContent, 'VERIFIED RESULT');
  assert.match(elements.get('outcome').textContent, /Withdrawn locally/);
  assert.match(elements.get('outcome').textContent, /No external contact was made/);
  assert.match(elements.get('outcome').textContent, /<img src=x onerror=alert\(1\)>/);
  assert.doesNotMatch(elements.get('outcome').textContent, /Saved locally|deleted/);
  context.fixture = {handoff:{status:'withdrawn',reason:'No longer needed'}};
  vm.runInContext('renderOutcome(fixture)', context);
  assert.equal(elements.get('outcome-label').textContent, 'LOCAL REQUEST WITHDRAWN');
  assert.match(elements.get('outcome').textContent, /Local advisor request withdrawn/);
});

for (const [key, value, label] of [
  ['pending_operations',[{id:'pending'}],'PROCESSING'],
  ['proposal',{kind:'cancel',summary:'Cancel this specific appointment?'},'YOUR CONFIRMATION'],
]) test(`${key} preserves priority over appointment and queue history`, async () => {
  const {context,elements} = await renderer();
  context.fixture = {appointment, handoff:savedHandoff, [key]:value};
  vm.runInContext('state.mode="live"; renderOutcome(fixture)', context);
  assert.equal(elements.get('outcome-label').textContent,label);
  assert.match(elements.get('outcome').textContent,/Saved locally/);
  assert.doesNotMatch(elements.get('outcome').textContent,/You’re on the calendar/);
});

const sid = 'sess_synthetic', cid = 'synthetic-provider-call', token = 'PRIVATE-CAPABILITY-DO-NOT-EXPORT';
const callKey = createHash('sha256').update(cid).digest('hex').slice(0,16);
const jsonResponse = data => ({ok:true,json:async()=>data});
function timingPayload() {
  return {kind:'gemini_gateway_timing',session_id:sid,provider_call_id:cid,capacity:256,requests:[{
    request_id:'a'.repeat(32),call_key:callKey,started_at_unix:1792330000,route:'deterministic_receipt',outcome:'completed',
    narration_reason:'verified_receipt',narrator_version:'receipt-en-2026-10-18-3',
    source_result_sha256:'b'.repeat(64),output_text_sha256:'c'.repeat(64),output_text_source:'verified_receipt',
    milestones_ms:{first_text:7,total:9},durations_ms:{upstream_eof_drain:1},counts:{text_chunks:1,tool_calls:0,jev_requests:0},
  }]};
}
function connected(harness) {
  harness.context.fixture = {id:sid,token};
  vm.runInContext(`state.session=fixture;state.revision=8;state.sessionEnded=true;
    state.voiceMetrics={hasConnected:true,elapsed:20000,connectionMs:45,gap:2000,gapP95:2500,gapSamples:2,gapSamplesMs:[1500,2500],interruptions:1,speechEventsObserved:true};
    state.snapshot={metrics:{tool_count:2,tool_p50_ms:3,tool_p95_ms:5}};`, harness.context);
}
function clickExport(harness) { return harness.elements.get('export-metrics').listeners.click(); }
async function exported(harness) { assert.equal(harness.downloads.length,1); return JSON.parse(await harness.downloads[0].blob.text()); }

test('actual export click authenticates once, verifies call association and projects only content-free timing fields', async () => {
  const data = timingPayload();
  data.token = token; data.transcript = 'PRIVATE TRANSCRIPT'; data.requests[0].headers = {Authorization:token};
  data.requests[0].output_text = 'PRIVATE TRANSCRIPT'; data.requests[0].counts.customer_phone = 5125550142;
  const harness = await renderer(async()=>jsonResponse(data)); connected(harness);
  harness.context.secret=token;
  vm.runInContext('state.voiceMetrics.token=secret;state.voiceMetrics.transcript="PRIVATE TRANSCRIPT";state.snapshot.customer={name:"PRIVATE CUSTOMER"};', harness.context);
  await clickExport(harness);
  const evidence=await exported(harness), serialized=JSON.stringify(evidence);
  assert.equal(harness.requests.length,1);
  assert.equal(harness.requests[0][0],`/api/sessions/${sid}/gateway-timing`);
  assert.equal(harness.requests[0][1].headers['X-Session-Token'],token);
  assert.equal(evidence.schema,'servicevoice-client-evidence-v2');
  assert.deepEqual(evidence.association,{session_id:sid,provider_call_id:cid,status:'verified_server_binding'});
  assert.equal(evidence.gateway.requests[0].output_text_sha256,'c'.repeat(64));
  assert.equal(evidence.gateway.requests[0].output_text_source,'verified_receipt');
  assert.equal(evidence.gateway.requests[0].milestones_ms.first_text,7);
  assert.equal(evidence.voice.gapP95,2500);
  assert.doesNotMatch(serialized,/PRIVATE-|PRIVATE TRANSCRIPT|PRIVATE CUSTOMER|5125550142|Authorization|"output_text"/);
  assert.equal(vm.runInContext('state.busy',harness.context),false);
  assert.equal(harness.elements.get('export-metrics').disabled,false);
});

test('slow export captures click-time metrics, coalesces repeated clicks and never blocks the voice controls', async () => {
  let resolve;
  const harness=await renderer(()=>new Promise(value=>{resolve=value;})); connected(harness);
  const pending=clickExport(harness); await clickExport(harness);
  assert.equal(harness.requests.length,1);
  assert.equal(harness.elements.get('export-metrics').disabled,true);
  vm.runInContext('state.voiceMetrics.gapSamplesMs.push(9000);state.voiceMetrics.gap=6000;renderMetrics();',harness.context);
  assert.equal(harness.elements.get('export-metrics').disabled,true);
  assert.equal(vm.runInContext('state.busy',harness.context),false);
  resolve(jsonResponse(timingPayload())); await pending;
  const evidence=await exported(harness);
  assert.deepEqual(evidence.voice.gapSamplesMs,[1500,2500]);
  assert.equal(evidence.voice.gap,2000);
});

test('export ignores a slow response after the user changes sessions', async () => {
  let resolve;
  const harness=await renderer(()=>new Promise(value=>{resolve=value;})); connected(harness);
  const pending=clickExport(harness);
  vm.runInContext('state.revision++;state.session={id:"sess_other",token:"other"};notice("Current session notice");',harness.context);
  resolve(jsonResponse(timingPayload())); await pending;
  assert.equal(harness.downloads.length,0);
  assert.equal(harness.elements.get('notice-text').textContent,'Current session notice');
});

for (const mutation of ['session','row','oversize']) test(`export rejects ${mutation} association/schema mismatch without downloading`, async () => {
  const data=timingPayload();
  if(mutation==='session')data.session_id='sess_other';
  if(mutation==='row')data.requests[0].call_key='d'.repeat(16);
  if(mutation==='oversize')data.requests=Array(257).fill(data.requests[0]);
  const harness=await renderer(async()=>jsonResponse(data));connected(harness);
  await clickExport(harness);
  assert.equal(harness.downloads.length,0);
  assert.match(harness.elements.get('notice-text').textContent,/identity could not be verified/);
});

test('the 3-second timeout aborts collection and labels browser-only evidence with no invented provider identity', async () => {
  const harness=await renderer((_,options)=>new Promise((_,reject)=>options.signal.addEventListener('abort',()=>reject(Object.assign(new Error('PRIVATE SERVER ERROR'),{name:'AbortError'})),{once:true})));
  connected(harness);const pending=clickExport(harness);
  const timeout=[...harness.timers.values()].find(timer=>timer.ms===3000);assert.ok(timeout);timeout.callback();await pending;
  const evidence=await exported(harness);
  assert.equal(harness.requests[0][1].signal.aborted,true);
  assert.deepEqual(evidence.association,{session_id:sid,provider_call_id:null,status:'unverified'});
  assert.equal(evidence.gateway.status,'unavailable');assert.deepEqual(evidence.gateway.requests,[]);
  assert.doesNotMatch(JSON.stringify(evidence),/PRIVATE SERVER ERROR|PRIVATE-CAPABILITY/);
});

test('pagehide aborts timing collection without exporting stale data', async () => {
  const harness=await renderer((_,options)=>new Promise((_,reject)=>options.signal.addEventListener('abort',()=>reject(new Error('aborted')),{once:true})));
  connected(harness);const pending=clickExport(harness);harness.windowListeners.pagehide();await pending;
  assert.equal(harness.requests.length,1);assert.equal(harness.requests[0][1].signal.aborted,true);
  assert.equal(harness.downloads.length,0);
});

test('unbound identity is explicit, raw sample retention is bounded and unknown string fields are omitted', async () => {
  const data=timingPayload();data.provider_call_id=null;data.requests=[];
  const harness=await renderer(async()=>jsonResponse(data));connected(harness);
  vm.runInContext('state.voiceMetrics.gapSamplesMs=Array(2050).fill(42);state.voiceMetrics.definition="PRIVATE TEXT";',harness.context);
  await clickExport(harness);const evidence=await exported(harness);
  assert.equal(evidence.association.status,'no_provider_binding');assert.equal(evidence.gateway.status,'unbound');
  assert.equal(evidence.voice.gapSamplesMs.length,2048);assert.equal(evidence.voice.samples_omitted,2);
  assert.doesNotMatch(JSON.stringify(evidence),/PRIVATE TEXT/);
});

test('a browser file-save error releases the export control and shows only a fixed error', async () => {
  const harness=await renderer(async()=>jsonResponse(timingPayload()));connected(harness);
  harness.context.URL.createObjectURL=()=>{throw new Error('PRIVATE FILE ERROR');};
  await clickExport(harness);
  assert.equal(harness.downloads.length,0);
  assert.equal(harness.elements.get('export-metrics').disabled,false);
  assert.match(harness.elements.get('notice-text').textContent,/file could not be saved/);
  assert.doesNotMatch(harness.elements.get('notice-text').textContent,/PRIVATE FILE ERROR/);
});

test('no export or gateway request is made before a voice call has connected', async () => {
  const harness=await renderer();connected(harness);
  vm.runInContext('state.voiceMetrics.hasConnected=false;',harness.context);
  await clickExport(harness);
  assert.equal(harness.requests.length,0);assert.equal(harness.downloads.length,0);
});

test('timing export retains reviewed issuance-context fallback reasons and narrator5', async () => {
  for(const reason of ['issuance_context_missing','issuance_context_changed']){
    const data=timingPayload();data.requests[0].narration_reason=reason;data.requests[0].narrator_version='receipt-en-2026-10-18-5';
    const harness=await renderer(async()=>jsonResponse(data));connected(harness);await clickExport(harness);
    const evidence=await exported(harness);
    assert.equal(evidence.gateway.requests[0].narration_reason,reason);
    assert.equal(evidence.gateway.requests[0].narrator_version,'receipt-en-2026-10-18-5');
  }
});

test('timing export preserves supersession outcomes separately from completion and excludes unknown values', async () => {
  const data=timingPayload();
  const outcomes=['superseded','superseded_after_release','supersession_wait_expired','PRIVATE-CALLER-TEXT'];
  data.requests=outcomes.map(outcome=>({...data.requests[0],outcome,narrator_version:'receipt-en-2026-10-18-6',
    output_text:'PRIVATE-CALLER-TEXT',token}));
  const harness=await renderer(async()=>jsonResponse(data));connected(harness);await clickExport(harness);
  const evidence=await exported(harness);
  assert.deepEqual(evidence.gateway.requests.map(row=>row.outcome),[...outcomes.slice(0,3),null]);
  assert.ok(evidence.gateway.requests.every(row=>row.narrator_version==='receipt-en-2026-10-18-6'));
  assert.doesNotMatch(JSON.stringify(evidence),/PRIVATE-CALLER-TEXT|PRIVATE-CAPABILITY/);
});
