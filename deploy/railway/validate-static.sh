#!/usr/bin/env bash
# Static deployment validator for Railway production topology.
#
# Runs without Railway access. Verifies:
#   1. All three Dockerfiles build successfully.
#   2. Environment variable contracts are internally consistent.
#   3. No secrets or generated files are tracked by git.
#   4. META_WRITE_ACCESS_TOKEN is empty in all contract files.
#   5. Trust boundary assertions hold in the contract definitions.
#
# Usage:  ./deploy/railway/validate-static.sh [--skip-docker-build]
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
cd "$repo"

RED='\033[0;31m'
GREEN='\033[0;32m'
DIM='\033[2m'
RESET='\033[0m'

failures=0
pass() { printf "${GREEN}  ✓ %s${RESET}\n" "$1"; }
fail() { printf "${RED}  ✗ %s${RESET}\n" "$1"; failures=$((failures + 1)); }

echo "Railway static deployment validation"
echo "====================================="

# ── 1. Dockerfile builds ─────────────────────────────────────────────
skip_docker=false
for arg in "$@"; do
  [[ "$arg" == "--skip-docker-build" ]] && skip_docker=true
done

if $skip_docker; then
  printf "${DIM}  ⊘ Docker builds skipped (--skip-docker-build)${RESET}\n"
else
  echo ""
  echo "Dockerfile builds"
  for svc in web api worker; do
    dockerfile="deploy/railway/Dockerfile.$svc"
    if [[ ! -f "$dockerfile" ]]; then
      fail "$dockerfile does not exist"
      continue
    fi
    if docker build -f "$dockerfile" -t "marketai-$svc:validate" . >/dev/null 2>&1; then
      pass "$dockerfile builds"
    else
      fail "$dockerfile failed to build"
    fi
  done
fi

# ── 2. Contract file syntax ──────────────────────────────────────────
echo ""
echo "Environment variable contracts"

for contract_file in deploy/railway/env/*.env.contract; do
  svc="$(basename "$contract_file" .env.contract)"

  # Every non-blank, non-comment line must start with REQUIRED|OPTIONAL|DENIED
  bad_lines=$(grep -vE '^\s*$|^\s*#' "$contract_file" \
    | grep -vcE '^\s*(REQUIRED|OPTIONAL|DENIED)\s+[A-Z0-9_]+' || true)
  if [[ "$bad_lines" -eq 0 ]]; then
    pass "$svc contract syntax valid"
  else
    fail "$svc contract has $bad_lines malformed lines"
  fi

  # No variable appears in both REQUIRED/OPTIONAL and DENIED
  allowed=$(grep -oE '(REQUIRED|OPTIONAL)\s+[A-Z0-9_]+' "$contract_file" \
    | awk '{print $2}' | sort -u)
  denied=$(grep -oE 'DENIED\s+[A-Z0-9_]+' "$contract_file" \
    | awk '{print $2}' | sort -u)
  overlap=$(comm -12 <(echo "$allowed") <(echo "$denied") || true)
  if [[ -z "$overlap" ]]; then
    pass "$svc contract has no allow/deny conflicts"
  else
    fail "$svc contract has conflicting entries: $overlap"
  fi
done

# Keep the operator-facing contracts aligned with the names the current
# runtimes actually read.  An internally valid but stale contract silently
# provisions a service with missing configuration.
echo ""
echo "Runtime variable names"
contract_requires() {
  grep -qE "^[[:space:]]*REQUIRED[[:space:]]+$2([[:space:]]|$)" "deploy/railway/env/$1.env.contract"
}
contract_mentions() {
  grep -qE "^[[:space:]]*(REQUIRED|OPTIONAL|DENIED)[[:space:]]+$2([[:space:]]|$)" "deploy/railway/env/$1.env.contract"
}
for entry in \
  'web DOMAIN_API_BASE_URL web/lib/env.ts' \
  'web DELEGATED_SIGNING_KEY_ENCRYPTION_KEY web/lib/env.ts' \
  'web PROJECTION_SYNC_SERVICE_TOKEN web/lib/env.ts' \
  'web DOMAIN_TO_CONTROL_PLANE_SYNC_TOKEN web/lib/env.ts' \
  'web MFA_ENCRYPTION_KEY web/lib/mfa.ts' \
  'web NEXT_PUBLIC_MARKETAI_STORAGE_ORIGINS web/middleware.ts' \
  'api MARKETAI_CONTROL_PLANE_INTERNAL_BASE_URL app/config.py' \
  'api MARKETAI_PROJECTION_SYNC_SERVICE_TOKEN app/config.py' \
  'api DOMAIN_TO_CONTROL_PLANE_SYNC_TOKEN app/config.py' \
  'api ENTITLEMENT_VERIFICATION_KEY_RING app/config.py' \
  'worker MARKETAI_CONTROL_PLANE_INTERNAL_BASE_URL app/config.py' \
  'worker DOMAIN_TO_CONTROL_PLANE_SYNC_TOKEN app/config.py'; do
  read -r svc name source <<< "$entry"
  if contract_requires "$svc" "$name" && grep -Fq "$name" "$source"; then
    pass "$svc requires runtime variable $name"
  else
    fail "$svc contract/runtime mismatch for $name"
  fi
done
for name in DATABASE_URL DOMAIN_API_BASE_URL CONTROL_PLANE_TO_DOMAIN_ENTITLEMENT_TOKEN ENTITLEMENT_SNAPSHOT_SIGNING_KEY ENTITLEMENT_SNAPSHOT_SIGNING_KEY_ID; do
  if contract_requires dispatcher "$name" && grep -Fq "$name" web/scripts/dispatch-entitlements.ts web/lib/env.ts web/lib/prisma.ts; then
    pass "dispatcher requires runtime variable $name"
  else
    fail "dispatcher contract/runtime mismatch for $name"
  fi
done
# `NEXT_PUBLIC_*` is inlined by `next build`; the Railway image must declare
# the build argument or the browser ships with an empty storage allowlist.
if grep -qE '^ARG NEXT_PUBLIC_MARKETAI_STORAGE_ORIGINS' deploy/railway/Dockerfile.web \
   && grep -qE '^ENV NEXT_PUBLIC_MARKETAI_STORAGE_ORIGINS=' deploy/railway/Dockerfile.web; then
  pass "web image inlines NEXT_PUBLIC_MARKETAI_STORAGE_ORIGINS at build time"
else
  fail "deploy/railway/Dockerfile.web must declare ARG/ENV NEXT_PUBLIC_MARKETAI_STORAGE_ORIGINS before next build"
fi
for entry in \
  'web DOMAIN_API_INTERNAL_URL' \
  'web DELEGATED_SIGNING_PRIVATE_KEY' \
  'web DELEGATED_SIGNING_KEY_ID' \
  'api CONTROL_PLANE_INTERNAL_BASE_URL' \
  'worker CONTROL_PLANE_INTERNAL_BASE_URL'; do
  read -r svc name <<< "$entry"
  if contract_mentions "$svc" "$name"; then
    fail "$svc contract still mentions obsolete variable $name"
  else
    pass "$svc contract excludes obsolete variable $name"
  fi
done

# ── 3. Cross-contract trust boundaries ───────────────────────────────
echo ""
echo "Trust boundary assertions"

# Web must not receive domain database credentials
if grep -qE 'DENIED\s+MARKETAI_DATABASE_URL' deploy/railway/env/web.env.contract \
   && grep -qE 'DENIED\s+MARKETAI_DATABASE_URL' deploy/railway/env/dispatcher.env.contract; then
  pass "Web and dispatcher denied domain database access"
else
  fail "Web and dispatcher must deny MARKETAI_DATABASE_URL"
fi

# API and Worker must not receive control-plane database
for svc in api worker; do
  if grep -qE 'DENIED\s+DATABASE_URL' "deploy/railway/env/$svc.env.contract"; then
    pass "$svc denied control-plane database access"
  else
    fail "$svc must deny DATABASE_URL"
  fi
done

# API must not receive delegated signing key (it verifies, never signs)
if grep -qE 'DENIED\s+DELEGATED_SIGNING_PRIVATE_KEY' deploy/railway/env/api.env.contract \
   && grep -qE 'DENIED\s+DELEGATED_SIGNING_KEY_ENCRYPTION_KEY' deploy/railway/env/api.env.contract; then
  pass "API denied delegated signing material"
else
  fail "API must deny delegated signing material"
fi

# No service receives META_WRITE_ACCESS_TOKEN
for contract_file in deploy/railway/env/*.env.contract; do
  svc="$(basename "$contract_file" .env.contract)"
  if grep -qE 'DENIED\s+META_WRITE_ACCESS_TOKEN' "$contract_file"; then
    pass "$svc denies META_WRITE_ACCESS_TOKEN"
  else
    fail "$svc must deny META_WRITE_ACCESS_TOKEN"
  fi
done

# No service receives schema-owner credentials at runtime
for contract_file in deploy/railway/env/*.env.contract; do
  svc="$(basename "$contract_file" .env.contract)"
  if grep -qE 'DENIED\s+DOMAIN_OWNER_PASSWORD' "$contract_file" \
     && grep -qE 'DENIED\s+CONTROL_OWNER_PASSWORD' "$contract_file"; then
    pass "$svc denies schema-owner credentials"
  else
    fail "$svc must deny schema-owner credentials"
  fi
done

# Web never receives bucket credentials. API and Worker need the environment-
# scoped Railway credential to sign browser capabilities and perform trusted
# storage operations; neither receives a database schema-owner credential.
if grep -qE 'DENIED\s+AWS_ACCESS_KEY_ID' deploy/railway/env/web.env.contract \
   && grep -qE 'DENIED\s+AWS_SECRET_ACCESS_KEY' deploy/railway/env/web.env.contract \
   && grep -qE 'DENIED\s+AWS_ACCESS_KEY_ID' deploy/railway/env/dispatcher.env.contract \
   && grep -qE 'DENIED\s+AWS_SECRET_ACCESS_KEY' deploy/railway/env/dispatcher.env.contract; then
  pass "Web and dispatcher denied Railway Bucket credentials"
else
  fail "Web and dispatcher must deny Railway Bucket credentials"
fi

for svc in api worker; do
  if grep -qE 'REQUIRED\s+AWS_ACCESS_KEY_ID' "deploy/railway/env/$svc.env.contract" \
     && grep -qE 'REQUIRED\s+AWS_SECRET_ACCESS_KEY' "deploy/railway/env/$svc.env.contract"; then
    pass "$svc requires environment-scoped Railway Bucket credentials"
  else
    fail "$svc must require Railway Bucket credentials"
  fi
done

# ── 4. No secrets in git ─────────────────────────────────────────────
echo ""
echo "Secret hygiene"

if git ls-files --cached | grep -qE '\.env\.railway\.|deploy/railway/\.generated/'; then
  fail "Railway secrets or generated files are tracked by git"
else
  pass "No Railway secrets or generated files tracked"
fi

if git ls-files --cached | grep -qE '\.env$' | grep -v '.env.example' | grep -v '.env.compose' >/dev/null 2>&1; then
  fail ".env files tracked by git"
else
  pass "No .env files tracked (excluding examples)"
fi

# ── summary ───────────────────────────────────────────────────────────
echo ""
if [[ "$failures" -eq 0 ]]; then
  printf "${GREEN}All static checks passed.${RESET}\n"
else
  printf "${RED}%d static check(s) failed.${RESET}\n" "$failures"
  exit 1
fi
