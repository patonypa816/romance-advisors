const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const read=file=>fs.readFileSync(path.join(__dirname,'../static',file),'utf8');
class Node{
  constructor(tag){this.tag=tag;this.children=[];this.attributes={};this.dataset={};this.events={};this.plays=0;this.pauses=0;}
  append(...nodes){this.children.push(...nodes);}
  addEventListener(type,fn){this.events[type]=fn;}
  setAttribute(key,value){this.attributes[key]=value;}
  getAttribute(key){return key==='src'?this.src:this.attributes[key];}
  play(){this.plays++;return Promise.resolve();}
  pause(){this.pauses++;}
}
function setup(){const context=vm.createContext({document:{createElement:tag=>new Node(tag)},URLSearchParams,Date:{now:()=>100000},matchMedia:()=>({matches:true})});vm.runInContext(read('ui.js').replaceAll('export ','')+';globalThis.api={createAppearanceMedia,appearanceImage,makeMessage};',context);return context.api;}
test('local media resumes current transition once and never overlays RTC',()=>{
  const {createAppearanceMedia}=setup(),image=new Node('img'),video=new Node('video'),media=createAppearanceMedia(image,video);
  const appearance={mode:'rehearsal',tier:'normal',transition:{id:'1',direction:'up',at:99.5}};
  media.update(appearance);assert.equal(image.src,'/static/assets/appearance/normal.jpg');assert.equal(video.src,'/static/assets/appearance/up.mp4');assert.equal(video.currentTime,.5);assert.equal(video.plays,1);
  media.update(appearance);assert.equal(video.plays,1);video.events.ended();media.update(appearance);assert.equal(video.hidden,true);assert.equal(video.plays,1);
  media.update({...appearance,transition:{id:'2',direction:'down',at:100}});assert.equal(video.src,'/static/assets/appearance/down.mp4');assert.equal(video.plays,2);
  media.update({...appearance,mode:'rtc'});assert.equal(video.hidden,true);assert.equal(video.pauses,1);
  media.update({...appearance,transition:{id:'old',direction:'up',at:90}});assert.equal(video.plays,2);
  media.dispose();
});
test('message portrait uses frozen approved key and old messages retain old behavior',()=>{
  const {makeMessage,appearanceImage}=setup();assert.equal(appearanceImage('../secret'),'');
  const state={episode:{man_name:'甲'},appearance:{tier:'fish'}};
  const row=makeMessage({id:'m1',role:'man',text:'你好',portrait:{key:'refined'}},state);
  assert.equal(row.children[0].src,'/static/assets/appearance/refined.jpg');assert.match(row.children[0].alt,/收拾好了/);
  assert.equal(makeMessage({id:'m0',role:'man',text:'旧消息'},state).children.length,2);
  assert.equal(makeMessage({id:'m2',role:'man',text:'输入',portrait:{key:'https://x'}},state).children.length,2);
});
test('formal page includes independent rehearsal and automatic appearance progress',()=>{
  const html=read('index.html');assert.ok(html.indexOf('id="appearance-start"')<html.indexOf('id="debug-template"'));
  assert.match(html,/不调用模型、语音或数字人/);assert.match(html,/不代表收到真实礼物/);assert.match(html,/正常换形自动衔接/);assert.doesNotMatch(html,/id="appearance-observed"|id="appearance-confirm"/);
  const workbench=read('appearance-workbench.js');assert.doesNotMatch(workbench,/\/api\/program\/start|\/api\/episode\/new/);assert.doesNotMatch(workbench,/\/api\/appearance\/confirm/);
});
test('automatic appearance progress never asks for normal or failure confirmation',()=>{
  const nodes=new Map(),calls=[];
  function node(id){if(!nodes.has(id))nodes.set(id,{value:'',checked:false,events:{},addEventListener(type,fn){this.events[type]=fn;}});return nodes.get(id);}
  const context=vm.createContext({document:{getElementById:node},fetch:()=>new Promise(()=>{}),setInterval:()=>0,
    post:async(path,body)=>{calls.push({path,body});return {appearance:{mode:'off'}};},APPEARANCE_LABELS:{refined:'精致'}});
  vm.runInContext(read('appearance-workbench.js').replace(/^import .*;\n/gm,'')+';globalThis.renderAppearance=render;',context);
  const render=context.renderAppearance;
  render({receipt:{request_id:'r1',target_key:'refined',status:'written'}});
  assert.equal(node('appearance-confirmation').hidden,false);assert.match(node('appearance-confirm-note').textContent,/无需点击确认/);
  render({receipt:{request_id:'r1',target_key:'refined',status:'failed'}});
  assert.match(node('appearance-confirm-note').textContent,/失败或结果未知.*不会自动重试/);
  render({receipt:{request_id:'r1',status:'applied',program_adopted:true}});
  assert.equal(node('appearance-confirmation').hidden,true);assert.equal(calls.length,0);
});
function bandWorkbench(){
  const nodes=new Map(),calls=[];
  function node(id){if(!nodes.has(id))nodes.set(id,{value:'',checked:false,events:{},addEventListener(type,fn){this.events[type]=fn;}});return nodes.get(id);}
  const context=vm.createContext({document:{getElementById:node},fetch:()=>new Promise(()=>{}),setInterval:()=>0,
    post:async(path,body)=>{calls.push({path,body});return {appearance:{mode:'off',settings:body}};},APPEARANCE_LABELS:{}});
  vm.runInContext(read('appearance-workbench.js').replace(/^import .*;\n/gm,'')+';globalThis.renderAppearance=render;',context);
  return {node,calls,render:context.renderAppearance,submit(){node('appearance-settings').events.submit({preventDefault(){}});}};
}
test('saved boundaries render exact closed ranges and old data defaults remain editable',()=>{
  const h=bandWorkbench();h.render({settings:{}});
  assert.equal(h.node('appearance-band-tired').value,20);assert.equal(h.node('appearance-band-stellar').value,80);
  assert.match(h.node('appearance-tiers').textContent,/1–19.*20–39.*40–59.*60–79.*80–100/);
  const band_starts={tired:10,normal:30,refined:70,stellar:90};h.render({settings:{band_starts}});
  assert.match(h.node('appearance-tiers').textContent,/1–9.*10–29.*30–69.*70–89.*90–100/);
  h.node('appearance-band-tired').value='15';h.node('appearance-band-tired').events.input();
  h.render({settings:{band_starts}});assert.equal(h.node('appearance-band-tired').value,'15');
  assert.match(h.node('appearance-tiers').textContent,/1–9/); // Unsaved editing cannot change the displayed rules.
});
test('valid custom boundaries save while real gifts are off, invalid partitions never post',()=>{
  for(const values of [[1,40,60,80],[20,20,60,80],[20,51,60,80],[20,40,50,80],[20,40,80,80],[20,40,60,101],[20.5,40,60,80]]){
    const h=bandWorkbench();h.render({settings:{}});
    ['tired','normal','refined','stellar'].forEach((key,i)=>{h.node('appearance-band-'+key).value=String(values[i]);});
    h.submit();assert.equal(h.calls.length,0,values.join(','));assert.match(h.node('appearance-error').textContent,/50 必须普通/);
  }
  const h=bandWorkbench();h.render({settings:{}});
  ['tired','normal','refined','stellar'].forEach((key,i)=>{h.node('appearance-band-'+key).value=String([10,30,70,90][i]);});
  h.submit();assert.equal(h.calls.length,1);assert.equal(h.calls[0].body.enabled,false);
  assert.deepEqual({...h.calls[0].body.band_starts},{tired:10,normal:30,refined:70,stellar:90});
});
test('real gift quantity probe stays waiting until a receipt and never checks formal verification',()=>{
  const h=bandWorkbench();
  const gifts=[{gift_id:'463',name:'小心心',direction:1,unit_diamonds:null,cumulative_count:0,accepted_count:0,last_increment:0},
    {gift_id:'164',name:'抖音',direction:-1,unit_diamonds:null,cumulative_count:0,accepted_count:0,last_increment:0}];
  const settings={quantity_verified:false,enabled:false};
  h.render({settings,gift_probe:{available:true,active:true,state:'waiting',score:50,gifts}});
  assert.equal(h.node('appearance-gift-probe').hidden,false);
  assert.match(h.node('appearance-gift-probe-state').textContent,/等待真实礼物，尚未实收.*50/);
  assert.match(h.node('appearance-gift-probe-rows').textContent,/小心心.*单价待实收.*抖音/);
  h.render({settings,gift_probe:{available:true,active:true,state:'receiving',score:60,
    gifts:[{...gifts[0],unit_diamonds:1,cumulative_count:10,accepted_count:10,last_increment:10},gifts[1]]}});
  assert.match(h.node('appearance-gift-probe-state').textContent,/已收到真实礼物.*60/);
  assert.match(h.node('appearance-gift-probe-rows').textContent,/1 钻\/个.*去重共 10 个/);
  assert.equal(h.node('appearance-quantity').checked,false);
  h.node('appearance-gift-probe-reset').events.click();
  assert.equal(h.calls[0].path,'/api/appearance/gift-probe');
  assert.deepEqual({...h.calls[0].body},{});
});

test('on-air test submits the displayed live episode once and keeps the receipt separate from gifts',()=>{
  const h=bandWorkbench(),button=h.node('appearance-test-points');
  h.render({mode:'off'});assert.equal(h.node('appearance-onair-test').hidden,true);
  const test={eligible:true,used:false,run_id:'live-A',episode_id:'episode-B'};
  h.render({mode:'rtc',operator_test:test});assert.equal(button.disabled,false);
  button.events.click();button.events.click();
  assert.equal(h.calls.length,1);assert.equal(h.calls[0].path,'/api/appearance/operator-test');
  assert.deepEqual({...h.calls[0].body},{run_id:'live-A',episode_id:'episode-B'});
  h.render({mode:'rtc',operator_test:{...test,used:true,eligible:false,receipt:{score_before:50,score:70}}});
  assert.equal(button.disabled,true);assert.match(h.node('appearance-test-note').textContent,/50 → 70/);
});
