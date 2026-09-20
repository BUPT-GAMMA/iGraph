import { expect, test } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem('igraph-language', 'zh');
    localStorage.setItem('igraph_user_id', 'e2e-user');
  });
});

test('composer automatically fits two to six lines, reflows and resets after sending', async ({ page, request }) => {
  const api = 'http://127.0.0.1:11109/api/sessions';
  const created = await request.post(api, { data: { client_session_id: 'composer-autosize', project_id: 'project-1' } });
  expect(created.ok()).toBeTruthy();
  const { session_id: sid } = await created.json();
  try {
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto(`/?session=${sid}`);
    const composer = page.getByPlaceholder('今天帮你分析什么？粘贴边列表，或直接用自然语言提问');
    const visibleRows = () => composer.evaluate(element => {
      const style = getComputedStyle(element);
      return Math.round((element.clientHeight - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom)) / parseFloat(style.lineHeight));
    });
    await expect(composer).toHaveCSS('resize', 'none');
    await expect.poll(visibleRows).toBe(2);
    for (const count of [1, 2, 3, 6, 9, 3, 0]) {
      await composer.fill(Array.from({ length: count }, (_, index) => `第 ${index + 1} 行`).join('\n'));
      await expect.poll(visibleRows).toBe(Math.max(2, Math.min(6, count)));
      await expect(composer).toHaveCSS('overflow-y', count > 6 ? 'auto' : 'hidden');
    }

    await composer.fill('1\n2\n3\n4\n5\n6');
    await composer.press('End');
    await composer.press('Shift+Enter');
    await composer.press('7');
    await expect(composer).toHaveValue('1\n2\n3\n4\n5\n6\n7');
    await expect.poll(visibleRows).toBe(6);
    await expect.poll(() => composer.evaluate(element => element.scrollTop)).toBeGreaterThan(0);
    await composer.press('Enter');
    await expect(composer).toHaveValue('');
    await expect.poll(visibleRows).toBe(2);
    await expect(composer).toHaveCSS('overflow-y', 'hidden');

    const wrapped = '自动换行'.repeat(40);
    await composer.fill(wrapped);
    await expect.poll(visibleRows).toBeGreaterThan(2);
    await expect.poll(visibleRows).toBeLessThan(6);
    const desktopRows = await visibleRows();
    const desktopWidth = await composer.evaluate(element => element.clientWidth);
    await page.getByRole('button', { name: '收起左侧栏' }).click();
    await expect.poll(() => composer.evaluate(element => element.clientWidth)).toBeGreaterThan(desktopWidth);
    await expect.poll(visibleRows).toBeLessThanOrEqual(desktopRows);
    await page.setViewportSize({ width: 390, height: 844 });
    await expect.poll(visibleRows).toBe(6);
    await expect(composer).toHaveCSS('overflow-y', 'auto');
    await composer.fill('简短问题');
    await expect.poll(visibleRows).toBe(2);
    await expect(composer).toHaveCSS('overflow-y', 'hidden');
  } finally {
    await request.delete(`${api}/${sid}`);
  }
});

test('the model picker stays inside a compact mobile viewport', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 720 });
  await page.goto('/?session=session-a');
  const main = await page.locator('.main-column').boundingBox();
  expect(main).not.toBeNull();
  expect(main!.x).toBe(0);
  expect(main!.width).toBe(320);
  await expect(page.getByRole('button', { name: '发送' })).toBeInViewport();
  await page.getByRole('button', { name: '选择模型' }).click();

  const menu = page.getByRole('group', { name: '可用模型' });
  await expect(menu).toBeVisible();
  const box = await menu.boundingBox();
  expect(box).not.toBeNull();
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(320);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(320);
});

test('both docked panel toggles remain reachable in the same screen position', async ({ page }) => {
  await page.goto('/?session=session-a');
  await expect(page.locator('.session-row.selected .recent-session-name')).toHaveText('Session A');
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
await expect(page.getByLabel('关闭图工作台')).toHaveCount(0);
});

test('global new task follows project context while the recent-task action stays independent', async ({ page }) => {
  await page.goto('/?session=project-session');
  await page
    .getByRole('complementary')
    .getByRole('button', { name: '新建任务', exact: true })
    .click();
  await expect(page).toHaveURL('/?project=project-1&new=1');
  await expect(page.getByRole('heading', { name: '开始在 E2E Project 中图分析', exact: true })).toBeVisible();
  await expect(page.locator('.project-row').filter({ hasText: 'E2E Project' })).toHaveClass(/selected/);
  const composer = page.getByPlaceholder('今天帮你分析什么？粘贴边列表，或直接用自然语言提问');
  await composer.fill('项目对话草稿');
  await page.reload();
  await expect(composer).toHaveValue('项目对话草稿');
  await page.locator('.composer-dataset').click();
  await expect(page.getByRole('dialog', { name: '项目数据集' })).toBeVisible();
  await page.getByRole('dialog', { name: '项目数据集' }).getByRole('button', { name: '关闭', exact: true }).click();
  const request = page.waitForRequest(req => req.method() === 'POST' && new URL(req.url()).pathname === '/api/sessions');
  await composer.press('Enter');
  expect((await request).postDataJSON().project_id).toBe('project-1');
  await expect(page.locator('.project-session-list')).toContainText('项目对话草稿');

  await page.getByRole('button', { name: '新建独立会话' }).click();
  await expect(page).toHaveURL('/?new=1');
  await expect(page.getByRole('heading', { name: '开始图分析', exact: true })).toBeVisible();
  await page.locator('.composer-dataset').click();
  await expect(page.getByRole('dialog', { name: '个人数据集' })).toBeVisible();
});

test('new conversations stay local until the first prompt is sent', async ({ page }) => {
  let createRequests = 0;
  page.on('request', request => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/sessions') createRequests += 1;
  });
  await page.goto('/?session=session-a');
  await expect(page.locator('.session-row.selected .recent-session-name')).toHaveText('Session A');
  const sessionsBefore = await page.locator('.session-row').count();
  const newInProject = page.getByRole('button', { name: '在 E2E Project 中新建会话' });

  for (let index = 0; index < 10; index += 1) await newInProject.click();

  await expect(page).toHaveURL(/\?project=project-1&new=1$/);
  await expect(page.getByRole('heading', { name: '开始在 E2E Project 中图分析', exact: true })).toBeVisible();
  await expect(page.locator('.project-row').filter({ hasText: 'E2E Project' })).toHaveClass(/selected/);
  await expect(page.locator('.session-row.selected')).toHaveCount(0);
  await expect(page.locator('.session-row')).toHaveCount(sessionsBefore);
  expect(createRequests).toBe(0);

  const composer = page.getByPlaceholder('今天帮你分析什么？粘贴边列表，或直接用自然语言提问');
  await composer.fill('首次问题只创建一次');
  await composer.press('Enter');
  await expect.poll(() => createRequests).toBe(1);
  await expect(page).toHaveURL(/\?session=[^&]+$/);
  await expect(page.locator('.session-row')).toHaveCount(sessionsBefore);
  await expect(page.locator('.project-session-row.selected .project-session-main span')).toHaveText('首次问题只创建一次');
});

test('the workspace tab indicator slides between tabs', async ({ page }) => {
  await page.goto('/?session=session-a');
  await expect(page.locator('.session-row.selected .recent-session-name')).toHaveText('Session A');

  const indicator = page.locator('.workspace-tab-indicator');
  await expect(indicator).toBeVisible();
  expect(await indicator.evaluate(element => getComputedStyle(element).transitionDuration)).not.toBe('0s');
  const graphTransform = await indicator.evaluate(element => getComputedStyle(element).transform);

  await page.getByRole('tab', { name: '分析产物' }).click();
  await page.waitForTimeout(50);
  const movingTransform = await indicator.evaluate(element => getComputedStyle(element).transform);
  expect(movingTransform).not.toBe(graphTransform);

  await page.waitForTimeout(180);
  const artifactsTransform = await indicator.evaluate(element => getComputedStyle(element).transform);
  expect(artifactsTransform).not.toBe(movingTransform);
  await expect(page.getByRole('tab', { name: '分析产物' })).toHaveAttribute('aria-selected', 'true');
});

test('graph collections degrade safely and recover when switching back to a single graph', async ({ page }) => {
  await page.goto('/?session=session-a');
  await expect(page.locator('.graph-canvas svg')).toBeVisible();
  await page.getByRole('tab', { name: '概览' }).click();
  const graphScale = page.getByLabel('图规模', { exact: true });
  const nodeTotal = graphScale.locator('div').filter({ has: page.getByRole('term').filter({ hasText: /^节点$/ }) }).getByRole('definition');
  await expect(nodeTotal).toHaveText('2,708');
  await page.getByRole('tab', { name: '图谱', exact: true }).click();

  await page.locator('.composer-dataset').click();
  const datasetDialog = page.getByRole('dialog', { name: '个人数据集' });
  await datasetDialog.locator('.dataset-row').filter({ hasText: 'hiv' }).locator('.dataset-row-main').click();

  await expect(page.locator('#root').first()).not.toBeEmpty();
  await expect(page.getByText('非独立图，无法直接可视化')).toBeVisible();
  await expect(page.locator('.graph-canvas')).toHaveCount(0);
  await expect(page.locator('.graph-toolbar')).toHaveCount(0);
  await expect(page.locator('.node-inspector')).toHaveCount(0);

  await page.getByRole('tab', { name: '概览' }).click();
  await expect(page.getByText('41,127', { exact: true })).toBeVisible();
  await expect(page.getByText('roc_auc')).toBeVisible();
  await page.getByRole('tab', { name: '数据' }).click();
  await expect(page.getByText('32,901')).toBeVisible();
  await expect(page.getByText('节点词表')).toBeVisible();
  await page.getByRole('tab', { name: '机器学习' }).click();
  await expect(page.locator('.tab-placeholder strong')).toHaveText('图机器学习');
  await page.getByRole('tab', { name: '分析产物' }).click();
  await expect(page.getByRole('region', { name: '会话文件' })).toBeVisible();
  await expect(page.getByText('暂无上传文件或已发布产物')).toBeVisible();

  await page.reload();
  await expect(page.getByText('非独立图，无法直接可视化')).toBeVisible();
  await expect(page.locator('.graph-canvas')).toHaveCount(0);

  await page.locator('.composer-dataset').click();
  await page.getByRole('dialog', { name: '个人数据集' }).locator('.dataset-row').filter({ hasText: 'Cora' }).locator('.dataset-row-main').click();
  await expect(page.getByText('非独立图，无法直接可视化')).toHaveCount(0);
  await expect(page.locator('.graph-canvas svg')).toBeVisible();
});

test('a selected conversation owns the highlight and dark project fields share one style', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('igraph-theme', 'dark'));
  await page.goto('/?session=project-session');
  await expect(page.locator('.project-session-row.selected .project-session-main span')).toHaveText('Project Session');

  const projectNode = page.locator('.project-tree-node').filter({ hasText: 'E2E Project' });
  await expect(projectNode.locator(':scope > .project-row')).not.toHaveClass(/selected/);
  await expect(projectNode.locator('.project-session-row').filter({ hasText: 'Project Session' })).toHaveClass(/selected/);
  await expect(projectNode.locator('.project-row-main strong')).toHaveCSS('color', 'rgb(241, 245, 249)');
  await expect(projectNode.locator('.project-session-row').filter({ hasText: 'Project Session' }).locator('.project-session-main span')).toHaveCSS('color', 'rgb(147, 197, 253)');

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
  await expect(page.locator('.session-row.selected .recent-session-name')).toHaveText('Session A');

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
  const datasetDialog = page.getByRole('dialog', { name: '个人数据集' });
  await expect(datasetDialog).toBeVisible();
  await expect(datasetDialog.getByText('会话共享项目数据源，但各自维护独立的图运行状态。')).toHaveCount(0);
  await expect(datasetDialog.getByText('默认')).toHaveCount(0);
  await expect(datasetDialog.getByText('当前', { exact: true })).toBeVisible();
  const activeDatasetRow = datasetDialog.locator('.dataset-row.active');
  await expect(activeDatasetRow).toBeVisible();
  const activeDatasetBox = await activeDatasetRow.boundingBox();
  expect(activeDatasetBox!.height).toBeLessThanOrEqual(70);
  await expect(activeDatasetRow).toHaveCSS('background-color', 'rgb(241, 245, 249)');
  const mainCenter = await activeDatasetRow.locator('.dataset-row-main').evaluate(element => { const box = element.getBoundingClientRect(); return box.top + box.height / 2; });
  const actionsCenter = await activeDatasetRow.locator('.dataset-row-actions').evaluate(element => { const box = element.getBoundingClientRect(); return box.top + box.height / 2; });
  expect(Math.abs(mainCenter - actionsCenter)).toBeLessThanOrEqual(1);
  expect(await datasetDialog.evaluate(element => getComputedStyle(element).animationDuration)).not.toBe('0s');
  await datasetDialog.getByRole('button', { name: '关闭' }).click();
  await expect(page.locator('.drawer-overlay')).toHaveClass(/is-closing/);
  await expect(datasetDialog).toBeVisible({ timeout: 100 });
  await expect(datasetDialog).toHaveCount(0);

  await page.getByRole('button', { name: '记忆' }).click();
  const managementDialog = page.getByRole('dialog', { name: '记忆与技能' });
  await expect(managementDialog).toBeVisible();
  const managementIndicator = managementDialog.locator('.management-tab-indicator');
  await expect(managementIndicator).toBeVisible();
  const memoryTransform = await managementIndicator.evaluate(element => getComputedStyle(element).transform);
  await managementDialog.getByRole('tab', { name: '技能' }).click();
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
  await expect(page.locator('.session-row.selected .recent-session-name')).toHaveText('Session B');
  await expect(page.getByText('A-response-')).toHaveCount(0);

  await composer.fill('question for B');
  await composer.press('Enter');
  await expect(page.getByText('B-response-complete')).toBeVisible();
  await expect(page.locator('.message-row.assistant .message-content')).not.toContainText('A-response');

  await page.getByTitle('Session A').first().click();
  await expect(page.locator('.session-row.selected .recent-session-name')).toHaveText('Session A');
  await expect(page.getByText('A-response-complete')).toBeVisible();
});

test('a paused session replaces Composer while other sessions keep their drafts', async ({ page }) => {
  await page.goto('/?session=session-b');
  const composer = page.getByPlaceholder('今天帮你分析什么？粘贴边列表，或直接用自然语言提问');
  await composer.fill('draft in session B');

  await page.getByTitle('Session C Paused').first().click();
  await expect(page.getByText('助手请求确认')).toBeVisible();
  await expect(composer).toHaveCount(0);
  await expect(page.getByLabel('上传图文件')).toHaveCount(0);
  await expect(page.getByLabel('选择模型')).toHaveCount(0);

  await page.keyboard.press('Enter');
  await expect(page.getByText('助手请求确认')).toBeVisible();

  await page.getByTitle('Session B').first().click();
  await expect(composer).toBeEnabled();
  await expect(composer).toHaveValue('draft in session B');

  await page.getByTitle('Session C Paused').first().click();
  await expect(page.getByText('助手请求确认')).toBeVisible();

  await page.getByRole('button', { name: '批准并执行' }).dblclick();
  await expect(page.getByText('助手请求确认')).toHaveCount(0);
  await expect(composer).toBeVisible();
  await expect(page.getByLabel('发送')).toBeEnabled();
});

test('the approval dock stays inside a mobile viewport', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/?session=session-c');
  await expect(page.getByText('助手请求确认')).toBeVisible();
  await expect(page.getByPlaceholder('今天帮你分析什么？粘贴边列表，或直接用自然语言提问')).toHaveCount(0);
  await expect(page.locator('.graph-workspace')).toBeHidden();

  const panel = page.locator('.confirmation-card');
  await expect.poll(async () => panel.evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    return bounds.left >= 0
      && bounds.right <= document.documentElement.clientWidth
      && bounds.top >= 0
      && bounds.bottom <= document.documentElement.clientHeight;
  })).toBe(true);
  await expect(page.getByRole('button', { name: '拒绝' })).toBeVisible();
  await expect(page.getByRole('button', { name: '批准并执行' })).toBeVisible();
});

test('browser history keeps the URL and selected workspace in sync', async ({ page }) => {
  await page.goto('/?session=session-a');
  await expect(page.locator('.session-row.selected .recent-session-name')).toHaveText('Session A');

  await page.getByTitle('Session B').first().click();
  await expect(page).toHaveURL(/\?session=session-b$/);
  await expect(page.locator('.session-row.selected .recent-session-name')).toHaveText('Session B');

  await page.goBack();
  await expect(page).toHaveURL(/\?session=session-a$/);
  await expect(page.locator('.session-row.selected .recent-session-name')).toHaveText('Session A');
});

test('deleting a background session keeps the current session selected', async ({ page }) => {
  await page.goto('/?session=session-b');
  await expect(page.locator('.session-row.selected .recent-session-name')).toHaveText('Session B');
  page.once('dialog', dialog => dialog.accept());

  const backgroundSession = page.locator('.session-row').filter({ hasText: 'Session C Paused' });
  await backgroundSession.getByRole('button', { name: '删除任务' }).click();

  await expect(page).toHaveURL(/\?session=session-b$/);
  await expect(page.locator('.session-row.selected .recent-session-name')).toHaveText('Session B');
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
  await expect(page.locator('.session-row.selected .recent-session-name')).toHaveText('Session A');
  page.once('dialog', dialog => dialog.accept());

  const recent = page.locator('.session-row').filter({ hasText: 'Session A' });
  await recent.getByRole('button', { name: '删除任务' }).click();

  await expect(page).toHaveURL('/');
  await expect(page.getByRole('heading', { name: '开始新的图分析', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '新建任务' }).last()).toBeEnabled();
  await expect(page.getByPlaceholder('今天帮你分析什么？粘贴边列表，或直接用自然语言提问')).toHaveCount(0);
});

test('deleting a project also clears its active session and route', async ({ page }) => {
  await page.goto('/?session=project-session');
  await expect(page.locator('.project-session-row.selected .project-session-main span')).toHaveText('Project Session');

  await page.getByRole('button', { name: 'E2E Project 项目操作' }).click();
  await page.getByRole('menuitem', { name: '删除项目' }).click();
  await page.getByRole('button', { name: '删除项目及全部内容' }).click();

  await expect(page).toHaveURL('/?new=1');
  await expect(page.getByRole('heading', { name: '开始图分析', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '新建任务' }).last()).toBeEnabled();
  await expect(page.getByText('Project Session', { exact: true })).toHaveCount(0);
  await expect(page.getByText('Session B', { exact: true })).toBeVisible();
});
