const mockFindMany = jest.fn();

jest.mock('@/lib/prisma', () => ({
  prisma: {
    team: { findMany: (...args: unknown[]) => mockFindMany(...args) },
  },
}));

import { getTeams } from 'models/team';

/**
 * Regression guard for task-1-review.md finding #2: `getTeams` is the only
 * source of truth for the "stable order, caller's role included" contract
 * that `GET /api/teams` (tested separately in
 * `__tests__/api/teams/index.spec.ts`) depends on. The API handler just maps
 * whatever `getTeams` returns, so the ordering/selection guarantee has to be
 * asserted here, against the actual Prisma call shape.
 */
describe('getTeams', () => {
  beforeEach(() => jest.clearAllMocks());

  it('orders by createdAt then id, and selects only the caller membership row', async () => {
    mockFindMany.mockResolvedValue([]);

    await getTeams('u1');

    expect(mockFindMany).toHaveBeenCalledWith({
      where: { members: { some: { userId: 'u1' } } },
      include: {
        _count: { select: { members: true } },
        members: { where: { userId: 'u1' }, select: { role: true }, take: 1 },
      },
      orderBy: [{ createdAt: 'asc' }, { id: 'asc' }],
    });
  });

  it('returns whatever stable-ordered rows Prisma resolves, unmodified', async () => {
    const rows = [
      { id: 't1', createdAt: '2024-01-01', members: [{ role: 'OWNER' }] },
      { id: 't2', createdAt: '2024-01-02', members: [{ role: 'MEMBER' }] },
    ];
    mockFindMany.mockResolvedValue(rows);

    const result = await getTeams('u1');

    expect(result).toBe(rows);
  });
});
