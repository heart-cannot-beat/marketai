import crypto from 'node:crypto';
import type { NextApiRequest, NextApiResponse } from 'next';

import env from '@/lib/env';
import { prisma } from '@/lib/prisma';

/**
 * `GET /api/internal/domain/workspaces/{workspaceId}/workspace-projection-snapshot`
 * (spec §1.14) — the v2 pull-repair endpoint for EntitlementSnapshot envelopes.
 *
 * Returns the latest `canonicalEnvelope` bytes as `application/octet-stream`.
 * Service-token authenticated (same pattern as `authorization-snapshot.ts`):
 * the caller is FastAPI's entitlement projection sync, which holds no browser
 * session. Listed under the `apiServiceAuthenticatedRoutes` glob in
 * `middleware.ts` so the middleware skips session checks — this handler is
 * solely responsible for its own authentication.
 */
export default async function handler(
  req: NextApiRequest,
  res: NextApiResponse
): Promise<void> {
  if (req.method !== 'GET') {
    res.setHeader('Allow', 'GET');
    res.status(405).end();
    return;
  }

  const serviceToken = env.projectionSyncServiceToken;
  if (!serviceToken) {
    // Fail closed: an operator who has not configured the token gets a
    // loud 500, never a silent "no auth required" fallback.
    res.status(500).json({ error: 'Service token not configured' });
    return;
  }

  const authHeader = req.headers.authorization;
  if (!authHeader || !authHeader.startsWith('Bearer ')) {
    res.status(401).json({ error: 'Unauthorized' });
    return;
  }

  const token = authHeader.slice(7);
  const expected = Buffer.from(serviceToken, 'utf-8');
  const actual = Buffer.from(token, 'utf-8');
  // Length check before `timingSafeEqual` — it throws (not returns false) on
  // differing buffer lengths, which is the common case (empty/short token).
  if (
    expected.length !== actual.length ||
    !crypto.timingSafeEqual(expected, actual)
  ) {
    res.status(403).json({ error: 'Forbidden' });
    return;
  }

  const workspaceId = req.query.workspaceId;
  if (typeof workspaceId !== 'string' || workspaceId.length === 0) {
    res.status(400).json({ error: 'Invalid workspace ID' });
    return;
  }

  const snapshot = await prisma.entitlementSnapshot.findFirst({
    where: { teamId: workspaceId },
    orderBy: [{ entitlementVersion: 'desc' }, { leaseSequence: 'desc' }],
    select: { canonicalEnvelope: true },
  });

  if (!snapshot?.canonicalEnvelope) {
    res.status(404).json({ error: 'No snapshot available' });
    return;
  }

  // Never cached: this snapshot feeds an entitlement projection and must
  // always be read fresh from the database.
  res.setHeader('Cache-Control', 'no-store');
  res.setHeader('Content-Type', 'application/octet-stream');
  res.status(200).send(Buffer.from(snapshot.canonicalEnvelope));
}
