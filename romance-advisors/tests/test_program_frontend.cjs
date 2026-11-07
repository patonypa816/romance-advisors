// Exercise startup rendering without a live Studio or paid cloud session.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../static/workbench.js'),'utf8');
const nodes=new Map();
const getNode=id=>{
  if(!nodes.has(id))nodes.set(id,{dataset:{},classList:{toggle(){}},removeAttribute(){},textContent:''});
  return nodes.get(id);
};
const state={program:{state:'starting',avatar_state:'idle',startup_stage:'waiting_player'}};
const context=vm.createContext({state,URL,shotForState:()=>"game",applyPortraitLayout(){},loadLayout:()=>({}),$:getNode,programActive:()=>['starting','running','stopping'].includes(state.program.state),programLocked:()=>Boolean(vm.runInContext('programSubmitting',context))||['starting','running','stopping'].includes(state.program.state),avatarSendPending:()=>false,phaseText:()=>'',TERMINAL:[],PHASES:{}});
vm.runInContext("let connected=true,submitting=false,programSubmitting=false,debugMode=false,modeSubmitting=false,previewDemo=false,programRequestError='',heartbeatError='',playerSource='';"+source.slice(source.indexOf('function safeProgramMessage('),source.indexOf('async function operateProgram(')),context);
for(const [stage,expected] of [['preparing_session',/检查会话/],['waiting_player',/等待唯一预览播放器连接/],['starting',/提交云会话启动请求/],['creating',/创建数字人云会话/],['connecting',/正在连接数字人/],['waiting_stage',/等待舞台就绪/]]){
  state.program.startup_stage=stage;
  state.program.avatar_state=stage;
  vm.runInContext('renderProgram();',context);
  assert.match(getNode('program-progress').textContent,expected);
  assert.equal(getNode('program-progress').hidden,false);
}
state.program={state:'error',avatar_state:'ended',error:'上一场云端结束尚未确认，禁止创建下一场。'};
vm.runInContext('renderProgram();',context);
assert.match(getNode('program-error').textContent,/上一场云端结束尚未确认/);
assert.equal(getNode('program-error').hidden,false);
assert.equal(getNode('program-progress').hidden,true);
state.program={state:'stopping',startup_stage:'creating',avatar_state:'creating'};
vm.runInContext('renderProgram();',context);
assert.match(getNode('program-progress').textContent,/结束数字人云会话/);
// OBS is the single receiver: the operator frame stays a labelled still and
// repair instructions never mount a second player or change cloud state.
state.program={state:'idle',content_mode:'chat',obs_output:true};
vm.runInContext('renderProgram();',context);
assert.match(getNode('program-note').textContent,/先打开 OBS.*已有.*节目（Program）.*静态构图参考/);
assert.match(getNode('preview-mode-note').textContent,/启动前打开 OBS/);
assert.equal(getNode('avatar-live-frame').hidden,true);
assert.equal(getNode('program-start').textContent,'启动数字人');
// Rehearsal explains the disabled start at the control, then fully restores it.
const idleNote=getNode('program-note').textContent;
assert.equal(getNode('program-start').disabled,false);
state.appearance={mode:'rehearsal',rehearsal_active:true};
vm.runInContext('renderProgram();',context);
assert.equal(getNode('program-start').disabled,true);
assert.equal(getNode('program-start').textContent,'请先结束试玩');
assert.equal(getNode('program-note').textContent,'在「约会前照镜子」中点击「结束试玩」，再启动数字人。');
vm.runInContext('renderProgram();',context);
assert.equal(getNode('program-start').textContent,'请先结束试玩');
state.appearance={mode:'off',rehearsal_active:false};
vm.runInContext('renderProgram();',context);
assert.equal(getNode('program-start').disabled,false);
assert.equal(getNode('program-start').textContent,'启动数字人');
assert.equal(getNode('program-note').textContent,idleNote);
// Existing blockers remain in effect after leaving rehearsal.
for(const blocker of ['connected=false','submitting=true','state.busy=true','state.model={ready:false}','state.program.content_mode=undefined']){
  vm.runInContext(blocker+';renderProgram();',context);
  assert.equal(getNode('program-start').disabled,true,blocker);
  vm.runInContext('connected=true;submitting=false;state.busy=false;state.model={ready:true};state.program.content_mode="chat";',context);
}
vm.runInContext('debugMode=true;renderProgram();',context);
assert.equal(getNode('program-start').textContent,'开始自动联调');
state.appearance={rehearsal_active:true};
vm.runInContext('renderProgram();',context);
assert.equal(getNode('program-start').textContent,'请先结束试玩');
assert.equal(getNode('program-start').disabled,true);
vm.runInContext('debugMode=false;',context);
vm.runInContext("programSubmitting='start';renderProgram();",context);
assert.equal(getNode('program-start').textContent,'正在提交启动…');
assert.equal(getNode('program-progress').hidden,false);
assert.match(getNode('program-progress').textContent,/已点击启动/);
assert.equal(getNode('program-note').textContent,idleNote);
state.appearance={rehearsal_active:false};
vm.runInContext('programSubmitting=false;',context);
state.program={state:'starting',content_mode:'chat',obs_output:true,startup_stage:'waiting_player',elapsed_seconds:42};
vm.runInContext('renderProgram();',context);
assert.equal(getNode('program-start').textContent,'等待 OBS 接收…');
assert.equal(getNode('program-start').disabled,true);
assert.equal(getNode('program-stop').disabled,false);
assert.match(getNode('program-progress').textContent,/打开 OBS.*90 秒.*42 秒/);
assert.match(getNode('preview-mode-note').textContent,/正在启动.*静态参考/);
assert.match(getNode('preview-portrait-label').textContent,/实时画面看 OBS/);
assert.equal(getNode('avatar-live-frame').hidden,true);
state.program={state:'error',content_mode:'chat',obs_output:true,error:'预览连接超时',last_failure:{code:'player_timeout'}};
vm.runInContext('renderProgram();',context);
assert.match(getNode('program-error').textContent,/OBS 接收超时.*已有数字人节目场景.*结束状态确认/);
state.program={state:'error',content_mode:'chat',last_failure:{code:'startup_timeout'}};
vm.runInContext('renderProgram();',context);
assert.match(getNode('program-error').textContent,/云会话启动超时.*确认上一场结束/);
state.program={state:'error',content_mode:'game',error:'游戏已停止',last_failure:{stage:'PREPARING',code:'invalid_private_preferences'}};
vm.runInContext('renderProgram();',context);
assert.match(getNode('program-error').textContent,/开局故事未就绪.*女生私下偏好.*游戏已停止/);
state.program={state:'running',content_mode:'chat',obs_output:true,ready:true};
vm.runInContext('renderProgram();',context);
assert.match(getNode('preview-mode-note').textContent,/实时音画请在 OBS 核对/);
assert.equal(getNode('program-start').textContent,'数字人已启动');
assert.equal(getNode('avatar-live-frame').hidden,true);
state.program={state:'starting',content_mode:'chat',obs_output:false,startup_stage:'waiting_player',player_url:'http://127.0.0.1:1234/player'};
vm.runInContext('renderProgram();',context);
assert.equal(getNode('program-start').textContent,'等待播放器…');
assert.match(getNode('program-note').textContent,/工作台接收模式/);
assert.equal(getNode('avatar-live-frame').hidden,false);
assert.match(getNode('program-progress').textContent,/等待唯一预览播放器/);
console.log(JSON.stringify({status:'passed',checks:['rehearsal blocker and exit restoration','existing start blockers and debug labels','six startup phases','blocker reason visible','stop overrides startup phase','OBS preparation instructions before startup','immediate submission feedback','OBS wait and stop button','timeout recovery','OBS still versus sole preview receiver']}));
