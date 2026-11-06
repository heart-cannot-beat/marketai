#!/usr/bin/env bash
# Live deployment validator for a running Railway environment.
#
# Requires a reachable Web URL and optionally an API private URL.
# Does not modify any state; strictly read-only checks.
#
# Usage:
#   RAILWAY_WEB_URL=https://marketai.up.railway.app \
#   RAILWAY_API_PRIVATE_URL=http://marketai-api.railway.internal:8000 \
#     ./deploy/railway/validate-live.sh
#
# When run from outside the Railway private network (e.g. CI or a
# developer machine), API private URL checks are skipped gracefully.
set -euo pipefail

: "${RAILWAY_WEB_URL:?RAILWAY_WEB_URL is required (e.g. https://marketai.up.railway.app)}"
RAILWAY_API_PRIVATE_URL="${RAILWAY_API_PRIVATE_URL:-}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
DIM='\033[2m'
RESET='\033[0m'

failures=0
pass() { printf "${GREEN}  ✓ %s${RESET}\n" "$1"; }
fail() { printf "${RED}  ✗ %s${RESET}\n" "$1"; failures=$((failures + 1)); }
skip() { printf "${YELLOW}  ⊘ %s${RESET}\n" "$1"; }
http_status() {
  local status
  if status=$(curl -s -o /dev/null -w '%{http_code}' --max-time "$1" "$2" 2>/dev/null); then
    printf '%s' "$status"
  else
    printf '000'
  fi
}

echo "Railway live deployment validation"
echo "===================================="

# ── 1. Web readiness ─────────────────────────────────────────────────
echo ""
echo "Web service"

web_status=$(http_status 10 "${RAILWAY_WEB_URL%/}/api/readiness")
if [[ "$web_status" == "200" ]]; then
  pass "Web readiness returns HTTP 200"
else
  fail "Web readiness returned HTTP $web_status (expected 200)"
fi

# Check a public page rather than `/`: the root intentionally redirects to
# login and a redirect response need not carry the final page's headers.
web_page_headers=$(curl -fsS -D - -o /dev/null --max-time 10 \
  "${RAILWAY_WEB_URL%/}/auth/login" 2>/dev/null || true)
if printf '%s\n' "$web_page_headers" | grep -Eq '^HTTP/[0-9.]+[[:space:]]+200[[:space:]]*$'; then
  pass "Public login page returns HTTP 200"
else
  fail "Public login page did not return HTTP 200"
fi

csp_header=$(printf '%s' "$web_page_headers" \
  | grep -i 'content-security-policy' || true)
if [[ -n "$csp_header" ]]; then
  pass "Web sends Content-Security-Policy header"
else
  fail "Web missing Content-Security-Policy header"
fi

xfo_header=$(printf '%s' "$web_page_headers" \
  | grep -i 'x-frame-options' || true)
if [[ -n "$xfo_header" ]]; then
  pass "Web sends X-Frame-Options header"
else
  fail "Web missing X-Frame-Options header"
fi

# Verify powered-by is disabled
powered_by=$(printf '%s' "$web_page_headers" \
  | grep -i 'x-powered-by' || true)
if [[ -z "$powered_by" ]]; then
  pass "X-Powered-By header suppressed"
else
  fail "X-Powered-By header present (poweredByHeader should be false)"
fi

# ── 2. API private access ───────────────────────────────────────────
echo ""
echo "API service"

if [[ -n "$RAILWAY_API_PRIVATE_URL" ]]; then
  api_status=$(http_status 10 "${RAILWAY_API_PRIVATE_URL%/}/api/health/ready")
  if [[ "$api_status" == "200" ]]; then
    pass "API readiness returns HTTP 200 (private URL)"
  else
    fail "API readiness returned HTTP $api_status (expected 200)"
  fi

  # API liveness (lighter check)
  api_live=$(http_status 5 "${RAILWAY_API_PRIVATE_URL%/}/api/health/live")
  if [[ "$api_live" == "200" ]]; then
    pass "API liveness returns HTTP 200"
  else
    fail "API liveness returned HTTP $api_live"
  fi
else
  skip "API private URL not set; skipping API readiness checks"
fi

# ── 3. Browser cannot reach API directly ─────────────────────────────
echo ""
echo "Trust boundary: API not browser-addressable"

# An external HTTP probe cannot prove that a separate Railway API service has
# no public domain. Never report this unverified trust boundary as a pass.
skip "Verify in Railway that API and Worker have no public domains"

# ── 4. Disabled capabilities ─────────────────────────────────────────
echo ""
echo "Disabled capabilities"

if [[ -n "$RAILWAY_API_PRIVATE_URL" ]]; then
  # Check Meta write disabled via the capabilities endpoint if it exists
  meta_caps=$(curl -sf --max-time 5 \
    "$RAILWAY_API_PRIVATE_URL/api/health/ready" 2>/dev/null || echo "{}")

  # The readiness endpoint reports role and capability info
  if echo "$meta_caps" | grep -qi '"meta_write":\s*false\|meta_write.*disabled'; then
    pass "Meta write reported disabled in readiness"
  else
    skip "Meta write status not exposed in readiness (verify via env contract)"
  fi
else
  skip "Cannot verify disabled capabilities without API private URL"
fi

# ── 5. JWKS endpoint ─────────────────────────────────────────────────
echo ""
echo "Delegated identity"

jwks_status=$(http_status 5 "${RAILWAY_WEB_URL%/}/.well-known/jwks.json")
if [[ "$jwks_status" == "200" ]]; then
  pass "JWKS endpoint reachable at /.well-known/jwks.json"
else
  fail "JWKS endpoint returned HTTP $jwks_status (expected 200)"
fi

# ── summary ───────────────────────────────────────────────────────────
echo ""
if [[ "$failures" -eq 0 ]]; then
  printf "${GREEN}All live checks passed.${RESET}\n"
else
  printf "${RED}%d live check(s) failed.${RESET}\n" "$failures"
  exit 1
fi
