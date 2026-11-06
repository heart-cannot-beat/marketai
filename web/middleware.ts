import micromatch from 'micromatch';
import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';

import env from './lib/env';
import { parseStorageOrigins } from './lib/domain-client/storage-origin-parser';
import { authLoginRedirectUrl } from './lib/auth/redirect-url';
import {
  DEFAULT_LOCALE,
  LOCALE_COOKIE,
  isSupportedLocale,
  localeCookieOptions,
  localizedPath,
  resolveLocale,
} from './lib/i18n/locales';

/**
 * Security headers applied to every response, authenticated or not.
 *
 * Upstream applied these only after the authentication check, so unauthenticated
 * routes — including the sign-in and invitation pages — were served without
 * them, and the whole set was off unless SECURITY_HEADERS_ENABLED was set.
 */
export const SECURITY_HEADERS = {
  'Referrer-Policy': 'strict-origin-when-cross-origin',
  'Permissions-Policy':
    'geolocation=(), microphone=(), camera=(), payment=(), usb=()',
  'Cross-Origin-Embedder-Policy': 'require-corp',
  'Cross-Origin-Opener-Policy': 'same-origin',
  'Cross-Origin-Resource-Policy': 'same-origin',
  'X-Frame-Options': 'DENY',
  'X-Content-Type-Options': 'nosniff',
  'Strict-Transport-Security': 'max-age=31536000; includeSubDomains',
} as const;

/** Per-request nonce for the scripts Next.js emits. */
export const generateNonce = (): string => {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return btoa(String.fromCharCode(...Array.from(bytes)));
};

/**
 * Content Security Policy.
 *
 * Upstream allowed boxyhq.com, *.dicebear.com, *.google.com, *.gstatic.com,
 * *.ingest.sentry.io, *.mixpanel.com and both `'unsafe-inline'` and
 * `'unsafe-eval'` for scripts in production. The third-party origins belonged
 * to reCAPTCHA, Sentry and Mixpanel, all of which this fork removed, so the
 * policy is same-origin only, and production `script-src` carries a per-request
 * nonce instead of `'unsafe-inline'`.
 *
 * `style-src` keeps `'unsafe-inline'`: Next.js and daisyUI emit inline style
 * attributes that no nonce can cover, and inline style is not script execution.
 */
export const generateCSP = (
  nonce: string,
  isProduction = process.env.NODE_ENV === 'production',
  upgradeInsecureRequests = true
): string => {
  // `connect-src` is `'self'` plus the validated storage origins the browser
  // is allowed to POST/PUT/GET signed blob-transfer URLs against
  // (`BrowserBlobTransferClient`, spec §7.7, §7.14) — never `DOMAIN_API_BASE_URL`
  // (the FastAPI origin) or a wildcard scheme. `parseStorageOrigins` is the
  // single source of truth this shares with `StorageOriginPolicyImpl`; a
  // misconfigured env var fails closed (no storage origins added) rather
  // than widening the policy or crashing every response.
  let storageOrigins: string[] = [];
  try {
    storageOrigins = parseStorageOrigins(
      process.env.NEXT_PUBLIC_MARKETAI_STORAGE_ORIGINS ?? '',
      {
        production: isProduction,
        allowInsecureLoopback:
          process.env.NEXT_PUBLIC_MARKETAI_ALLOW_INSECURE_LOOPBACK_STORAGE ===
          'true',
      }
    );
  } catch (err) {
    // eslint-disable-next-line no-console
    console.error('Invalid NEXT_PUBLIC_MARKETAI_STORAGE_ORIGINS:', err);
  }

  const policies: Record<string, string[]> = {
    'default-src': ["'self'"],
    'img-src': ["'self'", 'data:', 'blob:'],
    // 'unsafe-eval' is required by the development bundler only and is never
    // emitted in production.
    'script-src': isProduction
      ? ["'self'", `'nonce-${nonce}'`]
      : ["'self'", "'unsafe-inline'", "'unsafe-eval'"],
    'style-src': ["'self'", "'unsafe-inline'"],
    'connect-src': ["'self'", ...storageOrigins],
    'frame-src': ["'none'"],
    'font-src': ["'self'"],
    'object-src': ["'none'"],
    'base-uri': ["'self'"],
    'form-action': ["'self'"],
    'frame-ancestors': ["'none'"],
    'worker-src': ["'self'", 'blob:'],
  };

  return Object.entries(policies)
    .map(([key, values]) => `${key} ${values.join(' ')}`)
    .concat(upgradeInsecureRequests ? ['upgrade-insecure-requests'] : [])
    .join('; ');
};

/**
 * `upgrade-insecure-requests` is meaningful only when the document itself was
 * requested over HTTPS. Emitting it for an intentional HTTP deployment (for
 * example the isolated Compose acceptance network) upgrades Next.js assets to
 * an HTTPS listener that does not exist and prevents the page from hydrating.
 */
export const requestUsesHttps = (req: NextRequest): boolean => {
  const forwardedProtocol = req.headers
    .get('x-forwarded-proto')
    ?.split(',')[0]
    ?.trim()
    .toLowerCase();

  return forwardedProtocol
    ? forwardedProtocol === 'https'
    : req.nextUrl.protocol === 'https:';
};

/**
 * Routes reachable without a session.
 *
 * Upstream also exposed `/api/oauth/**`, `/api/scim/v2.0/**`,
 * `/api/webhooks/stripe`, `/api/webhooks/dsync` and `/login/saml`. Those
 * handlers no longer exist in this fork; the allowlist is trimmed so a future
 * file added at one of those paths is not silently public.
 *
 * `/.well-known/jwks.json` (spec §1.4) is the one deliberate exception: it
 * publishes only public signing keys for FastAPI to verify delegated JWTs,
 * and FastAPI has no session of its own to present.
 */
export const unAuthenticatedRoutes = [
  '/api/health',
  '/api/readiness',
  '/api/auth/**',
  '/api/invitations/*',
  '/auth/**',
  '/invitations/*',
  '/.well-known/jwks.json',
  // Next rewrites the public well-known path to the Pages Router API route
  // before middleware evaluates the request in production.
  '/api/.well-known/jwks',
];

/**
 * Routes that are NOT public but also must not go through the middleware's
 * session-fetch redirect: they call `requireSession` themselves and return a
 * JSON 401 on failure rather than redirecting to `/auth/login` (spec §4.2).
 * Deliberately kept separate from `unAuthenticatedRoutes` so a route landing
 * here still reads as "authenticated" to anyone scanning this file — it is
 * just authenticated inside the handler instead of by the middleware.
 */
export const apiSelfAuthenticatedRoutes = ['/api/domain/**', '/api/csrf'];

/**
 * Routes authenticated by a shared service token rather than a browser
 * session or the delegated-JWT seam — FastAPI's projection sync calling
 * `/api/internal/domain/workspaces/{workspaceId}/authorization-snapshot`
 * (v1, spec §3.10) or `.../workspace-projection-snapshot` (v2 pull-repair,
 * spec §1.14) holds no session cookie to present; both share the same
 * `PROJECTION_SYNC_SERVICE_TOKEN`. The `/api/internal/**` wildcard already
 * covers both — and any future route under this prefix — without a
 * per-route entry. Kept separate from `apiSelfAuthenticatedRoutes` so the
 * distinction between "authenticates its own browser session" and
 * "authenticates a trusted backend caller" stays visible to anyone scanning
 * this file; both bypass the middleware's session-redirect gate and both
 * still receive security headers and CSP.
 */
export const apiServiceAuthenticatedRoutes = ['/api/internal/**'];

const applySecurityHeaders = (response: NextResponse, csp: string) => {
  if (!env.securityHeadersEnabled) {
    return response;
  }

  response.headers.set('Content-Security-Policy', csp);

  Object.entries(SECURITY_HEADERS).forEach(([key, value]) => {
    if (
      key === 'Strict-Transport-Security' &&
      !env.appUrl.startsWith('https://')
    ) {
      return;
    }

    response.headers.set(key, value);
  });

  return response;
};

/**
 * Whether a request renders a page, as opposed to an API call, a public key
 * document or the JSON a client-side navigation fetches (`/_next/data`, sent
 * with `x-nextjs-data`). Only page requests are moved to another locale: a
 * client-side navigation already carries the locale of the page it started
 * from, and a redirect there would break the transition instead of
 * reloading it.
 */
const isPageRequest = (req: NextRequest) => {
  const { pathname } = req.nextUrl;
  return (
    (req.method === 'GET' || req.method === 'HEAD') &&
    pathname !== '/api' &&
    !pathname.startsWith('/api/') &&
    !pathname.startsWith('/.well-known/') &&
    !req.headers.has('x-nextjs-data') &&
    !req.nextUrl.buildId
  );
};

/** The interface language this request should be served in (see `resolveLocale`). */
const requestLocale = (req: NextRequest, userLocale?: string | null) =>
  resolveLocale({
    userLocale,
    cookieLocale: req.cookies.get(LOCALE_COOKIE)?.value,
    acceptLanguage: req.headers.get('accept-language'),
  });

/**
 * Next.js only detects the locale on `/`. Every other page is moved here to
 * the signed-in user's saved language, else the language cookie, else the
 * browser's, so a refresh, a deep link or a new sign-in all land in the same
 * language. The saved preference is also written to the cookie, which keeps
 * the unauthenticated pages (sign-in, invitations) in that language after
 * sign-out.
 */
const localeRedirect = (
  req: NextRequest,
  userLocale?: string | null
): NextResponse | null => {
  if (!isPageRequest(req)) {
    return null;
  }

  const target = requestLocale(req, userLocale);
  if (target === (req.nextUrl.locale || DEFAULT_LOCALE)) {
    return null;
  }

  const response = NextResponse.redirect(
    new URL(
      localizedPath(target, `${req.nextUrl.pathname}${req.nextUrl.search}`),
      env.appUrl
    )
  );
  if (isSupportedLocale(userLocale)) {
    response.cookies.set(
      LOCALE_COOKIE,
      userLocale,
      localeCookieOptions(env.appUrl.startsWith('https://'))
    );
  }
  return response;
};

export default async function middleware(req: NextRequest) {
  const { pathname } = req.nextUrl;
  const nonce = generateNonce();
  const csp = generateCSP(
    nonce,
    process.env.NODE_ENV === 'production',
    requestUsesHttps(req)
  );

  const withNonce = () => {
    const requestHeaders = new Headers(req.headers);
    requestHeaders.set('x-nonce', nonce);
    requestHeaders.set('Content-Security-Policy', csp);
    return NextResponse.next({ request: { headers: requestHeaders } });
  };

  // Bypass routes that don't require authentication. With i18n enabled
  // `pathname` arrives without its locale prefix, so `/zh-CN/auth/login`
  // matches `/auth/**` too.
  if (micromatch.isMatch(pathname, unAuthenticatedRoutes)) {
    return applySecurityHeaders(localeRedirect(req) ?? withNonce(), csp);
  }

  // These routes authenticate themselves via `requireSession` inside the
  // handler (401 JSON, not a redirect) — skip the middleware's redirect gate
  // but still apply security headers and CSP.
  if (micromatch.isMatch(pathname, apiSelfAuthenticatedRoutes)) {
    return applySecurityHeaders(withNonce(), csp);
  }

  // These routes authenticate a trusted backend caller via a shared service
  // token checked inside the handler — there is no browser session to look
  // up, so the middleware's session-redirect gate would only ever fail them.
  if (micromatch.isMatch(pathname, apiServiceAuthenticatedRoutes)) {
    return applySecurityHeaders(withNonce(), csp);
  }

  /**
   * Authorization is a server-side session lookup, not a signature check.
   *
   * Upstream trusted `getToken()` alone under the JWT strategy, so a token
   * stayed valid for its full lifetime after sign-out or a password change.
   * `/api/auth/session` runs in the Node runtime, where the session callback
   * re-reads the session row named by the token's `sid` claim and returns
   * nothing when it has been revoked. That endpoint is excluded from this
   * matcher, so there is no recursion.
   */
  // Railway may expose an internal origin such as localhost in `req.url`.
  // Resolve the session endpoint against the configured app origin so the
  // authentication gate reaches the same deployed service as the browser.
  const sessionUrl = new URL('/api/auth/session', env.appUrl);

  let session: { user?: { locale?: string | null } } | null = null;

  try {
    const response = await fetch(sessionUrl, {
      headers: {
        'Content-Type': 'application/json',
        cookie: req.headers.get('cookie') || '',
      },
    });

    session = response.ok ? await response.json() : null;
  } catch {
    // Fail closed: an unreachable session endpoint is not an authorization.
    session = null;
  }

  if (!session?.user) {
    const redirectUrl = authLoginRedirectUrl(
      env.appUrl,
      req.nextUrl.pathname,
      req.nextUrl.search
    );
    // Straight to the sign-in page in the visitor's language, rather than
    // through a second redirect from `/auth/login`.
    redirectUrl.pathname = localizedPath(
      requestLocale(req),
      redirectUrl.pathname
    );

    return applySecurityHeaders(NextResponse.redirect(redirectUrl), csp);
  }

  return applySecurityHeaders(
    localeRedirect(req, session.user.locale) ?? withNonce(),
    csp
  );
}

/**
 * Paths the middleware does not run on.
 *
 * Upstream excluded only `_next/static`, `_next/image`, `favicon.ico` and the
 * session endpoint, which left everything in `public/` behind the
 * authentication redirect: the image optimizer fetched the sign-in HTML instead
 * of the PNG and answered 400.
 *
 * The exclusions are the exact file names served from `public/`, not a file
 * extension pattern. An extension pattern would also let through any dynamic or
 * API path that happened to end in `.png` or `.txt`.
 * `__tests__/security/headers.spec.ts` cross-checks this list against the real
 * contents of `public/`, so adding a file there without updating this line
 * fails the build.
 *
 * Next.js parses this object statically, so the pattern has to be a literal.
 */
export const config = {
  matcher: [
    '/((?!_next/static|_next/image|favicon\\.ico|api/auth/session|logo\\.png|logowhite\\.png|user-default-profile\\.jpeg).*)',
  ],
};
