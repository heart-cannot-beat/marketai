// This file has no static imports; `export {}` keeps it a module so its
// top-level names do not collide with the other integration suites.
export {};

/**
 * Membership history is durable (#150).
 *
 * A join, a removal and a role change each append exactly one
 * `ControlPlaneAuditEvent` in the same PostgreSQL transaction as the change,
 * so a rolled-back change leaves no event and an event never outlives its
 * change. Skips when no control-plane connection string is configured; CI
 * supplies one.
 *
 * Workspaces created here are not deleted afterwards: audit rows are
 * immutable and reference their Workspace.
 */
const controlUrl = process.env.CONTROL_PLANE_DATABASE_URL;
const describeIfConfigured = controlUrl ? describe : describe.skip;

describeIfConfigured('Membership audit events (PostgreSQL)', () => {
  jest.setTimeout(30_000);

  /* eslint-disable @typescript-eslint/no-require-imports */
  let prisma: any;
  let randomUUID: () => string;
  let consumeInvitation: any;
  let createInvitation: any;
  let removeTeamMember: any;
  let removeTeamMemberInTx: any;
  let updateTeamMember: any;
  let updateTeamMemberRoleInTx: any;
  let bindBetaPlan: any;
  const createdUsers: string[] = [];

  beforeAll(() => {
    process.env.DATABASE_URL = controlUrl as string;

    ({ randomUUID } = require('crypto'));
    ({ prisma } = require('../../lib/prisma'));
    ({
      consumeInvitation,
      createInvitation,
    } = require('../../models/invitation'));
    ({ removeTeamMember, removeTeamMemberInTx } = require('../../models/team'));
    ({
      updateTeamMember,
      updateTeamMemberRoleInTx,
    } = require('../../models/teamMember'));
    ({ bindBetaPlan } = require('../helpers/workspace-entitlements'));
  });
  /* eslint-enable @typescript-eslint/no-require-imports */

  afterAll(async () => {
    if (!prisma) return;
    await prisma.user.deleteMany({ where: { id: { in: createdUsers } } });
    await prisma.$disconnect();
  });

  const newUser = async (label: string) => {
    const suffix = randomUUID().slice(0, 8);
    const user = await prisma.user.create({
      data: { name: label, email: `${label}-${suffix}@example.test` },
    });
    createdUsers.push(user.id);
    return user;
  };

  const newWorkspace = async () => {
    const suffix = randomUUID().slice(0, 8);
    const workspace = await prisma.team.create({
      data: {
        name: `Acme Charge ${suffix}`,
        slug: `acme-${suffix}`,
        lifecycle: 'ACTIVE',
      },
    });
    // Joins and promotions check the seat limit on its snapshot (#157).
    await bindBetaPlan(prisma, workspace.id, { grantedBy: 'test-operator' });
    return workspace;
  };

  // Fixture memberships are inserted directly and so record nothing.
  const seedMember = (teamId: string, userId: string, role: string) =>
    prisma.teamMember.create({ data: { teamId, userId, role } });

  const memberEvents = (teamId: string) =>
    prisma.controlPlaneAuditEvent.findMany({
      where: { teamId, eventType: { startsWith: 'member.' } },
      orderBy: { createdAt: 'asc' },
    });

  const join = async (teamId: string, inviterId: string, user: any) => {
    const invitation = await createInvitation({
      teamId,
      invitedBy: inviterId,
      email: user.email,
      role: 'MEMBER',
    });
    const membership = await consumeInvitation({
      token: invitation.token,
      userId: user.id,
      email: user.email,
    });
    return { invitation, membership };
  };

  it('records one event per join, role change and removal, ids and roles only', async () => {
    const workspace = await newWorkspace();
    const owner = await newUser('owner');
    const invitee = await newUser('invitee');
    await seedMember(workspace.id, owner.id, 'OWNER');
    const asOwner = { kind: 'USER', id: owner.id };

    const { invitation, membership } = await join(
      workspace.id,
      owner.id,
      invitee
    );
    await updateTeamMember({
      teamId: workspace.id,
      userId: invitee.id,
      role: 'OWNER',
      actor: asOwner,
    });
    // Setting the role the member already holds is not a change.
    await updateTeamMember({
      teamId: workspace.id,
      userId: invitee.id,
      role: 'OWNER',
      actor: asOwner,
    });
    await removeTeamMember(workspace.id, invitee.id, {
      actor: asOwner,
      reason: 'REMOVED',
    });

    const events = await memberEvents(workspace.id);
    expect(
      events.map((e: any) => ({
        eventType: e.eventType,
        actorKind: e.actorKind,
        actorId: e.actorId,
        executor: e.executor,
        scopeKind: e.scopeKind,
        resourceType: e.resourceType,
        resourceId: e.resourceId,
        details: e.details,
      }))
    ).toEqual([
      {
        eventType: 'member.joined',
        actorKind: 'USER',
        actorId: invitee.id,
        executor: 'control-plane',
        scopeKind: 'WORKSPACE',
        resourceType: 'TeamMember',
        resourceId: membership.id,
        details: {
          userId: invitee.id,
          role: 'MEMBER',
          invitationId: invitation.id,
        },
      },
      {
        eventType: 'member.role_changed',
        actorKind: 'USER',
        actorId: owner.id,
        executor: 'control-plane',
        scopeKind: 'WORKSPACE',
        resourceType: 'TeamMember',
        resourceId: membership.id,
        details: { userId: invitee.id, fromRole: 'MEMBER', toRole: 'OWNER' },
      },
      {
        eventType: 'member.removed',
        actorKind: 'USER',
        actorId: owner.id,
        executor: 'control-plane',
        scopeKind: 'WORKSPACE',
        resourceType: 'TeamMember',
        resourceId: membership.id,
        details: { userId: invitee.id, role: 'OWNER', reason: 'REMOVED' },
      },
    ]);
    expect(JSON.stringify(events)).not.toContain('@');
  });

  it('records reason LEFT with the member as actor when they leave', async () => {
    const workspace = await newWorkspace();
    const member = await newUser('leaver');
    const membership = await seedMember(workspace.id, member.id, 'MEMBER');

    await removeTeamMember(workspace.id, member.id, {
      actor: { kind: 'USER', id: member.id },
      reason: 'LEFT',
    });

    const events = await memberEvents(workspace.id);
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({
      eventType: 'member.removed',
      actorId: member.id,
      resourceId: membership.id,
      details: { userId: member.id, role: 'MEMBER', reason: 'LEFT' },
    });
  });

  it('records exactly one join under concurrent acceptance', async () => {
    const workspace = await newWorkspace();
    const owner = await newUser('owner');
    const invitee = await newUser('invitee');
    const invitation = await createInvitation({
      teamId: workspace.id,
      invitedBy: owner.id,
      email: invitee.email,
      role: 'MEMBER',
    });

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
    const events = await memberEvents(workspace.id);
    expect(events.map((e: any) => e.eventType)).toEqual(['member.joined']);
  });

  it('leaves no event when the transaction that made the change rolls back', async () => {
    const workspace = await newWorkspace();
    const owner = await newUser('owner');
    const member = await newUser('member');
    await seedMember(workspace.id, owner.id, 'OWNER');
    await seedMember(workspace.id, member.id, 'MEMBER');
    const asOwner = { kind: 'USER', id: owner.id };

    await expect(
      prisma.$transaction(async (tx: any) => {
        await removeTeamMemberInTx(tx, workspace.id, member.id, {
          actor: asOwner,
          reason: 'REMOVED',
        });
        throw new Error('abort after removal');
      })
    ).rejects.toThrow('abort after removal');

    await expect(
      prisma.$transaction(async (tx: any) => {
        await updateTeamMemberRoleInTx(tx, {
          teamId: workspace.id,
          userId: member.id,
          role: 'OWNER',
          actor: asOwner,
        });
        throw new Error('abort after role change');
      })
    ).rejects.toThrow('abort after role change');

    // A join that is refused part-way is rolled back the same way.
    const stranger = await newUser('stranger');
    const invitation = await createInvitation({
      teamId: workspace.id,
      invitedBy: owner.id,
      email: (await newUser('invited')).email,
      role: 'MEMBER',
    });
    await expect(
      consumeInvitation({
        token: invitation.token,
        userId: stranger.id,
        email: stranger.email,
      })
    ).rejects.toThrow(/email address this invitation was sent to/i);

    // Removing someone who is not a member fails and records nothing.
    await expect(
      removeTeamMember(workspace.id, stranger.id, {
        actor: asOwner,
        reason: 'REMOVED',
      })
    ).rejects.toThrow();

    expect(
      await prisma.teamMember.findUnique({
        where: {
          teamId_userId: { teamId: workspace.id, userId: member.id },
        },
      })
    ).toMatchObject({ role: 'MEMBER' });
    expect(await memberEvents(workspace.id)).toEqual([]);
  });

  it('writes nothing for Team B when the same user changes in Team A', async () => {
    const teamA = await newWorkspace();
    const teamB = await newWorkspace();
    const owner = await newUser('owner');
    const shared = await newUser('shared');
    const invitee = await newUser('invitee');
    for (const team of [teamA, teamB]) {
      await seedMember(team.id, owner.id, 'OWNER');
      await seedMember(team.id, shared.id, 'MEMBER');
    }
    const asOwner = { kind: 'USER', id: owner.id };
    // Team B's own snapshot publication already wrote one event.
    const teamBEvents = () =>
      prisma.controlPlaneAuditEvent.count({ where: { teamId: teamB.id } });
    const teamBEventsBefore = await teamBEvents();

    await join(teamA.id, owner.id, invitee);
    await updateTeamMember({
      teamId: teamA.id,
      userId: shared.id,
      role: 'OWNER',
      actor: asOwner,
    });
    await removeTeamMember(teamA.id, shared.id, {
      actor: asOwner,
      reason: 'REMOVED',
    });

    expect(await memberEvents(teamA.id)).toHaveLength(3);
    expect(await memberEvents(teamB.id)).toEqual([]);
    expect(await teamBEvents()).toBe(teamBEventsBefore);
    expect(
      await prisma.teamMember.findUnique({
        where: { teamId_userId: { teamId: teamB.id, userId: shared.id } },
      })
    ).toMatchObject({ role: 'MEMBER' });
  });
});
