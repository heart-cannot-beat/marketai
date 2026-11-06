import crypto from 'node:crypto';
import type { NextApiRequest, NextApiResponse } from 'next';

import env from '@/lib/env';
import { prisma } from '@/lib/prisma';

/**
 * `GET /api/internal/domain/workspaces/{workspaceId}/authorization-snapshot`
 * (spec §3.10) — the read side of FastAPI's authorization projection sync.
 *
 * Service-token authenticated, not session-authenticated: the caller is
 * FastAPI's own sync job, which holds no browser session. Listed in
 * `apiServiceAuthenticatedRoutes` in `middleware.ts` so the middleware skips
 * both the unauthenticated bypass and the session-redirect gate for this
 * path — the handler is solely responsible for its own authentication.
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

  const team = await prisma.team.findUnique({
    where: { id: workspaceId },
    select: {
      id: true,
      name: true,
      authorizationEpoch: true,
      entitlementVersion: true,
      suspended: true,
      projectionVersion: true,
    },
  });

  if (!team) {
    res.status(404).json({ error: 'Workspace not found' });
    return;
  }

  // Never cached: this snapshot feeds an authorization projection and must
  // always be read fresh from the database.
  res.setHeader('Cache-Control', 'no-store');
  res.status(200).json({
    workspace_id: team.id,
    workspace_name: team.name,
    authorization_epoch: team.authorizationEpoch,
    entitlement_version: team.entitlementVersion,
    suspended: team.suspended,
    projection_version: team.projectionVersion,
  });
}
