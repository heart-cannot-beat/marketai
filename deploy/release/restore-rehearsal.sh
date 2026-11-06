#!/usr/bin/env bash
# deploy/release/restore-rehearsal.sh
#
# Repeatable restore rehearsal with evidence. Proves that a logical backup
# can be restored to a sibling database and that the data survives intact.
#
# Verifications:
#   - Database identity (name, schema version)
#   - Row count equality for every table
#   - Money column totals (cost_ledger, ad_daily_metrics)
#   - Critical table content hashes
#   - Alembic/Prisma migration state preservation
#
# Output: structured evidence to stdout, exit code = number of failures.
#
# Environment:
#   CONTROL_OWNER_DATABASE_URL   control-plane owner connection
#   DOMAIN_OWNER_DATABASE_URL    domain owner connection
#   SUPERUSER_DATABASE_URL       superuser for creating sibling databases
#   REHEARSAL_CLEANUP             "true" (default) to drop sibling databases after
set -euo pipefail

: "${CONTROL_OWNER_DATABASE_URL:?CONTROL_OWNER_DATABASE_URL is required}"
: "${DOMAIN_OWNER_DATABASE_URL:?DOMAIN_OWNER_DATABASE_URL is required}"
: "${SUPERUSER_DATABASE_URL:?SUPERUSER_DATABASE_URL is required}"
: "${REHEARSAL_CLEANUP:=true}"

FAILURES=0
TOTAL=0
pass() { TOTAL=$((TOTAL + 1)); echo "  PASS: $1"; }
fail() { TOTAL=$((TOTAL + 1)); echo "  FAIL: $1"; FAILURES=$((FAILURES + 1)); }

DOMAIN_RESTORE="marketai_domain_rehearsal"
CONTROL_RESTORE="marketai_control_rehearsal"

SU_PSQL="psql --dbname=$SUPERUSER_DATABASE_URL"

cleanup() {
  if [[ "$REHEARSAL_CLEANUP" == "true" ]]; then
    echo "--- Cleanup ---"
    $SU_PSQL -c "DROP DATABASE IF EXISTS ${DOMAIN_RESTORE}" 2>/dev/null || true
    $SU_PSQL -c "DROP DATABASE IF EXISTS ${CONTROL_RESTORE}" 2>/dev/null || true
    echo "  Sibling databases removed"
  else
    echo "--- Skipping cleanup (REHEARSAL_CLEANUP=${REHEARSAL_CLEANUP}) ---"
    echo "  ${DOMAIN_RESTORE} and ${CONTROL_RESTORE} left for inspection"
  fi
}
trap cleanup EXIT

echo "=== Restore Rehearsal ==="
echo "Started: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo ""

# ----------------------------------------------------------------
# Phase 1: Capture source state
# ----------------------------------------------------------------
echo "--- Phase 1: Source state ---"

domain_alembic="$(psql --dbname="$DOMAIN_OWNER_DATABASE_URL" -At -c \
  "SELECT version_num FROM alembic_version LIMIT 1" 2>/dev/null || echo 'ERROR')"
echo "  Domain Alembic version: ${domain_alembic}"

control_prisma="$(psql --dbname="$CONTROL_OWNER_DATABASE_URL" -At -c \
  "SELECT migration_name FROM _prisma_migrations ORDER BY finished_at DESC LIMIT 1" 2>/dev/null || echo 'ERROR')"
echo "  Control Prisma version: ${control_prisma}"

# ----------------------------------------------------------------
# Phase 2: Backup and restore to sibling databases
# ----------------------------------------------------------------
echo ""
echo "--- Phase 2: Backup and restore ---"

$SU_PSQL -c "DROP DATABASE IF EXISTS ${DOMAIN_RESTORE}" 2>/dev/null || true
$SU_PSQL -c "DROP DATABASE IF EXISTS ${CONTROL_RESTORE}" 2>/dev/null || true

$SU_PSQL -c "CREATE DATABASE ${DOMAIN_RESTORE}" \
  && pass "Created sibling: ${DOMAIN_RESTORE}" \
  || fail "Could not create ${DOMAIN_RESTORE}"

$SU_PSQL -c "CREATE DATABASE ${CONTROL_RESTORE}" \
  && pass "Created sibling: ${CONTROL_RESTORE}" \
  || fail "Could not create ${CONTROL_RESTORE}"

pg_dump --format=custom --no-owner --no-privileges \
  --dbname="$DOMAIN_OWNER_DATABASE_URL" \
  | pg_restore --dbname="${SUPERUSER_DATABASE_URL%/*}/${DOMAIN_RESTORE}" \
    --no-owner --no-privileges 2>&1 \
  && pass "Domain backup+restore" \
  || fail "Domain backup+restore failed"

pg_dump --format=custom --no-owner --no-privileges \
  --dbname="$CONTROL_OWNER_DATABASE_URL" \
  | pg_restore --dbname="${SUPERUSER_DATABASE_URL%/*}/${CONTROL_RESTORE}" \
    --no-owner --no-privileges 2>&1 \
  && pass "Control backup+restore" \
  || fail "Control backup+restore failed"

# Build a connection string for the restored sibling
DOMAIN_RESTORED_URL="${SUPERUSER_DATABASE_URL%/*}/${DOMAIN_RESTORE}"
CONTROL_RESTORED_URL="${SUPERUSER_DATABASE_URL%/*}/${CONTROL_RESTORE}"

# ----------------------------------------------------------------
# Phase 3: Database identity
# ----------------------------------------------------------------
echo ""
echo "--- Phase 3: Identity verification ---"

restored_domain_alembic="$(psql --dbname="$DOMAIN_RESTORED_URL" -At -c \
  "SELECT version_num FROM alembic_version LIMIT 1" 2>/dev/null || echo 'ERROR')"

if [[ "$domain_alembic" == "$restored_domain_alembic" ]] && [[ "$domain_alembic" != "ERROR" ]]; then
  pass "Domain Alembic version: ${domain_alembic}"
else
  fail "Domain Alembic mismatch: source=${domain_alembic} restored=${restored_domain_alembic}"
fi

restored_control_prisma="$(psql --dbname="$CONTROL_RESTORED_URL" -At -c \
  "SELECT migration_name FROM _prisma_migrations ORDER BY finished_at DESC LIMIT 1" 2>/dev/null || echo 'ERROR')"

if [[ "$control_prisma" == "$restored_control_prisma" ]] && [[ "$control_prisma" != "ERROR" ]]; then
  pass "Control Prisma version: ${control_prisma}"
else
  fail "Control Prisma mismatch: source=${control_prisma} restored=${restored_control_prisma}"
fi

# ----------------------------------------------------------------
# Phase 4: Row count equality
# ----------------------------------------------------------------
echo ""
echo "--- Phase 4: Row counts ---"

verify_row_counts() {
  local source_url="$1" restored_url="$2" label="$3"
  local tables
  tables="$(psql --dbname="$source_url" -At -c "
    SELECT tablename FROM pg_tables
    WHERE schemaname = 'public' ORDER BY tablename
  " 2>/dev/null)"

  local table_failures=0
  while IFS= read -r table; do
    [[ -z "$table" ]] && continue
    local orig rest
    orig="$(psql --dbname="$source_url" -At -c "SELECT count(*) FROM \"$table\"" 2>/dev/null)"
    rest="$(psql --dbname="$restored_url" -At -c "SELECT count(*) FROM \"$table\"" 2>/dev/null || echo 'MISSING')"
    if [[ "$orig" != "$rest" ]]; then
      fail "${label} ${table}: source=${orig} restored=${rest}"
      table_failures=$((table_failures + 1))
    fi
  done <<< "$tables"

  if [[ "$table_failures" -eq 0 ]]; then
    pass "${label} row counts match"
  fi
}

verify_row_counts "$DOMAIN_OWNER_DATABASE_URL" "$DOMAIN_RESTORED_URL" "Domain"
verify_row_counts "$CONTROL_OWNER_DATABASE_URL" "$CONTROL_RESTORED_URL" "Control"

# ----------------------------------------------------------------
# Phase 5: Money totals
# ----------------------------------------------------------------
echo ""
echo "--- Phase 5: Money totals ---"

verify_money_total() {
  local source_url="$1" restored_url="$2" table="$3" column="$4" label="$5"

  local source_exists
  source_exists="$(psql --dbname="$source_url" -At -c "
    SELECT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema='public' AND table_name='$table' AND column_name='$column')
  " 2>/dev/null || echo 'f')"

  if [[ "$source_exists" != "t" ]]; then
    echo "  SKIP: ${label} (${table}.${column} does not exist)"
    return
  fi

  local orig rest
  orig="$(psql --dbname="$source_url" -At -c "SELECT COALESCE(SUM(\"$column\"), 0)::text FROM \"$table\"" 2>/dev/null)"
  rest="$(psql --dbname="$restored_url" -At -c "SELECT COALESCE(SUM(\"$column\"), 0)::text FROM \"$table\"" 2>/dev/null || echo 'ERROR')"

  if [[ "$orig" == "$rest" ]]; then
    pass "${label}: ${orig}"
  else
    fail "${label}: source=${orig} restored=${rest}"
  fi
}

verify_money_total "$DOMAIN_OWNER_DATABASE_URL" "$DOMAIN_RESTORED_URL" \
  "cost_ledger" "amount" "Domain cost_ledger.amount"

verify_money_total "$DOMAIN_OWNER_DATABASE_URL" "$DOMAIN_RESTORED_URL" \
  "cost_ledger" "estimated_amount" "Domain cost_ledger.estimated_amount"

verify_money_total "$DOMAIN_OWNER_DATABASE_URL" "$DOMAIN_RESTORED_URL" \
  "ad_daily_metrics" "spend" "Domain ad_daily_metrics.spend"

# ----------------------------------------------------------------
# Phase 6: Critical table content hashes
# ----------------------------------------------------------------
echo ""
echo "--- Phase 6: Content hashes ---"

verify_content_hash() {
  local source_url="$1" restored_url="$2" table="$3" label="$4"

  local source_exists
  source_exists="$(psql --dbname="$source_url" -At -c "
    SELECT EXISTS (SELECT 1 FROM information_schema.tables
                   WHERE table_schema='public' AND table_name='$table')
  " 2>/dev/null || echo 'f')"

  if [[ "$source_exists" != "t" ]]; then
    echo "  SKIP: ${label} (table does not exist)"
    return
  fi

  local orig rest
  orig="$(psql --dbname="$source_url" -At -c "
    SELECT md5(string_agg(t::text, '' ORDER BY t))
    FROM \"$table\" AS t
  " 2>/dev/null || echo 'ERROR')"
  rest="$(psql --dbname="$restored_url" -At -c "
    SELECT md5(string_agg(t::text, '' ORDER BY t))
    FROM \"$table\" AS t
  " 2>/dev/null || echo 'ERROR')"

  if [[ "$orig" == "$rest" ]]; then
    pass "${label}: ${orig:0:12}..."
  else
    fail "${label}: source=${orig:0:12} restored=${rest:0:12}"
  fi
}

verify_content_hash "$DOMAIN_OWNER_DATABASE_URL" "$DOMAIN_RESTORED_URL" \
  "alembic_version" "Domain alembic_version"

verify_content_hash "$DOMAIN_OWNER_DATABASE_URL" "$DOMAIN_RESTORED_URL" \
  "workspaces" "Domain workspaces"

verify_content_hash "$CONTROL_OWNER_DATABASE_URL" "$CONTROL_RESTORED_URL" \
  "_prisma_migrations" "Control _prisma_migrations"

# ----------------------------------------------------------------
# Summary
# ----------------------------------------------------------------
echo ""
echo "=== Restore Rehearsal: $((TOTAL - FAILURES))/$TOTAL passed, $FAILURES failed ==="
echo "Finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
exit "$FAILURES"
