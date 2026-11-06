import type { NextApiRequest } from 'next';

import env from './env';

/**
 * Strict same-origin check for state-changing requests that do not go through
 * NextAuth's own CSRF double-submit.
 *
 * Taking over `/api/auth/signout` from NextAuth means taking over its CSRF
 * protection, so this fails closed: the request must prove its origin, and the
 * comparison is on the full origin — scheme, host and port — not just the host.
 * Comparing hosts alone would accept `http://app.example` against
 * `https://app.example`.
 *
 * Browsers send `Origin` on every same-origin non-GET request, so requiring it
 * costs the application nothing. A request that proves nothing is refused.
 */
const originOf = (value: string | undefined): string | null => {
  if (!value) {
    return null;
  }

  try {
    return new URL(value).origin;
  } catch {
    return null;
  }
};

export const trustedOrigin = (): string | null => originOf(env.appUrl);

export const isSameOrigin = (req: NextApiRequest): boolean => {
  const expected = trustedOrigin();

  if (!expected) {
    // A deployment without a usable APP_URL cannot authorize anything.
    return false;
  }

  const origin = originOf(req.headers.origin);

  if (origin) {
    return origin === expected;
  }

  // Some clients omit Origin but send Referer; accept only an exact match.
  const referer = originOf(req.headers.referer);

  if (referer) {
    return referer === expected;
  }

  // Neither header: the request cannot prove where it came from.
  return false;
};
