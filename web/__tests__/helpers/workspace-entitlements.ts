import { createHash, randomUUID } from 'node:crypto';
import { Prisma, type PrismaClient } from '@prisma/client';

import { publishSnapshotInTx } from '../../lib/entitlements/publication';

/**
 * Commercial access for a fixture Workspace in the PostgreSQL suites.
 *
 * Inviting, accepting and promoting read `members.max` from the Workspace's
 * current Entitlement Snapshot and fail closed without one (#157). These
 * helpers publish that snapshot through the production publication path: a
 * BASE AccessGrant on a Beta PlanVersion, its binding, and
 * `publishSnapshotInTx`.
 */
export const TEST_SIGNING_KEY_ID = 'test-key';
export const TEST_SIGNING_KEY = Buffer.from(
  'test-signing-key-at-least-32-bytes'
);

const betaPlan = (prisma: PrismaClient, seats: number | undefined) => {
  // With no `members.max` the platform default (5, #156) applies.
  const entitlements =
    seats === undefined
      ? { controlled_mock: true }
      : { controlled_mock: true, 'members.max': seats };
  const suffix = randomUUID().slice(0, 8);
  return prisma.planVersion.create({
    data: {
      planKey: `beta-seats-${suffix}`,
      versionNo: 1,
      displayName: `Beta seats ${suffix}`,
      state: 'EFFECTIVE',
      entitlements,
      entitlementsHash: createHash('sha256')
        .update(JSON.stringify(entitlements))
        .digest('hex'),
      publishedBy: 'operator-plan',
    },
  });
};

/**
 * Bind the Workspace to a new Beta PlanVersion and publish its snapshot. A
 * Workspace that already has a binding is moved off it, which is how a
 * downgrade reaches it: the next snapshot carries the new plan's limit.
 */
export async function bindBetaPlan(
  prisma: PrismaClient,
  teamId: string,
  options: { grantedBy: string; seats?: number }
) {
  const plan = await betaPlan(prisma, options.seats);
  await prisma.$transaction(
    async (tx) => {
      const now = new Date();
      const current = await tx.commercialAccessBinding.findFirst({
        where: { teamId, deactivatedAt: null },
      });
      if (current) {
        await tx.commercialAccessBinding.update({
          where: { id: current.id },
          data: { deactivatedAt: now },
        });
        if (current.accessGrantId) {
          await tx.accessGrant.update({
            where: { id: current.accessGrantId },
            data: {
              revokedAt: now,
              revokedBy: options.grantedBy,
              revokeReason: 'plan change',
            },
          });
        }
      }
      const grant = await tx.accessGrant.create({
        data: {
          teamId,
          planVersionId: plan.id,
          grantKind: 'BASE',
          source: 'beta_provisioning',
          reason: 'Seat-limit test fixture',
          periodAnchor: now,
          startsAt: now,
          grantedBy: options.grantedBy,
        },
      });
      await tx.commercialAccessBinding.create({
        data: {
          teamId,
          sourceKind: 'ACCESS_GRANT',
          accessGrantId: grant.id,
          planVersionId: plan.id,
          effectiveAt: now,
        },
      });
      if (!current) {
        await tx.commercialAccessProjection.create({ data: { teamId } });
      }
      await publishSnapshotInTx(
        tx,
        teamId,
        TEST_SIGNING_KEY_ID,
        TEST_SIGNING_KEY
      );
    },
    { isolationLevel: Prisma.TransactionIsolationLevel.Serializable }
  );
}

/** `members.max` on the snapshot the Workspace's projection points at. */
export async function currentSeatLimit(prisma: PrismaClient, teamId: string) {
  const projection = await prisma.commercialAccessProjection.findUnique({
    where: { teamId },
    select: { currentSnapshot: { select: { resolvedEntitlements: true } } },
  });
  return (
    projection?.currentSnapshot?.resolvedEntitlements as Record<
      string,
      unknown
    > | null
  )?.['members.max'];
}
