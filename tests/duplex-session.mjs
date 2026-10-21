import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createHash } from 'node:crypto';
import { DuplexSession } from '../app/static/duplex-session.js';

const flush = async () => { for (let count = 0; count < 30; count++) await Promise.resolve(); };
function deferred() { let resolve; const promise = new Promise(yes => { resolve = yes; }); return { promise, resolve }; }
function clock() {
  let time = 0, id = 0; const timers = new Map();
  return { now: () => time,
    setTimeout(fn, ms) { const value = ++id; timers.set(value, { fn, at: time + ms }); return value; },
    clearTimeout(value) { timers.delete(value); },
    async advance(ms) {
      const target = time + ms;
      for (;;) {
        const next = [...timers].filter(([, row]) => row.at <= target).sort((a, b) => a[1].at - b[1].at)[0];
        if (!next) break; time = next[1].at; timers.delete(next[0]); next[1].fn(); await flush();
      }
      time = target; await flush();
    }, get pending() { return timers.size; },
  };
}
function pcm(value = 1000) { const data = new Uint8Array(640); const view = new DataView(data.buffer); for (let i = 0; i < 320; i++) view.setInt16(i * 2, value, true); return data; }
function reply(json, status = 200) { return { ok: status >= 200 && status < 300, status, json: async () => json }; }
function fixture({ handler = null, resume = null, getUserMedia = null, autoOpen = true } = {}) {
  const time = clock(), requests = [], sockets = [], contexts = [], microphones = [], events = [], socketCreated = deferred();
  const tracks = [{ enabled: true, stopped: 0, stop() { this.stopped++; } }];
  const stream = { getTracks: () => tracks, getAudioTracks: () => tracks };
  const audio = pcm(), hash = createHash('sha256').update(audio).digest('hex');
  const output = { cues: [], main: [], stops: 0, pauses: 0, resumes: 0, paused: false, closed: false,
    playCue(phrase) { const done = deferred(); const handle = { phrase, stopped: false, ended: done.promise,
      finish() { done.resolve(); },
      stop() { this.stopped = true; done.resolve(); } }; this.cues.push(handle); return handle; },
    enqueueMain(row) { this.main.push(row); return true; }, stopMain() { this.stops++; this.paused = false; },
    pauseMain() { this.pauses++; this.paused = true; }, resumeMain() { this.resumes++; this.paused = false; return true; },
    close() { this.closed = true; },
  };
  class Context {
    constructor() { contexts.push(this); this.sampleRate = 16000; this.state = 'running'; this.closes = 0; }
    async resume() { if (resume) await resume.promise; }
    async close() { this.state = 'closed'; this.closes++; }
  }
  class Socket {
    constructor(url) { this.url = url; this.readyState = 0; this.bufferedAmount = 0; this.listeners = new Map(); this.sent = []; sockets.push(this);
      socketCreated.resolve(this);
      if (autoOpen) queueMicrotask(() => { if (this.readyState === 0) { this.readyState = 1; this.emit('open'); } }); }
    addEventListener(name, fn) { if (!this.listeners.has(name)) this.listeners.set(name, []); this.listeners.get(name).push(fn); }
    emit(name, value) { for (const fn of this.listeners.get(name) || []) fn(value); }
    send(data) { this.sent.push(data); }
    close() { if (this.readyState === 3) return; this.readyState = 3; this.emit('close'); }
  }
  async function fetch(url, options = {}) {
    const request = { url, options, body: options.body ? JSON.parse(options.body) : null }; requests.push(request);
    if (handler) { const override = await handler(request); if (override) return override; }
    if (url === '/api/sessions') return reply({ id: 'fixture-session', token: 'fixture-token', mode: 'live' });
    if (url === '/api/duplex/config') return reply({ enabled: true, sample_rate: 16000,
      phrases: ['neutral_ack', 'moment'].map(phrase_id => ({ phrase_id, sample_rate: 16000, pcm_bytes: audio.byteLength, sha256: hash })) });
    if (url.includes('/duplex/audio/')) return { ok: true, arrayBuffer: async () => audio.slice().buffer };
    if (url.endsWith('/duplex/call')) return reply({ call_id: 'fixture-call', websocket_url: 'wss://backend.vapi.ai/fixture', sample_rate: 16000 });
    if (url.endsWith('/duplex/cue') && request.body.prepare_only) return reply({ phrase_id: 'silence', prepared: true,
      reason: 'waiting_prepared', expires_in_ms: 0, prepared_expires_in_ms: 2000,
      epoch: request.body.epoch, revision: request.body.revision, requests: 1 });
    if (url.endsWith('/duplex/cue')) return reply({ phrase_id: request.body.phase === 'listening' ? 'neutral_ack' : 'moment',
      confidence: .99, language_choice: 'cache_compatible', expires_in_ms: 800,
      prepared_reused: Boolean(request.body.prepared_revision), requests: request.body.prepared_revision ? 0 : 1 });
    if (url.endsWith('/duplex/stop')) return reply({ ok: true });
    if (url === '/api/sessions/fixture-session') return reply({ ended: false, stage: 'service', proposal: null, pending_operations: [] });
    return reply({ ok: true });
  }
  let mediaRequests = 0;
  const session = new DuplexSession({ callbacks: { event: event => events.push(event) }, runtime: {
    ...time, fetch, AudioContext: Context, WebSocket: Socket,
    getUserMedia: async () => { mediaRequests++; return getUserMedia ? await getUserMedia.promise : stream; },
    playbackFactory: options => { output.failure = options.onFailure; return output; },
    microphoneFactory: options => {
      const mic = { options, starts: 0, stops: 0, muted: false,
        async start() { this.starts++; }, async stop() { this.stops++; }, setMuted(value) { this.muted = value; tracks[0].enabled = !value; } };
      microphones.push(mic); return mic;
    },
  } });
  return { session, time, requests, sockets, contexts, microphones, output, tracks, stream, events,
    firstSocket: socketCreated.promise,
    get mediaRequests() { return mediaRequests; },
    cues: () => requests.filter(row => row.url.endsWith('/duplex/cue') && !row.body.prepare_only),
    preparations: () => requests.filter(row => row.url.endsWith('/duplex/cue') && row.body.prepare_only) };
}
function message(session, body) { session.receive(JSON.stringify(body)); }
function speech(session, role, status) { message(session, { type: 'speech-update', role, status }); }
function transcript(session, text, final = false) { message(session, { type: 'transcript', role: 'user', transcript: text, transcriptType: final ? 'final' : 'partial' }); }

async function ordinaryCompletedGreeting(f) {
  await f.session.start(); speech(f.session, 'assistant', 'started'); f.session.receive(pcm(900).buffer);
  speech(f.session, 'assistant', 'stopped');
  // Production playback uses an active-node Set. This fixture keeps a history
  // array for assertions and explicitly supplies the observed drained count.
  f.output.main.size = 0;
  speech(f.session, 'user', 'started'); transcript(f.session, 'What services are available?', true);
  assert.equal(f.session.ordinaryTurnDrained, true);
}

async function ordinaryYieldedTurn(f) {
  await ordinaryCompletedGreeting(f); speech(f.session, 'user', 'stopped'); await f.time.advance(160);
  assert.equal(f.session.callerSpeaking, false); assert.equal(f.session.mainAllowed, false);
}

test('ordinary voiced1600-byte packet0.1ms before speech-start is copied and delivered exactly once', async () => {
  const f = fixture(); await ordinaryYieldedTurn(f);
  const first = new Uint8Array(1600); const view = new DataView(first.buffer);
  for (let i = 0; i < 800; i++) view.setInt16(i * 2, 1800 + i, true);
  const expected = first.slice(); f.session.receive(first.buffer); first.fill(0);
  assert.equal(f.output.main.length, 1); assert.equal(f.session.mainAllowed, false);
  await f.time.advance(.1); speech(f.session, 'assistant', 'started');
  assert.equal(f.session.preStartPcm, null); assert.equal(f.output.main.length, 2);
  assert.deepEqual(f.output.main[1].pcm, expected);
  speech(f.session, 'assistant', 'started'); assert.equal(f.output.main.length, 2);
  f.session.receive(pcm(2400).buffer); assert.equal(f.output.main.length, 3);
  assert.equal(f.session.metrics().received_output_bytes, 2880); assert.equal(f.session.metrics().discarded_output_bytes, 0);
  const proof = f.events.find(row => row.kind === 'pre_start_pcm' && row.disposition === 'promoted');
  assert.ok(Math.abs(proof.held_ms - .1) < .0001); assert.equal(proof.buffered_bytes, 1600); await f.session.stop();
});

for (const scenario of ['deadline', 'revision', 'new_local_voice', 'new_provider_voice', 'sensitive_epoch', 'muted', 'closed', 'assistant_stopped', 'buffer_limit']) {
  test(`pre-start PCM quarantine is revoked by ${scenario} without replaying its prefix`, async () => {
    const f = fixture(); await ordinaryYieldedTurn(f); f.session.receive(pcm(2200).buffer);
    assert.ok(f.session.preStartPcm);
    if (scenario === 'deadline') await f.time.advance(350);
    else if (scenario === 'revision') transcript(f.session, 'What tire services are available?', true);
    else if (scenario === 'new_local_voice') f.session.onLocalSpeech(true);
    else if (scenario === 'new_provider_voice') speech(f.session, 'user', 'started');
    else if (scenario === 'sensitive_epoch') f.session.coordinator.setSensitive(true);
    else if (scenario === 'muted') f.session.setMuted(true);
    else if (scenario === 'closed') await f.session.stop();
    else if (scenario === 'assistant_stopped') speech(f.session, 'assistant', 'stopped');
    else { for (let count = 0; count < 17; count++) f.session.receive(pcm(2200).buffer); }
    assert.equal(f.session.preStartPcm, null); assert.equal(f.output.main.length, 1);
    speech(f.session, 'assistant', 'started'); assert.equal(f.output.main.length, 1);
    assert.ok(f.session.metrics().discarded_output_bytes >= 640); await f.session.stop();
  });
}

test('additional quarantined packets cannot extend the first packet deadline or reopen it after expiry', async () => {
  const f = fixture(); await ordinaryYieldedTurn(f); f.session.receive(pcm(2200).buffer);
  const state = f.session.preStartPcm; await f.time.advance(300); f.session.receive(pcm(2300).buffer);
  assert.equal(f.session.preStartPcm, state); await f.time.advance(50); assert.equal(f.session.preStartPcm, null);
  f.session.receive(pcm(2400).buffer); assert.equal(f.session.preStartPcm, null);
  speech(f.session, 'assistant', 'started'); assert.equal(f.output.main.length, 1); await f.session.stop();
});

test('post-barge-in PCM stays discarded even when a fresh assistant start arrives0.1ms later', async () => {
  const f = fixture(); await f.session.start(); speech(f.session, 'assistant', 'started'); f.session.receive(pcm(900).buffer);
  f.output.main.size = 1; speech(f.session, 'user', 'started'); transcript(f.session, 'Actually a tire rotation instead', true);
  speech(f.session, 'assistant', 'stopped'); f.output.main.size = 0;
  speech(f.session, 'user', 'stopped'); await f.time.advance(160); f.session.receive(pcm(2200).buffer);
  assert.equal(f.session.preStartPcm, null); await f.time.advance(.1); speech(f.session, 'assistant', 'started');
  assert.equal(f.output.main.length, 1); f.session.receive(pcm(2300).buffer); assert.equal(f.output.main.length, 2);
  assert.equal(f.session.metrics().discarded_output_bytes, 640); await f.session.stop();
});

test('delayed explicit interruption revokes ordinary eligibility even after caller-start was already handled', async () => {
  const f = fixture(); await ordinaryCompletedGreeting(f);
  assert.equal(f.session.callerSpeaking, true); assert.equal(f.session.ordinaryTurnDrained, true);
  message(f.session, { type: 'user-interrupted' });
  assert.equal(f.session.ordinaryTurnDrained, false);
  speech(f.session, 'assistant', 'started'); f.session.receive(pcm(2200).buffer);
  assert.equal(f.session.earlyMainStart, null);
  speech(f.session, 'user', 'stopped'); await f.time.advance(160); f.session.receive(pcm(2300).buffer);
  assert.equal(f.session.preStartPcm, null); assert.equal(f.output.main.length, 1);
  speech(f.session, 'assistant', 'started'); assert.equal(f.output.main.length, 1);
  f.session.receive(pcm(2400).buffer); assert.equal(f.output.main.length, 2); await f.session.stop();
});

test('no final or undrained prior reply can quarantine otherwise unannounced main audio', async () => {
  for (const scenario of ['no_final', 'undrained']) {
    const f = fixture(); await ordinaryYieldedTurn(f);
    if (scenario === 'no_final') f.session.lastUserTranscriptFinal = false;
    else f.output.main.size = 1;
    f.session.receive(pcm(2200).buffer); assert.equal(f.session.preStartPcm, null);
    speech(f.session, 'assistant', 'started'); assert.equal(f.output.main.length, 1); await f.session.stop();
  }
});

test('ordinary FAQ start12ms before provider stop preserves every early PCM sample after safe yield', async () => {
  const f = fixture(); await ordinaryCompletedGreeting(f);
  speech(f.session, 'assistant', 'started'); await f.time.advance(5);
  const early = pcm(2200); f.session.receive(early.buffer);
  assert.equal(f.output.main.length, 1, 'New answer cannot sound while caller gate is active');
  await f.time.advance(7); speech(f.session, 'user', 'stopped'); await f.time.advance(160);
  assert.equal(f.session.earlyMainStart, null); assert.equal(f.session.mainAllowed, true);
  assert.equal(f.output.main.length, 2); assert.deepEqual(f.output.main[1].pcm, early);
  assert.equal(f.session.metrics().received_output_bytes, 1280); assert.equal(f.session.metrics().discarded_output_bytes, 0);
  const promoted = f.events.find(row => row.kind === 'early_main_start' && row.disposition === 'promoted');
  assert.equal(promoted.held_ms, 172); assert.equal(promoted.buffered_bytes, 640);
  await f.session.stop();
});

test('ordinary early announcement without early PCM enables only future audio after both gates yield', async () => {
  const f = fixture(); await ordinaryCompletedGreeting(f); f.session.onLocalSpeech(true);
  speech(f.session, 'assistant', 'started'); speech(f.session, 'user', 'stopped'); await f.time.advance(20);
  assert.equal(f.session.callerSpeaking, true); assert.equal(f.session.mainAllowed, false);
  f.session.onLocalSpeech(false); await f.time.advance(160); f.session.receive(pcm(2200).buffer);
  assert.equal(f.output.main.length, 2); assert.equal(f.session.metrics().discarded_output_bytes, 0); await f.session.stop();
});

for (const scenario of ['deadline', 'revision', 'new_local_voice', 'new_provider_voice', 'sensitive_epoch', 'muted', 'closed', 'assistant_stopped', 'buffer_limit']) {
  test(`ordinary early-start hold is revoked by ${scenario} and cannot replay its prefix`, async () => {
    const f = fixture(); await ordinaryCompletedGreeting(f); speech(f.session, 'assistant', 'started');
    f.session.receive(pcm(2200).buffer);
    if (scenario === 'deadline') await f.time.advance(350);
    else if (scenario === 'revision') transcript(f.session, 'What tire services are available?', true);
    else if (scenario === 'new_local_voice') f.session.onLocalSpeech(true);
    else if (scenario === 'new_provider_voice') speech(f.session, 'user', 'started');
    else if (scenario === 'sensitive_epoch') f.session.coordinator.setSensitive(true);
    else if (scenario === 'muted') f.session.setMuted(true);
    else if (scenario === 'closed') await f.session.stop();
    else if (scenario === 'assistant_stopped') speech(f.session, 'assistant', 'stopped');
    else { for (let count = 0; count < 17; count++) f.session.receive(pcm(2200).buffer); }
    assert.equal(f.session.earlyMainStart, null); assert.equal(f.output.main.length, 1);
    if (!f.session.closed && !f.session.muted) {
      speech(f.session, 'user', 'stopped'); f.session.onLocalSpeech(false); await f.time.advance(160);
      f.session.receive(pcm(2300).buffer); assert.equal(f.output.main.length, 1);
    }
    assert.ok(f.session.metrics().discarded_output_bytes >= 640); await f.session.stop();
  });
}

test('duplicate early announcements cannot extend the original350ms ownership deadline', async () => {
  const f = fixture(); await ordinaryCompletedGreeting(f); speech(f.session, 'assistant', 'started');
  const state = f.session.earlyMainStart; await f.time.advance(300); speech(f.session, 'assistant', 'started');
  assert.equal(f.session.earlyMainStart, state); await f.time.advance(50); assert.equal(f.session.earlyMainStart, null);
  speech(f.session, 'assistant', 'started'); assert.equal(f.session.earlyMainStart, null); await f.session.stop();
});

test('a barge-in keeps the fresh-start fence and cannot latch a stale interrupted announcement', async () => {
  const f = fixture(); await f.session.start(); speech(f.session, 'assistant', 'started');
  f.session.receive(pcm(900).buffer); f.output.main.size = 1;
  speech(f.session, 'user', 'started'); transcript(f.session, 'Actually change the service', true);
  speech(f.session, 'assistant', 'stopped'); f.output.main.size = 0;
  speech(f.session, 'assistant', 'started'); f.session.receive(pcm(2200).buffer);
  assert.equal(f.session.ordinaryTurnDrained, false); assert.equal(f.session.earlyMainStart, null);
  speech(f.session, 'user', 'stopped'); await f.time.advance(160); f.session.receive(pcm(2300).buffer);
  assert.equal(f.output.main.length, 1); assert.equal(f.session.mainAllowed, false); await f.session.stop();
});

test('a caller continuation after yield cannot inherit ordinary-turn early-start eligibility', async () => {
  const f = fixture(); await ordinaryCompletedGreeting(f);
  speech(f.session, 'user', 'stopped'); await f.time.advance(160); speech(f.session, 'user', 'started');
  transcript(f.session, 'And what are your opening hours?', true); speech(f.session, 'assistant', 'started');
  assert.equal(f.session.ordinaryTurnDrained, false); assert.equal(f.session.earlyMainStart, null); await f.session.stop();
});

test('diagnostics expose assistant start before caller release without authorizing unbound PCM', async () => {
  const f = fixture(); await f.session.start(); speech(f.session, 'user', 'started');
  f.session.onLocalSpeech(true); transcript(f.session, 'What services are available?', true);
  speech(f.session, 'assistant', 'started');
  const announced = f.events.findLast(row => row.kind === 'provider_assistant_speech_update');
  assert.equal(announced.disposition, 'caller_gate_active');
  assert.equal(announced.before.caller_speaking, true); assert.equal(announced.before.local_speaking, true);
  assert.equal(announced.before.provider_speaking, true); assert.equal(f.session.mainAllowed, false);
  speech(f.session, 'user', 'stopped'); f.session.onLocalSpeech(false); await f.time.advance(160);
  f.session.receive(pcm(2000).buffer);
  assert.equal(f.output.main.length, 0, 'Diagnostics must not loosen the missing fresh-announcement guard');
  const rejected = f.events.findLast(row => row.kind === 'main_pcm_rejected');
  assert.equal(rejected.reason, 'awaiting_fresh_speech_start'); assert.equal(rejected.voiced, true);
  assert.equal(rejected.caller_speaking, false); assert.equal(rejected.main_allowed, false);
  assert.ok(rejected.generation_sequence > announced.generation_sequence);
  assert.equal(f.session.metrics().output_rejections.awaiting_fresh_speech_start.bytes, 640);
  await f.session.stop();
});

test('diagnostics retain successful announcement ordering and never include raw provider content', async () => {
  const f = fixture(); await f.session.start(); speech(f.session, 'user', 'started');
  transcript(f.session, 'Opening hours', true); speech(f.session, 'user', 'stopped'); await f.time.advance(160);
  message(f.session, { type: 'speech-update', role: 'assistant', status: 'started', transcript: 'PRIVATE_PROVIDER_FIXTURE',
    callId: 'PRIVATE_CALL_FIXTURE', metadata: { token: 'PRIVATE_TOKEN_FIXTURE' } });
  f.session.receive(pcm(2000).buffer);
  const row = f.events.findLast(event => event.kind === 'provider_assistant_speech_update');
  assert.equal(row.disposition, 'accepted'); assert.equal(row.before.caller_speaking, false);
  assert.equal(row.main_allowed, true); assert.equal(f.output.main.length, 1);
  assert.equal(JSON.stringify(f.events).includes('PRIVATE_'), false);
  await f.session.stop();
});

test('speech telemetry and PCM rejection records are bounded while aggregate byte counts remain exact', async () => {
  const f = fixture(); await f.session.start(); speech(f.session, 'user', 'started');
  for (let index = 0; index < 500; index++) {
    message(f.session, { type: 'speech-update', role: 'assistant', status: 'PRIVATE_UNSUPPORTED_STATUS' });
    f.session.receive(pcm(2000).buffer);
  }
  assert.equal(f.events.filter(row => row.kind === 'provider_assistant_speech_update').length, 256);
  assert.ok(f.events.filter(row => row.kind === 'main_pcm_rejected').length <= 64);
  assert.equal(f.session.metrics().output_rejections.caller_speaking.packets, 500);
  assert.equal(f.session.metrics().output_rejections.caller_speaking.bytes, 500 * 640);
  assert.ok(f.session.metrics().transport_diagnostic_events_dropped > 0);
  assert.equal(JSON.stringify(f.events).includes('PRIVATE_'), false);
  await f.session.stop();
});

test('exact full short partial is prepared without audio and activated only after fresh confirmed yield', async () => {
  const f = fixture(); await f.session.start(); speech(f.session, 'user', 'started');
  transcript(f.session, 'Alex Morgan'); await f.time.advance(249);
  assert.equal(f.preparations().length, 0); await f.time.advance(1);
  const preparation = f.preparations()[0];
  assert.equal(preparation.body.text, 'Alex Morgan'); assert.equal(preparation.body.phase, 'waiting');
  assert.equal(f.output.cues.length, 0); assert.equal(f.cues().length, 0);
  await f.time.advance(400); assert.equal(f.output.cues.length, 0, 'Prediction is nonplaying while caller owns input');
  transcript(f.session, 'Alex Morgan', true); speech(f.session, 'user', 'stopped'); await f.time.advance(160);
  const activation = f.cues()[0];
  assert.equal(activation.body.prepared_revision, preparation.body.revision);
  assert.ok(activation.body.revision > preparation.body.revision);
  assert.equal(activation.body.text, preparation.body.text); assert.equal(activation.body.epoch, preparation.body.epoch);
  assert.equal(Object.hasOwn(activation.body, 'prepare_only'), false);
  await f.time.advance(549); assert.equal(f.output.cues.length, 0);
  await f.time.advance(1); assert.equal(f.output.cues[0].phrase, 'moment');
  assert.equal(f.session.metrics().waiting_preparation_requests, 1);
  assert.equal(f.session.metrics().waiting_preparation_reuses, 1); await f.session.stop();
});

test('a fast voiced answer revokes an exact prepared waiting result before its playback guard', async () => {
  const f = fixture(); await f.session.start(); speech(f.session, 'user', 'started');
  transcript(f.session, 'Opening hours'); await f.time.advance(650);
  transcript(f.session, 'Opening hours', true); speech(f.session, 'user', 'stopped'); await f.time.advance(160);
  assert.ok(f.cues()[0].body.prepared_revision);
  speech(f.session, 'assistant', 'started'); await f.time.advance(400); f.session.receive(pcm(2000).buffer);
  await f.time.advance(300); assert.equal(f.output.cues.length, 0); assert.equal(f.output.main.length, 1);
  assert.equal(f.session.waitingPrepared, null); await f.session.stop();
});

test('ordinary partial rewrites immediately abort prediction and its ignored-abort response never becomes playable', async () => {
  const held = deferred(); const f = fixture({ handler: request =>
    request.body?.prepare_only ? held.promise : null });
  await f.session.start(); speech(f.session, 'user', 'started'); transcript(f.session, 'Opening hours');
  await f.time.advance(250); const old = f.preparations()[0];
  transcript(f.session, 'Service prices'); assert.equal(old.options.signal.aborted, true);
  held.resolve(reply({ phrase_id: 'silence', prepared: true, reason: 'waiting_prepared', expires_in_ms: 0,
    prepared_expires_in_ms: 2000, revision: old.body.revision, epoch: old.body.epoch })); await flush();
  assert.equal(f.session.waitingPrepared, null); assert.equal(f.output.cues.length, 0);
  transcript(f.session, 'Service prices', true); speech(f.session, 'user', 'stopped'); await f.time.advance(710);
  assert.equal(Object.hasOwn(f.cues()[0].body, 'prepared_revision'), false);
  assert.equal(f.cues()[0].body.text, 'Service prices'); await f.session.stop();
});

for (const control of ['Yes', 'No', 'Only if I confirm', 'Please reply in Spanish']) {
  test(`raw ${JSON.stringify(control)} immediately invalidates a completed prediction without activation`, async () => {
    const f = fixture(); await f.session.start(); speech(f.session, 'user', 'started');
    transcript(f.session, 'Opening hours'); await f.time.advance(250); assert.ok(f.session.waitingPrepared);
    transcript(f.session, control); assert.equal(f.session.waitingPrepared, null);
    transcript(f.session, control, true); speech(f.session, 'user', 'stopped'); await f.time.advance(1200);
    assert.equal(f.cues().length, 0); assert.equal(f.output.cues.length, 0); assert.equal(f.preparations().length, 1);
    await f.session.stop();
  });
}

test('a confirmed caller resume discards a completed receipt even if the same words are repeated', async () => {
  const f = fixture(); await f.session.start(); speech(f.session, 'user', 'started');
  transcript(f.session, 'Opening hours'); await f.time.advance(250);
  transcript(f.session, 'Opening hours', true); speech(f.session, 'user', 'stopped'); await f.time.advance(160);
  assert.equal(f.cues().length, 0, 'The shared request cadence has not elapsed');
  speech(f.session, 'user', 'started'); assert.equal(f.session.waitingPrepared, null);
  transcript(f.session, 'Opening hours', true); speech(f.session, 'user', 'stopped'); await f.time.advance(710);
  assert.equal(f.cues().length, 1); assert.equal(Object.hasOwn(f.cues()[0].body, 'prepared_revision'), false);
  await f.session.stop();
});

test('a changed assistant question cannot activate an earlier completed preparation receipt', async () => {
  const f = fixture(); await f.session.start(); speech(f.session, 'user', 'started');
  transcript(f.session, 'Opening hours'); await f.time.advance(650); assert.ok(f.session.waitingPrepared);
  message(f.session, { type: 'transcript', role: 'assistant', transcript: 'Should I save this appointment?', transcriptType: 'final' });
  assert.equal(f.session.waitingPrepared, null);
  transcript(f.session, 'Opening hours', true); speech(f.session, 'user', 'stopped'); await f.time.advance(160);
  assert.equal(Object.hasOwn(f.cues()[0].body, 'prepared_revision'), false); await f.session.stop();
});

test('a malformed playable prediction response never reaches output or binds a reuse receipt', async () => {
  const f = fixture({ handler: request => request.body?.prepare_only
    ? reply({ phrase_id: 'moment', confidence: .99, expires_in_ms: 800, prepared: true,
      reason: 'waiting_prepared', epoch: request.body.epoch, revision: request.body.revision, prepared_expires_in_ms: 2000 }) : null });
  await f.session.start(); speech(f.session, 'user', 'started'); transcript(f.session, 'Opening hours');
  await f.time.advance(1000); assert.equal(f.output.cues.length, 0); assert.equal(f.session.waitingPrepared, null);
  await f.session.stop();
});

test('a listening classification has priority and preparation cannot send a concurrent newer server revision', async () => {
  const held = deferred(); const f = fixture({ handler: request => request.body?.phase === 'listening' ? held.promise : null });
  await f.session.start(); speech(f.session, 'user', 'started');
  transcript(f.session, 'Please explain all the service options'); await f.time.advance(700);
  assert.equal(f.cues().length, 1); assert.equal(f.preparations().length, 0); assert.equal(f.cues()[0].options.signal.aborted, false);
  held.resolve(reply({ phrase_id: 'neutral_ack', confidence: .99, language_choice: 'cache_compatible', expires_in_ms: 800 }));
  await flush(); await f.time.advance(350);
  assert.equal(f.output.cues.length, 1); assert.equal(f.preparations().length, 1);
  assert.equal(f.preparations()[0].body.text, 'Please explain all the service options');
  await f.session.stop();
});

test('preparation is capped twice per utterance and stop cancels every owned timer and request', async () => {
  const f = fixture(); await f.session.start(); speech(f.session, 'user', 'started');
  transcript(f.session, 'Opening hours'); await f.time.advance(400);
  transcript(f.session, 'Service prices', true); await f.time.advance(1000);
  for (const text of ['Vehicle inspections', 'Diagnostic options']) {
    transcript(f.session, text); await f.time.advance(400);
  }
  assert.equal(f.preparations().length, 2); assert.equal(f.session.coordinator.preparationRequests, 2);
  for (const cue of f.output.cues) cue.finish(); await flush();
  transcript(f.session, 'Diagnostic options', true); speech(f.session, 'user', 'stopped'); await f.time.advance(160);
  assert.equal(f.cues().filter(row => row.body.phase === 'waiting').length, 1, 'A fresh actual waiting attempt remains available without any cache');
  await f.session.stop(); assert.equal(f.time.pending, 0); assert.equal(f.session.waitingPrepared, null);
});

test('an exact in-flight prediction joins the original waiting deadline then activates with a completed receipt', async () => {
  const held = deferred(); const f = fixture({ handler: request => request.body?.prepare_only ? held.promise : null });
  await f.session.start(); speech(f.session, 'user', 'started'); transcript(f.session, 'Opening hours');
  await f.time.advance(250); const preparation = f.preparations()[0];
  transcript(f.session, 'Opening hours', true); speech(f.session, 'user', 'stopped'); await f.time.advance(350);
  assert.equal(f.session.callerSpeaking, false); assert.equal(preparation.options.signal.aborted, false);
  assert.equal(f.cues().length, 0); assert.equal(f.output.cues.length, 0);
  await f.time.advance(50);
  held.resolve(reply({ phrase_id: 'silence', prepared: true, reason: 'waiting_prepared', expires_in_ms: 0,
    prepared_expires_in_ms: 2000, epoch: preparation.body.epoch, revision: preparation.body.revision })); await flush();
  assert.equal(f.cues().length, 1); assert.equal(f.cues()[0].body.prepared_revision, preparation.body.revision);
  assert.equal(f.events.find(row => row.kind === 'waiting_preparation_joined').join_ms, 50);
  await f.time.advance(309); assert.equal(f.output.cues.length, 0); await f.time.advance(1);
  assert.equal(f.output.cues[0].phrase, 'moment'); await f.session.stop();
});

test('a provider-final snapshot can prepare inside the160ms endpoint tail but still cannot play before fresh yield', async () => {
  const held = deferred(); const f = fixture({ handler: request => request.body?.prepare_only ? held.promise : null });
  await f.session.start(); transcript(f.session, 'Alex Morgan', true); await f.time.advance(0);
  assert.equal(f.preparations().length, 1, 'Final bypasses only the250ms partial stability delay');
  const preparation = f.preparations()[0];
  await f.time.advance(159); assert.equal(f.session.callerSpeaking, true); assert.equal(f.output.cues.length, 0);
  await f.time.advance(1); assert.equal(f.session.callerSpeaking, false); assert.equal(f.output.cues.length, 0);
  await f.time.advance(290); assert.equal(f.cues().length, 0);
  held.resolve(reply({ phrase_id: 'silence', prepared: true, reason: 'waiting_prepared', expires_in_ms: 0,
    prepared_expires_in_ms: 2000, epoch: preparation.body.epoch, revision: preparation.body.revision })); await flush();
  assert.equal(f.cues()[0].body.prepared_revision, preparation.body.revision);
  assert.equal(f.events.find(row => row.kind === 'waiting_preparation_joined').join_ms, 100);
  await f.time.advance(259); assert.equal(f.output.cues.length, 0); await f.time.advance(1);
  assert.equal(f.output.cues[0].phrase, 'moment'); await f.session.stop();
});

test('actual main during a prediction join aborts prediction immediately without waiting for HTTP or playing a cue', async () => {
  const held = deferred(); const f = fixture({ handler: request => request.body?.prepare_only ? held.promise : null });
  await f.session.start(); speech(f.session, 'user', 'started'); transcript(f.session, 'Opening hours');
  await f.time.advance(250); const preparation = f.preparations()[0];
  transcript(f.session, 'Opening hours', true); speech(f.session, 'user', 'stopped'); await f.time.advance(350);
  speech(f.session, 'assistant', 'started'); f.session.receive(pcm(2000).buffer);
  assert.equal(preparation.options.signal.aborted, true); assert.equal(f.output.main.length, 1);
  held.resolve(reply({ phrase_id: 'silence', prepared: true, reason: 'waiting_prepared', expires_in_ms: 0,
    prepared_expires_in_ms: 2000, epoch: preparation.body.epoch, revision: preparation.body.revision }));
  await flush(); await f.time.advance(1000);
  assert.equal(f.cues().length, 0); assert.equal(f.output.cues.length, 0); assert.equal(f.session.waitingPrepared, null);
  await f.session.stop();
});

test('joining prediction cannot restart or extend the original850ms selection deadline', async () => {
  const preparation = deferred(), activation = deferred(); const f = fixture({ handler: request =>
    request.url.endsWith('/duplex/cue') ? request.body.prepare_only ? preparation.promise : activation.promise : null });
  await f.session.start(); speech(f.session, 'user', 'started'); transcript(f.session, 'Opening hours');
  await f.time.advance(250); const request = f.preparations()[0];
  transcript(f.session, 'Opening hours', true); speech(f.session, 'user', 'stopped'); await f.time.advance(800);
  preparation.resolve(reply({ phrase_id: 'silence', prepared: true, reason: 'waiting_prepared', expires_in_ms: 0,
    prepared_expires_in_ms: 2000, epoch: request.body.epoch, revision: request.body.revision })); await flush();
  assert.equal(f.time.now(), 1050); assert.equal(f.cues().length, 1);
  await f.time.advance(399); assert.equal(f.cues()[0].options.signal.aborted, false);
  await f.time.advance(1); assert.equal(f.cues()[0].options.signal.aborted, true);
  activation.resolve(reply({ phrase_id: 'moment', confidence: .99, language_choice: 'cache_compatible', expires_in_ms: 800 }));
  await flush(); assert.equal(f.output.cues.length, 0); await f.session.stop();
});

test('a long ordinary stream keeps its second preparation opportunity for a final stable tail', async () => {
  const f = fixture(); await f.session.start(); speech(f.session, 'user', 'started');
  transcript(f.session, 'Opening hours'); await f.time.advance(400);
  for (const text of ['Service prices', 'Vehicle inspections', 'Diagnostic options', 'Maintenance options']) {
    transcript(f.session, text); await f.time.advance(700);
  }
  assert.equal(f.preparations().length, 1, 'An actively growing long turn cannot spend both early predictions');
  transcript(f.session, 'Maintenance options', true); await f.time.advance(250);
  assert.equal(f.preparations().length, 2); assert.equal(f.preparations()[1].body.text, 'Maintenance options');
  speech(f.session, 'user', 'stopped'); await f.time.advance(160);
  assert.equal(f.cues().findLast(row => row.body.phase === 'waiting').body.prepared_revision, f.preparations()[1].body.revision);
  await f.session.stop();
});

test('an expired completed preparation receipt causes ordinary fresh selection without rolling its lifetime', async () => {
  const f = fixture({ handler: request => request.body?.prepare_only ? reply({ phrase_id: 'silence', prepared: true,
    reason: 'waiting_prepared', expires_in_ms: 0, prepared_expires_in_ms: 100,
    epoch: request.body.epoch, revision: request.body.revision }) : null });
  await f.session.start(); speech(f.session, 'user', 'started'); transcript(f.session, 'Opening hours');
  await f.time.advance(650); transcript(f.session, 'Opening hours', true); speech(f.session, 'user', 'stopped');
  await f.time.advance(160); assert.equal(f.cues().length, 1);
  assert.equal(Object.hasOwn(f.cues()[0].body, 'prepared_revision'), false); await f.session.stop();
});

for (const action of ['hard_context', 'stop']) {
  test(`a ${action} change cancels an in-flight preparation join and cannot activate its late receipt`, async () => {
    const held = deferred(); let blocked = false;
    const f = fixture({ handler: request => {
      if (request.body?.prepare_only) return held.promise;
      if (request.url === '/api/sessions/fixture-session' && blocked)
        return reply({ ended: false, stage: 'confirmation', proposal: {}, pending_operations: [] });
      return null;
    } });
    await f.session.start(); speech(f.session, 'user', 'started'); transcript(f.session, 'Opening hours');
    await f.time.advance(250); const preparation = f.preparations()[0];
    transcript(f.session, 'Opening hours', true); speech(f.session, 'user', 'stopped'); await f.time.advance(350);
    if (action === 'stop') await f.session.stop();
    else { blocked = true; await f.session.refreshSnapshot(); }
    assert.equal(preparation.options.signal.aborted, true);
    held.resolve(reply({ phrase_id: 'silence', prepared: true, reason: 'waiting_prepared', expires_in_ms: 0,
      prepared_expires_in_ms: 2000, epoch: preparation.body.epoch, revision: preparation.body.revision })); await flush();
    assert.equal(f.cues().length, 0); assert.equal(f.output.cues.length, 0); assert.equal(f.session.waitingPrepared, null);
    await f.session.stop(); assert.equal(f.time.pending, 0);
  });
}

test('an early provider main announcement cannot erase a waiting cue during a silent TTS gap', async () => {
  const f = fixture(); await f.session.start(); transcript(f.session, 'What services are available?', true);
  await f.time.advance(160); speech(f.session, 'assistant', 'started');
  f.session.receive(pcm(0).buffer); await f.time.advance(550);
  assert.equal(f.output.cues.length, 1); assert.equal(f.output.cues[0].phrase, 'moment');
  assert.equal(f.output.main.length, 0);
  f.session.receive(pcm(2000).buffer);
  assert.equal(f.output.cues[0].stopped, false); assert.equal(f.output.main.length, 1); await f.session.stop();
});

test('fast voiced main still cancels held waiting even after an early announcement', async () => {
  const f = fixture(); await f.session.start(); transcript(f.session, 'What services are available?', true);
  await f.time.advance(160); speech(f.session, 'assistant', 'started');
  await f.time.advance(400); f.session.receive(pcm(2000).buffer); await f.time.advance(300);
  assert.equal(f.output.cues.length, 0); assert.equal(f.output.main.length, 1); await f.session.stop();
});

test('idle microphone noise cannot cancel waiting selection or manufacture output pauses', async () => {
  const f = fixture(); await f.session.start(); transcript(f.session, 'What services are available?', true);
  await f.time.advance(160); f.microphones[0].options.onSpeech(true);
  await f.time.advance(550);
  assert.equal(f.output.cues.length, 1); assert.equal(f.output.pauses, 0);
  assert.equal(f.session.metrics().local_energy_onsets_ignored_without_output, 1);
  await f.session.stop();
});

test('false-energy cooldown protects main output but never delays a confirmed caller interruption', async () => {
  const f = fixture(); await f.session.start(); speech(f.session, 'assistant', 'started'); f.session.receive(pcm().buffer);
  f.microphones[0].options.onSpeech(true); await f.time.advance(100); f.microphones[0].options.onSpeech(false);
  assert.equal(f.output.pauses, 1); assert.equal(f.output.resumes, 1);
  for (let i = 0; i < 5; i++) {
    f.microphones[0].options.onSpeech(true); await f.time.advance(80); f.microphones[0].options.onSpeech(false);
    f.session.receive(pcm().buffer);
  }
  assert.equal(f.output.pauses, 1); assert.equal(f.session.metrics().local_energy_onsets_ignored_during_cooldown, 5);
  speech(f.session, 'user', 'started'); assert.equal(f.session.callerSpeaking, true);
  assert.equal(f.session.mainAllowed, false); assert.equal(f.session.mainSpeaking, false);
  const accepted = f.output.main.length; f.session.receive(pcm(2000).buffer); assert.equal(f.output.main.length, accepted);
  await f.session.stop();
});

test('duplicate and late punctuated ASR finals do not create tiny turns or cut a listening cue', async () => {
  const f = fixture(); await f.session.start(); speech(f.session, 'user', 'started');
  transcript(f.session, 'I need routine service for my vehicle'); await f.time.advance(0);
  const utterance = f.session.coordinator.utteranceId; const cue = f.output.cues[0];
  transcript(f.session, 'I need routine service for my vehicle.', true); speech(f.session, 'user', 'stopped');
  await f.time.advance(160); transcript(f.session, 'I need routine service for my vehicle!', true);
  await f.time.advance(10);
  assert.equal(f.session.coordinator.utteranceId, utterance); assert.equal(cue.stopped, false);
  assert.equal(f.events.filter(x => x.kind === 'caller_started' && x.source === 'user_transcript').length, 0);
  assert.ok(f.events.some(x => x.kind === 'transcript_refinement_ignored')); await f.session.stop();
});

test('late partial-prefix replay228ms after yield keeps an approved waiting request and its original budget', async () => {
  const held = deferred(); const f = fixture({ handler: request => request.url.endsWith('/duplex/cue') && !request.body.prepare_only ? held.promise : null });
  await f.session.start(); transcript(f.session, 'Please explain the regular service options', true); await f.time.advance(160);
  const epoch = f.session.coordinator.epoch, generation = f.session.generation, revision = f.session.revision;
  await f.time.advance(228); const selected = f.cues()[0]; assert.ok(selected);
  for (const value of ['Please explain', 'Please explain the regular', 'Please explain the regular service options']) transcript(f.session, value);
  transcript(f.session, 'Please explain the regular service options.', true);
  assert.equal(f.session.coordinator.epoch, epoch); assert.equal(f.session.revision, revision);
  assert.equal(f.session.generation, generation); assert.equal(f.session.callerSpeaking, false);
  assert.equal(selected.options.signal.aborted, false);
  held.resolve(reply({ phrase_id: 'moment', confidence: .99, language_choice: 'cache_compatible', expires_in_ms: 800 }));
  await flush(); await f.time.advance(400); assert.equal(f.output.cues.length, 1); assert.equal(f.cues().length, 1);
  assert.equal(f.events.some(row => row.kind === 'caller_resumed'), false); await f.session.stop();
});

for (const fastMain of [false, true]) {
  test(`an identical uncommitted second-clause tail347ms after yield preserves ${fastMain ? 'the fast main generation' : 'the ready waiting cue'}`, async () => {
    const f = fixture({ handler: request => request.url === '/api/sessions/fixture-session'
      ? reply({ ended: false, stage: 'details', proposal: { kind: 'book' }, pending_operations: [] }) : null });
    await f.session.start(); await f.session.refreshSnapshot(); speech(f.session, 'user', 'started');
    const first = 'I have a vehicle and would like to explain my routine maintenance needs.';
    const second = 'I drive mostly around town and would like to understand your services.';
    transcript(f.session, first, true); transcript(f.session, second);
    speech(f.session, 'user', 'stopped'); await f.time.advance(160);
    const generation = f.session.generation, revision = f.session.revision, epoch = f.session.coordinator.epoch;
    await f.time.advance(300);
    if (fastMain) { speech(f.session, 'assistant', 'started'); f.session.receive(pcm().buffer); }
    const currentEpoch = f.session.coordinator.epoch;
    await f.time.advance(47); transcript(f.session, second); await f.time.advance(7); transcript(f.session, second, true);
    assert.equal(f.session.callerSpeaking, false); assert.equal(f.session.generation, generation);
    assert.equal(f.session.revision, revision); assert.equal(f.session.coordinator.epoch, currentEpoch);
    assert.equal(f.events.some(row => row.kind === 'caller_resumed'), false);
    assert.equal(f.session.finalSegments.join(' '), first + ' ' + second, 'The matching final commits every existing fact');
    await f.time.advance(300);
    assert.equal(f.output.cues.length, fastMain ? 0 : 1);
    if (!fastMain) {
      assert.equal(f.session.coordinator.epoch, epoch); assert.equal(f.cues().length, 1);
      speech(f.session, 'user', 'started'); transcript(f.session, 'The vehicle is a blue sedan.', true);
      assert.ok(f.session.partial.includes(first) && f.session.partial.includes(second) && f.session.partial.includes('blue sedan'));
    }
    await f.session.stop();
  });
}

for (const tail of ['I drive mostly around town and would like to understand your services only if the price is lower.', 'Actually I need a different service.', 'Stop.']) {
  test(`a changed second-clause tail still revokes the earlier decision: ${tail}`, async () => {
    const f = fixture({ handler: request => request.url === '/api/sessions/fixture-session'
      ? reply({ ended: false, stage: 'details', proposal: { kind: 'book' }, pending_operations: [] }) : null });
    await f.session.start(); await f.session.refreshSnapshot(); speech(f.session, 'user', 'started');
    transcript(f.session, 'I have a vehicle and need routine maintenance.', true);
    transcript(f.session, 'I drive mostly around town and would like to understand your services.');
    speech(f.session, 'user', 'stopped'); await f.time.advance(507);
    const oldRequest = f.cues().find(row => row.body.phase === 'waiting'); assert.ok(oldRequest); transcript(f.session, tail);
    assert.equal(oldRequest.options.signal.aborted, true); assert.equal(f.output.cues.length, 0);
    await f.session.stop();
  });
}

for (const change of ['Actually explain tire repairs instead', 'Please stop', 'Please explain the regular service options for trucks']) {
  test(`a genuinely changed tail still cancels the old waiting request: ${change}`, async () => {
    const held = deferred(); const f = fixture({ handler: request => request.url.endsWith('/duplex/cue') && !request.body.prepare_only ? held.promise : null });
    await f.session.start(); transcript(f.session, 'Please explain the regular service options', true); await f.time.advance(388);
    const selected = f.cues()[0]; transcript(f.session, change);
    assert.equal(selected.options.signal.aborted, true);
    held.resolve(reply({ phrase_id: 'moment', confidence: .99, language_choice: 'cache_compatible', expires_in_ms: 800 }));
    await flush(); assert.equal(f.output.cues.length, 0); await f.session.stop();
  });
}

for (const source of ['provider', 'local_and_transcript']) {
  test(`real ${source} speech after a reserved waiting request gets one new epoch without losing prior facts`, async () => {
    const held = deferred(); let reservations = 0;
    const f = fixture({ handler: request => {
      if (!request.url.endsWith('/duplex/cue') || request.body.prepare_only) return null;
      reservations++;
      return reservations === 1 ? held.promise : reply({ phrase_id: 'moment', confidence: .99, language_choice: 'cache_compatible', expires_in_ms: 800 });
    } });
    await f.session.start(); transcript(f.session, 'The vehicle is a blue sedan', true); await f.time.advance(388);
    const first = f.cues()[0], old = f.session.coordinator.utteranceId;
    if (source === 'provider') speech(f.session, 'user', 'started');
    else f.session.onLocalSpeech(true);
    transcript(f.session, 'Actually the sedan is red', true);
    assert.equal(f.session.coordinator.utteranceId, old + 1); assert.equal(first.options.signal.aborted, true);
    speech(f.session, 'user', 'started'); assert.equal(f.session.coordinator.utteranceId, old + 1);
    assert.ok(f.session.partial.includes('blue sedan')); assert.ok(f.session.partial.includes('red'));
    held.resolve(reply({ phrase_id: 'moment', confidence: .99, language_choice: 'cache_compatible', expires_in_ms: 800 }));
    speech(f.session, 'user', 'stopped'); f.session.onLocalSpeech(false); await f.time.advance(1100);
    assert.equal(f.output.cues.length, 1); assert.equal(reservations, 2);
    assert.equal(f.cues()[1].body.epoch, old + 1); assert.equal(f.cues()[1].body.follow_up, undefined);
    assert.equal(f.session.coordinator.requestTimes.length >= 2, true, 'Rolling attempts are not refunded by the fresh epoch');
    await f.session.stop();
  });
}

test('ten separated false energy bursts can pause one main generation at most twice', async () => {
  const f = fixture(); await f.session.start(); speech(f.session, 'assistant', 'started'); f.session.receive(pcm().buffer);
  const generation = f.session.generation;
  for (let index = 0; index < 10; index++) {
    f.microphones[0].options.onSpeech(true); await f.time.advance(100); f.microphones[0].options.onSpeech(false);
    await f.time.advance(3100); f.session.receive(pcm().buffer);
  }
  assert.equal(f.output.pauses, 2); assert.equal(f.output.resumes, 2);
  assert.equal(f.session.generation, generation); assert.equal(f.session.mainSpeaking, true);
  speech(f.session, 'user', 'started'); assert.equal(f.session.mainSpeaking, false);
  assert.equal(f.session.generation, null); await f.session.stop();
});

test('unconfirmed energy preserves an active cached hold but confirmed speech stops it', async () => {
  const f = fixture(); await f.session.start(); transcript(f.session, 'What services are available?', true);
  await f.time.advance(710); const cue = f.output.cues[0];
  f.microphones[0].options.onSpeech(true); await f.time.advance(100); f.microphones[0].options.onSpeech(false);
  assert.equal(cue.stopped, false); await f.time.advance(200); cue.finish(); await flush();
  assert.equal(cue.stopped, false); assert.ok(f.events.some(x => x.kind === 'cue_ended'));
  await f.session.stop();
  const g = fixture(); await g.session.start(); transcript(g.session, 'What services are available?', true);
  await g.time.advance(710); const interrupted = g.output.cues[0];
  g.microphones[0].options.onSpeech(true); speech(g.session, 'user', 'started');
  assert.equal(interrupted.stopped, true); await g.session.stop();
});

test('noise during cue-only playback cannot buffer newly-ready main behind a350ms provisional pause', async () => {
  const f = fixture(); await f.session.start(); transcript(f.session, 'What services are available?', true);
  await f.time.advance(710); const cue = f.output.cues[0];
  f.microphones[0].options.onSpeech(true);
  assert.equal(f.session.provisional, null); assert.equal(cue.stopped, false);
  speech(f.session, 'assistant', 'started'); f.session.receive(pcm(2000).buffer);
  assert.equal(f.output.main.length, 1); assert.equal(cue.stopped, false);
  assert.equal(f.output.pauses, 0); assert.equal(f.session.metrics().local_energy_onsets_ignored_during_cached_cue, 1);
  await f.session.stop();
});

for (const text of ['I have a 2020 Toyota Camry', 'My car is not starting this morning',
  'The engine does not start', 'I want to change the oil', 'What is the current wait time for service']) {
  test(`routine service text reaches Jev without the old lexical false block: ${text}`, async () => {
    const f = fixture(); await f.session.start(); speech(f.session, 'user', 'started'); transcript(f.session, text);
    await f.time.advance(0); assert.equal(f.session.coordinator.sensitive, false); assert.equal(f.cues().length, 1);
    await f.session.stop();
  });
}

for (const text of ['Do not start booking', 'No', 'My PIN is 2020',
  'My vehicle card is 4111 1111 1111 1111', 'Call my phone 202 555 0100', 'My email is alex@example.com']) {
  test(`routine exceptions never hide a sensitive control or contact: ${text}`, async () => {
    const f = fixture(); await f.session.start(); speech(f.session, 'user', 'started'); transcript(f.session, text);
    await f.time.advance(0); assert.equal(f.session.coordinator.sensitive, true); assert.equal(f.cues().length, 0);
    await f.session.stop();
  });
}

test('eight finalized clauses retain the entire bounded car story through endpointing jitter', async () => {
  const f = fixture(); await f.session.start(); speech(f.session, 'user', 'started');
  const clauses = ['I have a 2020 Toyota Camry.', 'I drive it mostly around town.', 'It has been making a noise.',
    'The noise is near the front wheel.', 'I would like to understand the options.', 'The car is not starting reliably.',
    'I need to arrange routine service.', 'What can you do?'];
  for (const clause of clauses) {
    transcript(f.session, clause.replace(/\S+\s*$/, 'different')); await f.time.advance(20);
    transcript(f.session, clause, true); speech(f.session, 'user', 'stopped');
    await f.time.advance(50); speech(f.session, 'user', 'started');
  }
  assert.equal(f.session.coordinator.utteranceId, 1); assert.equal(f.session.finalSegments.length, 8);
  assert.ok(f.session.partial.startsWith(clauses[0])); assert.ok(f.session.partial.endsWith(clauses.at(-1)));
  assert.ok(!f.session.partial.includes('different'));
  await f.time.advance(110); f.output.cues[0].finish(); await flush();
  speech(f.session, 'user', 'stopped'); await f.time.advance(710);
  const waitingRequest = f.cues().findLast(row => row.body.phase === 'waiting');
  assert.ok(waitingRequest); assert.equal(waitingRequest.body.text, clauses.join(' '));
  assert.equal(f.events.filter(x => x.kind === 'caller_started' && x.source).length, 1);
  await f.session.stop();
});

test('a resumed clause keeps listening ownership but a real answered turn clears accumulated text', async () => {
  const f = fixture(); await f.session.start(); speech(f.session, 'user', 'started');
  transcript(f.session, 'I have a Toyota Camry', true); await f.time.advance(0);
  const listening = f.output.cues[0]; speech(f.session, 'user', 'stopped'); await f.time.advance(200);
  speech(f.session, 'user', 'started'); transcript(f.session, 'I drive it around town', true);
  assert.equal(f.session.coordinator.utteranceId, 1); assert.equal(listening.stopped, false);
  assert.equal(f.session.partial, 'I have a Toyota Camry I drive it around town');
  speech(f.session, 'user', 'stopped'); await f.time.advance(160); speech(f.session, 'assistant', 'started');
  f.session.receive(pcm().buffer); speech(f.session, 'assistant', 'stopped');
  speech(f.session, 'user', 'started'); transcript(f.session, 'What time do you close', true);
  assert.equal(f.session.coordinator.utteranceId, 2); assert.equal(f.session.partial, 'What time do you close'); await f.session.stop();
});

test('a voice-failed completed provider turn and a long pending gap cannot retain old caller context', async () => {
  const f = fixture(); await f.session.start(); transcript(f.session, 'I have a Toyota Camry', true); await f.time.advance(160);
  speech(f.session, 'assistant', 'started'); speech(f.session, 'assistant', 'stopped');
  transcript(f.session, 'What time do you close', true); assert.equal(f.session.coordinator.utteranceId, 2);
  assert.equal(f.session.partial, 'What time do you close'); await f.time.advance(160 + 15001);
  transcript(f.session, 'What services are available', true); assert.equal(f.session.coordinator.utteranceId, 3);
  assert.equal(f.session.partial, 'What services are available'); await f.session.stop();
});

test('continuous input frames flow during a listening cue and main speech without any capture mute', async () => {
  const f = fixture(); await f.session.start();
  f.microphones[0].options.onSpeech(true); speech(f.session, 'user', 'started');
  transcript(f.session, 'I need an oil change for my car'); await f.time.advance(0);
  assert.equal(f.output.cues.length, 1); assert.equal(f.output.cues[0].phrase, 'neutral_ack');
  f.microphones[0].options.onFrame({ pcm: pcm() });
  assert.equal(f.session.metrics().input_frames_during_cue, 1); assert.equal(f.tracks[0].enabled, true);
  f.microphones[0].options.onSpeech(false); speech(f.session, 'user', 'stopped'); await f.time.advance(160);
  speech(f.session, 'assistant', 'started'); f.session.receive(pcm().buffer);
  f.microphones[0].options.onFrame({ pcm: pcm() });
  assert.equal(f.output.cues[0].stopped, false);
  assert.equal(f.session.metrics().input_frames_during_main, 1); assert.equal(f.tracks[0].enabled, true);
  assert.equal(f.sockets[0].sent.filter(value => value instanceof Uint8Array).length, 2);
  await f.session.stop(); assert.equal(f.output.closed, true);
});

test('answer latency measures turn release separately from the end of local microphone activity', async () => {
  const f = fixture(); await f.session.start();
  f.microphones[0].options.onSpeech(true); speech(f.session, 'user', 'started');
  await f.time.advance(100);
  f.microphones[0].options.onSpeech(false);
  const captureEndedAt = f.time.now();
  // The provider still owns an open caller turn after local capture activity.
  await f.time.advance(400); speech(f.session, 'user', 'stopped');
  await f.time.advance(160);
  const releasedAt = f.time.now();
  assert.equal(f.session.callerSpeaking, false);
  await f.time.advance(40); speech(f.session, 'assistant', 'started');
  f.session.receive(pcm(0).buffer);
  assert.deepEqual(f.session.metrics().first_voiced_main_after_turn_release_ms, []);
  await f.time.advance(300); f.session.receive(pcm(1000).buffer);
  assert.equal(f.time.now() - captureEndedAt, 900);
  assert.equal(f.time.now() - releasedAt, 340);
  assert.deepEqual(f.session.metrics().first_voiced_main_after_turn_release_ms, [340]);
  assert.equal('first_voiced_main_after_input_ms' in f.session.metrics(), false);
  await f.session.stop();
});

test('an interim finalized ASR clause stays listening-eligible while the caller keeps speaking', async () => {
  const f = fixture(); await f.session.start(); f.microphones[0].options.onSpeech(true);
  speech(f.session, 'user', 'started'); transcript(f.session, 'I need routine service for my vehicle', true);
  await f.time.advance(600); assert.equal(f.cues().length, 1); assert.equal(f.cues()[0].body.phase, 'listening');
  speech(f.session, 'user', 'stopped'); await f.time.advance(200); assert.equal(f.cues().length, 1, 'Local capture remains active');
  f.output.cues[0].finish(); await flush();
  f.microphones[0].options.onSpeech(false); await f.time.advance(160 + 550);
  assert.equal(f.cues().length, 2); assert.equal(f.cues()[1].body.phase, 'waiting');
  await f.session.stop();
});

test('assistant start before the queued user-stop timer does not lose the complete main response', async () => {
  const f = fixture(); await f.session.start(); speech(f.session, 'user', 'started');
  transcript(f.session, 'Please explain your regular service options', true); speech(f.session, 'user', 'stopped');
  speech(f.session, 'assistant', 'started'); f.session.receive(pcm(1800).buffer);
  assert.equal(f.session.callerSpeaking, false); assert.equal(f.output.main.length, 1);
  assert.equal(f.session.mainAllowed, true); await f.session.stop();
});

test('a final-only transcript creates a bounded waiting turn and never plays a listening cue', async () => {
  const f = fixture(); await f.session.start(); transcript(f.session, 'Please explain the service options', true);
  await f.time.advance(550);
  assert.equal(f.cues().length, 1); assert.equal(f.cues()[0].body.phase, 'waiting');
  assert.equal(f.cues()[0].body.epoch, 1); await f.session.stop();
});

test('a one-word name reply in details refreshes stale hard context and plays waiting before the next poll', async () => {
  let reads = 0;
  const f = fixture({ handler: request => {
    if (request.url !== '/api/sessions/fixture-session') return null;
    reads++;
    return reply({ ended: false, stage: reads === 1 ? 'confirmation' : 'details', proposal: { id: 'fixture-proposal' }, pending_operations: [] });
  } });
  await f.session.start(); await flush(); assert.equal(f.session.coordinator.sensitive, true);
  message(f.session, { type: 'transcript', role: 'assistant', transcript: 'What name should I put on the appointment?', transcriptType: 'final' });
  speech(f.session, 'user', 'started'); transcript(f.session, 'Alex'); await f.time.advance(100);
  assert.equal(f.cues().length, 0, 'A short name cannot play a listening cue');
  transcript(f.session, 'Alex', true); speech(f.session, 'user', 'stopped'); await f.time.advance(160);
  assert.ok(reads >= 2); assert.equal(f.session.coordinator.listeningBlocked, true);
  assert.equal(f.session.coordinator.sensitive, false); assert.equal(f.cues().length, 1);
  assert.equal(f.cues()[0].body.text, 'Alex'); assert.equal(f.cues()[0].body.phase, 'waiting');
  assert.equal(Object.hasOwn(f.cues()[0].body, 'previous_assistant'), false, 'Trusted assistant context belongs to the server');
  await f.time.advance(549); assert.equal(f.output.cues.length, 0);
  await f.time.advance(1); assert.equal(f.output.cues.length, 1); assert.equal(f.output.cues[0].phrase, 'moment');
  assert.ok(f.events.find(event => event.kind === 'cue_started').at_ms < 1200);
  await f.session.stop();
});

for (const text of ['Hours?', 'Opening hours']) {
  test(`short FAQ ${JSON.stringify(text)} can select waiting without a listening acknowledgment`, async () => {
    const f = fixture(); await f.session.start();
    message(f.session, { type: 'transcript', role: 'assistant', transcript: 'What would you like to know about our service department?', transcriptType: 'final' });
    speech(f.session, 'user', 'started'); transcript(f.session, text); await f.time.advance(100);
    assert.equal(f.cues().length, 0); transcript(f.session, text, true); speech(f.session, 'user', 'stopped');
    await f.time.advance(710); assert.equal(f.cues().length, 1);
    assert.equal(f.cues()[0].body.phase, 'waiting'); assert.equal(f.cues()[0].body.text, text);
    assert.equal(f.output.cues.length, 1); assert.equal(f.output.cues[0].phrase, 'moment'); await f.session.stop();
  });
}

test('details proposals suppress listening while retaining neutral waiting eligibility', async () => {
  const f = fixture({ handler: request => request.url === '/api/sessions/fixture-session'
    ? reply({ ended: false, stage: 'details', proposal: { id: 'fixture-proposal' }, pending_operations: [] }) : null });
  await f.session.start(); await flush(); speech(f.session, 'user', 'started');
  transcript(f.session, 'Please explain the available service options'); await f.time.advance(400);
  assert.equal(f.session.coordinator.listeningBlocked, true); assert.equal(f.cues().length, 0);
  transcript(f.session, 'Please explain the available service options', true); speech(f.session, 'user', 'stopped');
  await f.time.advance(710); assert.equal(f.cues().length, 1); assert.equal(f.cues()[0].body.phase, 'waiting');
  assert.equal(f.output.cues.length, 1); await f.session.stop();
});

test('an older snapshot response cannot reapply a hard block after the turn-yield refresh', async () => {
  const old = deferred(); let reads = 0;
  const f = fixture({ handler: request => {
    if (request.url !== '/api/sessions/fixture-session') return null;
    if (++reads === 1) return old.promise;
    return reply({ ended: false, stage: 'details', proposal: { id: 'fixture-proposal' }, pending_operations: [] });
  } });
  await f.session.start(); transcript(f.session, 'Alex', true); await f.time.advance(160);
  assert.equal(f.preparations().length, 1); assert.equal(f.session.coordinator.sensitive, false);
  old.resolve(reply({ ended: false, stage: 'confirmation', proposal: { id: 'fixture-proposal' }, pending_operations: [] }));
  await flush(); assert.equal(f.session.coordinator.sensitive, false);
  await f.time.advance(190); assert.equal(f.cues().length, 1); assert.equal(f.cues()[0].options.signal.aborted, false);
  await f.time.advance(360); assert.equal(f.output.cues.length, 1); await f.session.stop();
});

test('a raw one-word consent remains blocked even in otherwise eligible details context', async () => {
  const f = fixture({ handler: request => request.url === '/api/sessions/fixture-session'
    ? reply({ ended: false, stage: 'details', proposal: { id: 'fixture-proposal' }, pending_operations: [] }) : null });
  await f.session.start(); await flush();
  message(f.session, { type: 'transcript', role: 'assistant', transcript: 'Should I save those details?', transcriptType: 'final' });
  transcript(f.session, 'Yes', true); await f.time.advance(1500);
  assert.equal(f.session.coordinator.sensitive, true); assert.equal(f.cues().length, 0); assert.equal(f.output.cues.length, 0);
  await f.session.stop();
});

for (const [name, snapshot] of [
  ['confirmation', { stage: 'confirmation', proposal: { id: 'fixture-proposal' }, pending_operations: [] }],
  ['active operation', { stage: 'details', proposal: { id: 'fixture-proposal' }, pending_operations: ['fixture-operation'] }],
  ['ended session', { ended: true, stage: 'details', proposal: { id: 'fixture-proposal' }, pending_operations: [] }],
  ['proposal outside details', { stage: 'service', proposal: { id: 'fixture-proposal' }, pending_operations: [] }],
]) {
  test(`raw short transcript cannot clear the persisted ${name} safety gate`, async () => {
    const f = fixture({ handler: request => request.url === '/api/sessions/fixture-session' ? reply({ ended: false, ...snapshot }) : null });
    await f.session.start(); await flush(); transcript(f.session, 'Alex', true); await f.time.advance(1000);
    assert.equal(f.session.coordinator.sensitive, true); assert.equal(f.cues().length, 0); assert.equal(f.output.cues.length, 0);
    await f.session.stop();
  });
}

test('provider announcement and quiet PCM preserve a playing cue until voiced main samples arrive', async () => {
  const f = fixture(); await f.session.start(); speech(f.session, 'user', 'started');
  transcript(f.session, 'Please explain your regular service options'); await f.time.advance(0);
  speech(f.session, 'user', 'stopped'); await f.time.advance(160); speech(f.session, 'assistant', 'started');
  f.session.receive(pcm(0).buffer);
  assert.equal(f.output.cues[0].stopped, false); assert.equal(f.output.main.length, 0);
  assert.equal(f.session.mainSpeaking, false);
  f.session.receive(pcm(1000).buffer);
  assert.equal(f.output.cues[0].stopped, false); assert.equal(f.output.main.length, 1);
  await f.session.stop();
});

test('interruption without companion speech-start clears ownership and never replays buffered old PCM', async () => {
  const f = fixture(); await f.session.start(); speech(f.session, 'assistant', 'started'); f.session.receive(pcm(1200).buffer);
  message(f.session, { type: 'user-interrupted' }); const stops = f.output.stops;
  f.session.receive(pcm(1400).buffer); assert.equal(f.output.main.length, 1);
  transcript(f.session, 'Could you explain the opening hours', true); await f.time.advance(0);
  f.session.receive(pcm(1600).buffer); assert.equal(f.output.main.length, 1, 'Old output before fresh start is discarded');
  speech(f.session, 'assistant', 'started'); f.session.receive(pcm(1800).buffer);
  assert.equal(f.output.main.length, 2); assert.ok(stops > 0);
  assert.equal(new DataView(f.output.main[1].pcm.buffer).getInt16(0, true), 1800);
  await f.session.stop();
});

test('raw sensitive partial cancels an in-flight classifier before another selection can be coalesced', async () => {
  const held = deferred(); let classifierSignal;
  const f = fixture({ handler: request => {
    if (request.url.endsWith('/duplex/cue')) { classifierSignal = request.options.signal; return held.promise; }
  } });
  await f.session.start(); speech(f.session, 'user', 'started'); transcript(f.session, 'Please explain your regular service options');
  await f.time.advance(0); assert.ok(classifierSignal);
  transcript(f.session, 'Actually cancel the previous request');
  assert.equal(classifierSignal.aborted, true);
  held.resolve(reply({ phrase_id: 'neutral_ack', confidence: .99, language_choice: 'cache_compatible', expires_in_ms: 800 }));
  await flush(); assert.equal(f.output.cues.length, 0); await f.session.stop();
});

test('stable utterance identity and monotonic request revision survive several partials and final waiting phase', async () => {
  const f = fixture({ handler: request => request.url.endsWith('/duplex/cue') ? reply({ phrase_id: 'silence', confidence: 0 }) : null });
  await f.session.start(); speech(f.session, 'user', 'started'); transcript(f.session, 'Please explain the service options'); await f.time.advance(0);
  transcript(f.session, 'Please explain the service options and typical process'); await f.time.advance(350);
  transcript(f.session, 'Please explain the service options and typical process', true); speech(f.session, 'user', 'stopped'); await f.time.advance(710);
  assert.equal(f.cues().length, 3);
  assert.deepEqual(f.cues().map(row => row.body.epoch), [1, 1, 1]);
  assert.deepEqual(f.cues().map(row => row.body.revision), [1, 2, 3]);
  assert.deepEqual(f.cues().map(row => row.body.phase), ['listening', 'listening', 'waiting']);
  await f.session.stop();
});

test('a long caller turn receives at most one cue per listening and waiting phase across revisions', async () => {
  const f = fixture(); await f.session.start(); speech(f.session, 'user', 'started');
  transcript(f.session, 'Please explain the service options'); await f.time.advance(0);
  await f.time.advance(16000); transcript(f.session, 'Please also tell me more about the same issue'); await f.time.advance(0);
  assert.equal(f.cues().length, 1, 'Partial revisions cannot create a second listening cue');
  transcript(f.session, 'Please also tell me more about the same issue', true); speech(f.session, 'user', 'stopped'); await f.time.advance(710);
  assert.equal(f.cues().length, 2); assert.equal(f.output.cues.length, 2);
  assert.deepEqual(f.cues().map(row => row.body.phase), ['listening', 'waiting']);
  assert.deepEqual(f.cues().map(row => row.body.epoch), [1, 1]);
  await f.time.advance(300);
  transcript(f.session, 'Please also tell me more about the same issue and timing', true); await f.time.advance(710);
  assert.equal(f.cues().length, 2, 'A revised final cannot create a second waiting cue');
  assert.equal(f.output.cues.length, 2); await f.session.stop();
});

test('stop during AudioContext resume never prompts for microphone or creates a session', async () => {
  const resume = deferred(), f = fixture({ resume }); const starting = f.session.start(); await flush();
  await f.session.stop(); resume.resolve(); await starting;
  assert.equal(f.mediaRequests, 0); assert.equal(f.requests.length, 0); assert.equal(f.sockets.length, 0);
});

test('microphone permission arriving after stop releases the returned hardware without allocating a call', async () => {
  const permission = deferred(), f = fixture({ getUserMedia: permission }); const starting = f.session.start(); await flush();
  await f.session.stop(); permission.resolve(f.stream); await starting;
  assert.ok(f.tracks[0].stopped > 0); assert.equal(f.requests.length, 0); assert.equal(f.sockets.length, 0);
});

test('a late-created local session is ended once and never proceeds to cue bank or paid allocation', async () => {
  const held = deferred(), f = fixture({ handler: request => request.url === '/api/sessions' ? held.promise : null });
  const starting = f.session.start(); await flush(); await f.session.stop();
  held.resolve(reply({ id: 'fixture-session', token: 'fixture-token', mode: 'live' })); await starting;
  assert.equal(f.requests.filter(row => row.url.endsWith('/end')).length, 1);
  assert.equal(f.requests.some(row => row.url.endsWith('/duplex/call')), false);
  assert.equal(f.sockets.length, 0);
});

test('a paid call returned after Stop is terminated through the scoped stop endpoint without a media connection', async () => {
  const held = deferred(), f = fixture({ handler: request => request.url.endsWith('/duplex/call') ? held.promise : null });
  const starting = f.session.start();
  for (let i = 0; i < 30 && !f.requests.some(row => row.url.endsWith('/duplex/call')); i++) { await new Promise(resolve => setImmediate(resolve)); }
  assert.equal(f.requests.some(row => row.url.endsWith('/duplex/call')), true);
  await f.session.stop();
  held.resolve(reply({ call_id: 'fixture-call', websocket_url: 'wss://backend.vapi.ai/fixture', sample_rate: 16000 })); await starting;
  assert.equal(f.requests.filter(row => row.url.endsWith('/duplex/stop')).length, 1);
  assert.equal(f.requests.filter(row => row.url.endsWith('/end')).length, 1);
  assert.equal(f.sockets.length, 0); assert.equal(f.session.active, false);
});

test('Stop cancels an unfinished WebSocket handshake immediately and clears its deadline', async () => {
  const f = fixture({ autoOpen: false }), starting = f.session.start();
  let timer;
  try {
    await Promise.race([f.firstSocket, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error('fixture_socket_creation_deadline')), 5000);
    })]);
  } finally { clearTimeout(timer); }
  assert.equal(f.sockets.length, 1); await f.session.stop(); await starting;
  assert.equal(f.sockets[0].readyState, 3); assert.equal(f.time.pending, 0);
  assert.equal(f.requests.filter(row => row.url.endsWith('/duplex/stop')).length, 1);
});

test('failed control URL cleanup uses only a short owned termination handshake and never starts media', async () => {
  const f = fixture({ handler: request => request.url.endsWith('/duplex/stop') ? reply({ ok: false }) : null });
  await f.session.start(); await f.session.stop();
  assert.equal(f.sockets.length, 2);
  assert.deepEqual(f.sockets[1].sent.map(value => JSON.parse(value)), [{ type: 'end-call' }]);
  assert.equal(f.sockets[1].readyState, 3); assert.equal(f.time.pending, 0);
});

test('a fresh local session cannot send frames through a stopped previous session', async () => {
  const first = fixture(); await first.session.start(); await first.session.stop();
  const second = fixture(); await second.session.start();
  first.microphones[0].options.onFrame({ pcm: pcm() }); second.microphones[0].options.onFrame({ pcm: pcm() });
  assert.equal(first.session.inputFrames, 0); assert.equal(second.session.inputFrames, 1);
  assert.equal(first.contexts[0].state, 'closed'); assert.equal(second.contexts[0].state, 'running');
  await second.session.stop();
});

test('malformed JSON values and explicitly older provider user turns cannot interrupt the current response', async () => {
  const f = fixture(); await f.session.start();
  for (const value of ['null', '[]', '12', '{invalid']) assert.doesNotThrow(() => f.session.receive(value));
  message(f.session, { type: 'transcript', role: 'user', turn: 3, transcriptType: 'final', transcript: 'Explain the available service options' });
  await f.time.advance(0); speech(f.session, 'assistant', 'started'); f.session.receive(pcm().buffer);
  const generation = f.session.generation, stops = f.output.stops;
  message(f.session, { type: 'transcript', role: 'user', turn: 2, transcriptType: 'final', transcript: 'This is an older completed utterance' });
  message(f.session, { type: 'speech-update', role: 'user', turn: 2, status: 'started' });
  assert.equal(f.session.generation, generation); assert.equal(f.output.stops, stops);
  assert.equal(f.session.callerSpeaking, false);
  assert.equal(f.events.filter(event => event.kind === 'stale_provider_event_discarded').length, 2);
  await f.session.stop();
});

test('false local-energy onset pauses immediately and resumes the same answer when short noise ends', async () => {
  const f = fixture(); await f.session.start(); speech(f.session, 'assistant', 'started'); f.session.receive(pcm(1000).buffer);
  const generation = f.session.generation, before = f.session.audioBytes;
  f.microphones[0].options.onSpeech(true);
  assert.equal(f.output.paused, true); assert.equal(f.session.callerSpeaking, false);
  assert.equal(f.session.generation, generation); assert.equal(f.session.metrics().provisional_interruption, true);
  f.session.receive(pcm(2000).buffer); assert.equal(f.output.main.length, 1);
  f.microphones[0].options.onFrame({ pcm: pcm() }); assert.equal(f.session.inputFrames, 1);
  await f.time.advance(80); f.microphones[0].options.onSpeech(false);
  assert.equal(f.output.paused, false); assert.equal(f.output.resumes, 1);
  assert.equal(f.session.generation, generation); assert.equal(f.output.main.length, 2);
  assert.equal(f.session.audioBytes, before + 640, 'Buffered bytes are not counted twice during recovery');
  assert.ok(f.events.some(event => event.kind === 'provisional_resumed' && event.reason === 'energy_ended'));
  assert.equal(f.tracks[0].enabled, true); await f.session.stop();
});

test('real provider speech corroborates provisional interruption and destroys buffered old-answer audio', async () => {
  const f = fixture(); await f.session.start(); speech(f.session, 'assistant', 'started'); f.session.receive(pcm().buffer);
  f.microphones[0].options.onSpeech(true); f.session.receive(pcm(2000).buffer);
  speech(f.session, 'user', 'started');
  assert.equal(f.session.provisional, null); assert.equal(f.session.callerSpeaking, true);
  assert.equal(f.session.generation, null); assert.equal(f.output.paused, false);
  assert.equal(f.output.resumes, 0); assert.equal(f.output.main.length, 1);
  assert.equal(f.session.discardedOutputBytes, 640);
  assert.ok(f.events.some(event => event.kind === 'provisional_confirmed'));
  await f.time.advance(400); assert.equal(f.output.resumes, 0); await f.session.stop();
});

test('sustained background energy gets only one extension and recovers at the fixed800ms ceiling', async () => {
  const f = fixture(); await f.session.start(); speech(f.session, 'assistant', 'started'); f.session.receive(pcm().buffer);
  const generation = f.session.generation; f.microphones[0].options.onSpeech(true);
  for (let count = 0; count < 17; count++) { f.session.receive(pcm(1800).buffer); await f.time.advance(20); }
  assert.equal(f.output.main.length, 1); await f.time.advance(10);
  assert.ok(f.session.provisional); assert.equal(f.output.resumes, 0);
  assert.equal(f.events.filter(event => event.kind === 'provisional_extended').length, 1);
  await f.time.advance(449); assert.equal(f.output.resumes, 0); await f.time.advance(1);
  assert.equal(f.session.provisional, null); assert.equal(f.output.resumes, 1);
  assert.equal(f.output.main.length, 18); assert.equal(f.session.generation, generation);
  assert.equal(f.session.localSpeaking, true); assert.equal(f.session.callerSpeaking, false);
  assert.ok(f.events.some(event => event.kind === 'provisional_resumed' && event.reason === 'confirmation_timeout'));
  f.session.receive(pcm(1900).buffer); assert.equal(f.output.main.length, 19); await f.session.stop();
});

test('provider confirmation529ms after local pause destroys the old queue without any intermediate resume', async () => {
  const f = fixture(); await f.session.start(); speech(f.session, 'assistant', 'started'); f.session.receive(pcm().buffer);
  f.session.onLocalSpeech(true);
  for (let i = 0; i < 26; i++) { f.session.receive(pcm(1800).buffer); await f.time.advance(20); }
  await f.time.advance(9); message(f.session, { type: 'user-interrupted' });
  assert.equal(f.session.provisional, null); assert.equal(f.output.resumes, 0); assert.equal(f.output.main.length, 1);
  assert.ok(f.events.some(row => row.kind === 'provisional_confirmed' && row.source === 'provider_interruption_event'));
  f.session.receive(pcm(1900).buffer); await f.time.advance(1000); assert.equal(f.output.main.length, 1);
  assert.equal(f.session.mainAllowed, false); await f.session.stop();
});

test('confirmation after the800ms noise ceiling still revokes current main and never replays its saved suffix', async () => {
  const f = fixture(); await f.session.start(); speech(f.session, 'assistant', 'started'); f.session.receive(pcm().buffer);
  f.session.onLocalSpeech(true); f.session.receive(pcm(1800).buffer); await f.time.advance(850);
  assert.equal(f.output.resumes, 1); assert.equal(f.output.main.length, 2);
  message(f.session, { type: 'user-interrupted' }); const accepted = f.output.main.length;
  f.session.receive(pcm(1900).buffer); await f.time.advance(1000);
  assert.equal(f.output.main.length, accepted); assert.equal(f.session.generation, null);
  assert.equal(f.session.ordinaryTurnDrained, false); await f.session.stop();
});

test('provisional output buffer is bounded and Stop removes its timer without recovery', async () => {
  const f = fixture(); await f.session.start(); speech(f.session, 'assistant', 'started'); f.session.receive(pcm().buffer);
  f.microphones[0].options.onSpeech(true);
  for (let count = 0; count < 50; count++) f.session.receive(pcm().buffer);
  assert.equal(f.session.provisional.bytes, 25600); assert.ok(f.session.discardedOutputBytes > 0);
  await f.session.stop(); await f.time.advance(400);
  assert.equal(f.session.provisional, null); assert.equal(f.output.resumes, 0); assert.equal(f.time.pending, 0);
});

test('real adapter survives20ms ordinary ASR extensions and plays a listening cue before final speech', async () => {
  let f;
  f = fixture({ handler: request => {
    if (!request.url.endsWith('/duplex/cue')) return null;
    return new Promise(resolve => f.time.setTimeout(() => resolve(reply({
      phrase_id: request.body.text.split(/\s+/).length >= 6 ? 'neutral_ack' : 'silence',
      confidence: .99, language_choice: 'cache_compatible', expires_in_ms: 800,
    })), 180));
  } });
  await f.session.start(); speech(f.session, 'user', 'started');
  const text = 'I would like routine service for my vehicle and I can describe the issue in more detail';
  for (let length = 1; length <= text.length; length++) { transcript(f.session, text.slice(0, length)); await f.time.advance(20); }
  assert.equal(f.output.cues.length, 1); assert.equal(f.session.callerSpeaking, true);
  assert.ok(f.cues().length <= 5); assert.ok(f.cues().every(row => !row.options.signal.aborted));
  assert.deepEqual([...new Set(f.cues().map(row => row.body.epoch))], [1]);
  assert.equal(f.session.coordinator.decisionDeadlineMs, 850); await f.session.stop();
});

for (const phrase of ['not ready', "don't book it", 'never do that', 'only if it is available', 'unless I confirm',
  'please reply in Spanish', 'y tambiÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â©n el motor']) {
  test(`append-compatible sensitive text immediately revokes an earlier snapshot: ${phrase}`, async () => {
    const held = deferred(); let signal;
    const f = fixture({ handler: request => {
      if (!request.url.endsWith('/duplex/cue')) return null;
      signal = request.options.signal; return held.promise;
    } });
    await f.session.start(); speech(f.session, 'user', 'started');
    transcript(f.session, 'I need routine service for my vehicle'); await f.time.advance(0);
    assert.ok(signal && !signal.aborted);
    transcript(f.session, `I need routine service for my vehicle ${phrase}`);
    assert.equal(signal.aborted, true);
    held.resolve(reply({ phrase_id: 'neutral_ack', confidence: .99, language_choice: 'cache_compatible', expires_in_ms: 800 }));
    await flush(); assert.equal(f.output.cues.length, 0); await f.session.stop();
  });
}

test('assistant stopped cancels a pending waiting cue even when its start or audio was unavailable', async () => {
  const f = fixture(); await f.session.start(); transcript(f.session, 'Please explain the available service options', true);
  await f.time.advance(350); assert.equal(f.session.coordinator.generating, true);
  speech(f.session, 'assistant', 'stopped'); await f.time.advance(1000);
  assert.equal(f.session.coordinator.generating, false); assert.equal(f.cues().length, 1);
  assert.equal(f.cues()[0].options.signal.aborted, true, 'Stop revokes the overlapped waiting request before audio');
  assert.equal(f.output.cues.length, 0); await f.session.stop();
});

test('assistant stopped preserves the announced generation so its late final PCM can finish', async () => {
  const f = fixture(); await f.session.start(); speech(f.session, 'assistant', 'started'); f.session.receive(pcm(1000).buffer);
  const generation = f.session.generation;
  speech(f.session, 'assistant', 'stopped'); f.session.receive(pcm(1500).buffer);
  assert.equal(f.session.generation, generation); assert.equal(f.output.main.length, 2);
  assert.equal(f.output.main[1].generationId, generation); assert.equal(f.session.coordinator.generating, false);
  await f.session.stop();
});

test('unknown user speech statuses cannot end a caller turn or advance provider turn identity', async () => {
  const f = fixture(); await f.session.start();
  message(f.session, { type: 'speech-update', role: 'user', status: 'started', turn: 2 });
  transcript(f.session, 'Please explain your regular service options');
  const generation = f.session.generation;
  message(f.session, { type: 'speech-update', role: 'user', status: 'buffering', turn: 99 });
  await f.time.advance(200);
  assert.equal(f.session.providerSpeaking, true); assert.equal(f.session.callerSpeaking, true);
  assert.equal(f.session.generation, generation); assert.equal(f.session.lastProviderUserTurn, 2);
  message(f.session, { type: 'speech-update', role: 'user', status: 'stopped', turn: 2 });
  await f.time.advance(160); assert.equal(f.session.callerSpeaking, false); await f.session.stop();
});


for (const text of ['Actually I meant a tire rotation', 'No, I need Friday afternoon instead', 'Change the vehicle model to a Camry', 'That is not the right service']) {
  test(`a correction can request a fresh neutral waiting cue while listening remains blocked: ${text}`, async () => {
    const f = fixture(); await f.session.start(); speech(f.session, 'user', 'started');
    transcript(f.session, 'Opening hours'); await f.time.advance(250);
    const old = f.session.waitingPrepared; assert.ok(old);
    transcript(f.session, text); assert.equal(f.session.waitingPrepared, null);
    assert.equal(f.session.coordinator.listeningBlocked, true); assert.equal(f.session.coordinator.sensitive, false);
    await f.time.advance(0); assert.equal(f.cues().length, 0);
    transcript(f.session, text, true); speech(f.session, 'user', 'stopped'); await f.time.advance(1200);
    const current = f.cues().find(row => row.body.phase === 'waiting'); assert.ok(current);
    assert.equal(current.body.text, text); assert.notEqual(current.body.prepared_revision, old.revision);
    assert.equal(f.cues().some(row => row.body.phase === 'listening'), false);
    assert.equal(f.output.cues.length, 1); assert.equal(f.output.cues[0].phrase, 'moment'); await f.session.stop();
  });
}

test('an appended correction cancels a running listening selection even when the old prefix still matches', async () => {
  const held = deferred(); const f = fixture({ handler: request => request.url.endsWith('/duplex/cue') && request.body.phase === 'listening' ? held.promise : null });
  await f.session.start(); speech(f.session, 'user', 'started'); transcript(f.session, 'I need a tire rotation for my car');
  await f.time.advance(0); const request = f.cues()[0];
  transcript(f.session, 'I need a tire rotation for my car actually I meant an inspection');
  assert.equal(request.options.signal.aborted, true);
  held.resolve(reply({ phrase_id: 'neutral_ack', confidence: .99, language_choice: 'cache_compatible', expires_in_ms: 800 }));
  await flush(); assert.equal(f.output.cues.length, 0); assert.equal(f.session.coordinator.listeningBlocked, true); await f.session.stop();
});

test('a trailing changed correction immediately stops stale waiting audio before fresh classification', async () => {
  const f = fixture(); await f.session.start(); transcript(f.session, 'Opening hours', true); await f.time.advance(710);
  const old = f.output.cues[0]; assert.ok(old);
  transcript(f.session, 'Actually service prices instead', true);
  assert.equal(old.stopped, true); assert.equal(f.session.coordinator.sensitive, false);
  assert.equal(f.session.coordinator.listeningBlocked, true); await f.session.stop();
});

for (const text of ['No', 'Okay', 'Please stop and change that', 'Actually cancel it', 'Only if the price is low', 'Please hang up', 'My address is 123 Main Street']) {
  test(`relaxed correction waiting policy retains hard controls and private data: ${text}`, async () => {
    const f = fixture(); await f.session.start(); transcript(f.session, text, true); await f.time.advance(2500);
    assert.equal(f.output.cues.length, 0); assert.equal(f.cues().length, 0); assert.equal(f.preparations().length, 0); await f.session.stop();
  });
}

test('follow-up HTTP is a fresh revision with follow_up true and can never reuse a prepared receipt', async () => {
  const f = fixture({ handler: request => request.url.endsWith('/duplex/cue') && request.body.follow_up
    ? reply({ phrase_id: 'one_sec', confidence: .99, language_choice: 'cache_compatible', expires_in_ms: 800, max_request_age_ms: 800 }) : null });
  await f.session.start(); transcript(f.session, 'Opening hours', true); await f.time.advance(710);
  const first = f.cues()[0]; assert.equal(f.output.cues.length, 1);
  await f.time.advance(1050); f.output.cues[0].finish(); await flush();
  await f.time.advance(600); const next = f.cues().at(-1);
  assert.equal(next.body.follow_up, true); assert.equal(next.body.epoch, first.body.epoch);
  assert.ok(next.body.revision > first.body.revision); assert.equal(Object.hasOwn(next.body, 'prepared_revision'), false);
  assert.equal(Object.hasOwn(next.body, 'prepare_only'), false);
  await f.time.advance(650); assert.equal(f.output.cues[1].phrase, 'one_sec');
  f.output.cues[1].finish(); await flush(); await f.time.advance(10000);
  assert.equal(f.cues().length, 2); assert.equal(f.output.cues.length, 2); await f.session.stop();
});

test('new main PCM before a queued follow-up prevents even its HTTP classification', async () => {
  const f = fixture(); await f.session.start(); transcript(f.session, 'Opening hours', true); await f.time.advance(710);
  await f.time.advance(1050); f.output.cues[0].finish(); await flush(); await f.time.advance(599);
  speech(f.session, 'assistant', 'started'); f.session.receive(pcm(2000).buffer); await f.time.advance(5000);
  assert.equal(f.cues().length, 1); assert.equal(f.output.cues.length, 1); await f.session.stop();
});

test('muting during a main handoff clears queued output and invalidates the old generation', async () => {
  const f = fixture(); await f.session.start(); transcript(f.session, 'Opening hours', true); await f.time.advance(710);
  speech(f.session, 'assistant', 'started'); f.session.receive(pcm(2000).buffer);
  const old = f.session.generation, stops = f.output.stops; f.session.setMuted(true);
  assert.equal(f.output.cues[0].stopped, true); assert.ok(f.output.stops > stops);
  assert.equal(f.session.coordinator.receiveMainPcm(old, pcm(), 16000), false);
  f.session.receive(pcm(3000).buffer); assert.equal(f.output.main.length, 1); await f.session.stop();
});

for (const phrase of ['one_sec', 'just_a_sec', 'let_me_see', 'checking', 'taking_a_look', 'working_on_it']) {
  test(`a server-selected approved waiting ID reaches the owned output unchanged: ${phrase}`, async () => {
    const f = fixture({ handler: request => request.url.endsWith('/duplex/cue') && !request.body.prepare_only
      ? reply({ phrase_id: phrase, confidence: .99, language_choice: 'cache_compatible', expires_in_ms: 800 }) : null });
    await f.session.start(); transcript(f.session, 'Opening hours', true); await f.time.advance(710);
    assert.equal(f.output.cues[0].phrase, phrase);
    assert.equal(f.requests.find(row => row.body?.event === 'cue_started').body.phrase_id, phrase); await f.session.stop();
  });
}

test('an overlong bank asset is rejected before allocating a paid call or output', async () => {
  const long = new Uint8Array(35202), hash = createHash('sha256').update(long).digest('hex');
  const f = fixture({ handler: request => request.url === '/api/duplex/config'
    ? reply({ enabled: true, sample_rate: 16000, phrases: [{ phrase_id: 'moment', sample_rate: 16000, pcm_bytes: long.byteLength, sha256: hash }] })
    : request.url.includes('/duplex/audio/') ? { ok: true, arrayBuffer: async () => long.buffer } : null });
  await assert.rejects(f.session.start(), /cue_audio_geometry/); assert.equal(f.session.active, false); assert.equal(f.output.cues.length, 0);
  assert.equal(f.requests.some(row => row.url.endsWith('/duplex/call')), false); await f.session.stop();
});


test('fresh accepted-work snapshot hint enables waiting only and cannot authorize consent', async () => {
  const f = fixture({ handler: request => request.url === '/api/sessions/fixture-session'
    ? reply({ ended: false, stage: 'details', proposal: null, pending_operations: [{ status: 'running' }], accepted_work_pending: true }) : null });
  await f.session.start(); await flush(); speech(f.session, 'user', 'started'); transcript(f.session, 'The vehicle is a blue sedan');
  await f.time.advance(400); assert.equal(f.session.coordinator.listeningBlocked, true); assert.equal(f.cues().length, 0);
  transcript(f.session, 'The vehicle is a blue sedan', true); speech(f.session, 'user', 'stopped'); await f.time.advance(710);
  assert.equal(f.output.cues.length, 1); assert.equal(f.cues()[0].body.phase, 'waiting');
  speech(f.session, 'user', 'started'); transcript(f.session, 'Yes confirm it', true);
  await f.time.advance(2500); assert.equal(f.output.cues.length, 1); assert.equal(f.session.coordinator.sensitive, true); await f.session.stop();
});

function progressSnapshot(text = 'Book it.') {
  const turn = { turn_key: 'a'.repeat(64), caller_text_sha256: createHash('sha256').update(text.trim()).digest('hex'),
    generation_id: '12345678-1234-4234-8234-123456789abc', user_count: 3 };
  const progress = { source: 'canonical_queued_operation', kind: 'booking_commit', operation_id: 'op_' + 'b'.repeat(24),
    status: 'running', turn_key: turn.turn_key, caller_text_sha256: turn.caller_text_sha256, generation_id: turn.generation_id };
  return { ended: false, stage: 'confirmation', proposal: { kind: 'book' },
    pending_operations: [{ id: progress.operation_id, status: 'running' }], duplex_canonical_turn: turn,
    accepted_operation_progress: progress };
}

async function progressFixture(text = 'Book it.', extraHandler = null) {
  const state = { snapshot: { ended: false, stage: 'confirmation', proposal: { kind: 'book' }, pending_operations: [] } };
  const f = fixture({ handler: request => {
    if (extraHandler) { const result = extraHandler(request); if (result) return result; }
    if (request.url === '/api/sessions/fixture-session') return reply(structuredClone(state.snapshot));
    if (request.url === '/api/duplex/config') return reply({ enabled: true, sample_rate: 16000,
      phrases: ['neutral_ack', 'moment', 'working_on_it'].map(phrase_id => ({ phrase_id, sample_rate: 16000,
        pcm_bytes: pcm().byteLength, sha256: createHash('sha256').update(pcm()).digest('hex') })) });
    if (request.url.endsWith('/duplex/cue')) return reply({ phrase_id: 'working_on_it', confidence: .99,
      language_choice: 'cache_compatible', expires_in_ms: 800, requests: 1 });
    return null;
  } });
  await f.session.start(); await flush(); transcript(f.session, text, true); await f.time.advance(160);
  state.snapshot = progressSnapshot(text); return { ...f, state };
}

test('exact fresh accepted booking progress allows only one work cue after yield with no prediction', async () => {
  const f = await progressFixture(); await f.session.refreshSnapshot(); await f.time.advance(710);
  assert.equal(f.output.cues.length, 1); assert.equal(f.output.cues[0].phrase, 'working_on_it');
  assert.equal(f.cues()[0].body.phase, 'waiting'); assert.equal(f.preparations().length, 0);
  assert.equal(f.cues()[0].body.prepared_revision, undefined); assert.equal(f.session.coordinator.listeningBlocked, true);
  f.output.cues[0].finish(); await flush(); await f.time.advance(1800);
  assert.equal(f.cues().length, 1); assert.equal(f.output.cues.length, 1); await f.session.stop();
});

for (const mutation of ['hash', 'generation', 'pending_id', 'unknown', 'no_final', 'caller', 'main', 'revoked']) {
  test(`accepted progress cannot bypass a mismatched or obsolete ${mutation} binding`, async () => {
    const f = await progressFixture(); const value = f.state.snapshot;
    if (mutation === 'hash') value.accepted_operation_progress.caller_text_sha256 = 'c'.repeat(64);
    if (mutation === 'generation') value.accepted_operation_progress.generation_id = '22345678-1234-4234-8234-123456789abc';
    if (mutation === 'pending_id') value.pending_operations[0].id = 'op_' + 'c'.repeat(24);
    if (mutation === 'unknown') value.pending_operations[0].status = 'unknown';
    if (mutation === 'no_final') f.session.lastUserTranscriptFinal = false;
    if (mutation === 'caller') f.session.localSpeaking = true;
    if (mutation === 'main') { speech(f.session, 'assistant', 'started'); f.session.receive(pcm().buffer); }
    if (mutation === 'revoked') f.session.revokedProgressTurnKeys.add(value.duplex_canonical_turn.turn_key);
    await f.session.refreshSnapshot(); await f.time.advance(1200);
    assert.equal(f.output.cues.length, 0); assert.equal(f.session.snapshotHardBlocked, true); await f.session.stop();
  });
}

for (const text of ['Yes.', 'Stop.', 'Cancel it.', 'Actually book another day.', 'Book it only if the price is lower.', 'Book it and speak in Spanish.']) {
  test(`accepted progress retains the raw text restriction for ${text}`, async () => {
    const f = await progressFixture(text); await f.session.refreshSnapshot(); await f.time.advance(1200);
    assert.equal(f.cues().length, 0); assert.equal(f.output.cues.length, 0); await f.session.stop();
  });
}

test('async final-text digest cannot restore progress after a new caller event', async () => {
  const f = await progressFixture(), held = deferred(); f.session.runtime.textDigest = () => held.promise;
  const refreshing = f.session.refreshSnapshot(); await flush(); speech(f.session, 'user', 'started');
  held.resolve(f.state.snapshot.accepted_operation_progress.caller_text_sha256); await refreshing;
  assert.equal(f.session.acceptedOperationProgress, null); assert.equal(f.output.cues.length, 0);
  await f.session.stop();
});

test('an in-flight old snapshot cannot bind to a resumed same-words caller turn before any canonical key was observed', async () => {
  const f = await progressFixture(), held = deferred(), priorRequest = f.session.request.bind(f.session);
  f.session.request = (path, ...args) => path === '' ? held.promise : priorRequest(path, ...args);
  const refreshing = f.session.refreshSnapshot(); await flush();
  assert.equal(f.session.observedCanonicalTurnKey, null);
  // Isolate the binding fence from the independent newer-read sequence fence.
  // Later refresh attempts are deliberately unavailable in this fixture.
  f.session.refreshSnapshot = async () => {};
  speech(f.session, 'user', 'started'); transcript(f.session, 'Book it.', true);
  speech(f.session, 'user', 'stopped'); await f.time.advance(160);
  held.resolve(f.state.snapshot); await refreshing;
  assert.equal(f.session.acceptedOperationProgress, null); assert.equal(f.session.observedCanonicalTurnKey, null);
  assert.equal(f.session.snapshotHardBlocked, true);
  await f.session.stop();
});

test('same-frame main observer cannot erase ownership of an already-started progress clip', async () => {
  const f = await progressFixture(), prior = f.session.callbacks.event;
  f.session.callbacks.event = row => {
    prior(row);
    if (row.kind === 'cue_started' && row.phrase_id === 'working_on_it') {
      speech(f.session, 'assistant', 'started'); f.session.receive(pcm().buffer);
    }
  };
  await f.session.refreshSnapshot(); await f.time.advance(710);
  assert.equal(f.session.progressCuePlaying, true); assert.equal(f.session.acceptedOperationProgress, null);
  f.state.snapshot = { ended: false, stage: 'complete', proposal: null, pending_operations: [] };
  await f.session.refreshSnapshot(); assert.equal(f.output.cues[0].stopped, false);
  f.output.cues[0].finish(); await flush(); assert.equal(f.session.progressCuePlaying, false);
  await f.session.stop();
});

test('caller lifecycle sends only an observed canonical key and the same key cannot enable a new epoch', async () => {
  const f = await progressFixture();
  const initial = f.requests.filter(row => row.url.endsWith('/duplex/event') && row.body.event === 'caller_started');
  assert.ok(initial.length); assert.ok(initial.every(row => row.body.progress_turn_key === undefined));
  await f.session.refreshSnapshot(); speech(f.session, 'user', 'started'); transcript(f.session, 'Book it.', true);
  const latest = f.requests.filter(row => row.url.endsWith('/duplex/event') && ['caller_started', 'caller_resumed'].includes(row.body.event)).at(-1);
  assert.equal(latest.body.progress_turn_key, f.state.snapshot.duplex_canonical_turn.turn_key);
  speech(f.session, 'user', 'stopped'); await f.time.advance(160); await f.session.refreshSnapshot();
  assert.equal(f.session.acceptedOperationProgress, null); await f.time.advance(1000); assert.equal(f.output.cues.length, 0);
  await f.session.stop();
});

test('operation completion and main arrival preserve only the already-started complete progress phrase', async () => {
  const f = await progressFixture(); await f.session.refreshSnapshot(); await f.time.advance(710);
  const clip = f.output.cues[0]; assert.ok(clip);
  f.state.snapshot = { ended: false, stage: 'complete', proposal: null, pending_operations: [] };
  f.session.onLocalSpeech(true); // Unconfirmed noise still cannot cut a cue-only phrase.
  await f.session.refreshSnapshot(); assert.equal(clip.stopped, false); assert.equal(f.session.snapshotHardBlocked, true);
  f.session.onLocalSpeech(false);
  transcript(f.session, 'Book it.', true); assert.equal(clip.stopped, false, 'Known endpoint tail cannot cut the started clip');
  speech(f.session, 'assistant', 'started'); f.session.receive(pcm().buffer);
  await f.session.refreshSnapshot(); assert.equal(clip.stopped, false);
  clip.finish(); await flush(); assert.equal(f.session.progressCuePlaying, false); assert.equal(f.session.coordinator.sensitive, true);
  await f.time.advance(2000); assert.equal(f.cues().length, 1); await f.session.stop();
});

for (const change of ['Actually book another day.', 'Stop.', 'Book it for a different vehicle.']) {
  test(`started progress audio immediately cancels on revised or sensitive text: ${change}`, async () => {
    const f = await progressFixture(); await f.session.refreshSnapshot(); await f.time.advance(710);
    const clip = f.output.cues[0]; transcript(f.session, change, true);
    assert.equal(clip.stopped, true); assert.equal(f.session.acceptedOperationProgress, null);
    await f.session.stop();
  });
}


for (const audible of [false, true]) {
  test(`caller resume during waiting ${audible ? 'voiced audio' : 'leading padding'} has the correct turn identity and keeps prior facts`, async () => {
    const f = fixture(); await f.session.start(); transcript(f.session, 'The vehicle is a blue sedan', true); await f.time.advance(710);
    f.output.cues[0].hasAudiblyStarted = () => audible;
    const oldEpoch = f.session.coordinator.utteranceId, oldText = f.session.partial;
    speech(f.session, 'user', 'started'); transcript(f.session, 'Actually the vehicle is a red sedan instead', true);
    assert.equal(f.session.coordinator.utteranceId, oldEpoch + 1);
    assert.ok(f.session.partial.includes(oldText)); assert.ok(f.session.partial.includes('red sedan'));
    assert.equal(f.output.cues[0].stopped, true);
    speech(f.session, 'user', 'stopped'); await f.time.advance(1500);
    assert.equal(f.output.cues.length, 2); assert.equal(f.cues().at(-1).body.epoch, oldEpoch + 1);
    assert.equal(f.cues().at(-1).body.follow_up, undefined, 'Fresh caller turn is not a continuation hold');
    assert.equal(f.session.coordinator.cueTimes.length, 2, 'Neither padding nor voiced cancellation refunds rolling spend');
    await f.session.stop();
  });
}

test('caller after voiced waiting with main queued behind it retains facts and revokes the old generation', async () => {
  const f = fixture(); await f.session.start(); transcript(f.session, 'The vehicle is a blue sedan', true); await f.time.advance(710);
  f.output.cues[0].hasAudiblyStarted = () => true;
  speech(f.session, 'assistant', 'started'); f.session.receive(pcm().buffer); const old = f.session.generation;
  speech(f.session, 'user', 'started'); transcript(f.session, 'Actually the sedan is red', true);
  assert.equal(f.session.coordinator.utteranceId, 2); assert.ok(f.session.partial.includes('blue sedan'));
  assert.equal(f.session.coordinator.receiveMainPcm(old, pcm(), 16000), false); await f.session.stop();
});


test('current-output failure ends the owned call instead of dropping an arbitrary answer chunk', async () => {
  const f = fixture(); const shown = []; f.session.callbacks.error = reason => shown.push(reason);
  await f.session.start(); speech(f.session, 'assistant', 'started');
  f.output.enqueueMain = () => { f.output.failure('output_queue_bound'); return false; };
  f.session.receive(pcm().buffer); assert.equal(f.session.closed, true); assert.equal(f.output.closed, true);
  assert.ok(f.events.some(row => row.kind === 'playback_failed' && row.reason === 'output_queue_bound'));
  assert.deepEqual(shown, ['output_queue_bound']);
  assert.ok(f.requests.some(row => row.body?.event === 'playback_failed' && row.body.reason === 'output_queue_bound'));
  assert.ok(f.requests.some(row => row.body?.event === 'ended' && row.body.reason === 'output_queue_bound'));
  await f.session.stopping; assert.equal(f.requests.filter(row => row.url.endsWith('/duplex/stop')).length, 1);
});

test('ordinary stale or caller-owned PCM rejection never raises a playback failure or ends the call', async () => {
  const f = fixture(); await f.session.start(); speech(f.session, 'user', 'started'); f.session.receive(pcm().buffer);
  assert.equal(f.session.closed, false); assert.equal(f.events.some(row => row.kind === 'playback_failed'), false); await f.session.stop();
});

test('throwing failure and ended observers cannot prevent owned playback and call cleanup', async () => {
  const f = fixture(); await f.session.start(); speech(f.session, 'assistant', 'started');
  f.session.callbacks.event = event => { if (['playback_failed', 'ended'].includes(event.kind)) throw new Error('observer'); };
  f.session.callbacks.error = () => { throw new Error('error observer'); };
  f.output.enqueueMain = () => { try { f.output.failure('output_queue_bound'); } catch {} return false; };
  f.session.receive(pcm().buffer);
  assert.equal(f.session.closed, true); assert.equal(f.output.closed, true);
  await f.session.stopping; assert.equal(f.tracks[0].stopped, 1);
  assert.equal(f.requests.filter(row => row.url.endsWith('/duplex/stop')).length, 1);
  assert.ok(f.requests.some(row => row.body?.event === 'playback_failed' && row.body.reason === 'output_queue_bound'));
  assert.ok(f.requests.some(row => row.body?.event === 'ended' && row.body.reason === 'output_queue_bound'));
});
