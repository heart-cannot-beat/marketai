import { createMocks } from 'node-mocks-http';
import type { NextApiRequest, NextApiResponse } from 'next';

import invitationsHandler from '../../../pages/api/teams/[slug]/invitations';
import membersHandler from '../../../pages/api/teams/[slug]/members';
import joinHandler from '../../../pages/api/auth/join';
import { ApiError } from '@/lib/errors';
import { resetRateLimit } from '@/lib/rateLimit';
import { Prisma } from '@prisma/client';

jest.mock('@/lib/prisma', () => ({ prisma: {} }));
jest.mock('@/lib/requireSession', () => ({ requireSession: jest.fn() }));
jest.mock('@/lib/audit', () => ({ sendAudit: jest.fn() }));
jest.mock('@/lib/metrics', () => ({ recordMetric: jest.fn() }));
jest.mock('@/lib/auth', () => ({
  hashPassword: jest.fn(async () => 'hashed'),
}));
jest.mock('@/lib/email/sendTeamInviteEmail', () => ({
  sendTeamInviteEmail: jest.fn(),
}));

jest.mock('models/invitation', () => ({
  ...jest.requireActual('models/invitation'),
  acceptInvitationForNewUser: jest.fn(),
  consumeInvitation: jest.fn(),
  createInvitation: jest.fn(),
  getInvitation: jest.fn(),
  getInvitationCount: jest.fn(),
  isInvitationExpired: jest.fn(),
}));
jest.mock('models/team', () => ({
  getTeamMember: jest.fn(),
  getTeamMembers: jest.fn(),
  removeTeamMember: jest.fn(),
  throwIfNoTeamAccess: jest.fn(),
}));
jest.mock('models/teamMember', () => ({
  countTeamMembers: jest.fn(),
  updateTeamMember: jest.fn(),
}));
jest.mock('models/user', () => ({
  ...jest.requireActual('models/user'),
  getUser: jest.fn(),
}));

import { sendTeamInviteEmail } from '@/lib/email/sendTeamInviteEmail';
import { requireSession } from '@/lib/requireSession';
import {
  acceptInvitationForNewUser,
  consumeInvitation,
  createInvitation,
  getInvitation,
  getInvitationCount,
  isInvitationExpired,
} from 'models/invitation';
import { getTeamMember, throwIfNoTeamAccess } from 'models/team';
import { countTeamMembers, updateTeamMember } from 'models/teamMember';
import { getUser } from 'models/user';

/**
 * A seat refusal (#157) reaches the page as its code and the limit its copy
 * interpolates, on every route that can raise one; the page renders
 * `error-<code>` (see `__tests__/locales/error-codes.spec.ts`).
 */
const team = { id: 't1', slug: 'acme', lifecycle: 'ACTIVE' };
const owner = {
  role: 'OWNER',
  teamId: team.id,
  team,
  userId: 'owner_1',
  sessionId: 'sess_1',
  user: { id: 'owner_1', email: 'owner@acmecharge.example' },
};

const reached = new ApiError(409, 'All seats in this Workspace are taken.', {
  code: 'seat_limit_reached',
  params: { limit: 5 },
});
const exceeded = new ApiError(409, 'This Workspace is over its seat limit.', {
  code: 'seat_limit_exceeded',
  params: { limit: 3 },
});

const call = async (
  handler: (req: NextApiRequest, res: NextApiResponse) => Promise<unknown>,
  options: Parameters<typeof createMocks>[0]
) => {
  const { req, res } = createMocks<NextApiRequest, NextApiResponse>(options);
  await handler(req, res);
  return { status: res.statusCode, body: res._getJSONData() };
};

beforeEach(() => {
  resetRateLimit();
  jest.clearAllMocks();
  (throwIfNoTeamAccess as jest.Mock).mockResolvedValue(owner);
  (countTeamMembers as jest.Mock).mockResolvedValue(0);
  (getInvitationCount as jest.Mock).mockResolvedValue(0);
});

describe('seat refusals carry their code', () => {
  it('answers a refused invitation with seat_limit_reached and sends no email', async () => {
    (createInvitation as jest.Mock).mockRejectedValue(reached);

    const { status, body } = await call(invitationsHandler, {
      method: 'POST',
      query: { slug: team.slug },
      body: { email: 'new@acmecharge.example', role: 'MEMBER' },
    });

    expect(status).toBe(409);
    expect(body.error).toEqual({
      message: 'All seats in this Workspace are taken.',
      code: 'seat_limit_reached',
      params: { limit: 5 },
    });
    expect(sendTeamInviteEmail).not.toHaveBeenCalled();
  });

  it('answers a refused acceptance with seat_limit_exceeded', async () => {
    (requireSession as jest.Mock).mockResolvedValue({
      user: { id: 'invitee_1', email: 'new@acmecharge.example' },
    });
    (consumeInvitation as jest.Mock).mockRejectedValue(exceeded);

    const { status, body } = await call(invitationsHandler, {
      method: 'PUT',
      query: { slug: team.slug },
      body: { inviteToken: 'token-a-newcomer' },
    });

    expect(status).toBe(409);
    expect(body.error).toMatchObject({
      code: 'seat_limit_exceeded',
      params: { limit: 3 },
    });
  });

  it('answers a refused sign-up through an invitation with seat_limit_exceeded', async () => {
    (getInvitation as jest.Mock).mockResolvedValue({
      email: 'new@acmecharge.example',
      expires: new Date(Date.now() + 60_000),
    });
    (isInvitationExpired as jest.Mock).mockResolvedValue(false);
    (getUser as jest.Mock).mockResolvedValue(null);
    (acceptInvitationForNewUser as jest.Mock).mockRejectedValue(exceeded);

    const { status, body } = await call(joinHandler, {
      method: 'POST',
      body: {
        name: 'New Person',
        password: 'correct-Horse-battery-9',
        inviteToken: 'token-a-newcomer',
      },
    });

    expect(status).toBe(409);
    expect(body.error).toMatchObject({ code: 'seat_limit_exceeded' });
  });

  it('answers a refused promotion with seat_limit_exceeded', async () => {
    (getTeamMember as jest.Mock).mockResolvedValue({
      role: 'MEMBER',
      userId: 'member_2',
    });
    (updateTeamMember as jest.Mock).mockRejectedValue(exceeded);

    const { status, body } = await call(membersHandler, {
      method: 'PATCH',
      query: { slug: team.slug },
      body: {
        memberId: 'member_2',
        role: 'OWNER',
        confirmation: 'PROMOTE TO OWNER',
      },
    });

    expect(status).toBe(409);
    expect(body.error).toMatchObject({
      code: 'seat_limit_exceeded',
      params: { limit: 3 },
    });
  });

  it('passes on no code of its own for any other failure', async () => {
    (createInvitation as jest.Mock).mockRejectedValue(
      new Prisma.PrismaClientKnownRequestError('Unique constraint failed', {
        code: 'P2002',
        clientVersion: 'test',
      })
    );

    const { status, body } = await call(invitationsHandler, {
      method: 'POST',
      query: { slug: team.slug },
      body: { email: 'new@acmecharge.example', role: 'MEMBER' },
    });

    expect(status).toBe(500);
    expect(body.error).toEqual({ message: 'Unique constraint failed' });
  });
});
