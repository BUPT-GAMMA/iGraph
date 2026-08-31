import { expect, test } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('igraph_user_id', 'e2e-user'));
});

test('both docked panel toggles remain reachable in the same screen position', async ({ page }) => {
  await page.goto('/?session=session-a');
  await expect(page.locator('.workspace-context strong')).toHaveText('Session A');
  await expect(page.getByText('iGraph 1.0')).toHaveCount(0);
  await page.waitForTimeout(220);

  const leftCollapse = page.getByRole('button', { name: '收起左侧栏' });
  const leftExpandedBox = await leftCollapse.boundingBox();
  const mainExpandedBox = await page.locator('.main-column').boundingBox();
  expect(leftExpandedBox).not.toBeNull();
  expect(mainExpandedBox).not.toBeNull();

  await leftCollapse.click();
  await expect(page.getByRole('button', { name: '展开左侧栏' })).toBeVisible();
  await page.waitForTimeout(50);
  const leftCollapsedBox = await page.getByRole('button', { name: '展开左侧栏' }).boundingBox();
  const mainDuringLeftCollapse = await page.locator('.main-column').boundingBox();
  expect(leftCollapsedBox?.x).toBeCloseTo(leftExpandedBox!.x, 0);
  expect(leftCollapsedBox?.y).toBeCloseTo(leftExpandedBox!.y, 0);
  expect(mainDuringLeftCollapse!.width).toBeGreaterThan(mainExpandedBox!.width);
  await page.waitForTimeout(220);
  const mainAfterLeftCollapse = await page.locator('.main-column').boundingBox();
  expect(mainAfterLeftCollapse!.width).toBeGreaterThan(mainDuringLeftCollapse!.width);

  const rightCollapse = page.getByRole('button', { name: '收起右侧栏' });
  const rightExpandedBox = await rightCollapse.boundingBox();
  const viewportWidth = page.viewportSize()!.width;
  const expandedRightInset = viewportWidth - rightExpandedBox!.x - rightExpandedBox!.width;
  const mainBeforeRightCollapse = await page.locator('.main-column').boundingBox();

  await rightCollapse.click();
  await expect(page.getByRole('button', { name: '展开右侧栏' })).toBeVisible();
  await page.waitForTimeout(50);
  const rightCollapsedBox = await page.getByRole('button', { name: '展开右侧栏' }).boundingBox();
  const collapsedRightInset = viewportWidth - rightCollapsedBox!.x - rightCollapsedBox!.width;
  const mainDuringRightCollapse = await page.locator('.main-column').boundingBox();
  expect(collapsedRightInset).toBeCloseTo(expandedRightInset, 0);
  expect(Math.abs(rightCollapsedBox!.y - rightExpandedBox!.y)).toBeLessThanOrEqual(1);
  expect(mainDuringRightCollapse!.width).toBeGreaterThan(mainBeforeRightCollapse!.width);
  await page.waitForTimeout(220);
  const mainAfterRightCollapse = await page.locator('.main-column').boundingBox();
  expect(mainAfterRightCollapse!.width).toBeGreaterThan(mainDuringRightCollapse!.width);
  await expect(page.getByLabel('关闭图谱工作台')).toHaveCount(0);
});

test('the workspace tab indicator slides between tabs', async ({ page }) => {
  await page.goto('/?session=session-a');
  await expect(page.locator('.workspace-context strong')).toHaveText('Session A');

  const indicator = page.locator('.workspace-tab-indicator');
  await expect(indicator).toBeVisible();
  expect(await indicator.evaluate(element => getComputedStyle(element).transitionDuration)).not.toBe('0s');
  const graphTransform = await indicator.evaluate(element => getComputedStyle(element).transform);

  await page.getByRole('tab', { name: 'Artifacts' }).click();
  await page.waitForTimeout(50);
  const movingTransform = await indicator.evaluate(element => getComputedStyle(element).transform);
  expect(movingTransform).not.toBe(graphTransform);

  await page.waitForTimeout(180);
  const artifactsTransform = await indicator.evaluate(element => getComputedStyle(element).transform);
  expect(artifactsTransform).not.toBe(movingTransform);
  await expect(page.getByRole('tab', { name: 'Artifacts' })).toHaveAttribute('aria-selected', 'true');
});

test('a selected conversation owns the highlight and dark project fields share one style', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('igraph-theme', 'dark'));
  await page.goto('/?session=session-a');
  await expect(page.locator('.workspace-context strong')).toHaveText('Session A');

  const projectNode = page.locator('.project-tree-node').filter({ hasText: 'E2E Project' });
  await expect(projectNode.locator(':scope > .project-row')).not.toHaveClass(/selected/);
  await expect(projectNode.locator('.project-session-row').filter({ hasText: 'Session A' })).toHaveClass(/selected/);
  await expect(projectNode.locator('.project-row-main strong')).toHaveCSS('color', 'rgb(241, 243, 242)');
  await expect(projectNode.locator('.project-session-row').filter({ hasText: 'Session A' }).locator('.project-session-main span')).toHaveCSS('color', 'rgb(241, 243, 242)');

  await page.getByRole('button', { name: '创建项目' }).click();
  const nameInput = page.getByLabel('项目名称');
  const descriptionInput = page.getByLabel('描述');
  await expect(nameInput).toBeVisible();
  await nameInput.evaluate(element => (element as HTMLInputElement).blur());
  const nameStyle = await nameInput.evaluate(element => {
    const style = getComputedStyle(element);
    return { background: style.backgroundColor, border: style.borderColor, color: style.color };
  });
  const descriptionStyle = await descriptionInput.evaluate(element => {
    const style = getComputedStyle(element);
    return { background: style.backgroundColor, border: style.borderColor, color: style.color };
  });
  expect(nameStyle).toEqual(descriptionStyle);
});

test('mobile navigation and docked drawers keep their exit animation mounted', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/?session=session-a');
  await expect(page.locator('.workspace-context strong')).toHaveText('Session A');

  const sidebar = page.locator('.sidebar');
  const backdrop = page.locator('.mobile-sidebar-backdrop');
  await page.getByRole('button', { name: '打开侧栏' }).click();
  await expect(sidebar).toBeVisible();
  await expect(backdrop).toBeVisible();
  expect(await sidebar.evaluate(element => getComputedStyle(element).transitionDuration)).not.toBe('0s');

  await page.getByRole('button', { name: '关闭左侧栏' }).click();
  await expect(sidebar).toBeVisible({ timeout: 100 });
  await expect(backdrop).toBeVisible({ timeout: 100 });
  await expect(sidebar).toBeHidden();
  await expect(backdrop).toBeHidden();

  await page.setViewportSize({ width: 1280, height: 800 });
  await page.locator('.composer-dataset').click();
  const datasetDialog = page.getByRole('dialog', { name: '项目数据集' });
  await expect(datasetDialog).toBeVisible();
  await expect(datasetDialog.getByText('会话共享项目数据源，但各自维护独立的图运行状态。')).toHaveCount(0);
  await expect(datasetDialog.getByText('默认')).toHaveCount(0);
  await expect(datasetDialog.getByText('当前')).toBeVisible();
  const activeDatasetRow = datasetDialog.locator('.dataset-row.active');
  await expect(activeDatasetRow).toBeVisible();
  const activeDatasetBox = await activeDatasetRow.boundingBox();
  expect(activeDatasetBox!.height).toBeLessThanOrEqual(70);
  const activeBackground = await activeDatasetRow.evaluate(element => getComputedStyle(element).backgroundColor.match(/\d+/g)?.map(Number) || []);
  expect(Math.max(...activeBackground.slice(0, 3)) - Math.min(...activeBackground.slice(0, 3))).toBeLessThanOrEqual(2);
  const mainCenter = await activeDatasetRow.locator('.dataset-row-main').evaluate(element => { const box = element.getBoundingClientRect(); return box.top + box.height / 2; });
  const actionsCenter = await activeDatasetRow.locator('.dataset-row-actions').evaluate(element => { const box = element.getBoundingClientRect(); return box.top + box.height / 2; });
  expect(Math.abs(mainCenter - actionsCenter)).toBeLessThanOrEqual(1);
  expect(await datasetDialog.evaluate(element => getComputedStyle(element).animationDuration)).not.toBe('0s');
  await datasetDialog.getByRole('button', { name: '关闭' }).click();
  await expect(page.locator('.drawer-overlay')).toHaveClass(/is-closing/);
  await expect(datasetDialog).toBeVisible({ timeout: 100 });
  await expect(datasetDialog).toHaveCount(0);

  await page.getByRole('button', { name: 'Memory' }).click();
  const managementDialog = page.getByRole('dialog', { name: 'Memory 与 Skills' });
  await expect(managementDialog).toBeVisible();
  const managementIndicator = managementDialog.locator('.management-tab-indicator');
  await expect(managementIndicator).toBeVisible();
  const memoryTransform = await managementIndicator.evaluate(element => getComputedStyle(element).transform);
  await managementDialog.getByRole('tab', { name: 'Skills' }).click();
  await page.waitForTimeout(50);
  const managementMovingTransform = await managementIndicator.evaluate(element => getComputedStyle(element).transform);
  expect(managementMovingTransform).not.toBe(memoryTransform);
  await page.waitForTimeout(180);
  expect(await managementIndicator.evaluate(element => getComputedStyle(element).transform)).not.toBe(managementMovingTransform);
  await managementDialog.getByRole('button', { name: '关闭管理面板' }).click();
  await expect(page.locator('.drawer-overlay')).toHaveClass(/is-closing/);
  await expect(managementDialog).toHaveCount(0);
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
  await expect(page.locator('.graph-workspace')).toBeHidden();

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
