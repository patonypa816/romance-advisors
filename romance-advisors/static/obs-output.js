import {createLiveRenderer} from './renderer.js';
import {normalizeLayout} from './layout.js';
import {applyPortraitLayout,shotForState} from './scene-layout.js';
import {createPlayerMount} from './obs-output-player.js';
const $=id=>document.getElementById(id),scene=$('program-scene'),root=$('overlay-root');
const renderer=createLiveRenderer(root);
// Diagnostics deliberately stay outside the audience composition. iframe load
// confirms a document only; it must never be described as audio/video proof.
const diagnostic={state:'waiting',player:'waiting',lastStateAt:null};
window.obsOutputStatus=diagnostic;
function record(key,value){diagnostic[key]=value;scene.dataset[key]=value;}
const player=createPlayerMount({frame:$('avatar-player'),still:$('avatar-still'),label:$('avatar-still-label'),placeholder:$('avatar-placeholder'),onStatus:value=>record('player',value)});
function fit(){const scale=Math.min(innerWidth/1080,innerHeight/1920);scene.style.transform=`translate(-50%, -50%) scale(${scale})`;}
window.addEventListener('resize',fit);fit();
let polling=false,stopped=false,timer=null;
async function poll(){
  if(polling||stopped)return;
  polling=true;
  try{
    const response=await fetch('/api/obs-output',{method:'GET',cache:'no-store',credentials:'same-origin'});
    if(!response.ok)throw new Error('state_unavailable');
    const state=await response.json();
    if(stopped)return;
    // Use server coordinates, never a browser's localStorage layout. Invalid
    // layout leaves the last valid composition and player untouched.
    const layout=normalizeLayout(state.layout);
    renderer.setLayout(layout);
    const shot=shotForState(state);
    applyPortraitLayout($('program-avatar'),layout,shot);
    scene.dataset.shot=shot;
    renderer.render(state);
    player.update(state,location.href);
    diagnostic.lastStateAt=new Date().toISOString();record('state','ready');
  }catch{
    // Keep the current iframe mounted on transient GET/render failure. Rendering
    // an idle portrait here would misrepresent a disconnected live programme.
    renderer.offline();player.disconnected();record('state','unavailable');
  }finally{polling=false;if(!stopped)timer=setTimeout(poll,900);}
}
poll();
window.addEventListener('pagehide',event=>{stopped=true;clearTimeout(timer);renderer.suspend();if(!event.persisted)renderer.dispose();});
window.addEventListener('pageshow',event=>{if(event.persisted){stopped=false;renderer.resume();poll();}});
