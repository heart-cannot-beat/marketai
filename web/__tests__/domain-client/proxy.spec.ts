import http from 'node:http';
import type { AddressInfo } from 'node:net';
import type { ValidateFunction } from 'ajv/dist/core';
import type { NextApiRequest, NextApiResponse } from 'next';

import {
  DomainProxy,
  DomainProxyError,
  GatewayTimeoutError,
  type OperationEntry,
} from '../../lib/domain-client/proxy';
import type { DelegatedTokenIssuer } from '../../lib/delegated-identity/token-issuer';

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

/** Destroys the socket for the first `failCount` connections (no response), then answers normally — simulates a transient network failure that recovers. */
function startFlakyServer(
  failCount: number,
  respond: http.RequestListener
): Promise<TestServer & { attempts: () => number }> {
  let attempts = 0;
  return new Promise((resolve) => {
    const server = http.createServer((req, res) => {
      attempts++;
      if (attempts <= failCount) {
        req.socket.destroy();
        return;
      }
      respond(req, res);
    });
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address() as AddressInfo;
      resolve({
        url: `http://127.0.0.1:${port}`,
        close: () => new Promise((res) => server.close(() => res())),
        attempts: () => attempts,
      });
    });
  });
}

function fakeTokenIssuer(
  token = 'fake.jwt.token'
): DelegatedTokenIssuer & { calls: number } {
  return {
    calls: 0,
    async issueForRequest() {
      this.calls++;
      return token;
    },
  };
}

/** Builds a fixture `ValidateFunction` that always returns `result` — these tests exercise `DomainProxy`'s transport/retry/validation-dispatch mechanics, not real per-operation schemas (spec §6.2.5 owns real schema fidelity, covered by the generated-artifact tests). */
function fixtureValidator(result: boolean): ValidateFunction {
  const fn = (() => result) as unknown as ValidateFunction;
  fn.errors = null;
  return fn;
}

const alwaysValid = fixtureValidator(true);

const FIXTURE_VALIDATORS = new Map<string, ValidateFunction>([
  ['get_identity_probe_200', alwaysValid],
  ['create_upload_intent_201', alwaysValid],
  ['get_operation_200', alwaysValid],
  ['problem_details', alwaysValid],
]);

const GET_IDENTITY: OperationEntry = {
  operationId: 'get_identity_probe',
  method: 'GET',
  pathTemplate: '/api/v1/identity',
  pathParams: [],
  queryParams: [],
  requestMediaTypes: [],
  requiresIdempotency: false,
  maxRequestBytes: 0,
  maxResponseBytes: 65_536,
  responses: {
    200: {
      mediaTypes: ['application/json'],
      body: { validatorId: 'get_identity_probe_200' },
      locationShapes: [],
    },
  },
};

const CREATE_UPLOAD_INTENT: OperationEntry = {
  operationId: 'create_upload_intent',
  method: 'POST',
  pathTemplate: '/api/v1/upload-intents',
  pathParams: [],
  queryParams: [],
  requestMediaTypes: ['application/json'],
  requiresIdempotency: true,
  maxRequestBytes: 8_192,
  maxResponseBytes: 65_536,
  responses: {
    201: {
      mediaTypes: ['application/json'],
      body: { validatorId: 'create_upload_intent_201' },
      locationShapes: [],
    },
  },
};

const baseParams = () => ({
  req: {} as NextApiRequest,
  res: {} as NextApiResponse,
  slug: 'acme',
  query: new URLSearchParams(),
  idempotencyKey: undefined as string | undefined,
  requestId: 'req_1',
  traceId: 'trace_1',
  conditionalHeaders: {} as Record<string, string>,
});

describe('DomainProxy', () => {
  let proxy: DomainProxy | undefined;
  let server: TestServer | undefined;

  const makeProxy = (
    baseUrl: string,
    registry: OperationEntry[],
    overrides: Partial<{
      maxConnections: number;
      maxRetries: number;
      headersTimeoutMs: number;
      bodyTimeoutMs: number;
      validators: ReadonlyMap<string, ValidateFunction>;
    }> = {}
  ) => {
    proxy = new DomainProxy(
      {
        baseUrl,
        maxConnections: overrides.maxConnections ?? 5,
        maxRetries: overrides.maxRetries ?? 1,
        headersTimeoutMs: overrides.headersTimeoutMs ?? 2_000,
        bodyTimeoutMs: overrides.bodyTimeoutMs ?? 2_000,
      },
      new Map(registry.map((entry) => [entry.operationId, entry])),
      overrides.validators ?? FIXTURE_VALIDATORS
    );
    return proxy;
  };

  afterEach(async () => {
    await proxy?.close();
    proxy = undefined;
    await server?.close();
    server = undefined;
  });

  it('forwards a GET and builds request headers from an empty set (spec §4.4)', async () => {
    let received: http.IncomingHttpHeaders = {};
    server = await startServer((req, res) => {
      received = req.headers;
      res.writeHead(200, { 'content-type': 'application/json', etag: '"abc"' });
      res.end(JSON.stringify({ hello: 'world' }));
    });
    const client = makeProxy(server.url, [GET_IDENTITY]);
    const tokenIssuer = fakeTokenIssuer();

    const result = await client.forward('get_identity_probe', {
      ...baseParams(),
      tokenIssuer,
      pathParams: {},
      signal: AbortSignal.timeout(5_000),
    });

    expect(result.status).toBe(200);
    expect(result.body).toEqual({ hello: 'world' });
    expect(result.validated).toBe(true);
    expect(result.headers['content-type']).toBe('application/json');
    expect(result.headers.etag).toBe('"abc"');
    expect(result.headers['Cache-Control']).toBe('no-store');

    expect(received.authorization).toBe('Bearer fake.jwt.token');
    expect(received.accept).toBe('application/json');
    expect(received['x-request-id']).toBe('req_1');
    expect(received['x-trace-id']).toBe('trace_1');
    // Never a pass-through of browser-only headers.
    expect(received.cookie).toBeUndefined();
    expect(received['x-csrf-token']).toBeUndefined();
    expect(received['content-length']).toBeUndefined();
  });

  it('serializes the body once and forwards Content-Type/Content-Length/Idempotency-Key on POST', async () => {
    let receivedBody = '';
    let received: http.IncomingHttpHeaders = {};
    server = await startServer((req, res) => {
      received = req.headers;
      const chunks: Buffer[] = [];
      req.on('data', (c) => chunks.push(c));
      req.on('end', () => {
        receivedBody = Buffer.concat(chunks).toString('utf-8');
        res.writeHead(201, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ id: 'ui_1' }));
      });
    });
    const client = makeProxy(server.url, [CREATE_UPLOAD_INTENT]);
    const tokenIssuer = fakeTokenIssuer();

    const result = await client.forward('create_upload_intent', {
      ...baseParams(),
      tokenIssuer,
      pathParams: {},
      idempotencyKey: 'idem_abc',
      signal: AbortSignal.timeout(5_000),
      body: { file_name: 'a.png' },
    });

    expect(result.status).toBe(201);
    expect(receivedBody).toBe(JSON.stringify({ file_name: 'a.png' }));
    expect(received['content-type']).toBe('application/json');
    expect(received['content-length']).toBe(
      String(Buffer.byteLength(receivedBody, 'utf-8'))
    );
    expect(received['idempotency-key']).toBe('idem_abc');
  });

  it('forwards If-Match/If-None-Match only when the caller supplies them as conditionalHeaders', async () => {
    let received: http.IncomingHttpHeaders = {};
    server = await startServer((req, res) => {
      received = req.headers;
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ ok: true }));
    });
    const client = makeProxy(server.url, [GET_IDENTITY]);

    await client.forward('get_identity_probe', {
      ...baseParams(),
      tokenIssuer: fakeTokenIssuer(),
      pathParams: {},
      signal: AbortSignal.timeout(5_000),
      conditionalHeaders: { 'If-None-Match': '"etag-1"' },
    });

    expect(received['if-none-match']).toBe('"etag-1"');
    expect(received['if-match']).toBeUndefined();
  });

  it('passes through a well-formed undeclared Problem Details error (spec §6.8 step 4)', async () => {
    server = await startServer((_req, res) => {
      res.writeHead(409, { 'content-type': 'application/problem+json' });
      res.end(
        JSON.stringify({
          title: 'Conflict',
          status: 409,
          code: 'workspace_conflict',
          trace_id: 'trace_1',
        })
      );
    });
    // GET_IDENTITY declares only 200 — 409 is undeclared, exercising the
    // transparent-pass-through branch rather than a declared-status check.
    const client = makeProxy(server.url, [GET_IDENTITY]);

    const result = await client.forward('get_identity_probe', {
      ...baseParams(),
      tokenIssuer: fakeTokenIssuer(),
      pathParams: {},
      signal: AbortSignal.timeout(5_000),
    });

    expect(result.status).toBe(409);
    expect(result.body).toMatchObject({ code: 'workspace_conflict' });
  });

  it('fails closed (502 invalid_domain_response) on an undeclared error without problem+json', async () => {
    server = await startServer((_req, res) => {
      res.writeHead(500, { 'content-type': 'text/html' });
      res.end('<h1>oops</h1>');
    });
    const client = makeProxy(server.url, [GET_IDENTITY]);

    await expect(
      client.forward('get_identity_probe', {
        ...baseParams(),
        tokenIssuer: fakeTokenIssuer(),
        pathParams: {},
        signal: AbortSignal.timeout(5_000),
      })
    ).rejects.toMatchObject({
      status: 502,
      code: 'invalid_domain_response',
    });
  });

  it('fails closed on an undeclared error with a malformed problem+json body', async () => {
    server = await startServer((_req, res) => {
      res.writeHead(500, { 'content-type': 'application/problem+json' });
      res.end(JSON.stringify({ not: 'a problem details body' }));
    });
    // A validator fixture that rejects everything, so the malformed body
    // fails the problem_details check regardless of `alwaysValid` elsewhere.
    const client = makeProxy(server.url, [GET_IDENTITY], {
      validators: new Map([['problem_details', fixtureValidator(false)]]),
    });

    await expect(
      client.forward('get_identity_probe', {
        ...baseParams(),
        tokenIssuer: fakeTokenIssuer(),
        pathParams: {},
        signal: AbortSignal.timeout(5_000),
      })
    ).rejects.toBeInstanceOf(DomainProxyError);
  });

  it('fails closed on invalid JSON in a declared success status', async () => {
    server = await startServer((_req, res) => {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end('not json{{{');
    });
    const client = makeProxy(server.url, [GET_IDENTITY]);

    await expect(
      client.forward('get_identity_probe', {
        ...baseParams(),
        tokenIssuer: fakeTokenIssuer(),
        pathParams: {},
        signal: AbortSignal.timeout(5_000),
      })
    ).rejects.toMatchObject({ status: 502, code: 'invalid_domain_response' });
  });

  it('fails closed when the declared success body fails schema validation', async () => {
    server = await startServer((_req, res) => {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ unexpected: 'shape' }));
    });
    const client = makeProxy(server.url, [GET_IDENTITY], {
      validators: new Map([
        ['get_identity_probe_200', fixtureValidator(false)],
      ]),
    });

    await expect(
      client.forward('get_identity_probe', {
        ...baseParams(),
        tokenIssuer: fakeTokenIssuer(),
        pathParams: {},
        signal: AbortSignal.timeout(5_000),
      })
    ).rejects.toMatchObject({ status: 502, code: 'invalid_domain_response' });
  });

  it('fails closed when a declared status has no registered validator (spec §6.8 step 5)', async () => {
    server = await startServer((_req, res) => {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ hello: 'world' }));
    });
    const client = makeProxy(server.url, [GET_IDENTITY], {
      validators: new Map(), // no "get_identity_probe_200" entry
    });

    await expect(
      client.forward('get_identity_probe', {
        ...baseParams(),
        tokenIssuer: fakeTokenIssuer(),
        pathParams: {},
        signal: AbortSignal.timeout(5_000),
      })
    ).rejects.toMatchObject({ status: 502, code: 'invalid_domain_response' });
  });

  it('rejects with 502 when the response exceeds maxResponseBytes (spec §4.5)', async () => {
    server = await startServer((_req, res) => {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ blob: 'x'.repeat(100) }));
    });
    const tinyEntry: OperationEntry = { ...GET_IDENTITY, maxResponseBytes: 10 };
    const client = makeProxy(server.url, [tinyEntry]);

    await expect(
      client.forward('get_identity_probe', {
        ...baseParams(),
        tokenIssuer: fakeTokenIssuer(),
        pathParams: {},
        signal: AbortSignal.timeout(5_000),
      })
    ).rejects.toMatchObject({ status: 502, code: 'invalid_domain_response' });
  });

  it('fails closed on a Location header with no matching LocationShape (spec §4.7)', async () => {
    server = await startServer((_req, res) => {
      res.writeHead(200, {
        'content-type': 'application/json',
        location: '/api/v1/upload-intents/ui_1',
      });
      res.end(JSON.stringify({ hello: 'world' }));
    });
    // GET_IDENTITY's 200 declares locationShapes: [] — no shape can match.
    const client = makeProxy(server.url, [GET_IDENTITY]);

    await expect(
      client.forward('get_identity_probe', {
        ...baseParams(),
        tokenIssuer: fakeTokenIssuer(),
        pathParams: {},
        signal: AbortSignal.timeout(5_000),
      })
    ).rejects.toMatchObject({ status: 502, code: 'invalid_domain_response' });
  });

  it('builds the FastAPI path from the template, path params, and query string', async () => {
    let receivedPath = '';
    server = await startServer((req, res) => {
      receivedPath = req.url ?? '';
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ ok: true }));
    });
    const entry: OperationEntry = {
      operationId: 'get_operation',
      method: 'GET',
      pathTemplate: '/api/v1/operations/{operation_id}',
      pathParams: ['operation_id'],
      queryParams: ['expand'],
      requestMediaTypes: [],
      requiresIdempotency: false,
      maxRequestBytes: 0,
      maxResponseBytes: 65_536,
      responses: {
        200: {
          mediaTypes: ['application/json'],
          body: { validatorId: 'get_operation_200' },
          locationShapes: [],
        },
      },
    };
    const client = makeProxy(server.url, [entry]);
    const query = new URLSearchParams();
    query.set('expand', 'result');

    await client.forward('get_operation', {
      ...baseParams(),
      tokenIssuer: fakeTokenIssuer(),
      pathParams: { operation_id: 'op_123' },
      query,
      signal: AbortSignal.timeout(5_000),
    });

    expect(receivedPath).toBe('/api/v1/operations/op_123?expand=result');
  });

  it('rejects an unregistered operationId without dispatching a request', async () => {
    server = await startServer((_req, res) => {
      res.writeHead(200);
      res.end();
    });
    const client = makeProxy(server.url, []);

    await expect(
      client.forward('nonexistent_operation', {
        ...baseParams(),
        tokenIssuer: fakeTokenIssuer(),
        pathParams: {},
        signal: AbortSignal.timeout(5_000),
      })
    ).rejects.toThrow('Unknown operationId: nonexistent_operation');
  });

  it('retries a GET once after a transient network failure and succeeds (spec §4.9)', async () => {
    const flaky = await startFlakyServer(1, (_req, res) => {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ recovered: true }));
    });
    server = flaky;
    const client = makeProxy(server.url, [GET_IDENTITY], { maxRetries: 1 });
    const tokenIssuer = fakeTokenIssuer();

    const result = await client.forward('get_identity_probe', {
      ...baseParams(),
      tokenIssuer,
      pathParams: {},
      signal: AbortSignal.timeout(5_000),
    });

    expect(result.status).toBe(200);
    expect(result.body).toEqual({ recovered: true });
    expect(flaky.attempts()).toBe(2);
    // A fresh token is signed for every attempt (spec §4.8).
    expect(tokenIssuer.calls).toBe(2);
  });

  it('retries a POST carrying an Idempotency-Key after a transient network failure', async () => {
    const flaky = await startFlakyServer(1, (_req, res) => {
      res.writeHead(201, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ id: 'ui_1' }));
    });
    server = flaky;
    const client = makeProxy(server.url, [CREATE_UPLOAD_INTENT], {
      maxRetries: 1,
    });

    const result = await client.forward('create_upload_intent', {
      ...baseParams(),
      tokenIssuer: fakeTokenIssuer(),
      pathParams: {},
      idempotencyKey: 'idem_1',
      signal: AbortSignal.timeout(5_000),
      body: { file_name: 'a.png' },
    });

    expect(result.status).toBe(201);
    expect(flaky.attempts()).toBe(2);
  });

  it('never retries a POST without an Idempotency-Key, even on a transient network failure', async () => {
    // requiresIdempotency: false here — the caller passed no idempotencyKey.
    const nonIdempotentPost: OperationEntry = {
      ...CREATE_UPLOAD_INTENT,
      requiresIdempotency: false,
    };
    const flaky = await startFlakyServer(1, (_req, res) => {
      res.writeHead(201, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ id: 'ui_1' }));
    });
    server = flaky;
    const client = makeProxy(server.url, [nonIdempotentPost], {
      maxRetries: 1,
    });

    await expect(
      client.forward('create_upload_intent', {
        ...baseParams(),
        tokenIssuer: fakeTokenIssuer(),
        pathParams: {},
        signal: AbortSignal.timeout(5_000),
        body: { file_name: 'a.png' },
      })
    ).rejects.toThrow();

    expect(flaky.attempts()).toBe(1);
  });

  it('never retries once a complete HTTP response has been received, even a 5xx', async () => {
    let attempts = 0;
    server = await startServer((_req, res) => {
      attempts++;
      res.writeHead(503, { 'content-type': 'application/problem+json' });
      res.end(
        JSON.stringify({
          title: 'Unavailable',
          status: 503,
          code: 'domain_unavailable',
          trace_id: 'trace_1',
        })
      );
    });
    const client = makeProxy(server.url, [GET_IDENTITY], { maxRetries: 1 });

    const result = await client.forward('get_identity_probe', {
      ...baseParams(),
      tokenIssuer: fakeTokenIssuer(),
      pathParams: {},
      signal: AbortSignal.timeout(5_000),
    });

    expect(result.status).toBe(503);
    expect(attempts).toBe(1);
  });

  it('rejects immediately with GatewayTimeoutError when the deadline is already past, before signing a token', async () => {
    server = await startServer((_req, res) => {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ ok: true }));
    });
    const client = makeProxy(server.url, [GET_IDENTITY]);
    const tokenIssuer = fakeTokenIssuer();
    const controller = new AbortController();
    controller.abort();

    await expect(
      client.forward('get_identity_probe', {
        ...baseParams(),
        tokenIssuer,
        pathParams: {},
        signal: controller.signal,
      })
    ).rejects.toBeInstanceOf(GatewayTimeoutError);
    expect(tokenIssuer.calls).toBe(0);
  });

  it('rejects with GatewayTimeoutError when the deadline elapses while reading a slow response body', async () => {
    server = await startServer((_req, res) => {
      res.writeHead(200, { 'content-type': 'application/json' });
      // Headers are already flushed; delay the body well past the deadline.
      setTimeout(() => res.end(JSON.stringify({ ok: true })), 300);
    });
    const client = makeProxy(server.url, [GET_IDENTITY]);

    await expect(
      client.forward('get_identity_probe', {
        ...baseParams(),
        tokenIssuer: fakeTokenIssuer(),
        pathParams: {},
        signal: AbortSignal.timeout(30),
      })
    ).rejects.toBeInstanceOf(GatewayTimeoutError);
  });
});
