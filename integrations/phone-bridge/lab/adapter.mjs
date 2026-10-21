import { writeFileSync, mkdirSync, chmodSync, statSync } from 'node:fs';
import http from 'node:http';
import { createPhoneBridge } from '/repo/integrations/phone-bridge/server.mjs';
import { PhonePlayback, PhoneDownsampler } from '/repo/integrations/phone-bridge/media.mjs';
const id='11111111-1111-4111-8111-111111111111', org='22222222-2222-4222-8222-222222222222', phone='33333333-3333-4333-8333-333333333333';
const call={id,orgId:org,phoneNumberId:phone,type:'inboundPhoneCall',status:'ringing'};
const tone=(n,hz)=>{const b=Buffer.alloc(n*2);for(let i=0;i<n;i++)b.writeInt16LE(Math.round(8000*Math.sin(2*Math.PI*hz*i/16000)),i*2);return b;};
const cue=tone(10720,440), main=tone(16000,700), events=[];
const assets={moment:{pcm:cue,sampleRate:16000}};
const expected=new PhonePlayback({cueAssets:assets});expected.playCue('moment');expected.enqueueMain({pcm:main,sampleRate:16000});
const down=new PhoneDownsampler(), out=[];for(let i=0;i<167;i++)out.push(down.push(expected.render()));
writeFileSync('/out/expected-output.pcm',Buffer.concat(out));
let session;
class LabSession {
 constructor({runtime}){this.runtime=runtime;this.bytes=0;this.frames=0;session=this;this.input=[];this.speech=[];}
 async start(){this.mic=this.runtime.microphoneFactory({onFrame:()=>{this.frames++;this.lastEncodedAt=performance.now();},
  onSpeech:active=>this.speech.push({active,at_ms:performance.now(),frames:this.frames})});await this.mic.start();
  const push=this.mic.push.bind(this.mic);this.mic.push=b=>{this.bytes+=b.length;this.lastCarrierAt=performance.now();this.input.push(Buffer.from(b));push(b);};
  this.play=this.runtime.playbackFactory({cueAssets:assets});this.play.playCue('moment');this.play.enqueueMain({pcm:main,sampleRate:16000});
  this.callId='44444444-4444-4444-8444-444444444444';this.active=true;}
 async stop(){this.active=false;await this.mic?.stop();this.play?.close();}
}
let reads=0;
mkdirSync('/run/servicevoice-phone',{recursive:true,mode:0o700});
const bridge=createPhoneBridge({httpSocketPath:'/run/servicevoice-phone/ingress.sock',ownershipFile:'/run/servicevoice-phone/ownership.json',assistantId:'44444444-4444-4444-8444-444444444444',enabled:true,organizationId:org,phoneNumberId:phone,webhookSecret:'synthetic-network-none-secret-0123456789',vapiKey:'synthetic',consoleUrl:'http://127.0.0.1:8785',sipHost:'unused.example.invalid',sipPort:5061},
 {Session:LabSession,fetcher:async()=>({ok:true,json:async()=>({...call,status:++reads>1?'ended':'ringing'})}),onEvent:e=>events.push(e)});
chmodSync('/run/servicevoice-phone',0o755);
let rejected=false;try{await bridge.listen({httpPort:0,mediaPort:9019});}catch(e){rejected=e.message==='phone_unix_directory_permissions';}
if(!rejected)throw Error('lab_unix_directory_mode_not_rejected');
chmodSync('/run/servicevoice-phone',0o700);
await bridge.listen({httpPort:0,mediaPort:9019});
const request=(authorization,body,path='/phone-bridge/ingress')=>new Promise((resolve,reject)=>{
 const r=http.request({socketPath:'/run/servicevoice-phone/ingress.sock',path,method:'POST',headers:{authorization,'content-type':'application/json'}},res=>{const parts=[];res.on('data',b=>parts.push(b));res.on('end',()=>resolve({status:res.statusCode,body:Buffer.concat(parts).toString()}));});
 r.on('error',reject);r.end(body);
});
if((statSync('/run/servicevoice-phone/ingress.sock').mode&0o777)!==0o600)throw Error('lab_unix_socket_mode');
if((await request('Bearer wrong','{}')).status!==401||reads!==0)throw Error('lab_unix_auth_failed');
const authorization='Bearer synthetic-network-none-secret-0123456789', drain_token='55555555-5555-4555-8555-555555555555';
if((await request(authorization,JSON.stringify({drain_token}),'/control/drain')).status!==200)throw Error('lab_drain_acquire_failed');
if((await request(authorization,JSON.stringify({message:{type:'assistant-request',call}}))).status!==503||reads!==0)throw Error('lab_drain_admission_race');
if((await request(authorization,JSON.stringify({drain_token:'66666666-6666-4666-8666-666666666666'}),'/control/resume')).status!==409)throw Error('lab_drain_wrong_owner');
if((await request(authorization,JSON.stringify({drain_token}),'/control/resume')).status!==200)throw Error('lab_drain_resume_failed');
const r=await request('Bearer synthetic-network-none-secret-0123456789',JSON.stringify({message:{type:'assistant-request',call}}));
if(r.status!==200)throw Error('lab_admission_failed');const owner=[...bridge.sessions.values()][0];
writeFileSync('/out/bridge-ready.json',JSON.stringify({token:owner.row.token,ingress_transport:'unix',socket_mode:'0600',queued_bytes:owner.playback.queuedBytes,sent_samples_before_attach:owner.playback.sentSamples}));
const stop=async()=>{const cleanup=await bridge.close();writeFileSync('/out/bridge-input.pcm',Buffer.concat(session?.input||[]));writeFileSync('/out/bridge-result.json',JSON.stringify({input_bytes:session?.bytes,input_frames:session?.frames,ownership_cleanup:cleanup,
 input_silence_samples:session?.mic?.silenceSamples,last_carrier_at_ms:session?.lastCarrierAt,last_encoded_at_ms:session?.lastEncodedAt,
 speech_events:session?.speech,drain_controls:true,outer_state:owner.row.state,inner_bound:!!owner.row.innerCallId,remaining_owned:bridge.sessions.size,events},null,2));process.exit(0);};
process.once('SIGTERM',()=>void stop());process.once('SIGINT',()=>void stop());
