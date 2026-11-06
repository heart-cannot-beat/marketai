import { test, expect } from '@playwright/test';

/**
 * Keyboard accessibility across the workspace shell. `WorkspaceLayout`
 * (`components/layouts/WorkspaceLayout.tsx`) renders a "Skip to content"
 * link targeting `#main-content`, and its `useEffect` on `router.asPath`
 * focuses the page's `<h1>` after every route change so screen reader
 * users land on the new heading instead of the previous scroll position.
 */

test('skip-to-content link works', async ({ page }) => {
  await page.goto('/w/workspace-a/generate');
  await page.keyboard.press('Tab');
  const skipLink = page.locator('a:has-text("Skip to content")');
  await expect(skipLink).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page.locator('#main-content')).toBeFocused();
});

test('workspace selector is keyboard operable', async ({ page }) => {
  await page.goto('/w/workspace-a/generate');
  const selector = page.locator('#workspace-selector');
  await selector.focus();
  await expect(selector).toBeFocused();
});

test('route change moves focus to h1', async ({ page }) => {
  await page.goto('/w/workspace-a/generate');
  await page.click('a[href="/w/workspace-a/assets"]');
  await expect(page).toHaveURL(/assets/);
  const h1 = page.locator('h1');
  await expect(h1).toBeFocused();
});

test('navigation items reachable via tab', async ({ page }) => {
  await page.goto('/w/workspace-a/generate');
  const navItems = page.locator('nav a');
  const count = await navItems.count();
  expect(count).toBeGreaterThanOrEqual(6);
});
