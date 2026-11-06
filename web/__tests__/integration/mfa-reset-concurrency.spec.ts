import { randomUUID } from 'node:crypto';

import {
  signTestOperatorIdentity,
  testOperatorJwks,
} from '../helpers/operator-identity';

export {};

const controlUrl = process.env.CONTROL_PLANE_DATABASE_URL;
const describeIfConfigured = controlUrl ? describe : describe.skip;

describeIfConfigured('MFA reset (PostgreSQL)', () => {
  jest.setTimeout(45_000);

  let prisma: any;
  let hashPassword: (value: string) => Promise<string>;
  let createMfaResetCase: any;
  let beginMfaRecoveryEvidence: any;
  let verifyMfaRecoveryEmailChallenge: any;
  let approveMfaResetCase: any;
  let createAuthSession: any;
  let verifyOperatorIdentityToken: any;
  const operatorToken = signTestOperatorIdentity;

  const operator = (subject: string) =>
    verifyOperatorIdentityToken(operatorToken(subject), testOperatorJwks);

  beforeAll(async () => {
    process.env.DATABASE_URL = controlUrl as string;
    ({ prisma } = await import('../../lib/prisma'));
    ({ hashPassword } = await import('../../lib/auth'));
    ({
      createMfaResetCase,
      beginMfaRecoveryEvidence,
      verifyMfaRecoveryEmailChallenge,
      approveMfaResetCase,
    } = await import('../../lib/mfa-reset'));
    ({ createAuthSession } = await import('../../models/session'));
    ({ verifyOperatorIdentityToken } =
      await import('../../lib/operator-identity'));
  });

  afterAll(async () => {
    await prisma?.$disconnect();
  });

  it('completes a dual-operator reset once and lets no concurrent login escape', async () => {
    const suffix = randomUUID().slice(0, 8);
    const password = 'correct horse battery staple';
    const user = await prisma.user.create({
      data: {
        name: 'Recovered Owner',
        email: 'recover-' + suffix + '@example.test',
        emailVerified: new Date(),
        password: await hashPassword(password),
      },
    });
    const team = await prisma.team.create({
      data: {
        name: 'Recovery ' + suffix,
        slug: 'recovery-' + suffix,
        lifecycle: 'ACTIVE',
      },
    });
    await prisma.teamMember.create({
      data: { teamId: team.id, userId: user.id, role: 'OWNER' },
    });
    const invitation = await prisma.invitation.create({
      data: {
        teamId: team.id,
        invitedBy: user.id,
        email: user.email,
        role: 'OWNER',
        kind: 'FIRST_OWNER',
        token: randomUUID(),
        expires: new Date(Date.now() + 60_000),
        consumedAt: new Date(),
        consumedByUserId: user.id,
      },
    });
    const provisioningAudit = await prisma.controlPlaneAuditEvent.create({
      data: {
        scopeKind: 'WORKSPACE',
        teamId: team.id,
        actorKind: 'OPERATOR',
        actorId: 'operator-provision',
        executor: 'ops:provision-workspace',
        eventType: 'workspace.provisioning_started',
        resourceType: 'Team',
        resourceId: team.id,
        details: { invitationId: invitation.id },
      },
    });
    await prisma.mfaCredential.create({
      data: {
        userId: user.id,
        protectedSecret: { ciphertext: 'redacted' },
        verifiedAt: new Date(),
      },
    });
    await prisma.mfaRecoveryCode.create({
      data: { userId: user.id, codeHash: 'redacted-hash' },
    });
    await prisma.session.create({
      data: {
        sessionToken: randomUUID(),
        userId: user.id,
        expires: new Date(Date.now() + 60_000),
      },
    });

    const caseId = 'CASE-' + suffix;
    await createMfaResetCase(prisma, {
      caseId,
      teamSlug: team.slug,
      targetUserId: user.id,
      operator: operator('operator-request'),
      reason: 'Customer verified support recovery request',
    });
    const evidence = await beginMfaRecoveryEvidence({
      caseId,
      email: user.email,
      password,
    });
    await verifyMfaRecoveryEmailChallenge({
      caseId,
      recoveryToken: evidence.recoveryToken,
      challenge: evidence.challengeForDelivery,
    });
    await approveMfaResetCase(prisma, {
      caseId,
      operator: operator('operator-one'),
    });

    const race = await Promise.allSettled([
      approveMfaResetCase(prisma, {
        caseId,
        operator: operator('operator-two'),
      }),
      createAuthSession(user.id, 3600, 0, new Date()),
    ]);
    expect(race.some((result) => result.status === 'fulfilled')).toBe(true);
    await expect(
      approveMfaResetCase(prisma, {
        caseId,
        operator: operator('operator-two'),
      })
    ).resolves.toMatchObject({ status: 'COMPLETED', completed: true });

    expect(await prisma.session.count({ where: { userId: user.id } })).toBe(0);
    expect(
      await prisma.mfaCredential.count({ where: { userId: user.id } })
    ).toBe(0);
    expect(
      await prisma.mfaRecoveryCode.count({ where: { userId: user.id } })
    ).toBe(0);
    await expect(
      prisma.user.findUnique({ where: { id: user.id } })
    ).resolves.toMatchObject({
      credentialVersion: 1,
    });
    await expect(
      prisma.mfaResetCase.findUnique({ where: { id: caseId } })
    ).resolves.toMatchObject({
      status: 'COMPLETED',
      provisioningAuditEventId: provisioningAudit.id,
    });
    expect(
      await prisma.controlPlaneAuditEvent.count({
        where: { eventType: 'mfa_reset.completed', resourceId: caseId },
      })
    ).toBe(1);
  });
});

if (!controlUrl) {
  describe('MFA reset (PostgreSQL)', () => {
    it('is skipped without CONTROL_PLANE_DATABASE_URL', () => {
      expect(controlUrl).toBeUndefined();
    });
  });
}
