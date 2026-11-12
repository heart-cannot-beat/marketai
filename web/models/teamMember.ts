import { prisma } from '@/lib/prisma';
import type { Prisma, Role } from '@prisma/client';
import {
  recordMembershipChangeInTx,
  type MembershipActor,
} from '@/lib/membershipAudit';

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
  return await prisma.$transaction((tx) =>
    updateTeamMemberRoleInTx(tx, params)
  );
};
