import { test, expect } from '@playwright/test';

/**
 * Cross-workspace asset isolation (Task 12, spec §14 row 15).
 *
 * Deliberately diverges from the Task 12 brief's literal example
 * (`expect(response?.status()).toBe(403)` on the page navigation): this
 * repo's own `getWorkspacePageProps()` (`lib/workspace-bootstrap.ts`)
 * encodes "caller has no membership in this Workspace" as
 * `errorCode: 403` / `errorContext: 'no-membership'` *props*, with the
 * page itself still responding HTTP 200 -- `WorkspaceLayout` renders
 * `ForbiddenState` ("Access Denied") for those props. This is the same
 * pattern `e2e/workspace/owner-member.spec.ts` already asserts on for the
 * `ownerOnly` variant of the same gate (`expect(response?.status()).
 * toBe(200)` + "Access Denied" visible), so this spec follows it rather
 * than the brief's un-verified `403` status code, which no route in this
 * app actually returns for a no-membership Workspace page load. See
 * `task-12-report.md` Brief deviations.
 *
 * Assumes the default `chromium` project's `owner.json` storage state
 * (`e2e/workspace/fixtures.ts`) is OWNER of `workspace-a` only -- not a
 * member of `workspace-b` -- mirroring the assumption
 * `owner-member.spec.ts` already makes about the `member.json` credential
 * on the opposite side of the same fixture.
 */
test.describe('Cross-workspace asset isolation', () => {
  test('Assets page for a foreign Workspace renders Access Denied, not the gallery', async ({
    page,
  }) => {
    const response = await page.goto('/w/workspace-b/assets');

    expect(response?.status()).toBe(200);
    await expect(page.getByText('Access Denied')).toBeVisible();
    await expect(page.locator('[data-testid="asset-card"]')).toHaveCount(0);
  });

  test("asset-version detail 404s for an id outside the caller's Workspace", async ({
    request,
  }) => {
    // Hits the real same-origin BFF proxy (`pages/api/domain/v1/w/[slug]/
    // [...path].ts`) -- not `page.route()`-mocked -- so this needs the dev
    // server + FastAPI + a seeded `workspace-a` running, per
    // `playwright.config.ts`'s `webServer`. RLS scopes
    // `get_generation_asset_version` to the caller's own Workspace
    // (CLAUDE.md invariant: "Production tenancy uses PostgreSQL RLS"), so
    // a version id that exists only in a different Workspace (or not at
    // all) resolves as not-found rather than leaking a 403 that would
    // confirm the id's existence elsewhere.
    const response = await request.get(
      '/api/domain/v1/w/workspace-a/generation/asset-versions/foreign-version-id'
    );
    expect(response.status()).toBe(404);
  });
});
