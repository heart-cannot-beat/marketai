import { NextApiRequest, NextApiResponse } from 'next';
import { prisma } from '@/lib/prisma';
import { logger } from '@/lib/logger';

import packageInfo from '../../package.json';

type CheckResult = { name: string; ok: boolean; detail?: string };

const requiredConfig = ['DATABASE_URL', 'NEXTAUTH_SECRET', 'APP_URL'];

/**
 * Readiness probe.
 *
 * Reports whether the control plane can serve traffic: required configuration
 * is present and the control-plane database answers. Never returns the values
 * of the configuration keys it checks.
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

  const checks: CheckResult[] = [];

  const missingConfig = requiredConfig.filter((key) => !process.env[key]);

  checks.push({
    name: 'configuration',
    ok: missingConfig.length === 0,
    detail:
      missingConfig.length === 0
        ? undefined
        : `missing: ${missingConfig.join(', ')}`,
  });

  try {
    await prisma.$queryRaw`SELECT 1`;
    checks.push({ name: 'control_plane_database', ok: true });
  } catch (error: any) {
    logger.error('control_plane.readiness_database_failed', { error });
    checks.push({
      name: 'control_plane_database',
      ok: false,
      detail: 'unreachable',
    });
  }

  const ready = checks.every((check) => check.ok);

  res.status(ready ? 200 : 503).json({
    status: ready ? 'ready' : 'not-ready',
    service: 'control-plane',
    version: packageInfo.version,
    checks,
  });
}
