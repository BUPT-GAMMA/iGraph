import { expect, test, type Page, type Request } from '@playwright/test';

test('shows the workspace before login and authenticates from the avatar menu', async ({ page }) => {
  await page.goto('/');

  await expect(page.locator('.login-screen')).toHaveCount(0);
  await expect(page.getByRole('heading', { name: '探索图数据，从一个问题开始' })).toBeVisible();
  await expect(page.getByText('无需再经过单独的登录开始页')).toBeVisible();

  await page.getByRole('button', { name: '打开登录' }).click();
  await expect(page.getByRole('dialog', { name: '账户' })).toBeVisible();
  await page.getByLabel('用户 ID').fill('e2e-user');
  await page.getByRole('button', { name: '登录', exact: true }).click();

  await expect(page.getByRole('button', { name: '账户：e2e-user' })).toBeVisible();
  await expect(page.getByRole('heading', { name: '开始新的图分析' })).toBeVisible();
  await expect(page.getByText('Session A', { exact: true })).toBeVisible();
});

test('routes signed-out workspace actions to the same account menu', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: '新建任务', exact: true }).click();

  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByRole('dialog', { name: '账户' })).toBeVisible();
  await expect(page.getByLabel('用户 ID')).toBeFocused();
});

async function loginAs(page: Page, userId: string) {
  await page.getByRole('button', { name: '打开登录' }).click();
  await page.getByLabel('用户 ID').fill(userId);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page.getByRole('button', { name: `账户：${userId}` })).toBeVisible();
}

async function switchTo(page: Page, from: string, to: string) {
  await page.getByRole('button', { name: `账户：${from}` }).click();
  await page.getByRole('button', { name: '使用其他用户登录' }).click();
  await page.getByLabel('用户 ID').fill(to);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page.getByRole('button', { name: `账户：${to}` })).toBeVisible();
}

const composer = (page: Page) => page.getByRole('textbox', { name: '今天帮你分析什么？粘贴边列表，或直接用自然语言提问' });

test('two tabs synchronize accounts without sending or copying the previous account draft and attachment', async ({ page, context }) => {
  await context.route('**/api/sessions', async route => {
    const request = route.request();
    if (request.method() !== 'GET') return route.continue();
    const userId = request.headers()['x-user-id'];
    await route.fulfill({ json: { sessions: [{ session_id: `${userId}-session`, title: `${userId} private session` }] } });
  });
  await context.route('**/api/sessions/*/upload', route => route.fulfill({ json: { success: true, filename: 'b.csv', file_token: 'b-file-token' } }));
  const writes: Request[] = [];
  context.on('request', request => {
    if (request.method() === 'POST' && /\/api\/sessions(?:$|\/[^/]+\/(?:upload|chat\/stream)$)/.test(new URL(request.url()).pathname)) writes.push(request);
  });
  await page.goto('/');
  await loginAs(page, 'account-a');
  const peer = await context.newPage();
  await peer.goto('/?new=1');
  await composer(peer).fill('A private draft');
  await peer.locator('input[type="file"]').setInputFiles({ name: 'a-private.csv', mimeType: 'text/csv', buffer: Buffer.from('A-private-data') });
  await expect(peer.getByText('a-private.csv', { exact: true })).toBeVisible();

  await switchTo(page, 'account-a', 'account-b');
  await expect(peer.getByRole('button', { name: '账户：account-b' })).toBeVisible();
  await expect(peer).toHaveURL('/');
  await expect(peer.getByText('account-b private session', { exact: true })).toBeVisible();
  await expect(peer.getByText('account-a private session', { exact: true })).toHaveCount(0);
  await expect(peer.getByRole('button', { name: '发送', exact: true })).toHaveCount(0);
  expect(writes).toHaveLength(0);

  await peer.getByRole('complementary').getByRole('button', { name: '新建任务', exact: true }).click();
  await expect(composer(peer)).toHaveValue('');
  await expect(peer.getByText('a-private.csv', { exact: true })).toHaveCount(0);
  await composer(peer).fill('B public draft');
  await peer.locator('input[type="file"]').setInputFiles({ name: 'b.csv', mimeType: 'text/csv', buffer: Buffer.from('B-public-data') });
  await peer.getByRole('button', { name: '发送', exact: true }).click();
  await expect.poll(() => writes.length).toBe(3);
  expect(writes.every(request => request.headers()['x-user-id'] === 'account-b')).toBe(true);
  expect(writes[0].postDataJSON().initial_message_preview).toBe('B public draft');
  expect(writes[1].postData()).toContain('B-public-data');
  expect(writes[2].postDataJSON()).toMatchObject({ message: 'B public draft', attachments: [{ file_token: 'b-file-token', filename: 'b.csv' }] });
  expect(writes.some(request => /A private draft|A-private-data|a-private.csv/.test(request.postData() || ''))).toBe(false);

  await switchTo(page, 'account-b', 'account-a');
  await expect(peer.getByRole('button', { name: '账户：account-a' })).toBeVisible();
  await peer.getByRole('complementary').getByRole('button', { name: '新建任务', exact: true }).click();
  await expect(composer(peer)).toHaveValue('A private draft');
  await expect(peer.getByText('a-private.csv', { exact: true })).toBeVisible();
});

test('a click before the storage event cannot create a session under the new identity', async ({ page }) => {
  await page.goto('/');
  await loginAs(page, 'account-a');
  await page.getByRole('complementary').getByRole('button', { name: '新建任务', exact: true }).click();
  await composer(page).fill('A private draft');
  const writes: Request[] = [];
  page.on('request', request => { if (request.method() === 'POST') writes.push(request); });
  // Model the interval after shared storage changes but before its event arrives.
  await page.evaluate(() => {
    localStorage.setItem('igraph_user_id', 'account-b');
    document.querySelector<HTMLButtonElement>('.send-button')!.click();
  });
  await expect(page.getByText('账户已在其他标签页切换，请等待同步后重试', { exact: true })).toBeVisible();
  expect(writes).toHaveLength(0);
  await expect(composer(page)).toHaveValue('A private draft');
  await page.evaluate(() => window.dispatchEvent(new StorageEvent('storage', { key: 'igraph_user_id', newValue: 'account-b', storageArea: localStorage })));
  await expect(page.getByRole('button', { name: '账户：account-b' })).toBeVisible();
  await expect(page.getByRole('button', { name: '发送', exact: true })).toHaveCount(0);
});
