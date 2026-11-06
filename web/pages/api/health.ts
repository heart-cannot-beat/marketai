import { NextApiRequest, NextApiResponse } from 'next';

import packageInfo from '../../package.json';

/**
 * Liveness probe.
 *
 * Deliberately does not touch PostgreSQL. Upstream's health endpoint ran
 * `SELECT 1`, which makes a database blip look like a dead process and gets the
 * container restarted instead of drained. Dependency health is reported by
 * `/api/readiness`.
 */
export default async function handler(
  req: NextApiRequest,
  res: NextApiResponse
) {
  if (req.method !== 'GET') {
    res.setHeader('Allow', 'GET');
    res.status(405).json({ error: { message: 'Method Not Allowed' } });
    return;
  }

  res.status(200).json({
    status: 'ok',
    service: 'control-plane',
    version: packageInfo.version,
  });
}
