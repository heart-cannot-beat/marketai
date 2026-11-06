import { test, expect, LIBRARY_URL } from './fixtures';

test.describe('Library cross-workspace security', () => {
  test('domain requests include workspace slug in path', async ({ page }) => {
    const domainRequests: string[] = [];
    page.on('request', (req) => {
      if (req.url().includes('/api/domain/')) {
        domainRequests.push(req.url());
      }
    });

    await page.goto(LIBRARY_URL);
    await page.waitForTimeout(3000);

    for (const url of domainRequests) {
      expect(url).toContain('/w/workspace-a/');
    }
  });
});
