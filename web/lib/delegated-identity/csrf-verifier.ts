import crypto from 'node:crypto';
import type { NextApiRequest } from 'next';

import type { AuthenticatedSession } from '../requireSession';
import { ApiError } from './token-issuer';

/**
 * CSRF token binding for the BFF proxy (spec §4.1).
 *
 * The token is not stored anywhere: it is `HMAC-SHA256(secret, "csrf:" +
 * sessionId)`, so any request carrying a live session can recompute the
 * expected value and compare it against what the browser sends back on
 * `X-CSRF-Token`. `GET /api/csrf` (`pages/api/csrf.ts`) hands the browser
 * this same value once its session cookie identifies it.
 */
export interface CsrfVerifier {
  verify(req: NextApiRequest, session: AuthenticatedSession): Promise<void>;
}

const SAFE_METHODS = new Set(['GET', 'HEAD']);

export class HmacCsrfVerifier implements CsrfVerifier {
  constructor(private readonly secret: string) {}

  async verify(
    req: NextApiRequest,
    session: AuthenticatedSession
  ): Promise<void> {
    const method = (req.method ?? 'GET').toUpperCase();

    // Safe methods carry no side effects, so they need no CSRF proof (spec
    // §4.1 step 1). Enforcing this here — not just at the call site — keeps
    // the guard true even if a future caller invokes `verify()` unguarded.
    if (SAFE_METHODS.has(method)) {
      return;
    }

    const headerToken = req.headers['x-csrf-token'];
    if (typeof headerToken !== 'string' || headerToken.length === 0) {
      throw new ApiError(403, 'csrf_validation_failed');
    }

    const expected = this.expectedToken(session.sessionId);

    // Length must match before `timingSafeEqual` — it throws (not returns
    // false) on differing buffer lengths, and a length mismatch is common
    // (empty header, truncated header, token from a stale/different session).
    const headerBuffer = Buffer.from(headerToken, 'utf8');
    const expectedBuffer = Buffer.from(expected, 'utf8');
    if (
      headerBuffer.length !== expectedBuffer.length ||
      !crypto.timingSafeEqual(headerBuffer, expectedBuffer)
    ) {
      throw new ApiError(403, 'csrf_validation_failed');
    }
  }

  private expectedToken(sessionId: string): string {
    return crypto
      .createHmac('sha256', this.secret)
      .update(`csrf:${sessionId}`)
      .digest('hex');
  }
}
