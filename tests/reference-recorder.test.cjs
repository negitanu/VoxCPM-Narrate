const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const code = fs.readFileSync('src/voxcpm_narrate/web/static/reference-recorder.js', 'utf8');
function setup(getUserMedia, fetcher) {
  const elements = {}, tracks = {stops:0, stop(){this.stops++;}};
  const $ = id => elements[id] ||= {value:id === 'voice-style' ? 'standard' : '', files:[],
    classList:{toggle(){}}, addEventListener(name, fn){this[name]=fn;}, pause(){}, load(){}, removeAttribute(){}};
  const stream = {getTracks:()=>[tracks]};
  class Recorder {
    static isTypeSupported(t){return t.startsWith('audio/webm');}
    constructor(){this.mimeType='audio/webm';this.state='inactive';}
    start(){this.state='recording';}
    stop(){this.state='inactive';this.ondataavailable({data:new Blob(['audio'])});this.onstop();}
  }
  const context = {fetch:fetcher, document:{getElementById:$}, navigator:{mediaDevices:{getUserMedia:getUserMedia || (async()=>stream)}},
    MediaRecorder:Recorder, Blob, File, FormData, URL:{createObjectURL:()=> 'blob:test', revokeObjectURL(){}},
    setInterval:()=>1, clearInterval(){}, Date, window:{MediaRecorder:Recorder, addEventListener(){}}};
  vm.runInNewContext(code, context);
  return {$, api:context.window.referenceRecorder, tracks, stream};
}
test('default, presets and custom style update only reference script',()=>{
  const {$}=setup();
  assert.equal($('control').value,'日本語、明瞭な声、自然な抑揚、会話に近いテンポ');
  const original=$('reference-script').value;
  $('script').value='制作本文';
  $('voice-style').value='bright';$('voice-style').onchange();
  assert.notEqual($('reference-script').value,original);
  assert.equal($('script').value,'制作本文');
  $('control').value='独自の話し方';$('control').input();
  assert.equal($('voice-style').value,'custom');
  assert.equal($('control').value,'独自の話し方');
});
test('recording stops microphone and submits original script snapshot',async()=>{
  const {$,api,tracks}=setup();const original=$('reference-script').value;
  await $('record-start').onclick();
  assert.equal(api.busy,true);
  assert.throws(()=>api.appendTo(new FormData()),/録音を停止/);
  $('record-stop').onclick();
  assert.equal(api.busy,false);assert.ok(tracks.stops>0);
  $('reference-script').value='変更後';
  $('transcript-confirmed').checked=true;
  const body=new FormData();api.appendTo(body);
  assert.equal(body.get('reference_transcript'),original);
  assert.equal(body.get('reference_script'),original);
  assert.equal(body.get('reference').name,'recording.webm');
  $('reference-clear').onclick();const empty=new FormData();api.appendTo(empty);assert.equal(empty.get('reference'),null);
});
test('permission denied restores controls',async()=>{
  const {$,api}=setup(async()=>{throw Object.assign(new Error(),{name:'NotAllowedError'});});
  await $('record-start').onclick();
  assert.equal(api.busy,false);assert.equal($('record-start').disabled,false);
  assert.match($('record-status').textContent,/許可されていません/);
});
test('navigation while permission is pending releases late microphone',async()=>{
  let resolve;const {$,api,stream,tracks}=setup(()=>new Promise(r=>{resolve=r;}));
  const pending=$('record-start').onclick();api.cancel();resolve(stream);await pending;
  assert.ok(tracks.stops>0);assert.equal(api.busy,false);
});
test('uploaded file replaces recording without claiming its script',async()=>{
  const {$,api}=setup();await $('record-start').onclick();$('record-stop').onclick();
  $('reference').files=[new File(['upload'],'sample.wav')];$('reference').onchange();
  $('use-transcript').checked=false;
  const body=new FormData();api.appendTo(body);
  assert.equal(body.get('reference').name,'sample.wav');assert.equal(body.get('reference_script'),null);
});

test('transcript must be confirmed and edits invalidate confirmation',async()=>{
  const {$,api}=setup();await $('record-start').onclick();$('record-stop').onclick();
  assert.throws(()=>api.appendTo(new FormData()),/一致を確認/);
  $('reference-transcript').value='実際に話した言葉';$('transcript-confirmed').checked=true;
  const body=new FormData();api.appendTo(body);assert.equal(body.get('reference_transcript'),'実際に話した言葉');
  $('reference-transcript').input();assert.equal($('transcript-confirmed').checked,false);
});

test('automatic transcript remains unconfirmed until reviewed',async()=>{
  const {$,api}=setup(undefined,async()=>({ok:true,json:async()=>({text:'認識した実際の発話'})}));
  await $('record-start').onclick();$('record-stop').onclick();
  await $('transcribe-reference').onclick();
  assert.equal($('reference-transcript').value,'認識した実際の発話');
  assert.equal($('transcript-confirmed').checked,false);
  assert.equal(api.busy,false);
});
test('late transcription does not overwrite after cancellation',async()=>{
  let resolve;
  const {$,api}=setup(undefined,()=>new Promise(r=>{resolve=r;}));
  await $('record-start').onclick();$('record-stop').onclick();
  const before=$('reference-transcript').value;
  const pending=$('transcribe-reference').onclick();api.cancel();
  resolve({ok:true,json:async()=>({text:'古い返答'})});await pending;
  assert.equal($('reference-transcript').value,before);
});
