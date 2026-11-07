import {
  test,
  expect,
  LOCALIZATION_BFF,
  LOCALIZATION_URL,
  confirmBatchThroughUi,
  createBatchThroughUi,
  itemDurabilitySnapshot,
  prepareBatchThroughUi,
  readJson,
  seedLocalizationPrerequisites,
} from './fixtures';
import type {
  LocalizationBatchPayload,
  LocalizationItemPayload,
} from './fixtures';

test('drives one ready market from Winner to exact durable Asset lineage', async ({
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
  const confirmed = await confirmBatchThroughUi(page, batchId);
  expect(confirmed.item_ids).toHaveLength(1);
  expect(confirmed.operation_id).toMatch(/^op_/);

  const itemId = confirmed.item_ids[0];
  const itemUrl = `${LOCALIZATION_BFF}/batches/${batchId}/items/${itemId}`;
  let persistedItem: LocalizationItemPayload | undefined;
  await expect
    .poll(
      async () => {
        persistedItem = await readJson<LocalizationItemPayload>(
          await page.request.get(itemUrl)
        );
        return persistedItem.state;
      },
      { timeout: 90_000, intervals: [250, 500, 1000, 2000, 5000] }
    )
    .toBe('succeeded');

  await page.goto(`${LOCALIZATION_URL}?batch=${batchId}&item=${itemId}`);
  const persistedBatch = await readJson<LocalizationBatchPayload>(
    await page.request.get(`${LOCALIZATION_BFF}/batches/${batchId}`)
  );
  persistedItem = await readJson<LocalizationItemPayload>(
    await page.request.get(itemUrl)
  );
  expect(persistedBatch.truth_package_id).toBe(truthPackageId);
  expect(persistedBatch.sources[0].inventory_id).toBe(seed.inventoryId);
  expect(persistedBatch.markets).toEqual(
    expect.arrayContaining([
      expect.objectContaining({
        market: readyMarket!.market,
        readiness: 'ready',
      }),
    ])
  );
  expect(persistedItem.attempt_count).toBe(1);
  expect(persistedItem.attempts[0].qc.length).toBeGreaterThan(0);
  expect(persistedItem.cost).toBeDefined();
  expect(persistedItem.lineage.localized_asset_version_id).toMatch(/^av_/);
  expect(persistedItem.binding).toEqual(
    expect.objectContaining({
      kind: 'localized',
      bound_asset_version_id: persistedItem.lineage.localized_asset_version_id,
      review_state: 'pending_review',
      truth_freshness: 'current',
      ready_for_experiment: false,
    })
  );

  const truthEvidence = page
    .getByRole('heading', { name: 'Truth package' })
    .locator('xpath=ancestor::section');
  const itemEvidence = page
    .getByRole('heading', { name: 'Item attempt & QC' })
    .locator('xpath=ancestor::section');
  await expect(
    truthEvidence.getByText(truthPackageId, { exact: true })
  ).toBeVisible();
  await expect(
    itemEvidence.locator('p').filter({ hasText: itemId })
  ).toBeVisible();
  await expect(itemEvidence.getByText(/Attempt 1 ·/)).toBeVisible();
  await expect(itemEvidence.getByText(/Per-item cost/i)).toBeVisible();
  await expect(
    itemEvidence.getByText(
      String(persistedItem.lineage.localized_asset_version_id),
      { exact: true }
    )
  ).toBeVisible();
  await expect(
    page.getByText(
      /source approval and business evidence status were not inherited/i
    )
  ).toBeVisible();
  await expect(itemEvidence).toContainText(
    'Active binding: Localized version · review: pending review · Truth: current · not ready for a new experiment yet'
  );

  const beforeReload = itemDurabilitySnapshot(persistedItem);
  await page.reload();
  const reloadedBatch = await readJson<LocalizationBatchPayload>(
    await page.request.get(`${LOCALIZATION_BFF}/batches/${batchId}`)
  );
  const reloadedItem = await readJson<LocalizationItemPayload>(
    await page.request.get(itemUrl)
  );
  expect(reloadedBatch.truth_package_id).toBe(truthPackageId);
  expect(reloadedBatch.sources[0].inventory_id).toBe(seed.inventoryId);
  expect(itemDurabilitySnapshot(reloadedItem)).toEqual(beforeReload);
  await expect(
    truthEvidence.getByText(truthPackageId, { exact: true })
  ).toBeVisible();
  await expect(
    itemEvidence.getByText(
      String(reloadedItem.lineage.localized_asset_version_id),
      { exact: true }
    )
  ).toBeVisible();
});
