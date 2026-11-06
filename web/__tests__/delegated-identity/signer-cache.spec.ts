import crypto from 'node:crypto';
import {
  SignerCache,
  SignerCacheNotLoadedError,
} from '../../lib/delegated-identity/signer-cache';
import { AesGcmPrivateKeyProtector } from '../../lib/delegated-identity/key-protector';

function makeSignedKeyRow(kid = 'dsk_test') {
  const encKey = crypto.randomBytes(32);
  const protector = new AesGcmPrivateKeyProtector(encKey);
  const { privateKey } = crypto.generateKeyPairSync('ec', {
    namedCurve: 'P-256',
  });
  const privatePem = privateKey.export({
    type: 'pkcs8',
    format: 'pem',
  }) as string;
  const binding = { kid, algorithm: 'ES256', publicJwkThumbprint: 'thumb' };
  const protectedKey = protector.protect(Buffer.from(privatePem), binding);

  return {
    protector,
    privatePem,
    row: {
      id: 'dsk_001',
      kid,
      algorithm: 'ES256',
      publicJwkThumbprint: 'thumb',
      protectedPrivateKey: {
        ciphertext: protectedKey.ciphertext.toString('base64'),
        iv: protectedKey.iv.toString('base64'),
        authTag: protectedKey.authTag.toString('base64'),
      },
      state: 'ACTIVE',
    },
  };
}

describe('SignerCache', () => {
  beforeEach(() => {
    jest.useFakeTimers();
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  it('throws SignerCacheNotLoadedError before start() completes its first load', async () => {
    const { protector } = makeSignedKeyRow();
    const mockPrisma = {
      delegatedSigningKey: { findFirst: jest.fn().mockResolvedValue(null) },
    };
    const cache = new SignerCache(
      mockPrisma as any,
      protector,
      300_000,
      30_000
    );

    await expect(cache.getActiveKey()).rejects.toThrow(
      SignerCacheNotLoadedError
    );
  });

  it('loads the ACTIVE key after the jitter delay elapses and caches it', async () => {
    const { protector, privatePem, row } = makeSignedKeyRow();
    const findFirst = jest.fn().mockResolvedValue(row);
    const mockPrisma = { delegatedSigningKey: { findFirst } };
    const cache = new SignerCache(
      mockPrisma as any,
      protector,
      300_000,
      30_000
    );

    const startPromise = cache.start();
    await jest.advanceTimersByTimeAsync(30_000);
    await startPromise;

    expect(findFirst).toHaveBeenCalledTimes(1);
    const key = await cache.getActiveKey();
    expect(key.kid).toBe('dsk_test');
    expect(key.privateKeyPem.toString()).toBe(privatePem);

    cache.stop();
  });

  it('refreshes on the background timer independent of getActiveKey() calls', async () => {
    const { protector, row } = makeSignedKeyRow();
    const findFirst = jest.fn().mockResolvedValue(row);
    const mockPrisma = { delegatedSigningKey: { findFirst } };
    const cache = new SignerCache(mockPrisma as any, protector, 100_000, 0);

    const startPromise = cache.start();
    await jest.advanceTimersByTimeAsync(0);
    await startPromise;
    expect(findFirst).toHaveBeenCalledTimes(1);

    await jest.advanceTimersByTimeAsync(100_000);
    expect(findFirst).toHaveBeenCalledTimes(2);

    await jest.advanceTimersByTimeAsync(100_000);
    expect(findFirst).toHaveBeenCalledTimes(3);

    cache.stop();
  });

  it('stop() halts the background timer', async () => {
    const { protector, row } = makeSignedKeyRow();
    const findFirst = jest.fn().mockResolvedValue(row);
    const mockPrisma = { delegatedSigningKey: { findFirst } };
    const cache = new SignerCache(mockPrisma as any, protector, 100_000, 0);

    const startPromise = cache.start();
    await jest.advanceTimersByTimeAsync(0);
    await startPromise;
    expect(findFirst).toHaveBeenCalledTimes(1);

    cache.stop();
    await jest.advanceTimersByTimeAsync(500_000);
    expect(findFirst).toHaveBeenCalledTimes(1);
  });

  it('keeps the previously cached key when a background refresh fails', async () => {
    const { protector, row } = makeSignedKeyRow();
    const findFirst = jest
      .fn()
      .mockResolvedValueOnce(row)
      .mockRejectedValueOnce(new Error('db unavailable'));
    const mockPrisma = { delegatedSigningKey: { findFirst } };
    const cache = new SignerCache(mockPrisma as any, protector, 100_000, 0);

    const startPromise = cache.start();
    await jest.advanceTimersByTimeAsync(0);
    await startPromise;

    await jest.advanceTimersByTimeAsync(100_000);
    expect(findFirst).toHaveBeenCalledTimes(2);

    const key = await cache.getActiveKey();
    expect(key.kid).toBe('dsk_test');

    cache.stop();
  });

  it('single-flights concurrent refreshes into one Prisma read', async () => {
    const { protector, row } = makeSignedKeyRow();
    let resolveFindFirst: (value: unknown) => void = () => {};
    const findFirst = jest.fn().mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveFindFirst = resolve;
        })
    );
    const mockPrisma = { delegatedSigningKey: { findFirst } };
    const cache = new SignerCache(mockPrisma as any, protector, 300_000, 0);

    // Drive start() up to (but not through) its first `findFirst` call —
    // it awaits the pending promise from here on.
    const startPromise = cache.start();
    await jest.advanceTimersByTimeAsync(0);
    expect(findFirst).toHaveBeenCalledTimes(1);

    // A concurrent caller triggers the private `refresh()` path directly
    // (as the background timer would) while the first refresh is still
    // in flight. It must coalesce onto the same Prisma read rather than
    // starting a second one.
    const secondRefresh = (
      cache as unknown as { refresh(): Promise<unknown> }
    ).refresh();

    resolveFindFirst(row);
    await startPromise;
    await secondRefresh;

    expect(findFirst).toHaveBeenCalledTimes(1);
    const key = await cache.getActiveKey();
    expect(key.kid).toBe('dsk_test');

    cache.stop();
  });

  it('rejects start() when the initial load fails, leaving the cache empty', async () => {
    const { protector } = makeSignedKeyRow();
    const findFirst = jest.fn().mockResolvedValue(null);
    const mockPrisma = { delegatedSigningKey: { findFirst } };
    const cache = new SignerCache(mockPrisma as any, protector, 300_000, 0);

    const startPromise = cache.start();
    // Attach the rejection assertion before advancing timers: the reject
    // happens synchronously inside the timer flush below, and an
    // unhandled rejection at that point (even one later awaited) fails
    // the test under Jest's fake timers.
    const assertion = expect(startPromise).rejects.toThrow(
      'No ACTIVE signing key'
    );
    await jest.advanceTimersByTimeAsync(0);
    await assertion;
    await expect(cache.getActiveKey()).rejects.toThrow(
      SignerCacheNotLoadedError
    );
  });
});
