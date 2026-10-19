/** Local prerequisites for synthetic voice runners. No network or database creation.
 * SERVICEVOICE_PYTHON is an executable path, absolute or relative to the project.
 * Existing DB/cache defaults deliberately remain compatible with earlier runs.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import {execFileSync} from 'node:child_process';

export function runtimePaths(projectRoot, env = {}) {
  const root = path.resolve(projectRoot);
  return {
    databasePath:path.resolve(root, env.SERVICEVOICE_DB || '../../work/runtime/servicevoice.sqlite3'),
    databaseSource:env.SERVICEVOICE_DB ? 'configured_database' : 'legacy_default',
    callerPcmCachePath:path.resolve(root, '../../work/runtime/voice-eval-pcm'),
    qualityPcmCachePath:path.resolve(root, '../../work/runtime/voice-quality-pcm'),
  };
}

export async function resolvePython(projectRoot, env = {}, {platform = process.platform, stat = fs.stat} = {}) {
  const root = path.resolve(projectRoot);
  const suffix = platform === 'win32' ? ['Scripts','python.exe'] : ['bin','python'];
  const explicit = env.SERVICEVOICE_PYTHON;
  const candidates = explicit ? [{path:path.resolve(root, explicit), source:'explicit_override'}] : [
    {path:path.join(root, '.venv', ...suffix), source:'project_venv'},
    {path:path.resolve(root, '../../work/venv', ...suffix), source:'legacy_workspace_venv'},
  ];
  for (const candidate of candidates) {
    try { if ((await stat(candidate.path)).isFile()) return candidate; }
    catch (error) { if (!['ENOENT','ENOTDIR'].includes(error.code)) throw new Error('python_runtime_unavailable'); }
  }
  throw new Error(explicit ? 'python_override_unavailable' : 'python_runtime_unavailable');
}

const DATABASE_PROBE = `from runtime_sqlite import ensure_sqlite
sqlite_version=ensure_sqlite()
import json,sqlite3,sys
from pathlib import Path
from contextlib import closing
from scripts.inspect_vapi_calls import local_evidence
if sys.version_info < (3,12): raise RuntimeError('unsupported_python')
database=Path(sys.argv[1]).resolve()
if not database.is_file(): raise RuntimeError('database_missing')
with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
 db.execute('PRAGMA query_only=ON')
 db.execute('SELECT id,tenant,provider_call_id,mode,ended FROM sessions LIMIT 0')
 db.execute('SELECT id,reference,status,service,slot_id,tenant,session_id FROM appointments LIMIT 0')
 db.execute('SELECT id,kind,state,payload,expires,tenant,session_id FROM proposals LIMIT 0')
 db.execute('SELECT name,status,session_id FROM events LIMIT 0')
print(json.dumps({'version':1,'python':list(sys.version_info[:3]),'sqlite':sqlite_version,'database_read_only':True,'schema_compatible':True}))`;

const SESSION_PROBE = `from runtime_sqlite import ensure_sqlite
ensure_sqlite()
import json,sqlite3,sys
from pathlib import Path
from contextlib import closing
database=Path(sys.argv[1]).resolve()
if not database.is_file(): raise RuntimeError('database_missing')
with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
 db.execute('PRAGMA query_only=ON')
 row=db.execute('SELECT id,mode,ended FROM sessions WHERE id=? AND tenant=?',(sys.argv[2],'cedar-demo')).fetchone()
 if row != (sys.argv[2],'live',0): raise RuntimeError('local_session_database_mismatch')
print(json.dumps({'session_database_matched':True}))`;

function probe(executable, projectRoot, code, args, execute) {
  // Never expose child stderr, executable paths or inherited credentials in reports.
  const output = execute(executable, ['-B','-c',code,...args], {cwd:path.resolve(projectRoot),timeout:5000,maxBuffer:16*1024,
    windowsHide:true, encoding:'utf8',stdio:['ignore','pipe','pipe']});
  return JSON.parse(output);
}

export async function preflightBookingRuntime({projectRoot, env = {}, platform = process.platform, stat = fs.stat, execute = execFileSync}) {
  const python = await resolvePython(projectRoot, env, {platform, stat});
  const paths = runtimePaths(projectRoot, env);
  try { if (!(await stat(paths.databasePath)).isFile()) throw new Error('not_file'); }
  catch { throw new Error('runtime_database_missing'); }
  let checked;
  try {
    checked = probe(python.path, projectRoot, DATABASE_PROBE, [paths.databasePath], execute);
    if (checked?.version !== 1 || checked.database_read_only !== true || checked.schema_compatible !== true ||
        !Array.isArray(checked.python) || checked.python.length !== 3 || !checked.python.every(Number.isSafeInteger) ||
        checked.python[0] !== 3 || checked.python[1] < 12 || !/^\d+\.\d+\.\d+$/.test(checked.sqlite)) throw new Error('invalid_probe');
  } catch { throw new Error('runtime_verification_preflight_failed'); }
  return {
    pythonPath:python.path, ...paths,
    evidence:{python_source:python.source,database_source:paths.databaseSource,python_version:checked.python.join('.'),sqlite_version:checked.sqlite,
      database_read_only:true,schema_compatible:true},
  };
}

export function verifyLocalSessionDatabase(runtime, projectRoot, sessionId, {execute = execFileSync} = {}) {
  try {
    if (typeof sessionId !== 'string' || !/^sess_[a-zA-Z0-9_-]{1,128}$/.test(sessionId)) throw new Error('invalid_session');
    const checked = probe(runtime.pythonPath, projectRoot, SESSION_PROBE, [runtime.databasePath,sessionId], execute);
    if (checked?.session_database_matched !== true) throw new Error('invalid_probe');
    return true;
  } catch { throw new Error('local_session_database_mismatch'); }
}
