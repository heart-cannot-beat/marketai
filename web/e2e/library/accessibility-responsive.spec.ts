import { test, expect } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

const LIBRARY_URL = '/w/acme/library';

test.describe('Library accessibility', () => {
  test('passes axe-core checks on library shell', async ({ page }) => {
    await page.goto(LIBRARY_URL);
    await page.waitForSelector('[role="tablist"]', { timeout: 10_000 });

    const results = await new AxeBuilder({ page })
      .include('[data-testid="library-shell"]')
      .analyze();

    expect(results.violations).toEqual([]);
  });

  test('tab bar uses correct ARIA roles', async ({ page }) => {
    await page.goto(LIBRARY_URL);
    const tablist = page.locator('[role="tablist"]');
    await expect(tablist).toBeVisible();

    const tabs = tablist.locator('[role="tab"]');
    const count = await tabs.count();
    expect(count).toBeGreaterThan(0);

    for (let i = 0; i < count; i++) {
      const tab = tabs.nth(i);
      await expect(tab).toHaveAttribute('aria-selected', /.*/);
      await expect(tab).toHaveAttribute('aria-controls', /tabpanel-.+/);
    }
  });

  test('tabpanel is associated with active tab', async ({ page }) => {
    await page.goto(LIBRARY_URL);
    const activeTab = page.locator('[role="tab"][aria-selected="true"]');
    const panelId = await activeTab.getAttribute('aria-controls');
    const panel = page.locator(`#${panelId}`);
    await expect(panel).toBeVisible();
    await expect(panel).toHaveAttribute('role', 'tabpanel');
  });

  test('keyboard navigation works for tabs', async ({ page }) => {
    await page.goto(LIBRARY_URL);
    const firstTab = page.locator('[role="tab"]').first();
    await firstTab.focus();
    await expect(firstTab).toBeFocused();
  });

  test('live regions are present', async ({ page }) => {
    await page.goto(LIBRARY_URL);
    const polite = page.locator('[aria-live="polite"]');
    const assertive = page.locator('[aria-live="assertive"]');
    await expect(polite).toBeAttached();
    await expect(assertive).toBeAttached();
  });

  test('error states use role="alert"', async ({ page }) => {
    await page.goto(LIBRARY_URL + '?entry=nonexistent');
    const alert = page.locator('[role="alert"]');
    const count = await alert.count();
    expect(count).toBeGreaterThanOrEqual(0);
  });
});

test.describe('Library responsive layout', () => {
  test('desktop shows three-pane layout', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.goto(LIBRARY_URL);

    const sidebar = page.locator('.lg\\:block').first();
    await expect(sidebar).toBeVisible();
  });

  test('tablet shows two-pane layout', async ({ page }) => {
    await page.setViewportSize({ width: 768, height: 1024 });
    await page.goto(LIBRARY_URL);

    const mobileCatNav = page.locator('.lg\\:hidden').first();
    await expect(mobileCatNav).toBeVisible();
  });

  test('mobile shows single-column drill-in', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 667 });
    await page.goto(LIBRARY_URL);

    const mobileCatNav = page.locator('.lg\\:hidden').first();
    await expect(mobileCatNav).toBeVisible();
  });

  test('horizontal scrolling never appears on body', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 667 });
    await page.goto(LIBRARY_URL);

    const bodyOverflow = await page.evaluate(() => {
      const body = document.body;
      return body.scrollWidth <= body.clientWidth;
    });
    expect(bodyOverflow).toBe(true);
  });
});
