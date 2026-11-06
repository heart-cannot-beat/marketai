import { createMocks } from 'node-mocks-http';
import type { NextApiRequest, NextApiResponse } from 'next';

import membersHandler from '../../../pages/api/teams/[slug]/members';
import { resetRateLimit } from '@/lib/rateLimit';

jest.mock('@/lib/prisma', () => ({ prisma: {} }));

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

jest.mock('@/lib/audit', () => ({ sendAudit: jest.fn() }));
jest.mock('@/lib/metrics', () => ({ recordMetric: jest.fn() }));

import {
  getTeamMember,
  removeTeamMember,
  throwIfNoTeamAccess,
} from 'models/team';
import { updateTeamMember } from 'models/teamMember';

/**
 * ADR 0015 (#378): MFA is checked only at login. Owner membership changes
 * keep their typed confirmation phrase but no longer ask for an MFA check
 * from the last ten minutes, so an Owner whose session carries no MFA time
 * at all can still make them.
 */
const team = { id: 't1', slug: 'acme', lifecycle: 'ACTIVE' };

function caller(role: 'OWNER' | 'MEMBER') {
  return {
    role,
    teamId: team.id,
    team,
    sessionId: 'sess_1',
    user: { id: `${role.toLowerCase()}_1`, email: 'caller@example.com' },
  };
}

async function patchRole(body: Record<string, unknown>) {
  const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
    method: 'PATCH',
    query: { slug: team.slug },
    body,
  });
  await membersHandler(req, res);
  return res;
}

async function removeMember(query: Record<string, string>) {
  const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
    method: 'DELETE',
    query: { slug: team.slug, ...query },
  });
  await membersHandler(req, res);
  return res;
}

describe('Owner membership changes check role and the confirmation phrase only', () => {
  beforeEach(() => {
    resetRateLimit();
    jest.clearAllMocks();
    (getTeamMember as jest.Mock).mockResolvedValue({
      role: 'MEMBER',
      userId: 'member_2',
    });
    (updateTeamMember as jest.Mock).mockResolvedValue({ role: 'OWNER' });
    (removeTeamMember as jest.Mock).mockResolvedValue(undefined);
  });

  it('lets an Owner with no MFA time on the session promote with the phrase', async () => {
    (throwIfNoTeamAccess as jest.Mock).mockResolvedValue(caller('OWNER'));

    const res = await patchRole({
      memberId: 'member_2',
      role: 'OWNER',
      confirmation: 'PROMOTE TO OWNER',
    });

    expect(res.statusCode).toBe(200);
    expect(updateTeamMember).toHaveBeenCalledTimes(1);
    expect(JSON.stringify(res._getJSONData())).not.toMatch(/MFA/);
  });

  it('still refuses a promotion without the phrase', async () => {
    (throwIfNoTeamAccess as jest.Mock).mockResolvedValue(caller('OWNER'));

    const res = await patchRole({ memberId: 'member_2', role: 'OWNER' });

    expect(res.statusCode).toBe(400);
    expect(res._getJSONData().error.message).toBe(
      'Type PROMOTE TO OWNER to confirm this action.'
    );
    expect(updateTeamMember).not.toHaveBeenCalled();
  });

  it('still refuses a Member, phrase or not', async () => {
    (throwIfNoTeamAccess as jest.Mock).mockResolvedValue(caller('MEMBER'));

    const res = await patchRole({
      memberId: 'member_2',
      role: 'OWNER',
      confirmation: 'PROMOTE TO OWNER',
    });

    expect(res.statusCode).toBe(403);
    expect(updateTeamMember).not.toHaveBeenCalled();
  });

  it('lets an Owner with no MFA time remove another Owner with the phrase', async () => {
    (throwIfNoTeamAccess as jest.Mock).mockResolvedValue(caller('OWNER'));
    (getTeamMember as jest.Mock).mockResolvedValue({
      role: 'OWNER',
      userId: 'owner_2',
    });

    const refused = await removeMember({ memberId: 'owner_2' });
    expect(refused.statusCode).toBe(400);
    expect(removeTeamMember).not.toHaveBeenCalled();

    const res = await removeMember({
      memberId: 'owner_2',
      confirmation: 'REMOVE OWNER',
    });
    expect(res.statusCode).toBe(200);
    expect(removeTeamMember).toHaveBeenCalledWith(team.id, 'owner_2');
  });

  it('changes nothing for a caller outside the Workspace', async () => {
    (throwIfNoTeamAccess as jest.Mock).mockRejectedValue(
      new Error('You do not have access to this Workspace')
    );

    const res = await patchRole({
      memberId: 'member_2',
      role: 'OWNER',
      confirmation: 'PROMOTE TO OWNER',
    });

    expect(res.statusCode).not.toBe(200);
    expect(updateTeamMember).not.toHaveBeenCalled();
  });
});
