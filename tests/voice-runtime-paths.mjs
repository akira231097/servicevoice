/** Runtime portability checks. Synthetic files/probes only; no provider or live DB. */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {spawnSync} from 'node:child_process';
import {runtimePaths, resolvePython, preflightBookingRuntime, verifyLocalSessionDatabase} from '../scripts/voice_runtime_paths.mjs';

const project = path.resolve('synthetic-project');
const probeResult = {version:1,python:[3,12,10],sqlite:'3.54.1',database_read_only:true,schema_compatible:true};
function fakeStat(files) {
  return async filename => {
    if (!files.has(filename)) { const error = new Error('missing synthetic file'); error.code = 'ENOENT'; throw error; }
    return {isFile:() => files.get(filename) !== 'directory'};
  };
}
const paths = runtimePaths(project);
const projectPython = path.join(project,'.venv','Scripts','python.exe');
const legacyPython = path.resolve(project,'../../work/venv/Scripts/python.exe');

test('explicit executable path wins over project and legacy interpreters, including spaces', async () => {
  const explicit = path.resolve(project,'custom runtime','python.exe');
  const files = new Map([[explicit,true],[projectPython,true],[legacyPython,true]]);
  const result = await resolvePython(project,{SERVICEVOICE_PYTHON:'custom runtime/python.exe'},{platform:'win32',stat:fakeStat(files)});
  assert.deepEqual(result,{path:explicit,source:'explicit_override'});
});

test('missing explicit interpreter fails closed rather than falling back', async () => {
  await assert.rejects(resolvePython(project,{SERVICEVOICE_PYTHON:'missing/python.exe'},
    {platform:'win32',stat:fakeStat(new Map([[projectPython,true],[legacyPython,true]]))}),/^Error: python_override_unavailable$/);
});

test('standalone project venv is preferred; absent project venv retains legacy compatibility', async () => {
  const withBoth = new Map([[projectPython,true],[legacyPython,true]]);
  assert.deepEqual(await resolvePython(project,{}, {platform:'win32',stat:fakeStat(withBoth)}),{path:projectPython,source:'project_venv'});
  assert.deepEqual(await resolvePython(project,{}, {platform:'win32',stat:fakeStat(new Map([[legacyPython,true]]))}),{path:legacyPython,source:'legacy_workspace_venv'});
});

test('POSIX venv uses bin/python and no undocumented PATH fallback', async () => {
  const posix = path.join(project,'.venv','bin','python');
  assert.equal((await resolvePython(project,{}, {platform:'linux',stat:fakeStat(new Map([[posix,true]]))})).path,posix);
  await assert.rejects(resolvePython(project,{}, {platform:'linux',stat:fakeStat(new Map())}),/python_runtime_unavailable/);
});

test('directories and inaccessible executables are not accepted as available', async () => {
  await assert.rejects(resolvePython(project,{}, {platform:'win32',stat:fakeStat(new Map([[projectPython,'directory']]))}),/python_runtime_unavailable/);
  await assert.rejects(resolvePython(project,{}, {platform:'win32',stat:async()=>{const error=new Error('private path');error.code='EACCES';throw error;}}),/^Error: python_runtime_unavailable$/);
});

test('database and PCM cache resolution preserves established environment and default semantics', () => {
  assert.equal(paths.databasePath,path.resolve(project,'../../work/runtime/servicevoice.sqlite3'));
  assert.equal(paths.callerPcmCachePath,path.resolve(project,'../../work/runtime/voice-eval-pcm'));
  assert.equal(paths.qualityPcmCachePath,path.resolve(project,'../../work/runtime/voice-quality-pcm'));
  assert.equal(paths.databaseSource,'legacy_default');
  assert.equal(runtimePaths(project,{SERVICEVOICE_DB:'data/existing.sqlite3'}).databasePath,path.resolve(project,'data/existing.sqlite3'));
  const absolute = path.resolve(os.tmpdir(),'existing.sqlite3');
  assert.equal(runtimePaths(project,{SERVICEVOICE_DB:absolute}).databasePath,absolute);
  assert.equal(runtimePaths(project,{SERVICEVOICE_DB:''}).databasePath,paths.databasePath);
});

test('missing database stops preflight without executing Python or creating a file', async () => {
  let called = false;
  await assert.rejects(preflightBookingRuntime({projectRoot:project,platform:'win32',stat:fakeStat(new Map([[projectPython,true]])),execute:()=>{called=true;}}),/^Error: runtime_database_missing$/);
  assert.equal(called,false);
});

test('preflight runs the chosen interpreter with bounded read-only schema probe and allowlisted evidence', async () => {
  let captured;
  const runtime = await preflightBookingRuntime({projectRoot:project,platform:'win32',stat:fakeStat(new Map([[projectPython,true],[paths.databasePath,true]])),
    execute:(file,args,options)=>{captured={file,args,options};return JSON.stringify({...probeResult,secret:'must-not-leak',path:'private'});}});
  assert.equal(captured.file,projectPython);
  assert.equal(captured.args[0],'-B'); assert.equal(captured.args[1],'-c');
  assert.match(captured.args[2],/ensure_sqlite\(\)/);
  assert.match(captured.args[2],/from scripts\.inspect_vapi_calls import local_evidence/);
  assert.match(captured.args[2],/\?mode=ro/); assert.match(captured.args[2],/PRAGMA query_only=ON/);
  assert.equal((captured.args[2].match(/LIMIT 0/g)||[]).length,4);
  assert.doesNotMatch(captured.args[2],/CREATE |INSERT |UPDATE |DELETE /);
  assert.equal(captured.args[3],paths.databasePath);
  assert.equal(captured.options.cwd,project); assert.equal(captured.options.timeout,5000);
  assert.equal(captured.options.maxBuffer,16*1024); assert.equal(captured.options.windowsHide,true);
  assert.deepEqual(captured.options.stdio,['ignore','pipe','pipe']);
  assert.deepEqual(runtime.evidence,{python_source:'project_venv',database_source:'legacy_default',python_version:'3.12.10',sqlite_version:'3.54.1',database_read_only:true,schema_compatible:true});
  assert.doesNotMatch(JSON.stringify(runtime.evidence),/secret|private|synthetic-project/);
});

test('broken selected interpreter and incompatible probes stop without raw stderr or legacy retry', async () => {
  for (const result of [()=>{throw new Error('stderr private token');}, ()=>'not json',()=>JSON.stringify({...probeResult,python:[3,11,9]}),
    ()=>JSON.stringify({...probeResult,database_read_only:false}),()=>JSON.stringify({...probeResult,schema_compatible:false})]) {
    let calls=0;
    await assert.rejects(preflightBookingRuntime({projectRoot:project,platform:'win32',stat:fakeStat(new Map([[projectPython,true],[legacyPython,true],[paths.databasePath,true]])),
      execute:()=>{calls++;return result();}}),/^Error: runtime_verification_preflight_failed$/);
    assert.equal(calls,1);
  }
});

test('pre-call session probe requires the same live local session and cannot create a database', () => {
  const runtime={pythonPath:projectPython,databasePath:paths.databasePath}; let captured;
  assert.equal(verifyLocalSessionDatabase(runtime,project,'sess_fixture',{execute:(file,args,options)=>{captured={file,args,options};return JSON.stringify({session_database_matched:true});}}),true);
  assert.equal(captured.file,projectPython); assert.equal(captured.args.at(-1),'sess_fixture');
  assert.match(captured.args[2],/WHERE id=\? AND tenant=\?/);
  assert.match(captured.args[2],/'live',0/); assert.match(captured.args[2],/\?mode=ro/);
  assert.doesNotMatch(captured.args[2],/CREATE |INSERT |UPDATE |DELETE /);
  assert.throws(()=>verifyLocalSessionDatabase(runtime,project,'sess_fixture',{execute:()=>JSON.stringify({session_database_matched:false})}),/local_session_database_mismatch/);
  assert.throws(()=>verifyLocalSessionDatabase(runtime,project,'../escape',{execute:()=>{throw new Error('must never execute');}}),/^Error: local_session_database_mismatch$/);
});

test('actual booking runner fails before any network, TTS, session or Vapi call when override is missing', async () => {
  const temporary=await fs.mkdtemp(path.join(os.tmpdir(),'voice-runtime-preflight-'));
  try {
    const sourceRoot=path.dirname(path.dirname(fileURLToPath(import.meta.url)));
    const copy=path.join(temporary,'standalone-servicevoice');
    await fs.mkdir(path.join(copy,'scripts'),{recursive:true}); await fs.mkdir(path.join(copy,'docs'));
    for (const name of ['voice_booking_smoke.mjs','booking_recovery_checks.mjs','voice_regression_checks.mjs','voice_runtime_paths.mjs'])
      await fs.copyFile(path.join(sourceRoot,'scripts',name),path.join(copy,'scripts',name));
    await fs.writeFile(path.join(copy,'.env'),'VAPI_API_KEY=synthetic-private-key\nVAPI_ASSISTANT_ID=synthetic-assistant\nELEVENLABS_API_KEY=synthetic-tts-key\nVAPI_ELEVENLABS_VOICE_ID=synthetic-voice\nSERVICEVOICE_PYTHON=missing/python.exe\n');
    const guard='data:text/javascript,'+encodeURIComponent(`globalThis.fetch=()=>{throw new Error('OFFLINE_NETWORK_FORBIDDEN');};globalThis.WebSocket=class{static CONNECTING=0;static OPEN=1;static CLOSED=3;constructor(){throw new Error('OFFLINE_MEDIA_FORBIDDEN');}};`);
    const env=Object.fromEntries(Object.entries(process.env).filter(([key])=>!/KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|VAPI|ELEVENLABS|GEMINI|GOOGLE|SERVICEVOICE_PYTHON|SERVICEVOICE_DB/i.test(key)));
    const result=spawnSync(process.execPath,['--import',guard,path.join(copy,'scripts','voice_booking_smoke.mjs'),'--output','preflight-fixture'],{cwd:os.tmpdir(),env,encoding:'utf8',timeout:5000});
    assert.equal(result.error,undefined); assert.equal(result.status,1,result.stderr);
    const report=JSON.parse(await fs.readFile(path.join(copy,'docs','voice-booking-smoke-preflight-fixture.json'),'utf8'));
    assert.equal(report.failure,'python_override_unavailable');
    assert.equal(report.provider_call_created,false); assert.equal(report.booking_smoke_passed,false);
    assert.equal(report.session_id,undefined); assert.equal(report.input_turns.length,0);
    assert.doesNotMatch(result.stdout,/OFFLINE_NETWORK_FORBIDDEN|synthetic-private-key|synthetic-tts-key/);
    assert.equal((await fs.readdir(copy)).includes('work'),false);
  } finally {
    assert.equal(path.dirname(path.resolve(temporary)),path.resolve(os.tmpdir()));
    assert.ok(path.basename(temporary).startsWith('voice-runtime-preflight-'));
    await fs.rm(temporary,{recursive:true,force:true});
  }
});

test('actual runner rejects a schema-compatible wrong database before paid TTS and ends its local session', async () => {
  const temporary=await fs.mkdtemp(path.join(os.tmpdir(),'voice-runtime-ordering-'));
  try {
    const sourceRoot=path.dirname(path.dirname(fileURLToPath(import.meta.url)));
    const copy=path.join(temporary,'standalone-servicevoice');
    await fs.mkdir(path.join(copy,'scripts'),{recursive:true}); await fs.mkdir(path.join(copy,'docs'));
    for (const name of ['voice_booking_smoke.mjs','booking_recovery_checks.mjs','voice_regression_checks.mjs','voice_runtime_paths.mjs'])
      await fs.copyFile(path.join(sourceRoot,'scripts',name),path.join(copy,'scripts',name));
    await fs.writeFile(path.join(copy,'.env'),'VAPI_API_KEY=synthetic-private-key\nVAPI_ASSISTANT_ID=synthetic-assistant\nELEVENLABS_API_KEY=synthetic-tts-key\nVAPI_ELEVENLABS_VOICE_ID=synthetic-voice\nSERVICEVOICE_PYTHON=synthetic-python.exe\nSERVICEVOICE_DB=wrong.sqlite3\n');
    // Placeholder files satisfy stat; every Python execution is intercepted.
    await fs.writeFile(path.join(copy,'synthetic-python.exe'),'not executable');
    await fs.writeFile(path.join(copy,'wrong.sqlite3'),'not a real database');
    const preload=path.join(temporary,'offline-provider.mjs'),statsFile=path.join(temporary,'stats.json');
    await fs.writeFile(preload,`
import fs from 'node:fs';
import childProcess from 'node:child_process';
import {syncBuiltinESMExports} from 'node:module';
const events=[];
const record=value=>{events.push(value);fs.writeFileSync(process.env.VOICE_RUNTIME_STATS,JSON.stringify(events));};
childProcess.execFileSync=(file,args)=>{
  if(args[2].includes("'session_database_matched'")){
    record('probe-session-mismatch');return JSON.stringify({session_database_matched:false});
  }
  if(!args[2].includes('LIMIT 0'))throw new Error('unexpected_subprocess');
  record('probe-schema');return JSON.stringify({version:1,python:[3,12,10],sqlite:'3.54.1',database_read_only:true,schema_compatible:true});
};
syncBuiltinESMExports();
const json=value=>new Response(JSON.stringify(value),{headers:{'Content-Type':'application/json'}});
globalThis.fetch=async(url,options={})=>{
  const parsed=new URL(url),method=options.method||'GET';
  record(parsed.hostname+':'+method+':'+parsed.pathname);
  if(parsed.hostname!=='127.0.0.1')throw new Error('PAID_PROVIDER_FORBIDDEN');
  if(parsed.pathname==='/api/config')return json({live_voice_available:true,vapi_assistant_id:'synthetic-assistant'});
  if(parsed.pathname==='/api/sessions'&&method==='POST')return json({id:'sess_fixture',token:'synthetic-session-token'});
  if(parsed.pathname==='/api/sessions/sess_fixture/end'&&method==='POST')return json({ended:true});
  if(parsed.pathname==='/api/sessions/sess_fixture')return json({id:'sess_fixture',mode:'live',transcript:[],events:[],pending_operations:[]});
  throw new Error('unexpected_local_request');
};
globalThis.WebSocket=class{static CONNECTING=0;static OPEN=1;static CLOSED=3;constructor(){throw new Error('OFFLINE_MEDIA_FORBIDDEN');}};
`);
    const env=Object.fromEntries(Object.entries(process.env).filter(([key])=>!/KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|VAPI|ELEVENLABS|GEMINI|GOOGLE|SERVICEVOICE_PYTHON|SERVICEVOICE_DB/i.test(key)));
    env.VOICE_RUNTIME_STATS=statsFile;
    const result=spawnSync(process.execPath,['--import',pathToFileURL(preload).href,path.join(copy,'scripts','voice_booking_smoke.mjs'),'--output','ordering-fixture'],{cwd:os.tmpdir(),env,encoding:'utf8',timeout:5000});
    assert.equal(result.error,undefined); assert.equal(result.status,1,result.stderr);
    assert.equal(result.stderr,'');
    const report=JSON.parse(await fs.readFile(path.join(copy,'docs','voice-booking-smoke-ordering-fixture.json'),'utf8'));
    const events=JSON.parse(await fs.readFile(statsFile,'utf8'));
    assert.equal(report.failure,'local_session_database_mismatch');
    assert.equal(report.local_runtime_preflight.schema_compatible,true);
    assert.equal(report.local_runtime_preflight.session_database_matched,undefined);
    assert.equal(report.provider_call_created,false); assert.equal(report.local_session_ended,true);
    assert.equal(report.session_id,'sess_fixture'); assert.equal(report.call_observation_seconds,0);
    assert.equal(report.input_turns.length,0);
    assert.deepEqual(events,['probe-schema','127.0.0.1:GET:/api/config','127.0.0.1:POST:/api/sessions',
      '127.0.0.1:GET:/api/sessions/sess_fixture','probe-session-mismatch',
      '127.0.0.1:POST:/api/sessions/sess_fixture/end','127.0.0.1:GET:/api/sessions/sess_fixture']);
    assert.doesNotMatch(result.stdout,/synthetic-private-key|synthetic-tts-key|synthetic-session-token/);
    assert.equal(await fs.readFile(path.join(copy,'wrong.sqlite3'),'utf8'),'not a real database');
  } finally {
    assert.equal(path.dirname(path.resolve(temporary)),path.resolve(os.tmpdir()));
    assert.ok(path.basename(temporary).startsWith('voice-runtime-ordering-'));
    await fs.rm(temporary,{recursive:true,force:true});
  }
});
