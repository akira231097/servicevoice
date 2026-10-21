/** Browser audio-graph verification. Defaults to an offline plan.
 * --run uses a local provider replay, synthetic microphone and synthetic cue PCM.
 * --run --live --base-url http://127.0.0.1:8785 --caller-pcm PRIVATE.pcm
 * explicitly permits one real isolated call; no phone number is used.
 * Optional --interrupt-pcm SECOND.pcm injects a second synthetic utterance
 * during the first main answer and verifies provider-confirmed barge-in.
 * --require-waiting additionally requires a waiting cue after caller input,
 * even when the same utterance already received a listening cue.
 * --waiting-only requires the waiting/main handoff without requiring a prior
 * listening cue, for short caller replies such as a name or a brief FAQ.
 * --conversation-plan JSON adds up to six fixed synthetic follow-up turns to
 * the same provider call, preserving the first turn and every later outcome.
 * Provider replay proves transport/playback behavior, never Jev model quality.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import http from 'node:http';
import os from 'node:os';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createHash } from 'node:crypto';
import assert from 'node:assert/strict';

const args = process.argv.slice(2);
const value = flag => { const at = args.indexOf(flag); return at < 0 ? null : args[at + 1]; };
const run = args.includes('--run'), live = args.includes('--live');
const waitingOnly = args.includes('--waiting-only');
const requireWaiting = args.includes('--require-waiting') || waitingOnly;
const plan = { kind: 'duplex_browser_audio_graph', mode: run ? live ? 'live_isolated_provider' : 'offline_provider_replay' : 'plan_only',
  calls_started: 0, external_requests: 0, microphone_hardware_used: false, phone_number_used: false,
  human_listening_verified: false, classification_quality_verified: false,
  requires: '--run (offline replay), or --run --live --base-url LOOPBACK --caller-pcm PCM16LE16K' };
if (!run) { console.log(JSON.stringify(plan, null, 2)); process.exit(0); }
const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const report = { ...plan, protocol_version: 'whole_clip_handoff_v4', max_main_handoff_ms: 1100, started_at: new Date().toISOString(), checks: {}, limitations: [
  'The energy gate is a cancellation heuristic, not speech recognition.',
  live ? 'Live verification uses synthetic caller audio; acoustic echo and human conversational quality remain unverified.'
    : 'Provider replay uses deterministic classifier decisions and synthetic cue PCM.',
  'Browser playback nodes and received PCM do not establish human listening quality.',
  'Provider audio has no documented per-frame generation IDs; local epochs are not upstream lineage.' ] };
report.source_sha256 = Object.fromEntries(await Promise.all(['app/static/duplex-coordinator.js', 'app/static/duplex-session.js',
  'app/static/duplex-microphone.js', 'scripts/duplex_browser_smoke.mjs'].map(async name =>
  [name, createHash('sha256').update(await fs.readFile(path.join(root, name))).digest('hex')])));
let browser, context, page, server, provider, base;
const sockets = [], uplink = [], requests = [], errors = [];
let sessions = 0;
const cueRequests = [];
const fixtureContexts = new Map();
const selections = session => cueRequests.filter(row => row.session === session && !row.prepare_only);
const preparations = session => cueRequests.filter(row => row.session === session && row.prepare_only);

function pcmTone(frequency, seconds, volume = 0.2) {
  const output = Buffer.alloc(Math.round(16000 * seconds) * 2);
  for (let i = 0; i < output.length / 2; i++) output.writeInt16LE(Math.round(volume * 32767 * Math.sin(2 * Math.PI * frequency * i / 16000)), i * 2);
  return output;
}
function inputRmsNear(pcm, atMs) {
  const first = Math.max(0, Math.floor(atMs * 16) - 400), last = Math.min(pcm.length / 2, first + 800);
  if (last <= first) return 0;
  let energy = 0;
  for (let index = first; index < last; index++) energy += (pcm.readInt16LE(index * 2) / 32768) ** 2;
  return Math.sqrt(energy / (last - first));
}
const cueAudio = { neutral_ack: pcmTone(220, .67), moment: pcmTone(250, 1.05),
  one_sec: pcmTone(290, .5), just_a_sec: pcmTone(310, .6), let_me_see: pcmTone(330, .8),
  checking: pcmTone(350, .9), taking_a_look: pcmTone(370, .95), working_on_it: pcmTone(390, .7) };
const phrases = Object.entries(cueAudio).map(([phrase_id, pcm]) => ({ phrase_id, sample_rate: 16000,
  pcm_bytes: pcm.length, sha256: createHash('sha256').update(pcm).digest('hex') }));
const sendJson = (socket, message) => socket.send(JSON.stringify(message));
const eventCount = async kind => page.evaluate(name => window.duplexSession.events.filter(row => row.kind === name).length, kind);
async function greetingState() {
  return page.evaluate(() => {
    const session = window.duplexSession, rows = session.events;
    const started = rows.find(row => row.kind === 'main_started');
    const stopped = rows.findLast(row => row.kind === 'main_stopped');
    const announced = rows.findLast(row => row.kind === 'provider_main_announced');
    const signal = window.duplexGraphEvidence.samples.findLast(row => !row.cue);
    const drained = session.playback.main.size === 0 && session.context.currentTime >= session.playback.mainEnd - .001;
    const lastActivity = Math.max(signal?.at_ms ?? 0, session.startAt + (stopped?.at_ms ?? 0));
    const voiced = Boolean(started && signal && signal.at_ms >= session.startAt + started.at_ms);
    const laterStop = Boolean(started && stopped && stopped.at_ms > started.at_ms
      && (!announced || announced.at_ms <= stopped.at_ms));
    const idleMs = performance.now() - Math.max(lastActivity, session.startAt + (announced?.at_ms ?? 0));
    const safeIdle = voiced && drained && idleMs >= 1000;
    return { ready: Boolean(voiced && drained && (laterStop && idleMs >= 200 || safeIdle)),
      actual_voiced_output_seen: voiced, later_stop_observed: laterStop,
      safe_idle_fallback: Boolean(safeIdle && !laterStop), playback_drained: drained };
  });
}
async function replyState(input, completed = true) {
  return page.evaluate(({ input, completed }) => {
    const session = window.duplexSession, rows = session.events;
    const main = rows.find(row => row.kind === 'main_started' && row.at_ms >= input.input_end_ms
      && rows.some(release => release.kind === 'caller_stopped' && release.at_ms >= input.started_at_ms && release.at_ms < row.at_ms));
    const release = main && rows.findLast(row => row.kind === 'caller_stopped'
      && row.at_ms >= input.started_at_ms && row.at_ms < main.at_ms);
    const stopped = main && rows.find(row => row.kind === 'main_stopped' && row.at_ms > main.at_ms);
    const drained = session.playback.main.size === 0 && session.context.currentTime >= session.playback.mainEnd - .001;
    return { ready: Boolean(main && release && (completed ? stopped && drained : session.mainSpeaking)),
      main_started_at_ms: main?.at_ms ?? null, turn_released_at_ms: release?.at_ms ?? null,
      main_stopped_at_ms: stopped?.at_ms ?? null, playback_drained: drained };
  }, { input, completed });
}
function cueFade(graph, source) {
  const gain = graph.gains?.find(row => row.gain_id === source?.gain_id);
  const ramp = gain?.automation.findLast(row => row.method === 'linearRampToValueAtTime' && row.args[0] === 0);
  if (!ramp) return null;
  const held = gain.automation.findLast(row => row.method === 'setValueAtTime' && row.args[1] <= ramp.args[1]);
  const start = held?.args[1] ?? ramp.observed_context_s;
  return { gain_id: gain.gain_id, start_context_s: start, end_context_s: ramp.args[1],
    duration_ms: (ramp.args[1] - start) * 1000 };
}
async function verifyRecordedFade(source, fade) {
  const recorded = await page.evaluate(() => window.duplexSession.context.recordBlocks.flatMap(block => [...block]));
  const length = Math.round(fade.duration_ms * 16), before = 64, after = 64;
  const first = Math.round((fade.start_context_s - source.scheduled_at_s) * 16000);
  const main = pcmTone(730, after / 16000), expected = [], abrupt = [];
  for (let index = -before; index < length + after; index++) {
    const cue = index < length ? cueAudio.moment.readInt16LE((first + index) * 2) / 32768 : main.readInt16LE((index - length) * 2) / 32768;
    expected.push(cue * (index < 0 || index >= length ? 1 : 1 - index / length));
    abrupt.push(cue);
  }
  let minimum = Infinity, offset = 0;
  for (let start = 0; start + expected.length <= recorded.length; start++) {
    let error = 0;
    for (let index = 0; index < expected.length; index++) error += (recorded[start + index] - expected[index]) ** 2;
    if (error < minimum) { minimum = error; offset = start; }
  }
  let abruptError = 0;
  for (let index = 0; index < abrupt.length; index++) abruptError += (recorded[offset + index] - abrupt[index]) ** 2;
  return { post_gain_recording_checked: true, transition_samples: expected.length,
    faded_transition_mse: minimum / expected.length, unfaded_transition_mse: abruptError / expected.length };
}
async function verifyRecordedCompleteTransition(source) {
  const recorded = await page.evaluate(() => window.duplexSession.context.recordBlocks.flatMap(block => [...block]));
  const tail = 128, head = 128, cue = cueAudio.moment, main = pcmTone(730, head / 16000);
  const expected = [...Array(tail)].map((_, index) => cue.readInt16LE(cue.length - (tail - index) * 2) / 32768)
    .concat([...Array(head)].map((_, index) => main.readInt16LE(index * 2) / 32768));
  let minimum = Infinity;
  for (let start = 0; start + expected.length <= recorded.length; start++) {
    let error = 0;
    for (let index = 0; index < expected.length; index++) error += (recorded[start + index] - expected[index]) ** 2;
    minimum = Math.min(minimum, error / expected.length);
  }
  return { post_gain_recording_checked: true, full_cue_natural_end_verified: Boolean(source.ended_ms && !source.stopped),
    transition_samples: expected.length, mse: minimum };
}
async function until(predicate, code, maximum = 6000) {
  const began = performance.now();
  while (performance.now() - began < maximum) { if (await predicate()) return; await delay(20); }
  throw new Error(code);
}
async function streamMain(socket, duration = 0.7) {
  const pcm = pcmTone(730, duration), start = performance.now();
  for (let offset = 0; offset < pcm.length; offset += 640) {
    socket.send(pcm.subarray(offset, offset + 640));
    await delay(Math.max(0, start + (offset + 640) / 32 - performance.now()));
  }
}
async function setMic(level) { await page.evaluate(volume => { window.duplexFixtureInput.gain.gain.value = volume; }, level); }
async function newFixtureSession({ snapshot = null, previousAssistant = '', shortReplies = [],
  decisionDelayMs = null, maxRequestAgeMs = null, firstWaitingReason = null,
  serverReadDelayMs = null, serverAfterReadDelayMs = null, enforceServerPacing = false,
  preparationDecision = 'prepare', preparationDelayMs = null, waitingPhrase = 'moment', followUpPhrase = 'one_sec', followUpDelayMs = null,
  predictionEnabled = true, predictionControlReason = null } = {}) {
  if (!predictionEnabled) {
    report.legacy_prediction_controls ||= [];
    report.legacy_prediction_controls.push({ session: sessions + 1, prediction_enabled: false,
      reason: predictionControlReason, deadlines_or_budgets_changed: false });
  }
  fixtureContexts.set(sessions + 1, { previousAssistant, shortReplies,
    decisionDelayMs, maxRequestAgeMs, firstWaitingReason,
    serverReadDelayMs, serverAfterReadDelayMs, enforceServerPacing, preparationDecision, preparationDelayMs, waitingPhrase, followUpPhrase, followUpDelayMs, prepared: null,
    serverLastClassifiedAt: -Infinity, serverLastCueAt: -Infinity, serverRevision: 0,
    snapshot: { mode: 'live', stage: 'service', ended: false, proposal: null, pending_operations: [], transcript: [], events: [], ...snapshot } });
  await page.evaluate(async predictionEnabled => {
    await window.duplexSession?.stop('fixture_reset');
    const { DuplexSession } = await import('/static/duplex-session.js');
    const input = new AudioContext({ sampleRate: 48000 }), destination = input.createMediaStreamDestination();
    const oscillator = input.createOscillator(), gain = input.createGain();
    oscillator.frequency.value = 430; gain.gain.value = 0;
    oscillator.connect(gain); gain.connect(destination); oscillator.start(); await input.resume();
    window.duplexFixtureInputs ||= []; window.duplexFixtureInputs.push({ input, oscillator });
    window.duplexFixtureInput = { input, gain };
    window.duplexEvidence = [];
    window.duplexGraphEvidence = { starts: [], samples: [], gains: [] };
    window.duplexSession = new DuplexSession({ callbacks: {
      event: row => window.duplexEvidence.push(row),
      status: status => { document.querySelector('#status').textContent = status; },
    }, runtime: { getUserMedia: async () => { window.duplexSession.context.tapConversationCaller(destination.stream); return destination.stream; }, AudioContext: window.DuplexObservedAudioContext } });
    if (!predictionEnabled) window.duplexSession.queueWaitingPreparation = () => {};
    await window.duplexSession.start();
  }, predictionEnabled);
  await until(() => Boolean(sockets[sessions - 1]), 'fixture_socket_missing');
  provider = sockets[sessions - 1];
  return provider;
}

async function yieldFixtureCaller(text = 'What are the usual maintenance options for this vehicle?', { turn = 1 } = {}) {
  const stopped = await eventCount('caller_stopped');
  await setMic(0);
  sendJson(provider, { type: 'speech-update', role: 'user', status: 'stopped', turn });
  // A waiting-only fixture must really release both speech gates. Finals
  // delivered while either gate is active are valid listening observations.
  await until(() => page.evaluate(() => !window.duplexSession.localSpeaking), 'fixture_microphone_did_not_yield');
  sendJson(provider, { type: 'transcript', role: 'user', transcriptType: 'final', transcript: text, turn });
  await until(async () => await eventCount('caller_stopped') > stopped, 'fixture_caller_did_not_yield');
  return page.evaluate(() => ({ origin_ms: window.duplexSession.startAt,
    released_at_ms: window.duplexSession.events.findLast(row => row.kind === 'caller_stopped').at_ms }));
}

async function completedListeningCaller() {
  await newFixtureSession(); await setMic(0.25);
  sendJson(provider, { type: 'speech-update', role: 'user', status: 'started', turn: 1 });
  sendJson(provider, { type: 'transcript', role: 'user', transcriptType: 'partial', transcript: 'My vehicle has been making a humming noise during longer highway journeys.', turn: 1 });
  await until(async () => await eventCount('cue_started') > 0, 'scenario_listening_cue_missing');
  await until(() => page.evaluate(() => window.duplexGraphEvidence.starts.some(source => source.samples === 10720
    && source.ended_ms && !source.stopped)), 'scenario_listening_clip_incomplete');
}

async function captureBackendCueEvidence() {
  if (!live || !page) return;
  try {
    report.backend_cue_evidence = await page.evaluate(async () => {
      const evidence = await window.duplexSession.request('/duplex/evidence');
      const numbers = ['epoch', 'revision', 'confidence', 'probability', 'language_probability', 'retry_after_ms',
        'request_elapsed_ms', 'classification_ms', 'expires_in_ms', 'max_request_age_ms'];
      const budgetKeys = ['window_ms', 'cue_reservations_in_window', 'classification_attempts_in_window',
        'turn_classification_attempts', 'lifetime_cue_reservations', 'lifetime_classification_attempts'];
      const rows = Array.isArray(evidence.cue_decisions) ? evidence.cue_decisions.slice(-256) : [];
      return { available: Array.isArray(evidence.cue_decisions),
        scope: evidence.cue_decision_scope === 'bounded_server_selection_results_not_proof_of_browser_playback'
          ? evidence.cue_decision_scope : 'unknown',
        cue_decisions: rows.map(row => ({ phase: ['listening', 'waiting'].includes(row.phase) ? row.phase : null,
          phrase_id: ['neutral_ack', 'moment', 'one_sec', 'just_a_sec', 'let_me_see', 'checking', 'taking_a_look', 'working_on_it', 'silence'].includes(row.phrase_id) ? row.phrase_id : null,
          reason: /^[a-z_]{1,60}$/.test(row.reason || '') ? row.reason : 'unclassified_result',
          ...Object.fromEntries(numbers.map(key => [key, Number.isFinite(row[key]) ? row[key] : null])),
          budget: Object.fromEntries(budgetKeys.map(key => [key, Number.isFinite(row.budget?.[key]) ? row.budget[key] : null])) })),
        transcript_or_credentials_included: false };
    });
  } catch { report.backend_cue_evidence = { available: false, reason: 'readback_unavailable' }; }
}

async function saveLiveRecordings() {
  if (!live || !page || !value('--output') || report.playback_recording) return;
  const recorded = await page.evaluate(() => {
    const audio = window.duplexSession?.context;
    if (!audio?.recordBlocks) return null;
    const encode = blocks => {
      const length = blocks.reduce((sum, block) => sum + block.length, 0);
      const pcm = new Uint8Array(length * 2), view = new DataView(pcm.buffer); let offset = 0;
      for (const block of blocks) for (const sample of block) {
        const bounded = Math.max(-1, Math.min(1, sample));
        view.setInt16(offset, Math.round(bounded * (bounded < 0 ? 32768 : 32767)), true); offset += 2;
      }
      const strings = [];
      for (let index = 0; index < pcm.length; index += 8192) strings.push(String.fromCharCode(...pcm.subarray(index, index + 8192)));
      return btoa(strings.join(''));
    };
    return { playback: encode(audio.recordBlocks), conversation: encode(audio.conversationBlocks),
      sample_rate: audio.sampleRate, start_ms: audio.recordingStartMs };
  });
  if (!recorded) return;
  for (const kind of ['playback', 'conversation']) {
    const raw = Buffer.from(recorded[kind], 'base64'), header = Buffer.alloc(44);
    header.write('RIFF', 0); header.writeUInt32LE(raw.length + 36, 4); header.write('WAVEfmt ', 8);
    header.writeUInt32LE(16, 16); header.writeUInt16LE(1, 20); header.writeUInt16LE(1, 22);
    header.writeUInt32LE(recorded.sample_rate, 24); header.writeUInt32LE(recorded.sample_rate * 2, 28);
    header.writeUInt16LE(2, 32); header.writeUInt16LE(16, 34); header.write('data', 36); header.writeUInt32LE(raw.length, 40);
    const output = path.resolve(value('--output')).replace(/\.json$/i, '') + '-' + kind + '.wav';
    const wav = Buffer.concat([header, raw]);
    await fs.writeFile(output, wav);
    report[kind + '_recording'] = { path: output, sha256: createHash('sha256').update(wav).digest('hex'), wav_bytes: wav.length,
      scope: kind === 'playback' ? 'actual_browser_mixed_assistant_output_cached_cues_and_main_PCM' : 'synthetic_caller_plus_actual_assistant_browser_graph_at_half_gain_each',
      caller_audio_included: kind === 'conversation', sample_rate: recorded.sample_rate,
      recording_start_browser_ms: recorded.start_ms, pcm_bytes: raw.length, seconds: raw.length / (recorded.sample_rate * 2) };
  }
}

try {
  if (live && value('--output')) {
    const outputBase = path.resolve(value('--output')).replace(/\.json$/i, '');
    for (const target of [path.resolve(value('--output')), outputBase + '-playback.wav', outputBase + '-conversation.wav']) {
      try { await fs.access(target); throw new Error('live_output_already_exists'); }
      catch (error) { if (error.code !== 'ENOENT') throw error; }
    }
  }
  const module = value('--playwright-module');
  let playwright;
  if (module) playwright = await import(pathToFileURL(path.resolve(module)).href);
  else {
    try { playwright = await import('playwright'); }
    catch {
      const candidates = [path.resolve(path.dirname(process.execPath), '..', 'node_modules', 'playwright', 'index.mjs'),
        path.join(os.homedir(), '.cache', 'codex-runtimes', 'codex-primary-runtime', 'dependencies', 'node', 'node_modules', 'playwright', 'index.mjs')];
      for (const candidate of candidates) { try { playwright = await import(pathToFileURL(candidate).href); break; } catch {} }
      if (!playwright) throw new Error('playwright_module_unavailable');
    }
  }
  const { chromium } = playwright;
  const launch = { headless: true, args: ['--autoplay-policy=no-user-gesture-required'] };
  if (value('--browser')) launch.executablePath = path.resolve(value('--browser'));
  else launch.channel = 'chrome';
  browser = await chromium.launch(launch);
  context = await browser.newContext(); page = await context.newPage();
  await page.addInitScript(() => {
    const NativeContext = AudioContext;
    window.DuplexObservedAudioContext = class extends NativeContext {
      constructor(options) {
        super(options);
        this.analyser = this.createAnalyser(); this.analyser.fftSize = 256;
        this.analysisSink = this.createGain(); this.analysisSink.gain.value = 0;
        this.analyser.connect(this.analysisSink); this.analysisSink.connect(this.destination);
        this.recordBlocks = []; this.conversationBlocks = [];
        this.recordingReady = this.audioWorklet.addModule(new URL('/static/duplex-capture-worklet.js', location.href).href).then(() => {
          this.recordingNode = new AudioWorkletNode(this, 'servicevoice-duplex-capture', {
            numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1], channelCount: 1, channelCountMode: 'explicit' });
          this.recordingNode.port.onmessage = event => {
            if (this.recordBlocks.length < 40000 && event.data?.samples instanceof Float32Array) this.recordBlocks.push(event.data.samples);
          };
          this.recordingNode.connect(this.analysisSink);
          this.conversationNode = new AudioWorkletNode(this, 'servicevoice-duplex-capture', {
            numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1], channelCount: 1, channelCountMode: 'explicit' });
          this.conversationNode.port.onmessage = event => {
            if (this.conversationBlocks.length < 40000 && event.data?.samples instanceof Float32Array) this.conversationBlocks.push(event.data.samples);
          };
          this.conversationNode.connect(this.analysisSink);
          this.conversationAssistantGain = this.createGain(); this.conversationAssistantGain.gain.value = 0.5;
          this.conversationAssistantGain.connect(this.conversationNode);
          // Keep the recording clock continuous through pauses between speakers.
          this.recordingSilence = this.createConstantSource(); this.recordingSilence.offset.value = 0;
          this.recordingSilence.connect(this.recordingNode); this.recordingSilence.start();
          this.recordingSilence.connect(this.conversationNode);
          this.recordingStartMs = performance.now();
        });
        const makeGain = this.createGain.bind(this), gainIds = new WeakMap();
        this.createGain = () => {
          const gain = makeGain(), connect = gain.connect.bind(gain);
          const row = { gain_id: window.duplexGraphEvidence.gains.length + 1, automation: [], to_destination: false };
          gainIds.set(gain, row.gain_id); window.duplexGraphEvidence.gains.push(row);
          for (const method of ['setValueAtTime', 'linearRampToValueAtTime', 'cancelScheduledValues', 'cancelAndHoldAtTime']) {
            if (typeof gain.gain[method] !== 'function') continue;
            const original = gain.gain[method].bind(gain.gain);
            gain.gain[method] = (...args) => {
              row.automation.push({ method, args, observed_context_s: this.currentTime });
              return original(...args);
            };
          }
          gain.connect = (destination, ...args) => {
            if (destination === this.destination && !row.to_destination) {
              row.to_destination = true; row.initial_value = gain.gain.value;
              // Observe the post-gain signal, so a scheduled fade is present in
              // the analyser and both retained recordings, not just metadata.
              connect(this.analyser); if (this.recordingNode) connect(this.recordingNode);
              if (this.conversationAssistantGain) connect(this.conversationAssistantGain);
            }
            return connect(destination, ...args);
          };
          return gain;
        };
        const makeSource = this.createBufferSource.bind(this);
        this.createBufferSource = () => {
          const source = makeSource(), connect = source.connect.bind(source), start = source.start.bind(source), stop = source.stop.bind(source);
          let row, outputGainId = null;
          source.connect = (destination, ...args) => {
            outputGainId = gainIds.get(destination) ?? outputGainId;
            if (destination === this.destination) {
              connect(this.analyser); if (this.recordingNode) connect(this.recordingNode);
              if (this.conversationAssistantGain) connect(this.conversationAssistantGain);
            }
            return connect(destination, ...args);
          };
          source.start = (...args) => {
            const offset = Number.isFinite(args[1]) ? args[1] : 0;
            const clockBefore = performance.now(), contextClock = this.currentTime, clockAfter = performance.now();
            row = { sample_rate: source.buffer.sampleRate, samples: source.buffer.length,
              buffer_seconds: source.buffer.duration, start_offset_s: offset,
              seconds: Math.max(0, source.buffer.duration - offset),
              scheduled_at_s: args[0], context_time_at_start_s: contextClock,
              clock_read_span_ms: clockAfter - clockBefore,
              started_ms: clockAfter, stopped: false, gain_id: outputGainId };
            window.duplexGraphEvidence.starts.push(row); return start(...args);
          };
          source.stop = (...args) => { if (row) { row.stopped = true; row.stopped_ms = performance.now();
            row.stopped_context_s = Number.isFinite(args[0]) ? args[0] : this.currentTime; } return stop(...args); };
          source.addEventListener('ended', () => { if (row) row.ended_ms = performance.now(); });
          return source;
        };
        const sample = new Float32Array(this.analyser.fftSize);
        this.analysisTimer = setInterval(() => {
          this.analyser.getFloatTimeDomainData(sample);
          const rms = Math.sqrt(sample.reduce((n, x) => n + x * x, 0) / sample.length);
          if (rms > 0.001 && window.duplexGraphEvidence.samples.length < 12000) window.duplexGraphEvidence.samples.push({ at_ms: performance.now(), rms,
            cue: window.duplexSession?.cuePlaying === true, input_frames: window.duplexSession?.inputFrames || 0 });
        }, 10);
      }
      tapConversationCaller(stream) {
        this.conversationCallerSource = this.createMediaStreamSource(stream);
        this.conversationCallerGain = this.createGain(); this.conversationCallerGain.gain.value = 0.5;
        this.conversationCallerSource.connect(this.conversationCallerGain);
        // This recorder's output is always zero; caller audio never reaches speakers.
        this.conversationCallerGain.connect(this.conversationNode);
      }
      async resume() { await super.resume(); await this.recordingReady; }
      async close() { clearInterval(this.analysisTimer); return super.close(); }
    };
  });
  page.on('pageerror', () => errors.push('browser_page_error'));

  if (live) {
    const address = new URL(value('--base-url') || 'http://127.0.0.1:8785');
    if (!['http:', 'https:'].includes(address.protocol) || !['127.0.0.1', 'localhost', '[::1]'].includes(address.hostname)
        || address.username || address.password || address.search || address.hash) throw new Error('loopback_base_required');
    if (!value('--caller-pcm')) throw new Error('caller_pcm_required');
    const pcm = await fs.readFile(path.resolve(value('--caller-pcm')));
    if (!pcm.length || pcm.length % 2 || pcm.length > 16000 * 2 * 25) throw new Error('caller_pcm_invalid');
    const interruptPcm = value('--interrupt-pcm') ? await fs.readFile(path.resolve(value('--interrupt-pcm'))) : null;
    if (interruptPcm && (!interruptPcm.length || interruptPcm.length % 2 || interruptPcm.length > 16000 * 2 * 15)) throw new Error('interrupt_pcm_invalid');
    if (interruptPcm) report.interruption = { requested: true, input_seconds: interruptPcm.length / 32000, passed: false };
    const extraTurns = [];
    if (value('--conversation-plan')) {
      if (interruptPcm) throw new Error('conversation_plan_and_interrupt_are_separate_scenarios');
      const planPath = path.resolve(value('--conversation-plan'));
      const turnPlan = JSON.parse(await fs.readFile(planPath, 'utf8'));
      if (!Array.isArray(turnPlan) || !turnPlan.length || turnPlan.length > 6) throw new Error('conversation_plan_bounds');
      for (const turn of turnPlan) {
        if (!/^[a-z0-9_-]{1,50}$/.test(turn.id) || typeof turn.pcm_path !== 'string'
          || typeof turn.expect_waiting !== 'boolean' || !Number.isFinite(turn.pause_ms)
          || turn.pause_ms < 300 || turn.pause_ms > 17000) throw new Error('conversation_turn_invalid');
        const audio = await fs.readFile(path.resolve(path.dirname(planPath), turn.pcm_path));
        if (!audio.length || audio.length % 2 || audio.length > 16000 * 2 * 25) throw new Error('conversation_pcm_invalid');
        extraTurns.push({ id: turn.id, expect_waiting: turn.expect_waiting, pause_ms: turn.pause_ms, audio });
      }
    }
    base = address.origin;
    report.cue_decisions = [];
    page.on('request', request => {
      if (!request.url().endsWith('/duplex/cue')) return;
      try {
        const input = request.postDataJSON();
        cueRequests.push({ request, phase: input.phase, epoch: input.epoch, revision: input.revision,
          prepare_only: input.prepare_only === true, prepared_revision: input.prepared_revision ?? null, follow_up: input.follow_up === true,
          started_at_ms: performance.now(), text_characters: typeof input.text === 'string' ? input.text.length : 0 });
      } catch { /* Request diagnostics never retain caller transcript content. */ }
    });
    page.on('requestfailed', request => {
      const entry = cueRequests.find(row => row.request === request);
      if (entry) { entry.finished_at_ms = performance.now(); entry.outcome = 'aborted_or_failed'; }
    });
    page.on('response', async response => {
      if (response.url().endsWith('/api/duplex/config')) {
        try { const config = await response.json(); report.cue_assets = config.phrases.map(row => ({
          phrase_id: row.phrase_id, pcm_bytes: row.pcm_bytes, sample_rate: row.sample_rate, sha256: row.sha256 })); } catch {}
      }
      if (response.url().endsWith('/duplex/call') && response.status() >= 200 && response.status() < 300) {
        // Allocation can succeed before browser start subsequently fails. Count
        // it even when the later WebSocket or audio initialization rejects.
        report.calls_started = 1;
        report.call_allocation_observed = true;
        try {
          const body = await response.json();
          if (typeof body.call_id === 'string' && /^[a-z0-9-]{1,100}$/i.test(body.call_id)) report.call_id = body.call_id;
        } catch { /* Never copy capability URLs or token-bearing raw responses. */ }
      }
      if (response.url().endsWith('/duplex/stop')) {
        report.provider_stop = { http_status: response.status() };
        try { const body = await response.json(); report.provider_stop.ok = body.ok === true; } catch {}
      }
      if (!response.url().endsWith('/duplex/cue')) return;
      const entry = cueRequests.find(row => row.request === response.request());
      if (entry) { entry.finished_at_ms = performance.now(); entry.outcome = 'response'; entry.status = response.status(); }
      try {
        const data = await response.json();
        report.cue_decisions.push(Object.fromEntries(['phrase_id', 'phase', 'reason', 'probability', 'confidence', 'classification_ms',
          'language_choice', 'language_probability', 'language_confidence', 'epoch', 'revision',
          'prepared', 'prepared_reused', 'prepared_expires_in_ms', 'requests', 'follow_up', 'content_kind', 'content_kind_probability', 'content_kind_confidence']
          .filter(key => Object.hasOwn(data, key)).map(key => [key, data[key]])));
      } catch { report.cue_decisions.push({ status: response.status() }); }
    });
    await page.goto(base + '/duplex', { waitUntil: 'domcontentloaded' });
    await page.evaluate(async encoded => {
      const { DuplexSession } = await import('/static/duplex-session.js');
      const input = new AudioContext({ sampleRate: 16000 }), destination = input.createMediaStreamDestination();
      const makeInput = base64 => {
        const bytes = Uint8Array.from(atob(base64), x => x.charCodeAt(0));
        const view = new DataView(bytes.buffer), samples = new Float32Array(bytes.length / 2);
        for (let i = 0; i < samples.length; i++) samples[i] = view.getInt16(i * 2, true) / 32768;
        const buffer = input.createBuffer(1, samples.length, 16000); buffer.copyToChannel(samples, 0);
        const source = input.createBufferSource(); source.buffer = buffer; source.connect(destination);
        return { source, seconds: samples.length / 16000 };
      };
      const caller = makeInput(encoded.caller), interruption = encoded.interruption ? makeInput(encoded.interruption) : null;
      window.duplexEvidence = [];
      window.duplexGraphEvidence = { starts: [], samples: [], gains: [] };
      window.duplexSession = new DuplexSession({ callbacks: { event: row => window.duplexEvidence.push(row) },
        runtime: { getUserMedia: async () => { window.duplexSession.context.tapConversationCaller(destination.stream); return destination.stream; }, AudioContext: window.DuplexObservedAudioContext } });
      window.duplexLiveInput = { input, destination, ...caller, interruption };
      await input.resume(); await window.duplexSession.start();
    }, { caller: pcm.toString('base64'), interruption: interruptPcm?.toString('base64') || null });
    report.calls_started = 1; report.external_requests = 'provider_requests_from_operator_backend';
    await until(async () => (await greetingState()).ready, 'greeting_not_completed', 15000);
    report.greeting = { ...(await greetingState()), quiet_guard_ms: 200, missing_stop_idle_fallback_ms: 1000 };
    report.caller_input = await page.evaluate(() => {
      const caller = window.duplexLiveInput;
      caller.started_at_ms = performance.now() - window.duplexSession.startAt; caller.source.start();
      return { started_at_ms: caller.started_at_ms, input_end_ms: caller.started_at_ms + caller.seconds * 1000,
        duration_ms: caller.seconds * 1000 };
    });
    await delay(pcm.length / 32 + 500);
    if (interruptPcm) {
      await until(async () => (await replyState(report.caller_input, false)).ready,
        'main_answer_not_started_for_interruption', 35000);
      await delay(150);
      const start = await page.evaluate(() => {
        const session = window.duplexSession, interruption = window.duplexLiveInput.interruption;
        interruption.started_at_ms = performance.now() - session.startAt;
        const state = { at_ms: interruption.started_at_ms, end_ms: interruption.started_at_ms + interruption.seconds * 1000,
          main_speaking: session.mainSpeaking, input_frames: session.inputFrames,
          last_signal_ms: window.duplexGraphEvidence.samples.at(-1)?.at_ms - session.startAt };
        interruption.source.start(); return state;
      });
      Object.assign(report.interruption, { started_at_ms: start.at_ms, input_end_ms: start.end_ms });
      await delay(interruptPcm.length / 32 + 500);
      await until(async () => (await replyState({ started_at_ms: start.at_ms, input_end_ms: start.end_ms })).ready,
        'main_answer_after_interruption_not_completed', 35000);
      const interrupted = await page.evaluate(start => ({
        rows: window.duplexSession.events.filter(row => row.at_ms >= start.at_ms),
        graph: window.duplexGraphEvidence.samples.filter(row => row.at_ms - window.duplexSession.startAt >= start.at_ms),
        start_at_ms: window.duplexSession.startAt, input_frames: window.duplexSession.inputFrames,
      }), start);
      const pause = interrupted.rows.find(row => row.kind === 'provisional_barge_in');
      const confirmation = interrupted.rows.find(row => row.kind === 'provisional_confirmed');
      const answered = interrupted.rows.some(row => row.kind === 'main_started' && row.at_ms > start.end_ms);
      const graphQuiet = pause && confirmation && !interrupted.graph.some(row => !row.cue
        && row.at_ms - interrupted.start_at_ms > pause.at_ms + 80
        && row.at_ms - interrupted.start_at_ms < confirmation.at_ms);
      report.interruption = { requested: true, input_seconds: interruptPcm.length / 32000,
        started_at_ms: start.at_ms, input_end_ms: start.end_ms,
        pause_at_ms: pause?.at_ms ?? null, provider_confirmation_at_ms: confirmation?.at_ms ?? null,
        confirmation_source: confirmation?.source || null,
        checks: {
          began_during_audible_main: start.main_speaking && start.last_signal_ms >= start.at_ms - 100,
          uplink_continued: interrupted.input_frames > start.input_frames + 4,
          provider_confirmed_interruption: Boolean(confirmation),
          local_pause_preceded_provider_confirmation: Boolean(pause && confirmation && pause.at_ms <= confirmation.at_ms && graphQuiet),
          main_answer_after_new_input: answered,
          confirmed_input_did_not_play_old_main: Boolean(confirmation) && !interrupted.graph.some(row => !row.cue
            && row.at_ms - interrupted.start_at_ms > confirmation.at_ms + 80 && row.at_ms - interrupted.start_at_ms < start.end_ms),
        } };
      report.interruption.passed = Object.values(report.interruption.checks).every(Boolean);
    } else await until(async () => (await replyState(report.caller_input)).ready, 'main_answer_not_completed', 35000);
    if (extraTurns.length) {
      const summarizeTurn = async (id, input, expectWaiting) => page.evaluate(({ id, input, expectWaiting }) => {
        const session = window.duplexSession, events = session.events;
        const main = events.find(row => row.kind === 'main_started' && row.at_ms >= input.input_end_ms);
        const cues = events.filter(row => row.kind === 'cue_started' && row.at_ms >= input.started_at_ms && row.at_ms < main.at_ms);
        const waiting = cues.find(row => row.phase === 'waiting');
        const release = events.findLast(row => row.kind === 'caller_stopped' && row.at_ms >= input.started_at_ms && row.at_ms < main.at_ms);
        const delay = main.at_ms - input.input_end_ms;
        return { id, ...input, main_packet_ready_at_ms: main.at_ms, main_after_source_end_ms: delay,
          main_after_release_ms: main.at_ms - release.at_ms, expected_waiting_when_slow: expectWaiting,
          waiting_cue_present: Boolean(waiting), waiting_after_source_end_ms: waiting ? waiting.at_ms - input.input_end_ms : null,
          listening_cues: cues.filter(row => row.phase === 'listening').length,
          cue_stop_reasons: events.filter(row => row.kind === 'cue_stopped' && row.at_ms >= input.started_at_ms && row.at_ms <= main.at_ms + 50).map(row => row.reason),
          passed: !expectWaiting || delay < 1100 || Boolean(waiting) };
      }, { id, input, expectWaiting });
      report.conversation_turns = [await summarizeTurn('initial', report.caller_input, requireWaiting)];
      for (const turn of extraTurns) {
        await delay(turn.pause_ms);
        const input = await page.evaluate(encoded => {
          const input = window.duplexLiveInput.input, session = window.duplexSession;
          if (session.closed) throw new Error('conversation_ended_before_next_turn');
          const bytes = Uint8Array.from(atob(encoded), x => x.charCodeAt(0)), view = new DataView(bytes.buffer);
          const samples = new Float32Array(bytes.length / 2);
          for (let index = 0; index < samples.length; index++) samples[index] = view.getInt16(index * 2, true) / 32768;
          const buffer = input.createBuffer(1, samples.length, 16000); buffer.copyToChannel(samples, 0);
          const source = input.createBufferSource(); source.buffer = buffer; source.connect(window.duplexLiveInput.destination);
          const started = performance.now() - session.startAt; source.start();
          return { started_at_ms: started, input_end_ms: started + samples.length / 16, duration_ms: samples.length / 16 };
        }, turn.audio.toString('base64'));
        await delay(turn.audio.length / 32 + 400);
        await until(async () => (await replyState(input)).ready, 'conversation_reply_not_completed_' + turn.id, 35000);
        report.conversation_turns.push(await summarizeTurn(turn.id, input, turn.expect_waiting));
      }
      report.checks.multi_turn_waiting_coverage = report.conversation_turns.every(row => row.passed);
    }
    // Provider speech-stop can precede the final queued browser samples. Drain
    // the actual playback clock so recordings retain the sentence's ending.
    await delay(400);
    await until(() => page.evaluate(() => {
      const session = window.duplexSession;
      return session.playback.main.size === 0 && session.context.currentTime >= session.playback.mainEnd - 0.01;
    }), 'audio_playback_not_drained', 4000);
    report.checks.final_browser_playback_drained = true;
    await captureBackendCueEvidence();
    const evidence = await page.evaluate(() => ({ events: window.duplexSession.events, metrics: window.duplexSession.metrics(),
      graph: window.duplexGraphEvidence, start_at_ms: window.duplexSession.startAt, input_start_ms: window.duplexLiveInput.started_at_ms,
      input_end_ms: window.duplexLiveInput.started_at_ms + window.duplexLiveInput.seconds * 1000 }));
    report.events = evidence.events; report.metrics = evidence.metrics;
    report.timeline_origin_browser_ms = evidence.start_at_ms;
    const answerAfterSource = (end, before = Infinity, start = evidence.input_start_ms) => {
      const first = evidence.events.find(row => row.kind === 'main_started' && row.at_ms >= end && row.at_ms < before
        && evidence.events.some(release => release.kind === 'caller_stopped' && release.at_ms >= start && release.at_ms < row.at_ms));
      return first ? first.at_ms - end : null;
    };
    report.answer_after_source_end_ms = {
      caller: answerAfterSource(evidence.input_end_ms, report.interruption?.started_at_ms ?? Infinity),
      ...(report.interruption?.requested ? { interruption: answerAfterSource(report.interruption.input_end_ms, Infinity, report.interruption.started_at_ms) } : {}),
    };
    report.cue_requests = cueRequests.map(({ request, ...entry }) => ({ ...entry,
      elapsed_ms: entry.finished_at_ms === undefined ? null : entry.finished_at_ms - entry.started_at_ms }));
    report.checks.listening_cue_during_input = evidence.events.some(event => event.kind === 'cue_started' && event.phase === 'listening'
      && event.at_ms >= evidence.input_start_ms && event.at_ms < evidence.input_end_ms);
    report.checks.main_audio_observed = evidence.metrics.first_voiced_main_after_turn_release_ms?.length > 0
      && evidence.graph.samples.some(sample => !sample.cue && sample.at_ms - evidence.start_at_ms >= evidence.input_end_ms);
    report.checks.microphone_frames_during_actual_cue = evidence.metrics.input_frames_during_cue > 0
      && evidence.graph.samples.some(sample => sample.cue && sample.rms > 0.01);
    report.checks.audible_caller_overlapped_audible_cue = evidence.graph.samples.some(sample => sample.cue && sample.rms > 0.01
      && sample.at_ms - evidence.start_at_ms >= evidence.input_start_ms
      && sample.at_ms - evidence.start_at_ms < evidence.input_end_ms
      && inputRmsNear(pcm, sample.at_ms - evidence.start_at_ms - evidence.input_start_ms) > 0.01);
    if (requireWaiting) {
      const main = evidence.events.find(row => row.kind === 'main_started' && row.at_ms >= evidence.input_end_ms
        && evidence.events.some(release => release.kind === 'caller_stopped' && release.at_ms >= evidence.input_start_ms && release.at_ms < row.at_ms));
      const waiting = evidence.events.find(row => row.kind === 'cue_started' && row.phase === 'waiting'
        && row.at_ms >= evidence.input_end_ms && (!main || row.at_ms < main.at_ms));
      const moment = report.cue_assets?.find(row => row.phrase_id === waiting?.phrase_id);
      const source = waiting && evidence.graph.starts.find(row => row.samples * 2 === moment?.pcm_bytes
        && Math.abs(row.started_ms - evidence.start_at_ms - waiting.at_ms) < 10);
      const mainSource = main && evidence.graph.starts.find(row => !row.gain_id && Math.abs(row.started_ms - evidence.start_at_ms - main.at_ms) < 10);
      const cueEnd = source && (source.stopped_context_s ?? source.scheduled_at_s + source.seconds);
      const fade = cueFade(evidence.graph, source);
      report.waiting_cue = { required: true, started_at_ms: waiting?.at_ms ?? null,
        prior_listening_required: !waitingOnly,
        intentionally_preempted: Boolean(source?.stopped), clip_completed: Boolean(source?.ended_ms && !source.stopped),
        scheduled_clip_ms: source ? source.seconds * 1000 : null,
        played_clip_ms: source ? Math.max(0, Math.min(source.seconds, cueEnd - source.scheduled_at_s)) * 1000 : null,
        end_reason: source?.stopped ? 'intentionally_stopped' : source?.ended_ms ? 'natural' : 'unknown',
        fade: fade,
        main_handoff_delay_ms: mainSource ? Math.max(0, mainSource.scheduled_at_s - mainSource.context_time_at_start_s) * 1000 : null, checks: {
          ...(waitingOnly ? { waiting_after_input: Boolean(waiting) }
            : { same_input_received_listening_and_waiting: Boolean(waiting && report.checks.listening_cue_during_input) }),
          waiting_was_audible_before_main: Boolean(waiting && main && evidence.graph.samples.some(row => row.cue && row.rms > .01
            && row.at_ms - evidence.start_at_ms >= waiting.at_ms && row.at_ms - evidence.start_at_ms < main.at_ms)),
          assistant_sources_did_not_play_simultaneously: Boolean(source && mainSource && cueEnd <= mainSource.scheduled_at_s + .000001),
          cue_completed_without_main_truncation: Boolean(source?.ended_ms && !source.stopped),
          main_handoff_bounded_1100ms: Boolean(mainSource && Number.isFinite(mainSource.context_time_at_start_s)
            && mainSource.scheduled_at_s - mainSource.context_time_at_start_s <= 1.100001),
        } };
      report.waiting_cue.passed = Object.values(report.waiting_cue.checks).every(Boolean);
    }
    report.audio_graph = evidence.graph;
    await saveLiveRecordings();
    report.checks.live_scope = 'synthetic_audio_input_and_actual_browser_playback';
    report.classification_quality_verified = false;
    report.passed = report.checks.main_audio_observed
      && (waitingOnly || (report.checks.listening_cue_during_input && report.checks.microphone_frames_during_actual_cue
        && report.checks.audible_caller_overlapped_audible_cue))
      && (!requireWaiting || report.waiting_cue?.passed === true)
      && (!extraTurns.length || report.checks.multi_turn_waiting_coverage)
      && (!interruptPcm || report.interruption?.passed === true);
  } else {
    server = http.createServer(async (request, response) => {
      const pathname = new URL(request.url, 'http://127.0.0.1').pathname;
      if (pathname === '/probe') {
        response.writeHead(200, { 'Content-Type': 'text/html' });
        response.end('<!doctype html><html><title>Duplex deterministic browser test</title><body><h1>Duplex playback verification</h1><p id="status">Starting</p></body></html>'); return;
      }
      if (/^\/static\/duplex-[a-z-]+\.js$/.test(pathname)) {
        try { const source = await fs.readFile(path.join(root, 'app', pathname)); response.writeHead(200, { 'Content-Type': 'application/javascript' }); response.end(source); }
        catch { response.writeHead(404); response.end(); } return;
      }
      requests.push({ method: request.method, pathname });
      let body = ''; for await (const chunk of request) body += chunk;
      const reply = object => { response.writeHead(200, { 'Content-Type': 'application/json' }); response.end(JSON.stringify(object)); };
      if (pathname === '/api/duplex/config') { reply({ enabled: true, sample_rate: 16000, phrases, scope: 'isolated_browser_owned_playback', phone_enabled: false }); return; }
      if (pathname === '/api/sessions' && request.method === 'POST') { sessions++; reply({ id: 'fixture-session-' + sessions, token: 'fixture-token' }); return; }
      if (pathname.includes('/duplex/audio/') && Object.hasOwn(cueAudio, pathname.split('/').at(-1))) {
        const pcm = cueAudio[pathname.split('/').at(-1)]; response.writeHead(200, { 'Content-Type': 'application/octet-stream', 'X-Audio-Sample-Rate': '16000' }); response.end(pcm); return;
      }
      if (pathname.endsWith('/duplex/call')) { reply({ call_id: 'fixture-call-' + sessions, websocket_url: 'wss://duplex-fixture.vapi.ai/fixture-call-' + sessions + '/transport', sample_rate: 16000 }); return; }
      if (pathname.endsWith('/duplex/cue')) {
        const input = JSON.parse(body);
        const sessionNumber = Number(pathname.match(/fixture-session-(\d+)/)?.[1]);
        const trusted = fixtureContexts.get(sessionNumber);
        const short = input.text.trim().split(/\s+/).length < 3;
        // Only trusted fixture-side context admits the named short replies;
        // caller text cannot supply or override its previous assistant turn.
        const trustedShort = input.phase === 'waiting' && trusted?.previousAssistant
          && trusted.shortReplies.includes(input.text);
        const state = trusted?.snapshot;
        const hardBlocked = state?.ended || state?.pending_operations?.length || ['confirmation', 'complete', 'handoff'].includes(state?.stage)
          || (state?.proposal && state.stage !== 'details');
        const listeningBlocked = input.phase === 'listening' && Boolean(state?.proposal);
        const prepareOnly = input.prepare_only === true;
        const firstWaitingReason = !prepareOnly && input.phase === 'waiting' && trusted?.firstWaitingReason
          && !selections(sessionNumber).some(row => row.phase === 'waiting') ? trusted.firstWaitingReason : null;
        let silence = hardBlocked || listeningBlocked || firstWaitingReason
          || /stop|confirm|goodbye/i.test(input.text) || (input.phase === 'listening' && /actually|correct/i.test(input.text)) || (short && !trustedShort);
        // First session reproduces a provider stream whose partials arrive far
        // faster than a classifier response. A stable-only fixture missed this.
        const entry = { session: sessionNumber, phase: input.phase, epoch: input.epoch,
          revision: input.revision, prepare_only: prepareOnly, prepared_revision: input.prepared_revision ?? null, follow_up: input.follow_up === true,
          at_ms: performance.now() };
        cueRequests.push(entry);
        response.on('close', () => { entry.transport_closed_at_ms = performance.now(); });
        const byPhase = setting => typeof setting === 'number' ? setting : setting?.[input.phase] ?? 0;
        const readDelay = byPhase(trusted?.serverReadDelayMs);
        if (readDelay) await delay(readDelay);
        entry.after_initial_read_ms = performance.now();
        const cached = trusted.prepared;
        const reused = !prepareOnly && !input.follow_up && !silence && cached && cached.text === input.text && cached.epoch === input.epoch
          && cached.revision === input.prepared_revision && cached.previousAssistant === trusted.previousAssistant
          && cached.context === JSON.stringify(trusted.snapshot) && performance.now() - cached.began < 2000;
        const preparationDeclined = prepareOnly && trusted.preparationDecision === 'decline';
        let pacingReason = null, retryAfter = null;
        if (trusted?.enforceServerPacing) {
          trusted.serverRevision = Math.max(trusted.serverRevision, input.revision);
          const cueRemaining = trusted.serverLastCueAt + 300 - performance.now();
          const requestRemaining = trusted.serverLastClassifiedAt + 300 - performance.now();
          if (!prepareOnly && cueRemaining > 0) { pacingReason = 'cue_spacing'; retryAfter = Math.ceil(cueRemaining); }
          else if (!reused && !preparationDeclined && requestRemaining > 0) { pacingReason = 'classification_throttled'; retryAfter = Math.ceil(requestRemaining); }
        }
        const configuredDelay = trusted?.decisionDelayMs;
        const decisionDelay = input.follow_up && trusted.followUpDelayMs !== null ? trusted.followUpDelayMs
          : prepareOnly && trusted.preparationDelayMs !== null ? trusted.preparationDelayMs
          : (configuredDelay !== null && typeof configuredDelay === 'object' ? configuredDelay[input.phase] : configuredDelay)
          ?? (sessionNumber === 7 && input.phase === 'waiting' ? 700
          : [10, 11].includes(sessionNumber) && input.phase === 'waiting' ? 150 : [1, 5].includes(sessionNumber) ? 220 : 35);
        const absoluteAge = trusted?.maxRequestAgeMs ?? (sessionNumber === 11 && input.phase === 'waiting' ? 300 : 800);
        entry.server_classified = !pacingReason && !reused && !preparationDeclined;
        let superseded = false;
        if (entry.server_classified) {
          entry.classification_started_at_ms = performance.now();
          trusted.serverLastClassifiedAt = entry.classification_started_at_ms;
          await delay(decisionDelay);
          superseded = trusted.enforceServerPacing && trusted.serverRevision !== input.revision;
          if (superseded) silence = true;
        }
        if (preparationDeclined) silence = true;
        const afterReadDelay = byPhase(trusted?.serverAfterReadDelayMs);
        if (afterReadDelay && !preparationDeclined) await delay(afterReadDelay);
        if (pacingReason) silence = true;
        entry.responded_at_ms = performance.now();
        entry.max_request_age_ms = absoluteAge;
        entry.expires_in_ms = Math.max(0, absoluteAge - Math.ceil(entry.responded_at_ms - entry.at_ms));
        entry.reason = pacingReason || (superseded ? 'superseded' : null) || firstWaitingReason
          || (preparationDeclined ? 'fixture_preparation_abstained' : null)
          || (hardBlocked ? 'unverified_or_sensitive_context' : 'fixture_decision');
        entry.retry_after_ms = retryAfter === null ? null : Math.max(1, retryAfter - Math.ceil(entry.responded_at_ms - entry.after_initial_read_ms));
        const prepared = prepareOnly && !silence && !response.destroyed && entry.expires_in_ms > 0;
        if (prepared) {
          trusted.prepared = { text: input.text, epoch: input.epoch, revision: input.revision,
            began: entry.classification_started_at_ms, previousAssistant: trusted.previousAssistant,
            context: JSON.stringify(trusted.snapshot) };
          entry.reason = 'waiting_prepared';
        } else if (!prepareOnly && !silence) {
          trusted.serverLastCueAt = performance.now(); entry.reserved_at_ms = trusted.serverLastCueAt;
          trusted.prepared = null;
        }
        entry.prepared = prepared; entry.prepared_reused = Boolean(reused && !silence);
        if (prepareOnly) entry.expires_in_ms = 0;
        reply({ phrase_id: prepareOnly || silence ? 'silence' : input.phase === 'listening' ? 'neutral_ack' : input.follow_up ? trusted.followUpPhrase : trusted.waitingPhrase,
          reason: entry.reason, requests: entry.server_classified ? 1 : 0,
          ...(pacingReason ? { retry_after_ms: entry.retry_after_ms } : {}), confidence: 1,
          probability: 1, language_choice: 'cache_compatible', expires_in_ms: prepareOnly || silence ? 0 : entry.expires_in_ms,
          prepared, prepared_reused: entry.prepared_reused,
          prepared_expires_in_ms: prepared ? Math.max(1, 2000 - Math.ceil(performance.now() - trusted.prepared.began)) : 0,
          max_request_age_ms: absoluteAge,
          epoch: input.epoch, revision: input.revision, phase: input.phase }); return;
      }
      if (pathname.endsWith('/duplex/event') || pathname.endsWith('/duplex/stop') || pathname.endsWith('/end')) {
        const trusted = fixtureContexts.get(Number(pathname.match(/fixture-session-(\d+)/)?.[1]));
        const event = body ? JSON.parse(body).event : null;
        if (trusted && (['barge_in', 'main_started', 'ended', 'muted'].includes(event)
          || pathname.endsWith('/duplex/stop') || pathname.endsWith('/end'))) trusted.prepared = null;
        reply({ ok: true }); return;
      }
      if (/^\/api\/sessions\/fixture-session-\d+$/.test(pathname)) {
        reply(fixtureContexts.get(Number(pathname.match(/fixture-session-(\d+)$/)[1])).snapshot); return;
      }
      response.writeHead(404); response.end();
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve)); base = 'http://127.0.0.1:' + server.address().port;
    await context.routeWebSocket('wss://duplex-fixture.vapi.ai/**', socket => {
      const index = sockets.length; sockets.push(socket);
      socket.onMessage(message => {
        if (typeof message !== 'string') uplink.push({ socket: index, at: performance.now(), bytes: message.length,
          nonzero: Buffer.from(message).some(byte => byte !== 0) });
      });
    });
    await context.route(/^https?:\/\//, route => {
      if (new URL(route.request().url()).origin === base) return route.continue();
      report.external_requests++; return route.abort('blockedbyclient');
    });
    await page.goto(base + '/probe', { waitUntil: 'domcontentloaded' });
    await newFixtureSession();
    await setMic(0.25);
    await until(() => uplink.some(frame => frame.nonzero), 'synthetic_microphone_had_no_signal');
    sendJson(provider, { type: 'speech-update', role: 'user', status: 'started', turn: 1 });
    const stablePrefix = 'My car has been making a humming noise whenever I drive';
    const streamedWords = 'on the highway and I also want to arrange a routine maintenance visit before my next longer journey this month'.split(' ');
    let partialText = stablePrefix + ' fas';
    sendJson(provider, { type: 'transcript', role: 'user', transcriptType: 'partial', transcript: partialText, turn: 1 });
    const partialStream = (async () => {
      for (const tail of ['fast', 'steadily']) {
        await delay(75); partialText = stablePrefix + ' ' + tail;
        sendJson(provider, { type: 'transcript', role: 'user', transcriptType: 'partial', transcript: partialText, turn: 1 });
      }
      for (const word of streamedWords) {
        await delay(75); partialText += ' ' + word;
        sendJson(provider, { type: 'transcript', role: 'user', transcriptType: 'partial', transcript: partialText, turn: 1 });
      }
    })();
    await until(async () => await eventCount('cue_started') > 0, 'listening_cue_not_played');
    let events = await page.evaluate(() => window.duplexSession.events);
    assert.equal(events.find(event => event.kind === 'cue_started').phase, 'listening');
    report.checks.listening_cue_before_caller_finished = true;
    const before = uplink.length; await delay(240);
    report.checks.microphone_frames_during_cue = uplink.length - before;
    assert.ok(report.checks.microphone_frames_during_cue >= 4, 'input_paused_during_cue');
    assert.ok(uplink.slice(before).some(frame => frame.nonzero), 'caller_samples_lost_during_cue');
    await until(() => page.evaluate(() => window.duplexGraphEvidence.samples.some(sample => sample.cue && sample.rms > 0.01)), 'cue_graph_had_no_signal');
    await until(() => page.evaluate(() => window.duplexGraphEvidence.starts.some(source => source.samples === 10720 && source.ended_ms && !source.stopped)), 'complete_cached_clip_not_played');
    await partialStream;
    const streamingRequests = cueRequests.filter(row => row.session === 1);
    assert.ok(streamingRequests.length <= 6, 'rapid_partials_exhausted_classifier_budget');
    report.checks.rapid_partial_updates_preserved_inflight_selection = true;
    report.checks.unstable_last_word_rewrites_preserved_stable_prefix = true;
    report.checks.streaming_partial_revisions = streamedWords.length + 3;
    report.checks.streaming_classifier_requests = streamingRequests.length;
    report.first_session_cue_requests = streamingRequests;
    report.checks.complete_670ms_cached_clip_played_in_real_graph = true;
    report.first_session_graph = await page.evaluate(() => window.duplexGraphEvidence);
    report.first_session_events = await page.evaluate(() => window.duplexSession.events);
    await yieldFixtureCaller();
    await until(() => page.evaluate(() => window.duplexSession.events.some(row => row.kind === 'cue_started'
      && row.phase === 'waiting')), 'same_utterance_waiting_cue_missing');
    await until(() => page.evaluate(() => window.duplexGraphEvidence.starts.some(row => row.samples === 16800
      && row.ended_ms && !row.stopped)), 'same_utterance_waiting_clip_incomplete');
    sendJson(provider, { type: 'speech-update', role: 'assistant', status: 'started', turn: 2 });
    await streamMain(provider, .6);
    sendJson(provider, { type: 'speech-update', role: 'assistant', status: 'stopped', turn: 2 });
    const dualPhases = await page.evaluate(() => ({ events: window.duplexSession.events, graph: window.duplexGraphEvidence }));
    assert.deepEqual(dualPhases.events.filter(row => row.kind === 'cue_started').map(row => row.phase), ['listening', 'waiting']);
    const sameTurnRequests = cueRequests.filter(row => row.session === 1);
    assert.equal(new Set(sameTurnRequests.map(row => row.epoch)).size, 1, 'phase_change_created_another_utterance');
    const completeMoment = dualPhases.graph.starts.find(row => row.samples === 16800);
    const firstMain = dualPhases.graph.starts.find(row => row.samples === 320);
    assert.ok(completeMoment.scheduled_at_s + completeMoment.seconds <= firstMain.scheduled_at_s, 'completed_waiting_cue_overlapped_main');
    report.checks.same_utterance_received_full_listening_then_full_waiting_cue = true;
    report.checks.completed_waiting_cue_did_not_overlap_main = true;
    report.same_utterance_phase_events = dualPhases.events;
    report.same_utterance_phase_graph = dualPhases.graph;

    await newFixtureSession();
    await setMic(0.25); sendJson(provider, { type: 'speech-update', role: 'user', status: 'started', turn: 1 });
    sendJson(provider, { type: 'transcript', role: 'user', transcriptType: 'partial', transcript: 'My car has been making a humming noise whenever I drive on the highway.', turn: 1 });
    await until(async () => await eventCount('cue_started') > 0, 'second_listening_cue_not_played');
    sendJson(provider, { type: 'transcript', role: 'user', transcriptType: 'partial', transcript: 'Actually stop, I need to correct that.', turn: 1 });
    await until(async () => await eventCount('cue_stopped') > 0, 'correction_did_not_cancel_cue');
    report.checks.correction_cancelled_cached_cue = true;
    report.correction_session_events = await page.evaluate(() => window.duplexSession.events);
    report.correction_session_graph = await page.evaluate(() => window.duplexGraphEvidence);

    await newFixtureSession();
    await setMic(0.25); sendJson(provider, { type: 'speech-update', role: 'user', status: 'started', turn: 1 });
    await delay(220); await yieldFixtureCaller('What time does your service department close on weekdays?');
    await until(async () => await eventCount('cue_started') > 0, 'waiting_cue_not_played');
    events = await page.evaluate(() => window.duplexSession.events);
    if (events.find(event => event.kind === 'cue_started').phase !== 'waiting') throw new Error('waiting_phase_was_not_selected');
    sendJson(provider, { type: 'speech-update', role: 'assistant', status: 'started', turn: 2 });
    provider.send(Buffer.alloc(640)); await delay(80);
    assert.equal(await page.evaluate(() => window.duplexSession.cuePlaying), true, 'provider_announcement_or_silence_cut_cue');
    report.checks.provider_announcement_and_silence_preserved_cue = true;
    const count = uplink.length;
    const main = streamMain(provider, 1.2);
    await until(() => page.evaluate(() => window.duplexGraphEvidence.starts.some(row => row.samples === 16800 && row.ended_ms && !row.stopped)), 'main_did_not_preserve_complete_cue');
    await until(() => page.evaluate(() => window.duplexGraphEvidence.samples.some(row => !row.cue && row.rms > .01)), 'main_did_not_follow_complete_cue');
    report.checks.main_audio_waited_for_complete_cue = true;
    await delay(100); assert.ok(uplink.length > count + 2, 'silence_input_paused_during_main');
    const stops = await eventCount('provisional_barge_in');
    await setMic(0.25);
    await until(async () => await eventCount('provisional_barge_in') > stops, 'provisional_input_pause_not_observed');
    // Deliberately delayed provider event: local activity must already pause.
    await delay(100);
    const paused = await page.evaluate(() => ({ event: window.duplexSession.events.findLast(event => event.kind === 'provisional_barge_in'),
      start: window.duplexSession.startAt, last_signal: window.duplexGraphEvidence.samples.at(-1)?.at_ms ?? 0 }));
    assert.ok(paused.last_signal <= paused.start + paused.event.at_ms + 75, 'output_did_not_pause_before_provider_event');
    sendJson(provider, { type: 'user-interrupted', turn: 2 });
    await main;
    report.checks.provisional_output_pause_before_provider_notification = true;
    report.second_session_events = await page.evaluate(() => window.duplexSession.events);
    report.checks.obsolete_main_audio_rejected = (await page.evaluate(() => window.duplexSession.metrics())).discarded_output_bytes > 0;
    assert.equal(report.checks.obsolete_main_audio_rejected, true);
    report.metrics = await page.evaluate(() => window.duplexSession.metrics());

    await newFixtureSession();
    sendJson(provider, { type: 'speech-update', role: 'assistant', status: 'started', turn: 1 });
    const uninterrupted = streamMain(provider, 2);
    await until(() => page.evaluate(() => window.duplexSession.inputFrames >= 6 && window.duplexGraphEvidence.samples.length > 0), 'false_onset_preflight');
    const generation = await page.evaluate(() => window.duplexSession.generation);
    await setMic(0.25);
    await until(async () => await eventCount('provisional_barge_in') > 0, 'false_onset_was_not_detected');
    await setMic(0);
    await until(async () => await eventCount('provisional_resumed') > 0, 'false_onset_lost_primary_answer', 1600);
    const resumed = await page.evaluate(() => ({ generation: window.duplexSession.generation,
      at: window.duplexSession.events.findLast(event => event.kind === 'provisional_resumed').at_ms + window.duplexSession.startAt }));
    assert.equal(resumed.generation, generation, 'false_onset_created_replacement_generation');
    await until(() => page.evaluate(at => window.duplexGraphEvidence.samples.some(sample => sample.at_ms > at && !sample.cue), resumed.at), 'resumed_audio_had_no_graph_signal');
    await uninterrupted;
    report.false_onset_session_events = await page.evaluate(() => window.duplexSession.events);
    report.checks.false_energy_onset_resumed_same_main_generation = true;

    await newFixtureSession();
    await setMic(0.25); sendJson(provider, { type: 'speech-update', role: 'user', status: 'started', turn: 1 });
    sendJson(provider, { type: 'transcript', role: 'user', transcriptType: 'partial', transcript: 'My car has been making a humming noise while driving.', turn: 1 });
    await until(() => cueRequests.some(row => row.session === 5), 'pending_rewrite_request_missing');
    await delay(75);
    sendJson(provider, { type: 'transcript', role: 'user', transcriptType: 'partial', transcript: 'The tires have been vibrating during the longer highway journey.', turn: 1 });
    // Old response arrives after220ms; the cadence-gated replacement cannot
    // arrive until570ms. No output is allowed in that stale-result window.
    await delay(190);
    assert.equal(await eventCount('cue_started'), 0, 'rewritten_prefix_response_played');
    assert.equal(await page.evaluate(() => window.duplexGraphEvidence.starts.length), 0, 'rewritten_prefix_entered_audio_graph');
    await until(async () => await eventCount('cue_started') > 0, 'replacement_prefix_cue_missing');
    const rewriteEvents = await page.evaluate(() => window.duplexSession.events);
    assert.equal(rewriteEvents.find(row => row.kind === 'cue_started').snapshot_revision, 2, 'stale_snapshot_ownership');
    report.checks.pending_snapshot_prefix_rewrite_rejected_before_graph = true;
    report.prefix_rewrite_session_events = rewriteEvents;
    report.prefix_rewrite_cue_requests = cueRequests.filter(row => row.session === 5);

    await completedListeningCaller();
    await yieldFixtureCaller(); await delay(400);
    sendJson(provider, { type: 'speech-update', role: 'assistant', status: 'started', turn: 2 });
    await streamMain(provider, .6);
    sendJson(provider, { type: 'speech-update', role: 'assistant', status: 'stopped', turn: 2 });
    await delay(250);
    const fast = await page.evaluate(() => ({ events: window.duplexSession.events, graph: window.duplexGraphEvidence }));
    assert.equal(fast.events.filter(row => row.kind === 'cue_started' && row.phase === 'waiting').length, 0, 'fast_answer_played_waiting_cue');
    const fastWaitingRequests = selections(6).filter(row => row.phase === 'waiting');
    assert.equal(fastWaitingRequests.length, 1, 'fast_answer_selection_was_not_bounded');
    assert.ok(fastWaitingRequests[0].responded_at_ms !== undefined, 'fast_answer_did_not_exercise_held_result');
    assert.equal(fast.graph.starts.filter(row => row.samples === 16800).length, 0, 'fast_answer_waiting_clip_entered_graph');
    report.checks.main_after_400ms_skipped_waiting_cue = true;
    report.checks.fast_main_revoked_preselected_waiting_result = true;
    report.fast_answer_events = fast.events;

    await completedListeningCaller();
    await yieldFixtureCaller();
    await until(() => selections(7).some(row => row.phase === 'waiting'), 'pending_waiting_request_missing');
    await delay(80);
    sendJson(provider, { type: 'speech-update', role: 'assistant', status: 'started', turn: 2 });
    await streamMain(provider, .8);
    sendJson(provider, { type: 'speech-update', role: 'assistant', status: 'stopped', turn: 2 });
    await delay(150);
    const late = await page.evaluate(() => ({ events: window.duplexSession.events, graph: window.duplexGraphEvidence }));
    assert.equal(late.events.filter(row => row.kind === 'cue_started' && row.phase === 'waiting').length, 0, 'late_waiting_decision_played_over_main');
    assert.equal(late.graph.starts.filter(row => row.samples === 16800).length, 0, 'late_waiting_decision_entered_graph');
    report.checks.main_audio_revoked_pending_waiting_decision = true;
    report.pending_waiting_events = late.events;

    report.waiting_complete_handoff_cases = [];
    for (const mainDelayMs of [800, 1000]) {
      await completedListeningCaller();
      const release = await yieldFixtureCaller();
      await until(() => page.evaluate(() => window.duplexSession.events.some(row => row.kind === 'cue_started'
        && row.phase === 'waiting')), 'handoff_waiting_cue_missing');
      await until(() => page.evaluate(at => performance.now() >= at,
        release.origin_ms + release.released_at_ms + mainDelayMs), 'handoff_deadline_not_reached', 2000);
      sendJson(provider, { type: 'speech-update', role: 'assistant', status: 'started', turn: 2 });
      await streamMain(provider, .6);
      sendJson(provider, { type: 'speech-update', role: 'assistant', status: 'stopped', turn: 2 });
      await until(() => page.evaluate(() => window.duplexSession.playback.main.size === 0), 'complete_handoff_main_not_drained');
      const result = await page.evaluate(() => ({ events: window.duplexSession.events, graph: window.duplexGraphEvidence }));
      const cue = result.graph.starts.find(row => row.samples === 16800), main = result.graph.starts.find(row => row.samples === 320);
      const cueEnd = cue.scheduled_at_s + cue.seconds;
      assert.ok(cue.ended_ms && !cue.stopped, 'main_truncated_waiting_clip');
      assert.equal(cueFade(result.graph, cue), null, 'main_faded_complete_cue');
      assert.ok(Math.abs(cueEnd - main.scheduled_at_s) < .000001, 'whole_cue_handoff_gap_or_overlap');
      const hold = result.events.find(row => row.kind === 'main_handoff');
      assert.ok(hold && hold.hold_ms > 0 && hold.hold_ms <= 1100.001, 'main_handoff_bound_missing');
      const transition = await verifyRecordedCompleteTransition(cue);
      assert.ok(transition.mse < .00001, 'complete_cue_transition_not_present_in_post_gain_recording');
      report.waiting_complete_handoff_cases.push({ main_ready_after_release_ms: mainDelayMs,
        waiting_clip_complete: true, waiting_clip_intentionally_stopped: false, waiting_clip_scheduled_ms: cue.seconds * 1000,
        waiting_clip_played_ms: cue.seconds * 1000, assistant_audio_overlap_ms: Math.max(0, cueEnd - main.scheduled_at_s) * 1000,
        added_main_delay_ms: hold.hold_ms, recorded_transition: transition, events: result.events, graph: result.graph });
    }
    report.checks.ready_main_preserved_whole_waiting_clip_without_overlap = true;
    report.checks.complete_cue_main_transition_verified_in_post_gain_recording = true;

    await completedListeningCaller();
    const heldRelease = await yieldFixtureCaller();
    await until(() => selections(10).some(row => row.phase === 'waiting'
      && row.responded_at_ms !== undefined), 'early_waiting_result_not_returned');
    const beforeGuard = await page.evaluate(() => ({ events: window.duplexSession.events, graph: window.duplexGraphEvidence }));
    assert.equal(beforeGuard.events.filter(row => row.kind === 'cue_started' && row.phase === 'waiting').length, 0, 'early_result_bypassed_waiting_guard');
    assert.equal(beforeGuard.graph.starts.filter(row => row.samples === 16800).length, 0, 'early_waiting_result_entered_graph');
    await until(() => page.evaluate(() => window.duplexSession.events.some(row => row.kind === 'cue_started'
      && row.phase === 'waiting')), 'held_waiting_result_never_played');
    const heldEvent = await page.evaluate(() => window.duplexSession.events.find(row => row.kind === 'cue_started' && row.phase === 'waiting'));
    const heldDelay = heldEvent.at_ms - heldRelease.released_at_ms;
    assert.ok(heldDelay >= 549 && heldDelay < 750, 'selection_and_guard_did_not_overlap');
    report.checks.early_150ms_selection_held_until_550ms_playback_guard = true;
    report.early_waiting_result = { classification_delay_ms: 150, observed_playback_after_turn_release_ms: heldDelay,
      classification_request_count: selections(10).filter(row => row.phase === 'waiting').length };

    await completedListeningCaller();
    await yieldFixtureCaller();
    await delay(900);
    const expired = await page.evaluate(() => ({ events: window.duplexSession.events, graph: window.duplexGraphEvidence }));
    assert.equal(selections(11).filter(row => row.phase === 'waiting').length, 1, 'expired_selection_retried_without_new_context');
    assert.equal(expired.events.filter(row => row.kind === 'cue_started' && row.phase === 'waiting').length, 0, 'held_result_outlived_its_300ms_ttl');
    assert.equal(expired.graph.starts.filter(row => row.samples === 16800).length, 0, 'expired_held_result_entered_graph');
    report.checks.playback_guard_did_not_extend_held_result_ttl = true;

    await newFixtureSession({ snapshot: { stage: 'details', proposal: { fixture_only: true } },
      previousAssistant: 'What name should I put on the service visit?', shortReplies: ['Alex'] });
    await until(() => page.evaluate(() => window.duplexSession.coordinator.listeningBlocked), 'details_fixture_context_not_loaded');
    await setMic(.25); sendJson(provider, { type: 'speech-update', role: 'user', status: 'started', turn: 1 });
    sendJson(provider, { type: 'transcript', role: 'user', transcriptType: 'partial', transcript: 'My name for the service visit is Alex.', turn: 1 });
    await delay(250);
    assert.equal(selections(12).length, 0, 'details_proposal_permitted_listening_cue');
    await yieldFixtureCaller('Alex');
    await until(() => page.evaluate(() => window.duplexSession.events.some(row => row.kind === 'cue_started'
      && row.phase === 'waiting')), 'short_name_after_trusted_question_had_no_waiting_cue');
    assert.deepEqual(selections(12).map(row => row.phase), ['waiting']);
    await until(() => page.evaluate(() => window.duplexGraphEvidence.samples.some(row => row.cue && row.rms > .01)), 'short_name_waiting_cue_not_audible');
    report.checks.short_name_in_details_proposal_received_waiting_only = true;

    await newFixtureSession({ previousAssistant: 'How can I help with your service visit?', shortReplies: ['Hours?'] });
    await setMic(.25); sendJson(provider, { type: 'speech-update', role: 'user', status: 'started', turn: 1 });
    await delay(100); await yieldFixtureCaller('Hours?');
    await until(() => page.evaluate(() => window.duplexSession.events.some(row => row.kind === 'cue_started'
      && row.phase === 'waiting')), 'short_faq_had_no_waiting_cue');
    report.checks.short_faq_received_waiting_cue_with_trusted_context = true;

    await newFixtureSession({ previousAssistant: 'Would you like me to explain the available appointments?' });
    await setMic(.25); sendJson(provider, { type: 'speech-update', role: 'user', status: 'started', turn: 1 });
    await delay(100); await yieldFixtureCaller('Yes'); await delay(900);
    assert.equal(cueRequests.filter(row => row.session === 14).length, 0, 'yes_issued_speculative_selection');
    assert.equal(await eventCount('cue_started'), 0, 'yes_played_speculative_audio');
    assert.equal(await page.evaluate(() => window.duplexGraphEvidence.starts.length), 0, 'yes_entered_output_graph');
    report.checks.yes_remained_silent_without_classification_or_audio = true;

    await newFixtureSession({ previousAssistant: 'How can I help with your service visit?', shortReplies: ['Hours?'] });
    await setMic(.25); sendJson(provider, { type: 'speech-update', role: 'user', status: 'started', turn: 1 });
    await delay(100); await yieldFixtureCaller('Hours?'); await delay(400);
    sendJson(provider, { type: 'speech-update', role: 'assistant', status: 'started', turn: 2 });
    await streamMain(provider, .6);
    sendJson(provider, { type: 'speech-update', role: 'assistant', status: 'stopped', turn: 2 }); await delay(200);
    assert.equal(selections(15).filter(row => row.phase === 'waiting').length, 1, 'short_fast_answer_selection_not_bounded');
    assert.equal(await eventCount('cue_started'), 0, 'short_fast_answer_played_speculative_cue');
    assert.equal(await page.evaluate(() => window.duplexGraphEvidence.starts.filter(row => [10720, 16800].includes(row.samples)).length), 0,
      'short_fast_answer_cue_entered_graph');
    report.checks.short_faq_with_400ms_main_produced_no_audible_cue = true;

    await newFixtureSession({ previousAssistant: 'How can I help with your service visit?', shortReplies: ['Hours?'] });
    sendJson(provider, { type: 'speech-update', role: 'assistant', status: 'started', turn: 1 });
    sendJson(provider, { type: 'speech-update', role: 'assistant', status: 'stopped', turn: 1 });
    await delay(70);
    assert.equal((await greetingState()).ready, false, 'early_provider_stop_was_mistaken_for_a_greeting');
    sendJson(provider, { type: 'speech-update', role: 'assistant', status: 'started', turn: 1 });
    const greeting = streamMain(provider, .45);
    await until(() => page.evaluate(() => window.duplexGraphEvidence.samples.some(row => !row.cue)), 'race_greeting_was_not_audible');
    assert.equal((await greetingState()).ready, false, 'playing_greeting_was_not_drained');
    await greeting; sendJson(provider, { type: 'speech-update', role: 'assistant', status: 'stopped', turn: 1 });
    await until(async () => (await greetingState()).ready, 'real_greeting_did_not_become_ready');
    const shortInput = await page.evaluate(() => ({ started_at_ms: performance.now() - window.duplexSession.startAt,
      input_end_ms: performance.now() - window.duplexSession.startAt + 220 }));
    await setMic(.25); sendJson(provider, { type: 'speech-update', role: 'user', status: 'started', turn: 1 });
    await delay(220);
    // A stale greeting stop would satisfy the old stop-counter predicate, but
    // there has been no caller release or fresh main response for this input.
    sendJson(provider, { type: 'speech-update', role: 'assistant', status: 'stopped', turn: 1 });
    await delay(20);
    assert.equal((await replyState(shortInput)).ready, false, 'stale_greeting_stop_completed_short_reply');
    await yieldFixtureCaller('Hours?'); await delay(400);
    sendJson(provider, { type: 'speech-update', role: 'assistant', status: 'started', turn: 2 });
    const freshReply = streamMain(provider, .45);
    await until(async () => (await replyState(shortInput, false)).ready, 'fresh_short_reply_start_not_detected');
    assert.equal((await replyState(shortInput)).ready, false, 'unfinished_fresh_reply_was_counted_complete');
    await freshReply; sendJson(provider, { type: 'speech-update', role: 'assistant', status: 'stopped', turn: 2 });
    await until(async () => (await replyState(shortInput)).ready, 'fresh_short_reply_completion_not_detected');
    report.checks.short_turn_early_greeting_stop_and_stale_reply_race_rejected = true;

    // A response's remaining TTL is not its original absolute age allowance.
    // Charging the300ms selection twice would expire this result before guard.
    await newFixtureSession({ decisionDelayMs: 300, maxRequestAgeMs: 800,
      predictionEnabled: false, predictionControlReason: 'isolate_original_delayed_selection_ttl_regression' });
    const ttlSession = sessions;
    sendJson(provider, { type: 'speech-update', role: 'user', status: 'started', turn: 1 });
    const ttlRelease = await yieldFixtureCaller();
    await until(() => page.evaluate(() => window.duplexSession.events.some(row => row.kind === 'cue_started'
      && row.phase === 'waiting')), 'remaining_ttl_was_charged_twice');
    const ttlCue = await page.evaluate(() => window.duplexSession.events.find(row => row.kind === 'cue_started'));
    const ttlRequest = cueRequests.find(row => row.session === ttlSession && row.phase === 'waiting');
    assert.ok(ttlRequest.expires_in_ms <= 500 && ttlRequest.expires_in_ms > 350, 'remaining_ttl_fixture_invalid');
    assert.ok(ttlCue.at_ms - ttlRelease.released_at_ms >= 540 && ttlCue.at_ms - ttlRelease.released_at_ms < 750,
      'remaining_ttl_did_not_preserve_original_guard');
    assert.ok(ttlCue.decision_age_ms > ttlRequest.expires_in_ms && ttlCue.decision_age_ms < 800,
      'double_count_regression_was_not_exercised');
    report.remaining_ttl_case = { ...ttlRequest, playback_after_release_ms: ttlCue.at_ms - ttlRelease.released_at_ms,
      playback_request_age_ms: ttlCue.decision_age_ms };
    report.checks.remaining_ttl_and_absolute_age_were_not_double_counted = true;

    await newFixtureSession({ decisionDelayMs: 150 });
    sendJson(provider, { type: 'speech-update', role: 'user', status: 'started', turn: 1 });
    const announcementRelease = await yieldFixtureCaller();
    sendJson(provider, { type: 'speech-update', role: 'assistant', status: 'started', turn: 2 });
    for (let index = 0; index < 6; index++) { provider.send(Buffer.alloc(640)); await delay(100); }
    await until(() => page.evaluate(() => window.duplexSession.events.some(row => row.kind === 'cue_started'
      && row.phase === 'waiting')), 'early_announcement_erased_pending_waiting_cue');
    const announcementEvidence = await page.evaluate(() => ({ events: window.duplexSession.events,
      graph: window.duplexGraphEvidence }));
    assert.equal(announcementEvidence.events.filter(row => row.kind === 'main_started').length, 0,
      'quiet_pcm_was_mistaken_for_main');
    const announcedCue = announcementEvidence.events.find(row => row.kind === 'cue_started');
    assert.ok(announcedCue.at_ms - announcementRelease.released_at_ms >= 540);
    const announcementMain = streamMain(provider, .4);
    await until(() => page.evaluate(() => window.duplexSession.events.some(row => row.kind === 'main_handoff'
      && row.hold_ms > 0 && row.hold_ms <= 1100.001)), 'actual_main_handoff_missing_after_early_announcement');
    await announcementMain;
    report.checks.early_announcement_and_silent_tts_gap_preserved_waiting_then_actual_main_won = true;

    await newFixtureSession({ decisionDelayMs: 150 });
    sendJson(provider, { type: 'speech-update', role: 'user', status: 'started', turn: 1 });
    await yieldFixtureCaller();
    await setMic(.25); await delay(100); await setMic(0);
    await until(() => page.evaluate(() => window.duplexSession.events.some(row => row.kind === 'cue_started'
      && row.phase === 'waiting')), 'idle_noise_canceled_held_waiting_selection');
    const idleNoise = await page.evaluate(() => ({ events: window.duplexSession.events, metrics: window.duplexSession.metrics() }));
    assert.equal(idleNoise.events.filter(row => row.kind === 'provisional_barge_in').length, 0);
    assert.ok(idleNoise.metrics.local_energy_onsets_ignored_without_output >= 1, 'idle_noise_was_not_exercised');
    report.checks.idle_energy_did_not_revoke_guard_held_waiting_result = true;

    // Noise while the short clip is sounding cannot truncate its last word.
    await setMic(.25); await delay(80); await setMic(0);
    await until(() => page.evaluate(() => window.duplexGraphEvidence.starts.some(row => row.samples === 16800
      && row.ended_ms && !row.stopped)), 'unconfirmed_noise_truncated_active_cached_clip');
    const cueNoise = await page.evaluate(() => ({ events: window.duplexSession.events, graph: window.duplexGraphEvidence }));
    assert.equal(cueNoise.events.filter(row => row.kind === 'cue_started').length, 1);
    assert.equal(cueNoise.events.filter(row => row.kind === 'cue_stopped').length, 0);
    report.checks.unconfirmed_energy_preserved_complete_active_waiting_clip = true;

    await newFixtureSession();
    sendJson(provider, { type: 'speech-update', role: 'user', status: 'started', turn: 1 });
    await yieldFixtureCaller();
    await until(async () => await eventCount('cue_started') === 1, 'cue_noise_priority_preflight_missing');
    await setMic(.25); await delay(80);
    assert.equal(await eventCount('provisional_barge_in'), 0, 'cue_only_noise_created_main_pause');
    sendJson(provider, { type: 'speech-update', role: 'assistant', status: 'started', turn: 2 });
    const cueNoiseMainAt = performance.now(); const cueNoiseMain = streamMain(provider, .3);
    await until(async () => await eventCount('main_started') === 1, 'cue_only_noise_delayed_first_main_pcm');
    assert.ok(performance.now() - cueNoiseMainAt < 150, 'cue_only_noise_buffered_main_for_confirmation_grace');
    const cueNoisePriority = await page.evaluate(() => ({ graph: window.duplexGraphEvidence,
      ignored: window.duplexSession.metrics().local_energy_onsets_ignored_during_cached_cue }));
    const noiseCut = cueNoisePriority.graph.starts.find(row => row.samples === 16800);
    const noiseMain = cueNoisePriority.graph.starts.find(row => row.samples === 320);
    assert.ok(!noiseCut.stopped && noiseMain, 'cue_only_noise_main_handoff_missing');
    assert.ok(Math.abs(noiseCut.scheduled_at_s + noiseCut.seconds - noiseMain.scheduled_at_s) < .00001, 'cue_only_noise_main_overlap');
    assert.ok(cueNoisePriority.ignored >= 1, 'cached_cue_noise_ignore_not_exercised');
    await cueNoiseMain; await setMic(0);
    report.checks.first_main_pcm_during_cue_only_noise_kept_complete_cue_handoff = true;

    await newFixtureSession();
    sendJson(provider, { type: 'speech-update', role: 'assistant', status: 'started', turn: 1 });
    const noisyMain = streamMain(provider, 4.5);
    await until(() => page.evaluate(() => window.duplexGraphEvidence.samples.some(row => !row.cue)), 'noise_main_not_audible');
    const noisyGeneration = await page.evaluate(() => window.duplexSession.generation);
    for (let index = 0; index < 6; index++) { await setMic(.25); await delay(80); await setMic(0); await delay(300); }
    const noiseBeforeConfirm = await page.evaluate(() => ({ events: window.duplexSession.events,
      metrics: window.duplexSession.metrics(), generation: window.duplexSession.generation,
      current_ms: performance.now(), cooldown_until_ms: window.duplexSession.energyCooldownUntil,
      samples: window.duplexGraphEvidence.samples.slice(-12) }));
    assert.equal(noiseBeforeConfirm.generation, noisyGeneration, 'false_noise_replaced_main_generation');
    const pauses = noiseBeforeConfirm.events.filter(row => row.kind === 'provisional_barge_in').length;
    assert.ok(pauses >= 1 && pauses <= 2, 'false_noise_pause_budget_not_bounded');
    assert.ok(noiseBeforeConfirm.metrics.local_energy_onsets_ignored_during_cooldown >= 2, 'repeated_noise_backoff_not_exercised');
    assert.ok(noiseBeforeConfirm.samples.some(row => !row.cue && row.rms > .01), 'false_noise_lost_remaining_main_answer');
    assert.ok(noiseBeforeConfirm.current_ms < noiseBeforeConfirm.cooldown_until_ms, 'confirmed_barge_in_cooldown_case_not_exercised');
    sendJson(provider, { type: 'speech-update', role: 'user', status: 'started', turn: 2 });
    await until(() => page.evaluate(() => window.duplexSession.callerSpeaking), 'confirmed_caller_was_blocked_by_noise_backoff');
    await delay(80);
    const actualQuiet = await page.evaluate(() => { const samples = new Float32Array(window.duplexSession.context.analyser.fftSize);
      window.duplexSession.context.analyser.getFloatTimeDomainData(samples);
      return { rms: Math.sqrt(samples.reduce((sum, sample) => sum + sample * sample, 0) / samples.length),
        main_allowed: window.duplexSession.mainAllowed, input_frames: window.duplexSession.inputFrames }; });
    assert.ok(actualQuiet.rms < .001 && !actualQuiet.main_allowed, 'confirmed_barge_in_did_not_quiet_main_graph');
    await noisyMain;
    report.noise_backoff_case = { provisional_pauses: pauses,
      ignored_onsets: noiseBeforeConfirm.metrics.local_energy_onsets_ignored_during_cooldown,
      same_generation_recovered: true, confirmed_interruption_rms: actualQuiet.rms };
    report.checks.repeated_false_energy_recovered_audio_and_confirmed_caller_bypassed_backoff = true;

    report.late_sensitive_guard_cases = [];
    for (const text of ['Actually, do not book anything.', 'Please answer in Spanish.', 'Only if the price is under fifty dollars.']) {
      await newFixtureSession({ decisionDelayMs: 150 }); const guardedSession = sessions;
      sendJson(provider, { type: 'speech-update', role: 'user', status: 'started', turn: 1 });
      await yieldFixtureCaller();
      await until(() => selections(guardedSession).some(row => row.responded_at_ms !== undefined),
        'late_guard_preselection_missing');
      sendJson(provider, { type: 'transcript', role: 'user', transcriptType: 'final', transcript: text, turn: 1 });
      await delay(850);
      const guarded = await page.evaluate(() => ({ events: window.duplexSession.events,
        graph: window.duplexGraphEvidence, utterance: window.duplexSession.coordinator.utteranceId }));
      assert.equal(guarded.events.filter(row => row.kind === 'cue_started').length, 0, 'late_sensitive_words_played_held_cue');
      assert.equal(guarded.graph.starts.length, 0, 'late_sensitive_words_entered_audio_graph');
      assert.equal(guarded.utterance, 1, 'late_final_created_spurious_new_turn');
      assert.equal(selections(guardedSession).length, 1, 'late_guard_retried_without_safe_context');
      report.late_sensitive_guard_cases.push({ case: report.late_sensitive_guard_cases.length + 1,
        held_selection_revoked: true, audio_sources: 0, utterances: guarded.utterance });
    }
    report.checks.late_consent_language_and_conditional_words_revoked_held_results = true;

    await newFixtureSession({ decisionDelayMs: 220 });
    const segmentedSession = sessions;
    const segments = ['My car is used for daily travel', 'and regular highway driving', 'with some longer weekend trips',
      'during the warmer months', 'and routine maintenance visits', 'at the usual service intervals',
      'for ordinary vehicle care', 'before another longer journey'];
    sendJson(provider, { type: 'speech-update', role: 'user', status: 'started', turn: 1 });
    for (let index = 0; index < segments.length; index++) {
      sendJson(provider, { type: 'transcript', role: 'user', transcriptType: 'final', transcript: segments[index], turn: 1 });
      sendJson(provider, { type: 'speech-update', role: 'user', status: 'stopped', turn: 1 });
      await delay(50);
      if (index < segments.length - 1) sendJson(provider, { type: 'speech-update', role: 'user', status: 'started', turn: 1 });
    }
    await yieldFixtureCaller(segments.join(' '));
    await until(() => page.evaluate(() => window.duplexSession.events.some(row => row.kind === 'cue_started'
      && row.phase === 'waiting')), 'segmented_finals_lost_waiting_phase');
    const segmented = await page.evaluate(() => ({ events: window.duplexSession.events,
      utterance: window.duplexSession.coordinator.utteranceId, text: window.duplexSession.partial }));
    assert.equal(segmented.utterance, 1, 'short_endpoint_jitter_split_conversational_turn');
    assert.equal(segmented.text, segments.join(' '), 'segmented_finals_lost_or_repeated_words');
    assert.deepEqual(segmented.events.filter(row => row.kind === 'cue_started').map(row => row.phase), ['listening', 'waiting']);
    assert.equal(selections(segmentedSession).length, 2, 'segmented_finals_unbounded_selection_attempts');
    report.checks.eight_final_segments_and_50ms_endpoint_jitter_preserved_one_turn_and_both_phases = true;

    report.context_recovery_cases = [];
    for (const reason of ['unverified_or_sensitive_context', 'classification_uncertain']) {
      await newFixtureSession({ firstWaitingReason: reason }); const recoverySession = sessions;
      sendJson(provider, { type: 'speech-update', role: 'user', status: 'started', turn: 1 });
      await yieldFixtureCaller();
      await until(() => page.evaluate(wanted => window.duplexSession.events.some(row => row.kind === 'cue_suppressed'
        && row.reason === wanted), reason), 'context_recovery_initial_silence_missing');
      const trusted = fixtureContexts.get(recoverySession);
      trusted.snapshot.pending_operations = [{ id: 'fixture-pending-operation' }];
      await page.evaluate(() => window.duplexSession.refreshSnapshot());
      assert.equal(await page.evaluate(() => window.duplexSession.snapshotHardBlocked), true);
      await delay(100); trusted.snapshot.pending_operations = [];
      await page.evaluate(() => window.duplexSession.refreshSnapshot());
      if (reason === 'unverified_or_sensitive_context') {
        await until(() => page.evaluate(() => window.duplexSession.events.some(row => row.kind === 'cue_started'
          && row.phase === 'waiting')), 'authoritative_context_clear_failed_to_reclassify');
        assert.equal(selections(recoverySession).length, 2);
      } else {
        await delay(850); assert.equal(await eventCount('cue_started'), 0);
        assert.equal(selections(recoverySession).length, 1, 'stable_uncertainty_was_retried_after_context_clear');
      }
      report.context_recovery_cases.push({ prior_reason: reason,
        selection_requests: selections(recoverySession).length,
        cues_played: await eventCount('cue_started') });
    }
    report.checks.only_authoritative_gate_clear_reclassified_context_failure = true;

    // Model authoritative reads separately from classification. Browser abort
    // does not unwind server work that has already crossed its first read.
    report.api_pacing_recovery_cases = [];
    for (const expectedReason of ['classification_throttled', 'cue_spacing']) {
      await newFixtureSession({ decisionDelayMs: { listening: 100, waiting: 35 },
        serverReadDelayMs: { listening: 280, waiting: expectedReason === 'cue_spacing' ? 240 : 0 },
        enforceServerPacing: true, predictionEnabled: false,
        predictionControlReason: 'isolate_original_preclassification_pacing_refusal_' + expectedReason });
      const pacedSession = sessions;
      const text = 'I would like information about routine maintenance options for my car';
      sendJson(provider, { type: 'speech-update', role: 'user', status: 'started', turn: 1 });
      sendJson(provider, { type: 'transcript', role: 'user', transcriptType: 'partial', transcript: text, turn: 1 });
      await until(() => cueRequests.some(row => row.session === pacedSession && row.phase === 'listening'),
        'delayed_routing_listening_request_missing');
      await delay(80); await yieldFixtureCaller(text);
      sendJson(provider, { type: 'speech-update', role: 'assistant', status: 'started', turn: 2 });
      await until(() => page.evaluate(reason => window.duplexSession.events.some(row => row.kind === 'cue_retry_scheduled'
        && row.reason === reason), expectedReason), 'preclassification_pacing_did_not_schedule_fresh_retry');
      await until(() => page.evaluate(() => window.duplexSession.events.some(row => row.kind === 'cue_started'
        && row.phase === 'waiting')), 'fresh_pacing_retry_did_not_play_waiting_cue');
      const pacedRequests = selections(pacedSession);
      const lateListening = pacedRequests.find(row => row.phase === 'listening');
      const waitingRequests = pacedRequests.filter(row => row.phase === 'waiting');
      assert.ok(lateListening.classification_started_at_ms > lateListening.transport_closed_at_ms,
        'canceled_request_did_not_reach_delayed_server_work');
      assert.equal(waitingRequests.length, 2, 'pacing_retry_was_not_one_fresh_request');
      assert.equal(waitingRequests[0].reason, expectedReason);
      assert.equal(waitingRequests[0].server_classified, false, 'pacing_refusal_spent_a_classification');
      assert.ok(waitingRequests[0].retry_after_ms >= 1 && waitingRequests[0].retry_after_ms <= 1000);
      assert.ok(waitingRequests[1].revision > waitingRequests[0].revision, 'pacing_retry_reused_old_revision');
      assert.equal(waitingRequests[1].server_classified, true);
      const paced = await page.evaluate(() => ({ events: window.duplexSession.events, graph: window.duplexGraphEvidence }));
      assert.equal(paced.events.filter(row => row.kind === 'cue_started' && row.phase === 'listening').length, 0,
        'canceled_listening_response_played_after_yield');
      assert.equal(paced.events.filter(row => row.kind === 'cue_retry_scheduled').length, 1);
      const accepted = paced.events.find(row => row.kind === 'cue_started');
      assert.ok(accepted.decision_age_ms < 800, 'fresh_pacing_retry_extended_an_accepted_deadline');
      await until(() => page.evaluate(() => window.duplexGraphEvidence.starts.some(row => row.samples === 16800
        && row.ended_ms && !row.stopped)), 'fresh_retry_waiting_clip_was_incomplete');
      report.api_pacing_recovery_cases.push({ reason: expectedReason, waiting_requests: waitingRequests,
        delayed_listening: lateListening, retry_count: 1, accepted_request_age_ms: accepted.decision_age_ms,
        complete_waiting_clip_ms: 1050 });
    }
    report.checks.delayed_api_reads_caused_one_fresh_throttle_or_spacing_retry_without_classifying_refusals = true;

    await newFixtureSession({ serverReadDelayMs: 180, decisionDelayMs: 200, serverAfterReadDelayMs: 100,
      enforceServerPacing: true, preparationDecision: 'decline' });
    const lateClipSession = sessions;
    const lateClipText = 'Please explain the usual routine maintenance options for my vehicle';
    sendJson(provider, { type: 'speech-update', role: 'user', status: 'started', turn: 1 });
    sendJson(provider, { type: 'transcript', role: 'user', transcriptType: 'partial', transcript: lateClipText, turn: 1 });
    await until(async () => await eventCount('cue_started') === 1, 'late_listening_decision_did_not_play');
    await delay(20);
    sendJson(provider, { type: 'speech-update', role: 'user', status: 'stopped', turn: 1 });
    sendJson(provider, { type: 'transcript', role: 'user', transcriptType: 'final', transcript: lateClipText, turn: 1 });
    sendJson(provider, { type: 'speech-update', role: 'assistant', status: 'started', turn: 2 });
    await until(async () => await eventCount('caller_stopped') > 0, 'late_listening_caller_did_not_yield');
    await delay(300);
    assert.equal(selections(lateClipSession).filter(row => row.phase === 'waiting').length, 0,
      'waiting_selection_spent_ttl_behind_active_listening_clip');
    await until(async () => await eventCount('cue_started') === 2, 'late_listening_clip_starved_waiting_phase');
    const lateClip = await page.evaluate(() => ({ events: window.duplexSession.events, graph: window.duplexGraphEvidence }));
    const latePhases = lateClip.events.filter(row => row.kind === 'cue_started');
    assert.deepEqual(latePhases.map(row => row.phase), ['listening', 'waiting']);
    const lateWaitingRequests = selections(lateClipSession).filter(row => row.phase === 'waiting');
    assert.equal(lateWaitingRequests.length, 1, 'accepted_waiting_decision_was_refreshed');
    assert.ok(latePhases[1].at_ms - latePhases[0].at_ms >= 960, 'late_clip_end_spacing_was_bypassed');
    assert.ok(latePhases[1].decision_age_ms >= 450 && latePhases[1].decision_age_ms < 800,
      'late_clip_decision_absolute_age_was_not_preserved');
    assert.equal(lateClip.events.filter(row => row.kind === 'cue_retry_scheduled').length, 0,
      'accepted_waiting_selection_used_pacing_retry');
    assert.equal(lateClip.events.filter(row => row.reason === 'selection_expired_before_playback').length, 0,
      'late_listening_clip_consumed_waiting_ttl');
    await until(() => page.evaluate(() => window.duplexGraphEvidence.starts.some(row => row.samples === 16800
      && row.ended_ms && !row.stopped)), 'late_listening_waiting_clip_was_incomplete');
    report.late_listening_decision_case = { api_before_read_ms: 180, classifier_ms: 200, api_after_read_ms: 100,
      waiting_requests: 1, accepted_expiry_refreshed: false, waiting_request_age_ms: latePhases[1].decision_age_ms,
      listening_to_waiting_start_ms: latePhases[1].at_ms - latePhases[0].at_ms, complete_waiting_clip_ms: 1050 };
    report.checks.late_listening_clip_deferred_waiting_selection_instead_of_expiring_an_accepted_decision = true;

    // Prediction protocol is exercised separately from the legacy timing
    // controls above: prepared results stay silent and spend no cue allowance.
    report.preparation_cases = [];
    for (const mode of ['completed', 'joined', 'fast_main', 'sensitive_correction']) {
      const name = 'Alex Morgan';
      await newFixtureSession({ snapshot: { stage: 'details', proposal: { fixture_only: true } },
        previousAssistant: 'May I have your name for the service visit?', shortReplies: [name],
        preparationDelayMs: mode === 'completed' ? 100 : mode === 'fast_main' ? 700 : 450 });
      const preparationSession = sessions;
      await until(() => page.evaluate(() => window.duplexSession.coordinator.listeningBlocked), 'preparation_details_gate_not_loaded');
      sendJson(provider, { type: 'speech-update', role: 'user', status: 'started', turn: 1 });
      if (mode === 'completed') {
        sendJson(provider, { type: 'transcript', role: 'user', transcriptType: 'partial', transcript: name, turn: 1 });
        await until(async () => await eventCount('waiting_preparation_ready') === 1, 'completed_prediction_missing');
        assert.equal(await eventCount('cue_started'), 0, 'prediction_played_before_yield');
        assert.equal(await page.evaluate(() => window.duplexGraphEvidence.starts.length), 0, 'prediction_entered_graph_before_yield');
      }
      const sourceEnd = await page.evaluate(() => performance.now() - window.duplexSession.startAt);
      const release = await yieldFixtureCaller(name);
      if (mode === 'fast_main') {
        await until(() => page.evaluate(at => performance.now() - window.duplexSession.startAt >= at,
          release.released_at_ms + 400), 'preparation_fast_main_deadline_missing', 1500);
        sendJson(provider, { type: 'speech-update', role: 'assistant', status: 'started', turn: 2 });
        await streamMain(provider, .35);
        await delay(200);
        const fastPrediction = await page.evaluate(() => ({ events: window.duplexSession.events,
          graph: window.duplexGraphEvidence, metrics: window.duplexSession.metrics() }));
        assert.equal(fastPrediction.graph.starts.filter(row => row.samples === 16800).length, 0, 'joined_prediction_played_after_fast_main');
        assert.equal(fastPrediction.events.filter(row => row.kind === 'cue_started').length, 0, 'joined_prediction_delayed_fast_main');
        assert.ok(fastPrediction.graph.starts.some(row => row.samples === 320), 'fast_main_waited_for_prediction');
        assert.equal(selections(preparationSession).length, 0, 'pending_prediction_activated_after_main');
        report.preparation_cases.push({ mode, predicted_requests: preparations(preparationSession).length,
          actual_selections: 0, waiting_sources: 0, main_did_not_wait: true });
        continue;
      }
      if (mode === 'sensitive_correction') {
        await until(() => page.evaluate(() => window.duplexSession.coordinator.request?.phase === 'waiting'
          && Boolean(window.duplexSession.waitingPreparation)), 'correction_prediction_join_not_exercised');
        sendJson(provider, { type: 'transcript', role: 'user', transcriptType: 'partial',
          transcript: 'Actually, do not book anything.', turn: 1 });
        await delay(1000);
        assert.equal(selections(preparationSession).length, 0, 'sensitive_correction_activated_prediction');
        assert.equal(await page.evaluate(() => window.duplexGraphEvidence.starts.length), 0, 'sensitive_correction_prediction_entered_graph');
        assert.equal(await page.evaluate(() => window.duplexSession.waitingPrepared), null, 'sensitive_correction_kept_prediction_receipt');
        report.preparation_cases.push({ mode, actual_selections: 0, output_sources: 0,
          raw_gate_canceled_pending_join: true });
        continue;
      }
      await until(async () => await eventCount('waiting_preparation_reused') === 1, 'completed_prediction_was_not_reused');
      await until(() => page.evaluate(() => window.duplexSession.events.some(row => row.kind === 'cue_started'
        && row.phase === 'waiting')), 'reused_prediction_did_not_play_waiting');
      const preparedGraph = await page.evaluate(() => ({ events: window.duplexSession.events, graph: window.duplexGraphEvidence,
        origin: window.duplexSession.startAt, metrics: window.duplexSession.metrics() }));
      const prediction = preparations(preparationSession)[0], activation = selections(preparationSession)[0];
      const cue = preparedGraph.events.find(row => row.kind === 'cue_started');
      const source = preparedGraph.graph.starts.find(row => row.samples === 16800);
      assert.equal(preparations(preparationSession).length, 1, 'prediction_model_work_repeated');
      assert.equal(selections(preparationSession).length, 1, 'prediction_activation_repeated');
      assert.equal(prediction.prepared, true); assert.equal(prediction.expires_in_ms, 0);
      assert.equal(prediction.reserved_at_ms, undefined, 'nonplaying_prediction_reserved_cue');
      assert.equal(activation.prepared_revision, prediction.revision, 'activation_did_not_bind_successful_receipt');
      assert.equal(activation.prepared_reused, true); assert.equal(activation.server_classified, false, 'activation_reclassified_cached_prediction');
      assert.ok(cue.at_ms - release.released_at_ms >= 549, 'prediction_bypassed_confirmed_yield_guard');
      assert.ok(source.started_ms - preparedGraph.origin - sourceEnd < 2000, 'prepared_waiting_audio_missed_source_end_budget');
      const joined = preparedGraph.events.find(row => row.kind === 'waiting_preparation_joined');
      if (mode === 'joined') assert.ok(joined && joined.join_ms > 20 && joined.join_ms < 850, 'inflight_prediction_join_was_not_exercised');
      await until(() => page.evaluate(() => window.duplexGraphEvidence.starts.some(row => row.samples === 16800
        && row.ended_ms && !row.stopped)), 'prepared_waiting_clip_incomplete');
      report.preparation_cases.push({ mode, predicted_requests: 1, preparation_model_requests: 1,
        actual_selections: 1, activation_model_requests: 0, prepared_reused: true,
        join_ms: joined?.join_ms ?? 0, waiting_after_release_ms: cue.at_ms - release.released_at_ms,
        waiting_source_after_input_end_ms: source.started_ms - preparedGraph.origin - sourceEnd,
        complete_waiting_clip_ms: 1050, output_before_yield: false, prediction_reserved_cue: false });
    }
    report.checks.completed_and_inflight_preparation_reused_only_after_fresh_yield_without_extra_model_work = true;
    report.checks.prediction_join_never_delayed_main_or_bypassed_late_sensitive_words = true;

    // New v4 contracts are additional scenarios. The retained old receipts and
    // original turn cases above keep their own historical grades unchanged.
    await newFixtureSession({ waitingPhrase: 'moment', predictionEnabled: false,
      predictionControlReason: 'exercise_exact_same_frame_ready_audio_handoff' });
    const instantSession = sessions;
    await page.evaluate(encoded => {
      const session = window.duplexSession, prior = session.callbacks.event;
      session.callbacks.event = event => {
        prior?.(event);
        if (event.kind === 'cue_started' && event.phase === 'waiting') {
          session.receive(JSON.stringify({ type: 'speech-update', role: 'assistant', status: 'started' }));
          session.receive(Uint8Array.from(atob(encoded), x => x.charCodeAt(0)).buffer);
        }
      };
    }, pcmTone(730, .02).toString('base64'));
    sendJson(provider, { type: 'speech-update', role: 'user', status: 'started', turn: 1 });
    await yieldFixtureCaller();
    await until(() => page.evaluate(() => window.duplexSession.events.some(row => row.kind === 'main_handoff')), 'same_frame_handoff_missing');
    await until(() => page.evaluate(() => window.duplexSession.playback.main.size === 0), 'same_frame_main_not_drained');
    const instant = await page.evaluate(() => ({ events: window.duplexSession.events, graph: window.duplexGraphEvidence }));
    const instantCue = instant.graph.starts.find(row => row.samples === 16800), instantMain = instant.graph.starts.find(row => row.samples === 320);
    const instantHold = instant.events.find(row => row.kind === 'main_handoff');
    assert.ok(instantCue.ended_ms && !instantCue.stopped, 'same_frame_main_cut_cue');
    assert.ok(instantHold.hold_ms >= 1000 && instantHold.hold_ms <= 1100.001, 'worst_case_handoff_bound_unexercised');
    assert.ok(Math.abs(instantMain.scheduled_at_s - instantCue.scheduled_at_s - instantCue.seconds) < .000001, 'same_frame_handoff_overlap');
    assert.equal(selections(instantSession).length, 1, 'same_frame_main_created_follow_up');
    report.same_frame_handoff = { added_main_delay_ms: instantHold.hold_ms, events: instant.events, graph: instant.graph };
    report.checks.same_frame_ready_main_preserved_full_cue_within1100ms = true;

    await newFixtureSession({ waitingPhrase: 'let_me_see', decisionDelayMs: 120 });
    const correctionSession = sessions;
    sendJson(provider, { type: 'speech-update', role: 'user', status: 'started', turn: 1 });
    await yieldFixtureCaller('Actually I meant a tire rotation instead of a brake inspection.');
    await until(async () => await eventCount('cue_started') === 1, 'fresh_correction_waiting_missing');
    const correctionCue = await page.evaluate(() => window.duplexSession.events.find(row => row.kind === 'cue_started'));
    assert.equal(correctionCue.phase, 'waiting'); assert.equal(correctionCue.phrase_id, 'let_me_see');
    assert.equal(selections(correctionSession).some(row => row.phase === 'listening'), false, 'correction_affirmative_listening_not_suppressed');
    sendJson(provider, { type: 'speech-update', role: 'assistant', status: 'started', turn: 2 });
    await streamMain(provider, .2); await until(() => page.evaluate(() => window.duplexSession.playback.main.size === 0), 'correction_main_not_drained');
    const corrected = await page.evaluate(() => window.duplexGraphEvidence.starts.find(row => row.samples === 12800));
    assert.ok(corrected.ended_ms && !corrected.stopped, 'correction_hold_was_truncated');
    report.checks.fresh_correction_received_complete_neutral_waiting_only = true;

    await newFixtureSession({ decisionDelayMs: 90, waitingPhrase: 'one_sec' });
    const shortSession = sessions, shortTurns = [];
    for (let turn = 1; turn <= 3; turn++) {
      sendJson(provider, { type: 'speech-update', role: 'user', status: 'started', turn });
      const release = await yieldFixtureCaller('What is the usual service availability for this week?', { turn });
      await until(async () => await eventCount('cue_started') === turn, 'short_interturn_waiting_missing');
      const cue = await page.evaluate(() => window.duplexSession.events.findLast(row => row.kind === 'cue_started'));
      sendJson(provider, { type: 'speech-update', role: 'assistant', status: 'started', turn: turn + 100 });
      await streamMain(provider, .2); await until(() => page.evaluate(() => window.duplexSession.playback.main.size === 0), 'short_turn_main_not_drained');
      sendJson(provider, { type: 'speech-update', role: 'assistant', status: 'stopped', turn: turn + 100 });
      shortTurns.push({ turn, cue_at_ms: cue.at_ms, after_release_ms: cue.at_ms - release.released_at_ms });
      await delay(120);
    }
    assert.equal(selections(shortSession).length, 3); assert.ok(shortTurns.at(-1).cue_at_ms - shortTurns[0].cue_at_ms < 10000, 'short_interturn_test_reused_old15s_spacing');
    assert.equal(await eventCount('cue_started'), 3); report.short_interturn_waiting = shortTurns;
    report.checks.short_turns_recover_waiting_without15s_spacing = true;

    await newFixtureSession({ decisionDelayMs: 120, waitingPhrase: 'one_sec', followUpPhrase: 'let_me_see' });
    const tailSession = sessions;
    sendJson(provider, { type: 'speech-update', role: 'user', status: 'started', turn: 1 }); await yieldFixtureCaller();
    await until(async () => await eventCount('cue_started') === 2, 'long_tail_second_fresh_cue_missing');
    const tail = await page.evaluate(() => ({ events: window.duplexSession.events, graph: window.duplexGraphEvidence }));
    const tailCues = tail.events.filter(row => row.kind === 'cue_started');
    const firstTail = tail.graph.starts.find(row => row.samples === 8000), secondTail = tail.graph.starts.find(row => row.samples === 12800);
    assert.deepEqual(tailCues.map(row => row.phrase_id), ['one_sec', 'let_me_see']);
    assert.ok(firstTail.ended_ms && !firstTail.stopped, 'followup_started_before_whole_first_cue');
    const tailGap = (secondTail.scheduled_at_s - firstTail.scheduled_at_s - firstTail.seconds) * 1000;
    assert.ok(tailGap >= 1240 && tailGap < 1600, 'fresh_followup_missed1250ms_target_in_bounded_fixture');
    const followupRequest = selections(tailSession)[1]; assert.equal(followupRequest.follow_up, true);
    assert.equal(followupRequest.prepared_revision, null); assert.equal(followupRequest.server_classified, true);
    sendJson(provider, { type: 'speech-update', role: 'assistant', status: 'started', turn: 2 });
    await streamMain(provider, .3); await until(() => page.evaluate(() => window.duplexSession.playback.main.size === 0), 'tail_main_not_drained');
    await delay(1800); assert.equal(await eventCount('cue_started'), 2); assert.equal(selections(tailSession).length, 2);
    report.long_tail_followup = { post_first_cue_gap_ms: tailGap, first_phrase: 'one_sec', second_phrase: 'let_me_see',
      fresh_classification: true, reused_preparation: false, events: tail.events, graph: tail.graph };
    report.checks.long_tail_used_one_fresh_different_followup_without_third_cue = true;

    for (const outcome of ['main_ready', 'deadline']) {
      await newFixtureSession({ decisionDelayMs: 90, followUpDelayMs: 900, waitingPhrase: 'one_sec' });
      const failedTailSession = sessions;
      sendJson(provider, { type: 'speech-update', role: 'user', status: 'started', turn: 1 }); await yieldFixtureCaller();
      await until(() => selections(failedTailSession).some(row => row.follow_up), 'tail_cancellation_request_missing');
      if (outcome === 'main_ready') {
        sendJson(provider, { type: 'speech-update', role: 'assistant', status: 'started', turn: 2 }); await streamMain(provider, .2);
      }
      await delay(1100);
      assert.equal(await eventCount('cue_started'), 1, 'late_followup_played_or_hid_failed_tail');
      const suppressed = await page.evaluate(() => window.duplexSession.events.filter(row => ['cue_suppressed', 'cue_canceled'].includes(row.kind)));
      assert.ok(suppressed.some(row => row.reason === (outcome === 'main_ready' ? 'main_audio_ready' : 'classifier_deadline')), 'tail_failure_diagnostic_missing');
      report.checks[outcome === 'main_ready' ? 'ready_main_canceled_inflight_followup' : 'exhausted_followup_deadline_remained_visible_silence'] = true;
    }

    await newFixtureSession({ waitingPhrase: 'moment', decisionDelayMs: 100 });
    const resumedSession = sessions;
    sendJson(provider, { type: 'speech-update', role: 'user', status: 'started', turn: 1 });
    await yieldFixtureCaller('The vehicle is a blue sedan that needs routine service.');
    await until(() => page.evaluate(() => window.duplexSession.coordinator.hasWaitingAudioStarted()), 'waiting_voiced_start_missing');
    sendJson(provider, { type: 'speech-update', role: 'assistant', status: 'started', turn: 2 });
    provider.send(pcmTone(730, .1));
    await until(async () => await eventCount('main_handoff') === 1, 'resume_queued_main_handoff_missing');
    const oldGeneration = await page.evaluate(() => window.duplexSession.generation);
    sendJson(provider, { type: 'speech-update', role: 'user', status: 'started', turn: 2 });
    sendJson(provider, { type: 'transcript', role: 'user', transcriptType: 'final', transcript: 'Actually the sedan is red instead.', turn: 2 });
    await until(() => page.evaluate(() => window.duplexSession.coordinator.utteranceId === 2), 'voiced_waiting_resume_did_not_get_new_epoch');
    const resumeState = await page.evaluate(old => ({ preserved: window.duplexSession.partial.includes('blue sedan')
      && window.duplexSession.partial.includes('red instead'),
      oldRejected: !window.duplexSession.coordinator.receiveMainPcm(old, new Uint8Array(640), 16000) }), oldGeneration);
    assert.ok(resumeState.preserved && resumeState.oldRejected, 'resume_lost_context_or_revived_old_generation');
    fixtureContexts.get(resumedSession).waitingPhrase = 'let_me_see';
    await yieldFixtureCaller('Actually the sedan is red instead.', { turn: 2 });
    await until(async () => await eventCount('cue_started') === 2, 'resumed_corrected_waiting_missing');
    const resumedWaiting = selections(resumedSession).filter(row => row.phase === 'waiting');
    assert.deepEqual(resumedWaiting.map(row => row.epoch), [1, 2]);
    assert.ok(resumedWaiting.every(row => !row.follow_up), 'new_caller_turn_misclassified_as_followup');
    const resumedGraph = await page.evaluate(() => window.duplexGraphEvidence);
    const originalCue = resumedGraph.starts.find(row => row.samples === 16800), oldQueuedMain = resumedGraph.starts.find(row => row.samples === 1600);
    assert.ok(originalCue.stopped && oldQueuedMain.stopped, 'caller_resume_failed_to_clear_queued_old_audio');
    assert.ok(oldQueuedMain.stopped_context_s < oldQueuedMain.scheduled_at_s, 'old_queued_main_reached_output');
    report.checks.voiced_waiting_resume_preserved_facts_and_created_fresh_correction_epoch = true;

    // Retain the original15s-window regression and real browser clocks. Six full
    // waiting clips in one call exceed both the old four-cue lifetime cap and
    // one rolling window without resetting the session, its counts or timers.
    await newFixtureSession({ decisionDelayMs: 90 }); const longSession = sessions;
    report.long_conversation_turns = [];
    for (let turn = 1; turn <= 6; turn++) {
      if (turn > 1) {
        const quietWait = await page.evaluate(() => { const last = window.duplexSession.events.findLast(row => row.kind === 'cue_started'
          && row.phase === 'waiting'); return Math.max(0, window.duplexSession.startAt + last.at_ms + 15100 - performance.now()); });
        await delay(quietWait);
      }
      const frames = uplink.length;
      await setMic(.25); sendJson(provider, { type: 'speech-update', role: 'user', status: 'started', turn });
      await delay(70);
      const release = await yieldFixtureCaller(`What routine maintenance options are available for my vehicle during visit number ${turn}?`, { turn });
      sendJson(provider, { type: 'speech-update', role: 'assistant', status: 'started', turn: turn + 100 });
      await until(async () => await eventCount('cue_started') === turn, 'long_call_lost_waiting_cue_after_lifetime_cap');
      const current = await page.evaluate(() => ({ cue: window.duplexSession.events.findLast(row => row.kind === 'cue_started'),
        origin: window.duplexSession.startAt, utterance: window.duplexSession.coordinator.utteranceId }));
      await until(() => page.evaluate(start => window.duplexGraphEvidence.starts.some(row => row.samples === 16800
        && row.started_ms >= start - 10 && row.ended_ms && !row.stopped), current.origin + current.cue.at_ms),
      'long_call_waiting_clip_was_incomplete');
      assert.equal(current.cue.phase, 'waiting'); assert.equal(current.utterance, turn);
      assert.ok(uplink.length - frames >= 20, 'long_call_microphone_paused_during_waiting_clip');
      await streamMain(provider, .25);
      sendJson(provider, { type: 'speech-update', role: 'assistant', status: 'stopped', turn: turn + 100 });
      await until(() => page.evaluate(() => window.duplexSession.playback.main.size === 0), 'long_call_main_did_not_drain');
      report.long_conversation_turns.push({ turn, utterance: current.utterance, cue_started_at_ms: current.cue.at_ms,
        cue_after_release_ms: current.cue.at_ms - release.released_at_ms, complete_waiting_clip_ms: 1050,
        continuous_input_frames: uplink.length - frames });
    }
    const longDuration = report.long_conversation_turns.at(-1).cue_started_at_ms - report.long_conversation_turns[0].cue_started_at_ms;
    assert.ok(longDuration >= 75000, 'long_call_production_cooldown_duration_not_exercised');
    assert.equal(selections(longSession).length, 6, 'long_call_requests_were_not_one_per_turn');
    assert.equal(await eventCount('cue_started'), 6); assert.equal(await eventCount('main_started'), 6);
    report.long_conversation = { session_count: 1, turns: 6, waiting_cues: 6, main_answers: 6,
      first_to_last_cue_ms: longDuration, phase_cooldown_ms: 1000, deliberately_spaced_15s_window_regression: true, clocks_or_budgets_modified: false };
    report.checks.six_real_clock_turns_survived_15s_windows_and_four_cue_lifetime_regression = true;
    report.checks.exact_20ms_input_frames = uplink.every(frame => frame.bytes === 640);
    assert.equal(report.checks.exact_20ms_input_frames, true);
    report.checks.browser_errors = errors.length; assert.equal(errors.length, 0);
    report.fixture_sessions = sessions; report.input_frames = uplink.length;
    report.offline_cue_requests = cueRequests;
    report.cue_geometry = phrases; report.http_request_count = requests.length;
    assert.equal(report.external_requests, 0);
    report.passed = true;
  }
} catch (error) {
  report.passed = false;
  const failureKey = String(error?.message || '').split(/\r?\n/, 1)[0];
  const assertionLine = error?.code === 'ERR_ASSERTION'
    ? String(error.stack || '').match(/duplex_browser_smoke\.mjs:(\d+):\d+/)?.[1] : null;
  report.failure = /^[a-z_][a-z_0-9]{0,119}$/.test(failureKey) ? failureKey
    : assertionLine ? 'assertion_failed_line_' + assertionLine : 'browser_probe_failed';
  if (!live) report.offline_cue_requests = cueRequests;
  if (live && cueRequests.length) report.cue_requests = cueRequests.map(({ request, ...entry }) => ({ ...entry,
    elapsed_ms: entry.finished_at_ms === undefined ? null : entry.finished_at_ms - entry.started_at_ms }));
  if (page) { try {
    const evidence = await page.evaluate(() => ({ events: window.duplexSession?.events || [],
      metrics: window.duplexSession?.metrics(), graph: window.duplexGraphEvidence }));
    report.events_at_failure = evidence.events;
    if (live) { report.metrics = evidence.metrics; report.audio_graph = evidence.graph; }
  } catch {} }
  await captureBackendCueEvidence();
  try { await saveLiveRecordings(); } catch { report.recording_failure = true; }
} finally {
  if (page) {
    try { await page.evaluate(async () => { await window.duplexSession?.stop('probe_finished');
      for (const entry of window.duplexFixtureInputs || []) { entry.oscillator.stop(); await entry.input.close(); }
      if (window.duplexLiveInput) { try { window.duplexLiveInput.source.stop(); } catch {}
        try { window.duplexLiveInput.interruption?.source.stop(); } catch {} await window.duplexLiveInput.input.close(); }
    }); } catch { report.cleanup_failure = true; }
  }
  await browser?.close();
  if (server) await new Promise(resolve => server.close(resolve));
  report.finished_at = new Date().toISOString();
  // Never replace a retained live attempt while reporting a preflight refusal.
  if (value('--output') && report.failure !== 'live_output_already_exists') await fs.writeFile(path.resolve(value('--output')), JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify(report, null, 2));
}
process.exitCode = report.passed ? 0 : 1;
