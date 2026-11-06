/**
 * Shared helpers for the generation pipeline (spec §6.2, §6.11). Every
 * `scripts/generate_*.ts` generator reads the same two inputs — the OpenAPI
 * spec and the policy manifest — through this module so path-param
 * extraction, `$ref` resolution and the policy fail-closed check behave
 * identically across all seven generated artifacts.
 */
import * as fs from "node:fs";
import * as path from "node:path";

export const ROOT_DIR = path.resolve(__dirname, "..", "..");
export const SPEC_PATH = path.join(
  ROOT_DIR,
  "contracts/openapi/marketai-domain-v1.json",
);
export const POLICY_PATH = path.join(
  ROOT_DIR,
  "contracts/openapi/marketai-domain-v1-policy.json",
);
export const GENERATED_DIR = path.join(
  ROOT_DIR,
  "web/lib/domain-client/generated",
);

export const HTTP_METHODS = [
  "get",
  "put",
  "post",
  "delete",
  "options",
  "head",
  "patch",
  "trace",
] as const;

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type JsonSchema = Record<string, any>;

export type OpenApiSpec = {
  openapi: string;
  info: Record<string, unknown>;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  paths: Record<string, Record<string, any>>;
  components?: { schemas?: Record<string, JsonSchema> };
};

export type OpenApiOperation = {
  operationId: string;
  /** Uppercase HTTP method, e.g. `"GET"`. */
  method: string;
  /** FastAPI-side path template with `{param}` placeholders. */
  pathTemplate: string;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  operation: any;
};

export type LocationShapePolicy = {
  pattern: string;
  targetOperationId: string;
  extractParams: string[];
};

export type PolicyEntry = {
  bffPattern: string;
  maxRequestBytes: number;
  maxResponseBytes: number;
  forwardConditionalHeaders: boolean;
  /** Optional — only operations whose FastAPI response carries a Location header need this (spec §4.7, §6.2.3). */
  locationShapes?: Record<string, LocationShapePolicy[]>;
};

export type PolicyManifest = Record<string, PolicyEntry>;

export function loadSpec(): OpenApiSpec {
  return JSON.parse(fs.readFileSync(SPEC_PATH, "utf-8"));
}

export function loadPolicy(): PolicyManifest {
  return JSON.parse(fs.readFileSync(POLICY_PATH, "utf-8"));
}

/** Every operation declared in the spec, sorted by operationId for deterministic (idempotent) generator output. */
export function listOperations(spec: OpenApiSpec): OpenApiOperation[] {
  const results: OpenApiOperation[] = [];
  for (const pathTemplate of Object.keys(spec.paths).sort()) {
    const pathItem = spec.paths[pathTemplate];
    for (const method of HTTP_METHODS) {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const operation = (pathItem as any)[method];
      if (!operation) continue;
      const operationId = operation.operationId;
      if (!operationId || typeof operationId !== "string") {
        throw new Error(
          `Operation ${method.toUpperCase()} ${pathTemplate} is missing operationId`,
        );
      }
      results.push({
        operationId,
        method: method.toUpperCase(),
        pathTemplate,
        operation,
      });
    }
  }
  results.sort((a, b) => a.operationId.localeCompare(b.operationId));
  return results;
}

/**
 * Looks up the policy entry for an operationId. A missing entry is a
 * generation-time error — the policy manifest is fail-closed (spec §6.2.3):
 * every operation in the OpenAPI spec must have a corresponding policy
 * declaration.
 */
export function policyFor(
  policy: PolicyManifest,
  operationId: string,
): PolicyEntry {
  const entry = policy[operationId];
  if (!entry) {
    throw new Error(
      `Policy manifest is missing an entry for operationId "${operationId}". ` +
        "Every operation in contracts/openapi/marketai-domain-v1.json must have a " +
        "corresponding entry in contracts/openapi/marketai-domain-v1-policy.json (fail-closed).",
    );
  }
  return entry;
}

/** Path param names from a FastAPI-style template, e.g. `/x/{a}/{b}` -> `["a", "b"]`. */
export function extractPathParamNames(pathTemplate: string): string[] {
  const matches = pathTemplate.match(/\{([^}]+)\}/g) ?? [];
  return matches.map((m) => m.slice(1, -1));
}

/** Path param names from a BFF-style pattern, e.g. `/w/:slug/x/:id` -> `["id"]` (`:slug` is not an operation path param — it is always the workspace slug). */
export function extractBffPathParamNames(bffPattern: string): string[] {
  const matches = bffPattern.match(/:([A-Za-z0-9_]+)/g) ?? [];
  return matches.map((m) => m.slice(1)).filter((name) => name !== "slug");
}

/** Resolves a `{ "$ref": "#/components/schemas/X" }` object to `"X"`, or `null` if `schema` is not a `$ref`. */
export function schemaRefName(
  schema: JsonSchema | undefined | null,
): string | null {
  if (!schema) return null;
  if (typeof schema.$ref === "string") {
    const parts = schema.$ref.split("/");
    return parts[parts.length - 1];
  }
  return null;
}

/** `Content-Type` values an operation's request body accepts, sorted, or `[]` if it declares no request body. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function requestMediaTypes(operation: any): string[] {
  const content = operation.requestBody?.content;
  return content ? Object.keys(content).sort() : [];
}

/** `x-marketai-idempotent: true` extension (spec §6.2.3) — defaults to `false` when absent. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function isIdempotent(operation: any): boolean {
  return operation["x-marketai-idempotent"] === true;
}

export interface QueryParamDef {
  name: string;
  type: "string" | "integer" | "boolean";
  required: boolean;
  enum?: string[];
  default?: unknown;
  minimum?: number;
  maximum?: number;
  isArray?: boolean; // repeated values via getAll()
}

/** Query parameter definitions (`in: "query"`) for an operation, in spec order. */
export function extractQueryParams(
  operation: Record<string, unknown>,
): QueryParamDef[] {
  const params = (operation.parameters ?? []) as Array<Record<string, unknown>>;
  return params
    .filter((p) => p.in === "query")
    .map((p) => {
      const schema = (p.schema ?? {}) as Record<string, unknown>;
      return {
        name: p.name as string,
        type: (schema.type as string) ?? "string",
        required: (p.required as boolean) ?? false,
        enum: schema.enum as string[] | undefined,
        default: schema.default,
        minimum: schema.minimum as number | undefined,
        maximum: schema.maximum as number | undefined,
        isArray: schema.type === "array",
      } as QueryParamDef;
    });
}

function generatedFileHeader(generator: string): string {
  return `/**
 * GENERATED FILE — do not edit by hand.
 * Generator: scripts/${generator}
 * Sources: contracts/openapi/marketai-domain-v1.json, contracts/openapi/marketai-domain-v1-policy.json
 * Regenerate with scripts/generate_all.sh — CI fails the build on drift.
 */
`;
}

/** Writes a generated artifact with the standard header, creating `web/lib/domain-client/generated/` if needed. */
export function writeGenerated(
  outPath: string,
  generator: string,
  body: string,
): void {
  fs.mkdirSync(path.dirname(outPath), { recursive: true });
  fs.writeFileSync(outPath, generatedFileHeader(generator) + body);
  console.log(`Wrote ${path.relative(ROOT_DIR, outPath)}`);
}
