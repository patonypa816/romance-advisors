// Exercise startup rendering without a live Studio or paid cloud session.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../static/workbench.js'),'utf8');
const nodes=new Map();
const getNode=id=>{
  if(!nodes.has(id))nodes.set(id,{classList:{toggle(){}},removeAttribute(){},textContent:''});
  return nodes.get(id);
};
const state={program:{state:'starting',avatar_state:'idle',startup_stage:'waiting_player'}};
const context=vm.createContext({state,URL,$:getNode,programActive:()=>['starting','running','stopping'].includes(state.program.state),programLocked:()=>true,avatarSendPending:()=>false,phaseText:()=>'',TERMINAL:[],PHASES:{}});
vm.runInContext("let connected=true,submitting=false,programSubmitting=false,previewDemo=false,programRequestError='',heartbeatError='',playerSource='';"+source.slice(source.indexOf('function safeProgramMessage('),source.indexOf('async function operateProgram(')),context);
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
console.log(JSON.stringify({status:'passed',checks:['six startup phases','blocker reason visible','stop overrides startup phase']}));
