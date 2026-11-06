/**
 * GENERATED FILE — do not edit by hand.
 * Generator: scripts/generate_allowlist_binding.ts
 * Sources: contracts/openapi/marketai-domain-v1.json, contracts/openapi/marketai-domain-v1-policy.json
 * Regenerate with scripts/generate_all.sh — CI fails the build on drift.
 */
import { OPERATION_REGISTRY, type ResponseShape } from './operation-registry';
import { DOMAIN_ALLOWLIST } from './allowlist';
import { BFF_ROUTES } from './paths';

function fail(message: string): never {
  throw new Error(`Allowlist binding validation failed: ${message}`);
}

/**
 * Checks performed (spec §6.2.4 table):
 *   - operationId resolution: every allowlist entry has a registry entry.
 *   - method match.
 *   - path parameter match.
 *   - request media type subset (allowlist ⊆ registry).
 *   - request validator coverage (body-bearing <-> non-null validator).
 *   - idempotency consistency.
 *   - response validation coverage: every declared status has a validator
 *     or is bodiless ("none") — enforced structurally by OperationEntry's
 *     type, checked here only for the shape, not the Map contents.
 *   - error response contract: every status >= 400 uses
 *     application/problem+json, except FastAPI's generated 422
 *     HTTPValidationError response, which is application/json and has its own
 *     operation-specific validator.
 *   - Location reverse mapping: every locationShape targetOperationId has
 *     an allowlist entry.
 *   - every allowlist entry has a matching BFF_ROUTES entry with the same bffPattern.
 *   - every registry operationId has an allowlist entry (no orphaned registry entries).
 */
export function validateAllowlistBinding(): void {
  const allowlistIds = new Set<string>(
    DOMAIN_ALLOWLIST.map((e) => e.operationId as string)
  );
  if (allowlistIds.size !== DOMAIN_ALLOWLIST.length) {
    fail('DOMAIN_ALLOWLIST contains a duplicate operationId');
  }

  for (const entry of DOMAIN_ALLOWLIST) {
    const registryEntry = OPERATION_REGISTRY.get(entry.operationId);
    if (!registryEntry) {
      fail(
        `operationId "${entry.operationId}" has no OPERATION_REGISTRY entry`
      );
    }

    if (registryEntry.method !== entry.method) {
      fail(
        `method mismatch for "${entry.operationId}": allowlist=${entry.method} registry=${registryEntry.method}`
      );
    }

    const registryParams = [...registryEntry.pathParams].sort();
    const allowlistParams = [...entry.pathParams].sort();
    if (JSON.stringify(registryParams) !== JSON.stringify(allowlistParams)) {
      fail(`path param mismatch for "${entry.operationId}"`);
    }

    const registryMediaTypes = new Set(registryEntry.requestMediaTypes);
    for (const mediaType of entry.requestMediaTypes) {
      if (!registryMediaTypes.has(mediaType)) {
        fail(
          `allowlist requestMediaTypes for "${entry.operationId}" is not a subset of the registry's`
        );
      }
    }

    const hasBody = registryEntry.requestMediaTypes.length > 0;
    if (hasBody && entry.requestBodyValidator === null) {
      fail(
        `"${entry.operationId}" accepts a request body but has no requestBodyValidator`
      );
    }
    if (!hasBody && entry.requestBodyValidator !== null) {
      fail(
        `"${entry.operationId}" declares a requestBodyValidator but accepts no request body`
      );
    }

    if (registryEntry.requiresIdempotency !== entry.requiresIdempotency) {
      fail(`requiresIdempotency mismatch for "${entry.operationId}"`);
    }

    for (const [status, shape] of Object.entries(registryEntry.responses)) {
      if (shape.body === 'none') continue;
      // FastAPI emits HTTPValidationError as application/json for request
      // validation failures. The registry's operation-specific 422
      // validator checks that body; all other error responses must use the
      // shared Problem Details media type.
      const isGeneratedValidationError =
        Number(status) === 422 &&
        shape.mediaTypes.includes('application/json') &&
        shape.body.validatorId === entry.operationId + '_422';
      if (
        Number(status) >= 400 &&
        !shape.mediaTypes.includes('application/problem+json') &&
        !isGeneratedValidationError
      ) {
        fail(
          `declared error status ${status} for "${entry.operationId}" does not use application/problem+json`
        );
      }
    }

    const bffRoute = BFF_ROUTES.find(
      (r) => r.operationId === entry.operationId
    );
    if (!bffRoute) {
      fail(
        `"${entry.operationId}" has an allowlist entry but no BFF_ROUTES entry`
      );
    } else if (bffRoute.bffPattern !== entry.bffPattern) {
      fail(
        `bffPattern mismatch for "${entry.operationId}" between paths.ts and allowlist.ts`
      );
    }
  }

  Array.from(OPERATION_REGISTRY.entries()).forEach(
    ([operationId, registryEntry]) => {
      if (!allowlistIds.has(operationId)) {
        fail(
          `operationId "${operationId}" is in OPERATION_REGISTRY but has no DOMAIN_ALLOWLIST entry`
        );
      }
      Object.values<ResponseShape>(registryEntry.responses).forEach((shape) => {
        shape.locationShapes.forEach((locationShape) => {
          if (!allowlistIds.has(locationShape.targetOperationId)) {
            fail(
              `locationShape targetOperationId "${locationShape.targetOperationId}" for "${operationId}" has no DOMAIN_ALLOWLIST entry`
            );
          }
        });
      });
    }
  );
}

// Runs at module load (server startup) — throws on any mismatch rather
// than surfacing as a runtime surprise on the first mismatched request.
validateAllowlistBinding();
