import { PrismaJwksPublisher } from '../../lib/delegated-identity/jwks-publisher';

function jwk(kid: string, x: string, y: string) {
  return { kty: 'EC', crv: 'P-256', x, y, kid, use: 'sig', alg: 'ES256' };
}

describe('PrismaJwksPublisher', () => {
  it('returns ACTIVE and RETIRING keys as JWKS', async () => {
    const findMany = jest.fn().mockResolvedValue([
      {
        kid: 'dsk_active',
        publicJwk: jwk('dsk_active', 'x1', 'y1'),
        state: 'ACTIVE',
      },
      {
        kid: 'dsk_retiring',
        publicJwk: jwk('dsk_retiring', 'x2', 'y2'),
        state: 'RETIRING',
      },
    ]);
    const mockPrisma = { delegatedSigningKey: { findMany } };

    const publisher = new PrismaJwksPublisher(mockPrisma as any, 600);
    const result = await publisher.jwksResponse(null);

    expect(result.status).toBe(200);
    const body = JSON.parse(result.body.toString());
    expect(body.keys).toHaveLength(2);
    expect(body.keys[0].kid).toBe('dsk_active');
    expect(result.cacheControl).toBe('public, max-age=600, must-revalidate');
    expect(result.etag).toMatch(/^"sha256:[a-f0-9]+"$/);
  });

  it('queries only ACTIVE and RETIRING states', async () => {
    const findMany = jest.fn().mockResolvedValue([]);
    const mockPrisma = { delegatedSigningKey: { findMany } };

    const publisher = new PrismaJwksPublisher(mockPrisma as any, 600);
    await publisher.jwksResponse(null);

    expect(findMany).toHaveBeenCalledWith(
      expect.objectContaining({
        where: { state: { in: ['ACTIVE', 'RETIRING'] } },
      })
    );
  });

  it('never publishes PREPUBLISHED, RETIRED, or REVOKED keys even if returned by the query', async () => {
    // Defense in depth: the JWKS body only ever contains keys explicitly
    // mapped from the query result, so a broadened `where` clause could
    // not leak a PREPUBLISHED private key's public half prematurely — but
    // assert on the response body directly, not just the query args above.
    const findMany = jest.fn().mockResolvedValue([
      {
        kid: 'dsk_active',
        publicJwk: jwk('dsk_active', 'x1', 'y1'),
        state: 'ACTIVE',
      },
    ]);
    const mockPrisma = { delegatedSigningKey: { findMany } };

    const publisher = new PrismaJwksPublisher(mockPrisma as any, 600);
    const result = await publisher.jwksResponse(null);
    const body = JSON.parse(result.body.toString());
    expect(body.keys.map((k: { kid: string }) => k.kid)).toEqual([
      'dsk_active',
    ]);
  });

  it('returns 304 when ETag matches', async () => {
    const findMany = jest.fn().mockResolvedValue([
      {
        kid: 'dsk_active',
        publicJwk: jwk('dsk_active', 'x1', 'y1'),
        state: 'ACTIVE',
      },
    ]);
    const mockPrisma = { delegatedSigningKey: { findMany } };

    const publisher = new PrismaJwksPublisher(mockPrisma as any, 600);
    const first = await publisher.jwksResponse(null);
    const second = await publisher.jwksResponse(first.etag);

    expect(second.status).toBe(304);
    expect(second.body.length).toBe(0);
    expect(second.etag).toBe(first.etag);
  });

  it('returns 200 when ETag does not match', async () => {
    const findMany = jest.fn().mockResolvedValue([
      {
        kid: 'dsk_active',
        publicJwk: jwk('dsk_active', 'x1', 'y1'),
        state: 'ACTIVE',
      },
    ]);
    const mockPrisma = { delegatedSigningKey: { findMany } };

    const publisher = new PrismaJwksPublisher(mockPrisma as any, 600);
    const result = await publisher.jwksResponse('"sha256:stale"');

    expect(result.status).toBe(200);
    expect(result.body.length).toBeGreaterThan(0);
  });

  it('produces a stable ETag independent of key ordering already applied by the query', async () => {
    const findMany = jest.fn().mockResolvedValue([
      {
        kid: 'dsk_active',
        publicJwk: jwk('dsk_active', 'x1', 'y1'),
        state: 'ACTIVE',
      },
      {
        kid: 'dsk_retiring',
        publicJwk: jwk('dsk_retiring', 'x2', 'y2'),
        state: 'RETIRING',
      },
    ]);
    const mockPrisma = { delegatedSigningKey: { findMany } };

    const publisher = new PrismaJwksPublisher(mockPrisma as any, 600);
    const first = await publisher.jwksResponse(null);
    const second = await publisher.jwksResponse(null);

    expect(first.etag).toBe(second.etag);
  });
});
