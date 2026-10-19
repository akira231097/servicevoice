// Vapi browser transport. Scheduling and authorization stay on the server.
// Timing here is an event-arrival proxy, never an acoustic latency measurement.
let sdkModule;
const ignoreLateError = () => {};

export function preloadVoiceSdk() {
  sdkModule ||= import('/static/vendor/vapi.js').catch(error => {
    sdkModule = null;
    throw error;
  });
  return sdkModule;
}

function percentile(sorted, fraction) {
  return sorted.length ? sorted[Math.max(0, Math.ceil(sorted.length * fraction) - 1)] : null;
}

function stopTracks(stream) {
  for (const track of stream?.getTracks?.() || []) {
    try { track.stop(); } catch { /* Attempt every remaining track. */ }
  }
}

export class VoiceSession {
  constructor(config, session, callbacks, runtime = {}) {
    this.config = config;
    this.session = session;
    this.callbacks = callbacks;
    // Injection keeps timing/race tests deterministic without microphone/API use.
    this.runtime = {
      now: () => performance.now(),
      setTimeout: (...args) => setTimeout(...args), clearTimeout: timer => clearTimeout(timer),
      setInterval: (...args) => setInterval(...args), clearInterval: timer => clearInterval(timer),
      loadSdk: preloadVoiceSdk,
      getUserMedia: constraints => navigator.mediaDevices.getUserMedia(constraints),
      mediaAvailable: () => Boolean(window.isSecureContext && navigator.mediaDevices?.getUserMedia),
      ...runtime,
    };
    this.sdk = null;
    this.closed = false;
    this.active = false;
    this.providerEndedReason = null;
    this.muted = false; // Explicit user intent; SDK changes are asynchronous.
    this.muteCommand = null;
    this.muteTimer = null;
    this.assistantSpeaking = false;
    this.userSpeaking = false;
    this.startRequestedAt = null;
    this.startTime = null;
    this.elapsed = 0;
    this.timer = null;
    this.connectTimer = null;
    this.listeners = [];
    this.audioPlayers = new Set();
    this.seenInterruptions = new Set();
    this.seenTranscriptEvents = new Set();
    this.pendingGap = null;
    this.lastStopKey = null;
    this.userPhase = 'unknown';
    this.gaps = [];
    this.stoppedTurns = 0;
    this.abandonedTurns = 0;
    this.interruptions = 0;
    this.speechEventsObserved = false;
    this.interruptedConnections = new Set();
    this.networkInterruptions = 0;
    this.preflightStream = null;
    this.stopping = null;
    this.startPending = false;
  }

  on(event, handler) {
    const guarded = (...args) => { if (!this.closed) handler(...args); };
    this.listeners.push([event, guarded]);
    this.sdk.on(event, guarded);
  }

  async start() {
    if (this.closed || this.startPending || this.sdk) return;
    this.startPending = true;
    this.startRequestedAt = this.runtime.now();
    this.callbacks.status('connecting');
    try {
      if (!this.runtime.mediaAvailable()) throw new Error('secure-context-required');
      // No provider call is opened before explicit microphone permission.
      this.preflightStream = await this.runtime.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }, video: false,
      });
      if (this.closed) { this.releaseAudio(); return; }
      const microphone = this.preflightStream.getAudioTracks()[0];
      if (!microphone || microphone.readyState === 'ended') throw new Error('microphone-unavailable');
      let Vapi;
      try { ({ default: Vapi } = await this.runtime.loadSdk()); }
      catch { throw new Error('sdk-unavailable'); }
      if (this.closed) { this.releaseAudio(); return; }
      if (typeof Vapi !== 'function') throw new Error('sdk-unavailable');
      // Retain our source so End releases the hardware even during SDK startup.
      this.sdk = new Vapi(this.config.vapi_public_key, undefined, undefined, { audioSource: microphone });
      this.bindEvents();
      this.connectTimer = this.runtime.setTimeout(() => { void this.fail(new Error('connection-timeout')); }, 45000);
      const call = await this.sdk.start(this.config.vapi_assistant_id, {
        variableValues: {
          session_id: this.session.id, session_token: this.session.token,
          current_date: this.config.today, timezone: this.config.timezone,
        },
      });
      if (!this.closed && !call) throw new Error('call-not-created');
    } catch (error) {
      if (!this.closed) await this.fail(error);
    } finally {
      this.startPending = false;
      if (this.closed) {
        // start() may allocate its call after an earlier stop() finished.
        try { await this.sdk?.stop(); } catch { /* The primary failure was shown. */ }
        this.releaseAudio();
        this.detachListeners();
      }
    }
  }

  bindEvents() {
    // SDK async initialization can emit EventEmitter errors after teardown.
    // This module-level sink prevents an unhandled error without retaining UI.
    this.sdk.on('error', ignoreLateError);
    this.on('call-start', () => {
      if (this.startTime !== null) return;
      this.runtime.clearTimeout(this.connectTimer);
      this.active = true;
      this.startTime = this.runtime.now();
      this.callbacks.status('listening');
      this.callbacks.metrics(this.metrics());
      this.timer = this.runtime.setInterval(() => {
        this.elapsed = this.runtime.now() - this.startTime;
        this.callbacks.metrics(this.metrics());
        if (this.elapsed >= 300000) {
          this.callbacks.notice('The five-minute demo call has ended. Verified results will remain below.');
          void this.stop('limit');
        }
      }, 1000);
    });
    this.on('call-end', () => { void this.stop('remote'); });
    this.on('speech-start', () => {
      if (!this.active) return;
      this.assistantSpeaking = true;
      if (this.pendingGap) {
        const gap = this.runtime.now() - this.pendingGap.at;
        // Keep slow samples. Dropping >30s turns would hide operational stalls.
        if (gap >= 0) this.gaps.push(gap);
        else this.abandonedTurns += 1;
        this.pendingGap = null;
        this.callbacks.metrics(this.metrics());
      }
      this.publishSpeakingState();
    });
    this.on('speech-end', () => {
      if (!this.active) return;
      this.assistantSpeaking = false;
      this.publishSpeakingState();
    });
    this.on('volume-level', volume => this.callbacks.volume(Math.max(0, Math.min(1, Number(volume) || 0))));
    const captureAudio = player => {
      if (!player) return;
      // Track replacement can remove an old SDK element without pausing its
      // live MediaStream. Keep one playback sink. Do not stop old tracks here:
      // the new element may share them; full call cleanup owns track stopping.
      for (const previous of this.audioPlayers) {
        if (previous === player) continue;
        try { previous.pause(); } catch { /* Still detach its live source. */ }
        try { previous.srcObject = null; } catch { /* Still remove the element. */ }
        try { previous.remove(); } catch { /* Already detached. */ }
        this.audioPlayers.delete(previous);
      }
      this.audioPlayers.add(player);
      if (this.closed) this.releaseAudio();
    };
    this.listeners.push(['audio', captureAudio]);
    this.sdk.on('audio', captureAudio);
    this.on('message', message => this.handleMessage(message));
    this.on('daily-participant-updated', participant => {
      if (!this.active || participant?.local !== true) return;
      const state = participant.tracks?.audio?.state;
      if (['off', 'blocked'].includes(state)) this.reconcileMute(true);
      else if (['playable', 'sendable'].includes(state)) this.reconcileMute(false);
    });
    this.on('network-connection', event => {
      if (!this.active || !['signaling', 'sfu', 'peer-to-peer'].includes(event?.type)) return;
      if (event.event === 'interrupted') {
        if (!this.interruptedConnections.has(event.type)) this.networkInterruptions += 1;
        this.interruptedConnections.add(event.type);
      } else if (event.event === 'connected') this.interruptedConnections.delete(event.type);
      // Daily already owns reconnection. Surface the interruption without
      // creating another call or losing this session's in-flight operations.
      this.publishSpeakingState();
      this.callbacks.metrics(this.metrics());
    });
    this.on('camera-error', error => { void this.fail(error); });
    this.on('call-start-failed', error => { void this.fail(error); });
    this.on('error', error => {
      const nonfatal = ['audio-observer-setup-error', 'audio-processing-setup-error', 'audio-processor-recovery-error'];
      if (nonfatal.includes(error?.type)) {
        this.callbacks.notice('An optional audio enhancement is unavailable. Voice can continue; timing samples may be incomplete.');
      } else { void this.fail(error); }
    });
  }

  publishSpeakingState() {
    if (this.closed || !this.active) return;
    if (this.interruptedConnections.size) this.callbacks.status('reconnecting');
    else if (this.muted) this.callbacks.status(this.assistantSpeaking ? 'muted-speaking' : 'muted');
    else if (this.userSpeaking) this.callbacks.status('listening');
    else if (this.assistantSpeaking) this.callbacks.status('speaking');
    else this.callbacks.status(this.pendingGap ? 'thinking' : 'listening');
  }

  abandonGap() {
    if (this.pendingGap) this.abandonedTurns += 1;
    this.pendingGap = null;
  }

  handleMessage(message) {
    if (this.closed || !message || typeof message !== 'object') return;
    if ((message.type === 'transcript' || message.type === 'transcript[transcriptType="final"]') && ['user', 'assistant'].includes(message.role)) {
      if (typeof message.transcript !== 'string' || !message.transcript.trim()) return;
      const final = message.transcriptType === 'final' || message.type !== 'transcript';
      // Dedupe explicit provider identities, not legitimate repeated words.
      const identity = message.id ?? message.eventId ?? message.timestamp;
      const key = identity === undefined ? null : JSON.stringify([identity, message.role, final, message.transcript]);
      if (key && this.seenTranscriptEvents.has(key)) return;
      if (key) this.seenTranscriptEvents.add(key);
      this.callbacks.transcript({ role: message.role, text: message.transcript, final });
      if (final) this.callbacks.refresh();
    }
    if (message.type === 'speech-update' && message.role === 'user' && this.active && !this.muted) {
      this.speechEventsObserved = true;
      if (message.status === 'started') {
        this.abandonGap();
        this.userPhase = 'speaking';
        this.userSpeaking = true;
        this.lastStopKey = null;
      } else if (message.status === 'stopped') {
        const key = message.turn ?? message.turnId ?? message.id ?? null;
        if (this.userPhase === 'stopped' && (key === null || key === this.lastStopKey)) return;
        this.abandonGap();
        this.userPhase = 'stopped';
        this.userSpeaking = false;
        this.lastStopKey = key;
        this.stoppedTurns += 1;
        this.pendingGap = { at: this.runtime.now() };
      }
      this.publishSpeakingState();
      this.callbacks.metrics(this.metrics());
    }
    if (message.type === 'user-interrupted') {
      const key = message.turnId ?? message.id ?? message.timestamp;
      if (key === undefined || !this.seenInterruptions.has(key)) {
        if (key !== undefined) this.seenInterruptions.add(key);
        this.interruptions += 1;
        this.callbacks.metrics(this.metrics());
      }
      this.callbacks.caption('');
    }
    if (['tool-calls', 'tool-calls-result', 'conversation-update'].includes(message.type)) this.callbacks.refresh();
    if (message.type === 'status-update' && message.status === 'ended') {
      // Only classify an explicit known provider value. Never display arbitrary
      // SDK details or infer why a reason-less disconnect happened.
      if (message.endedReason === 'silence-timed-out') this.providerEndedReason = 'silence-timed-out';
      void this.stop('remote');
    }
  }

  setMuted(muted) {
    if (!this.active || this.closed) return;
    this.muted = Boolean(muted);
    if (this.muted) { this.abandonGap(); this.userSpeaking = false; }
    try { if (this.muted) this.setOwnedInputEnabled(false); }
    catch { void this.fail(new Error('microphone-mute-failed')); return this.muted; }
    this.publishSpeakingState();
    if (this.muteCommand === null) this.sendMuteCommand();
    return this.muted;
  }

  setOwnedInputEnabled(enabled) {
    // This gates the source we supplied, not any independent vendor clone.
    // Unexpected SDK unmute therefore still ends the call below.
    for (const track of this.preflightStream?.getAudioTracks?.() || []) track.enabled = enabled;
  }

  sendMuteCommand() {
    if (this.closed || !this.active || this.muteCommand !== null) return;
    const requested = this.muted;
    this.muteCommand = requested;
    this.muteTimer = this.runtime.setTimeout(() => {
      if (this.closed || this.muteCommand !== requested) return;
      try {
        // A delayed snapshot can acknowledge a lost participant event. Never
        // read isMuted() immediately after the asynchronous setMuted() call.
        if (this.sdk.isMuted() === requested) this.reconcileMute(requested);
        else void this.fail(new Error('microphone-mute-failed'));
      } catch { void this.fail(new Error('microphone-mute-failed')); }
    }, 2000);
    try {
      // Only a queued explicit unmute request may enable our source. Do this
      // when dispatching, so a disabled source cannot block Daily's acknowledgement.
      this.setOwnedInputEnabled(!requested);
      this.sdk.setMuted(requested);
    }
    catch { void this.fail(new Error('microphone-mute-failed')); }
  }

  reconcileMute(actualMuted) {
    if (this.closed || !this.active) return;
    if (this.muteCommand !== null) {
      // A prior state may arrive while a command is pending. It must never
      // change user intent or enable the owned source.
      if (actualMuted !== this.muteCommand) return;
      this.runtime.clearTimeout(this.muteTimer);
      this.muteTimer = null;
      this.muteCommand = null;
      if (actualMuted !== this.muted) this.sendMuteCommand();
    } else if (this.muted && !actualMuted) {
      // SDK noise-processor recovery may otherwise override a confirmed mute.
      void this.fail(new Error('microphone-mute-failed'));
    }
  }

  metrics() {
    const sorted = [...this.gaps].sort((a, b) => a - b);
    return {
      kind: 'vapi_client_event_gap_proxy',
      definition: 'Browser receipt of Vapi user speech stopped to SDK remote audio-level speech-start; not acoustic end-of-speech to audible response. Endpointing and playback delays are not measured.',
      elapsed: this.active && this.startTime !== null ? this.runtime.now() - this.startTime : this.elapsed,
      connectionMs: this.startTime !== null && this.startRequestedAt !== null ? this.startTime - this.startRequestedAt : null,
      connectionDefinition: 'Browser voice.start invocation to SDK call-start; includes microphone permission time, excludes local application session creation.',
      gap: percentile(sorted, 0.5), gapP95: percentile(sorted, 0.95), gapMax: sorted.at(-1) ?? null,
      gapSamples: sorted.length, gapSamplesMs: [...this.gaps],
      stoppedTurns: this.stoppedTurns, pendingTurns: this.pendingGap ? 1 : 0, abandonedTurns: this.abandonedTurns,
      interruptions: this.interruptions,
      networkInterruptions: this.networkInterruptions,
      hasConnected: this.startTime !== null, speechEventsObserved: this.speechEventsObserved,
    };
  }

  async fail(error) {
    if (this.closed) return;
    const raw = error?.error?.message || error?.message || error?.error || '';
    const text = typeof raw === 'string' ? raw.toLowerCase() : '';
    const name = error?.error?.name || error?.name;
    // Classify errors; never echo arbitrary SDK text, request bodies, or tokens.
    let description = 'The voice connection failed. Check the Vapi configuration and network, then try a new session.';
    if (name === 'NotAllowedError' || /permission|notallowed/.test(text)) description = 'Microphone access was denied. Allow microphone access for this page in your browser, then start a new voice conversation.';
    else if (name === 'NotFoundError' || text === 'microphone-unavailable') description = 'No usable microphone was found. Connect a microphone and try again.';
    else if (name === 'NotReadableError') description = 'The microphone is busy or unavailable. Close other apps using it and try again.';
    else if (text === 'secure-context-required') description = 'Microphone access requires localhost or HTTPS in a supported browser.';
    else if (text === 'sdk-unavailable') description = 'The local Vapi browser bundle could not load. Run the project’s frontend setup, then reload.';
    else if (text === 'connection-timeout') description = 'The voice connection timed out. Check the Vapi configuration and network, then try again.';
    else if (text === 'call-not-created') description = 'Vapi did not create a voice call. Try a new session.';
    else if (text === 'microphone-mute-failed') description = 'The microphone state could not be kept consistent with your choice. The call was ended and the microphone released.';
    this.callbacks.notice(description);
    await this.stop('error');
  }

  releaseAudio() {
    for (const player of this.audioPlayers) {
      try { player.pause(); } catch { /* Still stop its tracks. */ }
      stopTracks(player.srcObject);
      try { player.srcObject = null; } catch { /* Already detached. */ }
      try { player.remove(); } catch { /* Already detached. */ }
    }
    this.audioPlayers.clear();
    stopTracks(this.preflightStream);
    this.preflightStream = null;
    this.callbacks.volume(0);
  }

  detachListeners() {
    for (const [event, listener] of this.listeners) {
      if (event !== 'audio' || !this.startPending) this.sdk?.off(event, listener);
    }
    this.listeners = this.startPending ? this.listeners.filter(([event]) => event === 'audio') : [];
  }

  async stop(reason = 'user') {
    if (this.stopping) return this.stopping;
    this.closed = true;
    this.active = false;
    this.runtime.clearInterval(this.timer);
    this.runtime.clearTimeout(this.connectTimer);
    this.runtime.clearTimeout(this.muteTimer);
    this.muteTimer = null;
    this.muteCommand = null;
    if (this.startTime !== null) this.elapsed = this.runtime.now() - this.startTime;
    this.abandonGap();
    this.callbacks.metrics(this.metrics());
    this.releaseAudio();
    this.callbacks.status('ending');
    this.stopping = (async () => {
      try { await this.sdk?.stop(); }
      catch { this.callbacks.notice('Your microphone is stopped. The voice connection reported an error during cleanup.'); }
      finally {
        this.releaseAudio();
        this.detachListeners();
        this.callbacks.status(reason === 'error' ? 'error'
          : this.providerEndedReason === 'silence-timed-out' ? 'silence-timeout' : 'ended');
        await this.callbacks.ended(reason);
      }
    })();
    return this.stopping;
  }
}
