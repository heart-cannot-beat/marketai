/**
 * `StorageOriginPolicy` — the browser-only allowlist that
 * `BrowserBlobTransferClient` checks every storage grant destination
 * against before starting a transfer (spec §7.7, §7.14). Built from the
 * public, non-secret `NEXT_PUBLIC_MARKETAI_STORAGE_ORIGINS` configuration —
 * distinct from object-storage CORS, which separately permits `APP_URL` as
 * the browser request's *source* origin.
 */

export interface StorageOriginPolicy {
  assertAllowed(rawUrl: string): URL;
}

export class StorageOriginPolicyImpl implements StorageOriginPolicy {
  private readonly allowedOrigins: Set<string>;

  constructor(origins: string[]) {
    this.allowedOrigins = new Set(
      origins.map((origin) => new URL(origin).origin)
    );
  }

  /**
   * Compares the exact normalized origin (scheme, host, effective port) —
   * never a prefix or wildcard match — and rejects credentials and
   * fragments before a grant URL is handed to `fetch` or a navigation.
   */
  assertAllowed(rawUrl: string): URL {
    const url = new URL(rawUrl);
    if (url.username || url.password) {
      throw new Error('Storage URL must not contain credentials');
    }
    if (url.hash) {
      throw new Error('Storage URL must not contain a fragment');
    }
    if (!this.allowedOrigins.has(url.origin)) {
      throw new Error(`Storage origin not allowed: ${url.origin}`);
    }
    return url;
  }
}
