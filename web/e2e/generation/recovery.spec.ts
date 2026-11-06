import {
  test,
  expect,
  GENERATE_URL,
  WORKSPACE_SLUG,
  RUN_ID,
  PARTIAL_RUN_ID,
  RUNNING_RUN_ID,
  EXPIRED_RECOVERY_PREVIEW_DETAIL,
} from './fixtures';

/**
 * Slice 3 E2E coverage (acceptance criteria 3.1-3.13, spec:
 * `.superpowers/sdd/2026-09-03-generation-workspace-ui/task-12-brief.md`):
 * the run page's recovery slot (`RecoveryPanel`, `RecoveryPreviewDisplay`,
 * `ReconciliationGuidance`, `CancelConfirmation` -- Task 11).
 *
 * Note: the Task 12 brief's own numbered acceptance-criteria list (3.1
 * through 3.13) isn't checked into this `.superpowers/sdd/` directory --
 * only two example tests (3.2, 3.10) ship in the brief itself. The numbers
 * below are this task's best-effort assignment across `RecoveryPanel`'s
 * actual observable behavior (mirrors how `draft-preview.spec.ts` and
 * `submission-progress.spec.ts` cover a representative subset of their own
 * slice's numbered criteria rather than every single one), reusing the
 * brief's two given tests (3.2, 3.10) verbatim where their assertions still
 * match the real DOM.
 *
 * Every Generation domain call, including the Slice 3 additions
 * (`create_generation_run_recovery_preview`,
 * `retry_generation_run_failed_items`, `resume_generation_run_paused_items`,
 * `cancel_generation_run`, `reconcile_generation_run`), is mocked via
 * `page.route()` (`./fixtures.ts::mockGenerationBackend`); see that file's
 * header comment for what is and isn't intercepted, and why the initial
 * page navigation still needs the `chromium` project's seeded owner auth.
 *
 * `PARTIAL_RUN_DETAIL` (`./fixtures.ts`) models a Run that finished
 * `partially_succeeded` with one `failed`-eligible item and one left
 * `uncertain` (`total - done - failed === 1`) -- the shape that makes every
 * one of `RecoveryPanel`'s four actions (`retry_failed` allowed,
 * `resume_paused` present-but-blocked, `cancel`, `reconcile`) exercisable
 * from a single fixture.
 */
test.describe('Slice 3: Recovery', () => {
  const PARTIAL_RUN_URL = `${GENERATE_URL}/runs/${PARTIAL_RUN_ID}`;

  test('3.1: Recovery panel stays hidden before the run reaches a terminal state', async ({
    page,
  }) => {
    // `RUNNING_RUN_DETAIL.status` ('running') isn't in `RUN_TERMINAL_STATES`
    // (`RunProgressSource.ts`), so `RunProgressView` never mounts its
    // `recoverySlot` for it.
    await page.goto(`${GENERATE_URL}/runs/${RUNNING_RUN_ID}`);
    await expect(page.locator('h1')).toBeVisible();
    await expect(page.getByText('Done', { exact: true })).toBeVisible();
    await expect(page.getByText('Recovery Actions')).not.toBeVisible();
  });

  test('3.2: Recovery actions appear from allowed_actions', async ({
    page,
  }) => {
    await page.goto(PARTIAL_RUN_URL);
    await expect(page.getByText('Recovery Actions')).toBeVisible();
    await expect(page.locator('button:has-text("Retry Failed")')).toBeVisible();
    await expect(page.locator('button:has-text("Cancel Run")')).toBeVisible();
  });

  test('3.3: A present-but-disallowed action renders disabled with its reason code', async ({
    page,
  }) => {
    // `PARTIAL_RUN_DETAIL.allowed_actions` carries `resume_paused` with
    // `allowed: false` -- present (so the button renders, per "ActionEligibility
    // drives UI eligibility") but disabled, with `reason_code` surfaced.
    await page.goto(PARTIAL_RUN_URL);
    const resumeButton = page.locator('button:has-text("Resume Paused")');
    await expect(resumeButton).toBeVisible();
    await expect(resumeButton).toBeDisabled();
    await expect(page.getByText('Unavailable: no_paused_items')).toBeVisible();
  });

  test('3.4: Retry Failed opens a recovery preview with targets and cost', async ({
    page,
  }) => {
    await page.goto(PARTIAL_RUN_URL);
    await page.click('button:has-text("Retry Failed")');

    await expect(
      page.getByRole('heading', { name: 'Recovery Preview' })
    ).toBeVisible();
    await expect(page.getByText('2 item(s)')).toBeVisible();
    await expect(page.getByText('$0.50')).toBeVisible();
  });

  test('3.5: Confirming the recovery preview executes retry and returns to the action list', async ({
    page,
  }) => {
    await page.goto(PARTIAL_RUN_URL);
    await page.click('button:has-text("Retry Failed")');
    await expect(
      page.getByRole('heading', { name: 'Recovery Preview' })
    ).toBeVisible();

    // `retry_generation_run_failed_items` falls through to the generic
    // `generation/**` 200-`{}` handler (`./fixtures.ts`) -- `RecoveryPanel`
    // only inspects `outcome.kind`, so an empty success body is enough to
    // drive it back to `{ phase: 'idle' }` and call `onRecoveryComplete`.
    await page.click('button:has-text("Confirm")');

    await expect(
      page.getByRole('heading', { name: 'Recovery Preview' })
    ).not.toBeVisible();
    await expect(page.getByText('Recovery Actions')).toBeVisible();
  });

  test('3.6: Discarding the recovery preview returns without executing a command', async ({
    page,
  }) => {
    await page.goto(PARTIAL_RUN_URL);
    await page.click('button:has-text("Retry Failed")');
    await expect(
      page.getByRole('heading', { name: 'Recovery Preview' })
    ).toBeVisible();

    await page.click('button:has-text("Discard")');

    await expect(
      page.getByRole('heading', { name: 'Recovery Preview' })
    ).not.toBeVisible();
    await expect(page.locator('button:has-text("Retry Failed")')).toBeVisible();
  });

  test('3.7: An expired recovery preview blocks confirmation', async ({
    page,
  }) => {
    // Overrides the shared `recovery-previews` mock
    // (`./fixtures.ts::mockGenerationBackend`) for this test only --
    // Playwright dispatches to the most-recently-registered matching
    // `page.route()` handler first, so this takes priority over the
    // fixture's default `RECOVERY_PREVIEW_DETAIL` response.
    await page.route(
      `**/api/domain/v1/w/${WORKSPACE_SLUG}/generation/runs/${PARTIAL_RUN_ID}/recovery-previews`,
      async (route) => {
        await route.fulfill({
          status: 201,
          contentType: 'application/json',
          body: JSON.stringify(EXPIRED_RECOVERY_PREVIEW_DETAIL),
        });
      }
    );

    await page.goto(PARTIAL_RUN_URL);
    await page.click('button:has-text("Retry Failed")');

    await expect(page.getByText(/expired/i)).toBeVisible();
    await expect(page.locator('button:has-text("Confirm")')).toBeDisabled();
  });

  test('3.8: Cancel Run opens an interstitial confirmation with an in-flight warning', async ({
    page,
  }) => {
    await page.goto(PARTIAL_RUN_URL);
    await page.click('button:has-text("Cancel Run")');

    await expect(page.getByText('Cancel run?')).toBeVisible();
    // `PARTIAL_RUN_DETAIL`'s `done + failed < total` makes
    // `CancelConfirmation.hasInFlightItems` true.
    await expect(page.getByText(/in flight/i)).toBeVisible();
    await expect(page.locator('button:has-text("Keep Running")')).toBeVisible();
  });

  test('3.9: Confirming cancellation executes the cancel command and returns to observation', async ({
    page,
  }) => {
    await page.goto(PARTIAL_RUN_URL);
    await page.click('button:has-text("Cancel Run")');
    await expect(page.getByText('Cancel run?')).toBeVisible();

    // The interstitial's own submit button is labelled "Cancel Run" too
    // (`generation-recovery-cancel-confirm-submit`) -- unambiguous here
    // because `RecoveryPanel` renders `CancelConfirmation` *instead of* the
    // idle action list (mutually exclusive branches), so the original
    // "Cancel Run" button that opened this view is no longer in the DOM.
    await page.click('button:has-text("Cancel Run")');

    await expect(page.getByText('Cancel run?')).not.toBeVisible();
    await expect(page.getByText('Recovery Actions')).toBeVisible();
  });

  test('3.10: Refresh during recovery returns to observation', async ({
    page,
  }) => {
    await page.goto(PARTIAL_RUN_URL);
    await page.reload();
    await expect(page.locator('h1')).toBeVisible();
    await expect(page.getByText('Recovery Actions')).toBeVisible();
  });

  test('3.11: Reconciliation requires an operator note before submitting', async ({
    page,
  }) => {
    await page.goto(PARTIAL_RUN_URL);
    const submitButton = page.locator('button:has-text("Reconcile")');
    await expect(submitButton).toBeVisible();
    await expect(submitButton).toBeDisabled();

    await page
      .getByPlaceholder(/operator note/i)
      .fill('Confirmed the miss with the provider dashboard.');
    await expect(submitButton).toBeEnabled();
  });

  test('3.12: Submitting reconciliation with a note executes the reconcile command', async ({
    page,
  }) => {
    await page.goto(PARTIAL_RUN_URL);
    await page
      .getByPlaceholder(/operator note/i)
      .fill('Confirmed the miss with the provider dashboard.');
    await page.click('button:has-text("Reconcile")');

    // `reconcile_generation_run` falls through to the generic 200-`{}`
    // handler, same as retry/resume/cancel above.
    await expect(
      page.getByRole('heading', { name: 'Reconciliation Required' })
    ).not.toBeVisible();
    await expect(page.getByText('Recovery Actions')).toBeVisible();
  });

  test('3.13: No recovery action buttons render when the run carries no allowed_actions', async ({
    page,
  }) => {
    // `RUN_DETAIL` (Slice 2's already-terminal fixture) has no
    // `allowed_actions` field at all -- `RecoveryPanel` defaults it to `[]`,
    // so every action button is absent even though the panel's heading
    // still renders (it isn't itself gated on any action being visible).
    await page.goto(`${GENERATE_URL}/runs/${RUN_ID}`);
    await expect(page.locator('h1')).toBeVisible();
    await expect(
      page.locator('button:has-text("Retry Failed")')
    ).not.toBeVisible();
    await expect(
      page.locator('button:has-text("Cancel Run")')
    ).not.toBeVisible();
    await expect(
      page.locator('button:has-text("Reconcile")')
    ).not.toBeVisible();
  });
});
