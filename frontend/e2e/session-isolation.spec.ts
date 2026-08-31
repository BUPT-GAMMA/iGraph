import { expect, test } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('igraph_user_id', 'e2e-user'));
});

test('a background stream cannot pollute the newly selected session', async ({ page }) => {
  await page.goto('/');
  const composer = page.getByPlaceholder('今天帮你分析什么？粘贴边列表，或直接用自然语言提问');
  await expect(composer).toBeEnabled();

  await composer.fill('question for A');
  await composer.press('Enter');
  await expect(page.getByText('A-response-')).toBeVisible();

  await page.getByTitle('Session B').first().click();
  await expect(page.locator('.workspace-context strong')).toHaveText('Session B');
  await expect(page.getByText('A-response-')).toHaveCount(0);

  await composer.fill('question for B');
  await composer.press('Enter');
  await expect(page.getByText('B-response-complete')).toBeVisible();
  await expect(page.locator('.message-row.assistant .message-content')).not.toContainText('A-response');

  await page.getByTitle('Session A').first().click();
  await expect(page.locator('.workspace-context strong')).toHaveText('Session A');
  await expect(page.getByText('A-response-complete')).toBeVisible();
});

test('a paused session replaces Composer while other sessions keep their drafts', async ({ page }) => {
  await page.goto('/');
  await page.getByTitle('Session B').first().click();
  const composer = page.getByPlaceholder('今天帮你分析什么？粘贴边列表，或直接用自然语言提问');
  await composer.fill('draft in session B');

  await page.getByTitle('Session C Paused').first().click();
  await expect(page.getByText('Agent 请求确认')).toBeVisible();
  await expect(composer).toHaveCount(0);
  await expect(page.getByLabel('上传图文件')).toHaveCount(0);
  await expect(page.getByLabel('选择模型')).toHaveCount(0);

  await page.keyboard.press('Enter');
  await expect(page.getByText('Agent 请求确认')).toBeVisible();

  await page.getByTitle('Session B').first().click();
  await expect(composer).toBeEnabled();
  await expect(composer).toHaveValue('draft in session B');

  await page.getByTitle('Session C Paused').first().click();
  await expect(page.getByText('Agent 请求确认')).toBeVisible();

  await page.getByRole('button', { name: '批准并执行' }).dblclick();
  await expect(page.getByText('Agent 请求确认')).toHaveCount(0);
  await expect(composer).toBeVisible();
  await expect(page.getByLabel('发送')).toBeEnabled();
});

test('the approval dock stays inside a mobile viewport', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/?session=session-c');
  await expect(page.getByText('Agent 请求确认')).toBeVisible();
  await expect(page.getByPlaceholder('今天帮你分析什么？粘贴边列表，或直接用自然语言提问')).toHaveCount(0);

  const panel = await page.locator('.confirmation-card').boundingBox();
  expect(panel).not.toBeNull();
  expect(panel!.x).toBeGreaterThanOrEqual(0);
  expect(panel!.x + panel!.width).toBeLessThanOrEqual(390);
  expect(panel!.y + panel!.height).toBeLessThanOrEqual(844);
  await expect(page.getByRole('button', { name: '拒绝' })).toBeVisible();
  await expect(page.getByRole('button', { name: '批准并执行' })).toBeVisible();
});
