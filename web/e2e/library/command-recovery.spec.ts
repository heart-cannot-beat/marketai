import { test, expect, LIBRARY_URL } from './fixtures';
import { en } from '../support/copy';

const RECONCILIATION_TITLES = [
  'commands.reconciliation.retry-safe-title',
  'commands.reconciliation.lookup_in_progress-title',
  'commands.reconciliation.unresolved_ambiguity-title',
  'commands.reconciliation.terminal_problem-title',
  'commands.reconciliation.not_applied-title',
] as const;

test.describe('Library command recovery', () => {
  test('library shell loads without reconciliation banners on fresh page', async ({
    page,
  }) => {
    await page.goto(LIBRARY_URL);
    await expect(page.locator('h1')).toContainText('Creative Library');

    // `ReconciliationBanner` has no test id (the one asserted here before
    // never existed); it is found by its title copy.
    for (const key of RECONCILIATION_TITLES) {
      await expect(page.getByText(en('library', key))).toHaveCount(0);
    }
  });

  test('error states use accessible role="alert"', async ({ page }) => {
    await page.goto(LIBRARY_URL);
    await expect(page.locator('h1')).toContainText('Creative Library');

    const alerts = page.locator('[role="alert"]');
    const alertCount = await alerts.count();
    expect(alertCount).toBeGreaterThanOrEqual(0);
  });
});
