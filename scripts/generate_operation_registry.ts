/**
 * Generates `web/lib/domain-client/generated/operation-registry.ts` — the
 * `OPERATION_REGISTRY` runtime dispatch table `DomainProxy` uses for
 * per-status response validation and Location rewriting (spec §6.2.3).
 *
 * Single source of truth: operationId, method, path/query params, request
 * media types and `requiresIdempotency` come from the OpenAPI spec;
 * `maxRequestBytes`, `maxResponseBytes` and per-status Location shapes come
 * from the policy manifest. A missing policy entry is a generation-time
 * error (fail-closed) — see `policyFor` in `scripts/lib/spec.ts`.
 */
import * as path from 'node:path';

import {
  GENERATED_DIR,
  type LocationShapePolicy,
  type PolicyEntry,
  extractPathParamNames,
  isIdempotent,
  listOperations,
  loadPolicy,
  loadSpec,
  policyFor,
  requestMediaTypes,
  writeGenerated,
} from './lib/spec';

// The static type block every generated registry ships, verbatim from spec
// §6.2.3. COMMON_ERROR_RESPONSES is merged into every OperationEntry's
// `responses` at generation time; an operation-specific entry for the same
// status (declared directly in the OpenAPI spec) overrides it.
const STATIC_PREAMBLE = `export type ResponseShape = {
  mediaTypes: readonly string[];
  body: { validatorId: string } | "none";
  locationShapes: readonly LocationShape[];
};

export type LocationShape = {
  pattern: RegExp;
  targetOperationId: string;
  extractParams: readonly string[];
};

export type OperationEntry = {
  operationId: string;
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  pathTemplate: string;
  pathParams: readonly string[];
  queryParams: readonly string[];
  requestMediaTypes: readonly string[];
  requiresIdempotency: boolean;
  maxRequestBytes: number;
  maxResponseBytes: number;
  responses: Readonly<Record<number, ResponseShape>>;
};

// Shared errors are spread first in every entry below so an
// operation-specific declaration for the same status wins.
const COMMON_ERROR_RESPONSES: Readonly<Record<number, ResponseShape>> = {
  401: { mediaTypes: ["application/problem+json"], body: { validatorId: "problem_details" }, locationShapes: [] },
  403: { mediaTypes: ["application/problem+json"], body: { validatorId: "problem_details" }, locationShapes: [] },
  409: { mediaTypes: ["application/problem+json"], body: { validatorId: "problem_details" }, locationShapes: [] },
  422: { mediaTypes: ["application/problem+json"], body: { validatorId: "problem_details" }, locationShapes: [] },
  500: { mediaTypes: ["application/problem+json"], body: { validatorId: "problem_details" }, locationShapes: [] },
};
`;

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function queryParamNames(operation: any): string[] {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return (operation.parameters ?? [])
    .filter((p: any) => p.in === 'query')
    .map((p: any) => p.name);
}

function locationShapeLiteral(shape: LocationShapePolicy): string {
  return (
    `{ pattern: ${shape.pattern}, ` +
    `targetOperationId: ${JSON.stringify(shape.targetOperationId)}, ` +
    `extractParams: ${JSON.stringify(shape.extractParams)} }`
  );
}

function responseShapeLiteral(
  status: string,
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  responseDef: any,
  operationId: string,
  policyEntry: PolicyEntry
): string {
  const numericStatus = Number(status);
  const isError = Number.isInteger(numericStatus) && numericStatus >= 400;
  const hasContent = Boolean(responseDef.content?.['application/json']);
  const mediaTypes = isError
    ? ['application/problem+json']
    : responseDef.content
      ? Object.keys(responseDef.content)
      : [];
  const bodyLiteral = isError
    ? '{ validatorId: "problem_details" }'
    : hasContent
      ? `{ validatorId: ${JSON.stringify(`${operationId}_${status}`)} }`
      : '"none"';
  const shapes = policyEntry.locationShapes?.[status] ?? [];
  const locationShapesLiteral =
    shapes.length === 0
      ? '[]'
      : `[${shapes.map(locationShapeLiteral).join(', ')}]`;
  return `{ mediaTypes: ${JSON.stringify(mediaTypes)}, body: ${bodyLiteral}, locationShapes: ${locationShapesLiteral} }`;
}

function main(): void {
  const spec = loadSpec();
  const policy = loadPolicy();
  const operations = listOperations(spec);

  const entryLiterals = operations.map(
    ({ operationId, method, pathTemplate, operation }) => {
      const policyEntry = policyFor(policy, operationId);
      const pathParams = extractPathParamNames(pathTemplate);
      const queryParams = queryParamNames(operation);
      const mediaTypes = requestMediaTypes(operation);

      const responseEntries = Object.entries<any>(operation.responses ?? {})
        .filter(([status]) => Number.isInteger(Number(status)))
        .map(
          ([status, def]) =>
            `        ${status}: ${responseShapeLiteral(status, def, operationId, policyEntry)},`
        )
        .join('\n');

      return (
        `  [\n` +
        `    ${JSON.stringify(operationId)},\n` +
        `    {\n` +
        `      operationId: ${JSON.stringify(operationId)},\n` +
        `      method: ${JSON.stringify(method)},\n` +
        `      pathTemplate: ${JSON.stringify(pathTemplate)},\n` +
        `      pathParams: ${JSON.stringify(pathParams)},\n` +
        `      queryParams: ${JSON.stringify(queryParams)},\n` +
        `      requestMediaTypes: ${JSON.stringify(mediaTypes)},\n` +
        `      requiresIdempotency: ${isIdempotent(operation)},\n` +
        `      maxRequestBytes: ${policyEntry.maxRequestBytes},\n` +
        `      maxResponseBytes: ${policyEntry.maxResponseBytes},\n` +
        `      responses: {\n` +
        `        ...COMMON_ERROR_RESPONSES,\n` +
        `${responseEntries}\n` +
        `      },\n` +
        `    },\n` +
        `  ],`
      );
    }
  );

  const body =
    `${STATIC_PREAMBLE}\n` +
    `export const OPERATION_REGISTRY: ReadonlyMap<string, OperationEntry> = new Map([\n` +
    `${entryLiterals.join('\n')}\n` +
    `]);\n`;

  writeGenerated(
    path.join(GENERATED_DIR, 'operation-registry.ts'),
    'generate_operation_registry.ts',
    body
  );
}

main();
