import {
  DEFAULT_RENEWAL_INTERVAL_SECONDS,
  renewExpiringLeases,
  renewalIntervalSeconds,
} from '@/lib/entitlements/renewal';

const KEY = Buffer.from('test-signing-key', 'utf8');
const NOW = new Date('2026-10-08T12:00:00.000Z');

function makePrisma(dueTeamIds: string[]) {
  return {
    $queryRaw: jest
      .fn()
      .mockResolvedValue(dueTeamIds.map((teamId) => ({ teamId }))),
  };
}

/** The tagged-template values `$queryRaw` received, in order. */
function queryValues(prisma: ReturnType<typeof makePrisma>): unknown[] {
  const [, ...values] = prisma.$queryRaw.mock.calls[0];
  return values;
}

function queryText(prisma: ReturnType<typeof makePrisma>): string {
  const [strings] = prisma.$queryRaw.mock.calls[0] as [TemplateStringsArray];
  return strings.join('?');
}

describe('renewExpiringLeases', () => {
  test('republishes every due Workspace through the Publication Module', async () => {
    const prisma = makePrisma(['team_a', 'team_b']);
    const publish = jest.fn().mockResolvedValue({});

    const result = await renewExpiringLeases(prisma as any, {
      signingKeyId: 'dev-v1',
      signingKey: KEY,
      now: NOW,
      publish,
    });

    expect(result).toEqual({
      examined: 2,
      renewed: 2,
      failed: 0,
      failures: [],
    });
    expect(publish.mock.calls).toEqual([
      [prisma, 'team_a', 'dev-v1', KEY],
      [prisma, 'team_b', 'dev-v1', KEY],
    ]);
  });

  test('selects ACTIVE/SUSPENDED Workspaces whose newest snapshot is older than the interval', async () => {
    const prisma = makePrisma([]);

    await renewExpiringLeases(prisma as any, {
      signingKeyId: 'dev-v1',
      signingKey: KEY,
      now: NOW,
      batchSize: 7,
      publish: jest.fn(),
    });

    const sql = queryText(prisma);
    expect(sql).toContain(`t."lifecycle" IN ('ACTIVE', 'SUSPENDED')`);
    expect(sql).toContain('JOIN "CommercialAccessProjection"');
    expect(sql).toContain('MAX(s."issuedAt")');
    // issuedBefore = now - 6h by default; the batch size bounds the scan.
    expect(queryValues(prisma)).toEqual([
      new Date(NOW.getTime() - DEFAULT_RENEWAL_INTERVAL_SECONDS * 1000),
      7,
    ]);
  });

  test('honours a custom interval', async () => {
    const prisma = makePrisma([]);

    await renewExpiringLeases(prisma as any, {
      signingKeyId: 'dev-v1',
      signingKey: KEY,
      now: NOW,
      intervalSeconds: 3600,
      publish: jest.fn(),
    });

    expect(queryValues(prisma)[0]).toEqual(
      new Date(NOW.getTime() - 3600 * 1000)
    );
  });

  test('a failing Workspace is reported and does not stop the others', async () => {
    const prisma = makePrisma(['team_a', 'team_b', 'team_c']);
    const publish = jest
      .fn()
      .mockResolvedValueOnce({})
      .mockRejectedValueOnce(new Error('no active commercial access binding'))
      .mockResolvedValueOnce({});

    const result = await renewExpiringLeases(prisma as any, {
      signingKeyId: 'dev-v1',
      signingKey: KEY,
      now: NOW,
      publish,
    });

    expect(result.renewed).toBe(2);
    expect(result.failed).toBe(1);
    expect(result.failures).toEqual([
      { teamId: 'team_b', error: 'no active commercial access binding' },
    ]);
    expect(publish).toHaveBeenCalledTimes(3);
  });

  test('nothing due publishes nothing', async () => {
    const publish = jest.fn();
    const result = await renewExpiringLeases(makePrisma([]) as any, {
      signingKeyId: 'dev-v1',
      signingKey: KEY,
      now: NOW,
      publish,
    });
    expect(result).toEqual({
      examined: 0,
      renewed: 0,
      failed: 0,
      failures: [],
    });
    expect(publish).not.toHaveBeenCalled();
  });
});

describe('renewalIntervalSeconds', () => {
  test('defaults to the spec cadence of 6 hours', () => {
    expect(renewalIntervalSeconds(undefined)).toBe(21600);
    expect(renewalIntervalSeconds('')).toBe(21600);
  });

  test('accepts a positive interval that keeps the 24h lease covered', () => {
    expect(renewalIntervalSeconds('3600')).toBe(3600);
  });

  test.each(['0', '-5', 'abc', '1.5', '86400'])('rejects %s', (raw) => {
    expect(() => renewalIntervalSeconds(raw)).toThrow(
      /ENTITLEMENT_RENEWAL_INTERVAL_SECONDS/
    );
  });
});
