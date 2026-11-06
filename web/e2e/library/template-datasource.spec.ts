import { test, expect, LIBRARY_URL } from './fixtures';

test.describe('Library template and data source operations', () => {
  test('library shell loads with category navigation for template access', async ({
    page,
  }) => {
    await page.goto(LIBRARY_URL);
    await expect(page.locator('h1')).toContainText('Creative Library');

    await expect(page.getByText('Templates')).toBeVisible();
    await page.getByText('Templates').click();
    await page.waitForTimeout(500);
    expect(page.url()).toContain('cat=template');
  });

  test('data source category is navigable and renders', async ({ page }) => {
    await page.goto(LIBRARY_URL);
    await expect(page.locator('h1')).toContainText('Creative Library');

    await expect(page.getByText('Data Sources')).toBeVisible();
    await page.getByText('Data Sources').click();
    await page.waitForTimeout(500);
    expect(page.url()).toContain('cat=datasource');
  });
});
