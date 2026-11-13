import { sendTeamInviteEmail } from '@/lib/email/sendTeamInviteEmail';
import { ApiError, apiErrorBody } from '@/lib/errors';
import { sendAudit } from '@/lib/audit';
import { requireSession } from '@/lib/requireSession';
import { assertCustomerRole, requireConfirmationPhrase } from '@/lib/rbac';
import { withRateLimit } from '@/lib/rateLimit';
import {
  consumeInvitation,
  createInvitation,
  deleteInvitation,
  getInvitation,
  getInvitationCount,
  getInvitations,
  normalizeEmail,
} from 'models/invitation';
import { throwIfNoTeamAccess } from 'models/team';
import { throwIfNotAllowed } from 'models/user';
import type { NextApiRequest, NextApiResponse } from 'next';
import { recordMetric } from '@/lib/metrics';
import { isEmailAllowed } from '@/lib/email/utils';
import { Role } from '@prisma/client';
import { countTeamMembers } from 'models/teamMember';
import {
  acceptInvitationSchema,
  deleteInvitationSchema,
  inviteViaEmailSchema,
  validateWithSchema,
} from '@/lib/zod';

async function handler(req: NextApiRequest, res: NextApiResponse) {
  const { method } = req;

  try {
    switch (method) {
      case 'GET':
        await handleGET(req, res);
        break;
      case 'POST':
        await handlePOST(req, res);
        break;
      case 'PUT':
        await handlePUT(req, res);
        break;
      case 'DELETE':
        await handleDELETE(req, res);
        break;
      default:
        res.setHeader('Allow', 'GET, POST, PUT, DELETE');
        res.status(405).json({
          error: { message: `Method ${method} Not Allowed` },
        });
    }
  } catch (error: any) {
    const status = error.status || 500;

    res.status(status).json({ error: apiErrorBody(error) });
  }
}

/**
 * Invite one named person to a Workspace.
 *
 * Upstream also created shareable link invitations (`sentViaEmail: false`) that
 * admitted anyone holding the URL whose email domain matched. That mode is gone:
 * an invitation is addressed to exactly one email address and is consumed once.
 */
const handlePOST = async (req: NextApiRequest, res: NextApiResponse) => {
  const teamMember = await throwIfNoTeamAccess(req, res);
  throwIfNotAllowed(teamMember, 'team_invitation', 'create');

  const { email, role } = validateWithSchema(
    inviteViaEmailSchema,
    req.body
  ) as {
    email: string;
    role: Role;
  };

  // Invitations are the only way an account enters a Workspace, so the role
  // they carry is checked again here.
  assertCustomerRole(role);
  if (teamMember.team.lifecycle !== 'ACTIVE') {
    throw new ApiError(409, 'Workspace commands require an ACTIVE Workspace.');
  }
  if (role === Role.OWNER) {
    requireConfirmationPhrase(req.body?.confirmation, 'INVITE OWNER');
  }

  const normalizedEmail = normalizeEmail(email);

  if (!isEmailAllowed(normalizedEmail)) {
    throw new ApiError(
      400,
      'It seems you entered a non-business email. Invitations can only be sent to work emails.'
    );
  }

  const memberExists = await countTeamMembers({
    where: {
      teamId: teamMember.teamId,
      user: {
        email: normalizedEmail,
      },
    },
  });

  if (memberExists) {
    throw new ApiError(400, 'This user is already a member of the Workspace.');
  }

  const invitationExists = await getInvitationCount({
    where: {
      email: normalizedEmail,
      teamId: teamMember.teamId,
      consumedAt: null,
      revokedAt: null,
    },
  });

  if (invitationExists) {
    throw new ApiError(400, 'An invitation already exists for this email.');
  }

  const invitation = await createInvitation({
    teamId: teamMember.teamId,
    invitedBy: teamMember.userId,
    email: normalizedEmail,
    role,
  });

  await sendTeamInviteEmail(teamMember.team, invitation);

  sendAudit({
    action: 'member.invitation.create',
    crud: 'c',
    user: teamMember.user,
    team: teamMember.team,
  });

  recordMetric('invitation.created');

  res.status(204).end();
};

// Get all invitations for a team
const handleGET = async (req: NextApiRequest, res: NextApiResponse) => {
  const teamMember = await throwIfNoTeamAccess(req, res);
  throwIfNotAllowed(teamMember, 'team_invitation', 'read');

  const invitations = await getInvitations(teamMember.teamId);

  recordMetric('invitation.fetched');

  res.status(200).json({ data: invitations });
};

// Revoke an invitation
const handleDELETE = async (req: NextApiRequest, res: NextApiResponse) => {
  const teamMember = await throwIfNoTeamAccess(req, res);
  throwIfNotAllowed(teamMember, 'team_invitation', 'delete');
  if (teamMember.team.lifecycle !== 'ACTIVE') {
    throw new ApiError(409, 'Workspace commands require an ACTIVE Workspace.');
  }

  const { id } = validateWithSchema(
    deleteInvitationSchema,
    req.query as { id: string }
  );

  const invitation = await getInvitation({ id });

  if (
    invitation.invitedBy != teamMember.user.id ||
    invitation.team.id != teamMember.teamId
  ) {
    throw new ApiError(
      400,
      `You don't have permission to delete this invitation.`
    );
  }

  await deleteInvitation({ id });

  sendAudit({
    action: 'member.invitation.delete',
    crud: 'd',
    user: teamMember.user,
    team: teamMember.team,
  });

  recordMetric('invitation.removed');

  res.status(200).json({ data: {} });
};

/**
 * Accept an invitation.
 *
 * Consumption is one transaction: the invitation row is deleted and the
 * membership created together, or neither happens.
 */
const handlePUT = async (req: NextApiRequest, res: NextApiResponse) => {
  const { inviteToken } = validateWithSchema(
    acceptInvitationSchema,
    req.body as { inviteToken: string }
  );

  const session = await requireSession(req, res);

  const membership = await consumeInvitation({
    token: inviteToken,
    userId: session.user.id,
    email: session.user.email,
  });

  // Issue, revoke and accept are all auditable; upstream recorded only the
  // first two and left acceptance — the moment an account gains access to a
  // Workspace — with nothing but a metric.
  sendAudit({
    action: 'member.invitation.accept',
    crud: 'c',
    user: { id: session.user.id, email: session.user.email },
    team: { id: membership.teamId, slug: membership.teamSlug },
  });

  recordMetric('member.created');

  res.status(204).end();
};

export default withRateLimit('mutation', handler);
