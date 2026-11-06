import type { NextApiRequest, NextApiResponse } from 'next';
import type { ValidateFunction } from 'ajv/dist/core';

import env from '@/lib/env';
import { prisma } from '@/lib/prisma';
import { requireSession } from '@/lib/requireSession';
import {
  getCsrfVerifier,
  getTokenIssuer,
} from '@/lib/delegated-identity/instances';
import { getDomainProxy } from '@/lib/domain-client/instances';
import { LOCALE_COOKIE, resolveLocale } from '@/lib/i18n/locales';
import {
  DomainProxyError,
  GatewayTimeoutError,
  awaitWithinDeadline,
  type ValidatedResponse,
} from '@/lib/domain-client/proxy';
import {
  ApiError,
  assertNoRequestBody,
  extractSearchParams,
  generateRequestId,
  matchAllowlistEntry,
  normalizeCommandIdentity,
  normalizeBrowserTraceId,
  readLimitedBody,
  serializeValidatedQuery,
  validateHost,
  validateOriginIfNonSafe,
  validatePathSegments,
  writeHandlerProblemResponse,
  writeProxyResponse,
  zodToFieldErrors,
  ajvToFieldErrors,
  deriveIdempotencyKey,
} from '@/lib/domain-client/request-pipeline';

/**
 * BFF catch-all domain proxy — `GET|POST|PUT|PATCH|DELETE
 * /api/domain/v1/w/{slug}/**` (spec §4.10). Implements the full 13-step
 * pipeline: correlation/deadline, host validation, path normalization,
 * route matching, origin validation, session verification, CSRF
 * verification, query validation, idempotency, body validation, conditional
 * headers, `DomainProxy.forward()`, and response writing.
 *
 * The allowlist (`DOMAIN_ALLOWLIST`, generated) mixes operations with no
 * request body/query params (e.g. `get_identity_probe`) alongside
 * body- and query-bearing operations (e.g. `list_library_entries`) — the
 * request-body and query-validation branches below both run their
 * "nothing declared" path for the former and their validating path for the
 * latter, without any change to this handler.
 */
export const config = {
  api: {
    bodyParser: false,
  },
};

export default async function handler(
  req: NextApiRequest,
  res: NextApiResponse
): Promise<void> {
  // Step 1: correlation and one absolute deadline — before any I/O.
  const deadline = AbortSignal.timeout(env.totalDeadlineMs);
  const requestId = generateRequestId();
  const traceId =
    normalizeBrowserTraceId(req.headers['x-trace-id']) ?? requestId;

  try {
    const appUrl = new URL(env.appUrl);

    // Step 2: host validation.
    validateHost(req, appUrl);

    // Step 3: path normalization — before route matching or session access.
    const rawSlug = req.query.slug;
    const rawPath = req.query.path;
    if (
      typeof rawSlug !== 'string' ||
      !Array.isArray(rawPath) ||
      !rawPath.every(
        (segment): segment is string => typeof segment === 'string'
      ) ||
      !validatePathSegments([rawSlug, ...rawPath])
    ) {
      writeHandlerProblemResponse(res, 400, 'invalid_path', traceId, false);
      return;
    }
    const slug = rawSlug;
    const pathSegments = rawPath;

    // Step 4: route matching — before session (avoid a DB hit for unknown routes).
    const matched = matchAllowlistEntry(req.method, pathSegments);
    if (!matched) {
      writeHandlerProblemResponse(res, 404, 'route_not_found', traceId, false);
      return;
    }
    const { entry: matchedEntry, extractedParams } = matched;

    // Step 5: origin validation — non-safe methods only.
    validateOriginIfNonSafe(req, appUrl);

    // Step 6: session verification.
    const session = await awaitWithinDeadline(
      requireSession(req, res),
      deadline
    );

    // Step 7: CSRF verification — non-safe methods only.
    await awaitWithinDeadline(getCsrfVerifier().verify(req, session), deadline);

    // Step 8: query validation — parsed from the raw URL, not req.query
    // (req.query also carries slug/path[]). Operations without a declared
    // querySchema reject any query string. Repeated keys are preserved as
    // arrays (via getAll(), not Object.fromEntries) so Zod z.array() schemas
    // can validate them (spec §9.3) — a single-valued key stays a plain
    // string for backward compatibility.
    const rawSearchParams = extractSearchParams(req.url);
    let validatedQuery = new URLSearchParams();
    if (matchedEntry.querySchema) {
      const collapsed: Record<string, unknown> = {};
      const seenKeys = new Set<string>(Array.from(rawSearchParams.keys()));
      seenKeys.forEach((key) => {
        const values = rawSearchParams.getAll(key);
        collapsed[key] = values.length === 1 ? values[0] : values;
      });
      const parsed = matchedEntry.querySchema.safeParse(collapsed);
      if (!parsed.success) {
        writeHandlerProblemResponse(
          res,
          400,
          'invalid_query',
          traceId,
          false,
          zodToFieldErrors(parsed.error)
        );
        return;
      }
      validatedQuery = serializeValidatedQuery(
        parsed.data as Record<string, unknown>
      );
    } else if (rawSearchParams.size > 0) {
      writeHandlerProblemResponse(res, 400, 'unexpected_query', traceId, false);
      return;
    }

    // Step 9: idempotency.
    let idempotencyKey: string | undefined;
    let commandIdentity: string | undefined;
    if (matchedEntry.requiresIdempotency) {
      const intentId = req.headers['x-intent-id'];
      if (typeof intentId !== 'string' || intentId.length === 0) {
        writeHandlerProblemResponse(
          res,
          400,
          'missing_intent_id',
          traceId,
          false
        );
        return;
      }
      const normalizedIntentId = normalizeCommandIdentity(intentId);
      if (normalizedIntentId === null) {
        writeHandlerProblemResponse(
          res,
          400,
          'invalid_command_identity',
          traceId,
          false
        );
        return;
      }
      idempotencyKey = deriveIdempotencyKey(
        matchedEntry.operationId,
        normalizedIntentId
      );
      // The browser cannot choose a FastAPI header directly. Only a validated
      // Library intent crossing this authenticated, Workspace-checked BFF
      // boundary becomes an authoritative command identity.
      if (pathSegments[0] === 'library') {
        commandIdentity = normalizedIntentId;
      }
    }

    // Step 10: body reading, Content-Type validation, and request body validation.
    let parsedBody: unknown;
    if (matchedEntry.requestMediaTypes.length > 0) {
      const contentType = (req.headers['content-type'] ?? '')
        .split(';')[0]
        .trim()
        .toLowerCase();
      if (!matchedEntry.requestMediaTypes.includes(contentType)) {
        writeHandlerProblemResponse(
          res,
          415,
          'unsupported_media_type',
          traceId,
          false
        );
        return;
      }

      const rawBody = await readLimitedBody(
        req,
        matchedEntry.maxRequestBytes,
        deadline
      );

      let candidateBody: unknown;
      try {
        candidateBody = JSON.parse(rawBody.toString('utf-8'));
      } catch {
        writeHandlerProblemResponse(res, 400, 'invalid_json', traceId, false);
        return;
      }

      // Widened from the allowlist's precise per-operation conditional type
      // (`AllowlistEntry<K>['requestBodyValidator']`, spec §4.3): with only
      // one, body-less operation registered today, that type resolves to
      // exactly `null`, and TypeScript correctly proves the call below is
      // unreachable — this handler is written for the allowlist Milestone 2
      // will populate with body-bearing operations, not just today's.
      const requestValidator =
        matchedEntry.requestBodyValidator as ValidateFunction<unknown> | null;
      if (requestValidator === null) {
        // Generated binding/typecheck should make this unreachable — the
        // allowlist binding (spec §6.2.4) fails startup otherwise. Kept as
        // a fail-closed runtime boundary in case the generated artifacts
        // are ever corrupted independently of the type system.
        throw new ApiError(500, 'bff_contract_misconfigured');
      }
      if (!requestValidator(candidateBody)) {
        writeHandlerProblemResponse(
          res,
          422,
          'request_validation_failed',
          traceId,
          false,
          ajvToFieldErrors(requestValidator.errors)
        );
        return;
      }
      parsedBody = candidateBody;
    } else {
      await assertNoRequestBody(req, deadline);
    }

    // Lifecycle is authoritative at the BFF command boundary. Perform this
    // lookup only after all cheap shape/contract checks, and keep safe reads
    // available while the domain projection is still provisioning.
    let commandWorkspace: { id: string; lifecycle: string } | null | undefined;
    if (!['GET', 'HEAD', 'OPTIONS'].includes(req.method ?? '')) {
      commandWorkspace = await awaitWithinDeadline(
        prisma.team.findUnique({
          where: { slug },
          select: { id: true, lifecycle: true },
        }),
        deadline
      );
      if (!commandWorkspace) {
        writeHandlerProblemResponse(
          res,
          404,
          'workspace_not_found',
          traceId,
          false
        );
        return;
      }
      if (commandWorkspace.lifecycle !== 'ACTIVE') {
        writeHandlerProblemResponse(
          res,
          423,
          'workspace_provisioning',
          traceId,
          true
        );
        return;
      }
    }

    // Step 11: extract conditional headers (If-Match, If-None-Match) when the allowlist permits.
    const conditionalHeaders: Record<string, string> = {};
    if (matchedEntry.forwardConditionalHeaders) {
      const ifMatch = req.headers['if-match'];
      if (typeof ifMatch === 'string') conditionalHeaders['If-Match'] = ifMatch;
      const ifNoneMatch = req.headers['if-none-match'];
      if (typeof ifNoneMatch === 'string') {
        conditionalHeaders['If-None-Match'] = ifNoneMatch;
      }
    }

    // Step 12: proxy to FastAPI via DomainProxy (spec §6.7) with deadline, retries, token issuance.
    const proxyParams = {
      tokenIssuer: getTokenIssuer(),
      req,
      res,
      slug,
      pathParams: extractedParams,
      query: validatedQuery,
      signal: deadline,
      idempotencyKey,
      commandIdentity,
      requestId,
      traceId,
      conditionalHeaders,
      // The page's own precedence (middleware.ts): saved preference, cookie,
      // browser. Only a supported locale can come out, and FastAPI uses it
      // for wording alone -- Workspace and actor still come from the token.
      uiLanguage: resolveLocale({
        userLocale: session.user.locale,
        cookieLocale: req.cookies?.[LOCALE_COOKIE],
        acceptLanguage: req.headers['accept-language'],
      }),
      ...(matchedEntry.requestBodyValidator !== null
        ? { body: parsedBody }
        : {}),
    };
    const proxyResponse: ValidatedResponse = await getDomainProxy().forward(
      matchedEntry.operationId,
      proxyParams
    );

    // Step 13: send the validated response to the browser.
    if (deadline.aborted) throw new GatewayTimeoutError();
    writeProxyResponse(res, proxyResponse, traceId);
  } catch (error) {
    if (res.headersSent) return;
    if (error instanceof GatewayTimeoutError || deadline.aborted) {
      writeHandlerProblemResponse(res, 504, 'gateway_timeout', traceId, true);
      return;
    }
    if (error instanceof DomainProxyError || error instanceof ApiError) {
      writeHandlerProblemResponse(
        res,
        error.status,
        error.code,
        traceId,
        error.retryable
      );
      return;
    }
    writeHandlerProblemResponse(res, 502, 'domain_unavailable', traceId, true);
  }
}
