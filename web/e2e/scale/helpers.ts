import { execFileSync, spawnSync } from 'child_process';
import crypto from 'crypto';
import path from 'path';
import type { Page, Request } from '@playwright/test';

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

function runSeed(args: string[]): Record<string, unknown> {
  const raw = runPython('seed_prerequisites.py', args);
  return JSON.parse(raw.trim()) as Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// Seeding
// ---------------------------------------------------------------------------

export interface ScaleChainSeed {
  workspaceId: string;
  workspaceSlug: string;
  userId: string;
  tag: string;
  metaMode: 'controlled_mock' | 'customer_sandbox';
  productId: string;
  productVersionId: string;
  offerVersionId: string;
  marketCode: string;
  assetVersionId: string;
  decisionId: string;
  deliveryTargetId: string;
  bindingId: string;
  contractSetId: string;
  policyVersionId: string;
}

const REQUIRED_CHAIN_FIELDS: Array<keyof ScaleChainSeed> = [
  'workspaceId',
  'workspaceSlug',
  'userId',
  'tag',
  'metaMode',
  'productId',
  'productVersionId',
  'offerVersionId',
  'marketCode',
  'assetVersionId',
  'decisionId',
  'deliveryTargetId',
  'bindingId',
  'contractSetId',
  'policyVersionId',
];

function assertCompleteChain(seed: Partial<ScaleChainSeed>): ScaleChainSeed {
  const missing = REQUIRED_CHAIN_FIELDS.filter((field) => {
    const value = seed[field];
    return value === undefined || value === null || value === '';
  });
  if (missing.length > 0) {
    throw new Error(`Scale prerequisite seed omitted: ${missing.join(', ')}`);
  }
  return seed as ScaleChainSeed;
}

/**
 * Seeds one fresh, tag-scoped confirmed Winner + `controlled_mock`
 * DeliveryTarget pair (shells out to `seed_prerequisites.py
 * seed-controlled-mock` -- see that file for the exact records it
 * creates), optionally carrying `faultConfig` as the `controlled_mock`
 * ScenarioVersion's `scenario_config`
 * (`app.adapters.meta.fake_adapter.FakeMetaAdapter`'s fault-injection
 * shape).
 */
export function seedControlledMockChain(
  faultConfig?: Record<string, unknown>
): ScaleChainSeed {
  const args = ['seed-controlled-mock'];
  if (faultConfig) args.push('--fault-config', JSON.stringify(faultConfig));
  return assertCompleteChain(runSeed(args));
}

/** Same chain, bound to the fixed `customer_sandbox` MetaConnection --
 * injects no fault config (spec §4.5: the Sandbox smoke never rewrites the
 * fixed Sandbox scenario). */
export function seedCustomerSandboxChain(): ScaleChainSeed {
  return assertCompleteChain(runSeed(['seed-customer-sandbox']));
}

export interface ManifestAdvanceResult {
  runId: string;
  outcomeKind: string;
  outcomeDetail: string;
  evaluationId: string | null;
  asOf: string;
  episodePhase: string;
  maturity: string | null;
  health: string | null;
  healthReason: string | null;
  dataQualityState: string | null;
  adviceThreadIds: string[];
  adviceThreadLifecycles: Record<string, string>;
}

export interface AdvanceManifestOptions {
  metaMode: 'controlled_mock' | 'customer_sandbox';
  enrollmentId: string;
  episodeId: string;
  policyVersionId: string;
  dayIndex: number;
  kind?: 'complete' | 'blocked';
  triggerKind?: string;
  targetVersionId?: string;
  metrics?: Record<string, unknown>;
  baselineMetrics?: Record<string, unknown>;
}

/**
 * Advances one Enrollment/Episode's real durable evidence one day forward
 * through the exact orchestration entry points the Scale Worker's
 * `_scale_evaluate` handler calls
 * (`app.scale.service.request_scale_evaluation` / `run_scale_evaluation`),
 * after seeding the daily/exact-window/delivery-configuration source rows
 * a real Meta sync would have produced (`kind: 'blocked'` skips seeding
 * evidence and marks the manifest blocked instead, for the terminal
 * blocked-manifest scenario). This is the test-harness-only seam
 * `tests/test_scale_evidence.py`'s own fixture builders use -- there is no
 * public Scale OpenAPI route for evidence ingestion
 * (`app/scale/v1_router.py`'s module docstring).
 */
export function advanceManifest(
  options: AdvanceManifestOptions
): ManifestAdvanceResult {
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
    options.triggerKind ?? 'live',
    '--metrics-json',
    JSON.stringify(options.metrics ?? {}),
  ];
  if (options.targetVersionId)
    args.push('--target-version-id', options.targetVersionId);
  if (options.baselineMetrics) {
    args.push(
      '--baseline-metrics-json',
      JSON.stringify(options.baselineMetrics)
    );
  }
  return runSeed(args) as unknown as ManifestAdvanceResult;
}

// ---------------------------------------------------------------------------
// Inspection
// ---------------------------------------------------------------------------

function runInspect(args: string[]): Record<string, unknown> {
  const raw = execFileSync(
    PYTHON,
    [path.join(__dirname, 'inspect_db.py'), ...args],
    {
      cwd: REPOSITORY_ROOT,
      env: { ...process.env, PYTHONPATH: REPOSITORY_ROOT },
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'inherit'],
    }
  );
  return JSON.parse(raw.trim()) as Record<string, unknown>;
}

export function inspectEnrollment(
  workspaceId: string,
  metaMode: string,
  enrollmentId: string
): Record<string, unknown> {
  return runInspect([
    'enrollment',
    '--workspace-id',
    workspaceId,
    '--meta-mode',
    metaMode,
    '--enrollment-id',
    enrollmentId,
  ]);
}

export function inspectEpisode(
  workspaceId: string,
  metaMode: string,
  episodeId: string
): Record<string, unknown> {
  return runInspect([
    'episode',
    '--workspace-id',
    workspaceId,
    '--meta-mode',
    metaMode,
    '--episode-id',
    episodeId,
  ]);
}

export function inspectEvaluations(
  workspaceId: string,
  metaMode: string,
  episodeId: string,
  limit = 10
): Array<Record<string, unknown>> {
  return runInspect([
    'evaluations',
    '--workspace-id',
    workspaceId,
    '--meta-mode',
    metaMode,
    '--episode-id',
    episodeId,
    '--limit',
    String(limit),
  ]) as unknown as Array<Record<string, unknown>>;
}

export function inspectAdviceThreads(
  workspaceId: string,
  metaMode: string,
  enrollmentId: string
): Array<Record<string, unknown>> {
  return runInspect([
    'advice-threads',
    '--workspace-id',
    workspaceId,
    '--meta-mode',
    metaMode,
    '--enrollment-id',
    enrollmentId,
  ]) as unknown as Array<Record<string, unknown>>;
}

export function inspectTarget(
  workspaceId: string,
  metaMode: string,
  targetId: string
): Record<string, unknown> {
  return runInspect([
    'target',
    '--workspace-id',
    workspaceId,
    '--meta-mode',
    metaMode,
    '--target-id',
    targetId,
  ]);
}

export function inspectExport(
  workspaceId: string,
  metaMode: string,
  exportId: string
): Record<string, unknown> {
  return runInspect([
    'export',
    '--workspace-id',
    workspaceId,
    '--meta-mode',
    metaMode,
    '--export-id',
    exportId,
  ]);
}

export function inspectCommandJournalEntry(
  workspaceId: string,
  metaMode: string,
  commandType: string,
  dedupKey: string
): Record<string, unknown> {
  return runInspect([
    'command-journal',
    '--workspace-id',
    workspaceId,
    '--meta-mode',
    metaMode,
    '--command-type',
    commandType,
    '--dedup-key',
    dedupKey,
  ]);
}

export function inspectZeroMetaWrites(workspaceId: string): {
  total_call_intents: number;
  offending: Array<{ id: string; meta_api_method: string }>;
  meta_write_token_empty: boolean;
  meta_write_ready: boolean;
} {
  return runInspect([
    'zero-meta-writes',
    '--workspace-id',
    workspaceId,
  ]) as unknown as {
    total_call_intents: number;
    offending: Array<{ id: string; meta_api_method: string }>;
    meta_write_token_empty: boolean;
    meta_write_ready: boolean;
  };
}

/** Client-computable mirror of `app.scale.command_journal.
 * compute_identity_hash` -- lets a test recover a command's outcome via
 * `GET .../command-outcomes/{identity}` from only the `(command_type,
 * dedup_key)` pair it itself supplied, exactly like a real client
 * recovering a lost response would. */
export function computeCommandIdentityHash(
  commandType: string,
  dedupKey: string
): string {
  return crypto
    .createHash('sha256')
    .update(`${commandType}:${dedupKey}`)
    .digest('hex');
}

/** Mirror of the BFF's `deriveIdempotencyKey`
 * (`lib/domain-client/request-pipeline.ts`): the BFF drops a client
 * `Idempotency-Key` and sends FastAPI `SHA-256(operationId:intentId)` from
 * the `X-Intent-Id` header instead. That derived key is the command's
 * `dedup_key`, so a test recovering an outcome through the BFF hashes it,
 * not the raw intent id, with `computeCommandIdentityHash`. */
export function deriveBffIdempotencyKey(
  operationId: string,
  intentId: string
): string {
  return crypto
    .createHash('sha256')
    .update(`${operationId}:${intentId}`)
    .digest('hex');
}

// ---------------------------------------------------------------------------
// Worker / compose control
// ---------------------------------------------------------------------------

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
  const project = process.env.E2E_SCALE_COMPOSE_PROJECT?.trim();
  if (!project) {
    throw new Error('Missing E2E_SCALE_COMPOSE_PROJECT');
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

/** FastAPI itself, polled for readiness after a restart: the control plane
 * at API_BASE keeps answering (or redirecting) while the API is down. */
const DOMAIN_API_BASE =
  process.env.E2E_DOMAIN_BASE_URL ?? 'http://localhost:8787';

export async function restartScaleApiServer(): Promise<void> {
  composeService('restart', 'api');
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`${DOMAIN_API_BASE}/api/health/ready`);
      if (res.ok) return;
    } catch {
      /* retry */
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  throw new Error('API server did not become healthy in time');
}

export function restartScaleWorker(): void {
  composeService('restart', 'worker');
}

export function stopScaleWorker(): void {
  composeService('stop', 'worker');
}

export function startScaleWorker(): void {
  composeService('start', 'worker');
}

/**
 * Processes at most one durable `scale_evaluate`/`scale_export` Job in a
 * disposable real `ScaleWorkerRuntime` container (`python -m
 * app.scale.worker --once` -- `app/scale/worker.py`'s own `__main__`
 * entry point, run inside the same `worker` compose service the
 * long-lived Worker uses, mirroring
 * `web/e2e/localization/fixtures.ts::runOneLocalizationWorkerJob`'s
 * `worker.main --once` pattern). The restart-recovery step (spec §4.5 step
 * 17) uses this to give the durable Job queue a deterministic processing
 * boundary instead of racing the long-lived Worker service.
 */
export function runOneScaleWorkerJob(): void {
  const project = process.env.E2E_SCALE_COMPOSE_PROJECT?.trim();
  if (!project) {
    throw new Error('Missing E2E_SCALE_COMPOSE_PROJECT');
  }
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
      'app.scale.worker',
      '--once',
    ],
    { cwd: REPOSITORY_ROOT, env: process.env, encoding: 'utf8' }
  );
  if (result.status !== 0) {
    throw new Error(
      `One-job Scale Worker failed (${result.status}): ${result.stderr || result.stdout}`
    );
  }
}

// ---------------------------------------------------------------------------
// Zero-Meta-writes browser network spy
// ---------------------------------------------------------------------------

const META_WRITE_HOSTS = ['graph.facebook.com', 'graph.instagram.com'];
const META_WRITE_METHODS = new Set(['POST', 'PUT', 'DELETE', 'PATCH']);

export interface NetworkSpy {
  /** Every real outbound Meta write request observed during this Page's
   * lifetime -- must stay empty throughout the whole Controlled-Mock
   * journey and Sandbox smoke (spec §4.5's closing sentence: "Scale never
   * performs Meta writes"). Read-only host calls (Graph API GETs a real
   * Meta adapter would make in `internal_validation`) are intentionally
   * not flagged -- this suite never runs `internal_validation` mode, so in
   * practice this list should never gain an entry either way. */
  offendingRequests: Request[];
}

/**
 * Installs a page-level network observer that records any outbound
 * request whose host and method combination would constitute a real Meta
 * write call. Scale's `FakeMetaAdapter` never reaches a real network host
 * at all (`app.scale.worker`'s module docstring: no `MetaWritePort`
 * import), so under correct behavior this spy's list stays permanently
 * empty -- it exists to catch a regression, not to observe expected
 * traffic.
 */
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
