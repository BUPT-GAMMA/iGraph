import { expect as baseExpect, test } from '@playwright/test';
const expect = baseExpect.configure({ timeout: 20000 });
const api = 'http://127.0.0.1:11129';
const headers = { 'X-User-ID': 'admin' };
const placeholder = '今天帮你分析什么？粘贴边列表，或直接用自然语言提问';
test.beforeEach(async ({ page }) => { await page.addInitScript(() => localStorage.setItem('igraph_user_id', 'admin')); });

for (const stopped of [false, true]) {
  test(`replace latest ${stopped ? 'stopped' : 'completed'} turn through the real runtime`, async ({ page, request }) => {
    test.setTimeout(90000);
    const created = await request.post(`${api}/api/sessions`, { headers, data: { model: 'qwen3.6-plus' } });
    expect(created.ok()).toBeTruthy();
    const { session_id: sid } = await created.json();
    const earlier = `complete-earlier-${sid}`, original = `${stopped ? 'stream' : 'complete'}-original-${sid}`, replacement = `complete-revised-${sid}`;
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto(`/?session=${sid}`);
    const composer = page.getByPlaceholder(placeholder);
    await composer.fill(earlier); await composer.press('Enter');
    await expect(page.getByRole('button', { name: '修改提示词' })).toHaveCount(1, { timeout: 20000 });
    await composer.fill(original); await composer.press('Enter');
    if (stopped) {
      await expect(page.locator('.message-row.assistant .message-content').last()).toHaveText('A');
      await expect(page.getByRole('button', { name: '修改提示词' })).toHaveCount(0);
      await page.getByRole('button', { name: '终止当前任务', exact: true }).click();
    }
    await expect(page.getByRole('button', { name: '修改提示词' })).toHaveCount(1, { timeout: 20000 });
    const before = await (await request.post(`${api}/api/sessions/${sid}/activate`, { headers })).json();
    const oldRunId = before.messages.at(-1).run_id;
    await composer.fill('保留独立草稿');
    await page.locator('.message-row.user').last().hover();
    await page.getByRole('button', { name: '修改提示词' }).click();
    const editor = page.getByRole('textbox', { name: '修改本轮提示词' });
    await expect(editor).toHaveValue(original);
    await editor.fill(replacement);
    if (!stopped) {
      await page.screenshot({ path: '../output/playwright/edit-turn-desktop.png' });
      await page.getByRole('button', { name: '切换到深色主题' }).click();
      await page.screenshot({ path: '../output/playwright/edit-turn-desktop-dark.png' });
      await page.getByRole('button', { name: '收起右侧栏' }).click();
      await page.getByRole('button', { name: '收起左侧栏' }).click();
      await page.setViewportSize({ width: 390, height: 844 });
      await expect.poll(async () => (await page.locator('.main-column').boundingBox())?.x).toBe(0);
      await expect.poll(async () => (await page.locator('.main-column').boundingBox())?.width).toBe(390);
      await expect(editor).toBeInViewport();
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(390);
      await page.screenshot({ path: '../output/playwright/edit-turn-mobile.png' });
    }
    const sent = page.waitForRequest(req => req.url().endsWith('/chat/stream'));
    await page.getByRole('button', { name: '发送修改后的提示词' }).click();
    expect((await sent).postDataJSON()).toMatchObject({ message: replacement, replace_run_id: oldRunId });
    await expect(page.locator('.message-row.user .message-content')).toHaveText([earlier, replacement]);
    await expect(page.getByRole('button', { name: '修改提示词' })).toHaveCount(1, { timeout: 20000 });
    await expect(page.locator('.message-row.assistant .message-content')).toHaveText(['ABC', 'ABC']);
    await expect(composer).toHaveValue('保留独立草稿');
    const contexts = (await (await request.get(`${api}/test-metrics`)).json()).contexts;
    const modelInput = JSON.stringify(contexts[replacement]);
    expect(modelInput).toContain(earlier); expect(modelInput).toContain(replacement); expect(modelInput).not.toContain(original);
    await page.reload();
    await expect(page.locator('.message-row.user .message-content')).toHaveText([earlier, replacement]);
    await expect(page.locator('.message-row.assistant')).toHaveCount(2);
    // A stale second window cannot delete the newly completed turn.
    const stale = await request.post(`${api}/api/sessions/${sid}/chat/stream`, { headers, data: { message: 'stale edit', replace_run_id: oldRunId, start_request_id: crypto.randomUUID() } });
    expect(stale.status()).toBe(409);
    const after = await (await request.post(`${api}/api/sessions/${sid}/activate`, { headers })).json();
    expect(after.messages[0]).toEqual(before.messages[0]);
    expect(after.messages[1]).toEqual(before.messages[1]);
    expect(after.messages.filter((message: any) => message.role === 'user').map((message: any) => message.content)).toEqual([earlier, replacement]);
  });
}
