import type { NextApiRequest, NextApiResponse } from 'next';

import { getJwksPublisher } from '@/lib/delegated-identity/instances';

/**
 * `GET /.well-known/jwks.json` (spec §1.4), reached through the `next.config.js`
 * rewrite since Pages Router API routes must live under `/api/*`.
 *
 * Public — listed in `unAuthenticatedRoutes` in `middleware.ts` — because it
 * publishes only the public half of ACTIVE/RETIRING signing keys, which
 * FastAPI needs to verify delegated JWTs without a session of its own.
 */
export default async function handler(
  req: NextApiRequest,
  res: NextApiResponse
): Promise<void> {
  if (req.method !== 'GET' && req.method !== 'HEAD') {
    res.setHeader('Allow', 'GET, HEAD');
    res.status(405).end();
    return;
  }

  const publisher = getJwksPublisher();
  const requestEtag = req.headers['if-none-match'];
  const normalizedEtag = Array.isArray(requestEtag)
    ? requestEtag[0]
    : requestEtag;
  const result = await publisher.jwksResponse(normalizedEtag ?? null);

  res.setHeader('Content-Type', 'application/json');
  res.setHeader('Cache-Control', result.cacheControl);
  res.setHeader('ETag', result.etag);

  if (result.status === 304) {
    res.status(304).end();
    return;
  }

  if (req.method === 'HEAD') {
    res.status(200).end();
    return;
  }

  res.status(200).send(result.body);
}
