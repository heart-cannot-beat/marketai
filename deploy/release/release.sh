#!/usr/bin/env bash
# deploy/release/release.sh
#
# Run-to-completion release for the MarketAI Railway deployment.
# Entrypoint for the Release service container (Dockerfile.release).
#
# Ordered sequence:
#   1. Pre-flight — verify environment, database identity, connectivity
#   2. Pre-migration backup — logical dump of both databases
#   3. Control-plane migration — Prisma migrate deploy
#   4. Domain migration — roles → nonce verify → alembic → re-grant
#   5. Post-migration verification — schema consistency checks
#   6. Evidence — structured output for audit
#
# The ordering matters: the control-plane schema must advance first because
# domain services may depend on control-plane tables (e.g. entitlement
# projections, workspace provisioning). Rolling back is also cleaner when
# the domain migration has not yet run.
#
# Environment (all required):
#   CONTROL_OWNER_DATABASE_URL    control-plane as marketai_control_owner
#   DOMAIN_OWNER_DATABASE_URL     domain database as marketai_domain_owner
#   MARKETAI_DATABASE_URL         domain database as marketai_domain_owner (for Alembic)
#   MIGRATION_DATABASE_URL        control-plane as marketai_control_owner (for Prisma)
#   SUPERUSER_DATABASE_URL        superuser for role convergence
#   DOMAIN_API_PASSWORD           }
#   DOMAIN_WORKER_PASSWORD        } domain runtime role passwords
#   DOMAIN_READONLY_PASSWORD      }
#   MARKETAI_TENANT_CONTEXT_SECRET  ≥32 byte HMAC secret
#
# Optional:
#   BACKUP_DIR                    where to write pre-migration dumps
#   RELEASE_SKIP_BACKUP           set to "true" to skip backup (staging only)
#   RAILWAY_ENVIRONMENT           "staging" or "production"
#   MARKETAI_BLOB_STORE_ADAPTER   must be "railway_bucket" in production
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
cd "$repo"

RAILWAY_ENVIRONMENT="${RAILWAY_ENVIRONMENT:-unknown}"

echo "================================================================="
echo "  MarketAI Release — ${RAILWAY_ENVIRONMENT}"
echo "  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "  git: $(git rev-parse --short HEAD 2>/dev/null || echo 'unknown')"
echo "================================================================="
echo ""

# ----------------------------------------------------------------
# Step 1: Pre-flight checks
# ----------------------------------------------------------------
echo "==> 1/6 Pre-flight"

: "${CONTROL_OWNER_DATABASE_URL:?CONTROL_OWNER_DATABASE_URL is required}"
: "${DOMAIN_OWNER_DATABASE_URL:?DOMAIN_OWNER_DATABASE_URL is required}"
: "${MARKETAI_DATABASE_URL:?MARKETAI_DATABASE_URL is required}"
: "${MIGRATION_DATABASE_URL:?MIGRATION_DATABASE_URL is required}"
: "${SUPERUSER_DATABASE_URL:?SUPERUSER_DATABASE_URL is required}"
: "${DOMAIN_API_PASSWORD:?DOMAIN_API_PASSWORD is required}"
: "${DOMAIN_WORKER_PASSWORD:?DOMAIN_WORKER_PASSWORD is required}"
: "${DOMAIN_READONLY_PASSWORD:?DOMAIN_READONLY_PASSWORD is required}"
: "${MARKETAI_TENANT_CONTEXT_SECRET:?MARKETAI_TENANT_CONTEXT_SECRET is required}"

if [[ "$RAILWAY_ENVIRONMENT" == "production" \
      && "${MARKETAI_BLOB_STORE_ADAPTER:-}" != "railway_bucket" ]]; then
  echo "FATAL: production requires MARKETAI_BLOB_STORE_ADAPTER=railway_bucket" >&2
  exit 1
fi

if [[ "${MARKETAI_BLOB_STORE_ADAPTER:-}" == "railway_bucket" ]]; then
  : "${MARKETAI_BLOB_STORE_S3_BUCKET:?MARKETAI_BLOB_STORE_S3_BUCKET is required}"
  : "${MARKETAI_BLOB_STORE_S3_ENDPOINT_URL:?MARKETAI_BLOB_STORE_S3_ENDPOINT_URL is required}"
  : "${MARKETAI_BLOB_STORE_S3_REGION:?MARKETAI_BLOB_STORE_S3_REGION is required}"
  : "${AWS_ACCESS_KEY_ID:?AWS_ACCESS_KEY_ID is required}"
  : "${AWS_SECRET_ACCESS_KEY:?AWS_SECRET_ACCESS_KEY is required}"
fi

case "$MARKETAI_DATABASE_URL" in
  postgres://*|postgresql://*|postgresql+*://*)
    ;;
  *)
    echo "FATAL: MARKETAI_DATABASE_URL is not a PostgreSQL URL." >&2
    echo "The Release service only operates on hosted PostgreSQL." >&2
    exit 1
    ;;
esac

# Verify connectivity to both databases
control_db="$(psql --dbname="$CONTROL_OWNER_DATABASE_URL" -At -c "SELECT current_database()" 2>/dev/null)" || {
  echo "FATAL: cannot connect to control-plane database" >&2; exit 1
}
domain_db="$(psql --dbname="$DOMAIN_OWNER_DATABASE_URL" -At -c "SELECT current_database()" 2>/dev/null)" || {
  echo "FATAL: cannot connect to domain database" >&2; exit 1
}

if [[ "$control_db" != "marketai_control" ]]; then
  echo "FATAL: CONTROL_OWNER_DATABASE_URL connects to '${control_db}', not 'marketai_control'" >&2
  exit 1
fi
if [[ "$domain_db" != "marketai_domain" ]]; then
  echo "FATAL: DOMAIN_OWNER_DATABASE_URL connects to '${domain_db}', not 'marketai_domain'" >&2
  exit 1
fi

control_user="$(psql --dbname="$CONTROL_OWNER_DATABASE_URL" -At -c "SELECT current_user" 2>/dev/null)"
domain_user="$(psql --dbname="$DOMAIN_OWNER_DATABASE_URL" -At -c "SELECT current_user" 2>/dev/null)"

if [[ "$control_user" != "marketai_control_owner" ]]; then
  echo "FATAL: CONTROL_OWNER_DATABASE_URL connects as '${control_user}', not 'marketai_control_owner'" >&2
  exit 1
fi
if [[ "$domain_user" != "marketai_domain_owner" ]]; then
  echo "FATAL: DOMAIN_OWNER_DATABASE_URL connects as '${domain_user}', not 'marketai_domain_owner'" >&2
  exit 1
fi

echo "  Control-plane: ${control_db} as ${control_user} ✓"
echo "  Domain:        ${domain_db} as ${domain_user} ✓"

if [[ "${MARKETAI_BLOB_STORE_ADAPTER:-}" == "railway_bucket" ]]; then
  # PYTHON may intentionally be a multi-word command in CI/release images.
  # shellcheck disable=SC2086
  ${PYTHON:-python3} -m scripts.verify_railway_bucket
  echo "  Railway Bucket: read/write/presign/delete ✓"
  if [[ -n "${MARKETAI_BLOB_STORE_BROWSER_ORIGINS:-}" ]]; then
    echo "  Railway Bucket: browser CORS converged for the Web origin ✓"
  else
    echo "  WARNING: MARKETAI_BLOB_STORE_BROWSER_ORIGINS is unset; bucket CORS was not converged and browser blob transfers (gallery, uploads) will fail" >&2
  fi
else
  echo "  Railway Bucket: skipped outside production"
fi

# ----------------------------------------------------------------
# Step 2: Pre-migration backup
# ----------------------------------------------------------------
echo ""
echo "==> 2/6 Pre-migration backup"

if [[ "${RELEASE_SKIP_BACKUP:-false}" == "true" ]]; then
  echo "  RELEASE_SKIP_BACKUP=true — skipping (staging only)"
  if [[ "$RAILWAY_ENVIRONMENT" == "production" ]]; then
    echo "FATAL: RELEASE_SKIP_BACKUP=true is not allowed in production" >&2
    exit 1
  fi
else
  BACKUP_LABEL="pre-release" \
    bash "$here/backup.sh"
fi

# ----------------------------------------------------------------
# Step 3: Control-plane migration (Prisma)
# ----------------------------------------------------------------
echo ""
echo "==> 3/6 Control-plane migration (Prisma)"

prisma_version_before="$(psql --dbname="$CONTROL_OWNER_DATABASE_URL" -At -c \
  "SELECT migration_name FROM _prisma_migrations ORDER BY finished_at DESC LIMIT 1" 2>/dev/null || echo 'none')"
echo "  Before: ${prisma_version_before}"

cd "$repo/web"
node scripts/prisma-migrate.mjs deploy
cd "$repo"

prisma_version_after="$(psql --dbname="$CONTROL_OWNER_DATABASE_URL" -At -c \
  "SELECT migration_name FROM _prisma_migrations ORDER BY finished_at DESC LIMIT 1" 2>/dev/null || echo 'none')"
echo "  After:  ${prisma_version_after}"

if [[ "$prisma_version_before" == "$prisma_version_after" ]]; then
  echo "  No new control-plane migrations applied"
else
  echo "  Control-plane migrated: ${prisma_version_before} → ${prisma_version_after}"
fi

# ----------------------------------------------------------------
# Step 4: Domain migration (Alembic via existing release.sh)
# ----------------------------------------------------------------
echo ""
echo "==> 4/6 Domain migration (Alembic)"

alembic_version_before="$(psql --dbname="$DOMAIN_OWNER_DATABASE_URL" -At -c \
  "SELECT version_num FROM alembic_version LIMIT 1" 2>/dev/null || echo 'none')"
echo "  Before: ${alembic_version_before}"

PYTHON="${PYTHON:-python3}" \
ALEMBIC="${ALEMBIC:-alembic}" \
  bash "$repo/deploy/postgres/release.sh"

alembic_version_after="$(psql --dbname="$DOMAIN_OWNER_DATABASE_URL" -At -c \
  "SELECT version_num FROM alembic_version LIMIT 1" 2>/dev/null || echo 'none')"
echo "  After:  ${alembic_version_after}"

if [[ "$alembic_version_before" == "$alembic_version_after" ]]; then
  echo "  No new domain migrations applied"
else
  echo "  Domain migrated: ${alembic_version_before} → ${alembic_version_after}"
fi

# ----------------------------------------------------------------
# Step 5: Post-migration verification
# ----------------------------------------------------------------
echo ""
echo "==> 5/6 Post-migration verification"

# Prisma: no pending migrations
prisma_status_exit=0
cd "$repo/web"
node scripts/prisma-migrate.mjs status || prisma_status_exit=$?
cd "$repo"
if [[ "$prisma_status_exit" -ne 0 ]]; then
  echo "FATAL: Prisma reports pending or failed migrations after deploy" >&2
  exit 1
fi
echo "  Prisma migrate status: clean ✓"

# Alembic: head matches current
alembic_current="$(alembic current 2>/dev/null | head -1 || echo 'error')"
alembic_check_exit=0
alembic check 2>/dev/null || alembic_check_exit=$?
if [[ "$alembic_check_exit" -ne 0 ]]; then
  echo "FATAL: Alembic detects schema drift after migration" >&2
  exit 1
fi
echo "  Alembic check: no drift ✓"
echo "  Alembic current: ${alembic_current}"

# ----------------------------------------------------------------
# Step 6: Evidence
# ----------------------------------------------------------------
echo ""
echo "==> 6/6 Release evidence"

cat <<EVIDENCE
{
  "release": {
    "environment": "${RAILWAY_ENVIRONMENT}",
    "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
    "git_sha": "$(git rev-parse HEAD 2>/dev/null || echo 'unknown')",
    "control_plane": {
      "before": "${prisma_version_before}",
      "after": "${prisma_version_after}"
    },
    "domain": {
      "before": "${alembic_version_before}",
      "after": "${alembic_version_after}"
    },
    "status": "success"
  }
}
EVIDENCE

echo ""
echo "================================================================="
echo "  Release complete — ${RAILWAY_ENVIRONMENT}"
echo "================================================================="
