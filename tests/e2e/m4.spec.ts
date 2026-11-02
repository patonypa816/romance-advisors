import { randomUUID } from 'node:crypto';
import { test, expect } from '../../frontend/node_modules/@playwright/test/index.mjs';

// Offline protocol acceptance only: no TTS, cloud session, PCM, RTC or audience evidence.
// M2/M3 setup uses the real authenticated API with explicit rubric fixtures;
// selecting/freezing/submitting their speech is exercised through the workbench UI.
test('M4 offline workbench: fixed script, source integrity, cancellation, pause and recovery guards', async ({ page, context }, testInfo) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/#token=browser-m4-test-token-only');
  await expect(page.locator('#connection')).toContainText('已连接真实 PostgreSQL');
  expect(new URL(page.url()).hash).toBe('');
  const get = async (path: string) => {
    const r = await page.request.get(path); expect(r.status()).toBe(200); return r.json();
  };
  const post = async (path: string, body: unknown) => {
    const r = await page.request.post(path, { data: body, headers: { Origin: 'http://127.0.0.1:8083' } });
    expect(r.ok(), await r.text()).toBe(true); return r.json();
  };
  const command = async (path: string, action: string) => {
    const envelope = await get(path);
    const state = envelope.interview ?? envelope.competition;
    return post(`${path}/${action}`, { command_id: randomUUID(), expected_version: state.version });
  };
  const person = async (n: number) => (await post('/api/players', {
    command_id: randomUUID(), provider: 'simulated', identity_scope: 'local-test',
    subject_id: `browser-m4-${n}`, display_name: `离线演练游客 ${n}`,
  })).player.player_id;
  const fixture = (criteria: string[], quote: string, comment?: string) => ({
    criteria: criteria.map(id => ({ id, score: 2, evidence: [quote], reason: '明确的离线契约样例，不是真实模型评价。' })),
    ...(comment === undefined ? {} : { comment }),
  });
  const acceptEvaluation = async (path: string, evaluation: unknown) => {
    const e = await get(`${path}/evaluation-request`);
    return post(`${path}/evaluation-result`, {
      command_id: randomUUID(), task_id: e.task_id, attempt_no: e.attempt_no,
      input_hash: e.input_hash, policy_hash: e.policy_hash, evaluation,
    });
  };

  // UI protocol assertions are below; game fixtures retain exact authoritative rows.
  const playerId = await person(0);
  const interview = (await post('/api/interviews', { command_id: randomUUID(), player_id: playerId })).interview;
  const interviewPath = `/api/interviews/${interview.id}`;
  await command(interviewPath, 'open');
  const answer = '很抱歉让您失望。免费重做噩梦，并请地狱质检员核查。';
  await post(`${interviewPath}/answers`, { message_id: randomUUID(), player_id: playerId, text: answer });
  await acceptEvaluation(interviewPath, fixture(['empathy', 'solution', 'setting'], answer));
  const interviewBefore = (await get(interviewPath)).interview;
  const profileBefore = (await get(`/api/players/${playerId}`)).player;

  const people = [await person(1), await person(2), await person(3)];
  const competition = (await post('/api/competitions', { command_id: randomUUID() })).competition;
  const competitionPath = `/api/competitions/${competition.id}`;
  await command(competitionPath, 'open');
  const work = '噩梦打卡枕：睡着就替客户上班，醒来发现老板变成枕头。投诉后客服免费换梦并退回加班时间。';
  const submissions: string[] = [];
  for (const id of people) submissions.push((await post(`${competitionPath}/submissions`, {
    message_id: randomUUID(), player_id: id, expected_submission_revision: 0, text: work,
  })).submission_id);
  await command(competitionPath, 'close-submissions');
  for (const id of submissions) await acceptEvaluation(`${competitionPath}/submissions/${id}`,
    fixture(['originality', 'coherence', 'setting'], work, '这款枕头代替客户上班，并由客服处理换梦。'));
  await page.reload();
  await expect(page.locator('#performance-session-create')).toBeEnabled();
  await expect(page.locator('#performance-aspect')).toHaveValue('');
  await page.locator('#performance-aspect').selectOption('16:9');
  await expect(page.locator('#performance-avatar-placeholder')).toContainText('16:9');
  await page.locator('#performance-aspect').selectOption('4:3');
  await expect(page.locator('#performance-avatar-placeholder')).toContainText('4:3');
  await page.locator('#performance-session-create').click();
  await expect(page.locator('#performance-session-state')).toContainText('active');
  const session = (await get('/api/performance-session')).session;
  expect(session.avatar_aspect).toBe('4:3');
  expect(session.media_verified).toBe(false);
  const snapshotPerformance = async (id: string) => (await get(`/api/performances/${id}`)).performance;
  const submitFrozen = async () => {
    await expect(page.locator('#performance-submit')).toBeEnabled();
    const response = page.waitForResponse(r => r.url().endsWith('/api/performances') && r.request().method() === 'POST');
    await page.locator('#performance-submit').click();
    const r = await response; expect(r.status()).toBe(201);
    return (await r.json()).performance;
  };
  const execute = async (id: string) => {
    await expect(page.locator('#performance-execute')).toBeEnabled();
    await page.locator('#performance-execute').click();
    await expect.poll(async () => (await snapshotPerformance(id)).state).toBe('send_completed');
    await expect(page.locator('#performance-detail')).toContainText('离线演练完成');
    const p = await snapshotPerformance(id);
    expect(p.evidence).toMatchObject({ execution_kind: 'offline_fixture', media_verified: false, audience_heard: false });
    expect(p.result.fixture_receipt).toBeTruthy();
    await expect(page.locator('#performance-execute')).toBeDisabled();
    return p;
  };
  await page.locator('#performance-freeze').click();
  await expect(page.locator('#performance-frozen')).toContainText('m4-demo-v1');
  const first = await submitFrozen();
  await execute(first.performance_id);
  // A distinct accepted performance can be cancelled without ever executing it.
  const cancelled = await submitFrozen();
  await page.locator('#performance-cancel').click();
  await expect.poll(async () => (await snapshotPerformance(cancelled.performance_id)).state).toBe('cancelled');
  const cancelledRecord = await snapshotPerformance(cancelled.performance_id);
  expect(cancelledRecord.segments.every((s: any) => s.state === 'cancelled')).toBe(true);
  await expect(page.locator('#performance-execute')).toBeDisabled();

  // Source selection is a UI operation; source snapshots and assets are real DB data.
  await page.locator('[data-testid="interview-freeze-performance"]').click();
  await expect(page.locator('#performance-frozen')).toContainText(interview.id);
  const interviewSpeech = await submitFrozen();
  expect(interviewSpeech.source_ref.kind).toBe('interview_result');
  expect(interviewSpeech.source_ref.id).toBe(interview.id);
  await execute(interviewSpeech.performance_id);
  expect((await get(interviewPath)).interview).toEqual(interviewBefore);
  expect((await get(`/api/players/${playerId}`)).player).toEqual(profileBefore);

  const competitionBefore = (await get(competitionPath)).competition;
  await page.locator(`[data-testid="competition-freeze-comment"][data-submission-id="${submissions[0]}"]`).click();
  await expect(page.locator('#performance-frozen')).toContainText(submissions[0]);
  const commentSpeech = await submitFrozen();
  expect(commentSpeech.source_ref.kind).toBe('competition_comment');
  expect(commentSpeech.segments.map((s: any) => s.text).join('')).toBe('这款枕头代替客户上班，并由客服处理换梦。');
  await execute(commentSpeech.performance_id);
  expect((await get(competitionPath)).competition).toEqual(competitionBefore);
  expect(competitionBefore.submissions.every((s: any) => !s.display_confirmed_at)).toBe(true);
  await expect(page.locator('#competition-open-voting')).toBeDisabled();
  for (const id of submissions) await command(competitionPath, `submissions/${id}/confirm-display`);
  await command(competitionPath, 'open-voting');
  await command(competitionPath, 'close-voting');
  await command(competitionPath, 'settle');
  const settledBefore = (await get(competitionPath)).competition;
  const profilesBefore = await Promise.all(people.map(async id => (await get(`/api/players/${id}`)).player));
  await expect(page.locator('[data-testid="competition-freeze-result"]')).toBeEnabled();
  await page.locator('[data-testid="competition-freeze-result"]').click();
  await expect(page.locator('#performance-frozen')).toContainText('competition_result');
  const resultSpeech = await submitFrozen();
  expect(resultSpeech.source_ref.id).toBe(competition.id);
  await execute(resultSpeech.performance_id);
  expect((await get(competitionPath)).competition).toEqual(settledBefore);
  expect(await Promise.all(people.map(async id => (await get(`/api/players/${id}`)).player))).toEqual(profilesBefore);

  await page.locator('#performance-session-pause').click();
  await expect(page.locator('#performance-session-state')).toContainText('paused');
  await expect(page.locator('#performance-submit')).toBeDisabled();
  await page.locator('#performance-session-resume').click();
  await expect(page.locator('#performance-session-state')).toContainText('active');
  await context.setOffline(true);
  await expect(page.locator('#connection')).toContainText('离线');
  await expect(page.locator('#performance-submit')).toBeDisabled();
  await expect(page.locator('#performance-session-close')).toBeDisabled();
  await context.setOffline(false);
  await expect(page.locator('#performance-submit')).toBeEnabled();
  await page.screenshot({ path: testInfo.outputPath('m4-offline-fixture-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('m4-offline-fixture-mobile.png'), fullPage: true });
  await page.locator('#performance-session-close').click();
  await expect.poll(async () => (await get('/api/performance-session')).session).toBeNull();
  await expect(page.locator('#performance-session-create')).toBeEnabled();
  await context.clearCookies();
  await page.locator('#reconnect').click();
  await expect(page.locator('#connection')).toContainText('尚未登录');
  await expect(page.locator('#performance-session-create')).toBeDisabled();
  await expect(page.locator('#performance-detail')).toBeEmpty();
  expect(interviewBefore.status).toBe('COMPLETED');
  expect(profileBefore.role).toBe('employee');
  expect(errors).toEqual([]);
});
