jest.mock('@/lib/env', () => ({
  __esModule: true,
  default: {
    domainApi: { baseUrl: 'https://domain.example' },
    controlPlaneToDomainEntitlementToken: 'push-token-sentinel-4c1e',
  },
}));

import { claimAndDispatch } from '@/lib/entitlements/dispatcher';
import { dispatchWorkspaceOutboxNow } from '@/lib/entitlements/immediate-dispatch';

const PUSH_TOKEN = 'push-token-sentinel-4c1e';
const ENVELOPE = Buffer.from('{"payload":"acme-charge"}', 'utf-8');

function outboxRow(overrides: Record<string, unknown> = {}) {
  return {
    id: 'ob_1',
    snapshotId: 'snap_1',
    teamId: 'ws_1',
    deliveryState: 'DELIVERING',
    attempts: 1,
    maxAttempts: 5,
    lastAttemptAt: null,
    nextAttemptAt: null,
    lastError: null,
    deliveryLeaseId: null,
    deliveryLeaseExpiresAt: null,
    deliveredAt: null,
    ...overrides,
  };
}

/** The dispatcher's Prisma surface; `claimed` is what the claim returns. */
function makePrisma(claimed: unknown[] = [outboxRow()]) {
  return {
    $queryRaw: jest.fn().mockResolvedValue(claimed),
    entitlementSnapshotOutbox: {
      updateMany: jest.fn().mockResolvedValue({ count: 1 }),
    },
    entitlementSnapshot: {
      findUnique: jest.fn().mockResolvedValue({ canonicalEnvelope: ENVELOPE }),
    },
  };
}

/** The claim's interpolated values, in order, and its SQL text. */
function claimCall(prisma: ReturnType<typeof makePrisma>) {
  const [strings, ...values] = prisma.$queryRaw.mock.calls[0] as [
    TemplateStringsArray,
    ...unknown[],
  ];
  return { sql: strings.join('¶'), values };
}

function captureLogs() {
  const lines: string[] = [];
  const record = (line: unknown) => lines.push(String(line));
  jest.spyOn(console, 'log').mockImplementation(record);
  jest.spyOn(console, 'warn').mockImplementation(record);
  jest.spyOn(console, 'error').mockImplementation(record);
  return lines;
}

afterEach(() => {
  jest.restoreAllMocks();
  jest.useRealTimers();
});

describe('claimAndDispatch Workspace scope', () => {
  const opts = (extra: Record<string, unknown> = {}) => ({
    domainBaseUrl: 'https://domain.example',
    pushToken: PUSH_TOKEN,
    deliverFn: jest.fn().mockResolvedValue({ status: 200 }),
    ...extra,
  });

  it('claims only the given Workspace when teamId is set', async () => {
    const prisma = makePrisma([]);
    await claimAndDispatch(prisma as any, 'disp', opts({ teamId: 'ws_1' }));

    const { sql, values } = claimCall(prisma);
    expect(sql).toContain('¶::text IS NULL OR "teamId" = ¶');
    expect(sql).toContain('FOR UPDATE SKIP LOCKED');
    expect(values.filter((value) => value === 'ws_1')).toHaveLength(2);
  });

  it('keeps the scheduled run unscoped', async () => {
    const prisma = makePrisma([]);
    await claimAndDispatch(prisma as any, 'disp', opts());

    const { values } = claimCall(prisma);
    // Both team-filter slots are NULL, so the predicate admits every row.
    expect(values.filter((value) => value === null)).toHaveLength(2);
  });

  it('hands the delivery an abort signal only when a timeout is set', async () => {
    const timed = opts({ deliveryTimeoutMs: 1_000 });
    await claimAndDispatch(makePrisma() as any, 'disp', timed);
    expect(timed.deliverFn.mock.calls[0][3]).toBeInstanceOf(AbortSignal);

    const untimed = opts();
    await claimAndDispatch(makePrisma() as any, 'disp', untimed);
    expect(untimed.deliverFn.mock.calls[0]).toHaveLength(3);
  });
});

describe('dispatchWorkspaceOutboxNow', () => {
  it('delivers the Workspace record through the scoped claim', async () => {
    const lines = captureLogs();
    const prisma = makePrisma();
    const deliverFn = jest.fn().mockResolvedValue({ status: 200 });

    const outcome = await dispatchWorkspaceOutboxNow(prisma as any, 'ws_1', {
      deliverFn,
    });

    expect(outcome).toEqual({
      outcome: 'dispatched',
      result: { claimed: 1, delivered: 1, failed: 0, deadLettered: 0 },
    });
    expect(claimCall(prisma).values).toContain('ws_1');
    expect(deliverFn).toHaveBeenCalledWith(
      'https://domain.example/api/internal/domain/workspaces/ws_1/entitlement-inbox',
      ENVELOPE,
      PUSH_TOKEN,
      expect.any(AbortSignal)
    );
    expect(prisma.entitlementSnapshotOutbox.updateMany).toHaveBeenCalledWith(
      expect.objectContaining({
        data: expect.objectContaining({ deliveryState: 'DELIVERED' }),
      })
    );
    expect(lines).toHaveLength(1);
    expect(JSON.parse(lines[0])).toMatchObject({
      level: 'info',
      teamId: 'ws_1',
      outcome: 'dispatched',
      delivered: 1,
    });
  });

  it('leaves a failed delivery FAILED for the scheduled run and does not throw', async () => {
    const lines = captureLogs();
    const prisma = makePrisma();
    const deliverFn = jest
      .fn()
      .mockRejectedValue(new Error(`connect refused Bearer ${PUSH_TOKEN}`));

    const outcome = await dispatchWorkspaceOutboxNow(prisma as any, 'ws_1', {
      deliverFn,
    });

    expect(outcome).toMatchObject({
      outcome: 'dispatched',
      result: { claimed: 1, delivered: 0, failed: 1 },
    });
    const [settlement] = prisma.entitlementSnapshotOutbox.updateMany.mock
      .calls[0] as [{ data: { deliveryState: string; nextAttemptAt: Date } }];
    expect(settlement.data.deliveryState).toBe('FAILED');
    expect(settlement.data.nextAttemptAt).toBeInstanceOf(Date);
    expect(JSON.parse(lines[0])).toMatchObject({
      level: 'warn',
      outcome: 'dispatched',
      failed: 1,
    });
    expect(lines.join('\n')).not.toContain(PUSH_TOKEN);
  });

  it('logs only the error class when the claim itself fails', async () => {
    const lines = captureLogs();
    const prisma = makePrisma();
    class PrismaClientKnownRequestError extends Error {
      name = 'PrismaClientKnownRequestError';
    }
    prisma.$queryRaw.mockRejectedValue(
      new PrismaClientKnownRequestError(
        `UPDATE "EntitlementSnapshotOutbox" ... ${PUSH_TOKEN}`
      )
    );

    await expect(
      dispatchWorkspaceOutboxNow(prisma as any, 'ws_1')
    ).resolves.toEqual({ outcome: 'error' });

    expect(lines).toHaveLength(1);
    expect(JSON.parse(lines[0])).toMatchObject({
      level: 'warn',
      teamId: 'ws_1',
      outcome: 'error',
      reason: 'PrismaClientKnownRequestError',
    });
    expect(lines[0]).not.toContain('EntitlementSnapshotOutbox');
    expect(lines[0]).not.toContain(PUSH_TOKEN);
  });

  it('stops waiting at the deadline and lets the cycle settle in the background', async () => {
    const lines = captureLogs();
    const prisma = makePrisma();
    let signal: AbortSignal | undefined;
    const deliverFn = jest.fn(
      (_url: string, _body: Buffer, _token: string, given?: AbortSignal) =>
        new Promise<{ status: number }>((_, reject) => {
          signal = given;
          given?.addEventListener('abort', () => reject(given.reason));
        })
    );

    const started = Date.now();
    const outcome = await dispatchWorkspaceOutboxNow(prisma as any, 'ws_1', {
      deliverFn,
      timeoutMs: 600,
    });
    const elapsed = Date.now() - started;

    // The POST is aborted 500 ms before the deadline, so it settles first.
    expect(outcome).toMatchObject({
      outcome: 'dispatched',
      result: { delivered: 0, failed: 1 },
    });
    expect(signal?.aborted).toBe(true);
    expect(elapsed).toBeLessThan(600 + 250);
    expect(JSON.parse(lines[0])).toMatchObject({ outcome: 'dispatched' });
  });

  it('returns at the deadline even when the claim hangs', async () => {
    const lines = captureLogs();
    const prisma = makePrisma();
    let releaseClaim: (rows: unknown[]) => void = () => undefined;
    prisma.$queryRaw.mockReturnValue(
      new Promise((resolve) => {
        releaseClaim = resolve;
      })
    );

    const started = Date.now();
    const outcome = await dispatchWorkspaceOutboxNow(prisma as any, 'ws_1', {
      timeoutMs: 100,
      deliverFn: jest.fn().mockResolvedValue({ status: 200 }),
    });

    expect(outcome).toEqual({ outcome: 'timed_out' });
    expect(Date.now() - started).toBeLessThan(100 + 250);
    expect(JSON.parse(lines[0])).toMatchObject({
      level: 'warn',
      outcome: 'timed_out',
    });

    // The abandoned cycle still finishes and settles its claim.
    releaseClaim([outboxRow()]);
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    expect(prisma.entitlementSnapshotOutbox.updateMany).toHaveBeenCalled();
  });

  it('skips without claiming when delivery is not configured', async () => {
    const lines = captureLogs();
    const prisma = makePrisma();

    const outcome = await dispatchWorkspaceOutboxNow(prisma as any, 'ws_1', {
      domainBaseUrl: null,
    });

    expect(outcome).toEqual({ outcome: 'skipped_unconfigured' });
    expect(prisma.$queryRaw).not.toHaveBeenCalled();
    expect(JSON.parse(lines[0])).toMatchObject({
      level: 'warn',
      outcome: 'skipped_unconfigured',
    });
  });
});
