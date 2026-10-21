import assert from 'node:assert/strict';
import { test } from 'node:test';
import fs from 'node:fs/promises';
import vm from 'node:vm';
import { StreamingPcmEncoder, EnergySpeechDetector, DuplexMicrophone } from '../app/static/duplex-microphone.js';

function encode(samples, rate, sizes = [128]) {
  const encoder = new StreamingPcmEncoder({ inputSampleRate: rate }), frames = [];
  let offset = 0, chunk = 0;
  while (offset < samples.length) {
    const length = sizes[chunk++ % sizes.length];
    frames.push(...encoder.push(samples.slice(offset, offset + length))); offset += length;
  }
  return frames;
}
const bytes = frames => Buffer.concat(frames.map(frame => frame.pcm));
function tone(rate, frequency, seconds = 1) {
  return Float32Array.from({ length: Math.round(rate * seconds) }, (_, i) => 0.5 * Math.sin(2 * Math.PI * frequency * i / rate));
}
function decodedRms(frames, skip = 3) {
  const value = bytes(frames.slice(skip)); let energy = 0;
  for (let i = 0; i < value.length; i += 2) energy += (value.readInt16LE(i) / 32768) ** 2;
  return Math.sqrt(energy / (value.length / 2));
}

test('sample-rate conversion preserves duration and pitch at 16, 44.1, 48 and 96 kHz', () => {
  for (const rate of [16000, 44100, 48000, 96000]) {
    const frames = encode(tone(rate, 1000), rate, [128, 47, 1024, 333]);
    assert.equal(frames.length, 50, `${rate} source must produce exactly one second`);
    assert.ok(frames.every(frame => frame.pcm.length === 640 && frame.sampleRate === 16000 && frame.durationMs === 20));
    assert.deepEqual(frames.map(frame => frame.sequence), Array.from({ length: 50 }, (_, i) => i));
    const output = bytes(frames.slice(3)); let crossings = 0;
    for (let i = 2; i < output.length; i += 2) if (output.readInt16LE(i - 2) <= 0 && output.readInt16LE(i) > 0) crossings++;
    assert.ok(Math.abs(crossings / 0.94 - 1000) < 3, `${rate} source changed the tone frequency`);
    assert.ok(Math.abs(decodedRms(frames) - Math.sqrt(0.125)) < 0.008, `${rate} source changed level`);
  }
});

test('resampling is identical across arbitrary AudioWorklet chunk boundaries', () => {
  const input = tone(44100, 2357, 0.5);
  assert.deepEqual(bytes(encode(input, 44100, [2048])), bytes(encode(input, 44100, [1, 17, 128, 349, 65])));
});

test('downsampling rejects out-of-band energy rather than aliasing it as caller speech', () => {
  const ordinary = decodedRms(encode(tone(48000, 3000), 48000));
  const outOfBand = decodedRms(encode(tone(48000, 12000), 48000));
  assert.ok(ordinary > 0.3); assert.ok(outOfBand < ordinary * 0.02, `${outOfBand} leaked after conversion`);
});

test('PCM is signed little endian, safely clamped and nonfinite samples become silence', () => {
  const encoder = new StreamingPcmEncoder({ inputSampleRate: 16000 });
  const input = new Float32Array(320); input.set([-2, -1, 0, 1, 2, NaN, Infinity]);
  const frame = encoder.push(input)[0], view = new DataView(frame.pcm.buffer);
  assert.deepEqual(Array.from({ length: 7 }, (_, i) => view.getInt16(i * 2, true)), [-32768, -32768, 0, 32767, 32767, 0, 0]);
  assert.throws(() => encoder.push(new Float32Array(8193)), /input-block/);
  assert.throws(() => new StreamingPcmEncoder({ inputSampleRate: 16000, outputSampleRate: 24000 }), /geometry/);
});

test('continuous silence produces continuous 20 ms PCM frames without muting reception', () => {
  const frames = encode(new Float32Array(48000), 48000);
  assert.equal(frames.length, 50); assert.ok(frames.every(frame => frame.rms === 0 && frame.pcm.every(byte => byte === 0)));
});

test('energy gate requires sustained activity and a release window; it labels its evidence', () => {
  const changes = [], detector = new EnergySpeechDetector({ onActivity: (...args) => changes.push(args) });
  const frame = (rms, sequence) => ({ rms, peak: rms, durationMs: 20, sequence });
  detector.push(frame(0.1, 0)); detector.push(frame(0, 1)); assert.equal(changes.length, 0);
  detector.push(frame(0.1, 2)); detector.push(frame(0.1, 3)); assert.equal(changes[0][0], true);
  for (let i = 0; i < 10; i++) detector.push(frame(0, i + 4));
  assert.equal(changes.length, 1);
  detector.push(frame(0, 14)); assert.equal(changes[1][0], false);
  assert.ok(changes.every(([, info]) => info.source === 'local_energy_heuristic'));
});

function fixture({ mediaPromise, modulePromise, ownStream = true } = {}) {
  const track = { enabled: true, readyState: 'live', stopped: 0, handlers: new Map(),
    stop() { this.stopped++; this.readyState = 'ended'; },
    addEventListener(name, handler) { this.handlers.set(name, handler); }, removeEventListener(name) { this.handlers.delete(name); } };
  const stream = { getAudioTracks: () => [track], getTracks: () => [track] };
  const makeNode = () => ({ disconnected: false, connect() {}, disconnect() { this.disconnected = true; } });
  const port = { sent: [], onmessage: null, closed: false, postMessage(message) { this.sent.push(message); }, close() { this.closed = true; } };
  const node = { ...makeNode(), port };
  const context = { sampleRate: 16000, state: 'running', destination: {}, closed: false,
    audioWorklet: { addModule: async () => modulePromise }, createMediaStreamSource: () => makeNode(),
    createGain: () => ({ ...makeNode(), gain: { value: 1 } }), close() { this.closed = true; } };
  const frames = [], speech = [], errors = [];
  const microphone = new DuplexMicrophone({ audioContext: context, onFrame: frame => frames.push(frame),
    onSpeech: (...args) => speech.push(args), onError: error => errors.push(error.message),
    runtime: { getUserMedia: async () => mediaPromise ? await mediaPromise : stream, makeWorklet: () => node } });
  return { track, stream, node, context, frames, speech, errors, microphone, ownStream,
    send(samples, epoch = microphone.epoch) { port.onmessage?.({ data: { samples, epoch } }); } };
}

test('microphone keeps forwarding during output, mute emits zeros and unmutes reject old worklet epochs', async () => {
  const f = fixture(); await f.microphone.start();
  assert.equal(f.microphone.sink.gain.value, 0, 'microphone is not monitored to speaker');
  f.send(new Float32Array(320).fill(0.2)); f.send(new Float32Array(320).fill(0.2));
  assert.equal(f.frames.length, 2); assert.equal(f.speech[0][0], true);
  f.microphone.setMuted(true); assert.equal(f.track.enabled, false); assert.equal(f.speech.at(-1)[0], false);
  f.send(new Float32Array(320).fill(0.7)); assert.equal(f.frames.length, 3); assert.ok(f.frames[2].pcm.every(byte => byte === 0));
  f.microphone.setMuted(false); assert.equal(f.track.enabled, true);
  f.send(new Float32Array(320).fill(0.9), 0); assert.equal(f.frames.length, 3, 'premute queued block is obsolete');
  f.send(new Float32Array(320).fill(0.3)); assert.equal(f.frames.length, 4);
  await f.microphone.stop(); assert.equal(f.track.stopped, 1); assert.equal(f.context.closed, false);
  assert.equal(f.node.port.closed, true); assert.equal(f.errors.length, 0);
});

test('stop while permission is pending stops late acquired tracks and never allocates capture nodes', async () => {
  let resolveMedia; const media = new Promise(resolve => { resolveMedia = resolve; });
  const f = fixture({ mediaPromise: media });
  const starting = f.microphone.start(); await f.microphone.stop(); resolveMedia(f.stream);
  await assert.rejects(starting, /cancelled/); assert.equal(f.track.stopped, 1); assert.equal(f.microphone.node, null);
});

test('borrowed input remains owned by caller and capture processor failure cleans up', async () => {
  const f = fixture(); await f.microphone.start({ stream: f.stream, ownStream: false });
  f.node.onprocessorerror(); assert.deepEqual(f.errors, ['microphone-processor-failed']);
  assert.equal(f.microphone.closed, true); assert.equal(f.track.stopped, 0); assert.equal(f.track.handlers.size, 0);
});

test('AudioWorklet copies input, emits epoch-labelled blocks and zeroes its own output', async () => {
  const source = await fs.readFile(new URL('../app/static/duplex-capture-worklet.js', import.meta.url), 'utf8');
  let Processor; const messages = [];
  vm.runInNewContext(source, { AudioWorkletProcessor: class { constructor() { this.port = { postMessage: value => messages.push(value) }; } },
    Float32Array, registerProcessor(name, processor) { assert.equal(name, 'servicevoice-duplex-capture'); Processor = processor; } });
  const processor = new Processor(), input = new Float32Array([0.25, -0.5]), output = new Float32Array([1, 1]);
  assert.equal(processor.process([[input]], [[output]]), true);
  assert.deepEqual(Array.from(messages[0].samples), [0.25, -0.5]); assert.deepEqual(Array.from(output), [0, 0]);
  input[0] = 0.9; assert.equal(messages[0].samples[0], 0.25);
  processor.port.onmessage({ data: { type: 'epoch', epoch: 4, muted: true } });
  processor.process([[input]], [[output]]); assert.equal(messages[1].epoch, 4); assert.ok(messages[1].samples.every(x => x === 0));
});
