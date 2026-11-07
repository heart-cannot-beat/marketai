import {
  test,
  expect,
  ASSETS_URL,
  EXPORT_PACKAGE_ID,
  VERSION_ID,
} from './fixtures';
import { en } from '../support/copy';

/**
 * Export flow against the async 202 acknowledgement, recoverable package
 * projection, typed manifest, and signed-get download capability.
 */
test.describe('Export flow', () => {
  test('configure -> start -> ready, with package recovery and dedup summary', async ({
    page,
  }) => {
    await page.goto(`${ASSETS_URL}?export=new`);

    const dialog = page.locator('dialog.modal-open');
    await expect(dialog).toBeVisible();
    await expect(dialog.getByText('Approved only')).toBeVisible();
    await expect(
      dialog.getByTestId('export-preset-approved_only')
    ).toBeChecked();

    const createRequest = page.waitForRequest((request) =>
      request.url().endsWith('/generation/exports')
    );
    await dialog.getByTestId('export-start').click();

    const request = await createRequest;
    expect(request.method()).toBe('POST');
    expect(request.postDataJSON()).toEqual({
      selector: { kind: 'approved_only' },
    });

    await expect(page).toHaveURL(new RegExp(`export=${EXPORT_PACKAGE_ID}`));
    await expect(dialog.getByTestId('export-package-state')).toHaveText(
      'Ready'
    );
    // EXPORT_MANIFEST_READY has two entries with one payload hash.
    await expect(dialog.getByTestId('export-dedup')).toContainText(
      en('assets', 'manifest-deduplicated', { dedupCount: 1, uniqueCount: 1 })
    );
    await expect(dialog.getByTestId('export-download')).toHaveText(
      'Download ZIP'
    );
    await expect(dialog.getByTestId('export-download')).toBeEnabled();
    // Version ids and hashes stay in the collapsed internal view.
    expect(await dialog.innerText()).not.toMatch(/seeded-a|hash-/);
    await dialog.getByRole('button', { name: 'Show internal view' }).click();
    await expect(dialog.getByTestId('export-internal-view')).toContainText(
      'seeded-a'
    );
  });

  test('submits selected exact versions with the explicit selector shape', async ({
    page,
  }) => {
    await page.goto(ASSETS_URL);
    await page
      .getByRole('checkbox', { name: 'Select asset 1' })
      .check({ force: true });
    await page.getByTestId('selection-export').click();

    const dialog = page.locator('dialog.modal-open');
    await expect(dialog.getByText('Selected assets (1)')).toBeVisible();

    const createRequest = page.waitForRequest((request) =>
      request.url().endsWith('/generation/exports')
    );
    await dialog.getByTestId('export-start').click();

    const request = await createRequest;
    expect(request.postDataJSON()).toEqual({
      selector: { kind: 'explicit', version_ids: [VERSION_ID] },
    });
  });

  test('recovers into the ready step from an existing package id', async ({
    page,
  }) => {
    await page.goto(`${ASSETS_URL}?export=${EXPORT_PACKAGE_ID}`);

    const dialog = page.locator('dialog.modal-open');
    await expect(dialog).toBeVisible();
    await expect(dialog.getByTestId('export-package-state')).toHaveText(
      'Ready'
    );
    await expect(dialog.getByText('Approved only')).not.toBeVisible();
  });

  test('requests the ZIP through the exact package signed-get intent', async ({
    page,
  }) => {
    await page.goto(`${ASSETS_URL}?export=${EXPORT_PACKAGE_ID}`);
    const dialog = page.locator('dialog.modal-open');
    await expect(dialog.getByTestId('export-package-state')).toHaveText(
      'Ready'
    );

    const downloadIntentRequest = page.waitForRequest((request) =>
      request
        .url()
        .endsWith(`/generation/exports/${EXPORT_PACKAGE_ID}/download-intent`)
    );
    await dialog.getByTestId('export-download').click();

    const request = await downloadIntentRequest;
    expect(request.method()).toBe('POST');
    expect(request.postData()).toBeNull();
  });

  test('Cancel closes the dialog and clears the export param', async ({
    page,
  }) => {
    await page.goto(`${ASSETS_URL}?export=new`);
    await expect(page.locator('dialog.modal-open')).toBeVisible();

    await page.getByTestId('export-cancel').click();

    await expect(page.locator('dialog.modal-open')).not.toBeVisible();
    await expect(page).not.toHaveURL(/export=/);
  });
});
