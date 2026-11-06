import { prisma } from '@/lib/prisma';
import { requireSession } from '@/lib/requireSession';
import { Role, Team } from '@prisma/client';
import type { NextApiRequest, NextApiResponse } from 'next';
import { getCurrentUser } from './user';
import { normalizeUser } from './user';
import { validateWithSchema, teamSlugSchema } from '@/lib/zod';
import { customerRoles } from '@/lib/roles';

/**
 * Operator-only Workspace provisioning.
 *
 * There is no customer-facing route that reaches this function: `POST
 * /api/teams` and the team-creating branch of `POST /api/auth/join` are both
 * closed. It is called by `scripts/provision-workspace.ts` only.
 */
export const createTeam = async (param: {
  userId: string;
  name: string;
  slug: string;
}) => {
  const { userId, name, slug } = param;

  const team = await prisma.team.create({
    data: {
      name,
      slug,
      lifecycle: 'ACTIVE',
    },
  });

  await addTeamMember(team.id, userId, Role.OWNER);

  return team;
};

export const getTeam = async (key: { id: string } | { slug: string }) => {
  return await prisma.team.findUniqueOrThrow({
    where: key,
  });
};

/**
 * There is deliberately no `deleteTeam`.
 *
 * CONTEXT.md defines the Workspace lifecycle as
 * provisioning -> active -> suspended -> deletion_pending -> deleted, with a
 * 30-day recovery window. Upstream exported `prisma.team.delete`, which is a
 * physical delete and cannot express that. Deletion returns when the lifecycle
 * state machine lands; until then no helper exists for anything to call.
 */

export const addTeamMember = async (
  teamId: string,
  userId: string,
  role: Role
) => {
  return await prisma.teamMember.upsert({
    create: {
      teamId,
      userId,
      role,
    },
    update: {
      role,
    },
    where: {
      teamId_userId: {
        teamId,
        userId,
      },
    },
  });
};

export const removeTeamMember = async (teamId: string, userId: string) => {
  return await prisma.teamMember.delete({
    where: {
      teamId_userId: {
        teamId,
        userId,
      },
    },
  });
};

export const getTeams = async (userId: string) => {
  return await prisma.team.findMany({
    where: {
      members: {
        some: {
          userId,
        },
      },
    },
    include: {
      _count: {
        select: { members: true },
      },
      members: {
        where: { userId },
        select: { role: true },
        take: 1,
      },
    },
    orderBy: [{ createdAt: 'asc' }, { id: 'asc' }],
  });
};

export async function getTeamRoles(userId: string) {
  return await prisma.teamMember.findMany({
    where: {
      userId,
    },
    select: {
      teamId: true,
      role: true,
    },
  });
}

// Check if the user owns the team
export async function isTeamOwner(userId: string, teamId: string) {
  const teamMember = await prisma.teamMember.findUniqueOrThrow({
    where: {
      teamId_userId: {
        userId,
        teamId,
      },
    },
  });

  return teamMember.role === Role.OWNER;
}

export const getTeamMembers = async (slug: string) => {
  const members = await prisma.teamMember.findMany({
    where: {
      team: {
        slug,
      },
    },
    include: {
      user: {
        select: {
          name: true,
          email: true,
          image: true,
        },
      },
    },
  });

  return members?.map((member) => {
    member.user = normalizeUser(member.user);
    return member;
  });
};

export const updateTeam = async (slug: string, data: Partial<Team>) => {
  return await prisma.team.update({
    where: {
      slug,
    },
    data: data,
  });
};

export const isTeamExists = async (slug: string) => {
  return await prisma.team.count({
    where: {
      slug,
    },
  });
};

// Check if the current user has access to the team
// Should be used in API routes to check if the user has access to the team
export const throwIfNoTeamAccess = async (
  req: NextApiRequest,
  res: NextApiResponse
) => {
  const session = await requireSession(req, res);

  const { slug } = validateWithSchema(teamSlugSchema, req.query);

  const teamMember = await getTeamMember(session.user.id, slug);

  if (!teamMember) {
    throw new Error('You do not have access to this Workspace');
  }

  return {
    ...teamMember,
    sessionId: session.sessionId,
    user: {
      ...session.user,
    },
  };
};

// Get the current user's team member object
export const getTeamMember = async (userId: string, slug: string) => {
  return await prisma.teamMember.findFirstOrThrow({
    where: {
      userId,
      team: {
        slug,
      },
      role: {
        in: [...customerRoles],
      },
    },
    include: {
      team: true,
    },
  });
};

// Get current user with team info
export const getCurrentUserWithTeam = async (
  req: NextApiRequest,
  res: NextApiResponse
) => {
  const user = await getCurrentUser(req, res);

  const { slug } = validateWithSchema(teamSlugSchema, req.query);

  const { role, team } = await getTeamMember(user.id, slug);

  return {
    ...user,
    role,
    team,
  };
};
