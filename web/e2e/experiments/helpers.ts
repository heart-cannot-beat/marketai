import { execFileSync, spawnSync } from 'child_process';
import path from 'path';
import type { Page } from '@playwright/test';

const REPOSITORY_ROOT = path.resolve(__dirname, '..', '..', '..');
const PYTHON = process.env.E2E_PYTHON ?? 'python3';

export const API_BASE =
  process.env.API_BASE_URL ??
  process.env.E2E_BASE_URL ??
  'http://localhost:4002';

function runPython(script: string, args: string[]): string {
  return execFileSync(PYTHON, [path.join(__dirname, script), ...args], {
    cwd: REPOSITORY_ROOT,
    env: { ...process.env, PYTHONPATH: REPOSITORY_ROOT },
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'inherit'],
  });
}

export interface FakeMetaObject {
  object_type: string;
  remote_object_id: string;
  state: Record<string, unknown>;
}

export interface CallIntent {
  id: string;
  meta_api_method: string;
  network_dispatched_at: string | null;
}

/**
 * Direct-database inspection: `/api/internal/fake-meta-objects` and
 * `/api/internal/call-intents` do not exist as HTTP routes in this
 * codebase, so this shells out to `inspect_db.py`, which runs the same
 * query inside a `tenant_session` (bound by the same RLS a real
 * tenant-scoped request would be) instead.
 */
export function inspectFakeMetaObjects(
  workspaceId: string,
  metaMode: string,
  scenarioVersionId: string
): FakeMetaObject[] {
  const raw = runPython('inspect_db.py', [
    'fake-meta-objects',
    '--workspace-id',
    workspaceId,
    '--meta-mode',
    metaMode,
    '--scenario-version-id',
    scenarioVersionId,
  ]);
  return JSON.parse(raw.trim()) as FakeMetaObject[];
}

export function inspectCallIntents(
  workspaceId: string,
  operationId: string
): CallIntent[] {
  const raw = runPython('inspect_db.py', [
    'call-intents',
    '--workspace-id',
    workspaceId,
    '--operation-id',
    operationId,
  ]);
  return JSON.parse(raw.trim()) as CallIntent[];
}

export interface JobRecordSummary {
  id: string;
  type: string;
  state: string;
  attempt: number;
  max_attempts: number;
  lease_owner: string | null;
  lease_expires_at: string | null;
  error_code: string;
}

/**
 * Jobs enqueued for one Operation -- the Worker-restart/lease-expiry
 * recovery scenarios (Scenarios 5a-c, 9) need this to find the exact
 * `launch_provisioning` Job id `claimWorkerCheckpoint` must target, and to
 * assert a Job's final `state`/`attempt`/`error_code` after Worker
 * restart/recovery. `/api/internal/jobs` does not exist either, so this
 * shells out to `inspect_db.py` the same way `inspectFakeMetaObjects`/
 * `inspectCallIntents` do.
 */
export function inspectJobsForOperation(
  workspaceId: string,
  operationId: string,
  jobType?: string
): JobRecordSummary[] {
  const args = [
    'job-for-operation',
    '--workspace-id',
    workspaceId,
    '--operation-id',
    operationId,
  ];
  if (jobType) args.push('--job-type', jobType);
  const raw = runPython('inspect_db.py', args);
  return JSON.parse(raw.trim()) as JobRecordSummary[];
}

export interface WorkerCheckpointResult {
  job_id?: string;
  job_type?: string;
  attempt?: number;
  max_attempts?: number;
  lease_owner?: string;
  state?: string;
  lease_seconds?: number;
  error?: string;
}

/**
 * Leases exactly one expected Job through the real queue API
 * (`app.platform.jobs.claim_next`, via `worker_checkpoint.py`) and returns
 * before any Worker `mark_running` transition happens -- the crash
 * checkpoint the leased-before-running recovery scenario needs. Returns
 * `{ error }` rather than throwing when the checkpoint script's own guard
 * fails (wrong/missing Job), since that is itself a meaningful, assertable
 * test outcome.
 */
export function claimWorkerCheckpoint(
  expectedJobId: string,
  leaseSeconds = 5
): WorkerCheckpointResult {
  try {
    const raw = runPython('worker_checkpoint.py', [
      '--expected-job-id',
      expectedJobId,
      '--lease-seconds',
      String(leaseSeconds),
    ]);
    return JSON.parse(raw.trim()) as WorkerCheckpointResult;
  } catch (error) {
    const stdout = (error as { stdout?: Buffer | string }).stdout;
    if (stdout) {
      return JSON.parse(stdout.toString().trim()) as WorkerCheckpointResult;
    }
    throw error;
  }
}

function composeService(
  action: 'restart' | 'stop' | 'start',
  service: 'api' | 'worker'
): void {
  const project = process.env.E2E_EXPERIMENTS_COMPOSE_PROJECT?.trim();
  if (!project) {
    throw new Error('Missing E2E_EXPERIMENTS_COMPOSE_PROJECT');
  }
  const result = spawnSync(
    'docker',
    ['compose', '-p', project, action, service],
    {
      cwd: REPOSITORY_ROOT,
      env: process.env,
      encoding: 'utf8',
    }
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
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  throw new Error('API server did not become healthy in time');
}

export function restartWorker(): void {
  composeService('restart', 'worker');
}

export function stopWorker(): void {
  composeService('stop', 'worker');
}

export function startWorker(): void {
  composeService('start', 'worker');
}

/**
 * Polls the real launch-operation BFF endpoint
 * (`GET /api/domain/v1/w/:slug/launch/operations/:id?meta_mode=`) until
 * `operation_state` matches `expectedState`, or throws with the last
 * observed payload once `timeoutMs` elapses. `metaMode` is the Launch's
 * mode; the BFF rejects the read without it.
 */
export async function waitForOperationState(
  page: Page,
  workspaceSlug: string,
  provisioningId: string,
  metaMode: 'customer_sandbox' | 'controlled_mock',
  expectedState: string,
  timeoutMs = 30_000
): Promise<Record<string, unknown>> {
  const deadline = Date.now() + timeoutMs;
  let last: Record<string, unknown> | undefined;
  let lastFailure: string | undefined;
  while (Date.now() < deadline) {
    const response = await page.request.get(
      `/api/domain/v1/w/${workspaceSlug}/launch/operations/${provisioningId}`,
      { params: { meta_mode: metaMode } }
    );
    if (response.ok()) {
      last = (await response.json()) as Record<string, unknown>;
      if (last.operation_state === expectedState) return last;
    } else {
      lastFailure = `${response.status()} ${await response.text()}`;
    }
    await page.waitForTimeout(500);
  }
  throw new Error(
    `Operation ${provisioningId} never reached ${expectedState}. Last seen: ${JSON.stringify(last)}` +
      (lastFailure ? `; last failed read: ${lastFailure}` : '')
  );
}
