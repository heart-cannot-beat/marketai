import type { PublicationResult } from '@/lib/entitlements/publication';

jest.mock('@/lib/env', () => ({
  __esModule: true,
  default: { appUrl: 'http://localhost:4002' },
}));
jest.mock('@/lib/entitlements/publication', () => ({
  publishSnapshotInTx: jest.fn<Promise<PublicationResult>, any[]>(),
}));
jest.mock('@/lib/entitlements/immediate-dispatch', () => ({
  dispatchWorkspaceOutboxNow: jest.fn(),
}));

import { dispatchWorkspaceOutboxNow } from '@/lib/entitlements/immediate-dispatch';
import { publishSnapshotInTx } from '@/lib/entitlements/publication';
import {
  activateWorkspaceAndPublish,
  reconcileProvisioningWorkspace,
} from '@/lib/onboarding';

const dispatchNow = dispatchWorkspaceOutboxNow as jest.Mock;
const SIGNING = { signingKeyId: 'beta-v1', signingKey: Buffer.from('k') };

/** Records when the transaction commits relative to the dispatch call. */
function makeDb(tx: Record<string, unknown>, events: string[]) {
  return {
    $transaction: jest.fn(async (fn: (tx: any) => Promise<unknown>) => {
      const result = await fn(tx);
      events.push('commit');
      return result;
    }),
  } as any;
}

function provisioningTx(overrides: Record<string, unknown> = {}) {
  return {
    $queryRaw: jest.fn().mockResolvedValue([{ id: 'ws_1' }]),
    team: {
      findUniqueOrThrow: jest
        .fn()
        .mockResolvedValue({ id: 'ws_1', lifecycle: 'PROVISIONING' }),
      update: jest.fn().mockResolvedValue({ id: 'ws_1', lifecycle: 'ACTIVE' }),
    },
    teamMember: {
      findFirst: jest.fn().mockResolvedValue({
        id: 'mem_1',
        user: { mfaCredential: { verifiedAt: new Date() } },
      }),
    },
    commercialAccessProjection: {
      findUnique: jest.fn().mockResolvedValue({
        currentSnapshot: { outboxEntry: { deliveredAt: new Date() } },
      }),
    },
    controlPlaneAuditEvent: { create: jest.fn().mockResolvedValue({}) },
    controlPlaneOutboxEvent: { create: jest.fn().mockResolvedValue({}) },
    ...overrides,
  };
}

beforeEach(() => {
  jest.clearAllMocks();
  (publishSnapshotInTx as jest.Mock).mockResolvedValue({
    snapshotId: 'es_1',
    entitlementVersion: 2,
  });
});

describe('activateWorkspaceAndPublish', () => {
  it('delivers the Workspace outbox after the activation commits', async () => {
    const events: string[] = [];
    dispatchNow.mockImplementation(async () => {
      events.push('dispatch');
      return { outcome: 'dispatched' };
    });
    const db = makeDb(provisioningTx(), events);

    const result = await activateWorkspaceAndPublish(db, {
      teamId: 'ws_1',
      ...SIGNING,
    });

    expect(result).toEqual({
      team: { id: 'ws_1', lifecycle: 'ACTIVE' },
      activeSnapshotVersion: 2,
    });
    expect(dispatchNow).toHaveBeenCalledTimes(1);
    expect(dispatchNow).toHaveBeenCalledWith(db, 'ws_1');
    expect(events).toEqual(['commit', 'dispatch']);
  });

  it('still activates when the immediate delivery does not deliver', async () => {
    dispatchNow.mockResolvedValue({ outcome: 'timed_out' });
    const db = makeDb(provisioningTx(), []);

    await expect(
      activateWorkspaceAndPublish(db, { teamId: 'ws_1', ...SIGNING })
    ).resolves.toMatchObject({ team: { lifecycle: 'ACTIVE' } });
  });

  it('does not dispatch when the activation is rejected', async () => {
    const tx = provisioningTx();
    (tx.team.findUniqueOrThrow as jest.Mock).mockResolvedValue({
      id: 'ws_1',
      lifecycle: 'ACTIVE',
    });

    await expect(
      activateWorkspaceAndPublish(makeDb(tx, []), {
        teamId: 'ws_1',
        ...SIGNING,
      })
    ).rejects.toThrow('Workspace is already active.');
    expect(dispatchNow).not.toHaveBeenCalled();
  });
});

describe('reconcileProvisioningWorkspace', () => {
  it('delivers the Workspace outbox after it activates', async () => {
    const events: string[] = [];
    dispatchNow.mockImplementation(async () => {
      events.push('dispatch');
      return { outcome: 'dispatched' };
    });
    const db = makeDb(provisioningTx(), events);

    await expect(
      reconcileProvisioningWorkspace(db, { teamId: 'ws_1', ...SIGNING })
    ).resolves.toBe('activated');
    expect(dispatchNow).toHaveBeenCalledWith(db, 'ws_1');
    expect(events).toEqual(['commit', 'dispatch']);
  });

  it.each([
    [
      'waiting',
      {
        commercialAccessProjection: {
          findUnique: jest.fn().mockResolvedValue({
            currentSnapshot: { outboxEntry: { deliveredAt: null } },
          }),
        },
      },
    ],
    [
      'already_active',
      {
        team: {
          findUniqueOrThrow: jest
            .fn()
            .mockResolvedValue({ id: 'ws_1', lifecycle: 'ACTIVE' }),
        },
      },
    ],
  ])('does not dispatch when the outcome is %s', async (expected, override) => {
    const db = makeDb(provisioningTx(override), []);

    await expect(
      reconcileProvisioningWorkspace(db, { teamId: 'ws_1', ...SIGNING })
    ).resolves.toBe(expected);
    expect(dispatchNow).not.toHaveBeenCalled();
  });
});
