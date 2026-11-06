import { test, expect } from '@playwright/test';
import { LIBRARY_URL } from '../library/fixtures';
import {
  DRAFT_ID,
  PARTIAL_OPERATION_ID,
  PARTIAL_RUN_ID,
  PREVIEW_ID,
  PRODUCT_NAME,
  WORKSPACE_SLUG,
  mockGenerationBackend,
} from '../generation/fixtures';
import { ASSETS_URL, VERSION_ID, mockAssetsBackend } from '../assets/fixtures';

/**
 * Issue #27 close-out journey across the three Workspace UIs delivered by
 * #47, #48, and #49.
 *
 * The Library step deliberately remains on the real same-origin BFF path: it
 * proves the seeded owner's SSR membership and one live Domain read before
 * the lower browser boundary is mocked. Consequently this spec has the same
 * prerequisites as the existing Library suite: a running control-plane
 * Postgres, owner storage state for `workspace-a`, and a reachable FastAPI
 * Domain service. It never fabricates membership or SSR bootstrap data.
 *
 * Generation and Assets reuse their existing route fixtures after that
 * boundary. Those fixtures exercise the real page, hooks, generated gateway,
 * command intent/CSRF pipeline, and recovery UI while replacing paid/worker
 * domain outcomes with deterministic responses. The two fixture sets both
 * own `/generation/**`, so they are installed one phase at a time and removed
 * between phases instead of allowing one broad fallback to hide the other.
 */
test('Library -> Generation recovery -> Assets review, regeneration, and export', async ({
  page,
}) => {
  const observedMutations: string[] = [];
  page.on('request', (request) => {
    if (request.method() !== 'GET') {
      observedMutations.push(new URL(request.url()).pathname);
    }
  });

  // 1. Creative Library: cross the real SSR + BFF boundary and prove that
  // the default Product collection is supplied by the live Domain service.
  const libraryEntriesResponse = page.waitForResponse(
    (response) =>
      response.request().method() === 'GET' &&
      new URL(response.url()).pathname ===
        `/api/domain/v1/w/${WORKSPACE_SLUG}/library/entries`
  );
  await page.goto(LIBRARY_URL);
  await expect(
    page.getByRole('heading', { name: 'Creative Library' })
  ).toBeVisible();
  await expect(
    page.locator('aside').getByRole('button', { name: 'Products' })
  ).toBeVisible();
  expect((await libraryEntriesResponse).status()).toBe(200);

  // 2. New Generation: reuse the deterministic browser fixture, submit the
  // draft into its partially-succeeded Run, then confirm Retry Failed.
  await mockGenerationBackend(page);
  await page.route(
    `**/api/domain/v1/w/${WORKSPACE_SLUG}/generation/runs`,
    async (route) => {
      if (route.request().method() !== 'POST') {
        await route.fallback();
        return;
      }
      await route.fulfill({
        status: 202,
        contentType: 'application/json',
        body: JSON.stringify({
          operation_id: PARTIAL_OPERATION_ID,
          generation_run_id: PARTIAL_RUN_ID,
        }),
      });
    }
  );

  // The v9 areas sit in the 高级工作台 nav group (#165); it is open here
  // because the Library page is one of them.
  await page.locator('nav a', { hasText: 'New generation' }).click();
  await page.getByRole('button', { name: 'New Draft' }).first().click();
  const createDialog = page.locator('dialog[aria-label="Create New Draft"]');
  await createDialog.getByRole('radio', { name: PRODUCT_NAME }).check();
  await createDialog.locator('#new-draft-provider').selectOption({ index: 1 });
  await createDialog.locator('#new-draft-model').selectOption({ index: 1 });
  await createDialog.locator('#new-draft-mode').selectOption({ index: 1 });
  await createDialog.getByRole('button', { name: 'Create Draft' }).click();
  await expect(page).toHaveURL(new RegExp(`/generate/drafts/${DRAFT_ID}$`));

  await page.getByRole('button', { name: 'Generate Preview' }).click();
  await expect(page).toHaveURL(new RegExp(`[?&]preview=${PREVIEW_ID}`));
  await page.getByRole('button', { name: 'Submit Run' }).click();
  await expect(page).toHaveURL(new RegExp(`/generate/runs/${PARTIAL_RUN_ID}$`));
  await expect(page.getByText('Recovery Actions')).toBeVisible();
  await page.getByRole('button', { name: 'Retry Failed' }).click();
  await expect(
    page.getByRole('heading', { name: 'Recovery Preview' })
  ).toBeVisible();
  await page.getByRole('button', { name: 'Confirm' }).click();
  await expect(page.getByText('Recovery Actions')).toBeVisible();

  await expect
    .poll(() =>
      observedMutations.some((path) =>
        path.endsWith(`/generation/runs/${PARTIAL_RUN_ID}/retry-failed-items`)
      )
    )
    .toBe(true);

  // 3. Assets: replace the Generation fixture as one unit, then drive the
  // first gallery item through append-only review, regeneration draft, and
  // export-package recovery. Assertions include the actual BFF mutation
  // paths so a button-only UI success cannot satisfy the journey.
  await page.unrouteAll({ behavior: 'wait' });
  await mockAssetsBackend(page);
  await page.locator('nav a', { hasText: 'Assets' }).click();
  await expect(page).toHaveURL(ASSETS_URL);
  await page.locator('[data-testid="asset-card"]').first().click();

  const lightbox = page.locator('dialog.modal-open');
  await expect(lightbox.getByTestId('asset-detail-title')).toHaveText(
    'Asset details'
  );
  await lightbox.getByTestId('review-approve').click();
  await expect
    .poll(
      () =>
        observedMutations.filter((path) =>
          path.endsWith(`/generation/asset-versions/${VERSION_ID}/reviews`)
        ).length
    )
    .toBe(1);

  const qualityFeedbackRequest = page.waitForRequest((request) =>
    request
      .url()
      .endsWith(`/generation/asset-versions/${VERSION_ID}/regeneration-drafts`)
  );
  await lightbox.getByTestId('regenerate-quality_feedback').click();
  expect((await qualityFeedbackRequest).postDataJSON()).toEqual({
    intent: 'quality_feedback',
    qc_attempt_id: 'qc-attempt-1',
  });
  await expect
    .poll(
      () =>
        observedMutations.filter((path) =>
          path.endsWith(
            `/generation/asset-versions/${VERSION_ID}/regeneration-drafts`
          )
        ).length
    )
    .toBe(1);
  await lightbox.getByRole('button', { name: 'Close asset details' }).click();

  await page.goto(`${ASSETS_URL}?export=new`);
  const exportDialog = page.locator('dialog.modal-open');
  await exportDialog.getByTestId('export-start').click();
  await expect(exportDialog.getByTestId('export-package-state')).toHaveText(
    'Ready'
  );
  await expect(exportDialog.getByTestId('export-dedup')).toBeVisible();
  await expect(exportDialog.getByTestId('export-download')).toBeEnabled();
  await expect
    .poll(
      () =>
        observedMutations.filter((path) => path.endsWith('/generation/exports'))
          .length
    )
    .toBe(1);
});
