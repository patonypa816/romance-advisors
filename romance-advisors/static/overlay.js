import {getState} from './ui.js';
import {createLiveRenderer} from './renderer.js';
import {loadLayout,observeLayout,normalizeLayout} from './layout.js';
import {createPreviewFixture} from './preview-fixture.js';
const root=document.getElementById('overlay-root');
const renderer=createLiveRenderer(root);
renderer.setLayout(loadLayout());observeLayout(value=>renderer.setLayout(value));
function fit(){const scale=Math.min(innerWidth/1080,innerHeight/1920);root.style.transform=`translate(-50%, -50%) scale(${scale})`;}
window.addEventListener('resize',fit);fit();
let running=false,lastFailure=null;
async function poll(){
  if(running)return;
  running=true;
  try{
    let state;
    // A state request can succeed even when rendering fails. Keep the two
    // outcomes separate so a UI bug is never reported as a lost connection.
    try{state=await getState(true);}
    catch(error){
      renderer.offline();
      if(lastFailure!=='state')console.error('叠层状态读取失败',error);
      lastFailure='state';return;
    }
    try{
      if(state.layout)renderer.setLayout(normalizeLayout(state.layout));
      renderer.render(state);
      lastFailure=null;
    }catch(error){
      renderer.renderFailed();
      // Log once per continuous failure, rather than flooding every 900ms.
      if(lastFailure!=='render')console.error('叠层画面更新失败',error);
      lastFailure='render';
    }
  }finally{running=false;}
}
// Demo stays entirely in memory and uses the production renderer; never poll or mutate game data.
if(new URLSearchParams(location.search).get('demo')==='1'){
  root.classList.add('is-preview-fixture');
  renderer.render(createPreviewFixture());
}else{poll();setInterval(poll,900);}
