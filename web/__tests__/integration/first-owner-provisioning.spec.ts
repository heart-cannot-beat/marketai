import { createHash, randomUUID } from 'node:crypto';

import { verifiedTestOperator } from '../helpers/operator-identity';

export {};

const controlUrl = process.env.CONTROL_PLANE_DATABASE_URL;
const describeIfConfigured = controlUrl ? describe : describe.skip;

describeIfConfigured('first-Owner Beta provisioning (PostgreSQL)', () => {
  jest.setTimeout(45_000);

  let prisma: any;
  let hashPassword: (value: string) => Promise<string>;
  let provisionFirstOwnerWorkspace: any;
  let requestFirstOwnerReissueCase: any;
  let approveOperatorActionCase: any;
  let executeApprovedFirstOwnerReissue: any;
  let requestProvisioningReconcileCase: any;
  let reconcileProvisioningWorkspace: any;
  let reconcileReadyProvisioningWorkspaces: any;
  let acceptInvitationForNewUser: any;
  let consumeInvitation: any;
  const operator = verifiedTestOperator;

  beforeAll(async () => {
    process.env.DATABASE_URL = controlUrl as string;
    ({ prisma } = await import('../../lib/prisma'));
    ({ hashPassword } = await import('../../lib/auth'));
    ({
      provisionFirstOwnerWorkspace,
      requestFirstOwnerReissueCase,
      approveOperatorActionCase,
      executeApprovedFirstOwnerReissue,
      requestProvisioningReconcileCase,
      reconcileProvisioningWorkspace,
      reconcileReadyProvisioningWorkspaces,
    } = await import('../../lib/onboarding'));
    ({ acceptInvitationForNewUser, consumeInvitation } =
      await import('../../models/invitation'));
  });

  afterAll(async () => {
    await prisma?.$disconnect();
  });

  const plan = async (suffix: string) => {
    const entitlements = { controlled_mock: true };
    return prisma.planVersion.create({
      data: {
        planKey: `beta-${suffix}`,
        versionNo: 1,
        displayName: `Beta ${suffix}`,
        state: 'EFFECTIVE',
        entitlements,
        entitlementsHash: createHash('sha256')
          .update(JSON.stringify(entitlements))
          .digest('hex'),
        publishedBy: 'operator-plan',
      },
    });
  };

  it('reuses provisioning state, accepts once, and activates only after dependencies', async () => {
    const suffix = randomUUID().slice(0, 8);
    const provisioningOperator = await prisma.user.create({
      data: {
        name: 'Beta Operator',
        email: `operator-${suffix}@example.test`,
      },
    });
    const betaPlan = await plan(suffix);
    const params = {
      name: `Workspace ${suffix}`,
      slug: `first-owner-${suffix}`,
      ownerEmail: `owner-${suffix}@example.test`,
      operatorUserId: provisioningOperator.id,
      betaPlanVersionId: betaPlan.id,
      signingKeyId: 'test-key',
      signingKey: Buffer.from('test-signing-key-at-least-32-bytes'),
    };

    const first = await provisionFirstOwnerWorkspace(prisma, params);
    const retry = await provisionFirstOwnerWorkspace(prisma, params);
    expect(retry).toMatchObject({ replay: true });
    expect(retry.team.id).toBe(first.team.id);
    expect(retry.invitation.id).toBe(first.invitation.id);
    expect(
      await prisma.accessGrant.count({ where: { teamId: first.team.id } })
    ).toBe(1);
    expect(
      await prisma.invitation.count({
        where: { teamId: first.team.id, kind: 'FIRST_OWNER' },
      })
    ).toBe(1);

    const accepted = await acceptInvitationForNewUser({
      token: first.invitation.token,
      name: 'First Owner',
      password: await hashPassword('correct horse battery staple'),
      emailVerified: new Date(),
    });
    await expect(
      consumeInvitation({
        token: first.invitation.token,
        userId: accepted.user.id,
        email: params.ownerEmail.toUpperCase(),
      })
    ).resolves.toMatchObject({ id: accepted.membership.id, role: 'OWNER' });
    expect(
      await prisma.teamMember.count({ where: { teamId: first.team.id } })
    ).toBe(1);

    await expect(
      reconcileProvisioningWorkspace(prisma, {
        teamId: first.team.id,
        signingKeyId: params.signingKeyId,
        signingKey: params.signingKey,
      })
    ).resolves.toBe('waiting');

    await prisma.mfaCredential.create({
      data: {
        userId: accepted.user.id,
        protectedSecret: { ciphertext: 'redacted' },
        verifiedAt: new Date(),
      },
    });
    const projection = await prisma.commercialAccessProjection.findUnique({
      where: { teamId: first.team.id },
      select: { currentSnapshotId: true },
    });
    await prisma.entitlementSnapshotOutbox.update({
      where: { snapshotId: projection.currentSnapshotId },
      data: { deliveryState: 'DELIVERED', deliveredAt: new Date() },
    });

    const scans = await Promise.all([
      reconcileReadyProvisioningWorkspaces(prisma, {
        signingKeyId: params.signingKeyId,
        signingKey: params.signingKey,
      }),
      reconcileReadyProvisioningWorkspaces(prisma, {
        signingKeyId: params.signingKeyId,
        signingKey: params.signingKey,
      }),
    ]);
    expect(scans.reduce((total, scan) => total + scan.activated, 0)).toBe(1);
    await expect(
      prisma.team.findUnique({ where: { id: first.team.id } })
    ).resolves.toMatchObject({ lifecycle: 'ACTIVE' });
    expect(
      await prisma.controlPlaneAuditEvent.count({
        where: {
          teamId: first.team.id,
          eventType: 'workspace.activated',
        },
      })
    ).toBe(1);
    expect(
      await prisma.entitlementSnapshot.count({
        where: { teamId: first.team.id, lifecycle: 'ACTIVE' },
      })
    ).toBe(1);
    await expect(
      requestProvisioningReconcileCase(prisma, {
        teamId: first.team.id,
        operator: operator('reconcile-requester'),
        caseId: `RECONCILE-${suffix}`,
        reason: 'Normal convergence already activated this Workspace',
      })
    ).rejects.toThrow('Only a provisioning Workspace can be reconciled');
  });

  it('reissues one expired first-Owner invitation with audited case evidence', async () => {
    const suffix = randomUUID().slice(0, 8);
    const provisioningOperator = await prisma.user.create({
      data: {
        name: 'Reissue Operator',
        email: `reissue-operator-${suffix}@example.test`,
      },
    });
    const betaPlan = await plan(`reissue-${suffix}`);
    const provisioned = await provisionFirstOwnerWorkspace(prisma, {
      name: `Reissue ${suffix}`,
      slug: `reissue-${suffix}`,
      ownerEmail: `reissue-owner-${suffix}@example.test`,
      operatorUserId: provisioningOperator.id,
      betaPlanVersionId: betaPlan.id,
      signingKeyId: 'test-key',
      signingKey: Buffer.from('test-signing-key-at-least-32-bytes'),
    });
    await prisma.invitation.update({
      where: { id: provisioned.invitation.id },
      data: { expires: new Date(Date.now() - 1_000) },
    });

    const caseId = `CASE-${suffix}`;
    await requestFirstOwnerReissueCase(prisma, {
      teamId: provisioned.team.id,
      email: provisioned.invitation.email,
      operator: operator('support-requester'),
      caseId,
      reason: 'Customer requested an expired invitation replacement',
    });
    await expect(
      approveOperatorActionCase(prisma, {
        caseId,
        operator: operator('support-approver-one'),
      })
    ).resolves.toMatchObject({ status: 'AWAITING_APPROVAL' });
    await expect(
      approveOperatorActionCase(prisma, {
        caseId,
        operator: operator('support-approver-one'),
      })
    ).resolves.toMatchObject({ status: 'AWAITING_APPROVAL' });
    await expect(
      approveOperatorActionCase(prisma, {
        caseId,
        operator: operator('support-approver-two'),
      })
    ).resolves.toMatchObject({ status: 'APPROVED' });

    const [reissued, replay] = await Promise.all([
      executeApprovedFirstOwnerReissue(prisma, {
        caseId,
        operator: operator('support-approver-one'),
      }),
      executeApprovedFirstOwnerReissue(prisma, {
        caseId,
        operator: operator('support-approver-two'),
      }),
    ]);
    expect(reissued.id).not.toBe(provisioned.invitation.id);
    expect(reissued.token).not.toBe(provisioned.invitation.token);
    expect(reissued.expires.getTime()).toBeGreaterThan(Date.now());
    expect(replay.token).toBe(reissued.token);
    await expect(
      prisma.invitation.findUnique({
        where: { id: provisioned.invitation.id },
      })
    ).resolves.toMatchObject({
      consumedAt: null,
      revokedReason: `superseded_by_case:${caseId}`,
    });
    expect(
      await prisma.invitation.count({
        where: { teamId: provisioned.team.id, kind: 'FIRST_OWNER' },
      })
    ).toBe(2);
    expect(
      await prisma.operatorActionApproval.count({ where: { caseId } })
    ).toBe(2);
    expect(
      await prisma.controlPlaneAuditEvent.count({
        where: {
          teamId: provisioned.team.id,
          eventType: 'invitation.reissued',
          resourceId: reissued.id,
        },
      })
    ).toBe(1);
    expect(
      await prisma.controlPlaneOutboxEvent.count({
        where: {
          teamId: provisioned.team.id,
          eventType: 'workspace.first_owner_invitation_reissued',
        },
      })
    ).toBe(1);
  });
});

if (!controlUrl) {
  describe('first-Owner Beta provisioning (PostgreSQL)', () => {
    it('is skipped without CONTROL_PLANE_DATABASE_URL', () => {
      expect(controlUrl).toBeUndefined();
    });
  });
}
