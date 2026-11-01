import { test, expect } from '../../frontend/node_modules/@playwright/test/index.mjs';

// Explicit OFFLINE rubric fixture. This is browser/DB evidence, not a real model run.
test('M2 native-manual seam: frozen export, fixture import, atomic profile, recovery, offline and auth', async ({ page, context }, testInfo) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/#token=browser-m2-test-token-only');
  await expect(page.locator('#connection')).toContainText('已连接真实 PostgreSQL');
  expect(new URL(page.url()).hash).toBe('');
  await expect(page.getByText('ChatGPT 辅助评估（人工导入；尚非自动 API）', { exact: false })).toBeVisible();
  const card = page.locator('#interview-card');
  async function guest(subject: string) {
    await page.getByLabel('稳定身份 ID').fill(subject);
    await page.getByLabel('昵称', { exact: true }).fill('面试测试游客');
    await page.locator('#register').click();
    await expect(page.locator('#detail-content')).toContainText(subject);
    await expect(page.locator('#interview-start')).toBeEnabled();
  }
  await guest('browser-m2-hire');
  await page.getByRole('button', { name: '开始固定题面试', exact: true }).click();
  await expect(card).toContainText('READY');
  await expect(card).toContainText('客户投诉收到的噩梦不够恐怖');
  await page.getByRole('button', { name: '确认题目已展示，开始答题', exact: true }).click();
  await expect(card).toContainText('ANSWERING');
  const answer = ' 很抱歉让您失望。我们为您免费重做噩梦，并请地狱质检员确认恐怖等级。 <img src=x onerror=alert(1)> ';
  await page.getByLabel('最终回答', { exact: true }).fill(answer);
  await page.getByRole('button', { name: '提交最终回答', exact: true }).click();
  await expect(card).toContainText('EVALUATING');
  await expect(card).toContainText('不会自动调用模型');
  await expect(card.locator('img')).toHaveCount(0);
  await page.getByRole('button', { name: '导出评估任务', exact: true }).click();
  const frozen = page.getByLabel('冻结评估任务 JSON', { exact: true });
  await expect(frozen).not.toHaveValue('');
  const exported = JSON.parse(await frozen.inputValue());
  expect(exported.attempt_no).toBe(1);
  expect(JSON.stringify(exported.frozen_input)).toContain(answer);
  expect(JSON.stringify(exported)).not.toContain('browser-m2-test-token-only');
  await expect(page.locator('#detail-content')).toContainText('游客');

  // Pause invalidates the exported attempt; resume cannot silently evaluate.
  await page.getByRole('button', { name: '暂停面试', exact: true }).click();
  await expect(card).toContainText('PAUSED');
  await expect(frozen).toHaveValue('');
  await page.getByRole('button', { name: '恢复面试', exact: true }).click();
  await expect(card).toContainText('REVIEW_FAILED');
  await expect(page.getByRole('button', { name: '导入评价并结算', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: '重试评估（新尝试）', exact: true }).click();
  await expect(card).toContainText('EVALUATING');
  await page.getByRole('button', { name: '导出评估任务', exact: true }).click();
  await expect(frozen).not.toHaveValue('');
  const retried = JSON.parse(await frozen.inputValue());
  expect(retried.attempt_no).toBe(2);
  expect(retried.input_hash).toBe(exported.input_hash);
  expect(retried.frozen_input).toEqual(exported.frozen_input);
  const fixture = { criteria: ['empathy', 'solution', 'setting'].map((id, i) => ({
    id, score: 2, evidence: [['很抱歉让您失望'], ['免费重做噩梦'], ['地狱质检员']][i], reason: '离线验收样例，并非真实模型调用',
  })) };
  await page.getByLabel('ChatGPT 评价 JSON', { exact: true }).fill(JSON.stringify(fixture));
  await page.getByRole('button', { name: '导入评价并结算', exact: true }).click();
  await expect(card).toContainText('COMPLETED');
  await expect(card).toContainText('服务器总分：6 / 6');
  await expect(page.locator('#detail-content')).toContainText('危机公关实习生');
  await expect(page.locator('#interview-start')).toBeDisabled();
  await page.reload();
  await expect(card).toContainText('COMPLETED');
  await expect(card.locator('img')).toHaveCount(0);

  await guest('browser-m2-cancel');
  await page.getByRole('button', { name: '开始固定题面试', exact: true }).click();
  await expect(card).toContainText('READY');
  await context.setOffline(true);
  await expect(page.locator('#connection')).toContainText('离线');
  await expect(page.getByRole('button', { name: '确认题目已展示，开始答题', exact: true })).toBeDisabled();
  await context.setOffline(false);
  await expect(page.getByRole('button', { name: '确认题目已展示，开始答题', exact: true })).toBeEnabled();
  await page.getByText('取消当前面试', { exact: true }).click();
  await page.getByLabel('取消原因', { exact: true }).fill('浏览器测试取消');
  await page.getByRole('button', { name: '确认取消面试', exact: true }).click();
  await expect(card).toContainText('CANCELLED');
  await expect(page.locator('#detail-content')).toContainText('暂无经历');
  await page.screenshot({ path: testInfo.outputPath('m2-fixture-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('m2-fixture-mobile.png'), fullPage: true });
  await context.clearCookies();
  await page.getByRole('button', { name: '重新检查连接', exact: true }).click();
  await expect(page.locator('#connection')).toContainText('尚未登录');
  await expect(page.locator('#interview-start')).toBeDisabled();
  await expect(card).toBeEmpty();
  expect(errors).toEqual([]);
});
