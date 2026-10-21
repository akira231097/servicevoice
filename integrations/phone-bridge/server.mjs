// Isolated opt-in bridge. Saved numbers and assistants are never modified here.
import http from 'node:http';
import net from 'node:net';
import { timingSafeEqual, createHash } from 'node:crypto';
import { DuplexSession } from '../../app/static/duplex-session.js';
import { PhoneAdmissions, AudioSocketParser, PhoneMicrophone, PhonePlayback,
  PhoneOutputClock, PhoneDownsampler, audioSocketFrame } from './media.mjs';

const same = (a, b) => {
  if (typeof a !== 'string' || typeof b !== 'string') return false;
  const left = Buffer.from(a), right = Buffer.from(b);
  return left.length === right.length && timingSafeEqual(left, right);
};
const callKey = value => createHash('sha256').update(value).digest('hex').slice(0, 16);
const loopback = address => ['127.0.0.1', '::1', '::ffff:127.0.0.1'].includes(address);

export function validateBridgeConfig(value) {
  if (!value || value.enabled !== true || typeof value.webhookSecret !== 'string' || value.webhookSecret.length < 32
    || typeof value.vapiKey !== 'string' || !value.vapiKey || !/^[a-z0-9.-]+$/.test(value.sipHost || '')
    || /^(?:localhost|127\.|0\.)/.test(value.sipHost) || !Number.isInteger(value.sipPort) || value.sipPort !== 5061
    || ![8000, 16000].includes(value.audioSampleRate ?? 8000)) throw new Error('phone_bridge_configuration');
  const consoleUrl = new URL(value.consoleUrl);
  if (consoleUrl.protocol !== 'http:' || !['127.0.0.1', '[::1]'].includes(consoleUrl.hostname)
    || consoleUrl.username || consoleUrl.password || consoleUrl.pathname !== '/' || consoleUrl.search || consoleUrl.hash) throw new Error('phone_console_not_loopback');
  return { ...value, consoleUrl: consoleUrl.origin, audioSampleRate: value.audioSampleRate ?? 8000 };
}

export async function verifyOriginalCall(call, config, fetcher = fetch) {
  const response = await fetcher('https://api.vapi.ai/call/' + encodeURIComponent(call.id), {
    headers: { Authorization: 'Bearer ' + config.vapiKey }, signal: AbortSignal.timeout(2000), redirect: 'error',
  });
  if (!response.ok) throw new Error('phone_provider_verification');
  const row = await response.json();
  if (row.id !== call.id || row.type !== 'inboundPhoneCall' || row.orgId !== config.organizationId
    || row.phoneNumberId !== config.phoneNumberId || row.status === 'ended' || row.endedAt) throw new Error('phone_provider_identity');
}

export async function stopOriginalCall(callId, config, fetcher = fetch) {
  const options = { headers: { Authorization: 'Bearer ' + config.vapiKey }, signal: AbortSignal.timeout(2000), redirect: 'error' };
  const response = await fetcher('https://api.vapi.ai/call/' + encodeURIComponent(callId), options);
  if (!response.ok) return false;
  const row = await response.json();
  if (row.id !== callId || row.type !== 'inboundPhoneCall' || row.orgId !== config.organizationId
    || row.phoneNumberId !== config.phoneNumberId) return false;
  if (row.status === 'ended') return true;
  let url;
  try { url = new URL(row.monitor?.controlUrl); } catch { return false; }
  if (url.protocol !== 'https:' || !url.hostname.endsWith('.vapi.ai') || url.username || url.password
    || url.port && url.port !== '443' || url.hash) return false;
  return (await fetcher(url, { ...options, headers: { ...options.headers, 'Content-Type': 'application/json' },
    method: 'POST', body: JSON.stringify({ type: 'end-call' }), signal: AbortSignal.timeout(2000) })).ok;
}

export function createPhoneBridge(input, { fetcher = fetch, Session = DuplexSession, onEvent = () => {} } = {}) {
  const config = validateBridgeConfig(input);
  const admissions = new PhoneAdmissions(config), sessions = new Map(), sockets = new Set(), owners = new Set();
  let closing = false;
  const emit = event => { try { onEvent(event); } catch { /* Content-free observation only. */ } };
  const prewarm = row => {
    const existing = sessions.get(row.token);
    if (existing) return existing;
    const owner = { row, closed: false, ready: false, socket: null, ticker: null, cleanup: null };
    owners.add(owner);
    sessions.set(row.token, owner);
    owner.stop = reason => {
      if (owner.closed) return owner.cleanup || Promise.resolve();
      owner.closed = true;
      clearTimeout(owner.expiry); clearTimeout(owner.startDeadline); clearTimeout(owner.ticker);
      owner.playback?.close(); owner.socket?.destroy();
      owner.cleanup = (async () => {
        try { await owner.session?.stop(reason); } catch { /* Continue exact owned cleanup. */ }
        let confirmed = false;
        try { confirmed = await stopOriginalCall(row.outerCallId, config, fetcher); } catch { /* Explicit unknown stop. */ }
        admissions.end(row); sessions.delete(row.token);
        owner.cleaned = true;
        if (owner.startSettled) owners.delete(owner);
        emit({ kind: 'phone_outer_stop', confirmed });
        emit({ kind: 'phone_media_ended', reason, outer_call_key: callKey(row.outerCallId) });
      })();
      return owner.cleanup;
    };
    owner.session = new Session({ callbacks: { event: event => {
      emit({ ...event, scope: 'phone_bridge_adapter' });
      if (event.kind === 'ended') void owner.stop('assistant_ended');
    } }, runtime: {
      AudioContext: PhoneOutputClock, getUserMedia: async () => ({ getTracks: () => [] }),
      fetch: (url, options = {}) => {
        // Preserve a late /call allocation response for exact cleanup. The
        // operator API has its own20s provider bound and durable no-replay ledger.
        const allocation = String(url).endsWith('/duplex/call');
        const signal = allocation ? options.signal : options.signal
          ? AbortSignal.any([options.signal, AbortSignal.timeout(5000)]) : AbortSignal.timeout(5000);
        return fetcher(new URL(url, config.consoleUrl), { ...options, signal });
      },
      microphoneFactory: options => (owner.microphone = new PhoneMicrophone({ ...options, inputSampleRate: config.audioSampleRate })),
      playbackFactory: options => (owner.playback = new PhonePlayback({ ...options, onEvent: emit,
        onOverflow: () => { void owner.stop('phone_output_bound'); } })),
    } });
    owner.expiry = setTimeout(() => {
      if (!owner.socket) void owner.stop('phone_unclaimed_expired');
    }, Math.max(0, row.expiresAt - admissions.now()));
    owner.started = (async () => {
      try {
        await owner.session.start();
        if (owner.closed) { await owner.session.stop('phone_closed_during_start'); return false; }
        if (!owner.session.active || !owner.playback || !owner.microphone) throw new Error('phone_session_not_ready');
        owner.ready = true; return true;
      } catch {
        void owner.stop('phone_session_start_failed'); return false;
      }
    })().finally(() => {
      owner.startSettled = true;
      if (owner.cleaned) owners.delete(owner);
    });
    // Original reservation owns the deadline; duplicate webhooks join it.
    owner.readyPromise = new Promise(resolve => {
      owner.startDeadline = setTimeout(() => { void owner.stop('phone_prewarm_deadline'); resolve(false); }, 5000);
      owner.started.then(ready => { clearTimeout(owner.startDeadline); resolve(ready); });
    });
    return owner;
  };

  const httpServer = http.createServer(async (request, response) => {
    const send = (status, body) => {
      if (response.destroyed) return;
      response.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
      response.end(JSON.stringify(body));
    };
    if (!loopback(request.socket.remoteAddress) || closing) return send(503, { error: 'phone_bridge_unavailable' });
    if (request.method === 'GET' && request.url === '/health') return send(200, {
      ready: true, owned_calls: sessions.size, active_media: [...sessions.values()].filter(owner => owner.socket).length,
      scope: 'isolated_phone_bridge_not_main_routing',
    });
    if (request.method !== 'POST' || request.url !== '/phone-bridge/ingress') return send(404, { error: 'not_found' });
    if (!same(request.headers.authorization, 'Bearer ' + config.webhookSecret)) return send(401, { error: 'phone_webhook_authentication' });
    const began = performance.now();
    let owner;
    try {
      const chunks = []; let total = 0;
      for await (const chunk of request) {
        total += chunk.length; if (total > 65536) throw new Error('phone_webhook_bound'); chunks.push(chunk);
      }
      const message = JSON.parse(Buffer.concat(chunks).toString('utf8'))?.message;
      if (message?.type !== 'assistant-request') return send(400, { error: 'phone_event_not_supported' });
      const call = message.call;
      if (!call || call.type !== 'inboundPhoneCall' || call.orgId !== config.organizationId
        || call.phoneNumberId !== config.phoneNumberId || !/^[0-9a-f-]{36}$/.test(call.id || '')) throw new Error('phone_identity_rejected');
      await verifyOriginalCall(call, config, fetcher);
      const row = admissions.issue(call);
      owner = prewarm(row);
      const remaining = 5000 - (performance.now() - began);
      if (remaining <= 0) throw new Error('phone_webhook_deadline');
      let timer;
      const ready = await Promise.race([owner.readyPromise, new Promise(resolve => {
        timer = setTimeout(() => resolve(false), Math.ceil(remaining));
      })]).finally(() => clearTimeout(timer));
      if (!ready || owner.closed || closing) throw new Error('phone_prewarm_not_ready');
      emit({ kind: 'phone_admission_issued', outer_call_key: callKey(row.outerCallId) });
      return send(200, { destination: { type: 'sip',
        sipUri: 'sip:' + row.token + '@' + config.sipHost + ':' + config.sipPort + ';transport=tls', message: '' } });
    } catch (error) {
      if (owner) void owner.stop('phone_admission_failed');
      const code = /^[a-z_]{1,60}$/.test(error?.message || '') ? error.message : 'phone_admission_failed';
      emit({ kind: 'phone_admission_rejected', reason: code }); return send(503, { error: code });
    }
  });
  httpServer.requestTimeout = 6000; httpServer.headersTimeout = 6000;

  const mediaServer = net.createServer(socket => {
    sockets.add(socket); socket.setNoDelay(true); socket.setTimeout(10000);
    let owner;
    const reject = () => { clearTimeout(identityTimer); socket.destroy(); sockets.delete(socket); };
    const identityTimer = setTimeout(reject, 2000);
    const downsample = new PhoneDownsampler(config.audioSampleRate);
    const parser = new AudioSocketParser({ sampleRate: config.audioSampleRate,
      onIdentity: token => {
        if (!loopback(socket.remoteAddress) || closing) return false;
        const candidate = sessions.get(token);
        if (!candidate?.ready || candidate.closed || candidate.socket) return false;
        try { admissions.consume(token); } catch { return false; }
        owner = candidate; owner.socket = socket;
        clearTimeout(identityTimer); clearTimeout(owner.expiry);
        try { admissions.bindInner(owner.row, owner.session.callId); } catch { void owner.stop('phone_inner_binding'); return false; }
        emit({ kind: 'phone_inner_bound', outer_call_key: callKey(owner.row.outerCallId), inner_call_key: callKey(owner.row.innerCallId) });
        let nextTick = performance.now() + 10;
        const tick = () => {
          if (owner.closed) return;
          const now = performance.now();
          if (now < nextTick) { owner.ticker = setTimeout(tick, Math.ceil(nextTick - now)); return; }
          if (now - nextTick > 100 || socket.writableLength > 640) { void owner.stop('phone_output_backpressure'); return; }
          const audio = downsample.push(owner.playback.render());
          socket.write(audioSocketFrame(config.audioSampleRate === 8000 ? 0x10 : 0x12, audio));
          nextTick += 10; owner.ticker = setTimeout(tick, Math.max(0, Math.ceil(nextTick - performance.now())));
        };
        owner.ticker = setTimeout(tick, 10); return true;
      },
      onAudio: audio => { if (owner && !owner.closed) owner.microphone.push(audio); },
      onHangup: () => { if (owner) void owner.stop('caller_hangup'); },
    });
    socket.on('data', data => {
      try { parser.push(data); } catch { if (owner) void owner.stop('phone_media_protocol'); else reject(); }
    });
    socket.on('timeout', () => { if (owner) void owner.stop('phone_media_timeout'); else reject(); });
    socket.on('error', () => { if (owner) void owner.stop('phone_media_error'); else reject(); });
    socket.on('close', () => { sockets.delete(socket); if (owner) void owner.stop('caller_hangup'); else reject(); });
    if (!loopback(socket.remoteAddress)) reject();
  });
  return { httpServer, mediaServer, admissions, sessions,
    async listen({ httpPort = 8790, mediaPort = 9019 } = {}) {
      await new Promise((resolve, reject) => { httpServer.once('error', reject); httpServer.listen(httpPort, '127.0.0.1', resolve); });
      try { await new Promise((resolve, reject) => { mediaServer.once('error', reject); mediaServer.listen(mediaPort, '127.0.0.1', resolve); }); }
      catch (error) { await new Promise(resolve => httpServer.close(resolve)); throw error; }
    },
    async close() {
      closing = true;
      const owned = [...owners];
      await Promise.allSettled(owned.map(owner => owner.stop('bridge_shutdown')));
      // In-flight allocation responses are still observed; no new allocation is retried.
      await Promise.allSettled(owned.map(owner => owner.started));
      for (const socket of sockets) socket.destroy();
      await Promise.all([httpServer, mediaServer].map(server => new Promise(resolve => server.close(resolve))));
    },
  };
}
