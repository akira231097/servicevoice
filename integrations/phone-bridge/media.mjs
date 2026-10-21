// AudioSocket is a private Asterisk media leg, never an unauthenticated public API.
import { randomUUID } from 'node:crypto';
import { StreamingPcmEncoder, EnergySpeechDetector } from '../../app/static/duplex-microphone.js';

export const SAMPLE_RATE = 16000;
export const FRAME_SAMPLES = 160; // Ten milliseconds; never enqueue a whole phrase in TCP.
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const pcm = value => {
  if (!(value instanceof ArrayBuffer) && !ArrayBuffer.isView(value)) throw new Error('pcm_type');
  const bytes = value instanceof ArrayBuffer ? Buffer.from(value) : Buffer.from(value.buffer, value.byteOffset, value.byteLength);
  if (!bytes.length || bytes.length % 2) throw new Error('pcm_geometry');
  return Buffer.from(bytes);
};

export function audioSocketFrame(type, payload = Buffer.alloc(0)) {
  if (!Buffer.isBuffer(payload) || payload.length > 4096) throw new Error('audiosocket_payload');
  const header = Buffer.alloc(3); header[0] = type; header.writeUInt16BE(payload.length, 1);
  return Buffer.concat([header, payload]);
}

export class AudioSocketParser {
  constructor({ sampleRate = 8000, onIdentity, onAudio, onHangup = () => {}, onDtmf = () => {} } = {}) {
    if (![8000, 16000].includes(sampleRate) || typeof onIdentity !== 'function' || typeof onAudio !== 'function') throw new Error('audiosocket_configuration');
    this.audioType = sampleRate === 8000 ? 0x10 : 0x12;
    this.onIdentity = onIdentity; this.onAudio = onAudio; this.onHangup = onHangup; this.onDtmf = onDtmf;
    this.pending = Buffer.alloc(0); this.identified = false; this.closed = false;
  }
  push(data) {
    if (this.closed) throw new Error('audiosocket_after_close');
    if (!Buffer.isBuffer(data) || data.length > 65536) throw new Error('audiosocket_input_bound');
    this.pending = Buffer.concat([this.pending, data]);
    while (this.pending.length >= 3) {
      const type = this.pending[0], length = this.pending.readUInt16BE(1);
      if (length > 4096) throw new Error('audiosocket_frame_bound');
      if (this.pending.length < length + 3) break;
      const payload = this.pending.subarray(3, length + 3);
      this.pending = this.pending.subarray(length + 3);
      if (!this.identified) {
        if (type !== 0x01 || length !== 16) throw new Error('audiosocket_identity_required');
        const hex = payload.toString('hex');
        const id = `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
        if (!uuid.test(id) || this.onIdentity(id) !== true) throw new Error('audiosocket_identity_rejected');
        this.identified = true;
      } else if (type === this.audioType && length > 0 && length % 2 === 0) this.onAudio(Buffer.from(payload));
      else if (type === 0 && length === 0) {
        this.closed = true;
        if (this.pending.length) throw new Error('audiosocket_after_close');
        this.onHangup(); return;
      } else if (type === 3 && length === 1 && /^[0-9*#ABCD]$/.test(payload.toString('ascii'))) this.onDtmf();
      else throw new Error('audiosocket_unsupported_frame');
    }
  }
  end() { if (this.pending.length) throw new Error('audiosocket_truncated_frame'); this.closed = true; }
}

export class PhoneAdmissions {
  constructor({ organizationId, phoneNumberId, now = () => performance.now(), lifetimeMs = 15000, capacity = 1 } = {}) {
    if (!uuid.test(organizationId || '') || !uuid.test(phoneNumberId || '') || !Number.isInteger(capacity) || capacity < 1 || capacity > 4
      || !Number.isInteger(lifetimeMs) || lifetimeMs < 1000 || lifetimeMs > 30000) throw new Error('phone_admission_configuration');
    this.organizationId = organizationId; this.phoneNumberId = phoneNumberId; this.now = now;
    this.lifetimeMs = lifetimeMs; this.capacity = capacity; this.calls = new Map(); this.tokens = new Map();
  }
  issue(call) {
    // The HTTP boundary authenticates the separate provider webhook before here.
    if (call?.type !== 'inboundPhoneCall' || !uuid.test(call.id || '') || call.orgId !== this.organizationId
      || call.phoneNumberId !== this.phoneNumberId || call.status === 'ended' || call.endedAt) throw new Error('phone_identity_rejected');
    const existing = this.calls.get(call.id);
    if (existing) {
      if (existing.state === 'issued' && this.now() < existing.expiresAt) return existing;
      throw new Error('phone_admission_already_used');
    }
    for (const row of this.calls.values()) if (row.state === 'issued' && row.expiresAt <= this.now()) row.state = 'expired';
    if ([...this.calls.values()].filter(row => ['issued', 'active'].includes(row.state)).length >= this.capacity) throw new Error('phone_capacity');
    if (this.calls.size >= 256) throw new Error('phone_admission_ledger_bound');
    const row = { outerCallId: call.id, token: randomUUID(), expiresAt: this.now() + this.lifetimeMs, state: 'issued', innerCallId: null };
    this.calls.set(call.id, row); this.tokens.set(row.token, row); return row;
  }
  consume(token) {
    const row = this.tokens.get(token);
    if (!row || row.state !== 'issued' || this.now() >= row.expiresAt) throw new Error('phone_admission_invalid');
    row.state = 'active'; return row;
  }
  bindInner(row, innerCallId) {
    if (this.calls.get(row.outerCallId) !== row || row.state !== 'active' || row.innerCallId || !uuid.test(innerCallId || '')) throw new Error('phone_inner_binding');
    row.innerCallId = innerCallId;
  }
  end(row) { if (this.calls.get(row?.outerCallId) === row) row.state = 'ended'; }
}

// Use the tested speech-turn adapter with a server clock and a private PCM input.
// This is not a WebAudio implementation: all output goes through PhonePlayback.
export class PhoneOutputClock {
  constructor() { this.state = 'running'; this.sampleRate = SAMPLE_RATE; this.began = performance.now(); }
  get currentTime() { return (performance.now() - this.began) / 1000; }
  async resume() { if (this.state === 'closed') throw new Error('phone_clock_closed'); }
  async close() { this.state = 'closed'; }
}

export class PhonePlayback {
  constructor({ cueAssets = {}, maxQueuedSeconds = 3, maximumCueMs = 1100, onEvent = () => {}, onOverflow = () => {} } = {}) {
    if (!(maxQueuedSeconds > 0 && maxQueuedSeconds <= 3) || !(maximumCueMs > 0 && maximumCueMs <= 1100)) throw new Error('phone_output_configuration');
    this.cueAssets = cueAssets; this.maxBytes = maxQueuedSeconds * SAMPLE_RATE * 2;
    this.maximumCueMs = maximumCueMs; this.onEvent = onEvent;
    this.onOverflow = onOverflow;
    this.main = new Set(); this.cue = null; this.pausedMain = null; this.closed = false; this.queuedBytes = 0;
    this.sentSamples = 0; this.handoff = null;
    this.handoffTelemetry = null;
  }
  playCue(phraseId) {
    if (this.closed || this.cue || this.main.size || this.pausedMain) return null;
    const asset = this.cueAssets[phraseId];
    if (!asset || asset.sampleRate !== SAMPLE_RATE) return null;
    const data = pcm(asset.pcm);
    if (data.length / 32 > this.maximumCueMs) return null;
    let resolve;
    const ended = new Promise(done => { resolve = done; });
    const firstAudibleByte = (() => { for (let i = 0; i < data.length; i += 2) if (Math.abs(data.readInt16LE(i)) > 64) return i; return Infinity; })();
    const handle = { data, offset: 0, ended, hasAudiblyStarted: () => handle.offset > firstAudibleByte,
      stop: () => {
      if (this.cue === handle) { this.cue = null; this.handoff = null; resolve(); }
    }, finish: resolve };
    this.cue = handle; return handle;
  }
  enqueueMain({ pcm: value, sampleRate }) {
    if (this.closed || this.pausedMain || sampleRate !== SAMPLE_RATE) return false;
    const data = pcm(value);
    const cueRemainder = this.cue ? this.cue.data.length - this.cue.offset : 0;
    if (cueRemainder + this.queuedBytes + data.length > this.maxBytes) {
      this.onOverflow(); return false;
    }
    if (this.cue && !this.handoff) {
      const remaining = (this.cue.data.length - this.cue.offset) / 2;
      this.handoff = { barrierSample: this.sentSamples + remaining, remainingMs: remaining / 16 };
      this.handoffTelemetry = { ready_at_s: this.sentSamples / SAMPLE_RATE,
        scheduled_at_s: this.handoff.barrierSample / SAMPLE_RATE, hold_ms: remaining / 16 };
      this.onEvent({ kind: 'phone_main_handoff', remaining_ms: remaining / 16 });
    }
    this.main.add({ data, offset: 0 }); this.queuedBytes += data.length; return true;
  }
  render(sampleCount = FRAME_SAMPLES) {
    if (!Number.isInteger(sampleCount) || sampleCount < 1 || sampleCount > 320) throw new Error('phone_output_frame');
    const output = Buffer.alloc(sampleCount * 2);
    if (this.closed) return output;
    let written = 0;
    while (written < output.length) {
      const row = this.cue || (this.pausedMain ? null : this.main.values().next().value);
      if (!row) break;
      const count = Math.min(output.length - written, row.data.length - row.offset);
      row.data.copy(output, written, row.offset, row.offset + count);
      row.offset += count; written += count;
      if (row !== this.cue) this.queuedBytes -= count;
      if (row.offset === row.data.length) {
        if (row === this.cue) { this.cue = null; this.handoff = null; row.finish(); }
        else this.main.delete(row);
      }
    }
    this.sentSamples += sampleCount; return output;
  }
  stopMain() { this.main.clear(); this.pausedMain = null; this.queuedBytes = 0; this.handoff = null; this.handoffTelemetry = null; }
  takeHandoffTelemetry() { const value = this.handoffTelemetry; this.handoffTelemetry = null; return value; }
  pauseMain() {
    if (this.closed || this.pausedMain) return;
    this.pausedMain = [...this.main]; this.main.clear();
  }
  resumeMain() {
    if (this.closed) return false;
    for (const row of this.pausedMain || []) this.main.add(row);
    this.pausedMain = null; return true;
  }
  close() { if (this.closed) return; this.closed = true; this.cue?.stop(); this.stopMain(); }
}

export class PhoneMicrophone {
  constructor({ inputSampleRate = 8000, onFrame, onSpeech = () => {}, onLevel = () => {} } = {}) {
    this.encoder = new StreamingPcmEncoder({ inputSampleRate });
    this.detector = new EnergySpeechDetector({ onActivity: onSpeech });
    this.onFrame = onFrame; this.onLevel = onLevel; this.closed = false; this.muted = false;
  }
  async start() { if (this.closed) throw new Error('phone_input_closed'); }
  push(bytes) {
    if (this.closed) return;
    const data = pcm(bytes), input = new Float32Array(data.length / 2);
    for (let i = 0; i < input.length; i++) input[i] = this.muted ? 0 : data.readInt16LE(i * 2) / 32768;
    for (const frame of this.encoder.push(input)) {
      if (!this.muted) this.detector.push(frame);
      this.onLevel(frame.rms); this.onFrame(frame); // Never gate caller input on output.
    }
  }
  setMuted(value) { this.muted = Boolean(value); this.detector.reset(); this.encoder.reset({ preserveSequence: true }); }
  async stop() { this.closed = true; this.detector.reset({ notify: false }); }
}

// Asterisk AudioSocket() uses8kHz. A stateful63tap lowpass precedes decimation;
// the16kHz channel-driver option skips conversion only after version validation.
export class PhoneDownsampler {
  constructor(sampleRate = 8000) {
    if (![8000, 16000].includes(sampleRate)) throw new Error('phone_output_rate');
    this.sampleRate = sampleRate; this.history = new Float64Array(63); this.at = 0; this.inputAt = 0;
    const values = [];
    for (let i = 0; i < 63; i++) {
      const x = i - 31, sinc = x === 0 ? .45 : Math.sin(Math.PI * .45 * x) / (Math.PI * x);
      values.push(sinc * (.54 - .46 * Math.cos(2 * Math.PI * i / 62)));
    }
    const sum = values.reduce((a, b) => a + b, 0); this.coefficients = values.map(value => value / sum);
  }
  push(value) {
    const data = pcm(value);
    if (this.sampleRate === 16000) return data;
    const output = [];
    for (let i = 0; i < data.length; i += 2) {
      this.history[this.at] = data.readInt16LE(i);
      if (this.inputAt++ % 2 === 0) {
        let filtered = 0;
        for (let tap = 0; tap < 63; tap++) filtered += this.history[(this.at - tap + 63) % 63] * this.coefficients[tap];
        output.push(Math.max(-32768, Math.min(32767, Math.round(filtered))));
      }
      this.at = (this.at + 1) % 63;
    }
    const result = Buffer.alloc(output.length * 2);
    output.forEach((sample, i) => result.writeInt16LE(sample, i * 2)); return result;
  }
}
