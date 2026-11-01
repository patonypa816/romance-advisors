import { test, expect } from '../../frontend/node_modules/@playwright/test/index.mjs';

// Deterministic OFFLINE fixtures exercise native-manual transport, not model quality.
// No API writes: every registration, revision, review, confirmation and vote uses the UI.
test('M3 three guests: revision, failed review gate, retry, all-display gate, vote change and atomic result', async ({ page, context }, testInfo) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/#token=browser-m3-test-token-only');
  await expect(page.locator('#connection')).toContainText('已连接真实 PostgreSQL');
  expect(new URL(page.url()).hash).toBe('');
  const card = page.locator('#competition-card');
  const guestIds: string[] = [];
  for (let i = 0; i < 3; i++) {
    await page.locator('#subject-id').fill(`browser-m3-guest-${i}`);
    await page.locator('#display-name').fill(`创意游客 ${i}`);
    await page.locator('#register').click();
    await expect(page.locator('#detail-content')).toContainText(`browser-m3-guest-${i}`);
  }
  const directory = await (await page.request.get('/api/players')).json();
  for (let i = 0; i < 3; i++) {
    const player = directory.players.find((p: any) => p.subject_id === `browser-m3-guest-${i}`);
    expect(player.role).toBe('guest');
    guestIds.push(player.player_id);
  }
  await page.locator('#competition-create').click();
  await expect(card).toContainText('READY');
  const rounds = await (await page.request.get('/api/competitions')).json();
  const id = rounds.competitions[0].id;
  const snapshot = async () => (await (await page.request.get(`/api/competitions/${id}`)).json()).competition;
  await page.locator('#competition-open').click();
  await expect(card).toContainText('SUBMITTING');
  const texts = [
    ' 噩梦考勤钟：迟到一分钟便倒流一天；客户投诉后客服补发清醒假。 <img src=x onerror=alert(1)> ',
    '噩梦退货枕：每次翻身退回老板的梦；客户投诉后客服提供无梦替换芯。',
    '噩梦复印机：复印加班影子代替本人；客户投诉后客服删除影子并退还睡眠。',
  ];
  for (let i = 0; i < 3; i++) {
    await page.locator('#competition-author').selectOption(guestIds[i]);
    await page.locator('#competition-text').fill(i === 0 ? '旧稿：噩梦考勤钟。' : texts[i]);
    await page.locator('#competition-submit').click();
    await expect.poll(async () => (await snapshot()).submissions.length).toBe(i + 1);
  }
  await page.locator('#competition-author').selectOption(guestIds[0]);
  await page.locator('#competition-text').fill(texts[0]);
  await page.locator('#competition-submit').click();
  await expect.poll(async () => (await snapshot()).submissions[0].revision).toBe(2);
  expect((await snapshot()).submissions[0].text).toBe(texts[0]);
  await page.locator('#competition-close-submissions').click();
  await expect(card).toContainText('REVIEWING');
  const submissions = (await snapshot()).submissions;
  const workButton = (action: string, index: number) => page.locator(`[data-testid="competition-${action}"][data-submission-id="${submissions[index].id}"]`);
  const exported = page.locator('#competition-evaluation-request');
  const result = page.locator('#competition-evaluation-result');
  const fixture = (i: number) => ({
    criteria: ['originality', 'coherence', 'setting'].map(id => ({ id, score: 2, evidence: [texts[i].trim()], reason: '离线契约样例：证据为该作品原文，不代表真实模型判断。' })),
    comment: ['考勤钟将迟到转为时间倒流，客服提供清醒假。', '枕头退回老板的梦，并提供替换芯。', '复印机用影子加班，客服负责删除并归还睡眠。'][i],
  });
  const exports: any[] = [];
  for (let i = 0; i < 3; i++) {
    await workButton('export', i).click();
    await expect(exported).not.toHaveValue('');
    exports.push(JSON.parse(await exported.inputValue()));
    expect(exports[i].frozen_input.submission.text).toBe(texts[i]);
    expect(JSON.stringify(exports[i])).not.toContain('browser-m3-test-token-only');
    await result.fill(JSON.stringify(i === 1 ? { ...fixture(i), criteria: fixture(i).criteria.map((row, index) => index === 0 ? { ...row, evidence: ['这段证据没有出现在任何作品原文'] } : row) } : fixture(i)));
    await page.locator('#competition-import').click();
    if (i === 1) await expect(card).toContainText('WAITING_REVIEW');
    else await expect.poll(async () => (await snapshot()).submissions[i].task.status).toBe('accepted');
  }
  let before = await snapshot();
  expect(before.status).toBe('WAITING_REVIEW');
  expect(before.result).toBeNull();
  expect(before.votes).toBeNull();
  for (const work of before.submissions) { expect(work.comment).toBeNull(); expect(work.evaluation).toBeNull(); }
  await expect(page.locator('#competition-open-voting')).toBeDisabled();
  await expect(page.locator('#competition-settle')).toBeDisabled();
  await expect(workButton('retry', 0)).toBeDisabled();
  await workButton('retry', 1).click();
  await workButton('export', 1).click();
  const retry = JSON.parse(await exported.inputValue());
  expect(retry.attempt_no).toBe(exports[1].attempt_no + 1);
  expect(retry.input_hash).toBe(exports[1].input_hash);
  expect(retry.frozen_input).toEqual(exports[1].frozen_input);
  await result.fill(JSON.stringify(fixture(1)));
  await page.locator('#competition-import').click();
  await expect(card).toContainText('DISPLAYING');
  for (let i = 0; i < 3; i++) {
    await expect(page.locator('#competition-display')).toContainText(texts[i].trim());
    await expect(page.locator('#competition-display')).toContainText(fixture(i).comment);
    await expect(page.locator('#competition-open-voting')).toBeDisabled();
    await workButton('confirm-display', i).click();
    await expect.poll(async () => Boolean((await snapshot()).submissions[i].display_confirmed_at)).toBe(true);
  }
  await expect(page.locator('#competition-works img, #competition-display img')).toHaveCount(0);
  await page.locator('#competition-open-voting').click();
  await expect(card).toContainText('VOTING');
  await page.locator('#competition-voter').selectOption(guestIds[0]);
  await page.locator('#competition-candidate').selectOption(submissions[1].id);
  await page.locator('#competition-vote').click();
  await expect.poll(async () => (await (await page.request.get(`/api/competitions/${id}/vote-state?player_id=${guestIds[0]}`)).json()).vote_revision).toBe(1);
  await page.locator('#competition-candidate').selectOption(submissions[0].id);
  await page.locator('#competition-vote').click();
  await expect.poll(async () => (await (await page.request.get(`/api/competitions/${id}/vote-state?player_id=${guestIds[0]}`)).json()).vote_revision).toBe(2);
  before = await snapshot();
  expect(before.votes).toBeNull(); expect(before.result).toBeNull();
  for (const work of before.submissions) expect(work.evaluation).toBeNull();
  await page.locator('#competition-close-voting').click();
  await expect(card).toContainText('READY_TO_SETTLE');
  await page.locator('#competition-settle').click();
  await expect(card).toContainText('COMPLETED');
  const completed = await snapshot();
  expect(completed.result.awards).toHaveLength(2);
  expect(completed.result.awards.every((award: any) => award.player_id === guestIds[0] && award.performance_delta === 0)).toBe(true);
  for (const playerId of guestIds) {
    const profile = (await (await page.request.get(`/api/players/${playerId}`)).json()).player;
    expect(profile.role).toBe('guest'); expect(profile.performance).toBe(0);
    expect(profile.recent_events).toHaveLength(1);
  }
  await page.getByRole('button', { name: '查看详情 browser-m3-guest-0', exact: true }).click();
  await expect(page.locator('#detail-content')).toContainText('browser-m3-guest-0');
  await expect(page.locator('#detail-content')).toContainText('游客');
  await expect(page.locator('#detail-content')).not.toContainText('暂无经历');
  await page.reload();
  await expect(card).toContainText('COMPLETED');
  await page.screenshot({ path: testInfo.outputPath('m3-fixture-completed-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('m3-fixture-completed-mobile.png'), fullPage: true });

  await page.locator('#competition-create').click();
  await expect(card).toContainText('READY');
  await page.locator('#competition-open').click();
  await expect(card).toContainText('SUBMITTING');
  await page.locator('#competition-pause').click();
  await expect(card).toContainText('PAUSED');
  await expect(page.locator('#competition-submit')).toBeDisabled();
  await page.locator('#competition-resume').click();
  await expect(card).toContainText('SUBMITTING');
  await context.setOffline(true);
  await expect(page.locator('#connection')).toContainText('离线');
  await expect(page.locator('#competition-submit')).toBeDisabled();
  await expect(page.locator('#competition-close-submissions')).toBeDisabled();
  await context.setOffline(false);
  await expect(page.locator('#competition-close-submissions')).toBeEnabled();
  await page.getByText('取消当前比赛', { exact: true }).click();
  await page.locator('#competition-cancel-reason').fill('浏览器隔离测试取消，无奖励');
  await page.locator('#competition-cancel').click();
  await expect(card).toContainText('CANCELLED');
  await context.clearCookies();
  await page.locator('#reconnect').click();
  await expect(page.locator('#connection')).toContainText('尚未登录');
  await expect(page.locator('#competition-create')).toBeDisabled();
  await expect(card).toBeEmpty();
  expect(errors).toEqual([]);
});
