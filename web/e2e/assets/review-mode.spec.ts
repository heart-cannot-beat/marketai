import type { Page } from '@playwright/test';
import { test, expect, ASSETS_URL, VERSION_ID, VERSION_ID_2 } from './fixtures';

async function selectAssetsAndOpenReview(
  page: Page,
  assetNumbers: number[]
): Promise<void> {
  await page.goto(ASSETS_URL);

  for (const assetNumber of assetNumbers) {
    await page
      .getByRole('checkbox', { name: `Select asset ${assetNumber}` })
      .check({ force: true });
  }

  const firstVersionId = assetNumbers[0] === 2 ? VERSION_ID_2 : VERSION_ID;
  const detailResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      response.request().method() === 'GET' &&
      url.pathname.endsWith(`/generation/asset-versions/${firstVersionId}`) &&
      response.status() === 200
    );
  });

  await page.getByTestId('selection-review').click();
  await detailResponse;
}

/**
 * Fullscreen exact-version review mode. The collection-level
 * `ActionEligibility` enables the selection action; each current
 * AssetVersion's own eligibility then gates A/C/X submission.
 */
test.describe('Review mode', () => {
  test('reviews and skips the selected exact-version queue from the keyboard', async ({
    page,
  }) => {
    await selectAssetsAndOpenReview(page, [1, 2]);
    await expect(page.getByTestId('review-position')).toHaveText('0/2');
    // The A / C / X keys stay letters with the translated verdict beside them.
    const shortcuts = page.getByTestId('review-shortcuts');
    await expect(shortcuts).toContainText('A Approve');
    await expect(shortcuts).toContainText('C Needs changes');
    await expect(shortcuts).toContainText('X Reject');

    const reviewRequest = page.waitForRequest((request) =>
      request.url().endsWith(`/asset-versions/${VERSION_ID}/reviews`)
    );
    await page.keyboard.press('A');

    const request = await reviewRequest;
    expect(request.method()).toBe('POST');
    expect(request.postDataJSON()).toEqual({ review_value: 'approved' });
    await expect(page.getByText('1/2', { exact: true })).toBeVisible();

    await page.keyboard.press('ArrowLeft');
    await expect(page.getByTestId('review-position')).toHaveText('0/2');
    const correctionRequest = page.waitForRequest((candidate) =>
      candidate.url().endsWith(`/asset-versions/${VERSION_ID}/reviews`)
    );
    await page.keyboard.press('X');
    expect((await correctionRequest).postDataJSON()).toEqual({
      review_value: 'rejected',
      supersedes_review_id: `review-${VERSION_ID}`,
    });
    await expect(page.getByText('1/2', { exact: true })).toBeVisible();

    await page.keyboard.press('Space');
    await expect(page.getByTestId('review-complete')).toHaveText(
      'Review complete'
    );
    const reviewed = page.getByTestId('review-stat-reviewed');
    await expect(reviewed).toContainText('Reviewed');
    await expect(reviewed).toContainText('2 in all');
    await expect(reviewed.locator('.stat-value')).toHaveText('1');
    await expect(
      page.getByTestId('review-stat-skipped').locator('.stat-value')
    ).toHaveText('1');
    await expect(page.getByTestId('review-summary')).not.toContainText(
      /Review|Reviewed|Skipped|Done/
    );
  });

  test('restores queue position on refresh and appends a correction to the current version', async ({
    page,
  }) => {
    await selectAssetsAndOpenReview(page, [1, 2]);

    const firstReview = page.waitForRequest((request) =>
      request.url().endsWith(`/asset-versions/${VERSION_ID}/reviews`)
    );
    await page.keyboard.press('A');
    await firstReview;
    await expect(page.getByText('1/2', { exact: true })).toBeVisible();
    await expect
      .poll(() =>
        page.evaluate(() => {
          const key = Object.keys(sessionStorage).find((candidate) =>
            candidate.startsWith('asset-review-queue:workspace-a:')
          );
          if (!key) return null;
          return JSON.parse(sessionStorage.getItem(key) ?? 'null')
            ?.currentIndex;
        })
      )
      .toBe(1);

    const restoredDetail = page.waitForResponse((response) =>
      response.url().endsWith(`/generation/asset-versions/${VERSION_ID_2}`)
    );
    await page.reload();
    await restoredDetail;
    await expect(page.getByText('1/2', { exact: true })).toBeVisible();
    await expect(page.locator('.loading-spinner')).toHaveCount(0);

    const correctionRequest = page.waitForRequest((request) =>
      request.url().endsWith(`/asset-versions/${VERSION_ID_2}/reviews`)
    );
    await page.keyboard.press('X');

    const request = await correctionRequest;
    expect(request.method()).toBe('POST');
    expect(request.postDataJSON()).toEqual({
      review_value: 'rejected',
      supersedes_review_id: 'review-existing-2',
    });
    await expect(page.getByTestId('review-complete')).toBeVisible();
  });

  test('Escape exits an active review queue and clears the URL', async ({
    page,
  }) => {
    await selectAssetsAndOpenReview(page, [1]);
    await expect(page.getByText('0/1', { exact: true })).toBeVisible();

    await page.keyboard.press('Escape');

    await expect(page.getByText('0/1', { exact: true })).not.toBeVisible();
    await expect(page).not.toHaveURL(/reviewMode=/);
  });

  test('Done exits a completed review queue', async ({ page }) => {
    await selectAssetsAndOpenReview(page, [1]);
    await page.keyboard.press('Space');
    await expect(page.getByTestId('review-complete')).toBeVisible();

    await page.getByTestId('review-done').click();

    await expect(page.getByTestId('review-complete')).not.toBeVisible();
    await expect(page).not.toHaveURL(/reviewMode=/);
  });
});
