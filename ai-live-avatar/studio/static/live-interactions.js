'use strict';
(() => {
  const el = id => document.getElementById(id);
  if (!el('liveInteractionsPanel')) return;
  const clone = value => JSON.parse(JSON.stringify(value));
  const normalizeText = value => value.trim().replace(/\s+/gu, ' ');
  const canonical = value => JSON.stringify(value, (_, item) => item && typeof item === 'object' && !Array.isArray(item)
    ? Object.fromEntries(Object.entries(item).sort(([a], [b]) => a.localeCompare(b))) : item);
  const parts = {head:'头部', face:'面部', left_arm:'左臂', right_arm:'右臂', both_arms:'双臂', torso:'上身'};
  const modes = {speech:'感谢词＋动作', motion:'只做动作'};
  let draft = null, dirty = false, signature = '', submitting = false, available = false;
  let editRevision = 0;

  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text) element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  function field(container, title, key, value, options = {}) {
    const label = node('label', title), input = node(options.choices ? 'select' : 'input');
    input.dataset.field = key;
    if (options.choices) for (const [value, title] of Object.entries(options.choices)) {
      const option = node('option', title); option.value = value; input.appendChild(option);
    }
    else {
      input.type = options.type || 'text';
      if (options.maxLength) input.maxLength = options.maxLength;
      if (input.type === 'number') { input.min = '0'; input.max = '3600'; input.step = '1'; }
    }
    if (input.type === 'checkbox') { input.checked = value === true; label.className = 'interaction-check'; label.prepend(input); }
    else { input.value = value ?? ''; label.appendChild(input); }
    container.appendChild(label);
    return input;
  }
  function drawRule(rule, index, fallback = false) {
    const details = node('details', '', 'gift-rule'); details.dataset.ruleIndex = String(index);
    if (fallback) details.dataset.fallback = 'true';
    const summary = node('summary', fallback ? '默认反馈内容' : rule.name || '新礼物规则'); details.appendChild(summary);
    field(details, '启用这条规则', 'enabled', rule.enabled !== false, {type:'checkbox'});
    if (!fallback) {
      field(details, '礼物名称', 'name', rule.name, {maxLength:64}).required = true;
      field(details, '别名（用逗号分隔）', 'aliases', (rule.aliases || []).join('，'), {maxLength:600});
    }
    const grid = node('div', '', 'interaction-grid'); details.appendChild(grid);
    field(grid, '反馈方式', 'mode', rule.mode || 'speech', {choices:modes});
    field(grid, '动作部位', 'part', rule.part || 'head', {choices:parts});
    field(details, '动作描述（可留空）', 'action', rule.action, {maxLength:70});
    field(details, '感谢词', 'template', rule.template, {maxLength:4096});
    details.appendChild(node('p', '用 {nickname} 插入昵称，{gift} 插入礼物名称。只做动作时可留空；实际台词超过120字会略过，请用规则检查核对。', 'footnote'));
    field(details, '同一观众与礼物的冷却间隔（秒）', 'cooldown_seconds', rule.cooldown_seconds ?? 15, {type:'number'});
    if (!fallback) {
      const advanced = node('details', '', 'interaction-advanced'); advanced.appendChild(node('summary', '高级匹配'));
      field(advanced, '礼物 ID（可选）', 'id', rule.id, {maxLength:256});
      advanced.appendChild(node('p', '有真实 ID 时优先按 ID 匹配；留空时按名字或别名精确匹配。', 'footnote'));
      details.appendChild(advanced);
      const remove = node('button', '移除这条规则'); remove.type = 'button'; remove.dataset.removeRule = String(index); details.appendChild(remove);
    }
    return details;
  }
  function drawSettings() {
    if (!draft) return;
    el('giftEnabled').checked = draft.gift_enabled === true;
    el('welcomeEnabled').checked = draft.welcome_enabled === true;
    el('giftFallbackEnabled').checked = draft.gift_fallback_enabled === true;
    el('welcomeTemplate').value = draft.welcome_template || '';
    el('welcomeCooldown').value = draft.welcome_cooldown_seconds ?? 0;
    el('giftRules').replaceChildren(...(draft.gift_rules || []).map((rule, index) => drawRule(rule, index)));
    el('giftFallbackRule').replaceChildren(drawRule(draft.gift_fallback || {}, -1, true));
  }
  function readRule(container, original) {
    const rule = {...original};
    for (const input of container.querySelectorAll('[data-field]')) {
      const key = input.dataset.field;
      rule[key] = input.type === 'checkbox' ? input.checked : key === 'cooldown_seconds' ? Number(input.value)
        : key === 'aliases' ? [...new Set(input.value.split(/[,，\n]/).map(normalizeText).filter(Boolean))] : normalizeText(input.value);
    }
    return rule;
  }
  function readSettings() {
    if (!draft) return null;
    return {...draft, gift_enabled:el('giftEnabled').checked, welcome_enabled:el('welcomeEnabled').checked,
      gift_fallback_enabled:el('giftFallbackEnabled').checked, welcome_template:normalizeText(el('welcomeTemplate').value),
      welcome_cooldown_seconds:Number(el('welcomeCooldown').value),
      gift_rules:[...el('giftRules').children].map(row => readRule(row, draft.gift_rules[Number(row.dataset.ruleIndex)])),
      gift_fallback:readRule(el('giftFallbackRule'), draft.gift_fallback)};
  }
  function markDirty() {
    dirty = true; editRevision++;
    el('interactionsSettingsResult').textContent = '';
    update();
  }
  function queueCount(value) { return Array.isArray(value) ? value.length : Number.isFinite(value) ? value : '—'; }
  function canEnqueue() {
    const data = current || {}, state = data.interactions || {};
    return connected && available && state.can_enqueue !== false
      && ['ready','sending','draining'].includes(String(data.session?.state || '').toLowerCase())
      && !data.studio?.paused && !data.studio?.avatar_switch?.holding
      && !data.studio?.performance_holding && !data.brain?.last_error;
  }
  function update() {
    const data = current || {}, interaction = data.interactions;
    available = !!interaction?.settings && interaction.supported !== false;
    // A two-second state refresh must not replace an operator's unfinished form.
    const incoming = interaction?.settings ? JSON.stringify(interaction.settings) : '';
    if (interaction?.settings && (!draft || (!dirty && !submitting && incoming !== signature))) {
      draft = clone(interaction.settings); signature = incoming; drawSettings();
    }
    const locked = submitting || busy || !connected || !available;
    el('interactionsSettingsFields').disabled = locked;
    el('interactionSimulateFields').disabled = locked;
    el('saveInteractions').disabled = locked || !dirty;
    el('resetInteractionsDraft').disabled = locked || !dirty;
    el('addGiftRule').disabled = locked || (draft?.gift_rules?.length || 0) >= 40;
    el('interactionsDraft').hidden = !dirty;
    el('interactionsBadge').className = `status ${!connected ? 'bad' : available ? 'neutral' : 'warn'}`;
    el('interactionsBadge').textContent = !connected ? '工作台未连接' : interaction?.supported === false ? '专项模式不支持' : available ? '功能已加载' : '待重启工作台';
    el('interactionsStatus').textContent = !connected ? '无法读取后台状态，恢复连接后再操作。'
      : interaction?.supported === false ? '当前为专项试验或固定回放模式，不支持礼物与欢迎；请在正常工作台模式使用。'
      : !available ? '当前后台未加载礼物与欢迎功能。需要重启工作台服务；此时保存与模拟均未生效。'
      : interaction.saturated?.length ? '部分新礼物暂时被限流；记录到期后自动恢复，已排队内容与欢迎继续处理'
      : '功能已加载。真实事件沿用上方来源订阅；未连接来源时，可先做规则检查。';
    el('giftQueueCount').textContent = `礼物待处理 ${queueCount(interaction?.pending_gifts ?? interaction?.gift_pending)}`;
    el('welcomeQueueCount').textContent = `欢迎待选 ${queueCount(interaction?.pending_welcomes ?? interaction?.welcome_pending)} · 准备／发送 ${queueCount(interaction?.welcome_occupied_count)} · 已欢迎 ${queueCount(interaction?.welcome_seen_count)}`;
    const visible = draft ? readSettings() : {};
    el('giftEnabledSummary').textContent = !draft ? '待加载' : visible.gift_enabled ? '已勾选' : '未勾选';
    el('welcomeEnabledSummary').textContent = !draft ? '待加载' : visible.welcome_enabled ? '已勾选' : '未勾选';
    const cooldown = visible.welcome_cooldown_seconds || 0;
    el('welcomePolicy').textContent = `仅欢迎等级严格大于 10 的观众；等级未知不欢迎；多人合并、少量点名，整轮信号发完才记为已欢迎。${cooldown > 0 ? `同一观众间隔 ${cooldown} 秒可再次欢迎。` : '同一观众本场只欢迎一次。'}`;
    const member = el('interactionSimKind').value === 'member', dryRun = el('interactionSimDryRun').checked;
    el('interactionSimGiftFields').hidden = member;
    el('interactionSimMemberFields').hidden = !member;
    el('interactionSimAdvanced').hidden = member;
    el('interactionSimGiftName').required = !member;
    el('interactionSimMode').classList.toggle('actual', !dryRun);
    el('interactionSimMode').textContent = dirty ? '检查使用已保存规则，请先保存修改；也可撤回修改后检查。'
      : dryRun ? '规则检查：不播放，不占用真实队列；未开场也可使用。'
      : canEnqueue() ? '实际排队：本次模拟会加入本场互动队列，由数字人执行。' : '实际排队：需要会话就绪、恢复接单且模型无错误；现在不能加入队列。';
    el('simulateInteraction').textContent = submitting ? '正在处理…' : dryRun ? '检查规则' : '加入本场播放队列';
    el('simulateInteraction').disabled = locked || dirty || (!dryRun && !canEnqueue());
  }
  function describeResult(response, dryRun) {
    const area = el('interactionSimResult'), result = response.result || {};
    area.replaceChildren(node('strong', dryRun ? '规则检查完成 · 未排队、未播放' : '实际排队请求已处理'));
    const labels = {accepted:'已接收', queued:dryRun ? '符合排队条件' : '已排队', ignored:'已略过', skipped:'已略过', rejected:'未接收', duplicate:'重复事件', cooldown:'处于冷却间隔', disabled:'功能关闭', merged:'已合并', eligible:'符合规则', matched:'匹配规则', expired:'事件已过期'};
    const reasons = {gift_queued:'礼物已匹配反馈规则', welcome_queued:'入场观众符合欢迎条件', gift_group_seen:'这组连送礼物已经处理', duplicate_message:'这条事件已经处理', welcome_already_seen:'这位观众本场已经欢迎过', welcome_pending:'这位观众的欢迎已在队列中', gift_cooldown:'同一观众的这类礼物仍在冷却间隔内', gift_rule_disabled:'匹配的礼物规则已关闭，本次不反馈', disabled:'相应互动功能已关闭', unmatched_gift:'没有匹配的礼物规则，且未启用默认反馈', queue_full:'待处理队列已满', dedupe_capacity:'暂时达到互动冷却容量，本次略过；记录到期后恢复', gift_group_capacity:'连击记录暂时已满，本次新礼物略过；记录到期后恢复', speech_text_too_long:'实际互动台词超过120字，本次略过；请缩短模板', level_not_above_10:'等级未知或不大于 10，不欢迎', not_confirmed_entry:'事件未确认是进入直播间', event_expired:'事件已超过处理时限'};
    if (result.status) area.appendChild(node('p', `结果：${labels[result.status] || '已处理，请查看说明'}`));
    if (result.reason) area.appendChild(node('p', `说明：${reasons[result.reason] || (String(result.reason).startsWith('invalid_') ? '模拟内容不完整或格式不正确，请检查输入' : /[\u4e00-\u9fff]/.test(result.reason) ? result.reason : '后台已完成判断，请查看规则与当前队列')}`));
    const preview = response.preview || {}, plan = response.plan || {};
    const text = plan.text || plan.speech || plan.template || preview.text || preview.template;
    if (typeof text === 'string' && text) area.appendChild(node('p', `台词：${text}`));
    const action = plan.action || preview.action;
    if (typeof action === 'string' && action) area.appendChild(node('p', `动作：${action}`));
    else if (Array.isArray(plan.motions || preview.motions)) {
      const motions = (plan.motions || preview.motions).map(motion => `${parts[motion.part] || '动作'}：${motion.description || ''}`).join('；');
      if (motions) area.appendChild(node('p', motions));
    }
    if (!dryRun) area.appendChild(node('p', '请查看队列与实际画面；请求成功不表示已播放完成。', 'footnote'));
  }
  async function perform(task, onError) {
    if (submitting || busy || !connected || !available) return;
    submitting = true; busy = true; render(current);
    try { await task(); }
    catch (error) { onError(error); }
    finally { submitting = false; busy = false; if (current) render(current); }
  }
  el('interactionsSettingsForm').addEventListener('input', markDirty);
  el('interactionsSettingsForm').addEventListener('change', markDirty);
  el('interactionsSettingsForm').addEventListener('invalid', event => {
    // Native validation must reveal a required input even inside folded rules.
    for (let parent = event.target.parentElement; parent; parent = parent.parentElement) {
      if (parent.tagName === 'DETAILS') parent.open = true;
    }
  }, true);
  el('addGiftRule').addEventListener('click', () => {
    draft = readSettings(); draft.gift_rules.push({enabled:true, id:'', name:'', aliases:[], part:'head', action:'', mode:'speech', template:'谢谢{nickname}送来的{gift}。', cooldown_seconds:15});
    drawSettings(); markDirty(); el('giftRules').lastElementChild.open = true;
    el('giftRules').lastElementChild.querySelector('[data-field="name"]').focus();
  });
  el('giftRules').addEventListener('click', event => {
    const remove = event.target.closest('button[data-remove-rule]');
    if (!remove || remove.disabled) return;
    draft = readSettings(); draft.gift_rules.splice(Number(remove.dataset.removeRule), 1); drawSettings(); markDirty();
  });
  el('resetInteractionsDraft').addEventListener('click', () => {
    if (!current?.interactions?.settings) return;
    dirty = false; draft = null; el('interactionsSettingsResult').textContent = '已撤回未保存的修改。'; update();
  });
  el('interactionsSettingsForm').addEventListener('submit', event => {
    event.preventDefault(); if (!dirty || el('saveInteractions').disabled) return;
    const settings = readSettings(), revision = editRevision;
    perform(async () => {
      await request('interactions/settings', settings);
      // Ignore any older in-flight poll, then verify the saved configuration.
      idlePollRevision++;
      const data = await request('state');
      if (!data.interactions?.settings) throw new Error('保存后未读到互动设置，请检查后台版本');
      if (canonical(data.interactions.settings) !== canonical(settings)) throw new Error('后台回读的设置与提交内容不一致，请核对后再保存');
      if (editRevision === revision) { dirty = false; draft = clone(data.interactions.settings); signature = JSON.stringify(draft); drawSettings(); }
      connected = true; render(data); el('interactionsSettingsResult').textContent = '互动设置已保存并回读。';
    }, error => { el('interactionsSettingsResult').textContent = `保存未确认：${error.message}。修改仍保留，可检查后重试。`; });
  });
  el('interactionSimulateForm').addEventListener('change', update);
  el('interactionSimulateForm').addEventListener('submit', event => {
    event.preventDefault(); if (el('simulateInteraction').disabled) return;
    const dryRun = el('interactionSimDryRun').checked;
    const body = {kind:el('interactionSimKind').value, nickname:el('interactionSimNickname').value.trim(), user_id:el('interactionSimUserId').value.trim(), dry_run:dryRun};
    if (body.kind === 'gift') {
      body.gift_name = el('interactionSimGiftName').value.trim(); body.repeat_count = Number(el('interactionSimCount').value);
      if (el('interactionSimGiftId').value.trim()) body.gift_id = el('interactionSimGiftId').value.trim();
      if (el('interactionSimGroupId').value.trim()) body.group_id = el('interactionSimGroupId').value.trim();
    } else if (el('interactionSimLevel').value !== '') body.level = Number(el('interactionSimLevel').value);
    perform(async () => {
      const response = await request('interactions/simulate', body); describeResult(response, dryRun); await poll();
    }, error => { el('interactionSimResult').textContent = `${dryRun ? '规则检查失败' : '未能确认排队'}：${error.message}${dryRun ? '' : '。请先查看队列，避免重复提交。'}`; });
  });
  document.addEventListener('studio-state-updated', update);
  update();
})();
