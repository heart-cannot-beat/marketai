import http from 'node:http';
import type { AddressInfo } from 'node:net';
import { createMocks } from 'node-mocks-http';
import type { NextApiRequest, NextApiResponse } from 'next';

import { DomainProxy } from '../../../../../lib/domain-client/proxy';
import { OPERATION_REGISTRY } from '../../../../../lib/domain-client/generated/operation-registry';
import { RESPONSE_VALIDATORS } from '../../../../../lib/domain-client/generated/validators';
import {
  ApiError,
  type DelegatedTokenIssuer,
} from '../../../../../lib/delegated-identity/token-issuer';
import type { AuthenticatedSession } from '../../../../../lib/requireSession';
import type { CsrfVerifier } from '../../../../../lib/delegated-identity/csrf-verifier';

jest.mock('../../../../../lib/requireSession', () => ({
  requireSession: jest.fn(),
}));
jest.mock('../../../../../lib/delegated-identity/instances', () => ({
  getTokenIssuer: jest.fn(),
  getCsrfVerifier: jest.fn(),
}));
jest.mock('../../../../../lib/domain-client/instances', () => ({
  getDomainProxy: jest.fn(),
}));
jest.mock('../../../../../lib/prisma', () => ({
  prisma: {
    team: {
      findUnique: jest.fn().mockResolvedValue({
        id: 'ws_1',
        lifecycle: 'ACTIVE',
      }),
    },
  },
}));

import { requireSession } from '../../../../../lib/requireSession';
import {
  getTokenIssuer,
  getCsrfVerifier,
} from '../../../../../lib/delegated-identity/instances';
import { getDomainProxy } from '../../../../../lib/domain-client/instances';
import handler, {
  config,
} from '../../../../../pages/api/domain/v1/w/[slug]/[...path]';

/**
 * Milestone 1 vertical-tracer integration test (spec §8.1, §2.10):
 *
 *   login -> session -> membership -> ES256 JWT -> BFF -> FastAPI verify ->
 *   TenantContext -> RLS -> identity probe
 *
 * `requireSession` and the delegated-identity singletons (`getTokenIssuer`,
 * `getCsrfVerifier`) are stubbed — they depend on Prisma and a real ES256
 * signing key, which are Task 6/8's concern and already covered by their
 * own unit suites. Everything downstream of session/token issuance is
 * real: `matchAllowlistEntry` runs against the real generated
 * `DOMAIN_ALLOWLIST`, and `DomainProxy` runs against the real generated
 * `OPERATION_REGISTRY`/`RESPONSE_VALIDATORS` — the exact artifacts
 * `scripts/generate_all.sh` produces from
 * `contracts/openapi/marketai-domain-v1.json` — against a real HTTP server
 * standing in for FastAPI. This is what proves the OpenAPI contract, the
 * generation pipeline, and the BFF handler actually fit together, not just
 * that each compiles in isolation.
 */

type TestServer = { url: string; close: () => Promise<void> };

function startServer(listener: http.RequestListener): Promise<TestServer> {
  return new Promise((resolve) => {
    const server = http.createServer(listener);
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address() as AddressInfo;
      resolve({
        url: `http://127.0.0.1:${port}`,
        close: () => new Promise((res) => server.close(() => res())),
      });
    });
  });
}

const SESSION: AuthenticatedSession = {
  sessionId: 'sess_1',
  user: { id: 'user_1', email: 'owner@example.com' },
};

const APP_HOST = new URL(process.env.APP_URL as string).host;
const APP_ORIGIN = new URL(process.env.APP_URL as string).origin;

/**
 * `node-mocks-http`'s mock request has no body of its own — its async
 * iterator only resolves once something calls `req.send()`. The handler's
 * `assertNoRequestBody` (spec §4.5) doesn't reach that iteration until
 * after several internal awaits (session, CSRF), so `req.send()` cannot be
 * called eagerly; `req.on('async_iterator', ...)` is node-mocks-http's own
 * signal that a `for await` loop has just started consuming the stream —
 * exactly the point at which it's safe to end it with no bytes.
 */
function mockGetRequest(
  options: Parameters<typeof createMocks<NextApiRequest, NextApiResponse>>[0]
) {
  const mocks = createMocks<NextApiRequest, NextApiResponse>(options);
  mocks.req.on('async_iterator', () => mocks.req.send());
  return mocks;
}

function mockJsonRequest(
  options: Parameters<typeof createMocks<NextApiRequest, NextApiResponse>>[0],
  body: unknown
) {
  const serialized = JSON.stringify(body);
  const mocks = createMocks<NextApiRequest, NextApiResponse>({
    ...options,
    headers: {
      ...(options?.headers ?? {}),
      'content-type': 'application/json',
      'content-length': String(Buffer.byteLength(serialized)),
    },
  });
  mocks.req.on('async_iterator', () => mocks.req.send(Buffer.from(serialized)));
  return mocks;
}

function fakeTokenIssuer(token = 'signed.jwt.token'): DelegatedTokenIssuer {
  return { issueForRequest: jest.fn().mockResolvedValue(token) };
}

const PASSTHROUGH_CSRF_VERIFIER: CsrfVerifier = {
  verify: jest.fn().mockResolvedValue(undefined),
};

describe('BFF domain proxy catch-all — tracer integration (spec §8.1)', () => {
  let server: TestServer | undefined;
  let proxy: DomainProxy | undefined;

  beforeEach(() => {
    (requireSession as jest.Mock).mockResolvedValue(SESSION);
    (getCsrfVerifier as jest.Mock).mockReturnValue(PASSTHROUGH_CSRF_VERIFIER);
  });

  afterEach(async () => {
    await proxy?.close();
    proxy = undefined;
    await server?.close();
    server = undefined;
    jest.clearAllMocks();
  });

  /** Wires `getDomainProxy()` to a real `DomainProxy` — built from the real generated registry/validators — pointed at the ephemeral test server. */
  function wireDomainProxy(baseUrl: string): void {
    proxy = new DomainProxy(
      {
        baseUrl,
        maxConnections: 5,
        maxRetries: 1,
        headersTimeoutMs: 2_000,
        bodyTimeoutMs: 2_000,
      },
      OPERATION_REGISTRY,
      RESPONSE_VALIDATORS
    );
    (getDomainProxy as jest.Mock).mockReturnValue(proxy);
  }

  it('disables the built-in body parser', () => {
    expect(config).toEqual({ api: { bodyParser: false } });
  });

  it('proves the full identity-probe round trip: BFF -> DomainProxy -> FastAPI -> validated 200 (spec §8.1)', async () => {
    let receivedAuth: string | undefined;
    let receivedPath: string | undefined;
    server = await startServer((req, res) => {
      receivedAuth = req.headers.authorization;
      receivedPath = req.url;
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(
        JSON.stringify({
          workspace_id: 'ws_1',
          role: 'owner',
          initiator_id: 'user_1',
          authorization_epoch: 0,
        })
      );
    });
    wireDomainProxy(server.url);
    const tokenIssuer = fakeTokenIssuer('tenant.jwt.token');
    (getTokenIssuer as jest.Mock).mockReturnValue(tokenIssuer);

    const { req, res } = mockGetRequest({
      method: 'GET',
      query: { slug: 'acme', path: ['identity'] },
      headers: { host: APP_HOST },
    });

    await handler(req, res);

    expect(res.statusCode).toBe(200);
    expect(res._getJSONData()).toEqual({
      workspace_id: 'ws_1',
      role: 'owner',
      initiator_id: 'user_1',
      authorization_epoch: 0,
    });
    expect(res.getHeader('Cache-Control')).toBe('no-store');
    expect(res.getHeader('X-Trace-Id')).toBeDefined();
    // FastAPI-side path came from the generated registry's pathTemplate,
    // not a hand-maintained constant in the handler.
    expect(receivedPath).toBe('/api/v1/identity');
    expect(receivedAuth).toBe('Bearer tenant.jwt.token');
    expect(tokenIssuer.issueForRequest).toHaveBeenCalledTimes(1);
  });

  it('returns 502 invalid_domain_response when the FastAPI body fails the generated schema (fail-closed, spec §6.8)', async () => {
    server = await startServer((_req, res) => {
      res.writeHead(200, { 'content-type': 'application/json' });
      // Missing role/initiator_id/authorization_epoch — fails the real
      // get_identity_probe_200 Ajv validator compiled from the OpenAPI spec.
      res.end(JSON.stringify({ workspace_id: 'ws_1' }));
    });
    wireDomainProxy(server.url);
    (getTokenIssuer as jest.Mock).mockReturnValue(fakeTokenIssuer());

    const { req, res } = mockGetRequest({
      method: 'GET',
      query: { slug: 'acme', path: ['identity'] },
      headers: { host: APP_HOST },
    });

    await handler(req, res);

    expect(res.statusCode).toBe(502);
    expect(res._getJSONData().code).toBe('invalid_domain_response');
  });

  it('returns 404 route_not_found for a path with no allowlist entry, before touching the session', async () => {
    const { req, res } = mockGetRequest({
      method: 'GET',
      query: { slug: 'acme', path: ['not-a-real-route'] },
      headers: { host: APP_HOST },
    });

    await handler(req, res);

    expect(res.statusCode).toBe(404);
    expect(res._getJSONData().code).toBe('route_not_found');
    expect(requireSession).not.toHaveBeenCalled();
  });

  it('returns 400 invalid_host on a Host header mismatch, before route matching', async () => {
    const { req, res } = mockGetRequest({
      method: 'GET',
      query: { slug: 'acme', path: ['identity'] },
      headers: { host: 'evil.example.com' },
    });

    await handler(req, res);

    expect(res.statusCode).toBe(400);
    expect(res._getJSONData().code).toBe('invalid_host');
  });

  it('returns 400 invalid_path for a path segment containing residual percent-encoding', async () => {
    const { req, res } = mockGetRequest({
      method: 'GET',
      query: { slug: 'acme', path: ['%2e%2e'] },
      headers: { host: APP_HOST },
    });

    await handler(req, res);

    expect(res.statusCode).toBe(400);
    expect(res._getJSONData().code).toBe('invalid_path');
  });

  it('echoes a well-formed browser X-Trace-Id back as trace_id even on a boundary failure', async () => {
    const traceId = 'browser-trace-id-123';
    const { req, res } = mockGetRequest({
      method: 'GET',
      query: { slug: 'acme', path: ['not-a-real-route'] },
      headers: { host: APP_HOST, 'x-trace-id': traceId },
    });

    await handler(req, res);

    expect(res._getJSONData().trace_id).toBe(traceId);
  });

  it('ignores a malformed X-Trace-Id and generates its own req_ id instead', async () => {
    const { req, res } = mockGetRequest({
      method: 'GET',
      query: { slug: 'acme', path: ['not-a-real-route'] },
      headers: { host: APP_HOST, 'x-trace-id': 'short' },
    });

    await handler(req, res);

    expect(res._getJSONData().trace_id).toMatch(/^req_[0-9a-f-]{36}$/);
  });

  it('proves the list_library_entries query round trip: validated/defaulted params are forwarded to FastAPI (spec §9.3)', async () => {
    let receivedUrl: string | undefined;
    server = await startServer((req, res) => {
      receivedUrl = req.url;
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ items: [], next_cursor: null }));
    });
    wireDomainProxy(server.url);
    (getTokenIssuer as jest.Mock).mockReturnValue(fakeTokenIssuer());

    const { req, res } = mockGetRequest({
      method: 'GET',
      url: '/api/domain/v1/w/acme/library/entries?entry_type=product',
      query: { slug: 'acme', path: ['library', 'entries'] },
      headers: { host: APP_HOST },
    });

    await handler(req, res);

    expect(res.statusCode).toBe(200);
    // include_archived/limit came from the generated schema's Zod defaults
    // (false/50) — not hand-typed by the browser — proving
    // serializeValidatedQuery forwards the *parsed* output, not the raw
    // query string.
    const forwardedQuery = new URL(receivedUrl ?? '', 'http://x').searchParams;
    expect(forwardedQuery.get('entry_type')).toBe('product');
    expect(forwardedQuery.get('include_archived')).toBe('false');
    expect(forwardedQuery.get('limit')).toBe('50');
    expect(forwardedQuery.has('cursor')).toBe(false);
  });

  it('forwards the validated Library intent as X-Command-Identity and ignores a spoofed browser header', async () => {
    let receivedCommandIdentity: string | undefined;
    let receivedIdempotencyKey: string | undefined;
    server = await startServer((req, res) => {
      receivedCommandIdentity = req.headers['x-command-identity'] as
        | string
        | undefined;
      receivedIdempotencyKey = req.headers['idempotency-key'] as
        | string
        | undefined;
      res.writeHead(201, { 'content-type': 'application/json' });
      res.end(
        JSON.stringify({
          id: 'lib_created',
          entry_type: 'product',
          display_name: 'Created',
          lifecycle: 'active',
          current_version_id: null,
          draft: {
            id: 'libdraft_created',
            entry_id: 'lib_created',
            revision: 1,
            state: 'editing',
            content: {},
          },
          versions: [],
          revision: 1,
          allowed_actions: [],
        })
      );
    });
    wireDomainProxy(server.url);
    (getTokenIssuer as jest.Mock).mockReturnValue(fakeTokenIssuer());
    const intentId = 'command.intent.browser.001';
    const { req, res } = mockJsonRequest(
      {
        method: 'POST',
        url: '/api/domain/v1/w/acme/library/entries',
        query: { slug: 'acme', path: ['library', 'entries'] },
        headers: {
          host: APP_HOST,
          origin: APP_ORIGIN,
          'x-csrf-token': 'csrf-token',
          'x-intent-id': intentId,
          'x-command-identity': 'browser-spoofed-value',
        },
      },
      { entry_type: 'product', display_name: 'Created', content: {} }
    );

    await handler(req, res);

    expect(res.statusCode).toBe(201);
    expect(receivedCommandIdentity).toBe(intentId);
    expect(receivedIdempotencyKey).toMatch(/^[a-f0-9]{64}$/);
  });

  it('rewrites the export command Location header to the public operation path', async () => {
    server = await startServer((_req, res) => {
      res.writeHead(202, {
        'content-type': 'application/json',
        location: '/api/v1/operations/op_export_1',
      });
      res.end(
        JSON.stringify({
          package_id: 'expkg_1',
          operation_id: 'op_export_1',
          state: 'queued',
        })
      );
    });
    wireDomainProxy(server.url);
    (getTokenIssuer as jest.Mock).mockReturnValue(fakeTokenIssuer());

    const { req, res } = mockJsonRequest(
      {
        method: 'POST',
        url: '/api/domain/v1/w/acme/generation/exports',
        query: { slug: 'acme', path: ['generation', 'exports'] },
        headers: {
          host: APP_HOST,
          origin: APP_ORIGIN,
          'x-csrf-token': 'csrf-token',
          'x-intent-id': 'export.intent.browser.001',
        },
      },
      { selector: { kind: 'approved_only' } }
    );

    await handler(req, res);

    expect(res.statusCode).toBe(202);
    expect(res.getHeader('Location')).toBe(
      '/api/domain/v1/w/acme/operations/op_export_1'
    );
    expect(res._getJSONData()).toEqual({
      package_id: 'expkg_1',
      operation_id: 'op_export_1',
      state: 'queued',
    });
  });

  it('rejects a malformed Library command identity before proxying', async () => {
    (getTokenIssuer as jest.Mock).mockReturnValue(fakeTokenIssuer());
    const { req, res } = mockJsonRequest(
      {
        method: 'POST',
        url: '/api/domain/v1/w/acme/library/entries',
        query: { slug: 'acme', path: ['library', 'entries'] },
        headers: {
          host: APP_HOST,
          origin: APP_ORIGIN,
          'x-csrf-token': 'csrf-token',
          'x-intent-id': 'bad identity with spaces',
        },
      },
      { entry_type: 'product', display_name: 'Rejected', content: {} }
    );

    await handler(req, res);

    expect(res.statusCode).toBe(400);
    expect(res._getJSONData().code).toBe('invalid_command_identity');
    expect(getDomainProxy).not.toHaveBeenCalled();
  });

  it('rejects repeated query params instead of silently keeping only the last value (fixes the Object.fromEntries collapse bug, spec §9.3)', async () => {
    (getTokenIssuer as jest.Mock).mockReturnValue(fakeTokenIssuer());

    const { req, res } = mockGetRequest({
      method: 'GET',
      url: '/api/domain/v1/w/acme/library/entries?entry_type=product&entry_type=service',
      query: { slug: 'acme', path: ['library', 'entries'] },
      headers: { host: APP_HOST },
    });

    await handler(req, res);

    // entry_type's schema is z.string() (not z.array()) — the fixed handler
    // surfaces the repeated key as ['product', 'service'] via getAll(),
    // which correctly fails z.string() validation. The old
    // Object.fromEntries(rawSearchParams) collapse would have silently kept
    // only 'service' and let the request through.
    expect(res.statusCode).toBe(400);
    expect(res._getJSONData().code).toBe('invalid_query');
  });

  it('returns 502 domain_unavailable when FastAPI is unreachable', async () => {
    // No server started — the pool's connection attempt fails, and the
    // handler's central catch maps any non-boundary error to 502.
    wireDomainProxy('http://127.0.0.1:1');
    (getTokenIssuer as jest.Mock).mockReturnValue(fakeTokenIssuer());

    const { req, res } = mockGetRequest({
      method: 'GET',
      query: { slug: 'acme', path: ['identity'] },
      headers: { host: APP_HOST },
    });

    await handler(req, res);

    expect(res.statusCode).toBe(502);
    expect(res._getJSONData().code).toBe('domain_unavailable');
  });

  describe('interface language (#255)', () => {
    /** Runs the identity probe and returns the X-UI-Language FastAPI saw. */
    async function forwardedLanguage(options: {
      locale?: string | null;
      cookies?: Record<string, string>;
      headers?: Record<string, string>;
    }): Promise<string | undefined> {
      let received: string | undefined;
      server = await startServer((req, res) => {
        received = req.headers['x-ui-language'] as string | undefined;
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end(
          JSON.stringify({
            workspace_id: 'ws_1',
            role: 'owner',
            initiator_id: 'user_1',
            authorization_epoch: 0,
          })
        );
      });
      wireDomainProxy(server.url);
      const tokenIssuer = fakeTokenIssuer();
      (getTokenIssuer as jest.Mock).mockReturnValue(tokenIssuer);
      (requireSession as jest.Mock).mockResolvedValue({
        ...SESSION,
        user: { ...SESSION.user, locale: options.locale ?? null },
      });
      const { req, res } = mockGetRequest({
        method: 'GET',
        query: { slug: 'acme', path: ['identity'] },
        headers: { host: APP_HOST, ...(options.headers ?? {}) },
        cookies: options.cookies ?? {},
      });

      await handler(req, res);

      expect(res.statusCode).toBe(200);
      // The language never reaches the token: the Workspace is still the
      // route's, resolved and checked by the issuer alone.
      expect(tokenIssuer.issueForRequest).toHaveBeenCalledWith(
        expect.objectContaining({ workspaceSlug: 'acme' })
      );
      return received;
    }

    it('sends the saved preference ahead of the cookie and the browser', async () => {
      await expect(
        forwardedLanguage({
          locale: 'zh-CN',
          cookies: { NEXT_LOCALE: 'en' },
          headers: { 'accept-language': 'en-US' },
        })
      ).resolves.toBe('zh-CN');
    });

    it('follows the cookie when no preference is saved', async () => {
      await expect(
        forwardedLanguage({
          cookies: { NEXT_LOCALE: 'zh-CN' },
          headers: { 'accept-language': 'en-US' },
        })
      ).resolves.toBe('zh-CN');
    });

    it('follows Accept-Language without a preference or a cookie', async () => {
      await expect(
        forwardedLanguage({ headers: { 'accept-language': 'zh-TW,en;q=0.5' } })
      ).resolves.toBe('zh-CN');
    });

    it('falls back to en for an unoffered language or a tampered cookie', async () => {
      await expect(
        forwardedLanguage({
          cookies: { NEXT_LOCALE: 'fr' },
          headers: { 'accept-language': 'fr-FR' },
        })
      ).resolves.toBe('en');
    });

    it('ignores an X-UI-Language header the browser sends itself', async () => {
      await expect(
        forwardedLanguage({
          locale: 'en',
          headers: { 'x-ui-language': 'zh-CN' },
        })
      ).resolves.toBe('en');
    });
  });

  describe('former step-up commands (ADR 0015)', () => {
    const PUBLISHED_VERSION = {
      id: 'libver_1',
      entry_id: 'lib_source',
      entry_type: 'data_source',
      version_no: 1,
      state: 'effective',
      eligibility: 'eligible',
      content: {},
      allowed_actions: [],
    };

    function commandHeaders(extra: Record<string, string> = {}) {
      return {
        host: APP_HOST,
        origin: APP_ORIGIN,
        'x-csrf-token': 'csrf-token',
        ...extra,
      };
    }

    function problem(code: string, status: number) {
      return JSON.stringify({
        type: `urn:marketai:error:${code.replace(/_/g, '-')}`,
        title: code,
        status,
        detail: code,
        instance: '/api/v1/scale/sandbox/targets',
        code,
        trace_id: 'trace-1',
        retryable: false,
        field_errors: [],
      });
    }

    it('declares no step-up header on any allowlisted operation', async () => {
      const { DOMAIN_ALLOWLIST } =
        await import('../../../../../lib/domain-client/generated/allowlist');
      for (const entry of Object.values(DOMAIN_ALLOWLIST)) {
        expect(entry).not.toHaveProperty('forwardStepUpHeader');
      }
    });

    it('forwards publish_library_entry for a data source without a grant and drops any step-up header the browser sends', async () => {
      let received: http.IncomingHttpHeaders = {};
      server = await startServer((req, res) => {
        received = req.headers;
        res.writeHead(201, { 'content-type': 'application/json' });
        res.end(JSON.stringify(PUBLISHED_VERSION));
      });
      wireDomainProxy(server.url);
      (getTokenIssuer as jest.Mock).mockReturnValue(fakeTokenIssuer());

      const { req, res } = mockJsonRequest(
        {
          method: 'POST',
          url: '/api/domain/v1/w/acme/library/entries/lib_source/publish',
          query: {
            slug: 'acme',
            path: ['library', 'entries', 'lib_source', 'publish'],
          },
          headers: commandHeaders({
            'x-intent-id': 'publish.intent.browser.001',
            'if-match': '1',
            'x-step-up-grant': 'grant_1',
            'x-step-up-assertion': 'forged.step-up.assertion',
          }),
        },
        { impact_id: 'imp_1', impact_hash: 'h1', acknowledged_warnings: [] }
      );
      await handler(req, res);

      expect(res.statusCode).toBe(201);
      expect(res._getJSONData().id).toBe('libver_1');
      expect(received['x-step-up-assertion']).toBeUndefined();
      expect(received['x-step-up-grant']).toBeUndefined();
    });

    it('forwards create_credential without a grant', async () => {
      let received: http.IncomingHttpHeaders | null = null;
      server = await startServer((req, res) => {
        received = req.headers;
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ credential_version_id: 'cv_1' }));
      });
      wireDomainProxy(server.url);
      (getTokenIssuer as jest.Mock).mockReturnValue(fakeTokenIssuer());

      const { req, res } = mockJsonRequest(
        {
          method: 'POST',
          url: '/api/domain/v1/w/acme/credentials',
          query: { slug: 'acme', path: ['credentials'] },
          headers: commandHeaders({ 'x-intent-id': 'credential.intent.001' }),
        },
        {
          credential_key: 'OPENAI_API_KEY',
          provider: 'openai',
          plaintext_b64: 'c2VjcmV0',
        }
      );
      await handler(req, res);

      expect(res.statusCode).toBe(200);
      expect(received).not.toBeNull();
      expect(received!['x-step-up-assertion']).toBeUndefined();
    });

    it('forwards create_scale_target without a grant and relays FastAPI’s role refusal', async () => {
      let forwarded = false;
      server = await startServer((_req, res) => {
        forwarded = true;
        res.writeHead(403, { 'content-type': 'application/problem+json' });
        res.end(problem('owner_required', 403));
      });
      wireDomainProxy(server.url);
      (getTokenIssuer as jest.Mock).mockReturnValue(fakeTokenIssuer());

      const { req, res } = mockJsonRequest(
        {
          method: 'POST',
          url: '/api/domain/v1/w/acme/scale/sandbox/targets',
          query: { slug: 'acme', path: ['scale', 'sandbox', 'targets'] },
          headers: commandHeaders({ 'x-intent-id': 'target.intent.0001' }),
        },
        { publication_preview_id: 'stpp_1' }
      );
      await handler(req, res);

      expect(forwarded).toBe(true);
      expect(res.statusCode).toBe(403);
      expect(res._getJSONData().code).toBe('owner_required');
    });
  });

  describe('Scale problem responses (#368)', () => {
    /** A FastAPI refusal for `create_scale_export_download_intent`, which declares no 404 or 409 of its own. */
    async function downloadIntentRefusedWith(
      status: number,
      contentType: string,
      body: Record<string, unknown>
    ) {
      server = await startServer((_req, res) => {
        res.writeHead(status, { 'content-type': contentType });
        res.end(JSON.stringify(body));
      });
      wireDomainProxy(server.url);
      (getTokenIssuer as jest.Mock).mockReturnValue(fakeTokenIssuer());

      const { req, res } = mockGetRequest({
        method: 'POST',
        url: '/api/domain/v1/w/acme/scale/customer_sandbox/exports/sexp_1/download-intents',
        query: {
          slug: 'acme',
          path: [
            'scale',
            'customer_sandbox',
            'exports',
            'sexp_1',
            'download-intents',
          ],
        },
        headers: {
          host: APP_HOST,
          origin: APP_ORIGIN,
          'x-csrf-token': 'csrf-token',
          'x-intent-id': 'export.download.intent.0001',
        },
      });
      await handler(req, res);
      return res;
    }

    it('relays a full Scale problem with its own status and code', async () => {
      const res = await downloadIntentRefusedWith(
        404,
        'application/problem+json',
        {
          type: 'urn:marketai:error:export-not-found',
          title: 'export_not_found',
          status: 404,
          detail: 'export_not_found: sexp_1',
          instance: '/exports/{export_id}/download-intents',
          code: 'export_not_found',
          trace_id: 'trace-368',
          retryable: false,
          field_errors: [],
        }
      );

      expect(res.statusCode).toBe(404);
      expect(res._getJSONData()).toMatchObject({
        code: 'export_not_found',
        trace_id: 'trace-368',
        retryable: false,
      });
    });

    it('turns the old Scale problem, without trace_id and retryable, into 502', async () => {
      const res = await downloadIntentRefusedWith(
        409,
        'application/problem+json',
        {
          type: 'about:blank',
          title: 'export_not_complete',
          status: 409,
          detail: 'export_not_complete: sexp_1',
          code: 'export_not_complete',
        }
      );

      expect(res.statusCode).toBe(502);
      expect(res._getJSONData().code).toBe('invalid_domain_response');
    });

    it('turns a plain application/json refusal into 502', async () => {
      const res = await downloadIntentRefusedWith(404, 'application/json', {
        code: 'export_not_found',
        message: 'sexp_1',
      });

      expect(res.statusCode).toBe(502);
      expect(res._getJSONData().code).toBe('invalid_domain_response');
    });
  });

  describe('Usage (#8)', () => {
    /** What FastAPI's `get_usage` answers: provider calls per execution mode, Mock out of `total`. */
    const USAGE = {
      workspace_id: 'ws_1',
      metric_key: 'provider_calls',
      window_start: '2026-11-07T12:00:00+00:00',
      window_end: '2026-11-08T12:00:00+00:00',
      total: '3',
      by_execution_mode: {
        byok: '2',
        sandbox: '1',
        mock: '4',
        not_applicable: '0',
      },
    };

    function usageRequest(url = '/api/domain/v1/w/acme/usage') {
      return mockGetRequest({
        method: 'GET',
        url,
        query: { slug: 'acme', path: ['usage'] },
        headers: { host: APP_HOST },
      });
    }

    async function usageServer(): Promise<string[]> {
      const received: string[] = [];
      server = await startServer((req, res) => {
        received.push(req.url ?? '');
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end(JSON.stringify(USAGE));
      });
      wireDomainProxy(server.url);
      return received;
    }

    it('relays the parameterless read with its execution-mode buckets', async () => {
      const received = await usageServer();
      (getTokenIssuer as jest.Mock).mockReturnValue(fakeTokenIssuer());
      const { req, res } = usageRequest();

      await handler(req, res);

      expect(res.statusCode).toBe(200);
      expect(res._getJSONData()).toEqual(USAGE);
      expect(received).toEqual(['/api/v1/usage']);
    });

    it('refuses a query string: the contract declares no Usage parameters', async () => {
      const received = await usageServer();
      (getTokenIssuer as jest.Mock).mockReturnValue(fakeTokenIssuer());
      const { req, res } = usageRequest(
        '/api/domain/v1/w/acme/usage?metric_key=provider_calls'
      );

      await handler(req, res);

      expect(res.statusCode).toBe(400);
      expect(res._getJSONData().code).toBe('unexpected_query');
      expect(received).toEqual([]);
    });

    it('answers 403 to a member of another Workspace and never reaches FastAPI', async () => {
      const received = await usageServer();
      // The issuer finds no membership of this user in the slug's Workspace.
      (getTokenIssuer as jest.Mock).mockReturnValue({
        issueForRequest: jest
          .fn()
          .mockRejectedValue(
            new ApiError(403, 'workspace_membership_required')
          ),
      });
      const { req, res } = usageRequest();

      await handler(req, res);

      expect(res.statusCode).toBe(403);
      expect(res._getJSONData().code).toBe('workspace_membership_required');
      expect(received).toEqual([]);
    });

    it('answers 401 without a session and never reaches FastAPI', async () => {
      const received = await usageServer();
      (requireSession as jest.Mock).mockRejectedValue(
        new ApiError(401, 'unauthenticated')
      );
      const { req, res } = usageRequest();

      await handler(req, res);

      expect(res.statusCode).toBe(401);
      expect(getTokenIssuer).not.toHaveBeenCalled();
      expect(received).toEqual([]);
    });
  });

  describe('workspace lifecycle gate', () => {
    it('returns 423 workspace_provisioning for non-ACTIVE workspace and does not call FastAPI', async () => {
      // Override the prisma mock to return a non-ACTIVE lifecycle
      const { prisma } = await import('../../../../../lib/prisma');
      (prisma.team.findUnique as jest.Mock).mockResolvedValueOnce({
        id: 'ws_provisioning',
        lifecycle: 'PROVISIONING',
      });

      (getTokenIssuer as jest.Mock).mockReturnValue(fakeTokenIssuer());

      const { req, res } = mockJsonRequest(
        {
          method: 'POST',
          url: '/api/domain/v1/w/test-slug/meta/connections',
          query: { slug: 'test-slug', path: ['meta', 'connections'] },
          headers: {
            host: APP_HOST,
            origin: APP_ORIGIN,
            'x-csrf-token': 'valid-csrf',
          },
        },
        { meta_mode: 'customer_sandbox' }
      );

      await handler(req, res);

      expect(res._getStatusCode()).toBe(423);
      const body = res._getJSONData();
      expect(body.code).toBe('workspace_provisioning');

      // Verify FastAPI proxy was NOT called
      expect(getDomainProxy).not.toHaveBeenCalled();
    });
  });
});
