import { VoiceSession, preloadVoiceSdk } from './voice.js';
import { SnapshotChannel } from './snapshot.js';

const $ = id => document.getElementById(id);
const state = {
  config: null, session: null, snapshot: null, mode: 'rehearsal', busy: false,
  voice: null, voiceStatus: 'idle', voiceMessages: [], voiceMetrics: null,
  poll: null, refreshTimer: null, revision: 0, settling: false,
  transcriptFingerprint: '', sessionEnded: false, rendered: {}, timingExport: null,
};

const snapshots = new SnapshotChannel({
  read: (session, options) => request(sessionPath('', session), options, session),
  apply: snapshot => renderSnapshot(snapshot),
  onError: error => notice(error.message),
});

const friendlyTools = {
  get_dealership_info: 'Dealership information', search_availability: 'Availability checked',
  begin_service_request: 'Service and availability checked', get_session_context: 'Conversation reviewed',
  get_operation: 'Action status checked', dismiss_proposal: 'Proposed change withdrawn',
  prepare_booking: 'Booking prepared', confirm_action: 'Confirmation processed',
  prepare_first_available_booking: 'Earliest appointment prepared',
  get_appointment: 'Appointment found', prepare_reschedule: 'Change prepared',
  prepare_cancellation: 'Cancellation prepared', request_handoff: 'Advisor request',
  withdraw_handoff: 'Local advisor request withdrawn',
};

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = String(text);
  return element;
}

function notice(text) {
  $('notice-text').textContent = text;
  $('notice').hidden = !text;
}

function errorText(detail) {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) return detail.map(item => item.msg || 'Invalid request.').join(' ');
  return detail?.message || 'The request could not be completed. Please try again.';
}

async function request(path, options = {}, session = state.session) {
  const { timeoutMs = 20000, signal: externalSignal, ...fetchOptions } = options;
  const headers = { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers };
  if (session?.token) headers['X-Session-Token'] = session.token;
  const controller = new AbortController();
  const cancel = () => controller.abort();
  if (externalSignal?.aborted) controller.abort();
  externalSignal?.addEventListener('abort', cancel, { once: true });
  const timer = setTimeout(cancel, timeoutMs);
  try {
    const response = await fetch(path, { ...fetchOptions, headers, signal: controller.signal, cache: 'no-store' });
    let data;
    try { data = await response.json(); } catch { data = {}; }
    if (!response.ok) throw new Error(errorText(data.detail || data.error));
    return data;
  } catch (error) {
    if (externalSignal?.aborted) throw error;
    if (error.name === 'AbortError') throw new Error('The service desk took too long to respond. Check the connection and try again.');
    if (error instanceof TypeError) throw new Error('The local service desk is unavailable. Check that the server is running, then reload this page.');
    throw error;
  } finally { clearTimeout(timer); externalSignal?.removeEventListener('abort', cancel); }
}

function sessionPath(suffix = '', session = state.session) {
  return `/api/sessions/${encodeURIComponent(session.id)}${suffix}`;
}

function setBusy(busy) {
  state.busy = busy;
  if (state.mode === 'rehearsal' && state.session && !state.sessionEnded) {
    $('advisor-state').textContent = busy ? 'Checking the service desk…' : 'Ready to help · scripted rehearsal';
  }
  updateControls();
}

function updateControls() {
  const available = Boolean(state.config);
  const active = ['connecting', 'listening', 'thinking', 'speaking', 'muted', 'muted-speaking', 'reconnecting', 'ending'].includes(state.voiceStatus);
  const canChat = available && state.session && !state.busy && state.mode === 'rehearsal' && !state.sessionEnded;
  $('reset-button').disabled = !available || state.busy;
  $('rehearsal-mode').disabled = !available || state.busy;
  $('live-mode').disabled = !available || state.busy;
  $('message-input').disabled = !canChat;
  $('send-button').disabled = !canChat || !$('message-input').value.trim();
  $('sample-details').disabled = !canChat;
  document.querySelectorAll('[data-message], .slot-option, .result-actions button, #confirm-action').forEach(button => {
    button.disabled = !canChat;
  });
  $('start-voice').hidden = active;
  $('start-voice').disabled = !available || state.busy || state.settling || !state.config?.live_voice_available;
  $('end-button').hidden = !active;
  $('end-button').disabled = state.voiceStatus === 'ending';
  $('mute-button').hidden = !active || ['connecting', 'ending'].includes(state.voiceStatus);
  $('mute-button').disabled = !state.voice?.active;
  $('refresh-result').hidden = state.mode !== 'live' || !state.session;
  $('refresh-result').disabled = state.busy || state.settling;
  $('rehearsal-mode').classList.toggle('selected', state.mode === 'rehearsal');
  $('rehearsal-mode').setAttribute('aria-pressed', String(state.mode === 'rehearsal'));
  $('live-mode').classList.toggle('selected', state.mode === 'live');
  $('live-mode').setAttribute('aria-pressed', String(state.mode === 'live'));
}

function renderMode() {
  const live = state.mode === 'live';
  $('voice-panel').hidden = !live;
  $('rehearsal-controls').hidden = live;
  $('live-footer').hidden = !live;
  $('conversation-badge').textContent = live ? 'VAPI LIVE VOICE' : 'TEXT REHEARSAL';
  $('mode-description').textContent = live ? 'Natural voice conversation · powered by Vapi' : 'Scripted text rehearsal · no API key needed';
  $('mode-pill').textContent = live ? (state.config?.live_voice_available ? 'VOICE CONFIGURED' : 'SETUP REQUIRED') : 'LOCAL WORKFLOW';
  $('setup-list').replaceChildren();
  const reasons = state.config?.live_voice_missing || state.config?.voice_setup_required || [];
  if (live && !state.config?.live_voice_available) {
    $('voice-title').textContent = 'Your voice desk is almost ready.';
    $('voice-description').textContent = 'Configure Vapi to try the same service workflow by voice.';
    const items = Array.isArray(reasons) && reasons.length ? reasons : ['Vapi public key and assistant ID', 'A secure public webhook connected to this service desk'];
    items.forEach(item => $('setup-list').append(node('li', '', item)));
    $('setup-list').hidden = false;
    $('start-voice').textContent = 'Voice setup required';
    $('advisor-state').textContent = 'Voice setup required · rehearsal is available';
  } else {
    $('setup-list').hidden = true;
    setVoiceStatus(state.voiceStatus);
  }
  if (live && state.config?.live_voice_available) {
    // Fetch/parse only the local bundle. No media or provider call is opened.
    void preloadVoiceSdk().catch(() => {});
  }
  renderTranscript();
  updateControls();
}

function renderTranscript() {
  const messages = state.mode === 'live' ? state.voiceMessages : state.snapshot?.transcript || [];
  const fingerprint = JSON.stringify([state.mode, messages]);
  if (fingerprint === state.transcriptFingerprint) return;
  state.transcriptFingerprint = fingerprint;
  const container = $('transcript');
  const nearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 100;
  // Preserve existing nodes when messages are appended; screen readers announce
  // only the additions instead of replaying the entire log after each poll.
  let common = 0;
  for (const existing of container.querySelectorAll('.message')) {
    const message = messages[common];
    if (!message || existing.dataset.role !== message.role || existing.dataset.text !== message.text) break;
    common += 1;
  }
  const existingNodes = [...container.children];
  existingNodes.slice(common).forEach(child => child.remove());
  if (!messages.length) {
    container.replaceChildren();
    const empty = node('div', 'conversation-empty');
    empty.append(node('strong', '', state.mode === 'live' ? 'A conversation, at your pace.' : 'Your service desk is ready.'));
    empty.append(node('p', '', state.mode === 'live' ? 'Start a voice conversation to see live captions here.' : 'Choose a service or tell the advisor what you need.'));
    container.append(empty);
  } else {
    for (const message of messages.slice(common)) {
      const item = node('article', `message ${message.role === 'user' ? 'user' : 'assistant'}`);
      item.dataset.role = message.role;
      item.dataset.text = message.text;
      item.append(node('p', 'message-author', message.role === 'user' ? 'You' : 'Service advisor'));
      item.append(node('div', 'message-bubble', message.text));
      container.append(item);
    }
  }
  if (nearBottom || messages.length <= 2) container.scrollTop = container.scrollHeight;
}

function renderJourney(snapshot) {
  const stage = String(snapshot?.stage || '').toLowerCase();
  let step = 0;
  if (snapshot?.slots?.length || /slot|availability|time/.test(stage)) step = 1;
  if (snapshot?.customer || snapshot?.proposal || /customer|detail|confirm|review/.test(stage)) step = 2;
  if (snapshot?.appointment && !snapshot?.proposal) step = 3;
  for (const item of $('journey').children) {
    const index = Number(item.dataset.step);
    item.classList.toggle('active', index === step);
    item.classList.toggle('complete', index < step);
    item.querySelector('.step-number').textContent = index < step ? '✓' : String(index + 1);
    if (index === step) item.setAttribute('aria-current', 'step');
    else item.removeAttribute('aria-current');
  }
}

function renderSlots(snapshot) {
  const slots = snapshot?.slots || [];
  $('slots-count').textContent = slots.length;
  $('slots-description').textContent = slots.length ? (state.mode === 'rehearsal' ? 'Choose an option to continue.' : 'Tell the advisor which option you prefer.') : 'Times appear after an availability search.';
  const container = $('slots');
  container.replaceChildren();
  if (!slots.length) {
    const empty = node('div', 'empty-slot');
    const icon = node('span', 'calendar-icon', '▦');
    icon.setAttribute('aria-hidden', 'true');
    empty.append(icon, node('p', '', 'No times to display'), node('small', '', 'Ask for a service and a date.'));
    container.append(empty);
    return;
  }
  slots.forEach((slot, index) => {
    const button = node('button', 'slot-option');
    button.type = 'button';
    const details = node('div');
    const remainingLabel = slot.remaining === undefined ? 'Available' : `${slot.remaining} ${Number(slot.remaining) === 1 ? 'place' : 'places'} remaining`;
    details.append(node('strong', '', slot.label || formatDate(slot.start)), node('small', '', remainingLabel));
    button.append(node('span', 'slot-index', index + 1), details);
    button.setAttribute('aria-label', `Choose option ${index + 1}, ${slot.label || formatDate(slot.start)}`);
    button.addEventListener('click', () => sendMessage(String(index + 1)));
    button.disabled = state.mode !== 'rehearsal' || state.busy;
    container.append(button);
  });
}

function addDetail(list, label, value) {
  if (value === null || value === undefined || value === '') return;
  const row = node('div');
  row.append(node('dt', '', label), node('dd', '', value));
  list.append(row);
}

function renderHandoff(handoff, secondary = false) {
  const withdrawn = handoff.status === 'withdrawn';
  const saved = handoff.status === 'callback_requested';
  const section = node('section', secondary ? 'secondary-outcome' : '');
  section.append(node(secondary ? 'h4' : 'h3', '', withdrawn ? 'Local advisor request withdrawn.' : saved ? 'An advisor request is saved.' : 'Local advisor request record.'));
  const details = node('dl');
  addDetail(details, 'Reason', handoff.reason);
  addDetail(details, 'Status', withdrawn ? 'Withdrawn locally' : saved ? 'Saved locally' : 'Status unavailable');
  section.append(details, node('p', 'confirm-note', withdrawn
    ? 'The local record is retained for review. No external contact was made.'
    : 'No one has been contacted. This demo cannot transfer you or arrange a callback.'));
  return section;
}

function renderOutcome(snapshot) {
  const outcome = $('outcome');
  const card = outcome.closest('.card');
  const proposal = snapshot?.proposal;
  const appointment = snapshot?.appointment;
  const handoff = snapshot?.handoff;
  const operations = snapshot?.pending_operations;
  const pending = Array.isArray(operations) ? operations.length : Number(operations?.count ?? operations ?? 0);
  const hasResult = Boolean(appointment || handoff);
  card.classList.toggle('has-result', hasResult && !proposal && !pending);
  card.classList.toggle('has-proposal', Boolean(proposal) || pending > 0);
  outcome.replaceChildren();
  if (pending) {
    $('outcome-label').textContent = 'PROCESSING';
    outcome.append(node('div', 'outcome-icon', '⋯'), node('h3', '', 'Your request is processing.'), node('p', '', 'The result will appear here once the service desk verifies it. Please wait before repeating the action.'));
    if (appointment) {
      const details = node('dl');
      addDetail(details, 'Current record', appointment.reference);
      addDetail(details, 'Current status', appointment.status);
      outcome.append(details);
    }
  } else if (proposal) {
    $('outcome-label').textContent = 'YOUR CONFIRMATION';
    outcome.append(node('div', 'outcome-icon', '◇'), node('h3', '', 'Ready for your review.'), node('p', '', proposal.summary));
    const confirmation = node('p', 'confirm-note', state.mode === 'live' ? 'Tell the advisor “confirm” to approve these exact details.' : 'Confirm only if these details are correct.');
    outcome.append(confirmation);
    if (state.mode === 'rehearsal') {
      const confirm = node('button', 'button button-primary', proposal.kind === 'cancel' ? 'Confirm cancellation' : 'Confirm details');
      confirm.id = 'confirm-action';
      confirm.type = 'button';
      confirm.addEventListener('click', () => sendMessage('confirm'));
      outcome.append(confirm);
    }
  } else if (appointment) {
    const cancelled = /cancel/i.test(appointment.status);
    $('outcome-label').textContent = cancelled ? 'CANCELLATION SAVED' : 'VERIFIED RESULT';
    outcome.append(node('div', 'outcome-icon', cancelled ? '−' : '✓'), node('h3', '', cancelled ? 'Appointment cancelled.' : 'You’re on the calendar.'));
    if (appointment.reference) outcome.append(node('p', 'reference', appointment.reference));
    const details = node('dl');
    addDetail(details, 'Service', appointment.service_name);
    addDetail(details, 'When', appointment.label || formatDate(appointment.start));
    addDetail(details, 'Customer', appointment.customer_name);
    addDetail(details, 'Vehicle', appointment.vehicle);
    addDetail(details, 'Status', appointment.status);
    outcome.append(details);
    if (state.mode === 'rehearsal' && !cancelled) {
      const actions = node('div', 'result-actions');
      for (const [label, text] of [['Reschedule', 'reschedule'], ['Cancel', 'cancel']]) {
        const button = node('button', '', label);
        button.type = 'button';
        button.addEventListener('click', () => sendMessage(text));
        actions.append(button);
      }
      outcome.append(actions);
    }
  } else if (handoff) {
    $('outcome-label').textContent = handoff.status === 'withdrawn' ? 'LOCAL REQUEST WITHDRAWN' : 'LOCAL ADVISOR REQUEST';
    outcome.append(node('div', 'outcome-icon', handoff.status === 'withdrawn' ? '−' : '✓'));
  } else {
    $('outcome-label').textContent = 'AWAITING DETAILS';
    outcome.append(node('div', 'outcome-icon', '↗'), node('h3', '', 'Let’s make a plan.'), node('p', '', 'Your appointment or advisor request will appear here when it’s verified.'));
  }
  // Queue history is independent of scheduler state. It must never obscure a
  // later verified booking/cancellation or a still-pending confirmation.
  if (handoff) outcome.append(renderHandoff(handoff, Boolean(pending || proposal || appointment)));
}

function renderActivity(snapshot) {
  const events = snapshot?.events || [];
  $('activity-count').textContent = events.length;
  const container = $('activity');
  const opened = new Set([...container.querySelectorAll('details[open]')].map(item => item.dataset.id));
  container.replaceChildren();
  if (!events.length) {
    const empty = node('div', 'empty-activity');
    empty.append(node('span', '', '⌁'), node('p', '', 'No tools called yet.'), node('small', '', 'The advisor’s actions will appear here.'));
    container.append(empty);
    return;
  }
  [...events].reverse().forEach(event => {
    const failed = /error|fail|reject/i.test(event.status);
    const item = node('details', `tool-event${failed ? ' failed' : ''}`);
    item.dataset.id = String(event.id);
    item.open = opened.has(String(event.id));
    const summary = node('summary');
    const title = node('div');
    title.append(node('strong', '', friendlyTools[event.name] || String(event.name).replaceAll('_', ' ')));
    title.append(node('time', '', `${formatTime(event.at)} · ${event.status || 'completed'}`));
    summary.append(node('span', 'tool-dot', failed ? '!' : '✓'), title, node('span', 'tool-time', Number.isFinite(event.elapsed_ms) ? `${Math.round(event.elapsed_ms)} ms` : ''));
    item.append(summary);
    if (event.summary) item.append(node('p', '', typeof event.summary === 'string' ? event.summary : JSON.stringify(event.summary)));
    item.append(node('pre', '', `${event.name}\nEvent: ${event.id}\nStatus: ${event.status || 'completed'}`));
    container.append(item);
  });
}

function formatDate(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return String(value);
  return new Intl.DateTimeFormat('en-US', { timeZone: state.config?.timezone || 'America/Chicago', weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }).format(date);
}

function formatTime(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return '';
  return new Intl.DateTimeFormat('en-US', { timeZone: state.config?.timezone || 'America/Chicago', hour: 'numeric', minute: '2-digit', second: '2-digit' }).format(date);
}

function renderMetrics() {
  const metrics = state.snapshot?.metrics;
  const hasTools = Number(metrics?.tool_count) > 0;
  const p50 = hasTools && Number.isFinite(metrics.tool_p50_ms) ? Math.round(metrics.tool_p50_ms) : '—';
  const p95 = hasTools && Number.isFinite(metrics.tool_p95_ms) ? Math.round(metrics.tool_p95_ms) : '—';
  $('tool-latency').replaceChildren(document.createTextNode(`${p50} / ${p95}`), node('span', '', 'ms'));
  const voice = state.voiceMetrics;
  const gap50 = voice?.gap === null || !voice ? '—' : String(Math.round(voice.gap));
  const gap95 = voice?.gapP95 === null || !voice ? '—' : String(Math.round(voice.gapP95));
  $('voice-latency').replaceChildren(document.createTextNode(`${gap50} / ${gap95}`), node('span', '', 'ms'));
  $('call-duration').textContent = voice?.hasConnected ? `${Math.floor(voice.elapsed / 60000)}:${String(Math.floor(voice.elapsed / 1000) % 60).padStart(2, '0')}` : '—';
  $('interruptions').textContent = voice?.hasConnected ? String(voice.interruptions) : '—';
  $('metrics-note').textContent = voice?.hasConnected
    ? `Event gap p50/p95: ${voice.gapSamples} paired, ${voice.pendingTurns} pending, ${voice.abandonedTurns} unpaired turns. Excludes endpointing and playback delays; not acoustic latency. Interruptions are provider events.`
    : 'Voice measures populate only during a live call. No estimated scores.';
  renderExportControl();
}

const TIMING_CODES = new Set(`feature_disabled not_immediate_tool_result tool_batch_or_history_shape unsupported_tool confirmation_not_asserted unsuccessful_result presentation_missing_or_changed unsupported_language receipt_missing receipt_mismatch presentation_invalid inactive_session durable_result_mismatch newer_result unresolved_operation stale_proposal proposal_mismatch unsafe_display_text unsupported_phone_format appointment_changed handoff_changed verified_receipt state_unavailable read_capacity read_deadline not_first_user_turn input_or_language_bounds action_or_complex_request not_simple_faq_candidate eligible_text credential_missing router_closed classifier_not_ready active_or_unverified_state state_changed unknown_label verified_static_faq classification_deadline classifier_timeout classifier_transport_error classifier_model_mismatch classifier_invalid_response classifier_response_bounds classifier_inconsistent_confidence classifier_unavailable cancelled interrupted rejected capacity_rejected generation_conflict superseded superseded_after_release supersession_wait_expired upstream_unavailable completed stream_unavailable not_attempted budget_exhausted size_limit eof timeout read_error active_or_uncertain_state non_ascii_language language_uncertain intent_uncertain candidate_only availability_changed availability_incomplete issuance_context_missing issuance_context_changed`.split(' '));
const timingNumber = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
const timingCode = value => TIMING_CODES.has(value) ? value : null;
const timingHash = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value) ? value : null;
function numericFields(source, keys) {
  return Object.fromEntries(keys.map(key => [key, timingNumber(source?.[key])]));
}

function clientTimingSnapshot() {
  const voice = state.voiceMetrics;
  const samples = Array.isArray(voice?.gapSamplesMs) ? voice.gapSamplesMs : [];
  const retained = samples.slice(-2048).filter(value => timingNumber(value) !== null);
  return {
    voice: {
      kind: 'vapi_client_event_gap_proxy',
      definition: 'Browser user-speech-stopped event to SDK remote-audio detection. Excludes endpointing and speaker playback; not acoustic latency.',
      connectionDefinition: 'Browser connection duration includes microphone permission; excludes local session creation.',
      ...numericFields(voice, ['elapsed', 'connectionMs', 'gap', 'gapP95', 'gapMax', 'gapSamples', 'stoppedTurns', 'pendingTurns', 'abandonedTurns', 'interruptions', 'networkInterruptions']),
      hasConnected: voice?.hasConnected === true, speechEventsObserved: voice?.speechEventsObserved === true,
      gapSamplesMs: retained, samples_omitted: samples.length - retained.length,
    },
    tools: {
      count: timingNumber(state.snapshot?.metrics?.tool_count),
      p50_ms: timingNumber(state.snapshot?.metrics?.tool_p50_ms),
      p95_ms: timingNumber(state.snapshot?.metrics?.tool_p95_ms),
    },
  };
}

function gatewayTimingRow(row) {
  // This is an explicit projection, not a dump of transport/model data. Unknown
  // codes are omitted until reviewed; server prose and future fields stay out.
  return {
    request_id: typeof row.request_id === 'string' && /^[a-f0-9]{32}$/.test(row.request_id) ? row.request_id : null,
    call_key: row.call_key, started_at_unix: timingNumber(row.started_at_unix),
    route: ['gemini', 'deterministic_receipt', 'jev_static_faq'].includes(row.route) ? row.route : null,
    outcome: timingCode(row.outcome), narration_reason: timingCode(row.narration_reason),
    narrator_version: /^receipt-en-\d{4}-\d{2}-\d{2}-\d+$/.test(row.narrator_version) ? row.narrator_version : null,
    system_prompt_sha256: timingHash(row.system_prompt_sha256), source_result_sha256: timingHash(row.source_result_sha256),
    output_text_sha256: timingHash(row.output_text_sha256),
    output_text_source: ['verified_receipt', 'static_faq'].includes(row.output_text_source) ? row.output_text_source : null,
    jev_faq_version: /^jev-static-faq-\d{4}-\d{2}-\d{2}-\d+$/.test(row.jev_faq_version) ? row.jev_faq_version : null,
    jev_model: /^jev-\d+\.\d+\.\d+$/.test(row.jev_model) ? row.jev_model : null,
    jev_reason: timingCode(row.jev_reason),
    ...numericFields(row, ['jev_probability', 'jev_margin', 'jev_language_probability']),
    milestones_ms: numericFields(row.milestones_ms, ['preflight', 'upstream_headers', 'first_upstream_event', 'first_text', 'first_tool_fragment', 'upstream_done', 'tool_emitted', 'total']),
    durations_ms: numericFields(row.durations_ms, ['tool_signature_persist', 'upstream_eof_drain', 'jev_route', 'jev_classification']),
    eof_drain_outcome: timingCode(row.eof_drain_outcome),
    counts: numericFields(row.counts, ['history_messages', 'history_tool_results', 'upstream_events', 'text_chunks', 'text_utf8_bytes', 'tool_calls', 'tcp_connects', 'tls_handshakes', 'eof_tail_bytes', 'jev_requests']),
  };
}

async function associatedGatewayTiming(data, sid) {
  if (data?.kind !== 'gemini_gateway_timing' || data.session_id !== sid || !Array.isArray(data.requests) || data.requests.length > 256) return null;
  const callId = data.provider_call_id;
  if (callId === null) return data.requests.length ? null : { callId: null, status: 'unbound', requests: [] };
  if (typeof callId !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/.test(callId)) return null;
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(callId));
  const callKey = Array.from(new Uint8Array(digest), value => value.toString(16).padStart(2, '0')).join('').slice(0, 16);
  if (data.requests.some(row => !row || row.call_key !== callKey)) return null;
  return { callId, status: 'available', requests: data.requests.map(gatewayTimingRow) };
}

function renderExportControl() {
  $('export-metrics').disabled = !state.voiceMetrics?.hasConnected || !state.session || Boolean(state.timingExport);
  $('export-metrics').textContent = state.timingExport ? 'Collecting timing evidence…' : 'Export timing evidence';
}

function cancelTimingExport() {
  state.timingExport?.controller.abort();
  state.timingExport = null;
}

async function exportTimingEvidence() {
  if (!state.voiceMetrics?.hasConnected || !state.session || state.timingExport) return;
  const session = state.session;
  const operation = { revision: state.revision, controller: new AbortController() };
  state.timingExport = operation;
  const current = () => state.timingExport === operation && state.revision === operation.revision && state.session === session;
  const evidence = {
    schema: 'servicevoice-client-evidence-v2', captured_at: new Date().toISOString(),
    provider: 'Vapi', measured_audio_latency: false,
    association: { session_id: session.id, provider_call_id: null, status: 'unverified' },
    ...clientTimingSnapshot(),
    gateway: { status: 'unavailable', capacity: 256, requests: [] },
    limitations: [
      'Browser/tool metrics are captured at export click; gateway metrics are fetched afterward.',
      'Gateway timings contain finalized requests only, from a shared 256-entry process-memory ring. Earlier requests may be evicted; restart clears the ring.',
      'An empty gateway list does not prove that no generation occurred. This is not acoustic latency or booking proof.',
      'No transcript, customer details, credentials, network addresses, or acoustic recording are included.',
    ],
  };
  renderExportControl();
  try {
    let data;
    try {
      data = await request(sessionPath('/gateway-timing', session), { timeoutMs: 3000, signal: operation.controller.signal }, session);
    } catch {
      // Browser-only evidence remains useful after expiry/network failure, but
      // never infer a provider identity or serialize an error/provider payload.
    }
    if (!current()) return;
    if (data !== undefined) {
      let associated = null;
      try { associated = await associatedGatewayTiming(data, session.id); } catch { /* fail closed on identity verification */ }
      if (!current()) return;
      if (!associated) {
        notice('Timing evidence was not exported because its call identity could not be verified. Refresh the result and try again.');
        return;
      }
      evidence.association.provider_call_id = associated.callId;
      evidence.association.status = associated.callId ? 'verified_server_binding' : 'no_provider_binding';
      evidence.gateway.status = associated.status;
      evidence.gateway.requests = associated.requests;
    }
    evidence.completed_at = new Date().toISOString();
    const url = URL.createObjectURL(new Blob([JSON.stringify(evidence, null, 2)], { type: 'application/json' }));
    try {
      const link = document.createElement('a');
      link.href = url;
      link.download = `servicevoice-timing-${Date.now()}.json`;
      link.click();
    } finally { setTimeout(() => URL.revokeObjectURL(url), 1000); }
    if (evidence.gateway.status === 'unavailable') notice('Browser timing evidence was exported. Gateway timings and the provider call identity were unavailable; the file marks them as unverified.');
  } catch {
    if (current()) notice('The timing evidence file could not be saved. Try exporting again.');
  } finally {
    if (state.timingExport === operation) { state.timingExport = null; renderExportControl(); }
  }
}

function renderChanged(key, value, render) {
  const fingerprint = JSON.stringify(value);
  if (state.rendered[key] === fingerprint) return;
  state.rendered[key] = fingerprint;
  render();
}

function renderSnapshot(snapshot) {
  state.snapshot = snapshot;
  renderTranscript();
  renderChanged('journey', [snapshot?.stage, snapshot?.slots?.length, snapshot?.customer, snapshot?.proposal, snapshot?.appointment], () => renderJourney(snapshot));
  renderChanged('slots', [state.mode, snapshot?.slots], () => renderSlots(snapshot));
  renderChanged('outcome', [state.mode, snapshot?.proposal, snapshot?.appointment, snapshot?.handoff, snapshot?.pending_operations], () => renderOutcome(snapshot));
  renderChanged('activity', snapshot?.events, () => renderActivity(snapshot));
  renderMetrics();
  if (state.mode === 'rehearsal') {
    $('advisor-state').textContent = state.busy ? 'Checking the service desk…' : 'Ready to help · scripted rehearsal';
    const stage = String(snapshot?.stage || '');
    if (/customer|detail/.test(stage)) $('message-input').placeholder = 'Name, phone, year / make / model…';
    else if (/date/.test(stage)) $('message-input').placeholder = 'Try tomorrow, next Monday, or YYYY-MM-DD…';
    else if (/slot|time/.test(stage)) $('message-input').placeholder = 'Choose an option, such as 1…';
    else if (snapshot?.proposal) $('message-input').placeholder = 'Review the details, then type confirm…';
    else $('message-input').placeholder = 'Tell us what your vehicle needs…';
  }
  updateControls();
}

function refreshSnapshot(options = {}) {
  if (!state.session) return Promise.resolve(null);
  return snapshots.refresh(options);
}

function scheduleRefresh() {
  const revision = state.revision;
  clearTimeout(state.refreshTimer);
  state.refreshTimer = setTimeout(() => {
    if (state.revision === revision) void refreshSnapshot({ fresh: true });
  }, 150);
}

async function endApplicationSession(session = state.session, revision = state.revision) {
  if (!session) return;
  try { await request(sessionPath('/end', session), { method: 'POST', body: '{}', timeoutMs: 3000 }, session); }
  catch (error) { if (state.revision === revision) notice(`Audio is stopped. ${error.message}`); }
  if (state.session?.id === session.id) state.sessionEnded = true;
}

async function createSession(mode) {
  const session = await request('/api/sessions', { method: 'POST', body: JSON.stringify({ mode }) }, null);
  state.session = session;
  snapshots.bind(session);
  state.sessionEnded = false;
  state.snapshot = { ...session, transcript: mode === 'rehearsal' && session.greeting ? [{ role: 'assistant', text: session.greeting }] : [], events: [], slots: [] };
  renderSnapshot(state.snapshot);
  if (mode === 'rehearsal') await refreshSnapshot();
  return session;
}

async function newSession(mode = state.mode) {
  if (state.busy) return;
  setBusy(true);
  state.revision += 1;
  cancelTimingExport();
  snapshots.bind(null);
  notice('');
  try {
    clearInterval(state.poll);
    state.poll = null;
    clearTimeout(state.refreshTimer);
    if (state.voice && !state.voice.closed) await state.voice.stop('new-session');
    else if (state.session && !state.sessionEnded) await endApplicationSession();
    state.mode = mode;
    state.session = null;
    state.snapshot = null;
    state.voice = null;
    state.voiceStatus = 'idle';
    state.voiceMessages = [];
    state.voiceMetrics = null;
    state.sessionEnded = false;
    state.settling = false;
    state.rendered = {};
    state.transcriptFingerprint = '';
    $('message-input').value = '';
    $('message-input').style.height = '';
    $('live-caption').hidden = true;
    renderSnapshot(null);
    renderMode();
    if (mode === 'rehearsal') await createSession(mode);
  } catch (error) { notice(error.message); }
  finally { setBusy(false); }
}

async function sendMessage(text) {
  text = text.trim();
  if (!text || state.busy || !state.session || state.mode !== 'rehearsal' || state.sessionEnded) return;
  setBusy(true);
  notice('');
  $('advisor-state').textContent = 'Checking the service desk…';
  try {
    const snapshot = await request(sessionPath('/message'), { method: 'POST', body: JSON.stringify({ text }) });
    $('message-input').value = '';
    $('message-input').style.height = '';
    renderSnapshot(snapshot);
  } catch (error) { notice(error.message); }
  finally {
    setBusy(false);
    $('advisor-state').textContent = 'Ready to help · scripted rehearsal';
    $('message-input').focus({ preventScroll: true });
  }
}

function setVoiceStatus(status) {
  state.voiceStatus = status;
  const labels = {
    idle: ['Meet your voice advisor', 'Speak naturally, ask questions, and interrupt when you need to.', 'Ready when you are'],
    connecting: ['Connecting your conversation…', 'Allow microphone access in your browser when prompted.', 'Connecting to Vapi…'],
    listening: ['I’m listening.', 'Tell the advisor what your vehicle needs.', 'Microphone on · listening'],
    thinking: ['One moment…', 'The advisor is working on your request.', 'Considering your request'],
    reconnecting: ['Reconnecting your conversation…', 'Your network was interrupted. The voice connection is recovering automatically.', 'Connection interrupted · reconnecting'],
    speaking: ['Your advisor is speaking.', 'You can interrupt or ask a follow-up at any time.', 'Advisor speaking · microphone on'],
    muted: ['Your microphone is muted.', 'Unmute when you’re ready to continue.', 'Microphone muted'],
    'muted-speaking': ['Your advisor is speaking.', 'Your microphone is muted.', 'Advisor speaking · microphone muted'],
    ending: ['Ending your conversation…', 'Stopping the microphone and closing the voice connection…', 'Ending call · releasing microphone'],
    ended: ['Thanks for the conversation.', 'The conversation has ended. Any verified result appears below.', 'Call ended · microphone released'],
    'silence-timeout': ['The call ended after silence.', 'The call ended because no speech was detected. Any verified result appears below. You can start a new conversation when ready.', 'Call ended · microphone released'],
    error: ['Let’s try that again.', 'Resolve the issue above, then start a new voice conversation.', 'Disconnected · microphone released'],
  };
  const [title, description, advisor] = labels[status] || labels.idle;
  $('voice-title').textContent = title;
  $('voice-description').textContent = description;
  if (state.mode === 'live') $('advisor-state').textContent = advisor;
  $('start-voice').textContent = status === 'idle' ? 'Start voice conversation ↗' : 'Start a new voice conversation ↗';
  const muted = Boolean(state.voice?.muted) || status.startsWith('muted');
  $('mute-button').textContent = muted ? 'Unmute microphone' : 'Mute microphone';
  $('mute-button').setAttribute('aria-pressed', String(muted));
  $('voice-visual').classList.toggle('speaking', status.includes('speaking'));
  if (['ended', 'silence-timeout', 'error', 'idle'].includes(status)) $('live-caption').hidden = true;
  updateControls();
}

async function startVoice() {
  if (state.busy || !state.config?.live_voice_available || state.voice && !state.voice.closed) return;
  setBusy(true);
  notice('');
  const revision = ++state.revision;
  cancelTimingExport();
  snapshots.bind(null);
  const isCurrent = () => state.revision === revision;
  try {
    if (state.session && !state.sessionEnded) await endApplicationSession();
    if (!isCurrent()) return;
    state.voiceMessages = [];
    state.voiceMetrics = null;
    state.transcriptFingerprint = '';
    state.settling = false;
    state.rendered = {};
    const session = await createSession('live');
    const callbacks = {
      status: status => {
        if (!isCurrent()) return;
        setVoiceStatus(status);
        if (status === 'listening' && !state.poll) {
          state.poll = setInterval(() => { if (isCurrent()) void refreshSnapshot(); }, 2500);
        }
      },
      notice: text => { if (isCurrent()) notice(text); },
      volume: level => { if (isCurrent()) $('voice-visual').style.setProperty('--level', level); },
      metrics: metrics => { if (isCurrent()) { state.voiceMetrics = metrics; renderMetrics(); } },
      caption: text => { if (isCurrent()) { $('live-caption').textContent = text; $('live-caption').hidden = !text; } },
      transcript: message => {
        if (!isCurrent()) return;
        if (message.final) {
          // Final captions are displayed from the client; webhook transcripts
          // independently populate the backend’s audit trail.
          state.voiceMessages.push({ role: message.role, text: message.text });
          $('live-caption').hidden = true;
          renderTranscript();
        } else {
          $('live-caption').textContent = `${message.role === 'user' ? 'You' : 'Advisor'}: ${message.text}`;
          $('live-caption').hidden = false;
        }
      },
      refresh: () => { if (isCurrent()) scheduleRefresh(); },
      ended: async () => {
        if (isCurrent()) {
          clearInterval(state.poll);
          state.poll = null;
          clearTimeout(state.refreshTimer);
          snapshots.bind(session); // Cancel a slow poll before the bounded drain.
          state.settling = true;
          $('advisor-state').textContent = 'Microphone off · checking final results';
          updateControls();
        }
        // Closing the application session rejects new actions. Already-started
        // backend operations may complete; ended snapshots remain readable.
        const deadline = performance.now() + 10000;
        await endApplicationSession(session, revision);
        let pending = 0;
        let finalSnapshot = null;
        while (isCurrent() && performance.now() < deadline) {
          finalSnapshot = await refreshSnapshot({ fresh: true, timeoutMs: Math.min(2000, deadline - performance.now()), reportErrors: false });
          const operations = finalSnapshot?.pending_operations;
          pending = Array.isArray(operations) ? operations.length : Number(operations?.count ?? operations ?? 0);
          if (finalSnapshot && !pending) break;
          await new Promise(resolve => setTimeout(resolve, 400));
        }
        if (isCurrent()) {
          state.settling = false;
          $('advisor-state').textContent = pending || !finalSnapshot ? 'Microphone off · final result not yet verified' : 'Call ended · final service state checked';
          if (pending) notice('An operation is still processing. Its outcome is not yet confirmed. Use “Refresh result” to check the saved record before retrying.');
          else if (!finalSnapshot) notice('The final service state could not be checked. Use “Refresh result” before retrying an action.');
          updateControls();
        }
      },
    };
    const voice = new VoiceSession(state.config, session, callbacks);
    state.voice = voice;
    setBusy(false);
    await voice.start();
  } catch (error) {
    if (isCurrent()) { notice(error.message); setVoiceStatus('error'); }
  } finally { if (isCurrent()) setBusy(false); }
}

$('dismiss-notice').addEventListener('click', () => notice(''));
$('reset-button').addEventListener('click', () => { void newSession(); });
$('rehearsal-mode').addEventListener('click', () => { if (state.mode !== 'rehearsal') void newSession('rehearsal'); });
$('live-mode').addEventListener('click', () => { if (state.mode !== 'live') void newSession('live'); });
$('start-voice').addEventListener('click', () => { void startVoice(); });
$('end-button').addEventListener('click', () => { void state.voice?.stop(); });
$('refresh-result').addEventListener('click', () => { void refreshSnapshot({ fresh: true }); });
$('export-metrics').addEventListener('click', exportTimingEvidence);
$('mute-button').addEventListener('click', () => {
  try { state.voice?.setMuted(!state.voice.muted); }
  catch { notice('The microphone could not be toggled. End the call and reconnect.'); }
});
$('message-form').addEventListener('submit', event => {
  event.preventDefault();
  void sendMessage($('message-input').value);
});
$('message-input').addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    void sendMessage(event.currentTarget.value);
  }
});
$('message-input').addEventListener('input', event => {
  event.currentTarget.style.height = 'auto';
  event.currentTarget.style.height = `${Math.min(event.currentTarget.scrollHeight, 110)}px`;
  updateControls();
});
$('sample-details').addEventListener('click', () => {
  $('message-input').value = 'Alex Morgan, 5125550142, 2021 Toyota Camry';
  $('message-input').dispatchEvent(new Event('input'));
  $('message-input').focus();
});
document.querySelectorAll('[data-message]').forEach(button => button.addEventListener('click', () => { void sendMessage(button.dataset.message); }));
window.addEventListener('pagehide', () => {
  state.revision += 1; // Ignore late UI callbacks while navigating away.
  cancelTimingExport();
  clearInterval(state.poll);
  clearTimeout(state.refreshTimer);
  snapshots.bind(null);
  if (state.voice && !state.voice.closed) void state.voice.stop('pagehide');
  if (state.session && !state.sessionEnded) {
    // Keep tokens in headers, including on navigation. Never put them in a URL.
    void fetch(sessionPath('/end'), { method: 'POST', keepalive: true, headers: { 'X-Session-Token': state.session.token, 'Content-Type': 'application/json' }, body: '{}' }).catch(() => {});
  }
});

async function initialize() {
  try {
    state.config = await request('/api/config', {}, null);
    $('server-status').textContent = 'Service desk connected';
    $('server-dot').classList.add('ready');
    $('timezone').textContent = `${state.config.timezone || 'America/Chicago'} · local demo`;
    for (const limitation of state.config.limitations || []) $('limitations').append(node('li', '', limitation));
    renderMode();
    await newSession('rehearsal');
  } catch (error) {
    $('server-status').textContent = 'Service desk unavailable';
    $('server-dot').classList.add('error');
    $('timezone').textContent = 'Start the local server, then reload.';
    $('advisor-state').textContent = 'Connection required';
    notice(error.message);
  }
}

void initialize();
