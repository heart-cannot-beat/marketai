import {
  test,
  expect,
  ASSETS_URL,
  PRODUCT_NAME,
  RUN_ID,
  VERSION_ID,
  VERSION_ID_2,
} from './fixtures';

/** Internal identifiers that must never reach the front-flow page text
 * (front-flow contract §1 principle 2, Issue #191). */
const INTERNAL_ID =
  /\b(prod|lib|libver|gd|gr|av|rs|op|exp)_[A-Za-z0-9]{4,}|seeded-(run|asset|plan)/;

/**
 * Gallery navigation (Task 12, spec §14 rows 1/3/9/13). Uses
 * `./fixtures.ts::mockAssetsBackend` (installed automatically by this
 * file's `test` fixture) so `AssetGrid`/`AssetCard`/`LightboxModal` render
 * against two seeded `AssetVersionSummary`/`AssetVersionDetail` fixtures
 * rather than a live domain API.
 */
test.describe('Asset gallery', () => {
  test('loads assets into the grid', async ({ page }) => {
    await page.goto(ASSETS_URL);

    await expect(
      page.locator('[data-testid="asset-card"]').first()
    ).toBeVisible();
    expect(await page.locator('[data-testid="asset-card"]').count()).toBe(2);
  });

  test('shows the 示例 (Controlled Mock) badge on a mock-mode asset only', async ({
    page,
  }) => {
    await page.goto(ASSETS_URL);

    const cards = page.locator('[data-testid="asset-card"]');
    await expect(cards).toHaveCount(2);

    // ASSET_SUMMARY_MOCK (`execution_mode: 'mock'`) is the second seeded
    // item -- its card carries the overlay badge (Task 12 Step 3); the
    // first (`byok`) does not.
    await expect(cards.nth(0).getByTestId('asset-mock-badge')).toHaveCount(0);
    await expect(cards.nth(1).getByTestId('asset-mock-badge')).toHaveText(
      'Example'
    );
    await expect(cards.nth(1).getByTestId('asset-mock-badge')).toHaveClass(
      /badge-error/
    );
  });

  test('names batches by test and stage, with Chinese status and no ids', async ({
    page,
  }) => {
    await page.goto(ASSETS_URL);

    const run = page.getByTestId('batch-rail-run');
    await expect(run).toHaveCount(1);
    await expect(run).toHaveAttribute('data-run-id', RUN_ID);
    await expect(run).toContainText(`Ad test for ${PRODUCT_NAME} · Samples`);
    await expect(run).toContainText('2 directions');
    await expect(run.getByTestId('batch-rail-status')).toHaveText('Completed');
    await expect(run).toContainText('2/2');
    await expect(page.getByTestId('asset-card').first()).toBeVisible();

    const text = await page.locator('main').innerText();
    expect(text).not.toMatch(INTERNAL_ID);
    expect(text).not.toMatch(
      /\b(succeeded|approved|needs_changes|Production|Controlled Mock|Runs)\b/
    );
  });

  test('opens the lightbox on card click and reflects it in the URL', async ({
    page,
  }) => {
    await page.goto(ASSETS_URL);

    await page.locator('[data-testid="asset-card"]').first().click();

    await expect(page.locator('dialog.modal-open')).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`overlay=${VERSION_ID}`));
    await expect(page.getByTestId('asset-detail-title')).toHaveText(
      'Asset details'
    );
  });

  test('Escape closes the lightbox and clears the overlay param', async ({
    page,
  }) => {
    await page.goto(`${ASSETS_URL}?overlay=${VERSION_ID}`);
    await expect(page.locator('dialog.modal-open')).toBeVisible();

    await page.keyboard.press('Escape');

    await expect(page.locator('dialog.modal-open')).not.toBeVisible();
    await expect(page).not.toHaveURL(/overlay=/);
  });

  test('overlay param deep-links directly to a specific version', async ({
    page,
  }) => {
    // `LightboxModal` only wires `ArrowLeft`/`ArrowRight` to `onPrev`/
    // `onNext` when the page shell supplies them --
    // `pages/w/[slug]/assets.tsx` does not currently pass either prop, so
    // in-lightbox arrow-key stepping is not yet reachable end to end (see
    // `task-12-report.md` Concerns). This proves the URL-driven half of
    // navigation: refreshing on a different `overlay` id opens that
    // version's detail directly (spec §14 row 11, refresh-safe recovery).
    await page.goto(`${ASSETS_URL}?overlay=${VERSION_ID_2}`);
    await expect(page.locator('dialog.modal-open')).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`overlay=${VERSION_ID_2}`));
  });

  test('shows QC lineage and sends quality feedback for the exact attempt', async ({
    page,
  }) => {
    await page.goto(`${ASSETS_URL}?overlay=${VERSION_ID}`);

    const lightbox = page.locator('dialog.modal-open');
    await expect(lightbox.getByTestId('qc-state')).toHaveText(
      'Failed (blocking)'
    );
    await expect(
      lightbox.getByTestId('regenerate-quality_feedback')
    ).toBeVisible();

    const regenerationRequest = page.waitForRequest((request) =>
      request
        .url()
        .endsWith(`/asset-versions/${VERSION_ID}/regeneration-drafts`)
    );
    await lightbox.getByTestId('regenerate-quality_feedback').click();

    const request = await regenerationRequest;
    expect(request.method()).toBe('POST');
    expect(request.postDataJSON()).toEqual({
      intent: 'quality_feedback',
      qc_attempt_id: 'qc-attempt-1',
    });

    await lightbox.getByTestId('lineage-child').first().click();
    await expect(page).toHaveURL(new RegExp(`overlay=${VERSION_ID_2}`));
    await expect(lightbox.getByTestId('lineage-parent')).toHaveText(
      /Previous version/
    );
  });

  test('filters by provider and forwards the query to the BFF', async ({
    page,
  }) => {
    await page.goto(ASSETS_URL);

    const filteredRequest = page.waitForRequest((request) => {
      const url = new URL(request.url());
      return (
        request.method() === 'GET' &&
        url.pathname.endsWith('/generation/assets') &&
        url.searchParams.get('provider') === 'openai'
      );
    });
    await page.getByRole('checkbox', { name: /openai 2/ }).click();

    await filteredRequest;
    await expect(page).toHaveURL(/provider=openai/);
    await expect(
      page.getByRole('checkbox', { name: /openai 2/ })
    ).toHaveAttribute('aria-checked', 'true');
  });

  test('restores filters on page refresh', async ({ page }) => {
    await page.goto(`${ASSETS_URL}?provider=openai&review=approved`);

    const openaiChip = page.getByRole('checkbox', { name: /openai 2/ });
    const approvedChip = page.getByTestId('filter-reviewValue-approved');
    await expect(openaiChip).toHaveAttribute('aria-checked', 'true');
    await expect(approvedChip).toHaveAttribute('aria-checked', 'true');

    const restoredRequest = page.waitForRequest((request) => {
      const url = new URL(request.url());
      return (
        request.method() === 'GET' &&
        url.pathname.endsWith('/generation/assets') &&
        url.searchParams.get('provider') === 'openai' &&
        url.searchParams.get('review_value') === 'approved'
      );
    });
    await page.reload();
    await restoredRequest;

    await expect(openaiChip).toHaveAttribute('aria-checked', 'true');
    await expect(approvedChip).toHaveAttribute('aria-checked', 'true');
  });
});
