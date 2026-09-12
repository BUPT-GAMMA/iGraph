import { expect, test } from '@playwright/test';

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
