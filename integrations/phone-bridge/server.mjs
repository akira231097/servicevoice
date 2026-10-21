// Isolated opt-in bridge. Saved numbers and assistants are never modified here.
import http from 'node:http';
import net from 'node:net';
import fs from 'node:fs/promises';
import { timingSafeEqual, createHash } from 'node:crypto';
import { DuplexSession } from '../../app/static/duplex-session.js';
import { PhoneAdmissions, AudioSocketParser, PhoneMicrophone, PhonePlayback,
  PhoneOutputClock, PhoneDownsampler, audioSocketFrame } from './media.mjs';
import { PhoneOwnershipJournal } from './ownership.mjs';

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
  if (value.httpSocketPath !== undefined && value.httpSocketPath !== '/run/servicevoice-phone/ingress.sock') throw new Error('phone_unix_socket_scope');
  if (value.ownershipFile && !/^[0-9a-f-]{36}$/.test(value.assistantId || '')) throw new Error('phone_owned_assistant_required');
  const consoleUrl = new URL(value.consoleUrl);
  if (consoleUrl.protocol !== 'http:' || !['127.0.0.1', '[::1]'].includes(consoleUrl.hostname)
    || consoleUrl.username || consoleUrl.password || consoleUrl.pathname !== '/' || consoleUrl.search || consoleUrl.hash) throw new Error('phone_console_not_loopback');
  let admissionUrl;
  if (value.admissionUrl !== undefined) {
    admissionUrl = new URL(value.admissionUrl);
    if (admissionUrl.protocol !== 'http:' || !['127.0.0.1', '[::1]'].includes(admissionUrl.hostname)
      || admissionUrl.username || admissionUrl.password || admissionUrl.pathname !== '/' || admissionUrl.search || admissionUrl.hash
      || typeof value.admissionSecret !== 'string' || value.admissionSecret.length < 32
      || same(value.admissionSecret, value.webhookSecret) || !value.ownershipFile) throw new Error('phone_admission_configuration');
  } else if (value.admissionSecret !== undefined) throw new Error('phone_admission_configuration');
  return { ...value, consoleUrl: consoleUrl.origin, ...(admissionUrl ? { admissionUrl: admissionUrl.origin } : {}),
    audioSampleRate: value.audioSampleRate ?? 8000 };
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
  const stopped = await fetcher(url, { ...options, headers: { ...options.headers, 'Content-Type': 'application/json' },
    method: 'POST', body: JSON.stringify({ type: 'end-call' }), signal: AbortSignal.timeout(2000) });
  if (!stopped.ok) return false;
  // The control acknowledgment is not an ended-call proof.
  return providerEnded(callId, 'inboundPhoneCall', config, fetcher);
}

async function providerEnded(callId, type, config, fetcher) {
  const response = await fetcher('https://api.vapi.ai/call/' + encodeURIComponent(callId), {
    headers: { Authorization: 'Bearer ' + config.vapiKey }, signal: AbortSignal.timeout(2000), redirect: 'error',
  });
  if (!response.ok) return false;
  const row = await response.json();
  return row.id === callId && row.orgId === config.organizationId && row.type === type && row.status === 'ended'
    && (type === 'inboundPhoneCall' ? row.phoneNumberId === config.phoneNumberId : row.assistantId === config.assistantId);
}

export function createPhoneBridge(input, { fetcher = fetch, Session = DuplexSession, onEvent = () => {} } = {}) {
  const config = validateBridgeConfig(input);
  const admissions = new PhoneAdmissions(config), sessions = new Map(), sockets = new Set(), owners = new Set();
  const journal = config.ownershipFile ? new PhoneOwnershipJournal(config.ownershipFile) : null;
  let closing = false, unixIngress = false, drainToken = null;
  const emit = event => { try { onEvent(event); } catch { /* Content-free observation only. */ } };
  const admissionBlocked = () => drainToken !== null || [...owners].some(owner => owner.closed);
  const prewarm = row => {
    const existing = sessions.get(row.token);
    if (existing) return existing;
    const owner = { row, closed: false, ready: false, socket: null, ticker: null, cleanup: null };
    journal?.reserve(row.outerCallId);
    owners.add(owner);
    sessions.set(row.token, owner);
    const backend = async (action, body = {}) => {
      const route = action === 'admit' ? '/api/phone-bridge/admit'
        : '/api/phone-bridge/' + encodeURIComponent(row.outerCallId) + '/' + action;
      const response = await fetcher(new URL(route, config.admissionUrl), { method: 'POST', redirect: 'error',
        headers: { Authorization: 'Bearer ' + config.admissionSecret, 'Content-Type': 'application/json' },
        body: JSON.stringify(body), signal: AbortSignal.timeout(5000) });
      if (!response.ok) throw new Error('phone_backend_' + action + '_failed');
      return response.json();
    };
    owner.reconcile = () => {
      if (!owner.startSettled || !owner.cleaned) return Promise.resolve();
      if (owner.verified) return Promise.resolve();
      if (owner.reconciling) return owner.reconciling;
      owner.reconcileDeadline ??= performance.now() + 30000;
      owner.reconciling = (async () => {
        let outerEnded = false, innerEnded = !owner.allocationAttempted;
        const inner = owner.allocatedCallId || owner.session.callId;
        for (let attempt = 0; attempt < 2; attempt++) {
          try { outerEnded = await providerEnded(row.outerCallId, 'inboundPhoneCall', config, fetcher); } catch { /* Unknown stays owned. */ }
          if (owner.allocationAttempted && inner) {
            try { innerEnded = await providerEnded(inner, 'webCall', config, fetcher); } catch { /* Unknown stays owned. */ }
          }
          if (outerEnded && innerEnded) break;
          if (attempt === 0) await new Promise(resolve => setTimeout(resolve, 200));
        }
        let backendClosed = !config.admissionUrl;
        if (outerEnded && innerEnded && config.admissionUrl) {
          try {
            const closed = await backend('close');
            backendClosed = closed.closed === true && closed.outer_ended === true
              && (!owner.allocationAttempted || closed.inner_ended === true);
          } catch { /* Durable backend capacity stays held on an unknown result. */ }
        }
        if (outerEnded && innerEnded && backendClosed) {
          try { journal?.release(row.outerCallId, { outerEnded, innerEnded }); }
          catch { owner.unknown = true; emit({ kind: 'phone_ownership_unknown', reason: 'durable_release_failed' }); return; }
          admissions.confirmEnded(row); owners.delete(owner);
          owner.verified = true; owner.unknown = false;
          clearTimeout(owner.reconcileTimer); owner.reconcileTimer = null;
          emit({ kind: 'phone_ownership_ended', outer_verified: true, inner_verified: true });
        } else {
          owner.unknown = true;
          emit({ kind: 'phone_ownership_unknown', outer_verified: outerEnded, inner_verified: innerEnded,
            ...(config.admissionUrl ? { backend_closed: backendClosed } : {}) });
        }
      })().finally(() => {
        owner.reconciling = null;
        const retry = [500, 1000, 2000, 4000][owner.reconcileRetries || 0];
        // Reconcile only known saved identities. No allocation/stop is replayed.
        // Unknown allocation without an ID remains closed for exact discovery.
        if (!closing && !owner.verified && retry && (!owner.allocationAttempted || owner.allocatedCallId || owner.session.callId)
          && performance.now() + retry < owner.reconcileDeadline) {
          owner.reconcileRetries = (owner.reconcileRetries || 0) + 1;
          owner.reconcileTimer = setTimeout(() => { owner.reconcileTimer = null; void owner.reconcile(); }, retry);
          emit({ kind: 'phone_ownership_retry_scheduled', retry_after_ms: retry });
        }
      });
      return owner.reconciling;
    };
    owner.stop = reason => {
      if (owner.closed) return owner.cleanup || Promise.resolve();
      owner.closed = true;
      clearTimeout(owner.expiry); clearTimeout(owner.startDeadline); clearTimeout(owner.durationDeadline); clearTimeout(owner.ticker);
      owner.playback?.close(); owner.socket?.destroy();
      owner.cleanup = (async () => {
        try { await owner.session?.stop(reason); } catch { /* Continue exact owned cleanup. */ }
        let confirmed = false;
        try { confirmed = await stopOriginalCall(row.outerCallId, config, fetcher); } catch { /* Explicit unknown stop. */ }
        admissions.end(row); sessions.delete(row.token);
        owner.cleaned = true;
        await owner.reconcile();
        emit({ kind: 'phone_outer_stop', confirmed });
        emit({ kind: 'phone_media_ended', reason, outer_call_key: callKey(row.outerCallId) });
      })();
      return owner.cleanup;
    };
    owner.session = new Session({ callbacks: { event: event => {
      emit({ ...event, scope: 'phone_bridge_adapter' });
      // DuplexSession emits before assigning its asynchronous stop promise.
      // Join it after that synchronous stack, rather than reentering stop and
      // checking terminal ownership before its provider cleanup has begun.
      if (event.kind === 'ended') queueMicrotask(() => { void owner.stop('assistant_ended'); });
    } }, runtime: {
      AudioContext: PhoneOutputClock, getUserMedia: async () => ({ getTracks: () => [] }),
      fetch: async (url, options = {}) => {
        // Preserve a late /call allocation response for exact cleanup. The
        // operator API has its own20s provider bound and durable no-replay ledger.
        const allocation = String(url).endsWith('/duplex/call');
        if (String(url) === '/api/sessions' && config.admissionUrl) {
          const began = performance.now();
          owner.backendRequested = true;
          const admitted = await backend('admit', { outer_call_id: row.outerCallId });
          if (admitted.outer_call_id !== row.outerCallId || admitted.mode !== 'live' || admitted.allocation_state !== 'reserved'
            || typeof admitted.id !== 'string' || !admitted.id || typeof admitted.token !== 'string' || admitted.token.length < 32
            || admitted.max_duration_seconds !== 300 || !Number.isInteger(admitted.remaining_duration_ms)
            || admitted.remaining_duration_ms <= 0 || admitted.remaining_duration_ms > 300000) throw new Error('phone_backend_admission_shape');
          const remaining = admitted.remaining_duration_ms - (performance.now() - began);
          if (remaining <= 0 || owner.closed) throw new Error('phone_outer_duration_elapsed');
          // Subtract the ENTIRE admission trip from the returned remaining
          // lifetime, so neither readback nor a late connection rolls300s forward.
          owner.durationDeadline = setTimeout(() => { void owner.stop('phone_outer_duration_limit'); }, Math.ceil(remaining));
          emit({ kind: 'phone_backend_admitted', remaining_duration_ms: Math.floor(remaining) });
          return new Response(JSON.stringify(admitted), { status: 200 });
        }
        if (allocation) {
          journal?.allocating(row.outerCallId); owner.allocationAttempted = true;
          if (config.admissionUrl) {
            const reserved = await backend('allocation');
            if (reserved.allocation_reserved !== true || owner.closed) throw new Error('phone_backend_allocation_unconfirmed');
          }
        }
        const signal = allocation ? options.signal : options.signal
          ? AbortSignal.any([options.signal, AbortSignal.timeout(5000)]) : AbortSignal.timeout(5000);
        const response = await fetcher(new URL(url, config.consoleUrl), { ...options, signal });
        if (allocation && response.ok) {
          const result = await response.clone().json();
          if (typeof result.call_id === 'string') {
            // Retain the observed identity even if durable bind fails: that
            // failure must never make cleanup forget an already allocated leg.
            owner.allocatedCallId = result.call_id; owner.session.callId = result.call_id;
            journal?.allocated(row.outerCallId, result.call_id);
            // Retain returned owned identities BEFORE backend bind. If bind
            // fails, ordinary Session cleanup can still stop the paid leg.
            if (config.admissionUrl) {
              owner.session.callId = result.call_id;
              const transport = new URL(result.websocket_url);
              if (transport.protocol !== 'wss:' || !transport.hostname.endsWith('.vapi.ai') || transport.username || transport.password)
                throw new Error('phone_inner_transport_identity');
              owner.session.callUrl = result.websocket_url;
              const bound = await backend('bind', { inner_call_id: result.call_id });
              if (bound.bound !== true || owner.closed) throw new Error('phone_backend_bind_unconfirmed');
            }
          }
        }
        return response;
      },
      microphoneFactory: options => (owner.microphone = new PhoneMicrophone({ ...options, inputSampleRate: config.audioSampleRate,
        onOverflow: () => { void owner.stop('phone_input_jitter_bound'); } })),
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
    })().finally(async () => {
      owner.startSettled = true;
      await owner.reconcile();
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
    if ((!unixIngress && !loopback(request.socket.remoteAddress)) || closing) return send(503, { error: 'phone_bridge_unavailable' });
    if (request.method === 'GET' && request.url === '/health') return send(200, {
      ready: true, owned_calls: owners.size, active_media: [...sessions.values()].filter(owner => owner.socket).length,
      admission_paused: admissionBlocked(), ownership_unknown: [...owners].filter(owner => owner.unknown).length,
      admission_ledger_rows: admissions.calls.size, admission_ledger_retired: admissions.retired,
      scope: 'isolated_phone_bridge_not_main_routing',
    });
    const control = ['/control/drain', '/control/resume'].includes(request.url);
    if (request.method !== 'POST' || (request.url !== '/phone-bridge/ingress' && !control)) return send(404, { error: 'not_found' });
    if (!same(request.headers.authorization, 'Bearer ' + config.webhookSecret)) return send(401, { error: 'phone_webhook_authentication' });
    if (control) {
      // The public proxy exposes ingress only. Rotation controls require the
      // private Unix socket, bearer and an idempotent owner-held random token.
      if (!unixIngress) return send(404, { error: 'not_found' });
      try {
        let raw = ''; for await (const chunk of request) { raw += chunk; if (raw.length > 1024) throw new Error(); }
        const token = JSON.parse(raw).drain_token;
        if (!/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(token || '')) throw new Error();
        if (request.url === '/control/drain') {
          if (drainToken !== null && !same(drainToken, token)) return send(409, { error: 'phone_drain_owned' });
          drainToken = token;
        } else {
          if (!same(drainToken, token) || owners.size) return send(409, { error: 'phone_drain_not_idle_or_owned' });
          drainToken = null;
        }
        return send(200, { admission_paused: drainToken !== null, owned_calls: owners.size });
      } catch { return send(400, { error: 'phone_drain_token_required' }); }
    }
    if (admissionBlocked()) return send(503, { error: 'phone_admission_paused' });
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
      if (admissionBlocked()) throw new Error('phone_admission_paused');
      const row = admissions.issue(call);
      owner = prewarm(row);
      const remaining = 5000 - (performance.now() - began);
      if (remaining <= 0) throw new Error('phone_webhook_deadline');
      let timer;
      const ready = await Promise.race([owner.readyPromise, new Promise(resolve => {
        timer = setTimeout(() => resolve(false), Math.ceil(remaining));
      })]).finally(() => clearTimeout(timer));
      if (!ready || owner.closed || closing || admissionBlocked()) throw new Error('phone_prewarm_not_ready');
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
          owner.microphone.tick();
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
      if (config.httpSocketPath) {
        const parent = await fs.lstat('/run/servicevoice-phone');
        if (!parent.isDirectory() || parent.isSymbolicLink() || parent.mode & 0o077
          || parent.uid !== process.getuid?.()) throw new Error('phone_unix_directory_permissions');
        // Never unlink an unknown or live socket from an earlier process.
        await new Promise((resolve, reject) => {
          httpServer.once('error', reject); httpServer.listen(config.httpSocketPath, resolve);
        });
        unixIngress = true;
        try { await fs.chmod(config.httpSocketPath, 0o600); }
        catch (error) { await new Promise(resolve => httpServer.close(resolve)); throw error; }
      } else {
        await new Promise((resolve, reject) => { httpServer.once('error', reject); httpServer.listen(httpPort, '127.0.0.1', resolve); });
      }
      try { await new Promise((resolve, reject) => { mediaServer.once('error', reject); mediaServer.listen(mediaPort, '127.0.0.1', resolve); }); }
      catch (error) { await new Promise(resolve => httpServer.close(resolve)); throw error; }
    },
    async close() {
      closing = true;
      const owned = [...owners];
      for (const owner of owned) clearTimeout(owner.reconcileTimer);
      await Promise.allSettled(owned.map(owner => owner.stop('bridge_shutdown')));
      // In-flight allocation responses are still observed; no new allocation is retried.
      await Promise.allSettled(owned.map(owner => owner.started));
      await Promise.allSettled(owned.map(owner => owner.reconcile()));
      for (const socket of sockets) socket.destroy();
      await Promise.all([httpServer, mediaServer].map(server => new Promise(resolve => server.close(resolve))));
      journal?.close();
      return { provider_ownership_verified: owners.size === 0, ownership_unknown: owners.size };
    },
  };
}
