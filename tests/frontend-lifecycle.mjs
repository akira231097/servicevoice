// Browser/resource lifecycle checks using mocked media and SDK events.
// These do not make or validate a live Vapi call.
// Run from the project directory: node --test tests/frontend-lifecycle.mjs
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { test } from 'node:test';
import { VoiceSession } from '../app/static/voice.js';
import { SnapshotChannel } from '../app/static/snapshot.js';

function harness(runtime = {}) {
  const seen = { statuses: [], notices: [], ended: 0, metrics: [], transcripts: [], captions: [] };
  const callbacks = {
    status: value => seen.statuses.push(value),
    notice: value => seen.notices.push(value),
    metrics: value => seen.metrics.push(value),
    volume: () => {}, refresh: () => {}, caption: value => seen.captions.push(value), transcript: value => seen.transcripts.push(value),
    ended: async () => { seen.ended += 1; },
  };
  return { voice: new VoiceSession({}, { id: 'test', token: 'test' }, callbacks, runtime), seen };
}

function mockMedia(t, getUserMedia) {
  const windowDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'window');
  const navigatorDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
  Object.defineProperty(globalThis, 'window', { value: { isSecureContext: true }, configurable: true });
  Object.defineProperty(globalThis, 'navigator', { value: { mediaDevices: { getUserMedia } }, configurable: true });
  t.after(() => {
    if (windowDescriptor) Object.defineProperty(globalThis, 'window', windowDescriptor);
    else delete globalThis.window;
    if (navigatorDescriptor) Object.defineProperty(globalThis, 'navigator', navigatorDescriptor);
    else delete globalThis.navigator;
  });
}

test('denied microphone closes setup once and gives an actionable message', async t => {
  mockMedia(t, async () => {
    const error = new Error('Permission denied');
    error.name = 'NotAllowedError';
    throw error;
  });
  const { voice, seen } = harness();
  await voice.start();
  assert.equal(voice.closed, true);
  assert.equal(voice.active, false);
  assert.equal(voice.sdk, null);
  assert.equal(seen.ended, 1);
  assert.match(seen.notices[0], /Microphone access was denied/);
  assert.equal(seen.statuses.at(-1), 'error');
});

test('invalid SDK export gives a local bundle error and releases the microphone', async () => {
  let trackStops = 0;
  const track = { readyState: 'live', stop: () => { trackStops += 1; } };
  const { voice, seen } = harness({
    mediaAvailable: () => true,
    getUserMedia: async () => ({ getTracks: () => [track], getAudioTracks: () => [track] }),
    loadSdk: async () => ({ default: { default: class Vapi {} } }),
  });
  await voice.start();
  assert.equal(voice.sdk, null);
  assert.equal(trackStops, 1);
  assert.equal(seen.ended, 1);
  assert.equal(seen.statuses.at(-1), 'error');
  assert.match(seen.notices[0], /local Vapi browser bundle/);
});

test('ending while permission is pending stops the late track without creating a provider call', async t => {
  let resolveMicrophone;
  let trackStops = 0;
  mockMedia(t, () => new Promise(resolve => { resolveMicrophone = resolve; }));
  const { voice, seen } = harness();
  const pendingStart = voice.start();
  await voice.stop();
  resolveMicrophone({ getTracks: () => [{ stop: () => { trackStops += 1; } }] });
  await pendingStart;
  assert.equal(trackStops, 1);
  assert.equal(voice.sdk, null);
  assert.equal(voice.preflightStream, null);
  assert.equal(seen.ended, 1);
  assert.equal(seen.statuses.at(-1), 'ended');
});

test('voice metrics require observed events and duplicate interruption IDs count once', async t => {
  const { voice, seen } = harness();
  voice.sdk = new EventEmitter();
  let providerStops = 0;
  voice.sdk.stop = async () => { providerStops += 1; };
  voice.bindEvents();
  t.after(() => voice.stop());

  assert.equal(voice.metrics().gap, null);
  assert.equal(voice.metrics().hasConnected, false);
  voice.sdk.emit('call-start');
  voice.sdk.emit('speech-start');
  assert.equal(voice.metrics().gapSamples, 0);

  voice.handleMessage({ type: 'speech-update', role: 'user', status: 'stopped' });
  voice.sdk.emit('speech-start');
  assert.equal(voice.metrics().gapSamples, 1);
  assert.ok(voice.metrics().gap >= 0);
  voice.sdk.emit('speech-start');
  assert.equal(voice.metrics().gapSamples, 1, 'a stop event is consumed once');

  voice.handleMessage({ type: 'user-interrupted', turnId: 'turn-1' });
  voice.handleMessage({ type: 'user-interrupted', turnId: 'turn-1' });
  voice.handleMessage({ type: 'user-interrupted', turnId: 'turn-2' });
  assert.equal(voice.metrics().interruptions, 2);

  await voice.stop();
  await voice.stop();
  assert.equal(providerStops, 1);
  assert.equal(seen.ended, 1);
  assert.deepEqual(voice.sdk.eventNames(), ['error'], 'only a stateless late-error sink remains');
  assert.doesNotThrow(() => voice.sdk.emit('error', new Error('late SDK rejection')));
});

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function testClock() {
  let now = 0, nextId = 0;
  const intervals = new Map(), timeouts = new Map();
  return {
    intervals, timeouts,
    advance: delta => { now += delta; },
    runtime: {
      now: () => now,
      setInterval: callback => { intervals.set(++nextId, callback); return nextId; },
      clearInterval: id => intervals.delete(id),
      setTimeout: callback => { timeouts.set(++nextId, callback); return nextId; },
      clearTimeout: id => timeouts.delete(id),
    },
  };
}

function connected(t) {
  const clock = testClock();
  const context = harness(clock.runtime);
  const sdk = new EventEmitter();
  sdk.stop = async () => {};
  let muted = false;
  sdk.setMuted = value => { muted = value; };
  sdk.isMuted = () => muted;
  context.voice.sdk = sdk;
  context.voice.bindEvents();
  sdk.emit('call-start');
  t.after(() => context.voice.stop());
  return { ...context, clock, sdk };
}

test('duplicate call-start keeps one clock and does not reset duration', async t => {
  const { voice, sdk, clock } = connected(t);
  clock.advance(700);
  sdk.emit('call-start');
  assert.equal(clock.intervals.size, 1);
  assert.equal(voice.metrics().elapsed, 700);
  await voice.stop();
  assert.equal(clock.intervals.size, 0);
});

test('duplicate stop events do not shorten a sample and slow tails are retained', t => {
  const { voice, sdk, clock } = connected(t);
  const stopped = { type: 'speech-update', role: 'user', status: 'stopped', turn: 1 };
  voice.handleMessage(stopped);
  clock.advance(600);
  voice.handleMessage(stopped);
  clock.advance(400);
  sdk.emit('speech-start');
  assert.equal(voice.metrics().gap, 1000);
  voice.handleMessage({ type: 'speech-update', role: 'user', status: 'started', turn: 2 });
  voice.handleMessage({ ...stopped, turn: 2 });
  clock.advance(35000);
  sdk.emit('speech-start');
  const metrics = voice.metrics();
  assert.deepEqual(metrics.gapSamplesMs, [1000, 35000]);
  assert.equal(metrics.gap, 1000, 'nearest-rank p50 is explicit');
  assert.equal(metrics.gapP95, 35000);
  assert.equal(metrics.gapMax, 35000);
  assert.match(metrics.definition, /not acoustic/);
});

test('a caller resuming speech invalidates the earlier gap with visible coverage', t => {
  const { voice, sdk, clock } = connected(t);
  voice.handleMessage({ type: 'speech-update', role: 'user', status: 'stopped', turn: 1 });
  clock.advance(200);
  voice.handleMessage({ type: 'speech-update', role: 'user', status: 'started', turn: 1 });
  sdk.emit('speech-start');
  assert.equal(voice.metrics().gapSamples, 0);
  assert.equal(voice.metrics().abandonedTurns, 1);
  voice.handleMessage({ type: 'speech-update', role: 'user', status: 'stopped', turn: 1 });
  clock.advance(500);
  sdk.emit('speech-start');
  assert.equal(voice.metrics().gap, 500);
  assert.equal(voice.metrics().stoppedTurns, 2);
});

test('muting preserves assistant speaking state and excludes muted input', t => {
  const { voice, seen, sdk } = connected(t);
  sdk.emit('speech-start');
  voice.setMuted(true);
  assert.equal(seen.statuses.at(-1), 'muted-speaking');
  voice.handleMessage({ type: 'speech-update', role: 'user', status: 'stopped' });
  assert.equal(voice.metrics().stoppedTurns, 0);
  voice.setMuted(false);
  assert.equal(seen.statuses.at(-1), 'speaking');
  sdk.emit('speech-end');
  assert.equal(seen.statuses.at(-1), 'listening');
});

function asynchronousMute(t) {
  const context = connected(t);
  const commands = [];
  let actualMuted = false, stateReads = 0, trackStops = 0, providerStops = 0;
  const track = { enabled: true, readyState: 'live', stop() { trackStops++; this.readyState = 'ended'; } };
  context.voice.preflightStream = { getAudioTracks: () => [track], getTracks: () => [track] };
  context.sdk.setMuted = value => commands.push(value);
  context.sdk.isMuted = () => { stateReads++; return actualMuted; };
  context.sdk.stop = async () => { providerStops++; };
  return {
    ...context, commands, track,
    observed: () => ({ stateReads, trackStops, providerStops }),
    setActual: value => { actualMuted = value; },
    emitAudioState(state, local = true) {
      if (local) actualMuted = ['off', 'blocked'].includes(state);
      context.sdk.emit('daily-participant-updated', { local, tracks: { audio: { state } } });
    },
  };
}

test('mute intent and source gating are immediate while SDK acknowledgement is asynchronous', t => {
  const { voice, seen, commands, track, emitAudioState, observed, clock } = asynchronousMute(t);
  assert.equal(voice.setMuted(true), true);
  assert.equal(voice.muted, true);
  assert.equal(seen.statuses.at(-1), 'muted');
  assert.equal(track.enabled, false);
  assert.equal(observed().stateReads, 0, 'do not read stale SDK state after sending a command');
  assert.deepEqual(commands, [true]);
  emitAudioState('off', false);
  assert.equal(voice.muteCommand, true, 'remote participant cannot acknowledge our mute');
  emitAudioState('playable');
  assert.equal(voice.closed, false, 'prior unmuted state may arrive before mute acknowledgement');
  assert.equal(voice.muted, true);
  assert.equal(track.enabled, false);
  emitAudioState('off');
  assert.equal(voice.muteCommand, null);
  assert.equal(clock.timeouts.size, 0);
});

test('rapid mute then unmute serializes commands and only explicit unmute dispatch enables source', t => {
  const { voice, commands, track, emitAudioState, clock } = asynchronousMute(t);
  voice.setMuted(true);
  voice.setMuted(false);
  assert.equal(voice.muted, false, 'latest user intent controls the button');
  assert.deepEqual(commands, [true], 'unmute waits for pending mute to settle');
  assert.equal(track.enabled, false);
  emitAudioState('off');
  assert.deepEqual(commands, [true, false]);
  assert.equal(track.enabled, true, 'the queued explicit unmute is now dispatched');
  emitAudioState('off');
  assert.equal(track.enabled, true, 'stale events do not change source gating or user intent');
  emitAudioState('playable');
  assert.equal(track.enabled, true);
  assert.equal(voice.muteCommand, null);
  emitAudioState('off');
  assert.equal(voice.muted, false);
  assert.deepEqual(commands, [true, false], 'later events cannot issue an implicit unmute');
  assert.equal(clock.timeouts.size, 0);
});

test('rapid toggles coalesce to the latest mute intent without dispatching abandoned unmute', t => {
  const { voice, commands, track, emitAudioState, clock } = asynchronousMute(t);
  voice.setMuted(true);
  voice.setMuted(false);
  voice.setMuted(true);
  emitAudioState('off');
  assert.deepEqual(commands, [true]);
  assert.equal(voice.muted, true);
  assert.equal(track.enabled, false);
  assert.equal(voice.muteCommand, null);
  assert.equal(clock.timeouts.size, 0);
});

test('SDK recovery that overrides a confirmed mute ends the call and releases the source immediately', async t => {
  const { voice, sdk, seen, commands, track, emitAudioState, observed, clock } = asynchronousMute(t);
  voice.setMuted(true);
  emitAudioState('off');
  // Model a provider recovery enabling even the supplied track unexpectedly.
  track.enabled = true;
  emitAudioState('playable');
  assert.equal(voice.closed, true);
  assert.equal(track.readyState, 'ended');
  assert.equal(observed().trackStops, 1);
  assert.deepEqual(commands, [true], 'recovery cannot create new unmute intent');
  await voice.stopping;
  assert.equal(observed().providerStops, 1);
  assert.equal(seen.ended, 1);
  assert.match(seen.notices[0], /microphone state/);
  assert.deepEqual(sdk.eventNames(), ['error']);
  assert.equal(clock.timeouts.size, 0);
});

test('missing mute acknowledgement closes on uncertainty and a late event cannot revive audio', async t => {
  const { voice, track, emitAudioState, clock, observed } = asynchronousMute(t);
  voice.setMuted(true);
  const deadline = [...clock.timeouts.values()][0];
  deadline();
  assert.equal(voice.closed, true);
  assert.equal(observed().stateReads, 1, 'read SDK state once only after the deadline');
  assert.equal(track.readyState, 'ended');
  await voice.stopping;
  emitAudioState('playable');
  deadline();
  assert.equal(track.enabled, false);
  assert.equal(observed().providerStops, 1);
  assert.equal(clock.timeouts.size, 0);
});

test('delayed current SDK state acknowledges a lost event without changing mute intent', t => {
  const { voice, track, setActual, clock, observed } = asynchronousMute(t);
  voice.setMuted(true);
  setActual(true);
  [...clock.timeouts.values()][0]();
  assert.equal(voice.closed, false);
  assert.equal(voice.muted, true);
  assert.equal(voice.muteCommand, null);
  assert.equal(track.enabled, false);
  assert.equal(observed().stateReads, 1);
  assert.equal(clock.timeouts.size, 0);
});

test('SDK mute-command rejection fails closed without leaking its error', async t => {
  const { voice, sdk, seen, track, clock } = asynchronousMute(t);
  sdk.setMuted = () => { throw new Error('secret-session-capability'); };
  voice.setMuted(true);
  assert.equal(voice.closed, true);
  assert.equal(track.readyState, 'ended');
  await voice.stopping;
  assert.ok(!seen.notices.join(' ').includes('secret'));
  assert.equal(clock.timeouts.size, 0);
});

test('network interruptions stay visible without starting a replacement call', t => {
  const { voice, seen, sdk } = connected(t);
  sdk.emit('network-connection', { type: 'sfu', event: 'interrupted' });
  sdk.emit('network-connection', { type: 'sfu', event: 'interrupted' });
  assert.equal(seen.statuses.at(-1), 'reconnecting');
  assert.equal(voice.metrics().networkInterruptions, 1);
  sdk.emit('speech-end');
  assert.equal(seen.statuses.at(-1), 'reconnecting');
  sdk.emit('network-connection', { type: 'sfu', event: 'connected' });
  assert.equal(seen.statuses.at(-1), 'listening');
  assert.equal(voice.active, true);
});

test('caption dedupe uses event identity without losing legitimate repeated words', async t => {
  const { voice, seen } = connected(t);
  const caption = { type: 'transcript', role: 'user', transcriptType: 'final', transcript: 'Yes', id: 'caption-1' };
  voice.handleMessage(caption);
  voice.handleMessage(caption);
  voice.handleMessage({ ...caption, id: 'caption-2' });
  voice.handleMessage({ ...caption, id: undefined });
  assert.equal(seen.transcripts.length, 3);
  await voice.stop();
  voice.handleMessage({ ...caption, id: 'late' });
  assert.equal(seen.transcripts.length, 3);
});

test('explicit provider silence timeout explains the ending and cleans up once without restarting', async t => {
  const { voice, sdk, seen, track, observed } = asynchronousMute(t);
  let starts = 0;
  sdk.start = () => { starts++; };
  const ended = { type: 'status-update', status: 'ended', endedReason: 'silence-timed-out' };
  sdk.emit('message', ended);
  sdk.emit('call-end');
  sdk.emit('message', ended);
  await voice.stopping;
  assert.equal(voice.providerEndedReason, 'silence-timed-out');
  assert.equal(seen.statuses.at(-1), 'silence-timeout');
  assert.equal(seen.ended, 1);
  assert.equal(observed().providerStops, 1);
  assert.equal(observed().trackStops, 1);
  assert.equal(track.readyState, 'ended');
  assert.equal(starts, 0);
  assert.deepEqual(sdk.eventNames(), ['error']);
  assert.deepEqual(seen.notices, [], 'a silence timeout does not imply a microphone fault');
});

test('missing or unrecognized provider end reasons retain ordinary ending without echoing arbitrary values', async t => {
  for (const endedReason of [undefined, 'secret-token-in-provider-message', { message: 'silence-timed-out' }]) {
    const { voice, sdk, seen } = connected(t);
    sdk.emit('message', { type: 'status-update', status: 'ended', endedReason });
    await voice.stopping;
    assert.equal(voice.providerEndedReason, null);
    assert.equal(seen.statuses.at(-1), 'ended');
    assert.equal(seen.ended, 1);
    assert.deepEqual(seen.notices, []);
  }
});

test('reason-less remote end is not reclassified by irrelevant or late timeout messages', async t => {
  const { voice, sdk, seen } = connected(t);
  sdk.emit('message', { type: 'status-update', status: 'in-progress', endedReason: 'silence-timed-out' });
  assert.equal(voice.providerEndedReason, null);
  assert.equal(voice.closed, false);
  sdk.emit('call-end');
  await voice.stopping;
  voice.handleMessage({ type: 'status-update', status: 'ended', endedReason: 'silence-timed-out' });
  assert.equal(voice.providerEndedReason, null);
  assert.equal(seen.statuses.at(-1), 'ended');
  assert.equal(seen.ended, 1);
});

test('raw SDK errors never reveal session tokens or request content', async () => {
  const { voice, seen } = harness();
  await voice.fail({ error: { message: 'Request failed: secret-session-token in https://server/?token=secret' } });
  assert.equal(seen.ended, 1);
  assert.ok(!seen.notices.join(' ').includes('secret'));
  assert.match(seen.notices[0], /configuration and network/);
});

test('cleanup continues when a playback pause throws', async () => {
  const { voice } = harness();
  let stopped = 0, removed = 0;
  voice.audioPlayers.add({
    srcObject: { getTracks: () => [{ stop: () => { stopped++; } }] },
    pause: () => { throw new Error('detached player'); }, remove: () => { removed++; },
  });
  await voice.stop();
  assert.equal(stopped, 1);
  assert.equal(removed, 1);
});

test('replacement playback retires the old sink without stopping a shared remote track', async t => {
  const { voice, sdk } = connected(t);
  let trackStops = 0, oldPauses = 0, newPauses = 0;
  const shared = { getTracks: () => [{ stop: () => { trackStops++; } }] };
  const oldPlayer = { srcObject: shared, pause() { oldPauses++; }, remove() {} };
  const newPlayer = { srcObject: shared, pause() { newPauses++; }, remove() {} };
  sdk.emit('audio', oldPlayer);
  sdk.emit('audio', newPlayer);
  assert.equal(oldPauses, 1);
  assert.equal(oldPlayer.srcObject, null);
  assert.equal(trackStops, 0, 'replacement must not stop the new sink’s shared track');
  assert.deepEqual([...voice.audioPlayers], [newPlayer]);
  sdk.emit('audio', newPlayer);
  assert.equal(newPauses, 0, 'repeated notification of the same sink does not interrupt it');
  await voice.stop();
  assert.equal(newPauses, 1);
  assert.equal(trackStops, 1);
});

test('replacement still disconnects an old stream when pause throws', async t => {
  const { voice, sdk } = connected(t);
  let removed = 0;
  const oldPlayer = { srcObject: { getTracks: () => [] }, pause() { throw new Error('detached'); }, remove() { removed++; } };
  const currentPlayer = { srcObject: { getTracks: () => [] }, pause() {}, remove() {} };
  sdk.emit('audio', oldPlayer);
  sdk.emit('audio', currentPlayer);
  assert.equal(oldPlayer.srcObject, null);
  assert.equal(removed, 1);
  assert.deepEqual([...voice.audioPlayers], [currentPlayer]);
});

test('cancel during provider setup cleans late playback and absorbs late SDK rejection', async () => {
  const entered = deferred(), providerStart = deferred();
  let micStops = 0, sdkStops = 0, playbackStops = 0, sdk;
  const track = { stop: () => { micStops++; }, readyState: 'live' };
  class FakeVapi extends EventEmitter {
    constructor() { super(); sdk = this; }
    start() { entered.resolve(); return providerStart.promise; }
    async stop() { sdkStops++; }
  }
  const { voice, seen } = harness({
    mediaAvailable: () => true,
    getUserMedia: async () => ({ getTracks: () => [track], getAudioTracks: () => [track] }),
    loadSdk: async () => ({ default: FakeVapi }),
  });
  const pending = voice.start();
  await entered.promise;
  await voice.stop();
  assert.equal(micStops, 1);
  sdk.emit('audio', { srcObject: { getTracks: () => [{ stop: () => { playbackStops++; } }] }, pause() {}, remove() {} });
  assert.equal(playbackStops, 1);
  assert.doesNotThrow(() => sdk.emit('error', new Error('late rejection')));
  providerStart.reject(new Error('late rejected setup'));
  await pending;
  assert.equal(seen.ended, 1);
  assert.equal(seen.notices.length, 0);
  assert.ok(sdkStops >= 2, 'late SDK resources receive a final cleanup');
  assert.deepEqual(sdk.eventNames(), ['error']);
});

test('duplicate start requests do not request the microphone twice', async () => {
  const permission = deferred();
  let mediaRequests = 0;
  const { voice } = harness({ mediaAvailable: () => true, getUserMedia: () => { mediaRequests++; return permission.promise; } });
  const pending = voice.start();
  await voice.start();
  assert.equal(mediaRequests, 1);
  await voice.stop();
  permission.resolve({ getTracks: () => [] });
  await pending;
});

test('snapshot coalescing shares a read and performs one requested fresh follow-up', async () => {
  const reads = [], applied = [];
  const channel = new SnapshotChannel({
    read: () => { const pending = deferred(); reads.push(pending); return pending.promise; },
    apply: snapshot => applied.push(snapshot),
  });
  channel.bind({ id: 'a' });
  const first = channel.refresh();
  assert.equal(channel.refresh(), first);
  const next = channel.refresh({ fresh: true });
  const alsoNext = channel.refresh({ fresh: true });
  await Promise.resolve();
  assert.equal(reads.length, 1);
  reads[0].resolve({ version: 1 });
  await first;
  await Promise.resolve();
  assert.equal(reads.length, 2);
  reads[1].resolve({ version: 2 });
  await Promise.all([next, alsoNext]);
  assert.deepEqual(applied, [{ version: 1 }, { version: 2 }]);
});

test('late snapshots and errors cannot overwrite the replacement session', async () => {
  const reads = [], applied = [], errors = [];
  const channel = new SnapshotChannel({
    read: (session, options) => { const pending = deferred(); reads.push({ ...pending, session, ...options }); return pending.promise; },
    apply: snapshot => applied.push(snapshot), onError: error => errors.push(error),
  });
  channel.bind({ id: 'old' });
  const old = channel.refresh();
  await Promise.resolve();
  channel.bind({ id: 'new' });
  assert.equal(reads[0].signal.aborted, true);
  const current = channel.refresh();
  await Promise.resolve();
  reads[1].resolve({ id: 'new', appointment: 'verified' });
  await current;
  reads[0].resolve({ id: 'old', appointment: 'outdated' });
  await old;
  assert.deepEqual(applied, [{ id: 'new', appointment: 'verified' }]);
  const pending = channel.refresh();
  await Promise.resolve();
  channel.bind(null);
  reads[2].reject(new Error('old failure'));
  await pending;
  assert.deepEqual(errors, []);
});

test('end immediately stops owned microphone and playback tracks', async () => {
  const { voice } = harness();
  let microphoneStops = 0;
  let playbackStops = 0;
  let playerPauses = 0;
  let playerRemovals = 0;
  voice.preflightStream = { getTracks: () => [{ stop: () => { microphoneStops += 1; } }] };
  const player = {
    srcObject: { getTracks: () => [{ stop: () => { playbackStops += 1; } }] },
    pause: () => { playerPauses += 1; },
    remove: () => { playerRemovals += 1; },
  };
  voice.audioPlayers.add(player);
  await voice.stop();
  assert.equal(microphoneStops, 1);
  assert.equal(playbackStops, 1);
  assert.equal(playerPauses, 1);
  assert.equal(playerRemovals, 1);
  assert.equal(player.srcObject, null);
  assert.equal(voice.audioPlayers.size, 0);
});
