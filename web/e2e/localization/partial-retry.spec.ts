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
  restartLocalizationWorker,
  seedLocalizationPrerequisites,
} from './fixtures';
import type {
  LocalizationBatchPayload,
  LocalizationItemPayload,
} from './fixtures';
import { en } from '../support/copy';

interface MatrixEvidence {
  batchId: string;
  truthPackageId: string;
  operationId: string;
  readyItemId: string;
  retryItemId: string;
  unavailableItemId: string;
  readyBeforeRetry: ReturnType<typeof itemDurabilitySnapshot>;
  unavailableBeforeRetry: ReturnType<typeof itemDurabilitySnapshot>;
  retryAfterExhaustion?: ReturnType<typeof itemDurabilitySnapshot>;
}

let evidence: MatrixEvidence;

test.describe.serial('multi-market partial recovery', () => {
  test('projects ready, degraded/missing, unavailable, and locked-copy states around a real Worker restart', async ({
    page,
  }) => {
    test.setTimeout(240_000);
    const seed = seedLocalizationPrerequisites();
    const readyMarket = seed.markets.find(
      (market) => market.expectedReadiness === 'ready'
    );
    const retryMarket = seed.markets.find((market) => market.failsLockedCopyQc);
    const unavailableMarket = seed.markets.find(
      (market) => market.expectedReadiness === 'unavailable'
    );
    expect(readyMarket).toBeDefined();
    expect(retryMarket).toBeDefined();
    expect(unavailableMarket).toBeDefined();

    const batchId = await createBatchThroughUi(page, seed, seed.markets);
    const truthPackageId = await prepareBatchThroughUi(
      page,
      seed,
      batchId,
      seed.markets.length
    );

    const truthEvidence = page
      .getByRole('heading', { name: 'Truth package' })
      .locator('xpath=ancestor::section');
    await expect(
      truthEvidence.getByText(truthPackageId, { exact: true })
    ).toBeVisible();
    await expect(
      truthEvidence.getByText('degraded', { exact: true }).first()
    ).toBeVisible();
    await expect(truthEvidence).toContainText(readyMarket!.market);
    await expect(truthEvidence).toContainText(retryMarket!.market);
    await expect(truthEvidence).toContainText(unavailableMarket!.market);
    // The page words each market's readiness and each Truth failure or skip
    // (#17); the degraded market lists the missing fact type as it is. The
    // codes themselves are checked on the persisted source attempts.
    await expect(truthEvidence).toContainText(
      en('localization', 'truth.readiness-reason.fact_missing_permitted', {
        facts: 'discount',
      })
    );
    await expect(truthEvidence).toContainText(
      en('localization', 'truth.readiness-reason.skipped_unavailable')
    );
    await expect(truthEvidence).toContainText(
      en('common', 'error-fact_type_not_in_mock_scenario')
    );
    await expect(truthEvidence).toContainText(
      en('localization', 'truth.failure-line', {
        fact: 'discount',
        adapter: en('localization', 'truth.adapter.policy'),
        reason: en('localization', 'truth.skip.on_failure_skip'),
      })
    );
    const prepared = await readJson<LocalizationBatchPayload>(
      await page.request.get(`${LOCALIZATION_BFF}/batches/${batchId}`)
    );
    expect(
      prepared.markets.flatMap((market) => market.source_attempts ?? [])
    ).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          fact_type: 'discount',
          failure_code: 'fact_type_not_in_mock_scenario',
        }),
        expect.objectContaining({
          fact_type: 'discount',
          skip_code: 'on_failure_skip',
        }),
      ])
    );
    await expect(
      truthEvidence.getByText('unavailable', { exact: true })
    ).toBeVisible();

    const confirmed = await confirmBatchThroughUi(page, batchId);
    expect(confirmed.item_ids).toHaveLength(seed.markets.length);
    expect(confirmed.operation_id).toMatch(/^op_/);

    // This is an actual Docker Compose process restart after the command and
    // Operation are durable. Browser memory is then discarded before any
    // terminal-state assertion.
    restartLocalizationWorker();
    await page.reload();
    const operationEvidence = page
      .getByRole('heading', { name: 'Durable operation' })
      .locator('xpath=ancestor::section');
    await expect(
      operationEvidence
        .getByText(confirmed.operation_id, { exact: true })
        .first()
    ).toBeVisible({ timeout: 60_000 });

    let batch: LocalizationBatchPayload | undefined;
    await expect
      .poll(
        async () => {
          batch = await readJson<LocalizationBatchPayload>(
            await page.request.get(`${LOCALIZATION_BFF}/batches/${batchId}`)
          );
          return Object.fromEntries(
            batch.items.map((item) => [item.target_market, item.state])
          );
        },
        { timeout: 90_000, intervals: [250, 500, 1000, 2000, 5000] }
      )
      .toEqual({
        [readyMarket!.market]: 'succeeded',
        [retryMarket!.market]: 'retry_wait',
        [unavailableMarket!.market]: 'skipped',
      });

    expect(batch).toBeDefined();
    const itemFor = (market: string) => {
      const item = batch!.items.find(
        (candidate) => candidate.target_market === market
      );
      expect(item).toBeDefined();
      return item!;
    };
    const readyItem = itemFor(readyMarket!.market);
    const retryItem = itemFor(retryMarket!.market);
    const unavailableItem = itemFor(unavailableMarket!.market);
    expect(unavailableItem).toMatchObject({
      state: 'skipped',
      attempt_count: 0,
      reason_code: 'skipped_market_unavailable',
    });

    const readyDetail = await readJson<LocalizationItemPayload>(
      await page.request.get(
        `${LOCALIZATION_BFF}/batches/${batchId}/items/${readyItem.id}`
      )
    );
    const unavailableDetail = await readJson<LocalizationItemPayload>(
      await page.request.get(
        `${LOCALIZATION_BFF}/batches/${batchId}/items/${unavailableItem.id}`
      )
    );
    evidence = {
      batchId,
      truthPackageId,
      operationId: confirmed.operation_id,
      readyItemId: readyItem.id,
      retryItemId: retryItem.id,
      unavailableItemId: unavailableItem.id,
      readyBeforeRetry: itemDurabilitySnapshot(readyDetail),
      unavailableBeforeRetry: itemDurabilitySnapshot(unavailableDetail),
    };

    await page.goto(
      `${LOCALIZATION_URL}?batch=${batchId}&item=${retryItem.id}`
    );
    const retryEvidence = page
      .getByRole('heading', { name: 'Item attempt & QC' })
      .locator('xpath=ancestor::section');
    await expect(
      retryEvidence.getByText('Locked-copy / blocking QC failure')
    ).toBeVisible();
    await expect(retryEvidence).toContainText(
      en('localization', 'item.qc-kind.forbidden_term')
    );
    const retryDetail = await readJson<LocalizationItemPayload>(
      await page.request.get(
        `${LOCALIZATION_BFF}/batches/${batchId}/items/${retryItem.id}`
      )
    );
    expect(
      retryDetail.attempts.flatMap((attempt) => attempt.qc)
    ).toContainEqual(expect.objectContaining({ check_kind: 'forbidden_term' }));
  });

  test('retries only the failed item to the bound and preserves both siblings exactly', async ({
    page,
  }) => {
    test.setTimeout(180_000);
    const retryItemUrl = `${LOCALIZATION_BFF}/batches/${evidence.batchId}/items/${evidence.retryItemId}`;
    const readyItemUrl = `${LOCALIZATION_BFF}/batches/${evidence.batchId}/items/${evidence.readyItemId}`;
    const unavailableItemUrl = `${LOCALIZATION_BFF}/batches/${evidence.batchId}/items/${evidence.unavailableItemId}`;
    await page.goto(
      `${LOCALIZATION_URL}?batch=${evidence.batchId}&item=${evidence.retryItemId}`
    );
    const itemEvidence = page
      .getByRole('heading', { name: 'Item attempt & QC' })
      .locator('xpath=ancestor::section');

    await itemEvidence.getByRole('button', { name: 'Retry item' }).click();
    await expect
      .poll(
        async () => {
          const item = await readJson<LocalizationItemPayload>(
            await page.request.get(retryItemUrl)
          );
          return `${item.state}:${item.attempt_count}`;
        },
        { timeout: 90_000, intervals: [250, 500, 1000, 2000, 5000] }
      )
      .toBe('retry_wait:2');
    await page.reload();
    await expect(itemEvidence.getByText(/Attempt 2 ·/)).toBeVisible();

    expect(
      itemDurabilitySnapshot(
        await readJson<LocalizationItemPayload>(
          await page.request.get(readyItemUrl)
        )
      )
    ).toEqual(evidence.readyBeforeRetry);
    expect(
      itemDurabilitySnapshot(
        await readJson<LocalizationItemPayload>(
          await page.request.get(unavailableItemUrl)
        )
      )
    ).toEqual(evidence.unavailableBeforeRetry);

    await itemEvidence.getByRole('button', { name: 'Retry item' }).click();
    let exhausted: LocalizationItemPayload | undefined;
    await expect
      .poll(
        async () => {
          exhausted = await readJson<LocalizationItemPayload>(
            await page.request.get(retryItemUrl)
          );
          return `${exhausted.state}:${exhausted.attempt_count}`;
        },
        { timeout: 90_000, intervals: [250, 500, 1000, 2000, 5000] }
      )
      .toBe('failed:3');
    await expect
      .poll(
        async () =>
          (
            await readJson<LocalizationBatchPayload>(
              await page.request.get(
                `${LOCALIZATION_BFF}/batches/${evidence.batchId}`
              )
            )
          ).outcome,
        { timeout: 30_000, intervals: [250, 500, 1000, 2000] }
      )
      .toBe('partially_succeeded');

    expect(exhausted).toBeDefined();
    expect(exhausted!.attempts).toHaveLength(3);
    evidence.retryAfterExhaustion = itemDurabilitySnapshot(exhausted!);
    await page.reload();
    await expect(itemEvidence.getByText(/Attempt 3 ·/)).toBeVisible();
    await expect(
      itemEvidence.getByRole('button', { name: 'Retry item' })
    ).toBeDisabled();
    expect(
      exhausted!.allowed_actions?.find((action) => action.action === 'retry')
    ).toMatchObject({ allowed: false, reason_code: 'attempts_exhausted' });
    await expect(itemEvidence).toContainText(
      en('localization', 'item.retry-unavailable', {
        reason: en('localization', 'action-reason.attempts_exhausted'),
      })
    );

    expect(
      itemDurabilitySnapshot(
        await readJson<LocalizationItemPayload>(
          await page.request.get(readyItemUrl)
        )
      )
    ).toEqual(evidence.readyBeforeRetry);
    expect(
      itemDurabilitySnapshot(
        await readJson<LocalizationItemPayload>(
          await page.request.get(unavailableItemUrl)
        )
      )
    ).toEqual(evidence.unavailableBeforeRetry);
  });

  test('reloads the exact operation, attempts, cost, Truth, and lineage evidence', async ({
    page,
  }) => {
    await page.goto(
      `${LOCALIZATION_URL}?batch=${evidence.batchId}&item=${evidence.readyItemId}`
    );
    await page.reload();

    const batch = await readJson<LocalizationBatchPayload>(
      await page.request.get(`${LOCALIZATION_BFF}/batches/${evidence.batchId}`)
    );
    expect(batch.truth_package_id).toBe(evidence.truthPackageId);
    expect(batch.outcome).toBe('partially_succeeded');
    expect(batch.commands).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          command_kind: 'confirm',
          operation_id: evidence.operationId,
        }),
      ])
    );
    expect(
      itemDurabilitySnapshot(
        await readJson<LocalizationItemPayload>(
          await page.request.get(
            `${LOCALIZATION_BFF}/batches/${evidence.batchId}/items/${evidence.readyItemId}`
          )
        )
      )
    ).toEqual(evidence.readyBeforeRetry);
    expect(
      itemDurabilitySnapshot(
        await readJson<LocalizationItemPayload>(
          await page.request.get(
            `${LOCALIZATION_BFF}/batches/${evidence.batchId}/items/${evidence.unavailableItemId}`
          )
        )
      )
    ).toEqual(evidence.unavailableBeforeRetry);
    expect(
      itemDurabilitySnapshot(
        await readJson<LocalizationItemPayload>(
          await page.request.get(
            `${LOCALIZATION_BFF}/batches/${evidence.batchId}/items/${evidence.retryItemId}`
          )
        )
      )
    ).toEqual(evidence.retryAfterExhaustion);

    const operation = await page.request.get(
      `/api/domain/v1/w/${process.env.E2E_LOCALIZATION_WORKSPACE_SLUG ?? 'workspace-a'}` +
        `/operations/${evidence.operationId}`
    );
    expect(operation.ok()).toBeTruthy();
    await expect(
      page
        .getByRole('heading', { name: 'Durable operation' })
        .locator('xpath=ancestor::section')
        .getByText(evidence.operationId, { exact: true })
        .first()
    ).toBeVisible();
    await expect(
      page
        .getByRole('heading', { name: 'Truth package' })
        .locator('xpath=ancestor::section')
        .getByText(evidence.truthPackageId, { exact: true })
    ).toBeVisible();
    const itemEvidence = page
      .getByRole('heading', { name: 'Item attempt & QC' })
      .locator('xpath=ancestor::section');
    await expect(itemEvidence.getByText(/Attempt 1 ·/)).toBeVisible();
    await expect(itemEvidence.getByText(/Per-item cost/i)).toBeVisible();
    await expect(
      itemEvidence.getByText(
        String(evidence.readyBeforeRetry.lineage.localized_asset_version_id),
        { exact: true }
      )
    ).toBeVisible();
  });
});
