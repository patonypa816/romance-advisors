// Explicit local subscription only. This module never opens a source on load.
const host = document.querySelector('.control-section');
if (host) {
  const panel = document.createElement('details');
  panel.className = 'automation-panel';
  panel.innerHTML = `<summary>抖音评论来源 <span id="source-status" role="status">未连接</span></summary>
    <form id="source-form" class="advance-settings">
      <label>本机 DyHub 地址<input id="source-url" type="url" value="http://127.0.0.1:8757" required autocomplete="off"></label>
      <label>直播间号<input id="source-room" inputmode="numeric" pattern="[0-9]{1,32}" maxlength="32" required autocomplete="off" placeholder="抖音直播页末尾数字"></label>
      <button id="source-start" class="button small" type="submit">连接本期评论</button>
      <button id="source-stop" class="button small" type="button" disabled>停止接收</button>
    </form>
    <p class="quiet-note">先运行本机 DyHub、开始一期，再连接。本期普通评论持续接收；建议与投票仍按窗口判定。仅开放征集的建议候选保留头像链接，不下载图片。不会安装采集器或开启数字人；本期结束自动停止接收，DyHub 本身仍会运行。</p>
    <p id="source-note" class="quiet-note" role="status" aria-live="polite">默认未连接；页面刷新不会新建订阅。</p>`;
  host.prepend(panel);
  const find = id => panel.querySelector('#' + id);
  let pending = false, reading = false, current = null, revision = 0;
  const labels = {stopped: '未连接', connecting: '连接中', connected: '已连接', live: '直播中',
    retrying: '重连中', closed: '直播已结束', error: '来源异常'};
  const errors = {episode_ended: '本期结束，已停止接收。', episode_limit: '达到本期安全上限，已停止接收。',
    source_failed: '来源未完成，请检查本机采集服务。', connection_failed: '未连接到本机 DyHub，请确认已启动。',
    invalid_stream: '来源格式不匹配，请核对 DyHub 版本。', event_callback_failed: '评论未能保存，请检查游戏状态。',
    redirect_rejected: '来源重定向已拒绝，请使用正确的本机地址。', room_connect_failed: '房间连接失败，请核对房间号与直播状态。'};
  function render(value) {
    current = value;
    find('source-status').textContent = labels[value.state] || '来源待确认';
    find('source-start').disabled = pending || value.active;
    find('source-stop').disabled = pending || !value.active;
    find('source-url').disabled = pending || value.active;
    find('source-room').disabled = pending || value.active;
    if (value.active) {
      find('source-url').value = value.base_url;
      find('source-room').value = value.room_id;
    }
    const count = value.counts || {};
    find('source-note').textContent = (errors[value.error_code] || '') +
      ` 本次已收 ${count.accepted || 0} 条，重复 ${count.duplicate || 0} 条，未计入 ${count.ignored || 0} 条。本期最多 ${value.episode_limit || 5000} 条。`;
  }
  async function refresh() {
    if (reading || pending) return;
    reading = true;
    const requestedRevision = revision;
    try {
      const response = await fetch('/api/source', {cache: 'no-store'});
      if (response.ok) {
        const value = await response.json();
        if (!pending && revision === requestedRevision) render(value);
      }
    } catch { if (!pending && revision === requestedRevision) find('source-status').textContent = '本机服务未连接'; }
    finally { reading = false; }
  }
  async function act(action, body) {
    if (pending) return;
    pending = true;
    revision += 1;
    if (current) render(current);
    else find('source-start').disabled = true;
    try {
      const response = await fetch('/api/source/' + action, {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
      const value = await response.json();
      if (!response.ok) {
        find('source-note').textContent = value.error || '请求未完成，请检查本机来源。';
      } else {
        render(value);
      }
    } catch { find('source-note').textContent = '连接中断，请刷新状态确认。'; }
    finally {
      pending = false;
      find('source-start').disabled = Boolean(current?.active);
      find('source-stop').disabled = !current?.active;
      find('source-url').disabled = Boolean(current?.active);
      find('source-room').disabled = Boolean(current?.active);
    }
  }
  find('source-form').addEventListener('submit', event => {
    event.preventDefault();
    act('start', {base_url: find('source-url').value.trim(), room_id: find('source-room').value.trim()});
  });
  find('source-stop').addEventListener('click', () => act('stop', {}));
  refresh();
  const timer = setInterval(refresh, 2000);
  window.addEventListener('pagehide', () => clearInterval(timer), {once: true});
  window.addEventListener('pageshow', event => { if (event.persisted) window.location.reload(); });
}
