import {
  test,
  expect,
  LOCALIZATION_BFF,
  LOCALIZATION_URL,
  createBatchThroughUi,
  prepareBatchThroughUi,
  readJson,
  seedLocalizationPrerequisites,
  withdrawTruthPolicyVersion,
} from './fixtures';
import type { LocalizationBatchPayload } from './fixtures';

test('withdrawn Truth remains visible and blocks the real confirmation API after refresh', async ({
  page,
}) => {
  test.setTimeout(180_000);
  const seed = seedLocalizationPrerequisites();
  const readyMarket = seed.markets.find(
    (market) => market.expectedReadiness === 'ready'
  );
  expect(readyMarket).toBeDefined();

  const batchId = await createBatchThroughUi(page, seed, [readyMarket!]);
  const truthPackageId = await prepareBatchThroughUi(page, seed, batchId, 1);
  const withdrawn = withdrawTruthPolicyVersion(seed);
  expect(withdrawn).toMatchObject({
    entryType: 'truth_resolution_policy',
    state: 'withdrawn',
    versionId: seed.truthPolicyVersionId,
  });

  const staleBatch = await readJson<LocalizationBatchPayload>(
    await page.request.get(`${LOCALIZATION_BFF}/batches/${batchId}`)
  );
  expect(staleBatch.truth_package_id).toBe(truthPackageId);
  expect(staleBatch.truth_freshness).toBe('stale');

  const confirm = await page.request.post(
    `${LOCALIZATION_BFF}/batches/${batchId}/confirm`,
    {
      headers: {
        Origin: new URL(page.url()).origin,
        'X-CSRF-Token': (
          await readJson<{ token: string }>(await page.request.get('/api/csrf'))
        ).token,
        'X-Intent-Id': `localization-stale-confirm-${Date.now()}`,
        'If-Match': String(staleBatch.revision),
      },
      data: {
        edit_provider: 'mock',
        edit_model: 'localization-edit-v1',
        edit_adapter_version: 'v1',
        external_qc_steps: [],
        max_attempts: 3,
      },
    }
  );
  expect(confirm.status()).toBe(422);
  const problem = (await confirm.json()) as {
    code: string;
    findings?: string[];
  };
  expect(problem.code).toBe('batch_validation_failed');
  expect(
    problem.findings?.some((finding) => finding.startsWith('truth_stale:'))
  ).toBeTruthy();

  await page.goto(`${LOCALIZATION_URL}?batch=${batchId}`);
  const truthEvidence = page
    .getByRole('heading', { name: 'Truth package' })
    .locator('xpath=ancestor::section');
  await expect(
    page.getByRole('alert').filter({ hasText: 'Stale Truth blocks execution' })
  ).toBeVisible();
  await expect(
    truthEvidence.getByText(truthPackageId, { exact: true })
  ).toBeVisible();
  await expect(
    page.getByRole('button', { name: 'Confirm and create items' })
  ).toBeDisabled();

  await page.reload();
  await expect(
    page
      .getByRole('alert')
      .filter({ hasText: 'Historical evidence remains visible' })
  ).toBeVisible();
  await expect(
    truthEvidence.getByText(truthPackageId, { exact: true })
  ).toBeVisible();
});
