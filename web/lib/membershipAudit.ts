import type { Prisma, Role } from '@prisma/client';

/**
 * Durable membership history (#150).
 *
 * Every change to a `TeamMember` row appends exactly one
 * `ControlPlaneAuditEvent` through the caller's transaction, so the change and
 * its record commit or roll back together. `sendAudit()` and `recordMetric()`
 * stay as they were; neither is durable.
 *
 * The event carries ids and roles only, never an email address, so the active
 * member count of a Workspace can be rebuilt later: start from the current
 * `TeamMember` rows and walk these events backwards.
 */
export const MEMBERSHIP_EVENT_TYPES = [
  'member.joined',
  'member.removed',
  'member.role_changed',
] as const;

export type MembershipEventType = (typeof MEMBERSHIP_EVENT_TYPES)[number];

/** Who made the change. Every membership command today is a signed-in user. */
export type MembershipActor = { kind: 'USER'; id: string };

/** `LEFT` is the member removing themself; `REMOVED` is another member doing it. */
export type MemberRemovalReason = 'REMOVED' | 'LEFT';

export type MembershipChange =
  | { type: 'member.joined'; role: Role; invitationId: string }
  | { type: 'member.removed'; role: Role; reason: MemberRemovalReason }
  | { type: 'member.role_changed'; fromRole: Role; toRole: Role };

const detailsFor = (
  userId: string,
  change: MembershipChange
): Prisma.InputJsonObject => {
  switch (change.type) {
    case 'member.joined':
      return {
        userId,
        role: change.role,
        invitationId: change.invitationId,
      };
    case 'member.removed':
      return { userId, role: change.role, reason: change.reason };
    case 'member.role_changed':
      return { userId, fromRole: change.fromRole, toRole: change.toRole };
  }
};

export async function recordMembershipChangeInTx(
  tx: Prisma.TransactionClient,
  params: {
    teamId: string;
    membershipId: string;
    userId: string;
    actor: MembershipActor;
    change: MembershipChange;
  }
) {
  return tx.controlPlaneAuditEvent.create({
    data: {
      scopeKind: 'WORKSPACE',
      teamId: params.teamId,
      actorKind: params.actor.kind,
      actorId: params.actor.id,
      executor: 'control-plane',
      eventType: params.change.type,
      resourceType: 'TeamMember',
      resourceId: params.membershipId,
      details: detailsFor(params.userId, params.change),
    },
  });
}
