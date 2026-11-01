'use strict';
// 资产页与直播页共用文档和心跳；切页不导航，也不拆除唯一播放 iframe。
const renderCurrentState = () => render(current);
const avatarLibrary = (() => {
  let data = null, selectedId = null, visible = false, loading = false, actionBusy = false;
  let listSignature = '', detailId = null, uploadUrl = null, seenUnknownUploads = ''; 
  let pendingUpload = null;
  const drafts = new Map();
  const el = id => document.getElementById(id);
  const fields = {name:'assetName', persona:'assetPersona', reply_style:'assetReplyStyle'};
  const statusLabels = {ready:'形象已就绪', available:'形象已就绪', completed:'形象已就绪', success:'形象已就绪', processing:'处理中', pending:'等待处理', creating:'创建中', created:'已创建，等待就绪', failed:'创建失败', error:'处理失败', unknown:'状态待确认', creation_unknown:'创建结果未知', creation_failed:'创建失败'};
  const str = (value, fallback = '—') => value == null || value === '' ? fallback : String(value);
  const text = (parent, tag, value, className) => { const node = document.createElement(tag); node.textContent = value; if (className) node.className = className; parent.appendChild(node); return node; };
  const assetName = asset => str(asset?.name || asset?.settings?.name, '未命名数字人');
  const settingsComplete = item => Object.entries({name:30,persona:1000,reply_style:500}).every(([key,limit]) => typeof item?.settings?.[key] === 'string' && item.settings[key].trim().length > 0 && item.settings[key].trim().length <= limit);
  const unknownUpload = status => ['unknown','creation_unknown'].includes(status);
  const failedUpload = status => ['failed','creation_failed'].includes(status);
  const assets = () => Array.isArray(data?.assets) ? data.assets : [];
  const asset = id => assets().find(item => String(item.id) === String(id));
  const chosen = () => asset(selectedId);
  const imageUrl = item => `avatars/${encodeURIComponent(item.id)}/image?v=${encodeURIComponent(item.image_version || '0')}`;
  const imageReady = item => ['ready','available','completed','success'].includes(String(item?.status || '').toLowerCase());
  const ready = item => imageReady(item) && item?.image_available === true;
  const signature = settings => JSON.stringify([settings?.name || '', settings?.persona || '', settings?.reply_style || '']);
  function draftFor(item) {
    const key = String(item.id), incoming = item.settings || {};
    let draft = drafts.get(key);
    if (!draft || (!draft.dirty && draft.saved !== signature(incoming))) {
      draft = {values:{name:incoming.name || item.name || '', persona:incoming.persona || '', reply_style:incoming.reply_style || ''}, saved:signature(incoming), dirty:false};
      drafts.set(key, draft);
    }
    return draft;
  }
  function setError(message) { el('avatarError').textContent = message || ''; el('avatarError').hidden = !message; }
  function updateViewClasses() {
    const state = String(current?.session?.state || '').toLowerCase();
    const previewActive = current?.studio?.receiver_mode === 'preview' && !['idle','ended'].includes(state);
    document.body.classList.toggle('avatar-view', visible);
    document.body.classList.toggle('preview-active', previewActive);
  }
  function switchView(next, focus = true) {
    visible = next === 'avatars';
    updateViewClasses();
    el('liveView').hidden = visible; el('avatarsView').hidden = !visible;
    el('liveViewButton').setAttribute('aria-pressed', String(!visible));
    el('avatarsViewButton').setAttribute('aria-pressed', String(visible));
    for (const menu of document.querySelectorAll('[data-header-menu]')) menu.open = false;
    // replaceState changes only the local view marker and never reloads the player.
    if (location.hash !== `#${next}`) history.replaceState(null, '', `#${next}`);
    if (visible) { refresh(); if (focus) el('avatarsHeading').focus({preventScroll:true}); }
  }
  function renderSummary() {
    const summary = el('avatarStageSummary'); summary.replaceChildren();
    for (const [label, id, className] of [['当前直播', data?.current_id, ''], ['下次开场', data?.selected_id, ''], ['等待画面确认', data?.pending_id, 'pending-stage']]) {
      const p = text(summary, 'p', label, className);
      text(p, 'strong', id ? assetName(asset(id)) : label === '当前直播' ? '尚未开场' : label === '等待画面确认' ? '无' : '尚未选择');
    }
    if (data?.switch_pending) text(summary, 'p', '已发出切换请求；请返回直播查看动态画面，确认后再继续表演。', 'pending-stage');
  }
  function renderList() {
    const items = assets();
    el('avatarCount').textContent = `${items.length} 个`;
    const nextSignature = JSON.stringify(items.map(item => [item.id,item.name,item.settings?.name,item.status,item.image_available,item.image_version,data?.current_id,data?.selected_id,data?.pending_id,selectedId]));
    if (nextSignature === listSignature) return;
    listSignature = nextSignature;
    const list = el('avatarList'), scroll = list.scrollTop;
    const focusedId = document.activeElement?.closest('[data-avatar-id]')?.dataset.avatarId;
    list.replaceChildren();
    if (!items.length) { text(list, 'p', '还没有数字人。可同步已有 Vidu 资产，或上传图片创建。', 'empty'); return; }
    for (const item of items) {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'avatar-list-item'; button.dataset.avatarId = item.id;
      button.setAttribute('aria-current', String(String(item.id) === String(selectedId)));
      const thumb = document.createElement(item.image_available ? 'img' : 'span'); thumb.className = 'avatar-list-thumb';
      if (item.image_available) { thumb.src = imageUrl(item); thumb.alt = ''; thumb.loading = 'lazy'; thumb.addEventListener('error', () => { thumb.alt = '底图暂不可用'; }); }
      else { thumb.classList.add('avatar-list-placeholder'); thumb.textContent = '暂无底图'; }
      button.appendChild(thumb);
      const content = document.createElement('span'); text(content, 'span', assetName(item), 'avatar-list-name');
      if (item.settings?.name && item.settings.name !== item.name) text(content,'span',`角色：${item.settings.name}`,'avatar-list-meta');
      const tags = [statusLabels[item.status] || str(item.status,'状态未知')];
      if (imageReady(item) && !item.image_available) tags.push('底图暂不可用');
      if (!settingsComplete(item)) tags.push('待配置角色');
      if (String(data.current_id) === String(item.id)) tags.push('当前直播');
      if (String(data.pending_id) === String(item.id)) tags.push('待确认');
      else if (String(data.selected_id) === String(item.id)) tags.push('下次开场');
      text(content, 'span', tags.join(' · '), 'avatar-list-meta'); button.appendChild(content);
      button.addEventListener('click', () => { selectedId = item.id; renderList(); renderDetail(); });
      list.appendChild(button);
    }
    list.scrollTop = scroll;
    if (focusedId) [...list.querySelectorAll('[data-avatar-id]')].find(button => button.dataset.avatarId === focusedId)?.focus({preventScroll:true});
  }
  function createDetail(item) {
    const container = el('avatarDetail'); container.replaceChildren(); detailId = String(item.id);
    const head = document.createElement('div'); head.className = 'asset-detail-head';
    const heading = document.createElement('div'); const h2 = text(heading, 'h2', assetName(item)); h2.id = 'avatarDetailHeading';
    const roleName = text(heading,'p','','footnote'); roleName.id = 'assetRoleName';
    const badges = document.createElement('div'); badges.id = 'assetBadges'; badges.className = 'asset-badges'; heading.appendChild(badges); head.appendChild(heading); container.appendChild(head);
    const grid = document.createElement('div'); grid.className = 'asset-detail-grid';
    const figure = document.createElement('figure'); figure.className = 'asset-image-wrap';
    const imageWrap = document.createElement('div'); imageWrap.id = 'assetImageWrap'; figure.appendChild(imageWrap);
    const caption = text(figure, 'figcaption', '', 'asset-image-caption'); caption.id = 'assetImageCaption'; grid.appendChild(figure);
    const editor = document.createElement('div'), form = document.createElement('form'); form.id = 'assetSettingsForm'; form.className = 'asset-settings-form';
    for (const [key, label] of [['name','角色名称'],['persona','人设'],['reply_style','回复长度偏好']]) {
      const labelNode = text(form, 'label', label); labelNode.htmlFor = fields[key];
      const input = document.createElement(key === 'persona' ? 'textarea' : 'input'); input.id = fields[key]; input.name = key; input.required = true; input.maxLength = {name:30,persona:1000,reply_style:500}[key]; if (key === 'persona') input.rows = 5;
      input.addEventListener('input', () => { const draft = draftFor(chosen()); draft.values[key] = input.value; draft.dirty = true; updateDetailState(); });
      form.appendChild(input);
    }
    const save = text(form, 'button', '保存角色语气', 'wide'); save.type = 'submit'; save.id = 'saveAssetSettings';
    const note = text(form, 'p', '', 'asset-settings-note'); note.id = 'assetDraftNote'; note.setAttribute('aria-live','polite');
    form.addEventListener('submit', async event => {
      event.preventDefault(); const target = chosen(), draft = draftFor(target);
      const values = Object.fromEntries(Object.entries(draft.values).map(([key,value]) => [key,value.trim()]));
      await perform(`avatars/${encodeURIComponent(target.id)}/settings`, values, '角色语气已保存。', () => { draft.dirty = false; draft.saved = ''; quickRoleDrafts.delete(String(target.id)); if (settingsAssetId === String(target.id)) { settingsDirty = false; settingsLoaded = false; } });
    }); editor.appendChild(form);
    const voice = document.createElement('div'); voice.className = 'asset-voice';
    const voiceLabel = text(voice,'label','声音源（预留）'); voiceLabel.htmlFor = 'assetVoiceSource';
    const voiceInput = document.createElement('input'); voiceInput.id = 'assetVoiceSource'; voiceInput.disabled = true; voiceInput.value = '当前沿用 libai_v3.1'; voice.appendChild(voiceInput);
    text(voice,'p','声音切换暂未开放。切换数字人仍使用当前声音。','footnote'); editor.appendChild(voice); grid.appendChild(editor); container.appendChild(grid);
    const actions = document.createElement('div'); actions.className = 'asset-actions';
    const row = document.createElement('div'); row.className = 'asset-actions-row';
    for (const [id,label,handler] of [['selectAvatar','设为开场角色',() => perform('avatars/select',{avatar_id:chosen().id,generation:current?.session?.generation},'已设为下次开场角色；尚未开始会话。')],['switchToAvatar','切换到此数字人',() => perform('avatars/switch',{avatar_id:chosen().id,generation:current?.session?.generation,request_id:crypto.randomUUID()},'已提交切换；请查看动态画面，再确认继续。')],['refreshAvatar','刷新处理状态',() => perform(`avatars/${encodeURIComponent(chosen().id)}/refresh`,{},'已刷新处理状态。')]]) {
      const button = text(row,'button',label,id === 'selectAvatar' ? 'primary' : ''); button.id=id; button.type='button'; button.addEventListener('click',handler);
    }
    actions.appendChild(row); const note2=text(actions,'p','','asset-action-note'); note2.id='assetActionNote';
    const result=text(actions,'p','','hint'); result.id='assetActionResult'; result.setAttribute('role','status'); container.appendChild(actions);
  }
  function updateDetailState() {
    const item = chosen(); if (!item || detailId !== String(item.id)) return;
    const draft = draftFor(item), editable = connected && !busy && !actionBusy && current?.studio?.settings_editing_allowed === true && data?.editing_allowed === true;
    for (const id of Object.values(fields)) el(id).disabled = !editable;
    el('saveAssetSettings').disabled = !editable || !draft.dirty;
    el('assetDraftNote').textContent = draft.dirty ? '有未保存的修改；切换查看其他数字人会保留此草稿。' : data?.editing_allowed ? '角色语气与此数字人绑定，保存后在开场或确认切换时使用。' : current?.session?.state === 'ended' ? '会话已结束，请返回直播控制并新建会话，再编辑角色语气。' : '仅空闲时可以编辑角色语气。';
    const selectable = connected && !busy && !actionBusy && current?.studio?.settings_editing_allowed === true && data?.can_select === true && ready(item) && settingsComplete(item);
    const switchable = connected && !busy && !actionBusy && current?.studio?.avatar_switch?.available === true && data?.can_switch === true && ready(item) && settingsComplete(item) && String(data?.current_id) !== String(item.id);
    el('selectAvatar').disabled = !selectable || String(data?.selected_id) === String(item.id);
    el('selectAvatar').textContent = String(data?.selected_id) === String(item.id) ? '已设为开场角色' : '设为开场角色';
    el('switchToAvatar').disabled = !switchable; el('switchToAvatar').textContent = String(data?.current_id) === String(item.id) ? '当前直播数字人' : '切换到此数字人';
    el('refreshAvatar').disabled = !connected || busy || actionBusy;
    el('refreshAvatar').hidden = ready(item);
    el('assetActionNote').textContent = data?.switch_pending ? '切换请求已发送，尚未确认画面。请返回直播查看转场，再点击“确认画面并继续”。' : !ready(item) ? imageReady(item) ? '形象已就绪，但本机底图不可用。请点击“刷新处理状态”重新获取底图，成功后再选择。' : '数字人尚未就绪，请刷新处理状态后再选择。' : !settingsComplete(item) ? '请先在空闲时填写并保存角色名称、人设和回复长度偏好，再设为开场角色或切换。' : current?.session?.state === 'ended' ? '会话已结束，请返回直播控制并新建会话，再选择开场角色。' : data?.blocking_reason || '仅空闲时可设为开场角色；直播就绪且没有待处理内容时可切换。';
  }
  function renderDetail() {
    const item = chosen();
    if (!item) { el('avatarDetail').replaceChildren(); text(el('avatarDetail'),'p','选择一个数字人，查看底图和角色设置。','empty'); detailId = null; return; }
    if (detailId !== String(item.id)) createDetail(item);
    el('avatarDetailHeading').textContent = assetName(item);
    el('assetRoleName').textContent = item.settings?.name ? `角色：${item.settings.name}` : '角色语气尚未配置';
    const badges = el('assetBadges'); badges.replaceChildren();
    for (const [condition,label,style] of [[true,statusLabels[item.status] || str(item.status,'状态未知'),''],[imageReady(item) && !item.image_available,'底图暂不可用','pending'],[!settingsComplete(item),'待配置角色','pending'],[String(data.current_id)===String(item.id),'当前直播','current'],[String(data.selected_id)===String(item.id),'下次开场',''],[String(data.pending_id)===String(item.id),'等待画面确认','pending']]) if(condition) text(badges,'span',label,`asset-badge ${style}`);
    const wrap = el('assetImageWrap'), src = item.image_available ? imageUrl(item) : '';
    if (wrap.dataset.src !== src || !wrap.firstChild) {
      wrap.dataset.src = src; wrap.replaceChildren();
      if (src) { const image = document.createElement('img'); image.src=src; image.alt=`${assetName(item)}的形象底图`; image.className='asset-image'; image.addEventListener('error',() => { wrap.replaceChildren(); text(wrap,'p','底图暂不可用，可同步资产后重试。','asset-image asset-image-empty'); }); wrap.appendChild(image); }
      else text(wrap,'p','尚无可用底图','asset-image asset-image-empty');
    }
    el('assetImageCaption').textContent = `资产 ID：${item.id}${item.width && item.height ? ` · ${item.width} × ${item.height}` : ''}`;
    const draft = draftFor(item);
    for (const [key,id] of Object.entries(fields)) if (el(id).value !== draft.values[key]) el(id).value = draft.values[key];
    updateDetailState();
  }
  function resetUploadDraft() {
    pendingUpload = null;
    el('avatarUploadForm').reset(); el('avatarUploadPreview').hidden = true;
    if (uploadUrl) URL.revokeObjectURL(uploadUrl); uploadUrl = null;
  }
  function updateUploadControls() {
    const form = el('avatarUploadForm');
    // 未知结果锁住原草稿：查询沿用相同 ID 和文件快照，不会生成第二次创建请求。
    for (const input of form.elements) if (input !== el('createAvatar')) input.disabled = actionBusy || pendingUpload?.unknown === true;
    el('createAvatar').textContent = pendingUpload?.unknown ? '核对本次创建结果' : '创建数字人';
    el('createAvatar').disabled = !connected || busy || actionBusy || current?.studio?.settings_editing_allowed !== true || data?.editing_allowed !== true;
  }
  function reconcileUpload() {
    if (!pendingUpload?.unknown || actionBusy) return;
    const receipt = (Array.isArray(data?.uploads) ? data.uploads : []).find(upload => upload.request_id === pendingUpload.requestId);
    if (!receipt) return;
    if (failedUpload(receipt.status)) {
      pendingUpload = null;
      el('avatarUploadResult').textContent = `已确认创建失败：${str(receipt.error,'请检查图片和填写内容')}。草稿已保留，可以修改后重新创建。`;
    } else if (!unknownUpload(receipt.status) && receipt.asset_id) {
      selectedId = receipt.asset_id; resetUploadDraft();
      el('avatarUploadResult').textContent = '已核对到创建资产；请查看处理状态。不会自动开场或切换直播。';
    }
  }
  function renderUploadHistory() {
    const container = el('avatarUploadHistory');
    container.replaceChildren();
    const uploads = Array.isArray(data?.uploads) ? data.uploads : [];
    for (const upload of uploads.slice(-8).reverse()) {
      const record = document.createElement('p'); record.className = 'footnote';
      const status = {creation_unknown:'创建结果未知，请先核对云端资产，避免重复创建',creation_failed:'创建失败',unknown:'创建结果未知，请先核对云端资产，避免重复创建',processing:'正在处理',created:'已创建，等待就绪',success:'已创建',failed:'创建失败'}[upload.status] || str(upload.status,'状态未知');
      record.textContent = `${str(upload.name,'上传的数字人')} · ${status}${upload.error ? `：${upload.error}` : ''}${upload.asset_id ? ` · 资产 ID：${upload.asset_id}` : ''}`;
      container.appendChild(record);
    }
    const unknownSignature = uploads.filter(upload => unknownUpload(upload.status)).map(upload => upload.request_id).join(',');
    if (unknownSignature && unknownSignature !== seenUnknownUploads) el('avatarUploadDetails').open = true;
    seenUnknownUploads = unknownSignature;
  }
  function render() {
    reconcileUpload(); renderSummary(); renderList(); renderDetail(); renderUploadHistory();
    el('syncAvatars').disabled = !connected || busy || actionBusy || data?.syncing === true;
    el('syncAvatars').textContent = data?.syncing ? '正在同步…' : '同步 Vidu 资产';
    updateUploadControls();
    el('avatarLoadStatus').textContent = data?.syncing ? '正在同步 Vidu 资产…' : loading ? '正在读取资产…' : data?.last_sync ? `每 5 秒刷新本机列表；云端状态需点击同步或刷新处理状态。最近同步 ${new Date(Number.isFinite(Number(data.last_sync)) ? Number(data.last_sync)*1000 : data.last_sync).toLocaleString('zh-CN',{hour12:false})}` : '每 5 秒刷新本机列表；点击“同步 Vidu 资产”或“刷新处理状态”读取云端状态。';
  }
  async function refresh() {
    if (loading) return; loading = true;
    if (visible) el('avatarLoadStatus').textContent = '正在读取资产…';
    try {
      const response = await fetch('avatars',{cache:'no-store'}); let result;
      try { result = await response.json(); } catch { throw new Error(`服务返回异常（${response.status}）`); }
      if (!response.ok) throw new Error(str(result.error,`读取失败（${response.status}）`));
      data = result;
      if (!selectedId || !asset(selectedId)) selectedId = data.current_id || data.selected_id || assets()[0]?.id || null;
      setError(data.error ? `资产同步未完成：${data.error}。可保留现有资产，稍后手动同步。` : '');
      render(); syncQuickRoleContext();
    } catch (error) { setError(`无法读取数字人资产：${error.message}。请检查工作台连接，或点击“同步 Vidu 资产”重试。`); if (!data) { el('avatarList').replaceChildren(); text(el('avatarList'),'p','资产暂未加载，请恢复连接后重试。','empty'); } }
    finally { loading = false; if(data) render(); else { el('avatarLoadStatus').textContent='尚未读到资产。'; el('syncAvatars').disabled = !connected || busy || actionBusy; } }
  }
  async function perform(path, body, success, onSuccess) {
    if (actionBusy || busy || !connected) return false;
    actionBusy = true; busy = true; if(current) renderCurrentState(); if(data) render(); if(path === 'avatars/sync') { el('syncAvatars').disabled=true; el('syncAvatars').textContent='正在同步…'; } setError('');
    try {
      const result = await request(path,body);
      if (result.status === 'failed' || result.sent === false && path === 'avatars/switch') throw new Error(result.error_code || '本次请求未发送');
      if (onSuccess) onSuccess(result);
      showNotice(success,true); await poll(); await refresh(); return true;
    } catch(error) { setError(`操作未能确认：${error.message}。请核对当前资产和直播状态后再决定是否重试；不会自动重试。`); return false; }
    finally { actionBusy = false; busy = false; if(current) renderCurrentState(); if(data) render(); else { el('syncAvatars').disabled=!connected; el('syncAvatars').textContent='同步 Vidu 资产'; } }
  }
  function syncQuickRoleContext() {
    const id = data?.current_id || data?.selected_id;
    const item = asset(id); if(!item) return;
    syncRoleSettings(item.settings || {}, id);
  }
  el('liveViewButton').addEventListener('click', () => switchView('live',false));
  el('avatarsViewButton').addEventListener('click', () => switchView('avatars'));
  window.addEventListener('hashchange', () => switchView(location.hash === '#avatars' ? 'avatars' : 'live',false));
  el('syncAvatars').addEventListener('click', async () => { if(await perform('avatars/sync',{},'已同步 Vidu 资产。')) await refresh(); });
  el('openAvatarUpload').addEventListener('click', () => { el('avatarUploadDetails').open=true; el('avatarImageUpload').focus(); el('avatarUploadDetails').scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth',block:'start'}); });
  el('avatarImageUpload').addEventListener('change', () => {
    if(uploadUrl) URL.revokeObjectURL(uploadUrl); uploadUrl=null;
    const file=el('avatarImageUpload').files[0];
    el('avatarUploadPreview').hidden=!file;
    if(file) { uploadUrl=URL.createObjectURL(file); el('avatarUploadPreview').src=uploadUrl; }
  });
  el('avatarUploadForm').addEventListener('submit', async event => {
    event.preventDefault(); if(actionBusy || busy || !connected || current?.studio?.settings_editing_allowed !== true || data?.editing_allowed !== true) return;
    const form = el('avatarUploadForm');
    if (!pendingUpload) {
      const requestId = crypto.randomUUID(), snapshot = new FormData(form);
      snapshot.set('request_id',requestId);
      pendingUpload = {requestId, snapshot, unknown:false};
    }
    // 重试复制原快照；禁用后的表单字段不会被 FormData 悄悄遗漏。
    const payload = new FormData();
    for (const [key,value] of pendingUpload.snapshot.entries()) payload.append(key,value);
    const checking = pendingUpload.unknown;
    actionBusy=true; busy=true; if(current) renderCurrentState(); render();
    el('avatarUploadResult').textContent = checking ? '正在核对本次创建回执；沿用原请求，不会重复创建…' : '正在上传并创建，请保持页面打开…';
    try {
      const response=await fetch('avatars/create',{method:'POST',body:payload}); const result=await response.json();
      // 相同图片可能命中后台已有的未知回执，以后台原请求 ID 继续核对。
      if (result.request_id && pendingUpload && result.request_id !== pendingUpload.requestId) {
        pendingUpload.requestId = result.request_id; pendingUpload.snapshot.set('request_id',result.request_id);
      }
      if(!response.ok || result.error || unknownUpload(result.status) || failedUpload(result.status)) {
        const error = new Error(str(result.error, unknownUpload(result.status) ? '云端创建结果未知，请先同步核对资产' : `创建失败（${response.status}）`));
        error.uploadStatus = result.status; throw error;
      }
      const createdId=result.asset?.id || result.asset_id || result.avatar?.id || result.avatar_id || result.id;
      if (!createdId) throw new Error('服务尚未返回已创建的资产，请继续核对本次回执');
      selectedId=createdId;
      el('avatarUploadResult').textContent='创建请求已受理；请查看资产处理状态。不会自动开场或切换直播。';
      resetUploadDraft();
      await refresh();
    } catch(error) {
      if (failedUpload(error.uploadStatus)) {
        pendingUpload = null;
        el('avatarUploadResult').textContent = `创建失败：${error.message}。已保留填写内容，请检查原因后再决定是否重新创建。`;
      } else {
        pendingUpload.unknown = true;
        el('avatarUploadResult').textContent = `未能确认创建结果：${error.message}。原图片和填写内容已锁定，避免重复创建；可同步资产或点击“核对本次创建结果”。核对沿用原请求，不会自动重试。`;
      }
    }
    finally { actionBusy=false; busy=false; if(current) renderCurrentState(); await refresh(); render(); }
  });
  document.addEventListener('studio-state-updated', () => { updateViewClasses(); if(data) { updateDetailState(); el('syncAvatars').disabled=!connected || busy || actionBusy || data.syncing===true; updateUploadControls(); } });
  document.addEventListener('studio-role-saved', refresh);
  setInterval(() => { if(visible) refresh(); },5000);
  switchView(location.hash === '#avatars' ? 'avatars' : 'live',false);
  return {refresh, stateChanged(){ if(data) { updateDetailState(); el('syncAvatars').disabled=!connected || actionBusy || data.syncing===true; el('createAvatar').disabled=!connected || actionBusy || data.editing_allowed!==true; } }, roleAssetId(){ return data?.current_id || data?.selected_id || null; }, roleSaved(){ return refresh(); }};
})();
