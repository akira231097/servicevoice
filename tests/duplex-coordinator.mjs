import assert from 'node:assert/strict';
import { test } from 'node:test';
import { DuplexCoordinator, PcmPlayback } from '../app/static/duplex-coordinator.js';

const flush = async () => { for (let count = 0; count < 8; count++) await Promise.resolve(); };
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function clock() {
  let now = 0, serial = 0;
  const timers = new Map();
  return {
    now: () => now,
    setTimeout(callback, delay) { const id = ++serial; timers.set(id, { callback, at: now + delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
    async advance(milliseconds) {
      const target = now + milliseconds;
      while (true) {
        const next = [...timers].filter(([, timer]) => timer.at <= target).sort((left, right) => left[1].at - right[1].at)[0];
        if (!next) break;
        now = next[1].at; timers.delete(next[0]); next[1].callback(); await flush();
      }
      now = target; await flush();
    },
    async fireNextEarly(milliseconds) {
      const next = [...timers].sort((left, right) => left[1].at - right[1].at)[0];
      assert.ok(next); now = next[1].at - milliseconds; timers.delete(next[0]);
      next[1].callback(); await flush();
    },
    get pending() { return timers.size; },
  };
}
function playback() {
  const calls = [], handles = [];
  return {
    calls, handles,
    playCue(phraseId) {
      calls.push(['cue', phraseId]);
      const done = deferred();
      const handle = { ended: done.promise, stopped: false, finish() { done.resolve(); },
        stop() { this.stopped = true; calls.push(['cue-stop', phraseId]); done.resolve(); } };
      handles.push(handle); return handle;
    },
    enqueueMain(value) { calls.push(['main', value.generationId]); return true; },
    stopMain() { calls.push(['main-stop']); },
    close() { calls.push(['close']); },
  };
}
function decision(phase, overrides = {}) {
  return { phrase_id: phase === 'listening' ? 'neutral_ack' : 'moment', confidence: .99,
    language_choice: 'cache_compatible', expires_in_ms: 1000, ...overrides };
}
function setup(options = {}) {
  const time = clock(), output = playback(), requests = [], events = [];
  const selectCue = async request => { requests.push(request); return decision(request.phase); };
  const coordinator = new DuplexCoordinator({ playback: output, selectCue, now: time.now,
    setTimeout: time.setTimeout, clearTimeout: time.clearTimeout, onEvent: event => events.push(event), ...options });
  return { coordinator, time, output, requests, events };
}
function caller(coordinator, text = 'I need routine service for my car') {
  coordinator.userSpeechStarted(); coordinator.updatePartial(text, { revision: 1 });
}
function waiting(coordinator, id = 'main-1') {
  caller(coordinator); coordinator.userSpeechStopped(); coordinator.beginGeneration(id);
}

test('remaining server TTL is not subtracted twice from a delayed HTTP selection', async () => {
  const done = deferred(), { coordinator, time, output } = setup({ selectCue: () => done.promise });
  waiting(coordinator); await time.advance(500);
  done.resolve(decision('waiting', { expires_in_ms: 480, max_request_age_ms: 800 })); await flush();
  assert.equal(output.handles.length, 0); assert.equal(coordinator.request.expiresAt, 800);
  await time.advance(50); assert.equal(output.handles.length, 1); coordinator.close();
});

test('an explicit TTL total cannot roll forward or exceed the absolute800ms client bound', async () => {
  const done = deferred(), { coordinator, time, output, events } = setup({ selectCue: () => done.promise });
  waiting(coordinator); await time.advance(801);
  done.resolve(decision('waiting', { expires_in_ms: 700, max_request_age_ms: 5000 })); await flush();
  assert.equal(output.handles.length, 0);
  assert.ok(events.some(x => x.reason === 'selection_expired_on_arrival')); coordinator.close();
});

test('server silence reasons survive as content-free suppression diagnostics', async () => {
  const { coordinator, time, events } = setup({ selectCue: async () => decision('waiting', {
    phrase_id: 'silence', reason: 'classification_uncertain' }) });
  waiting(coordinator); await time.advance(0);
  assert.ok(events.some(x => x.reason === 'classification_uncertain' && x.phase === 'waiting'));
  assert.ok(!JSON.stringify(events).includes('routine service')); coordinator.close();
});

test('only a fresh safe context transition may reopen a failed waiting context preflight', async () => {
  let count = 0;
  const { coordinator, time, output } = setup({ selectCue: async () => ++count === 1
    ? decision('waiting', { phrase_id: 'silence', reason: 'unverified_or_sensitive_context' }) : decision('waiting') });
  waiting(coordinator); await time.advance(1000); assert.equal(count, 1); assert.equal(output.handles.length, 0);
  coordinator.setSensitive(true); coordinator.recoverWaitingContext(); await time.advance(1000); assert.equal(count, 1);
  coordinator.setSensitive(false); coordinator.recoverWaitingContext(); await time.advance(0);
  assert.equal(count, 2); assert.equal(output.handles.length, 1);
  coordinator.recoverWaitingContext(); await time.advance(1000); assert.equal(count, 2); coordinator.close();
});

test('a safe context transition never retries ordinary uncertain silence', async () => {
  let count = 0;
  const { coordinator, time } = setup({ selectCue: async () => { count++; return decision('waiting', {
    phrase_id: 'silence', reason: 'classification_uncertain' }); } });
  waiting(coordinator); await time.advance(0); coordinator.setSensitive(true); coordinator.setSensitive(false);
  coordinator.recoverWaitingContext(); await time.advance(1000); assert.equal(count, 1); coordinator.close();
});

for (const reason of ['classification_throttled', 'cue_spacing']) {
  test(`one fresh waiting request can recover a preclassification ${reason} refusal`, async () => {
    const calls = [];
    const { coordinator, time, output, events } = setup({ selectCue: async request => {
      calls.push(request); return calls.length === 1 ? decision('waiting', {
        phrase_id: 'silence', reason, requests: 0, retry_after_ms: 120, expires_in_ms: 0 }) : decision('waiting');
    } });
    waiting(coordinator); await time.advance(0); assert.equal(calls.length, 1);
    await time.advance(349); assert.equal(calls.length, 1);
    await time.advance(1); assert.equal(calls.length, 2);
    await time.advance(200); assert.equal(output.handles.length, 1);
    assert.ok(events.some(row => row.kind === 'cue_retry_scheduled' && row.reason === reason));
    await time.advance(3000); assert.equal(calls.length, 2); coordinator.close();
  });
}

test('a second pacing refusal cannot create a classification retry loop', async () => {
  let calls = 0;
  const { coordinator, time, output } = setup({ selectCue: async () => {
    calls++; return decision('waiting', { phrase_id: 'silence', reason: 'classification_throttled',
      requests: 0, retry_after_ms: 200, expires_in_ms: 0 });
  } });
  waiting(coordinator); await time.advance(5000);
  assert.equal(calls, 2); assert.equal(output.handles.length, 0); coordinator.close();
});

for (const [name, overrides] of [
  ['uncertainty', { reason: 'classification_uncertain' }],
  ['accepted or attempted classification', { requests: 1 }],
  ['zero retry delay', { retry_after_ms: 0 }],
  ['unbounded retry delay', { retry_after_ms: 1001 }],
]) {
  test(`waiting pacing recovery never retries ${name}`, async () => {
    let calls = 0;
    const { coordinator, time } = setup({ selectCue: async () => { calls++; return decision('waiting', {
      phrase_id: 'silence', reason: 'cue_spacing', requests: 0, retry_after_ms: 200, expires_in_ms: 0, ...overrides }); } });
    waiting(coordinator); await time.advance(5000); assert.equal(calls, 1); coordinator.close();
  });
}

for (const [name, act] of [
  ['ready main PCM', coordinator => coordinator.receiveMainPcm('main-1', pcm(), 16000)],
  ['sensitive caller change', coordinator => coordinator.setSensitive(true)],
  ['confirmed caller resumes', coordinator => coordinator.resumeUserSpeech()],
  ['Stop', coordinator => coordinator.close()],
]) {
  test(`a scheduled fresh waiting retry is revoked by ${name}`, async () => {
    let calls = 0;
    const { coordinator, time, output } = setup({ selectCue: async () => { calls++; return decision('waiting', {
      phrase_id: 'silence', reason: 'cue_spacing', requests: 0, retry_after_ms: 500, expires_in_ms: 0 }); } });
    waiting(coordinator); await time.advance(100); act(coordinator); await time.advance(1000);
    assert.equal(calls, 1); assert.equal(output.handles.length, 0); coordinator.close();
  });
}

test('late listening playback finishes before waiting selection consumes its absolute lifetime', async () => {
  let time; const selected = [];
  const f = setup({ selectCue: request => {
    selected.push({ phase: request.phase, at: time.now() });
    return request.phase === 'listening' ? Promise.resolve(decision('listening'))
      : new Promise(resolve => time.setTimeout(() => resolve(decision('waiting', {
        max_request_age_ms: 800, expires_in_ms: 500 })), 300));
  } });
  time = f.time; caller(f.coordinator); await time.advance(300);
  await time.advance(200); f.coordinator.userSpeechStopped(); f.coordinator.beginGeneration('main');
  await time.advance(470); assert.equal(selected.length, 1);
  f.output.handles[0].finish(); await flush(); await time.advance(0);
  assert.deepEqual(selected, [{ phase: 'listening', at: 300 }, { phase: 'waiting', at: 970 }]);
  await time.advance(300); assert.equal(f.output.handles.length, 2);
  assert.equal(f.events.findLast(row => row.kind === 'cue_started').at_ms, 1270); f.coordinator.close();
});

test('a full rolling cue window defers new work and recovers without a new call', async () => {
  const { coordinator, time, output, requests } = setup({ maxCues: 2, maxRequests: 2, cooldownMs: 0 });
  for (let turn = 0; turn < 2; turn++) {
    if (turn) coordinator.userSpeechStopped(); caller(coordinator);
    await time.advance(350); output.handles.at(-1).finish(); await flush();
  }
  coordinator.userSpeechStopped(); caller(coordinator); await time.advance(1000);
  assert.equal(requests.length, 2); assert.equal(output.handles.length, 2);
  await time.advance(15000); assert.equal(output.handles.length, 3);
  assert.equal(coordinator.cuesPlayed, 3); coordinator.close();
});

test('per-utterance listening attempts reserve one bounded request for waiting', async () => {
  const selected = [];
  const { coordinator, time } = setup({ selectCue: async request => { selected.push(request); return decision(request.phase, {
    phrase_id: request.phase === 'listening' ? 'silence' : 'moment', reason: 'classification_uncertain' }); } });
  caller(coordinator);
  for (let index = 0; index < 12; index++) {
    coordinator.updatePartial(`I need routine service for my car with detail ${index}`, { revision: index + 2 });
    await time.advance(350);
  }
  assert.equal(selected.length, 7);
  coordinator.userSpeechStopped(); coordinator.beginGeneration('answer'); await time.advance(550);
  assert.equal(selected.length, 8); assert.equal(selected.at(-1).phase, 'waiting'); coordinator.close();
});

test('a listening cue is selected and played while the caller is still speaking', async () => {
  const { coordinator, time, requests, output, events } = setup();
  caller(coordinator);
  await time.advance(299); assert.equal(requests.length, 0);
  await time.advance(1);
  assert.equal(coordinator.userSpeaking, true);
  assert.equal(requests.length, 1); assert.equal(requests[0].phase, 'listening');
  assert.equal(requests[0].revision, 1);
  assert.deepEqual(output.calls.filter(call => call[0] === 'cue'), [['cue', 'neutral_ack']]);
  assert.equal(events.some(event => JSON.stringify(event).includes('routine service')), false);
  coordinator.close();
});

test('the waiting cue runs independently while a main generation remains unfinished', async () => {
  const { coordinator, time, requests, output } = setup();
  waiting(coordinator); await time.advance(550);
  assert.equal(coordinator.generating, true); assert.equal(requests[0].phase, 'waiting');
  assert.deepEqual(output.calls.filter(call => call[0] === 'cue'), [['cue', 'moment']]);
  coordinator.close();
});

test('partial revisions abort old classification and reject its late result', async () => {
  const pending = [], { coordinator, time, output } = setup({ selectCue: request => {
    const done = deferred(); pending.push({ request, done }); return done.promise;
  } });
  caller(coordinator); await time.advance(300);
  coordinator.updatePartial('Actually I need a different service', { revision: 2 });
  assert.equal(pending[0].request.signal.aborted, true);
  pending[0].done.resolve(decision('listening')); await flush();
  assert.equal(output.handles.length, 0);
  await time.advance(350);
  pending[1].done.resolve(decision('listening')); await flush();
  assert.equal(output.handles.length, 1); coordinator.close();
});

test('caller stop invalidates a listening decision even if the selector ignores cancellation', async () => {
  const done = deferred(), { coordinator, time, output } = setup({ selectCue: () => done.promise });
  caller(coordinator); await time.advance(300); coordinator.userSpeechStopped();
  done.resolve(decision('listening')); await flush();
  assert.equal(output.handles.length, 0); coordinator.close();
});

test('main audio wins a pending selection and stops an audible cue before scheduling its first sample', async () => {
  const { coordinator, time, output } = setup();
  waiting(coordinator); await time.advance(550);
  assert.equal(output.handles[0].stopped, false);
  assert.equal(coordinator.receiveMainPcm('main-1', new Uint8Array([0, 0]), 16000), true);
  assert.equal(output.handles[0].stopped, true);
  assert.deepEqual(output.calls.slice(-2), [['cue-stop', 'moment'], ['main', 'main-1']]);
  await time.advance(20000);
  assert.equal(output.handles.length, 1); coordinator.close();
});

test('main readiness aborts a pending classifier and ignores its later success', async () => {
  const done = deferred(); let selected;
  const { coordinator, time, output } = setup({ selectCue: request => { selected = request; return done.promise; } });
  waiting(coordinator); await time.advance(550);
  coordinator.receiveMainPcm('main-1', new Uint8Array([0, 0]), 16000);
  assert.equal(selected.signal.aborted, true);
  done.resolve(decision('waiting')); await flush();
  assert.equal(output.handles.length, 0); coordinator.close();
});

test('barge-in stops every main node and rejects old PCM after the caller finishes', async () => {
  const { coordinator, output } = setup(); waiting(coordinator);
  coordinator.receiveMainPcm('main-1', new Uint8Array([0, 0]), 16000);
  coordinator.userSpeechStarted();
  assert.equal(output.calls.at(-1)[0], 'main-stop');
  assert.equal(coordinator.receiveMainPcm('main-1', new Uint8Array([0, 0]), 16000), false);
  coordinator.updatePartial('Please tell me the opening hours'); coordinator.userSpeechStopped();
  assert.equal(coordinator.receiveMainPcm('main-1', new Uint8Array([0, 0]), 16000), false);
  coordinator.beginGeneration('main-2');
  assert.equal(coordinator.receiveMainPcm('main-2', new Uint8Array([0, 0]), 16000), true);
  coordinator.close();
});

test('late classifier responses after the deadline cannot play even when abort is ignored', async () => {
  const done = deferred(); let signal;
  const { coordinator, time, output, events } = setup({ decisionDeadlineMs: 350,
    selectCue: request => { signal = request.signal; return done.promise; } });
  caller(coordinator); await time.advance(650);
  assert.equal(signal.aborted, true);
  done.resolve(decision('listening')); await flush();
  assert.equal(output.handles.length, 0);
  assert.equal(events.some(event => event.reason === 'classifier_deadline'), true);
  coordinator.close();
});

for (const [name, overrides] of [
  ['wrong phase clip', { phrase_id: 'moment' }],
  ['arbitrary classifier prose', { phrase_id: 'Your booking is confirmed.' }],
  ['low confidence', { confidence: .7 }],
  ['nonfinite confidence', { confidence: NaN }],
  ['unverified language', { language_choice: 'uncertain' }],
  ['expired selection', { expires_in_ms: 0 }],
]) {
  test(`unsafe selection remains silent: ${name}`, async () => {
    const { coordinator, time, output } = setup({ selectCue: async () => decision('listening', overrides) });
    caller(coordinator); await time.advance(300);
    assert.equal(output.handles.length, 0); coordinator.close();
  });
}

test('a sensitive state cancels active cues and requires fresh evidence after clearing', async () => {
  const { coordinator, time, output } = setup(); caller(coordinator); await time.advance(300);
  coordinator.setSensitive(true); assert.equal(output.handles[0].stopped, true);
  coordinator.updatePartial('Yes please confirm it', { revision: 2 }); await time.advance(20000);
  assert.equal(output.handles.length, 1);
  coordinator.setSensitive(false); await time.advance(1000);
  assert.equal(output.handles.length, 1);
  coordinator.userSpeechStopped(); coordinator.userSpeechStarted();
  coordinator.updatePartial('Could you explain your service options', { revision: 3 }); await time.advance(300);
  assert.equal(output.handles.length, 2); coordinator.close();
});

test('cue and request windows recover in a long call while same-phase pacing remains bounded', async () => {
  const { coordinator, time, output, requests } = setup({ maxCues: 2, maxRequests: 2 });
  caller(coordinator); await time.advance(300);
  coordinator.updatePartial('Here are a few more service details', { revision: 2 }); await time.advance(300);
  assert.equal(requests.length, 1);
  await time.advance(15000);
  coordinator.userSpeechStopped(); coordinator.userSpeechStarted();
  coordinator.updatePartial('I can leave the car there tomorrow', { revision: 3 }); await time.advance(300);
  assert.equal(output.handles.length, 2);
  await time.advance(15000);
  coordinator.userSpeechStopped(); coordinator.userSpeechStarted();
  coordinator.updatePartial('Let me also explain the issue clearly', { revision: 4 }); await time.advance(300);
  assert.equal(requests.length, 3); assert.equal(output.handles.length, 3); coordinator.close();
});

test('each phase has one allowance across revisions while the global cue cap remains bounded', async () => {
  const { coordinator, time, output, requests } = setup();
  caller(coordinator); await time.advance(300);
  assert.equal(requests[0].utteranceId, 1);
  await time.advance(20000);
  coordinator.updatePartial('I also want to tell you more about the same issue', { revision: 2 });
  await time.advance(300);
  coordinator.updateFinal('I also want to tell you more about the same issue', { revision: 3 });
  coordinator.userSpeechStopped(); coordinator.beginGeneration('main'); await time.advance(550);
  assert.equal(output.handles.length, 2); assert.equal(requests.length, 2);
  assert.deepEqual(requests.map(request => request.phase), ['listening', 'waiting']);
  coordinator.updateFinal('I also want to tell you more about the same issue tomorrow', { revision: 4 });
  await time.advance(20000);
  assert.equal(output.handles.length, 2); assert.equal(requests.length, 2);
  coordinator.close();
});

test('slow main permits a waiting cue after a full listening clip and a quiet gap without polling', async () => {
  const { coordinator, time, output, requests, events } = setup();
  caller(coordinator); await time.advance(300);
  await time.advance(200); coordinator.updateFinal('I need routine service for my car', { revision: 2 });
  coordinator.userSpeechStopped(); coordinator.beginGeneration('main-1');
  await time.advance(470); // Listening clip has now sounded for its full670ms.
  assert.equal(requests.length, 1); assert.equal(output.handles[0].stopped, false);
  output.handles[0].finish(); await flush();
  assert.equal(time.pending, 1, 'Clip completion owns one waiting timer, not a polling loop');
  await time.advance(299); assert.equal(requests.length, 2);
  await time.advance(1);
  assert.deepEqual(requests.map(request => request.phase), ['listening', 'waiting']);
  assert.deepEqual(output.calls.filter(call => call[0] === 'cue'), [['cue', 'neutral_ack'], ['cue', 'moment']]);
  assert.deepEqual(events.filter(event => event.kind === 'cue_started').map(event => event.at_ms), [300, 1270]);
  assert.equal(coordinator.receiveMainPcm('main-1', pcm(), 16000), true);
  assert.deepEqual(output.calls.slice(-2), [['cue-stop', 'moment'], ['main', 'main-1']]);
  coordinator.close();
});

test('waiting guard starts at confirmed yield and an identical trailing final preserves one held snapshot', async () => {
  const { coordinator, time, requests, output, events } = setup();
  waiting(coordinator); await time.advance(300);
  coordinator.updateFinal('I need routine service for my car', { revision: 2 });
  await time.advance(249); assert.equal(requests.length, 1); assert.equal(output.handles.length, 0);
  await time.advance(1); assert.equal(requests.length, 1);
  assert.equal(requests[0].phase, 'waiting'); assert.equal(requests[0].revision, 1);
  const played = events.find(event => event.kind === 'cue_started');
  assert.equal(played.at_ms, 550); assert.equal(played.snapshot_revision, 1); assert.equal(played.latest_revision, 2);
  coordinator.close();
});

test('a fractional early browser timer retains one waiting attempt without firing before its guard', async () => {
  const { coordinator, time, requests, output } = setup();
  waiting(coordinator); await time.advance(0);
  await time.fireNextEarly(.4);
  assert.equal(requests.length, 1); assert.equal(output.handles.length, 0); assert.equal(time.pending, 1);
  await time.advance(.9); assert.equal(output.handles.length, 0);
  await time.advance(.1); assert.equal(requests.length, 1); assert.equal(requests[0].phase, 'waiting');
  assert.equal(output.handles.length, 1);
  coordinator.close(); assert.equal(time.pending, 0);
});

test('fast main before the waiting guard has no audible filler or playback delay', async () => {
  const { coordinator, time, output, requests } = setup();
  waiting(coordinator); await time.advance(400);
  assert.equal(coordinator.receiveMainPcm('main-1', pcm(), 16000), true);
  assert.deepEqual(output.calls.at(-1), ['main', 'main-1']);
  await time.advance(2000); assert.equal(requests.length, 1); assert.equal(output.handles.length, 0);
  coordinator.close();
});

test('150 ms waiting classification overlaps the guard and plays at 550 ms with one request', async () => {
  const requests = [];
  const { coordinator, time, output, events } = setup({ selectCue: request => {
    requests.push({ request, at: time.now() });
    return new Promise(resolve => time.setTimeout(() => resolve(decision('waiting', { expires_in_ms: 800 })), 150));
  } });
  waiting(coordinator); await time.advance(150);
  assert.equal(requests.length, 1); assert.equal(requests[0].at, 0); assert.equal(output.handles.length, 0);
  assert.ok(coordinator.request.decision, 'The completed result remains owned during the playback guard');
  await time.advance(399); assert.equal(output.handles.length, 0);
  await time.advance(1); assert.equal(output.handles.length, 1); assert.equal(requests.length, 1);
  const played = events.find(event => event.kind === 'cue_started');
  assert.equal(played.at_ms, 550); assert.equal(played.selection_ms, 150);
  assert.equal(played.guard_hold_ms, 400); assert.equal(played.decision_age_ms, 550);
  assert.equal(coordinator.decisionDeadlineMs, 850); coordinator.close();
});

test('main announcement during a held waiting result cancels it before any cue node exists', async () => {
  const { coordinator, time, output, requests } = setup();
  waiting(coordinator); await time.advance(400);
  assert.ok(coordinator.request.decision); coordinator.invalidate('main_announced', { stopCue: false });
  assert.equal(requests[0].signal.aborted, true); assert.equal(coordinator.request, null);
  await time.advance(1000); assert.equal(output.handles.length, 0); assert.equal(requests.length, 1);
  assert.equal(coordinator.receiveMainPcm('main-1', pcm(), 16000), true); coordinator.close();
});

test('an original 300 ms selection TTL expires during the guard without rolling or retrying', async () => {
  const requests = [];
  const { coordinator, time, output, events } = setup({ selectCue: request => {
    requests.push(request);
    return new Promise(resolve => time.setTimeout(() => resolve(decision('waiting', { expires_in_ms: 300 })), 150));
  } });
  waiting(coordinator); await time.advance(299); assert.equal(output.handles.length, 0);
  assert.ok(coordinator.request.decision); await time.advance(1);
  assert.equal(coordinator.request, null); assert.equal(requests[0].signal.aborted, true);
  assert.equal(events.find(event => event.reason === 'selection_expired_before_playback').at_ms, 300);
  await time.advance(2000); assert.equal(output.handles.length, 0); assert.equal(requests.length, 1);
  coordinator.close();
});

test('the original client deadline includes the held guard and cannot be extended by a longer TTL', async () => {
  const { coordinator, time, output, requests } = setup({ decisionDeadlineMs: 500 });
  waiting(coordinator); await time.advance(500);
  assert.equal(requests.length, 1); assert.equal(requests[0].signal.aborted, true);
  assert.equal(coordinator.request, null); await time.advance(1000);
  assert.equal(output.handles.length, 0); assert.equal(requests.length, 1); coordinator.close();
});

test('waiting selection starts after listening completion and a short TTL still cannot roll past its guard', async () => {
  const requests = [];
  const { coordinator, time, output } = setup({ selectCue: async request => {
    requests.push(request); return decision(request.phase, { expires_in_ms: request.phase === 'waiting' ? 300 : 800 });
  } });
  caller(coordinator); await time.advance(300); await time.advance(200);
  coordinator.userSpeechStopped(); coordinator.beginGeneration('main-1'); await time.advance(470);
  assert.deepEqual(requests.map(request => request.phase), ['listening']);
  assert.equal(coordinator.request, null, 'No accepted waiting result spends its TTL behind a playing clip');
  output.handles[0].finish(); await flush(); await time.advance(2000);
  assert.equal(requests.length, 2); assert.equal(output.handles.length, 1); coordinator.close();
});

test('changed final text revokes a held waiting snapshot without spending another phase attempt', async () => {
  const { coordinator, time, output, requests } = setup();
  waiting(coordinator); await time.advance(150);
  coordinator.updateFinal('I need routine service for my car tomorrow', { revision: 2 });
  assert.equal(requests[0].signal.aborted, true);
  await time.advance(2000); assert.equal(requests.length, 1); assert.equal(output.handles.length, 0);
  coordinator.close();
});

for (const text of ['Alex', 'Opening hours']) {
  test(`a short ${JSON.stringify(text)} turn skips listening but can select one waiting cue`, async () => {
    const { coordinator, time, requests, output } = setup();
    caller(coordinator, text); await time.advance(300); assert.equal(requests.length, 0);
    coordinator.updateFinal(text, { revision: 2 }); coordinator.userSpeechStopped(); coordinator.beginGeneration('main-1');
    await time.advance(0); assert.equal(requests.length, 1); assert.equal(requests[0].phase, 'waiting');
    assert.equal(requests[0].text, text); assert.equal(output.handles.length, 0);
    await time.advance(550); assert.equal(output.handles.length, 1); coordinator.close();
  });
}

test('an empty waiting turn cannot reach the selector after lowering the waiting word minimum', async () => {
  const { coordinator, time, requests, output } = setup();
  coordinator.userSpeechStarted(); coordinator.updateFinal('   '); coordinator.userSpeechStopped(); coordinator.beginGeneration('main-1');
  await time.advance(1000); assert.equal(requests.length, 0); assert.equal(output.handles.length, 0); coordinator.close();
});

test('listening-only context block cancels a pending timer without blocking a later waiting phase', async () => {
  const { coordinator, time, requests } = setup();
  caller(coordinator); coordinator.setListeningBlocked(true); await time.advance(500);
  assert.equal(requests.length, 0); assert.equal(coordinator.pendingTimer, null);
  coordinator.userSpeechStopped(); coordinator.beginGeneration('main-1'); await time.advance(550);
  assert.equal(requests.length, 1); assert.equal(requests[0].phase, 'waiting'); coordinator.close();
});

test('listening-only context block aborts a classifier and rejects its ignored-abort success', async () => {
  const done = deferred(); let selected;
  const { coordinator, time, output } = setup({ selectCue: request => { selected = request; return done.promise; } });
  caller(coordinator); await time.advance(300); const epoch = coordinator.epoch;
  coordinator.setListeningBlocked(true); assert.equal(selected.signal.aborted, true); assert.equal(coordinator.epoch, epoch);
  done.resolve(decision('listening')); await flush(); assert.equal(output.handles.length, 0); coordinator.close();
});

test('listening-only context block preserves an already-held waiting decision and its original guard', async () => {
  const { coordinator, time, output, requests } = setup();
  waiting(coordinator); await time.advance(150); const held = coordinator.request, epoch = coordinator.epoch;
  coordinator.setListeningBlocked(true);
  assert.equal(coordinator.request, held); assert.equal(coordinator.epoch, epoch); assert.equal(requests[0].signal.aborted, false);
  await time.advance(400); assert.equal(output.handles.length, 1); assert.equal(requests.length, 1); coordinator.close();
});

test('listening-only block stops the listening source then starts a fresh waiting selection', async () => {
  const { coordinator, time, output, requests } = setup();
  caller(coordinator); await time.advance(300); await time.advance(200);
  coordinator.userSpeechStopped(); coordinator.beginGeneration('main-1'); await time.advance(200);
  assert.equal(requests.length, 1);
  coordinator.setListeningBlocked(true); assert.equal(output.handles[0].stopped, true);
  await time.advance(0); assert.equal(requests.length, 2); assert.equal(requests[1].signal.aborted, false);
  await time.advance(349); assert.equal(output.handles.length, 1);
  await time.advance(1); assert.equal(output.handles.length, 2); coordinator.close();
});

for (const [name, action] of [
  ['new caller speech', coordinator => coordinator.userSpeechStarted()],
  ['sensitive context', coordinator => coordinator.setSensitive(true)],
  ['finished generation', coordinator => coordinator.finishGeneration('main-1')],
  ['closed call', coordinator => coordinator.close()],
]) {
  test(`a held waiting result is revoked by ${name}`, async () => {
    const { coordinator, time, output, requests } = setup();
    waiting(coordinator); await time.advance(150); assert.ok(coordinator.request.decision);
    action(coordinator); assert.equal(requests[0].signal.aborted, true); assert.equal(coordinator.request, null);
    await time.advance(1000); assert.equal(output.handles.length, 0); assert.equal(requests.length, 1);
    coordinator.close(); assert.equal(time.pending, 0);
  });
}

test('a main announcement prevents clip completion from resurrecting a waiting cue', async () => {
  const { coordinator, time, output, requests } = setup();
  caller(coordinator); await time.advance(300); coordinator.userSpeechStopped(); coordinator.beginGeneration('main-1');
  await time.advance(100); coordinator.invalidate('main_announced', { stopCue: false });
  assert.equal(output.handles[0].stopped, false, 'Main announcement preserves the existing word until actual main PCM');
  await time.advance(570); output.handles[0].finish(); await flush();
  coordinator.updateFinal('I need routine service for my car tomorrow', { revision: 2 });
  await time.advance(3000);
  assert.equal(requests.length, 1); assert.equal(output.handles.length, 1);
  assert.equal(coordinator.receiveMainPcm('main-1', pcm(), 16000), true);
  coordinator.close();
});

test('a main announcement aborts an in-flight waiting selection and ignores its late result', async () => {
  const done = deferred(); let selected;
  const { coordinator, time, output } = setup({ selectCue: request => { selected = request; return done.promise; } });
  waiting(coordinator); await time.advance(550);
  coordinator.invalidate('main_announced', { stopCue: false });
  assert.equal(selected.signal.aborted, true);
  done.resolve(decision('waiting')); await flush(); await time.advance(2000);
  assert.equal(output.handles.length, 0); assert.equal(coordinator.requests, 1);
  coordinator.close();
});

test('main PCM arriving in the same tick as selection resolution wins before a cue node is created', async () => {
  const done = deferred(), { coordinator, time, output } = setup({ selectCue: () => done.promise });
  waiting(coordinator); await time.advance(550);
  done.resolve(decision('waiting'));
  assert.equal(coordinator.receiveMainPcm('main-1', pcm(), 16000), true);
  await flush();
  assert.equal(output.handles.length, 0); assert.equal(coordinator.cuesPlayed, 0);
  assert.deepEqual(output.calls.at(-1), ['main', 'main-1']); coordinator.close();
});

test('main PCM reentered during clip creation stops the uncommitted node and leaves no owned cue', async () => {
  const { coordinator, time, output } = setup();
  const playCue = output.playCue.bind(output);
  output.playCue = phrase => { const handle = playCue(phrase); coordinator.receiveMainPcm('main-1', pcm(), 16000); return handle; };
  waiting(coordinator); await time.advance(550);
  assert.equal(output.handles[0].stopped, true); assert.equal(coordinator.cue, null);
  assert.equal(coordinator.cuesPlayed, 0); assert.equal(coordinator.mainHasAudio, true);
  await time.advance(3000); assert.equal(output.handles.length, 1); coordinator.close();
});

test('phase pairing cannot repeat a waiting cue while a later utterance recovers its window budget', async () => {
  const { coordinator, time, output, requests } = setup({ maxCues: 2 });
  caller(coordinator); await time.advance(300);
  output.handles[0].finish(); await flush();
  coordinator.userSpeechStopped(); coordinator.beginGeneration('main-1'); await time.advance(550);
  output.handles[1].finish(); await flush(); await time.advance(15000);
  coordinator.updateFinal('I need routine service for my car tomorrow', { revision: 2 });
  await time.advance(550); assert.equal(requests.length, 2);
  coordinator.userSpeechStarted(); coordinator.updatePartial('Could you explain your service options today', { revision: 3 });
  await time.advance(1000); assert.equal(requests.length, 3); assert.equal(output.handles.length, 3);
  coordinator.close();
});

test('close clears the waiting retry created by listening clip completion', async () => {
  const { coordinator, time, output, requests } = setup();
  caller(coordinator); await time.advance(300); coordinator.userSpeechStopped(); coordinator.beginGeneration('main-1');
  output.handles[0].finish(); await flush(); assert.equal(time.pending, 1);
  coordinator.close(); assert.equal(time.pending, 0);
  await time.advance(3000); assert.equal(requests.length, 1); assert.equal(output.handles.length, 1);
});

test('a final transcript cancels listening classification without scheduling a new listening cue', async () => {
  const { coordinator, time, requests } = setup();
  caller(coordinator); coordinator.updateFinal('I need routine service for my car tomorrow', { revision: 2 });
  await time.advance(300); assert.equal(requests.length, 0);
  coordinator.userSpeechStopped(); coordinator.beginGeneration('main'); await time.advance(550);
  assert.equal(requests.length, 1); assert.equal(requests[0].phase, 'waiting'); coordinator.close();
});

test('selection expiry never truncates complete speech; a separate duration guard bounds runaway audio', async () => {
  const { coordinator, time, output } = setup({ selectCue: async () => decision('listening', { expires_in_ms: 100 }) });
  caller(coordinator); await time.advance(300);
  assert.equal(output.handles[0].stopped, false); await time.advance(100);
  assert.equal(output.handles[0].stopped, false); await time.advance(2399);
  assert.equal(output.handles[0].stopped, false); await time.advance(1);
  assert.equal(output.handles[0].stopped, true); coordinator.close();
});

test('normal ASR revisions and a caller yielding do not cut an already-started listening word', async () => {
  const { coordinator, time, output } = setup();
  caller(coordinator); await time.advance(300);
  coordinator.updatePartial('I need routine service for my vehicle tomorrow', { revision: 2 });
  assert.equal(output.handles[0].stopped, false);
  coordinator.userSpeechStopped(); coordinator.beginGeneration('main-1');
  assert.equal(output.handles[0].stopped, false);
  coordinator.receiveMainPcm('main-1', new Uint8Array([0, 0]), 16000);
  assert.equal(output.handles[0].stopped, true, 'Actual main audio still preempts immediately');
  coordinator.close();
});

test('closing cancels timers, active playback and a late ignored-abort classifier', async () => {
  const done = deferred(), { coordinator, time, output } = setup({ selectCue: () => done.promise });
  caller(coordinator); await time.advance(300); coordinator.close(); coordinator.close();
  assert.equal(time.pending, 0);
  done.resolve(decision('listening')); await flush(); await time.advance(5000);
  assert.equal(output.handles.length, 0); assert.equal(output.calls.filter(call => call[0] === 'close').length, 1);
  assert.equal(coordinator.beginGeneration('late'), false);
  assert.equal(coordinator.receiveMainPcm('late', new Uint8Array([0, 0]), 16000), false);
});

function audioContext() {
  const sources = [], buffers = [], gains = [];
  const context = {
    state: 'running', currentTime: 10, destination: {}, sources, buffers, gains,
    advanceTo(time) {
      this.currentTime = time;
      for (const source of sources) {
        if (!source.ended && (source.stopAt <= time || source.startedAt + source.buffer.duration - source.offset <= time)) {
          source.ended = true; source.onended?.();
        }
      }
    },
    createBuffer(channels, length, rate) {
      const buffer = { channels, length, sampleRate: rate, duration: length / rate,
        copyToChannel(samples) { this.samples = samples; } }; buffers.push(buffer); return buffer;
    },
    createGain() {
      const calls = [], param = { value: 1,
        setValueAtTime(value, at) { calls.push(['set', value, at]); },
        cancelScheduledValues(at) { calls.push(['cancel', at]); },
        linearRampToValueAtTime(value, at) { calls.push(['ramp', value, at]); },
      };
      const gain = { gain: param, calls, connectedTo: null, disconnected: false,
        connect(target) { this.connectedTo = target; }, disconnect() { this.disconnected = true; } };
      gains.push(gain); return gain;
    },
    createBufferSource() {
      const source = { stopped: false, disconnected: false, stopAt: Infinity, connect(target) { this.connectedTo = target; },
        start(at, offset = 0) { this.startedAt = at; this.offset = offset; },
        stop(at = context.currentTime) { this.stopped = true; this.stopAt = at;
          if (at <= context.currentTime) { this.ended = true; this.onended?.(); } },
        disconnect() { this.disconnected = true; } };
      sources.push(source); return source;
    },
  };
  return context;
}
const pcm = (samples = 1600) => new Uint8Array(samples * 2);

test('PCM playback decodes actual little-endian PCM geometry, including nonzero view offsets', () => {
  const context = audioContext(), output = new PcmPlayback({ context });
  const bytes = new Uint8Array([99, 0, 128, 255, 127, 99]);
  assert.equal(output.enqueueMain({ pcm: bytes.subarray(1, 5), sampleRate: 16000 }), true);
  assert.equal(context.buffers[0].length, 2); assert.equal(context.buffers[0].sampleRate, 16000);
  assert.deepEqual([...context.buffers[0].samples], [-1, 32767 / 32768]);
  assert.equal(context.sources[0].startedAt, 10); output.close();
});

test('local main PCM preempts a cached cue at its 8 ms fade barrier and later chunks share that playback clock', () => {
  const context = audioContext(), output = new PcmPlayback({ context, cueAssets: { neutral_ack: { pcm: pcm(2400).buffer, sampleRate: 24000 } } });
  const cue = output.playCue('neutral_ack'); assert.ok(cue); assert.equal(context.buffers[0].sampleRate, 24000);
  assert.equal(output.enqueueMain({ pcm: pcm(), sampleRate: 16000 }), true);
  assert.equal(context.sources[0].stopped, true); assert.equal(context.sources[0].stopAt, 10.008);
  assert.equal(context.sources[1].startedAt, 10.008);
  assert.equal(output.enqueueMain({ pcm: pcm(), sampleRate: 16000 }), true);
  assert.ok(Math.abs(context.sources[2].startedAt - 10.108) < 1e-12); assert.equal(output.playCue('neutral_ack'), null);
  output.close(); context.advanceTo(10.008);
  assert.equal(context.sources.every(source => source.stopped && source.disconnected), true);
  assert.equal(context.gains[0].disconnected, true);
});

test('same-frame main priority revokes the cue immediately and schedules one real gain ramp before main samples', async () => {
  const time = clock(), context = audioContext();
  const output = new PcmPlayback({ context, cueAssets: { moment: { pcm: pcm(16800), sampleRate: 16000 } } });
  const coordinator = new DuplexCoordinator({ playback: output, selectCue: async request => decision(request.phase),
    now: time.now, setTimeout: time.setTimeout, clearTimeout: time.clearTimeout });
  waiting(coordinator); await time.advance(550);
  const cue = context.sources[0], gain = context.gains[0];
  assert.equal(cue.connectedTo, gain); assert.equal(gain.connectedTo, context.destination);
  assert.equal(coordinator.receiveMainPcm('main-1', pcm(320), 16000), true);
  assert.equal(coordinator.cue, null, 'Logical ownership is revoked synchronously');
  assert.deepEqual(gain.calls, [['set', 1, 10], ['cancel', 10], ['set', 1, 10], ['ramp', 0, 10.008]]);
  assert.equal(cue.stopAt, 10.008); assert.equal(context.sources[1].startedAt, cue.stopAt);
  assert.equal(cue.disconnected, false, 'The gain stays connected for its actual fade');
  assert.equal(coordinator.receiveMainPcm('main-1', pcm(320), 16000), true);
  assert.ok(Math.abs(context.sources[2].startedAt - 10.028) < 1e-12);
  assert.equal(gain.calls.filter(call => call[0] === 'ramp').length, 1, 'Repeated same-frame main packets do not extend the fade');
  context.advanceTo(10.008); assert.equal(cue.disconnected, true); assert.equal(gain.disconnected, true);
  coordinator.close();
});

test('a cue with less than 8 ms remaining uses its natural end as the main handoff barrier', () => {
  const context = audioContext(), output = new PcmPlayback({ context,
    cueAssets: { moment: { pcm: pcm(1600), sampleRate: 16000 } } });
  output.playCue('moment'); context.currentTime = 10.097;
  assert.equal(output.enqueueMain({ pcm: pcm(320), sampleRate: 16000 }), true);
  assert.equal(context.sources[0].stopAt, 10.1); assert.equal(context.sources[1].startedAt, 10.1);
  assert.deepEqual(context.gains[0].calls.slice(-1), [['ramp', 0, 10.1]]);
  context.advanceTo(10.1); output.close();
});

test('an already-ended cue adds no phantom fade delay even before its promise cleanup runs', () => {
  const context = audioContext(), output = new PcmPlayback({ context,
    cueAssets: { moment: { pcm: pcm(1600), sampleRate: 16000 } } });
  output.playCue('moment'); context.advanceTo(10.1);
  assert.equal(output.enqueueMain({ pcm: pcm(320), sampleRate: 16000 }), true);
  assert.equal(context.sources[1].startedAt, 10.1);
  assert.equal(context.gains[0].calls.some(call => call[0] === 'ramp'), false);
  output.close();
});

test('caller cancellation and close reuse one bounded cue fade and cannot extend its lifetime', () => {
  const context = audioContext(), output = new PcmPlayback({ context,
    cueAssets: { neutral_ack: { pcm: pcm(16000), sampleRate: 16000 } } });
  const handle = output.playCue('neutral_ack'); assert.equal(handle.stop('caller_started'), 10.008);
  context.currentTime = 10.004; assert.equal(handle.stop('call_ended'), 10.008);
  output.close(); assert.equal(context.gains[0].calls.filter(call => call[0] === 'ramp').length, 1);
  assert.equal(context.sources[0].disconnected, false);
  context.advanceTo(10.008); assert.equal(context.sources[0].disconnected, true);
  assert.equal(context.gains[0].disconnected, true); assert.equal(context.sources.length, 1);
});

test('a suspended cue is stopped immediately without a future audio-clock barrier', () => {
  const context = audioContext(), output = new PcmPlayback({ context,
    cueAssets: { moment: { pcm: pcm(), sampleRate: 16000 } } });
  const handle = output.playCue('moment'); context.state = 'suspended';
  assert.equal(handle.stop('caller_started'), 10); assert.equal(context.sources[0].disconnected, true);
  assert.equal(context.gains[0].calls.some(call => call[0] === 'ramp'), false);
  context.state = 'running'; output.enqueueMain({ pcm: pcm(320), sampleRate: 16000 });
  assert.equal(context.sources[1].startedAt, 10); output.close();
});

test('a nonfinite gain readback still schedules a finite unity-to-zero cue fade', () => {
  const context = audioContext(), output = new PcmPlayback({ context,
    cueAssets: { moment: { pcm: pcm(), sampleRate: 16000 } } });
  const handle = output.playCue('moment'); context.gains[0].gain.value = NaN; handle.stop('main_audio_ready');
  assert.deepEqual(context.gains[0].calls.slice(-2), [['set', 1, 10], ['ramp', 0, 10.008]]);
  context.advanceTo(10.008); output.close();
});

test('missing gain scheduling support fails the cue without allocating a source or delaying main', () => {
  const context = audioContext(), output = new PcmPlayback({ context,
    cueAssets: { moment: { pcm: pcm(), sampleRate: 16000 } } });
  const badGain = { gain: { value: 1 }, disconnected: false, disconnect() { this.disconnected = true; } };
  context.createGain = () => badGain;
  assert.throws(() => output.playCue('moment'), /cue_gain_unavailable/);
  assert.equal(badGain.disconnected, true); assert.equal(context.sources.length, 0);
  output.enqueueMain({ pcm: pcm(320), sampleRate: 16000 }); assert.equal(context.sources[0].startedAt, 10);
  output.close();
});

test('an empty cue buffer cannot reserve a fade barrier or delay valid main PCM', () => {
  const context = audioContext(), output = new PcmPlayback({ context,
    cueAssets: { moment: { pcm: new ArrayBuffer(0), sampleRate: 16000 } } });
  assert.throws(() => output.playCue('moment'), /invalid_pcm16/);
  assert.equal(context.sources.length, 0); assert.equal(context.gains.length, 0);
  output.enqueueMain({ pcm: pcm(320), sampleRate: 16000 }); assert.equal(context.sources[0].startedAt, 10);
  output.close();
});

test('a gain setup exception disconnects the owned cue source and gain without affecting main playback', () => {
  const context = audioContext(), output = new PcmPlayback({ context,
    cueAssets: { moment: { pcm: pcm(), sampleRate: 16000 } } });
  const createGain = context.createGain.bind(context);
  context.createGain = () => { const gain = createGain(); gain.gain.setValueAtTime = () => { throw new Error('gain unavailable'); }; return gain; };
  assert.throws(() => output.playCue('moment'), /gain unavailable/);
  assert.equal(context.sources[0].stopped, true); assert.equal(context.sources[0].disconnected, true);
  assert.equal(context.gains[0].disconnected, true);
  output.enqueueMain({ pcm: pcm(320), sampleRate: 16000 }); assert.equal(context.sources[1].startedAt, 10);
  output.close();
});

test('PCM queue bounds reject oversized and malformed audio without extending the queue', () => {
  const context = audioContext(), output = new PcmPlayback({ context, maxQueuedSeconds: .2 });
  assert.throws(() => output.enqueueMain({ pcm: new Uint8Array([0]), sampleRate: 16000 }), /invalid_pcm16/);
  assert.throws(() => output.enqueueMain({ pcm: pcm(), sampleRate: 7999 }), /invalid_sample_rate/);
  assert.throws(() => output.enqueueMain({ pcm: pcm(4000), sampleRate: 16000 }), /audio_chunk_too_large/);
  assert.equal(output.enqueueMain({ pcm: pcm(), sampleRate: 16000 }), true);
  assert.equal(output.enqueueMain({ pcm: pcm(), sampleRate: 16000 }), true);
  assert.equal(output.enqueueMain({ pcm: pcm(), sampleRate: 16000 }), false);
  assert.equal(context.sources.length, 2); output.close();
});

test('suspended audio contexts fail silent and close cancels all scheduled future chunks', () => {
  const context = audioContext(), output = new PcmPlayback({ context, cueAssets: new Map([['neutral_ack', { pcm: pcm(), sampleRate: 16000 }]]) });
  context.state = 'suspended';
  assert.equal(output.playCue('neutral_ack'), null);
  assert.equal(output.enqueueMain({ pcm: pcm(), sampleRate: 16000 }), false);
  context.state = 'running';
  output.enqueueMain({ pcm: pcm(), sampleRate: 16000 }); output.enqueueMain({ pcm: pcm(), sampleRate: 16000 });
  output.close(); output.close();
  assert.equal(context.sources.every(source => source.stopped && source.disconnected), true);
  assert.equal(output.main.size, 0); assert.equal(output.playCue('neutral_ack'), null);
  assert.equal(output.enqueueMain({ pcm: pcm(), sampleRate: 16000 }), false);
  assert.equal(context.state, 'running', 'Shared context/microphone lifecycle remains transport-owned');
});

test('source start failure stops and disconnects the owned node', () => {
  const context = audioContext(), create = context.createBufferSource.bind(context);
  context.createBufferSource = () => { const source = create(); source.start = () => { throw new Error('browser start failed'); }; return source; };
  const output = new PcmPlayback({ context, cueAssets: { neutral_ack: { pcm: pcm(), sampleRate: 16000 } } });
  assert.throws(() => output.playCue('neutral_ack'), /browser start failed/);
  assert.equal(context.sources[0].stopped, true); assert.equal(context.sources[0].disconnected, true); output.close();
});

test('provisional pause retains the unsounded suffix and future chunks then resumes exact buffer offsets', () => {
  const context = audioContext(), output = new PcmPlayback({ context });
  output.enqueueMain({ pcm: pcm(16000), sampleRate: 16000 });
  output.enqueueMain({ pcm: pcm(8000), sampleRate: 16000 });
  context.currentTime = 10.4; output.pauseMain();
  assert.equal(context.sources[0].stopped, true); assert.equal(context.sources[1].stopped, true);
  assert.equal(output.pausedMain.length, 2);
  assert.ok(Math.abs(output.pausedMain[0].offset - .4) < .00001);
  assert.equal(output.pausedMain[1].offset, 0);
  assert.equal(output.enqueueMain({ pcm: pcm(), sampleRate: 16000 }), false);
  context.currentTime = 10.48; assert.equal(output.resumeMain(), true);
  assert.equal(context.sources[2].buffer, context.sources[0].buffer);
  assert.ok(Math.abs(context.sources[2].offset - .4) < .00001);
  assert.equal(context.sources[2].startedAt, 10.48);
  assert.ok(Math.abs(context.sources[3].startedAt - 11.08) < .00001);
  output.pauseMain(); output.stopMain();
  assert.equal(output.pausedMain, null); assert.equal(output.resumeMain(), true);
  assert.equal(context.sources.length, 4, 'Confirmed interruption never resurrects paused audio'); output.close();
});

test('20ms append-compatible ASR streaming produces a cue before caller end without cancelling or flooding Jev', async () => {
  const requests = [], time = clock(), output = playback(), events = [];
  const coordinator = new DuplexCoordinator({ playback: output, now: time.now, setTimeout: time.setTimeout,
    clearTimeout: time.clearTimeout, listeningDelayMs: 0, decisionDeadlineMs: 350, onEvent: event => events.push(event),
    selectCue: request => new Promise(resolve => {
      requests.push({ request, at: time.now() });
      time.setTimeout(() => resolve(decision('listening', request.text.split(/\s+/).length < 6 ? { phrase_id: 'silence' } : {})), 180);
    }) });
  coordinator.userSpeechStarted();
  const sentence = 'I would like routine service for my vehicle and I can describe the issue in more detail while you listen';
  for (let length = 1; length <= sentence.length; length++) {
    coordinator.updatePartial(sentence.slice(0, length), { revision: length }); await time.advance(20);
  }
  assert.equal(coordinator.userSpeaking, true); assert.equal(output.handles.length, 1);
  assert.ok(requests.length >= 2 && requests.length <= 5);
  assert.ok(requests.every(row => row.request.text.split(/\s+/).length >= 3));
  assert.ok(requests.every(row => !row.request.signal.aborted));
  assert.ok(requests.slice(1).every((row, index) => row.at - requests[index].at >= 350));
  const played = events.find(event => event.kind === 'cue_started');
  assert.ok(played.latest_revision > played.snapshot_revision, 'Evidence retains the earlier snapshot identity');
  assert.equal(coordinator.decisionDeadlineMs, 350, 'An explicitly strict listening decision deadline remains enforced');
  coordinator.close();
});

test('ordinary ASR trailing-word rewrites retain the classified stable prefix instead of starving it', async () => {
  const pending = deferred(); let selected;
  const { coordinator, time, output } = setup({ listeningDelayMs: 0,
    selectCue: request => { selected = request; return pending.promise; } });
  caller(coordinator, 'I need routine service for my vehi'); await time.advance(0);
  assert.equal(selected.text, 'I need routine service for my');
  for (const [index, text] of ['I need routine service for my vehicle',
    'I need routine service for my vehicle tomor', 'I need routine service for my vehicle tomorrow morning'].entries()) {
    coordinator.updatePartial(text, { revision: index + 2 }); await time.advance(40);
  }
  assert.equal(selected.signal.aborted, false);
  pending.resolve(decision('listening')); await flush();
  assert.equal(output.handles.length, 1); assert.equal(coordinator.userSpeaking, true);
  coordinator.close();
});
