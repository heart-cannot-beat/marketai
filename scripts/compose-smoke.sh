#!/usr/bin/env bash
# scripts/compose-smoke.sh
# Run integration smoke tests against a running compose stack.
# Exits non-zero on any failure; captures evidence to stdout.
set -euo pipefail

echo "=== Compose Integration Smoke ==="
echo "Started: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo ""

FAILURES=0
pass() { echo "  PASS: $1"; }
fail() { echo "  FAIL: $1"; FAILURES=$((FAILURES + 1)); }

# --- 1. API health ---
echo "--- API health ---"
api_health=$(curl -sf http://localhost:8787/api/health/ready 2>&1) && pass "API /health/ready" || fail "API /health/ready: $api_health"
echo "  Response: $api_health"

# --- 2. API liveness ---
echo "--- API liveness ---"
api_live=$(curl -sf http://localhost:8787/api/health/live 2>&1) && pass "API /health/live" || fail "API /health/live"

# --- 3. Web auth providers ---
echo "--- Web auth providers ---"
web_auth=$(curl -sf http://localhost:4002/api/auth/providers 2>&1) && pass "Web /api/auth/providers" || fail "Web /api/auth/providers"

# --- 4. JWKS endpoint ---
echo "--- JWKS endpoint ---"
jwks=$(curl -sf http://localhost:4002/.well-known/jwks.json 2>&1) && pass "Web JWKS endpoint" || fail "Web JWKS endpoint"

# --- 5. PostgreSQL role topology ---
echo "--- PostgreSQL role topology ---"
roles=$(docker compose exec -T postgres psql -U postgres -At -c \
  "SELECT rolname FROM pg_roles WHERE rolname LIKE 'marketai_%' ORDER BY 1")
expected_roles="marketai_control_app
marketai_control_owner
marketai_domain_api
marketai_domain_app
marketai_domain_owner
marketai_domain_readonly
marketai_domain_worker
marketai_erasure_principal
marketai_projection_writer"
if [[ "$roles" == "$expected_roles" ]]; then
  pass "All 9 roles present"
else
  fail "Role mismatch: got $(echo "$roles" | tr '\n' ',')"
fi

# --- 6. RLS enforcement ---
echo "--- RLS enforcement ---"
unprotected_tables=$(docker compose exec -T postgres psql -U postgres -d marketai_domain -At -c \
  "SELECT tablename FROM pg_tables WHERE schemaname='public' AND NOT rowsecurity ORDER BY tablename" 2>/dev/null || echo "error")
expected_unprotected_tables="alembic_version
market_policies
market_policy_version_events
market_policy_versions
prompt_baselines
scale_policy_versions
tenant_context_secrets"
if [[ "$unprotected_tables" == "$expected_unprotected_tables" ]]; then
  pass "Only the 7 reviewed platform tables omit tenant RLS"
else
  fail "Unexpected tables without RLS: $(echo "$unprotected_tables" | tr '\n' ',')"
fi

# --- 7. Database boundary ---
echo "--- Database boundary ---"
cross=$(docker compose exec -T postgres psql -U marketai_domain_api -d marketai_control -c "SELECT 1" 2>&1) \
  && fail "Domain API role can connect to control DB" \
  || pass "Domain API role cannot connect to control DB"

# --- 8. Domain runtime role assertions ---
echo "--- Runtime role assertions ---"
api_role=$(docker compose exec -T postgres psql -U postgres -d marketai_domain -At -c \
  "SELECT rolbypassrls FROM pg_roles WHERE rolname='marketai_domain_api'")
if [[ "$api_role" == "f" ]]; then
  pass "API role has NOBYPASSRLS"
else
  fail "API role has BYPASSRLS=$api_role"
fi

# --- 9. Worker is running ---
echo "--- Worker process ---"
worker_state=$(docker compose ps worker --format json 2>/dev/null \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('State',''))" 2>/dev/null || echo "unknown")
if [[ "$worker_state" == "running" ]]; then
  pass "Worker is running"
else
  fail "Worker state: $worker_state"
fi

# --- 10. In-process domain smoke (inside API container) ---
echo "--- Domain smoke (in-container) ---"
docker compose exec -T api python -c "
from app.adapters.persistence.database import database_ready, assert_runtime_role
database_ready()
assert_runtime_role('marketai_domain_api')
print('Domain database accessible as marketai_domain_api')
" && pass "In-container domain probe" || fail "In-container domain probe"

# --- Summary ---
echo ""
echo "=== Results: $((10 - FAILURES))/10 passed, $FAILURES failed ==="
echo "Finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
exit "$FAILURES"
