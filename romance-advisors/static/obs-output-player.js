// This page is a receiver only. The server decides whether the existing game
// player may be attached; this module never starts or keeps a cloud run alive.
export function authorizedPlayerSource(state,base) {
  if(state.obs_output!==true||typeof state.player_url!=='string'||!state.player_url)return '';
  try {
    const url=new URL(state.player_url,base);
    return url.protocol==='http:'&&url.hostname==='127.0.0.1'&&url.port&&url.pathname.startsWith('/player/')&&url.pathname.endsWith('/')&&!url.username&&!url.password?url.href:'';
  } catch {return '';}
}
export function createPlayerMount({frame,still,label,placeholder,onStatus=()=>{}}) {
  let source='',frameFailed=false,mode='waiting';
  function paint(){
    const live=Boolean(source)&&!frameFailed;
    frame.hidden=!live;
    still.hidden=mode!=='still';label.hidden=mode!=='still';
    placeholder.hidden=live||mode==='still';
  }
  frame.addEventListener('load',()=>{if(source&&!frameFailed)onStatus('frame-loaded');});
  frame.addEventListener('error',()=>{if(source){frameFailed=true;mode='unavailable';paint();onStatus('frame-error');}});
  still.addEventListener('error',()=>{if(mode==='still'){mode='unavailable';paint();onStatus('still-error');}});
  return {
    update(state,base){
      const next=authorizedPlayerSource(state,base);
      // Reassigning src would reconnect RTC on every public-state refresh.
      if(next!==source){
        source=next;frameFailed=false;
        if(source)frame.src=source;else frame.removeAttribute('src');
      }
      const programState=state.program?.state||'off';
      const preparing=state.obs_output===true&&['starting','running','stopping'].includes(programState);
      const failed=state.obs_output===true&&programState==='error';
      mode=source?(frameFailed?'unavailable':'live'):failed?'unavailable':preparing?'waiting':'still';
      paint();onStatus(source?(frameFailed?'frame-error':'frame-mounted'):mode);
    },
    disconnected(){onStatus('state-unavailable');},
  };
}
