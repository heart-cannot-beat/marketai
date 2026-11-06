import { ApiError } from '@/lib/errors';
import env from '@/lib/env';
import { getTeams } from 'models/team';
import type { NextApiRequest, NextApiResponse } from 'next';
import { recordMetric } from '@/lib/metrics';
import { getCurrentUser } from 'models/user';
import { logger } from '@/lib/logger';
import { withRateLimit } from '@/lib/rateLimit';

async function handler(req: NextApiRequest, res: NextApiResponse) {
  const { method } = req;

  try {
    switch (method) {
      case 'GET':
        await handleGET(req, res);
        break;
      case 'POST':
        await handlePOST(req);
        break;
      default:
        res.setHeader('Allow', 'GET');
        res.status(405).json({
          error: { message: `Method ${method} Not Allowed` },
        });
    }
  } catch (error: any) {
    const message = error.message || 'Something went wrong';
    const status = error.status || 500;

    res.status(status).json({ error: { message } });
  }
}

export default withRateLimit('mutation', handler);

// List the Workspaces the current user is a member of
const handleGET = async (req: NextApiRequest, res: NextApiResponse) => {
  const user = await getCurrentUser(req, res);
  const teams = await getTeams(user.id);

  recordMetric('team.fetched');

  const teamsWithRole = teams.map((team) => ({
    ...team,
    members: undefined,
    role: team.members[0]?.role ?? 'MEMBER',
  }));

  res.status(200).json({ data: teamsWithRole });
};

/**
 * Workspace creation is not a customer-facing capability in the Beta.
 *
 * Provisioning is an operator action (`npm run ops:provision-workspace`), so
 * this route fails closed for authenticated and unauthenticated callers alike.
 * The check runs before any session lookup so that an unauthenticated request
 * cannot distinguish "not signed in" from "creation disabled" and cannot reach
 * the create path at all.
 */
const handlePOST = async (req: NextApiRequest) => {
  if (!env.registration.selfServeWorkspaceCreationEnabled) {
    logger.warn('control_plane.workspace_creation_rejected', {
      path: req.url?.split('?')[0],
    });

    throw new ApiError(
      403,
      'Workspace creation is not available. Workspaces are provisioned by MarketAI during the invite-only Beta.'
    );
  }

  throw new ApiError(403, 'Workspace creation is not available.');
};
