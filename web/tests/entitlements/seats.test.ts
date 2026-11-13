jest.mock('@/lib/logger', () => ({ logger: { warn: jest.fn() } }));

import { ApiError } from '@/lib/errors';
import { logger } from '@/lib/logger';
import {
  assertSeatAvailableInTx,
  SEAT_LIMIT_KEY,
  type SeatClaim,
} from '@/lib/entitlements/seats';

/**
 * The seat check (#157, contract §8) against a fake transaction that answers
 * the three reads it makes. Counting against real rows, concurrency and the
 * routes that call it run on PostgreSQL in
 * `__tests__/integration/seat-limit.spec.ts`.
 */
type Reads = {
  projection?: unknown;
  members?: number;
  pendingInvitations?: number;
};

const fakeTx = ({
  projection = {
    currentSnapshot: { resolvedEntitlements: { 'members.max': 5 } },
  },
  members = 0,
  pendingInvitations = 0,
}: Reads) => {
  const tx = {
    commercialAccessProjection: { findUnique: jest.fn(async () => projection) },
    teamMember: { count: jest.fn(async () => members) },
    invitation: { count: jest.fn(async () => pendingInvitations) },
  };
  return tx;
};

const withLimit = (value: unknown) => ({
  currentSnapshot: {
    resolvedEntitlements: { controlled_mock: true, [SEAT_LIMIT_KEY]: value },
  },
});

const TEAM = 'team-acme';
const NOW = new Date('2026-11-13T09:00:00Z');
const invite: SeatClaim = { kind: 'invite' };
const accept: SeatClaim = { kind: 'accept', invitationId: 'inv-1' };
const promote: SeatClaim = { kind: 'promote' };

const check = (reads: Reads, claim: SeatClaim) => {
  const tx = fakeTx(reads);
  return {
    tx,
    result: assertSeatAvailableInTx(tx as never, TEAM, claim, NOW),
  };
};

const refusal = async (promise: Promise<void>) => {
  const error = await promise.then(
    () => null,
    (caught: unknown) => caught
  );
  expect(error).toBeInstanceOf(ApiError);
  const apiError = error as ApiError;
  return {
    status: apiError.status,
    code: apiError.code,
    params: apiError.params,
  };
};

beforeEach(() => jest.clearAllMocks());

describe('the limit is members.max on the current snapshot, failing closed', () => {
  it.each([
    ['no projection', null],
    ['a projection with no current snapshot', { currentSnapshot: null }],
    [
      'a snapshot without members.max',
      { currentSnapshot: { resolvedEntitlements: { controlled_mock: true } } },
    ],
    [
      'a snapshot with no entitlements',
      { currentSnapshot: { resolvedEntitlements: null } },
    ],
  ])('refuses every claim with %s', async (_label, projection) => {
    for (const claim of [invite, accept, promote]) {
      const { tx, result } = check({ projection }, claim);
      expect(await refusal(result)).toEqual({
        status: 503,
        code: 'seat_limit_unavailable',
        params: undefined,
      });
      // Nothing is counted against a limit that could not be read.
      expect(tx.teamMember.count).not.toHaveBeenCalled();
    }
    expect(logger.warn).toHaveBeenCalledWith(
      'control_plane.seat_limit_unavailable',
      expect.objectContaining({ workspace_id: TEAM })
    );
  });

  it.each([
    ['a string', '5'],
    ['a fraction', 2.5],
    ['a negative number', -1],
    ['a boolean', true],
  ])('refuses a limit that is %s', async (_label, value) => {
    const { result } = check({ projection: withLimit(value) }, invite);
    expect(await refusal(result)).toMatchObject({
      status: 503,
      code: 'seat_limit_unavailable',
    });
  });

  it('reads only this Workspace', async () => {
    const { tx, result } = check({}, invite);
    await result;
    expect(tx.commercialAccessProjection.findUnique).toHaveBeenCalledWith({
      where: { teamId: TEAM },
      select: { currentSnapshot: { select: { resolvedEntitlements: true } } },
    });
    expect(tx.teamMember.count).toHaveBeenCalledWith({
      where: { teamId: TEAM },
    });
  });
});

describe('what holds a seat', () => {
  it('counts members and valid pending invitations only', async () => {
    const { tx, result } = check({}, invite);
    await result;
    // Consumed, revoked and expired invitations hold no seat.
    expect(tx.invitation.count).toHaveBeenCalledWith({
      where: {
        teamId: TEAM,
        consumedAt: null,
        revokedAt: null,
        expires: { gte: NOW },
      },
    });
  });

  it('counts the invitation being accepted once, whatever its expiry', async () => {
    const { tx, result } = check({}, accept);
    await result;
    expect(tx.invitation.count).toHaveBeenCalledWith({
      where: {
        teamId: TEAM,
        consumedAt: null,
        revokedAt: null,
        expires: { gte: NOW },
        id: { not: 'inv-1' },
      },
    });
  });
});

describe('a new invitation needs a free seat', () => {
  it('is allowed while one seat is free', async () => {
    await expect(
      check({ members: 2, pendingInvitations: 2 }, invite).result
    ).resolves.toBeUndefined();
  });

  it.each([
    ['exactly at the limit', 3, 2],
    ['over the limit after a downgrade', 6, 1],
  ])('is refused %s', async (_label, members, pendingInvitations) => {
    const { result } = check({ members, pendingInvitations }, invite);
    expect(await refusal(result)).toEqual({
      status: 409,
      code: 'seat_limit_reached',
      params: { limit: 5 },
    });
    expect(logger.warn).toHaveBeenCalledWith(
      'control_plane.seat_limit_refused',
      {
        workspace_id: TEAM,
        claim: 'invite',
        limit: 5,
        occupied: members + pendingInvitations,
      }
    );
  });

  it('is refused when the limit is zero', async () => {
    const { result } = check({ projection: withLimit(0) }, invite);
    expect(await refusal(result)).toEqual({
      status: 409,
      code: 'seat_limit_reached',
      params: { limit: 0 },
    });
  });
});

describe('acceptance and promotion are refused only over the limit', () => {
  // The fake's pending count already leaves the accepted invitation out.
  it('accepts when the Workspace, this invitation included, is at the limit', async () => {
    await expect(
      check({ members: 3, pendingInvitations: 1 }, accept).result
    ).resolves.toBeUndefined();
  });

  it('refuses acceptance when the Workspace is over the limit', async () => {
    const { result } = check({ members: 4, pendingInvitations: 1 }, accept);
    expect(await refusal(result)).toEqual({
      status: 409,
      code: 'seat_limit_exceeded',
      params: { limit: 5 },
    });
    expect(logger.warn).toHaveBeenCalledWith(
      'control_plane.seat_limit_refused',
      { workspace_id: TEAM, claim: 'accept', limit: 5, occupied: 6 }
    );
  });

  it('promotes when the Workspace is at the limit', async () => {
    await expect(
      check({ members: 4, pendingInvitations: 1 }, promote).result
    ).resolves.toBeUndefined();
  });

  it('refuses promotion when the Workspace is over the limit', async () => {
    const { result } = check(
      { projection: withLimit(3), members: 4, pendingInvitations: 0 },
      promote
    );
    expect(await refusal(result)).toEqual({
      status: 409,
      code: 'seat_limit_exceeded',
      params: { limit: 3 },
    });
  });
});
