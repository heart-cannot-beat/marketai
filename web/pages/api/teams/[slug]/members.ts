import { ApiError, apiErrorBody } from '@/lib/errors';
import { sendAudit } from '@/lib/audit';
import { Role } from '@prisma/client';
import {
  getTeamMembers,
  removeTeamMember,
  throwIfNoTeamAccess,
} from 'models/team';
import { throwIfNotAllowed } from 'models/user';
import type { NextApiRequest, NextApiResponse } from 'next';
import { recordMetric } from '@/lib/metrics';
import { countTeamMembers, updateTeamMember } from 'models/teamMember';
import {
  assertCustomerRole,
  requireConfirmationPhrase,
  validateMembershipOperation,
} from '@/lib/rbac';
import { withRateLimit } from '@/lib/rateLimit';
import {
  deleteMemberSchema,
  updateMemberSchema,
  validateWithSchema,
} from '@/lib/zod';

async function handler(req: NextApiRequest, res: NextApiResponse) {
  const { method } = req;

  try {
    switch (method) {
      case 'GET':
        await handleGET(req, res);
        break;
      case 'DELETE':
        await handleDELETE(req, res);
        break;
      case 'PUT':
        await handlePUT(req, res);
        break;
      case 'PATCH':
        await handlePATCH(req, res);
        break;
      default:
        res.setHeader('Allow', 'GET, DELETE, PUT, PATCH');
        res.status(405).json({
          error: { message: `Method ${method} Not Allowed` },
        });
    }
  } catch (error: any) {
    const status = error.status || 500;

    res.status(status).json({ error: apiErrorBody(error) });
  }
}

// Get members of a team
const handleGET = async (req: NextApiRequest, res: NextApiResponse) => {
  const teamMember = await throwIfNoTeamAccess(req, res);
  throwIfNotAllowed(teamMember, 'team_member', 'read');

  const members = await getTeamMembers(teamMember.team.slug);

  recordMetric('member.fetched');

  res.status(200).json({ data: members });
};

// Delete the member from the team
const handleDELETE = async (req: NextApiRequest, res: NextApiResponse) => {
  const teamMember = await throwIfNoTeamAccess(req, res);
  throwIfNotAllowed(teamMember, 'team_member', 'delete');
  if (teamMember.team.lifecycle !== 'ACTIVE') {
    throw new ApiError(409, 'Workspace commands require an ACTIVE Workspace.');
  }

  const { memberId } = validateWithSchema(
    deleteMemberSchema,
    req.query as { memberId: string }
  );

  const removingMember = await validateMembershipOperation(
    memberId,
    teamMember
  );
  if (removingMember.role === Role.OWNER) {
    requireConfirmationPhrase(req.query.confirmation, 'REMOVE OWNER');
  }

  await removeTeamMember(teamMember.teamId, memberId, {
    actor: { kind: 'USER', id: teamMember.user.id },
    reason: 'REMOVED',
  });

  sendAudit({
    action: 'member.remove',
    crud: 'd',
    user: teamMember.user,
    team: teamMember.team,
  });

  recordMetric('member.removed');

  res.status(200).json({ data: {} });
};

// Leave a team
const handlePUT = async (req: NextApiRequest, res: NextApiResponse) => {
  const teamMember = await throwIfNoTeamAccess(req, res);
  throwIfNotAllowed(teamMember, 'team', 'leave');
  if (teamMember.team.lifecycle !== 'ACTIVE') {
    throw new ApiError(409, 'Workspace commands require an ACTIVE Workspace.');
  }

  const totalTeamOwners = await countTeamMembers({
    where: {
      role: Role.OWNER,
      teamId: teamMember.teamId,
    },
  });

  if (totalTeamOwners <= 1) {
    throw new ApiError(400, 'A Workspace must keep at least one Owner.');
  }

  if (teamMember.role === Role.OWNER) {
    requireConfirmationPhrase(req.body?.confirmation, 'REMOVE OWNER');
  }

  await removeTeamMember(teamMember.teamId, teamMember.user.id, {
    actor: { kind: 'USER', id: teamMember.user.id },
    reason: 'LEFT',
  });

  recordMetric('member.left');

  res.status(200).json({ data: {} });
};

// Update the role of a member
const handlePATCH = async (req: NextApiRequest, res: NextApiResponse) => {
  const teamMember = await throwIfNoTeamAccess(req, res);
  throwIfNotAllowed(teamMember, 'team_member', 'update');
  if (teamMember.team.lifecycle !== 'ACTIVE') {
    throw new ApiError(409, 'Workspace commands require an ACTIVE Workspace.');
  }

  const { memberId, role } = validateWithSchema(
    updateMemberSchema,
    req.body as { memberId: string; role: Role }
  );

  // Belt and braces: the schema already rejects non-contract roles.
  assertCustomerRole(role);

  const updatingMember = await validateMembershipOperation(
    memberId,
    teamMember,
    {
      role,
    }
  );
  if (role === Role.OWNER || updatingMember.role === Role.OWNER) {
    requireConfirmationPhrase(
      req.body?.confirmation,
      role === Role.OWNER ? 'PROMOTE TO OWNER' : 'REMOVE OWNER'
    );
  }

  const memberUpdated = await updateTeamMember({
    teamId: teamMember.teamId,
    userId: memberId,
    role,
    actor: { kind: 'USER', id: teamMember.user.id },
  });

  sendAudit({
    action: 'member.update',
    crud: 'u',
    user: teamMember.user,
    team: teamMember.team,
  });

  recordMetric('member.role.updated');

  res.status(200).json({ data: memberUpdated });
};

export default withRateLimit('mutation', handler);
