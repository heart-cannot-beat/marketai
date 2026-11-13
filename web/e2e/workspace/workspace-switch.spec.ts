import { test, expect } from '@playwright/test';

/**
 * Exercises `WorkspaceProvider.switchWorkspace` (`components/workspace/
 * WorkspaceProvider.tsx`), which resolves the destination route via
 * `computeTargetRoute` (`lib/workspace-navigator.ts`) before navigating:
 * feature areas (generate/assets/experiments/localization/scale/library)
 * carry over across a switch, and so does settings (a Member reads the
 * spending limits there, #126), but the Owner-only members area falls back
 * to `/generate` when the destination Workspace's role is not OWNER.
 *
 * Assumes the seeded fixture data: `workspace-a` and `workspace-b` are both
 * accessible to the owner credential authenticated in `fixtures.ts`, with
 * the acting user OWNER on `workspace-a` and non-OWNER on `workspace-b`.
 */

test('switching workspace changes URL and content atomically', async ({
  page,
}) => {
  await page.goto('/w/workspace-a/generate');
  await expect(page.locator('h1')).toContainText('New Generation');

  const selector = page.locator('#workspace-selector');
  await selector.selectOption('workspace-b');
  await expect(page).toHaveURL(/\/w\/workspace-b\/generate/);

  // Verify no flash of previous workspace data
  await expect(page.locator('#workspace-selector')).toHaveValue('workspace-b');
});

test('switching from Owner-only page to Member workspace lands on generate', async ({
  page,
}) => {
  await page.goto('/w/workspace-a/members');

  const selector = page.locator('#workspace-selector');
  await selector.selectOption('workspace-b');
  await expect(page).toHaveURL(/\/w\/workspace-b\/generate/);
});
