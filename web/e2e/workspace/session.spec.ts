import { test, expect } from '@playwright/test';

/**
 * Session boundary: `middleware.ts` redirects any request without a valid
 * NextAuth session to `/auth/login` (see the `session?.user` check), and
 * `getWorkspacePageProps` (`lib/workspace-bootstrap.ts`) redirects again at
 * the page level if the session cookie is missing or stale by the time
 * `getServerSideProps` runs.
 */

test('unauthenticated access redirects to login', async ({ browser }) => {
  const context = await browser.newContext({ storageState: undefined });
  const page = await context.newPage();
  await page.goto('/w/test-workspace/generate');
  await expect(page).toHaveURL(/\/auth\/login/);
  await context.close();
});

test('authenticated user reaches workspace page', async ({ page }) => {
  await page.goto('/w/test-workspace/generate');
  await expect(page.locator('h1')).toContainText('New Generation');
});

test('sign-in reaches a protected workspace through the dashboard', async ({
  browser,
}) => {
  const email = process.env.E2E_OWNER_EMAIL;
  const password = process.env.E2E_OWNER_PASSWORD;
  test.skip(!email || !password, 'requires the seeded E2E owner account');

  const context = await browser.newContext({ storageState: undefined });
  try {
    const page = await context.newPage();
    const baseURL = process.env.E2E_BASE_URL ?? 'http://localhost:4002';
    const postSubmitNavigations: string[] = [];
    let submitted = false;
    page.on('request', (request) => {
      if (submitted && request.isNavigationRequest()) {
        postSubmitNavigations.push(new URL(request.url()).pathname);
      }
    });

    await page.goto(
      new URL('/auth/login?callbackUrl=%2Fdashboard', baseURL).toString()
    );
    await page.fill('input[name="email"]', email!);
    await page.fill('input[name="password"]', password!);
    submitted = true;
    await page.click('button[type="submit"]');

    // The login page must not regard the session as authenticated while the
    // protected route's server-side gate rejects the same session and sends
    // the browser back to /auth/login.
    await expect(page).toHaveURL(/\/w\/[^/]+\/generate/, { timeout: 20_000 });
    await page.waitForTimeout(1_000);
    expect(postSubmitNavigations).not.toContain('/auth/login');
    await expect(page).toHaveURL(/\/w\/[^/]+\/generate/);
  } finally {
    await context.close();
  }
});
