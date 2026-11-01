import './style.css';
import { setupInterview } from './interview';
import { setupCompetition } from './competition';
let competitions: ReturnType<typeof setupCompetition> | undefined;
let interviews: ReturnType<typeof setupInterview> | undefined;

type Player = { player_id: string; provider: string; identity_scope: string; subject_id: string; display_name: string; role: string; department: string | null; position: string | null; performance: number; joined_at: string; updated_at: string; recent_events: unknown[] };
type Snapshot = { player: Player; revision: number; replayed?: boolean };
type Command = { command_id: string; provider: 'simulated'; identity_scope: 'local-test'; subject_id: string; display_name: string };
const el = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const subject = el<HTMLInputElement>('subject-id');
const nickname = el<HTMLInputElement>('display-name');
const query = el<HTMLInputElement>('query');
const register = el<HTMLButtonElement>('register');
let authenticated = false, healthy = false, busy = false, checking = false, authenticating = false;
let authEpoch = 0;
let transientNotice: string | null = null;
let revision = -1, listRequest = 0, detailRequest = 0, queryVersion = 0;
let selected: string | null = null, pending: Command | null = null;
class ApiError extends Error { constructor(message: string, public status: number) { super(message); } }
function syncControls() {
  interviews?.sync();
  competitions?.sync();
  register.disabled = !authenticated || !healthy || !navigator.onLine || busy || !!competitions?.locked() || !!interviews?.locked();
  subject.disabled = nickname.disabled = busy;
  el<HTMLButtonElement>('search-button').disabled = !authenticated || !healthy;
  register.textContent = busy ? '正在提交…' : pending ? '重试 / 确认原登记' : '登记游客';
}
function status(message: string) {
  // Offline is authoritative even when an earlier fetch settles after the browser
  // event. Neither a delayed health success nor a failed request may overwrite it.
  if (!navigator.onLine) {
    healthy = false;
    el('connection').textContent = '浏览器离线 · 显示内容可能已陈旧，登记暂停。';
  } else {
    el('connection').textContent = message;
  }
  syncControls();
}
function failure(error: unknown): string {
  if (error instanceof ApiError && error.status === 401) {
    authenticated = false; healthy = false;
    ++listRequest; ++detailRequest;
    el('players').replaceChildren(); el('detail-content').replaceChildren();
    status('登录已失效。请使用启动器生成的一次性链接重新进入。');
  } else if (!(error instanceof ApiError) || error.status >= 500) {
    healthy = false; status('连接异常 · 当前快照可能已陈旧；登记暂停，请重新检查连接。');
  }
  return error instanceof Error ? error.message : '请求失败，请稍后重试。';
}
function showReadError(error: unknown) {
  const message = failure(error);
  el('notice').textContent = message;
  transientNotice = !(error instanceof ApiError) || error.status >= 500 ? message : null;
}
async function api<T>(url: string, body?: unknown): Promise<T> {
  const response = await fetch(url, { method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store', headers: body === undefined ? {} : { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(10000) });
  const data = await response.json();
  if (!response.ok) throw new ApiError(data.error?.message || `请求失败 (${response.status})`, response.status);
  return data as T;
}
function node(tag: string, text: string, className?: string) {
  const result = document.createElement(tag); result.textContent = text;
  if (className) result.className = className;
  return result;
}
function acceptRevision(value: number) {
  if (!Number.isSafeInteger(value) || value < revision) return false;
  revision = value; el('revision').textContent = `版本 ${revision}`; return true;
}
function renderDetail(player: Player) {
  interviews?.selectPlayer(player);
  const container = el('detail-content'); container.replaceChildren();
  container.append(node('h3', player.display_name), node('p', `${player.provider} / ${player.identity_scope} / ${player.subject_id}`, 'identity'));
  const grid = node('dl', '', 'facts');
  for (const [label, value] of [['档案 ID', player.player_id], ['身份', player.role === 'guest' ? '游客 (guest)' : player.role], ['部门', player.department ?? '无'], ['岗位', player.position ?? '无'], ['绩效', String(player.performance)], ['登记时间', player.joined_at], ['更新时间', player.updated_at]]) {
    const pair = node('div', ''); pair.append(node('dt', label), node('dd', value)); grid.append(pair);
  }
  container.append(grid, node('h3', '最近经历'));
  if (!player.recent_events.length) container.append(node('p', '暂无经历。尚无已确认的职业事件。', 'empty'));
  else for (const event of player.recent_events) {
    if (event && typeof event === 'object') {
      const record = event as Record<string, unknown>;
      const title = record.type === 'interview_hired' ? '面试录用' : record.type === 'interview_rejected' ? '面试未录用' : '已确认职业事件';
      container.append(node('h3', title), node('p', typeof record.summary === 'string' ? record.summary : JSON.stringify(record), 'answer-copy'));
      if (typeof record.created_at === 'string') container.append(node('small', record.created_at));
    }
  }
}
async function loadDetail(id: string) {
  selected = id; const request = ++detailRequest;
  try {
    const data = await api<Snapshot>(`/api/players/${encodeURIComponent(id)}`);
    if (request !== detailRequest || selected !== id || !authenticated) return;
    if (acceptRevision(data.revision)) renderDetail(data.player);
  } catch (error) {
    if (request !== detailRequest || selected !== id) return;
    showReadError(error);
    el('detail-content').replaceChildren(node('p', '详情未能更新，请重新选择档案。', 'empty'));
  }
}
async function loadPlayers() {
  if (!authenticated || !healthy) return;
  const request = ++listRequest, version = queryVersion, term = query.value;
  try {
    const data = await api<{ players: Player[]; revision: number }>(`/api/players?q=${encodeURIComponent(term)}`);
    if (request !== listRequest || version !== queryVersion || !authenticated) return;
    if (!acceptRevision(data.revision)) return;
    const list = el('players'); list.replaceChildren();
    for (const player of data.players) {
      const item = node('li', '', 'player-row');
      const text = node('div', ''); text.append(node('strong', player.display_name), node('small', player.subject_id));
      const button = document.createElement('button'); button.type = 'button'; button.textContent = '查看详情'; button.setAttribute('aria-label', `查看详情 ${player.subject_id}`); button.addEventListener('click', () => void loadDetail(player.player_id));
      item.append(text, button); list.append(item);
    }
    el('list-status').textContent = data.players.length ? `${data.players.length} 份档案 · 已读取数据库快照` : '没有匹配的档案。可先登记一位游客。';
  } catch (error) {
    if (request !== listRequest || version !== queryVersion) return;
    el('list-status').textContent = `读取失败 · ${failure(error)}`;
  }
}
async function checkConnection() {
  if (checking || busy || authenticating) return;
  checking = true;
  const epoch = authEpoch;
  try {
    const session = await api<{ authenticated: boolean }>('/api/session');
    if (epoch !== authEpoch) return;
    authenticated = session.authenticated;
    if (!authenticated) {
      ++listRequest; ++detailRequest; selected = null;
      el('players').replaceChildren();
      el('detail-content').replaceChildren(node('p', '登录后查看档案详情。', 'empty'));
      el('list-status').textContent = '登录后读取真实档案。';
    }
    const health = await api<{ status: string; mode: string; world_id: string }>('/api/health');
    if (epoch !== authEpoch) return;
    healthy = health.status === 'ok' && health.mode === 'local-test';
    status(!authenticated ? '尚未登录 · 请从启动器的一次性链接进入。' : healthy ? `已连接真实 PostgreSQL · ${health.world_id} · local-test` : '数据库不可用或模式不符 · 登记暂停。');
    if (authenticated && healthy) {
      await loadPlayers();
      await interviews?.refresh();
      await competitions?.refresh();
      if (selected && authenticated && healthy) await loadDetail(selected);
      if (healthy && transientNotice !== null && el('notice').textContent === transientNotice) {
        el('notice').textContent = ''; transientNotice = null;
      }
    }
  } catch (error) { if (epoch === authEpoch) showReadError(error); }
  finally { checking = false; syncControls(); }
}
function editOperation() { pending = null; el('registration-message').textContent = '表单已修改；下一次提交将建立新的操作。'; syncControls(); }
subject.addEventListener('input', editOperation); nickname.addEventListener('input', editOperation);
el<HTMLFormElement>('register-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (!authenticated || !healthy || busy || competitions?.locked() || interviews?.locked() || !navigator.onLine) return;
  if (!subject.value || subject.value !== subject.value.trim() || [...subject.value].length > 128 || !nickname.value || [...nickname.value].length > 80) {
    el('registration-message').textContent = '请检查输入：身份 ID 不可有首尾空白，昵称须为 1–80 字符。'; return;
  }
  pending ??= { command_id: crypto.randomUUID(), provider: 'simulated', identity_scope: 'local-test', subject_id: subject.value, display_name: nickname.value };
  busy = true; syncControls();
  const epoch = authEpoch;
  try {
    const data = await api<Snapshot>('/api/players', pending);
    if (epoch !== authEpoch) return;
    el('registration-message').textContent = data.replayed ? '已确认原登记结果；没有重复创建档案。' : '登记成功，已保存到 PostgreSQL。';
    if (acceptRevision(data.revision)) { selected = data.player.player_id; ++detailRequest; renderDetail(data.player); }
    else el('notice').textContent = '原操作已确认，正在读取当前最新档案。';
    await loadPlayers(); await loadDetail(data.player.player_id);
  } catch (error) { if (epoch !== authEpoch) return; el('registration-message').textContent = `${failure(error)} 原操作编号和载荷已保留；恢复连接后可重试。修改表单会建立新操作。`; }
  finally { busy = false; syncControls(); }
});
query.addEventListener('input', () => { ++queryVersion; ++listRequest; el('players').replaceChildren(); el('list-status').textContent = '搜索条件已更改，等待读取。'; });
el<HTMLFormElement>('search-form').addEventListener('submit', event => { event.preventDefault(); void loadPlayers(); });
el('reconnect').addEventListener('click', () => void checkConnection());
window.addEventListener('offline', () => { healthy = false; status('浏览器离线 · 显示内容可能已陈旧，登记暂停。'); });
window.addEventListener('online', () => void checkConnection());
async function exchangeFragment() {
  // Hash-only navigation does not reload this module. Clear a new token synchronously
  // on both initial load and hashchange, before any asynchronous session exchange.
  const fragment = location.hash.slice(1);
  if (!fragment) return;
  history.replaceState(null, '', location.pathname + location.search);
  const token = fragment.includes('=') ? new URLSearchParams(fragment).get('token') : fragment;
  if (!token) return;
  const epoch = ++authEpoch;
  authenticating = true; authenticated = false; healthy = false;
  ++listRequest; ++detailRequest;
  el('players').replaceChildren(); el('detail-content').replaceChildren();
  status('正在验证一次性登录链接…');
  el('notice').textContent = ''; transientNotice = null;
  try { await api('/api/session', { token }); }
  catch (error) {
    if (epoch === authEpoch) el('notice').textContent = `登录失败：${failure(error)} 请重新生成启动链接。`;
  } finally {
    if (epoch === authEpoch) { authenticating = false; await checkConnection(); }
  }
}
window.addEventListener('hashchange', () => { void exchangeFragment(); });
async function start() {
  await exchangeFragment();
  await checkConnection();
  // Full snapshots, no overlapping polling requests. A changed query invalidates old responses.
  const poll = async () => { if (!document.hidden && !busy && navigator.onLine) await checkConnection(); window.setTimeout(() => void poll(), 1000); };
  window.setTimeout(() => void poll(), 1000);
}
interviews = setupInterview({ api, acceptRevision, epoch: () => authEpoch, authenticated: () => authenticated, ready: () => authenticated && healthy && navigator.onLine && !authenticating && !busy && !competitions?.locked(), failure, refreshPlayer: loadDetail });
competitions = setupCompetition({ api, acceptRevision, epoch: () => authEpoch, authenticated: () => authenticated, ready: () => authenticated && healthy && navigator.onLine && !authenticating && !busy && !interviews?.locked(), failure, refreshPlayer: loadDetail });
void start();
