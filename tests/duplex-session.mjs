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
  const time = clock(), requests = [], sockets = [], contexts = [], microphones = [], events = [];
  const tracks = [{ enabled: true, stopped: 0, stop() { this.stopped++; } }];
  const stream = { getTracks: () => tracks, getAudioTracks: () => tracks };
  const audio = pcm(), hash = createHash('sha256').update(audio).digest('hex');
  const output = { cues: [], main: [], stops: 0, pauses: 0, resumes: 0, paused: false, closed: false,
    playCue(phrase) { const done = deferred(); const handle = { phrase, stopped: false, ended: done.promise,
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
    if (url.endsWith('/duplex/cue')) return reply({ phrase_id: request.body.phase === 'listening' ? 'neutral_ack' : 'moment',
      confidence: .99, language_choice: 'cache_compatible', expires_in_ms: 800 });
    if (url.endsWith('/duplex/stop')) return reply({ ok: true });
    if (url === '/api/sessions/fixture-session') return reply({ ended: false, stage: 'service', proposal: null, pending_operations: [] });
    return reply({ ok: true });
  }
  let mediaRequests = 0;
  const session = new DuplexSession({ callbacks: { event: event => events.push(event) }, runtime: {
    ...time, fetch, AudioContext: Context, WebSocket: Socket,
    getUserMedia: async () => { mediaRequests++; return getUserMedia ? await getUserMedia.promise : stream; },
    playbackFactory: () => output,
    microphoneFactory: options => {
      const mic = { options, starts: 0, stops: 0, muted: false,
        async start() { this.starts++; }, async stop() { this.stops++; }, setMuted(value) { this.muted = value; tracks[0].enabled = !value; } };
      microphones.push(mic); return mic;
    },
  } });
  return { session, time, requests, sockets, contexts, microphones, output, tracks, stream, events,
    get mediaRequests() { return mediaRequests; }, cues: () => requests.filter(row => row.url.endsWith('/duplex/cue')) };
}
function message(session, body) { session.receive(JSON.stringify(body)); }
function speech(session, role, status) { message(session, { type: 'speech-update', role, status }); }
function transcript(session, text, final = false) { message(session, { type: 'transcript', role: 'user', transcript: text, transcriptType: final ? 'final' : 'partial' }); }

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
  assert.equal(f.output.cues[0].stopped, true);
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

test('a final transcript immediately cancels listening selection and waits for genuine speech stop', async () => {
  const f = fixture(); await f.session.start(); f.microphones[0].options.onSpeech(true);
  speech(f.session, 'user', 'started'); transcript(f.session, 'I need routine service for my vehicle', true);
  await f.time.advance(600); assert.equal(f.cues().length, 0);
  speech(f.session, 'user', 'stopped'); await f.time.advance(200); assert.equal(f.cues().length, 0, 'Local capture remains active');
  f.microphones[0].options.onSpeech(false); await f.time.advance(160 + 550);
  assert.equal(f.cues().length, 1); assert.equal(f.cues()[0].body.phase, 'waiting');
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
  await f.session.start(); transcript(f.session, 'Alex', true); await f.time.advance(0);
  assert.equal(f.cues().length, 1); assert.equal(f.session.coordinator.sensitive, false);
  old.resolve(reply({ ended: false, stage: 'confirmation', proposal: { id: 'fixture-proposal' }, pending_operations: [] }));
  await flush(); assert.equal(f.session.coordinator.sensitive, false); assert.equal(f.cues()[0].options.signal.aborted, false);
  await f.time.advance(550); assert.equal(f.output.cues.length, 1); await f.session.stop();
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
  assert.equal(f.output.cues[0].stopped, true); assert.equal(f.output.main.length, 1);
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
  await f.time.advance(16000);
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
  for (let i = 0; i < 30 && !f.sockets.length; i++) await new Promise(resolve => setImmediate(resolve));
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

test('sustained background energy cannot deadlock the main answer past the350ms confirmation window', async () => {
  const f = fixture(); await f.session.start(); speech(f.session, 'assistant', 'started'); f.session.receive(pcm().buffer);
  const generation = f.session.generation; f.microphones[0].options.onSpeech(true);
  for (let count = 0; count < 17; count++) { f.session.receive(pcm(1800).buffer); await f.time.advance(20); }
  assert.equal(f.output.main.length, 1); await f.time.advance(10);
  assert.equal(f.session.provisional, null); assert.equal(f.output.resumes, 1);
  assert.equal(f.output.main.length, 18); assert.equal(f.session.generation, generation);
  assert.equal(f.session.localSpeaking, true); assert.equal(f.session.callerSpeaking, false);
  assert.ok(f.events.some(event => event.kind === 'provisional_resumed' && event.reason === 'confirmation_timeout'));
  f.session.receive(pcm(1900).buffer); assert.equal(f.output.main.length, 19); await f.session.stop();
});

test('provisional output buffer is bounded and Stop removes its timer without recovery', async () => {
  const f = fixture(); await f.session.start(); speech(f.session, 'assistant', 'started'); f.session.receive(pcm().buffer);
  f.microphones[0].options.onSpeech(true);
  for (let count = 0; count < 30; count++) f.session.receive(pcm().buffer);
  assert.ok(f.session.provisional.bytes <= 11200); assert.ok(f.session.discardedOutputBytes > 0);
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
  'please reply in Spanish', 'y también el motor']) {
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
  await f.time.advance(0); assert.equal(f.session.coordinator.generating, true);
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
