import test from 'node:test';
import assert from 'node:assert/strict';
import net from 'node:net';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { AudioSocketParser, audioSocketFrame, PhoneAdmissions, PhonePlayback,
  PhoneMicrophone, PhoneDownsampler } from '../integrations/phone-bridge/media.mjs';
import { createPhoneBridge, validateBridgeConfig, stopOriginalCall } from '../integrations/phone-bridge/server.mjs';
import { StreamingPcmEncoder } from '../app/static/duplex-microphone.js';
import { PhoneOwnershipJournal } from '../integrations/phone-bridge/ownership.mjs';

const org = '11111111-1111-4111-8111-111111111111', number = '22222222-2222-4222-8222-222222222222';
const id = '01a15cd1-dc69-7ee7-95a6-244ec68909db', inner = '33333333-3333-4333-8333-333333333333';
const config = { enabled: true, organizationId: org, phoneNumberId: number,
  webhookSecret: 'synthetic-phone-secret-'.repeat(3), vapiKey: 'synthetic-vapi-key',
  consoleUrl: 'http://127.0.0.1:8785', sipHost: 'bridge.example.invalid', sipPort: 5061 };
const call = (change = {}) => ({ id, type: 'inboundPhoneCall', orgId: org, phoneNumberId: number, status: 'ringing', ...change });
const samples = (count, value = 1000) => {
  const bytes = Buffer.alloc(count * 2);
  for (let i = 0; i < count; i++) bytes.writeInt16LE(value, i * 2);
  return bytes;
};
const identity = token => audioSocketFrame(1, Buffer.from(token.replaceAll('-', ''), 'hex'));

test('AudioSocket parses fragmented/coalesced identity, PCM and hangup without mixing channels', () => {
  const seen = [], parser = new AudioSocketParser({ onIdentity: token => { seen.push(token); return true; }, onAudio: data => seen.push(data), onHangup: () => seen.push('end') });
  const encoded = Buffer.concat([identity(inner), audioSocketFrame(0x10, samples(160)), audioSocketFrame(0)]);
  for (let offset = 0; offset < encoded.length; offset += 7) parser.push(encoded.subarray(offset, offset + 7));
  assert.equal(seen[0], inner); assert.deepEqual(seen[1], samples(160)); assert.equal(seen[2], 'end');
});

for (const [name, bytes] of [
  ['audio before identity', audioSocketFrame(0x10, samples(160))],
  ['invalid UUID length', audioSocketFrame(1, Buffer.alloc(15))],
  ['oversized announced payload', Buffer.from([0x10, 0xff, 0xff])],
  ['duplicate identity', Buffer.concat([identity(inner), identity(inner)])],
  ['wrong negotiated rate', Buffer.concat([identity(inner), audioSocketFrame(0x12, samples(160))])],
  ['odd PCM length', Buffer.concat([identity(inner), audioSocketFrame(0x10, Buffer.alloc(3))])],
  ['data after hangup', Buffer.concat([identity(inner), audioSocketFrame(0), audioSocketFrame(0x10, samples(160))])],
]) test(`AudioSocket rejects ${name}`, () => {
  const parser = new AudioSocketParser({ onIdentity: () => true, onAudio() {} }); assert.throws(() => parser.push(bytes));
});

test('AudioSocket rejects unknown capability before forwarding any caller bytes', () => {
  let count = 0;
  const parser = new AudioSocketParser({ onIdentity: () => false, onAudio: () => count++ });
  assert.throws(() => parser.push(Buffer.concat([identity(inner), audioSocketFrame(0x10, samples(160))])));
  assert.equal(count, 0);
});

test('AudioSocket EOF exposes incomplete frame rather than accepting partial PCM', () => {
  const parser = new AudioSocketParser({ onIdentity: () => true, onAudio() {} });
  parser.push(identity(inner)); parser.push(Buffer.from([0x10, 0, 8, 1])); assert.throws(() => parser.end());
});

test('one-use phone admission binds UUIDv7 provider identity and one downstream call', () => {
  const admissions = new PhoneAdmissions({ organizationId: org, phoneNumberId: number });
  const grant = admissions.issue(call()); assert.equal(admissions.issue(call()), grant);
  assert.equal(admissions.consume(grant.token), grant); assert.throws(() => admissions.consume(grant.token));
  admissions.bindInner(grant, inner); assert.throws(() => admissions.bindInner(grant, inner));
  assert.equal(grant.outerCallId, id); assert.equal(grant.innerCallId, inner);
  admissions.end(grant); assert.throws(() => admissions.issue(call()));
});

for (const [name, change] of Object.entries({ organization: { orgId: inner }, number: { phoneNumberId: inner },
  outbound: { type: 'outboundPhoneCall' }, ended: { status: 'ended' }, invalid_id: { id: 'untrusted' } })) {
  test(`phone admission rejects ${name} before media allocation`, () => {
    const admissions = new PhoneAdmissions({ organizationId: org, phoneNumberId: number });
    assert.throws(() => admissions.issue(call(change))); assert.equal(admissions.calls.size, 0);
  });
}

test('admission deadline does not roll forward on duplicate webhook and capacity is bounded', () => {
  let now = 0; const admissions = new PhoneAdmissions({ organizationId: org, phoneNumberId: number, now: () => now });
  const grant = admissions.issue(call()); now = 14999; assert.equal(admissions.issue(call()).expiresAt, 15000);
  assert.throws(() => admissions.issue(call({ id: inner })));
  now = 15000; assert.throws(() => admissions.consume(grant.token));
  assert.equal(admissions.issue(call({ id: inner })).state, 'issued');
});

test('complete cue and main share one immutable sample barrier, with zero mixed samples', async () => {
  const playback = new PhonePlayback({ cueAssets: { moment: { pcm: samples(16800, 1000), sampleRate: 16000 } } });
  const cue = playback.playCue('moment'); assert.ok(cue);
  const output = [playback.render(160)];
  assert.equal(playback.enqueueMain({ pcm: samples(800, 2000), sampleRate: 16000 }), true);
  const first = playback.takeHandoffTelemetry(); assert.equal(first.hold_ms, 1040);
  assert.equal(playback.enqueueMain({ pcm: samples(800, 3000), sampleRate: 16000 }), true);
  assert.equal(playback.takeHandoffTelemetry(), null, 'Later PCM does not move the first-main barrier');
  for (let i = 0; i < 114; i++) output.push(playback.render(160));
  await cue.ended;
  const result = Buffer.concat(output);
  assert.deepEqual(result.subarray(0, 33600), samples(16800, 1000));
  assert.deepEqual(result.subarray(33600, 35200), samples(800, 2000));
  assert.deepEqual(result.subarray(35200, 36800), samples(800, 3000));
  assert.equal(playback.main.size, 0);
});

test('phone rejects overlong cues and bounded main queue never grows through a held cue', () => {
  const playback = new PhonePlayback({ cueAssets: { long: { pcm: samples(17601), sampleRate: 16000 } } });
  assert.equal(playback.playCue('long'), null);
  assert.equal(playback.enqueueMain({ pcm: samples(48000), sampleRate: 16000 }), true);
  assert.equal(playback.enqueueMain({ pcm: samples(1), sampleRate: 16000 }), false);
  playback.stopMain(); assert.equal(playback.queuedBytes, 0);
  const held = new PhonePlayback({ cueAssets: { moment: { pcm: samples(16800), sampleRate: 16000 } } });
  held.playCue('moment'); assert.equal(held.enqueueMain({ pcm: samples(31200), sampleRate: 16000 }), true);
  assert.equal(held.enqueueMain({ pcm: samples(1), sampleRate: 16000 }), false, 'The3s lead cap includes the complete cue remainder');
});

test('confirmed caller interruption clears active cue and every held main sample', async () => {
  const playback = new PhonePlayback({ cueAssets: { moment: { pcm: samples(16800), sampleRate: 16000 } } });
  const cue = playback.playCue('moment'); playback.render();
  playback.enqueueMain({ pcm: samples(800, 2000), sampleRate: 16000 });
  cue.stop('caller_started'); playback.stopMain(); await cue.ended;
  assert.deepEqual(playback.render(), samples(160, 0)); assert.equal(playback.main.size, 0);
});

test('provisional pause/resume preserves every queued main suffix and cue barrier', () => {
  const playback = new PhonePlayback({ cueAssets: { moment: { pcm: samples(320, 1000), sampleRate: 16000 } } });
  playback.playCue('moment'); playback.enqueueMain({ pcm: samples(480, 2000), sampleRate: 16000 });
  const barrier = playback.handoff.barrierSample; playback.pauseMain();
  assert.deepEqual(playback.render(), samples(160, 1000)); assert.equal(playback.handoff.barrierSample, barrier);
  assert.equal(playback.resumeMain(), true);
  assert.deepEqual(playback.render(), samples(160, 1000));
  assert.deepEqual(playback.render(), samples(160, 2000));
  playback.pauseMain(); assert.deepEqual(playback.render(), samples(160, 0)); playback.resumeMain();
  assert.deepEqual(playback.render(320), samples(320, 2000));
});

test('continuous caller input reaches the existing20ms encoder during both cue and main output', async () => {
  const frames = [], microphone = new PhoneMicrophone({ inputSampleRate: 16000, onFrame: frame => frames.push(frame) });
  const playback = new PhonePlayback({ cueAssets: { moment: { pcm: samples(640), sampleRate: 16000 } } });
  await microphone.start(); playback.playCue('moment');
  for (let i = 0; i < 8; i++) { microphone.push(samples(320)); microphone.tick(); microphone.tick(); playback.render(320); }
  playback.enqueueMain({ pcm: samples(640), sampleRate: 16000 });
  for (let i = 0; i < 8; i++) { microphone.push(samples(320)); microphone.tick(); microphone.tick(); playback.render(320); }
  assert.equal(frames.length, 16); assert.equal(frames.reduce((sum, frame) => sum + frame.pcm.length, 0), 10240);
  await microphone.stop(); microphone.push(samples(320)); assert.equal(frames.length, 16);
});

test('telephone resampler is chunk invariant and attenuates above telephone Nyquist', () => {
  const input = Buffer.alloc(16000 * 2);
  for (let i = 0; i < 16000; i++) input.writeInt16LE(Math.round(Math.sin(2 * Math.PI * 6000 * i / 16000) * 10000), i * 2);
  const one = new PhoneDownsampler().push(input), split = new PhoneDownsampler();
  const pieces = []; for (let i = 0; i < input.length; i += 318) pieces.push(split.push(input.subarray(i, i + 318)));
  assert.deepEqual(Buffer.concat(pieces), one); assert.equal(one.length, 16000);
  let peak = 0; for (let i = 200; i < one.length; i += 2) peak = Math.max(peak, Math.abs(one.readInt16LE(i)));
  assert.ok(peak < 100, 'A6kHz input must not alias into the telephone band');
});

test('bridge config fails closed without opt-in, TLS SIP target and loopback operator API', () => {
  assert.equal(validateBridgeConfig(config).audioSampleRate, 8000);
  for (const change of [{ enabled: false }, { sipPort: 5060 }, { sipHost: '127.0.0.1' },
    { consoleUrl: 'https://remote.example.invalid' }, { consoleUrl: 'http://127.0.0.1:8785/path' }, { webhookSecret: '' }]) {
    assert.throws(() => validateBridgeConfig({ ...config, ...change }));
  }
});

test('Unix ingress accepts only the dedicated shared private socket path', () => {
  assert.equal(validateBridgeConfig({ ...config, httpSocketPath: '/run/servicevoice-phone/ingress.sock' }).httpSocketPath,
    '/run/servicevoice-phone/ingress.sock');
  for (const path of ['/tmp/world-readable.sock', '/run/servicevoice-phone/../other.sock', '\\\\.\\pipe\\other', '']) {
    assert.throws(() => validateBridgeConfig({ ...config, httpSocketPath: path }), /phone_unix_socket_scope/);
  }
});

test('outer leg termination verifies exact provider call and refuses off-provider control URL', async () => {
  const sent = [];
  const fetcher = async (url, options) => { sent.push([String(url), options?.method]); return {
    ok: true, json: async () => ({ ...call(), monitor: { controlUrl: 'https://attacker.invalid/control' } }),
  }; };
  assert.equal(await stopOriginalCall(id, config, fetcher), false); assert.equal(sent.length, 1);
  assert.equal(await stopOriginalCall(id, config, async () => ({ ok: true, json: async () => call({ status: 'ended' }) })), true);
});


class ReadySession {
  constructor({ callbacks, runtime }) { this.runtime = runtime; this.callbacks = callbacks; this.stops = 0; }
  async start() {
    this.microphone = this.runtime.microphoneFactory({ onFrame: () => { this.inputFrames = (this.inputFrames || 0) + 1; } });
    await this.microphone.start();
    this.playback = this.runtime.playbackFactory({ cueAssets: {} });
    this.callId = inner; this.active = true;
  }
  async stop() { this.active = false; this.stops++; await this.microphone?.stop(); this.playback?.close(); }
}
const provider = async (_url, options) => {
  if (options?.method === 'POST') provider.ended = true;
  return { ok: true, json: async () => options?.method === 'POST' ? {} : call({
    status: provider.ended ? 'ended' : 'ringing', monitor: { controlUrl: 'https://control.vapi.ai/owned' },
  }) };
};
test.beforeEach(() => { provider.ended = false; });
const ingress = (bridge, change = {}) => fetch(`http://127.0.0.1:${bridge.httpServer.address().port}/phone-bridge/ingress`, {
  method: 'POST', headers: { Authorization: `Bearer ${config.webhookSecret}` },
  body: JSON.stringify({ message: { type: 'assistant-request', call: call(change) } }),
});
const settle = async predicate => {
  for (let i = 0; i < 200; i++) { if (predicate()) return; await new Promise(resolve => setTimeout(resolve, 5)); }
  assert.fail('bounded_local_lifecycle_did_not_settle');
};

test('HTTP rejects bad authentication before provider reads and prewarms once before forwarding', async () => {
  let reads = 0;
  const bridge = createPhoneBridge(config, { Session: ReadySession, fetcher: async (...args) => { reads++; return provider(...args); } });
  await bridge.listen({ httpPort: 0, mediaPort: 0 });
  try {
    const bad = await fetch(`http://127.0.0.1:${bridge.httpServer.address().port}/phone-bridge/ingress`, { method: 'POST', headers: { Authorization: 'Bearer wrong' }, body: '{}' });
    assert.equal(bad.status, 401); assert.equal(reads, 0);
    const response = await ingress(bridge), result = await response.json();
    assert.equal(response.status, 200); assert.equal(reads, 1);
    assert.match(result.destination.sipUri, /^sip:[0-9a-f-]{36}@bridge\.example\.invalid:5061;transport=tls$/);
    assert.equal(bridge.sessions.size, 1);
    const owner = [...bridge.sessions.values()][0];
    assert.equal(owner.ready, true); assert.equal(owner.row.state, 'issued');
    assert.equal(owner.playback.sentSamples, 0, 'Prewarm queues greeting but sends no carrier audio before attachment');
    const again = await ingress(bridge); assert.equal(again.status, 200);
    assert.deepEqual(await again.json(), result); assert.equal(bridge.sessions.size, 1);
  } finally { await bridge.close(); }
});

test('TCP rejects unknown UUID before session/provider allocation', async () => {
  let allocations = 0;
  class Forbidden { constructor() { allocations++; throw new Error('must_not_allocate'); } }
  const bridge = createPhoneBridge(config, { Session: Forbidden, fetcher: async () => { throw new Error('no_network'); } });
  await bridge.listen({ httpPort: 0, mediaPort: 0 });
  try {
    await new Promise((resolve, reject) => {
      const socket = net.connect(bridge.mediaServer.address().port, '127.0.0.1', () => socket.write(identity(inner)));
      socket.on('error', reject); socket.on('close', resolve);
    });
    assert.equal(allocations, 0);
  } finally { await bridge.close(); }
});

test('ready media never drops first caller PCM and hangup terminates the exact owned legs', async () => {
  const observed = [];
  class InputSession extends ReadySession {
    async start() {
      await super.start(); const push = this.microphone.push.bind(this.microphone);
      this.inputBytes = 0;
      this.microphone.push = bytes => { this.inputBytes += bytes.length; push(bytes); };
    }
  }
  const bridge = createPhoneBridge(config, { Session: InputSession, onEvent: event => observed.push(event), fetcher: provider });
  await bridge.listen({ httpPort: 0, mediaPort: 0 });
  try {
    assert.equal((await ingress(bridge)).status, 200);
    const owner = [...bridge.sessions.values()][0], row = owner.row;
    await new Promise((resolve, reject) => {
      const socket = net.connect(bridge.mediaServer.address().port, '127.0.0.1', () => socket.write(Buffer.concat([
        identity(row.token), audioSocketFrame(0x10, samples(160, 0)), audioSocketFrame(0x10, samples(160, 0)),
      ])));
      let ticks = 0;
      socket.on('data', () => { if (++ticks === 6) socket.end(audioSocketFrame(0)); });
      socket.on('error', reject); socket.on('close', resolve);
    });
    await settle(() => bridge.sessions.size === 0);
    assert.equal(owner.session.inputBytes, 640, 'Every byte of the first two carrier frames reaches the streaming encoder');
    assert.ok(owner.session.inputFrames >= 2, 'The clock forwards both first carrier frames plus the interpolation tail during silence');
    assert.equal(owner.session.stops, 1); assert.equal(row.innerCallId, inner); assert.equal(row.state, 'ended');
    assert.ok(observed.some(event => event.kind === 'phone_outer_stop'));
  } finally { await bridge.close(); }
});

test('duplicate in-flight webhooks join one paid allocation and shutdown observes late allocation', async () => {
  let release, startSeen, allocated = 0, stops = 0;
  const wait = new Promise(resolve => { release = resolve; });
  const starting = new Promise(resolve => { startSeen = resolve; });
  class LateSession extends ReadySession {
    async start() { allocated++; startSeen(); await wait; await super.start(); }
    async stop() { stops++; await super.stop(); }
  }
  const bridge = createPhoneBridge(config, { Session: LateSession, fetcher: provider });
  await bridge.listen({ httpPort: 0, mediaPort: 0 });
  const first = ingress(bridge); await starting;
  const second = ingress(bridge); await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(allocated, 1); const row = [...bridge.sessions.values()][0].row;
  const shutdown = bridge.close();
  await settle(() => row.state === 'ended');
  let complete = false; shutdown.then(() => { complete = true; });
  await new Promise(resolve => setTimeout(resolve, 10)); assert.equal(complete, false);
  release(); await shutdown;
  assert.equal((await first).status, 503); assert.equal((await second).status, 503);
  assert.ok(stops >= 2, 'A later allocated leg is stopped again after its ID becomes known');
  assert.equal(row.innerCallId, null); assert.equal(allocated, 1);
});

test('prewarm greeting exceeding3s fails both legs instead of silently dropping queued audio', async () => {
  const events = [];
  class OverflowSession extends ReadySession {
    async start() { await super.start(); this.playback.enqueueMain({ pcm: samples(48001), sampleRate: 16000 }); }
  }
  const bridge = createPhoneBridge(config, { Session: OverflowSession, onEvent: event => events.push(event), fetcher: provider });
  await bridge.listen({ httpPort: 0, mediaPort: 0 });
  try {
    assert.equal((await ingress(bridge)).status, 503); await settle(() => bridge.sessions.size === 0);
    assert.ok(events.some(event => event.kind === 'phone_media_ended' && event.reason === 'phone_output_bound'));
    assert.throws(() => bridge.admissions.issue(call()), /already_used/);
  } finally { await bridge.close(); }
});

test('unclaimed admission expires at its original bound and cannot replay allocation', async () => {
  const events = [];
  const bridge = createPhoneBridge({ ...config, lifetimeMs: 1000 }, { Session: ReadySession, onEvent: event => events.push(event), fetcher: provider });
  await bridge.listen({ httpPort: 0, mediaPort: 0 });
  try {
    assert.equal((await ingress(bridge)).status, 200);
    const owner = [...bridge.sessions.values()][0];
    await new Promise(resolve => setTimeout(resolve, 1050));
    await settle(() => owner.row.state === 'ended');
    assert.ok(events.some(event => event.reason === 'phone_unclaimed_expired'));
    assert.equal((await ingress(bridge)).status, 503); assert.equal(bridge.sessions.size, 0);
  } finally { await bridge.close(); }
});

test('audible-start follows paced samples beyond leading padding and survives natural completion', async () => {
  const playback = new PhonePlayback({ cueAssets: { moment: { pcm: Buffer.concat([samples(160, 0), samples(160, 65)]), sampleRate: 16000 } } });
  const cue = playback.playCue('moment'); assert.equal(cue.hasAudiblyStarted(), false);
  playback.render(); assert.equal(cue.hasAudiblyStarted(), false);
  playback.render(); await cue.ended; assert.equal(cue.hasAudiblyStarted(), true);
  const silent = new PhonePlayback({ cueAssets: { moment: { pcm: samples(160, 64), sampleRate: 16000 } } });
  const empty = silent.playCue('moment'); silent.render(); await empty.ended; assert.equal(empty.hasAudiblyStarted(), false);
});

test('no-RTP pause advances silence, preserves the encoded speech tail and releases local speech', async () => {
  const frames = [], speech = [];
  const microphone = new PhoneMicrophone({ inputSampleRate: 8000, onFrame: frame => frames.push(frame), onSpeech: active => speech.push(active) });
  await microphone.start();
  for (let i = 0; i < 10; i++) { microphone.push(samples(160, 8000)); microphone.tick(); microphone.tick(); }
  assert.deepEqual(speech, [true]);
  const framesAtEnd = frames.length;
  // No further incoming PCM or zero RTP packet: only the owned media clock.
  for (let i = 0; i < 30; i++) microphone.tick();
  assert.deepEqual(speech, [true, false]);
  assert.ok(frames.length >= framesAtEnd + 14);
  const output = Buffer.concat(frames.map(frame => Buffer.from(frame.pcm)));
  const referenceInput = new Float32Array(4000); referenceInput.fill(8000 / 32768, 0, 1600);
  const reference = new StreamingPcmEncoder({ inputSampleRate: 8000 });
  assert.deepEqual(output, Buffer.concat(reference.push(referenceInput).map(frame => Buffer.from(frame.pcm))),
    'Paced speech plus no-RTP silence retains every sample and the FIR/interpolation tail in original order');
  assert.equal(microphone.queuedBytes, 0); assert.equal(microphone.silenceSamples, 2400);
  assert.equal(frames.at(-1).rms, 0); await microphone.stop();
});

test('health retains ending prewarm allocation ownership until exact late cleanup settles', async () => {
  let release, entered;
  const waiting = new Promise(resolve => { release = resolve; });
  const starting = new Promise(resolve => { entered = resolve; });
  class LateSession extends ReadySession {
    async start() { entered(); await waiting; await super.start(); }
  }
  const bridge = createPhoneBridge(config, { Session: LateSession, fetcher: provider });
  await bridge.listen({ httpPort: 0, mediaPort: 0 });
  let request;
  try {
    request = ingress(bridge); await starting;
    const owner = [...bridge.sessions.values()][0]; await owner.stop('test_end_before_allocation');
    assert.equal(bridge.sessions.size, 0);
    const health = async () => (await fetch(`http://127.0.0.1:${bridge.httpServer.address().port}/health`)).json();
    assert.equal((await health()).owned_calls, 1, 'Renewal cannot treat an unknown late allocation as idle');
    release(); await owner.started;
    assert.equal((await health()).owned_calls, 0);
    assert.equal((await request).status, 503);
  } finally { release(); await bridge.close(); await request; }
});

test('carrier burst is paced without compression and over120ms jitter fails explicitly', async () => {
  const frames = []; let overflow = 0;
  const microphone = new PhoneMicrophone({ inputSampleRate: 16000, onFrame: frame => frames.push(frame), onOverflow: () => overflow++ });
  await microphone.start(); microphone.push(samples(960, 1000));
  assert.equal(frames.length, 0);
  microphone.tick(); assert.equal(frames.length, 0);
  microphone.tick(); assert.equal(frames.length, 1);
  assert.equal(microphone.queuedBytes, 1280);
  assert.throws(() => microphone.push(samples(1600, 2000)), /phone_input_jitter_bound/);
  assert.equal(overflow, 1); assert.equal(microphone.queuedBytes, 1280);
  await microphone.stop();
});

test('end-call200 acknowledgment without terminal readback never proves the outer leg ended', async () => {
  let posts = 0;
  const fetcher = async (_url, options) => {
    if (options?.method === 'POST') posts++;
    return { ok: true, json: async () => call({ monitor: { controlUrl: 'https://control.vapi.ai/owned' } }) };
  };
  assert.equal(await stopOriginalCall(id, config, fetcher), false); assert.equal(posts, 1);
});

test('unknown allocation remains owned, blocks new admission and survives process restart', async () => {
  const events = [];
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'phone-ownership-')); fs.chmodSync(directory, 0o700);
  const ownershipFile = path.join(directory, 'ownership.json');
  class UnknownSession extends ReadySession {
    async start() { await this.runtime.fetch('/duplex/call', { method: 'POST' }); throw new Error('unreachable'); }
  }
  const fetcher = async (url, options) => {
    if (String(url).endsWith('/duplex/call')) throw new Error('synthetic unknown allocation');
    return provider(url, options);
  };
  const bridge = createPhoneBridge({ ...config, assistantId: inner, ownershipFile }, { Session: UnknownSession, fetcher, onEvent: event => events.push(event) });
  await bridge.listen({ httpPort: 0, mediaPort: 0 });
  try {
    assert.equal((await ingress(bridge)).status, 503);
    await settle(() => bridge.sessions.size === 0);
    await settle(() => events.some(event => event.kind === 'phone_ownership_unknown'));
    const health = await (await fetch(`http://127.0.0.1:${bridge.httpServer.address().port}/health`)).json();
    assert.equal(health.owned_calls, 1); assert.equal(health.ownership_unknown, 1); assert.equal(health.admission_paused, true);
    assert.equal((await ingress(bridge)).status, 503);
    const result = await bridge.close(); assert.equal(result.provider_ownership_verified, false);
    const saved = JSON.parse(fs.readFileSync(ownershipFile));
    assert.equal(saved.pending.length, 1); assert.equal(saved.pending[0].allocation_attempted, true);
    assert.equal(saved.pending[0].inner_call_id, null);
    assert.throws(() => new PhoneOwnershipJournal(ownershipFile), /unresolved_ownership/);
  } finally { await bridge.close(); fs.rmSync(directory, { recursive: true }); }
});

test('journal release requires both exact terminal legs and a known bound inner call', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'phone-ownership-')); fs.chmodSync(directory, 0o700);
  const filename = path.join(directory, 'ownership.json');
  const journal = new PhoneOwnershipJournal(filename);
  try {
    journal.reserve(id); journal.allocating(id);
    assert.throws(() => journal.release(id, { outerEnded: true, innerEnded: true }), /end_unverified/);
    journal.allocated(id, inner);
    assert.throws(() => journal.allocated(id, org), /ownership_binding/);
    assert.throws(() => journal.release(id, { outerEnded: true, innerEnded: false }), /end_unverified/);
    assert.throws(() => new PhoneOwnershipJournal(filename), /EEXIST/);
    journal.release(id, { outerEnded: true, innerEnded: true });
    assert.equal(JSON.parse(fs.readFileSync(filename)).pending.length, 0);
  } finally { journal.close(); fs.rmSync(directory, { recursive: true }); }
});

for (const innerEnded of [true, false]) test(`allocated inner call must read back ended before ownership release: ${innerEnded}`, async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'phone-ownership-')); fs.chmodSync(directory, 0o700);
  const ownershipFile = path.join(directory, 'ownership.json'), events = [];
  class AllocatedSession extends ReadySession {
    async start() { await this.runtime.fetch('/duplex/call', { method: 'POST' }); await super.start(); }
  }
  const fetcher = async (url, options) => {
    if (String(url).endsWith('/duplex/call')) return new Response(JSON.stringify({ call_id: inner }), { status: 200 });
    if (String(url).endsWith('/call/' + inner)) return { ok: true, json: async () => ({ id: inner, type: 'webCall',
      orgId: org, assistantId: inner, status: innerEnded ? 'ended' : 'in-progress' }) };
    return provider(url, options);
  };
  const bridge = createPhoneBridge({ ...config, assistantId: inner, ownershipFile }, { Session: AllocatedSession, fetcher, onEvent: event => events.push(event) });
  await bridge.listen({ httpPort: 0, mediaPort: 0 });
  try {
    assert.equal((await ingress(bridge)).status, 200);
    const before = JSON.parse(fs.readFileSync(ownershipFile)); assert.equal(before.pending[0].inner_call_id, inner);
    const owner = [...bridge.sessions.values()][0]; await owner.stop('owned_test_complete');
    assert.equal(JSON.parse(fs.readFileSync(ownershipFile)).pending.length, innerEnded ? 0 : 1);
    const health = await (await fetch(`http://127.0.0.1:${bridge.httpServer.address().port}/health`)).json();
    assert.equal(health.owned_calls, innerEnded ? 0 : 1); assert.equal(health.ownership_unknown, innerEnded ? 0 : 1);
    assert.ok(events.some(event => event.kind === (innerEnded ? 'phone_ownership_ended' : 'phone_ownership_unknown')));
  } finally { await bridge.close(); fs.rmSync(directory, { recursive: true }); }
});

test('natural session ended event joins its actual asynchronous cleanup before terminal readback', async () => {
  let innerEnded = false;
  class EndingSession extends ReadySession {
    async start() { await this.runtime.fetch('/duplex/call', { method: 'POST' }); await super.start(); }
    stop() {
      if (this.stopping) return this.stopping;
      if (this.closed) return;
      this.closed = true; this.callbacks.event({ kind: 'ended' });
      this.stopping = new Promise(resolve => setTimeout(async () => { await super.stop(); innerEnded = true; resolve(); }, 40));
      return this.stopping;
    }
  }
  const fetcher = async (url, options) => {
    if (String(url).endsWith('/duplex/call')) return new Response(JSON.stringify({ call_id: inner }), { status: 200 });
    if (String(url).endsWith('/call/' + inner)) return { ok: true, json: async () => ({ id: inner, type: 'webCall',
      orgId: org, assistantId: inner, status: innerEnded ? 'ended' : 'in-progress' }) };
    return provider(url, options);
  };
  const bridge = createPhoneBridge({ ...config, assistantId: inner }, { Session: EndingSession, fetcher });
  await bridge.listen({ httpPort: 0, mediaPort: 0 });
  try {
    assert.equal((await ingress(bridge)).status, 200);
    const owner = [...bridge.sessions.values()][0]; await owner.session.stop();
    await settle(() => owner.cleaned); await owner.reconcile();
    assert.equal(innerEnded, true);
    assert.equal((await (await fetch(`http://127.0.0.1:${bridge.httpServer.address().port}/health`)).json()).owned_calls, 0);
  } finally { await bridge.close(); }
});

test('only verified ended rows retire after grant expiry, with old token rejection and bounded long operation', () => {
  let now = 0; const admissions = new PhoneAdmissions({ organizationId: org, phoneNumberId: number, now: () => now });
  let oldToken;
  for (let i = 0; i < 600; i++) {
    const callId = `aaaaaaaa-aaaa-4aaa-8aaa-${i.toString(16).padStart(12, '0')}`;
    const row = admissions.issue(call({ id: callId }));
    oldToken ??= row.token; admissions.end(row); admissions.confirmEnded(row); now += 16000;
  }
  assert.equal(admissions.calls.size, 1); assert.equal(admissions.retired, 599);
  assert.throws(() => admissions.consume(oldToken), /admission_invalid/);
  const unverified = admissions.issue(call()); admissions.end(unverified); now += 30000;
  admissions.issue(call({ id: inner })); assert.equal(admissions.calls.get(id), unverified, 'Unknown outcomes never retire');
});

test('normal delayed provider terminal state automatically reconciles known IDs without replaying stop or allocation', async () => {
  let stopPosts = 0, readsAfterStop = 0;
  const events = [];
  const fetcher = async (_url, options) => {
    if (options?.method === 'POST') { stopPosts++; return { ok: true, json: async () => ({}) }; }
    if (stopPosts) readsAfterStop++;
    return { ok: true, json: async () => call({ status: readsAfterStop >= 4 ? 'ended' : 'in-progress',
      monitor: { controlUrl: 'https://control.vapi.ai/owned' } }) };
  };
  const bridge = createPhoneBridge(config, { Session: ReadySession, fetcher, onEvent: event => events.push(event) });
  await bridge.listen({ httpPort: 0, mediaPort: 0 });
  try {
    assert.equal((await ingress(bridge)).status, 200);
    const owner = [...bridge.sessions.values()][0]; await owner.stop('caller_ended');
    assert.equal(owner.verified, undefined); assert.equal(owner.unknown, true);
    assert.ok(events.some(event => event.kind === 'phone_ownership_retry_scheduled'));
    await settle(() => owner.verified === true);
    assert.equal(stopPosts, 1); assert.equal(owner.unknown, false); assert.equal(owner.reconcileRetries, 1);
    assert.equal((await (await fetch(`http://127.0.0.1:${bridge.httpServer.address().port}/health`)).json()).owned_calls, 0);
  } finally { await bridge.close(); }
});

test('canonical backend mode requires a separate control secret, loopback URL and durable ownership', () => {
  const safe = { ...config, assistantId: inner, ownershipFile: '/private/ownership.json',
    admissionUrl: 'http://127.0.0.1:8775', admissionSecret: 'synthetic-control-secret-'.repeat(3) };
  assert.equal(validateBridgeConfig(safe).admissionUrl, 'http://127.0.0.1:8775');
  for (const change of [{ admissionUrl: 'https://foreign.invalid' }, { admissionSecret: config.webhookSecret },
    { admissionSecret: '' }, { ownershipFile: undefined }, { admissionUrl: 'http://127.0.0.1:8775/private' }]) {
    assert.throws(() => validateBridgeConfig({ ...safe, ...change }), /phone_admission_configuration/);
  }
});

async function productionFixture(options = {}) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'phone-canonical-')); fs.chmodSync(directory, 0o700);
  const ownershipFile = path.join(directory, 'ownership.json'), order = [], events = [], observedStops = [];
  let outerEnded = false, backendClosed = options.backendClosed ?? true;
  class CanonicalSession extends ReadySession {
    async start() {
      this.session = await (await this.runtime.fetch('/api/sessions', { method: 'POST', body: '{}' })).json();
      await this.runtime.fetch('/duplex/call', { method: 'POST', body: '{}' });
      await super.start();
    }
    async stop() { observedStops.push({ id: this.callId, url: this.callUrl }); await super.stop(); }
  }
  const controlSecret = 'synthetic-distinct-control-'.repeat(3);
  const fetcher = async (url, settings = {}) => {
    const parsed = new URL(url);
    if (parsed.pathname.startsWith('/api/phone-bridge/')) {
      assert.equal(parsed.origin, 'http://127.0.0.1:8775');
      assert.equal(settings.headers.Authorization, 'Bearer ' + controlSecret);
      assert.equal(settings.redirect, 'error');
      const action = parsed.pathname.split('/').at(-1); order.push(action);
      if (action === 'admit') {
        assert.deepEqual(JSON.parse(settings.body), { outer_call_id: id });
        if (options.admitDelay) await new Promise(resolve => setTimeout(resolve, options.admitDelay));
        return Response.json({ id: 'sess-canonical', token: 'synthetic-capability-'.repeat(3), mode: 'live', outer_call_id: id,
          allocation_state: 'reserved', max_duration_seconds: 300, remaining_duration_ms: options.remaining ?? 300000 });
      }
      if (action === 'allocation') return Response.json({ allocation_reserved: true });
      if (action === 'bind') {
        assert.deepEqual(JSON.parse(settings.body), { inner_call_id: inner });
        return Response.json({ bound: true }, { status: options.bindFailure ? 503 : 200 });
      }
      if (action === 'close') return Response.json({ closed: backendClosed, outer_ended: true, inner_ended: backendClosed });
      assert.fail('Unexpected private control route');
    }
    if (parsed.pathname.endsWith('/duplex/call')) {
      order.push('paid');
      const saved = JSON.parse(fs.readFileSync(ownershipFile));
      assert.equal(saved.pending[0].allocation_attempted, true, 'Local intent exists before paid allocation');
      assert.equal(order.at(-2), 'allocation', 'Authoritative reservation precedes paid allocation');
      if (options.unknownAllocation) throw new Error('synthetic allocation response lost');
      return Response.json({ call_id: inner, websocket_url: 'wss://owned.vapi.ai/transport' });
    }
    if (parsed.pathname === '/call/' + inner) return Response.json({ id: inner, orgId: org, assistantId: inner, type: 'webCall', status: 'ended' });
    if (settings.method === 'POST') { outerEnded = true; return Response.json({}); }
    return Response.json(call({ status: outerEnded ? 'ended' : 'ringing', monitor: { controlUrl: 'https://control.vapi.ai/owned' } }));
  };
  const bridge = createPhoneBridge({ ...config, assistantId: inner, ownershipFile,
    admissionUrl: 'http://127.0.0.1:8775', admissionSecret: controlSecret }, {
    Session: CanonicalSession, fetcher, onEvent: event => events.push(event),
  });
  await bridge.listen({ httpPort: 0, mediaPort: 0 });
  return { bridge, ownershipFile, order, events, observedStops, setBackendClosed: value => { backendClosed = value; },
    async close() { await bridge.close(); fs.rmSync(directory, { recursive: true }); } };
}

test('phone uses one canonical backend session and mark-before-call then exact bind ordering', async () => {
  const fixture = await productionFixture();
  try {
    assert.equal((await ingress(fixture.bridge)).status, 200);
    assert.deepEqual(fixture.order.slice(0, 4), ['admit', 'allocation', 'paid', 'bind']);
    const owner = [...fixture.bridge.sessions.values()][0];
    assert.equal(owner.session.session.id, 'sess-canonical');
    await owner.stop('synthetic_complete');
    assert.equal(owner.verified, true); assert.equal(fixture.order.filter(x => x === 'paid').length, 1);
    assert.equal(JSON.parse(fs.readFileSync(fixture.ownershipFile)).pending.length, 0);
    assert.doesNotMatch(JSON.stringify(fixture.events), /synthetic-capability|synthetic-distinct-control|synthetic-vapi-key/);
  } finally { await fixture.close(); }
});

test('both provider legs ended cannot release canonical ownership before backend closure proof', async () => {
  const fixture = await productionFixture({ backendClosed: false });
  try {
    assert.equal((await ingress(fixture.bridge)).status, 200);
    const owner = [...fixture.bridge.sessions.values()][0]; await owner.stop('synthetic_complete');
    assert.equal(owner.unknown, true); assert.equal(owner.verified, undefined);
    assert.equal(JSON.parse(fs.readFileSync(fixture.ownershipFile)).pending.length, 1);
    fixture.setBackendClosed(true); await owner.reconcile();
    assert.equal(owner.verified, true); assert.equal(JSON.parse(fs.readFileSync(fixture.ownershipFile)).pending.length, 0);
    assert.equal(fixture.order.filter(x => x === 'allocation').length, 1); assert.equal(fixture.order.filter(x => x === 'paid').length, 1);
  } finally { await fixture.close(); }
});

test('unknown paid response never resubmits allocation and preserves canonical capacity ownership', async () => {
  const fixture = await productionFixture({ unknownAllocation: true, backendClosed: false });
  try {
    assert.equal((await ingress(fixture.bridge)).status, 503);
    await settle(() => fixture.events.some(x => x.kind === 'phone_ownership_unknown'));
    assert.equal(fixture.order.filter(x => x === 'allocation').length, 1);
    assert.equal(fixture.order.filter(x => x === 'paid').length, 1); assert.equal(fixture.order.filter(x => x === 'bind').length, 0);
    const pending = JSON.parse(fs.readFileSync(fixture.ownershipFile)).pending[0];
    assert.equal(pending.allocation_attempted, true); assert.equal(pending.inner_call_id, null);
    assert.equal((await ingress(fixture.bridge)).status, 503);
  } finally { await fixture.close(); }
});

test('failed authoritative bind retains returned inner identity for ordinary owned cleanup', async () => {
  const fixture = await productionFixture({ bindFailure: true, backendClosed: false });
  try {
    assert.equal((await ingress(fixture.bridge)).status, 503);
    await settle(() => fixture.observedStops.length > 0);
    assert.equal(fixture.observedStops[0].id, inner); assert.equal(fixture.observedStops[0].url, 'wss://owned.vapi.ai/transport');
    assert.equal(fixture.order.filter(x => x === 'paid').length, 1);
    assert.equal(JSON.parse(fs.readFileSync(fixture.ownershipFile)).pending[0].inner_call_id, inner);
  } finally { await fixture.close(); }
});

test('outer lifetime subtracts the whole admission roundtrip from remaining duration', async () => {
  const fixture = await productionFixture({ admitDelay: 45, remaining: 500 });
  try {
    assert.equal((await ingress(fixture.bridge)).status, 200);
    const admitted = fixture.events.find(x => x.kind === 'phone_backend_admitted');
    assert.ok(admitted.remaining_duration_ms > 0 && admitted.remaining_duration_ms <= 465,
      'Returned remaining lifetime is reduced by time already spent awaiting admission');
  } finally { await fixture.close(); }
});
