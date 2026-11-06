/**
 * Generates `web/lib/domain-client/generated/paths.ts` — the browser-safe
 * `BFF_ROUTES` table and `buildBffPath()` (spec §6.3). No Zod, no Ajv: this
 * is the one generated artifact the browser bundle imports directly.
 */
import * as path from 'node:path';

import {
  GENERATED_DIR,
  extractBffPathParamNames,
  isIdempotent,
  listOperations,
  loadPolicy,
  loadSpec,
  policyFor,
  writeGenerated,
} from './lib/spec';

function main(): void {
  const spec = loadSpec();
  const policy = loadPolicy();
  const operations = listOperations(spec);

  const routeLiterals = operations.map(({ operationId, method, operation }) => {
    const policyEntry = policyFor(policy, operationId);
    const pathParams = extractBffPathParamNames(policyEntry.bffPattern);
    return (
      `  { operationId: ${JSON.stringify(operationId)}, ` +
      `method: ${JSON.stringify(method)}, ` +
      `bffPattern: ${JSON.stringify(policyEntry.bffPattern)}, ` +
      `pathParams: ${JSON.stringify(pathParams)}, ` +
      `requiresIdempotency: ${isIdempotent(operation)} },`
    );
  });

  const body = `export type BffRoute = {
  readonly operationId: string;
  readonly method: string;
  readonly bffPattern: string;
  readonly pathParams: readonly string[];
  readonly requiresIdempotency: boolean;
};

export const BFF_ROUTES: readonly BffRoute[] = [
${routeLiterals.join('\n')}
] as const;

/** Single source of truth for the browser \`DomainGateway\` and the server route matcher (spec §6.3). */
export function buildBffPath(
  operationId: string,
  params: { slug: string } & Record<string, string>
): string {
  const route = BFF_ROUTES.find((r) => r.operationId === operationId);
  if (!route) {
    throw new Error(\`Unknown operationId: \${operationId}\`);
  }
  let builtPath = route.bffPattern.replace(":slug", encodeURIComponent(params.slug));
  for (const param of route.pathParams) {
    const value = params[param];
    if (value === undefined) {
      throw new Error(\`Missing path param "\${param}" for \${operationId}\`);
    }
    builtPath = builtPath.replace(\`:\${param}\`, encodeURIComponent(value));
  }
  return builtPath;
}
`;

  writeGenerated(path.join(GENERATED_DIR, 'paths.ts'), 'generate_paths.ts', body);
}

main();
