import { expect, test } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem('igraph-language', 'zh');
    localStorage.setItem('igraph_user_id', 'e2e-user');
  });
});

test('recovery: dropped stream restores exact output once and never resends the question', async ({ page, request }) => {
  const created = await request.post('http://127.0.0.1:11109/api/sessions', { data: { client_session_id: `recovery-${Date.now()}`, initial_message_preview: 'Recovery', project_id: 'general' } });
  const { session_id: id } = await created.json();
  let sends = 0, resumes = 0;
  const cursors: string[] = [];
  page.on('request', req => {
    if (req.url().includes('/chat/stream')) sends++;
    if (req.url().includes('/resume?')) { resumes++; cursors.push(new URL(req.url()).searchParams.get('last_event_index')!); }
  });
  await page.route('**/chat/stream', async route => {
    const upstream = await route.fetch();
    const frames = (await upstream.text()).split('\n\n');
    // The deterministic producer completes in the background. Deliver only
    // its first token to the browser and lose the completion notification.
    await route.fulfill({ response: upstream, body: frames.slice(0, 2).join('\n\n') + '\n\n' });
  });
  await page.goto(`/?session=${id}`);
  const composer = page.getByPlaceholder('今天帮你分析什么？粘贴边列表，或直接用自然语言提问');
  await composer.fill('recover exact output'); await composer.press('Enter');
  await expect(page.locator('.message-row.assistant .message-content')).toHaveText('B-response-');
  await expect(page.locator('.message-row.assistant .message-content')).toHaveText('B-response-complete');
  await expect(page.getByLabel('发送', { exact: true })).toBeEnabled();
  expect(sends).toBe(1); expect(resumes).toBe(1); expect(cursors).toEqual(['1']);
  await expect(page.locator('.message-row.assistant')).toHaveCount(1);
});

test('recovery: reload discards a stored cursor without matching in-memory messages', async ({ page, request }) => {
  const created = await request.post('http://127.0.0.1:11109/api/sessions', { data: { client_session_id: `refresh-${Date.now()}`, project_id: 'general' } });
  const { session_id: id } = await created.json();
  await request.post(`http://127.0.0.1:11109/api/sessions/${id}/chat/stream`, { data: { message: 'refresh' } });
  const cursors: string[] = [];
  page.on('request', req => { if (req.url().includes('/resume?')) cursors.push(new URL(req.url()).searchParams.get('last_event_index')!); });
  await page.addInitScript(({ id }) => sessionStorage.setItem(`igraph-active-run:v2:e2e-user:${id}`, JSON.stringify({ runId: `run-${id}`, lastEventIndex: 999 })), { id });
  await page.route(`**/sessions/${id}/activate`, async route => {
    const upstream = await route.fetch(); const data = await upstream.json();
    await route.fulfill({ response: upstream, json: { ...data,
      messages: [{ id: `assistant:run-${id}`, role: 'assistant', content: 'B-response-' }],
      active_run: { run_id: `run-${id}`, status: 'running', last_event_index: 999 },
    } });
  });
  await page.goto(`/?session=${id}`);
  await expect(page.locator('.message-row.assistant .message-content')).toHaveText('B-response-complete');
  await expect(page.getByLabel('发送', { exact: true })).toBeEnabled();
  expect(cursors).toEqual(['-1']);
  await expect(page.locator('.message-row.assistant')).toHaveCount(1);
});
