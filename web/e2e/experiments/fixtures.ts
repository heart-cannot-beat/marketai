import { execFileSync } from 'child_process';
import { randomUUID } from 'crypto';
import path from 'path';
import { test, expect } from '@playwright/test';
import type {
  APIResponse,
  Browser,
  BrowserContext,
  Page,
  Response,
} from '@playwright/test';

export { test, expect };
export {
  API_BASE,
  inspectFakeMetaObjects,
  inspectCallIntents,
  claimWorkerCheckpoint,
  restartApiServer,
  restartWorker,
  stopWorker,
  startWorker,
  waitForOperationState,
} from './helpers';
export type {
  CallIntent,
  FakeMetaObject,
  WorkerCheckpointResult,
} from './helpers';

/**
 * Experiments browser coverage (issue #88: duplicate resolution + PE) reads
 * the live domain API, exactly like `web/e2e/localization/fixtures.ts`. This
 * suite never intercepts `/api/domain/**` and never manufactures Experiment,
 * duplicate-group, launch-preview, or provisioning state in browser memory
 * -- the whole point of replacing the route-mocked `golden-path.spec.ts`
 * (deleted alongside this file) is to exercise the real freeze /
 * duplicate-resolution / launch path end to end against real PostgreSQL and
 * the real `FakeMetaAdapter`.
 *
 * A dedicated environment must seed the Product/AssetVersion/MetaConnection
 * prerequisites below through the real FastAPI/PostgreSQL command path
 * before running Playwright -- see `seed_prerequisites.py`. The
 * pre-authenticated `chromium` Playwright project
 * (`storageState: 'e2e/.auth/owner.json'`, produced by
 * `e2e/workspace/fixtures.ts`'s `setup` project -- see
 * `playwright.config.ts`) still supplies the signed-in owner session; this
 * suite seeds *into* that owner's Workspace rather than creating a new one,
 * mirroring the Localization suite.
 */
export const WORKSPACE_SLUG =
  process.env.E2E_EXPERIMENTS_WORKSPACE_SLUG ?? 'workspace-a';
export const WORKSPACE_ID = process.env.E2E_EXPERIMENTS_WORKSPACE_ID!;
export const USER_ID = process.env.E2E_OWNER_USER_ID!;
export const EXPERIMENTS_URL = `/w/${WORKSPACE_SLUG}/legacy-experiments`;
export const EXPERIMENTS_BFF = `/api/domain/v1/w/${WORKSPACE_SLUG}/experiments`;

export interface ExperimentsPrerequisites {
  workspaceId: string;
  workspaceSlug: string;
  userId: string;
  /** Random per-run suffix every id below is scoped by, e.g. `prod_${tag}`. */
  tag: string;
  productId: string;
  productVersionId: string;
  offerVersionId: string;
  /** All three seeded AssetVersions, in creation order. */
  assetVersionIds: string[];
  /** The two AssetVersions that share one DimensionConceptKey -- the real
   * freeze-time duplicate pair `analyze_duplicates`/`freeze_experiment_v2`
   * must detect. */
  duplicatePairAssetVersionIds: string[];
  /** The one AssetVersion with a distinct concept key -- an independent,
   * non-duplicate candidate. */
  independentAssetVersionId: string;
  sharedConceptKeyId: string;
  distinctConceptKeyId: string;
  customerSandboxConnectionId: string;
  customerSandboxScenarioVersionId: string;
  /** The ad account the seeded binding covers; Launch must use this one. */
  customerSandboxAdAccountId: string;
  controlledMockConnectionId: string;
  controlledMockScenarioVersionId: string;
  controlledMockAdAccountId: string;
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

const APP_ORIGIN = new URL(process.env.E2E_BASE_URL ?? 'http://localhost:4002')
  .origin;

/**
 * Headers the BFF requires on every non-safe `/api/domain/**` request
 * (`pages/api/domain/v1/w/[slug]/[...path].ts`): a same-origin `Origin`,
 * the session's CSRF token, and a fresh `X-Intent-Id` the BFF derives the
 * FastAPI idempotency key from. Mirrors `domainMutationHeaders` in
 * `e2e/golden-path/fixtures.ts`, except the origin comes from the configured
 * base URL because specs create their Experiments before the first
 * navigation.
 */
/**
 * Ticks the freeze acknowledgement once the duplicate analysis has loaded.
 * `ExperimentsWorkspace` clears the box whenever a new analysis snapshot
 * arrives, so a tick made right after navigation can be lost and leave
 * "Freeze experiment" disabled; retry until the button is enabled.
 */
export async function acknowledgeFreeze(page: Page): Promise<void> {
  const checkbox = page
    .locator('label')
    .filter({
      hasText:
        'I reviewed the candidates, budget, schedule, and immutable context.',
    })
    .locator('input[type="checkbox"]');
  const freezeButton = page.getByRole('button', { name: 'Freeze experiment' });
  await expect(async () => {
    await checkbox.check();
    await expect(freezeButton).toBeEnabled({ timeout: 1_000 });
  }).toPass({ timeout: 30_000 });
}

/**
 * Waits for the frozen view after a freeze. Freezing bumps the revision,
 * and `ExperimentsWorkspace` keys the detail panel by id + revision, so
 * panel state set before the remount (such as the Evidence mode select) is
 * lost.
 */
export async function expectFrozenPanel(page: Page): Promise<void> {
  await expect(
    page.getByRole('heading', { name: 'Concept Resolution', exact: true })
  ).toBeVisible();
}

export async function domainMutationHeaders(
  page: Page
): Promise<Record<string, string>> {
  const { token } = await readJson<{ token: string }>(
    await page.request.get('/api/csrf')
  );
  return {
    Origin: APP_ORIGIN,
    'X-CSRF-Token': token,
    'X-Intent-Id': randomUUID(),
  };
}

const REQUIRED_PREREQUISITE_FIELDS: Array<keyof ExperimentsPrerequisites> = [
  'workspaceId',
  'workspaceSlug',
  'userId',
  'tag',
  'productId',
  'productVersionId',
  'offerVersionId',
  'assetVersionIds',
  'duplicatePairAssetVersionIds',
  'independentAssetVersionId',
  'sharedConceptKeyId',
  'distinctConceptKeyId',
  'customerSandboxConnectionId',
  'customerSandboxScenarioVersionId',
  'customerSandboxAdAccountId',
  'controlledMockConnectionId',
  'controlledMockScenarioVersionId',
  'controlledMockAdAccountId',
];

/**
 * Seeds one fresh, tag-scoped set of Experiments prerequisites by shelling
 * out to `seed_prerequisites.py` (see that file for the exact records it
 * creates). Every id returned is unique to this call, so concurrent or
 * repeated runs never collide -- call this once per test (or per spec file)
 * rather than sharing state across runs.
 *
 * `faultConfig`, when given, becomes the `controlled_mock` ScenarioVersion's
 * `scenario_config.faults`, which `resolve_adapter` hands to
 * `app.adapters.meta.fake_adapter.FakeMetaAdapter` -- the `{"rules": [...]}` shape
 * (e.g. `{"rules": [{"object_type": "campaign", "match_ordinal": 1,
 * "effect": "uncertain_before_apply"}]}`).
 */
export function seedPrerequisites(
  faultConfig?: Record<string, unknown>
): ExperimentsPrerequisites {
  const repositoryRoot = path.resolve(__dirname, '..', '..', '..');
  const python = process.env.E2E_PYTHON ?? 'python3';
  const script = path.join(__dirname, 'seed_prerequisites.py');
  const args = faultConfig
    ? [script, '--fault-config', JSON.stringify(faultConfig)]
    : [script];
  const raw = execFileSync(python, args, {
    cwd: repositoryRoot,
    env: { ...process.env, PYTHONPATH: repositoryRoot },
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'inherit'],
  });
  const seed = JSON.parse(raw.trim()) as Partial<ExperimentsPrerequisites>;
  const missing = REQUIRED_PREREQUISITE_FIELDS.filter((field) => {
    const value = seed[field];
    return (
      value === undefined ||
      value === null ||
      value === '' ||
      (Array.isArray(value) && value.length === 0)
    );
  });
  if (missing.length > 0) {
    throw new Error(
      `Experiments prerequisite seed omitted: ${missing.join(', ')}`
    );
  }
  return seed as ExperimentsPrerequisites;
}

const OWNER_STORAGE_STATE = path.resolve(
  __dirname,
  '..',
  '.auth',
  'owner.json'
);

/**
 * Opens a fresh `BrowserContext` carrying the same pre-authenticated owner
 * session the `chromium` Playwright project already applies to the ambient
 * `page` fixture (`storageState: 'e2e/.auth/owner.json'`). Most specs should
 * just use `page` directly; this exists for specs that need a second,
 * independent browsing context -- e.g. two tabs open on the same Experiment
 * to observe a freeze/launch mutation made in one reflected in the other.
 */
export async function createAuthenticatedContext(
  browser: Browser
): Promise<BrowserContext> {
  return browser.newContext({ storageState: OWNER_STORAGE_STATE });
}
