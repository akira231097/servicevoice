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
const report = { ...plan, started_at: new Date().toISOString(), checks: {}, limitations: [
  'The energy gate is a cancellation heuristic, not speech recognition.',
  live ? 'Live verification uses synthetic caller audio; acoustic echo and human conversational quality remain unverified.'
    : 'Provider replay uses deterministic classifier decisions and synthetic cue PCM.',
  'Browser playback nodes and received PCM do not establish human listening quality.',
  'Provider audio has no documented per-frame generation IDs; local epochs are not upstream lineage.' ] };
let browser, context, page, server, provider, base;
const sockets = [], uplink = [], requests = [], errors = [];
let sessions = 0;
const cueRequests = [];
const fixtureContexts = new Map();

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
const cueAudio = { neutral_ack: pcmTone(220, 0.67), moment: pcmTone(250, 1.05) };
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
async function newFixtureSession({ snapshot = null, previousAssistant = '', shortReplies = [] } = {}) {
  fixtureContexts.set(sessions + 1, { previousAssistant, shortReplies,
    snapshot: { mode: 'live', stage: 'service', ended: false, proposal: null, pending_operations: [], transcript: [], events: [], ...snapshot } });
  await page.evaluate(async () => {
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
    await window.duplexSession.start();
  });
  await until(() => Boolean(sockets[sessions - 1]), 'fixture_socket_missing');
  provider = sockets[sessions - 1];
  return provider;
}

async function yieldFixtureCaller(text = 'What are the usual maintenance options for this vehicle?') {
  await setMic(0);
  sendJson(provider, { type: 'transcript', role: 'user', transcriptType: 'final', transcript: text, turn: 1 });
  sendJson(provider, { type: 'speech-update', role: 'user', status: 'stopped', turn: 1 });
  await until(async () => await eventCount('caller_stopped') > 0, 'fixture_caller_did_not_yield');
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
    await fs.writeFile(output, Buffer.concat([header, raw]));
    report[kind + '_recording'] = { path: output,
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
            row = { sample_rate: source.buffer.sampleRate, samples: source.buffer.length, seconds: source.buffer.duration,
              scheduled_at_s: args[0], started_ms: performance.now(), stopped: false, gain_id: outputGainId };
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
    base = address.origin;
    report.cue_decisions = [];
    page.on('request', request => {
      if (!request.url().endsWith('/duplex/cue')) return;
      try {
        const input = request.postDataJSON();
        cueRequests.push({ request, phase: input.phase, epoch: input.epoch, revision: input.revision,
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
          phrase_id: row.phrase_id, pcm_bytes: row.pcm_bytes, sample_rate: row.sample_rate })); } catch {}
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
          'language_choice', 'language_probability', 'language_confidence', 'epoch', 'revision']
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
      window.duplexLiveInput = { input, ...caller, interruption };
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
    // Provider speech-stop can precede the final queued browser samples. Drain
    // the actual playback clock so recordings retain the sentence's ending.
    await delay(400);
    await until(() => page.evaluate(() => {
      const session = window.duplexSession;
      return session.playback.main.size === 0 && session.context.currentTime >= session.playback.mainEnd - 0.01;
    }), 'audio_playback_not_drained', 4000);
    report.checks.final_browser_playback_drained = true;
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
      const moment = report.cue_assets?.find(row => row.phrase_id === 'moment');
      const source = waiting && evidence.graph.starts.find(row => row.samples * 2 === moment?.pcm_bytes
        && Math.abs(row.started_ms - evidence.start_at_ms - waiting.at_ms) < 10);
      const mainSource = main && evidence.graph.starts.find(row => Math.abs(row.started_ms - evidence.start_at_ms - main.at_ms) < 10);
      const cueEnd = source && (source.stopped_context_s ?? source.scheduled_at_s + source.seconds);
      const fade = cueFade(evidence.graph, source);
      report.waiting_cue = { required: true, started_at_ms: waiting?.at_ms ?? null,
        prior_listening_required: !waitingOnly,
        intentionally_preempted: Boolean(source?.stopped), clip_completed: Boolean(source?.ended_ms && !source.stopped),
        scheduled_clip_ms: source ? source.seconds * 1000 : null,
        played_clip_ms: source ? Math.max(0, Math.min(source.seconds, cueEnd - source.scheduled_at_s)) * 1000 : null,
        end_reason: source?.stopped ? 'intentionally_stopped' : source?.ended_ms ? 'natural' : 'unknown',
        fade: fade,
        main_handoff_delay_ms: fade && mainSource ? (mainSource.scheduled_at_s - fade.start_context_s) * 1000 : 0, checks: {
          ...(waitingOnly ? { waiting_after_input: Boolean(waiting) }
            : { same_input_received_listening_and_waiting: Boolean(waiting && report.checks.listening_cue_during_input) }),
          waiting_was_audible_before_main: Boolean(waiting && main && evidence.graph.samples.some(row => row.cue && row.rms > .01
            && row.at_ms - evidence.start_at_ms >= waiting.at_ms && row.at_ms - evidence.start_at_ms < main.at_ms)),
          assistant_sources_did_not_play_simultaneously: Boolean(source && mainSource && cueEnd <= mainSource.scheduled_at_s + .000001),
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
      if (/\/duplex\/audio\/(neutral_ack|moment)$/.test(pathname)) {
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
        const silence = hardBlocked || listeningBlocked || /actually|stop|correct|confirm|goodbye/i.test(input.text) || (short && !trustedShort);
        // First session reproduces a provider stream whose partials arrive far
        // faster than a classifier response. A stable-only fixture missed this.
        const entry = { session: sessionNumber, phase: input.phase, epoch: input.epoch,
          revision: input.revision, at_ms: performance.now() };
        cueRequests.push(entry);
        await delay(sessionNumber === 7 && input.phase === 'waiting' ? 700
          : [10, 11].includes(sessionNumber) && input.phase === 'waiting' ? 150 : [1, 5].includes(sessionNumber) ? 220 : 35);
        entry.responded_at_ms = performance.now();
        reply({ phrase_id: silence ? 'silence' : input.phase === 'listening' ? 'neutral_ack' : 'moment', reason: 'fixture_decision', confidence: 1,
          probability: 1, language_choice: 'cache_compatible', expires_in_ms: sessionNumber === 11 && input.phase === 'waiting' ? 300 : 800,
          epoch: input.epoch, revision: input.revision, phase: input.phase }); return;
      }
      if (pathname.endsWith('/duplex/event') || pathname.endsWith('/duplex/stop') || pathname.endsWith('/end')) { reply({ ok: true }); return; }
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
    await delay(220); await setMic(0);
    sendJson(provider, { type: 'transcript', role: 'user', transcriptType: 'final', transcript: 'What time does your service department close on weekdays?', turn: 1 });
    sendJson(provider, { type: 'speech-update', role: 'user', status: 'stopped', turn: 1 });
    await until(async () => await eventCount('cue_started') > 0, 'waiting_cue_not_played');
    events = await page.evaluate(() => window.duplexSession.events);
    if (events.find(event => event.kind === 'cue_started').phase !== 'waiting') throw new Error('waiting_phase_was_not_selected');
    sendJson(provider, { type: 'speech-update', role: 'assistant', status: 'started', turn: 2 });
    provider.send(Buffer.alloc(640)); await delay(80);
    assert.equal(await page.evaluate(() => window.duplexSession.cuePlaying), true, 'provider_announcement_or_silence_cut_cue');
    report.checks.provider_announcement_and_silence_preserved_cue = true;
    const count = uplink.length;
    const main = streamMain(provider, 1.2);
    await until(async () => (await page.evaluate(() => window.duplexSession.events)).some(event => event.kind === 'cue_stopped' && ['main_audio_ready', 'main_started'].includes(event.reason)), 'main_did_not_preempt_cue');
    report.checks.main_audio_preempted_cue = true;
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
    const fastWaitingRequests = cueRequests.filter(row => row.session === 6 && row.phase === 'waiting');
    assert.equal(fastWaitingRequests.length, 1, 'fast_answer_selection_was_not_bounded');
    assert.ok(fastWaitingRequests[0].responded_at_ms !== undefined, 'fast_answer_did_not_exercise_held_result');
    assert.equal(fast.graph.starts.filter(row => row.samples === 16800).length, 0, 'fast_answer_waiting_clip_entered_graph');
    report.checks.main_after_400ms_skipped_waiting_cue = true;
    report.checks.fast_main_revoked_preselected_waiting_result = true;
    report.fast_answer_events = fast.events;

    await completedListeningCaller();
    await yieldFixtureCaller();
    await until(() => cueRequests.some(row => row.session === 7 && row.phase === 'waiting'), 'pending_waiting_request_missing');
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

    report.waiting_preemption_cases = [];
    for (const mainDelayMs of [800, 1000]) {
      await completedListeningCaller();
      const release = await yieldFixtureCaller();
      await until(() => page.evaluate(() => window.duplexSession.events.some(row => row.kind === 'cue_started'
        && row.phase === 'waiting')), 'preemption_waiting_cue_missing');
      await until(() => page.evaluate(() => window.duplexGraphEvidence.samples.some(row => row.cue && row.rms > .01
        && row.at_ms > window.duplexSession.startAt + window.duplexSession.events.find(row => row.kind === 'cue_started'
          && row.phase === 'waiting').at_ms)), 'preemption_waiting_cue_not_audible');
      await until(() => page.evaluate(at => performance.now() >= at,
        release.origin_ms + release.released_at_ms + mainDelayMs), 'preemption_deadline_not_reached', 2000);
      sendJson(provider, { type: 'speech-update', role: 'assistant', status: 'started', turn: 2 });
      await streamMain(provider, .6);
      sendJson(provider, { type: 'speech-update', role: 'assistant', status: 'stopped', turn: 2 });
      const preempt = await page.evaluate(() => ({ events: window.duplexSession.events, graph: window.duplexGraphEvidence }));
      const cutMoment = preempt.graph.starts.find(row => row.samples === 16800);
      const readyMain = preempt.graph.starts.find(row => row.samples === 320);
      const fade = cueFade(preempt.graph, cutMoment);
      assert.equal(cutMoment.stopped, true, 'main_did_not_cut_waiting_clip');
      assert.ok(cutMoment.stopped_ms <= readyMain.started_ms, 'main_source_created_before_waiting_stop');
      assert.ok(cutMoment.stopped_context_s <= readyMain.scheduled_at_s, 'waiting_and_main_audio_were_simultaneous');
      assert.ok(fade && Math.abs(fade.duration_ms - 8) < .01, 'actual_eight_ms_gain_ramp_missing');
      assert.ok(Math.abs(cutMoment.stopped_context_s - fade.end_context_s) < .000001, 'cue_stop_missed_fade_end');
      assert.ok(Math.abs(readyMain.scheduled_at_s - fade.end_context_s) < .000001, 'main_start_missed_fade_barrier');
      const recordedFade = await verifyRecordedFade(cutMoment, fade);
      assert.ok(recordedFade.faded_transition_mse < .00001
        && recordedFade.faded_transition_mse < recordedFade.unfaded_transition_mse * .1, 'gain_fade_not_present_in_recorded_output');
      report.waiting_preemption_cases.push({ main_ready_after_release_ms: mainDelayMs,
        waiting_clip_complete: false, waiting_clip_intentionally_stopped: true,
        waiting_clip_scheduled_ms: cutMoment.seconds * 1000,
        waiting_clip_played_ms: (cutMoment.stopped_context_s - cutMoment.scheduled_at_s) * 1000,
        assistant_audio_overlap_ms: Math.max(0, cutMoment.stopped_context_s - readyMain.scheduled_at_s) * 1000,
        fade, recorded_fade: recordedFade, added_main_delay_ms: (readyMain.scheduled_at_s - fade.start_context_s) * 1000,
        events: preempt.events, graph: preempt.graph });
    }
    report.checks.ready_main_intentionally_preempted_waiting_clip_without_overlap = true;
    report.checks.actual_eight_ms_fade_present_before_main_without_source_overlap = true;

    await completedListeningCaller();
    const heldRelease = await yieldFixtureCaller();
    await until(() => cueRequests.some(row => row.session === 10 && row.phase === 'waiting'
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
      classification_request_count: cueRequests.filter(row => row.session === 10 && row.phase === 'waiting').length };

    await completedListeningCaller();
    await yieldFixtureCaller();
    await delay(900);
    const expired = await page.evaluate(() => ({ events: window.duplexSession.events, graph: window.duplexGraphEvidence }));
    assert.equal(cueRequests.filter(row => row.session === 11 && row.phase === 'waiting').length, 1, 'expired_selection_retried_without_new_context');
    assert.equal(expired.events.filter(row => row.kind === 'cue_started' && row.phase === 'waiting').length, 0, 'held_result_outlived_its_300ms_ttl');
    assert.equal(expired.graph.starts.filter(row => row.samples === 16800).length, 0, 'expired_held_result_entered_graph');
    report.checks.playback_guard_did_not_extend_held_result_ttl = true;

    await newFixtureSession({ snapshot: { stage: 'details', proposal: { fixture_only: true } },
      previousAssistant: 'What name should I put on the service visit?', shortReplies: ['Alex'] });
    await until(() => page.evaluate(() => window.duplexSession.coordinator.listeningBlocked), 'details_fixture_context_not_loaded');
    await setMic(.25); sendJson(provider, { type: 'speech-update', role: 'user', status: 'started', turn: 1 });
    sendJson(provider, { type: 'transcript', role: 'user', transcriptType: 'partial', transcript: 'My name for the service visit is Alex.', turn: 1 });
    await delay(250);
    assert.equal(cueRequests.filter(row => row.session === 12).length, 0, 'details_proposal_permitted_listening_cue');
    await yieldFixtureCaller('Alex');
    await until(() => page.evaluate(() => window.duplexSession.events.some(row => row.kind === 'cue_started'
      && row.phase === 'waiting')), 'short_name_after_trusted_question_had_no_waiting_cue');
    assert.deepEqual(cueRequests.filter(row => row.session === 12).map(row => row.phase), ['waiting']);
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
    assert.equal(cueRequests.filter(row => row.session === 15 && row.phase === 'waiting').length, 1, 'short_fast_answer_selection_not_bounded');
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
    report.checks.exact_20ms_input_frames = uplink.every(frame => frame.bytes === 640);
    assert.equal(report.checks.exact_20ms_input_frames, true);
    report.checks.browser_errors = errors.length; assert.equal(errors.length, 0);
    report.fixture_sessions = sessions; report.input_frames = uplink.length;
    report.cue_geometry = phrases; report.http_request_count = requests.length;
    assert.equal(report.external_requests, 0);
    report.passed = true;
  }
} catch (error) {
  report.passed = false;
  report.failure = /^[a-z_]{1,100}$/.test(error?.message || '') ? error.message : 'browser_probe_failed';
  if (live && cueRequests.length) report.cue_requests = cueRequests.map(({ request, ...entry }) => ({ ...entry,
    elapsed_ms: entry.finished_at_ms === undefined ? null : entry.finished_at_ms - entry.started_at_ms }));
  if (page) { try {
    const evidence = await page.evaluate(() => ({ events: window.duplexSession?.events || [],
      metrics: window.duplexSession?.metrics(), graph: window.duplexGraphEvidence }));
    report.events_at_failure = evidence.events;
    if (live) { report.metrics = evidence.metrics; report.audio_graph = evidence.graph; }
  } catch {} }
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
