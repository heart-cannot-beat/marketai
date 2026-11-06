import {
  test,
  expect,
  WORKSPACE_SLUG,
  GENERATE_URL,
  DRAFT_ID,
  PREVIEW_ID,
  NONEXISTENT_DRAFT_ID,
  PRODUCT_NAME,
} from './fixtures';

/**
 * Slice 1 E2E coverage (acceptance criteria 1.1–1.16, spec:
 * `.superpowers/sdd/2026-09-03-generation-workspace-ui/task-7-brief.md`):
 * the list page → New Draft dialog → draft detail → preview journey.
 *
 * Every Generation domain call is mocked via `page.route()`
 * (`./fixtures.ts::mockGenerationBackend`, wired in as the `test` fixture's
 * `page` override) — see that file's header comment for exactly what is and
 * isn't intercepted, and why the initial page navigation still needs the
 * `chromium` project's seeded owner auth (`e2e/.auth/owner.json`).
 */
test.describe('Slice 1: Draft + Preview', () => {
  test('1.1: New Draft opens setup dialog, creates draft, navigates', async ({
    page,
  }) => {
    await page.goto(GENERATE_URL);
    await page.click('button:has-text("New Draft")');

    const dialog = page.locator('dialog[aria-label="Create New Draft"]');
    await expect(dialog).toBeVisible();

    // The product is picked from its card by name (#162), never typed.
    await dialog.getByRole('radio', { name: PRODUCT_NAME }).check();
    await dialog.locator('#new-draft-provider').selectOption({ index: 1 });
    await dialog.locator('#new-draft-model').selectOption({ index: 1 });
    await dialog.locator('#new-draft-mode').selectOption({ index: 1 });

    await dialog.locator('button:has-text("Create Draft")').click();

    await expect(page).toHaveURL(/\/generate\/drafts\//);
    await expect(page).toHaveURL(new RegExp(`/generate/drafts/${DRAFT_ID}$`));
    await expect(dialog).not.toBeVisible();
  });

  test('1.9: Refresh on draft page with preview restores from server', async ({
    page,
  }) => {
    await page.goto(GENERATE_URL);
    await page.click('a[href*="/generate/drafts/"]');
    await expect(page).toHaveURL(/\/generate\/drafts\//);
    await expect(page.locator('h1')).toBeVisible();

    // Request a preview (mocked `create_generation_preview`) — the page
    // shell (`pages/w/[slug]/generate/drafts/[draftId].tsx`) shallow-routes
    // the returned id into `?preview=`, which mounts `PreviewPanel`.
    await page.click('button:has-text("Generate Preview")');
    await expect(page).toHaveURL(new RegExp(`[?&]preview=${PREVIEW_ID}`));
    await expect(
      page.getByRole('heading', { name: 'Resolution Preview' })
    ).toBeVisible();

    // A hard reload re-runs `getServerSideProps` and remounts the page from
    // scratch — the `?preview=` id survives the navigation and
    // `PreviewPanel` re-fetches (mocked `get_generation_preview`) rather
    // than relying on any client-side cache.
    await page.reload();
    await expect(page.locator('h1')).toBeVisible();
    await expect(
      page.getByRole('heading', { name: 'Resolution Preview' })
    ).toBeVisible();
  });

  test('1.10: Refresh on generate list restores from server', async ({
    page,
  }) => {
    await page.goto(GENERATE_URL);
    await expect(page.locator('h1')).toContainText(/generation/i);
    await page.reload();
    await expect(page.locator('h1')).toContainText(/generation/i);
  });

  test('1.12: Back/forward between list and draft', async ({ page }) => {
    await page.goto(GENERATE_URL);
    await page.click('a[href*="/generate/drafts/"]');
    await expect(page).toHaveURL(/\/generate\/drafts\//);

    await page.goBack();
    await expect(page).toHaveURL(new RegExp(`/w/${WORKSPACE_SLUG}/generate$`));

    await page.goForward();
    await expect(page).toHaveURL(/\/generate\/drafts\//);
  });

  test('1.13: Nav item highlighted on all /generate/* routes', async ({
    page,
  }) => {
    // The production nav (`components/workspace/WorkspaceNavigation.tsx` →
    // `NavigationItems.tsx`) marks the active item with a CSS class
    // (`text-white bg-gray-800 font-semibold`), not a `data-active`
    // attribute — that attribute exists only in a Jest mock
    // (`__tests__/workspace-navigation-prefix.spec.tsx`), not in the
    // rendered DOM this E2E test drives. `bg-gray-800` alone isn't a safe
    // discriminator: the item's *inactive* classes already include
    // `dark:hover:bg-gray-800`, which contains that substring — `text-white`
    // only appears in the active branch, so it's the one that actually
    // distinguishes active from inactive here.
    await page.goto(GENERATE_URL);
    // 新建生成 lives in the 高级工作台 group, which opens by itself while
    // one of its areas is active (#165).
    const navLink = page.locator('nav a', { hasText: 'New generation' });
    await expect(navLink).toHaveClass(/\btext-white\b/);

    await page.click('a[href*="/generate/drafts/"]');
    await expect(page).toHaveURL(/\/generate\/drafts\//);
    await expect(navLink).toHaveClass(/\btext-white\b/);
  });

  test('1.11: Deep link with foreign resource ID surfaces an error state', async ({
    page,
  }) => {
    // `DraftWorkspace` (Task 5, `components/generation/draft/
    // DraftWorkspace.tsx`) renders `generation-draft-not-found` ("Draft not
    // found.") only when `!draft && !problem` — but `useDomainQuery` always
    // populates `problem` on any non-2xx response (`hooks/domain/
    // useDomainQuery.ts`), including the 404 this mock returns, so the
    // reachable branch is actually `generation-draft-load-error`
    // ("Couldn't load draft"). This is a real gap against Task 5's own
    // brief, which specified an unconditional "Draft not found." on
    // `!draft` — flagged in the Task 7 report rather than silently
    // asserting the literal (currently unreachable) "not found" copy.
    await page.goto(`${GENERATE_URL}/drafts/${NONEXISTENT_DRAFT_ID}`);
    await expect(
      page.getByText(/not found|couldn't load|could not load/i)
    ).toBeVisible();
  });
});
