import { test, expect } from '@playwright/test';
import path from 'path';

/**
 * Owner vs. Member authorization boundary.
 *
 * Nav visibility: `WorkspaceNavigation` (`components/workspace/
 * WorkspaceNavigation.tsx`) only pushes Settings/Members menu items when
 * `useCanAccess('team_member', ['create', 'update', 'delete'])` is true,
 * which only Owners satisfy.
 *
 * Server-side enforcement: `getWorkspacePageProps({ ownerOnly: true })`
 * (`lib/workspace-bootstrap.ts`) returns `errorCode: 403` /
 * `errorContext: 'owner-only'` props for a non-Owner rather than an HTTP
 * 403 status — `WorkspaceLayout` renders `ForbiddenState` ("Access Denied")
 * for those props while the page itself still responds 200. This mirrors
 * the SSR-props pattern used throughout the app rather than a hard HTTP
 * status code, so the assertion below intentionally checks for 200.
 *
 * Assumes the member credential authenticated in `fixtures.ts` is a
 * non-Owner member of `workspace-b`.
 */

test('Owner sees settings and members nav items', async ({ page }) => {
  await page.goto('/w/workspace-a/generate');
  await expect(page.getByText('Settings')).toBeVisible();
  await expect(page.getByText('Members')).toBeVisible();
});

test.describe('Member perspective', () => {
  test.use({
    storageState: path.join(__dirname, '..', '.auth', 'member.json'),
  });

  test('Member does not see settings nav', async ({ page }) => {
    await page.goto('/w/workspace-b/generate');
    await expect(page.getByText('Settings')).not.toBeVisible();
  });

  test('Member direct access to settings returns 403 context', async ({
    page,
  }) => {
    const response = await page.goto('/w/workspace-b/settings');
    // The route responds 200: getWorkspacePageProps encodes the
    // authorization failure as errorCode/errorContext props rather than an
    // HTTP status, and WorkspaceLayout renders ForbiddenState for them.
    expect(response?.status()).toBe(200);
    await expect(page.getByText('Access Denied')).toBeVisible();
  });
});
