import { prismaMock } from '../__mocks__/prisma';
import {
  incrementAuthorizationEpoch,
  incrementEntitlementVersion,
  toggleSuspension,
} from '../../lib/delegated-identity/epoch';

describe('incrementAuthorizationEpoch', () => {
  it('increments authorizationEpoch and projectionVersion atomically', async () => {
    const teamId = 'team_abc';
    prismaMock.$transaction.mockImplementation(
      async (ops: Promise<unknown>[]) => {
        for (const op of ops) await op;
      }
    );
    prismaMock.team.update.mockResolvedValue({
      id: teamId,
      authorizationEpoch: 1,
      projectionVersion: 1,
    });

    await incrementAuthorizationEpoch(prismaMock as any, teamId);

    expect(prismaMock.team.update).toHaveBeenCalledWith({
      where: { id: teamId },
      data: {
        authorizationEpoch: { increment: 1 },
        projectionVersion: { increment: 1 },
      },
    });
  });
});

describe('incrementEntitlementVersion', () => {
  it('increments entitlementVersion and projectionVersion', async () => {
    const teamId = 'team_xyz';
    prismaMock.team.update.mockResolvedValue({ id: teamId });

    await incrementEntitlementVersion(prismaMock as any, teamId);

    expect(prismaMock.team.update).toHaveBeenCalledWith({
      where: { id: teamId },
      data: {
        entitlementVersion: { increment: 1 },
        projectionVersion: { increment: 1 },
      },
    });
  });
});

describe('toggleSuspension', () => {
  it('sets suspended and increments authorizationEpoch + projectionVersion', async () => {
    const teamId = 'team_sus';
    prismaMock.team.update.mockResolvedValue({ id: teamId });

    await toggleSuspension(prismaMock as any, teamId, true);

    expect(prismaMock.team.update).toHaveBeenCalledWith({
      where: { id: teamId },
      data: {
        suspended: true,
        authorizationEpoch: { increment: 1 },
        projectionVersion: { increment: 1 },
      },
    });
  });
});
