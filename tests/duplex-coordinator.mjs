import assert from 'node:assert/strict';
import { test } from 'node:test';
import { DuplexCoordinator, PcmPlayback } from '../app/static/duplex-coordinator.js';

const flush = async () => { for (let count = 0; count < 8; count++) await Promise.resolve(); };
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function clock() {
  let now = 0, serial = 0;
  const timers = new Map();
  return {
    now: () => now,
    setTimeout(callback, delay) { const id = ++serial; timers.set(id, { callback, at: now + delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
    async advance(milliseconds) {
      const target = now + milliseconds;
      while (true) {
        const next = [...timers].filter(([, timer]) => timer.at <= target).sort((left, right) => left[1].at - right[1].at)[0];
        if (!next) break;
        now = next[1].at; timers.delete(next[0]); next[1].callback(); await flush();
      }
      now = target; await flush();
    },
    get pending() { return timers.size; },
  };
}
function playback() {
  const calls = [], handles = [];
  return {
    calls, handles,
    playCue(phraseId) {
      calls.push(['cue', phraseId]);
      const done = deferred();
      const handle = { ended: done.promise, stopped: false, stop() { this.stopped = true; calls.push(['cue-stop', phraseId]); done.resolve(); } };
      handles.push(handle); return handle;
    },
    enqueueMain(value) { calls.push(['main', value.generationId]); return true; },
    stopMain() { calls.push(['main-stop']); },
    close() { calls.push(['close']); },
  };
}
function decision(phase, overrides = {}) {
  return { phrase_id: phase === 'listening' ? 'neutral_ack' : 'moment', confidence: .99,
    language_choice: 'cache_compatible', expires_in_ms: 1000, ...overrides };
}
function setup(options = {}) {
  const time = clock(), output = playback(), requests = [], events = [];
  const selectCue = async request => { requests.push(request); return decision(request.phase); };
  const coordinator = new DuplexCoordinator({ playback: output, selectCue, now: time.now,
    setTimeout: time.setTimeout, clearTimeout: time.clearTimeout, onEvent: event => events.push(event), ...options });
  return { coordinator, time, output, requests, events };
}
function caller(coordinator, text = 'I need routine service for my car') {
  coordinator.userSpeechStarted(); coordinator.updatePartial(text, { revision: 1 });
}
function waiting(coordinator, id = 'main-1') {
  caller(coordinator); coordinator.userSpeechStopped(); coordinator.beginGeneration(id);
}

test('a listening cue is selected and played while the caller is still speaking', async () => {
  const { coordinator, time, requests, output, events } = setup();
  caller(coordinator);
  await time.advance(299); assert.equal(requests.length, 0);
  await time.advance(1);
  assert.equal(coordinator.userSpeaking, true);
  assert.equal(requests.length, 1); assert.equal(requests[0].phase, 'listening');
  assert.equal(requests[0].revision, 1);
  assert.deepEqual(output.calls.filter(call => call[0] === 'cue'), [['cue', 'neutral_ack']]);
  assert.equal(events.some(event => JSON.stringify(event).includes('routine service')), false);
  coordinator.close();
});

test('the waiting cue runs independently while a main generation remains unfinished', async () => {
  const { coordinator, time, requests, output } = setup();
  waiting(coordinator); await time.advance(550);
  assert.equal(coordinator.generating, true); assert.equal(requests[0].phase, 'waiting');
  assert.deepEqual(output.calls.filter(call => call[0] === 'cue'), [['cue', 'moment']]);
  coordinator.close();
});

test('partial revisions abort old classification and reject its late result', async () => {
  const pending = [], { coordinator, time, output } = setup({ selectCue: request => {
    const done = deferred(); pending.push({ request, done }); return done.promise;
  } });
  caller(coordinator); await time.advance(300);
  coordinator.updatePartial('Actually I need a different service', { revision: 2 });
  assert.equal(pending[0].request.signal.aborted, true);
  pending[0].done.resolve(decision('listening')); await flush();
  assert.equal(output.handles.length, 0);
  await time.advance(350);
  pending[1].done.resolve(decision('listening')); await flush();
  assert.equal(output.handles.length, 1); coordinator.close();
});

test('caller stop invalidates a listening decision even if the selector ignores cancellation', async () => {
  const done = deferred(), { coordinator, time, output } = setup({ selectCue: () => done.promise });
  caller(coordinator); await time.advance(300); coordinator.userSpeechStopped();
  done.resolve(decision('listening')); await flush();
  assert.equal(output.handles.length, 0); coordinator.close();
});

test('main audio wins a pending selection and stops an audible cue before scheduling its first sample', async () => {
  const { coordinator, time, output } = setup();
  waiting(coordinator); await time.advance(550);
  assert.equal(output.handles[0].stopped, false);
  assert.equal(coordinator.receiveMainPcm('main-1', new Uint8Array([0, 0]), 16000), true);
  assert.equal(output.handles[0].stopped, true);
  assert.deepEqual(output.calls.slice(-2), [['cue-stop', 'moment'], ['main', 'main-1']]);
  await time.advance(20000);
  assert.equal(output.handles.length, 1); coordinator.close();
});

test('main readiness aborts a pending classifier and ignores its later success', async () => {
  const done = deferred(); let selected;
  const { coordinator, time, output } = setup({ selectCue: request => { selected = request; return done.promise; } });
  waiting(coordinator); await time.advance(550);
  coordinator.receiveMainPcm('main-1', new Uint8Array([0, 0]), 16000);
  assert.equal(selected.signal.aborted, true);
  done.resolve(decision('waiting')); await flush();
  assert.equal(output.handles.length, 0); coordinator.close();
});

test('barge-in stops every main node and rejects old PCM after the caller finishes', async () => {
  const { coordinator, output } = setup(); waiting(coordinator);
  coordinator.receiveMainPcm('main-1', new Uint8Array([0, 0]), 16000);
  coordinator.userSpeechStarted();
  assert.equal(output.calls.at(-1)[0], 'main-stop');
  assert.equal(coordinator.receiveMainPcm('main-1', new Uint8Array([0, 0]), 16000), false);
  coordinator.updatePartial('Please tell me the opening hours'); coordinator.userSpeechStopped();
  assert.equal(coordinator.receiveMainPcm('main-1', new Uint8Array([0, 0]), 16000), false);
  coordinator.beginGeneration('main-2');
  assert.equal(coordinator.receiveMainPcm('main-2', new Uint8Array([0, 0]), 16000), true);
  coordinator.close();
});

test('late classifier responses after the deadline cannot play even when abort is ignored', async () => {
  const done = deferred(); let signal;
  const { coordinator, time, output, events } = setup({ selectCue: request => { signal = request.signal; return done.promise; } });
  caller(coordinator); await time.advance(650);
  assert.equal(signal.aborted, true);
  done.resolve(decision('listening')); await flush();
  assert.equal(output.handles.length, 0);
  assert.equal(events.some(event => event.reason === 'classifier_deadline'), true);
  coordinator.close();
});

for (const [name, overrides] of [
  ['wrong phase clip', { phrase_id: 'moment' }],
  ['arbitrary classifier prose', { phrase_id: 'Your booking is confirmed.' }],
  ['low confidence', { confidence: .7 }],
  ['nonfinite confidence', { confidence: NaN }],
  ['unverified language', { language_choice: 'uncertain' }],
  ['expired selection', { expires_in_ms: 0 }],
]) {
  test(`unsafe selection remains silent: ${name}`, async () => {
    const { coordinator, time, output } = setup({ selectCue: async () => decision('listening', overrides) });
    caller(coordinator); await time.advance(300);
    assert.equal(output.handles.length, 0); coordinator.close();
  });
}

test('a sensitive state cancels active cues and requires fresh evidence after clearing', async () => {
  const { coordinator, time, output } = setup(); caller(coordinator); await time.advance(300);
  coordinator.setSensitive(true); assert.equal(output.handles[0].stopped, true);
  coordinator.updatePartial('Yes please confirm it', { revision: 2 }); await time.advance(20000);
  assert.equal(output.handles.length, 1);
  coordinator.setSensitive(false); await time.advance(1000);
  assert.equal(output.handles.length, 1);
  coordinator.userSpeechStopped(); coordinator.userSpeechStarted();
  coordinator.updatePartial('Could you explain your service options', { revision: 3 }); await time.advance(300);
  assert.equal(output.handles.length, 2); coordinator.close();
});

test('cue cooldown, per-call count and request limits bound both speech and provider attempts', async () => {
  const { coordinator, time, output, requests } = setup({ maxCues: 2, maxRequests: 2 });
  caller(coordinator); await time.advance(300);
  coordinator.updatePartial('Here are a few more service details', { revision: 2 }); await time.advance(300);
  assert.equal(requests.length, 1);
  await time.advance(15000);
  coordinator.userSpeechStopped(); coordinator.userSpeechStarted();
  coordinator.updatePartial('I can leave the car there tomorrow', { revision: 3 }); await time.advance(300);
  assert.equal(output.handles.length, 2);
  await time.advance(15000);
  coordinator.userSpeechStopped(); coordinator.userSpeechStarted();
  coordinator.updatePartial('Let me also explain the issue clearly', { revision: 4 }); await time.advance(300);
  assert.equal(requests.length, 2); assert.equal(output.handles.length, 2); coordinator.close();
});

test('a long utterance and its waiting phase share one cue allowance across revisions', async () => {
  const { coordinator, time, output, requests } = setup();
  caller(coordinator); await time.advance(300);
  assert.equal(requests[0].utteranceId, 1);
  await time.advance(20000);
  coordinator.updatePartial('I also want to tell you more about the same issue', { revision: 2 });
  await time.advance(300);
  coordinator.updateFinal('I also want to tell you more about the same issue', { revision: 3 });
  coordinator.userSpeechStopped(); coordinator.beginGeneration('main'); await time.advance(550);
  assert.equal(output.handles.length, 1); assert.equal(requests.length, 1);
  coordinator.close();
});

test('a final transcript cancels listening classification without scheduling a new listening cue', async () => {
  const { coordinator, time, requests } = setup();
  caller(coordinator); coordinator.updateFinal('I need routine service for my car tomorrow', { revision: 2 });
  await time.advance(300); assert.equal(requests.length, 0);
  coordinator.userSpeechStopped(); coordinator.beginGeneration('main'); await time.advance(550);
  assert.equal(requests.length, 1); assert.equal(requests[0].phase, 'waiting'); coordinator.close();
});

test('selection expiry never truncates complete speech; a separate duration guard bounds runaway audio', async () => {
  const { coordinator, time, output } = setup({ selectCue: async () => decision('listening', { expires_in_ms: 100 }) });
  caller(coordinator); await time.advance(300);
  assert.equal(output.handles[0].stopped, false); await time.advance(100);
  assert.equal(output.handles[0].stopped, false); await time.advance(2399);
  assert.equal(output.handles[0].stopped, false); await time.advance(1);
  assert.equal(output.handles[0].stopped, true); coordinator.close();
});

test('normal ASR revisions and a caller yielding do not cut an already-started listening word', async () => {
  const { coordinator, time, output } = setup();
  caller(coordinator); await time.advance(300);
  coordinator.updatePartial('I need routine service for my vehicle tomorrow', { revision: 2 });
  assert.equal(output.handles[0].stopped, false);
  coordinator.userSpeechStopped(); coordinator.beginGeneration('main-1');
  assert.equal(output.handles[0].stopped, false);
  coordinator.receiveMainPcm('main-1', new Uint8Array([0, 0]), 16000);
  assert.equal(output.handles[0].stopped, true, 'Actual main audio still preempts immediately');
  coordinator.close();
});

test('closing cancels timers, active playback and a late ignored-abort classifier', async () => {
  const done = deferred(), { coordinator, time, output } = setup({ selectCue: () => done.promise });
  caller(coordinator); await time.advance(300); coordinator.close(); coordinator.close();
  assert.equal(time.pending, 0);
  done.resolve(decision('listening')); await flush(); await time.advance(5000);
  assert.equal(output.handles.length, 0); assert.equal(output.calls.filter(call => call[0] === 'close').length, 1);
  assert.equal(coordinator.beginGeneration('late'), false);
  assert.equal(coordinator.receiveMainPcm('late', new Uint8Array([0, 0]), 16000), false);
});

function audioContext() {
  const sources = [], buffers = [];
  return {
    state: 'running', currentTime: 10, destination: {}, sources, buffers,
    createBuffer(channels, length, rate) {
      const buffer = { channels, length, sampleRate: rate, duration: length / rate,
        copyToChannel(samples) { this.samples = samples; } }; buffers.push(buffer); return buffer;
    },
    createBufferSource() {
      const source = { stopped: false, disconnected: false, connect() {},
        start(at, offset = 0) { this.startedAt = at; this.offset = offset; }, stop() { this.stopped = true; this.onended?.(); },
        disconnect() { this.disconnected = true; } };
      sources.push(source); return source;
    },
  };
}
const pcm = (samples = 1600) => new Uint8Array(samples * 2);

test('PCM playback decodes actual little-endian PCM geometry, including nonzero view offsets', () => {
  const context = audioContext(), output = new PcmPlayback({ context });
  const bytes = new Uint8Array([99, 0, 128, 255, 127, 99]);
  assert.equal(output.enqueueMain({ pcm: bytes.subarray(1, 5), sampleRate: 16000 }), true);
  assert.equal(context.buffers[0].length, 2); assert.equal(context.buffers[0].sampleRate, 16000);
  assert.deepEqual([...context.buffers[0].samples], [-1, 32767 / 32768]);
  assert.equal(context.sources[0].startedAt, 10); output.close();
});

test('local main PCM preempts a cached cue immediately and main chunks use the same playback clock', () => {
  const context = audioContext(), output = new PcmPlayback({ context, cueAssets: { neutral_ack: { pcm: pcm(2400).buffer, sampleRate: 24000 } } });
  const cue = output.playCue('neutral_ack'); assert.ok(cue); assert.equal(context.buffers[0].sampleRate, 24000);
  assert.equal(output.enqueueMain({ pcm: pcm(), sampleRate: 16000 }), true);
  assert.equal(context.sources[0].stopped, true); assert.equal(context.sources[1].startedAt, 10);
  assert.equal(output.enqueueMain({ pcm: pcm(), sampleRate: 16000 }), true);
  assert.equal(context.sources[2].startedAt, 10.1); assert.equal(output.playCue('neutral_ack'), null);
  output.close(); assert.equal(context.sources.every(source => source.stopped && source.disconnected), true);
});

test('PCM queue bounds reject oversized and malformed audio without extending the queue', () => {
  const context = audioContext(), output = new PcmPlayback({ context, maxQueuedSeconds: .2 });
  assert.throws(() => output.enqueueMain({ pcm: new Uint8Array([0]), sampleRate: 16000 }), /invalid_pcm16/);
  assert.throws(() => output.enqueueMain({ pcm: pcm(), sampleRate: 7999 }), /invalid_sample_rate/);
  assert.throws(() => output.enqueueMain({ pcm: pcm(4000), sampleRate: 16000 }), /audio_chunk_too_large/);
  assert.equal(output.enqueueMain({ pcm: pcm(), sampleRate: 16000 }), true);
  assert.equal(output.enqueueMain({ pcm: pcm(), sampleRate: 16000 }), true);
  assert.equal(output.enqueueMain({ pcm: pcm(), sampleRate: 16000 }), false);
  assert.equal(context.sources.length, 2); output.close();
});

test('suspended audio contexts fail silent and close cancels all scheduled future chunks', () => {
  const context = audioContext(), output = new PcmPlayback({ context, cueAssets: new Map([['neutral_ack', { pcm: pcm(), sampleRate: 16000 }]]) });
  context.state = 'suspended';
  assert.equal(output.playCue('neutral_ack'), null);
  assert.equal(output.enqueueMain({ pcm: pcm(), sampleRate: 16000 }), false);
  context.state = 'running';
  output.enqueueMain({ pcm: pcm(), sampleRate: 16000 }); output.enqueueMain({ pcm: pcm(), sampleRate: 16000 });
  output.close(); output.close();
  assert.equal(context.sources.every(source => source.stopped && source.disconnected), true);
  assert.equal(output.main.size, 0); assert.equal(output.playCue('neutral_ack'), null);
  assert.equal(output.enqueueMain({ pcm: pcm(), sampleRate: 16000 }), false);
  assert.equal(context.state, 'running', 'Shared context/microphone lifecycle remains transport-owned');
});

test('source start failure stops and disconnects the owned node', () => {
  const context = audioContext(), create = context.createBufferSource.bind(context);
  context.createBufferSource = () => { const source = create(); source.start = () => { throw new Error('browser start failed'); }; return source; };
  const output = new PcmPlayback({ context, cueAssets: { neutral_ack: { pcm: pcm(), sampleRate: 16000 } } });
  assert.throws(() => output.playCue('neutral_ack'), /browser start failed/);
  assert.equal(context.sources[0].stopped, true); assert.equal(context.sources[0].disconnected, true); output.close();
});

test('provisional pause retains the unsounded suffix and future chunks then resumes exact buffer offsets', () => {
  const context = audioContext(), output = new PcmPlayback({ context });
  output.enqueueMain({ pcm: pcm(16000), sampleRate: 16000 });
  output.enqueueMain({ pcm: pcm(8000), sampleRate: 16000 });
  context.currentTime = 10.4; output.pauseMain();
  assert.equal(context.sources[0].stopped, true); assert.equal(context.sources[1].stopped, true);
  assert.equal(output.pausedMain.length, 2);
  assert.ok(Math.abs(output.pausedMain[0].offset - .4) < .00001);
  assert.equal(output.pausedMain[1].offset, 0);
  assert.equal(output.enqueueMain({ pcm: pcm(), sampleRate: 16000 }), false);
  context.currentTime = 10.48; assert.equal(output.resumeMain(), true);
  assert.equal(context.sources[2].buffer, context.sources[0].buffer);
  assert.ok(Math.abs(context.sources[2].offset - .4) < .00001);
  assert.equal(context.sources[2].startedAt, 10.48);
  assert.ok(Math.abs(context.sources[3].startedAt - 11.08) < .00001);
  output.pauseMain(); output.stopMain();
  assert.equal(output.pausedMain, null); assert.equal(output.resumeMain(), true);
  assert.equal(context.sources.length, 4, 'Confirmed interruption never resurrects paused audio'); output.close();
});

test('20ms append-compatible ASR streaming produces a cue before caller end without cancelling or flooding Jev', async () => {
  const requests = [], time = clock(), output = playback(), events = [];
  const coordinator = new DuplexCoordinator({ playback: output, now: time.now, setTimeout: time.setTimeout,
    clearTimeout: time.clearTimeout, listeningDelayMs: 0, onEvent: event => events.push(event),
    selectCue: request => new Promise(resolve => {
      requests.push({ request, at: time.now() });
      time.setTimeout(() => resolve(decision('listening', request.text.split(/\s+/).length < 6 ? { phrase_id: 'silence' } : {})), 180);
    }) });
  coordinator.userSpeechStarted();
  const sentence = 'I would like routine service for my vehicle and I can describe the issue in more detail while you listen';
  for (let length = 1; length <= sentence.length; length++) {
    coordinator.updatePartial(sentence.slice(0, length), { revision: length }); await time.advance(20);
  }
  assert.equal(coordinator.userSpeaking, true); assert.equal(output.handles.length, 1);
  assert.ok(requests.length >= 2 && requests.length <= 5);
  assert.ok(requests.every(row => row.request.text.split(/\s+/).length >= 3));
  assert.ok(requests.every(row => !row.request.signal.aborted));
  assert.ok(requests.slice(1).every((row, index) => row.at - requests[index].at >= 350));
  const played = events.find(event => event.kind === 'cue_started');
  assert.ok(played.latest_revision > played.snapshot_revision, 'Evidence retains the earlier snapshot identity');
  assert.equal(coordinator.decisionDeadlineMs, 350, 'Provider-facing deadline was not weakened');
  coordinator.close();
});

test('ordinary ASR trailing-word rewrites retain the classified stable prefix instead of starving it', async () => {
  const pending = deferred(); let selected;
  const { coordinator, time, output } = setup({ listeningDelayMs: 0,
    selectCue: request => { selected = request; return pending.promise; } });
  caller(coordinator, 'I need routine service for my vehi'); await time.advance(0);
  assert.equal(selected.text, 'I need routine service for my');
  for (const [index, text] of ['I need routine service for my vehicle',
    'I need routine service for my vehicle tomor', 'I need routine service for my vehicle tomorrow morning'].entries()) {
    coordinator.updatePartial(text, { revision: index + 2 }); await time.advance(40);
  }
  assert.equal(selected.signal.aborted, false);
  pending.resolve(decision('listening')); await flush();
  assert.equal(output.handles.length, 1); assert.equal(coordinator.userSpeaking, true);
  coordinator.close();
});
