import { prisma } from '@/lib/prisma';
import { Prisma, Role } from '@prisma/client';
import { assertSeatAvailableInTx } from '@/lib/entitlements/seats';
import {
  recordMembershipChangeInTx,
  type MembershipActor,
} from '@/lib/membershipAudit';
import { retrySerializableTransaction } from '@/lib/serializable-retry';

export const countTeamMembers = async ({ where }) => {
  return await prisma.teamMember.count({
    where,
  });
};

/**
 * Change one member's role and append its `member.role_changed` audit event in
 * the caller's transaction. The row is locked first so two concurrent changes
 * each record the role they actually replaced. Setting the role a member
 * already holds changes nothing and records nothing.
 *
 * Promotion to Owner is refused while the Workspace is over its seat limit
 * (contract §8); demotion never is. Run it in a SERIALIZABLE transaction so the
 * seat check and the change see the same limit.
 */
export const updateTeamMemberRoleInTx = async (
  tx: Prisma.TransactionClient,
  params: {
    teamId: string;
    userId: string;
    role: Role;
    actor: MembershipActor;
  }
) => {
  const { teamId, userId, role, actor } = params;

  await tx.$queryRaw<Array<{ id: string }>>`
    SELECT "id" FROM "TeamMember"
    WHERE "teamId" = ${teamId} AND "userId" = ${userId}
    FOR UPDATE
  `;
  const current = await tx.teamMember.findUniqueOrThrow({
    where: { teamId_userId: { teamId, userId } },
  });
  if (current.role === role) {
    return current;
  }
  if (role === Role.OWNER) {
    await assertSeatAvailableInTx(tx, teamId, { kind: 'promote' });
  }

  const updated = await tx.teamMember.update({
    where: { id: current.id },
    data: { role },
  });

  await recordMembershipChangeInTx(tx, {
    teamId,
    membershipId: updated.id,
    userId,
    actor,
    change: {
      type: 'member.role_changed',
      fromRole: current.role,
      toRole: updated.role,
    },
  });

  return updated;
};

export const updateTeamMember = async (params: {
  teamId: string;
  userId: string;
  role: Role;
  actor: MembershipActor;
}) => {
  return await retrySerializableTransaction(() =>
    prisma.$transaction((tx) => updateTeamMemberRoleInTx(tx, params), {
      isolationLevel: Prisma.TransactionIsolationLevel.Serializable,
    })
  );
};
