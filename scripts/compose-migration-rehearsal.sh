#!/usr/bin/env bash
# scripts/compose-migration-rehearsal.sh
# Backup/restore rehearsal — runs against a live compose stack.
# Proves pg_dump/pg_restore data integrity for both domain and control databases.
# Exits non-zero on any failure; captures evidence to stdout.
set -euo pipefail

echo "=== Compose Migration Rehearsal ==="
echo "Started: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo ""

FAILURES=0
TOTAL=0
pass() { TOTAL=$((TOTAL + 1)); echo "  PASS: $1"; }
fail() { TOTAL=$((TOTAL + 1)); echo "  FAIL: $1"; FAILURES=$((FAILURES + 1)); }

PSQL="docker compose exec -T postgres psql"
PGDUMP="docker compose exec -T postgres pg_dump"
PGRESTORE="docker compose exec -T postgres pg_restore"
CREATEDB="docker compose exec -T postgres createdb"
DROPDB="docker compose exec -T postgres dropdb"

cleanup() {
  echo "--- Cleanup ---"
  $DROPDB -U postgres marketai_domain_restore 2>/dev/null || true
  $DROPDB -U postgres marketai_control_restore 2>/dev/null || true
  echo "  Temporary databases removed"
}
trap cleanup EXIT

# ----------------------------------------------------------------
# Phase 1: Seed test data so backup/restore has rows to verify
# ----------------------------------------------------------------
echo "--- Seeding test data ---"

suffix=$($PSQL -U postgres -d marketai_domain -At -c \
  "SELECT substr(md5(random()::text || clock_timestamp()::text), 1, 8)")

# A signed tenant context is valid only for an authoritative Workspace
# projection. Provision that boundary through the admin seam before proving
# that the runtime role can insert tenant-scoped rehearsal data.
$PSQL -U postgres -d marketai_domain -c "
  INSERT INTO workspaces
    (id, name, default_currency, lifecycle, budget_currency, created_at)
  VALUES
    ('ws_rehearsal_${suffix}', 'ws_rehearsal_${suffix}', 'USD', 'active', 'USD', now())
" >/dev/null

docker compose exec -T -e REHEARSAL_SUFFIX="$suffix" api python << 'PYEOF'
from app.platform.tenancy import TenantContext
from app.adapters.persistence.database import tenant_session
from sqlalchemy import text
import os

suffix = os.environ["REHEARSAL_SUFFIX"]
ctx = TenantContext(
    workspace_id=f"ws_rehearsal_{suffix}", role="owner",
    initiator_id="rehearsal-seed", request_id=f"rehearsal-{suffix}",
)
with tenant_session(ctx) as session:
    session.execute(text(
        "INSERT INTO meta_accounts "
        "(id, workspace_id, ad_account_id, name, currency, timezone_name, api_version, created_at, updated_at) "
        "VALUES (:id, :ws, :act, 'Rehearsal Test', 'USD', 'UTC', 'v21.0', now(), now())"
    ), {"id": f"mact_rh_{suffix}", "ws": f"ws_rehearsal_{suffix}", "act": f"act_rh_{suffix}"})

print(f"SEED_SUFFIX={suffix}")
PYEOF

echo "  Test data seeded"

# ----------------------------------------------------------------
# Phase 2: Capture pre-backup row counts
# ----------------------------------------------------------------
echo "--- Pre-backup counts ---"

domain_tables_pre=$($PSQL -U marketai_domain_owner -d marketai_domain -At -c "
    SELECT tablename || '=' || n_live_tup
    FROM pg_stat_user_tables
    WHERE schemaname = 'public'
    ORDER BY tablename
" 2>/dev/null) || domain_tables_pre="(query failed)"
echo "  Domain table counts (pre-backup):"
echo "$domain_tables_pre" | while IFS= read -r line; do echo "    $line"; done

control_tables_pre=$($PSQL -U postgres -d marketai_control -At -c "
    SELECT tablename || '=' || n_live_tup
    FROM pg_stat_user_tables
    WHERE schemaname = 'public'
    ORDER BY tablename
" 2>/dev/null) || control_tables_pre="(no access or empty)"
echo "  Control table counts (pre-backup):"
echo "$control_tables_pre" | while IFS= read -r line; do echo "    $line"; done

# ----------------------------------------------------------------
# Phase 3: Backup both databases
# ----------------------------------------------------------------
echo "--- Backing up databases ---"

$PGDUMP -U marketai_domain_owner -Fc marketai_domain > /dev/null 2>&1 \
  && pass "Domain database backup (pg_dump -Fc)" \
  || fail "Domain database backup failed"

$PGDUMP -U postgres -Fc marketai_control > /dev/null 2>&1 \
  && pass "Control database backup (pg_dump -Fc)" \
  || fail "Control database backup failed"

# ----------------------------------------------------------------
# Phase 4: Restore to temporary databases
# ----------------------------------------------------------------
echo "--- Restoring to temporary databases ---"

$CREATEDB -U postgres marketai_domain_restore 2>/dev/null \
  && pass "Created restore target: marketai_domain_restore" \
  || fail "Could not create marketai_domain_restore"

$CREATEDB -U postgres marketai_control_restore 2>/dev/null \
  && pass "Created restore target: marketai_control_restore" \
  || fail "Could not create marketai_control_restore"

# pg_dump to stdout | pg_restore from stdin, both inside the container
docker compose exec -T postgres bash -c \
  'pg_dump -U marketai_domain_owner -Fc marketai_domain | pg_restore -U postgres -d marketai_domain_restore --no-owner --no-privileges' 2>&1 \
  && pass "Domain database restored" \
  || fail "Domain database restore failed"

docker compose exec -T postgres bash -c \
  'pg_dump -U postgres -Fc marketai_control | pg_restore -U postgres -d marketai_control_restore --no-owner --no-privileges' 2>&1 \
  && pass "Control database restored" \
  || fail "Control database restore failed"

# ----------------------------------------------------------------
# Phase 5: Verify row count equality
# ----------------------------------------------------------------
echo "--- Verifying row count equality ---"

# Use actual row counts via count(*) for both original and restored
domain_count_check=$(docker compose exec -T postgres bash -c '
    tables=$(psql -U marketai_domain_owner -d marketai_domain -At -c "
        SELECT tablename FROM pg_tables WHERE schemaname = '\''public'\'' ORDER BY tablename
    ")
    exit_code=0
    while IFS= read -r table; do
        [ -z "$table" ] && continue
        orig=$(psql -U marketai_domain_owner -d marketai_domain -At -c "SELECT count(*) FROM \"$table\"")
        rest=$(psql -U postgres -d marketai_domain_restore -At -c "SELECT count(*) FROM \"$table\"" 2>/dev/null || echo "MISSING")
        if [ "$orig" != "$rest" ]; then
            echo "MISMATCH:$table:orig=$orig:restored=$rest"
            exit_code=1
        fi
    done <<< "$tables"
    exit $exit_code
') && pass "Domain row counts match after restore" \
   || fail "Domain row count mismatch: $domain_count_check"

control_count_check=$(docker compose exec -T postgres bash -c '
    tables=$(psql -U postgres -d marketai_control -At -c "
        SELECT tablename FROM pg_tables WHERE schemaname = '\''public'\'' ORDER BY tablename
    ")
    exit_code=0
    while IFS= read -r table; do
        [ -z "$table" ] && continue
        orig=$(psql -U postgres -d marketai_control -At -c "SELECT count(*) FROM \"$table\"")
        rest=$(psql -U postgres -d marketai_control_restore -At -c "SELECT count(*) FROM \"$table\"" 2>/dev/null || echo "MISSING")
        if [ "$orig" != "$rest" ]; then
            echo "MISMATCH:$table:orig=$orig:restored=$rest"
            exit_code=1
        fi
    done <<< "$tables"
    exit $exit_code
') && pass "Control row counts match after restore" \
   || fail "Control row count mismatch: $control_count_check"

# ----------------------------------------------------------------
# Phase 6: Verify Alembic revision survived restore
# ----------------------------------------------------------------
echo "--- Verifying migration state ---"

orig_rev=$($PSQL -U marketai_domain_owner -d marketai_domain -At -c \
  "SELECT version_num FROM alembic_version LIMIT 1" 2>/dev/null) || orig_rev="ERROR"
restored_rev=$($PSQL -U postgres -d marketai_domain_restore -At -c \
  "SELECT version_num FROM alembic_version LIMIT 1" 2>/dev/null) || restored_rev="ERROR"
if [ "$orig_rev" = "$restored_rev" ] && [ -n "$orig_rev" ] && [ "$orig_rev" != "ERROR" ]; then
  pass "Alembic revision matches: $orig_rev"
else
  fail "Alembic revision mismatch: original=$orig_rev restored=$restored_rev"
fi

# Phase 7 cleanup handled by EXIT trap

# --- Summary ---
echo ""
echo "=== Migration Rehearsal: $((TOTAL - FAILURES))/$TOTAL passed, $FAILURES failed ==="
echo "Finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
exit "$FAILURES"
