import { expect, test } from '@playwright/test';

const api = 'http://127.0.0.1:11119';

for (const approval of [false, true]) {
  test(`native Factory → BFF → browser: ${approval ? 'approval snapshot recovery' : 'incremental recovery'}`, async ({ page, request }) => {
    await page.addInitScript(() => localStorage.setItem('igraph_user_id', 'admin'));
    const before = await (await request.get(`${api}/test-metrics`)).json();
    const created = await request.post(`${api}/api/sessions`, { headers: { 'X-User-ID': 'admin' }, data: {} });
    expect(created.ok(), await created.text()).toBeTruthy();
    const { session_id: sessionId } = await created.json();
    let sends = 0, decisions = 0, resumes = 0, cutoff = -1;
    const cursors: number[] = [];
    page.on('request', req => {
      if (req.url().endsWith('/chat/stream')) sends++;
      if (req.url().endsWith('/feedback/stream')) decisions++;
      if (req.url().includes('/resume?')) {
        resumes++;
        cursors.push(Number(new URL(req.url()).searchParams.get('last_event_index')));
      }
    });
    await page.route(approval ? '**/feedback/stream' : '**/chat/stream', async route => {
      const upstream = await route.fetch();
      expect(upstream.ok(), await upstream.text()).toBeTruthy();
      const frames = (await upstream.text()).split('\n\n').filter(Boolean);
      const firstToken = frames.findIndex(frame => frame.startsWith('data:') && JSON.parse(frame.slice(5)).events.some((event: any) => event.type === 'token'));
      expect(firstToken).toBeGreaterThanOrEqual(0);
      cutoff = JSON.parse(frames[firstToken].slice(5)).event_index;
      // Simulate a transport EOF after A. Native buffering/DB already contain
      // the tail; both public recovery endpoints remain completely real.
      await route.fulfill({ response: upstream, body: frames.slice(0, firstToken + 1).join('\n\n') + '\n\n' });
    });
    await page.goto(`/?session=${sessionId}`);
    const composer = page.getByPlaceholder('今天帮你分析什么？粘贴边列表，或直接用自然语言提问');
    await composer.fill(approval ? 'approval please' : 'recover ABC');
    await composer.press('Enter');
    if (approval) {
      await expect(page.getByRole('button', { name: '批准并执行' })).toBeEnabled();
      await page.getByRole('button', { name: '批准并执行' }).click();
    } else {
      await expect(page.locator('.message-row.assistant .message-content')).toHaveText('A');
    }
    await expect(page.locator('.message-row.assistant .message-content')).toHaveText('ABC');
    await expect(page.getByLabel('发送', { exact: true })).toBeEnabled();
    await expect(page.locator('.message-row.assistant')).toHaveCount(1);
    expect(sends).toBe(1);
    expect(decisions).toBe(approval ? 1 : 0);
    if (!approval) { expect(resumes).toBe(1); expect(cursors).toEqual([cutoff]); }
    await expect.poll(async () => (await (await request.get(`${api}/test-metrics`)).json()).subscribers).toBe(0);
    const after = await (await request.get(`${api}/test-metrics`)).json();
    expect(after.builds - before.builds).toBe(approval ? 2 : 1);
    expect(after.effects - before.effects).toBe(approval ? 1 : 0);
  });
}
