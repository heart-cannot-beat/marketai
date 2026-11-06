/**
 * `parseStorageOrigins` — validates `NEXT_PUBLIC_MARKETAI_STORAGE_ORIGINS`
 * (a comma-separated list) into an array of exact, normalized origins
 * (scheme + host + effective port) for two independent consumers: the CSP
 * `connect-src` directive built in `middleware.ts`, and
 * `StorageOriginPolicyImpl` (`storage-origin-policy.ts`), which
 * `BrowserBlobTransferClient` checks every storage grant destination
 * against before starting a transfer (spec §7.7, §7.14).
 *
 * Every entry must be an *origin* — no path, query, fragment, credentials,
 * or wildcard/unsafe scheme survives — and must never resolve to the same
 * origin as `DOMAIN_API_BASE_URL` (the FastAPI origin), which the browser
 * must never be told to `connect-src` directly (spec: browser calls only
 * go through the BFF via `DomainGateway`, never straight to FastAPI).
 */

const LOOPBACK = new Set(['localhost', '127.0.0.1', '[::1]']);

export function parseStorageOrigins(
  raw: string,
  options: { production: boolean; allowInsecureLoopback?: boolean }
): string[] {
  if (!raw.trim()) return [];

  const domainApiOrigin = process.env.DOMAIN_API_BASE_URL
    ? new URL(process.env.DOMAIN_API_BASE_URL).origin
    : null;

  return raw
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
    .map((entry) => {
      if (entry === '*') {
        throw new Error(
          'Wildcard origins are not allowed in NEXT_PUBLIC_MARKETAI_STORAGE_ORIGINS'
        );
      }
      if (entry === 'data:' || entry === 'blob:') {
        throw new Error(
          `Unsafe scheme source "${entry}" is not allowed in NEXT_PUBLIC_MARKETAI_STORAGE_ORIGINS`
        );
      }

      let url: URL;
      try {
        url = new URL(entry);
      } catch {
        throw new Error(`Invalid storage origin: "${entry}"`);
      }

      if (url.username || url.password) {
        throw new Error(
          `Credential in storage origin is not allowed: "${entry}"`
        );
      }

      if (url.pathname !== '/' || url.search || url.hash) {
        throw new Error(
          `Storage origin must not contain a path, query, or fragment: "${entry}"`
        );
      }

      const explicitlyAllowedLoopback =
        options.allowInsecureLoopback === true &&
        url.protocol === 'http:' &&
        LOOPBACK.has(url.hostname);

      if (
        options.production &&
        url.protocol !== 'https:' &&
        !explicitlyAllowedLoopback
      ) {
        throw new Error(
          `Production storage origins must use HTTPS: "${entry}"`
        );
      }

      if (
        !options.production &&
        url.protocol === 'http:' &&
        !LOOPBACK.has(url.hostname)
      ) {
        throw new Error(
          `HTTP storage origins are only allowed for loopback hosts in development: "${entry}"`
        );
      }

      const origin = url.origin;

      // `new URL('https://s3.example.com/').pathname` is also `'/'`, so the
      // pathname check above cannot by itself catch a trailing slash on an
      // otherwise-bare origin. Comparing the trimmed entry to the parsed
      // origin verbatim catches that (and any other content the URL parser
      // silently normalized away) in one place.
      if (entry !== origin) {
        throw new Error(
          `Storage origin must be an exact origin with no trailing slash, path, query, or fragment: "${entry}"`
        );
      }

      if (domainApiOrigin && origin === domainApiOrigin) {
        throw new Error(
          `Storage origin must not match the FastAPI origin (DOMAIN_API_BASE_URL): "${entry}"`
        );
      }

      return origin;
    });
}
