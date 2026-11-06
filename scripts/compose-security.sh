#!/usr/bin/env bash
# scripts/compose-security.sh
# Security evidence suite — runs against a live compose stack.
# Complements compose-smoke.sh with deeper behavioral security checks.
# Exits non-zero on any failure; captures evidence to stdout.
set -euo pipefail

echo "=== Compose Security Suite ==="
echo "Started: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo ""

FAILURES=0
TOTAL=0
pass() { TOTAL=$((TOTAL + 1)); echo "  PASS: $1"; }
fail() { TOTAL=$((TOTAL + 1)); echo "  FAIL: $1"; FAILURES=$((FAILURES + 1)); }

PSQL="docker compose exec -T postgres psql"

# ----------------------------------------------------------------
# Category 1: Privilege Boundaries (DDL/DML restrictions per role)
# ----------------------------------------------------------------
echo "--- Privilege boundaries ---"

# 1. API role cannot CREATE TABLE
err=$($PSQL -U marketai_domain_api -d marketai_domain -c "CREATE TABLE sec_test_ddl(id int)" 2>&1) \
  && fail "API role created a table (DDL should be denied)" \
  || { echo "$err" | grep -qi "permission denied" && pass "API role cannot CREATE TABLE" \
       || fail "API role CREATE TABLE failed but not with permission denied: $err"; }

# 2. API role cannot DROP TABLE. The table must exist: PostgreSQL treats
# `DROP TABLE IF EXISTS missing_table` as a successful no-op before checking
# ownership, so that command does not prove a privilege boundary.
$PSQL -U postgres -d marketai_domain -c \
  "CREATE TABLE IF NOT EXISTS sec_test_drop(id int)" >/dev/null
err=$($PSQL -U marketai_domain_api -d marketai_domain -c "DROP TABLE sec_test_drop" 2>&1) \
  && fail "API role dropped a table (DDL should be denied)" \
  || { echo "$err" | grep -Eqi "permission denied|must be owner" && pass "API role cannot DROP TABLE" \
       || fail "API role DROP TABLE failed but not with permission denied: $err"; }
$PSQL -U postgres -d marketai_domain -c \
  "DROP TABLE IF EXISTS sec_test_drop" >/dev/null

# 3. Worker role cannot CREATE TABLE
err=$($PSQL -U marketai_domain_worker -d marketai_domain -c "CREATE TABLE sec_test_ddl(id int)" 2>&1) \
  && fail "Worker role created a table (DDL should be denied)" \
  || { echo "$err" | grep -qi "permission denied" && pass "Worker role cannot CREATE TABLE" \
       || fail "Worker role CREATE TABLE failed but not with permission denied: $err"; }

# 4. Readonly role cannot INSERT
err=$($PSQL -U marketai_domain_readonly -d marketai_domain -c \
  "BEGIN; INSERT INTO meta_accounts (id, workspace_id, ad_account_id, name, currency, timezone_name, api_version) \
   VALUES ('sec_ro_test', 'ws_sec', 'act_sec', 'test', 'USD', 'UTC', 'v21.0'); ROLLBACK;" 2>&1) \
  && fail "Readonly role inserted a row" \
  || { echo "$err" | grep -qi "permission denied" && pass "Readonly role cannot INSERT" \
       || fail "Readonly role INSERT failed but not with permission denied: $err"; }

# 5. Readonly role cannot UPDATE
err=$($PSQL -U marketai_domain_readonly -d marketai_domain -c \
  "BEGIN; UPDATE meta_accounts SET name = 'hacked' WHERE 1=1; ROLLBACK;" 2>&1) \
  && fail "Readonly role updated rows" \
  || { echo "$err" | grep -qi "permission denied" && pass "Readonly role cannot UPDATE" \
       || fail "Readonly role UPDATE failed but not with permission denied: $err"; }

# 6. Readonly role cannot DELETE
err=$($PSQL -U marketai_domain_readonly -d marketai_domain -c \
  "BEGIN; DELETE FROM meta_accounts WHERE 1=1; ROLLBACK;" 2>&1) \
  && fail "Readonly role deleted rows" \
  || { echo "$err" | grep -qi "permission denied" && pass "Readonly role cannot DELETE" \
       || fail "Readonly role DELETE failed but not with permission denied: $err"; }

# ----------------------------------------------------------------
# Category 2: Secret Table Protection
# ----------------------------------------------------------------
echo "--- Secret table protection ---"

# 7. API role cannot read tenant_context_secrets
err=$($PSQL -U marketai_domain_api -d marketai_domain -c \
  "SELECT * FROM tenant_context_secrets" 2>&1) \
  && fail "API role read tenant_context_secrets" \
  || { echo "$err" | grep -qi "permission denied" && pass "API role cannot read tenant_context_secrets" \
       || fail "API role read of tenant_context_secrets failed but not with permission denied: $err"; }

# 8. Worker role cannot read tenant_context_secrets
err=$($PSQL -U marketai_domain_worker -d marketai_domain -c \
  "SELECT * FROM tenant_context_secrets" 2>&1) \
  && fail "Worker role read tenant_context_secrets" \
  || { echo "$err" | grep -qi "permission denied" && pass "Worker role cannot read tenant_context_secrets" \
       || fail "Worker role read of tenant_context_secrets failed but not with permission denied: $err"; }

# 9. Readonly role cannot read tenant_context_secrets
err=$($PSQL -U marketai_domain_readonly -d marketai_domain -c \
  "SELECT * FROM tenant_context_secrets" 2>&1) \
  && fail "Readonly role read tenant_context_secrets" \
  || { echo "$err" | grep -qi "permission denied" && pass "Readonly role cannot read tenant_context_secrets" \
       || fail "Readonly role read of tenant_context_secrets failed but not with permission denied: $err"; }

# ----------------------------------------------------------------
# Category 3: BYPASSRLS Enforcement
# (compose-smoke.sh checks API; we check Worker + Readonly)
# ----------------------------------------------------------------
echo "--- BYPASSRLS enforcement ---"

# 10. Worker role has NOBYPASSRLS
worker_bypass=$($PSQL -U postgres -d marketai_domain -At -c \
  "SELECT rolbypassrls FROM pg_roles WHERE rolname='marketai_domain_worker'")
if [[ "$worker_bypass" == "f" ]]; then
  pass "Worker role has NOBYPASSRLS"
else
  fail "Worker role has BYPASSRLS=$worker_bypass"
fi

# 11. Readonly role has NOBYPASSRLS
readonly_bypass=$($PSQL -U postgres -d marketai_domain -At -c \
  "SELECT rolbypassrls FROM pg_roles WHERE rolname='marketai_domain_readonly'")
if [[ "$readonly_bypass" == "f" ]]; then
  pass "Readonly role has NOBYPASSRLS"
else
  fail "Readonly role has BYPASSRLS=$readonly_bypass"
fi

# ----------------------------------------------------------------
# Category 4: Tenant Isolation (Python inside API container)
# ----------------------------------------------------------------
echo "--- Tenant isolation ---"

# Tenant RLS also checks the authoritative Workspace projection and its
# authorization epoch. Seed the three test Workspaces through the admin seam;
# runtime-role inserts below must then succeed only with a valid signed scope.
$PSQL -U postgres -d marketai_domain -c "
  INSERT INTO workspaces
    (id, name, default_currency, lifecycle, budget_currency, created_at)
  VALUES
    ('ws_sec_a', 'ws_sec_a', 'USD', 'active', 'USD', now()),
    ('ws_sec_b', 'ws_sec_b', 'USD', 'active', 'USD', now()),
    ('ws_sec_hmac', 'ws_sec_hmac', 'USD', 'active', 'USD', now())
  ON CONFLICT (id) DO NOTHING
" >/dev/null

# 12. Valid tenant context allows queries
docker compose exec -T api python << 'PYEOF' \
  && pass "Valid tenant context allows queries" \
  || fail "Valid tenant context rejected"
from app.platform.tenancy import TenantContext
from app.adapters.persistence.database import tenant_session
from sqlalchemy import text

ctx = TenantContext(
    workspace_id="ws_sec_a", role="owner",
    initiator_id="sec-test", request_id="sec-valid",
)
with tenant_session(ctx) as session:
    ws = session.execute(
        text("SELECT current_setting('marketai.workspace_id')")
    ).scalar()
    assert ws == "ws_sec_a", f"Expected ws_sec_a, got {ws}"
    visible = session.execute(
        text("SELECT count(*) FROM workspaces WHERE id = 'ws_sec_a'")
    ).scalar()
    assert visible == 1, f"Valid scope saw {visible} Workspace rows"
PYEOF

# 13. Forged HMAC returns 0 rows (RLS blocks without valid signature)
docker compose exec -T api python << 'PYEOF' \
  && pass "Forged HMAC signature blocked by RLS" \
  || fail "Forged HMAC was not blocked"
from app.platform.tenancy import TenantContext
from app.adapters.persistence.database import tenant_session
from sqlalchemy import create_engine, text
import os, uuid

suffix = uuid.uuid4().hex[:8]
row_id = f"mact_hmac_{suffix}"

ctx_real = TenantContext(
    workspace_id="ws_sec_hmac", role="owner",
    initiator_id="sec-test", request_id=f"sec-seed-{suffix}",
)
with tenant_session(ctx_real) as session:
    session.execute(text(
        "INSERT INTO meta_accounts "
        "(id, workspace_id, ad_account_id, name, currency, timezone_name, api_version, created_at, updated_at) "
        "VALUES (:id, :ws, :act, 'HMAC Test', 'USD', 'UTC', 'v21.0', now(), now())"
    ), {"id": row_id, "ws": "ws_sec_hmac", "act": f"act_hmac_{suffix}"})

with tenant_session(ctx_real) as session:
    seeded = session.execute(
        text("SELECT count(*) FROM meta_accounts WHERE id = :id"),
        {"id": row_id},
    ).scalar()
    assert seeded == 1, f"Seed failed: {seeded} rows"

engine = create_engine(os.environ["MARKETAI_DATABASE_URL"])
with engine.connect() as conn:
    trans = conn.begin()
    try:
        conn.execute(text("SET LOCAL marketai.workspace_id = 'ws_sec_hmac'"))
        conn.execute(text("SET LOCAL marketai.role = 'owner'"))
        conn.execute(text("SET LOCAL marketai.initiator_id = 'attacker'"))
        conn.execute(text("SET LOCAL marketai.executor = 'attacker'"))
        conn.execute(text("SET LOCAL marketai.authorization_epoch = '1'"))
        conn.execute(text("SET LOCAL marketai.context_signature = 'deadbeef00'"))
        result = conn.execute(text(
            "SELECT count(*) FROM meta_accounts WHERE id = :id"
        ), {"id": row_id})
        count = result.scalar()
        assert count == 0, f"Forged HMAC returned {count} rows for seeded row (expected 0)"
    except Exception as e:
        if "signature" in str(e).lower() or "denied" in str(e).lower():
            pass
        else:
            raise
    finally:
        trans.rollback()
PYEOF

# 14. Cross-tenant read isolation
docker compose exec -T api python << 'PYEOF' \
  && pass "Cross-tenant read isolation enforced" \
  || fail "Cross-tenant read isolation failed"
from app.platform.tenancy import TenantContext
from app.adapters.persistence.database import tenant_session
from sqlalchemy import text
import uuid

suffix = uuid.uuid4().hex[:8]
row_id = f"mact_sec_{suffix}"

ctx_a = TenantContext(
    workspace_id="ws_sec_a", role="owner",
    initiator_id="sec-test", request_id=f"sec-insert-{suffix}",
)
with tenant_session(ctx_a) as session:
    session.execute(text(
        "INSERT INTO meta_accounts "
        "(id, workspace_id, ad_account_id, name, currency, timezone_name, api_version, created_at, updated_at) "
        "VALUES (:id, :ws, :act, 'Security Test', 'USD', 'UTC', 'v21.0', now(), now())"
    ), {"id": row_id, "ws": "ws_sec_a", "act": f"act_sec_{suffix}"})

with tenant_session(ctx_a) as session:
    own_count = session.execute(
        text("SELECT count(*) FROM meta_accounts WHERE id = :id"),
        {"id": row_id},
    ).scalar()
    assert own_count == 1, f"Own-tenant read returned {own_count} (expected 1)"

ctx_b = TenantContext(
    workspace_id="ws_sec_b", role="owner",
    initiator_id="sec-test", request_id=f"sec-cross-{suffix}",
)
with tenant_session(ctx_b) as session:
    cross_count = session.execute(
        text("SELECT count(*) FROM meta_accounts WHERE id = :id"),
        {"id": row_id},
    ).scalar()
    assert cross_count == 0, f"Cross-tenant read returned {cross_count} (expected 0)"
PYEOF

# ----------------------------------------------------------------
# Category 5: JWKS Integration
# ----------------------------------------------------------------
echo "--- JWKS integration ---"

# 15. JWKS endpoint returns valid JSON with 'keys' array
jwks_ok=$(curl -sf http://localhost:4002/.well-known/jwks.json 2>/dev/null \
  | python3 -c "
import sys, json
data = json.load(sys.stdin)
assert 'keys' in data, 'missing keys field'
assert isinstance(data['keys'], list), 'keys is not a list'
assert len(data['keys']) > 0, 'keys is empty'
print('valid')
" 2>&1) || jwks_ok="error"
if [[ "$jwks_ok" == "valid" ]]; then
  pass "JWKS endpoint returns valid structure"
else
  fail "JWKS endpoint invalid: $jwks_ok"
fi

# 16. API health confirms ready (JWKS subsystem loaded)
api_ok=$(curl -sf http://localhost:8787/api/health/ready 2>/dev/null \
  | python3 -c "
import sys, json
data = json.load(sys.stdin)
assert data.get('ok') is True, f'health not ok: {data}'
print('ready')
" 2>&1) || api_ok="error"
if [[ "$api_ok" == "ready" ]]; then
  pass "API health confirms ready (JWKS loaded)"
else
  fail "API health not ready: $api_ok"
fi

# --- Summary ---
echo ""
echo "=== Security Suite: $((TOTAL - FAILURES))/$TOTAL passed, $FAILURES failed ==="
echo "Finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
exit "$FAILURES"
