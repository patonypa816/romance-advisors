import {el} from './ui.js';

// Presentation only. Scores and settled round deltas remain owned by the game.
export function createAffinityPanel(colorForScore) {
  const card=el('div','affinity-card');
  const main=el('div','affinity-main'),heading=el('div','affinity-heading');
  const label=el('span','affinity-label','亲密度'),change=el('span','affinity-change');
  change.setAttribute('aria-hidden','true');heading.append(label,change);
  const track=el('div','affinity-track'),fill=el('i','affinity-fill');track.append(fill);
  track.setAttribute('role','progressbar');track.setAttribute('aria-label','亲密度');
  track.setAttribute('aria-valuemin','0');track.setAttribute('aria-valuemax','100');
  main.append(heading,track);
  const number=el('strong','affinity-number','—');number.setAttribute('aria-hidden','true');
  const announce=el('span','affinity-sr-only');
  announce.setAttribute('role','status');announce.setAttribute('aria-live','polite');announce.setAttribute('aria-atomic','true');
  card.append(main,number,announce);
  const media=matchMedia('(prefers-reduced-motion: reduce)');
  let previous=null,contextKey=null,roundKey=null,hasEpisode=false,disposed=false;
  let frame=null,hideTimer=null,shown=0,target=0,animations=[];
  const completedRounds=new Set();
  // Suppress binary subtraction noise in display only, never mutate game state.
  const display=value=>typeof value==='number'?String(Number(value.toPrecision(12))):String(value);
  function showNumber(value){
    const text=display(value);number.textContent=text;number.title=String(value);
    // Keep the approved 52px integers; legal fractional scores fit the same slot.
    number.style.fontSize=`${Math.min(52,156/text.length)}px`;
  }
  function stopNumber(){if(frame!==null)cancelAnimationFrame(frame);frame=null;}
  function clearFeedback(){
    clearTimeout(hideTimer);hideTimer=null;
    animations.forEach(animation=>animation.cancel());animations=[];
    change.textContent='';announce.textContent='';card.classList.remove('gain','loss');
  }
  function settleMotion(){
    stopNumber();shown=target;showNumber(hasEpisode?target:'—');
    animations.forEach(animation=>animation.cancel());animations=[];
    card.classList.toggle('is-reduced',media.matches);
  }
  media.addEventListener('change',settleMotion);
  function update(state){
    if(disposed)return;
    const episode=state.episode,current=state.round;
    const score=episode?.score??50;
    const next=episode?Math.max(0,Math.min(100,typeof score==='number'&&Number.isFinite(score)?score:50)):0;
    const context=JSON.stringify([state.session?.id??null,episode?.id??null,!!episode]);
    const reset=previous===null||context!==contextKey;
    const currentRound=current?.id??null;
    const settled=currentRound!==null&&current?.phase==='DONE'&&typeof current.delta==='number'&&Number.isFinite(current.delta);
    hasEpisode=!!episode;target=next;
    if(reset){completedRounds.clear();clearFeedback();}
    else if(currentRound!==roundKey)clearFeedback();
    const freshSettlement=settled&&!completedRounds.has(currentRound);
    if(settled)completedRounds.add(currentRound);
    track.setAttribute('aria-valuenow',String(next));
    track.setAttribute('aria-valuetext',episode?`${display(next)} / 100`:'暂无亲密度');
    card.classList.toggle('is-reduced',media.matches);
    // A new episode must not slide from the previous episode's score/color.
    if(reset)fill.style.transition='none';
    fill.style.backgroundColor=colorForScore(next);
    fill.style.width=episode?`${next}%`:'0%';
    if(reset){
      stopNumber();shown=next;showNumber(episode?next:'—');
      fill.getBoundingClientRect();fill.style.removeProperty('transition');
    }else if(next!==previous){
      stopNumber();clearFeedback();
      if(media.matches){shown=next;showNumber(next);}
      else {
        const from=shown,started=performance.now();
        const tick=now=>{
          const t=Math.min(1,(now-started)/480),ease=1-Math.pow(1-t,3);
          shown=from+(next-from)*ease;
          showNumber(t===1?next:Math.round(shown));
          if(t<1)frame=requestAnimationFrame(tick);else frame=null;
        };
        frame=requestAnimationFrame(tick);
      }
      const actual=next-previous;
      // Do not present a stale round, a missed multi-round jump or a correction
      // as a newly settled turn. Repeated public-state polls never replay it.
      if(freshSettlement&&current.delta!==0&&Math.abs(current.delta-actual)<1e-8){
        const positive=actual>0;card.classList.add(positive?'gain':'loss');
        change.textContent=`${positive?'+':'−'}${display(Math.abs(current.delta))}`;
        announce.textContent=`亲密度${positive?'增加':'减少'} ${display(Math.abs(current.delta))}，当前 ${display(next)} 分`;
        if(!media.matches&&typeof change.animate==='function'){
          animations.push(change.animate([
            {opacity:0,transform:`translateY(${positive?5:-5}px)`},
            {opacity:1,transform:'translateY(0)',offset:.16},
            {opacity:1,transform:'translateY(0)',offset:.84},
            {opacity:0,transform:'translateY(0)'}
          ],{duration:2400,easing:'ease-out',fill:'forwards'}));
          const glow=positive?'#d6967f':'#8eafd4';
          animations.push(card.animate([
            {boxShadow:'inset 0 0 0 2px transparent'},
            {boxShadow:`inset 0 0 0 2px ${glow}`,offset:.2},
            {boxShadow:'inset 0 0 0 2px transparent'}
          ],{duration:700,easing:'ease-out'}));
        }
        hideTimer=setTimeout(clearFeedback,2400);
      }
    }
    previous=next;contextKey=context;roundKey=currentRound;
  }
  return {element:card,update,dispose(){
    disposed=true;stopNumber();clearFeedback();media.removeEventListener('change',settleMotion);
  }};
}
