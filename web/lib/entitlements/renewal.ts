/**
 * Periodic lease renewal (spec §1.12 "Periodic lease renewal (every 6 hours,
 * even if nothing changed)", §4.1 publication triggers).
 *
 * Every snapshot carries `validUntil <= issuedAt + 24h`, and the domain
 * refuses work that needs an entitlement once the projection's lease has
 * lapsed (`app.entitlements.policy.EntitlementPolicy` → `LeaseExpiredError`,
 * or `NoEntitlementProjectionError` when nothing was ever applied). Without a
 * renewal scan a Workspace is only covered for the day after its last
 * membership/plan change, so BYOK spend is refused from then on.
 *
 * `renewExpiringLeases` republishes, through the normal Publication Module
 * (`publishSnapshot`: same semantic content → same `entitlementVersion`,
 * `leaseSequence + 1`, a fresh `validUntil`, a new Outbox row), every
 * ACTIVE or SUSPENDED Workspace whose newest snapshot was issued more than
 * `intervalSeconds` ago. The dispatcher cron calls it on every run before
 * delivering, so a renewed snapshot is pushed in the same cycle. Workspaces
 * are renewed one transaction each; a failure is counted and retried on the
 * next run without blocking the others.
 */
import type { PrismaClient } from '@prisma/client';
import { publishSnapshot } from './publication';

/** Spec default: renew every 6 hours, well inside the 24h lease. */
export const DEFAULT_RENEWAL_INTERVAL_SECONDS = 6 * 60 * 60;

/** A lease is at most 24h; renewing less often than that lets it lapse. */
const MAX_RENEWAL_INTERVAL_SECONDS = 20 * 60 * 60;

/** Lifecycles whose domain projection must stay current. */
export const RENEWABLE_LIFECYCLES = ['ACTIVE', 'SUSPENDED'] as const;

export interface RenewalOpts {
  signingKeyId: string;
  signingKey: Buffer;
  intervalSeconds?: number;
  batchSize?: number;
  now?: Date;
  /** Injected for tests; defaults to the Publication Module. */
  publish?: typeof publishSnapshot;
}

export interface RenewalResult {
  examined: number;
  renewed: number;
  failed: number;
  failures: Array<{ teamId: string; error: string }>;
}

/** Parses `ENTITLEMENT_RENEWAL_INTERVAL_SECONDS`, rejecting values that would let a lease lapse. */
export function renewalIntervalSeconds(raw: string | undefined): number {
  if (raw === undefined || raw.trim() === '') {
    return DEFAULT_RENEWAL_INTERVAL_SECONDS;
  }
  const parsed = Number(raw);
  if (
    !Number.isSafeInteger(parsed) ||
    parsed <= 0 ||
    parsed > MAX_RENEWAL_INTERVAL_SECONDS
  ) {
    throw new Error(
      `ENTITLEMENT_RENEWAL_INTERVAL_SECONDS must be an integer in (0, ${MAX_RENEWAL_INTERVAL_SECONDS}].`
    );
  }
  return parsed;
}

export async function renewExpiringLeases(
  prisma: PrismaClient,
  opts: RenewalOpts
): Promise<RenewalResult> {
  const now = opts.now ?? new Date();
  const intervalSeconds =
    opts.intervalSeconds ?? DEFAULT_RENEWAL_INTERVAL_SECONDS;
  const batchSize = opts.batchSize ?? 50;
  const publish = opts.publish ?? publishSnapshot;
  const issuedBefore = new Date(now.getTime() - intervalSeconds * 1000);

  // A Workspace is due when it has a commercial projection (it was
  // provisioned) and no snapshot issued within the interval. Oldest first,
  // so a backlog drains in lease order.
  const due = await prisma.$queryRaw<Array<{ teamId: string }>>`
    SELECT t."id" AS "teamId"
    FROM "Team" t
    JOIN "CommercialAccessProjection" p ON p."teamId" = t."id"
    LEFT JOIN LATERAL (
      SELECT MAX(s."issuedAt") AS "lastIssuedAt"
      FROM "EntitlementSnapshot" s
      WHERE s."teamId" = t."id"
    ) latest ON TRUE
    WHERE t."lifecycle" IN ('ACTIVE', 'SUSPENDED')
      AND (latest."lastIssuedAt" IS NULL OR latest."lastIssuedAt" <= ${issuedBefore})
    ORDER BY latest."lastIssuedAt" ASC NULLS FIRST, t."id" ASC
    LIMIT ${batchSize}
  `;

  const result: RenewalResult = {
    examined: due.length,
    renewed: 0,
    failed: 0,
    failures: [],
  };
  for (const { teamId } of due) {
    try {
      await publish(prisma, teamId, opts.signingKeyId, opts.signingKey);
      result.renewed++;
    } catch (error) {
      result.failed++;
      result.failures.push({
        teamId,
        error: error instanceof Error ? error.message : String(error),
      });
    }
  }
  return result;
}
