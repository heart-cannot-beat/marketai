import { NextApiRequest, NextApiResponse } from 'next';
import { Prisma } from '@prisma/client';
import { getToken } from 'next-auth/jwt';

import env from '@/lib/env';
import { isSameOrigin } from '@/lib/sameOrigin';
import { sessionTokenCookieNames, useSecureCookie } from '@/lib/nextAuth';
import { deleteSessions } from 'models/session';
import { withRateLimit } from '@/lib/rateLimit';
import { logger } from '@/lib/logger';

const clearSessionCookies = (res: NextApiResponse) => {
  res.setHeader(
    'Set-Cookie',
    sessionTokenCookieNames.map((name) => {
      // The `__Secure-` prefix is only honoured on a Secure cookie.
      const secure = useSecureCookie || name.startsWith('__Secure-');

      return [
        `${name}=`,
        'Path=/',
        'Expires=Thu, 01 Jan 1970 00:00:00 GMT',
        'Max-Age=0',
        'HttpOnly',
        'SameSite=Lax',
        ...(secure ? ['Secure'] : []),
      ].join('; ');
    })
  );
};

/**
 * Every session id the request can prove it holds.
 *
 * `getToken()` picks a cookie name from `NEXTAUTH_URL`/`VERCEL`, while this
 * fork derives the `__Secure-` prefix from `APP_URL`. When those disagree —
 * `APP_URL` on https with `NEXTAUTH_URL` unset, for instance — the default
 * lookup misses the cookie that is actually present, returns null, and sign-out
 * would answer 200, clear the cookies and leave the `Session` row alive. Both
 * supported names are read explicitly instead, and a browser holding both gets
 * both revoked rather than one.
 */
const sessionIdsFromRequest = async (
  req: NextApiRequest
): Promise<string[]> => {
  const ids = new Set<string>();

  for (const cookieName of sessionTokenCookieNames) {
    const token = await getToken({
      req,
      secret: env.nextAuth.secret,
      cookieName,
    }).catch(() => null);

    if (typeof token?.sid === 'string' && token.sid) {
      ids.add(token.sid);
    }
  }

  return Array.from(ids);
};

/**
 * Sign out.
 *
 * The identity is read by verifying the cookie's signature directly rather than
 * through `getServerSession`. That matters for the failure path: the session
 * route reads the database, and when the database is unreachable NextAuth
 * catches the error, writes cookie-clearing headers onto the response and
 * returns an empty session. The handler would then answer 401 on a response
 * that had already signed the browser out while the `Session` row — and every
 * copy of the token — stayed alive. Decoding the JWT touches nothing and
 * mutates nothing.
 *
 * Order is therefore fixed: verify the tokens, delete every row they name in
 * one transaction, and only then clear the cookies.
 */
async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    return res.status(405).json({ error: { message: 'Method not allowed' } });
  }

  // Taking this route over from NextAuth means owning its CSRF protection.
  if (!isSameOrigin(req)) {
    logger.warn('control_plane.signout_rejected_cross_origin');
    return res.status(403).json({ error: { message: 'Forbidden' } });
  }

  try {
    const sessionIds = await sessionIdsFromRequest(req);

    if (sessionIds.length === 0) {
      // Nothing to revoke: no token, an unreadable one, or one that never
      // carried a session id. Clearing the cookies is still the right end
      // state and cannot report a false success, because no session exists.
      clearSessionCookies(res);
      return res.status(200).json({ success: true });
    }

    try {
      await deleteSessions(sessionIds);
    } catch (error) {
      // P2025 means a row is already gone — revoked from another device or by
      // a password reset. That is the desired end state, so carry on.
      const alreadyRevoked =
        error instanceof Prisma.PrismaClientKnownRequestError &&
        error.code === 'P2025';

      if (!alreadyRevoked) {
        // Anything else (connection lost, privilege denied) means the session
        // is still live. Clearing this browser's cookie now would report
        // success while every copy of the token kept working, so fail loudly
        // and leave the cookie untouched.
        logger.error('control_plane.signout_revocation_failed', { error });

        return res.status(503).json({
          error: {
            message:
              'Could not sign you out. Your session is still active — please try again.',
          },
        });
      }
    }

    clearSessionCookies(res);

    logger.info('control_plane.signed_out', {
      revoked_sessions: sessionIds.length,
    });

    return res.status(200).json({ success: true });
  } catch (error) {
    logger.error('control_plane.signout_failed', { error });
    return res.status(500).json({ error: { message: 'Failed to sign out' } });
  }
}

export default withRateLimit('auth', handler);
