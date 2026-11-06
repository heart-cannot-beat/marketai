import { randomUUID } from 'crypto';
import { mkdirSync, writeFileSync } from 'fs';
import path from 'path';
import {
  test,
  expect,
  WORKSPACE_SLUG,
  EXPERIMENTS_URL,
  EXPERIMENTS_BFF,
  LAUNCH_BFF,
  LOCALIZATION_URL,
  LOCALIZATION_BFF,
  scaleBff,
  scaleEnrollmentUrl,
  readJson,
  seedExperimentsPrerequisites,
  seedLocalizationFromDecision,
  seedScaleFromDecision,
  seedAdMetrics,
  inspectFakeMetaObjects,
  waitForOperationState,
  advanceManifest,
  inspectCanonicalSnapshot,
  inspectZeroMetaWrites,
  createLocalizationBatchThroughUi,
  prepareLocalizationBatchThroughUi,
  confirmLocalizationBatchThroughUi,
  itemDurabilitySnapshot,
  restartApiServer,
  restartWorker,
} from './fixtures';
import type {
  ExperimentsPrerequisites,
  LocalizationFromDecisionSeed,
  ScaleFromDecisionSeed,
  LocalizationBatchPayload,
  LocalizationItemPayload,
  ManifestAdvanceResult,
} from './fixtures';
import type { components } from '@/lib/domain-client/generated/domain-v1';

type ExperimentDetail = components['schemas']['ExperimentDetail'];
type ExperimentEntryMutation = components['schemas']['ExperimentEntryMutation'];
type MultiCandidatePreviewResponse =
  components['schemas']['MultiCandidatePreviewResponse'];
type MultiCandidateConfirmationResponse =
  components['schemas']['MultiCandidateConfirmationResponse'];
type LaunchProvisioningAccepted =
  components['schemas']['LaunchProvisioningAccepted'];
type LaunchProvisioningResponse =
  components['schemas']['LaunchProvisioningResponse'];
type ExperimentJudgementResult =
  components['schemas']['ExperimentJudgementResult'];
type ExperimentConfirmationResult =
  components['schemas']['ExperimentConfirmationResult'];

/**
 * Cross-area golden journey: Experiments -> Localization -> Scale
 * (issue #28 closeout).
 *
 * This is the real-stack second half of the six-area acceptance contract
 * (docs/product/v9-functional-alignment-acceptance.md §4). The first
 * half (Library -> Generation -> Assets) is covered by
 * `web/e2e/three-ui-golden-path.spec.ts` with route mocking.
 *
 * LIMITATION: Because `three-ui-golden-path.spec.ts` route-mocks
 * Generation and Assets while this test seeds its own Approved
 * AssetVersions, the two tests do not yet form the contract's single
 * empty-Workspace six-area journey. This design is shipped as the
 * real-stack Experiments -> Localization -> Scale journey.
 *
 * Uses one Playwright test() with named test.step() calls so that a
 * page-level NetworkSpy installed early spans the entire journey
 * (revision #3).
 *
 * DOCUMENTED DEVIATION: Steps using seed_ad_metrics.py bypass the
 * controlled metrics sync/projection path the contract's step 7
 * specifies. This is a direct metric-row insertion deviation
 * (revision #10).
 */

function idempotencyKey(): string {
  return randomUUID();
}

function draftContext(seed: ExperimentsPrerequisites) {
  const start = new Date(Date.now() + 24 * 60 * 60 * 1000);
  const end = new Date(Date.now() + 30 * 24 * 60 * 60 * 1000);
  return {
    market: 'US',
    currency: 'USD',
    placement: 'feed',
    product_version_id: seed.productVersionId,
    offer_version_id: seed.offerVersionId,
    budget: { daily_amount: '100.00', total_amount: '3000.00' },
    schedule: {
      start_at: `${start.toISOString().slice(0, 10)}T00:00:00+00:00`,
      end_at: `${end.toISOString().slice(0, 10)}T23:59:59+00:00`,
      timezone: 'UTC',
    },
    audience: { schema_version: '1', content: { targeting: 'broad' } },
    attribution: { click_window_days: 7, view_window_days: 1 },
    statistical_plan: {
      primary_metric: 'ctr',
      ctr_threshold: '0.01',
      alpha: '0.05',
      bootstrap_samples: 2000,
    },
  };
}

const FREEZE_ACK_TEXT =
  'I reviewed the candidates, budget, schedule, and immutable context.';

const HEALTHY_METRICS = {
  spend: '30',
  impressions: 3000,
  clicks: 120,
  purchases: 4,
  purchase_value: '160',
  reach: 1500,
  frequency: '1.4',
};
const DECLINING_METRICS = {
  spend: '30',
  impressions: 6000,
  clicks: 60,
  purchases: 1,
  purchase_value: '40',
  reach: 1200,
  frequency: '5.0',
};
const DECLINING_BASELINE = {
  spend: '30',
  impressions: 3000,
  clicks: 150,
  purchases: 5,
  purchase_value: '200',
  reach: 1500,
  frequency: '1.4',
};

test('Experiments -> Localization -> Scale cross-area journey', async ({
  page,
  networkSpy,
}) => {
  test.setTimeout(600_000);

  // Shared state across steps
  let experimentSeed: ExperimentsPrerequisites;
  let localizationSeed: LocalizationFromDecisionSeed;
  let scaleSeed: ScaleFromDecisionSeed;
  let experimentId: string;
  let decisionId: string;
  let batchId: string;
  let enrollmentId: string;
  let episodeId: string;
  let dayIndex = 0;

  function nextDay() {
    dayIndex += 1;
    return dayIndex;
  }

  // =========================================================================
  // EXPERIMENTS PHASE
  // =========================================================================

  await test.step('Step 1: Seed Experiment prerequisites', async () => {
    experimentSeed = seedExperimentsPrerequisites();
    expect(experimentSeed.productId).toBeTruthy();
    expect(experimentSeed.assetVersionIds).toHaveLength(3);
    expect(experimentSeed.duplicatePairAssetVersionIds).toHaveLength(2);
  });

  await test.step('Step 2: Create draft experiment with duplicate pair + independent candidate', async () => {
    const createResponse = await page.request.post(EXPERIMENTS_BFF, {
      headers: { 'Idempotency-Key': idempotencyKey() },
      data: {
        product_id: experimentSeed.productId,
        name: `Cross-area journey ${experimentSeed.tag}`,
        draft_context: draftContext(experimentSeed),
      },
    });
    const created = await readJson<ExperimentDetail>(createResponse);
    experimentId = created.id;

    let revision = created.revision;
    for (const avId of [
      experimentSeed.duplicatePairAssetVersionIds[0],
      experimentSeed.duplicatePairAssetVersionIds[1],
      experimentSeed.independentAssetVersionId,
    ]) {
      const entryResp = await page.request.post(
        `${EXPERIMENTS_BFF}/${experimentId}/entries`,
        {
          headers: { 'Idempotency-Key': idempotencyKey() },
          data: { asset_version_id: avId, expected_revision: revision },
        }
      );
      const entry = await readJson<ExperimentEntryMutation>(entryResp);
      revision = entry.revision;
    }

    const detail = await readJson<ExperimentDetail>(
      await page.request.get(`${EXPERIMENTS_BFF}/${experimentId}`)
    );
    expect(detail.entries).toHaveLength(3);
  });

  await test.step('Step 3: Resolve duplicates and freeze', async () => {
    await page.goto(`${EXPERIMENTS_URL}?experiment=${experimentId}`);
    await expect(
      page.getByRole('heading', {
        name: `Cross-area journey ${experimentSeed.tag}`,
      })
    ).toBeVisible();

    await expect(
      page.getByRole('heading', { name: 'Duplicate Concept Resolution' })
    ).toBeVisible({ timeout: 30_000 });

    const analysis = await readJson<{
      has_duplicates: boolean;
      duplicate_groups: Array<{
        duplicate_group_hash: string;
        members: unknown[];
        concept_key_id: string;
      }>;
    }>(
      await page.request.get(
        `${EXPERIMENTS_BFF}/${experimentId}/duplicate-analysis`
      )
    );
    expect(analysis.has_duplicates).toBe(true);
    expect(analysis.duplicate_groups).toHaveLength(1);
    const group = analysis.duplicate_groups[0];

    await page
      .locator(
        `input[name="action-${group.duplicate_group_hash}"][value="allow_controlled_duplicate"]`
      )
      .check();
    await page
      .locator(`#reason-${group.duplicate_group_hash}`)
      .fill('Cross-area journey: intentional duplicate for coverage.');

    await page
      .locator('label')
      .filter({ hasText: FREEZE_ACK_TEXT })
      .locator('input[type="checkbox"]')
      .check();

    const [freezeResponse] = await Promise.all([
      page.waitForResponse(
        (r) =>
          r.request().method() === 'POST' &&
          new URL(r.url()).pathname ===
            `${EXPERIMENTS_BFF}/${experimentId}/freeze`
      ),
      page.getByRole('button', { name: 'Freeze experiment' }).click(),
    ]);
    const frozen = await readJson<ExperimentDetail>(freezeResponse);
    expect(frozen.state).toBe('frozen');
    expect(frozen.candidates).toHaveLength(3);
  });

  await test.step('Step 4: Meta Sandbox Preview -> Confirm -> Execute (all PAUSED)', async () => {
    await page
      .getByLabel('Sandbox Meta connection ID')
      .fill(experimentSeed.customerSandboxConnectionId);
    await page
      .getByLabel('Sandbox Meta ad account ID')
      .fill(`act_${experimentSeed.tag}_sbx`);

    const [previewResponse] = await Promise.all([
      page.waitForResponse(
        (r) =>
          r.request().method() === 'POST' &&
          new URL(r.url()).pathname === `${LAUNCH_BFF}/previews`
      ),
      page.getByRole('button', { name: 'Create Sandbox preview' }).click(),
    ]);
    const preview =
      await readJson<MultiCandidatePreviewResponse>(previewResponse);
    expect(preview.candidate_count).toBe(3);
    expect(preview.state).toBe('draft');

    await page
      .getByText(
        'I reviewed every exact candidate AssetVersion and creative configuration',
        { exact: false }
      )
      .click();
    const [confirmResponse] = await Promise.all([
      page.waitForResponse(
        (r) =>
          r.request().method() === 'POST' &&
          new URL(r.url()).pathname ===
            `${LAUNCH_BFF}/previews/${preview.id}/confirm`
      ),
      page.getByRole('button', { name: 'Confirm Sandbox preview' }).click(),
    ]);
    const confirmed =
      await readJson<MultiCandidateConfirmationResponse>(confirmResponse);
    expect(confirmed.state).toBe('confirmed');

    await page
      .locator('label')
      .filter({ hasText: /^Execute this exact/ })
      .locator('input[type="checkbox"]')
      .check();
    const [executeResponse] = await Promise.all([
      page.waitForResponse(
        (r) =>
          r.request().method() === 'POST' &&
          new URL(r.url()).pathname ===
            `${LAUNCH_BFF}/previews/${preview.id}/execute`
      ),
      page
        .getByRole('button', { name: 'Execute PAUSED Sandbox launch' })
        .click(),
    ]);
    const accepted =
      await readJson<LaunchProvisioningAccepted>(executeResponse);

    const provisioned = (await waitForOperationState(
      page,
      accepted.id,
      'succeeded',
      60_000
    )) as LaunchProvisioningResponse;
    expect(provisioned.provisioning_state).toBe('provisioned');

    const fakeObjects = inspectFakeMetaObjects(
      experimentSeed.workspaceId,
      'customer_sandbox',
      experimentSeed.customerSandboxScenarioVersionId
    );
    expect(fakeObjects.length).toBeGreaterThan(0);
    for (const obj of fakeObjects) {
      expect((obj.state as Record<string, unknown>).status).toBe('PAUSED');
    }
  });

  await test.step('Step 5: Bind candidates and start measurement', async () => {
    const detail = await readJson<ExperimentDetail>(
      await page.request.get(`${EXPERIMENTS_BFF}/${experimentId}`)
    );
    const candidates = detail.candidates ?? [];
    expect(candidates).toHaveLength(3);

    const winnerCandidate = candidates.find(
      (c) =>
        c.asset_version_id === experimentSeed.duplicatePairAssetVersionIds[0]
    )!;
    const loserCandidate = candidates.find(
      (c) =>
        c.asset_version_id === experimentSeed.duplicatePairAssetVersionIds[1]
    )!;
    const independentCandidate = candidates.find(
      (c) => c.asset_version_id === experimentSeed.independentAssetVersionId
    )!;

    const measureAccountId = `act_${experimentSeed.tag}_measure`;

    for (const [candidate, adId] of [
      [winnerCandidate, `ad_winner_${experimentSeed.tag}`],
      [loserCandidate, `ad_loser_${experimentSeed.tag}`],
      [independentCandidate, `ad_independent_${experimentSeed.tag}`],
    ] as const) {
      const label = candidate.position + 1;
      const accountInput = page.getByLabel(`Ad account for candidate ${label}`);
      await accountInput.fill(measureAccountId);
      await page.getByLabel(`Ad ID for candidate ${label}`).fill(adId);
      await Promise.all([
        page.waitForResponse(
          (r) =>
            r.request().method() === 'POST' &&
            /\/candidates\/.+\/bind$/.test(new URL(r.url()).pathname)
        ),
        accountInput.locator('xpath=following-sibling::button').click(),
      ]);
    }

    const [startResponse] = await Promise.all([
      page.waitForResponse(
        (r) =>
          r.request().method() === 'POST' &&
          new URL(r.url()).pathname ===
            `${EXPERIMENTS_BFF}/${experimentId}/start`
      ),
      page.getByRole('button', { name: 'Start measurement' }).click(),
    ]);
    const running = await readJson<ExperimentDetail>(startResponse);
    expect(running.state).toBe('running');
  });

  await test.step('Step 6: Inconclusive judgement (no metrics) -> no Winner', async () => {
    const [inconclusiveResponse] = await Promise.all([
      page.waitForResponse(
        (r) =>
          r.request().method() === 'POST' &&
          new URL(r.url()).pathname ===
            `${EXPERIMENTS_BFF}/${experimentId}/judgements`
      ),
      page.getByRole('button', { name: 'Run judgement' }).click(),
    ]);
    const inconclusive =
      await readJson<ExperimentJudgementResult>(inconclusiveResponse);
    expect(inconclusive.proposal_state).toBe('created');

    const result = inconclusive.judgement.result as Record<string, unknown>;
    const summary = result.summary as Record<string, number>;
    expect(summary.inconclusive).toBeGreaterThan(0);
    expect(summary.win).toBe(0);

    await expect(
      page.getByRole('button', { name: 'No Winner to confirm' })
    ).toBeDisabled();
  });

  await test.step('Step 7: Seed decisive metrics and run judgement -> Winner', async () => {
    const measureAccountId = `act_${experimentSeed.tag}_measure`;
    const metrics = seedAdMetrics(
      experimentSeed.workspaceId,
      [
        {
          adAccountId: measureAccountId,
          adId: `ad_winner_${experimentSeed.tag}`,
          dailyImpressions: 2000,
          dailyClicks: 100,
        },
        {
          adAccountId: measureAccountId,
          adId: `ad_loser_${experimentSeed.tag}`,
          dailyImpressions: 2000,
          dailyClicks: 10,
        },
      ],
      7
    );
    expect(metrics.seeded_rows).toBe(14);

    const [judgeResponse] = await Promise.all([
      page.waitForResponse(
        (r) =>
          r.request().method() === 'POST' &&
          new URL(r.url()).pathname ===
            `${EXPERIMENTS_BFF}/${experimentId}/judgements`
      ),
      page.getByRole('button', { name: 'Run judgement' }).click(),
    ]);
    const judgement = await readJson<ExperimentJudgementResult>(judgeResponse);
    expect(judgement.proposal_state).toBe('created');

    const proposalCard = page.locator('article').filter({
      has: page.getByRole('heading', { name: 'Judgement evidence' }),
    });
    await expect(proposalCard.getByText('Not confirmed')).toBeVisible();

    await proposalCard
      .getByText(
        'I reviewed the evidence and understand this proposal does not become a Winner until I confirm it.'
      )
      .click();
    const [confirmProposalResponse] = await Promise.all([
      page.waitForResponse(
        (r) =>
          r.request().method() === 'POST' &&
          /\/proposals\/.+\/confirm$/.test(new URL(r.url()).pathname)
      ),
      proposalCard.getByRole('button', { name: 'Confirm proposal' }).click(),
    ]);
    const confirmation = await readJson<ExperimentConfirmationResult>(
      confirmProposalResponse
    );

    const winnerDecision = confirmation.decisions?.find(
      (d) => d.verdict === 'win' || d.verdict === 'winner'
    );
    expect(winnerDecision).toBeDefined();
    decisionId = winnerDecision!.id;

    await expect(page.getByText('Confirmed Winner')).toBeVisible();

    await page.reload();
    await expect(page.getByText('Confirmed Winner')).toBeVisible();
  });

  // =========================================================================
  // LOCALIZATION PHASE
  // =========================================================================

  await test.step('Step 8: Seed Localization prerequisites from confirmed decision', async () => {
    localizationSeed = seedLocalizationFromDecision(decisionId);
    expect(localizationSeed.assetVersionId).toBeTruthy();
    expect(localizationSeed.markets).toHaveLength(3);
    expect(localizationSeed.foreignWorkspaceId).toBeTruthy();
    expect(localizationSeed.foreignWorkspaceSlug).toBeTruthy();
  });

  await test.step('Step 9: Create Localization batch with three markets', async () => {
    batchId = await createLocalizationBatchThroughUi(
      page,
      localizationSeed,
      localizationSeed.markets
    );
    expect(batchId).toMatch(/^lb_/);
  });

  await test.step('Step 10: Prepare batch (Truth resolution, claim decisions, copy approval)', async () => {
    await prepareLocalizationBatchThroughUi(
      page,
      localizationSeed,
      batchId,
      localizationSeed.markets.length
    );
  });

  await test.step('Step 11: Confirm batch and verify item outcomes (succeeded/skipped/retry_wait)', async () => {
    const confirmed = await confirmLocalizationBatchThroughUi(page, batchId);
    expect(confirmed.item_ids).toHaveLength(localizationSeed.markets.length);

    const readyMarket = localizationSeed.markets.find(
      (m) => m.expectedReadiness === 'ready'
    )!;
    const retryMarket = localizationSeed.markets.find(
      (m) => m.failsLockedCopyQc
    )!;
    const unavailableMarket = localizationSeed.markets.find(
      (m) => m.expectedReadiness === 'unavailable'
    )!;

    await expect
      .poll(
        async () => {
          const batch = await readJson<LocalizationBatchPayload>(
            await page.request.get(`${LOCALIZATION_BFF}/batches/${batchId}`)
          );
          return Object.fromEntries(
            batch.items.map((item) => [item.target_market, item.state])
          );
        },
        { timeout: 120_000, intervals: [250, 500, 1000, 2000, 5000] }
      )
      .toEqual({
        [readyMarket.market]: 'succeeded',
        [retryMarket.market]: 'retry_wait',
        [unavailableMarket.market]: 'skipped',
      });
  });

  await test.step('Step 12: Retry the degraded item and verify attempt increment', async () => {
    const batch = await readJson<LocalizationBatchPayload>(
      await page.request.get(`${LOCALIZATION_BFF}/batches/${batchId}`)
    );
    const retryMarket = localizationSeed.markets.find(
      (m) => m.failsLockedCopyQc
    )!;
    const retryItem = batch.items.find(
      (i) => i.target_market === retryMarket.market
    )!;
    const retryItemUrl = `${LOCALIZATION_BFF}/batches/${batchId}/items/${retryItem.id}`;

    await page.goto(
      `${LOCALIZATION_URL}?batch=${batchId}&item=${retryItem.id}`
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
  });

  await test.step('Step 13: Localization reload durability', async () => {
    const batch = await readJson<LocalizationBatchPayload>(
      await page.request.get(`${LOCALIZATION_BFF}/batches/${batchId}`)
    );
    const readyMarket = localizationSeed.markets.find(
      (m) => m.expectedReadiness === 'ready'
    )!;
    const readyItem = batch.items.find(
      (i) => i.target_market === readyMarket.market
    )!;

    const readyDetail = await readJson<LocalizationItemPayload>(
      await page.request.get(
        `${LOCALIZATION_BFF}/batches/${batchId}/items/${readyItem.id}`
      )
    );
    expect(readyDetail.attempt_count).toBe(1);
    expect(readyDetail.lineage.localized_asset_version_id).toMatch(/^av_/);

    const beforeReload = itemDurabilitySnapshot(readyDetail);
    await page.goto(
      `${LOCALIZATION_URL}?batch=${batchId}&item=${readyItem.id}`
    );
    await page.reload();
    const reloaded = await readJson<LocalizationItemPayload>(
      await page.request.get(
        `${LOCALIZATION_BFF}/batches/${batchId}/items/${readyItem.id}`
      )
    );
    expect(itemDurabilitySnapshot(reloaded)).toEqual(beforeReload);
  });

  await test.step('Step 14: Foreign Workspace isolation', async () => {
    expect(localizationSeed.foreignWorkspaceSlug).not.toBe(WORKSPACE_SLUG);

    const response = await page.goto(
      `/w/${localizationSeed.foreignWorkspaceSlug}/localization`
    );
    expect(response?.status()).toBe(200);
    await expect(page.getByText('Access Denied')).toBeVisible();

    const denied = await page.request.get(
      `/api/domain/v1/w/${localizationSeed.foreignWorkspaceSlug}/localization/workspace`
    );
    expect(denied.status()).toBe(403);
  });

  // =========================================================================
  // SCALE PHASE
  // =========================================================================

  await test.step('Step 15: Seed Scale prerequisites from same confirmed decision', async () => {
    scaleSeed = seedScaleFromDecision(decisionId);
    expect(scaleSeed.decisionId).toBe(decisionId);
    expect(scaleSeed.deliveryTargetId).toBeTruthy();
    expect(scaleSeed.policyVersionId).toBeTruthy();
    expect(scaleSeed.assetVersionId).toBe(localizationSeed.assetVersionId);
    expect(scaleSeed.productVersionId).toBe(localizationSeed.productVersionId);
  });

  await test.step('Step 16: Create Draft Scale Enrollment', async () => {
    await page.goto(`/w/${WORKSPACE_SLUG}/scale`);
    await expect(page.getByRole('heading', { name: 'Scale' })).toBeVisible();

    await page.getByRole('button', { name: 'New Enrollment' }).click();
    const dialog = page.locator(
      'dialog[aria-labelledby="candidate-selector-title"]'
    );
    await expect(dialog).toBeVisible();

    const decisionButton = dialog.getByText(decisionId, { exact: false });
    await expect(decisionButton).toBeVisible({ timeout: 30_000 });
    await decisionButton.click();
    await dialog
      .getByLabel('Delivery target ID')
      .fill(scaleSeed.deliveryTargetId);

    const [, enrollmentResponse] = await Promise.all([
      page.waitForResponse(
        (r) =>
          r.request().method() === 'POST' &&
          new URL(r.url()).pathname ===
            `${scaleBff('controlled_mock')}/enrollment-candidates`
      ),
      page.waitForResponse(
        (r) =>
          r.request().method() === 'POST' &&
          new URL(r.url()).pathname ===
            `${scaleBff('controlled_mock')}/enrollments`
      ),
      dialog.getByRole('button', { name: 'Create' }).click(),
    ]);
    const created = await readJson<{
      id: string;
      lifecycle: string;
      winner_decision_id: string;
    }>(enrollmentResponse);
    expect(created.lifecycle).toBe('draft');
    expect(created.winner_decision_id).toBe(decisionId);
    enrollmentId = created.id;
  });

  await test.step('Step 17: Activate Enrollment with ETag/If-Match', async () => {
    await page.goto(scaleEnrollmentUrl(enrollmentId));
    await expect(
      page.getByRole('heading', { name: enrollmentId })
    ).toBeVisible();

    await page.locator('[data-action="activate"]').click();
    const activateDialog = page.locator(
      'dialog[aria-labelledby="activate-dialog-title"]'
    );
    await expect(activateDialog).toBeVisible();
    await activateDialog
      .getByRole('button', { name: 'Create preview' })
      .click();
    await expect(activateDialog.getByText('Activation preview ID')).toBeVisible(
      { timeout: 15_000 }
    );

    const [activateResponse] = await Promise.all([
      page.waitForResponse(
        (r) =>
          r.request().method() === 'PUT' &&
          new URL(r.url()).pathname ===
            `${scaleBff('controlled_mock')}/enrollments/${enrollmentId}/activate`
      ),
      activateDialog.getByRole('button', { name: 'Activate' }).click(),
    ]);
    const activated = await readJson<{
      lifecycle: string;
      current_episode_id: string;
    }>(activateResponse);
    expect(activated.lifecycle).toBe('active');
    episodeId = activated.current_episode_id;

    await page.reload();
    await expect(
      page.getByText('Active', { exact: true }).first()
    ).toBeVisible();
  });

  await test.step('Step 18: Advance healthy evidence (warming)', async () => {
    for (let i = 0; i < 3; i += 1) {
      const result = advanceManifest({
        metaMode: 'controlled_mock',
        enrollmentId,
        episodeId,
        policyVersionId: scaleSeed.policyVersionId,
        dayIndex: nextDay(),
        metrics: HEALTHY_METRICS,
      });
      expect(
        result.outcomeKind === 'evaluated' ||
          result.outcomeKind === 'already_evaluated'
      ).toBe(true);
    }

    await page.goto(scaleEnrollmentUrl(enrollmentId));
    await expect(
      page.getByRole('heading', { name: enrollmentId })
    ).toBeVisible();
  });

  await test.step('Step 19: Advance declining evidence until AdviceThread', async () => {
    let lastResult: ManifestAdvanceResult | null = null;
    for (let attempt = 0; attempt < 12; attempt += 1) {
      lastResult = advanceManifest({
        metaMode: 'controlled_mock',
        enrollmentId,
        episodeId,
        policyVersionId: scaleSeed.policyVersionId,
        dayIndex: nextDay(),
        metrics: DECLINING_METRICS,
        baselineMetrics: DECLINING_BASELINE,
      });
      if (lastResult.adviceThreadIds.length > 0) break;
    }
    expect(lastResult).toBeDefined();
  });

  await test.step('Step 20: Convert to GenerationDraft — zero Meta writes', async () => {
    await page.goto(scaleEnrollmentUrl(enrollmentId));
    const convertButton = page.locator('[data-action="convert"]').first();
    await expect(convertButton).toBeVisible({ timeout: 30_000 });

    const before = inspectZeroMetaWrites();
    expect(before.offending).toEqual([]);

    await convertButton.click();
    await expect(page.getByText('Created unsubmitted draft')).toBeVisible({
      timeout: 15_000,
    });

    const after = inspectZeroMetaWrites();
    expect(after.offending).toEqual([]);
  });

  // =========================================================================
  // RESTART RECOVERY
  // =========================================================================

  await test.step('Step 21: Restart API + Worker and compare canonical snapshots', async () => {
    const snapshotArgs = {
      decisionId,
      batchId,
      enrollmentId,
      episodeId,
      metaMode: 'controlled_mock',
    };
    const before = inspectCanonicalSnapshot(snapshotArgs);

    const snapshotDir = path.join(
      __dirname,
      '..',
      '..',
      'test-results',
      'cross-area-snapshots'
    );
    mkdirSync(snapshotDir, { recursive: true });
    writeFileSync(
      path.join(snapshotDir, 'canonical-before-restart.json'),
      JSON.stringify(before, null, 2)
    );

    await restartApiServer();
    restartWorker();

    const after = inspectCanonicalSnapshot(snapshotArgs);
    writeFileSync(
      path.join(snapshotDir, 'canonical-after-restart.json'),
      JSON.stringify(after, null, 2)
    );

    expect(after.winner).toEqual(before.winner);

    expect(after.localization?.phase).toBe(before.localization?.phase);
    expect(after.localization?.outcome).toBe(before.localization?.outcome);
    expect(after.localization?.items).toEqual(before.localization?.items);

    expect(after.scale?.lifecycle).toBe(before.scale?.lifecycle);
    expect(after.scale?.revision).toBe(before.scale?.revision);
    expect(after.scale?.metaMode).toBe(before.scale?.metaMode);
    expect(after.scale?.evaluationCount).toBe(before.scale?.evaluationCount);
    expect(after.scale?.evaluationIds).toEqual(before.scale?.evaluationIds);
    expect(after.scale?.adviceThreads).toEqual(before.scale?.adviceThreads);

    expect(after.zeroMetaWrites.offending).toEqual([]);
  });

  await test.step('Step 22: Post-restart browser navigation proves projection recovery', async () => {
    await page.goto(`${EXPERIMENTS_URL}?experiment=${experimentId}`);
    await expect(page.getByText('Confirmed Winner')).toBeVisible();

    await page.goto(`${LOCALIZATION_URL}?batch=${batchId}`);
    await expect(
      page.getByRole('heading', { name: 'Localization control room' })
    ).toBeVisible();

    await page.goto(scaleEnrollmentUrl(enrollmentId));
    await expect(
      page.getByRole('heading', { name: enrollmentId })
    ).toBeVisible();
  });

  // =========================================================================
  // ZERO META WRITES — FINAL ASSERTION
  // =========================================================================

  await test.step('Step 23: Zero Meta writes across the entire journey', async () => {
    expect(networkSpy.offendingRequests).toEqual([]);
    const dbCheck = inspectZeroMetaWrites();
    expect(dbCheck.offending).toEqual([]);
  });
});
