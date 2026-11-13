import os from 'node:os';
import type { PrismaClient } from '@prisma/client';

import env from '../env';
import { logger } from '../logger';
import {
  claimAndDispatch,
  type DispatcherOpts,
  type DispatchResult,
} from './dispatcher';

/**
 * Upper bound on how long a caller waits for the immediate delivery: claim,
 * POST and settlement together.
 */
export const IMMEDIATE_DISPATCH_TIMEOUT_MS = 3_000;

/**
 * The POST gets the budget minus this margin, so a slow domain settles the
 * record as a retryable failure before the caller stops waiting.
 */
const SETTLE_MARGIN_MS = 500;

const EVENT = 'Immediate entitlement dispatch';

export type ImmediateDispatchOutcome =
  | { outcome: 'dispatched'; result: DispatchResult }
  | { outcome: 'skipped_unconfigured' }
  | { outcome: 'timed_out' }
  | { outcome: 'error' };

export type ImmediateDispatchOverrides = {
  timeoutMs?: number;
  domainBaseUrl?: string | null;
  pushToken?: string;
  deliverFn?: DispatcherOpts['deliverFn'];
};

const dispatcherId = () => `immediate:${os.hostname()}:${process.pid}`;

/**
 * Best-effort delivery of one Workspace's outbox records right after the
 * transaction that published them commits, so the domain does not wait for
 * the next scheduled dispatcher run (#9).
 *
 * It goes through `claimAndDispatch`, scoped to the Workspace: the same
 * `FOR UPDATE SKIP LOCKED` claim, lease, CAS settlement and backoff as the
 * scheduled run, so the two can race without delivering a record twice. It
 * never throws and never holds the caller longer than `timeoutMs`. A record
 * it could not deliver stays PENDING or FAILED, or DELIVERING until its lease
 * expires, and the scheduled run delivers it.
 */
export async function dispatchWorkspaceOutboxNow(
  db: PrismaClient,
  teamId: string,
  overrides: ImmediateDispatchOverrides = {}
): Promise<ImmediateDispatchOutcome> {
  const startedAt = Date.now();
  const timeoutMs = overrides.timeoutMs ?? IMMEDIATE_DISPATCH_TIMEOUT_MS;
  const elapsedMs = () => Date.now() - startedAt;
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const domainBaseUrl =
      overrides.domainBaseUrl === undefined
        ? env.domainApi?.baseUrl
        : overrides.domainBaseUrl;
    const pushToken =
      overrides.pushToken ?? env.controlPlaneToDomainEntitlementToken;
    if (!domainBaseUrl || !pushToken) {
      logger.warn(EVENT, { teamId, outcome: 'skipped_unconfigured' });
      return { outcome: 'skipped_unconfigured' };
    }

    const dispatch = claimAndDispatch(db, dispatcherId(), {
      domainBaseUrl,
      pushToken,
      teamId,
      deliveryTimeoutMs: Math.max(1, timeoutMs - SETTLE_MARGIN_MS),
      deliverFn: overrides.deliverFn,
    });
    // After a timeout the cycle keeps settling in the background; its
    // rejection then has nobody awaiting it.
    dispatch.catch(() => undefined);
    const deadline = new Promise<'timed_out'>((resolve) => {
      timer = setTimeout(() => resolve('timed_out'), timeoutMs);
    });

    const settled = await Promise.race([dispatch, deadline]);
    if (settled === 'timed_out') {
      logger.warn(EVENT, {
        teamId,
        outcome: 'timed_out',
        elapsedMs: elapsedMs(),
      });
      return { outcome: 'timed_out' };
    }

    const fields = {
      teamId,
      outcome: 'dispatched',
      claimed: settled.claimed,
      delivered: settled.delivered,
      failed: settled.failed,
      deadLettered: settled.deadLettered,
      elapsedMs: elapsedMs(),
    };
    if (settled.delivered === settled.claimed) {
      logger.info(EVENT, fields);
    } else {
      logger.warn(EVENT, fields);
    }
    return { outcome: 'dispatched', result: settled };
  } catch (error) {
    // The class name only: a driver message can carry SQL or a URL.
    logger.warn(EVENT, {
      teamId,
      outcome: 'error',
      reason: error instanceof Error ? error.name : 'unknown',
      elapsedMs: elapsedMs(),
    });
    return { outcome: 'error' };
  } finally {
    clearTimeout(timer);
  }
}
