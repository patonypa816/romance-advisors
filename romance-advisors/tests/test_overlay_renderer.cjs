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
  vm.runInContext(overlay.slice(overlay.indexOf('let running='),overlay.indexOf('// Demo stays')),context);
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

test('unchanged state renders repeatedly without an error or scroll reset',()=>{
  const h=harness();h.render();h.flushFrames();
  const chat=h.root.querySelector('.live-chat');chat.scrollTop=75;
  for(let i=0;i<5;i++)h.render();
  assert.equal(h.phase,'观众投票中');assert.equal(chat.scrollTop,75);
  assert.equal(chat.querySelectorAll('[data-message-id]').length,1);
  assert.equal(h.frames.size,0);
  h.state={messages:[],leaderboards:{best:[],worst:[]}};
  h.render();h.flushFrames();h.render();
  assert.equal(h.phase,'等待开场');
});

test('votes and leaderboard update while the same messages remain visible',()=>{
  const h=harness();h.render();h.flushFrames();
  h.state.round.options[0].votes=7;
  h.state.leaderboards.best=[{nickname:'军师甲',rate:1,adopted:2}];h.render();
  assert.equal(h.root.querySelector('.option-votes').textContent,'7票');
  assert.equal(h.root.querySelector('.board-name').textContent,'军师甲');
  assert.equal(h.phase,'观众投票中');
});

test('affinity fill matches the preview anchors and OKLab intermediate colors',()=>{
  const h=harness(),track=h.root.querySelector('.affinity-track'),fill=track.children[0];
  const samples=[
    [0,'rgb(120,135,159)'],[1,'rgb(122,136,161)'],[12.5,'rgb(150,153,186)'],
    [25,'rgb(181,172,213)'],[37.5,'rgb(205,206,176)'],[50,'rgb(232,238,122)'],
    [62.5,'rgb(245,209,128)'],[75,'rgb(255,179,132)'],[87.5,'rgb(255,147,126)'],
    [99,'rgb(255,114,121)'],[100,'rgb(255,111,120)'],
  ];
  for(const [value,color] of samples){
    h.state.episode.score=value;h.render();
    assert.equal(fill.style.backgroundColor,color,`score ${value}`);
    assert.equal(fill.style.width,`${value}%`);
    assert.equal(h.number.textContent,String(value));
  }
  assert.deepEqual(Object.keys(fill.style).sort(),['backgroundColor','width']);
  assert.deepEqual(track.style,{});
  assert.match(read('affinity-panel.css'),/transition:width 480ms cubic-bezier\(.22,.7,.2,1\),background-color 480ms ease/);
});

test('affinity colors are continuous at anchors and display values remain bounded',()=>{
  const h=harness(),fill=h.root.querySelector('.affinity-track').children[0];
  for(const [value,color] of [[25,'rgb(181,172,213)'],[50,'rgb(232,238,122)'],[75,'rgb(255,179,132)']]){
    for(const offset of [-.001,0,.001]){
      h.state.episode.score=value+offset;h.render();
      assert.equal(fill.style.backgroundColor,color);
      assert.equal(fill.style.width,`${value+offset}%`);
    }
  }
  for(const [value,color] of [[-5,'rgb(120,135,159)'],[105,'rgb(255,111,120)']]){
    h.state.episode.score=value;h.render();
    assert.equal(fill.style.backgroundColor,color);
    assert.equal(fill.style.width,`${Math.max(0,Math.min(100,value))}%`);
  }
  h.state={messages:[],leaderboards:{best:[],worst:[]}};h.render();
  assert.equal(fill.style.width,'0%');
  assert.equal(h.number.textContent,'—');
});

test('polling updates affinity colors in either direction without replacing the fill',async()=>{
  const h=harness(),track=h.root.querySelector('.affinity-track'),fill=track.children[0];
  for(const [value,color] of [[50,'rgb(232,238,122)'],[100,'rgb(255,111,120)'],[0,'rgb(120,135,159)'],[37.5,'rgb(205,206,176)'],[50,'rgb(232,238,122)']]){
    h.state.episode.score=value;
    for(let i=0;i<3;i++){
      await h.poll();h.flushFrames();
      assert.strictEqual(track.children[0],fill);
      assert.equal(fill.style.backgroundColor,color);
      assert.equal(fill.style.width,`${value}%`);
      assert.equal(h.running,false);assert.equal(h.frames.size,0);
    }
  }
  assert.equal(h.fetchCount,15);assert.equal(h.errors.length,0);
});

test('new messages follow latest and stable voting retains only one scroll loop',()=>{
  const h=harness();h.render();h.flushFrames();
  h.state.messages.push({id:'m2',role:'woman',text:'新增的一段长回复'});h.render();
  const chat=h.root.querySelector('.live-chat');
  const message=chat.querySelectorAll('[data-message-id]')[1];
  message.offsetTop=100;message.offsetHeight=600;
  h.flushFrames();assert.equal(chat.scrollTop,84);assert.equal(h.frames.size,1);
  for(let i=0;i<5;i++)h.render();
  assert.equal(h.frames.size,1);
  h.state.round.phase='COLLECTING';h.render();h.flushFrames();
  assert.equal(h.frames.size,0);assert.equal(h.phase,'征集建议中');
});

test('state failures are distinct, release polling and recover on the same state',async()=>{
  const h=harness();await h.poll();
  h.fetchError=new Error('synthetic fetch failure');await h.poll();await h.poll();
  assert.equal(h.phase,'游戏状态读取失败');assert.equal(h.running,false);
  assert.equal(h.errors.length,1);assert.equal(h.errors[0][0],'叠层状态读取失败');
  h.fetchError=null;await h.poll();
  assert.equal(h.phase,'观众投票中');assert.equal(h.fetchCount,4);
  h.fetchError=new Error('new outage after recovery');await h.poll();
  assert.equal(h.errors.length,2);
});

test('render and layout failures are not reported as connection loss and recover',async()=>{
  const h=harness();await h.poll();
  const render=h.renderer.render;
  h.renderer.render=()=>{throw new Error('synthetic render failure');};
  await h.poll();await h.poll();
  assert.equal(h.phase,'画面更新失败');assert.equal(h.errors.length,1);
  assert.equal(h.errors[0][0],'叠层画面更新失败');assert.equal(h.running,false);
  h.renderer.render=render;await h.poll();assert.equal(h.phase,'观众投票中');
  const setLayout=h.renderer.setLayout;h.state.layout={};
  h.renderer.setLayout=()=>{throw new Error('synthetic layout failure');};
  await h.poll();assert.equal(h.phase,'画面更新失败');assert.equal(h.running,false);
  h.renderer.setLayout=setLayout;await h.poll();
  assert.equal(h.phase,'观众投票中');assert.equal(h.errors.length,2);
});

function settle(h,value,delta,id='r1'){
  h.state.episode.score=value;
  h.state.round={id,number:1,phase:'DONE',options:[],delta};
}

test('settled feedback runs once across 900ms public-state polls and expires at 2400ms',async()=>{
  const h=harness({reduced:false});await h.poll();h.flushFrames();
  assert.equal(h.number.textContent,'50');assert.equal(h.change.textContent,'');
  assert.equal(h.fill.transitionAtLayout,'none');
  settle(h,60,10);await h.poll();h.flushFrames();
  assert.equal(h.change.textContent,'+10');assert.equal(h.card.animations.length,1);
  const timer=[...h.timers.keys()][0];
  for(let i=0;i<2;i++){h.advance(900);await h.poll();assert.deepEqual([...h.timers.keys()],[timer]);}
  assert.equal(h.number.textContent,'60');assert.equal(h.card.animations.length,1);
  h.advance(600);assert.equal(h.change.textContent,'');
  for(let i=0;i<3;i++){h.advance(900);await h.poll();}
  assert.equal(h.change.textContent,'');assert.equal(h.card.animations.length,1);
  assert.equal(h.errors.length,0);assert.equal(h.timers.size,0);assert.equal(h.frames.size,0);
});

test('rapid gains and losses cancel prior motion and settle to the exact fractional target',()=>{
  const h=harness({reduced:false});h.render();h.flushFrames();
  settle(h,60,10);h.render();h.flushFrames();h.advance(100);
  const interrupted=Number(h.number.textContent);assert(interrupted>50&&interrupted<60);
  settle(h,30.5,-29.5,'r2');h.render();h.flushFrames();
  assert.equal(h.change.textContent,'−29.5');assert(h.card.classList.contains('loss'));
  assert(h.card.animations[0].cancelled);
  h.advance(480);assert.equal(h.number.textContent,'30.5');assert.equal(h.fill.style.width,'30.5%');
  assert.equal(h.frames.size,0);assert.equal(h.number.style.fontSize,'39px');
});

test('new episode or session clears feedback and immediately sets the bar without a transition',()=>{
  const h=harness({reduced:false});h.state.session={id:'s1'};h.render();h.flushFrames();
  settle(h,60,10);h.render();h.flushFrames();h.advance(100);
  h.state.episode.id='e2';h.state.episode.score=20;h.render();h.flushFrames();
  assert.equal(h.number.textContent,'20');assert.equal(h.fill.style.width,'20%');
  assert.equal(h.change.textContent,'');assert.equal(h.root.querySelector('.affinity-sr-only').textContent,'');
  assert.equal(h.fill.transitionAtLayout,'none');assert.equal(h.frames.size,0);assert.equal(h.timers.size,0);
  settle(h,30,10,'r2');h.render();h.flushFrames();assert.equal(h.change.textContent,'+10');
  h.state.session.id='s2';h.state.episode.score=50;h.render();h.flushFrames();
  assert.equal(h.number.textContent,'50');assert.equal(h.change.textContent,'');assert.equal(h.frames.size,0);
});

test('switching rounds clears old feedback even when the score is unchanged',()=>{
  const h=harness();h.render();h.flushFrames();settle(h,60,10);h.render();h.flushFrames();
  assert.equal(h.change.textContent,'+10');
  h.state.round={id:'r2',number:2,phase:'COLLECTING',options:[],delta:null};h.render();h.flushFrames();
  assert.equal(h.change.textContent,'');assert.equal(h.timers.size,0);
  assert.equal(h.root.querySelector('.affinity-sr-only').textContent,'');
});

test('stale, mismatched, repeated and zero deltas never invent a new round effect',()=>{
  const h=harness();h.render();h.flushFrames();
  settle(h,60,3);h.render();assert.equal(h.change.textContent,'');
  settle(h,63,3);h.render();assert.equal(h.change.textContent,''); // same completed ID
  settle(h,66,3,'r2');h.state.round.phase='WAITING_REPLY';h.render();assert.equal(h.change.textContent,'');
  settle(h,70,3,'r3');h.render();assert.equal(h.change.textContent,''); // missed or inconsistent change
  settle(h,70,0,'r4');h.render();assert.equal(h.change.textContent,'');
  settle(h,70.1,70.1-70,'r5');h.render();assert.equal(h.change.textContent,'+0.1');
  assert.equal(h.number.textContent,'70.1');
});

test('first load of a completed round is static and empty episodes remain empty across reduced-motion changes',()=>{
  const h=harness({reduced:false});settle(h,100,50);h.render();h.flushFrames();
  assert.equal(h.number.textContent,'100');assert.equal(h.change.textContent,'');assert.equal(h.card.animations,undefined);
  h.state.episode=null;h.state.round=null;h.render();h.flushFrames();
  h.setReduced(true);h.setReduced(false);
  assert.equal(h.number.textContent,'—');assert.equal(h.fill.style.width,'0%');
  assert.equal(h.root.querySelector('.affinity-track').attributes['aria-valuetext'],'暂无亲密度');
});

test('reduced motion settles in-flight numbers, keeps static feedback and never replays on toggle',()=>{
  const h=harness({reduced:false});h.render();h.flushFrames();
  settle(h,60,10);h.render();h.flushFrames();h.advance(100);
  h.setReduced(true);assert.equal(h.number.textContent,'60');assert.equal(h.frames.size,0);
  assert.equal(h.change.textContent,'+10');assert(h.card.animations.every(a=>a.cancelled));
  settle(h,40,-20,'r2');h.render();h.flushFrames();assert.equal(h.number.textContent,'40');assert.equal(h.change.textContent,'−20');
  assert.equal(h.card.animations.length,1);h.setReduced(false);h.render();h.flushFrames();
  assert.equal(h.card.animations.length,1);h.advance(2400);assert.equal(h.change.textContent,'');
});

test('renderer disposal removes affinity callbacks and later polls cannot restart its motion',()=>{
  const h=harness({reduced:false});h.render();h.flushFrames();settle(h,60,10);h.render();h.flushFrames();
  assert.equal(h.listeners.size,2);assert.equal(h.timers.size,1);assert.equal(h.frames.size,1);
  h.renderer.dispose();assert.equal(h.listeners.size,0);assert.equal(h.timers.size,0);assert.equal(h.frames.size,0);
  settle(h,70,10,'r2');h.render();h.flushFrames();
  assert.equal(h.timers.size,0);assert.equal(h.frames.size,0);assert.equal(h.change.textContent,'');
});

test('approved card CSS stays fixed and no preview testing controls enter the production workbench',()=>{
  const css=read('affinity-panel.css'),source=read('affinity-panel.js');
  assert.match(css,/flex:0 0 76px;height:76px;min-height:76px;width:100%/);
  assert.match(css,/grid-template-columns:minmax\(0,1fr\) 92px/);
  assert.match(css,/@media\(prefers-reduced-motion:reduce\)/);
  assert.doesNotMatch(source,/heart|♥|❤|createElement\('input'\)|type=['"]range/);
  assert.doesNotMatch(read('renderer.js'),/live-score-row|live-delta|live-score-track/);
  assert.match(read('overlay.html'),/href="\/static\/affinity-panel.css"/);
  assert.match(read('index.html'),/id="safe-top"/); // Existing layout controls remain intact.
});


test('disposal cancels a pending first-follow callback before it can restart scrolling',()=>{
  const h=harness({reduced:false});h.render();
  const message=h.root.querySelector('.live-chat').querySelectorAll('[data-message-id]')[0];
  message.offsetTop=100;message.offsetHeight=600;
  assert.equal(h.frames.size,1);h.renderer.dispose();h.flushFrames();
  assert.equal(h.frames.size,0);assert.equal(h.listeners.size,0);
  h.render();h.flushFrames();assert.equal(h.frames.size,0);
});


function endEpisode(h,{status='SUCCEEDED',elapsed=0,awards={best:{nickname:'最强本人'},worst:{nickname:'敢说本人'}}}={}) {
  h.state.episode.status=status;h.state.episode.score=status==='SUCCEEDED'?100:0;
  h.state.round.phase='DONE';
  h.state.finale={episode_id:h.state.episode.id,status,started_at:1000,duration_ms:6800,elapsed_ms:elapsed,awards};
}
function flight(h){return h.root.querySelector('.emblem-flight').animations.at(-1);}

test('committed success adds one overlay above the unchanged game and repeated polls only advance',()=>{
  const h=harness({reduced:false});h.render();h.flushFrames();
  const phone=h.root.querySelector('.live-conversation'),chat=h.root.querySelector('.live-chat');
  endEpisode(h);h.render();h.flushFrames();
  const initial=flight(h),card=h.root.querySelector('.medal-best');
  assert.equal(h.finale.hidden,false);assert.equal(h.finale.dataset.mode,'success');
  assert.equal(h.finale.dataset.state,'burst');assert.equal(initial.currentTime,0);
  h.advance(1000);assert.equal(initial.currentTime,1000);
  h.state.finale.awards.best.nickname='不得替换的晚到数据';h.state.finale.elapsed_ms=10;
  for(let i=0;i<4;i++)h.render();
  assert.equal(flight(h),initial);assert.equal(initial.currentTime,1000);
  assert.equal(h.root.querySelector('.medal-best'),card);assert.equal(card.querySelector('h3').textContent,'最强本人');
  assert.equal(h.root.querySelector('.live-conversation'),phone);assert.equal(h.root.querySelector('.live-chat'),chat);
  h.advance(6000);assert.equal(initial.currentTime,6800);assert.equal(h.finale.dataset.state,'complete');
  h.render();h.flushFrames();assert.equal(h.frames.size,0);assert.equal(flight(h),initial);
});

test('refresh resumes at server elapsed time and old or missing timing shows a static result',()=>{
  const h=harness({reduced:false});endEpisode(h,{elapsed:3500});h.render();h.flushFrames();
  assert.equal(flight(h).currentTime,3500);assert.equal(h.finale.dataset.state,'awards');
  const old=harness({reduced:false});endEpisode(old,{elapsed:900000});old.render();old.flushFrames();
  assert.equal(flight(old).currentTime,6800);assert.equal(old.frames.size,0);
  const missing=harness({reduced:false});endEpisode(missing);delete missing.state.finale.elapsed_ms;
  missing.render();missing.flushFrames();assert.equal(flight(missing).currentTime,6800);
});

test('failed results use the approved failure copy, with empty awards instead of fake people',()=>{
  const h=harness({reduced:true});endEpisode(h,{status:'FAILED',awards:{best:null,worst:null}});h.render();h.flushFrames();
  assert.equal(h.finale.dataset.mode,'failure');assert.equal(h.root.querySelector('.main-result').textContent,'没成…');
  assert.equal(h.root.querySelector('.main-joke').textContent,'建议军师先别开班。');
  assert.equal(h.root.querySelector('.medal-best').querySelector('h3').textContent,'本期暂缺');
  assert.equal(h.root.querySelector('.medal-goofy').querySelector('h3').textContent,'本期暂缺');
  assert.equal(h.frames.size,0);
});

test('no terminal score inference, technical ending, mismatched episode or stale snapshot can start an overlay',()=>{
  const h=harness();h.state.episode.score=100;h.render();assert.equal(h.finale.hidden,true);
  endEpisode(h);h.state.finale.episode_id='old-episode';h.render();assert.equal(h.finale.hidden,true);
  endEpisode(h);h.state.episode.status='TECH_ENDED';h.render();assert.equal(h.finale.hidden,true);
  endEpisode(h);h.state.finale.started_at=NaN;h.render();assert.equal(h.finale.hidden,true);
  endEpisode(h);delete h.state.finale;h.render();assert.equal(h.finale.hidden,true);
});

test('episode replacement, reset and renderer disposal cancel effects without touching the game DOM',()=>{
  const h=harness({reduced:false});endEpisode(h);h.render();h.flushFrames();const a=flight(h);
  h.state.episode={id:'e2',status:'ACTIVE',score:50};h.state.finale=null;h.render();h.flushFrames();
  assert.equal(h.finale.hidden,true);assert.equal(a.cancelled,true);assert.equal(h.frames.size,0);
  endEpisode(h);h.render();h.flushFrames();const b=flight(h);assert.notEqual(b,a);
  h.renderer.dispose();assert.equal(b.cancelled,true);assert.equal(h.finale.hidden,true);assert.equal(h.listeners.size,0);
  h.render();h.advance(9000);assert.equal(h.frames.size,0);assert.equal(h.finale.hidden,true);
});

test('changing reduced motion mid-flight settles once and never replays on toggle or poll',()=>{
  const h=harness({reduced:false});endEpisode(h);h.render();h.flushFrames();h.advance(800);
  const a=flight(h);h.setReduced(true);assert.equal(a.currentTime,6800);assert.equal(h.frames.size,0);
  h.setReduced(false);h.state.finale.elapsed_ms=1200;h.render();h.flushFrames();
  assert.equal(flight(h),a);assert.equal(a.currentTime,6800);assert.equal(h.frames.size,0);
});

test('suspending for browser history pauses callbacks, then resumes from server elapsed',()=>{
  const h=harness({reduced:false});endEpisode(h);h.render();h.flushFrames();const a=flight(h);
  h.renderer.suspend();h.state.finale.elapsed_ms=3500;h.render();h.advance(3500);
  assert.equal(h.frames.size,0);assert.equal(a.currentTime,0);
  h.renderer.resume();h.render();assert.equal(a.currentTime,3500);
  h.finale.isConnected=false;h.flushFrames();assert.equal(a.cancelled,true);assert.equal(h.frames.size,0);
});

test('award names are text-only and unsafe or missing avatars use circular initials',()=>{
  const name='<img src=x onerror=alert(1)>';
  const h=harness();endEpisode(h,{awards:{best:{nickname:name,avatar_url:'javascript:alert(1)'},worst:{nickname:'  阿明',avatar_url:'https://p3.douyinpic.com/avatar.jpg'}}});h.render();
  const best=h.root.querySelector('.medal-best'),worst=h.root.querySelector('.medal-goofy');
  assert.equal(best.querySelector('h3').textContent,name);assert.equal(best.querySelector('h3').children.length,0);
  assert.equal(best.querySelector('img').hidden,true);assert.equal(best.querySelector('img').src,undefined);
  assert.equal(worst.querySelector('img').hidden,false);assert.equal(worst.querySelector('img').referrerPolicy,'no-referrer');
  worst.querySelector('img').events.error();assert.equal(worst.querySelector('img').hidden,true);
  for(const url of ['http://p3.douyinpic.com/a','https://evil.test/a','https://douyinpic.com.evil.test/a','https://user:pass@douyinpic.com/a','https://douyinpic.com:8443/a','data:image/svg+xml,test','https://p3.douyinpic.com/a#x','https://p3.douyinpic.com/a b','https://p3.douyinpic.com\\evil.test/a'])assert.equal(h.safeAvatar(url),'',url);
  assert.equal(h.safeAvatar('https://p3.douyinpic.com/a'),'https://p3.douyinpic.com/a');
  assert.doesNotMatch(read('finale-overlay.js'),/innerHTML|insertAdjacentHTML|\.outerHTML/);
});

test('production keeps preview controls out and only trusts its own overlay for iframe stacking',()=>{
  assert.match(read('overlay.html'),/href="\/static\/finale-overlay.css"/);
  assert.doesNotMatch(read('finale-overlay.js'),/createElement\('(?:button|input)'\)|postMessage|fetch\(/);
  assert.match(read('workbench.js'),/event.origin!==location.origin\|\|event.source!==\$\('preview-frame'\).contentWindow/);
  assert.match(read('workbench.css'),/has-finale #preview-frame\{z-index:2;pointer-events:none\}/);
  assert.match(read('overlay.js'),/event.persisted\)\{renderer.resume\(\)/);
});


test('random fallback award is explicitly labelled as a playful draw, not a negative score',()=>{
  const h=harness();endEpisode(h,{awards:{best:null,worst:{nickname:'仅提过建议',adopted:0,selection_reason:'random_suggestion'}}});h.render();
  assert.equal(h.root.querySelector('.medal-goofy').querySelector('small').textContent,'本期随机彩蛋');
  assert.equal(h.root.querySelector('.medal-goofy').querySelector('h3').textContent,'仅提过建议');
});
