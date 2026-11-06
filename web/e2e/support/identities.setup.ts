import fs from 'node:fs';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { test, type Browser, type Page } from '@playwright/test';
import { expect, provisionWorkspace, totpCode } from '../golden-path/fixtures';
import { en } from './copy';

/**
 * Test identities for the real-backend suites that reuse stored browser
 * state (Localization and Scale; `e2e/README.md`), on a fresh Compose stack.
 *
 * The `setup` project signs in with a password, which an Owner with MFA
 * cannot do. This provisions a Workspace through the control plane, signs
 * its Owner up through the invitation and sets up MFA, signs a Member up
 * through an invitation, activates the Workspace through the entitlement
 * dispatch, and writes `e2e/.auth/owner.json`, `e2e/.auth/member.json` and
 * `e2e/.auth/identities.env` (ids, slug and test credentials the seeds read).
 * The suite runners load that file and then skip `setup`.
 *
 * Run it with `npx playwright test --project identities`. It needs the
 * golden-path provisioning variables and `E2E_IDENTITIES_ALLOW_MUTATIONS=1`.
 */
const AUTH_DIR = path.join(__dirname, '..', '.auth');

async function freshPage(browser: Browser): Promise<Page> {
  const context = await browser.newContext({ locale: 'en-US' });
  return context.newPage();
}

async function acceptInvitation(
  page: Page,
  token: string,
  name: string,
  password: string
): Promise<void> {
  await page.goto(`/auth/join?token=${encodeURIComponent(token)}`);
  await page.locator('input[name="name"]').fill(name);
  await page.locator('input[name="password"]').fill(password);
  await page
    .getByRole('button', { name: en('common', 'create-account') })
    .click();
  await page.waitForURL(/\/auth\/login/);
}

async function signIn(
  page: Page,
  email: string,
  password: string
): Promise<void> {
  await page.locator('input[name="email"]').fill(email);
  await page.locator('input[name="password"]').fill(password);
  await page.getByRole('button', { name: en('common', 'sign-in') }).click();
}

test('a Workspace with an MFA Owner and a Member', async ({ browser }) => {
  test.setTimeout(180_000);
  if (process.env.E2E_IDENTITIES_ALLOW_MUTATIONS !== '1') {
    throw new Error(
      'The identities project creates control-plane users and a Workspace; ' +
        'set E2E_IDENTITIES_ALLOW_MUTATIONS=1 against a disposable stack'
    );
  }
  fs.mkdirSync(AUTH_DIR, { recursive: true });
  const suffix = `id-${randomUUID().slice(0, 8)}`;
  const provisioned = await provisionWorkspace({
    suffix,
    mfaEncryptionKey: Buffer.from(
      process.env.MFA_ENCRYPTION_KEY ?? '',
      'base64'
    ),
  });
  const { prisma, domainDb, result: workspace } = provisioned;
  try {
    const owner = await freshPage(browser);
    await acceptInvitation(
      owner,
      workspace.invitationToken,
      'E2E Owner',
      workspace.ownerPassword
    );
    await signIn(owner, workspace.ownerEmail, workspace.ownerPassword);
    await owner.waitForURL(/\/settings\/security\?onboarding=/);
    await owner
      .getByRole('button', { name: en('common', 'onboarding-mfa-begin') })
      .click();
    const mfaSecret = (await owner.locator('code').first().innerText()).trim();
    await owner
      .getByLabel(en('common', 'onboarding-mfa-code'))
      .fill(totpCode(mfaSecret));
    await owner
      .getByRole('button', { name: en('common', 'onboarding-mfa-confirm') })
      .click();
    await expect(
      owner.getByText(en('common', 'onboarding-mfa-save-recovery'))
    ).toBeVisible();

    await provisioned.dispatchCurrentSnapshot(
      workspace.workspaceId,
      `identities-initial-${suffix}`
    );
    await expect
      .poll(
        async () =>
          (
            await prisma.team.findUnique({
              where: { id: workspace.workspaceId },
            })
          )?.lifecycle
      )
      .toBe('ACTIVE');
    await provisioned.dispatchCurrentSnapshot(
      workspace.workspaceId,
      `identities-active-${suffix}`
    );
    await owner
      .getByRole('link', {
        name: en('common', 'onboarding-continue-workspace'),
      })
      .click();
    await owner.waitForURL(new RegExp(`/w/${workspace.workspaceSlug}`));
    await owner
      .context()
      .storageState({ path: path.join(AUTH_DIR, 'owner.json') });
    const ownerUser = await prisma.user.findUniqueOrThrow({
      where: { email: workspace.ownerEmail },
      select: { id: true },
    });

    const memberEmail = `e2e-member-${suffix}@example.test`;
    const memberPassword = `Member-${randomUUID()}`;
    const invitation = await prisma.invitation.create({
      data: {
        teamId: workspace.workspaceId,
        email: memberEmail,
        role: 'MEMBER',
        invitedBy: ownerUser.id,
        token: randomUUID(),
        kind: 'MEMBER',
        expires: new Date(Date.now() + 24 * 60 * 60 * 1000),
      },
    });
    const member = await freshPage(browser);
    await acceptInvitation(
      member,
      invitation.token,
      'E2E Member',
      memberPassword
    );
    await signIn(member, memberEmail, memberPassword);
    await member.waitForURL((url) => !url.pathname.startsWith('/auth/'));
    await member
      .context()
      .storageState({ path: path.join(AUTH_DIR, 'member.json') });

    const [{ authorization_epoch: epoch }] = await domainDb.$queryRaw<
      { authorization_epoch: number }[]
    >`SELECT authorization_epoch FROM workspaces WHERE id = ${workspace.workspaceId}`;
    const values: Record<string, string> = {
      E2E_OWNER_EMAIL: workspace.ownerEmail,
      E2E_OWNER_PASSWORD: workspace.ownerPassword,
      E2E_OWNER_USER_ID: ownerUser.id,
      E2E_MEMBER_EMAIL: memberEmail,
      E2E_MEMBER_PASSWORD: memberPassword,
    };
    for (const suite of ['LOCALIZATION', 'SCALE']) {
      values[`E2E_${suite}_WORKSPACE_ID`] = workspace.workspaceId;
      values[`E2E_${suite}_WORKSPACE_SLUG`] = workspace.workspaceSlug;
      values[`E2E_${suite}_AUTHORIZATION_EPOCH`] = String(epoch);
    }
    fs.writeFileSync(
      path.join(AUTH_DIR, 'identities.env'),
      Object.entries(values)
        .map(([name, value]) => `${name}=${JSON.stringify(value)}`)
        .join('\n') + '\n',
      { mode: 0o600 }
    );
  } finally {
    await prisma.$disconnect();
    await domainDb.$disconnect();
  }
});
