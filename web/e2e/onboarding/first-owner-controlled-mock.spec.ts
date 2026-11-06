import { execFileSync } from 'node:child_process';
import {
  createCipheriv,
  createHash,
  createHmac,
  randomBytes,
  randomUUID,
} from 'node:crypto';
import path from 'node:path';

import { expect, test } from '@playwright/test';
import { hash } from 'bcryptjs';

const requiredEnv = (name: string) => {
  const value = process.env[name];
  if (!value) {
    throw new Error(
      `${name} is required for the non-mocked first-Owner onboarding project.`
    );
  }
  return value;
};

const controlDatabaseUrl = requiredEnv('CONTROL_PLANE_DATABASE_URL');
const domainOwnerDatabaseUrl = requiredEnv('DOMAIN_OWNER_DATABASE_URL');
const domainBaseUrl = requiredEnv('E2E_DOMAIN_BASE_URL');
const entitlementToken = requiredEnv(
  'CONTROL_PLANE_TO_DOMAIN_ENTITLEMENT_TOKEN'
);
const signingKey = requiredEnv('ENTITLEMENT_SNAPSHOT_SIGNING_KEY');
const signingKeyId = requiredEnv('ENTITLEMENT_SNAPSHOT_SIGNING_KEY_ID');
const mfaEncryptionKey = Buffer.from(
  requiredEnv('MFA_ENCRYPTION_KEY'),
  'base64'
);
if (mfaEncryptionKey.length !== 32) {
  throw new Error('MFA_ENCRYPTION_KEY must decode to 32 bytes.');
}
const baseUrl = requiredEnv('E2E_BASE_URL');
const composeProject = process.env.E2E_COMPOSE_PROJECT ?? 'marketai';
const composeFile =
  process.env.E2E_COMPOSE_FILE ??
  path.resolve(process.cwd(), '..', 'docker-compose.yml');
const composeOverrideFile = process.env.E2E_COMPOSE_OVERRIDE_FILE;

let prisma: any;
let domainDb: any;
let claimAndDispatch: any;
let reconcileReadyProvisioningWorkspaces: any;
let provisionFirstOwnerWorkspace: any;
let invitationToken: string;
let workspaceId: string;
let workspaceSlug: string;
let ownerEmail: string;
let productId: string;
let initialSnapshotId: string;
let ownerMfaSecret: string;
let existingOwnerEmail: string;
let existingOwnerMfaSecret: string;
let existingWorkspaceId: string;
let existingWorkspaceSlug: string;
let existingInvitationToken: string;
const ownerPassword = 'correct horse battery staple';

const dispatchCurrentSnapshot = async (
  teamId: string,
  dispatcherId: string
) => {
  const projection = await prisma.commercialAccessProjection.findUnique({
    where: { teamId },
    select: { currentSnapshotId: true },
  });
  expect(projection?.currentSnapshotId).toBeTruthy();
  const snapshotId = projection.currentSnapshotId as string;

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

const restartWebService = async () => {
  const args = [
    'compose',
    '-p',
    composeProject,
    '-f',
    composeFile,
    ...(composeOverrideFile ? ['-f', composeOverrideFile] : []),
    'restart',
    'web',
  ];
  execFileSync('docker', args, { stdio: 'inherit', timeout: 60_000 });
  await expect
    .poll(
      async () => {
        try {
          return (await fetch(`${baseUrl}/api/health`)).status;
        } catch {
          return 0;
        }
      },
      { timeout: 90_000 }
    )
    .toBe(200);
};

const totpCode = (secret: string) => {
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
};

const protectMfaFixtureSecret = (secret: string) => {
  const iv = randomBytes(12);
  const cipher = createCipheriv('aes-256-gcm', mfaEncryptionKey, iv);
  const ciphertext = Buffer.concat([
    cipher.update(secret, 'utf8'),
    cipher.final(),
  ]);
  return {
    ciphertext: ciphertext.toString('base64'),
    iv: iv.toString('base64'),
    authTag: cipher.getAuthTag().toString('base64'),
  };
};

test.beforeAll(async () => {
  process.env.DATABASE_URL = controlDatabaseUrl;
  const { PrismaClient } = await import('@prisma/client');
  ({ prisma } = await import('../../lib/prisma'));
  domainDb = new PrismaClient({ datasourceUrl: domainOwnerDatabaseUrl });
  ({ claimAndDispatch } = await import('../../lib/entitlements/dispatcher'));
  ({ provisionFirstOwnerWorkspace, reconcileReadyProvisioningWorkspaces } =
    await import('../../lib/onboarding'));

  const suffix = randomUUID().slice(0, 8);
  workspaceSlug = `e2e-onboarding-${suffix}`;
  ownerEmail = `e2e-owner-${suffix}@example.test`;
  productId = `product-e2e-${suffix}`;
  const operator = await prisma.user.create({
    data: {
      name: 'E2E Provisioning Operator',
      email: `e2e-operator-${suffix}@example.test`,
    },
  });
  const entitlements = { controlled_mock: true };
  const betaPlan = await prisma.planVersion.create({
    data: {
      planKey: `beta-e2e-${suffix}`,
      versionNo: 1,
      displayName: `Beta E2E ${suffix}`,
      state: 'EFFECTIVE',
      entitlements,
      entitlementsHash: createHash('sha256')
        .update(JSON.stringify(entitlements))
        .digest('hex'),
      publishedBy: 'e2e-onboarding',
    },
  });
  existingOwnerEmail = `e2e-existing-owner-${suffix}@example.test`;
  existingOwnerMfaSecret = 'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP';
  const existingOwner = await prisma.user.create({
    data: {
      name: 'Existing Enrolled Owner',
      email: existingOwnerEmail,
      password: await hash(ownerPassword, 12),
      emailVerified: new Date(),
      mfaCredential: {
        create: {
          protectedSecret: protectMfaFixtureSecret(existingOwnerMfaSecret),
          verifiedAt: new Date(),
        },
      },
    },
  });
  const existingProvisioned = await provisionFirstOwnerWorkspace(prisma, {
    name: `E2E Existing Owner ${suffix}`,
    slug: `e2e-existing-owner-${suffix}`,
    ownerEmail: existingOwner.email,
    operatorUserId: operator.id,
    betaPlanVersionId: betaPlan.id,
    signingKeyId,
    signingKey: Buffer.from(signingKey, 'utf8'),
  });
  existingWorkspaceId = existingProvisioned.team.id;
  existingWorkspaceSlug = existingProvisioned.team.slug;
  existingInvitationToken = existingProvisioned.invitation.token;
  await domainDb.$executeRaw`
    INSERT INTO workspaces (
      id, name, default_currency, authorization_epoch,
      entitlement_version, suspended, projection_version,
      created_at, lifecycle, budget_currency, is_platform_workspace
    ) VALUES (
      ${existingWorkspaceId}, ${existingProvisioned.team.name}, 'USD', 0,
      0, false, 0, now(), 'provisioning', 'USD', false
    )
  `;
  await dispatchCurrentSnapshot(
    existingWorkspaceId,
    `playwright-existing-initial-${suffix}`
  );
  await expect(
    prisma.team.findUnique({ where: { id: existingWorkspaceId } })
  ).resolves.toMatchObject({ lifecycle: 'PROVISIONING' });

  const provisioned = await provisionFirstOwnerWorkspace(prisma, {
    name: `E2E Onboarding ${suffix}`,
    slug: workspaceSlug,
    ownerEmail,
    operatorUserId: operator.id,
    betaPlanVersionId: betaPlan.id,
    signingKeyId,
    signingKey: Buffer.from(signingKey, 'utf8'),
  });
  workspaceId = provisioned.team.id;
  invitationToken = provisioned.invitation.token;

  await domainDb.$executeRaw`
    INSERT INTO workspaces (
      id, name, default_currency, authorization_epoch,
      entitlement_version, suspended, projection_version,
      created_at, lifecycle, budget_currency, is_platform_workspace
    ) VALUES (
      ${workspaceId}, ${`E2E Onboarding ${suffix}`}, 'USD', 0,
      0, false, 0, now(), 'provisioning', 'USD', false
    )
  `;
  await domainDb.$executeRaw`
    INSERT INTO products (
      id, workspace_id, name, description, selling_points, audience,
      brand_notes, ref_images, economics, revision, created_at, updated_at
    ) VALUES (
      ${productId}, ${workspaceId}, 'E2E Controlled Mock Product', '', '[]', '',
      '', '[]', '{}', 1, now(), now()
    )
  `;

  const projection = await prisma.commercialAccessProjection.findUnique({
    where: { teamId: workspaceId },
    select: { currentSnapshotId: true },
  });
  initialSnapshotId = projection.currentSnapshotId;
});

test.afterAll(async () => {
  await prisma?.$disconnect();
  await domainDb?.$disconnect();
});

test('invite -> sign-in -> MFA -> ACTIVE -> controlled Mock draft', async ({
  page,
}) => {
  test.setTimeout(90_000);
  await page.goto(`/auth/join?token=${encodeURIComponent(invitationToken)}`);
  await page.locator('input[name="name"]').fill('First Owner');
  await page.locator('input[name="password"]').fill(ownerPassword);
  await page.getByRole('button', { name: 'Create Account' }).click();

  await page.waitForURL(/\/auth\/login\?callbackUrl=/);
  await page.locator('input[name="email"]').fill(ownerEmail);
  await page.locator('input[name="password"]').fill(ownerPassword);
  await page.getByRole('button', { name: /sign in/i }).click();
  await page.waitForURL(/\/settings\/security\?onboarding=/);

  await page.getByRole('button', { name: 'Set up MFA' }).click();
  ownerMfaSecret = (await page.locator('code').first().innerText()).trim();
  await page.getByLabel('Authenticator code').fill(totpCode(ownerMfaSecret));
  await page.getByRole('button', { name: 'Confirm MFA' }).click();
  await expect(
    page.getByText(/Save these one-time recovery codes/)
  ).toBeVisible();
  await expect(
    page.locator('li').filter({ hasText: /^[A-F0-9-]{19}$/ })
  ).toHaveCount(10);

  await expect
    .poll(async () => {
      const team = await prisma.team.findUnique({ where: { id: workspaceId } });
      return team?.lifecycle;
    })
    .toBe('PROVISIONING');

  const deliveredInitialSnapshotId = await dispatchCurrentSnapshot(
    workspaceId,
    `playwright-initial-${randomUUID()}`
  );
  expect(deliveredInitialSnapshotId).toBe(initialSnapshotId);

  await expect
    .poll(async () => {
      const team = await prisma.team.findUnique({ where: { id: workspaceId } });
      return team?.lifecycle;
    })
    .toBe('ACTIVE');

  const activeSnapshotId = await dispatchCurrentSnapshot(
    workspaceId,
    `playwright-active-${randomUUID()}`
  );
  expect(activeSnapshotId).not.toBe(initialSnapshotId);

  const activeSnapshot = await prisma.entitlementSnapshot.findUniqueOrThrow({
    where: { id: activeSnapshotId },
    select: { canonicalEnvelope: true },
  });
  const duplicateDelivery = await fetch(
    `${domainBaseUrl}/api/internal/domain/workspaces/${workspaceId}/entitlement-inbox`,
    {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${entitlementToken}`,
        'Content-Type': 'application/octet-stream',
      },
      body: Uint8Array.from(activeSnapshot.canonicalEnvelope),
    }
  );
  expect(duplicateDelivery.status).toBe(200);
  await restartWebService();
  expect(
    await prisma.controlPlaneAuditEvent.count({
      where: { teamId: workspaceId, eventType: 'workspace.activated' },
    })
  ).toBe(1);
  expect(
    await prisma.entitlementSnapshot.count({
      where: { teamId: workspaceId, lifecycle: 'ACTIVE' },
    })
  ).toBe(1);
  const deliveredActiveSnapshots = await prisma.entitlementSnapshot.findMany({
    where: { teamId: workspaceId, lifecycle: 'ACTIVE' },
    include: { outboxEntry: true },
  });
  expect(deliveredActiveSnapshots).toHaveLength(1);
  expect(deliveredActiveSnapshots[0].outboxEntry).toMatchObject({
    deliveryState: 'DELIVERED',
  });

  const capabilitiesResponse = page.waitForResponse(
    (response) =>
      response.request().method() === 'GET' &&
      new URL(response.url()).pathname ===
        `/api/domain/v1/w/${workspaceSlug}/generation/capabilities`
  );
  await page.getByRole('link', { name: 'Continue to Workspace' }).click();
  await page.waitForURL(new RegExp(`/w/${workspaceSlug}/generate$`));
  await expect(
    page.getByRole('heading', { name: 'First value checklist' })
  ).toBeVisible();
  await expect(page.getByText('Workspace prepared')).toHaveClass(
    /line-through/
  );
  await expect(page.getByText('First Owner accepted')).toHaveClass(
    /line-through/
  );

  expect((await capabilitiesResponse).status()).toBe(200);
  await page.getByRole('button', { name: 'New Draft' }).first().click();
  const dialog = page.locator('dialog[aria-label="Create New Draft"]');
  await expect(dialog.locator('#new-draft-provider')).toHaveValue('mock');
  await expect(dialog.locator('#new-draft-model')).toHaveValue('mock-v1');
  await expect(dialog.locator('#new-draft-mode')).toHaveValue('mock');
  // Picked from its card by name (#162); the seeded legacy product is the
  // only one, so it is already chosen.
  await dialog
    .getByRole('radio', { name: 'E2E Controlled Mock Product' })
    .check();

  const createRequest = page.waitForRequest(
    (request) =>
      request.method() === 'POST' &&
      new URL(request.url()).pathname ===
        `/api/domain/v1/w/${workspaceSlug}/generation/drafts`
  );
  const createResponse = page.waitForResponse(
    (response) =>
      response.request().method() === 'POST' &&
      new URL(response.url()).pathname ===
        `/api/domain/v1/w/${workspaceSlug}/generation/drafts`
  );
  await dialog.getByRole('button', { name: 'Create Draft' }).click();
  expect((await createRequest).postDataJSON()).toMatchObject({
    provider: 'mock',
    model: 'mock-v1',
    execution_mode: 'mock',
  });
  expect((await createResponse).status()).toBe(201);
  await expect(page).toHaveURL(
    new RegExp(`/w/${workspaceSlug}/generate/drafts/gd_`)
  );
});

test('an existing enrolled user activates on first-Owner acceptance', async ({
  page,
}) => {
  test.setTimeout(90_000);
  await page.goto(
    `/auth/login?token=${encodeURIComponent(existingInvitationToken)}`
  );
  await page.locator('input[name="email"]').fill(existingOwnerEmail);
  await page.locator('input[name="password"]').fill(ownerPassword);
  await page
    .locator('input[name="mfaCode"]')
    .fill(totpCode(existingOwnerMfaSecret));
  await page.getByRole('button', { name: /sign in/i }).click();
  await page.waitForURL(new RegExp(`/invitations/${existingInvitationToken}$`));
  const acceptance = page.waitForResponse(
    (response) =>
      response.request().method() === 'PUT' &&
      new URL(response.url()).pathname ===
        `/api/teams/${existingWorkspaceSlug}/invitations`
  );
  await page.getByRole('button', { name: 'Join the Workspace' }).click();
  expect((await acceptance).status()).toBe(204);
  await page.waitForURL(
    (url) =>
      url.pathname === '/settings/security' &&
      url.searchParams.get('onboarding') === existingWorkspaceSlug
  );

  await expect
    .poll(async () => {
      const team = await prisma.team.findUnique({
        where: { id: existingWorkspaceId },
      });
      return team?.lifecycle;
    })
    .toBe('ACTIVE');
  expect(
    await prisma.controlPlaneAuditEvent.count({
      where: {
        teamId: existingWorkspaceId,
        eventType: 'workspace.activated',
      },
    })
  ).toBe(1);
});
