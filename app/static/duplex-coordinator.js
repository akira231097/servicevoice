// Browser-owned output arbitration. Microphone capture/transport is deliberately
// independent: neither a cue nor main speech ever disables an input track.
// Server-side Jev selection is advisory; this coordinator owns the final race.
const PHRASE_BY_PHASE = Object.freeze({ listening: 'neutral_ack', waiting: 'moment' });

export class DuplexCoordinator {
  constructor({ selectCue, playback, now = () => performance.now(),
    setTimeout: schedule = (...args) => setTimeout(...args),
    clearTimeout: cancel = timer => clearTimeout(timer), onEvent = () => {},
    listeningDelayMs = 300, waitingDelayMs = 550, decisionDeadlineMs = 850,
    cueTtlMs = 1000, maxCueDurationMs = 2500, cooldownMs = 15000, maxCues = 4, maxRequests = 64,
    minimumRequestIntervalMs = 350, minimumCueIntervalMs = 300,
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
      || !Number.isFinite(minimumCueIntervalMs) || minimumCueIntervalMs < 300 || minimumCueIntervalMs > 2000
      || !Number.isFinite(minimumConfidence) || minimumConfidence < .9 || minimumConfidence > 1) {
      throw new RangeError('invalid_duplex_limits');
    }
    Object.assign(this, { selectCue, playback, now, schedule, cancel, onEvent,
      listeningDelayMs, waitingDelayMs, decisionDeadlineMs, cueTtlMs, maxCueDurationMs, cooldownMs,
      maxCues, maxRequests, minimumConfidence, minimumRequestIntervalMs, minimumCueIntervalMs });
    this.epoch = 0; this.utteranceId = 0; this.closed = false; this.userSpeaking = false;
    this.cuedUtteranceByPhase = { listening: -1, waiting: -1 };
    this.waitingAttemptUtteranceId = -1;
    this.lastCueAtByPhase = { listening: -Infinity, waiting: -Infinity };
    this.sensitive = false; this.listeningBlocked = false; this.text = ''; this.revision = null; this.finalReceived = false;
    this.generationId = null; this.generating = false; this.mainHasAudio = false; this.mainAnnounced = false;
    this.turnYieldedAt = null; this.generationBeganAt = null; this.lastCueEndedAt = -Infinity;
    this.pendingTimer = null; this.pendingPhase = null; this.request = null; this.cue = null;
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
      try { cue.handle.stop(reason); } catch { /* Expired nodes may already be stopped. */ }
      this.lastCueEndedAt = this.now();
      this.emit('cue_stopped', { reason, phrase_id: cue.phraseId });
    }
  }

  invalidate(reason, { stopCue = true } = {}) {
    // The adapter announces main speech before its PCM arrives. A clip ending
    // afterward must not resurrect a waiting decision for this generation.
    if (reason === 'main_announced') this.mainAnnounced = true;
    this.epoch += 1;
    this.cancel(this.pendingTimer); this.pendingTimer = null; this.pendingPhase = null;
    if (this.request) {
      this.cancel(this.request.timer); this.request.controller.abort(); this.request = null;
    }
    if (stopCue) this.stopCue(reason);
  }

  userSpeechStarted() {
    if (this.closed || this.userSpeaking) return;
    this.invalidate('caller_started');
    this.utteranceId += 1;
    this.userSpeaking = true; this.text = ''; this.revision = null; this.finalReceived = false; this.turnYieldedAt = null;
    // Old generation IDs remain invalid even if late PCM arrives after speech ends.
    this.generationId = null; this.generating = false; this.mainHasAudio = false; this.mainAnnounced = false;
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
    // A trailing final with identical text does not replace a waiting snapshot
    // already selected from that exact text. Raw safety gating ran beforehand.
    if (!this.userSpeaking && this.generating && this.request?.phase === 'waiting' && this.request.snapshotText === next) {
      this.text = next; this.revision = revision; this.finalReceived = true; return true;
    }
    this.invalidate('final_revised', { stopCue: false }); this.text = next; this.revision = revision; this.finalReceived = true;
    if (!this.userSpeaking && this.generating) this.queueCue('waiting', this.waitingDelayMs);
    return true;
  }

  userSpeechStopped() {
    if (this.closed || !this.userSpeaking) return;
    this.invalidate('caller_stopped', { stopCue: false }); this.userSpeaking = false; this.turnYieldedAt = this.now();
    if (this.generating && this.generationId) this.queueCue('waiting', this.waitingDelayMs);
    this.emit('caller_stopped');
  }

  beginGeneration(generationId) {
    if (this.closed || typeof generationId !== 'string' || !generationId || generationId.length > 200) return false;
    if (this.generationId === generationId) return false;
    this.invalidate('generation_started', { stopCue: false }); this.playback.stopMain();
    this.generationId = generationId; this.generating = true; this.mainHasAudio = false; this.mainAnnounced = false;
    this.generationBeganAt = this.now();
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

  setListeningBlocked(value) {
    if (this.closed || Boolean(value) === this.listeningBlocked) return;
    this.listeningBlocked = Boolean(value);
    if (!this.listeningBlocked) return;
    if (this.pendingPhase === 'listening') {
      this.cancel(this.pendingTimer); this.pendingTimer = null; this.pendingPhase = null;
    }
    if (this.request?.phase === 'listening') {
      this.cancel(this.request.timer); this.request.controller.abort(); this.request = null;
    }
    if (this.cue?.phase === 'listening') { this.stopCue('listening_context_blocked'); this.wakeWaiting(); }
  }

  canCue(phase, epoch) {
    return Object.hasOwn(PHRASE_BY_PHASE, phase) && !this.closed && epoch === this.epoch
      && !this.sensitive && !this.mainHasAudio && !this.mainAnnounced
      && this.cuesPlayed < this.maxCues && this.utteranceId !== this.cuedUtteranceByPhase[phase]
      && (phase === 'listening' ? !this.listeningBlocked && this.userSpeaking && !this.finalReceived
        : !this.userSpeaking && this.generating && Boolean(this.generationId));
  }

  cueWait(phase) {
    const now = this.now();
    const yielded = this.turnYieldedAt ?? this.generationBeganAt ?? now;
    return Math.max(0, this.lastCueAtByPhase[phase] + this.cooldownMs - now,
      this.lastCueEndedAt + this.minimumCueIntervalMs - now,
      phase === 'waiting' ? yielded + this.waitingDelayMs - now : 0);
  }

  eligible(phase, epoch) {
    return this.canCue(phase, epoch) && !this.cue && this.cueWait(phase) <= 0;
  }

  selectionWait(phase) {
    // Read-only waiting classification overlaps the playback guard and any
    // active listening clip. Same-phase cooldown and request pacing still apply.
    return phase === 'waiting' ? Math.max(0, this.lastCueAtByPhase[phase] + this.cooldownMs - this.now()) : this.cueWait(phase);
  }

  queueCue(phase, delay) {
    const epoch = this.epoch;
    if (!this.canCue(phase, epoch) || (this.cue && phase !== 'waiting') || !this.text.trim()
      || this.text.split(/\s+/).length < (phase === 'waiting' ? 1 : 3) || this.requests >= this.maxRequests
      || (phase === 'waiting' && this.waitingAttemptUtteranceId === this.utteranceId)
      || this.pendingTimer !== null || this.request) return;
    const wait = Math.max(phase === 'waiting' ? 0 : delay, this.selectionWait(phase),
      this.minimumRequestIntervalMs - (this.now() - this.lastRequestAt));
    this.pendingPhase = phase;
    this.pendingTimer = this.schedule(() => {
      this.pendingTimer = null; this.pendingPhase = null;
      if (!this.canCue(phase, epoch) || (this.cue && phase !== 'waiting')) return;
      // Browser timers may fire just before a fractional performance-clock
      // deadline. Retain the one valid attempt instead of silently losing it.
      if (this.selectionWait(phase) > 0 || this.now() - this.lastRequestAt < this.minimumRequestIntervalMs) {
        this.queueCue(phase, 0); return;
      }
      void this.choose(phase, epoch);
    }, Math.ceil(wait));
  }

  async choose(phase, epoch) {
    if (!this.canCue(phase, epoch) || (phase !== 'waiting' && !this.eligible(phase, epoch))
      || (phase === 'waiting' && this.waitingAttemptUtteranceId === this.utteranceId)
      || this.selectionWait(phase) > 0 || this.requests >= this.maxRequests || this.request) return;
    const began = this.now(), controller = new AbortController();
    // ASR commonly revises its current last word. Keep that uncommitted word
    // outside a listening snapshot, while checking ALL raw words for sensitivity
    // in the adapter. Waiting snapshots use the caller's complete final text.
    const snapshotText = phase === 'listening' ? this.text.replace(/\s+\S+$/, '').trim() : this.text;
    if (!snapshotText || snapshotText.split(/\s+/).length < (phase === 'waiting' ? 1 : 3)) return;
    const snapshot = { text: snapshotText, observedText: this.text, revision: this.revision };
    const followup = () => {
      if (this.text !== snapshot.observedText && this.canCue(phase, epoch)) this.queueCue(phase, 0);
    };
    const request = { controller, timer: null, snapshotText: snapshot.text, snapshot, phase, epoch, began, decision: null };
    this.lastRequestAt = began;
    this.request = request; this.requests += 1;
    if (phase === 'waiting') this.waitingAttemptUtteranceId = this.utteranceId;
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
    }
    if (this.request !== request || controller.signal.aborted || !this.canCue(phase, epoch)
      || this.now() - began >= this.decisionDeadlineMs
      || (phase === 'listening' && !this.text.startsWith(snapshot.text))) {
      if (this.request === request) this.request = null;
      return;
    }
    const phraseId = decision?.phrase_id;
    const ttl = Math.min(this.cueTtlMs, Number(decision?.expires_in_ms ?? this.cueTtlMs));
    if (phraseId !== PHRASE_BY_PHASE[phase] || !Number.isFinite(decision?.confidence)
      || decision.confidence < this.minimumConfidence || decision.language_choice !== 'cache_compatible'
      || !Number.isFinite(ttl) || ttl <= 0 || this.now() - began >= ttl) {
      this.request = null;
      this.emit('cue_suppressed', { reason: 'invalid_or_uncertain_selection', phase }); followup(); return;
    }
    request.decision = decision;
    request.selectedAt = this.now();
    // Neither the TTL nor client deadline rolls forward when a result is held.
    request.expiresAt = Math.min(began + ttl, began + this.decisionDeadlineMs);
    this.playReady(request);
  }

  wakeWaiting() {
    if (this.request?.phase === 'waiting' && this.request.decision) this.playReady(this.request);
    else this.queueCue('waiting', 0);
  }

  playReady(request) {
    if (this.request !== request) return;
    const { phase, epoch, controller, began, snapshot, decision } = request;
    this.cancel(request.timer);
    if (controller.signal.aborted || !this.canCue(phase, epoch)
      || (phase === 'listening' && !this.text.startsWith(snapshot.text))) {
      this.request = null; return;
    }
    if (this.now() >= request.expiresAt) {
      this.request = null; controller.abort();
      this.emit('cue_suppressed', { reason: 'selection_expired_before_playback', phase }); return;
    }
    if (!this.eligible(phase, epoch)) {
      const wakeAt = this.cue ? request.expiresAt : Math.min(request.expiresAt, this.now() + this.cueWait(phase));
      request.timer = this.schedule(() => this.playReady(request), Math.ceil(wakeAt - this.now()));
      return;
    }
    this.request = null;
    const phraseId = decision.phrase_id;
    // playCue must synchronously return a cancellable local playback handle.
    // No remote say/audio injection command is used or queued here.
    let handle;
    try { handle = this.playback.playCue(phraseId); } catch { handle = null; }
    if (!handle || typeof handle.stop !== 'function' || !handle.ended?.then) {
      try { handle?.stop?.(); } catch { /* Invalid playback fails silent. */ }
      this.emit('cue_suppressed', { reason: 'clip_unavailable', phase }); return;
    }
    // A playback hook may synchronously deliver main readiness. Recheck before
    // committing cue ownership, and stop any just-created obsolete node.
    if (controller.signal.aborted || !this.eligible(phase, epoch)) { try { handle.stop(); } catch {} return; }
    const cue = { phraseId, phase, handle, expiryTimer: null }; this.cue = cue;
    // Selection expiry limits when a clip may START. Once started, allow its
    // complete words/endings; only a separate runaway-output guard bounds it.
    cue.expiryTimer = this.schedule(() => {
      if (this.cue === cue) { this.stopCue('cue_duration_limit'); this.wakeWaiting(); }
    }, this.maxCueDurationMs);
    this.cuesPlayed += 1; this.cuedUtteranceByPhase[phase] = this.utteranceId;
    this.lastCueAtByPhase[phase] = this.now(); this.lastCueAt = this.now();
    this.emit('cue_started', { phase, phrase_id: phraseId, selection_ms: request.selectedAt - began,
      guard_hold_ms: this.now() - request.selectedAt, decision_age_ms: this.now() - began,
      snapshot_revision: snapshot.revision, latest_revision: this.revision });
    Promise.resolve(handle.ended).catch(() => {}).finally(() => {
      this.cancel(cue.expiryTimer);
      if (this.cue === cue) {
        this.cue = null; this.lastCueEndedAt = this.now(); this.emit('cue_ended', { phrase_id: phraseId });
        this.wakeWaiting();
      }
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
    this.main = new Set(); this.pausedMain = null; this.cue = null; this.mainEnd = 0;
    this.cueBarrier = 0; this.cueFadeSeconds = .008; this.closed = false;
  }

  buffer(pcm, sampleRate) {
    if (!Number.isSafeInteger(sampleRate) || sampleRate < 8000 || sampleRate > 48000) throw new RangeError('invalid_sample_rate');
    const samples = pcmSamples(pcm);
    if (samples.length / sampleRate > this.maxQueuedSeconds) throw new RangeError('audio_chunk_too_large');
    const buffer = this.context.createBuffer(1, samples.length, sampleRate);
    buffer.copyToChannel(samples, 0); return buffer;
  }

  node(buffer, when, nodes, offset = 0, fadeOnStop = false) {
    const gain = fadeOnStop ? this.context.createGain?.() : null;
    if (fadeOnStop && (!gain?.gain || !['setValueAtTime', 'cancelScheduledValues', 'linearRampToValueAtTime']
      .every(method => typeof gain.gain[method] === 'function'))) {
      try { gain?.disconnect(); } catch { /* Incomplete gain support fails silent. */ }
      throw new TypeError('cue_gain_unavailable');
    }
    const source = this.context.createBufferSource(); source.buffer = buffer;
    try {
      source.connect(gain || this.context.destination);
      if (gain) { gain.connect(this.context.destination); gain.gain.setValueAtTime(1, when); }
    } catch (error) {
      try { source.stop(); } catch { /* The unstarted source may reject stop. */ }
      try { source.disconnect(); } catch { /* Already disconnected. */ }
      try { gain?.disconnect(); } catch { /* Already disconnected. */ }
      throw error;
    }
    let complete;
    const ended = new Promise(resolve => { complete = resolve; });
    let started = false, finished = false;
    const finish = () => {
      if (finished) return;
      finished = true; nodes.delete(handle);
      try { source.disconnect(); } catch { /* Already disconnected. */ }
      try { gain?.disconnect(); } catch { /* Already disconnected. */ }
      complete();
    };
    const stop = () => {
      if (handle.stopAt !== null) return handle.stopAt;
      const now = this.context.currentTime;
      if (finished) return now;
      nodes.delete(handle); // Revoke logical ownership before scheduling audio.
      const naturalEnd = when + buffer.duration - offset;
      let barrier = now;
      if (gain && started && this.context.state === 'running' && naturalEnd > now) {
        barrier = Math.min(now + this.cueFadeSeconds, naturalEnd);
        try {
          gain.gain.cancelScheduledValues(now);
          gain.gain.setValueAtTime(Number.isFinite(gain.gain.value) ? gain.gain.value : 1, now);
          gain.gain.linearRampToValueAtTime(0, barrier);
        } catch { barrier = now; }
      }
      handle.stopAt = barrier;
      if (gain) this.cueBarrier = Math.max(this.cueBarrier, barrier);
      try { source.stop(barrier); } catch { finish(); }
      if (barrier <= now) finish();
      return barrier;
    };
    const handle = { stop, ended, buffer, when, offset, stopAt: null };
    source.onended = finish;
    nodes.add(handle);
    try { source.start(when, offset); started = true; } catch (error) { stop(); throw error; }
    return handle;
  }

  playCue(phraseId) {
    if (this.closed || this.main.size || this.pausedMain || this.context.state !== 'running'
      || this.context.currentTime < this.cueBarrier) return null;
    const asset = this.cueAssets instanceof Map ? this.cueAssets.get(phraseId) : this.cueAssets[phraseId];
    if (!asset) return null;
    this.cue?.stop();
    const nodes = new Set(), handle = this.node(this.buffer(asset.pcm, asset.sampleRate), this.context.currentTime, nodes, 0, true);
    this.cue = handle;
    handle.ended.finally(() => { if (this.cue === handle) this.cue = null; });
    return handle;
  }

  enqueueMain({ pcm, sampleRate }) {
    if (this.closed || this.pausedMain || this.context.state !== 'running') return false;
    const buffer = this.buffer(pcm, sampleRate), now = this.context.currentTime;
    this.cue?.stop('main_audio_ready'); this.cue = null;
    // A preempted cue fades for at most8ms. Stop and start use the same audio
    // clock barrier, preventing assistant-source overlap without waiting for
    // the remaining phrase or adding another fade to each main PCM chunk.
    const start = Math.max(now, this.mainEnd, this.cueBarrier);
    if (start + buffer.duration - now > this.maxQueuedSeconds) return false;
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
    this.closed = true; this.cue?.stop('call_ended'); this.cue = null; this.stopMain();
    // The caller owns the shared AudioContext and may still need microphone DSP.
  }
}
