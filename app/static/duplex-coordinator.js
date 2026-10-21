// Browser-owned output arbitration. Microphone capture/transport is deliberately
// independent: neither a cue nor main speech ever disables an input track.
// Server-side Jev selection is advisory; this coordinator owns the final race.
export const CUE_PHRASES = Object.freeze(['neutral_ack', 'moment', 'one_sec', 'just_a_sec', 'let_me_see', 'checking', 'taking_a_look', 'working_on_it']);
const PHRASE_BY_PHASE = Object.freeze({ listening: ['neutral_ack'], waiting: CUE_PHRASES.filter(id => id !== 'neutral_ack') });

export class DuplexCoordinator {
  constructor({ selectCue, playback, now = () => performance.now(),
    setTimeout: schedule = (...args) => setTimeout(...args),
    clearTimeout: cancel = timer => clearTimeout(timer), onEvent = () => {},
    listeningDelayMs = 300, waitingDelayMs = 550, decisionDeadlineMs = 850,
    cueTtlMs = 1000, maxCueDurationMs = 2500, cooldownMs = 15000, waitingCooldownMs = 1000,
    followUpSilenceMs = 1250, maxCues = 4, maxRequests = 64,
    budgetWindowMs = 15000, maxRequestsPerUtterance = 8,
    minimumRequestIntervalMs = 350, minimumCueIntervalMs = 300,
    minimumConfidence = .9 } = {}) {
    if (typeof selectCue !== 'function' || !playback || typeof playback.playCue !== 'function'
      || typeof playback.enqueueMain !== 'function' || typeof playback.stopMain !== 'function') {
      throw new TypeError('duplex_dependencies_required');
    }
    for (const value of [listeningDelayMs, waitingDelayMs, cooldownMs, waitingCooldownMs]) {
      if (!Number.isFinite(value) || value < 0) throw new RangeError('invalid_duplex_timing');
    }
    if (!Number.isFinite(decisionDeadlineMs) || decisionDeadlineMs <= 0 || decisionDeadlineMs > 1000
      || !Number.isFinite(cueTtlMs) || cueTtlMs <= 0 || cueTtlMs > 2000
      || !Number.isFinite(maxCueDurationMs) || maxCueDurationMs <= 0 || maxCueDurationMs > 2500
      || !Number.isSafeInteger(maxCues) || maxCues < 0 || maxCues > 20
      || !Number.isSafeInteger(maxRequests) || maxRequests < 0 || maxRequests > 100
      || !Number.isFinite(budgetWindowMs) || budgetWindowMs < 1000 || budgetWindowMs > 60000
      || !Number.isSafeInteger(maxRequestsPerUtterance) || maxRequestsPerUtterance < 2 || maxRequestsPerUtterance > 16
      || !Number.isFinite(minimumRequestIntervalMs) || minimumRequestIntervalMs < 300 || minimumRequestIntervalMs > 2000
      || !Number.isFinite(minimumCueIntervalMs) || minimumCueIntervalMs < 300 || minimumCueIntervalMs > 2000
      || !Number.isFinite(followUpSilenceMs) || followUpSilenceMs < 1250 || followUpSilenceMs > 2000
      || !Number.isFinite(minimumConfidence) || minimumConfidence < .9 || minimumConfidence > 1) {
      throw new RangeError('invalid_duplex_limits');
    }
    Object.assign(this, { selectCue, playback, now, schedule, cancel, onEvent,
      listeningDelayMs, waitingDelayMs, decisionDeadlineMs, cueTtlMs, maxCueDurationMs, cooldownMs, waitingCooldownMs, followUpSilenceMs,
      maxCues, maxRequests, minimumConfidence, minimumRequestIntervalMs, minimumCueIntervalMs });
    Object.assign(this, { budgetWindowMs, maxRequestsPerUtterance });
    this.epoch = 0; this.utteranceId = 0; this.closed = false; this.userSpeaking = false;
    this.cuedUtteranceByPhase = { listening: -1, waiting: -1 };
    this.waitingAttemptUtteranceId = -1;
    this.waitingAttemptNumber = -1; this.waitingCueCount = 0; this.waitingCompletedAt = null; this.lastWaitingPhrase = null;
    this.waitingAudioStarted = false;
    this.lastCueAtByPhase = { listening: -Infinity, waiting: -Infinity };
    this.sensitive = false; this.listeningBlocked = false; this.text = ''; this.revision = null; this.finalReceived = false;
    this.generationId = null; this.generating = false; this.mainHasAudio = false; this.mainAnnounced = false;
    this.turnYieldedAt = null; this.generationBeganAt = null; this.lastCueEndedAt = -Infinity;
    this.pendingTimer = null; this.pendingPhase = null; this.request = null; this.cue = null;
    this.cuesPlayed = 0; this.requests = 0; this.lastCueAt = -Infinity; this.lastRequestAt = -Infinity;
    this.cueTimes = []; this.requestTimes = []; this.utteranceRequests = { listening: 0, waiting: 0 };
    this.preparationRequests = 0;
    this.retryableWaitingUtteranceId = -1;
    this.waitingPacingRetryUsed = false; this.waitingSelectionNotBefore = -Infinity;
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
      this.emit('cue_canceled', { reason, phase: this.request.phase, decision_ready: Boolean(this.request.decision) });
      this.cancel(this.request.timer); this.request.controller.abort(); this.request = null;
    }
    if (stopCue) this.stopCue(reason);
  }

  userSpeechStarted() {
    if (this.closed || this.userSpeaking) return;
    this.invalidate('caller_started');
    this.utteranceId += 1;
    this.userSpeaking = true; this.text = ''; this.revision = null; this.finalReceived = false; this.turnYieldedAt = null;
    this.utteranceRequests = { listening: 0, waiting: 0 };
    this.preparationRequests = 0;
    this.waitingCueCount = 0; this.waitingCompletedAt = null; this.lastWaitingPhrase = null; this.waitingAttemptNumber = -1;
    this.waitingAudioStarted = false;
    this.retryableWaitingUtteranceId = -1;
    this.waitingPacingRetryUsed = false; this.waitingSelectionNotBefore = -Infinity;
    // Old generation IDs remain invalid even if late PCM arrives after speech ends.
    this.generationId = null; this.generating = false; this.mainHasAudio = false; this.mainAnnounced = false;
    this.playback.stopMain();
    this.emit('caller_started');
  }

  resumeUserSpeech() {
    if (this.closed || this.userSpeaking) return;
    // ASR clause boundaries are not new conversational turns. Preserve the
    // stable utterance, its text and any already-played listening allowance.
    this.invalidate('caller_resumed', { stopCue: false });
    if (this.cue?.phase === 'waiting') this.stopCue('caller_resumed');
    this.userSpeaking = true; this.finalReceived = false; this.turnYieldedAt = null;
    this.generationId = null; this.generating = false; this.mainHasAudio = false; this.mainAnnounced = false;
    this.playback.stopMain(); this.emit('caller_resumed');
    if (this.utteranceId !== this.cuedUtteranceByPhase.waiting) this.waitingAttemptUtteranceId = -1;
  }

  hasWaitingAudioStarted() {
    if (this.waitingAudioStarted) return true;
    if (this.cue?.phase === 'waiting') {
      try { this.waitingAudioStarted = this.cue.handle.hasAudiblyStarted?.() === true; } catch { /* Unknown playback stays conservative. */ }
    }
    return this.waitingAudioStarted;
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
    this.invalidate('generation_started', { stopCue: this.generationId !== null }); this.playback.stopMain();
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
    this.emit('cue_gate_changed', { reason: 'sensitive_state', blocked: this.sensitive });
    // Only a fresh observation may schedule another cue after a sensitive state.
  }

  setListeningBlocked(value) {
    if (this.closed || Boolean(value) === this.listeningBlocked) return;
    this.listeningBlocked = Boolean(value);
    this.emit('cue_gate_changed', { reason: 'listening_context', blocked: this.listeningBlocked, phase: 'listening' });
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
    this.pruneBudget();
    return Object.hasOwn(PHRASE_BY_PHASE, phase) && !this.closed && epoch === this.epoch
      && !this.sensitive && !this.mainHasAudio && !this.mainAnnounced
      && (phase === 'waiting' ? this.waitingCueCount < 2 && (this.waitingCueCount === 0 || this.waitingCompletedAt !== null)
        : this.utteranceId !== this.cuedUtteranceByPhase[phase])
      && (phase === 'listening' ? !this.listeningBlocked && this.userSpeaking && !this.finalReceived
        : !this.userSpeaking && this.generating && Boolean(this.generationId));
  }

  pruneBudget() {
    const cutoff = this.now() - this.budgetWindowMs;
    while (this.cueTimes.length && this.cueTimes[0] <= cutoff) this.cueTimes.shift();
    while (this.requestTimes.length && this.requestTimes[0] <= cutoff) this.requestTimes.shift();
  }

  utteranceRequestCount() {
    return this.utteranceRequests.listening + this.utteranceRequests.waiting + this.preparationRequests;
  }

  reservePreparationBudget() {
    // Nonplaying preparation shares the actual classification budget. Keep one
    // final waiting attempt available; it must never depend on a cache hit.
    if (this.closed || !this.userSpeaking || this.sensitive || this.mainHasAudio || this.request
      || this.preparationRequests >= 2 || this.utteranceRequestCount() >= this.maxRequestsPerUtterance - 1
      || this.budgetWait() > 0 || this.now() - this.lastRequestAt < this.minimumRequestIntervalMs) return false;
    this.preparationRequests += 1; this.requests += 1;
    this.lastRequestAt = this.now(); this.requestTimes.push(this.lastRequestAt);
    return true;
  }

  budgetWait(includeRequests = true) {
    this.pruneBudget();
    if (!this.maxCues || !this.maxRequests) return Infinity;
    return Math.max(0,
      this.cueTimes.length >= this.maxCues ? this.cueTimes[0] + this.budgetWindowMs - this.now() : 0,
      includeRequests && this.requestTimes.length >= this.maxRequests ? this.requestTimes[0] + this.budgetWindowMs - this.now() : 0);
  }

  cueWait(phase) {
    const now = this.now();
    const yielded = this.turnYieldedAt ?? this.generationBeganAt ?? now;
    return Math.max(0, this.lastCueAtByPhase[phase] + (phase === 'waiting' ? this.waitingCooldownMs : this.cooldownMs) - now,
      this.lastCueEndedAt + this.minimumCueIntervalMs - now,
      phase === 'waiting' && this.waitingCueCount === 1 ? this.waitingCompletedAt + this.followUpSilenceMs - now : 0,
      phase === 'waiting' ? yielded + this.waitingDelayMs - now : 0);
  }

  eligible(phase, epoch) {
    return this.canCue(phase, epoch) && !this.cue && this.cueWait(phase) <= 0 && this.budgetWait(false) <= 0;
  }

  selectionWait(phase) {
    // Waiting classification overlaps the550ms playback guard once any active
    // clip has finished. Do not spend its absolute TTL behind an entire clip.
    const followUpLead = this.waitingCueCount === 1 ? 650 : 0;
    return phase === 'waiting' ? Math.max(0, this.lastCueAtByPhase[phase] + this.waitingCooldownMs - this.now(),
      this.waitingCueCount === 1 ? this.waitingCompletedAt + this.followUpSilenceMs - this.now() - followUpLead : 0,
      this.lastCueAt + this.minimumCueIntervalMs - this.now(), this.waitingSelectionNotBefore - this.now()) : this.cueWait(phase);
  }

  queueCue(phase, delay) {
    const epoch = this.epoch;
    if (!this.canCue(phase, epoch) || this.cue || !this.text.trim()
      || this.text.split(/\s+/).length < (phase === 'waiting' ? 1 : 3)
      || this.utteranceRequestCount() >= this.maxRequestsPerUtterance
      || (phase === 'listening' && this.utteranceRequests.listening + this.preparationRequests >= this.maxRequestsPerUtterance - 1)
      || (phase === 'waiting' && this.waitingAttemptUtteranceId === this.utteranceId && this.waitingAttemptNumber === this.waitingCueCount)
      || this.pendingTimer !== null || this.request) return;
    const wait = Math.max(phase === 'waiting' ? 0 : delay, this.selectionWait(phase), this.budgetWait(),
      this.minimumRequestIntervalMs - (this.now() - this.lastRequestAt));
    if (!Number.isFinite(wait)) return;
    // A rolling budget cannot turn one follow-up into a surprise cue many
    // seconds later. A missed bounded opportunity stays visibly silent.
    if (phase === 'waiting' && this.waitingCueCount === 1 && this.now() + wait > this.waitingCompletedAt + 1500) {
      this.waitingAttemptUtteranceId = this.utteranceId; this.waitingAttemptNumber = this.waitingCueCount;
      this.emit('cue_suppressed', { reason: 'follow_up_budget_deadline', phase }); return;
    }
    this.pendingPhase = phase;
    this.pendingTimer = this.schedule(() => {
      this.pendingTimer = null; this.pendingPhase = null;
      if (!this.canCue(phase, epoch) || this.cue) return;
      // Browser timers may fire just before a fractional performance-clock
      // deadline. Retain the one valid attempt instead of silently losing it.
      if (this.selectionWait(phase) > 0 || this.budgetWait() > 0 || this.now() - this.lastRequestAt < this.minimumRequestIntervalMs) {
        this.queueCue(phase, 0); return;
      }
      void this.choose(phase, epoch);
    }, Math.ceil(wait));
  }

  async choose(phase, epoch) {
    if (!this.canCue(phase, epoch) || this.cue || (phase !== 'waiting' && !this.eligible(phase, epoch))
      || (phase === 'waiting' && this.waitingAttemptUtteranceId === this.utteranceId && this.waitingAttemptNumber === this.waitingCueCount)
      || this.selectionWait(phase) > 0 || this.budgetWait() > 0 || this.request
      || this.utteranceRequestCount() >= this.maxRequestsPerUtterance
      || (phase === 'listening' && this.utteranceRequests.listening + this.preparationRequests >= this.maxRequestsPerUtterance - 1)) return;
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
    const request = { controller, timer: null, snapshotText: snapshot.text, snapshot, phase, epoch, began, decision: null,
      followUp: phase === 'waiting' && this.waitingCueCount === 1 };
    this.lastRequestAt = began;
    this.request = request; this.requests += 1;
    this.requestTimes.push(began); this.utteranceRequests[phase] += 1;
    if (phase === 'waiting') { this.waitingAttemptUtteranceId = this.utteranceId; this.waitingAttemptNumber = this.waitingCueCount; }
    request.timer = this.schedule(() => {
      controller.abort();
      if (this.request === request) this.request = null;
      this.emit('cue_suppressed', { reason: 'classifier_deadline', phase });
      followup();
    }, this.decisionDeadlineMs);
    let decision;
    try {
      decision = await this.selectCue({ phase, text: snapshot.text, epoch, utteranceId: this.utteranceId, revision: snapshot.revision,
        generationId: this.generationId, signal: controller.signal, followUp: request.followUp });
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
    const maxAge = decision?.max_request_age_ms === undefined ? ttl
      : Math.min(this.cueTtlMs, 800, Number(decision.max_request_age_ms));
    const expiresAt = Math.min(began + maxAge, began + this.decisionDeadlineMs, this.now() + ttl);
    let rejected = null;
    if (phraseId === 'silence') rejected = /^[a-z_]{1,60}$/.test(decision?.reason ?? '') ? decision.reason : 'classifier_silence';
    else if (!PHRASE_BY_PHASE[phase].includes(phraseId)) rejected = 'selection_phase_mismatch';
    else if (request.followUp && phraseId === this.lastWaitingPhrase) rejected = 'selection_repeated_phrase';
    else if (!Number.isFinite(decision?.confidence) || decision.confidence < this.minimumConfidence) rejected = 'selection_confidence';
    else if (decision.language_choice !== 'cache_compatible') rejected = 'selection_language';
    else if (!Number.isFinite(ttl) || ttl <= 0 || !Number.isFinite(maxAge) || maxAge <= 0 || this.now() >= expiresAt) rejected = 'selection_expired_on_arrival';
    if (rejected) {
      if (phase === 'waiting' && phraseId === 'silence' && rejected === 'unverified_or_sensitive_context') {
        this.retryableWaitingUtteranceId = this.utteranceId;
      }
      this.request = null;
      this.emit('cue_suppressed', { reason: rejected, phase, selection_ms: this.now() - began });
      // A preclassification pacing refusal has no accepted cue to resurrect.
      // At most one fresh, re-authorized waiting request may use the remaining
      // per-turn budget. Uncertainty, expiry or a reservation never retries.
      if (phase === 'waiting' && !request.followUp && phraseId === 'silence' && decision.requests === 0
        && ['classification_throttled', 'cue_spacing'].includes(rejected)
        && Number.isInteger(decision.retry_after_ms) && decision.retry_after_ms >= 1 && decision.retry_after_ms <= 1000
        && !this.waitingPacingRetryUsed && this.canCue(phase, epoch)
        && this.utteranceRequestCount() < this.maxRequestsPerUtterance) {
        this.waitingPacingRetryUsed = true; this.waitingAttemptUtteranceId = -1;
        this.waitingSelectionNotBefore = this.now() + decision.retry_after_ms;
        this.emit('cue_retry_scheduled', { phase, reason: rejected, retry_after_ms: decision.retry_after_ms });
        this.queueCue('waiting', 0); return;
      }
      followup(); return;
    }
    request.decision = decision;
    request.selectedAt = this.now();
    // Neither the TTL nor client deadline rolls forward when a result is held.
    request.expiresAt = expiresAt;
    this.playReady(request);
  }

  wakeWaiting() {
    if (this.request?.phase === 'waiting' && this.request.decision) this.playReady(this.request);
    else this.queueCue('waiting', 0);
  }

  recoverWaitingContext() {
    // Only an authoritative hard-block -> safe snapshot transition can reopen
    // this specific failed context preflight. No timer retries stable silence,
    // uncertainty, expired or accepted decisions, or an unresolved operation.
    if (this.retryableWaitingUtteranceId !== this.utteranceId || this.request
      || this.userSpeaking || this.sensitive || this.mainHasAudio
      || this.cuedUtteranceByPhase.waiting === this.utteranceId) return;
    this.retryableWaitingUtteranceId = -1; this.waitingAttemptUtteranceId = -1;
    this.queueCue('waiting', 0);
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
      const wakeAt = this.cue ? request.expiresAt : Math.min(request.expiresAt,
        this.now() + Math.max(this.cueWait(phase), this.budgetWait(false)));
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
    if (phase === 'waiting') { this.waitingCueCount += 1; this.waitingCompletedAt = null; this.lastWaitingPhrase = phraseId; }
    this.cueTimes.push(this.now());
    this.lastCueAtByPhase[phase] = this.now(); this.lastCueAt = this.now();
    this.emit('cue_started', { phase, phrase_id: phraseId, selection_ms: request.selectedAt - began,
      guard_hold_ms: this.now() - request.selectedAt, decision_age_ms: this.now() - began,
      snapshot_revision: snapshot.revision, latest_revision: this.revision, follow_up: request.followUp });
    Promise.resolve(handle.ended).then(() => true, () => false).then(completed => {
      this.cancel(cue.expiryTimer);
      if (this.cue === cue) {
        this.hasWaitingAudioStarted();
        if (phase === 'waiting' && completed) this.waitingCompletedAt = this.now();
        this.cue = null; this.lastCueEndedAt = this.now(); this.emit('cue_ended', { phrase_id: phraseId });
        this.wakeWaiting();
      }
    });
  }

  receiveMainPcm(generationId, pcm, sampleRate) {
    if (this.closed || this.userSpeaking || !generationId || generationId !== this.generationId) {
      this.emit('main_discarded', { reason: 'obsolete_generation_or_caller_speaking' }); return false;
    }
    // First PCM revokes all future selections. The output owner lets the current
    // validated whole clip finish behind one fixed, <=1100ms audio-clock barrier.
    if (!this.mainHasAudio) this.invalidate('main_audio_ready', { stopCue: false });
    this.mainHasAudio = true;
    try {
      const accepted = this.playback.enqueueMain({ pcm, sampleRate, generationId }) === true;
      const handoff = this.playback.takeHandoffTelemetry?.();
      if (handoff) this.emit('main_handoff', handoff);
      if (!accepted) this.emit('main_discarded', { reason: 'playback_rejected' });
      return accepted;
    }
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

// One output clock. A main answer queues behind the current complete short cue;
// caller cancellation remains immediate and independent from this handoff.
export class PcmPlayback {
  constructor({ context, cueAssets = {}, maxQueuedSeconds = 3, onFailure = () => {} } = {}) {
    if (!context || !Number.isFinite(maxQueuedSeconds) || maxQueuedSeconds <= 0 || maxQueuedSeconds > 10) {
      throw new TypeError('invalid_pcm_playback');
    }
    if (typeof onFailure !== 'function') throw new TypeError('invalid_playback_failure_handler');
    this.context = context; this.cueAssets = cueAssets; this.maxQueuedSeconds = maxQueuedSeconds; this.onFailure = onFailure;
    this.main = new Set(); this.pausedMain = null; this.cue = null; this.mainEnd = 0;
    this.cueBarrier = 0; this.cueFadeSeconds = .008; this.closed = false;
    this.maxHandoffSeconds = 1.1; this.mainHandoffBarrier = null; this.handoffTelemetry = null;
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
    const handle = { stop, ended, buffer, when, offset, stopAt: null, naturalEnd: when + buffer.duration - offset };
    source.onended = finish;
    nodes.add(handle);
    try { source.start(when, offset); started = true; } catch (error) { stop(); throw error; }
    return handle;
  }

  playCue(phraseId) {
    if (this.closed || this.main.size || this.pausedMain || this.context.state !== 'running'
      || !Number.isFinite(this.context.currentTime) || this.context.currentTime < 0
      || this.context.currentTime < this.cueBarrier || !CUE_PHRASES.includes(phraseId)
      || (this.cue && (this.cue.stopAt ?? this.cue.naturalEnd) > this.context.currentTime)) return null;
    const asset = this.cueAssets instanceof Map ? this.cueAssets.get(phraseId) : this.cueAssets[phraseId];
    if (!asset) return null;
    const buffer = this.buffer(asset.pcm, asset.sampleRate);
    // Admit the whole asset before making a source audible. A too-long phrase
    // cannot be made safe by cutting its ending when the main answer arrives.
    if (!Number.isFinite(buffer.duration) || buffer.duration <= 0 || buffer.duration > this.maxHandoffSeconds) return null;
    this.cue?.stop();
    const nodes = new Set(), handle = this.node(buffer, this.context.currentTime, nodes, 0, true);
    const samples = buffer.getChannelData?.(0) ?? pcmSamples(asset.pcm);
    const firstVoiced = samples.findIndex(sample => Math.abs(sample) > 64 / 32768);
    const voicedAt = firstVoiced < 0 ? Infinity : handle.when + firstVoiced / asset.sampleRate;
    handle.hasAudiblyStarted = () => this.context.state === 'running' && this.context.currentTime >= voicedAt
      && (handle.stopAt === null || handle.stopAt > voicedAt);
    this.cue = handle;
    handle.ended.finally(() => { if (this.cue === handle) this.cue = null; });
    return handle;
  }

  enqueueMain({ pcm, sampleRate }) {
    if (this.closed || this.pausedMain) return false;
    if (this.context.state !== 'running' || !Number.isFinite(this.context.currentTime) || this.context.currentTime < 0) {
      return this.fail('output_context_unavailable');
    }
    let buffer;
    try { buffer = this.buffer(pcm, sampleRate); }
    catch (error) { this.fail('output_geometry'); throw error; }
    const now = this.context.currentTime;
    if (this.mainHandoffBarrier === null) {
      const end = this.cue ? this.cue.stopAt ?? this.cue.naturalEnd : now;
      if (!Number.isFinite(end) || end - now > this.maxHandoffSeconds + 1e-9) return this.fail('output_handoff_bound');
      this.mainHandoffBarrier = Math.max(now, end, this.cueBarrier);
      this.handoffTelemetry = { ready_at_s: now, scheduled_at_s: this.mainHandoffBarrier,
        hold_ms: Math.max(0, this.mainHandoffBarrier - now) * 1000 };
    }
    // This barrier is fixed by the first packet; later chunks never extend it.
    const start = Math.max(now, this.mainEnd, this.mainHandoffBarrier, this.cueBarrier);
    if (start + buffer.duration - now > this.maxQueuedSeconds) return this.fail('output_queue_bound');
    try { this.node(buffer, start, this.main); this.mainEnd = start + buffer.duration; }
    catch (error) { this.fail('output_playback_failed'); throw error; }
    return true;
  }

  fail(reason) {
    try { this.onFailure(reason); } catch { /* Failure observation cannot revive output. */ }
    return false;
  }

  takeHandoffTelemetry() {
    const value = this.handoffTelemetry; this.handoffTelemetry = null; return value;
  }

  stopMain() {
    this.pausedMain = null;
    for (const handle of [...this.main]) handle.stop();
    this.mainEnd = this.context.currentTime;
    this.mainHandoffBarrier = null; this.handoffTelemetry = null;
  }

  pauseMain() {
    if (this.closed || this.pausedMain) return;
    const now = this.context.currentTime, remaining = [];
    for (const handle of this.main) {
      const offset = handle.offset + Math.max(0, now - handle.when);
      if (offset < handle.buffer.duration) remaining.push({ buffer: handle.buffer, offset });
    }
    const barrier = this.mainHandoffBarrier;
    this.stopMain(); this.pausedMain = remaining; this.mainHandoffBarrier = barrier;
  }

  resumeMain() {
    if (this.closed || this.context.state !== 'running') return false;
    const remaining = this.pausedMain;
    if (!remaining) return true;
    this.pausedMain = null;
    this.mainEnd = Math.max(this.context.currentTime, this.mainHandoffBarrier ?? 0, this.cueBarrier);
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
