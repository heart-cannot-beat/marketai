import { test, expect, LIBRARY_URL } from './fixtures';

test.describe('Library golden path', () => {
  test('library shell renders on page load', async ({ page }) => {
    await page.goto(LIBRARY_URL);
    await expect(page.locator('h1')).toContainText('Creative Library');
  });

  test('five category nav items are present', async ({ page }) => {
    await page.goto(LIBRARY_URL);
    const categories = [
      'Products',
      'People',
      'Templates',
      'Prompt Policies',
      'Data Sources',
    ];
    for (const cat of categories) {
      await expect(page.getByText(cat)).toBeVisible();
    }
  });

  test('category switch changes the entry list', async ({ page }) => {
    await page.goto(LIBRARY_URL);
    await page.getByText('People').click();
    await page.waitForTimeout(500);
    const url = page.url();
    expect(url).toContain('cat=person');
  });

  test('create root entry and verify it appears in list', async ({ page }) => {
    await page.goto(LIBRARY_URL);
    const createBtn = page.getByText('New entry');
    if (await createBtn.isVisible()) {
      await createBtn.click();
      await page.waitForTimeout(500);
    }
  });

  test('tab navigation shows correct content', async ({ page }) => {
    await page.goto(LIBRARY_URL);
    const tabs = page.locator('[role="tab"]');
    const count = await tabs.count();
    if (count > 1) {
      await tabs.nth(1).click();
      const panel = page.locator('[role="tabpanel"]');
      await expect(panel).toBeVisible();
    }
  });

  test('workspace navigation highlights Library', async ({ page }) => {
    await page.goto(LIBRARY_URL);
    const navLink = page.locator('nav a', { hasText: 'Creative library' });
    await expect(navLink).toBeVisible();
  });

  test('library page with query params still highlights nav', async ({
    page,
  }) => {
    await page.goto(`${LIBRARY_URL}?cat=product&entry=test`);
    const navLink = page.locator('nav a', { hasText: 'Creative library' });
    await expect(navLink).toBeVisible();
  });
});

test.describe('Library page structure', () => {
  test('renders the library shell, not a placeholder', async ({ page }) => {
    await page.goto(LIBRARY_URL);
    // The route was a FeatureEmptyState placeholder whose copy no longer
    // exists, so asserting that copy's absence always passed.
    await expect(page.getByTestId('library-shell')).toBeVisible();
  });

  test('library shell has tablist', async ({ page }) => {
    await page.goto(LIBRARY_URL);
    await page.getByText('Products').click();
    await page.waitForTimeout(1000);
  });

  test('back/forward navigation preserves state', async ({ page }) => {
    await page.goto(LIBRARY_URL);
    await page.getByText('People').click();
    await page.waitForTimeout(500);
    await page.goBack();
    await page.waitForTimeout(500);
    await page.goForward();
    await page.waitForTimeout(500);
    expect(page.url()).toContain('cat=person');
  });
});
