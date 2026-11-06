import crypto from 'node:crypto';
import type { Prisma, PrismaClient } from '@prisma/client';

import { verifyPassword } from './auth';
import { ApiError } from './errors';
import { prisma } from './prisma';
import { lockUserCredentials } from '../models/userLock';
import type { VerifiedOperatorIdentity } from './operator-identity';

const CASE_TTL_MS = 24 * 60 * 60 * 1000;
const RECOVERY_SESSION_TTL_MS = 30 * 60 * 1000;
const EMAIL_CHALLENGE_TTL_MS = 10 * 60 * 1000;
const PASSWORD_RESET_COOLING_MS = 24 * 60 * 60 * 1000;
const CASE_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{5,63}$/;

const sha256 = (value: string) =>
  crypto.createHash('sha256').update(value, 'utf8').digest('hex');

const supportError = () =>
  new ApiError(403, 'MFA recovery evidence could not be verified.');

export async function createMfaResetCase(
  db: PrismaClient,
  params: {
    caseId: string;
    teamSlug: string;
    targetUserId: string;
    operator: VerifiedOperatorIdentity;
    reason: string;
  }
) {
  if (!CASE_PATTERN.test(params.caseId)) {
    throw new ApiError(400, 'A stable support case ID is required.');
  }
  if (params.reason.trim().length < 8) {
    throw new ApiError(
      400,
      'A support reason of at least 8 characters is required.'
    );
  }

  const team = await db.team.findUnique({
    where: { slug: params.teamSlug },
    select: { id: true },
  });
  if (!team) throw new ApiError(404, 'Workspace not found.');

  const [owner, betaInvitation, provisioningAudit] = await Promise.all([
    db.teamMember.findUnique({
      where: {
        teamId_userId: { teamId: team.id, userId: params.targetUserId },
      },
      select: { role: true },
    }),
    db.invitation.findFirst({
      where: {
        teamId: team.id,
        kind: 'FIRST_OWNER',
        consumedByUserId: params.targetUserId,
        consumedAt: { not: null },
      },
      select: { id: true },
    }),
    db.controlPlaneAuditEvent.findFirst({
      where: {
        teamId: team.id,
        eventType: 'workspace.provisioning_started',
        resourceId: team.id,
      },
      select: { id: true },
    }),
  ]);
  if (owner?.role !== 'OWNER' || !betaInvitation || !provisioningAudit) {
    throw new ApiError(
      409,
      'Case does not match the original Beta provisioning record.'
    );
  }

  return db.$transaction(async (tx) => {
    const existing = await tx.mfaResetCase.findUnique({
      where: { id: params.caseId },
    });
    if (existing) {
      if (
        existing.teamId === team.id &&
        existing.targetUserId === params.targetUserId &&
        existing.requestedByOperatorId === params.operator.subject &&
        existing.reason === params.reason.trim()
      ) {
        return existing;
      }
      throw new ApiError(409, 'Support case ID is already in use.');
    }

    const resetCase = await tx.mfaResetCase.create({
      data: {
        id: params.caseId,
        teamId: team.id,
        targetUserId: params.targetUserId,
        betaInvitationId: betaInvitation.id,
        provisioningAuditEventId: provisioningAudit.id,
        requestedByOperatorId: params.operator.subject,
        reason: params.reason.trim(),
        expiresAt: new Date(Date.now() + CASE_TTL_MS),
      },
    });
    await tx.controlPlaneAuditEvent.create({
      data: {
        scopeKind: 'WORKSPACE',
        teamId: team.id,
        actorKind: 'OPERATOR',
        actorId: params.operator.subject,
        executor: 'ops:mfa-reset',
        eventType: 'mfa_reset.requested',
        resourceType: 'MfaResetCase',
        resourceId: resetCase.id,
        details: {
          targetUserId: params.targetUserId,
          betaInvitationId: betaInvitation.id,
          provisioningAuditEventId: provisioningAudit.id,
          reason: params.reason.trim(),
          operatorIssuer: params.operator.issuer,
          operatorAssertionId: params.operator.assertionId,
          operatorKeyId: params.operator.keyId,
        },
      },
    });
    await tx.controlPlaneOutboxEvent.create({
      data: {
        teamId: team.id,
        eventType: 'mfa_reset.evidence_required',
        payload: { caseId: resetCase.id, targetUserId: params.targetUserId },
      },
    });
    return resetCase;
  });
}

/**
 * Creates a password-authenticated but recovery-only Session. The raw session
 * token and email challenge are returned only to the HTTP/delivery adapters;
 * only their hashes are persisted.
 */
export async function beginMfaRecoveryEvidence(params: {
  caseId: string;
  email: string;
  password: string;
}) {
  const resetCase = await prisma.mfaResetCase.findUnique({
    where: { id: params.caseId },
    include: { targetUser: true },
  });
  if (
    !resetCase ||
    resetCase.completedAt ||
    resetCase.expiresAt <= new Date() ||
    resetCase.targetUser.email.trim().toLowerCase() !==
      params.email.trim().toLowerCase() ||
    !resetCase.targetUser.emailVerified ||
    !resetCase.targetUser.password ||
    !(await verifyPassword(params.password, resetCase.targetUser.password))
  ) {
    throw supportError();
  }
  const now = new Date();
  if (
    resetCase.targetUser.lastPasswordResetAt &&
    now.getTime() - resetCase.targetUser.lastPasswordResetAt.getTime() <
      PASSWORD_RESET_COOLING_MS
  ) {
    throw new ApiError(
      403,
      'MFA recovery is unavailable for 24 hours after a password reset.'
    );
  }

  const rawSessionToken = crypto.randomBytes(32).toString('base64url');
  const rawChallenge = crypto.randomBytes(6).toString('hex').toUpperCase();
  const result = await prisma.$transaction(async (tx) => {
    const lockedUser = await lockUserCredentials(tx, resetCase.targetUserId);
    if (
      !lockedUser ||
      lockedUser.credentialVersion !== resetCase.targetUser.credentialVersion
    ) {
      throw supportError();
    }
    const lockedCases = await tx.$queryRaw<
      Array<{ id: string; status: string }>
    >`
      SELECT "id", "status" FROM "MfaResetCase"
      WHERE "id" = ${resetCase.id} FOR UPDATE
    `;
    if (!lockedCases[0] || lockedCases[0].status === 'COMPLETED') {
      throw supportError();
    }

    if (resetCase.passwordSessionId) {
      await tx.session.deleteMany({
        where: { id: resetCase.passwordSessionId, recoveryOnly: true },
      });
    }
    const recoverySession = await tx.session.create({
      data: {
        sessionToken: sha256(rawSessionToken),
        userId: resetCase.targetUserId,
        recoveryOnly: true,
        expires: new Date(now.getTime() + RECOVERY_SESSION_TTL_MS),
      },
    });
    const challenge = await tx.mfaRecoveryEmailChallenge.upsert({
      where: { caseId: resetCase.id },
      create: {
        caseId: resetCase.id,
        tokenHash: sha256(rawChallenge),
        expiresAt: new Date(now.getTime() + EMAIL_CHALLENGE_TTL_MS),
      },
      update: {
        tokenHash: sha256(rawChallenge),
        expiresAt: new Date(now.getTime() + EMAIL_CHALLENGE_TTL_MS),
        attempts: 0,
        verifiedAt: null,
      },
    });
    await tx.mfaResetCase.update({
      where: { id: resetCase.id },
      data: {
        passwordSessionId: recoverySession.id,
        passwordVerifiedAt: now,
        passwordRevision: lockedUser.credentialVersion,
        emailVerifiedAt: null,
        status: 'AWAITING_EMAIL',
      },
    });
    await tx.controlPlaneAuditEvent.create({
      data: {
        scopeKind: 'WORKSPACE',
        teamId: resetCase.teamId,
        actorKind: 'USER',
        actorId: resetCase.targetUserId,
        executor: 'mfa-recovery-evidence',
        eventType: 'mfa_reset.password_proof_verified',
        resourceType: 'MfaResetCase',
        resourceId: resetCase.id,
        details: { recoverySessionId: recoverySession.id },
      },
    });
    return { recoverySession, challenge };
  });

  return {
    recoveryToken: rawSessionToken,
    challengeForDelivery: rawChallenge,
    verifiedEmail: resetCase.targetUser.email,
    expiresAt: result.challenge.expiresAt,
  };
}

export async function verifyMfaRecoveryEmailChallenge(params: {
  caseId: string;
  recoveryToken: string;
  challenge: string;
}) {
  const tokenHash = sha256(params.recoveryToken);
  const session = await prisma.session.findUnique({
    where: { sessionToken: tokenHash },
  });
  if (!session?.recoveryOnly || session.expires <= new Date()) {
    throw supportError();
  }

  const result = await prisma.$transaction(async (tx) => {
    const lockedUser = await lockUserCredentials(tx, session.userId);
    const rows = await tx.$queryRaw<
      Array<{
        id: string;
        targetUserId: string;
        passwordSessionId: string | null;
        passwordRevision: number | null;
        status: string;
      }>
    >`
      SELECT "id", "targetUserId", "passwordSessionId",
             "passwordRevision", "status"
      FROM "MfaResetCase" WHERE "id" = ${params.caseId} FOR UPDATE
    `;
    const resetCase = rows[0];
    if (
      !lockedUser ||
      !resetCase ||
      resetCase.targetUserId !== session.userId ||
      resetCase.passwordSessionId !== session.id ||
      resetCase.passwordRevision !== lockedUser.credentialVersion ||
      resetCase.status !== 'AWAITING_EMAIL'
    ) {
      throw supportError();
    }
    const emailChallenge = await tx.mfaRecoveryEmailChallenge.findUnique({
      where: { caseId: resetCase.id },
    });
    if (
      !emailChallenge ||
      emailChallenge.verifiedAt ||
      emailChallenge.expiresAt <= new Date() ||
      emailChallenge.attempts >= 5
    ) {
      throw supportError();
    }
    const valid = crypto.timingSafeEqual(
      Buffer.from(emailChallenge.tokenHash),
      Buffer.from(sha256(params.challenge.trim().toUpperCase()))
    );
    if (!valid) {
      await tx.mfaRecoveryEmailChallenge.update({
        where: { id: emailChallenge.id },
        data: { attempts: { increment: 1 } },
      });
      return { verified: false as const };
    }
    const now = new Date();
    await tx.mfaRecoveryEmailChallenge.update({
      where: { id: emailChallenge.id },
      data: { verifiedAt: now },
    });
    const updated = await tx.mfaResetCase.update({
      where: { id: resetCase.id },
      data: { emailVerifiedAt: now, status: 'READY_FOR_APPROVAL' },
    });
    await tx.controlPlaneAuditEvent.create({
      data: {
        scopeKind: 'WORKSPACE',
        teamId: updated.teamId,
        actorKind: 'SYSTEM',
        actorId: 'email-challenge',
        executor: 'mfa-recovery-evidence',
        eventType: 'mfa_reset.email_proof_verified',
        resourceType: 'MfaResetCase',
        resourceId: updated.id,
        details: { recoverySessionId: session.id },
      },
    });
    return { verified: true as const, resetCase: updated };
  });
  if (!result.verified) throw supportError();
  return result.resetCase;
}

function evidenceIsCurrent(
  resetCase: {
    status: string;
    expiresAt: Date;
    passwordSessionId: string | null;
    passwordVerifiedAt: Date | null;
    passwordRevision: number | null;
    emailVerifiedAt: Date | null;
  },
  user: { credentialVersion: number; lastPasswordResetAt: Date | null },
  recoverySession: { expires: Date; recoveryOnly: boolean } | null,
  invitationMatches: boolean
) {
  const now = Date.now();
  return Boolean(
    ['READY_FOR_APPROVAL', 'APPROVAL_PENDING'].includes(resetCase.status) &&
    resetCase.expiresAt.getTime() > now &&
    resetCase.passwordSessionId &&
    resetCase.passwordVerifiedAt &&
    resetCase.passwordRevision === user.credentialVersion &&
    resetCase.emailVerifiedAt &&
    recoverySession?.recoveryOnly &&
    recoverySession.expires.getTime() > now &&
    (!user.lastPasswordResetAt ||
      resetCase.passwordVerifiedAt.getTime() -
        user.lastPasswordResetAt.getTime() >=
        PASSWORD_RESET_COOLING_MS) &&
    invitationMatches
  );
}

/** Two distinct operator approvals; the second performs the reset exactly once. */
export async function approveMfaResetCase(
  db: PrismaClient,
  params: { caseId: string; operator: VerifiedOperatorIdentity }
) {
  const claimed = await db.mfaResetCase.findUnique({
    where: { id: params.caseId },
    select: { targetUserId: true },
  });
  if (!claimed) throw new ApiError(404, 'MFA reset case not found.');

  return db.$transaction(async (tx) => {
    // Global lock order: User, Case, Approval. createAuthSession takes the
    // same User lock, so an in-flight password login cannot escape reset.
    const lockedUser = await lockUserCredentials(tx, claimed.targetUserId);
    if (!lockedUser) throw supportError();
    const caseRows = await tx.$queryRaw<
      Array<{
        id: string;
        teamId: string;
        targetUserId: string;
        betaInvitationId: string;
        provisioningAuditEventId: string;
        status: string;
        expiresAt: Date;
        passwordSessionId: string | null;
        passwordVerifiedAt: Date | null;
        passwordRevision: number | null;
        emailVerifiedAt: Date | null;
        completedAt: Date | null;
      }>
    >`
        SELECT "id", "teamId", "targetUserId", "betaInvitationId",
               "provisioningAuditEventId", "status",
               "expiresAt", "passwordSessionId", "passwordVerifiedAt",
               "passwordRevision", "emailVerifiedAt", "completedAt"
        FROM "MfaResetCase" WHERE "id" = ${params.caseId} FOR UPDATE
      `;
    const resetCase = caseRows[0];
    if (!resetCase || resetCase.targetUserId !== claimed.targetUserId) {
      throw supportError();
    }
    if (resetCase.completedAt || resetCase.status === 'COMPLETED') {
      return { caseId: resetCase.id, status: 'COMPLETED', completed: true };
    }
    const [user, recoverySession, betaInvitation, provisioningAudit] =
      await Promise.all([
        tx.user.findUnique({
          where: { id: resetCase.targetUserId },
          select: { credentialVersion: true, lastPasswordResetAt: true },
        }),
        resetCase.passwordSessionId
          ? tx.session.findUnique({
              where: { id: resetCase.passwordSessionId },
              select: { expires: true, recoveryOnly: true },
            })
          : null,
        tx.invitation.findFirst({
          where: {
            id: resetCase.betaInvitationId,
            teamId: resetCase.teamId,
            kind: 'FIRST_OWNER',
            consumedByUserId: resetCase.targetUserId,
            consumedAt: { not: null },
          },
          select: { id: true },
        }),
        tx.controlPlaneAuditEvent.findFirst({
          where: {
            id: resetCase.provisioningAuditEventId,
            teamId: resetCase.teamId,
            eventType: 'workspace.provisioning_started',
            resourceId: resetCase.teamId,
          },
          select: { id: true },
        }),
      ]);
    if (
      !user ||
      !evidenceIsCurrent(
        resetCase,
        user,
        recoverySession,
        Boolean(betaInvitation && provisioningAudit)
      )
    ) {
      throw new ApiError(
        409,
        'MFA reset evidence is missing, stale, or mismatched.'
      );
    }

    const existingApproval = await tx.mfaResetApproval.findUnique({
      where: {
        caseId_operatorId: {
          caseId: resetCase.id,
          operatorId: params.operator.subject,
        },
      },
    });
    if (!existingApproval) {
      await tx.mfaResetApproval.create({
        data: { caseId: resetCase.id, operatorId: params.operator.subject },
      });
    }
    const approvals = await tx.mfaResetApproval.findMany({
      where: { caseId: resetCase.id },
      orderBy: { approvedAt: 'asc' },
      select: { operatorId: true },
    });
    if (approvals.length < 2) {
      if (existingApproval) {
        return {
          caseId: resetCase.id,
          status: 'APPROVAL_PENDING',
          completed: false,
        };
      }
      await tx.mfaResetCase.update({
        where: { id: resetCase.id },
        data: { status: 'APPROVAL_PENDING' },
      });
      await tx.controlPlaneAuditEvent.create({
        data: {
          scopeKind: 'WORKSPACE',
          teamId: resetCase.teamId,
          actorKind: 'OPERATOR',
          actorId: params.operator.subject,
          executor: 'ops:mfa-reset',
          eventType: 'mfa_reset.approved',
          resourceType: 'MfaResetCase',
          resourceId: resetCase.id,
          details: {
            approvalCount: approvals.length,
            operatorIssuer: params.operator.issuer,
            operatorAssertionId: params.operator.assertionId,
            operatorKeyId: params.operator.keyId,
          },
        },
      });
      return {
        caseId: resetCase.id,
        status: 'APPROVAL_PENDING',
        completed: false,
      };
    }

    const completing = await tx.mfaResetCase.updateMany({
      where: {
        id: resetCase.id,
        status: { in: ['READY_FOR_APPROVAL', 'APPROVAL_PENDING'] },
        completedAt: null,
      },
      data: { status: 'COMPLETING' },
    });
    if (completing.count !== 1) {
      throw new ApiError(
        409,
        'MFA reset completion raced; retry the case status.'
      );
    }

    const now = new Date();
    await tx.mfaCredential.deleteMany({
      where: { userId: resetCase.targetUserId },
    });
    await tx.mfaRecoveryCode.deleteMany({
      where: { userId: resetCase.targetUserId },
    });
    await tx.session.deleteMany({ where: { userId: resetCase.targetUserId } });
    await tx.user.update({
      where: { id: resetCase.targetUserId },
      data: { credentialVersion: { increment: 1 } },
    });
    await tx.mfaResetCase.update({
      where: { id: resetCase.id },
      data: { status: 'COMPLETED', completedAt: now },
    });
    await tx.controlPlaneAuditEvent.create({
      data: {
        scopeKind: 'WORKSPACE',
        teamId: resetCase.teamId,
        actorKind: 'OPERATOR',
        actorId: params.operator.subject,
        executor: 'ops:mfa-reset',
        eventType: 'mfa_reset.completed',
        resourceType: 'MfaResetCase',
        resourceId: resetCase.id,
        details: {
          targetUserId: resetCase.targetUserId,
          approvingOperatorIds: approvals.map(
            (approval) => approval.operatorId
          ),
          operatorIssuer: params.operator.issuer,
          operatorAssertionId: params.operator.assertionId,
          operatorKeyId: params.operator.keyId,
        },
      },
    });
    await tx.controlPlaneOutboxEvent.createMany({
      data: [
        {
          teamId: resetCase.teamId,
          eventType: 'mfa_reset.user_notification_requested',
          payload: {
            caseId: resetCase.id,
            targetUserId: resetCase.targetUserId,
          },
        },
        {
          teamId: resetCase.teamId,
          eventType: 'mfa_reset.owner_notification_requested',
          payload: { caseId: resetCase.id },
        },
      ] as Prisma.ControlPlaneOutboxEventCreateManyInput[],
    });
    return { caseId: resetCase.id, status: 'COMPLETED', completed: true };
  });
}
