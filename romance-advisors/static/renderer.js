import {el,phaseText,appendMessages} from './ui.js';
import {layoutRects} from './layout.js';
import {createAffinityPanel} from './affinity-panel.js';
import {createFinaleOverlay} from './finale-overlay.js';
// Match the approved preview: one fill color, interpolated in OKLab by score.
function affinityLab(rgb) {
  const [r,g,b]=rgb.map(value=>{
    const v=value/255;
    return v<=.04045?v/12.92:((v+.055)/1.055)**2.4;
  });
  const l=Math.cbrt(.4122214708*r+.5363325363*g+.0514459929*b);
  const m=Math.cbrt(.2119034982*r+.6806995451*g+.1073969566*b);
  const s=Math.cbrt(.0883024619*r+.2817188376*g+.6299787005*b);
  return [.2104542553*l+.793617785*m-.0040720468*s,
    1.9779984951*l-2.428592205*m+.4505937099*s,
    .0259040371*l+.7827717662*m-.808675766*s];
}
const affinityStops=[
  [120,135,159], // 0: cool blue-gray
  [181,172,213], // 25: soft purple
  [232,238,122], // 50: lemon
  [255,179,132], // 75: warm apricot
  [255,111,120], // 100: coral
].map(affinityLab);
function affinityColor(value) {
  const score=Math.max(0,Math.min(100,Number(value)||0));
  const index=Math.min(3,Math.floor(score/25)),t=(score-index*25)/25;
  const [L,a,b]=affinityStops[index].map((v,i)=>v+(affinityStops[index+1][i]-v)*t);
  const l=(L+.3963377774*a+.2158037573*b)**3;
  const m=(L-.1055613458*a-.0638541728*b)**3;
  const s=(L-.0894841775*a-1.291485548*b)**3;
  const rgb=[4.0767416621*l-3.3077115913*m+.2309699292*s,
    -1.2684380046*l+2.6097574011*m-.3413193965*s,
    -.0041960863*l-.7034186147*m+1.707614701*s].map(v=>
    Math.round(255*Math.max(0,Math.min(1,v<=.0031308?12.92*v:1.055*(Math.max(0,v)**(1/2.4))-.055))));
  return `rgb(${rgb.join(',')})`;
}
export function createLiveRenderer(root,{onFinaleVisibility=()=>{}}={}) {
  root.replaceChildren();
  const phase=el('span','live-phase','等待开场');
  const conversation=el('section','live-conversation');conversation.setAttribute('aria-label','两人的聊天');
  const speaker=el('div','phone-speaker');speaker.setAttribute('aria-hidden','true');
  const topLine=el('div','phone-topline');topLine.append(speaker,phase);
  const chatTitle=el('div','live-chat-title'),pair=el('span','','等待第一条来信'),round=el('span','','');chatTitle.append(pair,round);
  const affinity=createAffinityPanel(affinityColor);
  const chat=el('div','live-chat');chat.setAttribute('aria-live','polite');
  const home=el('div','phone-home');home.setAttribute('aria-hidden','true');
  conversation.append(topLine,chatTitle,affinity.element,chat,home);
  const poll=el('section','live-poll'), pollLabel=el('div','live-poll-title'), pollHeading=el('span','','下一句，你来支招'), pollStatus=el('span','','');pollLabel.append(pollHeading,pollStatus);
  const options=el('div','live-options');poll.append(pollLabel,options);
  const boards=el('section','live-boards');root.append(conversation,poll,boards);
  const finale=createFinaleOverlay({onVisibility:onFinaleVisibility});root.append(finale.element);
  let first=true,optionsSignature='',boardsSignature='',lastDisplayMode=null;
  const seenMessages=new Set();
  const reducedMotion=matchMedia('(prefers-reduced-motion: reduce)');
  let contextScrollFrame=null,contextScrollKey=null,followFrame=null,disposed=false;
  function stopContextScroll(){if(contextScrollFrame!==null)cancelAnimationFrame(contextScrollFrame);contextScrollFrame=null;contextScrollKey=null;}
  function scrollVotingContext(key,isVoting,contextId){
    const node=[...chat.querySelectorAll('[data-message-id]')].find(item=>item.dataset.messageId===String(contextId));
    // The phone retains both sides of the conversation. Only a latest reply
    // taller than its viewport loops; older history is never endlessly replayed.
    const max=Math.max(0,chat.scrollHeight-chat.clientHeight);
    // Retain the screen padding at each endpoint so the rounded viewport
    // does not shave the speaker name or the last line while paused.
    const start=node?Math.max(0,Math.min(node.offsetTop-16,max)):0;
    const end=node?Math.min(node.offsetTop+node.offsetHeight+16-chat.clientHeight,max):0;
    key+=`:${chat.clientWidth}:${chat.clientHeight}`;
    if(!isVoting||!node||end-start<=1){stopContextScroll();return;}
    if(contextScrollKey===key&&contextScrollFrame!==null)return;
    stopContextScroll();contextScrollKey=key;chat.scrollTop=start;
    let position=start,direction=1,previous=performance.now(),holdUntil=previous+3000;
    const tick=now=>{
      if(!root.isConnected||!root.classList.contains('is-voting')){stopContextScroll();return;}
      if(now>=holdUntil){
        if(reducedMotion.matches){position+=direction*46;holdUntil=now+4000;}
        else position+=direction*20*Math.min(now-previous,80)/1000;
        position=Math.max(start,Math.min(end,position));chat.scrollTop=position;
        if(position===end||position===start){direction=position===end?-1:1;holdUntil=now+3000;}
      }
      previous=now;contextScrollFrame=requestAnimationFrame(tick);
    };
    contextScrollFrame=requestAnimationFrame(tick);
  }
  return {
    setLayout(layout) {
      const {phone,poll:pollRect,boards:boardsRect}=layoutRects(layout);
      // The phone and avatar share the upper row. The two-column vote grid
      // keeps its own lower region, so four choices never shrink the chat.
      for(const [node,rect] of [[conversation,phone],[poll,pollRect],[boards,boardsRect]]){
        Object.assign(node.style,{left:`${rect.x}px`,top:`${rect.y}px`,width:`${rect.w}px`,height:`${rect.h}px`});
      }
    },
    render(state) {
      if(disposed)return;
      const episode=state.episode, current=state.round;
      const isVoting=current?.phase==='VOTING';root.classList.toggle('is-voting',isVoting);
      phase.textContent=phaseText(state);
      affinity.update(state);
      finale.update(state);
      pair.textContent=episode?`${episode.man_name} × ${episode.woman_name}`:'两人的聊天';round.textContent=current?`第 ${current.number} 轮`:'';
      const messages=state.messages || [];
      const followLatest=first||lastDisplayMode!==isVoting||messages.some(message=>!seenMessages.has(String(message.id)));
      lastDisplayMode=isVoting;
      const context=[...messages].reverse().find(message=>message.role==='woman');
      if (!messages.length) {chat.replaceChildren(el('p','live-empty',episode?'等她发来第一条消息…':'从一条消息，开始这场心动。'));}
      else {if(chat.querySelector('.live-empty'))chat.replaceChildren();appendMessages(chat,messages,state,{animate:message=>!first&&!seenMessages.has(String(message.id)),follow:true});}
      // The phone shows only the two characters' sent chat messages. The man's
      // spoken reaction belongs to the avatar performance, not this chat log.
      messages.forEach(message=>seenMessages.add(String(message.id)));
      const allOptions=(current?.options||[]).slice(0,4);
      const selected=allOptions.find(option=>option.id===current?.selected_option_id);
      const opts=isVoting?allOptions:selected?[selected]:[];
      pollStatus.textContent=isVoting?`投 1—${Math.max(1,allOptions.length)}${current.remaining_seconds!=null?' · '+Math.ceil(current.remaining_seconds)+'秒':''}`:selected?'':'本轮建议';
      pollHeading.textContent=isVoting?'下一句怎么回':selected?'本轮采用':current?.phase==='COLLECTING'?'军师团支招':current?.selection_source==='self'?'男主自拟':['DECIDING','DRAFTING'].includes(current?.phase)?'正在想怎么回':'等待支招';
      const sig=JSON.stringify([opts,current?.selected_option_id,isVoting,current?.phase]);
      if(sig!==optionsSignature) {optionsSignature=sig;options.replaceChildren();
        if(!opts.length) options.append(el('p','live-poll-empty',current?.phase==='COLLECTING'?'把你的回复建议发在弹幕里':current?.selection_source==='self'?'本轮由男主自拟回复':['DECIDING','DRAFTING','SELECTING'].includes(current?.phase)?'结果确定后会显示在这里。':'建议征集后，这里会出现真实候选。'));
        opts.forEach((option,index)=>{
          const row=el('div',`live-option ${current.selected_option_id===option.id?'chosen':''}`);
          const line=el('div','option-main'),num=el('strong','option-number',option.number ?? index+1);
          const text=el('span','option-text',option.text),votes=el('strong','option-votes',`${option.votes||0}票`);
          const author=el('span','option-author',`来自 ${option.author?.nickname||'参与者'}`);
          // The concise label is for the live screen; full source/attribution
          // remains in the game and operator view. Credit appears once adopted.
          votes.title=`${option.votes||0}票`;text.title=String(option.text||'');author.title=String(option.author?.nickname||'参与者');
          row.title=`${option.author?.nickname||'参与者'}：${option.text}`;
          line.append(num,text,votes);row.append(line,author);options.append(row);
        });
      }
      const boardSig=JSON.stringify(state.leaderboards);if(boardSig!==boardsSignature){boardsSignature=boardSig;boards.replaceChildren();
        [['best','最强军师'],['worst','狗头军师']].forEach(([key,label])=>{const board=el('div',`live-board ${key}`),heading=el('h2','',label);board.append(heading);const rows=(state.leaderboards?.[key]||[]).slice(0,1);if(!rows.length) board.append(el('p','board-empty','本场暂无采纳'));rows.forEach((entry,i)=>{const row=el('div','board-row'),name=el('span','board-name',entry.nickname),numeric=Number(entry.rate)||0,rate=el('strong','',`${Math.round(numeric<=1?numeric*100:numeric)}%`),times=el('small','',`采纳 ${entry.adopted} 次`);name.title=String(entry.nickname||'');times.title=`采纳 ${entry.adopted} 次`;row.append(name,rate,times);board.append(row);});boards.append(board);});
      }
      // Follow after layout/typing changes. If the latest reply is unusually
      // long, its own reading window takes over while votes are being collected.
      if(followLatest){
        if(followFrame!==null)cancelAnimationFrame(followFrame);
        followFrame=requestAnimationFrame(()=>{
          followFrame=null;if(disposed)return;
          chat.scrollTop=chat.scrollHeight;
          scrollVotingContext(`${episode?.id}:${context?.id}`,isVoting,context?.id);
        });
      }
      else scrollVotingContext(`${episode?.id}:${context?.id}`,isVoting,context?.id);
      first=false;
    },
    suspend(){finale.suspend();},
    resume(){finale.resume();},
    dispose(){finale.dispose();disposed=true;if(followFrame!==null)cancelAnimationFrame(followFrame);followFrame=null;stopContextScroll();affinity.dispose();},
    offline(){phase.textContent='游戏状态读取失败';},
    renderFailed(){phase.textContent='画面更新失败';}
  };
}
