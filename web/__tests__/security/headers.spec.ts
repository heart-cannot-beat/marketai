import fs from 'fs';
import path from 'path';

import micromatch from 'micromatch';

import {
  SECURITY_HEADERS,
  generateCSP,
  generateNonce,
  unAuthenticatedRoutes,
  apiSelfAuthenticatedRoutes,
  apiServiceAuthenticatedRoutes,
} from '../../middleware';
import env from '@/lib/env';

/**
 * Next.js requires the matcher to be a static literal inside `config`, so it is
 * read back out of the source rather than imported.
 */
const readMatcher = (): string => {
  const source = fs.readFileSync(
    path.join(__dirname, '..', '..', 'middleware.ts'),
    'utf8'
  );

  const match = source.match(/matcher: \[\s*'([^']+)'/);

  if (!match) {
    throw new Error('middleware matcher literal not found');
  }

  return match[1].replace(/\\\\/g, '\\');
};

describe('Security headers', () => {
  it('is enabled by default', () => {
    expect(env.securityHeadersEnabled).toBe(true);
  });

  it('sets the expected header set', () => {
    expect(SECURITY_HEADERS['X-Frame-Options']).toBe('DENY');
    expect(SECURITY_HEADERS['X-Content-Type-Options']).toBe('nosniff');
    expect(SECURITY_HEADERS['Referrer-Policy']).toBe(
      'strict-origin-when-cross-origin'
    );
    expect(SECURITY_HEADERS['Strict-Transport-Security']).toContain(
      'max-age=31536000'
    );
    expect(SECURITY_HEADERS['Cross-Origin-Opener-Policy']).toBe('same-origin');
    expect(SECURITY_HEADERS['Cross-Origin-Resource-Policy']).toBe(
      'same-origin'
    );
  });
});

describe('Content Security Policy', () => {
  const nonce = 'TEST-NONCE-VALUE';
  const production = generateCSP(nonce, true);
  const development = generateCSP(nonce, false);

  it('is same-origin by default and frames nothing', () => {
    expect(production).toContain("default-src 'self'");
    expect(production).toContain("frame-ancestors 'none'");
    expect(production).toContain("object-src 'none'");
    expect(production).toContain("connect-src 'self'");
    expect(production).toContain('upgrade-insecure-requests');
  });

  it('does not upgrade assets for an intentionally HTTP document', () => {
    const httpDeployment = generateCSP(nonce, true, false);

    expect(httpDeployment).not.toContain('upgrade-insecure-requests');
    expect(httpDeployment).toContain(
      "script-src 'self' 'nonce-TEST-NONCE-VALUE'"
    );
  });

  it('drops every third-party origin upstream allowed', () => {
    for (const origin of [
      'boxyhq.com',
      'dicebear.com',
      'google.com',
      'gstatic.com',
      'sentry.io',
      'mixpanel.com',
    ]) {
      expect(production).not.toContain(origin);
    }
  });

  it('never allows unsafe-eval in production', () => {
    expect(production).not.toContain("'unsafe-eval'");
    // The dev bundler needs it; that path is not shipped.
    expect(development).toContain("'unsafe-eval'");
  });

  it('uses a per-request nonce instead of unsafe-inline for scripts', () => {
    const scriptSrc = production
      .split('; ')
      .find((directive) => directive.startsWith('script-src'));

    expect(scriptSrc).toBe(`script-src 'self' 'nonce-${nonce}'`);
    expect(scriptSrc).not.toContain("'unsafe-inline'");
  });

  it('still allows inline style, which is not script execution', () => {
    expect(production).toContain("style-src 'self' 'unsafe-inline'");
  });

  it('mints a fresh, unpredictable nonce per request', () => {
    const nonces = new Set(Array.from({ length: 50 }, () => generateNonce()));

    expect(nonces.size).toBe(50);
    for (const value of Array.from(nonces)) {
      expect(value.length).toBeGreaterThanOrEqual(16);
    }
  });
});

describe('Document', () => {
  it('passes the nonce to every script Next.js emits', () => {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const fs = require('fs');
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const path = require('path');

    const document = fs.readFileSync(
      path.join(__dirname, '..', '..', 'pages', '_document.tsx'),
      'utf8'
    );

    expect(document).toContain("ctx.req?.headers['x-nonce']");
    expect(document).toContain('<Head {...nonceProp} />');
    expect(document).toContain('<NextScript {...nonceProp} />');
    // Never `nonce=""` in statically generated output.
    expect(document).toContain('const nonceProp = nonce ? { nonce } : {}');
  });
});

describe('Middleware matcher', () => {
  const matcher = readMatcher();
  const matches = (pathname: string) =>
    new RegExp(`^${matcher}$`).test(pathname);

  it('runs on application routes', () => {
    for (const pathname of [
      '/',
      '/dashboard',
      '/teams',
      '/teams/acme/settings',
      '/api/teams',
      '/api/teams/acme/members',
    ]) {
      expect(matches(pathname)).toBe(true);
    }
  });

  it('excludes exactly the files served from public/', () => {
    // Cross-checked against the real directory: adding a file to public/
    // without excluding it here fails this test rather than 400-ing at runtime.
    const publicFiles = fs
      .readdirSync(path.join(__dirname, '..', '..', 'public'))
      .filter((name) => !name.startsWith('.'));

    expect(publicFiles.length).toBeGreaterThan(0);

    for (const name of publicFiles) {
      expect(matches(`/${name}`)).toBe(false);
    }

    expect(matches('/favicon.ico')).toBe(false);
  });

  it('does not exempt a path merely because of its file extension', () => {
    // An extension-based exclusion would let these through unauthenticated.
    for (const pathname of [
      '/api/teams/acme/export.txt',
      '/teams/acme/report.png',
      '/api/users.txt',
    ]) {
      expect(matches(pathname)).toBe(true);
    }
  });

  it('still gates anything that is not a public asset', () => {
    expect(matches('/teams/acme/members')).toBe(true);
    expect(matches('/api/users')).toBe(true);
  });

  it('keeps the unauthenticated allowlist minimal', () => {
    expect(unAuthenticatedRoutes).toEqual([
      '/api/health',
      '/api/readiness',
      '/api/auth/**',
      '/api/invitations/*',
      '/auth/**',
      '/invitations/*',
      '/.well-known/jwks.json',
      '/api/.well-known/jwks',
    ]);
  });

  it('runs the middleware matcher over the JWKS well-known path', () => {
    // The route must still pass through `middleware.ts` — so it gets the
    // security headers — and be let through only via the allowlist below,
    // not via a matcher exclusion.
    expect(matches('/.well-known/jwks.json')).toBe(true);
  });

  it('bypasses authentication for the JWKS well-known path (spec §1.4)', () => {
    expect(
      micromatch.isMatch('/.well-known/jwks.json', unAuthenticatedRoutes)
    ).toBe(true);
    expect(
      micromatch.isMatch('/api/.well-known/jwks', unAuthenticatedRoutes)
    ).toBe(true);
  });

  it('does not accidentally open the rest of /.well-known/', () => {
    expect(
      micromatch.isMatch('/.well-known/other-file.txt', unAuthenticatedRoutes)
    ).toBe(false);
  });
});

/**
 * `/api/csrf` and `/api/domain/**` are not public (spec §4.2): they call
 * `requireSession` themselves and answer 401 rather than being reachable
 * without a session at all. They still need the middleware to skip its
 * redirect-based gate — that's `apiSelfAuthenticatedRoutes`, a list kept
 * separate from `unAuthenticatedRoutes` on purpose (see middleware.ts).
 */
describe('Self-authenticated API routes', () => {
  const matcher = readMatcher();
  const matches = (pathname: string) =>
    new RegExp(`^${matcher}$`).test(pathname);

  it('lists exactly the domain proxy and the CSRF endpoint', () => {
    expect(apiSelfAuthenticatedRoutes).toEqual(['/api/domain/**', '/api/csrf']);
  });

  it('matches /api/csrf and /api/domain/** paths', () => {
    expect(micromatch.isMatch('/api/csrf', apiSelfAuthenticatedRoutes)).toBe(
      true
    );
    expect(
      micromatch.isMatch(
        '/api/domain/v1/w/acme/campaigns',
        apiSelfAuthenticatedRoutes
      )
    ).toBe(true);
  });

  it('is disjoint from the public allowlist', () => {
    // These routes authenticate themselves in-handler; they must never also
    // be reachable through the fully-public bypass.
    for (const route of apiSelfAuthenticatedRoutes) {
      expect(micromatch.isMatch(route, unAuthenticatedRoutes)).toBe(false);
    }
  });

  it('still runs the middleware matcher over /api/csrf', () => {
    expect(matches('/api/csrf')).toBe(true);
  });
});

/**
 * `/api/internal/**` (spec §3.10) is authenticated by a shared service token
 * the handler checks itself — FastAPI's projection sync has no browser
 * session to present. `apiServiceAuthenticatedRoutes` is kept separate from
 * `apiSelfAuthenticatedRoutes` so the two authentication models (browser
 * session vs. trusted backend caller) stay visually distinct in
 * middleware.ts.
 */
describe('Service-authenticated API routes', () => {
  const matcher = readMatcher();
  const matches = (pathname: string) =>
    new RegExp(`^${matcher}$`).test(pathname);

  it('lists exactly the internal domain namespace', () => {
    expect(apiServiceAuthenticatedRoutes).toEqual(['/api/internal/**']);
  });

  it('matches nested /api/internal/** paths', () => {
    expect(
      micromatch.isMatch(
        '/api/internal/domain/workspaces/team_1/authorization-snapshot',
        apiServiceAuthenticatedRoutes
      )
    ).toBe(true);
  });

  it('is disjoint from the public allowlist', () => {
    // A service-token-authenticated route must never also be reachable
    // through the fully-public bypass.
    for (const route of apiServiceAuthenticatedRoutes) {
      expect(micromatch.isMatch(route, unAuthenticatedRoutes)).toBe(false);
    }
  });

  it('is disjoint from the self-authenticated (session) routes', () => {
    for (const route of apiServiceAuthenticatedRoutes) {
      expect(micromatch.isMatch(route, apiSelfAuthenticatedRoutes)).toBe(false);
    }
  });

  it('still runs the middleware matcher over /api/internal/** paths', () => {
    expect(
      matches('/api/internal/domain/workspaces/team_1/authorization-snapshot')
    ).toBe(true);
  });
});

/**
 * #15 accepts the tracer on the basis that security headers are asserted, so
 * the switch that turns them off must not be reachable in production. The
 * comment in lib/env.ts already claimed this; these cases make it true.
 */
describe('Security headers cannot be disabled in production', () => {
  const originalNodeEnv = process.env.NODE_ENV;
  const originalFlag = process.env.SECURITY_HEADERS_ENABLED;

  // Next.js types NODE_ENV as read-only; tests legitimately need to vary it.
  const mutableEnv = process.env as Record<string, string | undefined>;

  /** Re-evaluates lib/env.ts against a given environment. */
  const loadEnv = async (nodeEnv: string, flag: string | undefined) => {
    mutableEnv.NODE_ENV = nodeEnv;
    if (flag === undefined) {
      delete process.env.SECURITY_HEADERS_ENABLED;
    } else {
      process.env.SECURITY_HEADERS_ENABLED = flag;
    }

    let loaded: typeof env | undefined;
    await jest.isolateModulesAsync(async () => {
      loaded = (await import('../../lib/env')).default;
    });
    return loaded as typeof env;
  };

  afterEach(() => {
    mutableEnv.NODE_ENV = originalNodeEnv;
    if (originalFlag === undefined) {
      delete process.env.SECURITY_HEADERS_ENABLED;
    } else {
      process.env.SECURITY_HEADERS_ENABLED = originalFlag;
    }
  });

  it('refuses to boot production with SECURITY_HEADERS_ENABLED=false', async () => {
    await expect(loadEnv('production', 'false')).rejects.toThrow(
      /SECURITY_HEADERS_ENABLED=false is refused in production/
    );
  });

  it('is on in production when the variable is absent', async () => {
    expect(
      (await loadEnv('production', undefined)).securityHeadersEnabled
    ).toBe(true);
  });

  it('is on in production when the variable is explicitly true', async () => {
    expect((await loadEnv('production', 'true')).securityHeadersEnabled).toBe(
      true
    );
  });

  it('still allows development to turn them off', async () => {
    expect((await loadEnv('development', 'false')).securityHeadersEnabled).toBe(
      false
    );
  });

  it('defaults to on outside production too', async () => {
    expect(
      (await loadEnv('development', undefined)).securityHeadersEnabled
    ).toBe(true);
  });
});
