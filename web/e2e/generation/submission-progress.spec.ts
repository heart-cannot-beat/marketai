import {
  test,
  expect,
  WORKSPACE_SLUG,
  GENERATE_URL,
  DRAFT_ID,
  PREVIEW_ID,
  RUN_ID,
} from './fixtures';

/**
 * Slice 2 E2E coverage (acceptance criteria 2.1-2.16, spec:
 * `.superpowers/sdd/2026-09-03-generation-workspace-ui/task-10-brief.md`):
 * submitting a frozen preview as a GenerationRun (`SubmitConfirmation`, Task
 * 9), the run progress page (`RunProgressView`, Task 9), and the run list on
 * `/generate` (`RunList`, Task 9).
 *
 * Every Generation domain call -- including the Slice 2 additions
 * (`submit_generation_run`, `get_generation_run`,
 * `list_generation_run_items`, `get_operation`, `list_generation_runs`) --
 * is mocked via `page.route()` (`./fixtures.ts::mockGenerationBackend`); see
 * that file's header comment for what is and isn't intercepted, and why the
 * initial page navigation still needs the `chromium` project's seeded owner
 * auth (`e2e/.auth/owner.json`).
 *
 * `RUN_DETAIL`/`OPERATION_DETAIL` (`./fixtures.ts`) are both mocked already
 * terminal (`status`/`state: 'succeeded'`) so `PollingRunProgressSource`
 * settles after its first poll instead of scheduling further timers these
 * specs would otherwise have to wait out.
 */
test.describe('Slice 2: Submission + Progress', () => {
  test('2.2: Submit creates a Run and navigates to the run page', async ({
    page,
  }) => {
    // Pre-condition: a draft with a valid, submittable preview already
    // resolved (`?preview=` mirrors the shallow-routed state
    // `DraftWorkspace`/`handlePreviewCreated` leaves behind at the end of
    // the Slice 1 flow, `draft-preview.spec.ts`'s 1.9).
    await page.goto(`${GENERATE_URL}/drafts/${DRAFT_ID}?preview=${PREVIEW_ID}`);

    const submitButton = page.locator('button:has-text("Submit Run")');
    await expect(submitButton).toBeEnabled();
    await submitButton.click();

    await expect(page).toHaveURL(
      new RegExp(`/w/${WORKSPACE_SLUG}/generate/runs/${RUN_ID}$`)
    );
  });

  test('2.4: Run page shows RunSummary with done/total/failed counters', async ({
    page,
  }) => {
    await page.goto(`${GENERATE_URL}/runs/${RUN_ID}`);

    await expect(page.locator('h1')).toBeVisible();
    await expect(page.getByText('Done', { exact: true })).toBeVisible();
    await expect(page.getByText('Total', { exact: true })).toBeVisible();
    await expect(page.getByText('Failed', { exact: true })).toBeVisible();
  });

  test('2.11: Refresh on run page resumes progress from the server', async ({
    page,
  }) => {
    await page.goto(`${GENERATE_URL}/runs/${RUN_ID}`);
    await expect(page.locator('h1')).toBeVisible();

    // A hard reload re-runs `getServerSideProps` and remounts
    // `RunProgressView` from scratch -- progress resumes purely from the
    // mocked `get_generation_run`/`get_operation`/`list_generation_run_items`
    // responses, with no client-side cache to fall back on.
    await page.reload();
    await expect(page.locator('h1')).toBeVisible();
    await expect(page.getByText('Done', { exact: true })).toBeVisible();
  });

  test('2.14: Run list on /generate shows Runs', async ({ page }) => {
    await page.goto(GENERATE_URL);

    await expect(page.getByRole('heading', { name: 'Runs' })).toBeVisible();
    await expect(
      page.locator(`a[href*="/generate/runs/${RUN_ID}"]`)
    ).toBeVisible();
  });
});
