import { execFileSync, spawnSync } from 'child_process';
import {
  createCipheriv,
  createHash,
  createHmac,
  randomBytes,
  randomUUID,
} from 'crypto';
import path from 'path';
import { test as base, expect } from '@playwright/test';
import type { APIResponse, Page, Request, Response } from '@playwright/test';

export { expect };

export type MetaMode = 'controlled_mock' | 'customer_sandbox';

const REPOSITORY_ROOT = path.resolve(__dirname, '..', '..', '..');
const PYTHON = process.env.E2E_PYTHON ?? 'python3';

// ---------------------------------------------------------------------------
// Required environment
// ---------------------------------------------------------------------------

function requiredEnv(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(`${name} is required for the golden-path E2E project.`);
  }
  return value;
}

// ---------------------------------------------------------------------------
// Workspace-scoped URL helpers
// ---------------------------------------------------------------------------

export function libraryUrl(slug: string): string {
  return `/w/${slug}/library`;
}

export function generateUrl(slug: string): string {
  return `/w/${slug}/generate`;
}

export function assetsUrl(slug: string): string {
  return `/w/${slug}/assets`;
}

export function experimentsUrl(slug: string): string {
  return `/w/${slug}/legacy-experiments`;
}

export function localizationUrl(slug: string): string {
  return `/w/${slug}/localization`;
}

export function scaleUrl(slug: string): string {
  return `/w/${slug}/scale`;
}

export function scaleEnrollmentUrl(slug: string, enrollmentId: string): string {
  return `/w/${slug}/scale/${enrollmentId}`;
}

export function libraryBff(slug: string): string {
  return `/api/domain/v1/w/${slug}/library`;
}

export function generationBff(slug: string): string {
  return `/api/domain/v1/w/${slug}/generation`;
}

export function experimentsBff(slug: string): string {
  return `/api/domain/v1/w/${slug}/experiments`;
}

export function launchBff(slug: string): string {
  return `/api/domain/v1/w/${slug}/launch`;
}

export function localizationBff(slug: string): string {
  return `/api/domain/v1/w/${slug}/localization`;
}

export function scaleBff(slug: string, metaMode: MetaMode): string {
  return `/api/domain/v1/w/${slug}/scale/${metaMode}`;
}

// ---------------------------------------------------------------------------
// TOTP and MFA helpers (from onboarding test)
// ---------------------------------------------------------------------------

export function totpCode(secret: string): string {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
  let bits = '';
  for (const character of secret.replace(/\s|-/g, '').toUpperCase()) {
    bits += alphabet.indexOf(character).toString(2).padStart(5, '0');
  }
  const bytes: number[] = [];
  for (let offset = 0; offset + 8 <= bits.length; offset += 8) {
    bytes.push(Number.parseInt(bits.slice(offset, offset + 8), 2));
  }
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(Date.now() / 30_000)));
  const digest = createHmac('sha1', Buffer.from(bytes))
    .update(counter)
    .digest();
  const offset = digest[digest.length - 1] & 0x0f;
  return ((digest.readUInt32BE(offset) & 0x7fffffff) % 1_000_000)
    .toString()
    .padStart(6, '0');
}

export function protectMfaFixtureSecret(
  secret: string,
  encryptionKey: Buffer
): { ciphertext: string; iv: string; authTag: string } {
  const iv = randomBytes(12);
  const cipher = createCipheriv('aes-256-gcm', encryptionKey, iv);
  const ciphertext = Buffer.concat([
    cipher.update(secret, 'utf8'),
    cipher.final(),
  ]);
  return {
    ciphertext: ciphertext.toString('base64'),
    iv: iv.toString('base64'),
    authTag: cipher.getAuthTag().toString('base64'),
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
// Idempotency key
// ---------------------------------------------------------------------------

export function idempotencyKey(): string {
  return randomUUID();
}

export async function domainMutationHeaders(
  page: Page,
  ifMatch?: string
): Promise<Record<string, string>> {
  const { token } = await readJson<{ token: string }>(
    await page.request.get('/api/csrf')
  );
  return {
    Origin: new URL(page.url()).origin,
    'X-CSRF-Token': token,
    'X-Intent-Id': idempotencyKey(),
    ...(ifMatch === undefined ? {} : { 'If-Match': ifMatch }),
  };
}

// ---------------------------------------------------------------------------
// Control-plane provisioning (Prisma)
// ---------------------------------------------------------------------------

export interface WorkspaceProvisionResult {
  workspaceId: string;
  workspaceSlug: string;
  ownerEmail: string;
  ownerPassword: string;
  ownerMfaSecret: string;
  invitationToken: string;
}

export async function provisionWorkspace(options: {
  suffix: string;
  mfaEncryptionKey: Buffer;
  /** First-Owner invitation address; defaults to a new user per suffix. */
  ownerEmail?: string;
}): Promise<{
  result: WorkspaceProvisionResult;
  prisma: any;
  domainDb: any;
  dispatchCurrentSnapshot: (
    teamId: string,
    dispatcherId: string
  ) => Promise<string>;
}> {
  const controlDatabaseUrl = requiredEnv('CONTROL_PLANE_DATABASE_URL');
  const domainOwnerDatabaseUrl = requiredEnv('DOMAIN_OWNER_DATABASE_URL');
  const domainBaseUrl = requiredEnv('E2E_DOMAIN_BASE_URL');
  const entitlementToken = requiredEnv(
    'CONTROL_PLANE_TO_DOMAIN_ENTITLEMENT_TOKEN'
  );
  const signingKey = requiredEnv('ENTITLEMENT_SNAPSHOT_SIGNING_KEY');
  const signingKeyId = requiredEnv('ENTITLEMENT_SNAPSHOT_SIGNING_KEY_ID');

  process.env.DATABASE_URL = controlDatabaseUrl;
  const { PrismaClient } = await import('@prisma/client');
  const { prisma } = await import('../../lib/prisma');
  const domainDb = new PrismaClient({ datasourceUrl: domainOwnerDatabaseUrl });
  const { claimAndDispatch } =
    await import('../../lib/entitlements/dispatcher');
  const { provisionFirstOwnerWorkspace, reconcileReadyProvisioningWorkspaces } =
    await import('../../lib/onboarding');

  const { suffix } = options;
  const workspaceSlug = `e2e-golden-path-${suffix}`;
  const ownerEmail =
    options.ownerEmail ?? `e2e-gp-owner-${suffix}@example.test`;
  const ownerPassword = 'correct horse battery staple';
  const ownerMfaSecret = 'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP';

  const operator = await prisma.user.create({
    data: {
      name: 'Golden Path Provisioning Operator',
      email: `e2e-gp-operator-${suffix}@example.test`,
    },
  });
  const entitlements = { controlled_mock: true };
  const betaPlan = await prisma.planVersion.create({
    data: {
      planKey: `beta-gp-${suffix}`,
      versionNo: 1,
      displayName: `Beta Golden Path ${suffix}`,
      state: 'EFFECTIVE',
      entitlements,
      entitlementsHash: createHash('sha256')
        .update(JSON.stringify(entitlements))
        .digest('hex'),
      publishedBy: 'e2e-golden-path',
    },
  });

  const provisioned = await provisionFirstOwnerWorkspace(prisma, {
    name: `Golden Path ${suffix}`,
    slug: workspaceSlug,
    ownerEmail,
    operatorUserId: operator.id,
    betaPlanVersionId: betaPlan.id,
    signingKeyId,
    signingKey: Buffer.from(signingKey, 'utf8'),
  });
  const workspaceId = provisioned.team.id;
  const invitationToken = provisioned.invitation.token;

  // Seed the domain workspace row (empty — no products or business objects)
  await domainDb.$executeRaw`
    INSERT INTO workspaces (
      id, name, default_currency, authorization_epoch,
      entitlement_version, suspended, projection_version,
      created_at, lifecycle, budget_currency, is_platform_workspace
    ) VALUES (
      ${workspaceId}, ${`Golden Path ${suffix}`}, 'USD', 0,
      0, false, 0, now(), 'provisioning', 'USD', false
    )
  `;

  // No SpendGuardrail here: applying the first snapshot
  // (marketai_apply_workspace_projection_v2, dispatchCurrentSnapshot below)
  // provisions it at the contract's Beta defaults (#126), and the journey
  // runs on controlled Mock, which reserves nothing against it.

  const dispatchCurrentSnapshot = async (
    teamId: string,
    dispatcherId: string
  ): Promise<string> => {
    const projection = await prisma.commercialAccessProjection.findUnique({
      where: { teamId },
      select: { currentSnapshotId: true },
    });
    expect(projection?.currentSnapshotId).toBeTruthy();
    const snapshotId = projection!.currentSnapshotId as string;

    await claimAndDispatch(prisma, dispatcherId, {
      domainBaseUrl,
      pushToken: entitlementToken,
      batchSize: 100,
    });
    await reconcileReadyProvisioningWorkspaces(prisma, {
      signingKeyId,
      signingKey: Buffer.from(signingKey, 'utf8'),
      batchSize: 100,
    });
    const outbox = await prisma.entitlementSnapshotOutbox.findUnique({
      where: { snapshotId },
      select: { deliveryState: true, deliveredAt: true },
    });
    expect(outbox).toMatchObject({ deliveryState: 'DELIVERED' });
    expect(outbox?.deliveredAt).not.toBeNull();
    return snapshotId;
  };

  return {
    result: {
      workspaceId,
      workspaceSlug,
      ownerEmail,
      ownerPassword,
      ownerMfaSecret,
      invitationToken,
    },
    prisma,
    domainDb,
    dispatchCurrentSnapshot,
  };
}

// ---------------------------------------------------------------------------
// Python infrastructure seeding
// ---------------------------------------------------------------------------

export function runPython(
  script: string,
  args: string[],
  extraEnv?: Record<string, string>
): string {
  return execFileSync(PYTHON, [path.join(__dirname, script), ...args], {
    cwd: REPOSITORY_ROOT,
    env: { ...process.env, PYTHONPATH: REPOSITORY_ROOT, ...extraEnv },
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'inherit'],
  });
}

// ---------------------------------------------------------------------------
// Docker compose service control
// ---------------------------------------------------------------------------

const WEB_BASE = process.env.E2E_BASE_URL ?? 'http://localhost:4002';

const DOMAIN_API_BASE =
  process.env.E2E_DOMAIN_BASE_URL ?? 'http://localhost:8787';

function composeArgs(): string[] {
  const project = process.env.E2E_COMPOSE_PROJECT?.trim();
  if (!project) {
    throw new Error('Missing E2E_COMPOSE_PROJECT');
  }
  const file = process.env.E2E_COMPOSE_FILE?.trim();
  const overrideFile = process.env.E2E_COMPOSE_OVERRIDE_FILE?.trim();
  return [
    'compose',
    '-p',
    project,
    ...(file ? ['-f', file] : []),
    ...(overrideFile ? ['-f', overrideFile] : []),
  ];
}

function composeService(
  action: 'restart' | 'stop' | 'start',
  service: 'api' | 'worker' | 'web'
): void {
  const result = spawnSync('docker', [...composeArgs(), action, service], {
    cwd: REPOSITORY_ROOT,
    env: process.env,
    encoding: 'utf8',
  });
  if (result.status !== 0) {
    throw new Error(
      `${service} ${action} failed (${result.status}): ${result.stderr || result.stdout}`
    );
  }
}

export function stopWorker(): void {
  composeService('stop', 'worker');
}

export function startWorker(): void {
  const result = spawnSync(
    'docker',
    [...composeArgs(), 'up', '-d', '--no-deps', 'worker'],
    {
      cwd: REPOSITORY_ROOT,
      env: process.env,
      encoding: 'utf8',
    }
  );
  if (result.status !== 0) {
    throw new Error(
      `worker start failed (${result.status}): ${result.stderr || result.stdout}`
    );
  }
}

export function runWorkerOnce(): void {
  const result = spawnSync(
    'docker',
    [
      ...composeArgs(),
      'run',
      '--rm',
      '--no-deps',
      'worker',
      'python',
      '-m',
      'app.worker_runtime',
      '--once',
    ],
    {
      cwd: REPOSITORY_ROOT,
      env: process.env,
      encoding: 'utf8',
    }
  );
  if (result.status !== 0) {
    throw new Error(
      `worker one-shot failed (${result.status}): ${result.stderr || result.stdout}`
    );
  }
}

export function restartWorker(): void {
  composeService('restart', 'worker');
}

export async function restartApiServer(): Promise<void> {
  composeService('restart', 'api');
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`${DOMAIN_API_BASE}/api/health/ready`);
      if (res.ok) return;
    } catch {
      /* retry */
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  throw new Error('API server did not become healthy in time');
}

export async function restartWebService(): Promise<void> {
  composeService('restart', 'web');
  await expect
    .poll(
      async () => {
        try {
          return (await fetch(`${WEB_BASE}/api/health`)).status;
        } catch {
          return 0;
        }
      },
      { timeout: 90_000 }
    )
    .toBe(200);
}

// ---------------------------------------------------------------------------
// Operation polling
// ---------------------------------------------------------------------------

export async function waitForOperationState(
  page: Page,
  slug: string,
  operationId: string,
  expectedState: string,
  timeoutMs = 60_000
): Promise<Record<string, unknown>> {
  const deadline = Date.now() + timeoutMs;
  let last: Record<string, unknown> | undefined;
  while (Date.now() < deadline) {
    const response = await page.request.get(
      `/api/domain/v1/w/${slug}/operations/${operationId}`
    );
    if (response.ok()) {
      last = (await response.json()) as Record<string, unknown>;
      if (last.operation_state === expectedState) return last;
    }
    await page.waitForTimeout(500);
  }
  throw new Error(
    `Operation ${operationId} never reached ${expectedState}. Last: ${JSON.stringify(last)}`
  );
}

// ---------------------------------------------------------------------------
// Zero-Meta-writes page-level network spy
// ---------------------------------------------------------------------------

const META_WRITE_HOSTS = new Set(['graph.facebook.com', 'graph.instagram.com']);
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
    if (META_WRITE_HOSTS.has(host)) {
      spy.offendingRequests.push(request);
    }
  });
  return spy;
}

// ---------------------------------------------------------------------------
// Test fixture
// ---------------------------------------------------------------------------

interface GoldenPathFixtures {
  networkSpy: NetworkSpy;
}

export const test = base.extend<GoldenPathFixtures>({
  networkSpy: async ({ page }, provide) => {
    const spy = installNetworkSpy(page);
    await provide(spy);
  },
});
