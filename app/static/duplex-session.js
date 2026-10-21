// Isolated browser transport: continuous caller PCM, one owned output clock.
import { DuplexCoordinator, PcmPlayback } from './duplex-coordinator.js';
import { DuplexMicrophone } from './duplex-microphone.js';

const sensitive = text => /\b(?:stop|wait|actually|instead|correct|change|cancel|confirm|yes|no|not|never|unless|only\s+if|do\s+not|don['’]t|can['’]t|cannot|won['’]t|goodbye|bye|emergency|urgent|ignore|instructions?|password|card|social security)\b/i.test(text.replace(/\boil\s+change\b/gi, 'oil service'))
  || /\d{4,}|[^\x00-\x7f]/.test(text)
  || /\b(?:speak|reply|respond|answer|talk)\b.{0,35}\b(?:in|using)\s+[a-z]+/i.test(text);
const bytes = value => new Uint8Array(value);
const peak = pcm => {
  const view = new DataView(pcm.buffer, pcm.byteOffset, pcm.byteLength);
  let result = 0;
  for (let i = 0; i + 1 < pcm.byteLength; i += 2) result = Math.max(result, Math.abs(view.getInt16(i, true)));
  return result;
};

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
    this.provisional = null;
  }

  emit(kind, detail = {}) {
    const event = { kind, at_ms: Math.max(0, this.runtime.now() - this.startAt), ...detail };
    if (this.events.length < 1500) this.events.push(event);
    this.callbacks.event?.(event);
    if (kind === 'cue_started') this.cuePlaying = true;
    if (['cue_ended', 'cue_stopped', 'ended'].includes(kind)) this.cuePlaying = false;
    if (this.session && ['cue_started', 'cue_stopped', 'cue_ended', 'barge_in', 'main_started', 'ended', 'muted'].includes(kind)) {
      const body = { event: kind, at_ms: Math.min(event.at_ms, 360000), epoch: this.coordinator?.epoch ?? 0 };
      if (['neutral_ack', 'moment', 'silence'].includes(event.phrase_id)) body.phrase_id = event.phrase_id;
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
        selectCue: decision => this.request('/duplex/cue', { method: 'POST', signal: decision.signal,
          body: JSON.stringify({ text: decision.text, phase: decision.phase, epoch: decision.utteranceId,
            revision: ++this.cueRevision }) }),
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
    }
  }

  provisionalBargeIn() {
    if (this.provisional || this.closed || this.callerSpeaking) return;
    const state = { generation: this.generation, mainAllowed: this.mainAllowed,
      wasMainSpeaking: this.mainSpeaking, buffer: [], bytes: 0, timer: null };
    this.provisional = state;
    this.coordinator.invalidate('provisional_barge_in');
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
    const hadProvisionalOutput = Boolean(this.provisional?.wasMainSpeaking);
    this.confirmProvisional(source);
    this.callerSpeaking = true; this.partial = ''; this.revision = 0;
    const wasOutput = this.mainSpeaking || this.cuePlaying || hadProvisionalOutput;
    this.mainAllowed = false; this.mainSpeaking = false; this.pendingAudio = []; this.generation = null;
    this.coordinator.userSpeechStarted();
    this.coordinator.setSensitive(false);
    this.callbacks.status?.('Listening');
    this.emit(wasOutput ? 'barge_in' : 'caller_started', { source });
  }

  maybeUserStopped(force = false) {
    this.runtime.clearTimeout(this.userStopTimer);
    if (!this.callerSpeaking || this.providerSpeaking || this.localSpeaking || this.closed) return;
    this.userStopTimer = this.runtime.setTimeout(() => {
      if (this.localSpeaking || this.providerSpeaking || this.closed) return;
      this.endUserTurn();
    }, force ? 0 : 160);
  }

  endUserTurn() {
    if (!this.callerSpeaking || this.closed) return;
    this.runtime.clearTimeout(this.userStopTimer); this.userStopTimer = null;
    this.callerSpeaking = false; this.gapAt = this.runtime.now();
    this.coordinator.userSpeechStopped();
    this.generation = `playback-${++this.generationSequence}`;
    this.coordinator.beginGeneration(this.generation); this.mainAllowed = false;
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
        const text = message.transcript.trim().slice(-1600);
        const trailingFinal = message.transcriptType === 'final' && !this.callerSpeaking && this.partial
          && (text.startsWith(this.partial) || this.partial.startsWith(text));
        if (!this.callerSpeaking && !trailingFinal) this.userStarted('user_transcript');
        this.partial = text; this.revision += 1;
        this.coordinator.setSensitive(sensitive(text));
        if (message.transcriptType === 'final') this.coordinator.updateFinal(text, { revision: this.revision });
        else if (this.callerSpeaking) this.coordinator.updatePartial(text, { revision: this.revision });
        if (message.transcriptType === 'final') { this.lastFinal = text; this.maybeUserStopped(true); }
      }
    }
    if (message.type === 'speech-update' && message.role === 'user') {
      this.providerSpeaking = message.status === 'started';
      if (this.providerSpeaking && !this.muted) this.userStarted('provider_speech_event');
      else this.maybeUserStopped();
    }
    if (message.type === 'speech-update' && message.role === 'assistant') {
      if (message.status === 'started' && this.callerSpeaking && !this.localSpeaking && !this.providerSpeaking) this.endUserTurn();
      if (message.status === 'started' && !this.callerSpeaking) {
        if (!this.generation) { this.generation = `playback-${++this.generationSequence}`; this.coordinator.beginGeneration(this.generation); }
        this.coordinator.invalidate('main_announced', { stopCue: false });
        this.mainAllowed = true; this.emit('provider_main_announced');
        if (this.provisional) {
          this.provisional.mainAllowed = true; this.provisional.generation = this.generation;
        }
      } else if (message.status === 'stopped') {
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

  async pollSnapshot() {
    if (this.closed) return;
    try {
      const snapshot = await this.request('');
      if (this.closed) return;
      const blocked = snapshot.ended || snapshot.stage === 'confirmation' || snapshot.proposal || snapshot.pending_operations?.length;
      this.coordinator?.setSensitive(Boolean(blocked) || sensitive(this.partial));
      this.callbacks.snapshot?.(snapshot);
    } catch { /* Read-only UI evidence cannot change a booking or interrupt audio. */ }
    if (!this.closed) this.pollTimer = this.runtime.setTimeout(() => this.pollSnapshot(), 1200);
  }

  setMuted(muted) {
    this.muted = Boolean(muted); this.microphone?.setMuted(this.muted);
    if (this.muted) {
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
      cues_played: this.coordinator?.cuesPlayed || 0, classifier_requests: this.coordinator?.requests || 0,
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
