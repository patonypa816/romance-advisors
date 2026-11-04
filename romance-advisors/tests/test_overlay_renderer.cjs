// Execute the production UI, renderer and poll functions with a small DOM and
// controlled fetch/animation callbacks. No server, database or cloud is used.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {test}=require('node:test');
const rootDir=path.resolve(__dirname,'..');
const read=name=>fs.readFileSync(path.join(rootDir,'static',name),'utf8');
class Node {
  constructor(tag='div'){
    this.tag=tag;this.children=[];this.dataset={};this.style={};this.className='';this._text='';
    this.scrollTop=0;this.scrollHeight=1000;this.clientHeight=300;this.clientWidth=300;
    this.offsetTop=0;this.offsetHeight=40;this.isConnected=true;
    this.classList={contains:name=>this.className.split(' ').includes(name),toggle:(name,on)=>{
      const names=new Set(this.className.split(' ').filter(Boolean));
      if(on)names.add(name);else names.delete(name);this.className=[...names].join(' ');
    }};
  }
  set textContent(value){this._text=String(value);this.children=[];}
  get textContent(){return this._text+this.children.map(child=>child.textContent).join('');}
  append(...nodes){for(const node of nodes){node.parent=this;this.children.push(node);}}
  replaceChildren(...nodes){this._text='';this.children=[];this.append(...nodes);}
  setAttribute(){}
  querySelectorAll(selector){
    const found=[];
    for(const child of this.children){
      if(selector==='[data-message-id]'?child.dataset.messageId!==undefined:
         selector.startsWith('.')&&child.classList.contains(selector.slice(1)))found.push(child);
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
function harness(){
  const root=new Node(),frames=new Map(),errors=[];
  let frameId=0,fetchCount=0,current=state(),fetchError=null;
  const context=vm.createContext({document:{createElement:tag=>new Node(tag)},URLSearchParams,
    matchMedia:()=>({matches:true}),performance:{now:()=>0},
    requestAnimationFrame:callback=>{frames.set(++frameId,callback);return frameId;},
    cancelAnimationFrame:id=>frames.delete(id),root,
    layoutRects:()=>({phone:{},poll:{},boards:{}}),normalizeLayout:value=>value,
    console:{error:(...args)=>errors.push(args)},
    fetch:async()=>{fetchCount++;if(fetchError)throw fetchError;return {ok:true,json:async()=>current};}});
  vm.runInContext(read('ui.js').replaceAll('export ',''),context);
  vm.runInContext(read('renderer.js').replace(/^import .*;\n/gm,'').replace('export function ','function ')+
    ';var renderer=createLiveRenderer(root);',context);
  const overlay=read('overlay.js');
  vm.runInContext(overlay.slice(overlay.indexOf('let running='),overlay.indexOf('// Demo stays')),context);
  return {root,frames,errors,renderer:context.renderer,
    get state(){return current;},set state(value){current=value;},
    get fetchCount(){return fetchCount;},set fetchError(value){fetchError=value;},
    get phase(){return root.querySelector('.live-phase').textContent;},
    get running(){return vm.runInContext('running',context);},
    render(){context.renderer.render(current);},poll(){return vm.runInContext('poll()',context);},
    flushFrames(){for(const [id,callback] of [...frames])if(frames.delete(id))callback(0);}};
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
