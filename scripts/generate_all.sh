#!/usr/bin/env bash
# Orchestrates the seven-artifact generation pipeline (spec §6.2, §6.11):
# reads contracts/openapi/marketai-domain-v1.json + the policy manifest and
# writes every file in web/lib/domain-client/generated/. Run after editing
# either input, or after running scripts/generate_openapi.py. CI runs this
# script and then `git diff --exit-code` on the output directory — a
# forgotten regeneration fails the build instead of shipping drift.
#
# `npx` is unavailable in some environments this repo runs in — every tool
# below is invoked through its `web/node_modules/.bin` binary instead.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
WEB_DIR="$ROOT_DIR/web"
OUT_DIR="$WEB_DIR/lib/domain-client/generated"

mkdir -p "$OUT_DIR"

# ts-node needs `ajv`, `ajv-formats`, `zod` etc. to resolve — those only
# live in web/node_modules, but the generator scripts live outside web/.
# NODE_PATH lets Node's module resolution fall back to web/node_modules for
# any bare-specifier import a generator makes.
export NODE_PATH="$WEB_DIR/node_modules"
export TS_NODE_PROJECT="$SCRIPT_DIR/tsconfig.json"
export TS_NODE_TRANSPILE_ONLY=true
TS_NODE_REGISTER="$WEB_DIR/node_modules/ts-node/register"

run_ts_generator() {
  node -r "$TS_NODE_REGISTER" "$SCRIPT_DIR/$1"
}

echo "==> Generating TypeScript types from OpenAPI spec..."
"$WEB_DIR/node_modules/.bin/openapi-typescript" \
  "$ROOT_DIR/contracts/openapi/marketai-domain-v1.json" \
  -o "$OUT_DIR/domain-v1.d.ts"

echo "==> Generating operation map..."
run_ts_generator generate_operation_map.ts

echo "==> Generating operation registry..."
run_ts_generator generate_operation_registry.ts

echo "==> Generating response validators..."
run_ts_generator generate_response_validators.ts

echo "==> Generating paths..."
run_ts_generator generate_paths.ts

echo "==> Generating allowlist..."
run_ts_generator generate_allowlist.ts

echo "==> Generating allowlist binding..."
run_ts_generator generate_allowlist_binding.ts

echo "==> Formatting generated output (matches \`npm run check-format\`)..."
"$WEB_DIR/node_modules/.bin/prettier" --write "$OUT_DIR" >/dev/null

echo "==> Done. Checking for drift..."
cd "$ROOT_DIR"
git diff --exit-code -- "$OUT_DIR" || {
  echo "ERROR: Generated files have drift. Run scripts/generate_all.sh and commit." >&2
  exit 1
}
