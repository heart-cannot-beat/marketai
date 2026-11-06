import crypto from 'node:crypto';
import type { PrismaClient } from '@prisma/client';
import { logger } from '../logger';

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

export interface DispatcherOpts {
  domainBaseUrl: string;
  pushToken: string;
  batchSize?: number; // default 10
  leaseSeconds?: number; // default 120
  maxAttempts?: number; // default 5
  /** Injection point for tests — replaces the real HTTP POST. */
  deliverFn?: (
    url: string,
    body: Buffer,
    token: string
  ) => Promise<{ status: number }>;
}

export type DispatchResult = {
  claimed: number;
  delivered: number;
  failed: number;
  deadLettered: number;
};

// ---------------------------------------------------------------------------
// Internal types
// ---------------------------------------------------------------------------

interface OutboxRow {
  id: string;
  snapshotId: string;
  teamId: string;
  deliveryState: string;
  attempts: number;
  maxAttempts: number;
  lastAttemptAt: Date | null;
  nextAttemptAt: Date | null;
  lastError: string | null;
  deliveryLeaseId: string | null;
  deliveryLeaseExpiresAt: Date | null;
  deliveredAt: Date | null;
}

// ---------------------------------------------------------------------------
// Default HTTP delivery
// ---------------------------------------------------------------------------

async function defaultDeliverFn(
  url: string,
  body: Buffer,
  token: string
): Promise<{ status: number }> {
  const res = await fetch(url, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/octet-stream',
    },
    // Under TypeScript 5.7+'s generic-TypedArray lib.dom typings, Buffer is
    // typed as Uint8Array<ArrayBufferLike> (its backing store may be a
    // SharedArrayBuffer), which doesn't structurally satisfy BodyInit's
    // Uint8Array<ArrayBuffer> constraint. `Uint8Array.from` copies the bytes
    // into a fresh Uint8Array backed by a plain ArrayBuffer, which does.
    body: Uint8Array.from(body),
  });
  return { status: res.status };
}

// ---------------------------------------------------------------------------
// Backoff
// ---------------------------------------------------------------------------

function computeBackoff(attempts: number): Date {
  const backoffMs = Math.min(1000 * Math.pow(2, attempts), 300_000);
  return new Date(Date.now() + backoffMs);
}

// ---------------------------------------------------------------------------
// Settlement (CAS update + dead-letter alerting)
// ---------------------------------------------------------------------------

interface SettleContext {
  outboxId: string;
  snapshotId: string;
  teamId: string;
  leaseId: string;
  attempts: number;
}

/**
 * Applies the CAS settlement update (conditioned on `deliveryLeaseId`) and
 * verifies it actually affected a row. If `count === 0`, the lease was
 * reclaimed by another dispatcher (e.g. this dispatcher's lease expired and
 * a different instance already claimed/settled the record) between claim
 * and settlement — the write is a stale no-op and must not be treated as
 * having applied.
 */
async function settle(
  prisma: PrismaClient,
  ctx: SettleContext,
  data: Record<string, unknown>
): Promise<{ applied: boolean }> {
  const result = await prisma.entitlementSnapshotOutbox.updateMany({
    where: { id: ctx.outboxId, deliveryLeaseId: ctx.leaseId },
    data,
  });

  if (result.count === 0) {
    logger.warn('CAS settlement no-op — lease reclaimed before settlement', {
      outboxId: ctx.outboxId,
      snapshotId: ctx.snapshotId,
      teamId: ctx.teamId,
      leaseId: ctx.leaseId,
      attempts: String(ctx.attempts),
      attemptedState: data.deliveryState,
    });
    return { applied: false };
  }

  return { applied: true };
}

/** Structured alert for a DEAD_LETTER transition, per spec §4.2 response table. */
function alertDeadLetter(ctx: SettleContext, reason: string): void {
  logger.error('DEAD_LETTER — outbox delivery permanently failed', {
    outboxId: ctx.outboxId,
    snapshotId: ctx.snapshotId,
    teamId: ctx.teamId,
    attempts: String(ctx.attempts),
    reason,
  });
}

// ---------------------------------------------------------------------------
// Core dispatch cycle
// ---------------------------------------------------------------------------

/**
 * One claim-and-deliver cycle (spec section 4.2).
 *
 * 1. **Claim** — short query with `FOR UPDATE SKIP LOCKED` to atomically
 *    transition retryable outbox records to `DELIVERING`.
 * 2. **Deliver** — outside the transaction, POST each claimed record's
 *    `canonicalEnvelope` to the domain inbox endpoint.
 * 3. **Settle** — CAS update (conditioned on `deliveryLeaseId`) to transition
 *    each record to its final state based on the HTTP result.
 *
 * Multiple dispatcher instances with distinct `dispatcherId` values may run
 * concurrently; `FOR UPDATE SKIP LOCKED` prevents double-claiming, and the
 * `deliveryLeaseId` CAS prevents stale settlement.
 */
export async function claimAndDispatch(
  prisma: PrismaClient,
  dispatcherId: string,
  opts: DispatcherOpts
): Promise<DispatchResult> {
  if (!opts.pushToken) {
    throw new Error(
      'pushToken is required — refusing to deliver without authentication'
    );
  }

  const batchSize = opts.batchSize ?? 10;
  const leaseSeconds = opts.leaseSeconds ?? 120;
  const maxAttempts = opts.maxAttempts ?? 5;
  const deliver = opts.deliverFn ?? defaultDeliverFn;
  // Per-workspace inbox URL is computed per record using the outbox's teamId.
  // See: app/api/internal_routes.py for the actual endpoint registration.

  const result: DispatchResult = {
    claimed: 0,
    delivered: 0,
    failed: 0,
    deadLettered: 0,
  };

  // ------------------------------------------------------------------
  // Step 1: Claim — find retryable records and lease them
  // ------------------------------------------------------------------
  const leaseId = crypto.randomUUID();
  const leaseExpiry = new Date(Date.now() + leaseSeconds * 1000);
  const now = new Date();

  const claimed = await prisma.$queryRaw<OutboxRow[]>`
    UPDATE "EntitlementSnapshotOutbox"
    SET
      "deliveryState"          = 'DELIVERING',
      "deliveryLeaseId"        = ${leaseId},
      "deliveryLeaseExpiresAt" = ${leaseExpiry},
      "lastAttemptAt"          = ${now},
      "attempts"               = "attempts" + 1
    WHERE "id" IN (
      SELECT "id" FROM "EntitlementSnapshotOutbox"
      WHERE (
        "deliveryState" = 'PENDING'
        OR ("deliveryState" = 'FAILED' AND "nextAttemptAt" <= ${now})
        OR ("deliveryState" = 'DELIVERING' AND "deliveryLeaseExpiresAt" < ${now})
      )
      ORDER BY "createdAt" ASC
      LIMIT ${batchSize}
      FOR UPDATE SKIP LOCKED
    )
    RETURNING *
  `;

  result.claimed = claimed.length;
  if (claimed.length === 0) {
    return result;
  }

  // ------------------------------------------------------------------
  // Step 2 & 3: Deliver and settle each record
  // ------------------------------------------------------------------
  for (const record of claimed) {
    // Load the canonical envelope from the snapshot row.
    const snapshot = await prisma.entitlementSnapshot.findUnique({
      where: { id: record.snapshotId },
      select: { canonicalEnvelope: true },
    });

    const settleCtx: SettleContext = {
      outboxId: record.id,
      snapshotId: record.snapshotId,
      teamId: record.teamId,
      leaseId,
      attempts: record.attempts,
    };

    if (!snapshot?.canonicalEnvelope) {
      // Missing snapshot — cannot deliver; dead-letter immediately.
      const reason = `Snapshot ${record.snapshotId} missing or has no canonicalEnvelope`;
      const { applied } = await settle(prisma, settleCtx, {
        deliveryState: 'DEAD_LETTER',
        lastError: reason,
      });
      if (applied) {
        alertDeadLetter(settleCtx, reason);
        result.deadLettered++;
      }
      continue;
    }

    const envelopeBytes = Buffer.isBuffer(snapshot.canonicalEnvelope)
      ? snapshot.canonicalEnvelope
      : Buffer.from(snapshot.canonicalEnvelope as any);

    // Attempt delivery to the per-workspace inbox endpoint.
    const inboxUrl = `${opts.domainBaseUrl}/api/internal/domain/workspaces/${record.teamId}/entitlement-inbox`;
    let status: number;
    let networkError: string | null = null;
    try {
      const res = await deliver(inboxUrl, envelopeBytes, opts.pushToken);
      status = res.status;
    } catch (err: unknown) {
      status = -1;
      networkError =
        err instanceof Error ? err.message : 'Unknown network error';
    }

    // record.attempts already includes the increment from the claim SQL
    // (RETURNING * gives post-UPDATE values).
    const effectiveAttempts = record.attempts;

    // Settle based on status.
    if (status >= 200 && status < 300) {
      // Success.
      const { applied } = await settle(prisma, settleCtx, {
        deliveryState: 'DELIVERED',
        deliveredAt: new Date(),
        lastError: null,
      });
      if (applied) {
        result.delivered++;
      }
    } else if (status === 409) {
      // Conflict — domain already has a newer version; dead-letter.
      const reason = `HTTP ${status}: conflict — domain rejected as superseded`;
      const { applied } = await settle(prisma, settleCtx, {
        deliveryState: 'DEAD_LETTER',
        lastError: reason,
      });
      if (applied) {
        alertDeadLetter(settleCtx, reason);
        result.deadLettered++;
      }
    } else if (status === 400 || status === 422) {
      // Bad request / unprocessable — retrying won't help; dead-letter.
      const reason = `HTTP ${status}: request rejected as malformed`;
      const { applied } = await settle(prisma, settleCtx, {
        deliveryState: 'DEAD_LETTER',
        lastError: reason,
      });
      if (applied) {
        alertDeadLetter(settleCtx, reason);
        result.deadLettered++;
      }
    } else {
      // Retryable failure (408, 429, 5xx, network error).
      const errorMsg = networkError ?? `HTTP ${status}: retryable server error`;

      if (effectiveAttempts >= maxAttempts) {
        // Max attempts exhausted — dead-letter.
        const reason = `${errorMsg} (max attempts ${maxAttempts} exhausted)`;
        const { applied } = await settle(prisma, settleCtx, {
          deliveryState: 'DEAD_LETTER',
          lastError: reason,
        });
        if (applied) {
          alertDeadLetter(settleCtx, reason);
          result.deadLettered++;
        }
      } else {
        // Schedule retry with exponential backoff.
        const nextAttemptAt = computeBackoff(effectiveAttempts - 1);
        const { applied } = await settle(prisma, settleCtx, {
          deliveryState: 'FAILED',
          lastError: errorMsg,
          nextAttemptAt,
        });
        if (applied) {
          result.failed++;
        }
      }
    }
  }

  return result;
}
