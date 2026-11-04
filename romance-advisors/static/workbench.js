import {el,getState,post,phaseText,TERMINAL,PHASES} from './ui.js';
import {DEFAULT_LAYOUT,loadLayout,saveLayout,layoutRects,observeLayout} from './layout.js';
const $=id=>document.getElementById(id);
// Preview mode never changes the real game state; starting a real program explicitly leaves it.
let previewDemo=$('show-preview-demo').checked;
function setPreviewDemo(enabled){
  previewDemo=Boolean(enabled);$('show-preview-demo').checked=previewDemo;
  const source=previewDemo?'/overlay?demo=1':'/overlay';
  if($('preview-frame').getAttribute('src')!==source)$('preview-frame').src=source;
  $('avatar-preview-image').hidden=!previewDemo;
  $('phone-preview').classList.toggle('preview-demo',previewDemo);
  $('preview-mode-note').textContent=previewDemo?'排版示例 · 静态人物与虚构内容，不写入游戏。':'实时游戏画面';
}
let state=null, connected=false, submitting=false, polling=false, selectedOption=null, httpError='', layoutSaving=false, serverLayoutSignature='', lastServerBusy=false;
let programSubmitting=false, heartbeatSending=false, programRequestError='', heartbeatError='', playerSource='', manualAutoAdvance=null;
const activeProgramStates=new Set(['starting','running','stopping']);
const programActive=()=>activeProgramStates.has(state?.program?.state);
const programLocked=()=>programSubmitting||programActive();
let settingsDirty=false, commentSending=false, batchSending=false, pendingComment=null;
let commentsSignature='', roundFilterSignature='', proposalsSignature='', voteSignature='', batchFeedback='';
let participantRevision=0;
const commentRows=new Map(), proposalRows=new Map();
let renderedCommentRevision=-1, renderedCommentCursor=0, renderedNameRevision=-1, renderedProposalVersion=null;
let indexedVoteVersion=null, votesByAuthor=new Map();
const pendingKey='romance-advisors:pending-comment';
try { const saved=JSON.parse(sessionStorage.getItem(pendingKey)||'null'); if(saved?.body?.event_id)pendingComment=saved; } catch {}
function savePending(value){pendingComment=value;try{if(value)sessionStorage.setItem(pendingKey,JSON.stringify(value));else sessionStorage.removeItem(pendingKey);}catch{}}

let participants;try{participants=JSON.parse(localStorage.getItem('romance-advisors:participants')||'null');}catch{}
if(!Array.isArray(participants)||!participants.length)participants=['小舟','阿桃','老陈'].map(name=>({id:`manual-${crypto.randomUUID()}`,nickname:`${name}（模拟）`}));
let currentParticipant=participants[0].id;

function storeParticipants(){localStorage.setItem('romance-advisors:participants',JSON.stringify(participants));}
function displayName(id,nickname){const matches=participants.filter(p=>p.nickname===nickname);const index=matches.findIndex(p=>p.id===id);return matches.length>1&&index>=0?`${nickname} · ${index+1}`:nickname;}
function renderParticipants(){const select=$('participant-select');select.replaceChildren();participants.forEach(p=>{const option=el('option','',displayName(p.id,p.nickname));option.value=p.id;select.append(option);});select.value=currentParticipant;$('participant-count').textContent=`${participants.length} 位模拟参与者`;participantRevision++;proposalsSignature='';voteSignature='';} 
function participant(){return participants.find(p=>p.id===currentParticipant)||participants[0];}
function error(message){$('operation-error').textContent=message;$('operation-error').hidden=!message;}
function status(message){$('operation-status').textContent=message;$('operation-status').hidden=!message;}
function actionFor(s){if(!s?.episode)return {label:'开始第一期',path:'/api/episode/new'};if(TERMINAL.includes(s.episode.status))return {label:'开始下一期',path:'/api/episode/new'};const phase=s.round?.phase;if(!phase||phase==='DONE'||phase==='ABORTED')return {label:'开始下一轮征集',path:'/api/round/start'};if(phase==='COLLECTING')return {label:'结束征集，编选建议',path:'/api/collection/close'};if(phase==='VOTING')return {label:'结束投票，发送回复',path:'/api/voting/close'};return {label:phaseText(s),path:null};}
function avatarSendPending(round){
  if(round?.phase!=='WAITING_REPLY')return false;
  const performance=round.performance;
  if(typeof performance==='string')return ['pending','sending'].includes(performance);
  // A sent or released record has crossed the local sending boundary; cloud rendering continues independently.
  return Boolean(performance&&performance.man_released!==true&&performance.status!=='sent');
}
function renderState(next){state=next;if(previewDemo&&(programActive()||next.settings?.auto_advance||next.busy))setPreviewDemo(false);$('show-preview-demo').disabled=programActive()||Boolean(next.settings?.auto_advance)||Boolean(next.busy)||Boolean(programSubmitting);const episode=next.episode,round=next.round,busy=Boolean(next.busy)||submitting,programOwns=programLocked();const action=actionFor(next);
  const ready=next.model?.ready!==false,route=next.model?.billing_route==='token_plan'?' · Token Plan':'';$('model-status').textContent=ready?`模型：${next.model?.name||'已配置'}${route}`:`模型未配置${next.model?.last_error?'：'+next.model.last_error:''}`;
  $('primary-action').textContent=programOwns?'自动联调控制中':busy?'正在处理…':action.label;$('primary-action').disabled=previewDemo||!connected||programOwns||busy||!action.path||!ready||Boolean(next.error);
  $('retry-action').hidden=!next.error||next.error?.retryable===false;$('retry-action').disabled=previewDemo||programOwns||busy||!ready;
  $('action-note').textContent=previewDemo?'取消「完整排版示例」后可手动推进。':programOwns?'联调管理推进，评论可照常发送。':!ready?'先配置模型凭证，再开始一期。凭证只放本机环境，不写入网页。':busy?'模型正在处理，页面刷新会读取已有结果。':!episode||TERMINAL.includes(episode.status)?'开始一期将调用真实模型。默认由你手动推进。':'';
  if(next.error)error(typeof next.error==='string'?next.error:next.error.message||'当前步骤遇到技术问题，请重试。');else error(httpError);
  if(next.layout&&!layoutSaving){const signature=JSON.stringify(next.layout);if(signature!==serverLayoutSignature){serverLayoutSignature=signature;renderLayout(saveLayout(next.layout));}}
  renderProgram();renderCommentControls();renderAutomation();renderVoteOptions();renderProposals();renderComments();renderPerformance();
  // Polling must not erase unsaved settings when focus moves to another field.
  if(!settingsDirty){
    if(!programOwns)$('auto-advance').checked=Boolean(next.settings?.auto_advance);
    for(const [id,key] of [['collect-seconds','collect_seconds'],['vote-seconds','vote_seconds']])if(next.settings?.[key]!=null)$(id).value=next.settings[key];
  }
  $('abort-episode').disabled=!connected||programOwns||submitting||!episode||TERMINAL.includes(episode.status);$('new-session').disabled=!connected||programOwns||busy;
  if(programOwns)status('自动联调负责推进；你可以继续发送模拟评论。');else if(httpError)status('操作未完成；检查原因后可重新操作。');else if(next.automation?.note)status(next.automation.note);else if(next.error)status('当前步骤已暂停，请查看错误并重试。');else if(next.busy)status(phaseText(next)+'，请稍候。');else if(lastServerBusy){const completed=TERMINAL.includes(episode?.status)?'本期已结束，可以开始下一期。':round?.phase==='COLLECTING'?'准备完成，现在可以在下方提交模拟建议。':round?.phase==='VOTING'?'候选已准备好，可以在下方模拟投票。':round?.phase==='DONE'?'本轮已完成，消息和亲密度已经保存。':episode?'本期已准备好，可以开始下一轮征集。':'准备就绪，可以开始第一期。';status(completed);}
  lastServerBusy=Boolean(next.busy);
}
function safeProgramMessage(value){
  // Cloud player links may carry session access. They belong only in the iframe src.
  return String(value||'').replace(/https?:\/\/[^\s<>"']+/gi,'[播放地址已隐藏]');
}
function renderProgram(){
  const program=state?.program||{},running=programActive(),locked=programLocked();
  const labels={off:'尚未启动',starting:'正在准备数字人',running:'自动联调中',stopping:'正在结束云会话',stopped:'联调已停止',error:'联调遇到错误'};
  $('program-state').textContent=programSubmitting?(programSubmitting==='start'?'正在提交启动…':'正在提交停止…'):labels[program.state]||'尚未启动';
  $('program-state').classList.toggle('running',running&&program.state!=='stopping');
  $('program-state').classList.toggle('failed',program.state==='error');
  $('program-start').disabled=!connected||locked||submitting||Boolean(state?.busy)||state?.model?.ready===false;
  $('program-stop').disabled=!connected||Boolean(programSubmitting)||!running||program.state==='stopping';
  const elapsed=Math.max(0,Math.floor(Number(program.elapsed_seconds)||0)),limit=Number(program.limit_minutes)||20;
  $('program-elapsed').textContent=running||elapsed?`已联调 ${Math.floor(elapsed/60)} 分 ${String(elapsed%60).padStart(2,'0')} 秒 / 跨期 ${limit} 分钟`:`跨期累计上限 ${limit} 分钟`;
  $('program-avatar').textContent=`数字人：${program.avatar_name||'写实男主 · 近景 3:4'}`;
  const avatarLabels={off:'云会话未启动',idle:'云会话空闲',starting:'云会话正在启动',creating:'正在创建云会话',connecting:'正在连接数字人',waiting_stage:'等待数字人舞台就绪',creation_unknown:'云会话创建结果不明',stop_unconfirmed:'云会话结束尚未确认',running:'云会话运行中',stopping:'云会话正在结束',stopped:'云会话已结束',ended:'云会话已结束',error:'云会话异常',ready:'云会话已就绪'};
  $('program-avatar-state').textContent=program.ready?'数字人服务已就绪':avatarLabels[program.avatar_state]||(running?'等待数字人服务就绪':'云会话未启动');
  const round=state?.round;
  const startupLabels={preparing_session:'正在检查会话状态与独占权限。',waiting_player:'等待唯一预览播放器连接；连接后开始创建云会话。',starting:'正在提交云会话启动请求。',creating:'正在创建数字人云会话，请稍候。',connecting:'云会话已创建，正在连接数字人。',waiting_stage:'数字人已连接，等待舞台就绪后自动开场。'};
  const progress=program.state==='starting'?(startupLabels[program.startup_stage]||'正在准备数字人，完成后自动开场。'):program.state==='stopping'?'正在停止自动推进并结束数字人云会话，请稍候。':program.state==='running'?state?.error?'游戏步骤遇到错误，已停止继续发送；记录保留，查看下方原因。':avatarSendPending(round)?'正在发送数字人台词 · 发送完成后自动继续聊天与结算':round?.remaining_seconds!=null?`${PHASES[round.phase]||round.phase} · 剩余 ${Math.ceil(round.remaining_seconds)} 秒` :TERMINAL.includes(state?.episode?.status)?'本期已结束 · 自动准备下一期':`${phaseText(state)} · 自动继续`:'自动收集 → 编选 → 投票 → 发送数字人短反应 → 聊天与结算 → 下轮／下期';
  $('program-progress').textContent=progress;$('program-progress').hidden=!running;
  const message=safeProgramMessage(programRequestError||program.error||heartbeatError);
  $('program-error').textContent=message;$('program-error').hidden=!message;
  // Preserve the same player across polling and round changes: reassigning src restarts playback.
  const frame=$('avatar-live-frame');let source='';
  $('avatar-preview-image').hidden=!previewDemo;
  if(!previewDemo&&running&&program.player_url){try{const url=new URL(program.player_url);if(['http:','https:'].includes(url.protocol))source=program.player_url;}catch{}}
  if(source!==playerSource){if(source)frame.src=source;else frame.removeAttribute('src');playerSource=source;}
  frame.hidden=!source;$('avatar-guide').classList.toggle('has-live-avatar',Boolean(source));
}
async function operateProgram(action){
  if(programSubmitting||!connected||action==='start'&&programLocked()||action==='stop'&&!programActive())return;
  if(action==='start')setPreviewDemo(false);
  programSubmitting=action;programRequestError='';if(state)renderState(state);
  try{await post(`/api/program/${action}`,{});await refresh();}
  catch(e){programRequestError=safeProgramMessage(e.message);}
  finally{programSubmitting=false;if(state)renderState(state);}
}
async function heartbeatProgram(){
  const runId=state?.program?.run_id;if(heartbeatSending||!programActive()||!runId)return;
  heartbeatSending=true;
  try{await post('/api/program/heartbeat',{run_id:runId});if(heartbeatError){heartbeatError='';if(state)renderProgram();}}
  catch{heartbeatError='联调心跳未确认，页面会继续重试；长时间失联会自动停止云会话。';if(state)renderProgram();}
  finally{heartbeatSending=false;}
}
function renderAutomation(){
  const managed=programLocked(),automatic=Boolean(state?.settings?.auto_advance),paused=Boolean(state?.error);
  const label=managed?'由数字人自动联调管理':paused?'当前步骤因错误暂停':automatic?'本期自动推进已开启':'手动推进';
  $('automation-status').textContent=label;$('automation-status').classList.toggle('running',managed||automatic&&!paused);
  $('automation-note').textContent=managed?'联调会按以下时长自动收集和投票，完成后自动换轮、换期；统一累计最多 20 分钟。停止联调后可修改设置并使用手动文字模式。':[state?.automation?.note,paused?(typeof state.error==='string'?state.error:state.error.message):'', '自动推进会调用真实模型；仅推进本期，终局需手动下一期。单次 20 分钟到限不再启动下一步，正在生成的步骤会完成。'].filter(Boolean).join(' ');
  for(const id of ['auto-advance','collect-seconds','vote-seconds','save-settings'])$(id).disabled=previewDemo||!connected||submitting||managed;
  if(managed){if(manualAutoAdvance===null)manualAutoAdvance=$('auto-advance').checked;$('auto-advance').checked=true;}
  else if(manualAutoAdvance!==null){if(settingsDirty)$('auto-advance').checked=manualAutoAdvance;manualAutoAdvance=null;}
}
function renderCommentControls(){
  const disabled=!connected||commentSending||batchSending;
  $('submit-proposal').disabled=disabled;$('proposal-text').disabled=disabled;
  $('submit-proposal').textContent=commentSending?'正在发送…':'发送评论';
  $('retry-comment').hidden=!pendingComment;$('retry-comment').disabled=disabled;
  $('participant-select').disabled=commentSending||batchSending;
  $('proposal-hint').textContent=!connected?'服务未连接，重连后可发送。':state?.busy?'正在生成：评论会保留，联调时仍可回应；不计入本轮建议或投票。':state?.round?.phase==='COLLECTING'&&!TERMINAL.includes(state?.episode?.status)?'征集阶段：评论全部保留，再识别建议；同一用户的闲聊不会覆盖之前的支招。':state?.round?.phase==='VOTING'&&!TERMINAL.includes(state?.episode?.status)?'投票阶段：完整发送“投1”至“投4”计票，其他评论保留，联调时仍可回应。':'评论会保留；联调中的人物可继续回应，只有开放窗口内的建议和票计入游戏。';
}
function renderVoteOptions(){
  const container=$('vote-options'),options=(state?.round?.options||[]).slice(0,4),voting=connected&&state?.round?.phase==='VOTING'&&!state?.busy&&!submitting&&!commentSending&&!batchSending;
  if(selectedOption&&!options.some(o=>o.id===selectedOption))selectedOption=null;
  if(indexedVoteVersion!==state?.vote_version){
    const update=state?.input_update?.votes;
    if(!update||update.reset)votesByAuthor.clear();
    for(const vote of update?.changed||state?.votes||[])votesByAuthor.set(vote.author_id,vote);
    indexedVoteVersion=state?.vote_version;
  }
  const currentVote=votesByAuthor.get(currentParticipant),myVote=currentVote?.round_id===state?.round?.id?currentVote:null;
  const signature=JSON.stringify([options,voting,selectedOption,myVote?.option_id,currentParticipant]);
  if(signature!==voteSignature){voteSignature=signature;container.replaceChildren();if(!options.length)container.append(el('p','empty-inline','投票开放后，在这里选择建议。'));
    options.forEach((option,index)=>{const current=myVote?.option_id===option.id;const row=el('button',`vote-option ${selectedOption===option.id?'selected':''}`);row.type='button';row.disabled=!voting;row.setAttribute('aria-pressed',String(selectedOption===option.id));const number=el('strong','vote-number',option.number??index+1),copy=el('span','vote-copy'),text=el('span','',option.text),author=el('small','',`来自 ${option.author?.nickname||'参与者'}${current?' · 我的当前票':''}`),count=el('strong','vote-count',`${option.votes||0} 票`);copy.append(text,author);row.append(number,copy,count);row.addEventListener('click',async()=>{selectedOption=option.id;batchFeedback='';renderVoteOptions();await sendNewComment(`投${option.number??index+1}`,'vote');});container.append(row);});
  }
  $('batch-vote').disabled=!voting||!selectedOption;
  const option=options.find(o=>o.id===myVote?.option_id);
  $('vote-feedback').textContent=batchFeedback||(option?`${displayName(participant().id,participant().nickname)} 当前有效票：选项 ${option.number}。再次点击可改票。`:'批量按钮会创建明确标记的模拟用户，每人投一票；当前用户尚无有效票。');
}
function renderProposals(){
  const proposals=state?.proposals||[],version=state?.proposal_version??proposals;
  const signature=`${state?.round?.id||''}:${participantRevision}`;
  if(version===renderedProposalVersion&&signature===proposalsSignature)return;
  renderedProposalVersion=version;proposalsSignature=signature;
  const container=$('proposal-list'),top=container.scrollTop,wanted=new Set(proposals.map(p=>p.id));
  for(const [id,row] of proposalRows)if(!wanted.has(id)){row.node.remove();proposalRows.delete(id);}
  if(proposals.length&&container.querySelector('.empty-inline'))container.replaceChildren();
  for(const proposal of proposals){
    let row=proposalRows.get(proposal.id);
    if(!row){row={node:el('li'),author:el('strong'),text:el('p'),selection:el('div','proposal-selection'),status:el('strong','proposal-status'),reason:el('span','proposal-reason')};row.selection.append(row.status,row.reason);row.node.append(row.author,row.text,row.selection);proposalRows.set(proposal.id,row);container.append(row.node);}
    // The proposal ID is stable when an author changes their suggestion.
    const name=displayName(proposal.author_id,proposal.nickname||proposal.author?.nickname||'参与者');
    if(row.author.textContent!==name)row.author.textContent=name;
    if(row.text.textContent!==proposal.text)row.text.textContent=proposal.text;
    // Each delta replaces the entire proposal, so a revised suggestion cannot inherit an old result.
    const selection=proposal.selection, status=selection?.status;
    const labels={pending:'待编选',selected:'已入选',not_selected:'未入选',duplicate:'未入选',not_submitted:'未入选',unrecorded:'状态未记录'};
    row.status.textContent=labels[status]||'状态未记录';
    row.reason.textContent=selection?.reason||'本轮编选信息未记录。';
    row.selection.className=`proposal-selection ${Object.hasOwn(labels,status)?status:'unrecorded'}`;
  }
  if(!proposals.length&&!container.querySelector('.empty-inline'))container.append(el('li','empty-inline','本轮暂无建议候选；征集阶段的普通评论会进入这里。'));
  $('proposal-count').textContent=`${proposals.length} 条`;container.scrollTop=top;
}
const resultLabels={candidate:'建议候选',voted:'已计票',ignored:'仅记录',stale:'未计入：窗口已结束'};
function makeCommentRow(comment){
  const row=el('li','comment-row'),head=el('div','comment-meta'),name=el('strong','',displayName(comment.author_id,comment.nickname||'模拟用户')),time=el('time');
  const received=new Date(comment.received_at);time.textContent=Number.isNaN(received.getTime())?'时间未知':received.toLocaleTimeString('zh-CN',{hour12:false});time.dateTime=comment.received_at||'';time.title=Number.isNaN(received.getTime())?'':received.toLocaleString('zh-CN',{hour12:false});head.append(name,time);
  const context=el('span','comment-context',`${comment.round_number!=null?'第 '+comment.round_number+' 轮 · ':''}${PHASES[comment.phase]||'等待开场'}`),body=el('p','comment-text',comment.text),receipt=el('div',`comment-result ${comment.result||'ignored'}`);
  receipt.append(el('strong','',resultLabels[comment.result]||'已记录'),el('span','',comment.reason||''));row.append(head,context,body,receipt);
  return {node:row,name,comment};
}
function renderComments(){
  const rounds=state?.comment_rounds||[],select=$('comment-filter'),filterSignature=JSON.stringify(rounds);
  if(filterSignature!==roundFilterSignature){roundFilterSignature=filterSignature;const value=select.value;select.replaceChildren();for(const [id,label] of [['current','本轮'],['all','全场'],...rounds.map(r=>[r.id,`${r.title} · 第 ${r.number} 轮`])]){const option=el('option','',label);option.value=id;select.append(option);}select.value=[...select.options].some(o=>o.value===value)?value:'current';}
  const filter=select.value,roundId=filter==='current'?state?.round?.id:filter;
  const signature=`${state?.session?.id||''}:${filter}:${roundId||''}`,update=state?.comment_update;
  const rebuild=signature!==commentsSignature||Boolean(update?.reset&&update.revision!==renderedCommentRevision);
  const changed=update?update.revision!==renderedCommentRevision:renderedCommentRevision!==(state?.comments?.length||0);
  const namesChanged=renderedNameRevision!==participantRevision;
  if(!rebuild&&!changed&&!namesChanged)return;
  const container=$('comment-list'),top=container.scrollTop,follow=container.scrollHeight-container.scrollTop-container.clientHeight<70;
  if(rebuild){container.replaceChildren();commentRows.clear();}
  const matches=c=>filter==='all'||(c.round_id??null)===(roundId??null);
  const incoming=rebuild?(state?.comments||[]):changed?(state?.comments||[]).slice(renderedCommentCursor):[];
  for(const comment of incoming)if(matches(comment)&&!commentRows.has(comment.id)){
    if(!commentRows.size)container.replaceChildren();
    const row=makeCommentRow(comment);commentRows.set(comment.id,row);container.append(row.node);
  }
  if(namesChanged)for(const row of commentRows.values())row.name.textContent=displayName(row.comment.author_id,row.comment.nickname||'模拟用户');
  $('comment-count').textContent=`${commentRows.size} 条`;
  if(!commentRows.size&&!container.querySelector('.empty-inline'))container.append(el('li','empty-inline',filter==='all'?'本场还没有评论。发送后会保留每条原文与处理结果。':'这一轮还没有评论；可切换“全场”回看其他轮。'));
  container.scrollTop=follow?container.scrollHeight:top;
  renderedCommentCursor=state?.comments?.length||0;commentsSignature=signature;renderedCommentRevision=update?.revision??(state?.comments?.length||0);renderedNameRevision=participantRevision;
}
let performanceSignature='';
function renderPerformance(){
  const events=state?.performance_events||[],signature=JSON.stringify(events);
  if(signature===performanceSignature)return;performanceSignature=signature;
  let details=$('performance-details');
  if(!details){details=el('details','layout-settings');details.id='performance-details';details.append(el('summary','','最近本地性能记录'));const output=el('pre','quiet-note');output.id='performance-events';Object.assign(output.style,{whiteSpace:'pre-wrap',overflowWrap:'anywhere',maxHeight:'240px',overflow:'auto'});details.append(output);$('performance-host').append(details);}
  // Metrics are server-whitelisted durations and IDs, never model input/output.
  $('performance-events').textContent=events.length?events.slice(-12).map(event=>Object.entries(event).map(([key,value])=>`${key}: ${value}`).join(' · ')).join('\n'):'尚无性能记录。';
}
function commentBody(text,p=participant(),window={roundId:state?.round?.id??null,phase:state?.round?.phase??null}){return {event_id:crypto.randomUUID(),author_id:p.id,nickname:p.nickname,text,expected_round_id:window.roundId,expected_phase:window.phase};}
async function postComment(body){
  // A timed-out request may already be saved. Retrying keeps its original event ID and window.
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),20000);
  try {const response=await fetch('/api/comments',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal:controller.signal});const data=await response.json().catch(()=>({error:`服务返回异常（${response.status}）`}));if(!response.ok||data.error)throw new Error(data.error||'评论未完成，请重试');return data.comment;}
  catch(e){if(e.name==='AbortError')throw new Error('等待回执超时，评论可能已收到；请重试原评论确认结果。');throw e;}
  finally{clearTimeout(timer);}
}
function showReceipt(comment){$('comment-feedback').classList.remove('failed');$('comment-feedback').textContent=`${displayName(comment.author_id,comment.nickname)} · ${resultLabels[comment.result]||'已记录'}：${comment.reason}`;}
async function deliverComment(pending){
  if(commentSending||batchSending)return null;commentSending=true;savePending(pending);renderCommentControls();renderVoteOptions();
  try {const comment=await postComment(pending.body);savePending(null);showReceipt(comment);if(pending.source==='batch'&&pending.batchCounts){const {counted,notCounted,unsent}=pending.batchCounts;batchFeedback=`本批次 ${counted+(comment.result==='voted'?1:0)} 票已计入，${notCounted+(comment.result==='voted'?0:1)} 条未计入；${unsent} 条未发送。已确认重试结果，未转投新轮。`;}if(pending.source==='form'&&$('proposal-text').value===pending.body.text)$('proposal-text').value='';await refresh();return comment;}
  catch(e){$('comment-feedback').classList.add('failed');$('comment-feedback').textContent=`评论尚未确认：${e.message} 点“重试上一条评论”会使用同一条记录；编辑内容后发送会创建新评论。`;return null;}
  finally{commentSending=false;renderCommentControls();renderVoteOptions();}
}
async function sendNewComment(text,source='form'){
  if(!connected||commentSending||batchSending)return null;
  const p=participant();const old=pendingComment;
  // An unchanged retry must keep the original phase even after polling moves to the next round.
  const pending=old&&old.source===source&&old.body.author_id===p.id&&old.body.text===text?old:{body:commentBody(text,p),source};
  return deliverComment(pending);
}
async function refresh(){if(polling)return;polling=true;try{const next=await getState();connected=true;$('connection-dot').classList.add('online');$('connection-text').textContent='本机服务已连接';renderState(next);}catch(e){connected=false;$('connection-dot').classList.remove('online');$('connection-text').textContent='本机服务未连接';$('primary-action').disabled=true;$('abort-episode').disabled=true;$('new-session').disabled=true;renderProgram();renderAutomation();renderCommentControls();renderVoteOptions();status('暂时无法读取状态，页面会自动重连。');}finally{polling=false;if(connected&&state?.has_more)queueMicrotask(refresh);}}
async function operate(path,body={},success='操作已提交。'){if(previewDemo){status('当前为排版示例，请先切回实时画面再操作游戏。');return false;}if(submitting||programLocked())return false;submitting=true;httpError='';error('');if(state)renderState(state);try{await post(path,body);status(success);await refresh();return true;}catch(e){httpError=e.message;error(httpError);status('操作未完成；检查原因后可重新操作。');return false;}finally{submitting=false;if(state)renderState(state);}}
$('program-start').addEventListener('click',()=>operateProgram('start'));
$('program-stop').addEventListener('click',()=>operateProgram('stop'));
$('primary-action').addEventListener('click',()=>{const action=actionFor(state);if(action.path)operate(action.path,{},'已提交，正在读取最新进度。');});
$('retry-action').addEventListener('click',()=>operate('/api/retry',{},'已重试当前失败步骤。'));
$('proposal-form').addEventListener('submit',async event=>{event.preventDefault();const text=$('proposal-text').value;if(!text.trim()){$('proposal-text').focus();return;}await sendNewComment(text);});
$('retry-comment').addEventListener('click',()=>{if(pendingComment&&connected)deliverComment(pendingComment);});
$('proposal-text').addEventListener('input',()=>{if(pendingComment?.source==='form'&&$('proposal-text').value!==pendingComment.body.text){savePending(null);$('comment-feedback').classList.remove('failed');$('comment-feedback').textContent='内容已编辑，下次发送将是一条新评论；上一条可在评论流中核对。';renderCommentControls();}});
$('comment-filter').addEventListener('change',()=>{commentsSignature='';renderComments();});
$('participant-select').addEventListener('change',event=>{currentParticipant=event.target.value;batchFeedback='';renderVoteOptions();});
$('add-participant').addEventListener('click',()=>{$('participant-form').hidden=false;$('participant-name').focus();});$('cancel-participant').addEventListener('click',()=>{$('participant-form').hidden=true;});
$('participant-form').addEventListener('submit',event=>{event.preventDefault();const name=$('participant-name').value.trim();if(!name)return;const p={id:`manual-${crypto.randomUUID()}`,nickname:`${name}（模拟）`};participants.push(p);currentParticipant=p.id;storeParticipants();renderParticipants();$('participant-name').value='';$('participant-form').hidden=true;batchFeedback='';renderVoteOptions();if(state)renderComments();});
$('batch-vote').addEventListener('click',async()=>{
  if(!selectedOption||submitting||commentSending||batchSending||!connected)return;
  const option=(state?.round?.options||[]).find(o=>o.id===selectedOption);if(!option||state?.round?.phase!=='VOTING')return;
  const amount=Math.min(50,Math.max(1,Math.floor(Number($('batch-count').value)||1))),window={roundId:state.round.id,phase:state.round.phase};
  batchSending=true;batchFeedback='正在发送批量模拟票…';renderCommentControls();renderVoteOptions();let counted=0,notCounted=0,confirmed=0,uncertain=false;
  try {for(let i=0;i<amount;i++){
    const p={id:`batch-${crypto.randomUUID()}`,nickname:`模拟票-${participants.filter(p=>p.id.startsWith('batch-')).length+1}`};participants.push(p);storeParticipants();
    const pending={body:commentBody(`投${option.number}`,p,window),source:'batch'};savePending(pending);
    try {const comment=await postComment(pending.body);savePending(null);confirmed++;if(comment.result==='voted')counted++;else notCounted++;showReceipt(comment);}
    catch(e){uncertain=true;pending.batchCounts={counted,notCounted,unsent:amount-confirmed-1};savePending(pending);$('comment-feedback').classList.add('failed');$('comment-feedback').textContent=`批量发送中断：${e.message} 可重试上一条，轮号和阶段保持原值。`;break;}
  }}finally{
    batchFeedback=`本批次 ${counted} 票已计入，${notCounted} 条未计入${uncertain?`；1 条待确认，${amount-confirmed-1} 条未发送`:''}。发送窗口固定为第 ${state?.comment_rounds?.find(r=>r.id===window.roundId)?.number??'原'} 轮，不转投新轮。`;
    renderParticipants();batchSending=false;renderCommentControls();await refresh();renderVoteOptions();
  }
});
$('settings-form').addEventListener('input',()=>{settingsDirty=true;});
$('settings-form').addEventListener('submit',async event=>{event.preventDefault();if(await operate('/api/settings',{auto_advance:$('auto-advance').checked,collect_seconds:Number($('collect-seconds').value),vote_seconds:Number($('vote-seconds').value)},'本期推进设置已保存。')){settingsDirty=false;if(state)renderState(state);}});
$('abort-episode').addEventListener('click',()=>{if(!programLocked()&&confirm('技术结束本期会停止当前推进，保留已发生的聊天；未完成回合不计入正常成绩。确定结束吗？'))operate('/api/episode/abort',{},'本期已技术结束，实际记录已保留。');});
$('new-session').addEventListener('click',()=>{if(!programLocked()&&confirm('新开一场会重置本场两榜，并结束当前故事。确定新开一场吗？'))operate('/api/session/new',{},'已新开一场，本场两榜从零累计。');});
const layoutFields={'safe-top':'top','safe-right':'right','safe-bottom':'bottom','safe-left':'left','avatar-x':'avatarX','avatar-y':'avatarY','avatar-width':'avatarWidth'};
function renderLayout(value,force=false){$('avatar-y').min=String(value.top);for(const [id,key]of Object.entries(layoutFields))if(force||document.activeElement!==$(id))$(id).value=Math.round(value[key]);const {safe,avatar}=layoutRects(value);for(const [id,rect]of [['safe-guide',safe],['avatar-guide',avatar],['avatar-live-frame',avatar],['avatar-preview-image',avatar]])Object.assign($(id).style,{left:`${rect.x/10.8}%`,top:`${rect.y/19.2}%`,width:`${rect.w/10.8}%`,height:`${rect.h/19.2}%`});$('layout-note').textContent=`数字人 ${Math.round(avatar.w)} × ${Math.round(avatar.h)}（3:4）；左侧手机含聊天与亲密度，右侧为数字人及其下方双榜；下方统一两列两行候选，每条摘要最多两行，主内容底边不超过 1640。人物允许与右上活动卡重叠；原建议全文保留，输入框显示收回后的有效值。`;}
for(const id of Object.keys(layoutFields))$(id).addEventListener('change',()=>{const raw={};for(const [input,key]of Object.entries(layoutFields))raw[key]=Number($(input).value);persistLayout(raw);});
async function persistLayout(raw){const normalized=saveLayout(raw);renderLayout(normalized,true);layoutSaving=true;httpError='';error('');try{const response=await post('/api/layout',normalized);const actual=saveLayout(response.layout||normalized);serverLayoutSignature=JSON.stringify(actual);renderLayout(actual,true);status('直播叠层排版已保存，独立浏览器源会自动读取。');}catch(e){httpError=`排版未保存到服务：${e.message}`;error(httpError);serverLayoutSignature='';}finally{layoutSaving=false;await refresh();}}
$('reset-layout').addEventListener('click',()=>persistLayout(DEFAULT_LAYOUT));$('show-guides').addEventListener('change',()=>{$('preview-guides').hidden=!$('show-guides').checked;});$('show-occlusions').addEventListener('change',()=>{$('occlusions').hidden=!$('show-occlusions').checked;});
if(pendingComment){if(participants.some(p=>p.id===pendingComment.body.author_id))currentParticipant=pendingComment.body.author_id;if(pendingComment.source==='form')$('proposal-text').value=pendingComment.body.text;$('comment-feedback').textContent='上一条评论尚未确认，可点“重试上一条评论”；重试沿用原来的轮号与阶段。';}
$('show-preview-demo').addEventListener('change',()=>{setPreviewDemo($('show-preview-demo').checked);if(state)renderState(state);});
setPreviewDemo(previewDemo);
renderParticipants();renderCommentControls();renderLayout(loadLayout());observeLayout(renderLayout);storeParticipants();refresh();setInterval(refresh,900);setInterval(heartbeatProgram,5000);

// The screenshot overlay is a visual reference only; it never enters the public game output.
$('show-douyin-reference').addEventListener('change',()=>{$('douyin-reference').hidden=!$('show-douyin-reference').checked;});
