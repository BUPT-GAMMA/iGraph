import { expect, test } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('igraph_user_id', 'e2e-user'));
});

test('a background stream cannot pollute the newly selected session', async ({ page }) => {
  await page.goto('/?session=session-a');
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
  await page.goto('/?session=session-b');
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

test('browser history keeps the URL and selected workspace in sync', async ({ page }) => {
  await page.goto('/?session=session-a');
  await expect(page.locator('.workspace-context strong')).toHaveText('Session A');

  await page.getByTitle('Session B').first().click();
  await expect(page).toHaveURL(/\?session=session-b$/);
  await expect(page.locator('.workspace-context strong')).toHaveText('Session B');

  await page.goBack();
  await expect(page).toHaveURL(/\?session=session-a$/);
  await expect(page.locator('.workspace-context strong')).toHaveText('Session A');
});

test('deleting a background session keeps the current session selected', async ({ page }) => {
  await page.goto('/?session=session-b');
  await expect(page.locator('.workspace-context strong')).toHaveText('Session B');
  page.once('dialog', dialog => dialog.accept());

  const backgroundSession = page.locator('.session-row').filter({ hasText: 'Session C Paused' });
  await backgroundSession.getByRole('button', { name: '删除任务' }).click();

  await expect(page).toHaveURL(/\?session=session-b$/);
  await expect(page.locator('.workspace-context strong')).toHaveText('Session B');
  await expect(page.getByPlaceholder('今天帮你分析什么？粘贴边列表，或直接用自然语言提问')).toBeEnabled();
});

test('an invalid deep link returns to an actionable home page', async ({ page }) => {
  await page.goto('/?session=missing-session');
  await expect(page).toHaveURL('/');
  await expect(page.getByText('该会话不存在或已被删除')).toBeVisible();
  await expect(page.getByRole('button', { name: '新建任务' }).last()).toBeEnabled();
  await expect(page.getByPlaceholder('今天帮你分析什么？粘贴边列表，或直接用自然语言提问')).toHaveCount(0);

  await page.goto('/?project=missing-project');
  await expect(page).toHaveURL('/');
  await expect(page.getByText('该项目不存在或已被删除')).toBeVisible();
  await expect(page.getByRole('button', { name: '新建任务' }).last()).toBeEnabled();
});

test('deleting the active session replaces its stale URL with home', async ({ page }) => {
  await page.goto('/?session=session-a');
  await expect(page.locator('.workspace-context strong')).toHaveText('Session A');
  page.once('dialog', dialog => dialog.accept());

  const recent = page.locator('.session-row').filter({ hasText: 'Session A' });
  await recent.getByRole('button', { name: '删除任务' }).click();

  await expect(page).toHaveURL('/');
  await expect(page.locator('.workspace-context strong')).toHaveText('iGraph 工作台');
  await expect(page.getByRole('button', { name: '新建任务' }).last()).toBeEnabled();
  await expect(page.getByPlaceholder('今天帮你分析什么？粘贴边列表，或直接用自然语言提问')).toHaveCount(0);
});

test('deleting a project also clears its active session and route', async ({ page }) => {
  await page.goto('/?session=session-b');
  await expect(page.locator('.workspace-context strong')).toHaveText('Session B');

  await page.getByRole('button', { name: 'E2E Project 项目操作' }).click();
  await page.getByRole('button', { name: '删除项目' }).click();
  await page.getByText('删除项目和全部会话').click();
  await page.getByRole('button', { name: '继续' }).click();

  await expect(page).toHaveURL('/');
  await expect(page.locator('.workspace-context strong')).toHaveText('iGraph 工作台');
  await expect(page.getByRole('button', { name: '新建任务' }).last()).toBeEnabled();
  await expect(page.getByText('Session B')).toHaveCount(0);
});
