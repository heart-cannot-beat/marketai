import { getWorkspacePageProps } from '@/lib/workspace-bootstrap';

const mockRequireSession = jest.fn();
const mockFindUniqueTeam = jest.fn();
const mockFindFirstMember = jest.fn();
const mockServerSideTranslations = jest.fn().mockResolvedValue({});

jest.mock('@/lib/requireSession', () => ({
  getAuthenticatedSession: (...args: unknown[]) => mockRequireSession(...args),
}));
// Two-step Prisma lookup (team by slug, then membership by teamId+userId)
// rather than `models/team#getTeamMember`: that helper's `findFirstOrThrow`
// cannot distinguish "workspace doesn't exist" from "user isn't a member" —
// both throw — which would make the "no membership" 403 branch below
// unreachable in production. See task-1-review.md finding #1.
jest.mock('@/lib/prisma', () => ({
  prisma: {
    team: { findUnique: (...args: unknown[]) => mockFindUniqueTeam(...args) },
    teamMember: {
      findFirst: (...args: unknown[]) => mockFindFirstMember(...args),
    },
  },
}));
jest.mock('next-i18next/serverSideTranslations', () => ({
  serverSideTranslations: (...args: unknown[]) =>
    mockServerSideTranslations(...args),
}));

function mockContext(slug: string, locale = 'en') {
  return {
    params: { slug },
    locale,
    req: {} as any,
    res: {
      statusCode: 200,
      setHeader: jest.fn(),
    } as any,
    resolvedUrl: `/w/${slug}/generate`,
    query: { slug },
  };
}

describe('getWorkspacePageProps', () => {
  beforeEach(() => jest.clearAllMocks());

  it('redirects to login when no session', async () => {
    mockRequireSession.mockResolvedValue(null);
    const gssp = getWorkspacePageProps();
    const result = await gssp(mockContext('acme'));
    expect(result).toEqual({
      redirect: {
        destination: expect.stringContaining('/auth/login'),
        permanent: false,
      },
    });
  });

  it('returns 404 when workspace not found', async () => {
    mockRequireSession.mockResolvedValue({ user: { id: 'u1' } });
    mockFindUniqueTeam.mockResolvedValue(null);
    const gssp = getWorkspacePageProps();
    const result = await gssp(mockContext('nonexistent'));
    expect(result).toEqual({ notFound: true });
    expect(mockFindFirstMember).not.toHaveBeenCalled();
  });

  it('returns 403 props when user has no membership on an existing workspace', async () => {
    mockRequireSession.mockResolvedValue({ user: { id: 'u1' } });
    mockFindUniqueTeam.mockResolvedValue({
      id: 't1',
      slug: 'acme',
      name: 'Acme',
    });
    mockFindFirstMember.mockResolvedValue(null);
    const gssp = getWorkspacePageProps();
    const result = await gssp(mockContext('acme'));
    expect((result as any).props.errorCode).toBe(403);
    expect((result as any).props.errorContext).toBe('no-membership');
  });

  it('returns 403 when Member accesses ownerOnly page', async () => {
    mockRequireSession.mockResolvedValue({ user: { id: 'u1' } });
    mockFindUniqueTeam.mockResolvedValue({
      id: 't1',
      slug: 'acme',
      name: 'Acme',
    });
    mockFindFirstMember.mockResolvedValue({
      id: 'm1',
      teamId: 't1',
      userId: 'u1',
      role: 'MEMBER',
    });
    const gssp = getWorkspacePageProps({ ownerOnly: true });
    const result = await gssp(mockContext('acme'));
    expect((result as any).props.errorCode).toBe(403);
  });

  it('returns bootstrap for valid Owner', async () => {
    mockRequireSession.mockResolvedValue({ user: { id: 'u1' } });
    mockFindUniqueTeam.mockResolvedValue({
      id: 't1',
      slug: 'acme',
      name: 'Acme',
    });
    mockFindFirstMember.mockResolvedValue({
      id: 'm1',
      teamId: 't1',
      userId: 'u1',
      role: 'OWNER',
    });
    const gssp = getWorkspacePageProps();
    const result = await gssp(mockContext('acme'));
    expect((result as any).props.bootstrap).toEqual({
      workspaceId: 't1',
      slug: 'acme',
      displayName: 'Acme',
      membershipId: 'm1',
      role: 'OWNER',
    });
  });

  it('returns bootstrap for valid Member on non-ownerOnly page', async () => {
    mockRequireSession.mockResolvedValue({ user: { id: 'u1' } });
    mockFindUniqueTeam.mockResolvedValue({
      id: 't1',
      slug: 'acme',
      name: 'Acme',
    });
    mockFindFirstMember.mockResolvedValue({
      id: 'm2',
      teamId: 't1',
      userId: 'u1',
      role: 'MEMBER',
    });
    const gssp = getWorkspacePageProps();
    const result = await gssp(mockContext('acme'));
    expect((result as any).props.bootstrap).toEqual({
      workspaceId: 't1',
      slug: 'acme',
      displayName: 'Acme',
      membershipId: 'm2',
      role: 'MEMBER',
    });
  });

  it('propagates an unexpected DB error instead of swallowing it into a 404', async () => {
    mockRequireSession.mockResolvedValue({ user: { id: 'u1' } });
    mockFindUniqueTeam.mockRejectedValue(new Error('connection refused'));
    const gssp = getWorkspacePageProps();
    await expect(gssp(mockContext('acme'))).rejects.toThrow(
      'connection refused'
    );
  });
});
