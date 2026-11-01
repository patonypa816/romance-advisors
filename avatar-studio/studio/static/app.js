'use strict';
const $ = id => document.getElementById(id);
let current = null, busy = false, polling = false, connected = false, settingsLoaded = false, intakeSettingsLoaded = false, noticeTimer, networkFailed = false;
let avatarSwitchBusy = false;
let settingsDirty = false, settingsSignature = '', settingsAssetId = null;
const quickRoleDrafts = new Map();
function syncRoleSettings(settings, avatarId = null) {
  const key = avatarId == null ? null : String(avatarId);
  const signature = JSON.stringify([settings.name || '', settings.persona || '', settings.reply_style || '']);
  const changedAsset = settingsLoaded && key !== settingsAssetId;
  if (changedAsset && settingsDirty) quickRoleDrafts.set(settingsAssetId, {name:$('name').value, persona:$('persona').value, reply_style:$('replyStyle').value});
  if (!settingsLoaded || changedAsset || (!settingsDirty && signature !== settingsSignature)) {
    const draft = quickRoleDrafts.get(key);
    const values = draft || settings;
    $('name').value = values.name || ''; $('persona').value = values.persona || ''; $('replyStyle').value = values.reply_style || '';
    settingsDirty = !!draft; settingsLoaded = true; settingsAssetId = key; settingsSignature = signature;
  }
  $('quickRoleDraft').hidden = !settingsDirty;
  $('quickRoleContext').textContent = `${settings.name || '当前数字人'}的称呼与语气；仅空闲时可以保存；会话结束后需先新建会话。`;
  $('currentRoleLabel').textContent = `${settings.name || '数字人'} / 本机直播控制`;
}
for (const id of ['name','persona','replyStyle']) $(id).addEventListener('input', () => { settingsDirty = true; $('quickRoleDraft').hidden = false; });
let inputMode = 'comment';
const inputModeButtons = [...$('directorMode').querySelectorAll('button[data-mode]')];
const openCommandDetails = new Set(), commandCache = new Map();
const pendingStates = new Set(['pending', 'processing', 'ready', 'preparing', 'prepared', 'queued', 'synthesizing', 'planning_actions', 'sending']);
const idleStates = new Set(['idle']);
const labels = { idle:'空闲', starting:'正在开场', creating:'正在创建', connecting:'正在连接', waiting_rtc:'等待播放器连接', waiting_stage:'等待 OBS 音画', ready:'已就绪', sending:'发送中', draining:'处理剩余内容', stopping:'正在结束', ended:'已结束', error:'出错', stop_unconfirmed:'结束状态未确认', creation_unknown:'创建状态未知', pending:'待处理', processing:'处理中', synthesizing:'合成中', planning_actions:'编排动作中', preparing:'预处理音频与动作', prepared:'已准备，等待交接', queued:'等待发送', sent:'已发送', cancelled:'已取消', skipped:'已跳过' };
function display(value, fallback = '—') { return value == null || value === '' ? fallback : String(value); }
function localTime(value) { const date = new Date(Number(value) * 1000); return Number.isFinite(date.getTime()) ? date.toLocaleTimeString('zh-CN', {hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false}) : '时间未知'; }
function fixed(value, digits) { const n = Number(value); return value == null || !Number.isFinite(n) ? '—' : n.toFixed(digits); }
function stateLabel(value) { return labels[value] || display(value, '未知'); }
function setStatus(id, label, style) { const el = $(id); el.textContent = label; el.className = `status ${style}`; }
function showNotice(message, ok = false) { const el = $('notice'); clearTimeout(noticeTimer); el.hidden = false; el.className = `notice${ok ? ' ok' : ''}`; el.textContent = message; noticeTimer = setTimeout(() => { el.hidden = true; }, 3000); }
const headerMenus = [...document.querySelectorAll('[data-header-menu]')];
for (const menu of headerMenus) menu.addEventListener('toggle', () => {
  // Keep one utility panel open so small screens never stack overlapping forms.
  if (menu.open) for (const other of headerMenus) if (other !== menu) other.open = false;
});
document.addEventListener('click', event => {
  for (const menu of headerMenus) if (menu.open && !menu.contains(event.target)) menu.open = false;
});
document.addEventListener('keydown', event => {
  const menu = headerMenus.find(item => item.open);
  if (event.key === 'Escape' && menu) {
    menu.open = false;
    menu.querySelector('summary').focus();
  }
});
async function request(path, body) {
  const options = body === undefined ? {cache:'no-store'} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)};
  const response = await fetch(path, options);
  let data; try { data = await response.json(); } catch { throw new Error(`服务返回异常（${response.status}）`); }
  if (!response.ok || data.error) throw new Error(display(data.error, `请求失败（${response.status}）`));
  return data;
}
function isIdle(state) { return idleStates.has(state); }
function formatActions(actions) {
  if (actions == null || actions === '') return '暂无';
  if (typeof actions === 'string') return actions;
  if (Array.isArray(actions)) return actions.map(a => typeof a === 'string' ? a : JSON.stringify(a)).join('；') || '暂无';
  return JSON.stringify(actions);
}
function addText(parent, tag, text, className) { const el = document.createElement(tag); if (className) el.className = className; el.textContent = text; parent.appendChild(el); return el; }
function count(value) { return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? String(value) : '—'; }
function records(value) { return Array.isArray(value) ? value.filter(item => item && typeof item === 'object' && !Array.isArray(item)) : []; }
// Keep a reader's position while replacing only list contents, leaving details open state intact.
function updateList(container, draw) {
  const top = container.scrollTop;
  const atBottom = container.scrollHeight > container.clientHeight && container.scrollHeight - container.clientHeight - top < 2;
  container.replaceChildren(); draw();
  container.scrollTop = atBottom ? container.scrollHeight : top;
}
function renderIntake(data, sessionState, paused) {
  const intake = data.intake || {}, source = intake.source || {}, settings = intake.settings || {}, pool = intake.pool || {};
  const shortTest = data.studio?.short_performance_test === true;
  const continuousTest = data.studio?.continuous_speech_test === true;
  const longTest = data.studio?.long_speech_test === true;
  const enabled = intake.enabled === true, sourceState = source.state || 'stopped';
  const sourceLabels = {stopped:'已停止', connecting:'正在连接', connected:'订阅已连接', live:'采集中', streaming:'接收中', reconnecting:'正在重连', disconnected:'连接中断', error:'来源出错'};
  setStatus('intakeBadge', enabled ? display(sourceLabels[sourceState], '订阅已开启') : '来源已停止', source.error_code || intake.last_error ? 'bad' : 'neutral');
  const lastReceived = source.last_event_received_at ? ` · 最近事件 ${localTime(source.last_event_received_at)}` : ' · 尚无事件记录';
  $('sourceStatus').textContent = `${display(sourceLabels[sourceState], '来源状态未知')} · 房间 ${display(source.room_id || settings.room_id, '未设置')}${lastReceived} · 重连 ${count(source.reconnect_count)} 次`;
  $('intakeDetail').textContent = !enabled ? '尚未订阅真实评论；点击连接后才启动接收。' : paused ? '已暂停：不筛选新评论，已入回复队列的内容继续。' : ['ready','sending','draining'].includes(sessionState) ? '已开启接收；每 5 秒汇总筛选可回应的评论，空池或繁忙时跳过本轮。' : '订阅已开启；等待会话音画就绪后再筛选。';
  const errors = [source.error_code, intake.last_error].filter(Boolean);
  $('intakeError').textContent = errors.length ? `来源或筛选异常：${[...new Set(errors)].map(value => display(value)).join(' / ')}` : '';
  $('startIntake').disabled = busy || !connected || enabled || shortTest || continuousTest || longTest;
  $('stopIntake').disabled = busy || !connected || !enabled;
  for (const id of ['intakeBaseUrl','intakeRoomId','saveIntakeSettings']) $(id).disabled = busy || !connected || enabled || shortTest || continuousTest || longTest;
  if (shortTest) $('intakeDetail').textContent = '本次为单 S 短表演试验，可在下方模拟弹幕、主动安排或只做动作。';
  if (continuousTest) $('intakeDetail').textContent = '本次为连续口播双动作组试验，可在下方模拟弹幕或主动安排。';
  if (longTest) $('intakeDetail').textContent = '本次为长文多动作试验，可在下方模拟弹幕或主动安排。';
  if (!intakeSettingsLoaded && (settings.base_url || settings.room_id)) {
    $('intakeBaseUrl').value = settings.base_url || '';
    $('intakeRoomId').value = settings.room_id || '';
    intakeSettingsLoaded = true;
  }
  $('candidateCount').textContent = `候选 ${count(pool.candidate_count)} 条`;
  $('selectingCount').textContent = `筛选中 ${count(pool.selecting_count)} 条`;
  const countLabels = {received:'收到',total:'收到',accepted:'接收',candidate:'候选',candidates:'候选',selected:'选中',skipped:'略过',rejected:'略过',expired:'过期',duplicate:'重复',duplicates:'重复',dropped:'丢弃',enqueued:'已入回复队列',error:'错误',errors:'错误'};
  $('intakeCounts').replaceChildren();
  if (pool.counts && typeof pool.counts === 'object' && !Array.isArray(pool.counts)) {
    for (const [key, value] of Object.entries(pool.counts)) {
      if (typeof value === 'number' && Number.isFinite(value) && value >= 0) addText($('intakeCounts'), 'span', `${countLabels[key] || key} ${count(value)}`);
    }
  }
  const statusLabels = {candidate:'待筛选',pending:'待筛选',selecting:'筛选中',selected:'已选中',enqueued:'已入回复队列',skipped:'未选中',rejected:'已略过',expired:'已过期',duplicate:'重复',dropped:'已丢弃',cancelled:'已取消',error:'筛选出错'};
  function draw(id, list, empty) {
    const container = $(id); updateList(container, () => {
    if (!list.length) { addText(container, 'p', empty, 'empty'); return; }
    for (const item of list) {
      const article = document.createElement('article'); article.className = 'candidate';
      const top = document.createElement('div'); top.className = 'comment-top';
      addText(top, 'strong', display(item.nickname, '观众'));
      addText(top, 'span', statusLabels[item.status] || display(item.status, '状态未提供'), 'comment-status');
      article.appendChild(top);
      addText(article, 'p', display(item.text, '未提供评论原文'));
      if (item.reason) addText(article, 'p', `原因：${display(item.reason)}`, 'meta');
      if (item.age_seconds != null) addText(article, 'p', `评论年龄 ${fixed(item.age_seconds, 1)} 秒`, 'meta');
      container.appendChild(article);
    }
    });
  }
  draw('candidates', records(pool.candidates), '尚无待筛选评论。');
  const history = records(pool.history);
  $('selectionHistoryCount').textContent = `${history.length} 条`;
  draw('selectionHistory', history, '选取、略过和过期记录会显示在这里。');
}
// Fetch only on first expansion of a completed item. Each cached entry belongs
// to one session generation, so a new session cannot reuse old send records.
async function loadCommandDetails(key, item, generation) {
  commandCache.set(key, {loading:true});
  try {
    const result = await request('diagnostics/export');
    if (!Array.isArray(result.records)) throw new Error('导出数据缺少记录');
    // The server may switch sessions while this read is in flight. Never attach
    // another session's reused comment ID to the old reply.
    if (result.run_id !== generation) throw new Error('会话已切换，请查看当前会话记录');
    const motionEvents = [
      ...records(current?.session?.generation === generation ? current.session.motion_events : []),
      ...records(item.motion_receipts).flatMap(receipt => records(receipt.motion_events))
    ];
    const commands = records(result.records).filter(row => row.direction === 'out' && row.kind === 'ws_sent' && [9, 10].includes(Number(row.type)) && row.details?.comment_id === item.id)
      .sort((a, b) => Number(a.seq_id) - Number(b.seq_id)).map(row => {
        const motion = motionEvents.find(event => String(event.seq_id) === String(row.seq_id));
        return {kind:Number(row.type) === 9 ? 'action' : 'speech', role:row.details?.role,
          text:row.content, sent_at:row.time, actual_interval_seconds:motion?.actual_interval_seconds};
      });
    commandCache.set(key, {commands, incomplete:result.disk_complete === false});
  } catch (error) { commandCache.set(key, {error:error.message}); }
  if (current?.session?.generation === generation) render(current);
}
function renderComments(items, generation) {
  const pending = items.filter(item => pendingStates.has(String(item.status || '').toLowerCase()));
  const history = items.filter(item => !pending.includes(item)).sort((a, b) => (Number(b.created_at) || 0) - (Number(a.created_at) || 0));
  $('pendingCount').textContent = `${pending.length} 条`; $('historyCount').textContent = `${history.length} 条`;
  function renderList(id, list, emptyText, canSkip) {
    const container = $(id); updateList(container, () => {
    if (!list.length) { addText(container, 'p', emptyText, 'empty'); return; }
    for (const item of list) {
      const article = document.createElement('article'); article.className = 'comment';
      const top = document.createElement('div'); top.className = 'comment-top';
      addText(top, 'span', stateLabel(item.status), 'comment-status');
      if (item.created_at) addText(top, 'time', localTime(item.created_at));
      article.appendChild(top);
      const operator = item.input_kind === 'operator';
      const kind = operator ? ({once:'主持指令 · 一次安排', topic:`备用主讲 · 第 ${display(item.topic_round)} 段`, motion:'主持指令 · 只做动作'}[item.operator_mode] || '主持指令') : `观众 · ${display(item.nickname, '观众')}`;
      addText(article, 'p', `${kind}：${display(item.text)}`);
      if (!(operator && item.operator_mode === 'motion')) addText(article, 'p', `${operator ? '台词' : '回答'}：${display(item.answer, '等待回复')}`, 'answer');
      if (item.error) addText(article, 'p', `原因：${display(item.error)}`, 'hint');
      const reading = operator ? (item.operator_mode === 'motion' ? '纯动作' : '主持演绎') : !Object.hasOwn(item, 'reading') ? '尚未计划' : item.reading === null ? '直接接话' : typeof item.reading === 'object' ? '整套阅读' : display(item.reading, '尚未确定');
      const segmentInfo = operator && item.operator_mode === 'motion' ? '' : ` · 已发送片段：${display(item.segments_sent, '0')}`;
      addText(article, 'p', `方式：${reading}${segmentInfo} · 动作：${formatActions(item.actions)}`, 'meta');
      if (item.semantic_speech) {
        const groups = (item.delivery || []).flatMap(segment => (segment.actions || []).map(action => {
          const point = (segment.timeline || []).find(node => ['S', 'L'].includes(node.role) && node.group_index === segment.actions.indexOf(action) + 1);
          return `“${display(action.anchor?.quote)}”${point ? ` ${fixed(point.at_seconds, 2)}秒` : '（草案，待语音后编排）'}`;
        }));
        addText(article, 'p', groups.length ? `计划动作位置：${groups.join('；')}` : '语音回复 · 无主动动作', 'meta');
      }
      if (item.long_speech_test) addText(article, 'p', `${item.replay_fixture_fingerprint ? '复用原样本' : '本条生成'} ${display(item.generated_text_chars)} 字 · ${display(item.action_group_count)} 个动作节点`, 'meta');
      if (canSkip && !(operator && item.operator_mode === 'topic') && String(item.status || '').toLowerCase() === 'pending') {
        const button = addText(article, 'button', '跳过'); button.type = 'button'; button.disabled = busy || !connected; button.addEventListener('click', () => act('skip', {id:item.id}, '已跳过该任务'));
      }
      if (!canSkip) {
        const detail = document.createElement('details'); detail.className = 'command-details';
        const key = JSON.stringify([generation ?? null, item.id ?? null]);
        detail.open = openCommandDetails.has(key);
        let observedOpen = detail.open;
        detail.addEventListener('toggle', () => {
          // Restoring an open <details> also emits toggle; it is not a user retry.
          if (detail.open === observedOpen) return;
          observedOpen = detail.open;
          if (detail.open) {
            openCommandDetails.add(key);
            const cached = commandCache.get(key);
            if (!cached || cached.error) { loadCommandDetails(key, item, generation); render(current); }
          } else openCommandDetails.delete(key);
        });
        const cached = commandCache.get(key), commands = cached?.commands;
        addText(detail, 'summary', `发给 Vidu 的指令${commands ? `（${commands.length}条）` : ''}`);
        if (cached?.loading) addText(detail, 'p', '正在读取实际发送记录…', 'meta');
        else if (cached?.error) addText(detail, 'p', `读取发送记录失败：${cached.error}。关闭后重新展开可重试。`, 'hint');
        else if (!commands) addText(detail, 'p', '展开后读取实际发送记录。', 'meta');
        else if (!commands.length) addText(detail, 'p', '尚无实际发送记录。', 'meta');
        else for (const command of commands) {
          const entry = document.createElement('div'); entry.className = 'command-entry';
          const heading = command.kind === 'action' ? `动作（Type9 · ${display(command.role, '角色未知')}）` : command.kind === 'speech' ? '语音文本（Type10）' : '未知指令';
          const interval = command.kind === 'action' && command.actual_interval_seconds > 0 ? ` · 与上一动作间隔 ${fixed(command.actual_interval_seconds, 2)} 秒` : '';
          addText(entry, 'p', `${heading} · ${localTime(command.sent_at)}${interval}`, 'meta');
          addText(entry, 'p', display(command.text, '（空文本）'));
          detail.appendChild(entry);
        }
        if (cached?.incomplete) addText(detail, 'p', '诊断记录写入不完整，显示的发送明细可能不全。', 'hint');
        article.appendChild(detail);
      }
      container.appendChild(article);
    }
    });
  }
  renderList('pending', pending, '还没有待处理的内容。', true);
  renderList('history', history, '处理过的内容会显示在这里。', false);
}
function renderDirector(topic, {enabled}) {
  const state = topic?.state;
  const labels = {running:'等待互动空档', paused:'主讲已暂停', stopped:'主讲已停止', completed:'话题已完成', error:'主讲需处理'};
  setStatus('directorPhase', topic?.view_state || labels[state] || '未安排', state === 'error' ? 'bad' : 'neutral');
  $('directorStatus').textContent = topic
    ? `${display(topic.instruction)}\n已完整发送 ${display(topic.completed_rounds, '0')} / ${display(topic.total_rounds, '0')} 个小段`
    : '没有待回应互动时，按提纲讲述；讲完当前话题后待命。';
  const preparation = {pending:'等待准备', outline:'正在整理有限提纲', model:'正在准备下一段台词', audio_actions:'正在预处理下一段语音与动作', prepared:'下一段已准备，发送前仍优先检查互动'};
  const preparationText = state === 'running' ? preparation[topic?.preparation_state] : '';
  $('directorNext').textContent = [preparationText, topic?.next_action].filter(Boolean).join(' · ');
  const outline = $('directorOutline');
  outline.replaceChildren();
  for (const [index, unit] of records(topic?.outline).entries()) {
    const round = index + 1, completed = round <= (topic.completed_rounds || 0);
    const active = !completed && round === topic.current_round;
    const prepared = Array.isArray(topic.prepared_rounds) && topic.prepared_rounds.includes(round);
    const li = addText(outline, 'li', display(unit.title, unit.goal || `第 ${round} 段`));
    li.dataset.progress = completed ? 'sent' : active ? 'current' : 'pending';
    if (unit.goal) li.title = unit.goal;
    const status = completed ? '已发送' : active ? '当前段' : prepared ? '已准备，未发送' : '未讲';
    addText(li, 'span', status, 'outline-status');
  }
  const phase = topic?.error_phase === 'preparation' ? '准备未完成' : topic?.error_phase === 'sending' ? '发送未完成' : '主讲需处理';
  $('directorTopicError').textContent = topic?.error ? `${phase}：${topic.error}` : '';
  $('directorPause').disabled = !enabled || state !== 'running';
  // 自然完成是本次安排的终点；继续只恢复暂停许可，不增加提纲。
  $('directorContinue').disabled = !enabled || state !== 'paused';
  $('directorContinue').textContent = '继续主讲';
  $('directorStop').disabled = !enabled || !['running','paused','error'].includes(state);
}
function replayVersionKnown(replay, studio) {
  return replay?.semantic_speech !== true || (typeof replay.independent_ge === 'boolean'
    && typeof studio.runtime?.source_version === 'string' && Array.isArray(replay.timeline)
    && replay.timeline.length > 0);
}
// Render the fixture actually loaded by this process; URL labels never select a test.
function renderReplayCommands(replay, session, studio, comments) {
  const semantic = replay?.semantic_speech === true;
  document.body.classList.toggle('fixed-replay-mode', !!replay);
  $('replayTimebase').hidden = !semantic;
  $('replayLiveCommand').hidden = !semantic;
  if (!semantic) {
    $('replayRuntime').textContent = '';
    $('replayStructure').textContent = '';
    $('replayCommandTable').replaceChildren();
    $('previewTitle').textContent = 'Vidu 实时预览';
    document.title = '御书房 · 直播工作台';
    return;
  }
  if (!replayVersionKnown(replay, studio)) {
    $('fixedReplayTitle').textContent = '后台版本未确认 · 暂停测试';
    $('previewTitle').textContent = '后台版本未确认';
    document.title = '后台版本未确认｜御书房工作台';
    $('replayRuntime').textContent = '缺少运行版本或完整指令信息，请统一后台版本后重新加载本页。';
    $('replayStructure').textContent = '';
    $('replayCommandTable').replaceChildren();
    $('replayLiveCommand').textContent = '当前条件未核实，不能标记为 A 或 B。';
    $('replaySample').textContent = '后台版本未确认';
    return;
  }
  const minute = replay.source_kind === 'prepared_minute';
  const group = replay.independent_ge ? (replay.ge_lead_seconds === 1.1 ? 'A' : '基线') : 'B';
  const label = minute ? 'A · 1分钟六动作' : group === 'A' ? 'A · 独立 GE 提前 1.1 秒' : group === 'B' ? 'B · 取消独立 GE' : '基线 · 独立 GE 提前 0.5 秒';
  $('fixedReplayTitle').textContent = label;
  $('previewTitle').textContent = `${label} · 实时预览`;
  document.title = `${label}｜御书房工作台`;
  $('replaySample').textContent = minute ? '播放1分钟六动作' : `播放 ${group} 组对照样本`;
  const runtime = studio.runtime || {};
  $('replayRuntime').textContent = `${minute ? '两段真实语音连续播放 · ' : replay.source_kind === 'prepared' ? '新准备素材 · ' : '已播样本 · '}当前后台 PID ${display(runtime.pid)} · 启动 ${localTime(runtime.started_at)} · 运行版本 ${display(runtime.source_version?.slice(0, 12))} · 场次 ${display(session.generation)}`;
  const timeline = records(replay.timeline);
  $('replayStructure').textContent = timeline.map(node => `${node.role}${node.group_index} (${fixed(node.at_seconds, 2)}s)`).join(' → ') + ` · 共 ${timeline.length} 条 Type9`;
  // Match only the latest replay of this generation, so previous sends cannot
  // make a freshly queued sample appear complete.
  const item = [...records(comments)].reverse().find(c => c.replay_fixture_fingerprint === replay.fingerprint);
  const events = records(session.motion_events).filter(e => e.comment_id === item?.id);
  const latestGroup = events.at(-1)?.group_id;
  const sent = events.filter(e => e.group_id === latestGroup);
  const last = sent.at(-1);
  $('replayLiveCommand').textContent = last
    ? `最近发送：${last.role}${last.group_index} · ${fixed(last.actual_at_seconds, 2)} 秒\n${last.instruction}\n已写入 ${sent.length}/${timeline.length} 条 Type9 · 画面效果待观察`
    : `当前 ${group} 组 · ${item ? '等待发送' : '尚未播放样本'} · ${timeline.length} 条 Type9`;
  const container = $('replayCommandTable');
  updateList(container, () => {
    for (const node of timeline) {
      const event = sent.find(e => e.node_index === node.index && e.role === node.role && e.group_index === node.group_index);
      const row = document.createElement('div');
      row.className = `replay-command-row${event ? ' sent' : ''}${event && event === last ? ' latest' : ''}`;
      const status = document.createElement('div');
      addText(status, 'strong', `${node.role}${node.group_index} · ${fixed(node.at_seconds, 2)} 秒`);
      addText(status, 'span', event ? `已发送 ${fixed(event.actual_at_seconds, 3)} 秒` : '待发送', 'replay-send-status');
      if (event && event.actual_interval_seconds > 0) addText(status, 'span', `距上一条 ${fixed(event.actual_interval_seconds, 3)} 秒`, 'replay-send-status');
      row.appendChild(status);
      addText(row, 'p', event?.instruction || node.instruction, 'replay-instruction');
      container.appendChild(row);
    }
  });
}
let idleDraftDirty = false, idleDraftGeneration = null, idleCatalogSignature = '', idleStopBusy = false, idlePollRevision = 0, idleEnablePending = null;
// Closing and recovery remain reachable while an unrelated request is in flight.
async function idleStopRequest(path, body, message) {
  if (idleStopBusy || !connected) return;
  idleStopBusy = true; if (current) render(current);
  try { await request(path, body); showNotice(message, true); await poll(); }
  catch (error) { showNotice(`操作未完成：${error.message}`); }
  finally { idleStopBusy = false; if (current) render(current); }
}
function renderIdleMotions(studio, session, canPlay, blocked) {
  const module = studio.idle_motions || {}, scheduler = module.scheduler || {}, catalog = module.catalog || {};
  const actions = records(catalog.actions), settings = catalog.settings || {};
  const locked = scheduler.enabled === true || scheduler.needs_recovery === true || ['holding','recovering'].includes(scheduler.status);
  const signature = JSON.stringify([actions, settings]);
  // Keep the user's unfinished form untouched across state polling.
  if (!idleDraftDirty && signature !== idleCatalogSignature) {
    idleCatalogSignature = signature;
    $('idleMin').value = settings.min_interval_seconds ?? 15;
    $('idleMax').value = settings.max_interval_seconds ?? 25;
    $('idleMotionActions').replaceChildren();
    if (!actions.length) addText($('idleMotionActions'), 'p', catalog.configured === false ? '尚未配置动作库，请先刷新动作库。' : '暂无可用动作，请刷新动作库。', 'footnote');
    for (const action of actions) {
      const row = document.createElement('div'); row.className = 'idle-motion-row';
      const label = document.createElement('label'), check = document.createElement('input');
      check.type = 'checkbox'; check.checked = action.enabled === true; check.dataset.id = String(action.id);
      label.append(check, document.createTextNode(display(action.name, String(action.id)))); row.appendChild(label);
      if (action.visual_verified !== true) addText(row, 'span', '待试播', 'footnote');
      const test = document.createElement('button'); test.type = 'button'; test.textContent = '单次试播'; test.dataset.actionId = String(action.id); row.appendChild(test);
      $('idleMotionActions').appendChild(row);
    }
  }
  const staleDraft = idleDraftDirty && idleDraftGeneration !== session.generation;
  const noEdit = busy || !connected || locked || blocked || staleDraft;
  $('idleMin').disabled = $('idleMax').disabled = noEdit;
  for (const input of $('idleMotionActions').querySelectorAll('input')) input.disabled = noEdit;
  for (const button of $('idleMotionActions').querySelectorAll('button')) button.disabled = busy || !connected || locked || blocked || !canPlay || module.test_available !== true;
  for (const button of $('idleMotionActions').querySelectorAll('button')) button.title = module.test_reason || ''; 
  $('idleMotionsSave').disabled = noEdit || !idleDraftDirty;
  $('idleMotionsSync').disabled = noEdit || idleDraftDirty;
  $('idleMotionsDraft').hidden = !idleDraftDirty;
  $('idleMotionsDraft').textContent = staleDraft ? '场次已切换，未保存的设置不能应用到新场次；请刷新页面重新编辑。' : '设置尚未保存；请先保存，再刷新动作库。';
  const pendingEnable = idleEnablePending?.generation === session.generation ? idleEnablePending : null;
  $('idleMotionsToggle').textContent = pendingEnable ? '取消开启' : scheduler.enabled ? '关闭待机' : '开启待机';
  $('idleMotionsToggle').setAttribute('aria-pressed', String(scheduler.enabled === true));
  $('idleMotionsToggle').disabled = !connected || idleStopBusy || !!pendingEnable?.cancelled || (!pendingEnable && !scheduler.enabled && (busy || blocked || !canPlay || module.available !== true || locked || idleDraftDirty));
  $('idleMotionsRecover').hidden = module.recover_available !== true;
  $('idleMotionsRecover').disabled = idleStopBusy || !connected;
  const names = {off:'已关闭', stopped:'已停止', paused:'等待当前任务结束', waiting:'等待空闲', waiting_candidates:'等待可用动作', holding:'正在执行动作', recovering:'正在回正并让位', error:'出错，请检查'};
  let status = names[scheduler.status] || (scheduler.enabled ? '等待空闲' : '已关闭');
  if (scheduler.enabled && scheduler.status === 'waiting' && Number.isFinite(scheduler.seconds_until_next)) status = `约 ${Math.ceil(scheduler.seconds_until_next)} 秒后尝试下一个动作`;
  const catalogHint = catalog.error || (catalog.matches_avatar === false ? '动作库与当前形象不匹配，请刷新动作库' : catalog.configured === false ? '尚未配置动作库，请刷新动作库' : '');
  $('idleMotionsStatus').textContent = [status, scheduler.last_error || catalogHint || (!scheduler.enabled ? module.reason : '')].filter(Boolean).join(' · ');
}
$('idleMotionsPanel').addEventListener('input', event => {
  if (!event.target.matches('input')) return;
  if (!idleDraftDirty) idleDraftGeneration = current?.session?.generation;
  idleDraftDirty = true; if (current) render(current);
});
$('idleMotionsPanel').addEventListener('click', event => {
  const button = event.target.closest('button[data-action-id]');
  if (button && !button.disabled && current) act('idle-motion-test', {generation:current.session.generation, action_id:button.dataset.actionId}, '已提交单次试播，请观察画面');
});
$('idleMotionsToggle').addEventListener('click', async () => {
  if (!current || $('idleMotionsToggle').disabled) return;
  const generation = current.session.generation;
  const pending = idleEnablePending?.generation === generation ? idleEnablePending : null;
  if (pending) {
    // The server invalidates this enable even while its library check is in flight.
    pending.cancelled = true;
    idleStopRequest('idle-motions', {generation, enabled:false}, '已提交取消开启');
    return;
  }
  if (current.studio?.idle_motions?.scheduler?.enabled) {
    idleStopRequest('idle-motions', {generation, enabled:false}, '已提交关闭待机');
    return;
  }
  const attempt = {generation, cancelled:false};
  idleEnablePending = attempt; busy = true; render(current);
  try {
    await request('idle-motions', {generation, enabled:true});
    if (!attempt.cancelled && current?.session?.generation === generation) showNotice('待机已开启', true);
    await poll();
  } catch (error) {
    if (!attempt.cancelled && current?.session?.generation === generation) showNotice(`操作未完成：${error.message}`);
  } finally {
    if (idleEnablePending === attempt) idleEnablePending = null;
    busy = false; if (current) render(current);
  }
});
$('idleMotionsSave').addEventListener('click', async () => {
  if (!current || $('idleMotionsSave').disabled) return;
  const min = Number($('idleMin').value), max = Number($('idleMax').value);
  if (!Number.isInteger(min) || !Number.isInteger(max) || min < 5 || max > 300 || max < min) { showNotice('间隔需为5～300秒，最长间隔不能小于最短间隔。'); return; }
  const enabled_ids = [...$('idleMotionActions').querySelectorAll('input:checked')].map(input => input.dataset.id);
  const generation = current.session.generation;
  busy = true; render(current);
  try {
    await request('idle-motions-config', {generation, min_interval_seconds:min, max_interval_seconds:max, enabled_ids});
    // Invalidate older polls before reading back the saved values.
    idlePollRevision++;
    const data = await request('state');
    const saved = data.studio?.idle_motions?.catalog, selected = records(saved?.actions).filter(action => action.enabled).map(action => String(action.id)).sort();
    if (data.session?.generation !== generation || saved?.settings?.min_interval_seconds !== min || saved?.settings?.max_interval_seconds !== max || JSON.stringify(selected) !== JSON.stringify([...enabled_ids].sort())) throw new Error('保存后状态尚未确认，请检查设置');
    idleDraftDirty = false; idleCatalogSignature = ''; connected = true; render(data); showNotice('待机设置已保存', true);
  } catch (error) { showNotice(`设置尚未确认：${error.message}`); }
  finally { busy = false; if (current) render(current); }
});
$('idleMotionsSync').addEventListener('click', () => { if (current && !$('idleMotionsSync').disabled) act('idle-motions-sync', {generation:current.session.generation}, '动作库已刷新'); });
$('idleMotionsRecover').addEventListener('click', () => { if (current && !$('idleMotionsRecover').disabled) idleStopRequest('idle-motions-recover', {generation:current.session.generation}, '已提交回正'); });
function render(data) {
  current = data; const session = data.session || {}, studio = data.studio || {}, brain = data.brain || {}, player = studio.player || {}, totals = data.totals || {}, cloud = session.cloud || {};
  const preview = studio.receiver_mode === 'preview';
  const shortTest = studio.short_performance_test === true;
  const continuousTest = studio.continuous_speech_test === true;
  const longTest = studio.long_speech_test === true;
  const replay = studio.fixed_replay;
  const dialoguePolicy = studio.dialogue_action_policy;
  $('semanticSpeechHint').hidden = studio.semantic_speech !== true || !!replay;
  $('semanticSpeechHint').textContent = dialoguePolicy?.name === 'A'
    ? `口播默认动作：A 方案。眼神、表情提前 ${fixed(dialoguePolicy.ge_lead_seconds, 1)} 秒，短动作后 ${fixed(dialoguePolicy.short_to_q_seconds, 1)} 秒恢复坐姿；随本次台词安排。`
    : '动作随台词内容安排；回复记录可查看动作对应的语句与时间。';
  $('shortPerformanceHint').hidden = !shortTest;
  $('continuousSpeechHint').hidden = !continuousTest;
  $('longSpeechHint').hidden = !longTest || !!replay;
  $('fixedReplayPanel').hidden = !replay;
  $('directorForm').hidden = !!replay;
  document.querySelector('.director-topic').hidden = !!replay;
  $('pause').hidden = !!replay;
  $('avatarsViewButton').disabled = !!replay;
  document.querySelector('.comments .section-head p').textContent = replay
    ? (replay.semantic_speech
      ? (replay.minute_replay
        ? `${fixed(replay.audio_seconds, 2)} 秒连续口播，六个随台词发生的动作；录制后导出 60 秒视频。`
        : `共用同一份 ${fixed(replay.audio_seconds, 2)} 秒音频与 ${replay.actions.length} 处动作，观察连续表达、动作衔接与恢复坐姿。`)
      : replay.action_kind === 'L'
      ? (replay.long_action_group_count === 2 ? '复用原音频和台词，每两个原动作组成一条 L，两组之间发 Q 回到正常坐姿。' : '复用原音频和台词，将原四个动作合成一条完整 L 描述。')
      : '固定对照仅改变动作组间距；原音频、台词与动作保持一致。')
    : '观众互动优先，主讲在空档接续。暂停接单与暂停主讲分别控制。';
  if (replay) {
    const semanticReplay = replay.semantic_speech === true;
    const longAction = replay.action_kind === 'L';
    const pairedLongAction = longAction && replay.long_action_group_count === 2;
    $('fixedReplayTitle').textContent = semanticReplay ? replay.comparison_label : pairedLongAction
      ? (replay.second_l_anchor ? `双 L · L₂ 对齐“${replay.second_l_anchor}” ${fixed(replay.second_l_audio_seconds, 2)} 秒`
        : replay.first_q_after_seconds === 3 ? '双 L · 第一 Q 提前至 3 秒' : '双 L 分组对照')
      : longAction ? '单 L 长动作对照' : '5 秒间距对照';
    $('fixedReplaySummary').textContent = semanticReplay
      ? `${replay.minute_replay ? '沿用A方案，两段真实语音拼成一段连续播放。' : ''}${replay.independent_ge ? `先发眼神表情，${fixed(replay.ge_lead_seconds, 1)} 秒后发动作。` : '眼神、表情与动作合在同一条指令中发送。'}${replay.actions.length} 处 S 在 ${replay.s_seconds.map(n => fixed(n, 2)).join('、')} 秒开始，各自 1.5 秒后发恢复指令；原音频 ${fixed(replay.audio_seconds, 2)} 秒完整播放。`
      : pairedLongAction
      ? `原样本 ${display(replay.source_comment_id)} · 音频 ${fixed(replay.audio_seconds, 2)} 秒。第一组 L 发出 ${fixed(replay.group_holds_seconds?.[0], 2)} 秒后发 Q，至少 ${fixed(replay.q_to_next_l_seconds, 2)} 秒后发第二组 L（中间含 GE 前导）；共 6 条 Type9。第二组 L 计划在语音开始后 ${fixed(replay.second_l_audio_seconds, 2)} 秒发送，末尾 Q 仍在 ${fixed(replay.last_q_audio_seconds, 2)} 秒；实际以成功写入重新计时。`
      : longAction
      ? `原样本 ${display(replay.source_comment_id)} · 音频 ${fixed(replay.audio_seconds, 2)} 秒。GE 后 0.5 秒发 L 并开始语音，L 发出 ${fixed(replay.hold_seconds, 2)} 秒后发 Q；全段仅 3 条 Type9。`
      : `原样本 ${display(replay.source_comment_id)} · 音频 ${fixed(replay.audio_seconds, 2)} 秒 · 组间距 ${display(replay.group_interval)} 秒。每组 GE→S 仍为 0.5 秒，S→Q 仍为 1.5 秒。`;
    $('fixedReplayContext').textContent = replay.interaction_context ? `互动情境：${replay.interaction_context}` : '';
    $('fixedReplayText').textContent = replay.text;
    $('fixedReplayActions').textContent = semanticReplay
      ? replay.actions.map((action, index) => `S${index + 1}：${action} → 恢复坐姿`).join(' → ')
      : pairedLongAction
      ? `L₁：${replay.actions[0]} → Q：正常坐姿 → L₂：${replay.actions[1]} → Q：正常坐姿`
      : `${longAction ? 'L 动作描述' : '原动作顺序'}：${replay.actions.join(' → ')}`;
  }
  renderReplayCommands(replay, session, studio, data.comments);
  $('replaySample').disabled = busy || !connected || replay?.available !== true || !replayVersionKnown(replay, studio);
  // Preserve the operator's selected input across state polls; only the
  // continuous topic mode is outside this single-S trial.
  if (shortTest && inputMode === 'topic') inputMode = 'comment';
  if (continuousTest && ['topic', 'motion'].includes(inputMode)) inputMode = 'comment';
  if (longTest && ['topic', 'motion'].includes(inputMode)) inputMode = 'comment';
  for (const button of inputModeButtons) button.setAttribute('aria-pressed', String(button.dataset.mode === inputMode));
  if (shortTest || continuousTest || longTest) {
    $('directorRoundsWrap').hidden = true;
    const limit = inputMode === 'comment' ? 160 : 600;
    $('inputLimit').textContent = `当前输入 ${$('directorText').value.length} / ${limit} 字${$('directorText').value.length > limit ? '，请删减后提交' : ''}${longTest ? ' · 生成口播上限 120 字' : ''}`;
  }
  document.body.classList.toggle('preview-mode', preview);
  $('previewPanel').hidden = !preview;
  // Attach once: replacing the iframe on every poll would release the only
  // RTC receiver and correctly trigger a protective stop of the paid session.
  if (preview && studio.obs_url && !$('previewFrame').getAttribute('src')) $('previewFrame').src = studio.obs_url + '?preview=1';
  $('receiverLabel').textContent = preview ? '工作台播放页' : 'OBS 播放页';
  $('receiverDescription').textContent = preview ? '本次测试在右侧预览中播放画面和声音。' : '画面和声音通过 OBS 播放。';
  $('receiverHelp').textContent = preview ? '本次为临时预览模式；正常重启工作台后恢复 OBS 接收。' : '将地址添加到 OBS 浏览器源；本页面不播放画面。';
  $('copyObs').hidden = preview;
  const state = String(session.state || '').toLowerCase(), idle = isIdle(state), active = !['idle','ended'].includes(state) && (data.started === true || ['creating','connecting','waiting_rtc','waiting_stage','ready','sending','draining','stopping'].includes(state));
  // Creation/connection is pending, and an unconfirmed end is not a green live signal.
  const sessionStyle = ['error','failed','stop_unconfirmed','creation_unknown'].includes(state) ? 'bad'
    : ['ready','sending','draining'].includes(state) ? 'good' : active ? 'warn' : 'neutral';
  setStatus('sessionStatus', stateLabel(state), sessionStyle);
  $('sessionDetail').textContent = session.error ? `会话错误：${session.error}` : session.live_id ? `会话 ID：${session.live_id}` : '启动后可在这里查看当前会话状态。';
  $('blocking').textContent = studio.blocking_reason ? `暂不能开始：${studio.blocking_reason}` : '';
  const avatarSwitch = studio.avatar_switch || {}, avatarHolding = avatarSwitch.holding === true;
  const businessHolding = avatarHolding;
  const avatarSwitchError = avatarSwitch.receipt?.status === 'failed' ? avatarSwitch.receipt.error_code : '';
  const avatarSwitchHint = avatarHolding ? '请先查看转场后的动态画面；确认后恢复表演' : (avatarSwitch.reason || '会话就绪且无待处理任务时可重置当前形象');
  $('avatarSwitch').textContent = avatarHolding ? '确认画面并继续' : '重置当前形象';
  $('avatarSwitch').title = [avatarSwitchHint, avatarSwitchError ? `错误：${avatarSwitchError}` : ''].filter(Boolean).join('；');
  $('avatarSwitchStatus').textContent = [avatarHolding ? '已发送切换，等待你确认画面' : '', avatarSwitch.receipt?.status === 'failed' ? `切换失败：${avatarSwitchError || '原因未知'}` : ''].filter(Boolean).join('；');
  $('avatarSwitch').disabled = !!replay || !connected || busy || avatarSwitchBusy || !(avatarHolding ? avatarSwitch.resume_available : avatarSwitch.available);
  $('idleMotionsPanel').hidden = !!replay || shortTest || continuousTest || longTest;
  renderIdleMotions(studio, session, ['ready','sending','draining'].includes(state), businessHolding);
  $('start').disabled = busy || !connected || studio.can_start !== true || !replayVersionKnown(replay, studio);
  $('stop').disabled = busy || !connected || !active || state === 'stopping';
  $('newSession').hidden = state !== 'ended' && studio.can_new !== true; $('newSession').disabled = busy || !connected || studio.can_new !== true;
  $('pause').disabled = busy || !connected || !active; $('pause').textContent = studio.paused ? '恢复接单' : '暂停接单';
  const acceptsComments = ['ready','sending','draining'].includes(state) && !studio.paused && !businessHolding;
  const director = data.director || {}, topic = director.topic;
  const acceptsDirector = ['ready','sending','draining'].includes(state) && !businessHolding;
  for (const button of inputModeButtons) button.disabled = busy || businessHolding || !connected || (shortTest && button.dataset.mode === 'topic') || ((continuousTest || longTest) && ['topic', 'motion'].includes(button.dataset.mode));
  $('directorRounds').disabled = busy || !connected || !acceptsDirector;
  const acceptsInput = inputMode === 'comment' ? acceptsComments : acceptsDirector;
  $('directorText').disabled = $('directorSubmit').disabled = busy || !connected || !acceptsInput;
  renderDirector(topic, {enabled: !busy && connected && acceptsDirector});
  $('audioSeconds').textContent = totals.audio_seconds == null ? '—' : `${fixed(totals.audio_seconds, 1)} 秒`;
  $('cost').textContent = totals.cost_cny == null ? '—' : `¥ ${fixed(totals.cost_cny, 6)}`;
  $('cloudCost').textContent = cloud.credits_cost == null && cloud.billed_seconds == null ? '—' : `${display(cloud.credits_cost)} 积分 / ${fixed(cloud.billed_seconds, 1)} 秒`;
  $('obsStatus').textContent = player.connected ? `已连接 · 音频${player.audio_live ? '正常' : '未就绪'} / 画面${player.video_live ? '正常' : '未就绪'}` : '未连接';
  $('obsDot').className = `dot ${player.connected ? 'good' : 'bad'}`;
  $('brainProvider').textContent = replay ? '固定回放' : brain.provider === 'bailian' ? '百炼大脑' : brain.provider === 'cloudbase' ? 'CloudBase 大脑' : '大脑';
  $('brainModel').textContent = replay ? '复用原音频' : display(brain.model, '模型未配置');
  $('brainStatus').textContent = !brain.configured ? '配置未就绪' : brain.last_error ? '模型处理已暂停，等待手动恢复' : brain.busy ? '配置就绪 · 正在处理模型请求' : brain.worker_running ? '配置就绪 · 本机工作循环运行' : '配置就绪 · 工作循环未运行';
  if (replay) $('brainStatus').textContent = '本轮复用固定样本，不调用模型或语音合成';
  $('brainDot').className = `dot ${brain.last_error || !brain.configured ? 'bad' : 'warn'}`;
  const requests = brain.requests || {}, usage = brain.usage || {};
  $('brainRequests').textContent = `请求：筛选 ${count(requests.selection)} / 生成 ${count(requests.reply)} · 已报告 Token ${count(usage.selection?.total_tokens)} / ${count(usage.reply?.total_tokens)}`;
  $('brainError').textContent = brain.last_error ? `模型错误：${display(brain.last_error)}。不会自动重试，请检查后手动恢复。` : '';
  $('resumeAI').hidden = !brain.last_error;
  $('resumeAI').disabled = busy || !connected || !brain.last_error || !brain.configured;
  $('copyObs').disabled = !studio.obs_url;
  const settingsEditable = studio.settings_editing_allowed === true || (studio.settings_editing_allowed == null && idle);
  for (const id of ['name','persona','replyStyle']) $(id).disabled = busy || !connected || !settingsEditable;
  $('saveSettings').disabled = busy || !connected || !settingsEditable;
  if (studio.settings) syncRoleSettings(studio.settings, studio.avatar_current_id || studio.avatar_selected_id || null);
  document.dispatchEvent(new CustomEvent('studio-state-updated'));
  renderIntake(data, state, studio.paused === true);
  if (replay) $('intakeDetail').textContent = '本次为固定素材对照，仅播放已核验的原样本。';
  renderComments(records(data.comments), session.generation);
  const recent = data.diagnostics && Array.isArray(data.diagnostics.recent) ? data.diagnostics.recent : [];
  const openDiagnostics = new Set([...$('diagnostics').querySelectorAll('details[open]')].map(el => el.dataset.id));
  const diagnosticScroller = $('diagnostics').closest('.menu-popover');
  const diagnosticScroll = diagnosticScroller.scrollTop;
  $('diagnostics').replaceChildren(); if (!recent.length) addText($('diagnostics'), 'p', '暂无诊断记录。', 'empty');
  else for (const entry of [...recent].reverse()) {
    const eventNames = {long_audio_rejected:'长文音频不足以容纳动作', continuous_audio_rejected:'连续口播音频过短', llm_plan_rejected:'模型计划校验失败', brain_ai_error:'模型处理错误', cloudbase_ai_error:'模型处理错误', state:'会话状态变化', audio_start:'开始发送音频', audio_end:'音频发送结束', audio_interrupted:'音频发送中断', ws_received:'收到连接消息', ws_sent:'发送连接消息', ws_attempt:'准备发送消息', ws_failed:'发送消息失败', ws_transport:'连接状态变化', ws_ping_sent:'连接心跳', diagnostics_closed:'诊断记录已关闭', diagnostics_disk_write_failed:'诊断日志写入失败'};
    const item = document.createElement('details'); item.className = 'diagnostic'; item.dataset.id = String(entry.id); item.open = openDiagnostics.has(item.dataset.id);
    const summary = document.createElement('summary'); summary.textContent = `${localTime(entry.time)} · ${eventNames[entry.kind] || '诊断事件'}${eventNames[entry.kind] ? '' : `（${display(entry.kind)}）`}`; item.appendChild(summary);
    if (entry.details && Object.keys(entry.details).length) addText(item, 'p', JSON.stringify(entry.details, null, 2));
    else addText(item, 'p', '无附加详情。');
    $('diagnostics').appendChild(item);
  }
  diagnosticScroller.scrollTop = diagnosticScroll;
}
async function poll() {
  if (polling) return; polling = true;
  try { const revision = idlePollRevision; const data = await request('state'); if (revision !== idlePollRevision) return; connected = true; render(data); setStatus('connection', '工作台已连接', 'good'); networkFailed = false; }
  catch (error) { connected = false; if (current) render(current); setStatus('connection', '工作台连接中断', 'bad'); if (!networkFailed) showNotice(`无法读取工作台状态：${error.message}。请检查本机服务。`); networkFailed = true; $('start').disabled = $('stop').disabled = $('pause').disabled = $('directorSubmit').disabled = $('saveSettings').disabled = true; }
  finally { polling = false; }
}
async function act(path, body, success) {
  if (busy) return; busy = true;
  if (path === 'comments' || path.startsWith('director/')) $('inputError').hidden = true;
  if (current) render(current);
  try { await request(path, body); if (success && path !== 'comments' && !path.startsWith('director/')) showNotice(success, true); await poll(); return true; }
  catch (error) {
    // Requests rejected before enqueueing have no queue entry yet. Keep their
    // failure next to the input, while accepted entries show their own errors.
    if (path === 'comments') {
      $('inputError').textContent = `未能确认入队：${error.message}。请查看回复队列后再决定是否重发。`;
      $('inputError').hidden = false;
    } else if (path.startsWith('director/')) {
      $('inputError').textContent = `操作未完成：${error.message}。请查看当前状态后再决定是否重试。`;
      $('inputError').hidden = false;
    } else showNotice(`操作未完成：${error.message}`);
    return false;
  }
  finally { busy = false; if (current) render(current); }
}
$('avatarSwitch').addEventListener('click', async () => {
  const state = current?.studio?.avatar_switch;
  const holding = state?.holding === true;
  if (!connected || busy || avatarSwitchBusy || !(holding ? state?.resume_available : state?.available)) return;
  // Capture the generation with the click; the server rejects a stale session.
  const generation = current.session.generation;
  avatarSwitchBusy = true;
  render(current);
  try {
    if (holding) {
      await request('avatar-switch/resume', {generation});
      showNotice('已恢复接收表演', true);
    } else {
      const receipt = await request('avatar-switch', {generation, request_id:crypto.randomUUID()});
      showNotice(receipt.status === 'failed' ? `切换失败：${receipt.error_code || '原因未知'}` : receipt.sent === true ? 'Type16 已发送；请观察转场后的动态画面，再点继续表演' : `本次切换未发送${receipt.error_code ? `：${receipt.error_code}` : '，请查看状态后再试'}`, receipt.status !== 'failed' && receipt.sent === true);
    }
    await poll();
  } catch (error) { showNotice(`操作未完成：${error.message}`); }
  finally { avatarSwitchBusy = false; if (current) render(current); }
});
$('start').addEventListener('click', () => act('start', {}, '已提交开始请求'));
$('newSession').addEventListener('click', async () => { if (await act('new', {}, '新会话已创建')) { settingsLoaded = false; await poll(); } });
$('stop').addEventListener('click', () => act('stop', {}, '已提交结束请求'));
$('replaySample').addEventListener('click', () => act('replay', {
  generation: current?.session?.generation,
  fingerprint: current?.studio?.fixed_replay?.fingerprint,
  request_id: crypto.randomUUID(),
}, '对照样本已入队'));
$('pause').addEventListener('click', () => act('pause', {paused: !current?.studio?.paused}, current?.studio?.paused ? '已恢复接单' : '已暂停接单'));
function updateInputMode() {
  const mode = inputMode, limit = mode === 'comment' ? 160 : 600;
  for (const button of inputModeButtons) button.setAttribute('aria-pressed', String(button.dataset.mode === mode));
  $('directorRoundsWrap').hidden = mode !== 'topic';
  $('inputLimit').textContent = `当前 ${$('directorText').value.length} / ${limit} 字${$('directorText').value.length > limit ? '，请删减后提交' : ''}${mode === 'topic' ? ' · 建立有限提纲，互动空档自动讲述；重新提交即更换话题。' : ''}`;
  if (current) render(current);
}
for (const button of inputModeButtons) button.addEventListener('click', () => {
  inputMode = button.dataset.mode;
  updateInputMode();
});
$('directorText').addEventListener('input', updateInputMode);
$('directorForm').addEventListener('submit', async event => {
  event.preventDefault();
  const text = $('directorText').value.trim(), mode = inputMode;
  const limit = mode === 'comment' ? 160 : 600;
  if (!text) return;
  if ($('directorText').value.length > limit) { $('inputError').textContent = `当前方式最多输入 ${limit} 字，请删减后提交。`; $('inputError').hidden = false; return; }
  const body = mode === 'comment' ? {text} : {text, mode};
  if (mode === 'topic') body.rounds = Number($('directorRounds').value);
  if (await act(mode === 'comment' ? 'comments' : 'director/submit', body)) { $('directorText').value = ''; updateInputMode(); }
});
$('directorPause').addEventListener('click', () => act('director/pause', {}));
$('directorContinue').addEventListener('click', () => act('director/continue', {}));
$('directorStop').addEventListener('click', () => act('director/stop', {}));
updateInputMode();
$('settingsForm').addEventListener('submit', async event => {
  event.preventDefault();
  const path = settingsAssetId ? `avatars/${encodeURIComponent(settingsAssetId)}/settings` : 'settings';
  if (await act(path, {name:$('name').value.trim(), persona:$('persona').value.trim(), reply_style:$('replyStyle').value.trim()}, '角色设置已保存')) {
    quickRoleDrafts.delete(settingsAssetId); settingsDirty = false; settingsLoaded = false; $('quickRoleDraft').hidden = true;
    document.dispatchEvent(new CustomEvent('studio-role-saved')); await poll();
  }
});
$('intakeSettingsForm').addEventListener('submit', async event => {
  event.preventDefault();
  const baseUrl = $('intakeBaseUrl').value.trim(), roomId = $('intakeRoomId').value.trim();
  // Restrict this operator-supplied address to an explicit loopback port. The
  // server independently enforces the same boundary before subscribing.
  const match = /^http:\/\/127\.0\.0\.1:([1-9][0-9]{0,4})\/?$/.exec(baseUrl);
  if (!match || Number(match[1]) > 65535 || !/^[0-9]{1,32}$/.test(roomId)) {
    showNotice('请填写 http://127.0.0.1:端口（1–65535）和纯数字房间 ID。'); return;
  }
  await act('intake/settings', {base_url:baseUrl.replace(/\/$/, ''), room_id:roomId}, '来源设置已保存；尚未开始接收');
});
$('startIntake').addEventListener('click', () => act('intake/start', {}, '已请求连接评论来源；会话就绪后才筛选'));
$('stopIntake').addEventListener('click', () => act('intake/stop', {}, '已停止评论订阅；共享 DyHub 服务继续保留'));
$('resumeAI').addEventListener('click', () => act('ai/resume', {}, '已请求恢复模型处理；失败请求不会自动重发'));
$('copyObs').addEventListener('click', async () => { try { await navigator.clipboard.writeText(current.studio.obs_url); showNotice('OBS 播放地址已复制', true); } catch { showNotice('复制失败，请检查浏览器的剪贴板权限。'); } });
poll(); setInterval(poll, 2000);
// 心跳独立于状态轮询；失败由状态连接提示承担，避免重复刷屏。
setInterval(() => { request('heartbeat', {}).catch(() => {}); }, 5000);
