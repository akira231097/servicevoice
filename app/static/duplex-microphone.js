// The input path runs continuously during both cached cues and primary speech.
// Energy activity is a local cancellation heuristic, never transcription proof.

function finiteSample(sample) {
  return Number.isFinite(sample) ? Math.max(-1, Math.min(1, sample)) : 0;
}

function lowPass(inputRate, outputRate) {
  if (inputRate === outputRate) return new Float64Array([1]);
  const taps = 63, center = (taps - 1) / 2;
  const cutoff = 0.45 * Math.min(inputRate, outputRate) / inputRate;
  const coefficients = new Float64Array(taps);
  let sum = 0;
  for (let i = 0; i < taps; i += 1) {
    const distance = i - center;
    const sinc = distance === 0 ? 2 * cutoff : Math.sin(2 * Math.PI * cutoff * distance) / (Math.PI * distance);
    coefficients[i] = sinc * (0.54 - 0.46 * Math.cos(2 * Math.PI * i / (taps - 1)));
    sum += coefficients[i];
  }
  return Float64Array.from(coefficients, value => value / sum);
}

export class StreamingPcmEncoder {
  constructor({ inputSampleRate, outputSampleRate = 16000, frameMs = 20 } = {}) {
    if (!Number.isInteger(inputSampleRate) || inputSampleRate < 8000 || inputSampleRate > 192000
        || outputSampleRate !== 16000 || frameMs !== 20) throw new Error('invalid-pcm-geometry');
    this.inputSampleRate = inputSampleRate;
    this.outputSampleRate = outputSampleRate;
    this.frameMs = frameMs;
    this.frameSamples = 320;
    this.coefficients = lowPass(inputSampleRate, outputSampleRate);
    this.reset();
  }

  reset({ preserveSequence = false } = {}) {
    this.history = new Float64Array(this.coefficients.length);
    this.historyAt = 0;
    this.inputAt = 0;
    this.outputAt = 0;
    this.previous = 0;
    this.pending = [];
    if (!preserveSequence) this.sequence = 0;
  }

  push(samples) {
    if (!(samples instanceof Float32Array) || samples.length > 8192) throw new Error('invalid-input-block');
    const frames = [];
    for (const raw of samples) {
      this.history[this.historyAt] = finiteSample(raw);
      let filtered = 0;
      for (let tap = 0; tap < this.coefficients.length; tap += 1) {
        const index = (this.historyAt - tap + this.history.length) % this.history.length;
        filtered += this.history[index] * this.coefficients[tap];
      }
      this.historyAt = (this.historyAt + 1) % this.history.length;
      // Integer rate arithmetic avoids chunk boundaries and accumulating clock drift.
      while (this.outputAt * this.inputSampleRate <= this.inputAt * this.outputSampleRate) {
        const position = this.outputAt * this.inputSampleRate / this.outputSampleRate;
        const fraction = this.inputAt === 0 ? 1 : position - (this.inputAt - 1);
        this.pending.push(finiteSample(this.previous + (filtered - this.previous) * fraction));
        this.outputAt += 1;
        if (this.pending.length === this.frameSamples) {
          const pcm = new Uint8Array(this.frameSamples * 2), view = new DataView(pcm.buffer);
          let energy = 0, peak = 0;
          for (let i = 0; i < this.frameSamples; i += 1) {
            const value = this.pending[i];
            const integer = Math.round(value * (value < 0 ? 32768 : 32767));
            view.setInt16(i * 2, integer, true);
            energy += value * value;
            peak = Math.max(peak, Math.abs(value));
          }
          frames.push({ pcm, rms: Math.sqrt(energy / this.frameSamples), peak,
            sequence: this.sequence++, sampleRate: this.outputSampleRate, durationMs: this.frameMs });
          this.pending = [];
        }
      }
      this.previous = filtered;
      this.inputAt += 1;
    }
    return frames;
  }
}

export class EnergySpeechDetector {
  constructor({ attackMs = 40, releaseMs = 220, startRms = 0.018, stopRms = 0.010, onActivity = () => {} } = {}) {
    if (!Number.isFinite(attackMs) || attackMs < 20 || attackMs > 500
        || !Number.isFinite(releaseMs) || releaseMs < 20 || releaseMs > 1000
        || !Number.isFinite(startRms) || !Number.isFinite(stopRms)
        || stopRms <= 0 || startRms <= stopRms || startRms >= 1) throw new Error('invalid-energy-settings');
    this.attackMs = attackMs; this.releaseMs = releaseMs;
    this.startRms = startRms; this.stopRms = stopRms; this.onActivity = onActivity;
    this.speaking = false; this.highMs = 0; this.lowMs = 0;
  }

  reset({ notify = true } = {}) {
    const wasSpeaking = this.speaking;
    this.speaking = false; this.highMs = 0; this.lowMs = 0;
    if (wasSpeaking && notify) this.onActivity(false, { source: 'local_energy_heuristic', reason: 'reset' });
  }

  push(frame) {
    if (!Number.isFinite(frame?.rms) || frame.rms < 0 || frame.rms > 1 || frame.durationMs !== 20) throw new Error('invalid-energy-frame');
    if (!this.speaking) {
      this.highMs = frame.rms >= this.startRms ? this.highMs + 20 : 0;
      if (this.highMs >= this.attackMs) {
        this.speaking = true; this.highMs = 0; this.lowMs = 0;
        this.onActivity(true, { source: 'local_energy_heuristic', rms: frame.rms, peak: frame.peak, sequence: frame.sequence });
      }
    } else {
      this.lowMs = frame.rms <= this.stopRms ? this.lowMs + 20 : 0;
      if (this.lowMs >= this.releaseMs) {
        this.speaking = false; this.lowMs = 0;
        this.onActivity(false, { source: 'local_energy_heuristic', rms: frame.rms, peak: frame.peak, sequence: frame.sequence });
      }
    }
    return this.speaking;
  }
}

export class DuplexMicrophone {
  constructor({ audioContext, onFrame, onSpeech = () => {}, onLevel = () => {}, onError = () => {}, runtime = {} } = {}) {
    if (!audioContext || typeof onFrame !== 'function') throw new Error('microphone-runtime-required');
    this.context = audioContext; this.onFrame = onFrame; this.onLevel = onLevel; this.onError = onError;
    this.runtime = {
      getUserMedia: constraints => navigator.mediaDevices.getUserMedia(constraints),
      makeWorklet: (context, name, options) => new AudioWorkletNode(context, name, options),
      workletUrl: new URL('./duplex-capture-worklet.js', import.meta.url).href,
      ...runtime,
    };
    this.encoder = new StreamingPcmEncoder({ inputSampleRate: audioContext.sampleRate });
    this.detector = new EnergySpeechDetector({ onActivity: onSpeech });
    this.closed = false; this.startPending = false; this.started = false; this.muted = false; this.epoch = 0;
    this.stream = null; this.ownStream = true; this.trackListeners = [];
    this.source = null; this.node = null; this.sink = null;
  }

  async start({ stream, ownStream = true } = {}) {
    if (this.closed || this.startPending || this.started) throw new Error('microphone-start-state');
    this.startPending = true; this.ownStream = ownStream;
    try {
      this.stream = stream || await this.runtime.getUserMedia({ audio: {
        echoCancellation: true, noiseSuppression: true, autoGainControl: true,
        channelCount: 1, sampleRate: { ideal: 16000 },
      }, video: false });
      if (this.closed) throw new Error('microphone-start-cancelled');
      const tracks = this.stream.getAudioTracks();
      if (!tracks.length || tracks.every(track => track.readyState === 'ended')) throw new Error('microphone-unavailable');
      await this.context.audioWorklet.addModule(this.runtime.workletUrl);
      if (this.closed) throw new Error('microphone-start-cancelled');
      this.source = this.context.createMediaStreamSource(this.stream);
      this.node = this.runtime.makeWorklet(this.context, 'servicevoice-duplex-capture', {
        numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1], channelCount: 1, channelCountMode: 'explicit',
      });
      this.node.port.postMessage({ type: 'epoch', epoch: this.epoch, muted: this.muted });
      this.sink = this.context.createGain(); this.sink.gain.value = 0;
      this.node.port.onmessage = event => {
        if (this.closed) return;
        try {
          if (event.data?.epoch !== this.epoch) return; // Reject capture queued before mute/unmute.
          const samples = event.data.samples;
          if (!(samples instanceof Float32Array)) throw new Error('invalid-capture-block');
          if (this.muted) samples.fill(0);
          for (const frame of this.encoder.push(samples)) {
            if (this.closed) break;
            if (!this.muted) this.detector.push(frame);
            this.onLevel(frame.rms);
            this.onFrame(frame);
          }
        } catch { this.fail('microphone-frame-failed'); }
      };
      this.node.onprocessorerror = () => this.fail('microphone-processor-failed');
      for (const track of tracks) {
        const handler = () => this.fail('microphone-ended');
        track.addEventListener?.('ended', handler); this.trackListeners.push([track, handler]);
        track.enabled = !this.muted;
      }
      this.source.connect(this.node); this.node.connect(this.sink); this.sink.connect(this.context.destination);
      if (this.context.state === 'suspended') await this.context.resume();
      if (this.closed) throw new Error('microphone-start-cancelled');
      this.started = true;
      return true;
    } catch (error) {
      await this.stop();
      throw error;
    } finally { this.startPending = false; }
  }

  setMuted(muted) {
    if (this.closed) return;
    this.muted = Boolean(muted);
    this.epoch += 1;
    this.node?.port.postMessage({ type: 'epoch', epoch: this.epoch, muted: this.muted });
    for (const track of this.stream?.getAudioTracks?.() || []) track.enabled = !this.muted;
    this.encoder.reset({ preserveSequence: true });
    this.detector.reset();
  }

  fail(code) {
    if (this.closed) return;
    void this.stop();
    this.onError(new Error(code));
  }

  async stop() {
    this.closed = true; this.started = false;
    for (const [track, handler] of this.trackListeners) track.removeEventListener?.('ended', handler);
    this.trackListeners = [];
    if (this.node) { this.node.port.onmessage = null; this.node.onprocessorerror = null; this.node.port.close?.(); }
    for (const node of [this.source, this.node, this.sink]) { try { node?.disconnect(); } catch { /* Finish the remaining cleanup. */ } }
    if (this.ownStream) for (const track of this.stream?.getTracks?.() || []) { try { track.stop(); } catch { /* Finish remaining tracks. */ } }
    this.stream = null; this.source = null; this.node = null; this.sink = null;
    this.encoder.reset({ preserveSequence: true }); this.detector.reset({ notify: false });
    // The owner may still use this AudioContext for playback; never close it here.
  }
}
