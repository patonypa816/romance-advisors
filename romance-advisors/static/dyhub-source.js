// The service owns the broadcast watcher. Opening another page only reads it.
const host = document.querySelector('.control-section');
if (host) {
  const broadcast = !/(?:^|[?&])debug=1(?:&|$)/.test(window.location?.search || '');
  const panel = document.createElement('section');
  panel.className = 'source-panel';
  panel.innerHTML = `<div class="source-heading"><h2>抖音直播间评论</h2><span id="source-status" class="source-status" role="status">未连接</span></div>
    <p class="quiet-note">${broadcast?'检测到本房间开播后，等 5 秒复核并接收弹幕和已配置的礼物。':'连接本期调试评论来源。'}</p>
    <form id="source-form" class="source-form">
      <label class="source-room-label" for="source-room">数字房间号或直播页链接</label>
      <div class="source-connect-row">
        <input id="source-room" type="text" inputmode="url" maxlength="256" required autocomplete="off" placeholder="例如 123456789，或 live.douyin.com/123456789">
        <button id="source-start" class="button" type="submit">连接直播评论</button>
        <button id="source-stop" class="button secondary" type="button" disabled>断开</button>
      </div>
      <details class="source-advanced"><summary>高级设置</summary>
        <label for="source-url">本机 DyHub 地址</label><input id="source-url" type="url" value="http://127.0.0.1:8757" required autocomplete="off">
        <p class="quiet-note">使用已启动的本机评论采集服务，通常无需修改。</p>
      </details>
    </form>
    <p id="source-note" class="source-note" role="status" aria-live="polite">未连接。连接后会在这里显示是否收到真实评论。</p>
    <details id="source-recent-wrap" class="source-recent-wrap" hidden open><summary>最近直播评论</summary><pre id="source-recent" class="source-recent"></pre></details>`;
  host.prepend(panel);
  const find = id => panel.querySelector('#' + id);
  let pending = false, reading = false, current = null, revision = 0;
  let editingRoom = false;
  for (const id of ['source-room','source-url']) find(id).addEventListener('input', () => { editingRoom=true; });
  // Keep operation errors until the next explicit attempt; a routine poll must not erase them.
  let operationError = '', readError = '';
  const labels = {stopped: '未连接', connecting: '正在连接接收通道', connected: '接收通道已连接', live: '接收通道已连接',
    retrying: '接收通道重连中', closed: '采集连接已断开', error: '接收异常'};
  const errors = {episode_ended: '本期已结束，评论接收已停止。下一期开始后请重新连接。', episode_limit: '本期评论已达接收上限，已停止接收。',
    source_failed: '评论接收中断，请检查本机采集服务，再重新连接。', connection_failed: '未连接到本机采集服务，请先启动 DyHub，再重试。',
    invalid_stream: '采集服务返回格式不匹配，请检查 DyHub 是否正常运行。', event_callback_failed: '评论未能保存，请检查游戏状态。',
    redirect_rejected: '采集服务地址不正确，请在高级设置中核对本机地址。', room_connect_failed: '未连上直播间，请确认抖音已开播且数字房间号正确；可断开后重试。'};
  const autoLabels = {unconfigured:'待设置房间',waiting:'等候抖音开播',confirming_live:'正在复核本场开播',
    unknown:'开播状态待核实',retrying:'正在重试探测',paused:'本场采集已暂停',confirming_end:'正在核实抖音是否下播',
    finishing:'正在结束本场',stop_unconfirmed:'数字人停场待核实'};
  function render(value) {
    current = value;
    const auto = broadcast && value.auto;
    const label = auto && autoLabels[auto.state] || labels[value.state] || '接收状态待确认';
    find('source-status').textContent = pending ? '正在处理…' : readError ? '状态暂不可用' : label;
    find('source-start').textContent = auto ? (auto.paused ? '恢复自动接收' : '保存直播间') : '连接直播评论';
    find('source-stop').textContent = auto ? '暂停本场接收' : '断开';
    find('source-start').disabled = pending || value.active;
    find('source-stop').disabled = pending || !(value.active || (auto && auto.enabled && !auto.paused));
    find('source-url').disabled = pending || value.active;
    find('source-room').disabled = pending || value.active;
    if (value.active) {
      find('source-url').value = value.base_url;
      find('source-room').value = value.room_id;
    } else if (auto && auto.room_id && !editingRoom) {
      find('source-url').value = auto.base_url;
      find('source-room').value = auto.room_id;
    }
    const count = value.counts || {};
    const accepted = count.received ?? count.accepted ?? 0;
    const receipt = accepted > 0 ? `本次已收到 ${accepted} 条真实评论。` :
      value.active ? '尚未收到真实评论。通道连接后，请在抖音直播间发送评论并观察这里。' : '连接后会在这里显示是否收到真实评论。';
    // textContent keeps viewer names and comments inert, including HTML-like text.
    const recent = Array.isArray(value.recent_comments) ? value.recent_comments.slice(-40) : [];
    find('source-recent-wrap').hidden = recent.length === 0;
    find('source-recent').textContent = recent.map(row => `${row.nickname}：${row.text}`).join('\n');
    // These are accepted event updates, not a count of individual gifts:
    // a single combo update can add several items at once.
    const gifts = value.active || count.gift_accepted ? `游戏已计分礼物事件 ${count.gift_accepted||0} 条。` : '';
    const stop = auto && auto.stop_result;
    const stopNote = stop && (stop.cloud_stop_confirmed===false ? '上一场数字人云端结束尚未确认，请核实。' :
      stop.cloud_stop_confirmed===true ? '上一场数字人已确认结束。' : '');
    find('source-note').textContent = [operationError, readError, stopNote, errors[value.error_code] || '', receipt, gifts].filter(Boolean).join(' ');
  }
  async function refresh() {
    if (reading || pending) return;
    reading = true;
    const requestedRevision = revision;
    try {
      const response = await fetch('/api/source', {cache: 'no-store'});
      if (!response.ok) throw new Error('status unavailable');
      const value = await response.json();
      if (!pending && revision === requestedRevision) {
        readError = '';
        render(value);
      }
    } catch {
      if (!pending && revision === requestedRevision) {
        readError = '暂时无法读取接收状态，请检查游戏服务；状态恢复后会自动更新。';
        render(current || {active: false, state: 'stopped'});
      }
    } finally { reading = false; }
  }
  async function act(action, body) {
    if (pending) return;
    pending = true;
    revision += 1;
    operationError = '';
    readError = '';
    render(current || {active: false, state: 'stopped'});
    try {
      const response = await fetch('/api/source/' + action, {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
      const value = await response.json();
      if (!response.ok) operationError = value.error || '操作未完成，请检查直播状态和本机采集服务，再重试。';
      else {current = value;editingRoom=false;}
    } catch { operationError = '连接中断，正在刷新状态确认。请查看连接状态后重试。'; }
    finally {
      pending = false;
      render(current || {active: false, state: 'stopped'});
    }
  }
  function roomNumber(value) {
    // Only accept the public numeric room path, never profile IDs or arbitrary URLs.
    const match = value.trim().match(/^(?:https?:\/\/)?live\.douyin\.com\/([0-9]{1,32})\/?(?:[?#][^\s]*)?$/i);
    return match ? match[1] : /^[0-9]{1,32}$/.test(value.trim()) ? value.trim() : null;
  }
  find('source-form').addEventListener('submit', event => {
    event.preventDefault();
    if (pending) return;
    const room = roomNumber(find('source-room').value);
    if (!room) {
      operationError = '请输入直播页末尾的纯数字房间号，或 live.douyin.com/数字 链接；不要填写主播主页号。';
      render(current || {active: false, state: 'stopped'});
      return;
    }
    find('source-room').value = room;
    const body = {base_url: find('source-url').value.trim(), room_id: room};
    act(broadcast ? 'auto' : 'start', body);
  });
  find('source-stop').addEventListener('click', () => act('stop', {}));
  refresh();
  const timer = setInterval(refresh, 2000);
  window.addEventListener('pagehide', () => clearInterval(timer), {once: true});
  window.addEventListener('pageshow', event => { if (event.persisted) window.location.reload(); });
}
