import type { PrismaClient } from '@prisma/client';
import type { AesGcmPrivateKeyProtector, KeyBinding } from './key-protector';

type CachedSigner = {
  kid: string;
  privateKeyPem: Buffer;
};

type StoredProtectedPrivateKey = {
  ciphertext: string;
  iv: string;
  authTag: string;
};

function toProtectedKey(stored: StoredProtectedPrivateKey) {
  return {
    ciphertext: Buffer.from(stored.ciphertext, 'base64'),
    iv: Buffer.from(stored.iv, 'base64'),
    authTag: Buffer.from(stored.authTag, 'base64'),
  };
}

/**
 * Thrown by `getActiveKey()` when the cache has never successfully loaded
 * (startup hasn't finished, or the initial load failed). Per spec §1.5
 * there is no "refresh on first request" fallback — the caller should
 * translate this into a 503 rather than block the request on a database
 * round-trip that every other concurrent request would also block on.
 */
export class SignerCacheNotLoadedError extends Error {
  constructor() {
    super('Signer cache not loaded');
    this.name = 'SignerCacheNotLoadedError';
  }
}

/**
 * In-memory cache of the decrypted ACTIVE signing key, per spec §1.5. Each
 * BFF instance:
 *
 * - Waits a random jitter delay before its first load, so a fleet restart
 *   does not send every instance to the database at once.
 * - Refreshes on a background timer independent of request traffic, so an
 *   idle instance still picks up a rotated key before the old key's
 *   `verify_until` expires.
 * - Coalesces concurrent refreshes (timer- or start-triggered) into a
 *   single Prisma read via a shared in-flight `Promise`, so a slow refresh
 *   never causes duplicate database round-trips.
 *
 * `getActiveKey()` never triggers a load itself — it only ever returns
 * what is already cached, or throws `SignerCacheNotLoadedError`. This is
 * deliberate: a "refresh on first request" fallback would make every
 * concurrent request during a cold cache block on one database call.
 */
export class SignerCache {
  private cached: CachedSigner | null = null;
  private inflightRefresh: Promise<CachedSigner> | null = null;
  private refreshTimer: ReturnType<typeof setInterval> | null = null;

  constructor(
    private readonly prisma: PrismaClient,
    private readonly protector: AesGcmPrivateKeyProtector,
    private readonly refreshIntervalMs: number,
    private readonly jitterMaxMs: number
  ) {}

  /**
   * Waits a random 0–`jitterMaxMs` delay, performs the first load, then
   * starts the background refresh timer. Rejects if the first load fails
   * (there is nothing cached yet, and no fallback); callers should treat
   * that as fatal to startup or retry `start()`.
   */
  async start(): Promise<void> {
    const jitter = Math.random() * this.jitterMaxMs;
    await new Promise<void>((resolve) => setTimeout(resolve, jitter));
    await this.refresh();
    this.refreshTimer = setInterval(() => {
      this.refresh().catch(() => {
        // A failed background refresh leaves the previous cached key in
        // place; the next timer tick tries again. There is nothing useful
        // to do with the error here — it is not on any caller's stack.
      });
    }, this.refreshIntervalMs);
  }

  /** Stops the background refresh timer. Does not clear the cached key. */
  stop(): void {
    if (this.refreshTimer) {
      clearInterval(this.refreshTimer);
      this.refreshTimer = null;
    }
  }

  /**
   * Returns the cached signing key. Throws `SignerCacheNotLoadedError` if
   * `start()` has not yet completed its first load — never blocks on a
   * refresh (see class doc). Async to match the shape of every other
   * signer-facing accessor in this module, even though the cache lookup
   * itself is synchronous.
   */
  async getActiveKey(): Promise<CachedSigner> {
    if (!this.cached) {
      throw new SignerCacheNotLoadedError();
    }
    return this.cached;
  }

  /**
   * Coalesces concurrent refresh requests into one in-flight Prisma read:
   * a second caller while a refresh is already running awaits the first
   * caller's `Promise` instead of starting a new database round-trip.
   */
  private async refresh(): Promise<CachedSigner> {
    if (this.inflightRefresh) {
      return this.inflightRefresh;
    }

    this.inflightRefresh = this.doRefresh();
    try {
      const result = await this.inflightRefresh;
      this.cached = result;
      return result;
    } finally {
      this.inflightRefresh = null;
    }
  }

  private async doRefresh(): Promise<CachedSigner> {
    const activeKey = await this.prisma.delegatedSigningKey.findFirst({
      where: { state: 'ACTIVE' },
    });
    if (!activeKey) {
      throw new Error('No ACTIVE signing key');
    }

    const binding: KeyBinding = {
      kid: activeKey.kid,
      algorithm: activeKey.algorithm,
      publicJwkThumbprint: activeKey.publicJwkThumbprint,
    };
    const stored =
      activeKey.protectedPrivateKey as unknown as StoredProtectedPrivateKey;
    const privateKeyPem = this.protector.unprotect(
      toProtectedKey(stored),
      binding
    );

    return { kid: activeKey.kid, privateKeyPem };
  }
}
