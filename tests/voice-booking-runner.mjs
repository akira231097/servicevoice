/** Offline CLI and evidence regression. No credentials, provider or runtime DB. */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const script = path.join(root, 'scripts', 'voice_booking_smoke.mjs');
const guard = 'data:text/javascript,' + encodeURIComponent(`
  import fs from 'node:fs/promises';
  globalThis.fetch = () => { throw new Error('OFFLINE_NETWORK_FORBIDDEN'); };
  globalThis.WebSocket = class { constructor() { throw new Error('OFFLINE_MEDIA_FORBIDDEN'); } };
  fs.readFile = async () => { throw new Error('OFFLINE_FILE_READ_FORBIDDEN'); };
`);
function run(args, target = script) {
  return spawnSync(process.execPath, ['--import', guard, target, ...args], {
    cwd: os.tmpdir(), encoding: 'utf8', timeout: 5000,
    env: { ...process.env, VAPI_API_KEY: '', ELEVENLABS_API_KEY: '' },
  });
}
test('reference and telemetry fixtures have no file, network or media access', () => {
  const result = run(['--self-test']);
  assert.equal(result.status, 0, result.stderr);
  const evidence = JSON.parse(result.stdout);
  assert.equal(evidence.passed, 16);
  assert.equal(evidence.telemetry_assertions_passed, 11);
  assert.equal(evidence.jev_telemetry_assertions_passed, 12);
  assert.equal(evidence.booking_route_fixture_cases_passed, 15);
  assert.equal(evidence.booking_route_auxiliary_assertions_passed, 4);
  assert.equal(evidence.phone_correction_route_assertions_passed,5);
  assert.equal(evidence.phone_readback_fixtures_passed,6);
  assert.equal(evidence.phone_correction_route_audit_cases_passed,8);
  assert.equal(evidence.provider_calls, 0);
  assert.equal(evidence.file_reads, 0);
});
test('controlled comparison keeps the four original caller phrases', async () => {
  const source = await fs.readFile(script, 'utf8');
  const declaration = /const PHRASES = (\{[\s\S]*?\n\});/.exec(source)?.[1];
  assert.ok(declaration, 'The fixed phrase set must remain inspectable');
  assert.equal(createHash('sha256').update(declaration).digest('hex'),
    '299ef2a22c2bb5714177bc2f495f356e4c26cfbd6dc429ec3ea622e59cfeb520',
    'A phrase change invalidates the cached-audio comparison and must be a separate experiment');
});
test('invalid CLI labels and mixed modes stop before all external access', () => {
  for (const args of [ ['--output'], ['--output', '../escape'], ['--output', ''], ['--output', 'Upper'],
    ['--output', 'x'.repeat(61)], ['--output', '-hidden'], ['--output', 'valid', '--audit-existing'],
    ['--audit-existing', '--output', 'valid'], ['--self-test', '--output', 'valid'], ['--unknown'],
    ['--correct-phone-on-mismatch'], ['--correct-phone-on-mismatch','--self-test'],
    ['--exercise-phone-correction'], ['--exercise-phone-correction','--audit-existing'],
    ['--output','valid','--exercise-phone-correction','--correct-phone-on-mismatch'],
    ['--output','valid','--correct-phone-on-mismatch','--correct-phone-on-mismatch'] ]) {
    const result = run(args);
    assert.equal(result.status, 2, JSON.stringify(args) + result.stderr);
    assert.match(result.stderr, /Usage:/);
    assert.doesNotMatch(result.stderr, /OFFLINE_/);
  }
});
test('default and named reports are exclusively reserved before credential reads', async () => {
  const fixture = await fs.mkdtemp(path.join(os.tmpdir(), 'booking-cli-'));
  try {
    await fs.mkdir(path.join(fixture, 'scripts'));
    await fs.mkdir(path.join(fixture, 'docs'));
    const target = path.join(fixture, 'scripts', 'voice_booking_smoke.mjs');
    await fs.copyFile(script, target);
    for(const filename of ['booking_recovery_checks.mjs','voice_regression_checks.mjs','voice_runtime_paths.mjs'])await fs.copyFile(path.join(root,'scripts',filename),path.join(fixture,'scripts',filename));
    for (const [args, filename] of [ [[], 'voice-booking-smoke.json'],
      [['--output', 'baseline-test'], 'voice-booking-smoke-baseline-test.json'],
      [['--output','correction-test','--correct-phone-on-mismatch'],'voice-booking-smoke-correction-test.json'],
      [['--output','staged-test','--exercise-phone-correction'],'voice-booking-smoke-staged-test.json'] ]) {
      const report = path.join(fixture, 'docs', filename);
      await fs.writeFile(report, 'preserved evidence', { flag: 'wx' });
      const result = run(args, target);
      assert.notEqual(result.status, 0);
      assert.match(result.stderr, /EEXIST/);
      assert.doesNotMatch(result.stderr, /OFFLINE_/);
      assert.equal(await fs.readFile(report, 'utf8'), 'preserved evidence');
    }
  } finally {
    const resolved = path.resolve(fixture);
    assert.equal(path.dirname(resolved), path.resolve(os.tmpdir()));
    assert.ok(path.basename(resolved).startsWith('booking-cli-'));
    await fs.rm(resolved, { recursive: true, force: true });
  }
});
