import {
  claimAndDispatch,
  type DispatcherOpts,
  type DispatchResult,
} from '@/lib/entitlements/dispatcher';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Minimal mock Prisma client with only the methods the dispatcher uses. */
function makePrisma(overrides: {
  queryRawResults?: unknown[];
  snapshotRows?: Record<string, { canonicalEnvelope: Buffer }>;
  updateManyCounts?: number[];
}) {
  const updateManyResults = [...(overrides.updateManyCounts ?? [1])];
  let updateManyCallIndex = 0;

  return {
    $queryRaw: jest.fn().mockResolvedValue(overrides.queryRawResults ?? []),
    entitlementSnapshotOutbox: {
      updateMany: jest.fn().mockImplementation(() => {
        const count =
          updateManyResults[
            Math.min(updateManyCallIndex++, updateManyResults.length - 1)
          ];
        return Promise.resolve({ count });
      }),
    },
    entitlementSnapshot: {
      findUnique: jest.fn().mockImplementation(({ where }: any) => {
        const row = overrides.snapshotRows?.[where.id];
        return Promise.resolve(row ?? null);
      }),
    },
  };
}

function baseOpts(
  deliverFn: DispatcherOpts['deliverFn'],
  extra?: Partial<DispatcherOpts>
): DispatcherOpts {
  return {
    domainBaseUrl: 'https://domain.test',
    pushToken: 'test-token',
    batchSize: 10,
    leaseSeconds: 120,
    maxAttempts: 5,
    deliverFn,
    ...extra,
  };
}

const ENVELOPE_BYTES = Buffer.from('{"payload":"test"}', 'utf-8');

function makeOutboxRow(overrides?: Record<string, unknown>) {
  // Default attempts=1 simulates a row RETURNING * after the claim SQL
  // incremented attempts from 0→1 (first delivery attempt).
  return {
    id: 'ob_1',
    snapshotId: 'snap_1',
    teamId: 'team_1',
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

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('claimAndDispatch', () => {
  it('throws when pushToken is empty', async () => {
    const prisma = makePrisma({ queryRawResults: [] });
    await expect(
      claimAndDispatch(
        prisma as any,
        'disp_1',
        baseOpts(jest.fn(), { pushToken: '' })
      )
    ).rejects.toThrow('pushToken is required');
  });

  it('returns zeros when there are no claimable records', async () => {
    const prisma = makePrisma({ queryRawResults: [] });
    const result = await claimAndDispatch(
      prisma as any,
      'disp_1',
      baseOpts(jest.fn())
    );
    expect(result).toEqual<DispatchResult>({
      claimed: 0,
      delivered: 0,
      failed: 0,
      deadLettered: 0,
    });
  });

  it('claims records with DELIVERING state and a lease id', async () => {
    const row = makeOutboxRow();
    const prisma = makePrisma({
      queryRawResults: [row],
      snapshotRows: { snap_1: { canonicalEnvelope: ENVELOPE_BYTES } },
    });
    const deliverFn = jest.fn().mockResolvedValue({ status: 200 });

    await claimAndDispatch(prisma as any, 'disp_1', baseOpts(deliverFn));

    // The claim query should have been called
    expect(prisma.$queryRaw).toHaveBeenCalled();

    // Delivery should have been attempted
    expect(deliverFn).toHaveBeenCalledWith(
      'https://domain.test/api/internal/domain/workspaces/team_1/entitlement-inbox',
      ENVELOPE_BYTES,
      'test-token'
    );
  });

  it('transitions to DELIVERED on HTTP 2xx', async () => {
    const row = makeOutboxRow();
    const prisma = makePrisma({
      queryRawResults: [row],
      snapshotRows: { snap_1: { canonicalEnvelope: ENVELOPE_BYTES } },
    });
    const deliverFn = jest.fn().mockResolvedValue({ status: 200 });

    const result = await claimAndDispatch(
      prisma as any,
      'disp_1',
      baseOpts(deliverFn)
    );

    expect(result.delivered).toBe(1);
    expect(result.failed).toBe(0);
    expect(result.deadLettered).toBe(0);

    // Settlement update should set DELIVERED
    const updateCall =
      prisma.entitlementSnapshotOutbox.updateMany.mock.calls[0][0];
    expect(updateCall.data.deliveryState).toBe('DELIVERED');
    expect(updateCall.data.deliveredAt).toBeInstanceOf(Date);
    expect(updateCall.data.lastError).toBeNull();
  });

  it('transitions to FAILED with backoff on HTTP 5xx', async () => {
    const row = makeOutboxRow({ attempts: 2 });
    const prisma = makePrisma({
      queryRawResults: [row],
      snapshotRows: { snap_1: { canonicalEnvelope: ENVELOPE_BYTES } },
    });
    const deliverFn = jest.fn().mockResolvedValue({ status: 503 });

    const result = await claimAndDispatch(
      prisma as any,
      'disp_1',
      baseOpts(deliverFn)
    );

    expect(result.failed).toBe(1);
    expect(result.delivered).toBe(0);

    const updateCall =
      prisma.entitlementSnapshotOutbox.updateMany.mock.calls[0][0];
    expect(updateCall.data.deliveryState).toBe('FAILED');
    expect(updateCall.data.lastError).toContain('503');
    expect(updateCall.data.nextAttemptAt).toBeInstanceOf(Date);
    // Backoff: nextAttemptAt should be in the future
    expect(updateCall.data.nextAttemptAt.getTime()).toBeGreaterThan(
      Date.now() - 1000
    );
  });

  it('transitions to FAILED with backoff on network error', async () => {
    const row = makeOutboxRow({ attempts: 2 });
    const prisma = makePrisma({
      queryRawResults: [row],
      snapshotRows: { snap_1: { canonicalEnvelope: ENVELOPE_BYTES } },
    });
    const deliverFn = jest.fn().mockRejectedValue(new Error('ECONNREFUSED'));

    const result = await claimAndDispatch(
      prisma as any,
      'disp_1',
      baseOpts(deliverFn)
    );

    expect(result.failed).toBe(1);
    const updateCall =
      prisma.entitlementSnapshotOutbox.updateMany.mock.calls[0][0];
    expect(updateCall.data.deliveryState).toBe('FAILED');
    expect(updateCall.data.lastError).toContain('ECONNREFUSED');
  });

  it('transitions to DEAD_LETTER on HTTP 409 (conflict)', async () => {
    const row = makeOutboxRow();
    const prisma = makePrisma({
      queryRawResults: [row],
      snapshotRows: { snap_1: { canonicalEnvelope: ENVELOPE_BYTES } },
    });
    const deliverFn = jest.fn().mockResolvedValue({ status: 409 });

    const result = await claimAndDispatch(
      prisma as any,
      'disp_1',
      baseOpts(deliverFn)
    );

    expect(result.deadLettered).toBe(1);
    expect(result.delivered).toBe(0);

    const updateCall =
      prisma.entitlementSnapshotOutbox.updateMany.mock.calls[0][0];
    expect(updateCall.data.deliveryState).toBe('DEAD_LETTER');
    expect(updateCall.data.lastError).toContain('409');
  });

  it('transitions to DEAD_LETTER on HTTP 400 (bad request)', async () => {
    const row = makeOutboxRow();
    const prisma = makePrisma({
      queryRawResults: [row],
      snapshotRows: { snap_1: { canonicalEnvelope: ENVELOPE_BYTES } },
    });
    const deliverFn = jest.fn().mockResolvedValue({ status: 400 });

    const result = await claimAndDispatch(
      prisma as any,
      'disp_1',
      baseOpts(deliverFn)
    );

    expect(result.deadLettered).toBe(1);
    const updateCall =
      prisma.entitlementSnapshotOutbox.updateMany.mock.calls[0][0];
    expect(updateCall.data.deliveryState).toBe('DEAD_LETTER');
    expect(updateCall.data.lastError).toContain('400');
  });

  it('transitions to DEAD_LETTER on HTTP 422 (unprocessable)', async () => {
    const row = makeOutboxRow();
    const prisma = makePrisma({
      queryRawResults: [row],
      snapshotRows: { snap_1: { canonicalEnvelope: ENVELOPE_BYTES } },
    });
    const deliverFn = jest.fn().mockResolvedValue({ status: 422 });

    const result = await claimAndDispatch(
      prisma as any,
      'disp_1',
      baseOpts(deliverFn)
    );

    expect(result.deadLettered).toBe(1);
    const updateCall =
      prisma.entitlementSnapshotOutbox.updateMany.mock.calls[0][0];
    expect(updateCall.data.deliveryState).toBe('DEAD_LETTER');
  });

  it('transitions to DEAD_LETTER when max attempts reached', async () => {
    // RETURNING * gives post-increment attempts=5, which equals maxAttempts
    const row = makeOutboxRow({ attempts: 5, maxAttempts: 5 });
    const prisma = makePrisma({
      queryRawResults: [row],
      snapshotRows: { snap_1: { canonicalEnvelope: ENVELOPE_BYTES } },
    });
    const deliverFn = jest.fn().mockResolvedValue({ status: 500 });

    const result = await claimAndDispatch(
      prisma as any,
      'disp_1',
      baseOpts(deliverFn, { maxAttempts: 5 })
    );

    expect(result.deadLettered).toBe(1);
    expect(result.failed).toBe(0);

    const updateCall =
      prisma.entitlementSnapshotOutbox.updateMany.mock.calls[0][0];
    expect(updateCall.data.deliveryState).toBe('DEAD_LETTER');
  });

  it('uses CAS via deliveryLeaseId in settlement updates', async () => {
    const row = makeOutboxRow();
    const prisma = makePrisma({
      queryRawResults: [row],
      snapshotRows: { snap_1: { canonicalEnvelope: ENVELOPE_BYTES } },
    });
    const deliverFn = jest.fn().mockResolvedValue({ status: 200 });

    await claimAndDispatch(prisma as any, 'disp_1', baseOpts(deliverFn));

    const updateCall =
      prisma.entitlementSnapshotOutbox.updateMany.mock.calls[0][0];
    // The where clause must include deliveryLeaseId for CAS
    expect(updateCall.where.id).toBe('ob_1');
    expect(updateCall.where.deliveryLeaseId).toBeDefined();
    expect(typeof updateCall.where.deliveryLeaseId).toBe('string');
  });

  it('handles multiple records in a batch', async () => {
    const rows = [
      makeOutboxRow({ id: 'ob_1', snapshotId: 'snap_1' }),
      makeOutboxRow({ id: 'ob_2', snapshotId: 'snap_2' }),
      makeOutboxRow({ id: 'ob_3', snapshotId: 'snap_3' }),
    ];
    const prisma = makePrisma({
      queryRawResults: rows,
      snapshotRows: {
        snap_1: { canonicalEnvelope: ENVELOPE_BYTES },
        snap_2: { canonicalEnvelope: ENVELOPE_BYTES },
        snap_3: { canonicalEnvelope: ENVELOPE_BYTES },
      },
      updateManyCounts: [1, 1, 1],
    });
    const deliverFn = jest.fn().mockResolvedValue({ status: 200 });

    const result = await claimAndDispatch(
      prisma as any,
      'disp_1',
      baseOpts(deliverFn)
    );

    expect(result.claimed).toBe(3);
    expect(result.delivered).toBe(3);
    expect(deliverFn).toHaveBeenCalledTimes(3);
  });

  it('dead-letters when snapshot row is missing', async () => {
    const row = makeOutboxRow();
    const prisma = makePrisma({
      queryRawResults: [row],
      snapshotRows: {}, // no snapshot found
    });
    const deliverFn = jest.fn();

    const result = await claimAndDispatch(
      prisma as any,
      'disp_1',
      baseOpts(deliverFn)
    );

    expect(result.deadLettered).toBe(1);
    expect(deliverFn).not.toHaveBeenCalled();

    const updateCall =
      prisma.entitlementSnapshotOutbox.updateMany.mock.calls[0][0];
    expect(updateCall.data.deliveryState).toBe('DEAD_LETTER');
    expect(updateCall.data.lastError).toContain('missing');
  });

  it('computes exponential backoff correctly', async () => {
    // Post-increment attempts: 1→computeBackoff(0)=1s, 2→2s, 3→4s, 4→8s
    const testCases = [
      { attempts: 1, expectedMinMs: 1000, expectedMaxMs: 1500 },
      { attempts: 2, expectedMinMs: 2000, expectedMaxMs: 2500 },
      { attempts: 3, expectedMinMs: 4000, expectedMaxMs: 4500 },
      { attempts: 4, expectedMinMs: 8000, expectedMaxMs: 8500 },
    ];

    for (const tc of testCases) {
      const row = makeOutboxRow({ attempts: tc.attempts });
      const prisma = makePrisma({
        queryRawResults: [row],
        snapshotRows: { snap_1: { canonicalEnvelope: ENVELOPE_BYTES } },
      });
      const deliverFn = jest.fn().mockResolvedValue({ status: 500 });

      const now = Date.now();
      await claimAndDispatch(
        prisma as any,
        'disp_1',
        baseOpts(deliverFn, { maxAttempts: 10 })
      );

      const updateCall =
        prisma.entitlementSnapshotOutbox.updateMany.mock.calls[0][0];
      const nextAttemptAt = updateCall.data.nextAttemptAt as Date;
      const delayMs = nextAttemptAt.getTime() - now;

      expect(delayMs).toBeGreaterThanOrEqual(tc.expectedMinMs - 100);
      expect(delayMs).toBeLessThan(tc.expectedMaxMs + 500);
    }
  });

  it('caps backoff at 5 minutes', async () => {
    // Post-increment attempts=20, computeBackoff(19)=2^19s=524288s, capped at 300s
    const row = makeOutboxRow({ attempts: 20, maxAttempts: 100 });
    const prisma = makePrisma({
      queryRawResults: [row],
      snapshotRows: { snap_1: { canonicalEnvelope: ENVELOPE_BYTES } },
    });
    const deliverFn = jest.fn().mockResolvedValue({ status: 500 });

    const now = Date.now();
    await claimAndDispatch(
      prisma as any,
      'disp_1',
      baseOpts(deliverFn, { maxAttempts: 100 })
    );

    const updateCall =
      prisma.entitlementSnapshotOutbox.updateMany.mock.calls[0][0];
    const nextAttemptAt = updateCall.data.nextAttemptAt as Date;
    const delayMs = nextAttemptAt.getTime() - now;

    // Should be capped at 300_000ms (5 minutes) + small tolerance
    expect(delayMs).toBeLessThanOrEqual(301_000);
    expect(delayMs).toBeGreaterThanOrEqual(299_000);
  });

  it('HTTP 201 counts as successful delivery', async () => {
    const row = makeOutboxRow();
    const prisma = makePrisma({
      queryRawResults: [row],
      snapshotRows: { snap_1: { canonicalEnvelope: ENVELOPE_BYTES } },
    });
    const deliverFn = jest.fn().mockResolvedValue({ status: 201 });

    const result = await claimAndDispatch(
      prisma as any,
      'disp_1',
      baseOpts(deliverFn)
    );

    expect(result.delivered).toBe(1);
    const updateCall =
      prisma.entitlementSnapshotOutbox.updateMany.mock.calls[0][0];
    expect(updateCall.data.deliveryState).toBe('DELIVERED');
  });

  it('HTTP 429 (rate limited) transitions to FAILED with backoff', async () => {
    const row = makeOutboxRow({ attempts: 2 });
    const prisma = makePrisma({
      queryRawResults: [row],
      snapshotRows: { snap_1: { canonicalEnvelope: ENVELOPE_BYTES } },
    });
    const deliverFn = jest.fn().mockResolvedValue({ status: 429 });

    const result = await claimAndDispatch(
      prisma as any,
      'disp_1',
      baseOpts(deliverFn)
    );

    expect(result.failed).toBe(1);
    const updateCall =
      prisma.entitlementSnapshotOutbox.updateMany.mock.calls[0][0];
    expect(updateCall.data.deliveryState).toBe('FAILED');
  });

  // -------------------------------------------------------------------
  // Claim SQL shape: lease-expiry reclaim + SKIP LOCKED
  // -------------------------------------------------------------------

  it('claim SQL reclaims expired DELIVERING leases and uses SKIP LOCKED for concurrent-claim protection', async () => {
    const prisma = makePrisma({ queryRawResults: [] });

    await claimAndDispatch(prisma as any, 'disp_1', baseOpts(jest.fn()));

    expect(prisma.$queryRaw).toHaveBeenCalledTimes(1);
    const [strings] = prisma.$queryRaw.mock.calls[0] as [TemplateStringsArray];
    // Join with a placeholder so we can inspect the literal SQL text
    // surrounding each interpolated value (values themselves are passed
    // as separate tagged-template arguments, not embedded in `strings`).
    const sql = strings.join('¶');

    // Concurrent claim protection: FOR UPDATE SKIP LOCKED on the claim subquery.
    expect(sql).toContain('FOR UPDATE SKIP LOCKED');

    // Expired DELIVERING leases can be reclaimed: the WHERE clause must
    // include a branch that re-claims DELIVERING rows whose lease has
    // expired (deliveryLeaseExpiresAt < now).
    expect(sql).toContain('"deliveryState" = \'DELIVERING\'');
    expect(sql).toContain('"deliveryLeaseExpiresAt" < ¶');

    // Retryable PENDING/FAILED branches are still present alongside the
    // reclaim branch.
    expect(sql).toContain('"deliveryState" = \'PENDING\'');
    expect(sql).toContain('"deliveryState" = \'FAILED\'');
  });

  it('generates a fresh lease id per dispatch cycle so a reclaimed lease cannot be settled by its previous owner', async () => {
    const prisma = makePrisma({ queryRawResults: [] });

    await claimAndDispatch(prisma as any, 'disp_1', baseOpts(jest.fn()));
    await claimAndDispatch(prisma as any, 'disp_1', baseOpts(jest.fn()));

    const [, leaseId1] = prisma.$queryRaw.mock.calls[0] as unknown as [
      TemplateStringsArray,
      string,
    ];
    const [, leaseId2] = prisma.$queryRaw.mock.calls[1] as unknown as [
      TemplateStringsArray,
      string,
    ];

    expect(leaseId1).toEqual(expect.any(String));
    expect(leaseId2).toEqual(expect.any(String));
    expect(leaseId1).not.toBe(leaseId2);
  });

  // -------------------------------------------------------------------
  // CAS settlement count check
  // -------------------------------------------------------------------

  it('does not count a delivery as settled when the CAS update affects zero rows (lease reclaimed mid-flight)', async () => {
    const row = makeOutboxRow();
    const prisma = makePrisma({
      queryRawResults: [row],
      snapshotRows: { snap_1: { canonicalEnvelope: ENVELOPE_BYTES } },
      updateManyCounts: [0], // simulate another dispatcher reclaiming the lease first
    });
    const deliverFn = jest.fn().mockResolvedValue({ status: 200 });
    const warnSpy = jest.spyOn(console, 'warn').mockImplementation(() => {});

    const result = await claimAndDispatch(
      prisma as any,
      'disp_1',
      baseOpts(deliverFn)
    );

    expect(result.delivered).toBe(0);
    expect(result.failed).toBe(0);
    expect(result.deadLettered).toBe(0);
    expect(warnSpy).toHaveBeenCalledWith(
      expect.stringContaining('CAS settlement no-op')
    );

    warnSpy.mockRestore();
  });

  it('does not double-count a dead-letter when the CAS settlement no-ops', async () => {
    const row = makeOutboxRow();
    const prisma = makePrisma({
      queryRawResults: [row],
      snapshotRows: { snap_1: { canonicalEnvelope: ENVELOPE_BYTES } },
      updateManyCounts: [0],
    });
    const deliverFn = jest.fn().mockResolvedValue({ status: 409 });
    const warnSpy = jest.spyOn(console, 'warn').mockImplementation(() => {});
    const errorSpy = jest.spyOn(console, 'error').mockImplementation(() => {});

    const result = await claimAndDispatch(
      prisma as any,
      'disp_1',
      baseOpts(deliverFn)
    );

    expect(result.deadLettered).toBe(0);
    expect(warnSpy).toHaveBeenCalled();
    // No alert should fire for a settlement that never actually applied.
    expect(errorSpy).not.toHaveBeenCalled();

    warnSpy.mockRestore();
    errorSpy.mockRestore();
  });

  // -------------------------------------------------------------------
  // Dead-letter alerting
  // -------------------------------------------------------------------

  it('logs a structured alert on DEAD_LETTER (409 conflict)', async () => {
    const row = makeOutboxRow({
      id: 'ob_1',
      snapshotId: 'snap_1',
      teamId: 'team_1',
    });
    const prisma = makePrisma({
      queryRawResults: [row],
      snapshotRows: { snap_1: { canonicalEnvelope: ENVELOPE_BYTES } },
    });
    const deliverFn = jest.fn().mockResolvedValue({ status: 409 });
    const errorSpy = jest.spyOn(console, 'error').mockImplementation(() => {});

    await claimAndDispatch(prisma as any, 'disp_1', baseOpts(deliverFn));

    expect(errorSpy).toHaveBeenCalledWith(
      expect.stringMatching(/DEAD_LETTER.*ob_1.*snap_1.*team_1.*409/)
    );

    errorSpy.mockRestore();
  });

  it('logs a structured alert on DEAD_LETTER (max attempts exhausted)', async () => {
    const row = makeOutboxRow({ attempts: 5, maxAttempts: 5 });
    const prisma = makePrisma({
      queryRawResults: [row],
      snapshotRows: { snap_1: { canonicalEnvelope: ENVELOPE_BYTES } },
    });
    const deliverFn = jest.fn().mockResolvedValue({ status: 500 });
    const errorSpy = jest.spyOn(console, 'error').mockImplementation(() => {});

    await claimAndDispatch(
      prisma as any,
      'disp_1',
      baseOpts(deliverFn, { maxAttempts: 5 })
    );

    expect(errorSpy).toHaveBeenCalledWith(
      expect.stringMatching(/DEAD_LETTER.*max attempts/)
    );

    errorSpy.mockRestore();
  });

  it('logs a structured alert on DEAD_LETTER when the snapshot row is missing', async () => {
    const row = makeOutboxRow();
    const prisma = makePrisma({
      queryRawResults: [row],
      snapshotRows: {},
    });
    const errorSpy = jest.spyOn(console, 'error').mockImplementation(() => {});

    await claimAndDispatch(prisma as any, 'disp_1', baseOpts(jest.fn()));

    expect(errorSpy).toHaveBeenCalledWith(
      expect.stringMatching(/DEAD_LETTER.*missing/)
    );

    errorSpy.mockRestore();
  });

  it('does not alert on a retryable FAILED transition', async () => {
    const row = makeOutboxRow({ attempts: 2 });
    const prisma = makePrisma({
      queryRawResults: [row],
      snapshotRows: { snap_1: { canonicalEnvelope: ENVELOPE_BYTES } },
    });
    const deliverFn = jest.fn().mockResolvedValue({ status: 503 });
    const errorSpy = jest.spyOn(console, 'error').mockImplementation(() => {});

    await claimAndDispatch(prisma as any, 'disp_1', baseOpts(deliverFn));

    expect(errorSpy).not.toHaveBeenCalled();

    errorSpy.mockRestore();
  });
});
