import { test as setup } from '@playwright/test';
import path from 'path';

/**
 * Auth setup for the `chromium` Playwright project (see
 * `playwright.config.ts`). Runs once per suite invocation and persists
 * browser storage state so individual spec files do not each pay the cost
 * of a fresh login.
 *
 * Requires a seeded environment with two credentials:
 *   - E2E_OWNER_EMAIL / E2E_OWNER_PASSWORD — a user who is OWNER of at
 *     least one Workspace (e.g. "workspace-a" in the specs below).
 *   - E2E_MEMBER_EMAIL / E2E_MEMBER_PASSWORD — a user who is MEMBER (not
 *     OWNER) of at least one Workspace (e.g. "workspace-b").
 *
 * `pages/auth/login.tsx` submits via `signIn('credentials', { redirect:
 * false, callbackUrl: '/dashboard' })`; on success the page's own
 * `useSession` effect pushes to `/dashboard`, whose `getServerSideProps`
 * (`pages/dashboard.tsx`) redirects to `/w/{slug}/generate` for the
 * caller's first Workspace. Waiting on `/\/w\//` covers that redirect
 * chain without depending on which Workspace comes first.
 */
const ownerFile = path.join(__dirname, '..', '.auth', 'owner.json');
const memberFile = path.join(__dirname, '..', '.auth', 'member.json');

setup('authenticate as owner', async ({ page }) => {
  await page.goto('/auth/login');
  await page.fill('input[name="email"]', process.env.E2E_OWNER_EMAIL!);
  await page.fill('input[name="password"]', process.env.E2E_OWNER_PASSWORD!);
  await page.click('button[type="submit"]');
  await page.waitForURL(/\/w\//);
  await page.context().storageState({ path: ownerFile });
});

setup('authenticate as member', async ({ page }) => {
  await page.goto('/auth/login');
  await page.fill('input[name="email"]', process.env.E2E_MEMBER_EMAIL!);
  await page.fill('input[name="password"]', process.env.E2E_MEMBER_PASSWORD!);
  await page.click('button[type="submit"]');
  await page.waitForURL(/\/w\//);
  await page.context().storageState({ path: memberFile });
});
