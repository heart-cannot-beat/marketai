import { ApiError } from '@/lib/errors';

const SERIALIZABLE_TRANSACTION_ATTEMPTS = 5;
const SERIALIZABLE_RETRY_BASE_DELAY_MS = 5;

const carriesSerializationFailure = (error: unknown): boolean => {
  if (!error || typeof error !== 'object') return false;
  const candidate = error as {
    code?: unknown;
    message?: unknown;
    meta?: Record<string, unknown>;
    cause?: unknown;
  };
  if (candidate.code === 'P2034' || candidate.code === '40001') return true;

  const metaValues = candidate.meta
    ? [
        candidate.meta.code,
        candidate.meta.sqlstate,
        candidate.meta.sqlState,
        candidate.meta.database_error,
        candidate.meta.message,
      ]
    : [];
  if (
    [candidate.message, ...metaValues].some(
      (value) => typeof value === 'string' && /\b40001\b/.test(value)
    )
  ) {
    return true;
  }
  return candidate.cause ? carriesSerializationFailure(candidate.cause) : false;
};

/**
 * Retry only an aborted SERIALIZABLE transaction, always from its outer edge:
 * `operation` must open the whole transaction, so a retry re-reads everything
 * the aborted attempt read. Any other failure, and the last attempt's
 * serialization failure, is thrown as it was.
 */
export const retrySerializableTransaction = async <T>(
  operation: () => Promise<T>
): Promise<T> => {
  for (
    let attempt = 0;
    attempt < SERIALIZABLE_TRANSACTION_ATTEMPTS;
    attempt++
  ) {
    try {
      return await operation();
    } catch (error) {
      if (
        !carriesSerializationFailure(error) ||
        attempt === SERIALIZABLE_TRANSACTION_ATTEMPTS - 1
      ) {
        throw error;
      }
      await new Promise((resolve) =>
        setTimeout(resolve, SERIALIZABLE_RETRY_BASE_DELAY_MS * 2 ** attempt)
      );
    }
  }
  throw new ApiError(503, 'The transaction did not converge.');
};
