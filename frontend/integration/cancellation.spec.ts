import { expect, test } from '@playwright/test';

const api = 'http://127.0.0.1:11129';
const placeholder = '今天帮你分析什么？粘贴边列表，或直接用自然语言提问';
test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('igraph_user_id', 'admin'));
});

for (const mode of ['stream', 'first-token', 'approval']) {
  test(`real execution exits and send reopens: ${mode}`, async ({ page, request }) => {
    const capabilities = await (await request.get(`${api}/api/capabilities`)).json();
    expect(capabilities).toMatchObject({ run_cancel_v1: true, job_cancel_v1: true });
    const created = await request.post(`${api}/api/sessions`, { headers: { 'X-User-ID': 'admin' }, data: { model: 'qwen3.6-plus' } });
    expect(created.ok()).toBeTruthy();
    const { session_id: sid } = await created.json();
    const question = `${mode}-browser-${Date.now()}`;
    let starts = 0, approvals = 0, cancelTarget: unknown;
    page.on('request', request => {
      if (request.url().endsWith('/chat/stream')) starts++;
      if (request.url().endsWith('/feedback/stream')) approvals++;
      if (request.url().endsWith('/runs/cancel')) cancelTarget = request.postDataJSON().target;
    });
    await page.goto(`/?session=${sid}`);
    const composer = page.getByPlaceholder(placeholder);
    await composer.fill(question);
    await composer.press('Enter');
    if (mode === 'stream') await expect(page.locator('.message-row.assistant .message-content')).toHaveText('A');
    if (mode === 'approval') await expect(page.getByRole('button', { name: '批准并执行' })).toBeEnabled();
    await expect.poll(async () => (await (await request.get(`${api}/test-metrics`)).json()).calls[question]).toBe(1);
    await page.getByRole('button', { name: '终止当前任务', exact: true }).click();
    await expect.poll(() => cancelTarget).toBeTruthy();
    await expect(page.getByRole('button', { name: '终止当前任务', exact: true })).toHaveCount(0);
    await expect.poll(async () => (await (await request.get(`${api}/test-metrics`)).json()).closed[question]).toBe(1);
    if (mode === 'stream') await expect(page.locator('.message-row.assistant .message-content')).toHaveText('A');
    await expect(page.getByRole('button', { name: '批准并执行' })).toHaveCount(0);
    await expect(page.locator('.error-banner')).toHaveCount(0);
    expect(starts).toBe(1); expect(approvals).toBe(0);
    await composer.fill('complete-next');
    await expect(page.getByLabel('发送', { exact: true })).toBeEnabled();
    await composer.press('Enter');
    await expect(page.locator('.message-row.assistant .message-content').last()).toHaveText('ABC');
  });
}

test('failed old snapshot does not block a new request or overwrite its messages', async ({ page, request }) => {
  const created = await request.post(`${api}/api/sessions`, { headers: { 'X-User-ID': 'admin' }, data: { model: 'qwen3.6-plus' } });
  const { session_id: sid } = await created.json();
  await page.goto(`/?session=${sid}`);
  const composer = page.getByPlaceholder(placeholder);
  await composer.fill(`stream-snapshot-${Date.now()}`); await composer.press('Enter');
  await expect(page.locator('.message-row.assistant .message-content')).toHaveText('A');
  await page.route('**/snapshot', route => route.fulfill({ status: 503, json: { detail: 'test snapshot unavailable' } }));
  await page.getByRole('button', { name: '终止当前任务', exact: true }).click();
  await composer.fill('complete-after-snapshot-failure');
  await expect(page.getByLabel('发送', { exact: true })).toBeEnabled(); await composer.press('Enter');
  await expect(page.locator('.message-row.assistant .message-content').last()).toHaveText('ABC');
  await page.unroute('**/snapshot');
  await expect(page.locator('.message-row.assistant .message-content')).toHaveText(['A', 'ABC']);
});

test('reload after a lost cancel response queries the same intent and never resends the question', async ({ page, request }) => {
  const created = await request.post(`${api}/api/sessions`, { headers: { 'X-User-ID': 'admin' }, data: { model: 'qwen3.6-plus' } });
  const { session_id: sid } = await created.json();
  const question = `stream-lost-cancel-${Date.now()}`;
  const requests: string[] = [];
  page.on('request', req => { if (req.url().endsWith('/runs/cancel')) requests.push(req.postDataJSON().cancel_request_id); });
  await page.goto(`/?session=${sid}`);
  await page.getByPlaceholder(placeholder).fill(question);
  await page.getByPlaceholder(placeholder).press('Enter');
  await expect(page.locator('.message-row.assistant .message-content')).toHaveText('A');
  let accepted = false;
  await page.route('**/runs/control?*', route => route.fulfill({ status: 503, json: { detail: 'injected observation outage' } }));
  await page.route('**/runs/cancel', async route => {
    const reply = await route.fetch();
    expect([200, 202]).toContain(reply.status());
    accepted = true;
    await route.abort('failed');
  });
  await page.getByRole('button', { name: '终止当前任务', exact: true }).click();
  await expect.poll(() => accepted).toBeTruthy();
  const persisted = await page.evaluate(sid => JSON.parse(localStorage.getItem(`igraph-cancel:v1:admin:${sid}`) || '[]'), sid);
  expect(persisted).toHaveLength(1);
  expect(persisted[0].cancelRequestId).toBe(requests[0]);
  await page.unroute('**/runs/control?*'); await page.unroute('**/runs/cancel');
  await page.reload();
  await page.getByPlaceholder(placeholder).fill('complete-after-reload');
  await expect(page.getByLabel('发送', { exact: true })).toBeEnabled();
  await expect.poll(async () => (await (await request.get(`${api}/test-metrics`)).json()).closed[question]).toBe(1);
  expect((await (await request.get(`${api}/test-metrics`)).json()).calls[question]).toBe(1);
  expect(new Set(requests).size).toBe(1);
  await expect(page.locator('.message-row.assistant .message-content')).toHaveText('A');
});

test('stopping before session creation completes keeps the draft and blocks a late launch', async ({ page, request }) => {
  const created = await request.post(`${api}/api/projects`, { headers: { 'X-User-ID': 'admin' }, data: { name: 'Draft cancellation' } });
  expect(created.ok()).toBeTruthy();
  const payload = await created.json();
  const pid = payload.project?.project_id || payload.project_id;
  expect(pid).toBeTruthy();
  await page.goto(`/?project=${pid}&new=1`);
  let registered = false, sent = 0;
  let release!: () => void;
  const delayed = new Promise<void>(resolve => { release = resolve; });
  page.on('request', req => { if (req.url().endsWith('/chat/stream')) sent++; });
  await page.route('**/api/sessions', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    const reply = await route.fetch();
    registered = true;
    await delayed;
    await route.fulfill({ response: reply });
  });
  const composer = page.getByPlaceholder(placeholder);
  await composer.fill('this draft must never start'); await composer.press('Enter');
  await expect.poll(() => registered).toBeTruthy();
  await page.getByRole('button', { name: '终止当前任务', exact: true }).click();
  await expect(composer).toHaveValue('this draft must never start');
  await composer.fill('my newer edit');
  release();
  await expect(page.getByLabel('发送', { exact: true })).toBeEnabled();
  await expect(composer).toHaveValue('my newer edit');
  expect(sent).toBe(0);
  expect((await (await request.get(`${api}/test-metrics`)).json()).calls['this draft must never start']).toBeUndefined();
});

test('a historical training card stops its worker while the new run keeps streaming', async ({ page, request }) => {
  const created = await request.post(`${api}/api/sessions`, { headers: { 'X-User-ID': 'admin' }, data: { model: 'qwen3.6-plus' } });
  const { session_id: sid } = await created.json();
  // Current dev waits for tool jobs. Seed the old asynchronous producer explicitly.
  expect((await request.post(`${api}/test-seed/${sid}?legacy_async=true`)).ok()).toBeTruthy();
  const question = `background-training-card-${Date.now()}`;
  await page.goto(`/?session=${sid}`);
  const composer = page.getByPlaceholder(placeholder);
  await composer.fill(question); await composer.press('Enter');
  await expect(page.locator('.message-row.assistant .message-content').last()).toHaveText('ABC');
  let jid = '';
  await expect.poll(async () => {
    const metrics = await (await request.get(`${api}/test-metrics`)).json();
    jid = metrics.jobs.find((job: { session_id: string; tool_name: string; status: string; progress: number }) =>
      job.session_id === sid && job.tool_name === 'train' && job.status === 'running' && job.progress > 0)?.job_id || '';
    return jid;
  }, { timeout: 20_000 }).not.toBe('');
  await page.getByRole('tab', { name: '机器学习', exact: true }).click();
  const stopJob = page.getByRole('button', { name: '终止作业', exact: true }).first();
  await expect(stopJob).toBeVisible();
  const next = `stream-while-old-training-${Date.now()}`;
  await composer.fill(next); await composer.press('Enter');
  // Preparation includes project history and live job reads while the worker
  // is training. Wait for native model entry independently of stop latency.
  await expect.poll(async () => (await (await request.get(`${api}/test-metrics`)).json()).calls[next],
    { timeout: 20_000, message: 'the new run starts its model request while historical training continues' }).toBe(1);
  await expect(page.locator('.message-row.assistant .message-content').last()).toHaveText('A');
  await stopJob.click();
  await expect.poll(async () => (await (await request.get(`${api}/test-metrics`)).json()).jobs.find((job: { job_id: string }) => job.job_id === jid)?.status).toBe('cancelled');
  const metrics = await (await request.get(`${api}/test-metrics`)).json();
  expect(metrics.closed[next]).toBeUndefined();
  await expect(page.getByRole('button', { name: '终止当前任务', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: '终止当前任务', exact: true }).click();
  await expect.poll(async () => (await (await request.get(`${api}/test-metrics`)).json()).closed[next]).toBe(1);
});
