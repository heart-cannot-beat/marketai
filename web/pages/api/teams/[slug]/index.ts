import { sendAudit } from '@/lib/audit';
import {
  getCurrentUserWithTeam,
  getTeam,
  throwIfNoTeamAccess,
  updateTeam,
} from 'models/team';
import { throwIfNotAllowed } from 'models/user';
import type { NextApiRequest, NextApiResponse } from 'next';
import { recordMetric } from '@/lib/metrics';
import { ApiError } from '@/lib/errors';
import { withRateLimit } from '@/lib/rateLimit';
import { updateTeamSchema, validateWithSchema } from '@/lib/zod';
import { Prisma, Team } from '@prisma/client';

async function handler(req: NextApiRequest, res: NextApiResponse) {
  try {
    await throwIfNoTeamAccess(req, res);

    switch (req.method) {
      case 'GET':
        await handleGET(req, res);
        break;
      case 'PUT':
        await handlePUT(req, res);
        break;
      default:
        res.setHeader('Allow', 'GET, PUT');
        res.status(405).json({
          error: { message: `Method ${req.method} Not Allowed` },
        });
    }
  } catch (error: any) {
    const message = error.message || 'Something went wrong';
    const status = error.status || 500;

    res.status(status).json({ error: { message } });
  }
}

// Get a team by slug
const handleGET = async (req: NextApiRequest, res: NextApiResponse) => {
  const user = await getCurrentUserWithTeam(req, res);

  throwIfNotAllowed(user, 'team', 'read');

  const team = await getTeam({ id: user.team.id });

  recordMetric('team.fetched');

  res.status(200).json({ data: team });
};

// Update a team
const handlePUT = async (req: NextApiRequest, res: NextApiResponse) => {
  const user = await getCurrentUserWithTeam(req, res);

  throwIfNotAllowed(user, 'team', 'update');

  const { name, slug, domain } = validateWithSchema(updateTeamSchema, req.body);

  let updatedTeam: Team | null = null;

  try {
    updatedTeam = await updateTeam(user.team.slug, {
      name,
      slug,
      domain,
    });
  } catch (error: any) {
    if (
      error instanceof Prisma.PrismaClientKnownRequestError &&
      error.code === 'P2002' &&
      error.meta?.target
    ) {
      const target = error.meta.target as string[];

      if (target.includes('slug')) {
        throw new ApiError(
          409,
          'This slug is already taken by another Workspace.'
        );
      }

      if (target.includes('domain')) {
        throw new ApiError(
          409,
          'This domain is already associated with another Workspace.'
        );
      }
    }

    throw error;
  }

  sendAudit({
    action: 'team.update',
    crud: 'u',
    user,
    team: user.team,
  });

  recordMetric('team.updated');

  res.status(200).json({ data: updatedTeam });
};

export default withRateLimit('mutation', handler);
