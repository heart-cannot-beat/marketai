import path from 'path';
import { test as base, expect } from '@playwright/test';
import type {
  APIResponse,
  Browser,
  BrowserContext,
  Response,
} from '@playwright/test';

export { expect };
export {
  API_BASE,
  seedControlledMockChain,
  seedCustomerSandboxChain,
  advanceManifest,
  inspectEnrollment,
  inspectEpisode,
  inspectEvaluations,
  inspectAdviceThreads,
  inspectTarget,
  inspectExport,
  inspectCommandJournalEntry,
  inspectZeroMetaWrites,
  computeCommandIdentityHash,
  deriveBffIdempotencyKey,
  claimWorkerCheckpoint,
  restartScaleApiServer,
  restartScaleWorker,
  stopScaleWorker,
  startScaleWorker,
  runOneScaleWorkerJob,
  installNetworkSpy,
} from './helpers';
export type {
  ScaleChainSeed,
  ManifestAdvanceResult,
  AdvanceManifestOptions,
  WorkerCheckpointResult,
  NetworkSpy,
} from './helpers';

import { installNetworkSpy, type NetworkSpy } from './helpers';

/**
 * Scale browser coverage (issue #87 Task 9) reads the live domain API,
 * exactly like `web/e2e/experiments/fixtures.ts` and
 * `web/e2e/localization/fixtures.ts`. This suite never intercepts
 * `/api/domain/**` and never manufactures Enrollment, Episode, evidence,
 * Advice, Target, or Export state in browser memory -- the whole point is
 * to exercise the real Scale UI end to end against real PostgreSQL and the
 * real `FakeMetaAdapter`, replacing the documented-but-unexecuted Python
 * checklists this task deletes
 * (`tests/e2e/test_scale_controlled_mock_journey.py`,
 * `tests/e2e/test_scale_customer_sandbox_smoke.py` -- see their module
 * docstrings, removed in the immediately preceding commit).
 *
 * A dedicated environment must seed the Winner/DeliveryTarget/Policy
 * prerequisites below through the real FastAPI/PostgreSQL command path
 * before running Playwright -- see `seed_prerequisites.py`. The
 * pre-authenticated `chromium` Playwright project (`storageState:
 * 'e2e/.auth/owner.json'`, produced by `e2e/workspace/fixtures.ts`'s
 * `setup` project -- see `playwright.config.ts`) still supplies the
 * signed-in owner session; this suite seeds *into* that owner's Workspace
 * rather than creating a new one, mirroring every other real-API E2E
 * suite in this repository.
 */
export const WORKSPACE_SLUG =
  process.env.E2E_SCALE_WORKSPACE_SLUG ?? 'workspace-a';
export const WORKSPACE_ID = process.env.E2E_SCALE_WORKSPACE_ID!;
export const USER_ID = process.env.E2E_OWNER_USER_ID!;

/**
 * A Scale page reads its Meta mode from `?meta_mode=` and falls back to
 * `customer_sandbox` (`lib/scale/meta-mode.ts`), so a page for any other
 * mode has to name it.
 */
function inMode(pagePath: string, metaMode?: string): string {
  return metaMode ? `${pagePath}?meta_mode=${metaMode}` : pagePath;
}

export function scaleUrl(metaMode?: string): string {
  return inMode(`/w/${WORKSPACE_SLUG}/scale`, metaMode);
}

export function scaleEnrollmentUrl(
  enrollmentId: string,
  metaMode?: string,
  view?: 'evidence'
): string {
  const enrollmentPath = `/w/${WORKSPACE_SLUG}/scale/${enrollmentId}`;
  return inMode(view ? `${enrollmentPath}/${view}` : enrollmentPath, metaMode);
}

export function scaleTargetsUrl(metaMode?: string): string {
  return inMode(`/w/${WORKSPACE_SLUG}/scale/targets`, metaMode);
}

export function scaleBff(metaMode: string): string {
  return `/api/domain/v1/w/${WORKSPACE_SLUG}/scale/${metaMode}`;
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

const OWNER_STORAGE_STATE = path.resolve(
  __dirname,
  '..',
  '.auth',
  'owner.json'
);

/**
 * Opens a fresh `BrowserContext` carrying the same pre-authenticated owner
 * session the `chromium` Playwright project already applies to the ambient
 * `page` fixture. Exists for specs needing a second, independent browsing
 * context -- e.g. step 11's "dismiss then snooze survive refresh" or
 * step 18's cross-Workspace/cross-mode hostile-action checks.
 */
export async function createAuthenticatedContext(
  browser: Browser
): Promise<BrowserContext> {
  return browser.newContext({ storageState: OWNER_STORAGE_STATE });
}

interface ScaleFixtures {
  /** Navigates straight to the Scale Enrollment list for the seeded
   * Workspace and waits for the page heading, matching every other
   * `*Page` fixture in this repository's real-API E2E suites. */
  scalePage: import('@playwright/test').Page;
  /** Installs a Meta-write network spy on the ambient `page` before any
   * navigation happens, so it observes the whole test from the first
   * request onward. */
  networkSpy: NetworkSpy;
}

export const test = base.extend<ScaleFixtures>({
  scalePage: async ({ page }, provide) => {
    await page.goto(scaleUrl());
    await provide(page);
  },
  networkSpy: async ({ page }, provide) => {
    const spy = installNetworkSpy(page);
    await provide(spy);
  },
});
