// Exercise the shipped bundle rather than a mock of its export shape.
// Constructor and player lifecycle only: no start(), media or provider traffic.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { VoiceSession } from '../app/static/voice.js';

test('shipped Vapi bundle exports a usable constructor with the adapter contract', async t => {
  let networkAttempts = 0;
  const forbiddenNetwork = () => {
    networkAttempts += 1;
    throw new Error('Network use is forbidden in the bundle regression');
  };
  t.mock.method(globalThis, 'fetch', forbiddenNetwork);
  t.mock.method(globalThis, 'WebSocket', forbiddenNetwork);

  const { default: Vapi } = await import('../app/static/vendor/vapi.js');
  assert.equal(typeof Vapi, 'function', 'default export must be the constructor, not a CommonJS namespace');
  const syntheticTrack = { kind: 'audio', id: 'synthetic-offline-track' };
  const sdk = new Vapi('synthetic-offline-public-key', undefined, undefined, { audioSource: syntheticTrack });
  for (const method of ['start', 'stop', 'setMuted', 'isMuted', 'on', 'off']) {
    assert.equal(typeof sdk[method], 'function', `adapter method ${method} must exist`);
  }
  assert.equal(sdk.dailyCallObject.audioSource, syntheticTrack, 'fourth constructor argument must retain the supplied audio source');
  assert.equal(networkAttempts, 0);
});

test('actual bundle replacement event leaves only the current adapter playback sink', async t => {
  t.mock.method(globalThis, 'fetch', () => { throw new Error('Offline only'); });
  t.mock.method(globalThis, 'WebSocket', () => { throw new Error('Offline only'); });
  const { default: Vapi } = await import('../app/static/vendor/vapi.js');
  const sdk = new Vapi('synthetic-offline-public-key');
  const noop = () => {};
  const voice = new VoiceSession({}, {}, { status:noop, notice:noop, metrics:noop, volume:noop,
    refresh:noop, caption:noop, transcript:noop, ended:async () => {} });
  voice.sdk = sdk;
  voice.bindEvents();
  let oldPauses = 0, stopped = 0;
  const stream = { getTracks: () => [{ stop() { stopped++; } }] };
  const oldPlayer = { srcObject:stream, pause() { oldPauses++; }, remove() {} };
  const newPlayer = { srcObject:stream, pause() {}, remove() {} };
  try {
    // Exercise the shipped SDK's actual event order without starting a call.
    sdk.attachAudioPlayer(oldPlayer);
    sdk.attachAudioPlayer(newPlayer);
    assert.equal(sdk.getAudioPlayer(), newPlayer);
    assert.equal(oldPauses, 1);
    assert.equal(oldPlayer.srcObject, null);
    assert.equal(stopped, 0);
    assert.deepEqual([...voice.audioPlayers], [newPlayer]);
  } finally { await voice.stop(); }
  assert.equal(stopped, 1);
});

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes,no) => { resolve=yes; reject=no; });
  return {promise,resolve,reject};
}

async function audioBuildFixture(t, {rejectOnPause = false, pauseThrows = false, destroy} = {}) {
  let networkAttempts=0, trackStops=0;
  const forbidden=()=>{networkAttempts++;throw new Error('Offline only');};
  t.mock.method(globalThis,'fetch',forbidden);
  t.mock.method(globalThis,'WebSocket',forbidden);
  t.mock.method(console,'error',()=>{});
  const players=[], attached=new Set(), errors=[], emitted=[];
  const document={
    body:{appendChild(player){attached.add(player);}},
    createElement(tag){
      assert.equal(tag,'audio');
      const pending=deferred();
      const player={dataset:{},srcObject:null,paused:true,pauseCount:0,removeCount:0,pending,
        play(){this.paused=false;return pending.promise;},
        pause(){this.paused=true;this.pauseCount++;if(rejectOnPause)pending.reject(new Error('play canceled'));if(pauseThrows)throw new Error('pause failed');},
        remove(){this.removeCount++;attached.delete(this);}};
      players.push(player);return player;
    },
    querySelector(selector){const id=/data-participant-id="([^"]+)"/.exec(selector)?.[1];return [...attached].find(player=>player.dataset.participantId===id)||null;},
  };
  const originalDocument=Object.getOwnPropertyDescriptor(globalThis,'document');
  const originalStream=Object.getOwnPropertyDescriptor(globalThis,'MediaStream');
  Object.defineProperty(globalThis,'document',{configurable:true,value:document});
  Object.defineProperty(globalThis,'MediaStream',{configurable:true,value:class {constructor(tracks){this.tracks=tracks;}getTracks(){return this.tracks;}}});
  t.after(()=>{
    if(originalDocument)Object.defineProperty(globalThis,'document',originalDocument);else delete globalThis.document;
    if(originalStream)Object.defineProperty(globalThis,'MediaStream',originalStream);else delete globalThis.MediaStream;
    assert.equal(networkAttempts,0);
  });
  const {default:Vapi}=await import('../app/static/vendor/vapi.js');
  const sdk=new Vapi('synthetic-offline-public-key');
  sdk.call={sendAppMessage(){},destroy:destroy||async function(){}};
  sdk.on('audio',player=>emitted.push(player));sdk.on('error',error=>errors.push(error));
  const track={kind:'audio',stop(){trackStops++;}};
  const start=(participantId='synthetic-speaker')=>sdk.handleTrackStarted({participant:{local:false,user_name:'Vapi Speaker',session_id:participantId},track});
  return {sdk,players,attached,errors,emitted,start,trackStops:()=>trackStops};
}

test('actual SDK out-of-order audio build retires the unannounced stale player without stopping shared tracks', async t => {
  const f=await audioBuildFixture(t);
  const older=f.start(), newer=f.start();
  const [oldPlayer,currentPlayer]=f.players;
  f.players[1].pending.resolve();await newer;
  f.players[0].pending.resolve();await older;
  assert.equal(f.sdk.getAudioPlayer(),currentPlayer);
  assert.deepEqual(f.emitted,[currentPlayer], 'the stale player never reaches the app audio event');
  assert.equal(oldPlayer.paused,true);
  assert.equal(oldPlayer.srcObject,null);
  assert.equal(f.attached.has(oldPlayer),false);
  assert.equal(currentPlayer.paused,false);
  assert.notEqual(currentPlayer.srcObject,null);
  assert.equal(f.trackStops(),0);
  assert.equal(f.sdk.ownedAudioPlayers.size,1);
  await f.sdk.stop();
  assert.equal(f.sdk.ownedAudioPlayers.size,0);
});

test('ending during pending play retires the sink before async SDK destruction finishes', async t => {
  const destruction=deferred();
  const f=await audioBuildFixture(t,{rejectOnPause:true,destroy:()=>destruction.promise});
  const build=f.start();const player=f.players[0];
  const ending=f.sdk.stop();
  assert.equal(player.paused,true);
  assert.equal(player.srcObject,null);
  assert.equal(f.attached.size,0);
  await build;
  assert.equal(f.emitted.length,0);
  assert.equal(f.errors.length,0,'expected teardown is not a fresh audio-start failure');
  assert.equal(f.sdk.pendingAudioBuilds.size,0);
  assert.equal(f.trackStops(),0);
  destruction.resolve();await ending;
});

test('participant departure retires its pending builds while another participant playback stays live', async t => {
  const f=await audioBuildFixture(t,{rejectOnPause:true});
  const live=f.start('speaker-current');f.players[0].pending.resolve();await live;
  const leaving=f.start('speaker-leaving');const [current,discarded]=f.players;
  f.sdk.detachAudioPlayer('speaker-leaving');await leaving;
  assert.equal(discarded.srcObject,null);assert.equal(discarded.paused,true);
  assert.equal(f.attached.has(discarded),false);
  assert.equal(f.sdk.getAudioPlayer(),current);assert.equal(current.paused,false);
  assert.notEqual(current.srcObject,null);assert.equal(f.errors.length,0);
  assert.deepEqual(f.emitted,[current]);assert.equal(f.trackStops(),0);
  await f.sdk.stop();
});

test('genuine autoplay failure clears the sink and preserves the SDK error signal', async t => {
  const f=await audioBuildFixture(t);
  const build=f.start();const player=f.players[0];
  player.pending.reject(new Error('autoplay denied'));await build;
  assert.equal(player.paused,true);assert.equal(player.srcObject,null);assert.equal(f.attached.size,0);
  assert.equal(f.sdk.ownedAudioPlayers.size,0);assert.equal(f.emitted.length,0);
  assert.equal(f.errors.length,1);assert.equal(f.errors[0].type,'audio-start-failed');
  assert.equal(f.trackStops(),0);await f.sdk.stop();
});

test('SDK cleanup detaches pending playback even if pause throws, without stopping media tracks', async t => {
  const f=await audioBuildFixture(t,{pauseThrows:true});
  const build=f.start();const player=f.players[0];
  await f.sdk.cleanup();
  assert.equal(player.srcObject,null);assert.equal(f.attached.size,0);
  player.pending.resolve();await build;
  assert.equal(f.emitted.length,0);assert.equal(f.errors.length,0);
  assert.equal(f.sdk.ownedAudioPlayers.size,0);assert.equal(f.trackStops(),0);
});

test('a track arriving after teardown begins cannot create playback during or after async destroy', async t => {
  const destruction=deferred();
  const f=await audioBuildFixture(t,{destroy:()=>destruction.promise});
  const ending=f.sdk.stop();
  const late=f.start();
  const allocatedDuringDestroy=f.players.length;
  for(const player of f.players)player.pending.resolve();
  await late;
  destruction.resolve();await ending;
  const after=f.start();
  for(const player of f.players)player.pending.resolve();
  await after;
  assert.equal(allocatedDuringDestroy,0,'closing must gate new builds, not only retire prior builds');
  assert.equal(f.players.length,0);
  assert.equal(f.sdk.ownedAudioPlayers.size,0);assert.equal(f.attached.size,0);
  assert.equal(f.emitted.length,0);assert.equal(f.trackStops(),0);
});

test('cleanup rejects late track builds even when asynchronous Daily destruction fails', async t => {
  const destruction=deferred();
  const f=await audioBuildFixture(t,{destroy:()=>destruction.promise});
  const ending=f.sdk.cleanup();
  const rejected=assert.rejects(ending,/synthetic destroy failure/);
  const late=f.start();
  const allocatedDuringDestroy=f.players.length;
  for(const player of f.players)player.pending.resolve();
  await late;
  destruction.reject(new Error('synthetic destroy failure'));await rejected;
  const after=f.start();
  for(const player of f.players)player.pending.resolve();
  await after;
  assert.equal(allocatedDuringDestroy,0);
  assert.equal(f.players.length,0);
  assert.equal(f.sdk.ownedAudioPlayers.size,0);assert.equal(f.attached.size,0);
  assert.equal(f.emitted.length,0);assert.equal(f.trackStops(),0);
});
