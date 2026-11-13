import { createHash, randomUUID } from 'node:crypto';

import {
  bindBetaPlan,
  currentSeatLimit,
  TEST_SIGNING_KEY,
  TEST_SIGNING_KEY_ID,
} from '../helpers/workspace-entitlements';

export {};

/**
 * The Workspace seat limit (#157, contract §8) on PostgreSQL.
 *
 * Owners plus Members plus valid pending invitations may not exceed
 * `members.max` from the Workspace's current Entitlement Snapshot. A
 * downgrade below the current count keeps every member and refuses new
 * invitations, acceptance and promotion. Skips when no control-plane
 * connection string is configured; CI supplies one.
 *
 * Workspaces created here are not deleted afterwards: snapshots and audit
 * rows are immutable and reference their Workspace.
 */
const controlUrl = process.env.CONTROL_PLANE_DATABASE_URL;
const describeIfConfigured = controlUrl ? describe : describe.skip;

describeIfConfigured('Workspace seat limit (PostgreSQL)', () => {
  jest.setTimeout(45_000);

  /* eslint-disable @typescript-eslint/no-require-imports */
  let prisma: any;
  let createInvitation: any;
  let consumeInvitation: any;
  let acceptInvitationForNewUser: any;
  let deleteInvitation: any;
  let removeTeamMember: any;
  let updateTeamMember: any;
  let provisionFirstOwnerWorkspace: any;
  const createdUsers: string[] = [];

  beforeAll(() => {
    process.env.DATABASE_URL = controlUrl as string;

    ({ prisma } = require('../../lib/prisma'));
    ({
      acceptInvitationForNewUser,
      consumeInvitation,
      createInvitation,
      deleteInvitation,
    } = require('../../models/invitation'));
    ({ removeTeamMember } = require('../../models/team'));
    ({ updateTeamMember } = require('../../models/teamMember'));
    ({ provisionFirstOwnerWorkspace } = require('../../lib/onboarding'));
  });
  /* eslint-enable @typescript-eslint/no-require-imports */

  afterAll(async () => {
    if (!prisma) return;
    await prisma.user.deleteMany({ where: { id: { in: createdUsers } } });
    await prisma.$disconnect();
  });

  const address = (label: string) =>
    `${label}-${randomUUID().slice(0, 8)}@acmecharge.example`;

  const newUser = async (label: string) => {
    const user = await prisma.user.create({
      data: { name: label, email: address(label) },
    });
    createdUsers.push(user.id);
    return user;
  };

  /** An ACTIVE Workspace with its Owner, bound to a Beta plan. */
  const newWorkspace = async (seats?: number) => {
    const suffix = randomUUID().slice(0, 8);
    const owner = await newUser('owner');
    const team = await prisma.team.create({
      data: {
        name: `Acme Charge ${suffix}`,
        slug: `acme-seats-${suffix}`,
        lifecycle: 'ACTIVE',
      },
    });
    await seedMember(team.id, owner.id, 'OWNER');
    await bindBetaPlan(prisma, team.id, { grantedBy: owner.id, seats });
    return { team, owner };
  };

  // Fixture memberships are inserted directly; the seat check is not.
  const seedMember = (teamId: string, userId: string, role: string) =>
    prisma.teamMember.create({ data: { teamId, userId, role } });

  const invite = (teamId: string, invitedBy: string, email: string) =>
    createInvitation({ teamId, invitedBy, email, role: 'MEMBER' });

  const accept = (invitation: { token: string }, user: any) =>
    consumeInvitation({
      token: invitation.token,
      userId: user.id,
      email: user.email,
    });

  const promote = (teamId: string, userId: string, actorId: string) =>
    updateTeamMember({
      teamId,
      userId,
      role: 'OWNER',
      actor: { kind: 'USER', id: actorId },
    });

  /** The refusal's status and code, or null when it went through. */
  const outcome = (promise: Promise<unknown>) =>
    promise.then(
      () => null,
      (error: any) => ({ status: error.status, code: error.code })
    );

  const reached = { status: 409, code: 'seat_limit_reached' };
  const exceeded = { status: 409, code: 'seat_limit_exceeded' };

  const pendingInvitations = (teamId: string) =>
    prisma.invitation.count({
      where: {
        teamId,
        consumedAt: null,
        revokedAt: null,
        expires: { gte: new Date() },
      },
    });

  const occupied = async (teamId: string) =>
    (await prisma.teamMember.count({ where: { teamId } })) +
    (await pendingInvitations(teamId));

  const roster = (teamId: string) =>
    prisma.teamMember
      .findMany({ where: { teamId }, select: { userId: true, role: true } })
      .then((rows: Array<{ userId: string; role: string }>) =>
        rows.sort((a, b) => a.userId.localeCompare(b.userId))
      );

  it('lets members and pending invitations fill members.max and no further', async () => {
    const { team, owner } = await newWorkspace(3);
    const invitee = await newUser('invitee');

    const pending = await invite(team.id, owner.id, invitee.email);
    await invite(team.id, owner.id, address('second'));
    expect(await occupied(team.id)).toBe(3);

    expect(await outcome(invite(team.id, owner.id, address('third')))).toEqual(
      reached
    );

    // Accepting turns the invitation's seat into a membership.
    await expect(accept(pending, invitee)).resolves.toMatchObject({
      role: 'MEMBER',
    });
    expect(await occupied(team.id)).toBe(3);
    expect(await outcome(invite(team.id, owner.id, address('third')))).toEqual(
      reached
    );
  });

  it('never lets concurrent invitations take more than the free seats', async () => {
    const { team, owner } = await newWorkspace(4);
    await invite(team.id, owner.id, address('held'));
    // Owner + one pending invitation: two seats free, five competing for them.

    const attempts = await Promise.allSettled(
      Array.from({ length: 5 }, (_, n) =>
        invite(team.id, owner.id, address(`race-${n}`))
      )
    );

    const granted = attempts.filter((a) => a.status === 'fulfilled');
    const refused = attempts.filter(
      (a): a is PromiseRejectedResult => a.status === 'rejected'
    );
    expect(granted).toHaveLength(2);
    expect(refused.map((r) => r.reason.code)).toEqual([
      'seat_limit_reached',
      'seat_limit_reached',
      'seat_limit_reached',
    ]);
    expect(await occupied(team.id)).toBe(4);
  });

  it('frees the seat of a revoked or expired invitation', async () => {
    const { team, owner } = await newWorkspace(2);
    const first = await invite(team.id, owner.id, address('first'));
    expect(await outcome(invite(team.id, owner.id, address('next')))).toEqual(
      reached
    );

    await deleteInvitation({ id: first.id });
    const second = await invite(team.id, owner.id, address('second'));
    expect(await outcome(invite(team.id, owner.id, address('next')))).toEqual(
      reached
    );

    await prisma.invitation.update({
      where: { id: second.id },
      data: { expires: new Date(Date.now() - 60_000) },
    });
    await expect(
      invite(team.id, owner.id, address('third'))
    ).resolves.toMatchObject({ teamId: team.id });
    expect(await occupied(team.id)).toBe(2);
  });

  it('keeps every member after a downgrade and refuses only new seats and promotion', async () => {
    // Platform default: five seats.
    const { team, owner } = await newWorkspace();
    expect(await currentSeatLimit(prisma, team.id)).toBe(5);
    const coOwner = await newUser('co-owner');
    const member = await newUser('member');
    const existingUser = await newUser('existing');
    await seedMember(team.id, coOwner.id, 'OWNER');
    await seedMember(team.id, member.id, 'MEMBER');
    const forExisting = await invite(team.id, owner.id, existingUser.email);
    const newcomerEmail = address('newcomer');
    const forNewcomer = await invite(team.id, owner.id, newcomerEmail);
    expect(await occupied(team.id)).toBe(5);
    const before = await roster(team.id);

    await bindBetaPlan(prisma, team.id, { grantedBy: owner.id, seats: 3 });
    expect(await currentSeatLimit(prisma, team.id)).toBe(3);

    // Over the limit: no new invitation, acceptance or promotion.
    expect(await outcome(invite(team.id, owner.id, address('more')))).toEqual(
      reached
    );
    expect(await outcome(accept(forExisting, existingUser))).toEqual(exceeded);
    expect(
      await outcome(
        acceptInvitationForNewUser({
          token: forNewcomer.token,
          name: 'Newcomer',
          password: 'hashed-elsewhere',
          emailVerified: new Date(),
        })
      )
    ).toEqual(exceeded);
    expect(await outcome(promote(team.id, member.id, owner.id))).toEqual(
      exceeded
    );

    // Nothing was removed or changed, and no refused step left a trace.
    expect(await roster(team.id)).toEqual(before);
    expect(await prisma.user.count({ where: { email: newcomerEmail } })).toBe(
      0
    );
    expect(
      await prisma.invitation.findUnique({ where: { id: forExisting.id } })
    ).toMatchObject({ consumedAt: null, revokedAt: null });

    // Demotion, removal and revocation are never refused.
    await expect(
      updateTeamMember({
        teamId: team.id,
        userId: coOwner.id,
        role: 'MEMBER',
        actor: { kind: 'USER', id: owner.id },
      })
    ).resolves.toMatchObject({ role: 'MEMBER' });
    await removeTeamMember(team.id, member.id, {
      actor: { kind: 'USER', id: owner.id },
      reason: 'REMOVED',
    });
    expect(await occupied(team.id)).toBe(4);
    expect(await outcome(promote(team.id, coOwner.id, owner.id))).toEqual(
      exceeded
    );
    await deleteInvitation({ id: forExisting.id });
    expect(await occupied(team.id)).toBe(3);

    // Back at the limit: the held seat can be taken up, nothing more.
    const joined = await acceptInvitationForNewUser({
      token: forNewcomer.token,
      name: 'Newcomer',
      password: 'hashed-elsewhere',
      emailVerified: new Date(),
    });
    createdUsers.push(joined.user.id);
    expect(joined.membership).toMatchObject({ role: 'MEMBER' });
    await expect(promote(team.id, coOwner.id, owner.id)).resolves.toMatchObject(
      { role: 'OWNER' }
    );
    expect(await outcome(invite(team.id, owner.id, address('more')))).toEqual(
      reached
    );
    expect(await occupied(team.id)).toBe(3);
  });

  it("counts each Workspace's own seats only", async () => {
    const a = await newWorkspace(2);
    const b = await newWorkspace(4);
    const shared = await newUser('shared');
    await seedMember(a.team.id, shared.id, 'MEMBER');
    await seedMember(b.team.id, shared.id, 'MEMBER');
    await invite(b.team.id, b.owner.id, address('b-pending'));

    // A is full; B has one seat left, whatever A holds.
    expect(
      await outcome(invite(a.team.id, a.owner.id, address('a-more')))
    ).toEqual(reached);
    await expect(
      invite(b.team.id, b.owner.id, address('b-more'))
    ).resolves.toMatchObject({ teamId: b.team.id });
    expect(
      await outcome(invite(b.team.id, b.owner.id, address('b-more')))
    ).toEqual(reached);

    // A downgrade of A leaves B alone.
    await bindBetaPlan(prisma, a.team.id, { grantedBy: a.owner.id, seats: 1 });
    expect(await outcome(promote(a.team.id, shared.id, a.owner.id))).toEqual(
      exceeded
    );
    expect(await currentSeatLimit(prisma, b.team.id)).toBe(4);
    await expect(
      promote(b.team.id, shared.id, b.owner.id)
    ).resolves.toMatchObject({ role: 'OWNER' });
  });

  it('fails closed for a Workspace with no published snapshot', async () => {
    const suffix = randomUUID().slice(0, 8);
    const owner = await newUser('owner');
    const member = await newUser('member');
    const invitee = await newUser('invitee');
    const team = await prisma.team.create({
      data: {
        name: `Acme Charge ${suffix}`,
        slug: `acme-unbound-${suffix}`,
        lifecycle: 'ACTIVE',
      },
    });
    await seedMember(team.id, owner.id, 'OWNER');
    await seedMember(team.id, member.id, 'MEMBER');
    const pending = await prisma.invitation.create({
      data: {
        teamId: team.id,
        invitedBy: owner.id,
        email: invitee.email,
        role: 'MEMBER',
        token: randomUUID(),
        expires: new Date(Date.now() + 60 * 60 * 1000),
      },
    });

    const unavailable = { status: 503, code: 'seat_limit_unavailable' };
    expect(await outcome(invite(team.id, owner.id, address('more')))).toEqual(
      unavailable
    );
    expect(await outcome(accept(pending, invitee))).toEqual(unavailable);
    expect(await outcome(promote(team.id, member.id, owner.id))).toEqual(
      unavailable
    );
    await removeTeamMember(team.id, member.id, {
      actor: { kind: 'USER', id: owner.id },
      reason: 'REMOVED',
    });
    expect(await roster(team.id)).toEqual([
      { userId: owner.id, role: 'OWNER' },
    ]);
  });

  it('never refuses the first Owner for seats', async () => {
    const suffix = randomUUID().slice(0, 8);
    const operator = await newUser('operator');
    // A plan with no seats at all still admits its first Owner.
    const entitlements = { controlled_mock: true, 'members.max': 0 };
    const plan = await prisma.planVersion.create({
      data: {
        planKey: `beta-zero-${suffix}`,
        versionNo: 1,
        displayName: `Beta zero ${suffix}`,
        state: 'EFFECTIVE',
        entitlements,
        entitlementsHash: createHash('sha256')
          .update(JSON.stringify(entitlements))
          .digest('hex'),
        publishedBy: 'operator-plan',
      },
    });
    const provisioned = await provisionFirstOwnerWorkspace(prisma, {
      name: `Acme Charge ${suffix}`,
      slug: `first-owner-seats-${suffix}`,
      ownerEmail: address('first-owner'),
      operatorUserId: operator.id,
      betaPlanVersionId: plan.id,
      signingKeyId: TEST_SIGNING_KEY_ID,
      signingKey: TEST_SIGNING_KEY,
    });
    expect(await currentSeatLimit(prisma, provisioned.team.id)).toBe(0);

    const accepted = await acceptInvitationForNewUser({
      token: provisioned.invitation.token,
      name: 'First Owner',
      password: 'hashed-elsewhere',
      emailVerified: new Date(),
    });
    createdUsers.push(accepted.user.id);
    expect(accepted.membership).toMatchObject({
      teamId: provisioned.team.id,
      role: 'OWNER',
    });
  });
});
