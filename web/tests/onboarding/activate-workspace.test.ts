import type { PublicationResult } from '@/lib/entitlements/publication';

jest.mock('@/lib/env', () => ({
  __esModule: true,
  default: { appUrl: 'http://localhost:4002' },
}));
jest.mock('@/lib/entitlements/publication', () => ({
  publishSnapshotInTx: jest.fn<Promise<PublicationResult>, any[]>(),
}));

import { activateWorkspaceAndPublish } from '@/lib/onboarding';
import { publishSnapshotInTx } from '@/lib/entitlements/publication';

const MOCK_SNAPSHOT_RESULT: PublicationResult = {
  snapshotId: 'es_activation_1',
  entitlementVersion: 2,
  leaseSequence: 2,
  projectionVersion: 2,
  semanticHash: 'hash_active',
  semanticVersionChanged: true,
  envelope: {
    protected_header: {
      schema_version: 2 as const,
      signature_algorithm: 'hmac-sha256',
      signature_key_id: 'beta-v1',
    },
    payload: {} as any,
    signature: 'sig',
  },
};

function makeMockTx() {
  return {
    $queryRaw: jest.fn().mockResolvedValue([{ id: 'ws_test' }]),
    team: {
      findUniqueOrThrow: jest.fn().mockResolvedValue({
        id: 'ws_test',
        lifecycle: 'PROVISIONING',
      }),
      update: jest.fn().mockResolvedValue({
        id: 'ws_test',
        lifecycle: 'ACTIVE',
      }),
    },
    controlPlaneAuditEvent: {
      create: jest.fn().mockResolvedValue({ id: 'evt_1' }),
    },
    controlPlaneOutboxEvent: {
      create: jest.fn().mockResolvedValue({ id: 'outbox_1' }),
    },
  };
}

function makeMockDb(tx: ReturnType<typeof makeMockTx>) {
  return {
    $transaction: jest.fn((fn: (tx: any) => Promise<any>) => fn(tx)),
  } as any;
}

const SIGNING_PARAMS = {
  teamId: 'ws_test',
  signingKeyId: 'beta-v1',
  signingKey: Buffer.from('test-key'),
};

describe('activateWorkspaceAndPublish', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    (publishSnapshotInTx as jest.Mock).mockResolvedValue(MOCK_SNAPSHOT_RESULT);
  });

  it('transitions Team to ACTIVE and publishes a snapshot atomically', async () => {
    const tx = makeMockTx();
    const db = makeMockDb(tx);

    const result = await activateWorkspaceAndPublish(db, SIGNING_PARAMS);

    expect(result.team.lifecycle).toBe('ACTIVE');
    expect(result.activeSnapshotVersion).toBe(2);

    expect(tx.team.update).toHaveBeenCalledWith({
      where: { id: 'ws_test' },
      data: { lifecycle: 'ACTIVE' },
    });
    expect(publishSnapshotInTx).toHaveBeenCalledWith(
      tx,
      'ws_test',
      'beta-v1',
      SIGNING_PARAMS.signingKey
    );
  });

  it('creates an audit event with the snapshot details', async () => {
    const tx = makeMockTx();
    const db = makeMockDb(tx);

    await activateWorkspaceAndPublish(db, SIGNING_PARAMS);

    expect(tx.controlPlaneAuditEvent.create).toHaveBeenCalledTimes(1);
    const auditData = tx.controlPlaneAuditEvent.create.mock.calls[0][0].data;
    expect(auditData.eventType).toBe('workspace.activated');
    expect(auditData.teamId).toBe('ws_test');
    expect(auditData.details.snapshotId).toBe('es_activation_1');
    expect(auditData.details.entitlementVersion).toBe(2);
  });

  it('enqueues an outbox event for downstream consumers', async () => {
    const tx = makeMockTx();
    const db = makeMockDb(tx);

    await activateWorkspaceAndPublish(db, SIGNING_PARAMS);

    expect(tx.controlPlaneOutboxEvent.create).toHaveBeenCalledTimes(1);
    const outboxData = tx.controlPlaneOutboxEvent.create.mock.calls[0][0].data;
    expect(outboxData.eventType).toBe('workspace.activated');
    expect(outboxData.payload.workspaceId).toBe('ws_test');
  });

  it('uses Serializable isolation', async () => {
    const tx = makeMockTx();
    const db = makeMockDb(tx);

    await activateWorkspaceAndPublish(db, SIGNING_PARAMS);

    const txOpts = db.$transaction.mock.calls[0][1];
    expect(txOpts.isolationLevel).toBe('Serializable');
  });

  it('rejects activation of an already-ACTIVE workspace', async () => {
    const tx = makeMockTx();
    tx.team.findUniqueOrThrow.mockResolvedValue({
      id: 'ws_test',
      lifecycle: 'ACTIVE',
    });
    const db = makeMockDb(tx);

    await expect(
      activateWorkspaceAndPublish(db, SIGNING_PARAMS)
    ).rejects.toMatchObject({ status: 409 });
    expect(publishSnapshotInTx).not.toHaveBeenCalled();
  });

  it('rejects activation of a non-PROVISIONING workspace', async () => {
    const tx = makeMockTx();
    tx.team.findUniqueOrThrow.mockResolvedValue({
      id: 'ws_test',
      lifecycle: 'SUSPENDED',
    });
    const db = makeMockDb(tx);

    await expect(
      activateWorkspaceAndPublish(db, SIGNING_PARAMS)
    ).rejects.toMatchObject({ status: 409 });
    expect(tx.team.update).not.toHaveBeenCalled();
  });

  it('rejects when the workspace does not exist', async () => {
    const tx = makeMockTx();
    tx.$queryRaw.mockResolvedValue([]);
    const db = makeMockDb(tx);

    await expect(
      activateWorkspaceAndPublish(db, SIGNING_PARAMS)
    ).rejects.toMatchObject({ status: 404 });
  });
});
