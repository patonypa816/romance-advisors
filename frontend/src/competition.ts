import { performancePauseMessage, type PerformanceSource } from './performance';
type Obj = Record<string, unknown>;
type Person = {player_id: string; display_name: string; role?: string};
type Work = {id: string; player: Person; revision: number; text: string; task: {id: string; status: string; attempt_no: number; error_code: string | null} | null; comment: string | null; display_confirmed_at: string | null; evaluation: unknown};
type Competition = {id: string; version: number; status: string; paused_from: string | null; pause_reason: string | null; rule: Obj; deadline_at: string | null; submissions: Work[]; allowed_actions: string[]; result: {settlement_id: string; awards: {player_id: string; submission_id: string; award_type: string; performance_delta: number}[]; rankings: {submission_id: string; total: number; votes: number}[]} | null};
type Envelope = {task_id: string; attempt_no: number; input_hash: string; policy_hash: string; frozen_input: Obj};
type Dependencies = {freezePerformance?: (source: PerformanceSource) => Promise<void> | undefined; api: <T>(url: string, body?: unknown) => Promise<T>; acceptRevision: (revision: number) => boolean; epoch: () => number; ready: () => boolean; authenticated: () => boolean; failure: (error: unknown) => string; refreshPlayer: (id: string) => Promise<void>};
const get = <T extends HTMLElement>(id: string) => document.getElementById(`competition-${id}`) as T;
const obj = (v: unknown): v is Obj => typeof v === 'object' && v !== null && !Array.isArray(v);
const integer = (v: unknown): v is number => Number.isSafeInteger(v) && Number(v) >= 0;
const text = (tag: string, value: string, cls = '') => { const n = document.createElement(tag); n.textContent = value; n.className = cls; return n; };
const states = ['READY','SUBMITTING','REVIEWING','WAITING_REVIEW','DISPLAYING','VOTING','READY_TO_SETTLE','COMPLETED','PAUSED','CANCELLED','EXPIRED'];
function valid(v: unknown): v is Competition {
  return obj(v) && typeof v.id === 'string' && integer(v.version) && states.includes(String(v.status)) && obj(v.rule) && Array.isArray(v.allowed_actions) && v.allowed_actions.every(a => typeof a === 'string') && Array.isArray(v.submissions) && v.submissions.length <= 12 && v.submissions.every(s => obj(s) && typeof s.id === 'string' && obj(s.player) && typeof s.player.player_id === 'string' && typeof s.player.display_name === 'string' && integer(s.revision) && typeof s.text === 'string' && (s.comment === null || typeof s.comment === 'string') && (s.task === null || obj(s.task) && typeof s.task.id === 'string' && typeof s.task.status === 'string' && integer(s.task.attempt_no))) && (v.status === 'COMPLETED' ? obj(v.result) && Array.isArray(v.result.awards) && Array.isArray(v.result.rankings) : v.result === null);
}
export function setupCompetition(d: Dependencies) {
  let current: Competition | null = null, chosen: string | null = null;
  let epoch = d.epoch(), request = 0, voteRequest = 0, busy = false, reading = false, fresh = false, serverOffset = 0;
  let pending: {url: string; body: Obj} | null = null, exported: {work: string; envelope: Envelope} | null = null;
  let authorRevision = 0;
  let voterState: {player_id: string; vote_revision: number; submission_id: string | null} | null = null;
  const notice = (value: string) => { get('notice').textContent = value; };
  const allowed = (action: string) => !!current?.allowed_actions.includes(action);
  const enabled = () => d.ready() && fresh && !busy && !pending;
  const displayable = () => !!current && ['DISPLAYING','VOTING','READY_TO_SETTLE','COMPLETED'].includes(current.status) && current.submissions.every(w => typeof w.comment === 'string');
  function clearExport() { exported = null; get<HTMLTextAreaElement>('evaluation-request').value = ''; get<HTMLTextAreaElement>('evaluation-result').value = ''; }
  function controls() {
    if (epoch !== d.epoch() || !d.ready()) {
      fresh = false;
      if (epoch !== d.epoch() || !d.authenticated()) {
        epoch = d.epoch(); ++request; ++voteRequest; current = null; chosen = null; voterState = null; clearExport();
        for (const id of ['card','works','display','result','list']) get(id).replaceChildren();
        for (const id of ['author','voter','candidate']) get<HTMLSelectElement>(id).replaceChildren(new Option('登录后选择', ''));
        get<HTMLTextAreaElement>('text').value = '';
      }
    }
    const e = enabled();
    for (const b of document.querySelectorAll<HTMLButtonElement>('[data-performance-source]')) b.disabled = !e;
    get<HTMLButtonElement>('create').disabled = !e || !!current && !['COMPLETED','CANCELLED','EXPIRED'].includes(current.status);
    for (const a of ['open','close-submissions','open-voting','close-voting','settle','pause','resume','cancel']) get<HTMLButtonElement>(a).disabled = !e || !allowed(a);
    get<HTMLButtonElement>('open-voting').disabled ||= !displayable() || !current?.submissions.length || !current.submissions.every(w => !!w.display_confirmed_at);
    for (const id of ['author','text','submit']) (get(id) as HTMLInputElement).disabled = !e || !allowed('submit');
    get<HTMLButtonElement>('submit').disabled ||= !get<HTMLSelectElement>('author').value;
    get<HTMLSelectElement>('voter').disabled = !e || !allowed('vote');
    get<HTMLSelectElement>('candidate').disabled = !e || !allowed('vote') || !voterState;
    get<HTMLButtonElement>('vote').disabled = !e || !allowed('vote') || !voterState || !get<HTMLSelectElement>('candidate').value;
    get<HTMLInputElement>('cancel-reason').disabled = !e;
    get<HTMLTextAreaElement>('evaluation-result').disabled = !e || !exported;
    get<HTMLButtonElement>('import').disabled = !e || !exported;
    get<HTMLButtonElement>('replay').disabled = !d.ready() || busy || !pending;
    get('replay').hidden = !pending;
    get('freshness').textContent = e ? '服务器快照已确认' : pending ? '原操作结果待确认 · 其他比赛写入已锁定' : '状态待确认 / 快照可能陈旧 · 操作暂停';
    for (const b of document.querySelectorAll<HTMLButtonElement>('.competition [data-work-action]')) {
      const work = current?.submissions.find(w => w.id === b.dataset.submissionId), a = b.dataset.workAction;
      b.disabled = !e || !work || (a === 'export' ? !['REVIEWING','WAITING_REVIEW'].includes(current!.status) || work.task?.status !== 'awaiting_result' : a === 'retry' ? !allowed('retry') || !['timeout','invalid_schema','invalid_evidence','interrupted'].includes(work.task?.status ?? '') : !allowed('confirm-display') || !displayable() || !!work.display_confirmed_at);
    }
    for (const b of get('list').querySelectorAll<HTMLButtonElement>('button')) b.disabled = !e;
  }
  function freezeButton(kind: string, id: string, submission_id?: string) {const b = text('button',kind === 'competition_comment' ? '冻结短评到离线演出' : '冻结比赛结果到离线演出') as HTMLButtonElement; b.type = 'button'; b.dataset.performanceSource = kind; b.dataset.testid = `competition-freeze-${kind === 'competition_comment' ? 'comment' : 'result'}`; if (submission_id) b.dataset.submissionId = submission_id; b.addEventListener('click', () => {if (enabled()) void d.freezePerformance?.({kind,id,...(submission_id ? {submission_id} : {})});}); return b;}
  function workButton(w: Work, action: string, label: string) {
    const b = text('button', label) as HTMLButtonElement; b.type = 'button'; b.dataset.testid = `competition-${action === 'export' ? 'export' : action}`; b.dataset.submissionId = w.id; b.dataset.workAction = action;
    b.addEventListener('click', () => action === 'export' ? void exportWork(w.id) : void lifecycle(action, w.id)); return b;
  }
  function render() {
    for (const id of ['card','works','display','result']) get(id).replaceChildren();
    if (!current) { get('card').append(text('p','创建一轮创意赛，或选择最近比赛。','empty')); controls(); return; }
    const s = current;
    const state = text('p', `${s.status} · 比赛版本 ${s.version} · ${s.submissions.length} / 12 份作品`, 'chip'); state.id = 'competition-status'; state.dataset.testid = state.id;
    get('card').append(state, text('p', String(s.rule.prompt ?? s.rule.question ?? ''), 'question'), text('p', `比赛 ID：${s.id}`, 'identity'));
    if (s.status === 'PAUSED') get('card').append(text('p',`已暂停：${s.pause_reason ?? '人工暂停'}。恢复投稿 / 投票会重开完整测试窗口；中断评价需逐稿重试。`,'feedback'));
    for (const w of s.submissions) {
      const card = text('article','', 'competition-work'); card.dataset.submissionId = w.id;
      card.append(text('h3',`${w.player.display_name} · 稿件版本 ${w.revision}`),text('p',w.text,'answer-copy'));
      if (w.task) card.append(text('p',`评估尝试 ${w.task.attempt_no} · ${w.task.status}${w.task.error_code ? ` · ${w.task.error_code}` : ''}`,'muted'));
      card.append(workButton(w,'export','导出此稿评估任务'),workButton(w,'retry','仅重试此稿失败评估')); get('works').append(card);
      if (displayable()) {
        const display = text('article','','competition-work'); display.dataset.submissionId = w.id; display.dataset.testid = 'competition-display-work';
        display.append(text('h3',w.player.display_name),text('p',w.text,'answer-copy'),text('h4','ChatGPT 描述性短评'),text('p',w.comment!,'answer-copy'),workButton(w,'confirm-display',w.display_confirmed_at ? '此稿与短评已确认展示' : '已核对短评，确认此完整作品与短评已在本页展示')); get('display').append(display);
        if (d.freezePerformance) display.append(freezeButton('competition_comment', s.id, w.id));
      }
    }
    if (!displayable()) get('display').append(text('p','展示尚未开放。必须全部作品评价有效采纳，才可逐稿展示与确认；不以零分代替失败。','empty'));
    const candidate = get<HTMLSelectElement>('candidate'), saved = candidate.value;
    candidate.replaceChildren(new Option('请选择作品',''));
    if (displayable()) for (const w of s.submissions) candidate.add(new Option(`${s.submissions.indexOf(w) + 1}. ${w.player.display_name} · ${w.id.slice(0,8)}`,w.id));
    candidate.value = saved || voterState?.submission_id || '';
    if (s.status === 'COMPLETED' && s.result) {
      if (d.freezePerformance) get('result').append(freezeButton('competition_result', s.id));
      get('result').append(text('h3','服务器整轮结算结果'),text('p',`结算 ID：${s.result.settlement_id}`,'identity'));
      for (const a of s.result.awards) get('result').append(text('p',`${s.submissions.find(w => w.id === a.submission_id)?.player.display_name ?? a.player_id} · ${a.award_type} · 绩效变化 ${a.performance_delta >= 0 ? '+' : ''}${a.performance_delta}`));
      for (const r of s.result.rankings) get('result').append(text('p',`${s.submissions.find(w => w.id === r.submission_id)?.player.display_name ?? r.submission_id} · AI 总分 ${r.total} · 最终票数 ${r.votes}`));
      for (const w of s.submissions) if (w.evaluation) get('result').append(text('pre', JSON.stringify(w.evaluation,null,2),'answer-copy'));
      get('result').append(text('p','公司资产变化：无。游客仅获荣誉与记录；符合冻结资格的员工每奖绩效 +1。','muted'));
    }
    controls();
  }
  function adopt(data: unknown) {
    if (!obj(data) || !integer(data.revision) || !valid(data.competition)) throw new Error('比赛快照格式无效，操作暂停。');
    const s = data.competition;
    if (chosen && s.id !== chosen || current?.id === s.id && s.version < current.version) return false;
    if (!d.acceptRevision(data.revision)) { fresh = false; controls(); return false; }
    if (typeof data.server_time === 'string' && Number.isFinite(Date.parse(data.server_time))) serverOffset = Date.parse(data.server_time) - Date.now();
    if (exported) { const w = s.submissions.find(w => w.id === exported!.work); if (w?.task?.id !== exported.envelope.task_id || w.task.attempt_no !== exported.envelope.attempt_no || w.task.status !== 'awaiting_result' || !['REVIEWING','WAITING_REVIEW'].includes(s.status)) clearExport(); }
    if (current?.id !== s.id) { authorRevision = 0; get<HTMLSelectElement>('author').value = ''; get<HTMLTextAreaElement>('text').value = ''; voterState = null; ++voteRequest; get<HTMLSelectElement>('voter').value = ''; }
    current = s; chosen = s.id; fresh = true; render(); return true;
  }
  async function refresh() {
    controls(); if (!d.ready() || busy || reading) return;
    reading = true; const generation = ++request, auth = d.epoch();
    try {
      const data = await d.api<unknown>('/api/competitions');
      if (generation !== request || auth !== d.epoch() || !d.ready()) return;
      if (!obj(data) || !integer(data.revision) || !Array.isArray(data.competitions) || !data.competitions.every(valid)) throw new Error('比赛列表格式无效。');
      if (!d.acceptRevision(data.revision)) { fresh = false; return; }
      get('list').replaceChildren();
      for (const s of data.competitions as Competition[]) {
        const b = text('button',`${s.status} · ${s.id.slice(0,8)}`) as HTMLButtonElement; b.type = 'button'; b.setAttribute('aria-label',`查看创意赛 ${s.id}`);
        b.addEventListener('click', () => { if (!enabled()) return; chosen = s.id; current = null; voterState = null; ++voteRequest; clearExport(); fresh = false; ++request; render(); void refresh(); }); get('list').append(b);
      }
      if (!chosen && data.competitions.length) chosen = (data.competitions[0] as Competition).id;
      if (chosen) { const detail = await d.api<unknown>(`/api/competitions/${chosen}`); if (generation !== request || auth !== d.epoch() || !d.ready()) return; adopt(detail); } else { fresh = true; render(); }
      const people = await d.api<{players: Person[]; revision: number}>('/api/players?q=');
      if (generation !== request || auth !== d.epoch() || !d.ready()) return;
      if (!Array.isArray(people.players) || !integer(people.revision)) throw new Error('档案列表格式无效。');
      if (!d.acceptRevision(people.revision)) { fresh = false; return; }
      for (const id of ['author','voter']) { const sel = get<HTMLSelectElement>(id), saved = sel.value; sel.replaceChildren(new Option('请选择已登记档案','')); for (const p of people.players) sel.add(new Option(`${p.display_name} · ${p.role === 'guest' ? '游客' : '员工'} · ${p.player_id.slice(0,8)}`,p.player_id)); sel.value = saved; }
      if (current?.status === 'VOTING' && get<HTMLSelectElement>('voter').value && !voterState) await loadVote();
    } catch (e) { if (generation === request && auth === d.epoch()) { fresh = false; notice(d.failure(e)); } }
    finally { reading = false; controls(); }
  }
  async function submit(url?: string, body?: Obj) {
    if (!d.ready() || busy || (!pending && !fresh)) return;
    if (!pending && url && body) pending = {url,body: structuredClone(body)};
    if (!pending) return;
    const frozen = pending, auth = d.epoch(); busy = true; ++request; controls();
    try {
      const data = await d.api<unknown>(frozen.url,frozen.body);
      if (auth !== d.epoch()) return;
      if (!obj(data) || !integer(data.revision)) throw new Error('回执格式无效，结果未知。');
      if (frozen.url.endsWith('/submissions') || frozen.url.endsWith('/votes')) {
        if (typeof data.accepted !== 'boolean') throw new Error('接纳回执格式无效，结果未知。');
        notice(data.accepted ? data.replayed ? '原输入已确认，未重复写入。' : '输入已由服务器接纳。' : `输入未接纳：${String(data.rejection_code)}。若版本冲突，请重新选择作者载入最新稿再编辑。`);
        if (data.accepted && frozen.url.endsWith('/submissions') && integer(data.submission_revision)) authorRevision = data.submission_revision;
        d.acceptRevision(data.revision); voterState = null; ++voteRequest;
      } else { if (!valid(data.competition)) throw new Error('操作回执格式无效，结果未知。'); chosen = data.competition.id; adopt(data); notice(data.replayed ? '原操作结果已确认，未重复执行。' : '操作已由服务器确认。'); }
      if (frozen.url.endsWith('/pause')) notice(performancePauseMessage(data.performance_pause));
      pending = null;
      if (current?.status === 'COMPLETED') for (const w of current.submissions) await d.refreshPlayer(w.player.player_id);
    } catch (e) {
      if (auth !== d.epoch()) return;
      const status = obj(e) ? e.status : undefined;
      if (typeof status === 'number' && status >= 400 && status < 500 && status !== 401) { pending = null; notice(d.failure(e)); }
      else { fresh = false; notice(`${d.failure(e)} 结果未知；原操作编号和完整载荷已锁定。恢复连接后点击“确认原比赛操作”，不要创建替代操作。`); }
    } finally { busy = false; controls(); if (auth === d.epoch()) await refresh(); }
  }
  async function lifecycle(action: string, work?: string) {
    if (!enabled() || !current) return;
    const body: Obj = {command_id: crypto.randomUUID(),expected_version: current.version};
    if (action === 'cancel') { const reason = get<HTMLInputElement>('cancel-reason').value; if (!reason.trim() || [...reason].length > 200) { notice('请输入 1–200 字符取消原因。'); return; } body.reason = reason; }
    await submit(`/api/competitions/${current.id}/${work ? `submissions/${work}/` : ''}${action}`,body);
  }
  async function exportWork(work: string) {
    if (!enabled() || !current) return;
    const id = current.id, auth = d.epoch(); busy = true; controls();
    try {
      const e = await d.api<unknown>(`/api/competitions/${id}/submissions/${work}/evaluation-request`);
      if (auth !== d.epoch() || current?.id !== id || !d.ready()) return;
      if (!obj(e) || typeof e.task_id !== 'string' || !integer(e.attempt_no) || typeof e.input_hash !== 'string' || typeof e.policy_hash !== 'string' || !obj(e.frozen_input)) throw new Error('冻结任务格式无效。');
      exported = {work,envelope: e as Envelope}; get<HTMLTextAreaElement>('evaluation-request').value = JSON.stringify(e,null,2); get<HTMLTextAreaElement>('evaluation-result').value = ''; notice('此稿冻结任务已导出；请原生 ChatGPT 评价后人工导入 criteria 与 comment。');
    } catch (e) { if (auth === d.epoch()) { fresh = false; notice(d.failure(e)); } } finally { busy = false; controls(); }
  }
  async function loadVote() {
    voterState = null; const player = get<HTMLSelectElement>('voter').value, id = current?.id, generation = ++voteRequest, auth = d.epoch(); controls();
    if (!player || !id || current?.status !== 'VOTING' || !d.ready()) return;
    try {
      const data = await d.api<unknown>(`/api/competitions/${id}/vote-state?player_id=${encodeURIComponent(player)}`);
      if (generation !== voteRequest || auth !== d.epoch() || current?.id !== id || get<HTMLSelectElement>('voter').value !== player || !d.ready()) return;
      if (!obj(data) || data.player_id !== player || !integer(data.vote_revision) || !(data.submission_id === null || typeof data.submission_id === 'string')) throw new Error('投票版本格式无效。');
      voterState = data as typeof voterState & {player_id: string; vote_revision: number; submission_id: string | null};
      get('vote-state').textContent = `当前个人投票版本 ${data.vote_revision} · 不含票数或分数`;
      if (data.submission_id) get<HTMLSelectElement>('candidate').value = data.submission_id as string;
    } catch (e) { if (generation === voteRequest && auth === d.epoch()) { fresh = false; notice(d.failure(e)); } } finally { controls(); }
  }
  get('create').addEventListener('click',() => { if (enabled()) void submit('/api/competitions',{command_id:crypto.randomUUID()}); });
  get('replay').addEventListener('click',() => void submit());
  for (const action of ['open','close-submissions','open-voting','close-voting','settle','pause','resume','cancel']) get(action).addEventListener('click',() => void lifecycle(action));
  get('author').addEventListener('change',() => { const work = current?.submissions.find(w => w.player.player_id === get<HTMLSelectElement>('author').value); get<HTMLTextAreaElement>('text').value = work?.text ?? ''; authorRevision = work?.revision ?? 0; controls(); });
  get('submission-form').addEventListener('submit',event => {
    event.preventDefault(); if (!enabled() || !current || !allowed('submit')) return;
    const player = get<HTMLSelectElement>('author').value, value = get<HTMLTextAreaElement>('text').value;
    if (!player || !value.trim() || [...value].length > 1000 || value.includes('\0')) { notice('请选择作者并输入 1–1000 字符非空作品。'); return; }
    const revision = authorRevision;
    void submit(`/api/competitions/${current.id}/submissions`,{message_id:crypto.randomUUID(),player_id:player,expected_submission_revision:revision,text:value});
  });
  get('voter').addEventListener('change',() => void loadVote()); get('candidate').addEventListener('change',controls);
  get('vote-form').addEventListener('submit',event => { event.preventDefault(); if (!enabled() || !current || !voterState || !allowed('vote')) return; const candidate = get<HTMLSelectElement>('candidate').value; if (candidate) void submit(`/api/competitions/${current.id}/votes`,{message_id:crypto.randomUUID(),player_id:voterState.player_id,submission_id:candidate,expected_vote_revision:voterState.vote_revision}); });
  get('import').addEventListener('click',() => {
    if (!enabled() || !current || !exported) return;
    try { const evaluation: unknown = JSON.parse(get<HTMLTextAreaElement>('evaluation-result').value); if (!obj(evaluation) || Object.keys(evaluation).sort().join(',') !== 'comment,criteria' || !Array.isArray(evaluation.criteria) || typeof evaluation.comment !== 'string') { notice('只粘贴 {"criteria":[…],"comment":"…"}，不含总分、排名或 Markdown。'); return; }
      const e = exported.envelope; void submit(`/api/competitions/${current.id}/submissions/${exported.work}/evaluation-result`,{command_id:crypto.randomUUID(),task_id:e.task_id,attempt_no:e.attempt_no,input_hash:e.input_hash,policy_hash:e.policy_hash,evaluation});
    } catch { notice('不是有效 JSON；未提交。'); }
  });
  get('copy').addEventListener('click',async () => { const field = get<HTMLTextAreaElement>('evaluation-request'); try { await navigator.clipboard.writeText(field.value); notice('已复制冻结任务。'); } catch { field.focus(); field.select(); notice('请手动复制已选中 JSON。'); } });
  for (const n of document.querySelectorAll<HTMLElement>('[id^="competition-"]')) n.dataset.testid = n.id;
  window.setInterval(() => { get('clock').textContent = current?.deadline_at && ['SUBMITTING','VOTING'].includes(current.status) ? `参考倒计时：${Math.max(0,Math.ceil((Date.parse(current.deadline_at)-Date.now()-serverOffset)/1000))} 秒 · 接纳以服务器时间为准` : ''; },1000);
  render(); return {refresh,sync:controls,locked:() => busy || !!pending};
}
