/**
 * Generates `web/lib/domain-client/generated/validators.ts` — the Ajv
 * validators `DomainProxy` runs against every FastAPI response body, plus
 * the request-body validators the BFF handler runs before forwarding
 * (spec §6.2.5). Ajv is pinned at 8.17.1, ajv-formats at 3.0.1.
 *
 * Deviation from spec §6.2.5: schemas are compiled once at module load with
 * a shared Ajv2020 instance rather than emitted as `ajv/dist/standalone`
 * precompiled function bodies. Runtime cost is identical either way —
 * compilation happens once at server startup, never per request — this
 * only changes how the compiled function is produced, trading the fragility
 * of standalone ESM/CJS codegen for a straightforward `ajv.compile()` call
 * over a schema literal embedded by the generator.
 */
import * as path from 'node:path';

import {
  GENERATED_DIR,
  type JsonSchema,
  listOperations,
  loadSpec,
  schemaRefName,
  writeGenerated,
} from './lib/spec';

type ValidatorSpec = { id: string; schema: JsonSchema };

function main(): void {
  const spec = loadSpec();
  const operations = listOperations(spec);
  const schemas = spec.components?.schemas ?? {};

  const validatorSpecs: ValidatorSpec[] = [];
  const seen = new Set<string>();

  function addValidator(id: string, schemaName: string): void {
    if (seen.has(id)) return;
    const schema = schemas[schemaName];
    if (!schema) {
      throw new Error(
        `Schema "${schemaName}" referenced by validator "${id}" not found in components.schemas`
      );
    }
    seen.add(id);
    validatorSpecs.push({ id, schema });
  }

  function addInlineValidator(id: string, schema: JsonSchema): void {
    if (seen.has(id)) return;
    // OpenAPI's discriminator object is documentation/codegen metadata, not
    // a JSON Schema 2020-12 keyword. The oneOf branches retain their const
    // discriminator fields, so AJV can validate the same union strictly
    // without compiling this unsupported annotation.
    const jsonSchema = { ...schema };
    delete jsonSchema.discriminator;
    seen.add(id);
    validatorSpecs.push({ id, schema: jsonSchema });
  }

  // Always present: the shared Problem Details validator every undeclared
  // and declared error status validates against (spec §6.8 step 4).
  addValidator('problem_details', 'ProblemDetails');

  const requestValidatorEntries: string[] = [];

  for (const { operationId, operation } of operations) {
    for (const [status, responseDef] of Object.entries<any>(
      operation.responses ?? {}
    )) {
      const n = Number(status);
      if (!Number.isInteger(n) || n < 200 || n >= 400) continue;
      const content = responseDef.content?.['application/json'];
      const responseSchema = content?.schema as JsonSchema | undefined;
      const refName = schemaRefName(responseSchema);
      if (refName) {
        addValidator(`${operationId}_${status}`, refName);
      } else if (responseSchema) {
        addInlineValidator(`${operationId}_${status}`, responseSchema);
      }
    }

    const requestContent = operation.requestBody?.content?.['application/json'];
    const requestRefName = schemaRefName(requestContent?.schema);
    if (requestRefName) {
      const id = `${operationId}_request`;
      addValidator(id, requestRefName);
      requestValidatorEntries.push(
        `  ${operationId}: VALIDATOR_FUNCTIONS[${JSON.stringify(id)}] as ValidateFunction<DomainOperationMap[${JSON.stringify(operationId)}]["requestBody"]>,`
      );
    }
  }

  // Every component schema (not just the ones selected above as a
  // top-level validatorId) is registered with Ajv under its
  // `#/components/schemas/<name>` pointer. A validator schema can itself
  // contain a nested `$ref` to a sibling component (e.g.
  // `OperationDetailResponse.result` -> `OperationResultRefResponse`) that
  // was never chosen as an operation's own response schema; compiling it
  // without this registration step leaves that pointer unresolved and
  // `ajv.compile` throws "can't resolve reference" at server startup.
  const componentSchemaEntries = Object.keys(schemas)
    .sort()
    .map((name) => `  ${JSON.stringify(name)}: ${JSON.stringify(schemas[name])},`)
    .join('\n');

  const schemaEntries = validatorSpecs
    .map((v) => `  ${JSON.stringify(v.id)}: ${JSON.stringify(v.schema)},`)
    .join('\n');

  const responseEntries = validatorSpecs
    .map(
      (v) =>
        `  [${JSON.stringify(v.id)}, VALIDATOR_FUNCTIONS[${JSON.stringify(v.id)}]],`
    )
    .join('\n');

  const requestValidatorsBody =
    requestValidatorEntries.length === 0
      ? '{}'
      : `{\n${requestValidatorEntries.join('\n')}\n}`;

  const body = `import Ajv2020 from "ajv/dist/2020";
import addFormats from "ajv-formats";
import type { ValidateFunction } from "ajv/dist/core";
import type { DomainOperationMap } from "./operation-map";

const ajv = new Ajv2020({ strict: true, allErrors: true });
// ajv-formats resolves its own nested \`ajv\` copy for its \`Ajv\` parameter
// type (a duplicate-dependency artifact of the npm tree, not a version
// mismatch our own pin controls — see package.json's "ajv": "8.17.1"). Both
// copies satisfy ajv-formats' own \`^8.0.0\` dependency range and are
// structurally identical at runtime; the cast only works around TypeScript
// treating the two \`Ajv\` classes as nominally distinct.
addFormats(ajv as unknown as Parameters<typeof addFormats>[0]);

// Registered by pointer, not compiled: makes every component schema an
// addressable \`$ref\` target for the validators below, whether or not it
// was itself chosen as a top-level validatorId.
const COMPONENT_SCHEMAS: Record<string, object> = {
${componentSchemaEntries}
};
for (const [componentName, componentSchema] of Object.entries(COMPONENT_SCHEMAS)) {
  ajv.addSchema(componentSchema, \`#/components/schemas/\${componentName}\`);
}

const SCHEMAS: Record<string, object> = {
${schemaEntries}
};

const VALIDATOR_FUNCTIONS: Record<string, ValidateFunction> = Object.fromEntries(
  Object.entries(SCHEMAS).map(([id, schema]) => [id, ajv.compile(schema)])
);

/** Every declared 2xx/3xx response schema plus the shared Problem Details schema, keyed by validatorId (spec §6.2.5). */
export const RESPONSE_VALIDATORS: ReadonlyMap<string, ValidateFunction> = new Map([
${responseEntries}
]);

type OperationId = keyof DomainOperationMap;
type BodyOperationId = {
  [K in OperationId]: [DomainOperationMap[K]["requestBody"]] extends [never] ? never : K;
}[OperationId];

type RequestValidatorMap = {
  [K in BodyOperationId]: ValidateFunction<DomainOperationMap[K]["requestBody"]>;
};

/** One request-body validator per body-bearing operation (spec §6.2.5). Empty at the tracer milestone — no operation declares a request body yet. */
export const REQUEST_VALIDATORS = ${requestValidatorsBody} satisfies RequestValidatorMap;
`;

  writeGenerated(
    path.join(GENERATED_DIR, 'validators.ts'),
    'generate_response_validators.ts',
    body
  );
}

main();
