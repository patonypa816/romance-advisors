// Execute the production UI, renderer and poll functions with a small DOM and
// controlled fetch/animation callbacks. No server, database or cloud is used.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {test}=require('node:test');
const rootDir=path.resolve(__dirname,'..');
const read=name=>fs.readFileSync(path.join(rootDir,'static',name),'utf8');
class Node {
  constructor(tag='div'){
    this.tag=tag;this.children=[];this.dataset={};this.style={};this.className='';this._text='';this.attributes={};
    Object.defineProperty(this.style,'removeProperty',{value:key=>delete this.style[key]});
    this.scrollTop=0;this.scrollHeight=1000;this.clientHeight=300;this.clientWidth=300;
    this.offsetTop=0;this.offsetHeight=40;this.isConnected=true;
    this.classList={contains:name=>this.className.split(' ').includes(name),toggle:(name,on)=>{
      const names=new Set(this.className.split(' ').filter(Boolean));
      if(on)names.add(name);else names.delete(name);this.className=[...names].join(' ');
    },add:(...names)=>names.forEach(name=>this.classList.toggle(name,true)),remove:(...names)=>names.forEach(name=>this.classList.toggle(name,false))};
  }
  set textContent(value){this._text=String(value);this.children=[];}
  get textContent(){return this._text+this.children.map(child=>child.textContent).join('');}
  append(...nodes){for(const node of nodes){node.parent=this;this.children.push(node);}}
  replaceChildren(...nodes){this._text='';this.children=[];this.append(...nodes);}
  setAttribute(name,value){this.attributes[name]=value;}
  removeAttribute(name){delete this.attributes[name];}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(child=>child!==this);}
  addEventListener(name,fn){this.events??={};this.events[name]=fn;}
  getBoundingClientRect(){this.transitionAtLayout=this.style.transition;return {};}
  animate(){const animation={cancelled:false,currentTime:null,pause(){},cancel(){this.cancelled=true;}};this.animations??=[];this.animations.push(animation);return animation;}
  querySelectorAll(selector){
    if(selector.includes(' ')){const [head,...tail]=selector.split(' ');return this.querySelectorAll(head).flatMap(node=>node.querySelectorAll(tail.join(' ')));}
    const found=[];
    for(const child of this.children){
      if(selector==='[data-message-id]'?child.dataset.messageId!==undefined:
         selector.startsWith('.')?child.classList.contains(selector.slice(1)):child.tag===selector)found.push(child);
      found.push(...child.querySelectorAll(selector));
    }
    return found;
  }
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
}
function state(){return {episode:{id:'e1',man_name:'甲',woman_name:'乙',status:'ACTIVE',score:50},
  round:{id:'r1',number:1,phase:'VOTING',remaining_seconds:20,
    options:[{id:'o1',number:1,text:'先问周末',votes:0,author:{nickname:'观众'}}]},
  messages:[{id:'m1',role:'woman',text:'周末有空吗？'}],leaderboards:{best:[],worst:[]}};}
function harness({reduced=true}={}){
  const root=new Node(),frames=new Map(),timers=new Map(),errors=[],listeners=new Set();
  let clock=0,timerId=0;
  const media={matches:reduced,addEventListener:(_,fn)=>listeners.add(fn),removeEventListener:(_,fn)=>listeners.delete(fn)};
  let frameId=0,fetchCount=0,current=state(),fetchError=null;
  const context=vm.createContext({document:{createElement:tag=>new Node(tag)},URLSearchParams,
    URL,matchMedia:()=>media,performance:{now:()=>clock},
    setTimeout:(callback,delay)=>{timers.set(++timerId,{callback,at:clock+delay});return timerId;},clearTimeout:id=>timers.delete(id),
    requestAnimationFrame:callback=>{frames.set(++frameId,callback);return frameId;},
    cancelAnimationFrame:id=>frames.delete(id),root,
    layoutRects:()=>({phone:{},poll:{},boards:{}}),normalizeLayout:value=>value,
    console:{error:(...args)=>errors.push(args)},
    fetch:async()=>{fetchCount++;if(fetchError)throw fetchError;return {ok:true,json:async()=>current};}});
  vm.runInContext(read('ui.js').replaceAll('export ',''),context);
  vm.runInContext(read('affinity-panel.js').replace(/^import .*;\n/gm,'').replace('export function ','function '),context);
  vm.runInContext(read('finale-overlay.js').replace(/^import .*;\n/gm,'').replaceAll('export ',''),context);
  vm.runInContext(read('renderer.js').replace(/^import .*;\n/gm,'').replace('export function ','function ')+
    ';var renderer=createLiveRenderer(root);',context);
  const overlay=read('overlay.js');
  vm.runInContext(overlay.slice(overlay.indexOf('let running='),overlay.indexOf('// Demo')),context);
  return {root,frames,timers,listeners,errors,renderer:context.renderer,
    safeAvatar:value=>context.safeFinaleAvatar(value),
    get finale(){return root.querySelector('.finale-layer');},
    get card(){return root.querySelector('.affinity-card');},
    get number(){return root.querySelector('.affinity-number');},
    get change(){return root.querySelector('.affinity-change');},
    get fill(){return root.querySelector('.affinity-fill');},
    setReduced(value){media.matches=value;listeners.forEach(fn=>fn());},
    advance(ms){clock+=ms;for(const [id,timer]of [...timers])if(timer.at<=clock&&timers.delete(id))timer.callback();this.flushFrames();},
    get state(){return current;},set state(value){current=value;},
    get fetchCount(){return fetchCount;},set fetchError(value){fetchError=value;},
    get phase(){return root.querySelector('.live-phase').textContent;},
    get running(){return vm.runInContext('running',context);},
    render(){context.renderer.render(current);},poll(){return vm.runInContext('poll()',context);},
    flushFrames(){for(const [id,callback] of [...frames])if(frames.delete(id))callback(clock);}};
}

test('typing is transient, stable across polls, and yields to the reply',()=>{
  const h=harness();h.state.round.phase='WAITING_REPLY';h.state.round.woman_typing=true;h.render();
  const chat=h.root.querySelector('.live-chat'),indicator=chat.querySelector('.woman-typing');
  assert.ok(indicator);assert.equal(indicator.attributes.role,'status');assert.equal(indicator.attributes['aria-label'],'乙正在输入');
  assert.equal(indicator.querySelectorAll('.typing-dot').length,3);
  for(let i=0;i<5;i++)h.render();assert.equal(chat.querySelector('.woman-typing'),indicator);
  assert.equal(chat.querySelectorAll('[data-message-id]').length,1);assert.equal(h.state.messages.length,1);
  h.state.messages.push({id:'m2',round_id:'r1',role:'woman',text:'好呀'});h.render();
  assert.equal(chat.querySelector('.woman-typing'),null);assert.ok(chat.querySelector('.woman-reply-arriving'));
  h.render();assert.equal(chat.querySelector('.woman-typing'),null);
});
test('errors, changed round, terminal state and offline remove the indicator',()=>{
  const h=harness();h.state.round.woman_typing=true;h.render();const chat=h.root.querySelector('.live-chat');
  h.state.error='失败';h.render();assert.equal(chat.querySelector('.woman-typing'),null);
  h.state.error=null;h.render();const previous=chat.querySelector('.woman-typing');assert.ok(previous);
  h.state.round.id='r2';h.render();assert.notEqual(chat.querySelector('.woman-typing'),previous);
  h.state.episode.status='TECH_ENDED';h.render();assert.equal(chat.querySelector('.woman-typing'),null);
  h.state.episode.status='ACTIVE';h.render();h.renderer.offline();assert.equal(chat.querySelector('.woman-typing'),null);
  h.render();h.renderer.renderFailed();assert.equal(chat.querySelector('.woman-typing'),null);
  h.state.round.woman_typing=false;h.render();assert.equal(chat.querySelector('.woman-typing'),null);
});
test('reduced motion disables dot and arrival animation',()=>{
  assert.match(read('overlay.css'),/@media\(prefers-reduced-motion:reduce\)[^}]*}\s*\.typing-dot,\.woman-reply-arriving\{animation:none!important}/);
});


test('episode replacement and reset remove transient rows and update the empty-state copy',()=>{
  const h=harness();h.state.round.woman_typing=true;h.render();
  const chat=h.root.querySelector('.live-chat'),oldIndicator=chat.querySelector('.woman-typing');
  h.state={episode:{id:'e2',status:'ACTIVE',woman_name:'丙',score:50},round:{id:'r2',phase:'WAITING_REPLY',woman_typing:true},messages:[]};
  h.render();
  assert.notEqual(chat.querySelector('.woman-typing'),oldIndicator);
  assert.equal(chat.querySelector('.woman-typing').attributes['aria-label'],'丙正在输入');
  assert.equal(chat.querySelectorAll('[data-message-id]').length,0);
  h.state.round.woman_typing=false;h.render();
  const empty=chat.querySelector('.live-empty');assert.equal(empty.textContent,'等她发来第一条消息…');
  h.state={messages:[]};h.render();
  assert.equal(chat.querySelector('.woman-typing'),null);assert.equal(chat.querySelector('.live-empty'),empty);
  assert.equal(empty.textContent,'从一条消息，开始这场心动。');
});

test('reply arrival, affinity settlement and finale share the same persistent game DOM',()=>{
  const h=harness({reduced:false});h.state.round.phase='WAITING_REPLY';h.state.round.woman_typing=true;h.render();h.flushFrames();
  const phone=h.root.querySelector('.live-conversation'),chat=h.root.querySelector('.live-chat'),card=h.card;
  h.state.messages.push({id:'m2',round_id:'r1',role:'woman',text:'好呀'});
  h.state.round.phase='DONE';h.state.round.delta=50;
  h.state.episode.status='SUCCEEDED';h.state.episode.score=100;
  h.state.finale={episode_id:'e1',status:'SUCCEEDED',started_at:1000,duration_ms:6800,elapsed_ms:0,awards:{best:null,worst:null}};
  h.render();
  const reply=chat.querySelector('.woman-reply-arriving'),flight=h.root.querySelector('.emblem-flight').animations.at(-1);
  assert.ok(reply);assert.equal(chat.querySelector('.woman-typing'),null);assert.equal(h.change.textContent,'+50');
  assert.equal(h.finale.hidden,false);assert.equal(flight.currentTime,0);
  h.advance(500);h.render();h.flushFrames();
  assert.equal(h.root.querySelector('.live-conversation'),phone);assert.equal(h.root.querySelector('.live-chat'),chat);
  assert.equal(h.card,card);assert.equal(chat.querySelector('.woman-reply-arriving'),reply);
  assert.equal(chat.querySelectorAll('[data-message-id]').length,2);
  assert.equal(h.root.querySelector('.emblem-flight').animations.at(-1),flight);assert.equal(flight.currentTime,500);
  h.renderer.dispose();assert.equal(flight.cancelled,true);assert.equal(h.finale.hidden,true);
  assert.equal(h.frames.size,0);assert.equal(h.timers.size,0);assert.equal(h.listeners.size,0);
});

test('disposing during typing cancels pending follow and later renders cannot restore the indicator',()=>{
  const h=harness({reduced:false});h.state.round.phase='WAITING_REPLY';h.state.round.woman_typing=true;h.render();
  const chat=h.root.querySelector('.live-chat');assert.ok(chat.querySelector('.woman-typing'));assert.equal(h.frames.size,1);
  h.renderer.dispose();h.flushFrames();
  assert.equal(chat.querySelector('.woman-typing'),null);assert.equal(h.frames.size,0);assert.equal(h.listeners.size,0);
  h.render();assert.equal(chat.querySelector('.woman-typing'),null);assert.equal(h.frames.size,0);
});
