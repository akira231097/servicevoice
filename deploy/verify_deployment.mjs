/** Local synthetic integration check. Stops/restarts ONLY the new Docker sandbox. */
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {execFileSync,spawnSync} from 'node:child_process';
import {createHash,randomUUID} from 'node:crypto';
import http from 'node:http';
const root=path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const label=process.argv[2];
if(!/^[a-z0-9][a-z0-9-]{0,45}$/.test(label||''))throw Error('Supply one unique lowercase evidence label.');
const target=path.join(root,'docs',`deployment-verification-${label}.json`);
const handle=await fs.open(target,'wx');
const report={kind:'local_container_deployment_verification',started_at:new Date().toISOString(),passed:false,
  provider_calls:0,cloud_deployments:0,checks:{},limitations:[]};
const hash=b=>createHash('sha256').update(b).digest('hex');
let stage='start', providerCreated=false, volumeCreated=false;
const providerName='servicevoice-deploy-check-'+label, volumeName=providerName+'-data';
const localName='servicevoice-local-servicevoice-1';
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
function docker(args,{failure=false,input}={}){
  try{return {code:0,text:execFileSync('docker',args,{cwd:root,encoding:'utf8',timeout:55000,maxBuffer:5_000_000,windowsHide:true,input,stdio:['pipe','pipe','pipe']})};}
  catch(error){if(failure)return {code:error.status??-1,text:String(error.stdout||''),stderr:String(error.stderr||'')};throw Error('docker_command_failed');}
}
function assert(value,name){report.checks[name]=Boolean(value);if(!value)throw Error(name);}
async function checkpoint(){const b=Buffer.from(JSON.stringify(report,null,2)+'\n');await handle.truncate(0);await handle.write(b,0,b.length,0);}
async function request(port,url,{method='GET',body,headers={}}={}){
  // Native http preserves a deliberately hostile Host header; Node24 fetch
  // silently substitutes the target Host and cannot validate that boundary.
  const payload=body?JSON.stringify(body):null;
  return await new Promise((resolve,reject)=>{
    const req=http.request({host:'127.0.0.1',port,path:url,method,headers:{...(payload?{'Content-Type':'application/json','Content-Length':Buffer.byteLength(payload)}:{}),...headers}},response=>{
      const chunks=[];let size=0;response.on('data',chunk=>{size+=chunk.length;if(size>262144){req.destroy(Error('response_too_large'));return;}chunks.push(chunk);});
      response.on('end',()=>{const text=Buffer.concat(chunks).toString('utf8');let data;try{data=JSON.parse(text);}catch{data=null;}resolve({status:response.statusCode,data,text});});
    });
    req.setTimeout(5000,()=>req.destroy(Error('request_timeout')));req.on('error',reject);req.end(payload);
  });
}
async function healthy(port){for(let i=0;i<30;i++){try{if((await request(port,'/healthz')).status===200)return;}catch{}await sleep(200);}throw Error('startup_deadline');}
try{
  stage='start_local';
  docker(['compose','-f','deploy/compose.local.yaml','up','-d','--no-build']);await healthy(8876);
  const inspection=JSON.parse(docker(['inspect',localName]).text)[0];
  report.image_id=inspection.Image;
  report.runtime={user:inspection.Config.User,network:'dedicated bridge; outbound connectivity possible',
    host_bindings:inspection.HostConfig.PortBindings,read_only_root:inspection.HostConfig.ReadonlyRootfs,
    cap_drop:inspection.HostConfig.CapDrop,memory_bytes:inspection.HostConfig.Memory,nanocpus:inspection.HostConfig.NanoCpus,
    pids_limit:inspection.HostConfig.PidsLimit,stop_timeout_seconds:inspection.Config.StopTimeout};
  assert(inspection.Config.User==='10001:10001'&&inspection.HostConfig.ReadonlyRootfs,'nonroot_readonly');
  assert(Object.values(inspection.HostConfig.PortBindings).flat().every(p=>p.HostIp==='127.0.0.1'),'host_ports_loopback_only');
  assert((await request(8876,'/readyz')).status===200,'keyless_local_ready');
  const home=await request(8875,'/');assert(home.status===200&&home.text.includes('Local Docker sandbox'),'sandbox_ui_labeled');
  assert((await request(8875,'/',{headers:{Host:'untrusted.example:8875'}})).status===403,'untrusted_host_rejected');
  assert((await request(8875,'/api/operator/capability',{headers:{Origin:'https://untrusted.example'}})).status===403,'cross_origin_rejected');
  assert((await request(8875,'/api/operator/cases')).status===401,'operator_token_required');
  for(const route of ['/','/api/config','/api/docs','/api/operator/capability','/advisor','/static/app.js'])
    assert((await request(8876,route,{headers:{Host:'localhost:8876'}})).status===404,'provider_private_route_absent_'+route);
  await checkpoint();

  stage='synthetic_booking_and_operator';
  const created=await request(8875,'/api/sessions',{method:'POST',body:{mode:'rehearsal'}});
  assert(created.status===200,'rehearsal_session_created');
  const session=created.data, sessionHeaders={'X-Session-Token':session.token};
  let snapshot;
  for(const text of ['oil change','next available','1','Deployment Fixture, 5125550199, 2021 Toyota Camry']){
    const response=await request(8875,`/api/sessions/${session.id}/message`,{method:'POST',headers:sessionHeaders,body:{text}});
    assert(response.status===200,'rehearsal_intake');snapshot=response.data;
  }
  // The public snapshot intentionally exposes only the active proposal, not
  // its internal status column; stage/kind/ID are its actual API contract.
  assert(snapshot.stage==='confirmation'&&snapshot.proposal?.kind==='book'&&snapshot.proposal?.id&&!snapshot.appointment,'no_booking_before_confirmation');
  snapshot=(await request(8875,`/api/sessions/${session.id}/message`,{method:'POST',headers:sessionHeaders,body:{text:'confirm'}})).data;
  assert(snapshot.appointment?.status==='confirmed','synthetic_booking_confirmed');
  const appointmentId=snapshot.appointment.id;
  snapshot=(await request(8875,`/api/sessions/${session.id}/message`,{method:'POST',headers:sessionHeaders,body:{text:'advisor: synthetic deployment follow-up'}})).data;
  assert(Boolean(snapshot.handoff),'synthetic_advisor_case_created');
  const capability=(await request(8875,'/api/operator/capability')).data.token;
  const operatorHeaders={'X-Operator-Token':capability};
  const cases=(await request(8875,'/api/operator/cases?status=all', {headers:operatorHeaders})).data.items;
  let advisor=cases.find(c=>c.session_id===session.id);assert(Boolean(advisor),'operator_lists_case');
  for(const status of ['in_progress','resolved']){
    const saved=await request(8875,`/api/operator/cases/${advisor.id}/status`,{method:'POST',headers:operatorHeaders,
      body:{expected_revision:advisor.revision,status,operator:'Local deployment fixture',note:'Synthetic local review; nobody was contacted.'}});
    assert(saved.status===200,'operator_'+status);advisor=saved.data;
  }
  assert(advisor.context.appointment.id===appointmentId&&advisor.status==='resolved'&&advisor.revision===2,'operator_context_preserved');
  await checkpoint();

  stage='backup_restore';
  const backup='/data/deployment-'+label+'-backup.sqlite3',restored='/data/deployment-'+label+'-restored.sqlite3';
  const base=['exec',localName,'python','scripts/database_backup.py'];
  report.backup=JSON.parse(docker([...base,'backup','--source','/data/servicevoice.sqlite3','--destination',backup,'--timeout','15']).text);
  report.restore=JSON.parse(docker([...base,'restore','--source',backup,'--destination',restored,'--timeout','15']).text);
  report.restore_verify=JSON.parse(docker([...base,'verify','--source',restored,'--timeout','15']).text);
  assert(Boolean(report.backup)&&Boolean(report.restore)&&Boolean(report.restore_verify),'linux_backup_restore_verified');

  stage='restart_persistence';
  const stopAt=Date.now();docker(['stop','--time','40',localName]);
  const stopped=JSON.parse(docker(['inspect',localName]).text)[0].State;
  // Uvicorn0.54 intentionally re-raises SIGTERM after draining.143 can be a
  // clean stop;137 cannot. Require positive drain evidence and then persistence.
  const allLogs=spawnSync('docker',['logs','--tail','20',localName],{encoding:'utf8',timeout:5000,windowsHide:true});
  const logText=String(allLogs.stdout||'')+String(allLogs.stderr||'');
  report.shutdown={exit_code:stopped.ExitCode,oom_killed:stopped.OOMKilled,elapsed_ms:Date.now()-stopAt,
    application_shutdown_complete:logText.includes('Application shutdown complete.'),log_sha256:hash(logText),
    semantics:'Native Uvicorn SIGTERM handling retained;143 is accepted only with completed shutdown and verified restart persistence.'};
  assert([0,143].includes(stopped.ExitCode)&&!stopped.Running&&!stopped.OOMKilled
    &&allLogs.status===0&&report.shutdown.application_shutdown_complete,'sigterm_shutdown_not_forced');
  docker(['start',localName]);await healthy(8876);
  assert((await request(8875,'/api/operator/cases',{headers:operatorHeaders})).status===401,'operator_token_rotated_after_restart');
  const newToken=(await request(8875,'/api/operator/capability')).data.token;
  const persisted=(await request(8875,`/api/operator/cases/${advisor.id}`,{headers:{'X-Operator-Token':newToken}})).data;
  assert(persisted.status==='resolved'&&persisted.revision===2&&persisted.context.appointment.id===appointmentId,'booking_and_operator_state_survive_restart');
  const state=(await request(8875,`/api/sessions/${session.id}`,{headers:sessionHeaders})).data;
  assert(state.appointment?.id===appointmentId,'session_state_survives_restart');
  await checkpoint();

  stage='image_and_runtime_guards';
  const inspectPython="import os,json,sys,hashlib,importlib.metadata as m; from pathlib import Path; from runtime_sqlite import ensure_sqlite; root=Path('/app'); files=[p for p in root.rglob('*') if p.is_file()]; canary=b'synthetic-exclusion-check-2026-10-18-26df8c74'; print(json.dumps({'uid':os.getuid(),'python':sys.version.split()[0],'sqlite':ensure_sqlite(),'sqlite_build':json.loads(Path('/opt/sqlite/build-manifest.json').read_text()),'forbidden_files':[str(p.relative_to(root)) for p in files if p.name.startswith('.env') or p.suffix in ('.dll','.sqlite3') or any(x in p.parts for x in ('docs','work','.local','node_modules'))],'canary_found':any(canary in p.read_bytes() for p in files),'dependencies':{d.metadata['Name']:d.version for d in m.distributions()},'source_sha256':{str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.suffix in ('.py','.txt')}}))";
  report.image_runtime=JSON.parse(docker(['run','--rm','--network','none','--read-only','--cap-drop','ALL','--entrypoint','python','servicevoice-app:local','-c',inspectPython]).text);
  assert(report.image_runtime.uid===10001&&report.image_runtime.sqlite==='3.54.1','patched_sqlite_and_nonroot_actual');
  assert(!report.image_runtime.canary_found&&report.image_runtime.forbidden_files.length===0,'image_excludes_canary_credentials_customer_data');
  const older=docker(['run','--rm','--network','none','--read-only','--env','LD_LIBRARY_PATH=','--entrypoint','python','servicevoice-app:local','-c','from runtime_sqlite import ensure_sqlite; ensure_sqlite()'],{failure:true});
  assert(older.code!==0&&older.stderr.includes('SQLite 3.51.3+ is required'),'older_sqlite_rejected');
  const duplicate=docker(['run','--rm','--network','none','--read-only','--mount','type=volume,source=servicevoice-local_sandbox-data,target=/data','servicevoice-app:local'],{failure:true});
  assert(duplicate.code!==0&&duplicate.stderr.includes('already owns this data volume'),'second_process_on_same_volume_rejected');

  stage='provider_only_synthetic_boundary';
  docker(['volume','create',volumeName]);volumeCreated=true;
  const phoneSecret='synthetic-phone-'+randomUUID()+randomUUID(),gatewaySecret='synthetic-gateway-'+randomUUID()+randomUUID(),browserSecret='synthetic-browser-'+randomUUID()+randomUUID();
  const config={SERVICEVOICE_TELEPHONY_ENABLED:'on',VAPI_MODEL_PROFILE:'gemini-3.8-flash-custom',
    VAPI_PHONE_ORG_ID:'11111111-1111-4111-8111-111111111111',VAPI_PHONE_NUMBER_ID:'22222222-2222-4222-8222-222222222222',
    VAPI_PHONE_ASSISTANT_ID:'33333333-3333-4333-8333-333333333333',VAPI_ASSISTANT_ID:'44444444-4444-4444-8444-444444444444',
    VAPI_PHONE_WEBHOOK_SECRET:phoneSecret,GEMINI_GATEWAY_SECRET:gatewaySecret,VAPI_WEBHOOK_SECRET:browserSecret,GEMINI_API_KEY:'synthetic-not-a-provider-key'};
  const args=['run','-d','--name',providerName,'--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--pids-limit','128','--memory','512m','--cpus','1',
    '--tmpfs','/tmp:rw,nosuid,nodev,noexec,size=16m','--mount',`type=volume,source=${volumeName},target=/data`,'-p','127.0.0.1:8877:8000'];
  for(const [key,value] of Object.entries(config))args.push('--env',key+'='+value);
  docker([...args,'servicevoice-app:local']);providerCreated=true;await healthy(8877);
  assert((await request(8877,'/readyz')).status===200,'synthetic_provider_configuration_ready');
  for(const route of ['/','/api/config','/api/operator/capability','/static/app.js','/api/docs'])
    assert((await request(8877,route)).status===404,'provider_only_mode_private_route_absent_'+route);
  for(const route of ['/webhooks/vapi','/webhooks/vapi/inbound','/llm/chat/completions'])
    assert((await request(8877,route,{method:'POST',body:{}})).status===401,'unauthorized_rejected_'+route);
  const call={id:randomUUID(),type:'inboundPhoneCall',orgId:config.VAPI_PHONE_ORG_ID,phoneNumberId:config.VAPI_PHONE_NUMBER_ID};
  const admitted=await request(8877,'/webhooks/vapi/inbound',{method:'POST',headers:{Authorization:'Bearer '+phoneSecret},body:{message:{type:'assistant-request',call}}});
  assert(admitted.status===200&&admitted.data.assistantId===config.VAPI_PHONE_ASSISTANT_ID,'synthetic_inbound_admission_no_provider_request');
  const providerInspect=JSON.parse(docker(['inspect',providerName]).text)[0];
  assert(!Object.hasOwn(providerInspect.HostConfig.PortBindings,'8001/tcp'),'provider_only_no_console_port');
  report.synthetic_provider={inbound_admissions:1,external_generations:0,actual_phone_calls:0};
  await checkpoint();
  report.passed=Object.values(report.checks).every(Boolean);
}catch{report.failed_stage=stage;report.passed=false;process.exitCode=1;}
finally{
  if(providerCreated){docker(['stop','--time','40',providerName],{failure:true});docker(['rm',providerName],{failure:true});}
  if(volumeCreated)docker(['volume','rm',volumeName],{failure:true});
  report.finished_at=new Date().toISOString();
  report.limitations=['Local Docker rehearsal uses a separate named volume and has no provider credentials. Existing host8875/8876 are not the native8765/8766 service.',
    'Dedicated bridge permits outbound networking; no external model/provider call is made by this test.',
    'Readiness checks local DB/config only, not external credentials, provider availability or actual calls.',
    'Single process/instance per data volume; no multiworker or horizontal scaling claim.',
    'SIGTERM was verified without in-flight remote requests; blocked OS I/O has no hard drain guarantee.',
    'The local sandbox and its synthetic data remain available; temporary provider test container/volume are removed.'];
  report.source_sha256={};
  for(const file of ['Dockerfile','.dockerignore','requirements-lock.txt','runtime_sqlite.py','deploy/server.py','deploy/build_sqlite.py','deploy/runtime_health.py','deploy/label_sandbox.py','deploy/compose.local.yaml','deploy/compose.provider.yaml','deploy/test_server.py','deploy/verify_deployment.mjs','scripts/database_backup.py'])
    report.source_sha256[file]=hash(await fs.readFile(path.join(root,file)));
  await checkpoint();await handle.close();
  console.log(JSON.stringify({report:path.relative(root,target),passed:report.passed,failed_stage:report.failed_stage,checks:report.checks},null,2));
}
