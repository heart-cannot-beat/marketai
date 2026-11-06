import NextAuth from 'next-auth';
import type { NextApiRequest, NextApiResponse } from 'next';

import { getAuthOptions } from '@/lib/nextAuth';
import { enforceRateLimit } from '@/lib/rateLimit';
import signOutHandler from './custom-signout';

const routeSegments = (req: NextApiRequest): string[] =>
  Array.isArray(req.query.nextauth) ? req.query.nextauth : [];

export default async function auth(req: NextApiRequest, res: NextApiResponse) {
  const segments = routeSegments(req);

  /**
   * NextAuth's own `/api/auth/signout` clears the cookie and nothing else.
   *
   * Under this fork's model that is not a sign-out: the JWT is bound to a
   * server-side `Session` row, so a copy of the token held on another device
   * keeps working until the row is deleted. Hooking `events.signOut` is not
   * enough either — NextAuth swallows an exception from the event and clears
   * the cookie anyway, which is precisely the "reported success while still
   * signed in" failure we removed from the custom handler.
   *
   * So the standard route is taken over by the custom handler, which deletes
   * the row first and fails closed if it cannot.
   */
  // Exactly `/api/auth/signout`, not any path that happens to contain the word.
  if (segments.length === 1 && segments[0] === 'signout') {
    if (req.method !== 'POST') {
      res.setHeader('Allow', 'POST');
      return res.status(405).json({
        error: { message: `Method ${req.method} Not Allowed` },
      });
    }

    return signOutHandler(req, res);
  }

  // Credential submission is the highest-value guessing surface in the control
  // plane, so the sign-in callback is rate limited before NextAuth sees it.
  // Session reads (GET) are not counted.
  const isSignInAttempt =
    req.method === 'POST' &&
    (segments.includes('callback') || segments.includes('signin'));

  if (isSignInAttempt && !enforceRateLimit(req, res, 'auth')) {
    return;
  }

  return await NextAuth(req, res, getAuthOptions());
}
