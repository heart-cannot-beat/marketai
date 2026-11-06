import {
  test,
  expect,
  WORKSPACE_SLUG,
  GENERATE_URL,
  DRAFT_ID,
  PREVIEW_ID,
  PARTIAL_RUN_ID,
  PARTIAL_OPERATION_ID,
  PRODUCT_NAME,
} from './fixtures';

/**
 * Cross-slice golden path (Task 12, spec:
 * `.superpowers/sdd/2026-09-03-generation-workspace-ui/task-12-brief.md`):
 * the full Generation Workspace journey through all three slices in one
 * run -- create a draft (Slice 1, Task 7), generate a preview (Slice 1),
 * submit it as a Run (Slice 2, Task 10), observe the Run land in a
 * partially-failed terminal state (Slice 2), then drive Slice 3's recovery
 * flow (Task 12/`recovery.spec.ts`) end to end: request a recovery preview
 * and confirm it.
 *
 * Reuses `./fixtures.ts::mockGenerationBackend` for every step except one:
 * the shared fixture's `submit_generation_run` mock always hands back the
 * already-`succeeded` `RUN_ID`/`RUN_DETAIL` (Slice 2's fixture, deliberately
 * inert so `submission-progress.spec.ts` never has to drive recovery). This
 * test instead overrides that one POST response, before submitting, to
 * return `PARTIAL_RUN_ID` -- landing the submission on the *same*
 * `partially_succeeded` Run fixture `recovery.spec.ts` exercises
 * (`PARTIAL_RUN_DETAIL`/`PARTIAL_OPERATION_DETAIL`/
 * `RECOVERY_PREVIEW_DETAIL`), so "run completes with failures" and the
 * subsequent recovery preview/confirm are real, not simulated.
 */
test('Golden path: Draft to Preview to Submit to Progress to Recovery', async ({
  page,
}) => {
  // Registered before any navigation so it's already the most-recently-added
  // handler for `/generation/runs` by the time "Submit Run" fires its POST --
  // Playwright dispatches to the newest matching `page.route()` first, and
  // `route.fallback()` on the non-POST (list) branch defers to the fixture's
  // own handler for `list_generation_runs` on `/generate`.
  await page.route(
    `**/api/domain/v1/w/${WORKSPACE_SLUG}/generation/runs`,
    async (route) => {
      if (route.request().method() === 'POST') {
        await route.fulfill({
          status: 202,
          contentType: 'application/json',
          body: JSON.stringify({
            operation_id: PARTIAL_OPERATION_ID,
            generation_run_id: PARTIAL_RUN_ID,
          }),
        });
      } else {
        await route.fallback();
      }
    }
  );

  // 1. Create Draft (`NewDraftDialog`) -- the product is picked from its
  // card by name (`list_products`, #162), never typed.
  await page.goto(GENERATE_URL);
  await page.click('button:has-text("New Draft")');

  const dialog = page.locator('dialog[aria-label="Create New Draft"]');
  await expect(dialog).toBeVisible();
  await dialog.getByRole('radio', { name: PRODUCT_NAME }).check();
  await dialog.locator('#new-draft-provider').selectOption({ index: 1 });
  await dialog.locator('#new-draft-model').selectOption({ index: 1 });
  await dialog.locator('#new-draft-mode').selectOption({ index: 1 });
  await dialog.locator('button:has-text("Create Draft")').click();

  await expect(page).toHaveURL(new RegExp(`/generate/drafts/${DRAFT_ID}$`));
  await expect(dialog).not.toBeVisible();

  // 2. Generate Preview (`PreviewPanel`, Task 5/6) -- the page shell
  // shallow-routes the returned preview id into `?preview=`.
  await page.click('button:has-text("Generate Preview")');
  await expect(page).toHaveURL(new RegExp(`[?&]preview=${PREVIEW_ID}`));
  await expect(
    page.getByRole('heading', { name: 'Resolution Preview' })
  ).toBeVisible();

  // 3. Submit Run (`SubmitConfirmation`, Task 9) -- lands on `PARTIAL_RUN_ID`
  // via the route override above, not the shared fixture's `RUN_ID`.
  const submitButton = page.locator('button:has-text("Submit Run")');
  await expect(submitButton).toBeEnabled();
  await submitButton.click();

  await expect(page).toHaveURL(
    new RegExp(`/w/${WORKSPACE_SLUG}/generate/runs/${PARTIAL_RUN_ID}$`)
  );

  // 4. Observe Progress (`RunProgressView`, Task 9) -- `PARTIAL_RUN_DETAIL`
  // and `PARTIAL_OPERATION_DETAIL` are both already terminal, so the poller
  // settles on its first fetch instead of this test waiting out a timer.
  await expect(page.locator('h1')).toBeVisible();
  await expect(page.getByText('Done', { exact: true })).toBeVisible();
  await expect(page.getByText('Total', { exact: true })).toBeVisible();
  await expect(
    page.locator('.stat-title', { hasText: /^Failed$/ })
  ).toBeVisible();

  // 5. Run completed with failures -> Recovery becomes available
  // (`RecoveryPanel`, Task 11) once `isRunTerminal`.
  await expect(page.getByText('Recovery Actions')).toBeVisible();
  await expect(page.locator('button:has-text("Retry Failed")')).toBeEnabled();

  // 6. Recovery preview -- `create_generation_run_recovery_preview` returns
  // `RECOVERY_PREVIEW_DETAIL` (two failed-item targets, $0.50 additional
  // cost).
  await page.click('button:has-text("Retry Failed")');
  await expect(
    page.getByRole('heading', { name: 'Recovery Preview' })
  ).toBeVisible();
  await expect(page.getByText('2 item(s)')).toBeVisible();
  await expect(page.getByText('$0.50')).toBeVisible();

  // 7. Recovery run -- confirming executes `retry_generation_run_failed_
  // items` and the panel returns to observation, completing the journey.
  await page.click('button:has-text("Confirm")');
  await expect(
    page.getByRole('heading', { name: 'Recovery Preview' })
  ).not.toBeVisible();
  await expect(page.getByText('Recovery Actions')).toBeVisible();
});
