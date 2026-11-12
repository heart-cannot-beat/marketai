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
import { countTeamMembers, updateTeamMember } from 'models/teamMember';

/**
 * #150: the durable membership audit event names whoever made the change, so
 * each handler must hand the signed-in caller to the model as the actor.
 */
const team = { id: 't1', slug: 'acme', lifecycle: 'ACTIVE' };

const caller = (role: 'OWNER' | 'MEMBER', id: string) => ({
  role,
  teamId: team.id,
  team,
  sessionId: 'sess_1',
  user: { id, email: 'caller@acmecharge.example' },
});

const call = async (method: 'PUT' | 'PATCH', body: Record<string, unknown>) => {
  const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
    method,
    query: { slug: team.slug },
    body,
  });
  await membersHandler(req, res);
  return res;
};

describe('membership handlers pass the caller as the audit actor', () => {
  beforeEach(() => {
    resetRateLimit();
    jest.clearAllMocks();
  });

  it('records a leave as LEFT by the member themself', async () => {
    (throwIfNoTeamAccess as jest.Mock).mockResolvedValue(
      caller('MEMBER', 'member_1')
    );
    (countTeamMembers as jest.Mock).mockResolvedValue(2);

    const res = await call('PUT', {});

    expect(res.statusCode).toBe(200);
    expect(removeTeamMember).toHaveBeenCalledWith(team.id, 'member_1', {
      actor: { kind: 'USER', id: 'member_1' },
      reason: 'LEFT',
    });
  });

  it('records a role change by the Owner who made it', async () => {
    (throwIfNoTeamAccess as jest.Mock).mockResolvedValue(
      caller('OWNER', 'owner_1')
    );
    (getTeamMember as jest.Mock).mockResolvedValue({
      role: 'MEMBER',
      userId: 'member_2',
    });
    (updateTeamMember as jest.Mock).mockResolvedValue({ role: 'OWNER' });

    const res = await call('PATCH', {
      memberId: 'member_2',
      role: 'OWNER',
      confirmation: 'PROMOTE TO OWNER',
    });

    expect(res.statusCode).toBe(200);
    expect(updateTeamMember).toHaveBeenCalledWith({
      teamId: team.id,
      userId: 'member_2',
      role: 'OWNER',
      actor: { kind: 'USER', id: 'owner_1' },
    });
  });
});
