import crypto from 'node:crypto';
import type { NextApiRequest, NextApiResponse } from 'next';

import env from '@/lib/env';
import { requireSession } from '@/lib/requireSession';

/**
 * `GET /api/csrf` (spec §4.1, §6.9) — issues a CSRF token bound to the
 * caller's session, identified via the existing HttpOnly session cookie
 * (not a request parameter).
 *
 * The token is `HMAC-SHA256(NEXTAUTH_SECRET, "csrf:" + sessionId)`: nothing
 * is persisted, so a Team of BFF processes issues and verifies the same
 * token without a shared store. Listed in `apiSelfAuthenticatedRoutes` in
 * `middleware.ts` — it calls `requireSession` itself rather than relying on
 * the middleware's redirect-based gate.
 */
export default async function handler(
  req: NextApiRequest,
  res: NextApiResponse
): Promise<void> {
  try {
    if (req.method !== 'GET') {
      res.setHeader('Allow', 'GET');
      res.status(405).json({
        error: { message: `Method ${req.method} Not Allowed` },
      });
      return;
    }

    await handleGET(req, res);
  } catch (error: any) {
    const message = error.message || 'Something went wrong';
    const status = error.status || 500;

    res.status(status).json({ error: { message } });
  }
}

const handleGET = async (
  req: NextApiRequest,
  res: NextApiResponse
): Promise<void> => {
  const session = await requireSession(req, res);

  const secret = env.nextAuth.secret;
  if (!secret) {
    res.status(500).json({ error: { message: 'Server misconfigured' } });
    return;
  }

  const token = crypto
    .createHmac('sha256', secret)
    .update(`csrf:${session.sessionId}`)
    .digest('hex');

  // Never cached by an intermediary: the token is session-bound, and a
  // shared/CDN cache serving one user's token to another would defeat the
  // whole point of the check.
  res.setHeader('Cache-Control', 'no-store');
  res.status(200).json({ token });
};
