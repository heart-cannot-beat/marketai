/**
 * Generates `web/lib/domain-client/generated/operation-map.d.ts` — the
 * `DomainOperationMap` type-only artifact (spec §6.2.2). Zero runtime cost:
 * erased at build.
 */
import * as path from 'node:path';

import {
  GENERATED_DIR,
  extractPathParamNames,
  listOperations,
  loadSpec,
  schemaRefName,
  writeGenerated,
} from './lib/spec';

function tsPathParamsType(names: string[]): string {
  if (names.length === 0) return 'Record<string, never>';
  return `{ ${names.map((n) => `${JSON.stringify(n)}: string`).join('; ')} }`;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function tsQueryParamsType(operation: any): string {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const params = (operation.parameters ?? []).filter(
    (p: any) => p.in === 'query'
  );
  if (params.length === 0) return 'Record<string, never>';
  return `{ ${params
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    .map(
      (p: any) =>
        `${JSON.stringify(p.name)}${p.required ? '' : '?'}: string`
    )
    .join('; ')} }`;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function tsRequestBodyType(operation: any): string {
  const content = operation.requestBody?.content?.['application/json'];
  const refName = schemaRefName(content?.schema);
  return refName ? `components["schemas"][${JSON.stringify(refName)}]` : 'never';
}

function isSuccessStatus(status: string): boolean {
  const n = Number(status);
  return Number.isInteger(n) && n >= 200 && n < 400;
}

function tsResponseSchemaType(schema: unknown): string {
  if (!schema || typeof schema !== 'object') return 'null';

  const refName = schemaRefName(schema);
  if (refName) {
    return `components["schemas"][${JSON.stringify(refName)}]`;
  }

  const union = (schema as { oneOf?: unknown[]; anyOf?: unknown[] }).oneOf ??
    (schema as { oneOf?: unknown[]; anyOf?: unknown[] }).anyOf;
  if (Array.isArray(union) && union.length > 0) {
    return union.map(tsResponseSchemaType).join(' | ');
  }

  return 'null';
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function tsSuccessResponsesType(operation: any, operationId: string): string {
  const entries: string[] = [];
  for (const [status, responseDef] of Object.entries<any>(
    operation.responses ?? {}
  )) {
    if (!isSuccessStatus(status)) continue;
    const content = responseDef.content?.['application/json'];
    const bodyType = tsResponseSchemaType(content?.schema);
    entries.push(`${status}: ${bodyType}`);
  }
  if (entries.length === 0) {
    throw new Error(
      `Operation "${operationId}" declares no 2xx/3xx success response`
    );
  }
  return `{ ${entries.join('; ')} }`;
}

function main(): void {
  const spec = loadSpec();
  const operations = listOperations(spec);

  const entries = operations.map(({ operationId, operation, pathTemplate }) => {
    const pathParams = tsPathParamsType(extractPathParamNames(pathTemplate));
    const queryParams = tsQueryParamsType(operation);
    const requestBody = tsRequestBodyType(operation);
    const successResponses = tsSuccessResponsesType(operation, operationId);
    return (
      `  ${operationId}: {\n` +
      `    pathParams: ${pathParams};\n` +
      `    queryParams: ${queryParams};\n` +
      `    requestBody: ${requestBody};\n` +
      `    successResponses: ${successResponses};\n` +
      `  };`
    );
  });

  const body =
    `import type { components } from "./domain-v1";\n\n` +
    `export type DomainOperationMap = {\n${entries.join('\n')}\n};\n`;

  writeGenerated(
    path.join(GENERATED_DIR, 'operation-map.d.ts'),
    'generate_operation_map.ts',
    body
  );
}

main();
