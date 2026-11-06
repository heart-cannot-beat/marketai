import { SWRConfig } from 'swr';
import {
  isTransientProblem,
  retryTransientProblems,
  transientRetriesExhausted,
} from '@/lib/domain-client/transient-problems';
import { DomainProblemError } from '@/lib/i18n/domain-errors';

const syncing = () =>
  new DomainProblemError({
    code: 'projection_sync_unavailable',
    retryable: true,
  });

const retry = (error: Error, retryCount: number) => {
  const revalidate = jest.fn();
  retryTransientProblems(error, 'key', SWRConfig.defaultValue, revalidate, {
    retryCount,
    dedupe: true,
  });
  return revalidate;
};

describe('transient access problems (#309)', () => {
  beforeEach(() => jest.useFakeTimers());
  afterEach(() => {
    jest.useRealTimers();
    jest.restoreAllMocks();
  });

  it('are the two sync codes, only when the service says to retry', () => {
    for (const code of [
      'projection_sync_unavailable',
      'authorization_sync_pending',
    ]) {
      expect(isTransientProblem({ code, retryable: true })).toBe(true);
      expect(isTransientProblem({ code, retryable: false })).toBe(false);
      expect(isTransientProblem({ code })).toBe(false);
    }
    for (const code of [
      'entitlement_sync_pending', // commands only; never resent
      'authorization_epoch_stale',
      'service_unavailable',
    ])
      expect(isTransientProblem({ code, retryable: true })).toBe(false);
    expect(isTransientProblem(undefined)).toBe(false);
  });

  it('retry every 5 seconds, then stop after ten minutes', () => {
    const early = retry(syncing(), 1);
    jest.advanceTimersByTime(4_999);
    expect(early).not.toHaveBeenCalled();
    jest.advanceTimersByTime(1);
    expect(early).toHaveBeenCalledWith({ retryCount: 1, dedupe: true });

    // The 120th retry is the last: 1 + 120 reads over 10 minutes.
    const last = retry(syncing(), 120);
    jest.advanceTimersByTime(5_000);
    expect(last).toHaveBeenCalledTimes(1);

    const error = syncing();
    expect(transientRetriesExhausted(error)).toBe(false);
    const stopped = retry(error, 121);
    jest.advanceTimersByTime(60 * 60_000);
    expect(stopped).not.toHaveBeenCalled();
    expect(transientRetriesExhausted(error)).toBe(true);
  });

  it("leave every other error to SWR's own backoff", () => {
    const backoff = jest.spyOn(SWRConfig.defaultValue, 'onErrorRetry');
    for (const error of [
      new DomainProblemError({ code: 'service_unavailable', retryable: true }),
      new DomainProblemError({
        code: 'projection_sync_unavailable',
        retryable: false,
      }),
      new Error('projection_sync_unavailable'),
    ]) {
      retry(error, 121);
      expect(transientRetriesExhausted(error)).toBe(false);
    }
    expect(backoff).toHaveBeenCalledTimes(3);
  });
});
