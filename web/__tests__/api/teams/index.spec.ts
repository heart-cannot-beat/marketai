import { createMocks } from 'node-mocks-http';
import type { NextApiRequest, NextApiResponse } from 'next';

import teamsHandler from '../../../pages/api/teams';
import { resetRateLimit } from '@/lib/rateLimit';

jest.mock('models/user', () => ({
  getCurrentUser: jest.fn(),
}));

jest.mock('models/team', () => ({
  getTeams: jest.fn(),
}));

import { getCurrentUser } from 'models/user';
import { getTeams } from 'models/team';

/**
 * Regression guard for task-1-review.md finding #2: `GET /api/teams` maps
 * each Prisma row's `members[0].role` onto a top-level `role` field and
 * drops the raw `members` array from the wire payload. Stable ordering
 * itself is asserted against the `getTeams`/Prisma call shape in
 * `__tests__/models/team.spec.ts`; this test only covers the handler's
 * response-mapping contract, with `getTeams` mocked to return rows in a
 * fixed order to confirm the handler doesn't reorder them.
 */
describe('GET /api/teams response shape', () => {
  beforeEach(() => {
    resetRateLimit();
    jest.clearAllMocks();
    (getCurrentUser as jest.Mock).mockResolvedValue({ id: 'u1' });
  });

  it('returns a role field per team, derived from the caller membership row', async () => {
    (getTeams as jest.Mock).mockResolvedValue([
      {
        id: 't1',
        slug: 'acme',
        name: 'Acme',
        createdAt: '2024-01-01T00:00:00.000Z',
        _count: { members: 3 },
        members: [{ role: 'OWNER' }],
      },
      {
        id: 't2',
        slug: 'beta',
        name: 'Beta',
        createdAt: '2024-01-02T00:00:00.000Z',
        _count: { members: 1 },
        members: [{ role: 'MEMBER' }],
      },
    ]);

    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'GET',
    });

    await teamsHandler(req, res);

    expect(res.statusCode).toBe(200);
    const { data } = res._getJSONData();

    expect(data.map((t: any) => t.role)).toEqual(['OWNER', 'MEMBER']);
    // Preserves whatever order `getTeams` (createdAt ASC, id ASC) returned —
    // the handler must not reorder or drop rows.
    expect(data.map((t: any) => t.id)).toEqual(['t1', 't2']);
  });

  it('does not leak the raw members array onto the wire', async () => {
    (getTeams as jest.Mock).mockResolvedValue([
      {
        id: 't1',
        slug: 'acme',
        name: 'Acme',
        _count: { members: 1 },
        members: [{ role: 'OWNER' }],
      },
    ]);

    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'GET',
    });

    await teamsHandler(req, res);

    const { data } = res._getJSONData();
    expect(data[0].members).toBeUndefined();
    expect('members' in data[0]).toBe(false);
  });

  it('defaults to MEMBER when the caller has no membership row (defensive; should not occur)', async () => {
    (getTeams as jest.Mock).mockResolvedValue([
      {
        id: 't1',
        slug: 'acme',
        name: 'Acme',
        _count: { members: 1 },
        members: [],
      },
    ]);

    const { req, res } = createMocks<NextApiRequest, NextApiResponse>({
      method: 'GET',
    });

    await teamsHandler(req, res);

    const { data } = res._getJSONData();
    expect(data[0].role).toBe('MEMBER');
  });
});
