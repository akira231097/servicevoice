// Browser-owned output arbitration. Microphone capture/transport is deliberately
// independent: neither a cue nor main speech ever disables an input track.
// Server-side Jev selection is advisory; this coordinator owns the final race.
const PHRASE_BY_PHASE = Object.freeze({ listening: 'neutral_ack', waiting: 'moment' });

export class DuplexCoordinator {
  constructor({ selectCue, playback, now = () => performance.now(),
    setTimeout: schedule = (...args) => setTimeout(...args),
    clearTimeout: cancel = timer => clearTimeout(timer), onEvent = () => {},
    listeningDelayMs = 300, waitingDelayMs = 550, decisionDeadlineMs = 350,
    cueTtlMs = 1000, maxCueDurationMs = 2500, cooldownMs = 15000, maxCues = 4, maxRequests = 64,
    minimumRequestIntervalMs = 350,
    minimumConfidence = .9 } = {}) {
    if (typeof selectCue !== 'function' || !playback || typeof playback.playCue !== 'function'
      || typeof playback.enqueueMain !== 'function' || typeof playback.stopMain !== 'function') {
      throw new TypeError('duplex_dependencies_required');
    }
    for (const value of [listeningDelayMs, waitingDelayMs, cooldownMs]) {
      if (!Number.isFinite(value) || value < 0) throw new RangeError('invalid_duplex_timing');
    }
    if (!Number.isFinite(decisionDeadlineMs) || decisionDeadlineMs <= 0 || decisionDeadlineMs > 1000
      || !Number.isFinite(cueTtlMs) || cueTtlMs <= 0 || cueTtlMs > 2000
      || !Number.isFinite(maxCueDurationMs) || maxCueDurationMs <= 0 || maxCueDurationMs > 2500
      || !Number.isSafeInteger(maxCues) || maxCues < 0 || maxCues > 20
      || !Number.isSafeInteger(maxRequests) || maxRequests < 0 || maxRequests > 100
      || !Number.isFinite(minimumRequestIntervalMs) || minimumRequestIntervalMs < 300 || minimumRequestIntervalMs > 2000
      || !Number.isFinite(minimumConfidence) || minimumConfidence < .9 || minimumConfidence > 1) {
      throw new RangeError('invalid_duplex_limits');
    }
    Object.assign(this, { selectCue, playback, now, schedule, cancel, onEvent,
      listeningDelayMs, waitingDelayMs, decisionDeadlineMs, cueTtlMs, maxCueDurationMs, cooldownMs,
      maxCues, maxRequests, minimumConfidence, minimumRequestIntervalMs });
    this.epoch = 0; this.utteranceId = 0; this.cuedUtteranceId = -1; this.closed = false; this.userSpeaking = false;
    this.sensitive = false; this.text = ''; this.revision = null; this.finalReceived = false;
    this.generationId = null; this.generating = false; this.mainHasAudio = false;
    this.pendingTimer = null; this.request = null; this.cue = null;
    this.cuesPlayed = 0; this.requests = 0; this.lastCueAt = -Infinity; this.lastRequestAt = -Infinity;
  }

  emit(kind, detail = {}) {
    // Never log transcript text, provider errors or raw classifier responses.
    try { this.onEvent({ kind, epoch: this.epoch, at_ms: this.now(), ...detail }); } catch { /* Telemetry is optional. */ }
  }

  stopCue(reason) {
    const cue = this.cue; this.cue = null;
    if (cue) {
      this.cancel(cue.expiryTimer);
      try { cue.handle.stop(); } catch { /* Expired nodes may already be stopped. */ }
      this.emit('cue_stopped', { reason, phrase_id: cue.phraseId });
    }
  }

  invalidate(reason, { stopCue = true } = {}) {
    this.epoch += 1;
    this.cancel(this.pendingTimer); this.pendingTimer = null;
    if (this.request) {
      this.cancel(this.request.timer); this.request.controller.abort(); this.request = null;
    }
    if (stopCue) this.stopCue(reason);
  }

  userSpeechStarted() {
    if (this.closed || this.userSpeaking) return;
    this.invalidate('caller_started');
    this.utteranceId += 1;
    this.userSpeaking = true; this.text = ''; this.revision = null; this.finalReceived = false;
    // Old generation IDs remain invalid even if late PCM arrives after speech ends.
    this.generationId = null; this.generating = false; this.mainHasAudio = false;
    this.playback.stopMain();
    this.emit('caller_started');
  }

  updatePartial(text, { revision = null } = {}) {
    if (this.closed || !this.userSpeaking || typeof text !== 'string') return false;
    const next = text.trim();
    if (!next || next.length > 1600 || (next === this.text && revision === this.revision)) return false;
    // Extending the SAME ordinary utterance does not revoke a bounded decision
    // about its earlier prefix. A real ASR rewrite still invalidates ownership.
    // The adapter applies raw sensitive-text gating BEFORE calling this method.
    const protectedPrefix = this.request?.snapshotText ?? this.text;
    const appendCompatible = protectedPrefix && !this.finalReceived && next.startsWith(protectedPrefix);
    if (!appendCompatible) this.invalidate('partial_revised', { stopCue: false });
    this.text = next; this.revision = revision; this.finalReceived = false;
    this.queueCue('listening', this.listeningDelayMs);
    return true;
  }

  updateFinal(text, { revision = null } = {}) {
    if (this.closed || typeof text !== 'string') return false;
    const next = text.trim();
    if (!next || next.length > 1600 || (next === this.text && revision === this.revision)) return false;
    this.invalidate('final_revised', { stopCue: false }); this.text = next; this.revision = revision; this.finalReceived = true;
    if (!this.userSpeaking && this.generating) this.queueCue('waiting', this.waitingDelayMs);
    return true;
  }

  userSpeechStopped() {
    if (this.closed || !this.userSpeaking) return;
    this.invalidate('caller_stopped', { stopCue: false }); this.userSpeaking = false;
    if (this.generating && this.generationId) this.queueCue('waiting', this.waitingDelayMs);
    this.emit('caller_stopped');
  }

  beginGeneration(generationId) {
    if (this.closed || typeof generationId !== 'string' || !generationId || generationId.length > 200) return false;
    if (this.generationId === generationId) return false;
    this.invalidate('generation_started', { stopCue: false }); this.playback.stopMain();
    this.generationId = generationId; this.generating = true; this.mainHasAudio = false;
    if (!this.userSpeaking) this.queueCue('waiting', this.waitingDelayMs);
    this.emit('generation_started');
    return true;
  }

  finishGeneration(generationId) {
    if (this.closed || generationId !== this.generationId) return false;
    this.generating = false;
    this.invalidate('generation_finished', { stopCue: false });
    return true;
  }

  setSensitive(value) {
    if (this.closed || Boolean(value) === this.sensitive) return;
    this.sensitive = Boolean(value); this.invalidate('sensitive_state_changed');
    // Only a fresh observation may schedule another cue after a sensitive state.
  }

  eligible(phase, epoch) {
    return !this.closed && epoch === this.epoch && !this.sensitive && !this.mainHasAudio && !this.cue
      && this.cuesPlayed < this.maxCues && this.now() - this.lastCueAt >= this.cooldownMs
      && this.utteranceId !== this.cuedUtteranceId
      && (phase === 'listening' ? this.userSpeaking && !this.finalReceived : !this.userSpeaking && this.generating && Boolean(this.generationId));
  }

  queueCue(phase, delay) {
    const epoch = this.epoch;
    if (!this.eligible(phase, epoch) || this.text.split(/\s+/).length < 3 || this.requests >= this.maxRequests
      || this.pendingTimer !== null || this.request) return;
    const wait = Math.max(delay, this.minimumRequestIntervalMs - (this.now() - this.lastRequestAt));
    this.pendingTimer = this.schedule(() => {
      this.pendingTimer = null;
      if (this.eligible(phase, epoch)) void this.choose(phase, epoch);
    }, wait);
  }

  async choose(phase, epoch) {
    if (!this.eligible(phase, epoch) || this.requests >= this.maxRequests) return;
    const began = this.now(), controller = new AbortController();
    // ASR commonly revises its current last word. Keep that uncommitted word
    // outside a listening snapshot, while checking ALL raw words for sensitivity
    // in the adapter. Waiting snapshots use the caller's complete final text.
    const snapshotText = phase === 'listening' ? this.text.replace(/\s+\S+$/, '').trim() : this.text;
    if (snapshotText.split(/\s+/).length < 3) return;
    const snapshot = { text: snapshotText, observedText: this.text, revision: this.revision };
    const followup = () => {
      if (this.text !== snapshot.observedText && this.eligible(phase, epoch)) this.queueCue(phase, 0);
    };
    const request = { controller, timer: null, snapshotText: snapshot.text };
    this.lastRequestAt = began;
    this.request = request; this.requests += 1;
    request.timer = this.schedule(() => {
      controller.abort();
      if (this.request === request) this.request = null;
      this.emit('cue_suppressed', { reason: 'classifier_deadline', phase });
      followup();
    }, this.decisionDeadlineMs);
    let decision;
    try {
      decision = await this.selectCue({ phase, text: snapshot.text, epoch, utteranceId: this.utteranceId, revision: snapshot.revision,
        generationId: this.generationId, signal: controller.signal });
    } catch {
      if (!controller.signal.aborted) this.emit('cue_suppressed', { reason: 'classifier_unavailable', phase });
      if (this.request === request) this.request = null;
      if (!controller.signal.aborted) followup();
      return;
    } finally {
      this.cancel(request.timer);
      if (this.request === request) this.request = null;
    }
    if (controller.signal.aborted || !this.eligible(phase, epoch)
      || this.now() - began >= this.decisionDeadlineMs
      || (phase === 'listening' && !this.text.startsWith(snapshot.text))) return;
    const phraseId = decision?.phrase_id;
    const ttl = Math.min(this.cueTtlMs, Number(decision?.expires_in_ms ?? this.cueTtlMs));
    if (phraseId !== PHRASE_BY_PHASE[phase] || !Number.isFinite(decision?.confidence)
      || decision.confidence < this.minimumConfidence || decision.language_choice !== 'cache_compatible'
      || !Number.isFinite(ttl) || ttl <= 0 || this.now() - began >= ttl) {
      this.emit('cue_suppressed', { reason: 'invalid_or_uncertain_selection', phase }); followup(); return;
    }
    // playCue must synchronously return a cancellable local playback handle.
    // No remote say/audio injection command is used or queued here.
    let handle;
    try { handle = this.playback.playCue(phraseId); } catch { handle = null; }
    if (!handle || typeof handle.stop !== 'function' || !handle.ended?.then) {
      try { handle?.stop?.(); } catch { /* Invalid playback fails silent. */ }
      this.emit('cue_suppressed', { reason: 'clip_unavailable', phase }); return;
    }
    const cue = { phraseId, handle, expiryTimer: null }; this.cue = cue;
    // Selection expiry limits when a clip may START. Once started, allow its
    // complete words/endings; only a separate runaway-output guard bounds it.
    cue.expiryTimer = this.schedule(() => {
      if (this.cue === cue) this.stopCue('cue_duration_limit');
    }, this.maxCueDurationMs);
    this.cuesPlayed += 1; this.cuedUtteranceId = this.utteranceId; this.lastCueAt = this.now();
    this.emit('cue_started', { phase, phrase_id: phraseId, selection_ms: this.now() - began,
      snapshot_revision: snapshot.revision, latest_revision: this.revision });
    Promise.resolve(handle.ended).catch(() => {}).finally(() => {
      this.cancel(cue.expiryTimer);
      if (this.cue === cue) { this.cue = null; this.emit('cue_ended', { phrase_id: phraseId }); }
    });
  }

  receiveMainPcm(generationId, pcm, sampleRate) {
    if (this.closed || this.userSpeaking || !generationId || generationId !== this.generationId) {
      this.emit('main_discarded', { reason: 'obsolete_generation_or_caller_speaking' }); return false;
    }
    // Stop a filler synchronously before the first main sample is queued.
    this.invalidate('main_audio_ready'); this.mainHasAudio = true;
    try { return this.playback.enqueueMain({ pcm, sampleRate, generationId }) === true; }
    catch { this.emit('main_discarded', { reason: 'invalid_audio' }); return false; }
  }

  close() {
    if (this.closed) return;
    this.closed = true; this.invalidate('call_ended');
    this.generationId = null; this.generating = false; this.text = '';
    this.playback.stopMain();
    try { this.playback.close?.(); } catch { /* Microphone cleanup belongs to transport. */ }
  }
}

function pcmSamples(pcm) {
  const bytes = pcm instanceof ArrayBuffer ? new Uint8Array(pcm)
    : ArrayBuffer.isView(pcm) ? new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength) : null;
  if (!bytes || !bytes.byteLength || bytes.byteLength % 2) throw new TypeError('invalid_pcm16');
  const samples = new Float32Array(bytes.byteLength / 2), view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  for (let index = 0; index < samples.length; index++) samples[index] = view.getInt16(index * 2, true) / 32768;
  return samples;
}

// One owned local output path. Main PCM and cached clips share a clock, so a
// ready main answer can cancel a cue without waiting for its natural end.
export class PcmPlayback {
  constructor({ context, cueAssets = {}, maxQueuedSeconds = 3 } = {}) {
    if (!context || !Number.isFinite(maxQueuedSeconds) || maxQueuedSeconds <= 0 || maxQueuedSeconds > 10) {
      throw new TypeError('invalid_pcm_playback');
    }
    this.context = context; this.cueAssets = cueAssets; this.maxQueuedSeconds = maxQueuedSeconds;
    this.main = new Set(); this.pausedMain = null; this.cue = null; this.mainEnd = 0; this.closed = false;
  }

  buffer(pcm, sampleRate) {
    if (!Number.isSafeInteger(sampleRate) || sampleRate < 8000 || sampleRate > 48000) throw new RangeError('invalid_sample_rate');
    const samples = pcmSamples(pcm);
    if (samples.length / sampleRate > this.maxQueuedSeconds) throw new RangeError('audio_chunk_too_large');
    const buffer = this.context.createBuffer(1, samples.length, sampleRate);
    buffer.copyToChannel(samples, 0); return buffer;
  }

  node(buffer, when, nodes, offset = 0) {
    const source = this.context.createBufferSource(); source.buffer = buffer;
    source.connect(this.context.destination);
    let complete;
    const ended = new Promise(resolve => { complete = resolve; });
    const stop = () => {
      if (!nodes.delete(handle)) return;
      try { source.stop(); } catch { /* The source already ended. */ }
      try { source.disconnect(); } catch { /* Already disconnected. */ }
      complete();
    };
    const handle = { stop, ended, buffer, when, offset };
    source.onended = () => {
      nodes.delete(handle);
      try { source.disconnect(); } catch { /* Already disconnected. */ }
      complete();
    };
    nodes.add(handle);
    try { source.start(when, offset); } catch (error) { stop(); throw error; }
    return handle;
  }

  playCue(phraseId) {
    if (this.closed || this.main.size || this.pausedMain || this.context.state !== 'running') return null;
    const asset = this.cueAssets instanceof Map ? this.cueAssets.get(phraseId) : this.cueAssets[phraseId];
    if (!asset) return null;
    this.cue?.stop();
    const nodes = new Set(), handle = this.node(this.buffer(asset.pcm, asset.sampleRate), this.context.currentTime, nodes);
    this.cue = handle;
    handle.ended.finally(() => { if (this.cue === handle) this.cue = null; });
    return handle;
  }

  enqueueMain({ pcm, sampleRate }) {
    if (this.closed || this.pausedMain || this.context.state !== 'running') return false;
    const buffer = this.buffer(pcm, sampleRate), now = this.context.currentTime;
    const start = Math.max(now, this.mainEnd);
    if (start + buffer.duration - now > this.maxQueuedSeconds) return false;
    this.cue?.stop(); this.cue = null;
    this.node(buffer, start, this.main); this.mainEnd = start + buffer.duration;
    return true;
  }

  stopMain() {
    this.pausedMain = null;
    for (const handle of [...this.main]) handle.stop();
    this.mainEnd = this.context.currentTime;
  }

  pauseMain() {
    if (this.closed || this.pausedMain) return;
    const now = this.context.currentTime, remaining = [];
    for (const handle of this.main) {
      const offset = handle.offset + Math.max(0, now - handle.when);
      if (offset < handle.buffer.duration) remaining.push({ buffer: handle.buffer, offset });
    }
    this.stopMain(); this.pausedMain = remaining;
  }

  resumeMain() {
    if (this.closed || this.context.state !== 'running') return false;
    const remaining = this.pausedMain;
    if (!remaining) return true;
    this.pausedMain = null; this.mainEnd = this.context.currentTime;
    try {
      for (const row of remaining) {
        this.node(row.buffer, this.mainEnd, this.main, row.offset);
        this.mainEnd += row.buffer.duration - row.offset;
      }
      return true;
    } catch {
      this.stopMain(); return false;
    }
  }

  close() {
    if (this.closed) return;
    this.closed = true; this.cue?.stop(); this.cue = null; this.stopMain();
    // The caller owns the shared AudioContext and may still need microphone DSP.
  }
}
