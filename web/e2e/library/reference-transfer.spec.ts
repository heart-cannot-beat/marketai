import { test, expect, LIBRARY_URL } from './fixtures';

test.describe('Library reference transfer', () => {
  test('library shell renders and includes category navigation', async ({
    page,
  }) => {
    await page.goto(LIBRARY_URL);
    await expect(page.locator('h1')).toContainText('Creative Library');

    const categories = [
      'Products',
      'People',
      'Templates',
      'Prompt Policies',
      'Data Sources',
    ];
    for (const cat of categories) {
      await expect(page.getByText(cat).first()).toBeVisible();
    }
  });

  test('no signed URLs or object keys appear in DOM', async ({ page }) => {
    await page.goto(LIBRARY_URL);
    await expect(page.locator('h1')).toContainText('Creative Library');

    const bodyText = await page.locator('body').innerText();
    expect(bodyText).not.toContain('X-Amz-Signature');
    expect(bodyText).not.toContain('x-amz-credential');
  });
});
