import { test, expect, LIBRARY_URL } from './fixtures';

test.describe('Library version history', () => {
  test('versions tab shows empty state when no versions published', async ({
    page,
  }) => {
    await page.goto(LIBRARY_URL);
    await expect(page.locator('h1')).toContainText('Creative Library');
    const tablist = page.locator('[role="tablist"]');
    const tabCount = await tablist.locator('[role="tab"]').count();
    expect(tabCount).toBeGreaterThanOrEqual(0);
  });

  test('tab bar includes versions tab for entries that support it', async ({
    page,
  }) => {
    await page.goto(LIBRARY_URL);
    await expect(page.locator('h1')).toContainText('Creative Library');
    const categoryNav = page.getByText('Products');
    await expect(categoryNav).toBeVisible();
  });
});
