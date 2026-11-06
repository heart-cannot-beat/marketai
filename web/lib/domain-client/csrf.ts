/**
 * Browser-only CSRF token cache (spec §6.9).
 *
 * `GET /api/csrf` identifies the caller from their HttpOnly session cookie
 * and returns a token bound to that session (`web/pages/api/csrf.ts`). This
 * manager fetches that token once, caches it in memory (never
 * `localStorage` — spec §6.9) for `ttlMs`, and coalesces concurrent callers
 * into a single in-flight fetch so a burst of mutating requests doesn't
 * trigger a burst of token fetches.
 *
 * `fetchFn` defaults to the real same-origin `fetch("/api/csrf")` call, but
 * is overridable so tests can drive it with an in-memory transport
 * (`MemoryTransport` in `__tests__/domain-client/csrf.spec.ts`) instead of a
 * network stub.
 */

type CsrfFetchResponse = {
  ok: boolean;
  status: number;
  json: () => Promise<{ token: string }>;
};

type CsrfConfig = {
  ttlMs: number;
  fetchFn?: () => Promise<CsrfFetchResponse>;
};

const defaultFetchFn = (): Promise<CsrfFetchResponse> =>
  fetch('/api/csrf', {
    method: 'GET',
    credentials: 'same-origin',
  });

/** A failed preflight; the domain command has not been sent. */
export class CsrfTokenFetchError extends Error {
  constructor(readonly status: number) {
    super(`CSRF token fetch failed: ${status}`);
    this.name = 'CsrfTokenFetchError';
  }
}

export class CsrfTokenManager {
  private cachedToken: string | null = null;
  private cachedAt = 0;
  private inflight: Promise<string> | null = null;
  private readonly ttlMs: number;
  private readonly fetchFn: () => Promise<CsrfFetchResponse>;

  constructor(config: CsrfConfig) {
    this.ttlMs = config.ttlMs;
    this.fetchFn = config.fetchFn ?? defaultFetchFn;
  }

  async getToken(): Promise<string> {
    if (this.cachedToken !== null && Date.now() - this.cachedAt < this.ttlMs) {
      return this.cachedToken;
    }

    // Single-flight: concurrent callers coalesce into one fetch.
    if (this.inflight !== null) {
      return this.inflight;
    }

    this.inflight = this.doFetch();
    try {
      const token = await this.inflight;
      this.cachedToken = token;
      this.cachedAt = Date.now();
      return token;
    } finally {
      this.inflight = null;
    }
  }

  /** Drops the cached token. Called on logout and on a 401 from any BFF call. */
  clear(): void {
    this.cachedToken = null;
    this.cachedAt = 0;
  }

  private async doFetch(): Promise<string> {
    const response = await this.fetchFn();
    if (!response.ok) {
      throw new CsrfTokenFetchError(response.status);
    }
    const data = await response.json();
    return data.token;
  }
}
