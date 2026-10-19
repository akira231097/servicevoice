/** One explicitly invoked synthetic Vapi WebSocket booking; no mic or phone call.
 * Requires Node24. This script spends provider credits only when EXECUTED.
 * Syntax check: node --check scripts/voice_booking_smoke.mjs
 * A successful fictional appointment is retained. Existing reports are preserved.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseEnv } from 'node:util';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { AssistantCaptionTracker } from './voice_regression_checks.mjs';
import { parseBookingOptions, PhoneCorrectionGuard, PHONE_CORRECTION_PHRASE, initialBookingRequest, STAGED_INITIAL_PHONE } from './booking_recovery_checks.mjs';
import { runtimePaths, preflightBookingRuntime, verifyLocalSessionDatabase } from './voice_runtime_paths.mjs';

const modeArgs = process.argv.slice(2);
let bookingOptions;
try { bookingOptions=parseBookingOptions(modeArgs); } catch {
  console.error('Usage: node scripts/voice_booking_smoke.mjs [--self-test | --audit-existing | --output unique-label [--correct-phone-on-mismatch | --exercise-phone-correction]]');
  process.exit(2);
}
const {outputLabel,correctPhone:correctPhoneMode,exercisePhone:exercisePhoneMode}=bookingOptions;

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const output = path.join(root, 'docs', outputLabel ? `voice-booking-smoke-${outputLabel}.json` : 'voice-booking-smoke.json');
const local = 'http://127.0.0.1:8765';
const hash = value => createHash('sha256').update(value).digest('hex');
const TOOL_NAMES = new Set(['get_dealership_info', 'begin_service_request', 'search_availability', 'prepare_booking', 'prepare_first_available_booking', 'confirm_action',
  'dismiss_proposal', 'get_appointment', 'prepare_reschedule', 'prepare_cancellation', 'request_handoff', 'get_session_context', 'get_operation']);
const AUXILIARY_TOOLS = new Set(['get_dealership_info', 'get_appointment', 'get_session_context', 'get_operation']);
function bookingToolRoute(events, verifiedPhoneCorrection=false) {
  // A variant label cannot establish the execution route. Classify exact saved
  // core events, including failures and repeats; separate harmless reads/status.
  const result = { observed_route: 'unverified', exact_core_sequence: false,
    core_events: [], auxiliary_events: [], auxiliary_count: 0 };
  if (!Array.isArray(events) || events.length > 256) return result;
  for (const event of events) {
    const row = { name: TOOL_NAMES.has(event?.name) ? event.name : 'other',
      status: ['success', 'error'].includes(event?.status) ? event.status : 'other',
      elapsed_ms: numberOrNull(event?.elapsed_ms) };
    (AUXILIARY_TOOLS.has(row.name) ? result.auxiliary_events : result.core_events).push(row);
  }
  result.auxiliary_count = result.auxiliary_events.length;
  if (result.core_events.some(event => event.status !== 'success')) return result;
  const names = result.core_events.map(event => event.name);
  for (const [route, expected] of [
    ['standard_begin_prepare_confirm', ['begin_service_request', 'prepare_booking', 'confirm_action']],
    ['fused_prepare_confirm', ['prepare_first_available_booking', 'confirm_action']],
    ...(verifiedPhoneCorrection?[
      ['standard_prepare_phone_correction_confirm',['begin_service_request','prepare_booking','prepare_booking','confirm_action']],
      ['fused_prepare_phone_correction_confirm',['prepare_first_available_booking','prepare_booking','confirm_action']],
      ['fused_prepare_search_phone_correction_confirm',['prepare_first_available_booking','search_availability','prepare_booking','confirm_action']],
    ]:[]),
  ]) {
    if (names.length === expected.length && names.every((name, i) => name === expected[i])) {
      result.observed_route = route;
      result.exact_core_sequence = true;
      break;
    }
  }
  return result;
}
const PHRASES = {
  request: "I'd like an oil change at the first available time. My name is Alex Morgan. My callback number is five one two, five five five, zero one four two. I drive a two zero two one Toyota Camry. Please read back all the details and ask me to confirm before booking.",
  choose: 'I choose the first available time you offered. The name is Alex Morgan. My callback number is five one two, five five five, zero one four two. The car is a two zero two one Toyota Camry. Please read back the full details before booking.',
  readback: 'Before I confirm, please read back the service, full date and time in Central time, name, callback number, and vehicle. I have not confirmed the booking yet.',
  consent: "Yes, that's correct. Please book that exact appointment.",
};
const INITIAL_REQUEST=initialBookingRequest(PHRASES.request,exercisePhoneMode);
const report = {
  kind: 'synthetic_audio_vapi_booking_smoke', started_at: new Date().toISOString(),
  variant_label: outputLabel, caller_audio: [],
  microphone_used: false, telephone_call_used: false, provider_call_created: false,
  provider_max_duration_seconds: 150, maximum_clarification_turns: 2,
  synthetic_caller: { name: 'Alex Morgan', callback: '5125550142', vehicle: '2021 Toyota Camry', service: 'oil_change' },
  output_audio_bytes: 0, output_voiced_audio_bytes: 0, output_voiced_frames: 0,
  input_silence_bytes: 0, input_silence_policy: 'Paced20ms zero PCM frames whenever the synthetic caller is idle, matching a continuous microphone stream.',
  voice_activity_rule: 'Received PCM16LE frame peak amplitude >128; transport activity heuristic, not acoustic speech recognition.',
  messages_by_type: {}, provider_events: [], input_turns: [], milestones_ms: {},
  preconsent_checks: 0, no_appointment_before_consent: false, consent_sent: false,
  booking_smoke_passed: false, appointment_retained: false,
  limitations: [
    'Synthetic PCM and provider speech events; does not prove human audibility, microphone usability, or acoustic response latency.',
    'Readback checks cover expected fields in final transcripts, not independent audio recognition or a general spoken-consent classifier.',
    'Readback and completion timestamps are client observations and include polling/event-delivery delay.',
    'Fictional local scheduler only. Successful appointment remains in the runtime database.',
    'Variant labels describe runs, not a verified configuration change. Compare public runtime metadata, source hashes and identical caller PCM hashes.',
    'Provider message timestamps, client events and gateway milestones are separate clocks and are not acoustic EOS-to-first-audible measurements.',
    'The observed booking route comes from exact saved core events. Auxiliary reads are separate; neither a variant label nor a prepared proposal proves a completed booking.',
  ],
};
let env, session, call, socket, state, config, verificationRuntime, callStarted = 0, hardTimer, silenceTimer;
let inputPlaying = false;
let assistantSpeaking = false, lastAssistantStart = 0, lastAssistantStop = 0, lastVoicedAudio = 0;
let lastAssistantTextAt = 0, persistedAssistantCount = 0;
let socketFault = false, transportEnded = false, providerEndedEvent = false, messageCount = 0;
let latestInputStart = 0, latestInputEnd = 0, localTranscriptBaseline = 0, consentTranscriptBaseline = 0;
let selectedProposal, selectedSlot, preconsentViolation = false;
const phoneGuard=correctPhoneMode?new PhoneCorrectionGuard({requireExercise:exercisePhoneMode}):null;
const phoneCaptions=correctPhoneMode?new AssistantCaptionTracker():null;
let phoneCorrectionMs=0;
if(correctPhoneMode)report.phone_recovery={enabled:true,status:'not_exercised_yet',correction_sent:false,passed:false};
if(exercisePhoneMode)report.phone_recovery.fixture={kind:'deliberately_incorrect_initial_callback',
  intended_initial_callback:STAGED_INITIAL_PHONE,required_corrected_callback:'+15125550142',
  correction_must_be_exercised:true,recognition_fix_claimed:false,
  interpretation:'The initial caller request deliberately gives a different phone number. This tests spoken correction and proposal replacement, not an ASR repair.'};
const assistantFinals = [];
const life = new AbortController();
const clock = () => performance.now();
const mark = name => { report.milestones_ms[name] = Math.round(clock() - callStarted); };
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

function checkActive() {
  if (life.signal.aborted) throw new Error('call_time_cap_reached');
  if (socketFault) throw new Error('websocket_error');
  if (transportEnded) throw new Error('provider_ended_before_completion');
}
async function request(url, options = {}, cleanup = false) {
  const response = await fetch(url, { ...options, redirect: 'error',
    signal: cleanup ? AbortSignal.timeout(4_000) : AbortSignal.any([life.signal, AbortSignal.timeout(20_000)]) });
  if (!response.ok) throw new Error(`HTTP_${response.status}`);
  return response;
}
const numberOrNull = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
const hashOrNull = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value) ? value : null;
const codeOrNull = value => typeof value === 'string' && /^[a-zA-Z0-9_.-]{1,100}$/.test(value) ? value : null;
const boolOrNull = value => typeof value === 'boolean' ? value : null;
const probabilityOrNull = value => numberOrNull(value) !== null && value <= 1 ? value : null;
function numericFields(value, keys) {
  return Object.fromEntries(keys.map(key => [key, numberOrNull(value?.[key])]));
}
function publicRuntimeConfig(value) {
  return { prompt_version: codeOrNull(value.prompt_version),
    timezone: value.timezone === 'America/Chicago' ? value.timezone : null,
    today: typeof value.today === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value.today) ? value.today : null,
    voice_provider: value.voice_provider === 'Vapi' ? 'Vapi' : null,
    live_voice_available: boolOrNull(value.live_voice_available),
    voice_max_duration_seconds: numberOrNull(value.voice_max_duration_seconds),
    receipt_narration_enabled: boolOrNull(value.receipt_narration_enabled), narrator_version: codeOrNull(value.narrator_version),
    jev_faq_enabled: boolOrNull(value.jev_faq_enabled), jev_faq_version: codeOrNull(value.jev_faq_version), jev_model: codeOrNull(value.jev_model) };
}
function recordProviderEvent(value) {
  if (report.provider_events.length < 400) report.provider_events.push({ observed_ms: Math.round(clock() - callStarted), ...value });
  else report.provider_events_truncated = true;
}
async function boundedJson(response, maximumBytes) {
  const chunks = []; let bytes = 0;
  for await (const chunk of response.body) {
    bytes += chunk.length;
    if (bytes > maximumBytes) throw new Error('evidence_response_too_large');
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}
function sanitizeGatewayTiming(value) {
  if (value?.kind !== 'gemini_gateway_timing' || !Array.isArray(value.requests) || value.requests.length > 256)
    throw new Error('gateway_timing_shape_invalid');
  const expectedCallKey = hash(call.id).slice(0, 16);
  return { kind: 'gemini_gateway_timing', available: true, capacity: 256,
    scope: 'Finalized in-memory server requests for this bound call; content omitted, not acoustic latency.',
    requests: value.requests.map(row => {
      if (row?.call_key !== expectedCallKey) throw new Error('gateway_timing_scope_mismatch');
      return {
        request_id: typeof row.request_id === 'string' && /^[a-f0-9]{32}$/.test(row.request_id) ? row.request_id : null,
        call_key: expectedCallKey, started_at_unix: numberOrNull(row.started_at_unix),
        system_prompt_sha256: hashOrNull(row.system_prompt_sha256), outcome: codeOrNull(row.outcome),
        route: ['gemini', 'deterministic_receipt', 'jev_static_faq'].includes(row.route) ? row.route : null,
        narration_reason: codeOrNull(row.narration_reason), narrator_version: codeOrNull(row.narrator_version),
        source_result_sha256: hashOrNull(row.source_result_sha256),
        jev_reason: codeOrNull(row.jev_reason), jev_faq_version: codeOrNull(row.jev_faq_version), jev_model: codeOrNull(row.jev_model),
        jev_probability: probabilityOrNull(row.jev_probability), jev_margin: probabilityOrNull(row.jev_margin),
        jev_language_probability: probabilityOrNull(row.jev_language_probability),
        milestones_ms: numericFields(row.milestones_ms, ['preflight', 'upstream_headers', 'first_upstream_event', 'first_text', 'first_tool_fragment', 'upstream_done', 'tool_emitted', 'total']),
        durations_ms: numericFields(row.durations_ms, ['tool_signature_persist', 'upstream_eof_drain', 'jev_route', 'jev_classification']),
        eof_drain_outcome: codeOrNull(row.eof_drain_outcome),
        counts: numericFields(row.counts, ['history_messages', 'history_tool_results', 'upstream_events', 'text_chunks', 'text_utf8_bytes', 'tool_calls', 'tcp_connects', 'tls_handshakes', 'eof_tail_bytes', 'jev_requests']),
      };
    }) };
}
function sanitizeProviderEvidence(value) {
  if (value?.id !== call.id || value.assistantId !== config.vapi_assistant_id
      || (value.assistantOverrides?.variableValues?.session_id !== undefined && value.assistantOverrides.variableValues.session_id !== session.id))
    throw new Error('provider_evidence_scope_mismatch');
  const iso = input => typeof input === 'string' && Number.isFinite(Date.parse(input)) ? new Date(input).toISOString() : null;
  const fields = ['modelLatency', 'voiceLatency', 'transcriberLatency', 'endpointingLatency', 'turnLatency'];
  const metrics = value.artifact?.performanceMetrics;
  const messages = Array.isArray(value.artifact?.messages) ? value.artifact.messages : value.messages;
  if (Array.isArray(messages) && messages.length > 2000) throw new Error('provider_message_count_invalid');
  const rows = (Array.isArray(messages) ? messages : []).filter(row => ['user', 'bot', 'assistant'].includes(row?.role));
  return {
    available: true, collected_at: new Date().toISOString(), call_id: call.id,
    status: ['scheduled', 'queued', 'ringing', 'in-progress', 'forwarding', 'ended'].includes(value.status) ? value.status : null,
    started_at: iso(value.startedAt), ended_at: iso(value.endedAt), ended_reason: codeOrNull(value.endedReason),
    cost_usd: numberOrNull(value.cost),
    messages_available: Array.isArray(messages),
    message_policy: 'Only role, timestamp, text length and SHA256 retained; no message text, tool arguments, URLs, headers, SDP or provider payload.',
    messages: rows.map(row => {
      const text = typeof row.message === 'string' ? row.message : typeof row.content === 'string' ? row.content : '';
      return { role: row.role === 'user' ? 'user' : 'assistant',
        ...numericFields(row, ['time', 'endTime', 'secondsFromStart']),
        text_characters: text.length, text_sha256: hash(text) };
    }),
    performance: { available: !!metrics && typeof metrics === 'object',
      values: numericFields(metrics, [...fields.map(field => field + 'Average'), 'fromTransportLatencyAverage', 'toTransportLatencyAverage', 'numUserInterrupted', 'numAssistantInterrupted']),
      turn_latencies: Array.isArray(metrics?.turnLatencies) ? metrics.turnLatencies.slice(0, 256).map(row => numericFields(row, fields)) : null },
    limitations: ['A single post-cleanup GET may precede final artifacts; missing values remain null.',
      'Provider performance and transcript timestamps are not independent acoustic latency measurements.'],
  };
}
async function callerPcm(name, text) {
  const model = 'eleven_flash_v2_5', format = 'pcm_16000';
  const key = hash(JSON.stringify([text, env.VAPI_ELEVENLABS_VOICE_ID, model, format]));
  const cache = runtimePaths(root, env).callerPcmCachePath;
  await fs.mkdir(cache, { recursive: true });
  const file = path.join(cache, key + '.pcm');
  let pcm, source = 'cache';
  try {
    const info = await fs.stat(file);
    if (!info.isFile() || info.size < 2 || info.size > 32_000 * 40 || info.size % 2) throw new Error('synthetic_input_audio_invalid');
    pcm = await fs.readFile(file);
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
    source = 'generated';
    const response = await request(`https://api.elevenlabs.io/v1/text-to-speech/${encodeURIComponent(env.VAPI_ELEVENLABS_VOICE_ID)}?output_format=${format}`, {
      method: 'POST', headers: { 'xi-api-key': env.ELEVENLABS_API_KEY, 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, model_id: model }),
    });
    const chunks = []; let bytes = 0;
    for await (const chunk of response.body) {
      bytes += chunk.length;
      if (bytes > 32_000 * 40) throw new Error('synthetic_input_audio_invalid');
      chunks.push(chunk);
    }
    pcm = Buffer.concat(chunks);
    if (!pcm.length || pcm.length % 2) throw new Error('synthetic_input_audio_invalid');
    await fs.writeFile(file, pcm, { flag: 'wx' });
  }
  if (!pcm.length || pcm.length > 32_000 * 40 || pcm.length % 2) throw new Error('synthetic_input_audio_invalid');
  report.caller_audio.push({ phrase: name, source, cache_key_sha256: key, pcm_sha256: hash(pcm),
    phrase_sha256: hash(text), model, format, bytes: pcm.length, duration_seconds: pcm.length / 32_000 });
  return pcm;
}
async function snapshot(cleanup = false) {
  const value = await (await request(`${local}/api/sessions/${session.id}`, { headers: { 'x-session-token': session.token } }, cleanup)).json();
  if (value.id !== session.id || value.mode !== 'live') throw new Error('local_session_scope_mismatch');
  state = value;
  const assistantCount = state.transcript.filter(row => row.role === 'assistant').length;
  if (assistantCount > persistedAssistantCount) lastAssistantTextAt = clock();
  persistedAssistantCount = assistantCount;
  if (report.consent_sent && state.appointment?.status === 'confirmed' && report.milestones_ms.appointment_confirmed_observed === undefined) mark('appointment_confirmed_observed');
  if (!report.consent_sent) {
    report.preconsent_checks += 1;
    if (state.appointment || state.events.some(event => event.name === 'confirm_action' && event.status === 'success')) {
      preconsentViolation = true;
      throw new Error('appointment_created_before_explicit_consent');
    }
  }
  return state;
}
function speechFinished(after, quietMs = 1200) {
  // ClientMessageSpeechUpdate in installed @vapi-ai/web/api.ts uses
  // {type:'speech-update',role:'assistant',status:'started'|'stopped'}.
  // Quiet binary transport alone is deliberately insufficient for consent.
  return (!phoneCaptions||phoneCaptions.settled(state?.transcript)) && !assistantSpeaking && lastAssistantStop > after && lastAssistantStop >= lastAssistantStart
    && lastVoicedAudio > after && clock() - Math.max(lastVoicedAudio, lastAssistantStop, lastAssistantTextAt) >= quietMs;
}
function observedAssistantText(after, baseline = localTranscriptBaseline) {
  const direct = assistantFinals.filter(row => row.at >= after).map(row => row.text);
  const persisted = (state?.transcript || []).slice(baseline).filter(row => row.role === 'assistant').map(row => row.text);
  return [...direct, ...persisted].join(' ');
}
function words(value) { return String(value).toLowerCase().normalize('NFKC').replace(/&/g, ' and ').replace(/[^a-z0-9]+/g, ' ').trim(); }
function vehicleWords(value) {
  return words(value).replace(/\b(20 21|2 0 2 1|two zero two one|twenty twenty one|two thousand (and )?twenty one)\b/g, '2021');
}
function spokenDigits(value) {
  const map = { zero: '0', oh: '0', one: '1', two: '2', three: '3', four: '4', five: '5', six: '6', seven: '7', eight: '8', nine: '9' };
  return words(value).split(' ').map(token => map[token] ?? token).join(' ').replace(/[^0-9]/g, '');
}
function readbackChecks(text, slot) {
  const normalized = ` ${words(text)} `;
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-US', { timeZone: 'America/Chicago', month: 'long', day: 'numeric', weekday: 'long', hour: 'numeric', hour12: true }).formatToParts(new Date(slot.start)).map(part => [part.type, part.value.toLowerCase()]));
  const ordinal = ['', 'first', 'second', 'third', 'fourth', 'fifth', 'sixth', 'seventh', 'eighth', 'ninth', 'tenth', 'eleventh', 'twelfth', 'thirteenth', 'fourteenth', 'fifteenth', 'sixteenth', 'seventeenth', 'eighteenth', 'nineteenth', 'twentieth', 'twenty first', 'twenty second', 'twenty third', 'twenty fourth', 'twenty fifth', 'twenty sixth', 'twenty seventh', 'twenty eighth', 'twenty ninth', 'thirtieth', 'thirty first'];
  const cardinal = ['', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten', 'eleven', 'twelve', 'thirteen', 'fourteen', 'fifteen', 'sixteen', 'seventeen', 'eighteen', 'nineteen', 'twenty', 'twenty one', 'twenty two', 'twenty three', 'twenty four', 'twenty five', 'twenty six', 'twenty seven', 'twenty eight', 'twenty nine', 'thirty', 'thirty one'];
  const day = Number(parts.day), hour = Number(parts.hour);
  const has = value => normalized.includes(` ${value} `);
  return {
    service: /\boil\b/.test(normalized) && /\b(change|filter|service)\b/.test(normalized),
    name: /\balex morgan\b/.test(normalized),
    vehicle: /\b2021 toyota camry\b/.test(vehicleWords(text)),
    callback: spokenDigits(text).includes('5125550142'),
    weekday: has(parts.weekday), month: has(parts.month),
    day: has(parts.day) || has(`${parts.day}th`) || has(`${parts.day}st`) || has(`${parts.day}nd`) || has(`${parts.day}rd`) || has(ordinal[day]) || has(cardinal[day]),
    time: (has(String(hour)) || has(String(hour).padStart(2, '0')) || has(cardinal[hour]))
      && (has(parts.dayPeriod) || has(parts.dayPeriod.split('').join(' ')) || (parts.dayPeriod === 'am' ? has('morning') : has('afternoon'))),
    timezone: /\b(central|cdt|cst|chicago)\b/.test(normalized),
    confirmation_question: /\b(confirm|correct|right|shall i|should i|would you like|does that|may i)\b/.test(normalized),
  };
}
function referenceEvidence(text, reference) {
  // The scheduler issues CSV + eight hex characters. Parse only a CSV-prefixed
  // reference span, never remove words across the whole conversation or accept
  // a prefix of a longer code. TTS may pronounce the separator as 'minus'.
  const result = { literal_exact: false, spoken_digits_exact: false, spoken_separators_exact: false };
  const expected = /^CSV-([A-F0-9]{8})$/i.exec(String(reference));
  if (!expected) return result;
  const target = expected[1].toLowerCase();
  const digits = { zero: '0', oh: '0', one: '1', two: '2', three: '3', four: '4', five: '5', six: '6', seven: '7', eight: '8', nine: '9' };
  const normalized = words(text);
  const prefix = /\bc\s*s\s*v(?=\b|[a-f0-9])/g;
  for (const match of normalized.matchAll(prefix)) {
    const tokens = normalized.slice(match.index + match[0].length).trimStart().split(/\s+/);
    let candidate = '', usedWords = false, usedSeparator = false, boundarySeen = false;
    for (const token of tokens.slice(0, 24)) {
      if (['hyphen', 'dash', 'minus'].includes(token)) { usedSeparator = true; continue; }
      if (Object.hasOwn(digits, token)) { candidate += digits[token]; usedWords = true; }
      else if (/^[a-f0-9]+$/.test(token)) candidate += token;
      else { boundarySeen = true; break; }
      if (candidate.length > 8) break; // Extra characters are an invalid code.
    }
    if (candidate !== target || (tokens.length > 24 && !boundarySeen)) continue;
    if (usedWords) result.spoken_digits_exact = true;
    if (usedSeparator) result.spoken_separators_exact = true;
    if (!usedWords && !usedSeparator) result.literal_exact = true;
  }
  return result;
}
function referenceMatched(evidence) { return evidence.literal_exact || evidence.spoken_digits_exact || evidence.spoken_separators_exact; }
async function play(name, pcm) {
  checkActive();
  localTranscriptBaseline = state?.transcript.length || 0;
  latestInputStart = clock();
  const row = { phrase: name, text: name==='phone_correction'?PHONE_CORRECTION_PHRASE:name==='request'?INITIAL_REQUEST:PHRASES[name], start_ms: Math.round(latestInputStart - callStarted), audio_seconds: pcm.length / 32_000 };
  report.input_turns.push(row);
  inputPlaying = true;
  try {
    for (let offset = 0; offset < pcm.length; offset += 640) {
      checkActive();
      if (socket.readyState !== WebSocket.OPEN) throw new Error('transport_closed_during_input');
      if (socket.bufferedAmount > 128_000) throw new Error('audio_transport_backpressure');
      socket.send(pcm.subarray(offset, Math.min(offset + 640, pcm.length)));
      if (name === 'consent' && offset === 0) { report.consent_sent = true; mark('consent_started'); }
      if (name === 'phone_correction' && offset === 0) { report.phone_recovery.correction_sent=true;mark('phone_correction_started'); }
      await pause(Math.max(0, latestInputStart + Math.min(offset + 640, pcm.length) / 32 - clock()));
    }
  } finally {
    inputPlaying = false;
  }
  latestInputEnd = clock();
  row.end_ms = Math.round(latestInputEnd - callStarted);
}
async function waitForAssistant(after, seconds, predicate = () => true) {
  const until = clock() + seconds * 1000;
  while (clock() < until) {
    checkActive();
    await snapshot();
    if (speechFinished(after) && predicate()) return true;
    if (state.ended) throw new Error('local_session_ended_before_completion');
    await pause(350);
  }
  return false;
}
function substantiveClarification(text) {
  // A completed filler segment ('Hold on a sec?', 'One moment.') is not a
  // request for another caller turn. Require a task-related question instead.
  const value = words(text);
  const requestCue = /\b(which|what|could you|can you|may i|would you|do you|does that|is that|will that|would that|please (provide|tell|choose|select|repeat))\b/.test(value);
  const topic = /\b(time|date|day|monday|tuesday|wednesday|thursday|friday|available|availability|appointment|slot|name|callback|phone|number|vehicle|car|year|make|model|service|oil|first|earliest)\b/.test(value);
  return requestCue && topic;
}
async function waitForBookingDecision() {
  // Reserve enough of the fixed call budget for consent and its completion.
  const seconds = Math.min(45, (callStarted + 105_000 - clock()) / 1000);
  if (seconds <= 0) throw new Error('insufficient_time_for_verified_consent');
  let decision;
  const complete = await waitForAssistant(latestInputEnd, seconds, () => {
    const text = observedAssistantText(latestInputStart);
    if (state.proposal?.kind === 'book') {
      const offered = state.slots?.find(slot => slot.id === state.selected_slot);
      if (!offered || offered.id !== state.slots[0]?.id) throw new Error('proposal_does_not_use_first_offered_slot');
      const checks = readbackChecks(text, offered);
      report.readback_checks = checks;
      if(phoneGuard?.eligible(state,checks,true)) { decision={kind:'phone_correction',checks};return true; }
      if (Object.values(checks).every(Boolean)) {
        if(phoneGuard?.attempted){
          const evidence=durableEvidence(phoneGuard.original.id,state.proposal.id);
          report.phone_recovery.replacement=phoneGuard.verify(state,checks,evidence);
          report.phone_recovery.corrected_proposal={...state.proposal,phone:state.customer.phone,slot_id:state.selected_slot};
          report.phone_recovery.replacement_evidence=evidence;
          report.phone_recovery.status='fresh_readback_verified';
        }else if(correctPhoneMode){
          report.phone_recovery.status='recovery_not_exercised';
          if(exercisePhoneMode)throw new Error('phone_correction_exercise_not_observed');
        }
        decision = { kind: 'ready', proposal: { ...state.proposal }, slot: offered }; return true;
      }
      // Partial phone digits or a sentence pause keep waiting. Do not spend a
      // clarification turn or finish the test merely because one TTS chunk ends.
      return false;
    }
    if (!(state.pending_operations?.length) && substantiveClarification(text)) { decision = { kind: 'choose' }; return true; }
    return false;
  });
  if (complete) return decision;
  // A bounded timeout may request a fresh complete readback only when the
  // assistant has been quiet and its final transcript stable for at least3s.
  // Otherwise fail safely without interrupting an incomplete readback.
  if (state.proposal?.kind === 'book' && speechFinished(latestInputEnd, 3000)) return { kind: 'readback' };
  throw new Error('substantive_response_or_full_readback_not_observed');
}
function controlSocketEnd() {
  clearInterval(silenceTimer);
  if (socket?.readyState === WebSocket.OPEN) {
    try { socket.send(JSON.stringify({ type: 'end-call' })); report.end_control_sent = true; }
    catch { report.end_control_send_failed = true; }
    socket.close();
  } else if (socket?.readyState === WebSocket.CONNECTING) socket.close();
}
function durableEvidence(originalProposalId=null,replacementProposalId=null) {
  if (!verificationRuntime) throw new Error('runtime_verification_preflight_required');
  const python = verificationRuntime.pythonPath;
  const db = verificationRuntime.databasePath;
  const code = `from runtime_sqlite import ensure_sqlite
ensure_sqlite()
import json,sqlite3,sys
from pathlib import Path
from contextlib import closing
from scripts.inspect_vapi_calls import local_evidence
dbpath,sid,cid=sys.argv[1:4]
evidence=local_evidence(Path(dbpath),cid)
with closing(sqlite3.connect(Path(dbpath).resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
 db.execute('PRAGMA query_only=ON')
 row=db.execute('SELECT id,provider_call_id FROM sessions WHERE id=? AND tenant=?',(sid,'cedar-demo')).fetchone()
 if row != (sid,cid): raise RuntimeError('scope_mismatch')
 if len(sys.argv)>4:
  def proposal(pid):
   row=db.execute('SELECT id,kind,state,payload FROM proposals WHERE id=? AND session_id=? AND tenant=?',(pid,sid,'cedar-demo')).fetchone()
   if not row:return None
   payload=json.loads(row[3])
   return {'id':row[0],'kind':row[1],'state':row[2],'phone':payload.get('customer',{}).get('phone'),'slot_id':payload.get('slot_id')}
  evidence['phone_proposals']={'original':proposal(sys.argv[4]),'replacement':proposal(sys.argv[5])}
print(json.dumps(evidence))`;
  let evidence;
  try {
    evidence = JSON.parse(execFileSync(python, ['-c', code, db, session.id, call.id,
      ...(originalProposalId?[originalProposalId,replacementProposalId||'']:[])], { cwd: root, timeout: 5_000, maxBuffer: 64 * 1024, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }));
  } catch { throw new Error('durable_evidence_unavailable'); }
  const counts = value => value && typeof value === 'object' && !Array.isArray(value)
    && Object.values(value).every(count => Number.isSafeInteger(count) && count >= 0);
  if (evidence?.available !== true || evidence.session_binding !== 'matched_provider_call_id'
      || !Number.isSafeInteger(evidence.appointment_count) || evidence.appointment_count < 0
      || !counts(evidence.appointments_by_current_status) || !counts(evidence.committed_actions)) throw new Error('durable_evidence_incomplete');
  return evidence;
}

async function auditExistingBooking() {
  const scope = { call_id: '01a14cd1-bc04-7331-92db-e4928fff74ed', session_id: 'sess_916a987b49854c31' };
  const archiveName = 'voice-booking-smoke.before-reference-normalization.json';
  const auditPath = path.join(root, 'docs', 'voice-booking-audit.json');
  const audit = {
    kind: 'read_only_existing_synthetic_booking_audit', audited_at: new Date().toISOString(), scope,
    provider_get_requests: 0, new_provider_calls: 0, tts_requests: 0, end_call_requests: 0,
    scheduler_mutations: 0, original_report_modified: false, audit_passed: false,
    production_version_boundary: {
      original_call_predates: ['gateway-issued tool execution enforcement', 'bounded status-read lane', 'prompt version3'],
      current_source_live_verification: false,
      explanation: 'This rechecks a historical call. It does not validate deployment or live behavior of the later issued-tool gate, status lane, or prompt. A fresh post-restart call remains separate evidence.',
    },
    limitations: [
      'Historical pre-consent and readback checks come from the archived runner report; they are not independently reconstructed or retroactively verified.',
      'Current read-only scheduler records and an ended provider call support a fictional booking, not real DMS completion or human consent.',
      'Reference matching checks provider transcript characters, including spoken separators; it does not prove human audibility or acoustic accuracy.',
      'No original failed status is rewritten. The audit and original report are separate artifacts.',
    ],
  };
  // No GET is made if a previous audit already exists. No normal-call setup or
  // finally handler is entered in this mode, even on audit failure.
  const handle = await fs.open(auditPath, 'wx');
  try {
    const archivedBytes = await fs.readFile(path.join(root, 'docs', archiveName));
    if (archivedBytes.length > 1024 * 1024) throw new Error('archive_too_large');
    audit.original_report = { filename: archiveName, sha256: createHash('sha256').update(archivedBytes).digest('hex') };
    const archived = JSON.parse(archivedBytes.toString('utf8'));
    if (archived.kind !== 'synthetic_audio_vapi_booking_smoke' || archived.call_id !== scope.call_id || archived.session_id !== scope.session_id
        || archived.booking_smoke_passed !== false || archived.failure !== 'confirmed_appointment_and_complete_reference_reply_not_observed') throw new Error('archive_scope_or_expected_failure_mismatch');
    audit.original_report.booking_smoke_passed = archived.booking_smoke_passed;
    audit.original_report.failure = archived.failure;
    audit.original_report.started_at = archived.started_at;
    audit.original_report.finished_at = archived.finished_at;
    const countTotal = value => value && typeof value === 'object' && !Array.isArray(value)
      && Object.values(value).every(count => Number.isSafeInteger(count) && count >= 0)
      ? Object.values(value).reduce((sum, count) => sum + count, 0) : null;
    const historicZero = archived.preconsent_evidence?.available === true && archived.preconsent_evidence.session_binding === 'matched_provider_call_id'
      && archived.preconsent_evidence.appointment_count === 0 && countTotal(archived.preconsent_evidence.committed_actions) === 0;
    const historicOne = archived.final_evidence?.available === true && archived.final_evidence.session_binding === 'matched_provider_call_id'
      && archived.final_evidence.appointment_count === 1 && archived.final_evidence.appointments_by_current_status?.confirmed === 1
      && archived.final_evidence.committed_actions?.book === 1 && countTotal(archived.final_evidence.committed_actions) === 1;
    const checkNames = ['service', 'name', 'vehicle', 'callback', 'weekday', 'month', 'day', 'time', 'timezone', 'confirmation_question'];
    audit.historical_observations = {
      source: 'Archived runner observations, not independently retroverified',
      preconsent_zero_appointments_and_commits: historicZero, final_one_confirmed_booking: historicOne,
      consent_sent: archived.consent_sent === true, no_appointment_before_consent: archived.no_appointment_before_consent === true,
      all_readback_checks_passed: checkNames.every(key => archived.readback_checks?.[key] === true),
      readback_before_consent_before_booking_observation: Number.isFinite(archived.milestones_ms?.proposal_readback_completed)
        && archived.milestones_ms.proposal_readback_completed < archived.milestones_ms.consent_started
        && archived.milestones_ms.consent_started < archived.milestones_ms.appointment_confirmed_observed,
    };
    const auditEnv = { ...parseEnv(await fs.readFile(path.join(root, '.env'), 'utf8')), ...process.env };
    if (!auditEnv.VAPI_API_KEY) throw new Error('configuration_missing');
    const auditRuntime = await preflightBookingRuntime({projectRoot:root, env:auditEnv});
    audit.local_runtime_preflight = auditRuntime.evidence;
    const dbPath = auditRuntime.databasePath;
    const code = `from runtime_sqlite import ensure_sqlite
ensure_sqlite()
import json,sqlite3,sys
from pathlib import Path
from contextlib import closing
from scripts.inspect_vapi_calls import local_evidence
dbpath,sid,cid,pid=sys.argv[1:]
with closing(sqlite3.connect(Path(dbpath).resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
 db.execute('PRAGMA query_only=ON')
 db.row_factory=sqlite3.Row
 row=db.execute('SELECT id,provider_call_id,ended FROM sessions WHERE id=? AND tenant=?',(sid,'cedar-demo')).fetchone()
 if not row or (row['id'],row['provider_call_id'])!=(sid,cid): raise RuntimeError('scope_mismatch')
 appts=db.execute('SELECT id,reference,status,service,slot_id FROM appointments WHERE session_id=? AND tenant=?',(sid,'cedar-demo')).fetchall()
 proposal=db.execute('SELECT id,kind,state FROM proposals WHERE id=? AND session_id=? AND tenant=?',(pid,sid,'cedar-demo')).fetchone()
 extra={'session_ended':bool(row['ended']),'appointment':dict(appts[0]) if len(appts)==1 else None,'proposal':dict(proposal) if proposal else None}
value=local_evidence(Path(dbpath),cid)
value.update(extra)
print(json.dumps(value))`;
    let localEvidence;
    try {
      localEvidence = JSON.parse(execFileSync(auditRuntime.pythonPath,
        ['-B', '-c', code, dbPath, scope.session_id, scope.call_id, archived.proposal_id],
        { cwd: root, timeout: 5_000, maxBuffer: 64 * 1024, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }));
    } catch { throw new Error('audit_local_evidence_unavailable'); }
    if (localEvidence.available !== true || localEvidence.session_binding !== 'matched_provider_call_id'
        || localEvidence.appointment?.reference !== 'CSV-6977BF2E' || localEvidence.appointment?.slot_id !== archived.selected_slot_id
        || localEvidence.proposal?.id !== archived.proposal_id) throw new Error('audit_local_scope_or_reference_mismatch');
    // local_evidence and these SELECTs return only scoped operational columns.
    audit.current_read_only_scheduler = localEvidence;
    audit.provider_get_requests += 1;
    const response = await fetch(`https://api.vapi.ai/call/${scope.call_id}`, { method: 'GET', redirect: 'error',
      headers: { Authorization: 'Bearer ' + auditEnv.VAPI_API_KEY }, signal: AbortSignal.timeout(20_000) });
    if (!response.ok) throw new Error(`provider_GET_HTTP_${response.status}`);
    const chunks = []; let received = 0;
    for await (const chunk of response.body) {
      received += chunk.length;
      if (received > 12 * 1024 * 1024) throw new Error('provider_record_too_large');
      chunks.push(chunk);
    }
    const provider = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    if (provider.id !== scope.call_id || provider.status !== 'ended' || provider.type !== 'vapi.websocketCall'
        || !Number.isFinite(Date.parse(provider.endedAt))) throw new Error('provider_scope_or_ended_state_mismatch');
    const providerSid = provider.assistantOverrides?.variableValues?.session_id;
    if (providerSid !== undefined && providerSid !== scope.session_id) throw new Error('provider_session_scope_mismatch');
    audit.provider = { call_id: provider.id, status: 'ended', type: 'vapi.websocketCall', ended_at: provider.endedAt,
      session_variable_matches: providerSid === undefined ? null : true,
      session_scope_basis: providerSid === undefined ? 'Exact call GET plus required local session-to-call binding' : 'Provider session variable and local session-to-call binding' };
    const messages = Array.isArray(provider.artifact?.messages) ? provider.artifact.messages : provider.messages;
    if (!Array.isArray(messages) || messages.length > 2000) throw new Error('provider_spoken_messages_unavailable');
    const lastUser = messages.findLastIndex(message => message?.role === 'user');
    if (lastUser < 0) throw new Error('provider_user_turn_unavailable');
    const finalAssistant = messages.slice(lastUser + 1).filter(message => ['assistant', 'bot'].includes(message?.role))
      .map(message => typeof message.message === 'string' ? message.message : typeof message.content === 'string' ? message.content : '')
      .filter(Boolean);
    const finalText = finalAssistant.join(' ');
    if (!finalText || finalText.length > 100_000) throw new Error('provider_final_assistant_transcript_unavailable');
    audit.corrected_reference_verifier = {
      source: Array.isArray(provider.artifact?.messages) ? 'artifact.messages assistant/bot messages after last user turn' : 'messages assistant/bot entries after last user turn',
      assistant_segments_checked: finalAssistant.length, expected_reference: localEvidence.appointment.reference,
      ...referenceEvidence(finalText, localEvidence.appointment.reference),
    };
    audit.corrected_reference_verifier.matched = referenceMatched(audit.corrected_reference_verifier);
    audit.original_failure_cause = audit.corrected_reference_verifier.matched
      ? 'Historical reference matcher did not normalize spoken separator minus and separated code fragments; corrected exact parser matches the persisted reference.'
      : 'Corrected parser could not establish an exact reference match; original failure is not reclassified as a verifier-only failure.';
    const historical = audit.historical_observations;
    audit.audit_passed = historical.preconsent_zero_appointments_and_commits && historical.final_one_confirmed_booking
      && historical.consent_sent && historical.no_appointment_before_consent && historical.all_readback_checks_passed
      && historical.readback_before_consent_before_booking_observation && localEvidence.session_ended === true
      && localEvidence.appointment_count === 1 && localEvidence.appointments_by_current_status?.confirmed === 1
      && localEvidence.appointment.status === 'confirmed' && localEvidence.appointment.service === 'oil_change'
      && localEvidence.committed_actions?.book === 1 && countTotal(localEvidence.committed_actions) === 1
      && localEvidence.proposal.kind === 'book' && localEvidence.proposal.state === 'committed'
      && audit.corrected_reference_verifier.matched;
  } catch (error) {
    const safe = String(error?.message || 'unknown');
    audit.failure = /^[a-zA-Z0-9_]+$/.test(safe) ? safe : 'audit_evidence_unavailable';
    audit.audit_passed = false;
  } finally {
    audit.finished_at = new Date().toISOString();
    await handle.writeFile(JSON.stringify(audit, null, 2) + '\n');
    await handle.close();
    console.log(JSON.stringify(audit, null, 2));
  }
  return audit.audit_passed;
}

// Pure fixture mode stops before any file access, configuration, SQLite or
// provider request. It exercises only the reference parser above.
if (process.argv.includes('--self-test')) {
  const { strict: assert } = await import('node:assert');
  const expected = 'CSV-6977BF2E';
  const fixtures = [
    ['literal', 'Your reference is CSV-6977BF2E.', true],
    ['actual_fragment_join', 'Your confirmation reference is CSV minus 6977 BF 2 e. Is there anything else I can help with?', true],
    ['spoken_dash', 'The reference is C S V dash 6 9 7 7 B F 2 E.', true],
    ['spoken_hyphen_digits', 'Your reference is CSV hyphen six nine seven seven B F two E.', true],
    ['compact_literal', 'Reference CSV6977BF2E.', true],
    ['wrong_digit', 'Your reference is CSV minus 6978 BF 2 E.', false],
    ['wrong_letter', 'Your reference is CSV minus 6977 AF 2 E.', false],
    ['missing_character', 'Your reference is CSV minus 6977 BF 2.', false],
    ['extra_character', 'Your reference is CSV minus 6977 BF 2 E 9.', false],
    ['extra_contiguous_character', 'Your reference is CSV-6977BF2E9.', false],
    ['extra_spoken_character', 'Your reference is CSV minus six nine seven seven B F two E nine.', false],
    ['unrelated_digits', 'CSV service department. The unrelated number is 6977BF2E.', false],
    ['word_inside_reference', 'Your reference is CSV minus 6977 service BF2E.', false],
    ['missing_prefix', 'Your reference is 6977BF2E.', false],
    ['bounded_span_cannot_hide_extra', 'CSV 6977BF2E ' + 'dash '.repeat(24) + '9', false],
  ];
  const results = fixtures.map(([name, text, shouldMatch]) => {
    const evidence = referenceEvidence(text, expected);
    assert.equal(referenceMatched(evidence), shouldMatch, name);
    return { name, expected_match: shouldMatch, passed: true };
  });
  assert.equal(referenceMatched(referenceEvidence('CSV-6977BF2E', 'CSV-6977BF2')), false);
  // These fixtures never read files or call fetch. They check that telemetry
  // additions cannot retain arbitrary provider bodies or cross-call rows.
  call = { id: '00000000-0000-4000-8000-000000000001' };
  session = { id: 'sess_offline' };
  config = { vapi_assistant_id: '00000000-0000-4000-8000-000000000002' };
  const secret = 'OFFLINE_SECRET_MUST_NOT_BE_RETAINED';
  const providerFixture = { id: call.id, assistantId: config.vapi_assistant_id, status: 'ended',
    assistantOverrides: { variableValues: { session_id: session.id, session_token: secret } },
    artifact: { messages: [{ role: 'bot', message: secret, time: 100, endTime: 150, secondsFromStart: 0.1, headers: secret }],
      performanceMetrics: { modelLatencyAverage: 123, voiceLatencyAverage: Infinity, turnLatencies: [{ turnLatency: 234 }] } },
    monitor: { controlUrl: secret }, headers: secret, sdp: secret };
  const providerSafe = sanitizeProviderEvidence(providerFixture);
  assert.equal(JSON.stringify(providerSafe).includes(secret), false);
  assert.equal(providerSafe.messages[0].text_sha256, hash(secret));
  assert.equal(providerSafe.performance.values.modelLatencyAverage, 123);
  assert.equal(providerSafe.performance.values.voiceLatencyAverage, null);
  assert.throws(() => sanitizeProviderEvidence({ ...providerFixture, id: 'wrong-call' }), /scope_mismatch/);
  assert.throws(() => sanitizeProviderEvidence({ ...providerFixture, assistantOverrides: { variableValues: { session_id: 'wrong-session' } } }), /scope_mismatch/);
  const gatewayFixture = { kind: 'gemini_gateway_timing', requests: [{ call_key: hash(call.id).slice(0, 16),
    outcome: 'completed', route: 'deterministic_receipt', source_result_sha256: hash('receipt'),
    milestones_ms: { first_text: 1, total: 2 }, counts: { tool_calls: 0 }, prompt: secret, headers: secret }] };
  const gatewaySafe = sanitizeGatewayTiming(gatewayFixture);
  assert.equal(JSON.stringify(gatewaySafe).includes(secret), false);
  assert.equal(gatewaySafe.requests[0].route, 'deterministic_receipt');
  assert.equal(gatewaySafe.requests[0].milestones_ms.first_text, 1);
  assert.throws(() => sanitizeGatewayTiming({ ...gatewayFixture, requests: [{ ...gatewayFixture.requests[0], call_key: 'wrong-call' }] }), /scope_mismatch/);
  assert.throws(() => sanitizeGatewayTiming({ kind: 'other', requests: [] }), /shape_invalid/);
  const jevFixture = { ...gatewayFixture, requests: [{ ...gatewayFixture.requests[0], route: 'jev_static_faq',
    jev_reason: 'matched', jev_faq_version: 'jev-static-faq-2026-10-18-1', jev_model: 'jev-1.13.0',
    jev_probability: 0.98, jev_margin: 0.9, jev_language_probability: 0.99,
    counts: { jev_requests: 1 }, durations_ms: { jev_route: 51, jev_classification: 45 },
    state: secret, api_key: secret, label_text: secret }] };
  const jevSafe = sanitizeGatewayTiming(jevFixture).requests[0];
  assert.equal(jevSafe.route, 'jev_static_faq');
  assert.deepEqual([jevSafe.jev_probability, jevSafe.jev_margin, jevSafe.jev_language_probability], [0.98, 0.9, 0.99]);
  assert.deepEqual([jevSafe.counts.jev_requests, jevSafe.durations_ms.jev_route, jevSafe.durations_ms.jev_classification], [1, 51, 45]);
  assert.deepEqual([jevSafe.jev_reason, jevSafe.jev_faq_version, jevSafe.jev_model], ['matched', 'jev-static-faq-2026-10-18-1', 'jev-1.13.0']);
  assert.equal(JSON.stringify(jevSafe).includes(secret), false);
  assert.equal(sanitizeGatewayTiming({ ...gatewayFixture, requests: [{ ...gatewayFixture.requests[0], counts: { jev_requests: 0 }, jev_probability: 2 }] }).requests[0].counts.jev_requests, 0);
  assert.equal(sanitizeGatewayTiming({ ...gatewayFixture, requests: [{ ...gatewayFixture.requests[0], jev_probability: 2 }] }).requests[0].jev_probability, null);
  const publicSafe = publicRuntimeConfig({ receipt_narration_enabled: false, narrator_version: 'receipt-en-2026-10-18-1',
    jev_faq_enabled: true, jev_faq_version: 'jev-static-faq-2026-10-18-1', jev_model: 'jev-1.13.0', vapi_public_key: secret, session_token: secret });
  assert.equal(publicSafe.receipt_narration_enabled, false);
  assert.equal(publicSafe.jev_faq_enabled, true);
  assert.deepEqual([publicSafe.jev_faq_version, publicSafe.jev_model], ['jev-static-faq-2026-10-18-1', 'jev-1.13.0']);
  assert.equal(JSON.stringify(publicSafe).includes(secret), false);
  assert.equal(publicRuntimeConfig({ jev_faq_enabled: 'true' }).jev_faq_enabled, null);
  const events = names => names.map(name => ({ name, status: 'success', elapsed_ms: 2 }));
  const standard = ['begin_service_request', 'prepare_booking', 'confirm_action'];
  const fused = ['prepare_first_available_booking', 'confirm_action'];
  const routeFixtures = [
    ['standard', events(standard), 'standard_begin_prepare_confirm'],
    ['fused', events(fused), 'fused_prepare_confirm'],
    ['auxiliary_reads', events(['get_dealership_info', fused[0], 'get_operation', 'get_session_context', fused[1], 'get_appointment']), 'fused_prepare_confirm'],
    ['no_commit', events(fused.slice(0, 1)), 'unverified'],
    ['commit_only', events(['confirm_action']), 'unverified'],
    ['mixed_route', events([standard[0], ...fused]), 'unverified'],
    ['repeat_prepare', events([fused[0], ...fused]), 'unverified'],
    ['repeat_commit', events([...fused, fused[1]]), 'unverified'],
    ['wrong_order', events([...fused].reverse()), 'unverified'],
    ['changed_offer', events([fused[0], 'search_availability', fused[1]]), 'unverified'],
    ['failed_prepare', [{ name: fused[0], status: 'error' }, ...events(fused)], 'unverified'],
    ['failed_commit', [...events(fused.slice(0, 1)), { name: fused[1], status: 'error' }], 'unverified'],
    ['unknown_name', events([secret, ...fused]), 'unverified'],
    ['malformed', null, 'unverified'],
    ['oversized', events(Array(257).fill('get_operation')), 'unverified'],
  ];
  for (const [name, rows, expected] of routeFixtures) {
    const evidence = bookingToolRoute(rows);
    assert.equal(evidence.observed_route, expected, name);
    assert.equal(evidence.exact_core_sequence, expected !== 'unverified', name);
    assert.equal(JSON.stringify(evidence).includes(secret), false, name);
  }
  const auxiliary = bookingToolRoute(routeFixtures[2][1]);
  assert.equal(auxiliary.auxiliary_count, 4);
  assert.equal(auxiliary.auxiliary_events.reduce((total, event) => total + event.elapsed_ms, 0), 8);
  assert.equal(bookingToolRoute([{ name: 'get_operation', status: 'error' }, ...events(fused)]).observed_route, 'fused_prepare_confirm');
  assert.equal(bookingToolRoute([{ name: fused[0], status: secret }, ...events(fused.slice(1))]).observed_route, 'unverified');
  const correctedRoutes=[
    [['prepare_first_available_booking','prepare_booking','confirm_action'],'fused_prepare_phone_correction_confirm'],
    [['begin_service_request','prepare_booking','prepare_booking','confirm_action'],'standard_prepare_phone_correction_confirm'],
  ];
  for(const [names,expectedRoute] of correctedRoutes){
    assert.equal(bookingToolRoute(events(names)).observed_route,'unverified');
    assert.equal(bookingToolRoute(events(names),true).observed_route,expectedRoute);
  }
  assert.equal(bookingToolRoute(events(['prepare_first_available_booking','prepare_booking','prepare_booking','confirm_action']),true).observed_route,'unverified');
  const observedCorrection=['prepare_first_available_booking','search_availability','prepare_booking','confirm_action'];
  const routeAuditFixtures=[
    [events(observedCorrection),true,'fused_prepare_search_phone_correction_confirm'],
    [events(observedCorrection),false,'unverified'],
    [events(['prepare_first_available_booking','search_availability','search_availability','prepare_booking','confirm_action']),true,'unverified'],
    [events([...observedCorrection,'search_availability']),true,'unverified'],
    [events(['search_availability',...observedCorrection]),true,'unverified'],
    [events(['prepare_first_available_booking','prepare_booking','search_availability','confirm_action']),true,'unverified'],
    [events(observedCorrection).map(row=>row.name==='search_availability'?{...row,status:'error'}:row),true,'unverified'],
    [events(['begin_service_request','prepare_booking','search_availability','prepare_booking','confirm_action']),true,'unverified'],
  ];
  for(const [rows,verified,expectedRoute] of routeAuditFixtures)assert.equal(bookingToolRoute(rows,verified).observed_route,expectedRoute);
  const phoneSlot={start:'2026-10-20T16:00:00+00:00'};
  const readback='Oil and filter service for Alex Morgan, 2021 Toyota Camry, on Tuesday October 20 at 11 AM Central. Callback ';
  for(const [number,expectedMatch] of [
    ['1 5 1 2 5 5 5 0 1 4 2',true],['512-555-0142',true],['five one two five five five zero one four two',true],
    ['15. 125500. 1. 42.',false],['512-550-0142',false],['512-555-014',false],
  ])assert.equal(readbackChecks(readback+number+'. Would you like me to confirm?',phoneSlot).callback,expectedMatch,number);
  console.log(JSON.stringify({ kind: 'offline_reference_parser_fixtures', provider_calls: 0, file_reads: 0, database_accesses: 0,
    passed: results.length + 1, fixtures: results, malformed_expected_reference_rejected: true,
    telemetry_assertions_passed: 11,
    jev_telemetry_assertions_passed: 12,
    booking_route_fixture_cases_passed: routeFixtures.length,
    booking_route_auxiliary_assertions_passed: 4,
    phone_correction_route_assertions_passed:5,phone_readback_fixtures_passed:6,
    phone_correction_route_audit_cases_passed:routeAuditFixtures.length,
    limitation: 'Exact transcript spelling of a scoped reference, not independent verification of audio, human hearing, or consent.' }, null, 2));
  process.exit(0);
}

if (modeArgs[0] === '--audit-existing') {
  process.exitCode = await auditExistingBooking() ? 0 : 1;
  // Let fetch/native handles finish cleanup before Node exits naturally.
} else {

// Reserving the report first prevents an accidental repeat from spending again.
const outputHandle = await fs.open(output, 'wx');
try {
  env = { ...parseEnv(await fs.readFile(path.join(root, '.env'), 'utf8')), ...process.env };
  for (const key of ['VAPI_API_KEY', 'VAPI_ASSISTANT_ID', 'ELEVENLABS_API_KEY', 'VAPI_ELEVENLABS_VOICE_ID'])
    if (!env[key]) throw new Error('configuration_missing');
  verificationRuntime = await preflightBookingRuntime({projectRoot:root, env});
  report.local_runtime_preflight = verificationRuntime.evidence;
  config = await (await request(local + '/api/config')).json();
  if (!config.live_voice_available || config.vapi_assistant_id !== env.VAPI_ASSISTANT_ID) throw new Error('live_configuration_mismatch');
  report.runtime_config = publicRuntimeConfig(config);
  report.runner_sha256 = hash(await fs.readFile(fileURLToPath(import.meta.url)));
  report.phrase_set_sha256 = hash(JSON.stringify({...PHRASES,request:INITIAL_REQUEST}));
  if(exercisePhoneMode)report.original_phrase_set_sha256=hash(JSON.stringify(PHRASES));
  // Establish that the verifier and server use the same database before even
  // generating paid caller TTS. A mismatch still ends this local session below.
  session = await (await request(local + '/api/sessions', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode: 'live' }) })).json();
  report.session_id = session.id;
  await snapshot();
  report.local_runtime_preflight.session_database_matched = verifyLocalSessionDatabase(verificationRuntime, root, session.id);
  const clips = {};
  for (const [name, text] of Object.entries(PHRASES)) {
    clips[name] = await callerPcm(name, name==='request'?INITIAL_REQUEST:text);
  }
  if(correctPhoneMode){clips.phone_correction=await callerPcm('phone_correction',PHONE_CORRECTION_PHRASE);phoneCorrectionMs=clips.phone_correction.length/32;}
  callStarted = clock();
  // Stop locally at145s, leaving bounded cleanup before the provider's150s cap.
  hardTimer = setTimeout(() => { life.abort(); controlSocketEnd(); }, 145_000);
  call = await (await request('https://api.vapi.ai/call', { method: 'POST', headers: { Authorization: 'Bearer ' + env.VAPI_API_KEY, 'Content-Type': 'application/json' }, body: JSON.stringify({
    assistantId: env.VAPI_ASSISTANT_ID,
    assistantOverrides: { maxDurationSeconds: 150, variableValues: { session_id: session.id, session_token: session.token, current_date: config.today, timezone: config.timezone } },
    transport: { provider: 'vapi.websocket', audioFormat: { format: 'pcm_s16le', container: 'raw', sampleRate: 16000 } },
  }) })).json();
  report.provider_call_created = true;
  report.call_id = call.id;
  mark('call_created');
  const transportUrl = new URL(call.transport?.websocketCallUrl);
  if (transportUrl.protocol !== 'wss:' || !transportUrl.hostname.endsWith('.vapi.ai')) throw new Error('unexpected_transport_url');
  socket = new WebSocket(transportUrl);
  socket.binaryType = 'arraybuffer';
  socket.addEventListener('error', () => { socketFault = true; });
  socket.addEventListener('close', () => { transportEnded = true; });
  socket.addEventListener('message', event => {
    if (typeof event.data !== 'string') {
      report.output_audio_bytes += event.data.byteLength;
      const samples = new DataView(event.data);
      let peak = 0;
      for (let offset = 0; offset + 1 < samples.byteLength; offset += 2) peak = Math.max(peak, Math.abs(samples.getInt16(offset, true)));
      // Vapi sends continuous PCM, including silence. Raw frame arrival cannot
      // demonstrate assistant speech or determine the end of a readback.
      if (peak > 128) {
        report.output_voiced_frames += 1; report.output_voiced_audio_bytes += samples.byteLength; lastVoicedAudio = clock();
        const turn = report.input_turns.at(-1);
        if (!inputPlaying && turn?.end_ms !== undefined && turn.first_voiced_response_ms === undefined) {
          turn.first_voiced_response_ms = Math.round(lastVoicedAudio - callStarted);
          turn.client_pcm_end_to_first_voiced_ms = turn.first_voiced_response_ms - turn.end_ms;
        }
      }
      return;
    }
    if (++messageCount > 20_000 || event.data.length > 256 * 1024) { life.abort(); controlSocketEnd(); return; }
    try {
      const message = JSON.parse(event.data);
      if(phoneCaptions?.observe(message))lastAssistantTextAt=clock();
      const type = ['speech-update', 'transcript', 'status-update', 'conversation-update', 'model-output', 'tool-calls', 'user-interrupted',
        'metadata', 'hang', 'assistant.started', 'end-of-call-report', 'function-call'].includes(message.type) ? message.type : 'other';
      report.messages_by_type[type] = (report.messages_by_type[type] || 0) + 1;
      if (message.type === 'speech-update' && message.role === 'assistant') {
        if (message.status === 'started') { assistantSpeaking = true; lastAssistantStart = clock(); }
        if (message.status === 'stopped') { assistantSpeaking = false; lastAssistantStop = clock(); }
        if (['started', 'stopped'].includes(message.status)) recordProviderEvent({ type: 'speech-update', role: 'assistant', status: message.status });
      }
      if (message.type === 'transcript' && ['user', 'assistant'].includes(message.role) && message.transcriptType === 'final' && typeof message.transcript === 'string')
        recordProviderEvent({ type: 'transcript-final', role: message.role, text_characters: message.transcript.length, text_sha256: hash(message.transcript) });
      if (message.type === 'transcript' && message.role === 'assistant' && message.transcriptType === 'final' && typeof message.transcript === 'string') {
        assistantFinals.push({ at: clock(), text: message.transcript.slice(0, 8000) });
        lastAssistantTextAt = clock();
        if (assistantFinals.length > 60) assistantFinals.shift();
      }
      if (message.type === 'status-update' && message.status === 'ended') { transportEnded = true; providerEndedEvent = true; }
    } catch { report.messages_by_type.unparsed = (report.messages_by_type.unparsed || 0) + 1; }
  });
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('websocket_open_timeout')), 12_000);
    socket.addEventListener('open', () => { clearTimeout(timer); resolve(); }, { once: true });
    socket.addEventListener('error', () => { clearTimeout(timer); reject(new Error('websocket_error')); }, { once: true });
  });
  // Vapi's documented microphone example sends every audio callback, including
  // silence. Nova endpointing uses silence in incoming audio. Missing packets
  // are not a substitute; keep media flowing without overlapping caller speech.
  const silence = new Uint8Array(640);
  silenceTimer = setInterval(() => {
    if (inputPlaying || life.signal.aborted || socket.readyState !== WebSocket.OPEN) return;
    if (socket.bufferedAmount > 128_000) { report.audio_backpressure_detected = true; socketFault = true; controlSocketEnd(); return; }
    try { socket.send(silence); report.input_silence_bytes += silence.length; }
    catch { socketFault = true; controlSocketEnd(); }
  }, 20);
  if (!await waitForAssistant(callStarted, 20, () => /\bservice visit\b/.test(words(observedAssistantText(callStarted))))) throw new Error('complete_assistant_greeting_not_observed');
  mark('greeting_completed');
  await play('request', clips.request);
  for (let attempt = 0; attempt <= 2; attempt++) {
    const decision = await waitForBookingDecision();
    if (decision.kind === 'ready') {
      selectedProposal = decision.proposal;
      selectedSlot = decision.slot;
      report.proposal_id = selectedProposal.id;
      report.selected_slot_id = selectedSlot.id;
      report.expected_readback = selectedProposal.summary;
      mark('proposal_readback_completed');
      break;
    }
    if (attempt === 2) throw new Error('exact_proposal_readback_not_verified');
    const clarification = decision.kind;
    if(clarification==='phone_correction'){
      const evidence=durableEvidence(state.proposal.id);
      report.phone_recovery.original_proposal={id:state.proposal.id,summary:state.proposal.summary,phone:state.customer.phone,slot_id:state.selected_slot};
      report.phone_recovery.original_readback_checks={...decision.checks};
      report.phone_recovery.before_correction_evidence=evidence;
      report.phone_recovery.status='correction_not_started';
      phoneGuard.begin(state,decision.checks,evidence,
        {complete:speechFinished(latestInputEnd),clarificationsUsed:attempt,
          remainingMs:callStarted+105000-clock(),correctionMs:phoneCorrectionMs});
      report.phone_recovery.no_appointment_before_correction=true;
      report.phone_recovery.status='correction_requested';
    }
    await play(clarification, clips[clarification]);
  }
  // A fresh snapshot immediately before consent prevents confirming a stale
  // proposal or overlooking an already committed booking between polls.
  await snapshot();
  if (state.proposal?.id !== selectedProposal.id || state.proposal.summary !== selectedProposal.summary || Date.parse(selectedProposal.expires_at) <= Date.now()) throw new Error('proposal_changed_before_consent');
  if (state.service !== 'oil_change' || words(state.customer?.name) !== 'alex morgan' || state.customer?.phone !== '+15125550142' || vehicleWords(state.customer?.vehicle) !== '2021 toyota camry') throw new Error('proposal_customer_details_mismatch');
  if (!speechFinished(latestInputEnd)) throw new Error('readback_not_finished_before_consent');
  report.preconsent_evidence = durableEvidence();
  if (report.preconsent_evidence.appointment_count !== 0 || Object.values(report.preconsent_evidence.committed_actions || {}).some(count => count !== 0)) throw new Error('persisted_booking_preceded_consent');
  await snapshot();
  if (state.proposal?.id !== selectedProposal.id || state.proposal.summary !== selectedProposal.summary || !speechFinished(latestInputEnd)) throw new Error('proposal_or_speech_changed_before_consent');
  phoneGuard?.assertConsent(state,{correctionSent:report.phone_recovery?.correction_sent});
  report.no_appointment_before_consent = true;
  consentTranscriptBaseline = state.transcript.length;
  const beforeConfirmationAudio = report.output_audio_bytes;
  const beforeConfirmationVoicedAudio = report.output_voiced_audio_bytes;
  await play('consent', clips.consent);
  const consentEnd = latestInputEnd;
  if (!await waitForAssistant(consentEnd, 35, () => {
    if (state.appointment?.status !== 'confirmed') return false;
    const evidence = referenceEvidence(observedAssistantText(consentEnd, consentTranscriptBaseline), state.appointment.reference);
    report.final_transcript_reference = evidence;
    return referenceMatched(evidence);
  })) throw new Error('confirmed_appointment_and_complete_reference_reply_not_observed');
  mark('confirmation_reply_completed');
  if (state.appointment.service !== 'oil_change' || state.appointment.slot_id !== selectedSlot.id || words(state.appointment.customer_name) !== 'alex morgan' || vehicleWords(state.appointment.vehicle) !== '2021 toyota camry') throw new Error('confirmed_appointment_details_mismatch');
  report.appointment_id = state.appointment.id;
  report.appointment_reference = state.appointment.reference;
  report.final_transcript_reference = referenceEvidence(observedAssistantText(consentEnd, consentTranscriptBaseline), state.appointment.reference);
  report.output_after_consent_bytes = report.output_audio_bytes - beforeConfirmationAudio;
  report.voiced_output_after_consent_bytes = report.output_voiced_audio_bytes - beforeConfirmationVoicedAudio;
  report.local_tool_names = state.events.map(event => TOOL_NAMES.has(event.name) ? event.name : 'other');
  report.booking_tool_route = bookingToolRoute(state.events,Boolean(phoneGuard?.verifiedProposal));
  report.final_evidence = durableEvidence();
  report.booking_smoke_passed = !preconsentViolation && report.no_appointment_before_consent
    && report.final_evidence.appointment_count === 1 && report.final_evidence.appointments_by_current_status?.confirmed === 1
    && report.final_evidence.committed_actions?.book === 1 && Object.values(report.final_evidence.committed_actions).reduce((sum, count) => sum + count, 0) === 1
    && report.voiced_output_after_consent_bytes > 0
    && referenceMatched(report.final_transcript_reference)
    && report.booking_tool_route.exact_core_sequence;
  report.appointment_retained = true;
  if (!report.booking_smoke_passed) report.failure = 'booking_confirmation_evidence_incomplete';
} catch (error) {
  const code = String(error?.message || 'unknown');
  report.failure = /^[a-zA-Z0-9_]+$/.test(code) ? code : 'request_or_transport_failed';
  report.booking_smoke_passed = false;
} finally {
  clearTimeout(hardTimer);
  clearInterval(silenceTimer);
  controlSocketEnd();
  // If WebSocket setup failed, use the documented call monitor endpoint when
  // supplied. Neither its capability URL nor authorization enters the report.
  if (call && !report.end_control_sent && !providerEndedEvent && call.monitor?.controlUrl) {
    try {
      const url = new URL(call.monitor.controlUrl);
      if (url.protocol !== 'https:' || !url.hostname.endsWith('.vapi.ai')) throw new Error('unexpected_control_url');
      await request(url, { method: 'POST', headers: { 'Content-Type': 'application/json', ...(config?.vapi_public_key ? { Authorization: 'Bearer ' + config.vapi_public_key } : {}) }, body: JSON.stringify({ type: 'end-call' }) }, true);
      report.end_control_sent = true;
    } catch { report.end_control_send_failed = true; }
  }
  if (session) {
    try {
      await request(`${local}/api/sessions/${session.id}/end`, { method: 'POST', headers: { 'x-session-token': session.token } }, true);
      report.local_session_ended = true;
      await snapshot(true);
      report.local_tool_names = state.events.map(event => TOOL_NAMES.has(event.name) ? event.name : 'other');
      report.booking_tool_route = bookingToolRoute(state.events,Boolean(phoneGuard?.verifiedProposal));
      report.pending_operations_at_end = state.pending_operations?.length || 0;
    } catch { report.final_local_evidence_unavailable = true; }
    report.appointment_retained = !!state?.appointment;
    if (call) {
      try { report.final_evidence = durableEvidence(); }
      catch { report.final_durable_evidence_unavailable = true; report.booking_smoke_passed = false; }
      try {
        const value = await boundedJson(await request(`${local}/api/sessions/${session.id}/gateway-timing`,
          { headers: { 'x-session-token': session.token } }, true), 1024 * 1024);
        report.gateway_timing = sanitizeGatewayTiming(value);
      } catch { report.gateway_timing = { available: false }; }
    }
  }
  report.no_appointment_before_consent = report.no_appointment_before_consent && !preconsentViolation;
  report.provider_ended_event_observed = providerEndedEvent;
  report.websocket_closed_observed = transportEnded;
  report.finished_at = new Date().toISOString();
  report.call_observation_seconds = callStarted ? Math.round((clock() - callStarted) / 10) / 100 : 0;
  if (!report.local_session_ended || report.pending_operations_at_end > 0 || (call && !report.end_control_sent && !providerEndedEvent)) report.booking_smoke_passed = false;
  if (!report.booking_tool_route?.exact_core_sequence) report.booking_smoke_passed = false;
  if (report.booking_smoke_passed && (report.final_evidence.appointment_count !== 1 || report.final_evidence.appointments_by_current_status?.confirmed !== 1
      || report.final_evidence.committed_actions?.book !== 1 || Object.values(report.final_evidence.committed_actions).reduce((sum, count) => sum + count, 0) !== 1)) report.booking_smoke_passed = false;
  if(correctPhoneMode){
    report.phone_recovery.passed=Boolean(report.booking_smoke_passed&&report.phone_recovery.correction_sent&&phoneGuard.verifiedProposal);
    if(report.phone_recovery.passed)report.phone_recovery.status='recovered_and_booked';
    else if(phoneGuard.attempted)report.phone_recovery.status='recovery_incomplete';
    report.phone_recovery.pending_assistant_partial_at_end=phoneCaptions.pendingPartial;
    report.phone_recovery.assistant_caption_event_counts={partial:phoneCaptions.partialCount,final:phoneCaptions.finalCaptions.length};
    if(exercisePhoneMode&&!report.phone_recovery.passed)report.booking_smoke_passed=false;
  }
  // Exactly one read after media/local cleanup. This cannot extend the live call
  // or silently rerun it; final provider artifacts may still be unavailable.
  if (call && session) {
    try {
      const response = await request(`https://api.vapi.ai/call/${encodeURIComponent(call.id)}`,
        { headers: { Authorization: 'Bearer ' + env.VAPI_API_KEY } }, true);
      report.provider_evidence = sanitizeProviderEvidence(await boundedJson(response, 12 * 1024 * 1024));
    } catch { report.provider_evidence = { available: false, reason: 'single_post_cleanup_read_unavailable' }; }
  }
  let serialized = JSON.stringify(report, null, 2) + '\n';
  for (const [key, value] of Object.entries(env || {}))
    if (typeof value === 'string' && value.length >= 8 && /KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL/.test(key)) serialized = serialized.replaceAll(value, '[REDACTED]');
  if (session?.token) serialized = serialized.replaceAll(session.token, '[REDACTED]');
  await outputHandle.writeFile(serialized);
  await outputHandle.close();
  console.log(serialized.trimEnd());
}
process.exitCode = report.booking_smoke_passed ? 0 : 1;
}
