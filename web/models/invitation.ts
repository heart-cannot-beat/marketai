import env from '@/lib/env';
import { ApiError } from '@/lib/errors';
import { logger } from '@/lib/logger';
import { recordMembershipChangeInTx } from '@/lib/membershipAudit';
import { reconcileProvisioningWorkspaceFromRuntime } from '@/lib/onboarding';
import { prisma } from '@/lib/prisma';
import { Invitation, Prisma, Role, TeamMember } from '@prisma/client';
import { randomUUID } from 'crypto';

export type TeamInvitation = Pick<
  Invitation,
  'id' | 'email' | 'role' | 'expires' | 'token'
> & { url: string };

export const getInvitations = async (teamId: string) => {
  const invitations = await prisma.invitation.findMany({
    where: {
      teamId,
      consumedAt: null,
      revokedAt: null,
    },
    select: {
      id: true,
      email: true,
      role: true,
      expires: true,
      token: true,
    },
  });

  return invitations.map((invitation) => ({
    ...invitation,
    url: `${env.appUrl}/invitations/${invitation.token}`,
  })) as TeamInvitation[];
};

export const getInvitation = async (
  key: { token: string } | { id: string }
) => {
  const invitation = await prisma.invitation.findUnique({
    where: key,
    include: {
      team: {
        select: {
          id: true,
          name: true,
          slug: true,
        },
      },
    },
  });

  if (!invitation) {
    throw new ApiError(404, 'Invitation not found.');
  }

  if (invitation.consumedAt) {
    throw new ApiError(410, 'This invitation has already been used.');
  }
  if (invitation.revokedAt) {
    throw new ApiError(410, 'This invitation has been revoked.');
  }

  return invitation;
};

export const createInvitation = async (params: {
  teamId: string;
  invitedBy: string;
  email: string;
  role: Role;
}) => {
  const email = normalizeEmail(params.email);
  const token = randomUUID();
  const expires = new Date(Date.now() + 7 * 24 * 60 * 60 * 1000);
  try {
    return await prisma.$transaction(
      async (tx) => {
        const active = await tx.invitation.findFirst({
          where: {
            teamId: params.teamId,
            email,
            consumedAt: null,
            revokedAt: null,
          },
        });
        if (active) {
          throw new ApiError(409, 'An active invitation already exists.');
        }
        return tx.invitation.create({
          data: { ...params, email, token, expires },
        });
      },
      { isolationLevel: Prisma.TransactionIsolationLevel.Serializable }
    );
  } catch (error) {
    if (
      error instanceof Prisma.PrismaClientKnownRequestError &&
      ['P2002', 'P2034'].includes(error.code)
    ) {
      throw new ApiError(409, 'An active invitation already exists.');
    }
    throw error;
  }
};

export const deleteInvitation = async (
  key: { token: string } | { id: string }
) => {
  return await prisma.invitation.update({
    where: key,
    data: { revokedAt: new Date(), revokedReason: 'owner_revoked' },
  });
};

export const isInvitationExpired = async (expires: Date) => {
  return expires.getTime() < Date.now();
};

export const getInvitationCount = async ({ where }) => {
  return await prisma.invitation.count({
    where,
  });
};

/** Invitations are matched on a normalized address, never on a raw string. */
export const normalizeEmail = (email: string) => email.trim().toLowerCase();

const SERIALIZABLE_TRANSACTION_ATTEMPTS = 5;
const SERIALIZABLE_RETRY_BASE_DELAY_MS = 5;

const carriesSerializationFailure = (error: unknown): boolean => {
  if (!error || typeof error !== 'object') return false;
  const candidate = error as {
    code?: unknown;
    message?: unknown;
    meta?: Record<string, unknown>;
    cause?: unknown;
  };
  if (candidate.code === 'P2034' || candidate.code === '40001') return true;

  const metaValues = candidate.meta
    ? [
        candidate.meta.code,
        candidate.meta.sqlstate,
        candidate.meta.sqlState,
        candidate.meta.database_error,
        candidate.meta.message,
      ]
    : [];
  if (
    [candidate.message, ...metaValues].some(
      (value) => typeof value === 'string' && /\b40001\b/.test(value)
    )
  ) {
    return true;
  }
  return candidate.cause ? carriesSerializationFailure(candidate.cause) : false;
};

/** Retry only an aborted SERIALIZABLE transaction, always from its outer edge. */
export const retryInvitationSerializableTransaction = async <T>(
  operation: () => Promise<T>
): Promise<T> => {
  for (
    let attempt = 0;
    attempt < SERIALIZABLE_TRANSACTION_ATTEMPTS;
    attempt++
  ) {
    try {
      return await operation();
    } catch (error) {
      if (
        !carriesSerializationFailure(error) ||
        attempt === SERIALIZABLE_TRANSACTION_ATTEMPTS - 1
      ) {
        throw error;
      }
      await new Promise((resolve) =>
        setTimeout(resolve, SERIALIZABLE_RETRY_BASE_DELAY_MS * 2 ** attempt)
      );
    }
  }
  throw new ApiError(503, 'Invitation acceptance did not converge.');
};

const reconcileAcceptedOwner = async (
  membership: TeamMember & { teamSlug: string }
) => {
  if (membership.role !== 'OWNER') return;
  try {
    await reconcileProvisioningWorkspaceFromRuntime(prisma, membership.teamId);
  } catch (error) {
    logger.warn('Provisioning reconcile deferred after invitation acceptance', {
      teamId: membership.teamId,
      reason: error instanceof Error ? error.name : 'unknown',
    });
  }
};

/**
 * Consume an invitation exactly once.
 *
 * Consumption is retained durably so a retry by the same user can return the
 * membership idempotently. A conditional consumedAt=null update is the
 * compare-and-set; a different account, expired link, or email mismatch fails
 * without burning the invitation.
 *
 * Upstream created the membership and deleted the invitation as two independent
 * statements, and never deleted link invitations at all, so one token could
 * admit any number of accounts.
 */
export const consumeInvitation = async (params: {
  token: string;
  userId: string;
  email: string;
}): Promise<TeamMember & { teamSlug: string }> => {
  const email = normalizeEmail(params.email);

  const membership = await retryInvitationSerializableTransaction(() =>
    prisma.$transaction(
      async (tx) => {
        return consumeInvitationInTx(tx, params.token, params.userId, email);
      },
      { isolationLevel: Prisma.TransactionIsolationLevel.Serializable }
    )
  );
  await reconcileAcceptedOwner(membership);
  return membership;
};

async function consumeInvitationInTx(
  tx: Prisma.TransactionClient,
  token: string,
  userId: string,
  email: string
): Promise<TeamMember & { teamSlug: string }> {
  await tx.$queryRaw<Array<{ id: string }>>`
    SELECT "id" FROM "Invitation" WHERE "token" = ${token} FOR UPDATE
  `;
  const invitation = await tx.invitation.findUnique({
    where: { token },
    include: { team: { select: { id: true, slug: true, lifecycle: true } } },
  });
  if (!invitation) {
    throw new ApiError(
      400,
      'This invitation has already been used or was revoked.'
    );
  }

  if (normalizeEmail(invitation.email) !== email) {
    throw new ApiError(
      403,
      'You must be signed in with the email address this invitation was sent to.'
    );
  }

  if (invitation.consumedAt) {
    if (invitation.consumedByUserId !== userId) {
      throw new ApiError(
        400,
        'This invitation has already been used or was revoked.'
      );
    }
    const replayMembership = await tx.teamMember.findUnique({
      where: { teamId_userId: { teamId: invitation.teamId, userId } },
    });
    if (!replayMembership || replayMembership.role !== invitation.role) {
      throw new ApiError(
        409,
        'Invitation acceptance is inconsistent. Contact support.'
      );
    }
    return { ...replayMembership, teamSlug: invitation.team.slug };
  }

  if (invitation.revokedAt) {
    throw new ApiError(410, 'This invitation has been revoked.');
  }

  if (
    invitation.team.lifecycle === 'DELETION_PENDING' ||
    invitation.team.lifecycle === 'DELETED'
  ) {
    throw new ApiError(410, 'This Workspace no longer accepts invitations.');
  }
  if (invitation.team.lifecycle === 'SUSPENDED') {
    throw new ApiError(423, 'This Workspace is suspended.');
  }
  const lifecycleAllowsAcceptance =
    (invitation.team.lifecycle === 'PROVISIONING' &&
      invitation.kind === 'FIRST_OWNER') ||
    (invitation.team.lifecycle === 'ACTIVE' && invitation.kind === 'MEMBER');
  if (!lifecycleAllowsAcceptance) {
    throw new ApiError(
      409,
      'Invitation does not match the Workspace lifecycle.'
    );
  }

  if (invitation.expires.getTime() < Date.now()) {
    throw new ApiError(400, 'Invitation expired. Please request a new one.');
  }

  const existing = await tx.teamMember.findUnique({
    where: {
      teamId_userId: { teamId: invitation.teamId, userId },
    },
  });

  if (existing) {
    throw new ApiError(400, 'You are already a member of this Workspace.');
  }

  const member = await tx.teamMember.create({
    data: {
      teamId: invitation.teamId,
      userId,
      role: invitation.role,
    },
  });

  const consumed = await tx.invitation.updateMany({
    where: { id: invitation.id, consumedAt: null },
    data: { consumedAt: new Date(), consumedByUserId: userId },
  });
  if (consumed.count !== 1) {
    throw new ApiError(
      409,
      'Invitation was accepted concurrently. Retry safely.'
    );
  }

  // Every join, Member or first Owner, records one `member.joined`. The
  // first-Owner path below additionally records its onboarding event.
  await recordMembershipChangeInTx(tx, {
    teamId: invitation.teamId,
    membershipId: member.id,
    userId,
    actor: { kind: 'USER', id: userId },
    change: {
      type: 'member.joined',
      role: member.role,
      invitationId: invitation.id,
    },
  });

  if (invitation.kind === 'FIRST_OWNER') {
    const now = new Date();
    await tx.onboardingProgress.upsert({
      where: { teamId: invitation.teamId },
      create: { teamId: invitation.teamId, ownerAcceptedAt: now, version: 1 },
      update: { ownerAcceptedAt: now, version: { increment: 1 } },
    });
    await tx.controlPlaneAuditEvent.create({
      data: {
        scopeKind: 'WORKSPACE',
        teamId: invitation.teamId,
        actorKind: 'USER',
        actorId: userId,
        executor: 'control-plane',
        eventType: 'first_owner.accepted',
        resourceType: 'Invitation',
        resourceId: invitation.id,
        details: { email, membershipId: member.id },
      },
    });
    await tx.controlPlaneOutboxEvent.create({
      data: {
        teamId: invitation.teamId,
        eventType: 'workspace.first_owner_accepted',
        payload: {
          invitationId: invitation.id,
          userId,
          membershipId: member.id,
        },
      },
    });
  }

  return { ...member, teamSlug: invitation.team.slug };
}

/** Account, first membership, invite consumption, audit, and onboarding
 * projection are one serializable transaction. */
export const acceptInvitationForNewUser = async (params: {
  token: string;
  name: string;
  password: string;
  emailVerified: Date | null;
}) => {
  const accepted = await retryInvitationSerializableTransaction(() =>
    prisma.$transaction(
      async (tx) => {
        const invitation = await tx.invitation.findUnique({
          where: { token: params.token },
        });
        if (!invitation || invitation.consumedAt || invitation.revokedAt) {
          throw new ApiError(
            400,
            'This invitation has already been used or was revoked.'
          );
        }
        const email = normalizeEmail(invitation.email);
        if (await tx.user.findUnique({ where: { email } })) {
          throw new ApiError(
            409,
            'An account with this email already exists. Sign in to accept.'
          );
        }
        const user = await tx.user.create({
          data: {
            name: params.name,
            email,
            password: params.password,
            emailVerified: params.emailVerified,
          },
        });
        const membership = await consumeInvitationInTx(
          tx,
          params.token,
          user.id,
          email
        );
        return { user, membership };
      },
      { isolationLevel: Prisma.TransactionIsolationLevel.Serializable }
    )
  );
  await reconcileAcceptedOwner(accepted.membership);
  return accepted;
};
