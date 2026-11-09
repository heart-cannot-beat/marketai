import fs from 'node:fs';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { test, type Browser, type Page } from '@playwright/test';
import {
  expect,
  provisionWorkspace,
  totpCode,
  type WorkspaceProvisionResult,
} from '../golden-path/fixtures';
import { en } from './copy';

/**
 * Test identities for the real-backend suites that reuse stored browser
 * state (Localization, Scale and Experiments; `e2e/README.md`), on a fresh
 * Compose stack.
 *
 * The `setup` project signs in with a password, which an Owner with MFA
 * cannot do. This provisions a Workspace through the control plane, signs
 * its Owner up through the invitation and sets up MFA, signs a Member up
 * through an invitation, and activates the Workspace through the entitlement
 * dispatch. It then provisions a second Workspace for the Experiments suite,
 * which the same Owner and Member join through their invitations: each
 * Experiments seed retires the Workspace's Meta connections, while the Scale
 * seed adds an active one per mode, so the two cannot share a Workspace.
 * Last, it activates a third Workspace with an Owner of its own, which
 * neither of them belongs to, for the Experiments cross-Workspace checks. It
 * writes `e2e/.auth/owner.json`, `e2e/.auth/member.json` and
 * `e2e/.auth/identities.env` (ids, slugs and test credentials the seeds
 * read). The suite runners load that file and then skip `setup`.
 *
 * Run it with `npx playwright test --project identities`. It needs the
 * golden-path provisioning variables and `E2E_IDENTITIES_ALLOW_MUTATIONS=1`.
 */
const AUTH_DIR = path.join(__dirname, '..', '.auth');

type ProvisionedWorkspace = Awaited<ReturnType<typeof provisionWorkspace>>;

function provision(
  suffix: string,
  ownerEmail?: string
): Promise<ProvisionedWorkspace> {
  return provisionWorkspace({
    suffix,
    mfaEncryptionKey: Buffer.from(
      process.env.MFA_ENCRYPTION_KEY ?? '',
      'base64'
    ),
    ownerEmail,
  });
}

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

/** Accepts an invitation as the signed-in user it was sent to. */
async function acceptAsSignedIn(page: Page, token: string): Promise<void> {
  await page.goto(`/invitations/${encodeURIComponent(token)}`);
  await page
    .getByRole('button', { name: en('common', 'accept-invitation') })
    .click();
  await page.waitForURL((url) => !url.pathname.startsWith('/invitations/'));
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

/** Signs a first Owner up through their invitation and sets up MFA. */
async function signUpOwnerWithMfa(
  browser: Browser,
  workspace: WorkspaceProvisionResult,
  name: string
): Promise<Page> {
  const owner = await freshPage(browser);
  await acceptInvitation(
    owner,
    workspace.invitationToken,
    name,
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
  return owner;
}

/**
 * Activates a Workspace whose first Owner has accepted and set up MFA, then
 * delivers the snapshot activation published.
 */
async function activate(
  provisioned: ProvisionedWorkspace,
  dispatcherId: string
): Promise<void> {
  const { prisma, result } = provisioned;
  await provisioned.dispatchCurrentSnapshot(
    result.workspaceId,
    `${dispatcherId}-initial`
  );
  await expect
    .poll(
      async () =>
        (await prisma.team.findUnique({ where: { id: result.workspaceId } }))
          ?.lifecycle
    )
    .toBe('ACTIVE');
  await provisioned.dispatchCurrentSnapshot(
    result.workspaceId,
    `${dispatcherId}-active`
  );
}

test('Workspaces with an MFA Owner and a Member', async ({ browser }) => {
  test.setTimeout(240_000);
  if (process.env.E2E_IDENTITIES_ALLOW_MUTATIONS !== '1') {
    throw new Error(
      'The identities project creates control-plane users and a Workspace; ' +
        'set E2E_IDENTITIES_ALLOW_MUTATIONS=1 against a disposable stack'
    );
  }
  fs.mkdirSync(AUTH_DIR, { recursive: true });
  const suffix = `id-${randomUUID().slice(0, 8)}`;
  const provisioned = await provision(suffix);
  const { prisma, domainDb, result: workspace } = provisioned;
  const domainClients = [domainDb];
  try {
    const owner = await signUpOwnerWithMfa(browser, workspace, 'E2E Owner');
    await activate(provisioned, `identities-${suffix}`);
    await owner
      .getByRole('link', {
        name: en('common', 'onboarding-continue-workspace'),
      })
      .click();
    await owner.waitForURL(new RegExp(`/w/${workspace.workspaceSlug}`));
    const ownerUser = await prisma.user.findUniqueOrThrow({
      where: { email: workspace.ownerEmail },
      select: { id: true },
    });

    const memberEmail = `e2e-member-${suffix}@example.test`;
    const memberPassword = `Member-${randomUUID()}`;
    const inviteMember = async (teamId: string): Promise<string> =>
      (
        await prisma.invitation.create({
          data: {
            teamId,
            email: memberEmail,
            role: 'MEMBER',
            invitedBy: ownerUser.id,
            token: randomUUID(),
            kind: 'MEMBER',
            expires: new Date(Date.now() + 24 * 60 * 60 * 1000),
          },
        })
      ).token;
    const member = await freshPage(browser);
    await acceptInvitation(
      member,
      await inviteMember(workspace.workspaceId),
      'E2E Member',
      memberPassword
    );
    await signIn(member, memberEmail, memberPassword);
    await member.waitForURL((url) => !url.pathname.startsWith('/auth/'));

    const experiments = await provision(`${suffix}-exp`, workspace.ownerEmail);
    domainClients.push(experiments.domainDb);
    await acceptAsSignedIn(owner, experiments.result.invitationToken);
    await activate(experiments, `identities-${suffix}-exp`);
    await acceptAsSignedIn(
      member,
      await inviteMember(experiments.result.workspaceId)
    );

    const foreign = await provision(`${suffix}-foreign`);
    domainClients.push(foreign.domainDb);
    const foreignOwner = await signUpOwnerWithMfa(
      browser,
      foreign.result,
      'E2E Foreign Owner'
    );
    await activate(foreign, `identities-${suffix}-foreign`);
    await foreignOwner.context().close();

    await owner
      .context()
      .storageState({ path: path.join(AUTH_DIR, 'owner.json') });
    await member
      .context()
      .storageState({ path: path.join(AUTH_DIR, 'member.json') });

    const authorizationEpoch = async (workspaceId: string) => {
      const [{ authorization_epoch: epoch }] = await domainDb.$queryRaw<
        { authorization_epoch: number }[]
      >`SELECT authorization_epoch FROM workspaces WHERE id = ${workspaceId}`;
      return String(epoch);
    };
    const values: Record<string, string> = {
      E2E_OWNER_EMAIL: workspace.ownerEmail,
      E2E_OWNER_PASSWORD: workspace.ownerPassword,
      E2E_OWNER_USER_ID: ownerUser.id,
      E2E_MEMBER_EMAIL: memberEmail,
      E2E_MEMBER_PASSWORD: memberPassword,
      E2E_EXPERIMENTS_FOREIGN_WORKSPACE_SLUG: foreign.result.workspaceSlug,
    };
    const suiteWorkspaces: Array<[string, WorkspaceProvisionResult]> = [
      ['LOCALIZATION', workspace],
      ['SCALE', workspace],
      ['EXPERIMENTS', experiments.result],
    ];
    for (const [suite, { workspaceId, workspaceSlug }] of suiteWorkspaces) {
      values[`E2E_${suite}_WORKSPACE_ID`] = workspaceId;
      values[`E2E_${suite}_WORKSPACE_SLUG`] = workspaceSlug;
      values[`E2E_${suite}_AUTHORIZATION_EPOCH`] =
        await authorizationEpoch(workspaceId);
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
    for (const client of domainClients) {
      await client.$disconnect();
    }
  }
});
