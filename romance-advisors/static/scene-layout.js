import {layoutRects} from './layout.js';
// Shared by the responsive operator preview and the fixed 1080×1920 output.
export const PHONE_CONTENT_SCALE = 0.818662819;
export function shotForState(state, {demo=false}={}) {
  if(demo||state?.appearance?.mode==='rehearsal')return 'game';
  const program=state?.program,episode=state?.episode;
  // Older services and isolated design fixtures retain their original layout.
  if(!program || !Object.hasOwn(program,'content_mode'))return 'game';
  if(program.content_mode==='to_chat')return 'game';
  if(program.state==='starting'||program.content_mode==='to_game')return 'portrait';
  if(['running','stopping'].includes(program.state)){
    return program.content_mode==='game' && Boolean(episode?.id)
      && program.game_episode_id===episode.id ? 'game' : 'portrait';
  }
  return episode?.status==='ACTIVE' ? 'game' : 'portrait';
}
export function portraitRect(layout,shot) {
  if(shot!=='game')return {x:-180,y:0,w:1440,h:1920};
  const {avatar}=layoutRects(layout),scale=PHONE_CONTENT_SCALE;
  return {x:540+(avatar.x-540)*scale,y:avatar.y*scale,w:avatar.w*scale,h:avatar.h*scale};
}
export function applyPortraitLayout(node,layout,shot,{initial=false}={}) {
  const rect=portraitRect(layout,shot);
  const first=initial||node.dataset.placed!=='true';
  // Percentages refer to the carrier itself (1080×1440 at reference size),
  // so both a 390px preview and the OBS output follow identical geometry.
  if(first)node.style.transition='none';
  const transform=`translate(${rect.x/1080*100}%, ${rect.y/1440*100}%) scale(${rect.w/1080})`;
  if(node.style.transform!==transform)node.style.transform=transform;
  node.dataset.shot=shot;
  if(first){node.getBoundingClientRect();node.style.transition='';node.dataset.placed='true';}
  return rect;
}
