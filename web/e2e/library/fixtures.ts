import { test as base, expect, type Page } from '@playwright/test';

export const WORKSPACE_SLUG = 'workspace-a';
export const LIBRARY_URL = `/w/${WORKSPACE_SLUG}/library`;

export interface LibraryFixtures {
  libraryPage: Page;
}

export const test = base.extend<LibraryFixtures>({
  libraryPage: async ({ page }, provide) => {
    await page.goto(LIBRARY_URL);
    await page.waitForLoadState('networkidle');
    await provide(page);
  },
});

export { expect };
