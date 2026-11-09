import { execFileSync, spawnSync } from 'child_process';
import path from 'path';
import { test, expect } from '@playwright/test';
import type { APIResponse, Page, Response } from '@playwright/test';
import { en } from '../support/copy';

export { test, expect };

/**
 * Localization browser coverage intentionally reads the live domain API.
 * The suite never intercepts `/api/domain/**` and never manufactures Batch,
 * Truth, Inventory, QC, cost, or binding state in browser memory.
 *
 * A dedicated environment must seed these immutable ids through the real
 * FastAPI/PostgreSQL command path before running Playwright.  API integration
 * coverage for the same persistence invariants lives in
 * `tests/test_v1_localization_routes.py` and runs without this external seed.
 */
export const WORKSPACE_SLUG =
  process.env.E2E_LOCALIZATION_WORKSPACE_SLUG ?? 'workspace-a';
export const LOCALIZATION_URL = `/w/${WORKSPACE_SLUG}/localization`;
export const LOCALIZATION_BFF = `/api/domain/v1/w/${WORKSPACE_SLUG}/localization`;

export interface LocalizationMarketSeed {
  market: string;
  country: string;
  expectedReadiness: 'ready' | 'degraded' | 'unavailable';
  mockScenario: string;
  marketPolicyVersionId: string;
  failsLockedCopyQc: boolean;
}

export interface LocalizationPrerequisites {
  assetVersionId: string;
  decisionId: string;
  foreignWorkspaceId: string;
  foreignWorkspaceSlug: string;
  inventoryId: string;
  market: string;
  marketPolicyVersionId: string;
  truthPolicyVersionId: string;
  dataSourceVersionId: string;
  markets: LocalizationMarketSeed[];
}

export interface LocalizationBatchPayload {
  id: string;
  revision: number;
  phase: string;
  outcome?: string | null;
  product_version_id: string;
  offer_version_id: string;
  truth_package_id: string;
  truth_freshness?: string | null;
  sources: Array<{ inventory_id: string }>;
  markets: Array<{
    market: string;
    readiness: string;
    readiness_detail?: unknown;
    facts?: Array<{ fact_type: string }>;
    source_attempts?: Array<{
      fact_type: string;
      outcome: string;
      failure_code?: string | null;
      skip_code?: string | null;
    }>;
  }>;
  commands?: Array<{
    id: string;
    command_kind: string;
    operation_id?: string | null;
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
  allowed_actions?: Array<{
    action: string;
    allowed: boolean;
    reason_code?: string | null;
  }>;
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

export interface LocalizationConfirmPayload {
  batch_id: string;
  operation_id: string;
  item_ids: string[];
}

interface WithdrawnTruthPolicyVersion {
  action: 'withdraw_truth_policy_version';
  entryType: 'truth_resolution_policy';
  lifecycleRevision: number;
  previousState: string;
  state: 'withdrawn';
  versionId: string;
  workspaceId: string;
}

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

export function seedLocalizationPrerequisites(): LocalizationPrerequisites {
  const repositoryRoot = path.resolve(__dirname, '..', '..', '..');
  const python = process.env.E2E_PYTHON ?? 'python3';
  const script = path.join(__dirname, 'seed_prerequisites.py');
  const raw = execFileSync(python, [script], {
    cwd: repositoryRoot,
    env: { ...process.env, PYTHONPATH: repositoryRoot },
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'inherit'],
  });
  const seed = JSON.parse(raw) as Partial<LocalizationPrerequisites>;
  const missing = [
    'assetVersionId',
    'decisionId',
    'foreignWorkspaceId',
    'foreignWorkspaceSlug',
    'inventoryId',
    'market',
    'marketPolicyVersionId',
    'truthPolicyVersionId',
    'dataSourceVersionId',
    'markets',
  ].filter((field) => !seed[field as keyof LocalizationPrerequisites]);
  if (missing.length > 0) {
    throw new Error(
      `Localization prerequisite seed omitted: ${missing.join(', ')}`
    );
  }
  return seed as LocalizationPrerequisites;
}

export function withdrawTruthPolicyVersion(
  seed: LocalizationPrerequisites
): WithdrawnTruthPolicyVersion {
  const repositoryRoot = path.resolve(__dirname, '..', '..', '..');
  const python = process.env.E2E_PYTHON ?? 'python3';
  const script = path.join(__dirname, 'seed_prerequisites.py');
  const raw = execFileSync(
    python,
    [script, 'withdraw-version', '--version-id', seed.truthPolicyVersionId],
    {
      cwd: repositoryRoot,
      env: { ...process.env, PYTHONPATH: repositoryRoot },
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'inherit'],
    }
  );
  const withdrawn = JSON.parse(raw) as WithdrawnTruthPolicyVersion;
  if (
    withdrawn.action !== 'withdraw_truth_policy_version' ||
    withdrawn.entryType !== 'truth_resolution_policy' ||
    withdrawn.state !== 'withdrawn' ||
    withdrawn.versionId !== seed.truthPolicyVersionId ||
    withdrawn.workspaceId !== process.env.E2E_LOCALIZATION_WORKSPACE_ID
  ) {
    throw new Error(`Unexpected guarded withdrawal result: ${raw}`);
  }
  return withdrawn;
}

function composeWorker(action: 'restart' | 'stop' | 'start'): void {
  const project = process.env.E2E_LOCALIZATION_COMPOSE_PROJECT?.trim();
  if (!project) {
    throw new Error('Missing E2E_LOCALIZATION_COMPOSE_PROJECT');
  }
  const repositoryRoot = path.resolve(__dirname, '..', '..', '..');
  const result = spawnSync(
    'docker',
    ['compose', '-p', project, action, 'worker'],
    { cwd: repositoryRoot, env: process.env, encoding: 'utf8' }
  );
  if (result.status !== 0) {
    throw new Error(
      `Worker ${action} failed (${result.status}): ${result.stderr || result.stdout}`
    );
  }
}

/**
 * Process at most one durable Job in a disposable real Worker container.
 *
 * The recovery spec uses this while the long-lived Worker service is stopped.
 * A one-job process gives the test a deterministic boundary after the edit
 * output is durable and before a separately admitted QC Job can run.  This is
 * still the production Worker entry point against the real PostgreSQL queue;
 * only its existing `--once` lifecycle is different.
 */
export function runOneLocalizationWorkerJob(): void {
  const project = process.env.E2E_LOCALIZATION_COMPOSE_PROJECT?.trim();
  if (!project) {
    throw new Error('Missing E2E_LOCALIZATION_COMPOSE_PROJECT');
  }
  const repositoryRoot = path.resolve(__dirname, '..', '..', '..');
  const result = spawnSync(
    'docker',
    [
      'compose',
      '-p',
      project,
      'run',
      '--rm',
      '--no-deps',
      'worker',
      'python',
      '-m',
      'worker.main',
      '--once',
    ],
    { cwd: repositoryRoot, env: process.env, encoding: 'utf8' }
  );
  if (result.status !== 0) {
    throw new Error(
      `One-job Worker failed (${result.status}): ${result.stderr || result.stdout}`
    );
  }
}

export function restartLocalizationWorker(): void {
  composeWorker('restart');
}

/**
 * Stop and start the real Worker container.
 *
 * The isolated-output recovery spec needs a window in which no Worker can
 * advance the pipeline: it has to observe an Attempt whose external call
 * completed and then cancel the Batch before the frozen QC plan finishes
 * and publishes. Stopping the container is the only way to get that window
 * without mocking a route, which this suite never does.
 */
export function stopLocalizationWorker(): void {
  composeWorker('stop');
}

export function startLocalizationWorker(): void {
  composeWorker('start');
}

/**
 * Poll the live item projection until `predicate` holds, at the tightest
 * interval the BFF will take. `expect.poll` is the right tool everywhere
 * the test only *observes*; this exists for the one place that must act
 * (stop the Worker) at the instant the condition first becomes true.
 */
export async function pollItemUntil(
  page: Page,
  batchId: string,
  itemId: string,
  predicate: (item: LocalizationItemPayload) => boolean,
  {
    timeoutMs = 120_000,
    intervalMs = 150,
  }: { timeoutMs?: number; intervalMs?: number } = {}
): Promise<LocalizationItemPayload> {
  const deadline = Date.now() + timeoutMs;
  let last: LocalizationItemPayload | undefined;
  while (Date.now() < deadline) {
    last = await readJson<LocalizationItemPayload>(
      await page.request.get(
        `${LOCALIZATION_BFF}/batches/${batchId}/items/${itemId}`
      )
    );
    if (predicate(last)) return last;
    await page.waitForTimeout(intervalMs);
  }
  throw new Error(
    `Item ${itemId} never satisfied the predicate. Last seen: ${JSON.stringify(last)}`
  );
}

export async function createBatchThroughUi(
  page: Page,
  seed: LocalizationPrerequisites,
  targets: LocalizationMarketSeed[]
): Promise<string> {
  await page.goto(LOCALIZATION_URL);
  await expect(
    page.getByRole('heading', { name: 'Localization control room' })
  ).toBeVisible();

  const newBatch = page
    .getByRole('heading', { name: 'New batch' })
    .locator('xpath=ancestor::section');
  // An option's value is its Winner Decision; pick the one that shows the
  // seeded AssetVersion, as the golden path does.
  const source = newBatch.getByLabel(
    en('localization', 'new-batch.source-label')
  );
  const choice = source
    .locator('option')
    .filter({ hasText: seed.assetVersionId });
  await expect(choice).toHaveCount(1);
  await source.selectOption((await choice.getAttribute('value'))!);

  const selectedMarkets = targets.map((target) => target.market);
  for (const target of targets) {
    await newBatch
      .locator('label')
      .filter({ hasText: `${target.market} ·` })
      .getByRole('checkbox')
      .check();
  }
  for (const checkbox of await newBatch.getByRole('checkbox').all()) {
    const labelText = await checkbox.evaluate(
      (element) => element.parentElement?.textContent?.trim() ?? ''
    );
    if (
      !selectedMarkets.some((market) => labelText.startsWith(`${market} ·`)) &&
      (await checkbox.isChecked())
    ) {
      await checkbox.uncheck();
    }
  }

  const [createResponse] = await Promise.all([
    page.waitForResponse(
      (response) =>
        response.request().method() === 'POST' &&
        new URL(response.url()).pathname === `${LOCALIZATION_BFF}/batches`
    ),
    newBatch
      .getByRole('button', { name: 'Create Controlled Mock batch' })
      .click(),
  ]);
  const created = await readJson<{ batch_id: string }>(createResponse);
  expect(created.batch_id).toMatch(/^lb_/);
  await expect(page).toHaveURL(
    new RegExp(`[?&]batch=${created.batch_id}(?:&|$)`)
  );
  return created.batch_id;
}

export async function prepareBatchThroughUi(
  page: Page,
  seed: LocalizationPrerequisites,
  batchId: string,
  pairCount: number
): Promise<string> {
  const preparation = page
    .getByRole('heading', { name: 'Truth and claim preparation' })
    .locator('xpath=ancestor::section');
  // The policy's source_priority names the DataSource (#70); there is no
  // separate data source choice.
  await preparation
    .getByLabel('Truth policy version')
    .selectOption(seed.truthPolicyVersionId);

  const [startResponse] = await Promise.all([
    page.waitForResponse(
      (response) =>
        response.request().method() === 'POST' &&
        new URL(response.url()).pathname ===
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

  for (let index = 0; index < pairCount; index += 1) {
    const submit = preparation.getByRole('button', {
      name: 'Submit immutable Claim decisions',
    });
    await expect(submit).toBeEnabled({ timeout: 60_000 });
    const [decisionResponse] = await Promise.all([
      page.waitForResponse(
        (response) =>
          response.request().method() === 'POST' &&
          new URL(response.url()).pathname ===
            `${LOCALIZATION_BFF}/batches/${batchId}/claim-decisions`
      ),
      submit.click(),
    ]);
    const decision = await readJson<{ decision_set_id: string }>(
      decisionResponse
    );
    expect(decision.decision_set_id).toMatch(/^lcds_/);
    await expect(
      preparation.getByText(new RegExp(`\\b${index + 1} decision sets?\\b`))
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
  for (let index = 0; index < pairCount; index += 1) {
    const approve = localizedCopy
      .locator('button:enabled')
      .filter({ hasText: 'Approve copy' })
      .first();
    await expect(approve).toBeVisible({ timeout: 60_000 });
    const [approveResponse] = await Promise.all([
      page.waitForResponse(
        (response) =>
          response.request().method() === 'POST' &&
          new URL(response.url()).pathname ===
            `${LOCALIZATION_BFF}/batches/${batchId}/approve-copy`
      ),
      approve.click(),
    ]);
    await readJson<unknown>(approveResponse);
    await expect(
      localizedCopy
        .locator('button:enabled')
        .filter({ hasText: 'Approve copy' })
    ).toHaveCount(pairCount - index - 1, { timeout: 60_000 });
  }
  await expect(
    page.getByRole('button', { name: 'Confirm and create items' })
  ).toBeEnabled({ timeout: 60_000 });
  return started.truth_package_id;
}

export async function confirmBatchThroughUi(
  page: Page,
  batchId: string
): Promise<LocalizationConfirmPayload> {
  const [confirmResponse] = await Promise.all([
    page.waitForResponse(
      (response) =>
        response.request().method() === 'POST' &&
        new URL(response.url()).pathname ===
          `${LOCALIZATION_BFF}/batches/${batchId}/confirm`
    ),
    page.getByRole('button', { name: 'Confirm and create items' }).click(),
  ]);
  return readJson<LocalizationConfirmPayload>(confirmResponse);
}
