// A synthetic DOM verifies receiver ownership and reconnection behavior only.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=path.resolve(__dirname,'..'),source=fs.readFileSync(path.join(root,'static/obs-output-player.js'),'utf8').replaceAll('export ','');
const context=vm.createContext({URL});vm.runInContext(source+';globalThis.api={authorizedPlayerSource,createPlayerMount};',context);
const {authorizedPlayerSource,createPlayerMount}=context.api,base='http://127.0.0.1:55173/static/obs-output.html';
const good='http://127.0.0.1:8765/player/game-test/';
assert.equal(authorizedPlayerSource({obs_output:true,player_url:good},base),good);
for(const url of ['javascript:alert(1)','https://example.com/player/x/','http://localhost:8765/player/x/','http://127.0.0.1:8765/studio/','http://user:pass@127.0.0.1:8765/player/x/'])assert.equal(authorizedPlayerSource({obs_output:true,player_url:url},base),'');
assert.equal(authorizedPlayerSource({obs_output:false,player_url:good},base),'');
class Node{constructor(){this.hidden=true;this.listeners={};this.writes=0;this.removals=0;}addEventListener(name,fn){this.listeners[name]=fn;}set src(value){this.value=value;this.writes++;}removeAttribute(){this.value='';this.removals++;}}
const frame=new Node(),still=new Node(),label=new Node(),placeholder=new Node(),status=[];
const mount=createPlayerMount({frame,still,label,placeholder,onStatus:v=>status.push(v)});
mount.update({obs_output:false,player_url:null},base);assert.equal(still.hidden,false);assert.equal(label.hidden,false);assert.equal(frame.hidden,true);
mount.update({obs_output:true,player_url:good},base);assert.equal(frame.hidden,false);assert.equal(still.hidden,true);assert.equal(frame.writes,1);
mount.update({obs_output:true,player_url:good},base);mount.disconnected();assert.equal(frame.writes,1);assert.equal(frame.hidden,false);assert.equal(frame.removals,0);
frame.listeners.load();assert.equal(status.at(-1),'frame-loaded');
frame.listeners.error();assert.equal(frame.hidden,true);assert.equal(still.hidden,true);assert.equal(placeholder.hidden,false);
mount.update({obs_output:true,player_url:good},base);assert.equal(frame.writes,1);assert.equal(frame.hidden,true);
mount.update({obs_output:true,player_url:null,program:{state:'starting'}},base);assert.equal(frame.removals,1);assert.equal(still.hidden,true);
for(const state of ['off','idle','stopped']){mount.update({obs_output:true,player_url:null,program:{state}},base);assert.equal(still.hidden,false);assert.equal(label.hidden,false);}
for(const state of ['starting','running','stopping','error']){mount.update({obs_output:true,player_url:null,program:{state}},base);assert.equal(still.hidden,true);assert.equal(placeholder.hidden,false);}
assert.equal(status.at(-1),'unavailable');
mount.update({obs_output:false,player_url:null},base);assert.equal(frame.removals,1);assert.equal(still.hidden,false);
const page=fs.readFileSync(path.join(root,'static/obs-output.html'),'utf8'),script=fs.readFileSync(path.join(root,'static/obs-output.js'),'utf8');
assert.match(page,/allow="autoplay"/);assert.doesNotMatch(page,/workbench.js|preview-fixture|douyin-reference|<button/);
assert.doesNotMatch(script,/localStorage\.(getItem|setItem)|heartbeat|\/api\/program\/start|method:'POST'/);
assert.match(script,/renderer.render\(state\)/);
console.log('OBS receiver: URL authorization, sole iframe, no poll reconnect, honest fallback and receiver-only page passed.');

// Geometry/mode changes cannot create another RTC attachment.
const stableFrame=new Node(),stableMount=createPlayerMount({frame:stableFrame,still:new Node(),label:new Node(),placeholder:new Node()});
for(const content_mode of ['chat','chat','to_game','game','game','to_chat','chat']){
  stableMount.update({obs_output:true,player_url:good,program:{state:'running',content_mode}},base);
}
assert.equal(stableFrame.writes,1);assert.equal(stableFrame.removals,0);
assert.equal((page.match(/<iframe/g)||[]).length,1);
