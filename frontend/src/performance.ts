type Obj = Record<string, unknown>;
export type PerformanceSource = {kind: string; id: string; submission_id?: string};
type Segment = {text: string; intent: string; index?: number; state?: string; result?: unknown};
type Frozen = {source_ref: PerformanceSource & Obj; segments: Segment[]; content_hash: string};
type Session = {session_id: string; mode: string; status: string; avatar_aspect: string | null};
type Performance = {performance_id: string; session_id: string; source_ref: Obj; payload_hash: string; expires_at: string; state: string; segments: Segment[]; evidence: Obj; [key: string]: unknown};
type Snapshot = {revision: number; server_time: string; session?: Session | null; performance?: Performance; performances?: Performance[]; stop_status?: string; cancel_outcome?: string};
type Dependencies = {api: <T>(url: string, body?: unknown) => Promise<T>; acceptRevision: (revision: number) => boolean; epoch: () => number; ready: () => boolean; authenticated: () => boolean; failure: (error: unknown) => string; changed: () => void};
const get = <T extends HTMLElement>(id: string) => document.getElementById(`performance-${id}`) as T;
const text = (tag: string, value: string) => {const n = document.createElement(tag); n.textContent = value; return n;};
const terminal = ['send_completed','failed','cancelled','expired','unknown'];
const states = ['accepted','preparing','ready','sending',...terminal];
function validPerformance(p: unknown): p is Performance {
  if (!p || typeof p !== 'object') return false;
  const v = p as Performance;
  return typeof v.performance_id === 'string' && typeof v.session_id === 'string' && /^[0-9a-f]{64}$/.test(v.payload_hash) && states.includes(v.state) && Number.isFinite(Date.parse(v.expires_at)) && Array.isArray(v.segments) && v.segments.every(s => typeof s.text === 'string' && typeof s.intent === 'string' && states.includes(s.state ?? '')) && !!v.evidence && v.evidence.media_verified === false && v.evidence.audience_heard === false;
}
function validSession(v: unknown): v is Session | null {
  if (v === null) return true;
  if (!v || typeof v !== 'object') return false;
  const s = v as Session;
  return typeof s.session_id === 'string' && s.mode === 'offline_fixture' && ['active','paused','closed','unknown'].includes(s.status) && [null,'16:9','4:3'].includes(s.avatar_aspect);
}

function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value !== null && typeof value === 'object') return `{${Object.entries(value).sort(([a],[b]) => a < b ? -1 : a > b ? 1 : 0).map(([k,v]) => `${JSON.stringify(k)}:${canonical(v)}`).join(',')}}`;
  return JSON.stringify(value);
}
async function hash(value: unknown) {return [...new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(canonical(value))))].map(n => n.toString(16).padStart(2,'0')).join('');}
export function performancePauseMessage(value: unknown): string {
  const status = value && typeof value === 'object' ? (value as {stop_status?: string}).stop_status : undefined;
  return status === 'unstarted_cancelled' ? '游戏已暂停；演出未开始部分已取消，不代表远端音画完全停止。' : status === 'in_flight' ? '游戏已暂停；演出当前段仍在发送，未开始部分已取消。' : '游戏暂停回执已收到；演出停止状态待核查，不视为完全停止。';
}
export function setupPerformance(d: Dependencies) {
  let session: Session | null = null, current: Performance | null = null, frozen: Frozen | null = null;
  let records: Performance[] = [], epoch = d.epoch(), request = 0, localEpoch = 0, fresh = false, busy = false, reading = false, offset = 0;
  let pending: {url: string; body: Obj; queryOnly: boolean} | null = null;
  const notice = (v: string) => {get('notice').textContent = v;};
  function sync() {
    if (epoch !== d.epoch() || !d.authenticated()) {
      epoch = d.epoch(); ++request; ++localEpoch; session = current = frozen = null; records = []; pending = null; fresh = false;
      get('frozen').replaceChildren(); get('detail').replaceChildren(); get('list').replaceChildren();
    }
    if (!d.ready()) fresh = false;
    const e = d.ready() && fresh && !busy && !pending;
    get('freshness').textContent = fresh && d.ready() ? '服务器快照已确认 · LOCAL 离线协议' : '快照待确认 / 可能陈旧 · 写入暂停';
    get('session-state').textContent = session ? `LOCAL ${session.mode} · ${session.status} · ${session.session_id}` : '尚无 LOCAL 演练会话';
    for (const a of ['create','pause','resume','close']) get<HTMLButtonElement>(`session-${a}`).disabled = !e || (a === 'create' ? !!session && session.status !== 'closed' : !session || (a === 'pause' ? session.status !== 'active' : a === 'resume' ? session.status !== 'paused' : session.status === 'closed' || session.status === 'unknown' || records.some(p => ['sending','unknown'].includes(p.state))));
    for (const id of ['source-kind','source-id','submission-id','aspect','expiry']) (get(id) as HTMLInputElement).disabled = busy || !!pending;
    get<HTMLButtonElement>('freeze').disabled = !e;
    get<HTMLButtonElement>('submit').disabled = !e || !frozen || session?.status !== 'active';
    get<HTMLButtonElement>('execute').disabled = !e || session?.status !== 'active' || !current || !['accepted','ready'].includes(current.state);
    get<HTMLButtonElement>('cancel').disabled = !e || !current || terminal.includes(current.state);
    get<HTMLButtonElement>('refresh').disabled = !d.ready() || busy || reading;
    get('replay').hidden = !pending || pending.queryOnly;
    get<HTMLButtonElement>('replay').disabled = !d.ready() || busy;
    for (const b of get('list').querySelectorAll<HTMLButtonElement>('button')) b.disabled = !e;
  }
  function render() {
    const list = get('list'); list.replaceChildren();
    for (const p of records) {const b = text('button', `${p.performance_id.slice(0,8)} · ${label(p.state)}`) as HTMLButtonElement; b.type = 'button'; b.dataset.testid = 'performance-select'; b.addEventListener('click', () => {current = p; render();}); list.append(b);}
    get('detail').replaceChildren();
    if (current) {
      get('detail').append(text('h3', label(current.state)), text('p', `演出 ID：${current.performance_id}`), text('p', `失效时间：${current.expires_at}`), text('p', `payload_hash：${current.payload_hash}`));
      for (const s of current.segments) get('detail').append(text('p', `${s.index ?? ''} · ${s.intent} · ${s.state ?? 'accepted'}\n${s.text}`));
      get('detail').append(text('pre', JSON.stringify({source_ref: current.source_ref, source_snapshot: current.source_snapshot, evidence: current.evidence, result: current.result, error_code: current.error_code}, null, 2)));
      if (current.state === 'unknown') get('detail').append(text('p','发送结果未知：禁止重播；会话不能关闭或新建。需要核查，不代表音画已停止。'));
    }
    sync();
  }
  function label(state: string) {return state === 'send_completed' ? '离线演练完成（不代表观众听到）' : state;}
  function accept(data: Snapshot) {
    if (!Number.isSafeInteger(data.revision) || !Number.isFinite(Date.parse(data.server_time))) throw new Error('演出快照格式无效');
    if (('session' in data && !validSession(data.session)) || ('performance' in data && !validPerformance(data.performance)) || ('performances' in data && (!Array.isArray(data.performances) || !data.performances.every(validPerformance)))) throw new Error('演出协议字段无效；写入保持关闭');
    if (!d.acceptRevision(data.revision)) {fresh = false; return false;}
    offset = Date.parse(data.server_time) - Date.now(); fresh = true; return true;
  }
  async function refresh() {
    sync(); if (!d.ready() || busy || reading) return;
    reading = true; const r = ++request, ae = d.epoch(), le = localEpoch;
    try {
      const data = await d.api<Snapshot>('/api/performance-session');
      if (r !== request || ae !== d.epoch() || le !== localEpoch || !d.ready()) return;
      if (!accept(data)) return;
      session = data.session ?? null;
      const list = await d.api<Snapshot>(`/api/performances${session ? `?session_id=${encodeURIComponent(session.session_id)}` : ''}`);
      if (r !== request || ae !== d.epoch() || le !== localEpoch || !d.ready()) return;
      if (!accept(list) || !Array.isArray(list.performances)) {fresh = false; return;}
      records = list.performances;
      if (current) current = records.find(p => p.performance_id === current!.performance_id) ?? current;
      if (pending?.url === '/api/performances' && records.some(p => p.performance_id === pending!.body.performance_id && p.payload_hash === pending!.body.payload_hash)) {current = records.find(p => p.performance_id === pending!.body.performance_id)!; pending = null; notice('已查回原提交，未创建新演出。');}
      if (pending?.queryOnly && current && pending.url.includes(current.performance_id)) {pending = null; notice('执行请求回执曾丢失；已查询持久状态。不会自动重播。');}
      render();
    } catch (err) {if (ae === d.epoch() && le === localEpoch) {fresh = false; notice(d.failure(err));}}
    finally {reading = false; sync(); d.changed();}
  }
  async function mutate(url: string, body: Obj, queryOnly = false) {
    if (!d.ready() || busy) return;
    pending = {url,body,queryOnly}; busy = true; ++request; const ae = d.epoch(), le = localEpoch; sync(); d.changed();
    try {
      const data = await d.api<Snapshot>(url, body);
      if (ae !== d.epoch() || le !== localEpoch || !d.ready()) return;
      if (url.startsWith('/api/performances') ? !validPerformance(data.performance) : !('session' in data) || !validSession(data.session)) throw new Error('操作回执格式无效；保留原请求待确认');
      const accepted = accept(data);
      pending = null;
      if (accepted) {if ('session' in data) session = data.session ?? null; if (data.performance) current = data.performance;}
      const outcome = data.cancel_outcome ?? data.stop_status;
      notice(outcome === 'in_flight' ? '当前段仍在发送；仅取消未开始部分，不保证音画完全停止。' : outcome === 'unknown' ? '停止结果未知，请核查；禁止重播或新建会话。' : outcome === 'unstarted_cancelled' ? '未开始部分已取消；不代表远端媒体停止。' : '服务器操作已确认。此处仅离线协议演练，不播放音画。');
    } catch (err) {
      if (ae !== d.epoch() || le !== localEpoch) return;
      const status = (err as {status?: number}).status;
      if (status && status < 500) pending = null;
      fresh = false; notice(`${d.failure(err)} ${pending ? queryOnly ? '执行结果待查；只查询，不自动重播。' : '原编号和完整载荷已保留，只可原样确认。' : '操作未采纳。'}`);
    } finally {busy = false; render(); d.changed(); if (d.ready()) await refresh();}
  }
  async function freeze(source?: PerformanceSource) {
    if (!d.ready() || !fresh || busy || pending) return;
    if (source) {get<HTMLSelectElement>('source-kind').value = source.kind; get<HTMLInputElement>('source-id').value = source.id; get<HTMLInputElement>('submission-id').value = source.submission_id ?? '';}
    const kind = get<HTMLSelectElement>('source-kind').value;
    const s = source ?? {kind, id: kind === 'fixed_script' ? 'm4-demo-v1' : get<HTMLInputElement>('source-id').value, ...(kind === 'competition_comment' ? {submission_id: get<HTMLInputElement>('submission-id').value} : {})};
    frozen = null; get('frozen').replaceChildren(); busy = true; const ae = d.epoch(), le = localEpoch; sync(); d.changed();
    try {
      const data = await d.api<Frozen & Snapshot>('/api/performance-sources/freeze', {source:s});
      if (ae !== d.epoch() || le !== localEpoch || !d.ready() || !accept(data)) return;
      if (!data.source_ref || !Array.isArray(data.segments) || !data.segments.every(v => typeof v.text === 'string' && typeof v.intent === 'string') || !/^[0-9a-f]{64}$/.test(data.content_hash)) throw new Error('冻结载荷格式无效');
      frozen = data;
      get('frozen').append(text('h3','服务器冻结原文（不可编辑）'), text('pre', JSON.stringify(data.source_ref,null,2)));
      data.segments.forEach((s,i) => get('frozen').append(text('p', `${i + 1} · ${s.intent}\n${s.text}`)));
      get('frozen').append(text('p', `content_hash：${data.content_hash}`)); notice('冻结完成；提交时服务器重新核对来源与原文。不会修改档案、确认作品展示或开启投票。');
    } catch (err) {if (ae === d.epoch() && le === localEpoch) notice(d.failure(err));}
    finally {busy = false; sync(); d.changed();}
  }
  get('freeze').addEventListener('click', () => void freeze());
  get('refresh').addEventListener('click', () => void refresh());
  get('replay').addEventListener('click', () => {if (pending && !pending.queryOnly) void mutate(pending.url,pending.body);});
  get('submit').addEventListener('click', async () => {
    if (!frozen || session?.status !== 'active' || !fresh || !d.ready() || busy || pending) return;
    const seconds = Number(get<HTMLInputElement>('expiry').value);
    if (!Number.isInteger(seconds) || seconds < 30 || seconds > 900) {notice('有效期须为 30–900 秒。'); return;}
    const value = {session_id:session.session_id,source_ref:frozen.source_ref,segments:frozen.segments.map(({text,intent}) => ({text,intent})),expires_at:new Date(Date.now()+offset+seconds*1000).toISOString()};
    const ae = d.epoch(), le = localEpoch; busy = true; sync(); d.changed();
    try {const payload_hash = await hash(value); if (ae !== d.epoch() || le !== localEpoch || !d.ready()) return; busy = false; await mutate('/api/performances',{performance_id:crypto.randomUUID(),...value,payload_hash});}
    catch (err) {notice(d.failure(err));} finally {busy = false; sync(); d.changed();}
  });
  for (const action of ['create','pause','resume','close']) get(`session-${action}`).addEventListener('click', () => {
    if (!fresh || pending || busy || !d.ready()) return;
    if (action === 'create') void mutate('/api/performance-session',{command_id:crypto.randomUUID(),session_id:crypto.randomUUID(),mode:'offline_fixture',avatar_aspect:get<HTMLSelectElement>('aspect').value || null});
    else if (session) void mutate(`/api/performance-session/${session.session_id}/${action}`,{command_id:crypto.randomUUID()});
  });
  for (const action of ['execute','cancel']) get(action).addEventListener('click', () => {if (current && fresh && !pending && !busy && d.ready() && current.state !== 'unknown') void mutate(`/api/performances/${current.performance_id}/${action}`,{command_id:crypto.randomUUID()},action === 'execute');});
  for (const id of ['source-kind','source-id','submission-id']) get(id).addEventListener('change', () => {frozen = null; get('frozen').replaceChildren(); sync();});
  get('aspect').addEventListener('change', () => {const ratio = get<HTMLSelectElement>('aspect').value; get('avatar-placeholder').style.aspectRatio = ratio.replace(':',' / '); get('avatar-placeholder').hidden = !ratio; get('avatar-placeholder').textContent = ratio ? `数字人横向子区域 ${ratio} · 仅比例示意（不是视频）` : '';});
  window.addEventListener('offline', () => {++localEpoch; ++request; fresh = false; sync();});
  return {refresh,sync,locked: () => busy || !!pending, freezeSource: async (source: PerformanceSource) => {get('heading').scrollIntoView({block:'start'}); await freeze(source);}};
}
