import crypto from 'node:crypto';
import type { Prisma, PrismaClient } from '@prisma/client';
import { Prisma as PrismaRuntime } from '@prisma/client';

import { ApiError } from './errors';
import env from './env';
import { dispatchWorkspaceOutboxNow } from './entitlements/immediate-dispatch';
import { publishSnapshotInTx } from './entitlements/publication';
import type { VerifiedOperatorIdentity } from './operator-identity';

const FIRST_OWNER_INVITE_MS = 14 * 24 * 60 * 60 * 1000;
const CASE_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{5,63}$/;
const OPERATOR_CASE_TTL_MS = 24 * 60 * 60 * 1000;

export const OPERATOR_ACTION = {
  FIRST_OWNER_REISSUE: 'FIRST_OWNER_REISSUE',
  PROVISIONING_RECONCILE: 'PROVISIONING_RECONCILE',
} as const;

type ProvisionParams = {
  name: string;
  slug: string;
  ownerEmail: string;
  operatorUserId: string;
  betaPlanVersionId: string;
  signingKeyId: string;
  signingKey: Buffer;
};

export async function provisionFirstOwnerWorkspace(
  db: PrismaClient,
  params: ProvisionParams
) {
  const email = params.ownerEmail.trim().toLowerCase();
  const provisioned = await db.$transaction(
    async (tx) => {
      const plan = await tx.planVersion.findUnique({
        where: { id: params.betaPlanVersionId },
        select: { planKey: true, state: true, entitlements: true },
      });
      const entitlements = plan?.entitlements as Record<string, unknown> | null;
      if (
        !plan ||
        plan.state !== 'EFFECTIVE' ||
        !plan.planKey.toLowerCase().startsWith('beta') ||
        entitlements?.controlled_mock !== true
      ) {
        throw new ApiError(
          409,
          'The selected PlanVersion must be an effective Beta plan with controlled_mock enabled.'
        );
      }

      const existing = await tx.team.findUnique({
        where: { slug: params.slug },
      });
      if (existing && existing.lifecycle !== 'PROVISIONING') {
        throw new ApiError(
          409,
          'The Workspace slug is already active or unavailable.'
        );
      }
      const team =
        existing ??
        (await tx.team.create({
          data: {
            name: params.name,
            slug: params.slug,
            lifecycle: 'PROVISIONING',
          },
        }));

      const activeBinding = await tx.commercialAccessBinding.findFirst({
        where: { teamId: team.id, deactivatedAt: null },
      });
      if (!activeBinding) {
        const now = new Date();
        const grant = await tx.accessGrant.create({
          data: {
            teamId: team.id,
            planVersionId: params.betaPlanVersionId,
            grantKind: 'BASE',
            source: 'operator_invite',
            reason: 'Initial Beta access grant',
            periodAnchor: now,
            startsAt: now,
            grantedBy: params.operatorUserId,
          },
        });
        await tx.commercialAccessBinding.create({
          data: {
            teamId: team.id,
            sourceKind: 'ACCESS_GRANT',
            accessGrantId: grant.id,
            planVersionId: params.betaPlanVersionId,
            effectiveAt: now,
          },
        });
        await tx.commercialAccessProjection.create({
          data: { teamId: team.id },
        });
      } else if (activeBinding.planVersionId !== params.betaPlanVersionId) {
        throw new ApiError(
          409,
          'Workspace provisioning is bound to another plan.'
        );
      }

      const firstOwnerInvitation = await tx.invitation.findFirst({
        where: { teamId: team.id, kind: 'FIRST_OWNER' },
        orderBy: { createdAt: 'desc' },
      });
      if (firstOwnerInvitation && firstOwnerInvitation.email !== email) {
        throw new ApiError(
          409,
          'This Workspace is already bound to another first-Owner invitation.'
        );
      }
      const currentInvitation = await tx.invitation.findFirst({
        where: { teamId: team.id, email, kind: 'FIRST_OWNER' },
        orderBy: { createdAt: 'desc' },
      });
      if (currentInvitation?.consumedAt) {
        const member = await tx.teamMember.findFirst({
          where: {
            teamId: team.id,
            ...(currentInvitation.consumedByUserId
              ? { userId: currentInvitation.consumedByUserId }
              : { id: '__missing__' }),
          },
        });
        if (member?.role === 'OWNER') {
          return { team, invitation: currentInvitation, replay: true };
        }
        throw new ApiError(
          409,
          'The first-Owner invitation has already been consumed.'
        );
      }
      const invitation =
        currentInvitation ??
        (await tx.invitation.create({
          data: {
            teamId: team.id,
            email,
            role: 'OWNER',
            kind: 'FIRST_OWNER',
            token: crypto.randomUUID(),
            expires: new Date(Date.now() + FIRST_OWNER_INVITE_MS),
            invitedBy: params.operatorUserId,
          },
        }));

      await tx.onboardingProgress.upsert({
        where: { teamId: team.id },
        create: {
          teamId: team.id,
          workspacePreparedAt: new Date(),
          version: 1,
        },
        update: {},
      });

      const projection = await tx.commercialAccessProjection.findUnique({
        where: { teamId: team.id },
        select: { currentSnapshotId: true },
      });
      if (!projection?.currentSnapshotId) {
        await publishSnapshotInTx(
          tx,
          team.id,
          params.signingKeyId,
          params.signingKey
        );
      }

      await tx.controlPlaneAuditEvent.create({
        data: {
          scopeKind: 'WORKSPACE',
          teamId: team.id,
          actorKind: 'OPERATOR',
          actorId: params.operatorUserId,
          executor: 'ops:provision-workspace',
          eventType: existing
            ? 'workspace.provisioning_retried'
            : 'workspace.provisioning_started',
          resourceType: 'Team',
          resourceId: team.id,
          details: {
            ownerEmail: email,
            planVersionId: params.betaPlanVersionId,
          },
        },
      });
      await tx.controlPlaneOutboxEvent.create({
        data: {
          teamId: team.id,
          eventType: 'workspace.first_owner_invited',
          payload: {
            invitationId: invitation.id,
            email,
            expiresAt: invitation.expires.toISOString(),
          },
        },
      });
      return { team, invitation, replay: Boolean(existing) };
    },
    { isolationLevel: PrismaRuntime.TransactionIsolationLevel.Serializable }
  );
  // Activation waits for this provisioning Snapshot to be delivered, so an
  // Owner who accepts and enrolls MFA within minutes is not held up by the
  // scheduled dispatcher.
  await dispatchWorkspaceOutboxNow(db, provisioned.team.id);
  return provisioned;
}

async function requestOperatorActionCase(
  db: PrismaClient,
  params: {
    action: (typeof OPERATOR_ACTION)[keyof typeof OPERATOR_ACTION];
    teamId: string;
    targetResourceId: string;
    operator: VerifiedOperatorIdentity;
    caseId: string;
    reason: string;
  }
) {
  if (!CASE_PATTERN.test(params.caseId)) {
    throw new ApiError(400, 'A stable support case ID is required.');
  }
  if (params.reason.trim().length < 8) {
    throw new ApiError(
      400,
      'A reissue reason of at least 8 characters is required.'
    );
  }
  return db.$transaction(async (tx) => {
    const existing = await tx.operatorActionCase.findUnique({
      where: { id: params.caseId },
    });
    if (existing) {
      if (
        existing.teamId === params.teamId &&
        existing.action === params.action &&
        existing.targetResourceId === params.targetResourceId &&
        existing.requestedBySubject === params.operator.subject &&
        existing.reason === params.reason.trim()
      ) {
        return existing;
      }
      throw new ApiError(409, 'Support case ID is already in use.');
    }
    const actionCase = await tx.operatorActionCase.create({
      data: {
        id: params.caseId,
        teamId: params.teamId,
        action: params.action,
        targetResourceId: params.targetResourceId,
        requestedBySubject: params.operator.subject,
        reason: params.reason.trim(),
        expiresAt: new Date(Date.now() + OPERATOR_CASE_TTL_MS),
      },
    });
    await tx.controlPlaneAuditEvent.create({
      data: {
        scopeKind: 'WORKSPACE',
        teamId: params.teamId,
        actorKind: 'OPERATOR',
        actorId: params.operator.subject,
        executor: 'control-plane',
        eventType: 'operator_action.requested',
        resourceType: 'OperatorActionCase',
        resourceId: actionCase.id,
        details: {
          action: params.action,
          targetResourceId: params.targetResourceId,
          reason: params.reason.trim(),
          operatorIssuer: params.operator.issuer,
          operatorAssertionId: params.operator.assertionId,
          operatorKeyId: params.operator.keyId,
        },
      },
    });
    await tx.controlPlaneOutboxEvent.create({
      data: {
        teamId: params.teamId,
        eventType: 'operator_action.approval_requested',
        payload: { caseId: actionCase.id, action: params.action },
      },
    });
    return actionCase;
  });
}

export async function requestFirstOwnerReissueCase(
  db: PrismaClient,
  params: {
    teamId: string;
    email: string;
    operator: VerifiedOperatorIdentity;
    caseId: string;
    reason: string;
  }
) {
  const invitation = await db.invitation.findFirst({
    where: {
      teamId: params.teamId,
      email: params.email.trim().toLowerCase(),
      kind: 'FIRST_OWNER',
      consumedAt: null,
    },
    orderBy: { createdAt: 'desc' },
  });
  if (!invitation || invitation.kind !== 'FIRST_OWNER') {
    throw new ApiError(404, 'First-Owner invitation not found.');
  }
  if (invitation.consumedAt) {
    throw new ApiError(409, 'A consumed invitation cannot be reissued.');
  }
  if (!invitation.revokedAt && invitation.expires > new Date()) {
    throw new ApiError(
      409,
      'Only an expired or revoked invitation can be reissued.'
    );
  }
  return requestOperatorActionCase(db, {
    action: OPERATOR_ACTION.FIRST_OWNER_REISSUE,
    teamId: params.teamId,
    targetResourceId: invitation.id,
    operator: params.operator,
    caseId: params.caseId,
    reason: params.reason,
  });
}

export async function requestProvisioningReconcileCase(
  db: PrismaClient,
  params: {
    teamId: string;
    operator: VerifiedOperatorIdentity;
    caseId: string;
    reason: string;
  }
) {
  const team = await db.team.findUnique({ where: { id: params.teamId } });
  if (!team) throw new ApiError(404, 'Workspace not found.');
  if (team.lifecycle !== 'PROVISIONING') {
    throw new ApiError(409, 'Only a provisioning Workspace can be reconciled.');
  }
  return requestOperatorActionCase(db, {
    action: OPERATOR_ACTION.PROVISIONING_RECONCILE,
    teamId: params.teamId,
    targetResourceId: params.teamId,
    operator: params.operator,
    caseId: params.caseId,
    reason: params.reason,
  });
}

/** Persist one approval. Two distinct verified subjects move the case forward. */
export async function approveOperatorActionCase(
  db: PrismaClient,
  params: { caseId: string; operator: VerifiedOperatorIdentity }
) {
  return db.$transaction(async (tx) => {
    const rows = await tx.$queryRaw<Array<{ id: string }>>`
      SELECT "id" FROM "OperatorActionCase"
      WHERE "id" = ${params.caseId} FOR UPDATE
    `;
    if (!rows[0]) throw new ApiError(404, 'Operator action case not found.');
    const actionCase = await tx.operatorActionCase.findUniqueOrThrow({
      where: { id: params.caseId },
    });
    if (actionCase.completedAt || actionCase.status === 'COMPLETED') {
      return actionCase;
    }
    if (actionCase.expiresAt <= new Date()) {
      throw new ApiError(409, 'Operator action case has expired.');
    }
    const existing = await tx.operatorActionApproval.findUnique({
      where: {
        caseId_operatorSubject: {
          caseId: actionCase.id,
          operatorSubject: params.operator.subject,
        },
      },
    });
    if (!existing) {
      await tx.operatorActionApproval.create({
        data: {
          caseId: actionCase.id,
          operatorSubject: params.operator.subject,
          operatorIssuer: params.operator.issuer,
        },
      });
      const approvalCount = await tx.operatorActionApproval.count({
        where: { caseId: actionCase.id },
      });
      await tx.controlPlaneAuditEvent.create({
        data: {
          scopeKind: 'WORKSPACE',
          teamId: actionCase.teamId,
          actorKind: 'OPERATOR',
          actorId: params.operator.subject,
          executor: 'control-plane',
          eventType: 'operator_action.approved',
          resourceType: 'OperatorActionCase',
          resourceId: actionCase.id,
          details: {
            action: actionCase.action,
            approvalCount,
            operatorIssuer: params.operator.issuer,
            operatorAssertionId: params.operator.assertionId,
            operatorKeyId: params.operator.keyId,
          },
        },
      });
      if (approvalCount >= 2) {
        return tx.operatorActionCase.update({
          where: { id: actionCase.id },
          data: { status: 'APPROVED' },
        });
      }
    }
    return tx.operatorActionCase.findUniqueOrThrow({
      where: { id: actionCase.id },
    });
  });
}

async function approvedActionCase(
  tx: Prisma.TransactionClient,
  caseId: string,
  action: (typeof OPERATOR_ACTION)[keyof typeof OPERATOR_ACTION]
) {
  const actionCase = await tx.operatorActionCase.findUnique({
    where: { id: caseId },
  });
  if (!actionCase || actionCase.action !== action) {
    throw new ApiError(404, 'Approved operator action case not found.');
  }
  if (actionCase.status === 'COMPLETED') return actionCase;
  if (
    actionCase.status !== 'APPROVED' ||
    actionCase.expiresAt <= new Date() ||
    (await tx.operatorActionApproval.count({ where: { caseId } })) < 2
  ) {
    throw new ApiError(409, 'Two current operator approvals are required.');
  }
  return actionCase;
}

export async function executeApprovedFirstOwnerReissue(
  db: PrismaClient,
  params: { caseId: string; operator: VerifiedOperatorIdentity }
) {
  return db.$transaction(async (tx) => {
    await tx.$queryRaw<Array<{ id: string }>>`
      SELECT "id" FROM "OperatorActionCase"
      WHERE "id" = ${params.caseId} FOR UPDATE
    `;
    const actionCase = await approvedActionCase(
      tx,
      params.caseId,
      OPERATOR_ACTION.FIRST_OWNER_REISSUE
    );
    const existingInvitation = await tx.invitation.findUnique({
      where: { id: actionCase.targetResourceId },
    });
    if (actionCase.status === 'COMPLETED') {
      if (!actionCase.resultResourceId) {
        throw new ApiError(409, 'Completed case has no invitation result.');
      }
      return tx.invitation.findUniqueOrThrow({
        where: { id: actionCase.resultResourceId },
      });
    }
    if (
      !existingInvitation ||
      existingInvitation.teamId !== actionCase.teamId ||
      existingInvitation.kind !== 'FIRST_OWNER' ||
      existingInvitation.consumedAt
    ) {
      throw new ApiError(
        409,
        'The approved invitation target is no longer valid.'
      );
    }
    const activeReplacement = await tx.invitation.findFirst({
      where: {
        teamId: existingInvitation.teamId,
        kind: 'FIRST_OWNER',
        consumedAt: null,
        revokedAt: null,
        id: { not: existingInvitation.id },
      },
    });
    if (activeReplacement) {
      throw new ApiError(
        409,
        'Another active first-Owner invitation already exists.'
      );
    }
    if (!existingInvitation.revokedAt) {
      await tx.invitation.update({
        where: { id: existingInvitation.id },
        data: {
          revokedAt: new Date(),
          revokedReason: `superseded_by_case:${actionCase.id}`,
        },
      });
    }
    const expires = new Date(Date.now() + FIRST_OWNER_INVITE_MS);
    const invitation = await tx.invitation.create({
      data: {
        teamId: existingInvitation.teamId,
        email: existingInvitation.email,
        role: existingInvitation.role,
        kind: existingInvitation.kind,
        token: crypto.randomUUID(),
        expires,
        invitedBy: existingInvitation.invitedBy,
      },
    });
    await tx.operatorActionCase.update({
      where: { id: actionCase.id },
      data: {
        status: 'COMPLETED',
        completedAt: new Date(),
        resultResourceId: invitation.id,
      },
    });
    await tx.controlPlaneAuditEvent.create({
      data: {
        scopeKind: 'WORKSPACE',
        teamId: actionCase.teamId,
        actorKind: 'OPERATOR',
        actorId: params.operator.subject,
        executor: 'ops:reissue-first-owner',
        eventType: 'invitation.reissued',
        resourceType: 'Invitation',
        resourceId: invitation.id,
        details: {
          caseId: actionCase.id,
          expiresAt: expires.toISOString(),
          operatorIssuer: params.operator.issuer,
          operatorAssertionId: params.operator.assertionId,
          operatorKeyId: params.operator.keyId,
        },
      },
    });
    await tx.controlPlaneOutboxEvent.create({
      data: {
        teamId: actionCase.teamId,
        eventType: 'workspace.first_owner_invitation_reissued',
        payload: { invitationId: invitation.id, caseId: actionCase.id },
      },
    });
    return invitation;
  });
}

export type ActivateWorkspaceResult = {
  team: { id: string; lifecycle: string };
  activeSnapshotVersion: number;
};

export async function activateWorkspaceAndPublish(
  db: PrismaClient,
  params: {
    teamId: string;
    signingKeyId: string;
    signingKey: Buffer;
  }
): Promise<ActivateWorkspaceResult> {
  const activated = await db.$transaction(
    async (tx) => {
      const locked = await tx.$queryRaw<Array<{ id: string }>>`
        SELECT "id" FROM "Team" WHERE "id" = ${params.teamId} FOR UPDATE
      `;
      if (!locked[0]) throw new ApiError(404, 'Workspace not found.');
      const team = await tx.team.findUniqueOrThrow({
        where: { id: params.teamId },
      });
      if (team.lifecycle === 'ACTIVE') {
        throw new ApiError(409, 'Workspace is already active.');
      }
      if (team.lifecycle !== 'PROVISIONING') {
        throw new ApiError(
          409,
          `Cannot activate workspace in lifecycle ${team.lifecycle}.`
        );
      }

      await tx.team.update({
        where: { id: params.teamId },
        data: { lifecycle: 'ACTIVE' },
      });

      const snapshotResult = await publishSnapshotInTx(
        tx,
        params.teamId,
        params.signingKeyId,
        params.signingKey
      );

      await tx.controlPlaneAuditEvent.create({
        data: {
          scopeKind: 'WORKSPACE',
          teamId: params.teamId,
          actorKind: 'SYSTEM',
          actorId: 'workspace-activation',
          executor: 'control-plane',
          eventType: 'workspace.activated',
          resourceType: 'Team',
          resourceId: params.teamId,
          details: {
            snapshotId: snapshotResult.snapshotId,
            entitlementVersion: snapshotResult.entitlementVersion,
          },
        },
      });

      await tx.controlPlaneOutboxEvent.create({
        data: {
          teamId: params.teamId,
          eventType: 'workspace.activated',
          payload: { workspaceId: params.teamId },
        },
      });

      return {
        team: { id: params.teamId, lifecycle: 'ACTIVE' as const },
        activeSnapshotVersion: snapshotResult.entitlementVersion,
      };
    },
    { isolationLevel: PrismaRuntime.TransactionIsolationLevel.Serializable }
  );
  // Until this Snapshot arrives the domain has no active row for the
  // Workspace, and its pages fail (#9).
  await dispatchWorkspaceOutboxNow(db, params.teamId);
  return activated;
}

export async function reconcileProvisioningWorkspace(
  db: PrismaClient,
  params: { teamId: string; signingKeyId: string; signingKey: Buffer }
): Promise<'waiting' | 'activated' | 'already_active'> {
  const outcome = await activateProvisioningWorkspaceIfReady(db, params);
  if (outcome === 'activated') {
    // Same as `activateWorkspaceAndPublish`: deliver the active Snapshot now.
    await dispatchWorkspaceOutboxNow(db, params.teamId);
  }
  return outcome;
}

async function activateProvisioningWorkspaceIfReady(
  db: PrismaClient,
  params: { teamId: string; signingKeyId: string; signingKey: Buffer }
): Promise<'waiting' | 'activated' | 'already_active'> {
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      return await db.$transaction(
        async (tx) => {
          const locked = await tx.$queryRaw<Array<{ id: string }>>`
            SELECT "id" FROM "Team" WHERE "id" = ${params.teamId} FOR UPDATE
          `;
          if (!locked[0]) throw new ApiError(404, 'Workspace not found.');
          const team = await tx.team.findUniqueOrThrow({
            where: { id: params.teamId },
          });
          if (team.lifecycle === 'ACTIVE') return 'already_active';
          if (team.lifecycle !== 'PROVISIONING') {
            throw new ApiError(
              409,
              `Cannot reconcile lifecycle ${team.lifecycle}.`
            );
          }
          const [owner, projection] = await Promise.all([
            tx.teamMember.findFirst({
              where: { teamId: team.id, role: 'OWNER' },
              include: { user: { include: { mfaCredential: true } } },
            }),
            tx.commercialAccessProjection.findUnique({
              where: { teamId: team.id },
              include: { currentSnapshot: { include: { outboxEntry: true } } },
            }),
          ]);
          if (
            !owner?.user.mfaCredential?.verifiedAt ||
            !projection?.currentSnapshot?.outboxEntry?.deliveredAt
          )
            return 'waiting';

          await tx.team.update({
            where: { id: team.id },
            data: { lifecycle: 'ACTIVE' },
          });
          await publishSnapshotInTx(
            tx,
            team.id,
            params.signingKeyId,
            params.signingKey
          );
          await tx.controlPlaneAuditEvent.create({
            data: {
              scopeKind: 'WORKSPACE',
              teamId: team.id,
              actorKind: 'SYSTEM',
              actorId: 'provisioning-reconciler',
              executor: 'control-plane',
              eventType: 'workspace.activated',
              resourceType: 'Team',
              resourceId: team.id,
              details: { ownerMembershipId: owner.id },
            },
          });
          await tx.controlPlaneOutboxEvent.create({
            data: {
              teamId: team.id,
              eventType: 'workspace.activated',
              payload: { workspaceId: team.id },
            },
          });
          return 'activated';
        },
        { isolationLevel: PrismaRuntime.TransactionIsolationLevel.Serializable }
      );
    } catch (error) {
      const retryable =
        error instanceof PrismaRuntime.PrismaClientKnownRequestError &&
        error.code === 'P2034';
      if (!retryable || attempt === 2) throw error;
    }
  }
  throw new ApiError(503, 'Provisioning reconciliation did not converge.');
}

/**
 * Retry normal provisioning from a request event without exposing signing
 * material to the caller. Missing runtime configuration leaves the durable
 * state waiting for the scheduled dispatcher cycle.
 */
export async function reconcileProvisioningWorkspaceFromRuntime(
  db: PrismaClient,
  teamId: string
): Promise<'waiting' | 'activated' | 'already_active'> {
  if (!env.entitlementSnapshotSigningKey) return 'waiting';
  return reconcileProvisioningWorkspace(db, {
    teamId,
    signingKeyId: process.env.ENTITLEMENT_SNAPSHOT_SIGNING_KEY_ID || 'beta-v1',
    signingKey: Buffer.from(env.entitlementSnapshotSigningKey, 'utf8'),
  });
}

/**
 * Bounded scheduled convergence. The readiness predicate prevents permanently
 * waiting rows from starving later Workspaces, while each individual
 * reconciliation takes a Team row lock before producing side effects.
 */
export async function reconcileReadyProvisioningWorkspaces(
  db: PrismaClient,
  params: { signingKeyId: string; signingKey: Buffer; batchSize?: number }
) {
  const batchSize = Math.max(1, Math.min(params.batchSize ?? 50, 100));
  const ready = await db.team.findMany({
    where: {
      lifecycle: 'PROVISIONING',
      members: {
        some: {
          role: 'OWNER',
          user: { mfaCredential: { is: { verifiedAt: { not: null } } } },
        },
      },
      commercialAccessProjection: {
        is: {
          currentSnapshot: {
            is: { outboxEntry: { is: { deliveredAt: { not: null } } } },
          },
        },
      },
    },
    orderBy: { id: 'asc' },
    take: batchSize,
    select: { id: true },
  });
  let activated = 0;
  let alreadyActive = 0;
  let failed = 0;
  for (const team of ready) {
    try {
      const outcome = await reconcileProvisioningWorkspace(db, {
        teamId: team.id,
        signingKeyId: params.signingKeyId,
        signingKey: params.signingKey,
      });
      if (outcome === 'activated') activated++;
      if (outcome === 'already_active') alreadyActive++;
    } catch {
      failed++;
    }
  }
  return { examined: ready.length, activated, alreadyActive, failed };
}

export async function executeApprovedProvisioningReconcile(
  db: PrismaClient,
  params: {
    caseId: string;
    operator: VerifiedOperatorIdentity;
    signingKeyId: string;
    signingKey: Buffer;
  }
) {
  const actionCase = await db.$transaction((tx) =>
    approvedActionCase(
      tx,
      params.caseId,
      OPERATOR_ACTION.PROVISIONING_RECONCILE
    )
  );
  if (actionCase.status === 'COMPLETED') return 'already_active' as const;

  const outcome = await reconcileProvisioningWorkspace(db, {
    teamId: actionCase.teamId,
    signingKeyId: params.signingKeyId,
    signingKey: params.signingKey,
  });

  await db.$transaction(async (tx) => {
    await tx.controlPlaneAuditEvent.create({
      data: {
        scopeKind: 'WORKSPACE',
        teamId: actionCase.teamId,
        actorKind: 'OPERATOR',
        actorId: params.operator.subject,
        executor: 'ops:reconcile-workspace-provisioning',
        eventType: 'operator_action.execution_attempted',
        resourceType: 'OperatorActionCase',
        resourceId: actionCase.id,
        details: {
          action: actionCase.action,
          outcome,
          operatorIssuer: params.operator.issuer,
          operatorAssertionId: params.operator.assertionId,
          operatorKeyId: params.operator.keyId,
        },
      },
    });
    await tx.controlPlaneOutboxEvent.create({
      data: {
        teamId: actionCase.teamId,
        eventType: 'operator_action.execution_attempted',
        payload: { caseId: actionCase.id, action: actionCase.action, outcome },
      },
    });
    if (outcome !== 'waiting') {
      await tx.operatorActionCase.updateMany({
        where: { id: actionCase.id, status: 'APPROVED' },
        data: { status: 'COMPLETED', completedAt: new Date() },
      });
    }
  });
  return outcome;
}

export type OnboardingDomainFactType =
  | 'resolution_preview.validated'
  | 'generation_recipe_snapshot.mock_created'
  | 'asset_version.qualified'
  | 'quality_review.human_recorded';

const FACT_MILESTONE: Record<
  OnboardingDomainFactType,
  | 'validPreviewAt'
  | 'mockRecipeSnapshotAt'
  | 'assetQualifiedAt'
  | 'humanReviewAt'
> = {
  'resolution_preview.validated': 'validPreviewAt',
  'generation_recipe_snapshot.mock_created': 'mockRecipeSnapshotAt',
  'asset_version.qualified': 'assetQualifiedAt',
  'quality_review.human_recorded': 'humanReviewAt',
};

/** Called only from trusted domain reconciliation: completion is monotonic. */
export async function recordOnboardingFact(
  tx: Prisma.TransactionClient,
  params: {
    teamId: string;
    sourceEventId: string;
    factType: OnboardingDomainFactType;
    sourceResourceId: string;
    observedAt: Date;
  }
) {
  const milestone = FACT_MILESTONE[params.factType];
  const receipt = await tx.onboardingDomainFact.findUnique({
    where: { sourceEventId: params.sourceEventId },
  });
  if (receipt) {
    if (
      receipt.teamId !== params.teamId ||
      receipt.factType !== params.factType ||
      receipt.sourceResourceId !== params.sourceResourceId
    ) {
      throw new ApiError(
        409,
        'Domain fact event ID is already bound elsewhere.'
      );
    }
    return {
      progress: await tx.onboardingProgress.findUniqueOrThrow({
        where: { teamId: params.teamId },
      }),
      receiptCreated: false,
    };
  }
  await tx.onboardingDomainFact.create({
    data: {
      teamId: params.teamId,
      sourceEventId: params.sourceEventId,
      factType: params.factType,
      sourceResourceId: params.sourceResourceId,
      observedAt: params.observedAt,
    },
  });
  const current = await tx.onboardingProgress.findUnique({
    where: { teamId: params.teamId },
  });
  if (current?.[milestone]) {
    return { progress: current, receiptCreated: true };
  }
  const progress = await tx.onboardingProgress.upsert({
    where: { teamId: params.teamId },
    create: {
      teamId: params.teamId,
      [milestone]: params.observedAt,
      version: 1,
    },
    update: { [milestone]: params.observedAt, version: { increment: 1 } },
  });
  return { progress, receiptCreated: true };
}
