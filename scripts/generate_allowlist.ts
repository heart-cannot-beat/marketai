/**
 * Generates `web/lib/domain-client/generated/allowlist.ts` — the
 * server-only `DOMAIN_ALLOWLIST` (spec §6.3). Has Zod and Ajv; never
 * imported by browser code (the browser-safe projection is
 * `./generated/paths.ts`).
 *
 * `querySchema` is generated from each operation's OpenAPI `in: "query"`
 * parameters (`extractQueryParams`, `./lib/spec.ts`) as a `.strict()` Zod
 * object — unknown query keys are rejected (parameter-injection defense).
 * An operation with no query parameters still emits `querySchema: null`,
 * meaning any query string on that route is rejected outright.
 */
import * as path from "node:path";

import {
  GENERATED_DIR,
  extractPathParamNames,
  extractQueryParams,
  isIdempotent,
  listOperations,
  loadPolicy,
  loadSpec,
  policyFor,
  requestMediaTypes,
  writeGenerated,
  type QueryParamDef,
} from "./lib/spec";

/** Builds the Zod schema source expression for an operation's query params, or `'null'` when it has none. */
function buildQuerySchemaExpr(params: QueryParamDef[]): string {
  if (params.length === 0) return "null";
  const fields = params.map((p) => {
    let zodType: string;
    switch (p.type) {
      case "integer":
        zodType = "z.coerce.number().int()";
        if (p.minimum !== undefined) zodType += `.min(${p.minimum})`;
        if (p.maximum !== undefined) zodType += `.max(${p.maximum})`;
        break;
      case "boolean":
        zodType = "z.coerce.boolean()";
        break;
      default:
        if (p.enum) {
          zodType = `z.enum(${JSON.stringify(p.enum)})`;
        } else {
          zodType = "z.string()";
        }
    }
    if (!p.required) {
      zodType += ".optional()";
    }
    if (p.default !== undefined) {
      zodType += `.default(${JSON.stringify(p.default)})`;
    }
    return `    ${JSON.stringify(p.name)}: ${zodType},`;
  });
  return `z.object({\n${fields.join("\n")}\n  }).strict()`;
}

function main(): void {
  const spec = loadSpec();
  const policy = loadPolicy();
  const operations = listOperations(spec);

  let usesQuerySchemas = false;

  const entryLiterals = operations.map(
    ({ operationId, method, pathTemplate, operation }) => {
      const policyEntry = policyFor(policy, operationId);
      const pathParams = extractPathParamNames(pathTemplate);
      const mediaTypes = requestMediaTypes(operation);
      const hasBody = mediaTypes.length > 0;
      const requestBodyValidatorExpr = hasBody
        ? `REQUEST_VALIDATORS.${operationId}`
        : "null";

      const queryParams = extractQueryParams(operation);
      const querySchemaExpr = buildQuerySchemaExpr(queryParams);
      if (queryParams.length > 0) usesQuerySchemas = true;

      return (
        `  {\n` +
        `    operationId: ${JSON.stringify(operationId)},\n` +
        `    method: ${JSON.stringify(method)},\n` +
        `    bffPattern: ${JSON.stringify(policyEntry.bffPattern)},\n` +
        `    fastapiPathTemplate: ${JSON.stringify(pathTemplate)},\n` +
        `    pathParams: ${JSON.stringify(pathParams)},\n` +
        `    querySchema: ${querySchemaExpr},\n` +
        `    requestBodyValidator: ${requestBodyValidatorExpr},\n` +
        `    requestMediaTypes: ${JSON.stringify(mediaTypes)},\n` +
        `    maxRequestBytes: ${policyEntry.maxRequestBytes},\n` +
        `    maxResponseBytes: ${policyEntry.maxResponseBytes},\n` +
        `    requiresIdempotency: ${isIdempotent(operation)},\n` +
        `    forwardConditionalHeaders: ${policyEntry.forwardConditionalHeaders},\n` +
        `  },`
      );
    },
  );

  const usesRequestValidators = operations.some(
    ({ operation }) => requestMediaTypes(operation).length > 0,
  );
  const validatorsImport = usesRequestValidators
    ? '\nimport { REQUEST_VALIDATORS } from "./validators";'
    : "";
  const zodImport = usesQuerySchemas ? '\nimport { z } from "zod";' : "";

  const body = `import type { AnyAllowlistEntry } from "../allowlist-types";${zodImport}${validatorsImport}

export const DOMAIN_ALLOWLIST = [
${entryLiterals.join("\n")}
] as const satisfies readonly AnyAllowlistEntry[];
`;

  writeGenerated(
    path.join(GENERATED_DIR, "allowlist.ts"),
    "generate_allowlist.ts",
    body,
  );
}

main();
