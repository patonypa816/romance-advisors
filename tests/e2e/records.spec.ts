import { test, expect } from '../../frontend/node_modules/@playwright/test/index.mjs';

test('M1 real persistence, safe display, same identity, replay after response loss, offline and refresh', async ({ page, context }, testInfo) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/#token=browser-test-token-only');
  await expect(page.locator('#connection')).toContainText('已连接真实 PostgreSQL');
  expect(new URL(page.url()).hash).toBe('');
  expect(await page.evaluate(() => Object.keys(localStorage))).toEqual([]);
  expect(await page.evaluate(() => document.cookie)).not.toContain('hell_inc_session');

  const subject = page.getByLabel('稳定身份 ID');
  const nickname = page.getByLabel('昵称', { exact: true });
  const submit = page.locator('#register');
  async function register(id: string, name: string) {
    await subject.fill(id); await nickname.fill(name);
    const response = page.waitForResponse(r => r.url().endsWith('/api/players') && r.request().method() === 'POST');
    await submit.click();
    const data = await (await response).json();
    await expect(page.locator('#detail-content h3').first()).toHaveText(name);
    await expect(submit).toBeEnabled();
    return data;
  }
  const original = await register('browser-001', '小林');
  await expect(page.locator('#detail-content')).toContainText('暂无经历');
  const renamed = await register('browser-001', '改名后的小林');
  expect(renamed.player.player_id).toBe(original.player.player_id);
  const another = await register('browser-002', '改名后的小林');
  expect(another.player.player_id).not.toBe(original.player.player_id);

  await page.getByLabel('搜索档案').fill('browser-001');
  await page.getByRole('button', { name: '搜索', exact: true }).click();
  await expect(page.locator('#players li')).toHaveCount(1);
  await page.getByRole('button', { name: '查看详情 browser-001', exact: true }).click();
  await expect(page.locator('#detail-content')).toContainText(original.player.player_id);
  await page.reload();
  await expect(page.locator('#connection')).toContainText('已连接真实 PostgreSQL');
  await page.getByRole('button', { name: '查看详情 browser-001', exact: true }).click();
  await expect(page.locator('#detail-content')).toContainText(original.player.player_id);

  // A selected detail follows real changes committed by another client.
  const updated = await page.request.post('/api/players', {
    headers: { Origin: 'http://127.0.0.1:8080' },
    data: { command_id: crypto.randomUUID(), provider: 'simulated', identity_scope: 'local-test',
      subject_id: 'browser-001', display_name: '另一窗口改名' },
  });
  expect(updated.ok()).toBe(true);
  await expect(page.locator('#detail-content h3').first()).toHaveText('另一窗口改名');

  // Keep an older real query response in flight while a newer query wins.
  let releaseOld!: () => void;
  let sawOld!: () => void;
  const oldSeen = new Promise<void>(resolve => { sawOld = resolve; });
  const oldReleased = new Promise<void>(resolve => { releaseOld = resolve; });
  await page.route('**/api/players?q=browser-001', async route => {
    const response = await route.fetch();
    sawOld(); await oldReleased; await route.fulfill({ response });
  });
  await page.getByLabel('搜索档案').fill('browser-001');
  await page.getByRole('button', { name: '搜索', exact: true }).click();
  await oldSeen;
  await page.getByLabel('搜索档案').fill('browser-002');
  await page.getByRole('button', { name: '搜索', exact: true }).click();
  await expect(page.locator('#players')).toContainText('browser-002');
  const oldFinished = page.waitForResponse(r => r.url().endsWith('/api/players?q=browser-001'));
  releaseOld(); await oldFinished;
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await expect(page.locator('#players')).not.toContainText('browser-001');
  await page.unroute('**/api/players?q=browser-001');
  await page.getByLabel('搜索档案').fill('');
  await page.getByRole('button', { name: '搜索', exact: true }).click();
  await register('browser-unicode', '😀'.repeat(80));

  const malicious = '<img src=x onerror=alert(1)>' + '长'.repeat(50);
  await register('browser-malicious', malicious);
  await expect(page.locator('#detail-content img')).toHaveCount(0);
  await expect(page.locator('#players img')).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);

  // Deliver the first registration to PostgreSQL, then lose only the response.
  // The retry must retain the exact original command and receive replayed=true.
  const attempts: unknown[] = [];
  let committed: any;
  await page.route('**/api/players', async route => {
    if (route.request().method() !== 'POST') { await route.continue(); return; }
    attempts.push(route.request().postDataJSON());
    if (attempts.length === 1) {
      const response = await route.fetch();
      expect(response.ok()).toBe(true);
      committed = await response.json();
      await route.abort('failed');
    } else { await route.continue(); }
  });
  await subject.fill('browser-lost-response'); await nickname.fill('回执丢失测试');
  await submit.click();
  await expect(page.locator('#registration-message')).toContainText('原操作编号和载荷已保留');
  await page.getByRole('button', { name: '重新检查连接' }).click();
  await expect(submit).toBeEnabled();
  const retried = page.waitForResponse(r => r.url().endsWith('/api/players') && r.request().method() === 'POST');
  await submit.click();
  const replay = await (await retried).json();
  expect(attempts).toHaveLength(2);
  expect(attempts[1]).toEqual(attempts[0]);
  expect(replay).toEqual({ ...committed, replayed: true });
  await expect(page.locator('#registration-message')).toContainText('没有重复创建档案');
  await page.unroute('**/api/players');

  await context.setOffline(true);
  await expect(page.locator('#connection')).toContainText('离线');
  await expect(submit).toBeDisabled();
  await context.setOffline(false);
  await expect(page.locator('#connection')).toContainText('已连接真实 PostgreSQL');
  await expect(submit).toBeEnabled();
  await expect(page.locator('#notice')).not.toContainText('Failed to fetch');
  await page.screenshot({ path: testInfo.outputPath('m1-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('m1-mobile.png'), fullPage: true });
  expect(errors).toEqual([]);
});

test('fresh browser cannot read private records or reuse a consumed startup link', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('#connection')).toContainText('尚未登录');
  await expect(page.locator('#register')).toBeDisabled();
  const response = await page.request.get('/api/players');
  expect(response.status()).toBe(401);
  await page.goto('/#token=browser-test-token-only');
  await expect(page.locator('#notice')).toContainText('登录失败');
  expect(new URL(page.url()).hash).toBe('');
  await expect(page.locator('#register')).toBeDisabled();
  await expect(page.locator('#players li')).toHaveCount(0);
});
