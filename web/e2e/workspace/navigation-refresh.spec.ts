import { test, expect } from '@playwright/test';

/**
 * Layout persistence across client-side navigation, hard refresh, and
 * browser history. `WorkspaceLayout` (`components/layouts/
 * WorkspaceLayout.tsx`) keeps the sidebar (`WorkspaceSelector` +
 * `WorkspaceNavigation`) mounted across route changes within a Workspace,
 * and `getWorkspacePageProps` re-derives Workspace/session state from the
 * server on every full load, including a reload.
 */

test('navigating between six routes preserves layout', async ({ page }) => {
  await page.goto('/w/workspace-a/generate');
  const routes = ['assets', 'experiments', 'localization', 'scale', 'library'];
  for (const route of routes) {
    await page.click(`a[href="/w/workspace-a/${route}"]`);
    await expect(page).toHaveURL(new RegExp(`/w/workspace-a/${route}`));
    await expect(page.locator('#workspace-selector')).toBeVisible();
  }
});

test('page refresh restores workspace context', async ({ page }) => {
  await page.goto('/w/workspace-a/assets');
  await page.reload();
  await expect(page.locator('h1')).toContainText('Assets');
  await expect(page.locator('#workspace-selector')).toHaveValue('workspace-a');
});

test('back/forward navigation works', async ({ page }) => {
  await page.goto('/w/workspace-a/generate');
  await page.click('a[href="/w/workspace-a/assets"]');
  await expect(page).toHaveURL(/assets/);
  await page.goBack();
  await expect(page).toHaveURL(/generate/);
  await page.goForward();
  await expect(page).toHaveURL(/assets/);
});
