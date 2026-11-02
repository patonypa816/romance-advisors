import { setupReceiver } from './receiver';
type Obj = Record<string, unknown>;
export type PerformanceSource = {kind: string; id: string; submission_id?: string};
type Segment = {text: string; intent: string; index?: number; state?: string; result?: unknown};
type Frozen = {source_ref: PerformanceSource & Obj; segments: Segment[]; content_hash: string};
type Media = {state: string; real_enabled: boolean; paid_enabled: boolean; creation_attempted: boolean; cloud_end_confirmed: boolean; owned_live_id: string | null; operator_confirmed: boolean; receiver: {claimed: boolean; healthy: boolean; joined: boolean; remote_video: boolean; audio_playback: boolean; autoplay_blocked: boolean; evidence_source: string}};
type Session = {session_id: string; mode: string; status: string; avatar_aspect: string | null};
type Performance = {performance_id: string; session_id: string; source_ref: Obj; payload_hash: string; expires_at: string; state: string; segments: Segment[]; evidence: Obj; [key: string]: unknown};
type Snapshot = {media?: Media; revision: number; server_time: string; session?: Session | null; performance?: Performance; performances?: Performance[]; stop_status?: string; cancel_outcome?: string};
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
  return typeof s.session_id === 'string' && ['offline_fixture','real'].includes(s.mode) && ['active','paused','closed','unknown'].includes(s.status) && [null,'16:9','4:3'].includes(s.avatar_aspect);
}

function validMedia(m: Media) {
  return ['not_started','creating','connecting','waiting_receiver','ready','stopping','ended','unknown'].includes(m.state) && ['real_enabled','paid_enabled','creation_attempted','cloud_end_confirmed','operator_confirmed'].every(k => typeof (m as unknown as Obj)[k] === 'boolean') && !!m.receiver && ['claimed','healthy','joined','remote_video','audio_playback','autoplay_blocked'].every(k => typeof (m.receiver as unknown as Obj)[k] === 'boolean') && m.receiver.evidence_source === 'client_reported';
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
  let media: Media | null = null;
  const receiver = setupReceiver({api:d.api,epoch:d.epoch,ready:d.ready,authenticated:d.authenticated,changed:() => {sync(); d.changed();}});
  let records: Performance[] = [], epoch = d.epoch(), request = 0, localEpoch = 0, fresh = false, busy = false, reading = false, offset = 0;
  let safetyBusy = false;
  let safetyPending: {action: 'stop' | 'reconcile'; sessionId: string; command_id: string} | null = null;
  let pending: {url: string; body: Obj; queryOnly: boolean} | null = null;
  const notice = (v: string) => {get('notice').textContent = v;};
  function sync() {
    if (epoch !== d.epoch() || !d.authenticated()) {
      epoch = d.epoch(); ++request; ++localEpoch; media = null; session = current = frozen = null; records = []; pending = null; safetyPending = null; fresh = false;
      get('frozen').replaceChildren(); get('detail').replaceChildren(); get('list').replaceChildren();
    }
    if (!d.ready()) fresh = false;
    receiver.sync(session?.mode === 'real' && !['closed','unknown'].includes(session.status) && !['ended','unknown','stopping'].includes(media?.state ?? '') ? session.session_id : null,d.authenticated());
    const e = d.ready() && fresh && !busy && !pending && !safetyPending;
    const real = session?.mode === 'real';
    get('mode-marker').textContent = real ? '真实媒体控制（未完成真实音画验收）' : '离线协议演练（不播放音画）';
    const realReady = !!media && media.state === 'ready' && media.receiver.healthy && media.operator_confirmed;
    get<HTMLButtonElement>('session-real-create').disabled = !e || !media?.real_enabled || !!session && session.status !== 'closed';
    get<HTMLButtonElement>('media-start').disabled = !e || !real || !media?.real_enabled || !media.paid_enabled || media.creation_attempted || !get<HTMLInputElement>('paid-confirm').checked;
    get<HTMLInputElement>('paid-confirm').disabled = !e || !real || !media?.paid_enabled || !!media.creation_attempted;
    get<HTMLButtonElement>('media-stop').disabled = !d.authenticated() || !navigator.onLine || safetyBusy && safetyPending?.action === 'stop' || !real || !!media?.cloud_end_confirmed;
    get<HTMLButtonElement>('media-stop').textContent = safetyPending?.action === 'stop' ? '原样确认停止云场请求' : '停止云场并回读';
    get<HTMLButtonElement>('media-reconcile').disabled = !d.authenticated() || !navigator.onLine || safetyBusy || !real || !media?.creation_attempted || !!safetyPending && safetyPending.action !== 'reconcile';
    get<HTMLButtonElement>('media-confirm-ready').disabled = !e || !real || !media?.receiver.healthy || !receiver.active() || !receiver.evidence().remote_video || !receiver.evidence().audio_playback;
    document.getElementById('receiver-start')!.toggleAttribute('disabled',!e || !real || !get<HTMLSelectElement>('aspect').value || !media?.owned_live_id || !['waiting_receiver','connecting','ready'].includes(media.state) || !receiver.canStart(session!.session_id));
    get('media-state').textContent = !media?.real_enabled ? '真实媒体未配置 / 禁用；不能启动云场' : `真实媒体 ${media.state} · 付费开关 ${media.paid_enabled ? '已启用（仍需本次授权）' : '关闭'} · 云场结束回读 ${media.cloud_end_confirmed ? '已确认' : '未确认'} · 接收者 ${media.receiver.healthy ? 'client_reported 就绪' : '未就绪'} · 人工确认 ${media.operator_confirmed ? '已记录（非验收）' : '尚无'}`;
    get<HTMLButtonElement>('submit').textContent = real ? '提交真实媒体演出（不自动执行）' : '提交离线演出';
    get<HTMLButtonElement>('execute').textContent = real ? '执行真实演出（可能产生模型 / TTS 费用）' : '执行离线协议演练（不播放音画）';
    get('freshness').textContent = fresh && d.ready() ? session?.mode === 'real' ? '服务器快照已确认 · 受控真实媒体（未验收）' : '服务器快照已确认 · LOCAL 离线协议' : '快照待确认 / 可能陈旧 · 写入暂停';
    get('session-state').textContent = session ? `LOCAL ${session.mode} · ${session.status} · ${session.session_id}` : '尚无 LOCAL 演练会话';
    for (const a of ['create','pause','resume','close']) get<HTMLButtonElement>(`session-${a}`).disabled = !e || (a === 'create' ? !!session && session.status !== 'closed' : !session || (a === 'pause' ? session.status !== 'active' : a === 'resume' ? session.status !== 'paused' || real && !realReady : session.status === 'closed' || (real ? (!media || media.creation_attempted && !media.cloud_end_confirmed) || records.some(p => ['sending','preparing'].includes(p.state)) : session.status === 'unknown' || records.some(p => ['sending','unknown'].includes(p.state)))));
    for (const id of ['source-kind','source-id','submission-id','aspect','expiry']) (get(id) as HTMLInputElement).disabled = busy || !!pending;
    get<HTMLButtonElement>('freeze').disabled = !e;
    get<HTMLButtonElement>('submit').disabled = !e || !frozen || session?.status !== 'active' || real && !realReady;
    get<HTMLButtonElement>('execute').disabled = !e || session?.status !== 'active' || !current || !['accepted','ready'].includes(current.state) || real && !realReady;
    get<HTMLButtonElement>('cancel').disabled = !e || !current || terminal.includes(current.state);
    get<HTMLButtonElement>('refresh').disabled = !d.ready() || busy || reading;
    get('replay').hidden = !pending || pending.queryOnly;
    get<HTMLButtonElement>('replay').disabled = !d.ready() || busy;
    for (const b of get('list').querySelectorAll<HTMLButtonElement>('button')) b.disabled = !e;
  }
  function render() {
    const list = get('list'); list.replaceChildren();
    for (const p of records) {const b = text('button', `${p.performance_id.slice(0,8)} · ${label(p.state,p.mode === 'real')}`) as HTMLButtonElement; b.type = 'button'; b.dataset.testid = 'performance-select'; b.addEventListener('click', () => {current = p; render();}); list.append(b);}
    get('detail').replaceChildren();
    if (current) {
      get('detail').append(text('h3', label(current.state,current.mode === 'real')), text('p', `演出 ID：${current.performance_id}`), text('p', `失效时间：${current.expires_at}`), text('p', `payload_hash：${current.payload_hash}`));
      for (const s of current.segments) get('detail').append(text('p', `${s.index ?? ''} · ${s.intent} · ${s.state ?? 'accepted'}\n${s.text}`));
      get('detail').append(text('pre', JSON.stringify({source_ref: current.source_ref, source_snapshot: current.source_snapshot, evidence: current.evidence, result: current.result, error_code: current.error_code}, null, 2)));
      if (current.state === 'unknown') get('detail').append(text('p',current.mode === 'real' ? '发送结果未知：本演出永久禁止重播。云场正向结束回读且无本机进行中任务后才可关闭会话；不会清除未知证据。' : '发送结果未知：禁止重播；会话不能关闭或新建。需要核查，不代表音画已停止。'));
    }
    sync();
  }
  function label(state: string, real = false) {return state === 'send_completed' ? real ? '真实传输发送完成（不代表观众听到 / 验收通过）' : '离线演练完成（不代表观众听到）' : state;}
  function accept(data: Snapshot) {
    if (!Number.isSafeInteger(data.revision) || !Number.isFinite(Date.parse(data.server_time))) throw new Error('演出快照格式无效');
    if (('session' in data && !validSession(data.session)) || ('performance' in data && !validPerformance(data.performance)) || ('performances' in data && (!Array.isArray(data.performances) || !data.performances.every(validPerformance)))) throw new Error('演出协议字段无效；写入保持关闭');
    if (!d.acceptRevision(data.revision)) {fresh = false; return false;}
    if (data.media) {if (!validMedia(data.media)) throw new Error('媒体状态格式无效；操作关闭'); media = data.media;}
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
      if (pending?.queryOnly && pending.url.endsWith('/media/start') && media?.creation_attempted) {pending = null; notice('已查回云场创建尝试状态；不会重试创建。未知状态须只读核查或人工供应商控制台核对。');}
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
      notice(outcome === 'in_flight' ? '当前段仍在发送；仅取消未开始部分，不保证音画完全停止。' : outcome === 'unknown' ? '停止结果未知，请核查；禁止重播或新建会话。' : outcome === 'unstarted_cancelled' ? '未开始部分已取消；不代表远端媒体停止。' : session?.mode === 'real' ? '真实媒体控制回执已收到；发送、浏览器上报与人工确认均不是观众听到的证明。停止云场须查看已结束的正向回读。' : '服务器操作已确认。此处仅离线协议演练，不播放音画。');
    } catch (err) {
      if (ae !== d.epoch() || le !== localEpoch) return;
      const status = (err as {status?: number}).status;
      if (status && status < 500) pending = null;
      fresh = false; notice(`${d.failure(err)} ${pending ? queryOnly ? url.endsWith('/media/start') ? '云场创建尝试待查；不自动重建。' : '执行结果待查；只查询，不自动重播。' : '原编号和完整载荷已保留，只可原样确认。' : '操作未采纳。'}`);
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
  async function safetyAction(action: 'stop' | 'reconcile') {
    if (!d.authenticated() || !navigator.onLine || session?.mode !== 'real' || (safetyBusy || safetyPending && safetyPending.action !== action) && !(action === 'stop' && safetyPending?.action === 'reconcile')) return;
    // A read-only reconcile must never prevent an urgent stop. Supersede only that safe read.
    if (action === 'stop' && safetyPending?.action === 'reconcile') safetyPending = null;
    safetyPending ??= {action,sessionId:session.session_id,command_id:crypto.randomUUID()};
    const command = safetyPending, ae = d.epoch(), le = localEpoch; safetyBusy = true; sync(); d.changed();
    // Do not overwrite an unresolved submit/execute/start command to stop safely.
    if (action === 'stop') void receiver.leave();
    try {
      const data = await d.api<Snapshot>(`/api/performance-session/${command.sessionId}/media/${action}`,{command_id:command.command_id});
      if (ae !== d.epoch() || le !== localEpoch || safetyPending !== command) return;
      if (!validSession(data.session) || !data.media || !validMedia(data.media)) throw new Error('停止 / 核查回执无效');
      if (accept(data)) session = data.session ?? null;
      safetyPending = null;
      notice(data.media.cloud_end_confirmed ? '供应商已结束的正向回读已确认；未知演出证据仍保留，不重播。' : '停止 / 核查请求已受理，但云场结束尚未确认。未知时只读核查或人工供应商控制台核对。');
    } catch (err) {if (ae === d.epoch() && le === localEpoch && safetyPending === command) {fresh = false; notice(`${d.failure(err)} 停止 / 核查结果待确认，保留原安全命令；原演出请求没有被覆盖。`);}}
    finally {if (safetyPending === command || !safetyPending) safetyBusy = false; sync(); d.changed(); if (d.ready()) await refresh();}
  }
  for (const action of ['stop','reconcile'] as const) get(`media-${action}`).addEventListener('click',() => void safetyAction(action));
  get('paid-confirm').addEventListener('change',sync);
  get('session-real-create').addEventListener('click',() => {if (d.ready() && fresh && !pending && !busy && media?.real_enabled && (!session || session.status === 'closed')) {get<HTMLInputElement>('paid-confirm').checked = false; void mutate('/api/performance-session',{command_id:crypto.randomUUID(),session_id:crypto.randomUUID(),mode:'real',avatar_aspect:get<HTMLSelectElement>('aspect').value || null});}});
  for (const action of ['start','confirm-ready']) get(`media-${action}`).addEventListener('click',() => {
    if (!d.ready() || !fresh || pending || busy || session?.mode !== 'real' || get<HTMLButtonElement>(`media-${action}`).disabled) return;
    const body: Obj = {command_id:crypto.randomUUID()};
    if (action === 'start') {body.confirm_paid = true; get<HTMLInputElement>('paid-confirm').checked = false;}
    if (action === 'confirm-ready') {body.receiver_id = receiver.receiverId; body.confirm_audio_video = true;}
    void mutate(`/api/performance-session/${session.session_id}/media/${action}`,body,action === 'start');
  });
  document.getElementById('receiver-start')!.addEventListener('click',() => {if (session?.mode === 'real' && fresh && !pending && !busy && d.ready()) void receiver.start(session.session_id);});
  for (const action of ['create','pause','resume','close']) get(`session-${action}`).addEventListener('click', () => {
    if (!fresh || pending || busy || !d.ready()) return;
    if (action === 'create') void mutate('/api/performance-session',{command_id:crypto.randomUUID(),session_id:crypto.randomUUID(),mode:'offline_fixture',avatar_aspect:get<HTMLSelectElement>('aspect').value || null});
    else if (session) void mutate(`/api/performance-session/${session.session_id}/${action}`,{command_id:crypto.randomUUID()});
  });
  for (const action of ['execute','cancel']) get(action).addEventListener('click', () => {if (current && fresh && !pending && !busy && d.ready() && current.state !== 'unknown') void mutate(`/api/performances/${current.performance_id}/${action}`,{command_id:crypto.randomUUID()},action === 'execute');});
  for (const id of ['source-kind','source-id','submission-id']) get(id).addEventListener('change', () => {frozen = null; get('frozen').replaceChildren(); sync();});
  (document.getElementById('receiver-video') as HTMLVideoElement).hidden = !get<HTMLSelectElement>('aspect').value;
  get('aspect').addEventListener('change', () => {const ratio = get<HTMLSelectElement>('aspect').value; get('avatar-placeholder').style.aspectRatio = ratio.replace(':',' / '); get('avatar-placeholder').hidden = !ratio; const video = document.getElementById('receiver-video') as HTMLVideoElement; video.style.aspectRatio = ratio.replace(':',' / '); video.hidden = !ratio; video.setAttribute('aria-label', ratio ? `数字人横向 RTC 接收区域 ${ratio}` : '数字人横向 RTC 接收区域（比例未选）'); get('avatar-placeholder').textContent = ratio ? `数字人横向子区域 ${ratio} · 仅比例示意（不是视频）` : ''; sync();});
  window.addEventListener('offline', () => {++localEpoch; ++request; fresh = false; sync();});
  return {refresh,sync,locked: () => busy || safetyBusy || !!pending || !!safetyPending, freezeSource: async (source: PerformanceSource) => {get('heading').scrollIntoView({block:'start'}); await freeze(source);}};
}
