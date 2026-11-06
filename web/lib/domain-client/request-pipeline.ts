/**
 * The BFF domain-proxy request pipeline building blocks (spec §4.1–§4.7,
 * §4.10). Factored out of the catch-all route handler so each step —
 * host/path/origin validation, route matching, body reading, idempotency
 * derivation, and response writing — is independently testable and the
 * handler itself stays a readable ordered list of steps.
 */
import { createHash, randomUUID } from 'node:crypto';
import type { NextApiRequest, NextApiResponse } from 'next';
import type { ErrorObject } from 'ajv/dist/core';
import type { ZodError } from 'zod';

import { ApiError } from '../delegated-identity/token-issuer';
import { awaitWithinDeadline, type ValidatedResponse } from './proxy';
import { DOMAIN_ALLOWLIST } from './generated/allowlist';
import type { AnyAllowlistEntry } from './allowlist-types';
import { problemDetails } from './problem-details';

export { ApiError };

export function generateRequestId(): string {
  return `req_${randomUUID()}`;
}

/** Accepts only the browser's documented trace-ID shape; anything else is ignored, not forwarded. */
export function normalizeBrowserTraceId(
  raw: string | string[] | undefined
): string | null {
  if (typeof raw !== 'string') return null;
  if (raw.length < 8 || raw.length > 128) return null;
  if (!/^[a-zA-Z0-9_-]+$/.test(raw)) return null;
  return raw;
}

/** Opaque browser action identity accepted for idempotency and Library outcome recovery. */
export function normalizeCommandIdentity(raw: string): string | null {
  if (raw.length < 8 || raw.length > 128) return null;
  if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$/.test(raw)) return null;
  return raw;
}

/** The `Host` header (including port when non-default) must exactly match `APP_URL` — prevents DNS-rebinding attacks (spec §4.1). */
export function validateHost(req: NextApiRequest, appUrl: URL): void {
  const hostHeader = req.headers.host;
  if (!hostHeader) {
    throw new ApiError(400, 'missing_host');
  }
  const expected = appUrl.port
    ? `${appUrl.hostname}:${appUrl.port}`
    : appUrl.hostname;
  if (hostHeader !== expected) {
    throw new ApiError(400, 'invalid_host');
  }
}

/** Origin/CSRF checks apply only to non-safe methods — GET/HEAD typically omit `Origin` (spec §4.1). */
export function validateOriginIfNonSafe(
  req: NextApiRequest,
  appUrl: URL
): void {
  const method = (req.method ?? 'GET').toUpperCase();
  if (method === 'GET' || method === 'HEAD') {
    return;
  }
  const origin = req.headers.origin;
  if (!origin) {
    throw new ApiError(403, 'missing_origin');
  }
  if (origin !== appUrl.origin) {
    throw new ApiError(403, 'origin_not_allowed');
  }
}

/**
 * Validates and normalizes `[slug, ...pathSegments]` before any other
 * processing — including route matching and session access (spec §4.4).
 * Rejects residual/double percent-encoding, encoded slashes/backslashes,
 * NUL bytes, `.`/`..` segments and any decoded segment containing a path
 * separator.
 */
export function validatePathSegments(all: readonly string[]): boolean {
  for (const seg of all) {
    if (!seg) return false;
    let decoded: string;
    try {
      decoded = decodeURIComponent(seg);
    } catch {
      return false;
    }
    if (decoded === '.' || decoded === '..') return false;
    if (
      decoded.includes('\0') ||
      decoded.includes('\\') ||
      decoded.includes('/')
    ) {
      return false;
    }
    if (decoded.includes('%')) return false;
  }
  return true;
}

export type MatchedAllowlistEntry = {
  entry: AnyAllowlistEntry;
  extractedParams: Record<string, string>;
};

/**
 * Matches `method` + the path segments after `/api/domain/v1/w/:slug/`
 * against `DOMAIN_ALLOWLIST` (spec §4.2, §4.10 step 4). Runs before session
 * access — an unknown route should never cost a database round trip.
 */
export function matchAllowlistEntry(
  method: string | undefined,
  pathSegments: readonly string[]
): MatchedAllowlistEntry | null {
  const upperMethod = (method ?? 'GET').toUpperCase();
  for (const entry of DOMAIN_ALLOWLIST) {
    const patternSegments = entry.bffPattern.split('/').filter(Boolean);
    const slugIndex = patternSegments.indexOf(':slug');
    const operationSegments = patternSegments.slice(slugIndex + 1);
    if (operationSegments.length !== pathSegments.length) continue;

    const extractedParams: Record<string, string> = {};
    let matched = true;
    for (let i = 0; i < operationSegments.length; i++) {
      const patternSeg = operationSegments[i];
      const actualSeg = pathSegments[i];
      if (patternSeg.startsWith(':')) {
        extractedParams[patternSeg.slice(1)] = decodeURIComponent(actualSeg);
      } else if (patternSeg !== actualSeg) {
        matched = false;
        break;
      }
    }
    if (matched && entry.method === upperMethod) {
      return { entry: entry as AnyAllowlistEntry, extractedParams };
    }
  }
  return null;
}

/**
 * Query parameters are parsed from the raw URL, not `req.query` — Next.js
 * populates `req.query` with both search params and dynamic route params
 * (`slug`, `path[]`), making the two impossible to distinguish (spec §4.4).
 */
export function extractSearchParams(
  rawUrl: string | undefined
): URLSearchParams {
  if (!rawUrl) return new URLSearchParams();
  const qIdx = rawUrl.indexOf('?');
  if (qIdx === -1) return new URLSearchParams();
  return new URLSearchParams(rawUrl.slice(qIdx + 1));
}

/** Turns Zod's parsed (not pre-validation) query output back into a URLSearchParams — defaults, coercions and stripping can never diverge from what is forwarded (spec §4.10 step 8). */
export function serializeValidatedQuery(
  data: Record<string, unknown>
): URLSearchParams {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(data)) {
    if (Array.isArray(value)) {
      for (const item of value) params.append(key, String(item));
    } else if (value !== undefined && value !== null) {
      params.append(key, String(value));
    }
  }
  return params;
}

export function zodToFieldErrors(
  error: ZodError
): Array<{ field: string; code: string; detail: string }> {
  return error.issues.map((issue) => ({
    field: issue.path.join('.') || '(root)',
    code: issue.code,
    detail: issue.message,
  }));
}

export function ajvToFieldErrors(
  errors: readonly ErrorObject[] | null | undefined
): Array<{ field: string; code: string; detail: string }> {
  if (!errors) return [];
  return errors.map((err) => ({
    field: err.instancePath || '(root)',
    code: err.keyword,
    detail: err.message ?? 'Validation failed',
  }));
}

/** `SHA-256(operationId:intentId)` — FastAPI binds the key to workspace and actor server-side; the BFF derivation never includes them (spec §4.6). */
export function deriveIdempotencyKey(
  operationId: string,
  intentId: string
): string {
  return createHash('sha256')
    .update(`${operationId}:${intentId}`)
    .digest('hex');
}

/** Reads the raw request body up to `maxBytes`, aborting the stream and throwing 413 rather than buffering past the limit (spec §4.5). */
export async function readLimitedBody(
  req: NextApiRequest,
  maxBytes: number,
  signal: AbortSignal
): Promise<Buffer> {
  const read = (async () => {
    const chunks: Buffer[] = [];
    let totalBytes = 0;
    for await (const chunk of req) {
      totalBytes += chunk.length;
      if (totalBytes > maxBytes) {
        req.pause();
        throw new ApiError(413, 'request_body_too_large', false);
      }
      chunks.push(chunk);
    }
    return Buffer.concat(chunks);
  })();
  return awaitWithinDeadline(read, signal, () => req.pause());
}

/** For a no-body operation: 400 on any declared or actually-sent bytes, never a silent discard (spec §4.5). */
export async function assertNoRequestBody(
  req: NextApiRequest,
  signal: AbortSignal
): Promise<void> {
  const declaredLength = Number(req.headers['content-length'] ?? '0');
  if (Number.isFinite(declaredLength) && declaredLength > 0) {
    throw new ApiError(400, 'unexpected_body', false);
  }
  const read = (async () => {
    for await (const chunk of req) {
      if (chunk.length > 0) {
        req.pause();
        throw new ApiError(400, 'unexpected_body', false);
      }
    }
  })();
  await awaitWithinDeadline(read, signal, () => req.pause());
}

/**
 * Serializes the validated FastAPI response exactly once, recomputes
 * `Content-Length`, installs the BFF's own `X-Trace-Id`, and copies only
 * the already-filtered response headers `DomainProxy` built — never the
 * upstream `Content-Length` or `X-Trace-Id` (spec §4.10).
 */
export function writeProxyResponse(
  res: NextApiResponse,
  proxyResponse: ValidatedResponse,
  traceId: string
): void {
  res.status(proxyResponse.status);
  for (const [key, value] of Object.entries(proxyResponse.headers)) {
    res.setHeader(key, value);
  }
  res.setHeader('X-Trace-Id', traceId);

  if (
    proxyResponse.status === 204 ||
    proxyResponse.status === 304 ||
    proxyResponse.body === undefined
  ) {
    res.setHeader('Content-Length', '0');
    res.end();
    return;
  }

  const serialized = JSON.stringify(proxyResponse.body);
  res.setHeader(
    'Content-Length',
    String(Buffer.byteLength(serialized, 'utf-8'))
  );
  res.end(serialized);
}

/** Writes a boundary-failure Problem Details response (spec §9) — used by the handler's central catch. */
export function writeHandlerProblemResponse(
  res: NextApiResponse,
  status: number,
  code: string,
  traceId: string,
  retryable: boolean,
  fieldErrors?: Array<{ field: string; code: string; detail: string }>
): void {
  if (res.headersSent) return;
  res.status(status).json(
    problemDetails({
      title: code.replace(/_/g, ' '),
      status,
      code,
      traceId,
      retryable,
      fieldErrors,
    })
  );
}
