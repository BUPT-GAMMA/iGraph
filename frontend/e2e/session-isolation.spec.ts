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
