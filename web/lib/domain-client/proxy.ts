/**
 * `DomainProxy` — the server-only HTTP client that forwards BFF requests to
 * FastAPI over `undici` (spec §4.8, §4.9, §6.7). Runs on the server only;
 * never imported from a browser bundle.
 *
 * Full per-status schema validation and Location rewriting (spec §6.8) are
 * wired using Task 10's generated `OperationEntry`/`ResponseShape` types
 * (`./generated/operation-registry`), `RESPONSE_VALIDATORS`
 * (`./generated/validators`) and `buildBffPath` (`./generated/paths`).
 *
 * Deviation from spec §6.7's literal pseudocode: `forward()`'s `operationId`
 * is typed `string`, and the registry/validators are constructor-injected
 * (defaulting to `new Map()`) rather than imported as module-level
 * singletons inside this file. The production BFF handler still passes the
 * real generated `OPERATION_REGISTRY`/`RESPONSE_VALIDATORS` — the deviation
 * only keeps this module free of a hard import-time dependency on the
 * generated artifacts, so its own test suite is not coupled to how many
 * operations `contracts/openapi/marketai-domain-v1.json` currently
 * declares (the tracer milestone declares exactly one: `get_identity_probe`).
 */
import { Pool, type Dispatcher } from 'undici';
import type { ValidateFunction } from 'ajv/dist/core';
import type { NextApiRequest, NextApiResponse } from 'next';

import type { DelegatedTokenIssuer } from '../delegated-identity/token-issuer';
import type { SupportedLocale } from '../i18n/locales';
import { buildBffPath } from './generated/paths';
import type {
  LocationShape,
  OperationEntry,
  ResponseShape,
} from './generated/operation-registry';

export type {
  OperationEntry,
  ResponseShape,
  LocationShape,
} from './generated/operation-registry';

/** Thrown when the shared per-request deadline elapses (spec §4.8). */
export class GatewayTimeoutError extends Error {
  constructor() {
    super('gateway_timeout');
    this.name = 'GatewayTimeoutError';
  }
}

/** A boundary failure the proxy raises itself — never a pass-through of an upstream error body. */
export class DomainProxyError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    public readonly detail?: string,
    public readonly retryable = false
  ) {
    super(detail ?? code);
    this.name = 'DomainProxyError';
  }
}

/**
 * Runs `work` under `signal`, discarding a late result rather than letting a
 * dependency that cannot be physically cancelled (e.g. Prisma) extend the
 * request past its deadline. `onAbort` gets one best-effort chance to
 * release the underlying resource (e.g. destroy a response stream); a
 * failure there never overrides the timeout outcome (spec §4.8).
 */
export async function awaitWithinDeadline<T>(
  work: Promise<T>,
  signal: AbortSignal,
  onAbort: () => void = () => {}
): Promise<T> {
  if (signal.aborted) throw new GatewayTimeoutError();

  return new Promise<T>((resolve, reject) => {
    const cleanup = () => signal.removeEventListener('abort', abort);
    const abort = () => {
      cleanup();
      try {
        onAbort();
      } catch {
        // Cleanup failure cannot replace the stable deadline result.
      }
      reject(new GatewayTimeoutError());
    };
    signal.addEventListener('abort', abort, { once: true });
    work.then(
      (value) => {
        cleanup();
        resolve(value);
      },
      (error) => {
        cleanup();
        reject(error);
      }
    );
  });
}

export type ValidatedResponse<T = unknown> = {
  status: number;
  headers: Record<string, string>;
  body: T;
  /** `true` once the response has passed the full per-operation validation pipeline (spec §6.8) — `forward()` always returns `validated: true` or throws; there is no partially-validated result. */
  validated: true;
};

export type DomainProxyConfig = {
  baseUrl: string;
  maxConnections: number; // DOMAIN_API_MAX_CONNECTIONS, default 20
  maxRetries: number; // DOMAIN_API_MAX_RETRIES, default 1
  headersTimeoutMs: number; // DOMAIN_API_HEADERS_TIMEOUT_MS, default 10_000
  bodyTimeoutMs: number; // DOMAIN_API_BODY_TIMEOUT_MS, default 30_000 (stall timeout, not total)
};

type ForwardParams = {
  tokenIssuer: DelegatedTokenIssuer;
  req: NextApiRequest;
  res: NextApiResponse;
  slug: string;
  pathParams: Record<string, string>;
  query: URLSearchParams;
  signal: AbortSignal;
  idempotencyKey: string | undefined;
  commandIdentity?: string;
  requestId: string;
  traceId: string;
  conditionalHeaders: Record<string, string>;
  uiLanguage?: SupportedLocale;
  body?: unknown;
};

type DispatchParams = {
  tokenIssuer: DelegatedTokenIssuer;
  req: NextApiRequest;
  res: NextApiResponse;
  slug: string;
  path: string;
  serializedBody: string | undefined;
  signal: AbortSignal;
  idempotencyKey: string | undefined;
  commandIdentity?: string;
  requestId: string;
  traceId: string;
  conditionalHeaders: Record<string, string>;
  uiLanguage?: SupportedLocale;
};

const RETRYABLE_ERROR_SUBSTRINGS = [
  'ECONNREFUSED',
  'ECONNRESET',
  'UND_ERR_CONNECT_TIMEOUT',
  'UND_ERR_SOCKET',
];

function isRetryableError(error: unknown): boolean {
  if (!(error instanceof Error)) return false;
  // undici does not consistently put its error code in `message` — a
  // connection abruptly closed before headers arrive surfaces as
  // `SocketError { code: 'UND_ERR_SOCKET', message: 'other side closed' }`,
  // which no message-substring check matches. Node's own connection errors
  // (`ECONNREFUSED`, `ECONNRESET`) do embed the code in `message`. Checking
  // both `code` and `message` covers both shapes without narrowing which
  // network failures are eligible for the Phase 1 retry (spec §4.9).
  const code = (error as NodeJS.ErrnoException).code;
  if (typeof code === 'string' && RETRYABLE_ERROR_SUBSTRINGS.includes(code)) {
    return true;
  }
  return RETRYABLE_ERROR_SUBSTRINGS.some((needle) =>
    error.message.includes(needle)
  );
}

/** Reads a response body up to `maxBytes`; throws `DomainProxyError` rather than truncate (spec §4.5). */
async function readBoundedBody(
  body: AsyncIterable<Buffer | Uint8Array>,
  maxBytes: number,
  operationId: string
): Promise<string> {
  const chunks: Buffer[] = [];
  let total = 0;
  for await (const chunk of body) {
    const buf = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    total += buf.length;
    if (total > maxBytes) {
      throw new DomainProxyError(
        502,
        'invalid_domain_response',
        `Response from ${operationId} exceeded ${maxBytes} bytes`
      );
    }
    chunks.push(buf);
  }
  return Buffer.concat(chunks).toString('utf-8');
}

/**
 * The server-only HTTP client `DomainProxy.forward()` uses to reach FastAPI.
 * One instance per process — the `undici` `Pool` it wraps holds a keep-alive
 * connection pool that should not be recreated per request.
 */
export class DomainProxy {
  private readonly config: DomainProxyConfig;
  private readonly pool: Pool;
  private readonly registry: ReadonlyMap<string, OperationEntry>;
  private readonly validators: ReadonlyMap<string, ValidateFunction>;

  constructor(
    config: DomainProxyConfig,
    registry: ReadonlyMap<string, OperationEntry> = new Map(),
    validators: ReadonlyMap<string, ValidateFunction> = new Map()
  ) {
    this.config = config;
    this.registry = registry;
    this.validators = validators;
    this.pool = new Pool(config.baseUrl, {
      connections: config.maxConnections,
    });
  }

  /** Closes the underlying connection pool. Tests only — a live server keeps one pool for its lifetime. */
  async close(): Promise<void> {
    await this.pool.close();
  }

  async forward<T = unknown>(
    operationId: string,
    params: ForwardParams
  ): Promise<ValidatedResponse<T>> {
    const entry = this.registry.get(operationId);
    if (!entry) {
      throw new Error(`Unknown operationId: ${operationId}`);
    }

    // Build the FastAPI path from the template, path params, and query string.
    let path = entry.pathTemplate;
    for (const param of entry.pathParams) {
      const value = params.pathParams[param];
      path = path.replace(`{${param}}`, encodeURIComponent(value ?? ''));
    }
    const qs = params.query.toString();
    if (qs.length > 0) {
      path += `?${qs}`;
    }

    // Serialize the body once — forward() owns serialization, not the caller.
    const serializedBody =
      'body' in params && params.body !== undefined
        ? JSON.stringify(params.body)
        : undefined;

    return this.dispatchWithRetry<T>(entry, {
      tokenIssuer: params.tokenIssuer,
      req: params.req,
      res: params.res,
      slug: params.slug,
      path,
      serializedBody,
      signal: params.signal,
      idempotencyKey: params.idempotencyKey,
      commandIdentity: params.commandIdentity,
      requestId: params.requestId,
      traceId: params.traceId,
      conditionalHeaders: params.conditionalHeaders,
      uiLanguage: params.uiLanguage,
    });
  }

  private isRetryableOperation(
    entry: OperationEntry,
    idempotencyKey: string | undefined
  ): boolean {
    if (entry.method === 'GET') {
      return true;
    }
    return entry.requiresIdempotency && idempotencyKey !== undefined;
  }

  private async backoffWithJitter(
    attempt: number,
    deadline: AbortSignal
  ): Promise<void> {
    const baseMs = 100 * Math.pow(2, attempt);
    const jitterMs = Math.random() * baseMs;
    const delayMs = Math.min(baseMs + jitterMs, 2_000);
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(resolve, delayMs);
      deadline.addEventListener(
        'abort',
        () => {
          clearTimeout(timer);
          reject(new GatewayTimeoutError());
        },
        { once: true }
      );
    });
  }

  // Token is signed PER ATTEMPT (a retry may need a fresh token — the
  // previous one may be close to expiry after a backoff). Headers are built
  // per attempt because they include the token (spec §4.8).
  private async dispatchWithRetry<T>(
    entry: OperationEntry,
    params: DispatchParams
  ): Promise<ValidatedResponse<T>> {
    const MAX_RETRIES = this.config.maxRetries;
    const { signal } = params;
    const canRetry = this.isRetryableOperation(entry, params.idempotencyKey);

    let lastError: Error | null = null;

    for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
      if (signal.aborted) {
        throw new GatewayTimeoutError();
      }

      const token = await params.tokenIssuer.issueForRequest({
        req: params.req,
        res: params.res,
        workspaceSlug: params.slug,
        signal,
      });

      // Headers built from an empty set — never passed through from the
      // browser request or the FastAPI response (spec §4.4).
      const headers: Record<string, string> = {
        Authorization: `Bearer ${token}`,
        Accept: 'application/json',
        'X-Request-Id': params.requestId,
        'X-Trace-Id': params.traceId,
        ...params.conditionalHeaders,
      };

      if (params.serializedBody !== undefined) {
        headers['Content-Type'] = 'application/json';
        headers['Content-Length'] = String(
          Buffer.byteLength(params.serializedBody, 'utf-8')
        );
      }

      if (params.idempotencyKey !== undefined) {
        headers['Idempotency-Key'] = params.idempotencyKey;
      }
      if (params.commandIdentity !== undefined) {
        headers['X-Command-Identity'] = params.commandIdentity;
      }
      // The language of text FastAPI writes for this request (#255). Set by
      // the BFF from the session's preference, never copied from the browser.
      if (params.uiLanguage !== undefined) {
        headers['X-UI-Language'] = params.uiLanguage;
      }

      // Phase 1: send the request, receive response headers. undici's
      // pool.request() resolves once headers arrive — the body stream has
      // not been consumed yet. Only a Phase 1 failure (no headers received)
      // is eligible for retry.
      let response: Dispatcher.ResponseData;
      try {
        response = await this.pool.request({
          method: entry.method,
          path: params.path,
          headers,
          body: params.serializedBody,
          signal,
          headersTimeout: this.config.headersTimeoutMs,
          bodyTimeout: this.config.bodyTimeoutMs,
        });
      } catch (error) {
        if (signal.aborted) {
          throw new GatewayTimeoutError();
        }
        if (!canRetry || !isRetryableError(error) || attempt >= MAX_RETRIES) {
          throw error;
        }
        lastError = error as Error;
        await this.backoffWithJitter(attempt, signal);
        continue;
      }

      // Phase 2: response headers received — never retry from this point.
      // Body-read I/O failure and schema/validation failure use distinct
      // error codes for observability, but both are unrecoverable.
      let raw: string;
      try {
        raw = await awaitWithinDeadline(
          readBoundedBody(
            response.body,
            entry.maxResponseBytes,
            entry.operationId
          ),
          signal,
          () => response.body.destroy(new GatewayTimeoutError())
        );
      } catch (bodyReadError) {
        await response.body.dump().catch(() => {});
        if (signal.aborted) {
          throw new GatewayTimeoutError();
        }
        if (bodyReadError instanceof DomainProxyError) {
          throw bodyReadError;
        }
        throw new DomainProxyError(
          502,
          'domain_response_incomplete',
          `Response body read failed for ${entry.operationId}`
        );
      }

      return this.validateParsedResponse<T>(entry, response, raw, params.slug);
    }

    throw lastError ?? new GatewayTimeoutError();
  }

  /**
   * Full per-status response validation pipeline (spec §6.7, §6.8):
   * declared statuses validate media type + schema + Location shape; a
   * well-formed undeclared Problem Details error passes through
   * transparently; everything else fails closed with 502
   * `invalid_domain_response`.
   */
  private validateParsedResponse<T>(
    entry: OperationEntry,
    response: Dispatcher.ResponseData,
    raw: string,
    slug: string
  ): ValidatedResponse<T> {
    const status = response.statusCode;
    const responseShape: ResponseShape | undefined = entry.responses[status];

    // Step 1: undeclared status.
    if (!responseShape) {
      if (status >= 400) {
        const contentType = (
          (response.headers['content-type'] as string | undefined) ?? ''
        )
          .split(';')[0]
          .trim()
          .toLowerCase();
        if (contentType === 'application/problem+json' && raw.length > 0) {
          let parsed: unknown;
          try {
            parsed = JSON.parse(raw);
          } catch {
            parsed = undefined;
          }
          if (parsed !== undefined) {
            const problemValidator = this.validators.get('problem_details');
            if (problemValidator && problemValidator(parsed)) {
              return {
                status,
                headers: this.buildSafeResponseHeaders(response),
                body: parsed as T,
                validated: true,
              };
            }
          }
        }
      }
      throw new DomainProxyError(
        502,
        'invalid_domain_response',
        `Unexpected status ${status} from ${entry.operationId}`
      );
    }

    // Step 2: Content-Type check (skip for bodiless responses).
    if (responseShape.body !== 'none') {
      const contentType =
        (response.headers['content-type'] as string | undefined) ?? '';
      const mediaType = contentType.split(';')[0].trim().toLowerCase();
      if (!responseShape.mediaTypes.includes(mediaType)) {
        throw new DomainProxyError(
          502,
          'invalid_domain_response',
          `Unexpected Content-Type "${mediaType}" from ${entry.operationId}`
        );
      }
    }

    // Step 3: body validation.
    let parsedBody: unknown = undefined;
    if (responseShape.body === 'none') {
      if (raw.length > 0) {
        throw new DomainProxyError(
          502,
          'invalid_domain_response',
          `Expected empty body for status ${status} from ${entry.operationId}`
        );
      }
    } else {
      try {
        parsedBody = JSON.parse(raw);
      } catch {
        throw new DomainProxyError(
          502,
          'invalid_domain_response',
          `Invalid JSON from ${entry.operationId}`
        );
      }

      const validator = this.validators.get(responseShape.body.validatorId);
      if (!validator) {
        throw new DomainProxyError(
          502,
          'invalid_domain_response',
          `Missing response validator "${responseShape.body.validatorId}" — fail closed`
        );
      }
      if (!validator(parsedBody)) {
        throw new DomainProxyError(
          502,
          'invalid_domain_response',
          `Response body failed validation for ${entry.operationId} status ${status}`
        );
      }
    }

    // Step 4: Location rewriting — invalid Location is 502, not silent drop.
    const responseHeaders = this.buildSafeResponseHeaders(response);
    const rawLocation = response.headers['location'] as string | undefined;
    if (rawLocation) {
      const rewritten = this.rewriteLocation(
        rawLocation,
        responseShape.locationShapes,
        slug
      );
      if (rewritten === null) {
        throw new DomainProxyError(
          502,
          'invalid_domain_response',
          `Invalid Location header from ${entry.operationId}`
        );
      }
      responseHeaders['Location'] = rewritten;
    }

    return {
      status,
      headers: responseHeaders,
      body: parsedBody as T,
      validated: true,
    };
  }

  private rewriteLocation(
    rawLocation: string,
    shapes: readonly LocationShape[],
    slug: string
  ): string | null {
    for (const shape of shapes) {
      const match = shape.pattern.exec(rawLocation);
      if (match) {
        const extracted: { slug: string } & Record<string, string> = { slug };
        for (const paramName of shape.extractParams) {
          const value = match.groups?.[paramName];
          if (!value) return null;
          extracted[paramName] = value;
        }
        try {
          return buildBffPath(shape.targetOperationId, extracted);
        } catch {
          return null;
        }
      }
    }
    return null;
  }

  private buildSafeResponseHeaders(
    response: Dispatcher.ResponseData
  ): Record<string, string> {
    const headers: Record<string, string> = {};
    // Only forward safe response headers — never the upstream
    // Content-Length (the proxy re-serializes the body) or an internal
    // trace header. Cache-Control is forced, not copied.
    const safeHeaders = ['content-type', 'etag', 'retry-after'];
    for (const h of safeHeaders) {
      const value = response.headers[h];
      if (typeof value === 'string') {
        headers[h] = value;
      }
    }
    headers['Cache-Control'] = 'no-store';
    return headers;
  }
}
