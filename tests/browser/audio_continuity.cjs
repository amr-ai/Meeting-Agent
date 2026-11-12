const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function setup(fetch = async () => { throw Error('Unexpected fetch'); }) {
  const timers = [];
  class Room {
    constructor() {
      this.remoteParticipants = new Map();
      this.localParticipant = {publishData:async () => {}, publishTrack:async () => ({track:{}})};
    }
    async connect() { this.connections = (this.connections || 0) + 1; }
    async disconnect() { this.disconnected = true; }
    removeAllListeners() {}
  }
  const context = vm.createContext({Room, RoomEvent:{}, Track:{Source:{Microphone:'microphone'}},
    ParticipantKind:{}, AudioLatencyProbe:class {}, TextEncoder, console, fetch,
    document:{querySelector:() => ({})}, navigator:{mediaDevices:{}},
    setTimeout:fn => { timers.push(fn); return 1; }, clearTimeout:() => {},
    setInterval:() => 1, clearInterval:() => {}});
  const code = fs.readFileSync('static/media.js','utf8').replace(/^import .*;\r?\n/gm,'')
    .replace('export class AudioConnection','class AudioConnection');
  vm.runInContext(code + '\nglobalThis.AudioConnection = AudioConnection;', context);
  return {context, timers, connection:new context.AudioConnection('scoped-token', () => {})};
}

test('transient renewal failure retains audio and retries', async () => {
  let count = 0;
  const {connection:c,timers} = setup(async () => ++count === 1 ? {ok:false,status:503} :
    {ok:true,json:async () => ({bridge_token:'renewed',renew_after_seconds:30})});
  c.scheduleRenewal(0);
  await timers.shift()();
  assert.equal(c.closed,false);
  assert.equal(timers.length,1);
  await timers.shift()();
  assert.equal(c.token,'renewed');
  assert.equal(c.closed,false);
});

test('missing agent is ensured without reconnecting or recapturing input', async () => {
  const {connection:c,timers} = setup();
  c.agentIdentity = 'agent';
  let ensures = 0;
  c.credentials = async () => { ensures++; };
  c.recoverAgent();
  await timers.shift()();
  assert.equal(ensures,1);
  assert.equal(c.closed,false);
  assert.equal(c.room.connections,undefined);
  c.room.remoteParticipants.set('agent',{});
  await timers.shift()();
  assert.equal(ensures,1);
});

test('agent recovery retains input on transient failure and stops on close', async () => {
  const {connection:c,timers} = setup();
  let ensures = 0;
  c.credentials = async () => { ensures++; throw Error('API restarting'); };
  c.recoverAgent();
  await timers.shift()();
  assert.equal(c.closed,false);
  await c.close();
  await timers.shift()();
  assert.equal(ensures,1);
});

test('bridge reconnects after more than six failures without stopping agent', async () => {
  const {connection:c,context} = setup();
  context.setTimeout = fn => { queueMicrotask(fn); return 1; };
  let attempts = 0;
  c.credentials = async () => {
    if (++attempts <= 7) throw Error('Transient network loss');
    return {server_url:'server',participant_token:'scoped'};
  };
  c.publishInput = async () => {};
  await c.recover();
  assert.equal(attempts,8);
  assert.equal(c.closed,false);
  assert.equal(c.room.connections,1);
});

test('terminal authorization failure stops retrying', async () => {
  const {connection:c,context} = setup();
  context.setTimeout = fn => { queueMicrotask(fn); return 1; };
  c.credentials = async () => { const error = Error('Expired'); error.terminal = true; throw error; };
  await c.recover();
  assert.equal(c.closed,true);
  assert.equal(c.room.disconnected,true);
});

test('concurrent reconnect and track-end capture create only one stream', async () => {
  const {connection:c,context} = setup();
  let resolve, captures = 0;
  const stream = {getAudioTracks:() => [{addEventListener:() => {},muted:false}],getTracks:() => []};
  context.navigator.mediaDevices.getUserMedia = () => { captures++; return new Promise(r => {resolve=r;}); };
  const first = c.publishInput(), second = c.publishInput();
  assert.equal(captures,1);
  resolve(stream);
  await Promise.all([first,second]);
  assert.ok(c.inputTrack);
});

test('temporary capture failure retries on existing connected room', async () => {
  const {connection:c,context} = setup();
  context.setTimeout = fn => { queueMicrotask(fn); return 1; };
  c.room.state = 'connected';
  c.credentials = async () => ({server_url:'server',participant_token:'scoped'});
  let captures = 0;
  c.publishInput = async () => { if (++captures === 1) throw Error('Virtual microphone not ready'); };
  await c.recover();
  assert.equal(captures,2);
  assert.equal(c.closed,false);
  assert.equal(c.room.connections,undefined);
});

test('renewed scoped bridge token survives reload without staying in URL', async () => {
  const stored = new Map(), tokens = [];
  class AudioConnection {
    constructor(token,status,options) { tokens.push(token); this.options=options; }
    async connect() { this.options.onSessionToken('renewed'); }
  }
  const source = fs.readFileSync('static/bridge/bridge.js','utf8').replace(/^import .*;\r?\n/gm,'');
  for (const hash of ['#initial','']) {
    const location = {hash,pathname:'/bridge'};
    vm.runInNewContext(source, {AudioConnection,location,
      sessionStorage:{getItem:key=>stored.get(key),setItem:(key,value)=>stored.set(key,value)},
      history:{replaceState:()=>{location.hash='';}},document:{querySelector:()=>({})},window:{addEventListener:()=>{}}});
    assert.equal(location.hash,'');
  }
  assert.deepEqual(tokens,['initial','renewed']);
});
