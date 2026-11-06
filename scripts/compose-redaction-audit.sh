#!/usr/bin/env bash
# scripts/compose-redaction-audit.sh
# Scan compose service logs and live API responses for secrets, JWTs,
# cookies, and sensitive tokens that must never leak.
# Exits non-zero if any pattern is found.
# Never prints matched content — only occurrence counts.
set -euo pipefail

echo "=== Redaction audit ==="
echo "Started: $(date -u +%Y-%m-%dT%H:%M:%SZ)"

FAILURES=0
TOTAL=0
pass() { TOTAL=$((TOTAL + 1)); echo "  PASS: $1"; }
fail() { TOTAL=$((TOTAL + 1)); echo "  FAIL: $1"; FAILURES=$((FAILURES + 1)); }

LOGFILE=$(mktemp)
trap 'rm -f "$LOGFILE"' EXIT

docker compose logs --no-color > "$LOGFILE" 2>&1

echo "--- Scanning compose logs ---"

if grep -qEi 'eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}' "$LOGFILE"; then
  fail "JWT token found in compose logs ($(grep -cEi 'eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}' "$LOGFILE") occurrences)"
else
  pass "No JWT tokens in compose logs"
fi

if grep -qF 'compose-nextauth-secret-not-for-production' "$LOGFILE"; then
  fail "NEXTAUTH_SECRET value leaked in compose logs"
else
  pass "NEXTAUTH_SECRET absent from compose logs"
fi

if grep -qF 'compose-tenant-context-secret' "$LOGFILE"; then
  fail "MARKETAI_TENANT_CONTEXT_SECRET leaked in compose logs"
else
  pass "Tenant context secret absent from compose logs"
fi

if grep -qEi 'postgresql://[^:]+:[^@]+@' "$LOGFILE"; then
  fail "Database connection string with password in compose logs ($(grep -cEi 'postgresql://[^:]+:[^@]+@' "$LOGFILE") occurrences)"
else
  pass "No database passwords in compose logs"
fi

if grep -qEi 'set-cookie:.*next-auth\.session-token=' "$LOGFILE"; then
  fail "Session cookie value leaked in compose logs"
else
  pass "No session cookies in compose logs"
fi

if grep -qF 'compose-entitlement-signing-key' "$LOGFILE"; then
  fail "Entitlement signing key leaked in compose logs"
else
  pass "Entitlement signing key absent from compose logs"
fi

if grep -qF 'compose-kms-secret' "$LOGFILE"; then
  fail "KMS secret leaked in compose logs"
else
  pass "KMS secret absent from compose logs"
fi

if grep -qF 'Y29tcG9zZS1kZWxlZ2F0ZWQta2V5' "$LOGFILE"; then
  fail "Delegated signing key encryption key leaked in compose logs"
else
  pass "Delegated signing key encryption key absent from compose logs"
fi

echo "--- Scanning API responses ---"

HEALTH_RESP=$(curl -sf http://localhost:8787/api/health/ready) || {
  fail "Health endpoint unreachable (curl exit $?)"
  HEALTH_RESP=""
}
if [ -n "$HEALTH_RESP" ]; then
  if echo "$HEALTH_RESP" | grep -qEi 'password|secret|token.*=|eyJ[A-Za-z0-9_-]{10,}'; then
    fail "Health endpoint leaks sensitive data"
  else
    pass "Health endpoint clean"
  fi
fi

AUTH_RESP=$(curl -sf http://localhost:4002/api/auth/providers) || {
  fail "Auth providers endpoint unreachable (curl exit $?)"
  AUTH_RESP=""
}
if [ -n "$AUTH_RESP" ]; then
  if echo "$AUTH_RESP" | grep -qEi 'NEXTAUTH_SECRET|compose-nextauth|password'; then
    fail "Auth providers endpoint leaks sensitive data"
  else
    pass "Auth providers endpoint clean"
  fi
fi

ERR_RESP=$(curl -s http://localhost:8787/api/nonexistent) || {
  fail "Error endpoint unreachable (curl exit $?)"
  ERR_RESP=""
}
if [ -n "$ERR_RESP" ]; then
  if echo "$ERR_RESP" | grep -qEi 'postgresql://|password|NEXTAUTH_SECRET|compose-'; then
    fail "Error response leaks sensitive data"
  else
    pass "Error responses clean"
  fi
fi

echo ""
echo "=== Redaction audit: $((TOTAL - FAILURES))/$TOTAL passed, $FAILURES failed ==="
echo "Finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
exit "$FAILURES"
