import { createMocks } from 'node-mocks-http';
import type { NextApiRequest, NextApiResponse } from 'next';

import joinHandler from '../../pages/api/auth/join';
import teamsHandler from '../../pages/api/teams';
import env from '@/lib/env';
import { resetRateLimit } from '@/lib/rateLimit';

jest.mock('models/invitation', () => ({
  getInvitation: jest.fn(),
  isInvitationExpired: jest.fn(),
}));

jest.mock('models/user', () => ({
  createUser: jest.fn(),
  getUser: jest.fn(),
  getCurrentUser: jest.fn(),
}));

jest.mock('models/team', () => ({
  getTeam: jest.fn(),
  getTeams: jest.fn(),
  createTeam: jest.fn(),
}));

import { getInvitation } from 'models/invitation';
import { createUser } from 'models/user';
import { createTeam, getTeams } from 'models/team';

describe('Invite-only entry is enforced server-side', () => {
  beforeEach(() => {
    resetRateLimit();
    jest.clearAllMocks();
  });

  it('declares public signup and self-serve Workspace creation as closed', () => {
    expect(env.registration.publicSignupEnabled).toBe(false);
    expect(env.registration.selfServeWorkspaceCreationEnabled).toBe(false);
  });

  it('rejects signup without an invitation token', async () => {
    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'POST',
      body: {
        name: 'Public Person',
        email: 'public@example.com',
        password: 'correct-horse-battery',
        team: 'A brand new workspace',
      },
    });

    await joinHandler(req, res);

    expect(res.statusCode).toBe(403);
    expect(res._getJSONData().error.message).toMatch(/invitation-only/i);
    expect(createUser).not.toHaveBeenCalled();
    expect(createTeam).not.toHaveBeenCalled();
  });

  it('never creates a Workspace as a side effect of signup', async () => {
    (getInvitation as jest.Mock).mockRejectedValue(new Error('not found'));

    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'POST',
      body: {
        name: 'Invited Person',
        email: 'invited@example.com',
        password: 'correct-horse-battery',
        team: 'A brand new workspace',
        inviteToken: 'does-not-exist',
      },
    });

    await joinHandler(req, res);

    expect(res.statusCode).toBe(403);
    expect(createTeam).not.toHaveBeenCalled();
  });

  it('rejects unauthenticated Workspace creation before any session lookup', async () => {
    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'POST',
      body: { name: 'Unauthenticated Workspace' },
    });

    await teamsHandler(req, res);

    expect(res.statusCode).toBe(403);
    expect(res._getJSONData().error.message).toMatch(
      /Workspace creation is not available/i
    );
    expect(createTeam).not.toHaveBeenCalled();
    // No session was consulted: the route fails closed, it does not 401.
    expect(getTeams).not.toHaveBeenCalled();
  });

  it('does not advertise POST in the Allow header for /api/teams', async () => {
    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'PUT',
    });

    await teamsHandler(req, res);

    expect(res.statusCode).toBe(405);
    expect(res.getHeader('Allow')).toBe('GET');
  });
});
