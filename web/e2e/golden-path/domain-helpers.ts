import { execFileSync } from 'child_process';
import path from 'path';
import { expect } from '@playwright/test';
import type { Page } from '@playwright/test';
import {
  readJson,
  runPython,
  assetsUrl,
  generationBff,
  localizationBff,
  type MetaMode,
} from './fixtures';

const REPOSITORY_ROOT = path.resolve(__dirname, '..', '..', '..');
const PYTHON = process.env.E2E_PYTHON ?? 'python3';

// ---------------------------------------------------------------------------
// Infrastructure seeding
// ---------------------------------------------------------------------------

export interface InfrastructureSeedResult {
  controlledMockConnectionId: string;
  controlledMockScenarioVersionId: string;
  controlledMockBindingId: string;
  customerSandboxConnectionId: string;
  customerSandboxAdAccountId: string;
  customerSandboxScenarioVersionId: string;
  customerSandboxBindingId: string;
  scalePolicyVersionId: string;
}

export function seedInfrastructure(
  workspaceId: string,
  workspaceSlug: string,
  ownerUserId: string
): InfrastructureSeedResult {
  const raw = runPython(
    'golden_path_seed.py',
    [
      'seed-platform-infrastructure',
      '--workspace-id',
      workspaceId,
      '--workspace-slug',
      workspaceSlug,
    ],
    {
      E2E_GOLDEN_PATH_WORKSPACE_ID: workspaceId,
      E2E_OWNER_USER_ID: ownerUserId,
      E2E_GOLDEN_PATH_ALLOW_MUTATIONS: '1',
    }
  );
  return JSON.parse(raw.trim()) as InfrastructureSeedResult;
}

export interface LocalizationInfraResult {
  markets: Array<{
    market: string;
    country: string;
    expectedReadiness: 'ready' | 'degraded' | 'unavailable';
    mockScenario: string;
    marketPolicyVersionId: string;
    failsLockedCopyQc: boolean;
    // The confirmed Winner's own experiment market: Scale enrollment needs
    // the MarketAssetBinding this batch publishes there.
    isWinnerMarket: boolean;
  }>;
  truthPolicyVersionId: string;
  dataSourceVersionId: string;
  inventoryId: string;
}

export function seedLocalizationInfrastructure(
  workspaceId: string,
  assetVersionId: string,
  ownerUserId: string
): LocalizationInfraResult {
  const raw = runPython(
    'golden_path_seed.py',
    [
      'seed-localization-infrastructure',
      '--workspace-id',
      workspaceId,
      '--asset-version-id',
      assetVersionId,
    ],
    {
      E2E_GOLDEN_PATH_WORKSPACE_ID: workspaceId,
      E2E_OWNER_USER_ID: ownerUserId,
      E2E_GOLDEN_PATH_ALLOW_MUTATIONS: '1',
    }
  );
  return JSON.parse(raw.trim()) as LocalizationInfraResult;
}

/** The Winner's readiness for Scale (#129): the journey picks
 * `winnerAdId`, the sandbox ad its PAUSED Launch created, to make the
 * DeliveryTarget itself; nothing here is seeded. */
export interface ScaleInfraResult {
  winnerAdId: string;
  policyVersionId: string;
  marketAssetBindingId: string;
  marketCode: string;
}

/** The attestation activation needs, seeded for the DeliveryTarget the
 * journey made, on the contract set the product made (#129). */
export interface ScaleActivationEvidenceResult {
  attestationId: string;
  contractGenerationId: string;
  contractSetId: string;
}

export function seedScaleInfrastructure(
  workspaceId: string,
  decisionId: string,
  ownerUserId: string
): ScaleInfraResult {
  const raw = runPython(
    'golden_path_seed.py',
    [
      'seed-scale-infrastructure',
      '--workspace-id',
      workspaceId,
      '--decision-id',
      decisionId,
    ],
    {
      E2E_GOLDEN_PATH_WORKSPACE_ID: workspaceId,
      E2E_OWNER_USER_ID: ownerUserId,
      E2E_GOLDEN_PATH_ALLOW_MUTATIONS: '1',
    }
  );
  return JSON.parse(raw.trim()) as ScaleInfraResult;
}

export function seedScaleActivationEvidence(
  workspaceId: string,
  deliveryTargetId: string,
  ownerUserId: string
): ScaleActivationEvidenceResult {
  const raw = runPython(
    'golden_path_seed.py',
    [
      'seed-scale-activation-evidence',
      '--workspace-id',
      workspaceId,
      '--delivery-target-id',
      deliveryTargetId,
    ],
    {
      E2E_GOLDEN_PATH_WORKSPACE_ID: workspaceId,
      E2E_OWNER_USER_ID: ownerUserId,
      E2E_GOLDEN_PATH_ALLOW_MUTATIONS: '1',
    }
  );
  return JSON.parse(raw.trim()) as ScaleActivationEvidenceResult;
}

// ---------------------------------------------------------------------------
// Metrics sync via controlled adapter
// ---------------------------------------------------------------------------

export interface MetricsSyncResult {
  syncedDays: number;
  projectedCandidates: number;
}

export function seedExperimentMetrics(
  workspaceId: string,
  experimentId: string,
  ownerUserId: string,
  options: {
    days: number;
    dayOffset?: number;
    candidateMetrics: Array<{
      candidateId: string;
      dailyImpressions: number;
      dailyClicks: number;
      dailyPurchases?: number;
      dailySpend?: string;
    }>;
  }
): MetricsSyncResult {
  const args = [
    'seed-experiment-metrics',
    '--workspace-id',
    workspaceId,
    '--experiment-id',
    experimentId,
    '--days',
    String(options.days),
    '--candidate-metrics-json',
    JSON.stringify(options.candidateMetrics),
  ];
  if (options.dayOffset) {
    args.push('--day-offset', String(options.dayOffset));
  }
  const raw = runPython('golden_path_seed.py', args, {
    E2E_GOLDEN_PATH_WORKSPACE_ID: workspaceId,
    E2E_OWNER_USER_ID: ownerUserId,
    E2E_GOLDEN_PATH_ALLOW_MUTATIONS: '1',
  });
  return JSON.parse(raw.trim()) as MetricsSyncResult;
}

// ---------------------------------------------------------------------------
// Scale evidence advancement
// ---------------------------------------------------------------------------

export interface ManifestAdvanceResult {
  runId: string;
  outcomeKind: string;
  evaluationId: string | null;
  adviceThreadIds: string[];
  maturity: string | null;
  health: string | null;
  healthReason: string | null;
}

export function advanceManifest(options: {
  workspaceId: string;
  workspaceSlug: string;
  ownerUserId: string;
  // The domain Workspace's current authorization_epoch, which the Scale
  // seed script's preflight (_assert_workspace) requires to match.
  authorizationEpoch: number;
  metaMode: MetaMode;
  enrollmentId: string;
  episodeId: string;
  policyVersionId: string;
  dayIndex: number;
  kind?: 'complete' | 'blocked';
  metrics?: Record<string, unknown>;
  baselineMetrics?: Record<string, unknown>;
}): ManifestAdvanceResult {
  const script = path.resolve(
    __dirname,
    '..',
    'scale',
    'seed_prerequisites.py'
  );
  const args = [
    'advance-manifest',
    '--meta-mode',
    options.metaMode,
    '--enrollment-id',
    options.enrollmentId,
    '--episode-id',
    options.episodeId,
    '--policy-version-id',
    options.policyVersionId,
    '--day-index',
    String(options.dayIndex),
    '--kind',
    options.kind ?? 'complete',
    '--trigger-kind',
    'live',
    '--metrics-json',
    JSON.stringify(options.metrics ?? {}),
  ];
  if (options.baselineMetrics)
    args.push(
      '--baseline-metrics-json',
      JSON.stringify(options.baselineMetrics)
    );
  const raw = execFileSync(PYTHON, [script, ...args], {
    cwd: REPOSITORY_ROOT,
    env: {
      ...process.env,
      PYTHONPATH: REPOSITORY_ROOT,
      // The Scale suite's seed script refuses to mutate without its own
      // preflight contract (REQUIRED_ENVIRONMENT in seed_prerequisites.py).
      E2E_SCALE_ALLOW_MUTATIONS: '1',
      E2E_SCALE_WORKSPACE_ID: options.workspaceId,
      E2E_SCALE_WORKSPACE_SLUG: options.workspaceSlug,
      E2E_SCALE_AUTHORIZATION_EPOCH: String(options.authorizationEpoch),
      E2E_OWNER_USER_ID: options.ownerUserId,
    },
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'inherit'],
  });
  return JSON.parse(raw.trim()) as ManifestAdvanceResult;
}

// ---------------------------------------------------------------------------
// DB inspection — canonical snapshot
// ---------------------------------------------------------------------------

export interface CanonicalSnapshot {
  inspectedAt: string;
  workspaceId: string;
  library?: {
    versionCount: number;
    versions: Array<{
      id: string;
      entryId: string;
      entryType: string;
      versionNo: number;
      state: string;
      eligibility: string | null;
    }>;
  };
  generation?: {
    runCount: number;
    totalItems: number;
    succeededItems: number;
    failedItems: number;
    assetVersionCount: number;
    costReservationCount: number;
    costSettlementCount: number;
    costByState?: Array<{
      state: string;
      currency: string;
      totalProviderAmount: string;
      count: number;
    }>;
    providerCallCount: number;
    /** Distinct governed VLM quality-check calls (Issue #169). */
    qualityCheckCallCount?: number;
    costLedger?: Array<{
      id: string;
      entryType: string;
      direction: string;
      executionMode: string;
      provider: string;
      currency: string;
      amount: string;
      spendReservationId: string;
    }>;
    mockProvenance: Array<{
      runId: string;
      executionMode: string;
      provider: string;
    }>;
    runItemIdentity?: Array<{
      id: string;
      runId: string;
      state: string;
      combinationFingerprint: string;
      semanticFingerprint: string;
      executionMode: string;
      intentKind: string;
    }>;
  };
  assets?: {
    totalVersions: number;
    approvedCount: number;
    exportPackageCount: number;
    exportPackages?: Array<{
      id: string;
      state: string;
      zipHash: string | null;
      manifestHash: string | null;
      byteSize: number | null;
    }>;
    lineage: Array<{
      id: string;
      parentId: string | null;
      originKind: string;
      executionMode: string;
    }>;
  };
  winner?: {
    decisionId: string;
    found: boolean;
    verdict?: string;
    assetVersionId?: string;
    experimentId?: string;
    experimentVersionId?: string;
    productVersionId?: string;
    offerVersionId?: string;
    market?: string;
  };
  localization?: {
    batchId: string;
    found: boolean;
    phase?: string;
    outcome?: string;
    truthPackageId?: string;
    productVersionId?: string;
    offerVersionId?: string;
    sources?: Array<{
      id: string;
      assetVersionId: string;
      decisionId: string;
      sourceGenerationMode: string;
      experimentEvidenceMode: string;
    }>;
    items?: Array<{
      id: string;
      state: string;
      targetMarket: string;
      attemptCount: number;
      reasonCode?: string | null;
      copyVersionId?: string | null;
      sourceAssetVersionId?: string;
    }>;
  };
  scale?: {
    enrollmentId: string;
    found: boolean;
    lifecycle?: string;
    revision?: number;
    metaMode?: MetaMode;
    episodeId: string;
    episodePhase?: string;
    evaluationCount?: number;
    evaluations?: Array<{
      id: string;
      snapshotCanonicalHash: string;
      resultHash: string;
      maturity: string;
      dataQualityState: string;
      health: string | null;
      algorithmVersion: string;
    }>;
    adviceThreads?: Array<{
      id: string;
      adviceType: string;
      lifecycle: string;
      dispositionKind: string | null;
      dispositionAt: string | null;
    }>;
  };
  auditEvents?: Array<{
    eventType: string;
    count: number;
  }>;
  crossAreaIds?: {
    winnerAssetVersionId: string | null;
    localizationSourceAssetVersionId: string | null;
    scaleSourceAssetVersionId: string | null;
    idsConsistent: boolean;
  };
  evidenceSyncRuns?: Array<{
    id: string;
    state: string;
    pageCount: number;
    lastCompletedPageOrder: number | null;
    outputCursor: string | null;
  }>;
  zeroMetaWrites: {
    offending: Array<{ id: string; method: string }>;
  };
  metaWriteConfig: {
    metaWriteEnabled: boolean;
    metaWriteAccessToken: string;
  };
}

export function inspectCanonicalSnapshot(options: {
  workspaceId: string;
  ownerUserId: string;
  decisionId?: string;
  batchId?: string;
  enrollmentId?: string;
  episodeId?: string;
  metaMode?: MetaMode;
}): CanonicalSnapshot {
  const args = ['canonical-snapshot', '--workspace-id', options.workspaceId];
  if (options.decisionId) args.push('--decision-id', options.decisionId);
  if (options.batchId) args.push('--batch-id', options.batchId);
  if (options.enrollmentId) args.push('--enrollment-id', options.enrollmentId);
  if (options.episodeId) args.push('--episode-id', options.episodeId);
  if (options.metaMode) args.push('--meta-mode', options.metaMode);
  const raw = runPython('inspect_db.py', args, {
    E2E_GOLDEN_PATH_WORKSPACE_ID: options.workspaceId,
    E2E_OWNER_USER_ID: options.ownerUserId,
    E2E_COMPOSE_PROJECT:
      process.env.E2E_COMPOSE_PROJECT ?? process.env.COMPOSE_PROJECT_NAME ?? '',
  });
  return JSON.parse(raw.trim()) as CanonicalSnapshot;
}

export function inspectZeroMetaWrites(
  workspaceId: string,
  ownerUserId: string
): { offending: Array<{ id: string; method: string }> } {
  const raw = runPython(
    'inspect_db.py',
    ['zero-meta-writes', '--workspace-id', workspaceId],
    {
      E2E_GOLDEN_PATH_WORKSPACE_ID: workspaceId,
      E2E_OWNER_USER_ID: ownerUserId,
    }
  );
  return JSON.parse(raw.trim()) as {
    offending: Array<{ id: string; method: string }>;
  };
}

export function inspectFakeMetaObjects(
  workspaceId: string,
  metaMode: MetaMode,
  scenarioVersionId: string,
  ownerUserId: string
): Array<{
  object_type: string;
  remote_object_id: string;
  state: Record<string, unknown>;
}> {
  const raw = runPython(
    'inspect_db.py',
    [
      'fake-meta-objects',
      '--workspace-id',
      workspaceId,
      '--meta-mode',
      metaMode,
      '--scenario-version-id',
      scenarioVersionId,
    ],
    {
      E2E_GOLDEN_PATH_WORKSPACE_ID: workspaceId,
      E2E_OWNER_USER_ID: ownerUserId,
    }
  );
  return JSON.parse(raw.trim());
}

// ---------------------------------------------------------------------------
// Localization UI helpers
// ---------------------------------------------------------------------------

export async function createLocalizationBatchThroughUi(
  page: Page,
  slug: string,
  assetVersionId: string,
  targetMarkets: string[]
): Promise<string> {
  await page.goto(`/w/${slug}/localization`);
  await expect(
    page.getByRole('heading', { name: 'Localization control room' })
  ).toBeVisible();

  const newBatch = page
    .getByRole('heading', { name: 'New batch' })
    .locator('xpath=ancestor::section');
  const source = newBatch.getByLabel('Qualified creative / offer / source');
  const choice = source.locator('option').filter({ hasText: assetVersionId });
  await expect(choice).toHaveCount(1);
  await source.selectOption((await choice.getAttribute('value'))!);

  for (const market of targetMarkets) {
    await newBatch
      .locator('label')
      .filter({ hasText: `${market} ·` })
      .getByRole('checkbox')
      .check();
  }
  for (const checkbox of await newBatch.getByRole('checkbox').all()) {
    const labelText = await checkbox.evaluate(
      (el) => el.parentElement?.textContent?.trim() ?? ''
    );
    if (
      !targetMarkets.some((m) => labelText.startsWith(`${m} ·`)) &&
      (await checkbox.isChecked())
    ) {
      await checkbox.uncheck();
    }
  }

  const [createResponse] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.request().method() === 'POST' &&
        new URL(r.url()).pathname === `${localizationBff(slug)}/batches`
    ),
    newBatch
      .getByRole('button', { name: 'Create Controlled Mock batch' })
      .click(),
  ]);
  const created = await readJson<{ batch_id: string }>(createResponse);
  expect(created.batch_id).toMatch(/^lb_/);
  // The control room selects the new batch through ?batch=; preparation
  // controls belong to that selection, so wait for it before returning.
  await expect(page).toHaveURL(
    new RegExp(`[?&]batch=${created.batch_id}(?:&|$)`)
  );
  return created.batch_id;
}

export async function prepareLocalizationBatchThroughUi(
  page: Page,
  slug: string,
  batchId: string,
  truthPolicyVersionId: string,
  inventoryId: string,
  pairCount: number
): Promise<string> {
  const preparation = page
    .getByRole('heading', { name: 'Truth and claim preparation' })
    .locator('xpath=ancestor::section');
  // The policy's source_priority names the DataSource (#70); there is no
  // separate data source choice.
  await preparation
    .getByLabel('Truth policy version')
    .selectOption(truthPolicyVersionId);

  const [startResponse] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.request().method() === 'POST' &&
        new URL(r.url()).pathname ===
          `${localizationBff(slug)}/batches/${batchId}/start-preparation`
    ),
    preparation
      .getByRole('button', { name: 'Resolve Truth and analyze sources' })
      .click(),
  ]);
  const started = await readJson<{ truth_package_id: string }>(startResponse);
  expect(started.truth_package_id).toMatch(/^tp_/);
  await expect(
    preparation.getByText(new RegExp(`^Inventory ${inventoryId} · evidence `))
  ).toBeVisible({ timeout: 60_000 });

  for (let i = 0; i < pairCount; i += 1) {
    const submit = preparation.getByRole('button', {
      name: 'Submit immutable Claim decisions',
    });
    await expect(submit).toBeEnabled({ timeout: 60_000 });
    const [decisionResponse] = await Promise.all([
      page.waitForResponse(
        (r) =>
          r.request().method() === 'POST' &&
          new URL(r.url()).pathname ===
            `${localizationBff(slug)}/batches/${batchId}/claim-decisions`
      ),
      submit.click(),
    ]);
    const decision = await readJson<{ decision_set_id: string }>(
      decisionResponse
    );
    expect(decision.decision_set_id).toMatch(/^lcds_/);
    // Same synchronisation as the Localization suite (web/e2e/localization/
    // fixtures.ts): the submit button moves to the next source-market pair
    // only after the refreshed batch reports the new decision set.
    await expect(
      preparation.getByText(new RegExp(`\\b${i + 1} decision sets?\\b`))
    ).toBeVisible({ timeout: 60_000 });
  }
  await expect(
    preparation.getByText(
      'Every source-market pair has an immutable ClaimDecisionSet.'
    )
  ).toBeVisible({ timeout: 60_000 });

  const localizedCopy = page
    .getByRole('heading', { name: 'Localized copy' })
    .locator('xpath=ancestor::section');
  for (let i = 0; i < pairCount; i += 1) {
    const approve = localizedCopy
      .locator('button:enabled')
      .filter({ hasText: 'Approve copy' })
      .first();
    await expect(approve).toBeVisible({ timeout: 60_000 });
    const [approveResponse] = await Promise.all([
      page.waitForResponse(
        (r) =>
          r.request().method() === 'POST' &&
          new URL(r.url()).pathname ===
            `${localizationBff(slug)}/batches/${batchId}/approve-copy`
      ),
      approve.click(),
    ]);
    await readJson<unknown>(approveResponse);
    await expect(
      localizedCopy
        .locator('button:enabled')
        .filter({ hasText: 'Approve copy' })
    ).toHaveCount(pairCount - i - 1, { timeout: 60_000 });
  }
  await expect(
    page.getByRole('button', { name: 'Confirm and create items' })
  ).toBeEnabled({ timeout: 60_000 });
  return started.truth_package_id;
}

export async function confirmLocalizationBatchThroughUi(
  page: Page,
  slug: string,
  batchId: string
): Promise<{ batch_id: string; operation_id: string; item_ids: string[] }> {
  const [confirmResponse] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.request().method() === 'POST' &&
        new URL(r.url()).pathname ===
          `${localizationBff(slug)}/batches/${batchId}/confirm`
    ),
    page.getByRole('button', { name: 'Confirm and create items' }).click(),
  ]);
  return readJson<{
    batch_id: string;
    operation_id: string;
    item_ids: string[];
  }>(confirmResponse);
}

interface LocalizationItemBinding {
  id: string;
  kind: string;
  bound_asset_version_id: string;
  review_state: string;
  truth_freshness: string;
  ready_for_experiment: boolean;
}

/**
 * Approve a market's localized AssetVersion in the Assets area (#128).
 *
 * The binding a Localization item publishes starts `pending_review`. The
 * exact-version review a user records in Asset Detail is what moves it to
 * `approved` and `ready_for_experiment` -- there is no binding-approval
 * command, and no seed approves it -- and the Scale lineage gate requires it
 * for the Winner's own market. Returns the reviewed version and its binding.
 */
export async function approveLocalizedAssetVersionThroughAssets(
  page: Page,
  slug: string,
  batchId: string,
  market: string
): Promise<{ localizedAssetVersionId: string; bindingId: string }> {
  const batch = await readJson<{
    items?: Array<{ id: string; target_market: string }>;
  }>(await page.request.get(`${localizationBff(slug)}/batches/${batchId}`));
  const item = batch.items?.find(
    (candidate) => candidate.target_market === market
  );
  expect(item, `a Localization item for market ${market}`).toBeTruthy();
  const itemUrl = `${localizationBff(slug)}/batches/${batchId}/items/${item!.id}`;
  const readBinding = async () =>
    (
      await readJson<{ binding: LocalizationItemBinding | null }>(
        await page.request.get(itemUrl)
      )
    ).binding;

  const published = await readBinding();
  expect(published, `the ${market} item published a binding`).toBeTruthy();
  expect(published!.kind).toBe('localized');
  expect(published!.review_state).toBe('pending_review');
  expect(published!.ready_for_experiment).toBe(false);

  const versionId = published!.bound_asset_version_id;
  await page.goto(`${assetsUrl(slug)}?overlay=${versionId}`);
  const approve = page.getByTestId('review-approve');
  await expect(approve).toBeEnabled({ timeout: 15_000 });
  const [reviewResponse] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.request().method() === 'POST' &&
        new URL(r.url()).pathname ===
          `${generationBff(slug)}/asset-versions/${versionId}/reviews`
    ),
    approve.click(),
  ]);
  expect(reviewResponse.status()).toBe(201);

  await expect
    .poll(
      async () => {
        const binding = await readBinding();
        return binding
          ? `${binding.id}:${binding.review_state}:${binding.ready_for_experiment}`
          : 'missing';
      },
      { timeout: 15_000 }
    )
    .toBe(`${published!.id}:approved:true`);
  return { localizedAssetVersionId: versionId, bindingId: published!.id };
}
