import { test, expect } from '@playwright/test';

/**
 * Security-critical: proves the browser can only reach MarketAI domain data
 * through the same-origin BFF proxy (`pages/api/domain/v1/w/[slug]/
 * [...path].ts`), never directly against FastAPI. `middleware.ts`
 * (`generateCSP`) sets `connect-src` to `'self'` plus the validated blob
 * storage origins — `DOMAIN_API_BASE_URL` (the FastAPI origin) is
 * deliberately never added to that list, so a direct `fetch()` to FastAPI
 * from the page is refused by the browser itself before any request
 * leaves the page, independent of what the server would have answered.
 *
 * `SECURITY_HEADERS_ENABLED` defaults to `true` (see `lib/env.ts`
 * `productionLockedToggle`), so the CSP header is present in a normal dev
 * run without extra configuration.
 */

test('domain requests go through same-origin BFF only', async ({ page }) => {
  const domainRequests: string[] = [];
  page.on('request', (req) => {
    if (req.url().includes('/api/domain/')) {
      domainRequests.push(req.url());
    }
  });

  await page.goto('/w/workspace-a/library');
  await page.waitForTimeout(3000);

  expect(domainRequests.length).toBeGreaterThan(0);
  for (const url of domainRequests) {
    const requestUrl = new URL(url);
    const pageUrl = new URL(page.url());
    expect(requestUrl.origin).toBe(pageUrl.origin);
  }
});

test('CSP blocks direct fetch to FastAPI origin', async ({ page }) => {
  await page.goto('/w/workspace-a/generate');

  const cspViolations: string[] = [];
  page.on('console', (msg) => {
    if (msg.text().includes('connect-src')) {
      cspViolations.push(msg.text());
    }
  });

  const fastapiOrigin =
    process.env.DOMAIN_API_BASE_URL ?? 'http://localhost:8000';
  await page.evaluate(async (origin) => {
    try {
      await fetch(`${origin}/api/v1/health`);
    } catch {
      // Expected: the browser refuses the request before it leaves the
      // page. The CSP violation is what the test actually asserts on
      // (via the console listener above), not this catch.
    }
  }, fastapiOrigin);

  expect(cspViolations.length).toBeGreaterThan(0);
});

test('CSP header contains self and storage origins, not FastAPI', async ({
  page,
}) => {
  const response = await page.goto('/w/workspace-a/generate');
  const csp = response?.headers()['content-security-policy'] ?? '';

  expect(csp).toContain("connect-src 'self'");
  expect(csp).not.toContain(
    process.env.DOMAIN_API_BASE_URL ?? 'localhost:8000'
  );
  // No wildcard scheme/host anywhere in connect-src.
  const connectSrc = csp.match(/connect-src[^;]*/)?.[0] ?? '';
  expect(connectSrc).not.toContain('*');
});
