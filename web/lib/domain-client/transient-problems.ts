import { SWRConfig, type SWRConfiguration } from 'swr';
import { DomainProblemError } from '@/lib/i18n/domain-errors';

/**
 * Codes a read is answered with while the domain's copy of the Workspace's
 * access catches up with the control plane (#309): the identity middleware's
 * 503 `projection_sync_unavailable` (no projection row yet, or its repair
 * failed) and 403 `authorization_sync_pending` (the token's epoch is ahead).
 * Both clear without the user doing anything -- for a new Workspace once the
 * dispatcher delivers its first snapshot, at most one 5-minute run later --
 * so a read retries them every 5 seconds for about ten minutes, two dispatcher
 * runs, instead of SWR's backoff. `entitlement_sync_pending` answers only
 * commands, which never resend on their own.
 */
export const TRANSIENT_PROBLEM_CODES: readonly string[] = [
  'projection_sync_unavailable',
  'authorization_sync_pending',
];

export const TRANSIENT_RETRY_INTERVAL_MS = 5_000;
export const TRANSIENT_RETRY_LIMIT_MS = 10 * 60_000;

export function isTransientProblem(
  problem: { code?: string | null; retryable?: boolean } | null | undefined
): boolean {
  return (
    problem?.retryable === true &&
    TRANSIENT_PROBLEM_CODES.includes(problem.code ?? '')
  );
}

const exhausted = new WeakSet<object>();

/**
 * `SWRConfig.onErrorRetry` for reads that throw a `DomainProblemError`: a
 * transient problem retries every 5 seconds until the limit, then stops; any
 * other error keeps SWR's own backoff. SWR calls this before it renders the
 * error, so `transientRetriesExhausted` already answers for that render.
 */
export const retryTransientProblems: NonNullable<
  SWRConfiguration['onErrorRetry']
> = (error, key, config, revalidate, opts) => {
  if (
    !(error instanceof DomainProblemError) ||
    !isTransientProblem(error.problem)
  )
    return SWRConfig.defaultValue.onErrorRetry(
      error,
      key,
      config,
      revalidate,
      opts
    );
  if (
    opts.retryCount >
    TRANSIENT_RETRY_LIMIT_MS / TRANSIENT_RETRY_INTERVAL_MS
  ) {
    exhausted.add(error);
    return;
  }
  setTimeout(revalidate, TRANSIENT_RETRY_INTERVAL_MS, opts);
};

/** Whether a read stopped retrying a transient problem: offer Refresh. */
export function transientRetriesExhausted(error: unknown): boolean {
  return typeof error === 'object' && error !== null && exhausted.has(error);
}
