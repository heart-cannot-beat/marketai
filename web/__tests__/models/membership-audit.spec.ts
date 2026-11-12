/**
 * Membership history is durable (#150): a join, a removal and a role change
 * each append exactly one `ControlPlaneAuditEvent` in the same transaction as
 * the change.
 *
 * The fake below stages every write and commits only when the transaction
 * callback resolves, which is the property these tests depend on. The same
 * guarantees run against PostgreSQL in
 * `__tests__/integration/member-audit.spec.ts`.
 */
type Role = 'OWNER' | 'MEMBER';
type Member = { id: string; teamId: string; userId: string; role: Role };
type Invitation = {
  id: string;
  teamId: string;
  email: string;
  role: Role;
  kind: 'MEMBER';
  token: string;
  expires: Date;
  consumedAt: Date | null;
  consumedByUserId: string | null;
  revokedAt: Date | null;
};
type Tables = {
  members: Member[];
  invitations: Invitation[];
  audit: Array<Record<string, any>>;
};

const notFound = () =>
  Object.assign(new Error('Record not found'), { code: 'P2025' });

const mockDb = {
  committed: { members: [], invitations: [], audit: [] } as Tables,
  failAuditWrite: false,
  async transaction<T>(fn: (tx: unknown) => Promise<T>): Promise<T> {
    const draft: Tables = structuredClone(mockDb.committed);
    const result = await fn(mockTxClient(draft));
    mockDb.committed = draft;
    return result;
  },
};

function mockTxClient(t: Tables) {
  const findMember = (key: { teamId: string; userId: string }) =>
    t.members.find((m) => m.teamId === key.teamId && m.userId === key.userId) ??
    null;

  return {
    $queryRaw: async () => [],
    teamMember: {
      findUnique: async ({ where }) => findMember(where.teamId_userId),
      findUniqueOrThrow: async ({ where }) => {
        const member = findMember(where.teamId_userId);
        if (!member) throw notFound();
        return { ...member };
      },
      create: async ({ data }) => {
        const member = { id: `mem-${data.teamId}-${data.userId}`, ...data };
        t.members.push(member);
        return { ...member };
      },
      update: async ({ where, data }) => {
        const member = t.members.find((m) => m.id === where.id);
        if (!member) throw notFound();
        Object.assign(member, data);
        return { ...member };
      },
      delete: async ({ where }) => {
        const member = findMember(where.teamId_userId);
        if (!member) throw notFound();
        t.members.splice(t.members.indexOf(member), 1);
        return { ...member };
      },
    },
    invitation: {
      findUnique: async ({ where }) => {
        const invitation = t.invitations.find((i) => i.token === where.token);
        return invitation
          ? {
              ...invitation,
              team: {
                id: invitation.teamId,
                slug: `ws-${invitation.teamId}`,
                lifecycle: 'ACTIVE',
              },
            }
          : null;
      },
      updateMany: async ({ where, data }) => {
        const invitation = t.invitations.find(
          (i) => i.id === where.id && i.consumedAt === null
        );
        if (!invitation) return { count: 0 };
        Object.assign(invitation, data);
        return { count: 1 };
      },
    },
    controlPlaneAuditEvent: {
      create: async ({ data }) => {
        if (mockDb.failAuditWrite) throw new Error('audit insert refused');
        t.audit.push(structuredClone(data));
        return data;
      },
    },
  };
}

// models/team pulls in the session stack; none of it runs here.
jest.mock('@/lib/requireSession', () => ({ requireSession: jest.fn() }));
jest.mock('@/lib/logger', () => ({ logger: { warn: jest.fn() } }));
jest.mock('@/lib/onboarding', () => ({
  reconcileProvisioningWorkspaceFromRuntime: jest.fn(),
}));
jest.mock('@/lib/prisma', () => ({
  prisma: {
    $transaction: (fn: (tx: unknown) => Promise<unknown>) =>
      mockDb.transaction(fn),
  },
}));

import { consumeInvitation } from 'models/invitation';
import { removeTeamMember } from 'models/team';
import { updateTeamMember } from 'models/teamMember';

const TEAM_A = 'team-a';
const TEAM_B = 'team-b';
const OWNER = 'user-owner';
const SHARED = 'user-shared';
const NEWCOMER = 'user-newcomer';
const NEWCOMER_EMAIL = 'newcomer@acmecharge.example';
const asOwner = { kind: 'USER', id: OWNER } as const;

const seed = () => {
  mockDb.failAuditWrite = false;
  mockDb.committed = {
    members: [
      { id: 'mem-a-owner', teamId: TEAM_A, userId: OWNER, role: 'OWNER' },
      { id: 'mem-a-shared', teamId: TEAM_A, userId: SHARED, role: 'MEMBER' },
      { id: 'mem-b-owner', teamId: TEAM_B, userId: OWNER, role: 'OWNER' },
      { id: 'mem-b-shared', teamId: TEAM_B, userId: SHARED, role: 'MEMBER' },
    ],
    invitations: [
      {
        id: 'inv-a-newcomer',
        teamId: TEAM_A,
        email: NEWCOMER_EMAIL,
        role: 'MEMBER',
        kind: 'MEMBER',
        token: 'token-a-newcomer',
        expires: new Date(Date.now() + 60 * 60 * 1000),
        consumedAt: null,
        consumedByUserId: null,
        revokedAt: null,
      },
    ],
    audit: [],
  };
};

const events = (teamId?: string) =>
  mockDb.committed.audit.filter((e) => !teamId || e.teamId === teamId);
const member = (teamId: string, userId: string) =>
  mockDb.committed.members.find(
    (m) => m.teamId === teamId && m.userId === userId
  );

const join = () =>
  consumeInvitation({
    token: 'token-a-newcomer',
    userId: NEWCOMER,
    email: NEWCOMER_EMAIL,
  });
const removeShared = () =>
  removeTeamMember(TEAM_A, SHARED, { actor: asOwner, reason: 'REMOVED' });
const promoteShared = () =>
  updateTeamMember({
    teamId: TEAM_A,
    userId: SHARED,
    role: 'OWNER',
    actor: asOwner,
  });

beforeEach(seed);

describe('each membership change appends exactly one audit event', () => {
  it('records member.joined when an invitation is accepted', async () => {
    const membership = await join();

    expect(events()).toEqual([
      {
        scopeKind: 'WORKSPACE',
        teamId: TEAM_A,
        actorKind: 'USER',
        actorId: NEWCOMER,
        executor: 'control-plane',
        eventType: 'member.joined',
        resourceType: 'TeamMember',
        resourceId: membership.id,
        details: {
          userId: NEWCOMER,
          role: 'MEMBER',
          invitationId: 'inv-a-newcomer',
        },
      },
    ]);
  });

  it('records member.removed with the removing member as actor', async () => {
    await removeShared();

    expect(member(TEAM_A, SHARED)).toBeUndefined();
    expect(events()).toEqual([
      expect.objectContaining({
        teamId: TEAM_A,
        actorKind: 'USER',
        actorId: OWNER,
        eventType: 'member.removed',
        resourceType: 'TeamMember',
        resourceId: 'mem-a-shared',
        details: { userId: SHARED, role: 'MEMBER', reason: 'REMOVED' },
      }),
    ]);
  });

  it('records member.removed with reason LEFT when a member leaves', async () => {
    await removeTeamMember(TEAM_A, SHARED, {
      actor: { kind: 'USER', id: SHARED },
      reason: 'LEFT',
    });

    expect(events()).toEqual([
      expect.objectContaining({
        actorId: SHARED,
        eventType: 'member.removed',
        details: { userId: SHARED, role: 'MEMBER', reason: 'LEFT' },
      }),
    ]);
  });

  it('records member.role_changed with both roles, and nothing for a no-op', async () => {
    const updated = await promoteShared();
    expect(updated.role).toBe('OWNER');
    // Setting the role the member already holds is not a change.
    await promoteShared();

    expect(events()).toEqual([
      expect.objectContaining({
        teamId: TEAM_A,
        actorId: OWNER,
        eventType: 'member.role_changed',
        resourceId: 'mem-a-shared',
        details: { userId: SHARED, fromRole: 'MEMBER', toRole: 'OWNER' },
      }),
    ]);
  });

  it('carries ids and roles only, never an email address', async () => {
    await join();
    await promoteShared();
    await removeShared();

    expect(events()).toHaveLength(3);
    expect(JSON.stringify(events())).not.toContain('@');
  });
});

describe('a rolled-back change leaves no event', () => {
  it.each([
    ['a join', join, () => member(TEAM_A, NEWCOMER)],
    ['a removal', removeShared, () => member(TEAM_A, SHARED)],
    ['a role change', promoteShared, () => member(TEAM_A, SHARED)],
  ])(
    'when the audit insert fails, %s is not applied either',
    async (_label, change, row) => {
      const before = structuredClone(row());
      mockDb.failAuditWrite = true;

      await expect(change()).rejects.toThrow('audit insert refused');

      expect(row()).toEqual(before);
      expect(events()).toEqual([]);
    }
  );

  it('removing someone who is not a member records nothing', async () => {
    await expect(
      removeTeamMember(TEAM_A, NEWCOMER, { actor: asOwner, reason: 'REMOVED' })
    ).rejects.toMatchObject({ code: 'P2025' });

    expect(events()).toEqual([]);
  });
});

describe('Workspace isolation', () => {
  it('changes in Team A write nothing for Team B', async () => {
    await join();
    await promoteShared();
    await removeShared();

    expect(events(TEAM_A)).toHaveLength(3);
    expect(events(TEAM_B)).toEqual([]);
    // The same user's Team B membership is untouched.
    expect(member(TEAM_B, SHARED)).toEqual({
      id: 'mem-b-shared',
      teamId: TEAM_B,
      userId: SHARED,
      role: 'MEMBER',
    });
  });
});
