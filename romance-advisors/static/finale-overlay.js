import {el} from './ui.js';

// The approved v3 choreography is 6.8 seconds. Only a committed server finale
// can start it; this module never calculates scores or chooses award winners.
export const FINALE_DURATION_MS=6800;
export function safeFinaleAvatar(value) {
  if(typeof value!=='string'||value.length>2048||/[\s\\\x00-\x1f\x7f]/.test(value))return '';
  try {
    const url=new URL(value);
    const hosts=['douyinpic.com','douyinstatic.com','byteimg.com','ibytedtos.com'];
    return url.protocol==='https:'&&!url.username&&!url.password&&!url.port&&!url.hash&&!url.pathname.toLowerCase().endsWith('.svg')&&
      hosts.some(host=>url.hostname===host||url.hostname.endsWith('.'+host))?url.href:'';
  } catch {return '';}
}
export function createFinaleOverlay({onVisibility=()=>{}}={}) {
  const layer=el('section','finale-layer');layer.hidden=true;
  layer.setAttribute('aria-label','本期结算');layer.setAttribute('aria-hidden','true');
  const veil=el('div','grey-veil'),canvas=el('div','finale-canvas');layer.append(veil,canvas);
  const nodes=new Map();
  function part(tag,name,parent=canvas,text='') {
    const node=el(tag,name,text);nodes.set(name,node);parent.append(node);return node;
  }
  const $=name=>nodes.get(name);
  part('div','core-aura');const panel=part('div','core-panel');
  part('span','panel-cap',panel,'本期结算');part('div','panel-shine',panel);
  part('div','ring-one').classList.add('impact-ring');part('div','ring-two').classList.add('impact-ring');
  part('div','kinetic-particles').setAttribute('aria-hidden','true');
  const emblem=part('div','emblem-wrap'),flight=part('div','emblem-flight',emblem);
  part('div','seal-shadow',flight);part('div','seal-shape',flight);part('div','seal-rim',flight);
  const type=part('div','seal-type',flight);part('span','seal-eyebrow',type);
  part('div','seal-numeral',type);part('span','seal-bottom',type);
  part('span','seal-sticker',flight);part('span','seal-star',flight);
  const result=part('div','result-copy');part('h2','main-result',result);part('p','main-joke',result);
  const awards=part('section','center-awards');awards.setAttribute('aria-label','本期军师');
  const cards={};
  for(const [key,style,label,rank,mark] of [
    ['best','medal-best','本期最强军师','01','✦'],
    ['worst','medal-goofy','本期狗头军师','02','✳']
  ]) {
    const card=part('article',style,awards);card.classList.add('medal-card');
    const avatar=el('div','center-avatar'),initial=el('span'),image=el('img');
    image.hidden=true;image.referrerPolicy='no-referrer';image.decoding='async';image.alt='';
    image.addEventListener('error',()=>{image.hidden=true;});
    avatar.append(initial,image,el('b','',mark));
    const copy=el('div','medal-copy'),name=el('h3'),caption=el('small');
    copy.append(el('p','',label),name,caption);
    card.append(avatar,copy,el('span','medal-rank',rank),el('div','card-shine'));
    cards[key]={initial,image,name,caption};
  }
  const closing=part('div','closing-line'),closingText=el('span','closing-text');
  closing.append(closingText,el('span','','✦'));
  const announce=el('span','finale-sr-only');announce.setAttribute('role','status');
  announce.setAttribute('aria-live','polite');announce.setAttribute('aria-atomic','true');layer.append(announce);
  const media=matchMedia('(prefers-reduced-motion: reduce)');
  const OUT='cubic-bezier(.16,1,.3,1)',INOUT='cubic-bezier(.65,0,.35,1)';
  let mode='success',animations=[],frame=null,key=null,disposed=false,visible=false;
  let started=0,elapsed=0,completed=false,suspended=false;
  function setVisible(value) {
    layer.hidden=!value;layer.setAttribute('aria-hidden',String(!value));
    if(visible!==value){visible=value;onVisibility(value);}
  }
  function stopFrame(){if(frame!==null)cancelAnimationFrame(frame);frame=null;}
  function cancelAnimations(){for(const animation of animations)animation.cancel();animations=[];}
  function A(target,frames,duration,delay=0,easing=OUT) {
    const node=typeof target==='string'?$(target):target;
    if(typeof node.animate!=='function') {
      Object.assign(node.style,frames[frames.length-1]);return null;
    }
    const animation=node.animate(frames,{duration,delay,easing,fill:'both'});
    animation.pause();animation.currentTime=0;animations.push(animation);return animation;
  }
function pieces(){const root=$('kinetic-particles');root.replaceChildren();for(let i=0;i<24;i++){const n=document.createElement('i');n.dataset.particle=String(i);root.append(n);}for(let i=0;i<4;i++){const n=document.createElement('b');n.textContent=i%2?'✳':'✦';n.dataset.spark=String(i);root.append(n);}}
function build(){cancelAnimations();pieces();const win=mode==='success';
 A('core-aura',[{opacity:0,transform:'scale(.5)'},{opacity:.8,transform:'scale(1.05)',offset:.42},{opacity:.25,transform:'scale(.9)'}],1800,100);
 A('core-panel',[{opacity:0,transform:'translateY(48px) scale(.86,.30)'},{opacity:1,transform:'translateY(-5px) scale(1.015,1.015)',offset:.72},{opacity:1,transform:'translateY(0) scale(1)'}],650,1540);
 A('emblem-wrap',[{transform:'translateY(0) scale(1)'},{transform:'translateY(-90px) scale(.66)'}],690,1520,INOUT);
 A('emblem-flight',win?[
 {opacity:0,transform:'translateY(35px) rotate(-22deg) scale(.28)'},
 {opacity:1,transform:'translateY(42px) rotate(-25deg) scale(.24)',offset:.14},
 {opacity:1,transform:'translateY(-14px) rotate(5deg) scale(1.10,.94)',offset:.62},
 {opacity:1,transform:'translateY(4px) rotate(-3deg) scale(.98,1.025)',offset:.83},
 {opacity:1,transform:'translateY(0) rotate(0) scale(1)'}]:[
 {opacity:0,transform:'translate(26px,-52px) rotate(23deg) scale(.36)'},
 {opacity:1,transform:'translate(-6px,12px) rotate(-16deg) scale(1.02,.90)',offset:.44},
 {opacity:1,transform:'translate(3px,-17px) rotate(9deg) scale(.98,1.035)',offset:.68},
 {opacity:1,transform:'translate(0,4px) rotate(-4deg) scale(1.015,.99)',offset:.86},
 {opacity:1,transform:'translate(0,0) rotate(0) scale(1)'}],win?780:940,100,'linear');
 A('seal-shape',[{transform:'rotate(-32deg)'},{transform:'rotate(-8deg)'}],1000,100);
 A('seal-shadow',[{opacity:0,transform:'translateY(25px) scale(.6)'},{opacity:.7,transform:'translateY(0) scale(1)'}],600,180);
 for(const [i,el] of [...$('seal-numeral').children].entries())A(el,[{opacity:0,transform:`translateY(78px) rotate(${win?-9:12}deg) scale(.8)`},{opacity:1,transform:'translateY(-4px) rotate(0) scale(1.04)',offset:.75},{opacity:1,transform:'translateY(0) rotate(0) scale(1)'}],480,340+i*70);
 A('seal-eyebrow',[{opacity:0,transform:'translateY(7px)'},{opacity:1,transform:'translateY(0)'}],300,630);
 A('seal-bottom',[{opacity:0,transform:'translateY(8px)'},{opacity:1,transform:'translateY(0)'}],350,690);
 A('seal-sticker',[{opacity:0,transform:`rotate(${win?-50:45}deg) scale(.2)`},{opacity:1,transform:`rotate(${win?17:-16}deg) scale(1.18)`,offset:.7},{opacity:1,transform:`rotate(${win?12:-10}deg) scale(1)`}],470,730);
 A('seal-star',[{opacity:0,transform:'rotate(-65deg) scale(.1)'},{opacity:1,transform:'rotate(20deg) scale(1.13)',offset:.75},{opacity:1,transform:'rotate(14deg) scale(1)'}],540,550);
 A('ring-one',[{opacity:0,transform:'scale(.1)'},{opacity:.65,transform:'scale(.72)',offset:.18},{opacity:0,transform:win?'scale(1.45)':'scale(1.3,.7)'}],1000,420);
 A('ring-two',[{opacity:0,transform:'scale(.2) rotate(-20deg)'},{opacity:.35,transform:'scale(.8) rotate(0)',offset:.2},{opacity:0,transform:'scale(1.65) rotate(18deg)'}],1200,510);
 for(const el of $('kinetic-particles').querySelectorAll('i')){const i=Number(el.dataset.particle),angle=i*2.39996323,r=95+(i%5)*20,x=Math.cos(angle)*r,y=Math.sin(angle)*r*(win?.85:.45);A(el,[{opacity:0,transform:'translate(0,0) rotate(0) scale(.1)'},{opacity:win?.9:.55,transform:`translate(${x*.7}px,${y*.7}px) rotate(${i*31}deg) scale(1)`,offset:.18},{opacity:0,transform:`translate(${x}px,${y+(win?94:132)}px) rotate(${i*71}deg) scale(.35)`}],win?1500:1800,440+(i%5)*24);}
 for(const el of $('kinetic-particles').querySelectorAll('b')){const i=Number(el.dataset.spark),x=[-148,146,-118,128][i],y=[-70,-115,96,60][i];A(el,[{opacity:0,transform:'translate(0,0) scale(0)'},{opacity:.85,transform:`translate(${x}px,${y}px) rotate(${i*25}deg) scale(1)`,offset:.45},{opacity:0,transform:`translate(${x*1.1}px,${y*1.2}px) rotate(${i*40+20}deg) scale(.4)`}],1300,520+i*65);}
 A('result-copy',[{transform:'translateY(0) scale(1)'},{transform:'translateY(-144px) scale(.83)'}],690,1520,INOUT);
 for(const [i,el] of [...$('main-result').children].entries())A(el,[{opacity:0,transform:`translateY(22px) rotate(${win?-8:8}deg) scale(.6)`},{opacity:1,transform:'translateY(-4px) rotate(0) scale(1.08)',offset:.7},{opacity:1,transform:'translateY(0) rotate(0) scale(1)'}],460,700+i*70);
 A('main-joke',[{opacity:0,transform:'translateY(9px)'},{opacity:1,transform:'translateY(0)'}],400,1020);
 for(const [i,id] of ['medal-best','medal-goofy'].entries()){const card=$(id),delay=2140+i*110;A(card,[{opacity:0,transform:`translate(${i?24:-24}px,26px) rotate(${i?4:-4}deg) scale(.91)`},{opacity:1,transform:'translate(0,-3px) rotate(0) scale(1.025)',offset:.74},{opacity:1,transform:'translate(0,0) rotate(0) scale(1)'}],560,delay);
 A(card.querySelector('.center-avatar'),[{transform:'scale(.3) rotate(-25deg)',opacity:0},{transform:'scale(1.12) rotate(4deg)',opacity:1,offset:.72},{transform:'scale(1) rotate(0)',opacity:1}],480,delay+130);
 A(card.querySelector('.medal-copy'),[{opacity:0,transform:'translateX(10px)'},{opacity:1,transform:'translateX(0)'}],400,delay+220);
 A(card.querySelector('.card-shine'),[{transform:'translateX(0) skewX(-24deg)',opacity:0},{transform:'translateX(100px) skewX(-24deg)',opacity:.8,offset:.25},{transform:'translateX(490px) skewX(-24deg)',opacity:0}],700,delay+440);}
 A('closing-line',[{opacity:0,transform:'translateY(10px)'},{opacity:1,transform:'translateY(0)'}],400,2800);
 A('panel-shine',[{transform:'translateX(0) skewX(-24deg)',opacity:0},{transform:'translateX(180px) skewX(-24deg)',opacity:1,offset:.35},{transform:'translateX(700px) skewX(-24deg)',opacity:0}],1000,2800);
 // A final, restrained wink after the names have settled. Text remains still.
 A($('medal-goofy').querySelector('.center-avatar b'),[{transform:'rotate(0) scale(1)'},{transform:'rotate(-18deg) scale(1.13)',offset:.35},{transform:'rotate(8deg) scale(1.05)',offset:.65},{transform:'rotate(0) scale(1)'}],500,4100);
}

  function renderTime(value) {
    elapsed=Math.max(elapsed,Math.min(FINALE_DURATION_MS,value));
    const position=media.matches?FINALE_DURATION_MS:elapsed;
    for(const animation of animations)animation.currentTime=position;
    completed=position>=FINALE_DURATION_MS;
    layer.dataset.state=completed?'complete':position>=2140?'awards':'burst';
  }
  function tick(now) {
    frame=null;
    if(disposed||suspended||!visible)return;
    if(!layer.isConnected){reset();return;}
    renderTime(now-started);
    if(!completed)frame=requestAnimationFrame(tick);
  }
  function schedule(){if(!completed&&frame===null)frame=requestAnimationFrame(tick);}
  function settleMotion(){
    if(!visible)return;
    // Changing the OS setting mid-flight immediately lands on the readable
    // result. Switching it back must never replay the burst.
    if(media.matches){stopFrame();renderTime(FINALE_DURATION_MS);}
  }
  media.addEventListener('change',settleMotion);
  function reset(){
    stopFrame();cancelAnimations();key=null;elapsed=0;completed=false;
    setVisible(false);announce.textContent='';
    for(const {image} of Object.values(cards)){image.hidden=true;image.removeAttribute('src');}
  }
  function setCopy(finale) {
    mode=finale.status==='SUCCEEDED'?'success':'failure';layer.dataset.mode=mode;
    const win=mode==='success';
    $('seal-numeral').replaceChildren(...[...(win?'100':'哎呀')].map(c=>el('span','',c)));
    $('main-result').replaceChildren(...[...(win?'成了！':'没成…')].map(c=>el('span','',c)));
    $('seal-eyebrow').textContent=win?'MISSION COMPLETE':'NOT THIS TIME';
    $('seal-bottom').textContent=win?'亲密度 · 达成':'这次 · 没成';
    $('seal-sticker').textContent=win?'✓':'↺';$('seal-star').textContent=win?'✦':'✳';
    $('main-joke').textContent=win?'建议军师直接开班。':'建议军师先别开班。';
    closingText.textContent=win?'军师们，下局见。':'问题不大，下局再战。';
    const names=[];
    for(const award of ['best','worst']) {
      const entry=finale.awards?.[award],card=cards[award];
      const name=entry?String(entry.nickname||'匿名军师'):'本期暂缺';
      card.name.textContent=name;card.name.title=name;
      card.initial.textContent=entry?Array.from(name.trim())[0]||'军':'—';
      card.caption.textContent=!entry?'期待下期好建议':award==='best'?
        (win?'这波，确实有点东西。':'有点思路，还需练习。'):
        '主打一个敢说。';
      card.image.hidden=true;card.image.removeAttribute('src');
      const src=safeFinaleAvatar(entry?.avatar_url);
      if(src){card.image.src=src;card.image.hidden=false;}
      names.push(`${award==='best'?'最强军师':'狗头军师'}：${name}`);
    }
    announce.textContent=`${win?'本期牵手成功':'本期遗憾收场'}。${names.join('；')}`;
  }
  return {element:layer,
    update(state) {
      if(disposed||suspended)return;
      const episode=state?.episode,finale=state?.finale;
      if(!episode||!finale||!['SUCCEEDED','FAILED'].includes(finale.status)||
        finale.status!==episode.status||finale.episode_id!==episode.id||
        !Number.isFinite(finale.started_at)){if(key!==null)reset();return;}
      const nextKey=JSON.stringify([state.session?.id??null,finale.episode_id,finale.started_at,finale.status]);
      // Missing timing data is displayed statically; a refresh must not invent
      // a fresh 6.8-second performance for an old committed result.
      const serverElapsed=Number.isFinite(finale.elapsed_ms)&&finale.elapsed_ms>=0?
        Math.min(FINALE_DURATION_MS,finale.elapsed_ms):FINALE_DURATION_MS;
      const now=performance.now();
      if(nextKey!==key){
        reset();key=nextKey;setCopy(finale);build();elapsed=serverElapsed;
        started=now-elapsed;setVisible(true);renderTime(elapsed);
      } else {
        // Snapshot names stay frozen, and repeated/late polls can only advance
        // the timeline. They never replace nodes, reverse it or start it again.
        const position=Math.max(elapsed,serverElapsed,now-started);
        started=now-position;renderTime(position);
      }
      schedule();
    },
    suspend(){suspended=true;stopFrame();},
    resume(){suspended=false;},
    dispose(){disposed=true;reset();media.removeEventListener('change',settleMotion);}
  };
}
