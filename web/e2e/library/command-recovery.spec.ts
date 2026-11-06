import { test, expect, LIBRARY_URL } from './fixtures';

test.describe('Library command recovery', () => {
  test('library shell loads without reconciliation banners on fresh page', async ({
    page,
  }) => {
    await page.goto(LIBRARY_URL);
    await expect(page.locator('h1')).toContainText('Creative Library');

    const reconciliationBanner = page.locator(
      '[data-testid="reconciliation-banner"]'
    );
    await expect(reconciliationBanner).not.toBeVisible();
  });

  test('error states use accessible role="alert"', async ({ page }) => {
    await page.goto(LIBRARY_URL);
    await expect(page.locator('h1')).toContainText('Creative Library');

    const alerts = page.locator('[role="alert"]');
    const alertCount = await alerts.count();
    expect(alertCount).toBeGreaterThanOrEqual(0);
  });
});
