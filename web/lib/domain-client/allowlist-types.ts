/**
 * Server-only `AllowlistEntry` types (spec §4.3). Hand-written — not
 * generated — because it is the shared *shape* every generated
 * `DOMAIN_ALLOWLIST` entry (`./generated/allowlist.ts`) must satisfy, not
 * per-operation data. Imports Zod and re-exports the Ajv `ValidateFunction`
 * type, so this module must never be imported by browser code; the
 * browser-safe projection lives in `./generated/paths.ts` (spec §6.3).
 */
import type { z } from 'zod';
import type { ValidateFunction } from 'ajv/dist/core';

import type { DomainOperationMap } from './generated/operation-map';

export type OperationId = keyof DomainOperationMap;

export type RequestBody<K extends OperationId> =
  DomainOperationMap[K]['requestBody'];

type BodyOperationId = {
  [K in OperationId]: [RequestBody<K>] extends [never] ? never : K;
}[OperationId];

/** The union of every operation's request-body type — used to type a request body once Ajv has narrowed it (spec §4.10 step 10). */
export type AnyRequestBody = DomainOperationMap[BodyOperationId]['requestBody'];

export type AllowlistEntry<K extends OperationId> = {
  operationId: K;
  method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  /** Browser-facing path pattern with named params (`:slug`). Used for incoming request matching and Location rewriting. */
  bffPattern: string;
  /** FastAPI path template with `{param}` placeholders. */
  fastapiPathTemplate: string;
  /** Ordered list of path parameter names extracted from the pattern. */
  pathParams: readonly string[];
  /** `null` means no query parameters are allowed; any query string present is rejected. */
  querySchema: z.ZodType | null;
  /**
   * Pre-compiled Ajv validator for the request body JSON shape. `null`
   * means no body expected (must match `requestMediaTypes` being empty).
   * When `requestMediaTypes` is non-empty this must be non-null — the
   * allowlist binding (spec §6.2.4) validates the invariant at startup.
   */
  requestBodyValidator: [RequestBody<K>] extends [never]
    ? null
    : ValidateFunction<RequestBody<K>>;
  /** Allowed `Content-Type` values for the request body. Empty array means no body allowed. */
  requestMediaTypes: readonly string[];
  /** Hard limit on request body size. Exceeding returns 413. */
  maxRequestBytes: number;
  /** Hard limit on response body size from FastAPI. Exceeding returns 502 `invalid_domain_response`. */
  maxResponseBytes: number;
  /** Whether `X-Intent-Id` is required and an `Idempotency-Key` must be derived and forwarded. */
  requiresIdempotency: boolean;
  /** Whether `If-Match`/`If-None-Match` from the browser are forwarded to FastAPI. Default `false`. */
  forwardConditionalHeaders: boolean;
};

export type AnyAllowlistEntry = {
  [K in OperationId]: AllowlistEntry<K>;
}[OperationId];
