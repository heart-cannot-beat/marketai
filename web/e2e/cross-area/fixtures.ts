import { execFileSync, spawnSync } from 'child_process';
import path from 'path';
import { test as base, expect } from '@playwright/test';
import type { APIResponse, Page, Request, Response } from '@playwright/test';

export { expect };

const REPOSITORY_ROOT = path.resolve(__dirname, '..', '..', '..');
const PYTHON = process.env.E2E_PYTHON ?? 'python3';

export const WORKSPACE_SLUG =
  process.env.E2E_CROSS_AREA_WORKSPACE_SLUG ?? 'workspace-a';
export const WORKSPACE_ID = process.env.E2E_CROSS_AREA_WORKSPACE_ID!;
export const USER_ID = process.env.E2E_OWNER_USER_ID!;

export const EXPERIMENTS_URL = `/w/${WORKSPACE_SLUG}/legacy-experiments`;
export const EXPERIMENTS_BFF = `/api/domain/v1/w/${WORKSPACE_SLUG}/experiments`;
export const LAUNCH_BFF = `/api/domain/v1/w/${WORKSPACE_SLUG}/launch`;
export const LOCALIZATION_URL = `/w/${WORKSPACE_SLUG}/localization`;
export const LOCALIZATION_BFF = `/api/domain/v1/w/${WORKSPACE_SLUG}/localization`;

export function scaleBff(metaMode: string): string {
  return `/api/domain/v1/w/${WORKSPACE_SLUG}/scale/${metaMode}`;
}

export function scaleEnrollmentUrl(enrollmentId: string): string {
  return `/w/${WORKSPACE_SLUG}/scale/${enrollmentId}`;
}

export const API_BASE =
  process.env.API_BASE_URL ??
  process.env.E2E_BASE_URL ??
  'http://localhost:4002';

// ---------------------------------------------------------------------------
// Python helpers
// ---------------------------------------------------------------------------

function runPython(script: string, args: string[]): string {
  return execFileSync(PYTHON, [path.join(__dirname, script), ...args], {
    cwd: REPOSITORY_ROOT,
    env: { ...process.env, PYTHONPATH: REPOSITORY_ROOT },
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'inherit'],
  });
}

// ---------------------------------------------------------------------------
// Seeding — Experiments prerequisites (reuses experiments/seed_prerequisites.py)
// ---------------------------------------------------------------------------

export interface ExperimentsPrerequisites {
  workspaceId: string;
  workspaceSlug: string;
  userId: string;
  tag: string;
  productId: string;
  productVersionId: string;
  offerVersionId: string;
  assetVersionIds: string[];
  duplicatePairAssetVersionIds: string[];
  independentAssetVersionId: string;
  sharedConceptKeyId: string;
  distinctConceptKeyId: string;
  customerSandboxConnectionId: string;
  customerSandboxScenarioVersionId: string;
  controlledMockConnectionId: string;
  controlledMockScenarioVersionId: string;
}

export function seedExperimentsPrerequisites(): ExperimentsPrerequisites {
  const script = path.resolve(
    __dirname,
    '..',
    'experiments',
    'seed_prerequisites.py'
  );
  const raw = execFileSync(PYTHON, [script], {
    cwd: REPOSITORY_ROOT,
    env: {
      ...process.env,
      PYTHONPATH: REPOSITORY_ROOT,
      E2E_EXPERIMENTS_ALLOW_MUTATIONS: '1',
      E2E_EXPERIMENTS_WORKSPACE_ID: WORKSPACE_ID,
      E2E_EXPERIMENTS_WORKSPACE_SLUG: WORKSPACE_SLUG,
    },
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'inherit'],
  });
  return JSON.parse(raw.trim()) as ExperimentsPrerequisites;
}

// ---------------------------------------------------------------------------
// Seeding — Localization prerequisites from confirmed decision
// ---------------------------------------------------------------------------

export interface LocalizationMarketSeed {
  market: string;
  country: string;
  expectedReadiness: 'ready' | 'degraded' | 'unavailable';
  mockScenario: string;
  marketPolicyVersionId: string;
  failsLockedCopyQc: boolean;
}

export interface LocalizationFromDecisionSeed {
  decisionId: string;
  assetVersionId: string;
  productId: string;
  productVersionId: string;
  offerVersionId: string;
  market: string;
  foreignWorkspaceId: string;
  foreignWorkspaceSlug: string;
  inventoryId: string;
  markets: LocalizationMarketSeed[];
  truthPolicyVersionId: string;
  dataSourceVersionId: string;
}

export function seedLocalizationFromDecision(
  decisionId: string
): LocalizationFromDecisionSeed {
  const raw = runPython('seed_prerequisites.py', [
    'seed-localization-from-decision',
    '--decision-id',
    decisionId,
  ]);
  return JSON.parse(raw.trim()) as LocalizationFromDecisionSeed;
}

// ---------------------------------------------------------------------------
// Seeding — Scale prerequisites from confirmed decision
// ---------------------------------------------------------------------------

export interface ScaleFromDecisionSeed {
  decisionId: string;
  assetVersionId: string;
  productId: string;
  productVersionId: string;
  offerVersionId: string;
  marketCode: string;
  metaMode: string;
  deliveryTargetId: string;
  contractSetId: string;
  policyVersionId: string;
}

export function seedScaleFromDecision(
  decisionId: string
): ScaleFromDecisionSeed {
  const raw = runPython('seed_prerequisites.py', [
    'seed-scale-from-decision',
    '--decision-id',
    decisionId,
  ]);
  return JSON.parse(raw.trim()) as ScaleFromDecisionSeed;
}

// ---------------------------------------------------------------------------
// Canonical DB snapshot for restart comparison
// ---------------------------------------------------------------------------

export interface CanonicalSnapshot {
  inspectedAt: string;
  workspaceId: string;
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
    items?: Array<{
      id: string;
      state: string;
      targetMarket: string;
      attemptCount: number;
      reasonCode?: string | null;
    }>;
  };
  scale?: {
    enrollmentId: string;
    found: boolean;
    lifecycle?: string;
    revision?: number;
    metaMode?: string;
    scenarioVersionId?: string | null;
    episodeId: string;
    episodePhase?: string;
    evaluationCount?: number;
    evaluationIds?: string[];
    adviceThreads?: Array<{
      id: string;
      adviceType: string;
      lifecycle: string;
      dispositionKind: string | null;
    }>;
  };
  zeroMetaWrites: {
    offending: Array<{ id: string; method: string }>;
  };
}

export function inspectCanonicalSnapshot(options: {
  decisionId?: string;
  batchId?: string;
  enrollmentId?: string;
  episodeId?: string;
  metaMode?: string;
}): CanonicalSnapshot {
  const args = ['canonical-snapshot'];
  if (options.decisionId) args.push('--decision-id', options.decisionId);
  if (options.batchId) args.push('--batch-id', options.batchId);
  if (options.enrollmentId) args.push('--enrollment-id', options.enrollmentId);
  if (options.episodeId) args.push('--episode-id', options.episodeId);
  if (options.metaMode) args.push('--meta-mode', options.metaMode);
  const raw = runPython('inspect_db.py', args);
  return JSON.parse(raw.trim()) as CanonicalSnapshot;
}

export function inspectZeroMetaWrites(): {
  offending: Array<{ id: string; method: string }>;
} {
  const raw = runPython('inspect_db.py', ['zero-meta-writes']);
  return JSON.parse(raw.trim()) as {
    offending: Array<{ id: string; method: string }>;
  };
}

// ---------------------------------------------------------------------------
// JSON reader
// ---------------------------------------------------------------------------

export async function readJson<T>(
  response: APIResponse | Response
): Promise<T> {
  if (!response.ok()) {
    throw new Error(
      `Domain request failed (${response.status()}): ${await response.text()}`
    );
  }
  return (await response.json()) as T;
}

// ---------------------------------------------------------------------------
// Localization batch helpers (delegated from localization/fixtures.ts patterns)
// ---------------------------------------------------------------------------

export interface LocalizationBatchPayload {
  id: string;
  revision: number;
  phase: string;
  outcome?: string | null;
  product_version_id: string;
  offer_version_id: string;
  truth_package_id: string;
  sources: Array<{ inventory_id: string }>;
  markets: Array<{
    market: string;
    readiness: string;
  }>;
  items: Array<{
    id: string;
    state: string;
    target_market: string;
    attempt_count: number;
    reason_code?: string | null;
  }>;
}

export interface LocalizationItemPayload {
  id: string;
  revision: number;
  state: string;
  target_market: string;
  reason_code?: string | null;
  attempt_count: number;
  max_attempts: number;
  attempts: Array<{
    id: string;
    attempt_number: number;
    state: string;
    qc: Array<{ check_kind: string; result: string; severity: string }>;
    output?: { id: string; content_sha256: string } | null;
    publication?: {
      id: string;
      publication_kind: string;
      asset_version_id: string;
    } | null;
  }>;
  cost: unknown;
  lineage: Record<string, unknown>;
  binding: unknown;
}

export function itemDurabilitySnapshot(item: LocalizationItemPayload) {
  return {
    id: item.id,
    state: item.state,
    attempt_count: item.attempt_count,
    attempts: item.attempts,
    cost: item.cost,
    lineage: item.lineage,
    binding: item.binding,
  };
}

export async function createLocalizationBatchThroughUi(
  page: Page,
  seed: LocalizationFromDecisionSeed,
  targets: LocalizationMarketSeed[]
): Promise<string> {
  await page.goto(LOCALIZATION_URL);
  await expect(
    page.getByRole('heading', { name: 'Localization control room' })
  ).toBeVisible();

  const newBatch = page
    .getByRole('heading', { name: 'New batch' })
    .locator('xpath=ancestor::section');
  await newBatch
    .getByLabel('Winner / offer / source')
    .selectOption(seed.assetVersionId);

  const selectedMarkets = targets.map((t) => t.market);
  for (const target of targets) {
    await newBatch
      .locator('label')
      .filter({ hasText: `${target.market} ·` })
      .getByRole('checkbox')
      .check();
  }
  for (const checkbox of await newBatch.getByRole('checkbox').all()) {
    const labelText = await checkbox.evaluate(
      (el) => el.parentElement?.textContent?.trim() ?? ''
    );
    if (
      !selectedMarkets.some((m) => labelText.startsWith(`${m} ·`)) &&
      (await checkbox.isChecked())
    ) {
      await checkbox.uncheck();
    }
  }

  const [createResponse] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.request().method() === 'POST' &&
        new URL(r.url()).pathname === `${LOCALIZATION_BFF}/batches`
    ),
    newBatch
      .getByRole('button', { name: 'Create Controlled Mock batch' })
      .click(),
  ]);
  const created = await readJson<{ batch_id: string }>(createResponse);
  expect(created.batch_id).toMatch(/^lb_/);
  return created.batch_id;
}

export async function prepareLocalizationBatchThroughUi(
  page: Page,
  seed: LocalizationFromDecisionSeed,
  batchId: string,
  pairCount: number
): Promise<string> {
  const preparation = page
    .getByRole('heading', { name: 'Truth and claim preparation' })
    .locator('xpath=ancestor::section');
  await preparation
    .getByLabel('Truth policy version')
    .selectOption(seed.truthPolicyVersionId);
  await preparation
    .getByLabel('Fact data source version')
    .selectOption(seed.dataSourceVersionId);

  const [startResponse] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.request().method() === 'POST' &&
        new URL(r.url()).pathname ===
          `${LOCALIZATION_BFF}/batches/${batchId}/start-preparation`
    ),
    preparation
      .getByRole('button', { name: 'Resolve Truth and analyze sources' })
      .click(),
  ]);
  const started = await readJson<{ truth_package_id: string }>(startResponse);
  expect(started.truth_package_id).toMatch(/^tp_/);
  await expect(
    preparation.getByText(
      new RegExp(`^Inventory ${seed.inventoryId} · evidence `)
    )
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
            `${LOCALIZATION_BFF}/batches/${batchId}/claim-decisions`
      ),
      submit.click(),
    ]);
    await readJson<{ decision_set_id: string }>(decisionResponse);
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
    await Promise.all([
      page.waitForResponse(
        (r) =>
          r.request().method() === 'POST' &&
          new URL(r.url()).pathname ===
            `${LOCALIZATION_BFF}/batches/${batchId}/approve-copy`
      ),
      approve.click(),
    ]);
  }
  await expect(
    page.getByRole('button', { name: 'Confirm and create items' })
  ).toBeEnabled({ timeout: 60_000 });
  return started.truth_package_id;
}

export async function confirmLocalizationBatchThroughUi(
  page: Page,
  batchId: string
): Promise<{ batch_id: string; operation_id: string; item_ids: string[] }> {
  const [confirmResponse] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.request().method() === 'POST' &&
        new URL(r.url()).pathname ===
          `${LOCALIZATION_BFF}/batches/${batchId}/confirm`
    ),
    page.getByRole('button', { name: 'Confirm and create items' }).click(),
  ]);
  return readJson<{
    batch_id: string;
    operation_id: string;
    item_ids: string[];
  }>(confirmResponse);
}

// ---------------------------------------------------------------------------
// Scale evidence advancement (reuses scale/seed_prerequisites.py)
// ---------------------------------------------------------------------------

export interface ManifestAdvanceResult {
  runId: string;
  outcomeKind: string;
  evaluationId: string | null;
  adviceThreadIds: string[];
  maturity: string | null;
  health: string | null;
}

export function advanceManifest(options: {
  metaMode: string;
  enrollmentId: string;
  episodeId: string;
  policyVersionId: string;
  dayIndex: number;
  kind?: 'complete' | 'blocked';
  targetVersionId?: string;
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
  if (options.targetVersionId)
    args.push('--target-version-id', options.targetVersionId);
  if (options.baselineMetrics)
    args.push(
      '--baseline-metrics-json',
      JSON.stringify(options.baselineMetrics)
    );
  const raw = execFileSync(PYTHON, [script, ...args], {
    cwd: REPOSITORY_ROOT,
    env: { ...process.env, PYTHONPATH: REPOSITORY_ROOT },
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'inherit'],
  });
  return JSON.parse(raw.trim()) as ManifestAdvanceResult;
}

// ---------------------------------------------------------------------------
// Experiment ad-metrics seeding
// ---------------------------------------------------------------------------

export function seedAdMetrics(
  workspaceId: string,
  candidates: Array<{
    adAccountId: string;
    adId: string;
    dailyImpressions: number;
    dailyClicks: number;
  }>,
  days = 7
): { seeded_rows: number; start_day: string; end_day: string } {
  const script = path.resolve(
    __dirname,
    '..',
    'experiments',
    'seed_ad_metrics.py'
  );
  const raw = execFileSync(
    PYTHON,
    [
      script,
      '--workspace-id',
      workspaceId,
      '--days',
      String(days),
      '--candidates-json',
      JSON.stringify(
        candidates.map((c) => ({
          ad_account_id: c.adAccountId,
          ad_id: c.adId,
          daily_impressions: c.dailyImpressions,
          daily_clicks: c.dailyClicks,
        }))
      ),
    ],
    {
      cwd: REPOSITORY_ROOT,
      env: { ...process.env, PYTHONPATH: REPOSITORY_ROOT },
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'inherit'],
    }
  );
  return JSON.parse(raw.trim()) as {
    seeded_rows: number;
    start_day: string;
    end_day: string;
  };
}

// ---------------------------------------------------------------------------
// Experiment launch/operation helpers
// ---------------------------------------------------------------------------

export async function waitForOperationState(
  page: Page,
  provisioningId: string,
  expectedState: string,
  timeoutMs = 30_000
): Promise<Record<string, unknown>> {
  const deadline = Date.now() + timeoutMs;
  let last: Record<string, unknown> | undefined;
  while (Date.now() < deadline) {
    const response = await page.request.get(
      `/api/domain/v1/w/${WORKSPACE_SLUG}/launch/operations/${provisioningId}`
    );
    if (response.ok()) {
      last = (await response.json()) as Record<string, unknown>;
      if (last.operation_state === expectedState) return last;
    }
    await page.waitForTimeout(500);
  }
  throw new Error(
    `Operation ${provisioningId} never reached ${expectedState}. Last: ${JSON.stringify(last)}`
  );
}

export function inspectFakeMetaObjects(
  workspaceId: string,
  metaMode: string,
  scenarioVersionId: string
): Array<{
  object_type: string;
  remote_object_id: string;
  state: Record<string, unknown>;
}> {
  const script = path.resolve(__dirname, '..', 'experiments', 'inspect_db.py');
  const raw = execFileSync(
    PYTHON,
    [
      script,
      'fake-meta-objects',
      '--workspace-id',
      workspaceId,
      '--meta-mode',
      metaMode,
      '--scenario-version-id',
      scenarioVersionId,
    ],
    {
      cwd: REPOSITORY_ROOT,
      env: { ...process.env, PYTHONPATH: REPOSITORY_ROOT },
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'inherit'],
    }
  );
  return JSON.parse(raw.trim());
}

// ---------------------------------------------------------------------------
// Docker compose service control
// ---------------------------------------------------------------------------

function composeService(
  action: 'restart' | 'stop' | 'start',
  service: 'api' | 'worker'
): void {
  const project = process.env.E2E_CROSS_AREA_COMPOSE_PROJECT?.trim();
  if (!project) {
    throw new Error('Missing E2E_CROSS_AREA_COMPOSE_PROJECT');
  }
  const result = spawnSync(
    'docker',
    ['compose', '-p', project, action, service],
    { cwd: REPOSITORY_ROOT, env: process.env, encoding: 'utf8' }
  );
  if (result.status !== 0) {
    throw new Error(
      `${service} ${action} failed (${result.status}): ${result.stderr || result.stdout}`
    );
  }
}

export async function restartApiServer(): Promise<void> {
  composeService('restart', 'api');
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`${API_BASE}/healthz`);
      if (res.ok) return;
    } catch {
      /* retry */
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  throw new Error('API server did not become healthy in time');
}

export function restartWorker(): void {
  composeService('restart', 'worker');
}

// ---------------------------------------------------------------------------
// Zero-Meta-writes page-level network spy
// ---------------------------------------------------------------------------

const META_WRITE_HOSTS = ['graph.facebook.com', 'graph.instagram.com'];
const META_WRITE_METHODS = new Set(['POST', 'PUT', 'DELETE', 'PATCH']);

export interface NetworkSpy {
  offendingRequests: Request[];
}

export function installNetworkSpy(page: Page): NetworkSpy {
  const spy: NetworkSpy = { offendingRequests: [] };
  page.on('request', (request) => {
    const method = request.method().toUpperCase();
    if (!META_WRITE_METHODS.has(method)) return;
    let host = '';
    try {
      host = new URL(request.url()).host;
    } catch {
      return;
    }
    if (META_WRITE_HOSTS.some((h) => host.endsWith(h))) {
      spy.offendingRequests.push(request);
    }
  });
  return spy;
}

// ---------------------------------------------------------------------------
// Test fixture with NetworkSpy
// ---------------------------------------------------------------------------

interface CrossAreaFixtures {
  networkSpy: NetworkSpy;
}

export const test = base.extend<CrossAreaFixtures>({
  networkSpy: async ({ page }, provide) => {
    const spy = installNetworkSpy(page);
    await provide(spy);
  },
});
