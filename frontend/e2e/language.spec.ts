import { expect, test } from '@playwright/test';

test('defaults to English, switches to Chinese, and persists the preference', async ({ page }) => {
  await page.goto('/');

  await expect(page.getByRole('heading', { name: 'Explore graph data, starting with a question' })).toBeVisible();
  await expect(page.locator('html')).toHaveAttribute('lang', 'en');
  await expect(page).toHaveTitle('iGraph · Graph Intelligence Workspace');
  await expect(page.getByRole('button', { name: 'EN', exact: true })).toHaveAttribute('aria-pressed', 'true');

  await page.getByRole('button', { name: '中文' }).click();

  await expect(page.getByRole('heading', { name: '探索图数据，从一个问题开始' })).toBeVisible();
  await expect(page.locator('html')).toHaveAttribute('lang', 'zh-CN');
  await expect(page).toHaveTitle('iGraph · 图分析工作台');
  await page.getByRole('button', { name: '打开登录' }).click();
  await expect(page.getByRole('dialog', { name: '账户' })).toBeVisible();
  await expect(page.getByLabel('用户 ID')).toBeVisible();

  await page.reload();

  await expect(page.getByRole('heading', { name: '探索图数据，从一个问题开始' })).toBeVisible();
  await expect(page.getByRole('button', { name: '中文' })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('html')).toHaveAttribute('lang', 'zh-CN');
});
