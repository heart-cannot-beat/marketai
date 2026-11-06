/**
 * RFC 9457 Problem Details — construction, response writing, and parsing
 * (spec §4.10, §6.8, §9).
 *
 * The BFF is both a producer of Problem Details (its own boundary failures —
 * invalid path, CSRF failure, gateway timeout) and a consumer of them (an
 * error response FastAPI returns is only ever forwarded to the browser after
 * `parseProblemDetails` confirms it is well-formed; an undeclared error
 * status with a body that fails this parse becomes a BFF-originated 502
 * `invalid_domain_response` instead — spec §6.8 step 4).
 */
import type { NextApiResponse } from 'next';

export type ProblemDetailsInput = {
  type?: string;
  title: string;
  status: number;
  code: string;
  detail?: string;
  traceId: string;
  retryable?: boolean;
  fieldErrors?: Array<{ field: string; code: string; detail: string }>;
};

export function problemDetails(
  input: ProblemDetailsInput
): Record<string, unknown> {
  return {
    type: input.type ?? `urn:marketai:error:${input.code.replace(/_/g, '-')}`,
    title: input.title,
    status: input.status,
    code: input.code,
    detail: input.detail ?? input.title,
    trace_id: input.traceId,
    retryable: input.retryable ?? false,
    field_errors: input.fieldErrors ?? [],
  };
}

export function writeProblemResponse(
  res: NextApiResponse,
  status: number,
  code: string,
  traceId: string,
  retryable: boolean
): void {
  if (res.headersSent) return;
  res.status(status).json(
    problemDetails({
      title: code.replace(/_/g, ' '),
      status,
      code,
      traceId,
      retryable,
    })
  );
}

/** A Problem Details body that has passed shape validation (`parseProblemDetails`). */
export type ParsedProblemDetails = {
  type: string;
  title: string;
  status: number;
  code: string;
  detail: string;
  traceId: string;
  retryable: boolean;
  fieldErrors: Array<{ field: string; code: string; detail: string }>;
};

const isFieldError = (
  value: unknown
): value is { field: string; code: string; detail: string } =>
  typeof value === 'object' &&
  value !== null &&
  typeof (value as Record<string, unknown>).field === 'string' &&
  typeof (value as Record<string, unknown>).code === 'string' &&
  typeof (value as Record<string, unknown>).detail === 'string';

/**
 * Validates a decoded JSON body as RFC 9457 Problem Details forwarded by
 * FastAPI. Returns `null` on any shape mismatch — callers must fail closed
 * (502 `invalid_domain_response`) rather than forward an unvalidated body to
 * the browser (spec §6.8 step 4).
 *
 * Only the fields this BFF's error contract fixes (`code`, `status`,
 * `trace_id`, `retryable`, `field_errors`) are required to be well-formed;
 * `type` and `detail` fall back the same way `problemDetails()` builds them.
 */
export function parseProblemDetails(raw: unknown): ParsedProblemDetails | null {
  if (typeof raw !== 'object' || raw === null) return null;
  const obj = raw as Record<string, unknown>;

  if (typeof obj.title !== 'string') return null;
  if (typeof obj.status !== 'number' || !Number.isInteger(obj.status))
    return null;
  if (typeof obj.code !== 'string' || obj.code.length === 0) return null;
  if (typeof obj.trace_id !== 'string' || obj.trace_id.length === 0)
    return null;

  let fieldErrors: Array<{ field: string; code: string; detail: string }> = [];
  if (obj.field_errors !== undefined) {
    if (!Array.isArray(obj.field_errors)) return null;
    for (const entry of obj.field_errors) {
      if (!isFieldError(entry)) return null;
    }
    fieldErrors = obj.field_errors as Array<{
      field: string;
      code: string;
      detail: string;
    }>;
  }

  return {
    type:
      typeof obj.type === 'string'
        ? obj.type
        : `urn:marketai:error:${obj.code.replace(/_/g, '-')}`,
    title: obj.title,
    status: obj.status,
    code: obj.code,
    detail: typeof obj.detail === 'string' ? obj.detail : obj.title,
    traceId: obj.trace_id,
    retryable: typeof obj.retryable === 'boolean' ? obj.retryable : false,
    fieldErrors,
  };
}
