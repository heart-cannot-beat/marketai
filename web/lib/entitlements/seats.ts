import type { Prisma } from '@prisma/client';

import { ApiError } from '@/lib/errors';
import { logger } from '@/lib/logger';

/**
 * The Workspace seat limit (contract §8, #157).
 *
 * Owners plus Members may not exceed `members.max`, and a valid pending
 * invitation (not consumed, not revoked, not expired) holds a seat until it is
 * accepted, revoked or expires. A Workspace a downgrade left over the limit
 * keeps every member; it only refuses new invitations, invitation acceptance
 * and promotion until enough seats are freed. Removing a member, leaving,
 * revoking an invitation and demotion are never checked.
 */
export const SEAT_LIMIT_KEY = 'members.max';

/**
 * What the caller is about to do with a seat.
 *
 * - `invite`: a new invitation takes one more seat.
 * - `accept`: the invitation being accepted already holds its seat and turns
 *   it into a membership. It is refused only when the Workspace is over the
 *   limit, which happens only after a downgrade.
 * - `promote`: a role change moves no seat. It is refused only when the
 *   Workspace is over the limit.
 */
export type SeatClaim =
  | { kind: 'invite' }
  | { kind: 'accept'; invitationId: string }
  | { kind: 'promote' };

const unavailable = (teamId: string, reason: string) => {
  logger.warn('control_plane.seat_limit_unavailable', {
    workspace_id: teamId,
    reason,
  });
  return new ApiError(503, 'The Workspace seat limit could not be read.', {
    code: 'seat_limit_unavailable',
  });
};

/**
 * `members.max` from the Workspace's current Entitlement Snapshot, the one its
 * CommercialAccessProjection points at. A missing projection, snapshot or key,
 * or a value that is not a whole number of seats, fails closed: since #156
 * every published snapshot carries the key.
 */
const readSeatLimitInTx = async (
  tx: Prisma.TransactionClient,
  teamId: string
): Promise<number> => {
  const projection = await tx.commercialAccessProjection.findUnique({
    where: { teamId },
    select: { currentSnapshot: { select: { resolvedEntitlements: true } } },
  });
  if (!projection?.currentSnapshot) {
    throw unavailable(teamId, 'no_current_snapshot');
  }
  const entitlements = projection.currentSnapshot.resolvedEntitlements;
  const limit =
    entitlements && typeof entitlements === 'object'
      ? (entitlements as Record<string, unknown>)[SEAT_LIMIT_KEY]
      : undefined;
  if (limit === undefined) {
    throw unavailable(teamId, 'key_missing');
  }
  if (typeof limit !== 'number' || !Number.isSafeInteger(limit) || limit < 0) {
    throw unavailable(teamId, 'value_invalid');
  }
  return limit;
};

/**
 * Refuse `claim` when it would leave the Workspace over its seat limit.
 *
 * Run it inside the SERIALIZABLE transaction that makes the change: the limit
 * and the count are read there, so two concurrent claims cannot both see the
 * last free seat, and a concurrent publication that lowers the limit cannot
 * slip between the check and the change. The caller retries a serialization
 * failure from the transaction's outer edge.
 */
export const assertSeatAvailableInTx = async (
  tx: Prisma.TransactionClient,
  teamId: string,
  claim: SeatClaim,
  now: Date = new Date()
): Promise<void> => {
  const limit = await readSeatLimitInTx(tx, teamId);

  const members = await tx.teamMember.count({ where: { teamId } });
  // The invitation being accepted is left out here and added back below, so
  // it counts once even if it expires between its own check and this count.
  const pendingInvitations = await tx.invitation.count({
    where: {
      teamId,
      consumedAt: null,
      revokedAt: null,
      expires: { gte: now },
      ...(claim.kind === 'accept' ? { id: { not: claim.invitationId } } : {}),
    },
  });
  const occupied = members + pendingInvitations;
  const required = claim.kind === 'promote' ? occupied : occupied + 1;
  if (required <= limit) return;

  logger.warn('control_plane.seat_limit_refused', {
    workspace_id: teamId,
    claim: claim.kind,
    limit,
    occupied: claim.kind === 'accept' ? occupied + 1 : occupied,
  });
  if (claim.kind === 'invite') {
    throw new ApiError(409, 'All seats in this Workspace are taken.', {
      code: 'seat_limit_reached',
      params: { limit },
    });
  }
  throw new ApiError(409, 'This Workspace is over its seat limit.', {
    code: 'seat_limit_exceeded',
    params: { limit },
  });
};
