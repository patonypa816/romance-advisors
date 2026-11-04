// All model- and participant-supplied strings enter through textContent, never HTML.
export function el(tag, className = '', text = '') {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}
export const PHASES = {COLLECTING:'征集建议中',SELECTING:'正在编选建议',VOTING:'观众投票中',DECIDING:'男主正在决定',DRAFTING:'男主正在组织语言',WAITING_REPLY:'等待她的回复',DONE:'本轮已完成',ABORTED:'本轮技术结束'};
export const TERMINAL = ['SUCCEEDED','FAILED','TECH_ENDED'];
export function phaseText(state) {
  if (state?.error) return '技术候场';
  if (!state?.episode) return '等待开场';
  if (state.episode.status === 'SUCCEEDED') return '本期牵手成功';
  if (state.episode.status === 'FAILED') return '本期遗憾收场';
  if (state.episode.status === 'TECH_ENDED') return '本期技术结束';
  if (state.episode.status === 'PREPARING') return '新故事准备中';
  return PHASES[state.round?.phase] || '等待下一轮';
}
export function makeMessage(message, state, {animate = false} = {}) {
  const row = el('div', `chat-message ${message.role === 'man' ? 'man' : 'woman'}`);
  row.dataset.messageId = String(message.id);
  const name = message.role === 'man' ? state.episode?.man_name || '男主' : state.episode?.woman_name || '女生';
  row.append(el('span','message-name',name));
  const bubble = el('div','message-bubble',message.text);
  row.append(bubble);
  if (animate && message.role === 'man' && !matchMedia('(prefers-reduced-motion: reduce)').matches) {
    const full = String(message.text || '');
    // The complete saved message is authoritative; this only reveals the same string.
    bubble.setAttribute('aria-label', full);
    bubble.textContent = ''; 
    const started = performance.now();
    const tick = now => {
      if (!bubble.isConnected) return;
      const count = Math.max(1,Math.floor((now-started)/28));
      bubble.textContent = full.slice(0,count);
      const liveChat = bubble.closest('.live-chat');
      if (liveChat) liveChat.scrollTop = liveChat.scrollHeight;
      if (count < full.length) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }
  return row;
}
export function appendMessages(container, messages, state, {animate = false, follow = true} = {}) {
  const existing = new Set([...container.querySelectorAll('[data-message-id]')].map(n=>n.dataset.messageId));
  const wanted = new Set(messages.map(m=>String(m.id)));
  // A new episode replaces its history, while ordinary polling preserves scroll and typing.
  if ([...existing].some(id=>!wanted.has(id))) { container.replaceChildren(); existing.clear(); }
  if (messages.length && container.querySelector('.empty-inline')) container.replaceChildren();
  let added = false;
  for (const message of messages) if (!existing.has(String(message.id))) { container.append(makeMessage(message,state,{animate:typeof animate==='function'?animate(message):animate})); added=true; }
  if (added && follow) container.scrollTop = container.scrollHeight;
  return added;
}
// Keep committed input history locally. Cursor and versions advance only after
// a response arrives, so dropped polls can safely retry the same request.
export function createOperatorCache() {
  let previous=null, revision=0;
  const positions={proposals:new Map(),votes:new Map()};
  return {
    query() {
      const query=new URLSearchParams({comment_limit:'500'});
      if(previous)for(const key of ['session_id','comment_cursor','proposal_version','vote_version'])if(previous[key]!=null)query.set(key,String(previous[key]));
      return query.toString();
    },
    merge(data) {
      const scopeChanged=!previous||previous.session_id!==data.session_id;
      const reset=scopeChanged||data.reset;
      const comments=reset?[]:previous.comments;
      const added=data.comments||[];
      comments.push(...added);
      if(reset||added.length)revision++;
      const inputs={},input_update={};
      for(const [table,flag] of [['proposals','proposal_reset'],['votes','vote_reset']]){
        // A comment cursor reset is independent of candidate/vote versions.
        // Only the explicit table reset replaces these committed rows.
        const full=scopeChanged||Boolean(data[flag]),rows=full?[]:previous[table],index=positions[table];
        if(full)index.clear();
        for(const item of data[table]||[]){
          if(index.has(item.id))rows[index.get(item.id)]=item;
          else{index.set(item.id,rows.length);rows.push(item);}
        }
        inputs[table]=rows;input_update[table]={reset:full,changed:data[table]||[]};
      }
      const next={...data,...inputs,comments,input_update,comment_update:{revision,reset,added}};
      previous=next;
      return next;
    }
  };
}
const operatorCache=createOperatorCache();
export async function getState(publicOnly = false) {
  const response = await fetch(publicOnly ? '/api/public' : '/api/operator-state?'+operatorCache.query(),{cache:'no-store'});
  if (!response.ok) throw new Error(`连接失败（${response.status}）`);
  const data = await response.json();
  if (data.error && !('episode' in data)) throw new Error(data.error);
  return publicOnly?data:operatorCache.merge(data);
}
export async function post(path, body={}) {
  const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const data=await response.json().catch(()=>({error:`服务返回异常（${response.status}）`}));
  if (!response.ok || data.error) throw new Error(data.error || '操作未完成，请重试');
  return data;
}
