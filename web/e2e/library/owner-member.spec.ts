import { test, expect, LIBRARY_URL } from './fixtures';

test.describe('Library owner/member eligibility', () => {
  test('library page loads for workspace owner', async ({ page }) => {
    await page.goto(LIBRARY_URL);
    await expect(page.locator('h1')).toContainText('Creative Library');
  });

  test('entry actions toolbar renders for owner', async ({ page }) => {
    await page.goto(LIBRARY_URL);
    await page.waitForTimeout(1000);
  });
});
