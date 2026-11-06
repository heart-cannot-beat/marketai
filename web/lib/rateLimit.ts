import type { NextApiRequest, NextApiResponse } from 'next';
import env from './env';
import { logger } from './logger';

/**
 * Fixed-window rate limiting for authentication and mutation surfaces.
 *
 * The store is intentionally pluggable: the in-process implementation is
 * correct for a single node and for tests, and a shared backend is swapped in
 * when the control plane runs more than one replica. The hook itself is the
 * contract that route handlers depend on.
 */
export type RateLimitDecision = {
  allowed: boolean;
  limit: number;
  remaining: number;
  resetAt: number;
};

export interface RateLimitStore {
  hit(key: string, windowMs: number, limit: number): RateLimitDecision;
  reset(): void;
}

class InMemoryRateLimitStore implements RateLimitStore {
  private counters = new Map<string, { count: number; resetAt: number }>();

  hit(key: string, windowMs: number, limit: number): RateLimitDecision {
    const now = Date.now();
    const existing = this.counters.get(key);

    if (!existing || existing.resetAt <= now) {
      const resetAt = now + windowMs;
      this.counters.set(key, { count: 1, resetAt });
      return { allowed: true, limit, remaining: limit - 1, resetAt };
    }

    existing.count += 1;

    return {
      allowed: existing.count <= limit,
      limit,
      remaining: Math.max(0, limit - existing.count),
      resetAt: existing.resetAt,
    };
  }

  reset() {
    this.counters.clear();
  }
}

let store: RateLimitStore = new InMemoryRateLimitStore();

export const setRateLimitStore = (next: RateLimitStore) => {
  store = next;
};

export const resetRateLimit = () => store.reset();

export type RateLimitSurface = 'auth' | 'mutation';

const limitFor = (surface: RateLimitSurface) =>
  surface === 'auth'
    ? env.rateLimit.authMaxRequests
    : env.rateLimit.mutationMaxRequests;

export const clientIdentifier = (req: NextApiRequest): string => {
  const forwarded = req.headers['x-forwarded-for'];
  const first = Array.isArray(forwarded) ? forwarded[0] : forwarded;

  return (
    first?.split(',')[0]?.trim() ||
    req.socket?.remoteAddress ||
    'unknown-client'
  );
};

/**
 * Returns true when the request may proceed. When it returns false the
 * response has already been finished with 429.
 */
export const enforceRateLimit = (
  req: NextApiRequest,
  res: NextApiResponse,
  surface: RateLimitSurface
): boolean => {
  if (!env.rateLimit.enabled) {
    return true;
  }

  const limit = limitFor(surface);
  const key = `${surface}:${req.url?.split('?')[0]}:${clientIdentifier(req)}`;
  const decision = store.hit(key, env.rateLimit.windowMs, limit);

  res.setHeader('X-RateLimit-Limit', String(decision.limit));
  res.setHeader('X-RateLimit-Remaining', String(decision.remaining));
  res.setHeader(
    'X-RateLimit-Reset',
    String(Math.ceil(decision.resetAt / 1000))
  );

  if (decision.allowed) {
    return true;
  }

  res.setHeader(
    'Retry-After',
    String(Math.max(1, Math.ceil((decision.resetAt - Date.now()) / 1000)))
  );

  logger.warn('control_plane.rate_limited', {
    surface,
    path: req.url?.split('?')[0],
  });

  res.status(429).json({
    error: { message: 'Too many requests. Please retry later.' },
  });

  return false;
};

/**
 * Wraps a Next.js API handler so every non-GET request is counted.
 */
export const withRateLimit =
  (
    surface: RateLimitSurface,
    handler: (req: NextApiRequest, res: NextApiResponse) => unknown
  ) =>
  async (req: NextApiRequest, res: NextApiResponse) => {
    const counted = surface === 'auth' || req.method !== 'GET';

    if (counted && !enforceRateLimit(req, res, surface)) {
      return;
    }

    return handler(req, res);
  };
