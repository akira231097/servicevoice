// Isolated browser transport: continuous caller PCM, one owned output clock.
import { DuplexCoordinator, PcmPlayback } from './duplex-coordinator.js';
import { DuplexMicrophone } from './duplex-microphone.js';

const routineService = /\boil\s+changes?\b|\bchange\s+(?:(?:the|my|engine)\s+)?oil\b|\bwait(?:ing)?\s+times?\b/gi;
const symptomNegation = /\bno\s+(?:power|heat|cooling)\b|\b(?:car|vehicle|engine|motor|starter|truck|van|suv)\s+(?:(?:is|has|gets)\s+(?:no|not)\s+(?:power|heat|cooling|starting|working)|(?:do(?:es)?\s+not|don['’]?t|doesn['’]?t|cannot|can['’]?t|won['’]?t|will\s+not)\s+(?:start|run|work|turn\s+over))\b/gi;
const vehicleContext = /\b(?:car|vehicle|model|year|engine|truck|van|suv|sedan|hatchback|coupe|toyota|honda|ford|chevrolet|chevy|nissan|hyundai|kia|bmw|audi|tesla|subaru|mazda|mercedes|volkswagen|volvo|lexus|jeep|gmc|dodge|ram|buick|cadillac|acura|infiniti|mitsubishi|porsche|mini)\b/i;
const sensitive = text => {
  const gate = text.replace(routineService, 'routine service').replace(symptomNegation, 'vehicle symptom');
  const numbers = vehicleContext.test(text) ? text.replace(/\b(?:19|20)\d{2}\b/g, 'vehicle year') : text;
  return /\b(?:stop|wait|actually|instead|correct|change|cancel|confirm|yes|no|not|never|unless|only\s+if|do\s+not|don['’]t|can['’]t|cannot|won['’]t|goodbye|bye|emergency|urgent|ignore|instructions?|password|passcode|pin|card|social security)\b/i.test(gate)
    || /\d{4,}/.test(numbers) || /@|(?:\d[\s().+-]*){7,}|[^\x00-\x7f]/.test(text)
    || /\b(?:speak|reply|respond|answer|talk)\b.{0,35}\b(?:in|using)\s+[a-z]+/i.test(text);
};
const bytes = value => new Uint8Array(value);
const peak = pcm => {
  const view = new DataView(pcm.buffer, pcm.byteOffset, pcm.byteLength);
  let result = 0;
  for (let i = 0; i + 1 < pcm.byteLength; i += 2) result = Math.max(result, Math.abs(view.getInt16(i, true)));
  return result;
};
const speechText = text => text.toLowerCase().replace(/[.,!?;:]+/g, '').replace(/\s+/g, ' ').trim();

export class DuplexSession {
  constructor({ callbacks = {}, runtime = {} } = {}) {
    this.callbacks = callbacks;
    this.runtime = {
      fetch: (...args) => fetch(...args), AudioContext: globalThis.AudioContext,
      WebSocket: globalThis.WebSocket, getUserMedia: options => navigator.mediaDevices.getUserMedia(options),
      microphoneFactory: options => new DuplexMicrophone(options),
      playbackFactory: options => new PcmPlayback(options),
      now: () => performance.now(), setTimeout: (...args) => setTimeout(...args), clearTimeout: id => clearTimeout(id),
      ...runtime,
    };
    this.events = []; this.closed = false; this.started = false; this.active = false;
    this.callerSpeaking = false; this.localSpeaking = false; this.providerSpeaking = false;
    this.mainSpeaking = false; this.mainAllowed = false; this.generation = null; this.generationSequence = 0;
    this.revision = 0; this.cueRevision = 0; this.partial = ''; this.lastFinal = ''; this.userStopTimer = null;
    this.pendingAudio = []; this.audioBytes = 0; this.inputFrames = 0; this.inputFramesDuringCue = 0;
    this.inputFramesDuringMain = 0; this.discardedOutputBytes = 0; this.muted = false; this.pollTimer = null;
    this.cuePlaying = false; this.startAt = this.runtime.now(); this.gapAt = null;
    this.answerGaps = []; this.inputBytes = 0; this.evidenceRequests = new Set();
    this.endedSession = false; this.providerStop = null; this.stopping = null; this.handshakeCancel = null;
    this.lastProviderUserTurn = null;
    this.snapshotHardBlocked = false; this.snapshotReadSequence = 0;
    this.provisional = null;
    this.lastUserStoppedAt = -Infinity; this.lastUserTranscriptFinal = false;
    this.falseEnergyCount = 0; this.energyCooldownUntil = -Infinity;
    this.provisionalAttempts = 0;
    this.ignoredEnergyOnsets = { no_output: 0, cue_only: 0, cooldown: 0 };
    this.finalSegments = []; this.haveUserUtterance = false; this.userTurnHasMainAudio = false;
    this.userTurnClosed = false; this.generationAnnouncedAfterUser = false;
    this.waitingPreparationTimer = null; this.waitingPreparation = null; this.waitingPrepared = null;
    this.preparationTextChangedAt = -Infinity; this.preparationAttemptedTexts = new Set();
    this.waitingPreparationRequests = 0; this.lastWaitingPreparationAt = -Infinity;
    this.waitingPreparationReuses = 0;
  }

  emit(kind, detail = {}) {
    const event = { kind, at_ms: Math.max(0, this.runtime.now() - this.startAt), ...detail };
    if (this.events.length < 1500) this.events.push(event);
    this.callbacks.event?.(event);
    if (kind === 'cue_started') this.cuePlaying = true;
    if (['cue_ended', 'cue_stopped', 'ended'].includes(kind)) this.cuePlaying = false;
    if (['cue_started', 'cue_ended', 'cue_suppressed'].includes(kind)) this.queueWaitingPreparation();
    if (this.session && ['cue_started', 'cue_stopped', 'cue_ended', 'cue_canceled', 'cue_suppressed', 'cue_gate_changed', 'cue_retry_scheduled',
      'barge_in', 'main_started', 'ended', 'muted'].includes(kind)) {
      const body = { event: kind, at_ms: Math.min(event.at_ms, 360000), epoch: this.coordinator?.epoch ?? 0 };
      if (['neutral_ack', 'moment', 'silence'].includes(event.phrase_id)) body.phrase_id = event.phrase_id;
      if (['listening', 'waiting'].includes(event.phase)) body.phase = event.phase;
      if (/^[a-z_]{1,60}$/.test(event.reason ?? '')) body.reason = event.reason;
      if (typeof event.blocked === 'boolean') body.blocked = event.blocked;
      if (Number.isInteger(event.retry_after_ms) && event.retry_after_ms >= 1 && event.retry_after_ms <= 1000) body.retry_after_ms = event.retry_after_ms;
      const sent = this.request('/duplex/event', { method: 'POST', body: JSON.stringify(body) }).catch(() => {});
      this.evidenceRequests.add(sent); sent.finally(() => this.evidenceRequests.delete(sent));
    }
  }

  async request(path, options = {}, absolute = false) {
    const url = absolute ? path : `/api/sessions/${this.session.id}${path}`;
    const response = await this.runtime.fetch(url, { ...options, headers: {
      'Content-Type': 'application/json', ...(this.session ? { 'x-session-token': this.session.token } : {}), ...options.headers,
    } });
    if (!response.ok) throw new Error(`request_${response.status}`);
    return response.json();
  }

  cancelWaitingPreparation(reason, { forget = true } = {}) {
    this.runtime.clearTimeout(this.waitingPreparationTimer); this.waitingPreparationTimer = null;
    const pending = this.waitingPreparation; this.waitingPreparation = null;
    if (pending) {
      this.runtime.clearTimeout(pending.timer); pending.controller.abort();
      pending.finish();
      this.emit('waiting_preparation_canceled', { reason });
    }
    if (forget) this.waitingPrepared = null;
  }

  async selectCue(decision) {
    const pending = this.waitingPreparation, joinBegan = this.runtime.now();
    let joinMs = 0;
    const join = decision.phase === 'waiting' && pending && pending.text === decision.text
      && pending.utteranceId === decision.utteranceId && !pending.controller.signal.aborted;
    if (join) {
      // This promise belongs to the coordinator's ORIGINAL850ms selection
      // deadline. Main PCM and raw/caller changes abort it synchronously.
      await new Promise(resolve => {
        const abort = () => { this.cancelWaitingPreparation('selection_canceled'); resolve(); };
        decision.signal.addEventListener('abort', abort, { once: true });
        pending.completion.then(() => { decision.signal.removeEventListener('abort', abort); resolve(); });
        if (decision.signal.aborted) abort();
      });
      joinMs = Math.max(0, this.runtime.now() - joinBegan);
      this.emit('waiting_preparation_joined', { join_ms: joinMs });
    }
    if (decision.signal.aborted || this.closed) throw new Error('selection_canceled');
    const prepared = this.waitingPrepared;
    const canReuse = decision.phase === 'waiting' && prepared
      && prepared.utteranceId === decision.utteranceId && prepared.text === decision.text
      && this.runtime.now() < prepared.expiresAt;
    // Only a completed, successful receipt binds reuse. A different-text or
    // listening selection immediately revokes optional prediction work.
    this.cancelWaitingPreparation('selection_started');
    const result = await this.request('/duplex/cue', { method: 'POST', signal: decision.signal,
      body: JSON.stringify({ text: decision.text, phase: decision.phase, epoch: decision.utteranceId,
        revision: ++this.cueRevision, ...(canReuse ? { prepared_revision: prepared.revision } : {}) }) });
    if (!decision.signal.aborted && result?.prepared_reused === true && canReuse) {
      this.waitingPreparationReuses += 1;
      this.emit('waiting_preparation_reused', { join_ms: joinMs });
    }
    return result;
  }

  queueWaitingPreparation() {
    const coordinator = this.coordinator;
    if (this.closed || !this.active || this.muted || !this.callerSpeaking || this.snapshotHardBlocked
      || sensitive(this.partial) || !this.partial || !coordinator || coordinator.sensitive
      || coordinator.preparationRequests >= 2 || coordinator.utteranceRequestCount() >= coordinator.maxRequestsPerUtterance - 1
      || coordinator.cuedUtteranceByPhase.waiting === coordinator.utteranceId
      || this.waitingPreparation || this.waitingPreparationTimer !== null
      || this.preparationAttemptedTexts.has(this.partial)) return;
    // Retain the second bounded opportunity for a final/quiet tail instead of
    // spending both predictions on early growing clauses in a long utterance.
    if (coordinator.preparationRequests > 0 && !this.lastUserTranscriptFinal
      && (this.providerSpeaking || this.localSpeaking)) return;
    const now = this.runtime.now();
    // Exact full-text stability is separate from the listening prefix. A short
    // name can be prepared, while raw corrections/consent still gate every word.
    const stableAt = this.lastUserTranscriptFinal ? now : this.preparationTextChangedAt + 250;
    const wait = Math.max(0, stableAt - now,
      coordinator.lastRequestAt + coordinator.minimumRequestIntervalMs - now, coordinator.budgetWait(),
      coordinator.preparationRequests > 0 ? this.lastWaitingPreparationAt + 1000 - now : 0);
    if (!Number.isFinite(wait)) return;
    this.waitingPreparationTimer = this.runtime.setTimeout(() => {
      this.waitingPreparationTimer = null;
      if (coordinator.request || (coordinator.pendingPhase === 'listening' && coordinator.cueWait('listening') <= 0)) {
        // Let an already queued listening request start first. This bounded
        // timer only retries scheduling; it performs no network/model work.
        this.waitingPreparationTimer = this.runtime.setTimeout(() => {
          this.waitingPreparationTimer = null; this.queueWaitingPreparation();
        }, coordinator.minimumRequestIntervalMs);
        return;
      }
      if (this.closed || !this.callerSpeaking || this.muted || this.snapshotHardBlocked || sensitive(this.partial)) return;
      if (coordinator.preparationRequests > 0 && !this.lastUserTranscriptFinal
        && (this.providerSpeaking || this.localSpeaking)) return;
      if ((!this.lastUserTranscriptFinal && this.runtime.now() < this.preparationTextChangedAt + 250)
        || this.runtime.now() - coordinator.lastRequestAt < coordinator.minimumRequestIntervalMs
        || (coordinator.preparationRequests > 0 && this.runtime.now() < this.lastWaitingPreparationAt + 1000)) {
        this.queueWaitingPreparation(); return;
      }
      if (!coordinator.reservePreparationBudget()) return;
      void this.prepareWaitingCue();
    }, Math.ceil(wait));
  }

  async prepareWaitingCue() {
    const state = { text: this.partial, utteranceId: this.coordinator.utteranceId,
      revision: ++this.cueRevision, controller: new AbortController(), timer: null };
    state.completion = new Promise(resolve => { state.finish = resolve; });
    this.preparationAttemptedTexts.add(state.text); this.waitingPreparation = state;
    this.waitingPreparationRequests += 1; this.lastWaitingPreparationAt = this.runtime.now();
    state.timer = this.runtime.setTimeout(() => {
      if (this.waitingPreparation !== state) return;
      this.cancelWaitingPreparation('preparation_deadline');
    }, 850);
    this.emit('waiting_preparation_started');
    try {
      const decision = await this.request('/duplex/cue', { method: 'POST', signal: state.controller.signal,
        body: JSON.stringify({ text: state.text, phase: 'waiting', epoch: state.utteranceId,
          revision: state.revision, prepare_only: true }) });
      if (this.waitingPreparation !== state || state.controller.signal.aborted || this.closed || this.mainSpeaking || this.userTurnClosed
        || this.partial !== state.text || this.coordinator.utteranceId !== state.utteranceId
        || this.snapshotHardBlocked || sensitive(this.partial) || this.coordinator.sensitive) return;
      // A preparation response never enters the speaker coordinator, even if a
      // malformed server response unexpectedly contains a playable phrase.
      if (decision?.phrase_id !== 'silence' || decision.prepared !== true || decision.reason !== 'waiting_prepared'
        || decision.expires_in_ms !== 0 || decision.revision !== state.revision || decision.epoch !== state.utteranceId
        || !Number.isInteger(decision.prepared_expires_in_ms)
        || decision.prepared_expires_in_ms < 1 || decision.prepared_expires_in_ms > 2000) return;
      this.waitingPrepared = { ...state, expiresAt: this.runtime.now() + decision.prepared_expires_in_ms };
      this.emit('waiting_preparation_ready');
    } catch { /* Prediction is optional; the actual waiting path remains independent. */ }
    finally {
      this.runtime.clearTimeout(state.timer);
      if (this.waitingPreparation === state) this.waitingPreparation = null;
      state.finish();
    }
  }

  async start() {
    if (this.started || this.closed) return;
    this.started = true; this.callbacks.status?.('Connecting');
    try {
      // Permission and output readiness precede any paid call allocation.
      this.context = new this.runtime.AudioContext({ sampleRate: 16000, latencyHint: 'interactive' });
      await this.context.resume();
      if (this.closed) return;
      this.stream = await this.runtime.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }, video: false });
      if (this.closed) { this.releaseTracks(); return; }
      this.session = await this.request('/api/sessions', { method: 'POST', body: '{}' }, true);
      if (this.closed) return;
      const config = await this.request('/api/duplex/config', {}, true);
      if (this.closed) return;
      if (!config.enabled || config.sample_rate !== 16000) throw new Error('duplex_configuration');
      const cueAssets = {};
      for (const item of config.phrases) {
        if (!['neutral_ack', 'moment'].includes(item.phrase_id) || item.sample_rate !== 16000) throw new Error('cue_manifest');
        const response = await this.runtime.fetch(`/api/sessions/${this.session.id}/duplex/audio/${item.phrase_id}`, {
          headers: { 'x-session-token': this.session.token },
        });
        if (!response.ok) throw new Error('cue_audio_unavailable');
        const pcm = await response.arrayBuffer();
        if (this.closed) return;
        if (pcm.byteLength !== item.pcm_bytes || pcm.byteLength > 80000 || pcm.byteLength % 2) throw new Error('cue_audio_geometry');
        const hash = [...new Uint8Array(await crypto.subtle.digest('SHA-256', pcm))].map(x => x.toString(16).padStart(2, '0')).join('');
        if (this.closed) return;
        if (hash !== item.sha256) throw new Error('cue_audio_integrity');
        cueAssets[item.phrase_id] = { pcm, sampleRate: 16000 };
      }
      if (!cueAssets.neutral_ack || !cueAssets.moment) throw new Error('cue_bank_incomplete');
      if (this.closed) return;
      this.playback = this.runtime.playbackFactory({ context: this.context, cueAssets, maxQueuedSeconds: 3 });
      this.coordinator = new DuplexCoordinator({ playback: this.playback, listeningDelayMs: 0,
        now: this.runtime.now, setTimeout: this.runtime.setTimeout, clearTimeout: this.runtime.clearTimeout,
        decisionDeadlineMs: 850, // Includes loopback API + authoritative state reads; provider budget remains350ms.
        selectCue: decision => this.selectCue(decision),
        onEvent: event => { const { kind, at_ms, ...detail } = event; this.emit(kind, detail); },
      });
      this.microphone = this.runtime.microphoneFactory({ audioContext: this.context,
        onFrame: frame => this.sendInput(frame), onSpeech: speaking => this.onLocalSpeech(speaking),
        onLevel: level => this.callbacks.level?.(level), onError: () => { void this.stop('microphone_failed'); },
      });
      await this.microphone.start({ stream: this.stream, ownStream: true });
      if (this.closed) return;
      const call = await this.request('/duplex/call', { method: 'POST', body: '{}' });
      this.callId = call.call_id; this.callUrl = call.websocket_url;
      if (this.closed) return;
      const url = new URL(call.websocket_url);
      if (url.protocol !== 'wss:' || !url.hostname.endsWith('.vapi.ai') || url.username || url.password) throw new Error('transport_url');
      this.socket = new this.runtime.WebSocket(call.websocket_url); this.socket.binaryType = 'arraybuffer';
      this.socket.addEventListener('message', event => this.receive(event.data));
      this.socket.addEventListener('close', () => { if (!this.closed) void this.stop('provider_ended'); });
      await new Promise((resolve, reject) => {
        let settled = false;
        const finish = error => {
          if (settled) return; settled = true; this.runtime.clearTimeout(timer); this.handshakeCancel = null;
          if (error) reject(error); else resolve();
        };
        const timer = this.runtime.setTimeout(() => finish(new Error('transport_timeout')), 12000);
        this.handshakeCancel = () => finish(new Error('transport_cancelled'));
        this.socket.addEventListener('open', () => finish(), { once: true });
        this.socket.addEventListener('error', () => finish(new Error('transport_failed')), { once: true });
        this.socket.addEventListener('close', () => finish(new Error('transport_closed')), { once: true });
      });
      if (this.closed) return;
      this.active = true; this.generation = `playback-${++this.generationSequence}`;
      this.coordinator.beginGeneration(this.generation); this.mainAllowed = true;
      this.watchdog = this.runtime.setTimeout(() => { void this.stop('five_minute_limit'); }, 300000);
      this.callbacks.status?.('Listening'); this.emit('connected'); this.pollSnapshot();
    } catch (error) {
      if (this.closed) return;
      this.callbacks.error?.(String(error?.message || 'connection_failed').replace(/[^a-zA-Z0-9_]/g, '').slice(0,80));
      await this.stop('failed');
      throw error;
    } finally {
      if (this.closed) {
        this.releaseTracks();
        try { await this.microphone?.stop(); } catch { /* Continue all owned cleanup. */ }
        try { await this.context?.close(); } catch { /* Closed during startup. */ }
        await this.endProviderCall(); await this.endLocalSession();
      }
    }
  }

  sendInput(frame) {
    if (this.closed || !this.active || this.socket?.readyState !== 1) return;
    if (this.socket.bufferedAmount > 128000) { void this.stop('input_backpressure'); return; }
    this.socket.send(frame.pcm);
    this.inputFrames += 1; this.inputBytes += frame.pcm.byteLength;
    if (this.cuePlaying) this.inputFramesDuringCue += 1;
    if (this.mainSpeaking) this.inputFramesDuringMain += 1;
  }

  onLocalSpeech(speaking) {
    this.localSpeaking = speaking;
    if (!this.active || this.closed || this.muted) return;
    if (speaking) {
      if (!this.callerSpeaking) this.provisionalBargeIn();
    } else {
      if (this.provisional) this.recoverProvisional('energy_ended');
      this.maybeUserStopped();
      this.queueWaitingPreparation();
    }
  }

  provisionalBargeIn() {
    if (this.provisional || this.closed || this.callerSpeaking) return;
    // Energy alone cannot establish a new caller turn. In particular, ambient
    // noise while waiting must not revoke a valid classifier/held decision.
    if (!this.mainSpeaking && !this.playback?.main?.size) {
      // A cue alone has no main suffix to preserve. Entering the provisional
      // main buffer here would delay newly-ready main PCM by up to350ms.
      this.ignoredEnergyOnsets[this.cuePlaying ? 'cue_only' : 'no_output'] += 1; return;
    }
    if (this.provisionalAttempts >= 2 || this.runtime.now() < this.energyCooldownUntil) {
      this.ignoredEnergyOnsets.cooldown += 1; return;
    }
    const state = { generation: this.generation, mainAllowed: this.mainAllowed,
      wasMainSpeaking: this.mainSpeaking, buffer: [], bytes: 0, timer: null };
    this.provisional = state;
    this.provisionalAttempts += 1;
    // A short cached clip must not lose its ending to unconfirmed noise.
    // Confirmed provider/transcript speech still revokes it immediately.
    this.coordinator.invalidate('provisional_barge_in', { stopCue: false });
    this.playback.pauseMain();
    this.mainSpeaking = false;
    state.timer = this.runtime.setTimeout(() => this.recoverProvisional('confirmation_timeout'), 350);
    this.emit('provisional_barge_in', { source: 'local_energy_heuristic' });
  }

  recoverProvisional(reason) {
    const state = this.provisional;
    if (!state || this.closed) return;
    this.provisional = null; this.runtime.clearTimeout(state.timer);
    if (this.callerSpeaking || this.generation !== state.generation) {
      this.playback.stopMain();
      this.discardedOutputBytes += state.bytes; return;
    }
    if (!this.playback.resumeMain()) {
      this.discardedOutputBytes += state.bytes; void this.stop('output_resume_failed'); return;
    }
    this.mainAllowed = state.mainAllowed; this.mainSpeaking = state.wasMainSpeaking;
    this.falseEnergyCount += 1;
    this.energyCooldownUntil = this.runtime.now() + Math.min(3000, 1000 * this.falseEnergyCount);
    this.emit('provisional_resumed', { reason, buffered_bytes: state.bytes });
    for (const pcm of state.buffer) this.receive(pcm.buffer, { replayed: true });
  }

  confirmProvisional(source) {
    const state = this.provisional;
    if (!state) return;
    this.provisional = null; this.runtime.clearTimeout(state.timer);
    this.discardedOutputBytes += state.bytes; this.playback.stopMain();
    this.emit('provisional_confirmed', { source, discarded_bytes: state.bytes });
  }

  userStarted(source) {
    this.runtime.clearTimeout(this.userStopTimer);
    if (this.callerSpeaking || this.closed) return;
    this.cancelWaitingPreparation('caller_started');
    const hadProvisionalOutput = Boolean(this.provisional?.wasMainSpeaking);
    this.confirmProvisional(source);
    const resumed = this.haveUserUtterance && !this.userTurnHasMainAudio && !this.userTurnClosed
      && this.runtime.now() - this.lastUserStoppedAt < 15000;
    this.callerSpeaking = true;
    if (!resumed) {
      this.partial = ''; this.revision = 0; this.finalSegments = [];
      this.haveUserUtterance = true; this.userTurnHasMainAudio = false;
      this.userTurnClosed = false; this.generationAnnouncedAfterUser = false;
      this.preparationAttemptedTexts.clear();
      this.lastWaitingPreparationAt = -Infinity;
    }
    this.lastUserTranscriptFinal = false; this.falseEnergyCount = 0; this.energyCooldownUntil = -Infinity;
    const wasOutput = this.mainSpeaking || this.cuePlaying || hadProvisionalOutput;
    this.mainAllowed = false; this.mainSpeaking = false; this.pendingAudio = []; this.generation = null;
    if (resumed) this.coordinator.resumeUserSpeech();
    else this.coordinator.userSpeechStarted();
    this.coordinator.setSensitive(this.snapshotHardBlocked || sensitive(this.partial));
    this.callbacks.status?.('Listening');
    this.emit(wasOutput ? 'barge_in' : resumed ? 'caller_resumed' : 'caller_started', { source });
  }

  assembleTranscript(text, final) {
    const committed = this.finalSegments.join(' '), normalized = speechText(text);
    const last = this.finalSegments.at(-1);
    let parts;
    if (committed && (normalized === speechText(committed) || normalized === speechText(last))) {
      return { text: this.partial || committed, duplicate: true };
    }
    if (committed && normalized.startsWith(speechText(committed) + ' ')) parts = [text];
    else if (last && normalized.startsWith(speechText(last) + ' ')) parts = [...this.finalSegments.slice(0, -1), text];
    else parts = [...this.finalSegments, text];
    const combined = parts.join(' ').slice(-1600);
    if (final) this.finalSegments = parts.join(' ').length > 1600 || parts.length > 32 ? [combined] : parts;
    return { text: combined, duplicate: false };
  }

  maybeUserStopped(force = false) {
    this.runtime.clearTimeout(this.userStopTimer);
    if (!this.callerSpeaking || this.providerSpeaking || this.localSpeaking || this.closed) return;
    this.userStopTimer = this.runtime.setTimeout(() => {
      if (this.localSpeaking || this.providerSpeaking || this.closed) return;
      this.endUserTurn();
    }, 160);
  }

  endUserTurn() {
    if (!this.callerSpeaking || this.closed) return;
    // Keep an exact in-flight prediction available for the selector's bounded
    // join. There is still no permission to play before the fresh yield request.
    this.runtime.clearTimeout(this.waitingPreparationTimer); this.waitingPreparationTimer = null;
    this.runtime.clearTimeout(this.userStopTimer); this.userStopTimer = null;
    this.callerSpeaking = false; this.gapAt = this.runtime.now();
    this.lastUserStoppedAt = this.runtime.now();
    this.falseEnergyCount = 0; this.energyCooldownUntil = -Infinity; this.provisionalAttempts = 0;
    if (this.partial) this.coordinator.updateFinal(this.partial, { revision: this.revision });
    this.coordinator.userSpeechStopped();
    this.generation = `playback-${++this.generationSequence}`;
    this.coordinator.beginGeneration(this.generation); this.mainAllowed = false;
    // Refresh phase gates at turn yield; main output never awaits this read.
    void this.refreshSnapshot();
    this.callbacks.status?.('Preparing reply'); this.emit('caller_stopped');
  }

  receive(data, { replayed = false } = {}) {
    if (this.closed || !this.active) return;
    if (typeof data !== 'string') {
      const pcm = bytes(data); if (!replayed) this.audioBytes += pcm.byteLength;
      if (!pcm.byteLength || pcm.byteLength % 2 || pcm.byteLength > 128000) { void this.stop('output_geometry'); return; }
      if (this.provisional) {
        //350ms of16kHz mono PCM, independent of provider packet chunk size.
        if (this.provisional.bytes + pcm.byteLength <= 11200) {
          this.provisional.buffer.push(pcm.slice()); this.provisional.bytes += pcm.byteLength;
        } else this.discardedOutputBytes += pcm.byteLength;
        return;
      }
      // Vapi also streams silence. It must not preempt a listening cue.
      if (peak(pcm) <= 64 && !this.mainSpeaking) return;
      if (this.callerSpeaking || !this.generation) { this.discardedOutputBytes += pcm.byteLength; return; }
      if (!this.mainAllowed) {
        // No provider packet generation IDs exist. Do not replay buffered old
        // audio after an interruption merely because a new speech event arrives.
        this.discardedOutputBytes += pcm.byteLength;
        return;
      }
      if (!this.mainSpeaking && peak(pcm) > 64) {
        this.cancelWaitingPreparation('main_started');
        if (this.haveUserUtterance) this.userTurnHasMainAudio = true;
        this.mainSpeaking = true; this.callbacks.status?.('Speaking'); this.emit('main_started');
      }
      if (this.gapAt !== null && peak(pcm) > 64) { this.answerGaps.push(this.runtime.now() - this.gapAt); this.gapAt = null; }
      if (!this.coordinator.receiveMainPcm(this.generation, pcm, 16000)) this.discardedOutputBytes += pcm.byteLength;
      return;
    }
    if (data.length > 262144) return;
    let message;
    try { message = JSON.parse(data); } catch { return; }
    if (!message || typeof message !== 'object' || Array.isArray(message)) return;
    if (message.type === 'speech-update' && message.role === 'user'
      && !['started', 'stopped'].includes(message.status)) return;
    // Only an explicit numeric provider identity can reject an older user
    // event. An absent/string ID remains unknown; timestamps do not prove a join.
    if (message.role === 'user' || message.type === 'user-interrupted') {
      const turn = Number.isSafeInteger(message.turn) ? message.turn
        : Number.isSafeInteger(message.turnId) ? message.turnId : null;
      if (turn !== null && turn >= 0) {
        if (this.lastProviderUserTurn !== null && turn < this.lastProviderUserTurn) {
          this.emit('stale_provider_event_discarded'); return;
        }
        this.lastProviderUserTurn = turn;
      }
    }
    if (message.type === 'transcript' && typeof message.transcript === 'string') {
      this.callbacks.transcript?.({ role: message.role, text: message.transcript, final: message.transcriptType === 'final' });
      if (message.role === 'user' && !this.muted) {
        const rawText = message.transcript.trim().slice(-1600);
        if (!rawText) return;
        const final = message.transcriptType === 'final';
        const sameText = this.partial && speechText(rawText) === speechText(this.partial);
        // Delayed ASR finals/refinements in the endpointing tail belong to the
        // just-yielded utterance until new speech is actually observed. They
        // cannot manufacture a 3ms caller turn and cut a complete cue.
        const trailing = !this.callerSpeaking && !this.providerSpeaking && !this.localSpeaking && this.partial
          && final && !this.userTurnClosed && this.runtime.now() - this.lastUserStoppedAt <= 750;
        if (!this.callerSpeaking && !trailing) this.userStarted('user_transcript');
        const assembled = this.assembleTranscript(rawText, final), text = assembled.text;
        // Raw and accumulated safety checks run even for repeated ASR values.
        if (this.snapshotHardBlocked || sensitive(rawText) || sensitive(text)) this.cancelWaitingPreparation('sensitive_state_changed');
        this.coordinator.setSensitive(this.snapshotHardBlocked || sensitive(rawText) || sensitive(text));
        if (assembled.duplicate || (sameText && (!final || this.lastUserTranscriptFinal))) {
          this.emit('transcript_refinement_ignored', { transcript_type: final ? 'final' : 'partial', reason: 'same_words' });
          if (final) { this.lastUserTranscriptFinal = true; this.queueWaitingPreparation(); this.maybeUserStopped(true); }
          return;
        }
        if (text !== this.partial) {
          this.cancelWaitingPreparation('partial_revised'); this.preparationTextChangedAt = this.runtime.now();
        }
        this.partial = text; this.revision += 1;
        this.lastUserTranscriptFinal = final;
        this.coordinator.setSensitive(this.snapshotHardBlocked || sensitive(text));
        if (final && !this.providerSpeaking && !this.localSpeaking) this.coordinator.updateFinal(text, { revision: this.revision });
        else if (this.callerSpeaking) this.coordinator.updatePartial(text, { revision: this.revision });
        this.queueWaitingPreparation();
        if (final) { this.lastFinal = text; this.maybeUserStopped(true); }
      }
      if (message.role === 'assistant') this.cancelWaitingPreparation('assistant_context_changed');
    }
    if (message.type === 'speech-update' && message.role === 'user') {
      this.providerSpeaking = message.status === 'started';
      if (this.providerSpeaking && !this.muted) this.userStarted('provider_speech_event');
      else { this.maybeUserStopped(); this.queueWaitingPreparation(); }
    }
    if (message.type === 'speech-update' && message.role === 'assistant') {
      if (message.status === 'started' && this.callerSpeaking && !this.localSpeaking && !this.providerSpeaking) this.endUserTurn();
      if (message.status === 'started' && !this.callerSpeaking) {
        if (!this.generation) { this.generation = `playback-${++this.generationSequence}`; this.coordinator.beginGeneration(this.generation); }
        // Provider speech-start can precede usable TTS audio by seconds. Only
        // actual voiced PCM revokes waiting work; its graph handoff remains
        // synchronous and bounded by the existing 8ms cue fade.
        this.mainAllowed = true; this.emit('provider_main_announced');
        if (this.haveUserUtterance) this.generationAnnouncedAfterUser = true;
        if (this.provisional) {
          this.provisional.mainAllowed = true; this.provisional.generation = this.generation;
        }
      } else if (message.status === 'stopped') {
        this.cancelWaitingPreparation('generation_finished');
        if (this.generationAnnouncedAfterUser) this.userTurnClosed = true;
        this.mainSpeaking = false;
        if (this.generation) this.coordinator.finishGeneration(this.generation);
        this.callbacks.status?.(this.callerSpeaking ? 'Listening' : 'Ready'); this.emit('main_stopped');
      }
    }
    if (message.type === 'user-interrupted') {
      this.userStarted('provider_interruption_event');
      this.mainAllowed = false; this.mainSpeaking = false; this.generation = null;
      this.coordinator.stopCue('provider_interrupted'); this.playback.stopMain();
    }
    if (message.type === 'status-update' && message.status === 'ended') void this.stop('provider_ended');
  }

  async refreshSnapshot() {
    if (this.closed) return;
    const sequence = ++this.snapshotReadSequence;
    try {
      const snapshot = await this.request('');
      if (this.closed || sequence !== this.snapshotReadSequence) return;
      const detailsProposal = Boolean(snapshot.proposal) && snapshot.stage === 'details';
      const previouslyBlocked = this.snapshotHardBlocked;
      this.snapshotHardBlocked = Boolean(snapshot.ended || snapshot.stage === 'confirmation'
        || snapshot.pending_operations?.length || (snapshot.proposal && !detailsProposal));
      if (this.snapshotHardBlocked) this.cancelWaitingPreparation('sensitive_state_changed');
      this.coordinator?.setListeningBlocked(detailsProposal);
      this.coordinator?.setSensitive(this.snapshotHardBlocked || sensitive(this.partial));
      if (previouslyBlocked && !this.snapshotHardBlocked) this.coordinator?.recoverWaitingContext();
      if (!this.callerSpeaking) this.coordinator?.wakeWaiting();
      else this.queueWaitingPreparation();
      this.callbacks.snapshot?.(snapshot);
    } catch { /* Read-only UI evidence cannot change a booking or interrupt audio. */ }
  }

  async pollSnapshot() {
    if (this.closed) return;
    await this.refreshSnapshot();
    if (!this.closed) this.pollTimer = this.runtime.setTimeout(() => this.pollSnapshot(), 1200);
  }

  setMuted(muted) {
    this.muted = Boolean(muted); this.microphone?.setMuted(this.muted);
    if (this.muted) {
      this.cancelWaitingPreparation('muted');
      if (this.provisional) this.recoverProvisional('microphone_muted');
      this.coordinator?.invalidate('muted'); this.providerSpeaking = false; this.localSpeaking = false;
      this.callerSpeaking = false;
      if (this.coordinator) this.coordinator.userSpeaking = false;
      this.emit('muted');
    }
  }

  metrics() {
    return { input_frames: this.inputFrames, input_bytes: this.inputBytes,
      input_frames_during_cue: this.inputFramesDuringCue, input_frames_during_main: this.inputFramesDuringMain,
      received_output_bytes: this.audioBytes, discarded_output_bytes: this.discardedOutputBytes,
      provisional_interruption: Boolean(this.provisional),
      local_energy_onsets_ignored_without_output: this.ignoredEnergyOnsets.no_output,
      local_energy_onsets_ignored_during_cached_cue: this.ignoredEnergyOnsets.cue_only,
      local_energy_onsets_ignored_during_cooldown: this.ignoredEnergyOnsets.cooldown,
      cues_played: this.coordinator?.cuesPlayed || 0, classifier_requests: this.coordinator?.requests || 0,
      waiting_preparation_requests: this.waitingPreparationRequests,
      waiting_preparation_reuses: this.waitingPreparationReuses,
      // This clock begins when both local/provider turn gates release; it does
      // not identify the source's last sample or the caller's last voiced word.
      first_voiced_main_after_turn_release_ms: [...this.answerGaps],
      scope: 'browser_transport_and_scheduled_audio_not_human_listening_or_provider_generation_proof' };
  }

  releaseTracks() { for (const track of this.stream?.getTracks?.() || []) track.stop(); }

  async endLocalSession() {
    if (!this.session || this.endedSession) return;
    this.endedSession = true;
    try { await this.request('/end', { method: 'POST', body: '{}' }); } catch { /* Session TTL remains. */ }
  }

  async endProviderCall() {
    if (!this.callId) return;
    if (this.providerStop) return this.providerStop;
    this.providerStop = (async () => {
      try {
        const stopped = await this.request('/duplex/stop', { method: 'POST', body: '{}' });
        if (stopped.ok) return;
      } catch { /* Fall back to a short owned WebSocket termination handshake. */ }
      if (!this.callUrl) return;
      let url;
      try { url = new URL(this.callUrl); } catch { return; }
      if (url.protocol !== 'wss:' || !url.hostname.endsWith('.vapi.ai') || url.username || url.password) return;
      await new Promise(resolve => {
        let socket, done = false;
        const finish = () => {
          if (done) return; done = true; this.runtime.clearTimeout(timer);
          try { socket?.close(); } catch { /* Already closed. */ } resolve();
        };
        const timer = this.runtime.setTimeout(finish, 3000);
        try {
          socket = new this.runtime.WebSocket(this.callUrl);
          socket.addEventListener('open', () => {
            try { socket.send(JSON.stringify({ type: 'end-call' })); } catch { /* Teardown raced with provider close. */ }
            finally { finish(); }
          }, { once: true });
          socket.addEventListener('error', finish, { once: true }); socket.addEventListener('close', finish, { once: true });
        } catch { finish(); }
      });
    })();
    return this.providerStop;
  }

  async stop(reason = 'user') {
    if (this.stopping) return this.stopping;
    if (this.closed) return;
    this.closed = true; this.active = false; this.runtime.clearTimeout(this.watchdog);
    this.cancelWaitingPreparation('closed');
    this.runtime.clearTimeout(this.pollTimer); this.runtime.clearTimeout(this.userStopTimer);
    if (this.provisional) {
      this.runtime.clearTimeout(this.provisional.timer); this.discardedOutputBytes += this.provisional.bytes;
      this.provisional = null;
    }
    this.handshakeCancel?.();
    this.emit('ended', { reason }); this.coordinator?.close();
    try { if (this.socket?.readyState === 1) this.socket.send(JSON.stringify({ type: 'end-call' })); } catch { /* Remote close raced. */ }
    try { this.socket?.close(); } catch { /* A failed handshake has no live socket. */ }
    this.stopping = (async () => {
      try { await this.microphone?.stop(); } catch { /* Continue microphone hardware cleanup. */ }
      this.releaseTracks();
      try { await this.context?.close(); } catch { /* Context already closed. */ }
      await this.endProviderCall(); await this.endLocalSession();
      this.callbacks.status?.('Ended');
    })();
    return this.stopping;
  }
}
