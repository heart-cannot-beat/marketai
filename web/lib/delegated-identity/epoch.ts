import type { PrismaClient } from '@prisma/client';

/**
 * Bump on every membership or role change (removal, role edit). A
 * delegated BFF identity claim carries the authorizationEpoch it was
 * minted against; a mismatch forces re-authorization instead of trusting
 * a claim issued before the change.
 *
 * A single `update` call is used rather than an explicit transaction:
 * Postgres applies all column changes in one UPDATE statement atomically.
 */
export async function incrementAuthorizationEpoch(
  prisma: PrismaClient,
  teamId: string
): Promise<void> {
  await prisma.team.update({
    where: { id: teamId },
    data: {
      authorizationEpoch: { increment: 1 },
      projectionVersion: { increment: 1 },
    },
  });
}

/**
 * Bump on every subscription/entitlement change, so downstream consumers
 * can detect stale entitlement claims independently of authorization
 * changes.
 */
export async function incrementEntitlementVersion(
  prisma: PrismaClient,
  teamId: string
): Promise<void> {
  await prisma.team.update({
    where: { id: teamId },
    data: {
      entitlementVersion: { increment: 1 },
      projectionVersion: { increment: 1 },
    },
  });
}

/**
 * Set (or clear) Workspace suspension. Both directions bump
 * authorizationEpoch so any in-flight claim is invalidated immediately.
 */
export async function toggleSuspension(
  prisma: PrismaClient,
  teamId: string,
  suspended: boolean
): Promise<void> {
  await prisma.team.update({
    where: { id: teamId },
    data: {
      suspended,
      authorizationEpoch: { increment: 1 },
      projectionVersion: { increment: 1 },
    },
  });
}
