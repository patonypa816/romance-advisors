import {post,APPEARANCE_LABELS} from './ui.js';
let offeredSavedRoom = false;
const $=id=>document.getElementById(id);
let appearance=null,busy=false,dirty=false,polling=false,generation=0;
const defaultBandStarts={tired:20,normal:40,refined:60,stellar:80};
const bandKeys=Object.keys(defaultBandStarts);
const fields=[...bandKeys.map(key=>'appearance-band-'+key),'appearance-enabled','appearance-room','appearance-positive','appearance-negative','appearance-quantity'];
fields.forEach(id=>$(id).addEventListener('input',()=>{dirty=true;}));
const ids=value=>value.split(/[\s,，、]+/u).filter(Boolean);
function render(value){
  appearance=value;
  const active=value.rehearsal_active===true,ready=value.readiness;
  const bands={...defaultBandStarts,...value.settings?.band_starts};
  // Describe saved rules only. Unsaved edits must not suggest the game changed.
  $('appearance-tiers').textContent=`0 鱼人头 · 1–${bands.tired-1} 通宵残局 · ${bands.tired}–${bands.normal-1} 没睡醒 · ${bands.normal}–${bands.refined-1} 日常 · ${bands.refined}–${bands.stellar-1} 精致 · ${bands.stellar}–100 高光`;
  if(!dirty)for(const key of bandKeys)$('appearance-band-'+key).value=bands[key];
  const modeLabel=active?'本地试玩':value.mode==='rtc'?'真实动态模式':value.enabled?'真实收礼已开启 · 本期未计分':'真实收礼未开启';
  $('appearance-state').textContent=active||value.mode==='rtc'?`${modeLabel} · 造型 ${value.score??'—'} · 实际 ${APPEARANCE_LABELS[value.tier]||'未确认'}${value.target&&value.target!==value.tier?` → ${APPEARANCE_LABELS[value.target]||'待切'}`:''}`:modeLabel;
  $('appearance-ready').textContent=ready?.message||(ready?.ready?'真实动态绑定已就绪。':'尚未就绪：需绑定六档可驱动资产与双向转场；本地试玩可独立使用。');
  $('appearance-start').disabled=busy||active||value.mode==='rtc';
  for(const id of ['appearance-up','appearance-down','appearance-stop'])$(id).disabled=busy||!active;
  $('appearance-save').disabled=busy||active||value.mode==='rtc';
  const test=value.operator_test;
  $('appearance-onair-test').hidden=!test || !(test.run_id && test.episode_id);
  $('appearance-test-points').disabled=busy || test?.eligible!==true || test.used===true;
  const applied=test?.receipt;
  $('appearance-test-note').textContent=test?.used && applied
    ? `本期已用场控测试：${applied.score_before} → ${applied.score} 分。`
    : test?.eligible?'本期可用一次。分数立即进入直播游戏，跨档会触发换形。':test?.message||'';
  const probe=value.gift_probe;
  $('appearance-gift-probe').hidden=!probe?.available&&!probe?.started_at;
  $('appearance-gift-probe-reset').disabled=busy||!probe?.active;
  const probeStatus=!probe?.active?'请连接已保存的房间，等待真实礼物':probe.state==='waiting'?'等待真实礼物，尚未实收':'已收到真实礼物';
  const probeErrors={stale_gift:'已忽略时间不可信或过期的礼物',one_diamond_required:'已忽略非 1 钻礼物',unreliable_gift:'已忽略缺少可靠数量或组号的礼物',receipt_limit:'核量记录已满，请重新核量'};
  $('appearance-gift-probe-state').textContent=`一钻手测核量：${probeStatus} · 测试分 ${probe?.score??50}${probeErrors[probe?.last_reason]?` · ${probeErrors[probe.last_reason]}`:''}`;
  $('appearance-gift-probe-rows').textContent=(probe?.gifts||[]).map(row=>`${row.name} ${row.direction>0?'＋':'−'}（ID ${row.gift_id}）：${row.unit_diamonds===null?'单价待实收':`${row.unit_diamonds} 钻/个`} · 本组累计 ${row.cumulative_count} 个 · 去重共 ${row.accepted_count} 个 · 本次新增 ${row.last_increment} 个`).join('；');
  if(!dirty&&value.settings){
    const settings=value.settings;
    const sourceRoom=$('source-room');
    if(!offeredSavedRoom&&sourceRoom&&settings.room_id){
      // Offer the saved room on first load; subsequent edits belong to the operator.
      offeredSavedRoom=true;
      if(!sourceRoom.value&&!sourceRoom.disabled)sourceRoom.value=settings.room_id;
    }
    $('appearance-enabled').checked=settings.enabled===true;
    $('appearance-room').value=settings.room_id||'';
    $('appearance-positive').value=(settings.positive_gift_ids||[]).join(', ');
    $('appearance-negative').value=(settings.negative_gift_ids||[]).join(', ');
    $('appearance-quantity').checked=settings.quantity_verified===true;
  }
  const receipt=value.receipt||value.pending_confirmation;
  const automatic=receipt&&['pending','written'].includes(receipt.status);
  const failed=receipt&&['failed','unconfirmed'].includes(receipt.status);
  $('appearance-confirmation').hidden=!automatic&&!failed;
  $('appearance-confirm-note').textContent=failed
    ?'本次换形失败或结果未知，节目已暂停；请核对接收画面，必要时结束数字人。不会自动重试。'
    :automatic?'正在自动衔接造型，完成转场保护并收到新的健康画面后继续，无需点击确认。':'';

}
function showError(message){$('appearance-error').textContent=message;$('appearance-error').hidden=!message;}
async function perform(path,body,feedback){
  if(busy)return;generation++;busy=true;showError('');if(appearance)render(appearance);
  try{const response=await post(path,body);if(path.endsWith('/settings'))dirty=false;render(response.appearance);$('appearance-feedback').textContent=feedback;}
  catch(error){showError(error.message);}
  finally{busy=false;if(appearance)render(appearance);}
}
for(const action of ['start','up','down','stop'])$('appearance-'+action).addEventListener('click',()=>{
  const diamonds=Number($('appearance-diamonds').value);
  if(['up','down'].includes(action)&&(!Number.isInteger(diamonds)||diamonds<1||diamonds>100)){showError('模拟分数请输入 1–100 的整数。');return;}
  if(action==='start'){
    // The production overlay now follows rehearsal state rather than the layout fixture.
    const fixture=$('show-preview-demo');if(fixture.checked){fixture.checked=false;fixture.dispatchEvent(new Event('change'));}
  }
  perform('/api/appearance/rehearsal',['up','down'].includes(action)?{action,diamonds}:{action},action==='stop'?'本地试玩已结束。':action==='start'?'本地试玩开始：从普通档 50 分出场，无收费调用。':'已提交本地模拟加减分；这不是实际礼物收件。');
});
$('appearance-settings').addEventListener('submit',event=>{
  event.preventDefault();
  const band_starts=Object.fromEntries(bandKeys.map(key=>[key,Number($('appearance-band-'+key).value)]));
  const {tired,normal,refined,stellar}=band_starts;
  if(!Object.values(band_starts).every(Number.isInteger)||!(2<=tired&&tired<normal&&normal<=50&&50<refined&&refined<stellar&&stellar<=100)){
    showError('分界须为递增整数：2 ≤ 没睡醒 < 普通 ≤ 50 < 精致 < 高光 ≤ 100。0 固定鱼头，50 必须普通。');return;
  }
  const positive=ids($('appearance-positive').value),negative=ids($('appearance-negative').value);
  if(positive.some(id=>negative.includes(id))){showError('同一礼物 ID 不能同时用于正向和负向。');return;}
  perform('/api/appearance/settings',{enabled:$('appearance-enabled').checked,room_id:$('appearance-room').value.trim(),positive_gift_ids:positive,negative_gift_ids:negative,quantity_verified:$('appearance-quantity').checked,band_starts},'造型配置已保存；不会自动开场。');
});
$('appearance-gift-probe-reset').addEventListener('click',()=>perform('/api/appearance/gift-probe',{},'核量已从 50 分重新开始，等待新的真实礼物；正式配置未改变。'));
$('appearance-test-points').addEventListener('click',()=>{
  const test=appearance?.operator_test;
  if(!test?.eligible || test.used || !test.run_id || !test.episode_id || $('appearance-test-points').disabled)return;
  perform('/api/appearance/operator-test',{run_id:test.run_id,episode_id:test.episode_id},
    '本期造型分已更新。跨档时请在 OBS 查看转场和新造型。');
});
async function poll(){
  if(polling||busy)return;polling=true;const readGeneration=generation;
  try{const response=await fetch('/api/appearance',{cache:'no-store'});if(!response.ok)throw new Error('造型状态暂不可读');const data=await response.json();if(!busy&&readGeneration===generation)render(data.appearance);}
  catch(error){if(readGeneration!==generation)return;showError(error.message);for(const id of ['start','up','down','stop','save','gift-probe-reset','test-points'])$('appearance-'+id).disabled=true;}
  finally{polling=false;}
}
poll();setInterval(poll,900);
