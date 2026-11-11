import { test, expect } from '@playwright/test';
import path from 'path';

/**
 * Owner vs. Member authorization boundary.
 *
 * Nav visibility: `WorkspaceNavigation` (`components/workspace/
 * WorkspaceNavigation.tsx`) shows Settings to every member -- a Member reads
 * the spending limits there and nothing else (#126) -- and pushes the
 * Members item only when `useCanAccess('team_member', ['create', 'update',
 * 'delete'])` is true, which only Owners satisfy.
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

  test('Member sees settings nav but not members', async ({ page }) => {
    await page.goto('/w/workspace-b/generate');
    await expect(page.getByText('Settings')).toBeVisible();
    await expect(page.getByText('Members')).not.toBeVisible();
  });

  test('Member reads the spending limits in settings and nothing else', async ({
    page,
  }) => {
    const response = await page.goto('/w/workspace-b/settings');
    expect(response?.status()).toBe(200);
    await expect(page.getByTestId('spend-guardrail-section')).toBeVisible();
    await expect(page.getByTestId('spend-guardrail-edit')).toHaveCount(0);
    await expect(page.getByTestId('provider-credentials-section')).toHaveCount(
      0
    );
  });

  test('Member direct access to members returns 403 context', async ({
    page,
  }) => {
    const response = await page.goto('/w/workspace-b/members');
    // The route responds 200: getWorkspacePageProps encodes the
    // authorization failure as errorCode/errorContext props rather than an
    // HTTP status, and WorkspaceLayout renders ForbiddenState for them.
    expect(response?.status()).toBe(200);
    await expect(page.getByText('Access Denied')).toBeVisible();
  });
});
