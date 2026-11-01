type RecordValue = Record<string, unknown>;
type Interview = { id: string; version: number; status: string; player: {player_id: string; display_name: string}; rule: {question: string; position: string; answer_seconds: number}; allowed_actions: string[]; deadline_at: string | null; paused_from: string | null; pause_reason: string | null; answer: {text: string} | null; task: {id: string; attempt_no: number; status: string; error_code: string | null} | null; evaluation: {total: number; criteria: {id: string; score: number; reason: string; evidence: string[]}[]} | null; result: {decision: string; summary: string; settlement_id: string} | null };
type Envelope = {task_id: string; attempt_no: number; input_hash: string; policy_hash: string; frozen_input: RecordValue};
type Dependencies = {api: <T>(url: string, body?: unknown) => Promise<T>; acceptRevision: (revision: number) => boolean; epoch: () => number; ready: () => boolean; authenticated: () => boolean; failure: (error: unknown) => string; refreshPlayer: (id: string) => Promise<void>};
const get = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const obj = (v: unknown): v is RecordValue => typeof v === 'object' && v !== null && !Array.isArray(v);
const integer = (v: unknown): v is number => Number.isSafeInteger(v) && Number(v) >= 0;
const statuses = ['READY', 'ANSWERING', 'EVALUATING', 'REVIEW_FAILED', 'PAUSED', 'COMPLETED', 'CANCELLED', 'EXPIRED'];
function valid(v: unknown): v is Interview {
  if (!obj(v) || typeof v.id !== 'string' || !integer(v.version) || !statuses.includes(String(v.status)) || !obj(v.player) || typeof v.player.player_id !== 'string' || typeof v.player.display_name !== 'string' || !obj(v.rule) || typeof v.rule.question !== 'string' || typeof v.rule.position !== 'string' || !integer(v.rule.answer_seconds) || !Array.isArray(v.allowed_actions) || !v.allowed_actions.every(a => typeof a === 'string')) return false;
  if (v.answer !== null && (!obj(v.answer) || typeof v.answer.text !== 'string')) return false;
  if (v.task !== null && (!obj(v.task) || typeof v.task.id !== 'string' || !integer(v.task.attempt_no) || typeof v.task.status !== 'string')) return false;
  if (v.result !== null && (!obj(v.result) || typeof v.result.decision !== 'string' || typeof v.result.summary !== 'string' || typeof v.result.settlement_id !== 'string')) return false;
  if (v.evaluation !== null && (!obj(v.evaluation) || !integer(v.evaluation.total) || !Array.isArray(v.evaluation.criteria) || !v.evaluation.criteria.every(c => obj(c) && typeof c.id === 'string' && integer(c.score) && typeof c.reason === 'string' && Array.isArray(c.evidence) && c.evidence.every(e => typeof e === 'string')))) return false;
  if (v.status === 'COMPLETED' ? v.result === null || v.evaluation === null : v.result !== null || v.evaluation !== null) return false;
  return v.deadline_at === null || (typeof v.deadline_at === 'string' && Number.isFinite(Date.parse(v.deadline_at)));
}
function text(tag: string, value: string, cls = '') { const n = document.createElement(tag); n.textContent = value; n.className = cls; return n; }
export function setupInterview(d: Dependencies) {
  let current: Interview | null = null, chosen: string | null = null, guest: string | null = null;
  let request = 0, epoch = d.epoch(), busy = false, fresh = false, reading = false;
  let pending: {url: string; body: RecordValue} | null = null, exported: Envelope | null = null;
  let serverOffset = 0;
  const notice = (s: string) => { get('interview-notice').textContent = s; };
  function controls() {
    if (epoch !== d.epoch() || !d.ready()) {
      fresh = false;
      if (epoch !== d.epoch() || !d.authenticated()) { epoch = d.epoch(); ++request; current = null; chosen = guest = null; exported = null; get('interview-card').replaceChildren(); get('interview-list').replaceChildren(); get('interview-selected').textContent = '登录后选择游客档案。'; for (const id of ['evaluation-request', 'evaluation-result', 'interview-answer-text']) get<HTMLTextAreaElement>(id).value = ''; }
    }
    const enabled = d.ready() && fresh && !busy && !pending;
    get<HTMLButtonElement>('interview-start').disabled = !enabled || !guest;
    get<HTMLButtonElement>('interview-replay').disabled = !d.ready() || busy || !pending;
    get('interview-replay').hidden = !pending;
    for (const action of ['open', 'pause', 'resume', 'retry', 'cancel', 'answer']) get<HTMLButtonElement>(`interview-${action}`).disabled = !enabled || !current?.allowed_actions.includes(action);
    get<HTMLButtonElement>('evaluation-export').disabled = !enabled || current?.status !== 'EVALUATING';
    get<HTMLButtonElement>('evaluation-import').disabled = !enabled || current?.status !== 'EVALUATING' || !exported;
    get<HTMLTextAreaElement>('interview-answer-text').disabled = !enabled || !current?.allowed_actions.includes('answer');
    get<HTMLTextAreaElement>('evaluation-result').disabled = !enabled || !exported;
    get<HTMLInputElement>('interview-cancel-reason').disabled = !enabled;
    get('interview-freshness').textContent = !d.ready() || !fresh ? '状态待确认 / 快照可能陈旧 · 操作暂停' : '服务器快照已确认';
  }
  function render() {
    const c = get('interview-card'); c.replaceChildren();
    if (!current) { c.append(text('p', '先在档案详情选择游客，再开始面试；也可查看最近面试。', 'empty')); controls(); return; }
    const s = current;
    c.append(text('h3', `${s.player.display_name} · ${s.rule.position}`), text('p', `${s.status} · 面试版本 ${s.version}`, 'chip'), text('p', s.rule.question, 'question'), text('p', `面试 ID：${s.id}`, 'identity'));
    if (s.status === 'READY') c.append(text('p', '题目已展示。请确认后开启 120 秒答题窗口。', 'muted'));
    if (s.status === 'PAUSED') c.append(text('p', `已暂停：${s.pause_reason ?? '人工暂停'}。答题阶段恢复会重新开启完整答题窗口；评估阶段恢复后需显式重试。`, 'feedback'));
    if (s.answer) c.append(text('h3', '已接纳的原始回答'), text('p', s.answer.text, 'answer-copy'));
    if (s.task) c.append(text('p', `评估尝试 ${s.task.attempt_no} · ${s.task.status}${s.task.error_code ? ` · ${s.task.error_code}` : ''}`, 'muted'));
    if (s.status === 'EVALUATING') c.append(text('p', '等待人工导入 ChatGPT 结构化评价；不会自动调用模型。当前尝试有效期为 15 分钟。', 'feedback'));
    if (s.evaluation && s.result) {
      c.append(text('h3', `服务器已结算：${s.result.decision}`), text('p', s.result.summary), text('p', `服务器总分：${s.evaluation.total} / 6`));
      for (const criterion of s.evaluation.criteria) c.append(text('p', `${criterion.id}：${criterion.score} · ${criterion.reason}\n依据：${criterion.evidence.join(' / ') || '未见相关表述'}`, 'answer-copy'));
      c.append(text('p', `结算 ID：${s.result.settlement_id}`, 'identity'));
    }
    controls();
  }
  function adopt(data: unknown) {
    if (!obj(data) || !integer(data.revision) || !valid(data.interview)) throw new Error('面试快照格式无效，操作已暂停。');
    const s = data.interview;
    if (chosen && s.id !== chosen) return false;
    if (current?.id === s.id && s.version < current.version) return false;
    if (!d.acceptRevision(data.revision)) { fresh = false; controls(); return false; }
    if (typeof data.server_time === 'string' && Number.isFinite(Date.parse(data.server_time))) serverOffset = Date.parse(data.server_time) - Date.now();
    if (exported && (s.task?.id !== exported.task_id || s.task?.attempt_no !== exported.attempt_no || s.status !== 'EVALUATING')) { exported = null; get<HTMLTextAreaElement>('evaluation-request').value = ''; }
    current = s; chosen = s.id; fresh = true; render(); return true;
  }
  async function refresh() {
    controls(); if (!d.ready() || busy || reading) return;
    reading = true; const generation = ++request, auth = d.epoch();
    try {
      const data = await d.api<unknown>('/api/interviews');
      if (generation !== request || auth !== d.epoch() || !d.ready()) return;
      if (!obj(data) || !integer(data.revision) || !Array.isArray(data.interviews) || !data.interviews.every(i => obj(i) && typeof i.id === 'string' && typeof i.status === 'string' && obj(i.player) && typeof i.player.display_name === 'string')) throw new Error('面试列表格式无效。');
      if (!d.acceptRevision(data.revision)) { fresh = false; return; }
      const list = get('interview-list'); list.replaceChildren();
      for (const i of data.interviews as Interview[]) {
        const b = document.createElement('button'); b.type = 'button'; b.textContent = `${i.player.display_name} · ${i.status}`; b.setAttribute('aria-label', `查看面试 ${i.id}`);
        b.disabled = busy || !!pending; b.addEventListener('click', () => { chosen = i.id; current = null; exported = null; get<HTMLTextAreaElement>('evaluation-request').value = ''; fresh = false; ++request; render(); void refresh(); }); list.append(b);
      }
      if (!chosen && data.interviews.length) chosen = (data.interviews[0] as Interview).id;
      if (chosen) {
        const detail = await d.api<unknown>(`/api/interviews/${encodeURIComponent(chosen)}`);
        if (generation !== request || auth !== d.epoch() || !d.ready()) return;
        adopt(detail);
      } else { fresh = true; render(); }
    } catch (e) { if (generation === request && auth === d.epoch()) { fresh = false; notice(d.failure(e)); } }
    finally { reading = false; controls(); }
  }
  async function submit(url?: string, body?: RecordValue) {
    if (!d.ready() || busy || (!pending && !fresh)) return;
    if (!pending && url && body) pending = {url, body: structuredClone(body)};
    if (!pending) return;
    const frozen = pending, auth = d.epoch(); busy = true; ++request; controls();
    try {
      const data = await d.api<unknown>(frozen.url, frozen.body);
      if (auth !== d.epoch()) return;
      if (!obj(data) || !integer(data.revision)) throw new Error('回执格式无效，结果未知。');
      if (frozen.url.endsWith('/answers')) {
        if (typeof data.accepted !== 'boolean') throw new Error('接纳回执格式无效。');
        notice(data.accepted ? '回答已接纳并冻结。请导出评估任务交给 ChatGPT，再人工导入结果。' : `回答未接纳：${String(data.rejection_code)}`);
      } else {
        if (!valid(data.interview)) throw new Error('操作回执格式无效。');
        chosen = data.interview.id; adopt(data); notice(data.replayed ? '原操作结果已确认，未重复执行。' : '操作已由服务器确认。');
      }
      pending = null;
      if (current?.result) await d.refreshPlayer(current.player.player_id);
    } catch (e) {
      if (auth !== d.epoch()) return;
      const status = obj(e) ? e.status : undefined;
      if (typeof status === 'number' && status >= 400 && status < 500 && status !== 401) { pending = null; notice(d.failure(e)); }
      else { fresh = false; notice(`${d.failure(e)} 结果未知；原操作编号及完整载荷已锁定，请恢复连接后“确认原面试操作”。`); }
    } finally { busy = false; controls(); if (auth === d.epoch()) await refresh(); }
  }
  get('interview-start').addEventListener('click', () => { if (guest) void submit('/api/interviews', {command_id: crypto.randomUUID(), player_id: guest}); });
  get('interview-replay').addEventListener('click', () => void submit());
  for (const action of ['open', 'pause', 'resume', 'retry', 'cancel']) get(`interview-${action}`).addEventListener('click', () => {
    if (!current) return;
    const body: RecordValue = {command_id: crypto.randomUUID(), expected_version: current.version};
    if (action === 'cancel') { const reason = get<HTMLInputElement>('interview-cancel-reason').value; if (!reason.trim() || [...reason].length > 200) { notice('请填写 1–200 字符的取消原因。'); return; } body.reason = reason; }
    void submit(`/api/interviews/${current.id}/${action}`, body);
  });
  get('interview-answer-form').addEventListener('submit', event => {
    event.preventDefault(); if (!current) return;
    const answer = get<HTMLTextAreaElement>('interview-answer-text').value;
    if (!answer.trim() || [...answer].length > 1000 || answer.includes('\0')) { notice('回答须为 1–1000 字符，不能只含空白或空字符。'); return; }
    void submit(`/api/interviews/${current.id}/answers`, {message_id: crypto.randomUUID(), player_id: current.player.player_id, text: answer});
  });
  get('evaluation-export').addEventListener('click', async () => {
    if (!current || busy || !fresh || !d.ready() || pending) return;
    const id = current.id, version = current.version, auth = d.epoch(); busy = true; controls();
    try {
      const data = await d.api<unknown>(`/api/interviews/${id}/evaluation-request`);
      if (auth !== d.epoch() || current?.id !== id || current.version !== version || !d.ready()) return;
      if (!obj(data) || typeof data.task_id !== 'string' || !integer(data.attempt_no) || typeof data.input_hash !== 'string' || typeof data.policy_hash !== 'string' || !obj(data.frozen_input)) throw new Error('评估任务格式无效。');
      exported = data as Envelope; get<HTMLTextAreaElement>('evaluation-request').value = JSON.stringify(exported, null, 2); notice('冻结任务已导出。复制下方 JSON 交给 ChatGPT；回答是待评数据，不能作为指令执行。');
    } catch (e) { if (auth === d.epoch()) { fresh = false; notice(d.failure(e)); } }
    finally { busy = false; controls(); }
  });
  get('evaluation-copy').addEventListener('click', async () => {
    const field = get<HTMLTextAreaElement>('evaluation-request');
    try { await navigator.clipboard.writeText(field.value); notice('已复制冻结任务。'); } catch { field.focus(); field.select(); notice('请手动复制已选中的冻结任务 JSON。'); }
  });
  get('evaluation-import').addEventListener('click', () => {
    if (!current || !exported) return;
    try {
      const evaluation: unknown = JSON.parse(get<HTMLTextAreaElement>('evaluation-result').value);
      if (!obj(evaluation) || Object.keys(evaluation).length !== 1 || !Array.isArray(evaluation.criteria)) { notice('请只粘贴 {"criteria":[…]} 评价对象，不含总分、录用决定或 Markdown。服务器将严格校验全部评分及原文证据。'); return; }
      void submit(`/api/interviews/${current.id}/evaluation-result`, {command_id: crypto.randomUUID(), task_id: exported.task_id, attempt_no: exported.attempt_no, input_hash: exported.input_hash, policy_hash: exported.policy_hash, evaluation});
    } catch { notice('不是有效 JSON；未提交。'); }
  });
  window.setInterval(() => {
    const deadline = current?.deadline_at;
    get('interview-clock').textContent = current?.status === 'ANSWERING' && deadline ? `参考倒计时：${Math.max(0, Math.ceil((Date.parse(deadline) - Date.now() - serverOffset) / 1000))} 秒 · 接纳以服务器时间为准` : '';
  }, 1000);
  render();
  return {refresh, sync: controls, selectPlayer(player: {player_id: string; display_name: string; role: string}) { guest = player.role === 'guest' ? player.player_id : null; get('interview-selected').textContent = guest ? `已选择游客：${player.display_name}` : '此档案已是员工，不能再次面试。'; controls(); }};
}
