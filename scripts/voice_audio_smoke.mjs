/** One bounded synthetic Vapi audio call. No microphone or phone number is used.
 * Runs only when explicitly invoked. Uses at most 75 seconds of Vapi call time.
 * Requires the deployed assistant/gateway to have passed backend checks first.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseEnv } from 'node:util';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const env = parseEnv(await fs.readFile(path.join(root, '.env'), 'utf8'));
const report = { kind: 'synthetic_audio_vapi_smoke', started_at: new Date().toISOString(),
  input_text: 'What time does your service department close on weekdays?',
  microphone_used: false, phone_number_used: false, provider_call_created: false,
  output_audio_bytes: 0, output_voiced_audio_bytes: 0, messages_by_type: {},
  voice_activity_rule: 'PCM16LE frame peak >128; transport activity heuristic, not speech recognition.',
  limitation: 'Synthetic audio transport test; does not prove a human heard audio or browser microphone usability.' };
const output = path.join(root, 'docs', 'voice-audio-smoke.json');
let session, socket, call, silenceTimer, playingInput = false, stopped = false;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const request = async (url, options = {}) => {
  const r = await fetch(url, { ...options, signal: AbortSignal.timeout(20_000) });
  if (!r.ok) throw new Error(`HTTP_${r.status}`);
  return r;
};
const local = 'http://127.0.0.1:8765';

try {
  for (const name of ['VAPI_API_KEY', 'VAPI_ASSISTANT_ID', 'ELEVENLABS_API_KEY', 'VAPI_ELEVENLABS_VOICE_ID'])
    if (!env[name]) throw new Error('configuration_missing');
  const config = await (await request(local + '/api/config')).json();
  // Generate synthetic caller audio before allocating a paid live call.
  const tts = await request(`https://api.elevenlabs.io/v1/text-to-speech/${encodeURIComponent(env.VAPI_ELEVENLABS_VOICE_ID)}?output_format=pcm_16000`, {
    method: 'POST', headers: { 'xi-api-key': env.ELEVENLABS_API_KEY, 'Content-Type': 'application/json' },
    body: JSON.stringify({ text: report.input_text, model_id: 'eleven_flash_v2_5' }),
  });
  const pcm = new Uint8Array(await tts.arrayBuffer());
  if (!pcm.length || pcm.length > 16_000 * 2 * 20 || pcm.length % 2) throw new Error('input_audio_invalid');
  report.input_audio_seconds = pcm.length / 32_000;
  session = await (await request(local + '/api/sessions', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode: 'live' }) })).json();
  call = await (await request('https://api.vapi.ai/call', { method: 'POST', headers: { Authorization: 'Bearer ' + env.VAPI_API_KEY, 'Content-Type': 'application/json' }, body: JSON.stringify({
    assistantId: env.VAPI_ASSISTANT_ID,
    assistantOverrides: { maxDurationSeconds: 75, variableValues: { session_id: session.id, session_token: session.token, current_date: config.today, timezone: config.timezone } },
    transport: { provider: 'vapi.websocket', audioFormat: { format: 'pcm_s16le', container: 'raw', sampleRate: 16000 } },
  }) })).json();
  report.provider_call_created = true;
  report.call_id = call.id;
  const wsUrl = new URL(call.transport?.websocketCallUrl);
  if (wsUrl.protocol !== 'wss:' || !wsUrl.hostname.endsWith('.vapi.ai')) throw new Error('unexpected_transport_url');
  let lastVoicedAudio = 0, openedAt = 0, assistantSpeaking = false, lastAssistantStop = 0;
  socket = new WebSocket(wsUrl);
  socket.binaryType = 'arraybuffer';
  socket.addEventListener('message', event => {
    if (typeof event.data !== 'string') {
      report.output_audio_bytes += event.data.byteLength;
      const samples = new DataView(event.data);
      let peak = 0;
      for (let offset = 0; offset + 1 < samples.byteLength; offset += 2)
        peak = Math.max(peak, Math.abs(samples.getInt16(offset, true)));
      // Vapi sends continuous PCM silence; frame arrival is not speech activity.
      if (peak > 128) { report.output_voiced_audio_bytes += samples.byteLength; lastVoicedAudio = performance.now(); }
    }
    else {
      try {
        const message = JSON.parse(event.data);
        const type = typeof message.type === 'string' && /^[a-z-]{1,60}$/.test(message.type) ? message.type : 'other';
        report.messages_by_type[type] = (report.messages_by_type[type] || 0) + 1;
        if (type === 'speech-update' && message.role === 'assistant') {
          if (message.status === 'started') assistantSpeaking = true;
          if (message.status === 'stopped') { assistantSpeaking = false; lastAssistantStop = performance.now(); }
        }
      } catch { report.messages_by_type.unparsed = (report.messages_by_type.unparsed || 0) + 1; }
    }
  });
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('websocket_open_timeout')), 15_000);
    socket.addEventListener('open', () => { clearTimeout(timer); openedAt = performance.now(); resolve(); }, { once: true });
    socket.addEventListener('error', () => { clearTimeout(timer); reject(new Error('websocket_error')); }, { once: true });
  });
  // Match a live microphone: keep sending silence between spoken phrases so
  // transcription can observe the pause. Missing packets are not PCM silence.
  const silence = new Uint8Array(640);
  silenceTimer = setInterval(() => {
    if (!playingInput && socket.readyState === WebSocket.OPEN && socket.bufferedAmount < 128_000)
      socket.send(silence);
  }, 20);
  // Wait for greeting audio then a pause; this is a transport heuristic only.
  while (performance.now() - openedAt < 15_000) {
    if (!assistantSpeaking && lastAssistantStop && lastVoicedAudio && performance.now() - lastVoicedAudio > 1200) break;
    await sleep(100);
  }
  const before = report.output_audio_bytes;
  const beforeVoiced = report.output_voiced_audio_bytes;
  const inputStart = performance.now();
  playingInput = true;
  for (let offset = 0; offset < pcm.length; offset += 640) {
    if (socket.readyState !== WebSocket.OPEN) throw new Error('transport_closed_during_input');
    socket.send(pcm.slice(offset, offset + 640));
    // Twenty-millisecond PCM frames are paced to real time.
    await sleep(Math.max(0, inputStart + (offset + 640) / 32 - performance.now()));
  }
  playingInput = false;
  const inputEnd = performance.now();
  let state;
  while (performance.now() - inputEnd < 35_000) {
    state = await (await request(`${local}/api/sessions/${session.id}`, { headers: { 'x-session-token': session.token } })).json();
    const userTurns = state.transcript.filter(t => t.role === 'user').length;
    const assistantTurns = state.transcript.filter(t => t.role === 'assistant').length;
    if (userTurns > 0 && assistantTurns >= 2 && !assistantSpeaking && lastAssistantStop > inputEnd
      && lastVoicedAudio > inputEnd && performance.now() - lastVoicedAudio > 1200) break;
    if (state.ended) break;
    await sleep(500);
  }
  report.output_after_input_bytes = report.output_audio_bytes - before;
  report.voiced_output_after_input_bytes = report.output_voiced_audio_bytes - beforeVoiced;
  report.local_user_transcript_observed = !!state?.transcript.some(t => t.role === 'user');
  report.local_assistant_turns = state?.transcript.filter(t => t.role === 'assistant').length || 0;
  report.local_tool_names = state?.events.map(e => e.name) || [];
  report.local_appointment_created = !!state?.appointment;
  report.transport_smoke_passed = report.voiced_output_after_input_bytes > 0 && lastVoicedAudio > inputEnd
    && !assistantSpeaking && lastAssistantStop > inputEnd && report.local_user_transcript_observed
    && report.local_tool_names.includes('get_dealership_info') && !report.local_appointment_created;
} catch (error) {
  const safe = String(error.message || 'unknown');
  report.failure = /^[a-zA-Z0-9_]+$/.test(safe) ? safe : 'request_or_transport_failed';
  report.transport_smoke_passed = false;
} finally {
  clearInterval(silenceTimer);
  if (socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ type: 'end-call' }));
    socket.close();
    stopped = true;
  }
  if (session) {
    try { await request(`${local}/api/sessions/${session.id}/end`, { method: 'POST', headers: { 'x-session-token': session.token } }); }
    catch { report.local_session_end_failed = true; }
  }
  report.end_control_sent = stopped;
  report.finished_at = new Date().toISOString();
  await fs.writeFile(output, JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify(report, null, 2));
}
process.exitCode = report.transport_smoke_passed ? 0 : 1;
