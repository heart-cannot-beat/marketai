// This file has no static imports; `export {}` keeps it a module so its
// top-level names do not collide with the other integration suites.
export {};

/**
 * Invitation consumption is transactional and single-use.
 *
 * Runs against a real PostgreSQL instance because the guarantee is a database
 * one: a row lock plus conditional consumedAt update makes concurrent accepts
 * converge while retaining the original record as history. Skips when no
 * control-plane connection string is configured; CI supplies one.
 */
const controlUrl = process.env.CONTROL_PLANE_DATABASE_URL;
const configured = Boolean(controlUrl);

const describeIfConfigured = configured ? describe : describe.skip;

describeIfConfigured('Invitation single-use (PostgreSQL)', () => {
  jest.setTimeout(30_000);

  /* eslint-disable @typescript-eslint/no-require-imports */
  let prisma: any;
  let consumeInvitation: any;
  let createInvitation: any;
  let randomUUID: () => string;

  let workspaceId: string;
  let inviterId: string;
  const created: string[] = [];

  beforeAll(async () => {
    process.env.DATABASE_URL = controlUrl as string;

    ({ randomUUID } = require('crypto'));
    ({ prisma } = require('../../lib/prisma'));
    ({
      consumeInvitation,
      createInvitation,
    } = require('../../models/invitation'));

    const suffix = randomUUID().slice(0, 8);

    const inviter = await prisma.user.create({
      data: { name: 'Inviter', email: `inviter-${suffix}@example.test` },
    });
    inviterId = inviter.id;
    created.push(inviter.id);

    const workspace = await prisma.team.create({
      data: { name: `WS ${suffix}`, slug: `ws-${suffix}`, lifecycle: 'ACTIVE' },
    });
    workspaceId = workspace.id;
  });

  afterAll(async () => {
    if (!prisma) return;
    // The Workspace stays: each accepted invitation appended an immutable
    // `member.joined` audit row that references it (#150). Deleting the users
    // still cascades their memberships and invitations.
    await prisma.user.deleteMany({ where: { id: { in: created } } });
    await prisma.$disconnect();
  });

  beforeEach(async () => {
    if (!prisma || !workspaceId) return;
    await prisma.teamMember.deleteMany({ where: { teamId: workspaceId } });
    await prisma.invitation.deleteMany({ where: { teamId: workspaceId } });
    await prisma.team.update({
      where: { id: workspaceId },
      data: { lifecycle: 'ACTIVE' },
    });
  });

  const newInvitee = async () => {
    const suffix = randomUUID().slice(0, 8);
    const user = await prisma.user.create({
      data: { name: 'Invitee', email: `invitee-${suffix}@example.test` },
    });
    created.push(user.id);
    return user;
  };

  const newInvitation = async (email: string, expires?: Date) => {
    return prisma.invitation.create({
      data: {
        teamId: workspaceId,
        invitedBy: inviterId,
        email,
        role: 'MEMBER',
        token: randomUUID(),
        expires: expires ?? new Date(Date.now() + 60 * 60 * 1000),
      },
    });
  };

  it('admits exactly one account and then refuses the token', async () => {
    const first = await newInvitee();
    const second = await newInvitee();
    const invitation = await newInvitation(first.email);

    await expect(
      consumeInvitation({
        token: invitation.token,
        userId: first.id,
        email: first.email,
      })
    ).resolves.toMatchObject({ teamId: workspaceId, role: 'MEMBER' });

    // A second account cannot redeem the same token.
    await expect(
      consumeInvitation({
        token: invitation.token,
        userId: second.id,
        email: second.email,
      })
    ).rejects.toThrow(/email address|already been used|revoked/i);

    const members = await prisma.teamMember.findMany({
      where: { teamId: workspaceId },
    });

    expect(members.map((m: any) => m.userId)).toEqual([first.id]);

    await prisma.teamMember.deleteMany({ where: { teamId: workspaceId } });
  });

  it('returns one membership idempotently under concurrent acceptance', async () => {
    const invitee = await newInvitee();
    const invitation = await newInvitation(invitee.email);

    const attempts = await Promise.allSettled(
      Array.from({ length: 4 }, () =>
        consumeInvitation({
          token: invitation.token,
          userId: invitee.id,
          email: invitee.email,
        })
      )
    );

    expect(attempts.filter((a) => a.status === 'fulfilled')).toHaveLength(4);
    expect(
      new Set(
        attempts
          .filter(
            (a): a is PromiseFulfilledResult<any> => a.status === 'fulfilled'
          )
          .map((a) => a.value.id)
      ).size
    ).toBe(1);

    const members = await prisma.teamMember.count({
      where: { teamId: workspaceId },
    });

    expect(members).toBe(1);

    await prisma.teamMember.deleteMany({ where: { teamId: workspaceId } });
  });

  it('allows only one concurrent active invitation for an address', async () => {
    const invitee = await newInvitee();
    const attempts = await Promise.allSettled(
      Array.from({ length: 4 }, () =>
        createInvitation({
          teamId: workspaceId,
          invitedBy: inviterId,
          email: invitee.email,
          role: 'MEMBER',
        })
      )
    );

    expect(
      attempts.filter((attempt) => attempt.status === 'fulfilled')
    ).toHaveLength(1);
    expect(
      await prisma.invitation.count({
        where: {
          teamId: workspaceId,
          email: invitee.email,
          consumedAt: null,
          revokedAt: null,
        },
      })
    ).toBe(1);
  });

  it('retains consumed history and permits reinvite after member removal', async () => {
    const invitee = await newInvitee();
    const first = await createInvitation({
      teamId: workspaceId,
      invitedBy: inviterId,
      email: invitee.email,
      role: 'MEMBER',
    });
    const membership = await consumeInvitation({
      token: first.token,
      userId: invitee.id,
      email: invitee.email,
    });
    await prisma.teamMember.delete({ where: { id: membership.id } });

    const second = await createInvitation({
      teamId: workspaceId,
      invitedBy: inviterId,
      email: invitee.email,
      role: 'MEMBER',
    });
    expect(second.id).not.toBe(first.id);
    expect(second.token).not.toBe(first.token);
    await expect(
      consumeInvitation({
        token: first.token,
        userId: invitee.id,
        email: invitee.email,
      })
    ).rejects.toThrow(/inconsistent|already been used/i);
    await expect(
      consumeInvitation({
        token: second.token,
        userId: invitee.id,
        email: invitee.email,
      })
    ).resolves.toMatchObject({ userId: invitee.id });

    const history = await prisma.invitation.findMany({
      where: { teamId: workspaceId, email: invitee.email },
      orderBy: { createdAt: 'asc' },
    });
    expect(history).toHaveLength(2);
    expect(history[0]).toMatchObject({ id: first.id });
    expect(history[0].consumedAt).not.toBeNull();
  });

  it('does not burn the invitation when the wrong account tries to redeem it', async () => {
    const invited = await newInvitee();
    const stranger = await newInvitee();
    const invitation = await newInvitation(invited.email);

    await expect(
      consumeInvitation({
        token: invitation.token,
        userId: stranger.id,
        email: stranger.email,
      })
    ).rejects.toThrow(/email address this invitation was sent to/i);

    // Rolled back: the invitation is still redeemable by the right person.
    expect(
      await prisma.invitation.count({ where: { token: invitation.token } })
    ).toBe(1);
    expect(
      await prisma.teamMember.count({ where: { teamId: workspaceId } })
    ).toBe(0);

    await expect(
      consumeInvitation({
        token: invitation.token,
        userId: invited.id,
        email: invited.email,
      })
    ).resolves.toMatchObject({ userId: invited.id });

    await prisma.teamMember.deleteMany({ where: { teamId: workspaceId } });
  });

  it('refuses an expired invitation without consuming it', async () => {
    const invitee = await newInvitee();
    const invitation = await newInvitation(
      invitee.email,
      new Date(Date.now() - 1000)
    );

    await expect(
      consumeInvitation({
        token: invitation.token,
        userId: invitee.id,
        email: invitee.email,
      })
    ).rejects.toThrow(/expired/i);

    expect(
      await prisma.invitation.count({ where: { token: invitation.token } })
    ).toBe(1);
    expect(
      await prisma.teamMember.count({ where: { teamId: workspaceId } })
    ).toBe(0);

    await prisma.invitation.deleteMany({ where: { token: invitation.token } });
  });

  it('matches the invited address case-insensitively', async () => {
    const invitee = await newInvitee();
    const invitation = await newInvitation(invitee.email);

    await expect(
      consumeInvitation({
        token: invitation.token,
        userId: invitee.id,
        email: invitee.email.toUpperCase(),
      })
    ).resolves.toMatchObject({ userId: invitee.id });

    await prisma.teamMember.deleteMany({ where: { teamId: workspaceId } });
  });

  it.each(['SUSPENDED', 'DELETION_PENDING', 'DELETED'])(
    'fails %s without consuming the invitation',
    async (lifecycle) => {
      const invitee = await newInvitee();
      const invitation = await newInvitation(invitee.email);
      await prisma.team.update({
        where: { id: workspaceId },
        data: { lifecycle },
      });

      await expect(
        consumeInvitation({
          token: invitation.token,
          userId: invitee.id,
          email: invitee.email,
        })
      ).rejects.toThrow(/suspended|no longer accepts/i);
      await expect(
        prisma.invitation.findUnique({ where: { id: invitation.id } })
      ).resolves.toMatchObject({ consumedAt: null });
    }
  );
});

if (!configured) {
  describe('Invitation single-use (PostgreSQL)', () => {
    it('is skipped without CONTROL_PLANE_DATABASE_URL', () => {
      expect(configured).toBe(false);
    });
  });
}
