const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const read=name=>fs.readFileSync(path.join(__dirname,'../static',name),'utf8');
const ctx=vm.createContext({});
vm.runInContext(read('layout.js').replaceAll('export ',''),ctx);
vm.runInContext(read('scene-layout.js').replace(/^import .*;\n/gm,'').replaceAll('export ','')+';globalThis.api={shotForState,portraitRect,applyPortraitLayout,DEFAULT_LAYOUT};',ctx);
const {shotForState,portraitRect,applyPortraitLayout,DEFAULT_LAYOUT}=ctx.api;
const state=(content_mode,state='running',id='new')=>({program:{state,content_mode,game_episode_id:id},episode:{id:'new',status:'ACTIVE'}});
test('shot selection honors stable episode identity, both transition directions and refresh',()=>{
  assert.equal(shotForState(state('chat')),'portrait');
  assert.equal(shotForState(state('game','starting')),'portrait');
  assert.equal(shotForState(state('to_game')),'portrait');
  assert.equal(shotForState(state('game','running','old')),'portrait');
  assert.equal(shotForState(state('game')),'game');
  assert.equal(shotForState(state('to_chat','running',null)),'game');
  assert.equal(shotForState(state('chat')),'portrait');
  assert.equal(shotForState({program:{state:'idle',content_mode:'game'},episode:{status:'FAILED'}}),'portrait');
  assert.equal(shotForState(state('game','idle',null)),'game');
  assert.equal(shotForState({}), 'game');
  assert.equal(shotForState(state('chat'),{demo:true}),'game');
});
test('same normalized geometry fills both 390 and 1080 width without stretching the portrait',()=>{
  for(const width of [390,1080]){
    const full=portraitRect(DEFAULT_LAYOUT,'portrait'),factor=width/1080;
    assert.equal(full.h*factor,width*16/9);
    assert.equal(full.w/full.h,3/4);
    assert.equal((full.x+full.w/2)*factor,width/2);
    const small=portraitRect(DEFAULT_LAYOUT,'game');
    assert.equal(small.w/small.h,3/4);
    assert.ok(small.x*factor>=0 && (small.x+small.w)*factor<=width);
    assert.ok(small.y*factor>=0 && (small.y+small.h)*factor<width*16/9);
    // Centered lower label survives the horizontal portrait crop.
    assert.ok((full.x+full.w/2)*factor>0);
  }
});
test('first load snaps, identical polls do not rewrite transforms, subsequent shots animate through CSS',()=>{
  let writes=0,transform='';const layouts=[];
  const node={dataset:{},style:{},getBoundingClientRect(){layouts.push(this.style.transition);}};
  Object.defineProperty(node.style,'transform',{get:()=>transform,set:value=>{transform=value;writes++;}});
  applyPortraitLayout(node,DEFAULT_LAYOUT,'portrait');
  assert.deepEqual(layouts,['none']);assert.equal(node.dataset.placed,'true');
  for(let i=0;i<5;i++)applyPortraitLayout(node,DEFAULT_LAYOUT,'portrait');
  assert.equal(writes,1);assert.equal(layouts.length,1);
  applyPortraitLayout(node,DEFAULT_LAYOUT,'game');
  assert.equal(writes,2);assert.equal(node.style.transition,'');
  applyPortraitLayout(node,DEFAULT_LAYOUT,'portrait');
  assert.equal(writes,3);assert.equal(layouts.length,1);
  const refreshed={dataset:{},style:{},getBoundingClientRect(){assert.equal(this.style.transition,'none');}};
  applyPortraitLayout(refreshed,DEFAULT_LAYOUT,'game');
  assert.equal(refreshed.style.transform.includes('scale('),true);
});
test('motion is optional and every shared carrier keeps a 3:4 media surface',()=>{
  const css=read('phone-framing.css');
  assert.match(css,/aspect-ratio:3\/4/);
  assert.match(css,/transition:transform 600ms/);
  assert.match(css,/@media\(prefers-reduced-motion:reduce\).*\.portrait-carrier\{transition:none!important\}/);
  assert.match(css,/\.portrait-carrier \.avatar-still-label\{left:50%/);
});
