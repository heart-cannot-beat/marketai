#!/usr/bin/env bash
# deploy/release/backup.sh
#
# Portable logical backup for both control-plane and domain databases.
# Produces custom-format pg_dump files stamped with metadata.
#
# Modes:
#   pre-release   Called by release.sh before migrations. Requires both
#                 owner connection strings.
#   scheduled     Called by a cron job for daily/weekly backups. Same env.
#
# Output directory: $BACKUP_DIR (default: /tmp/marketai-backups).
# Each dump is named <database>_<timestamp>_<git-sha>.dump with a companion
# .meta.json file recording the migration version at backup time.
#
# Environment:
#   CONTROL_OWNER_DATABASE_URL   control-plane as marketai_control_owner
#   DOMAIN_OWNER_DATABASE_URL    domain database as marketai_domain_owner
#   BACKUP_DIR                   where to write dumps (default /tmp/marketai-backups)
#   BACKUP_LABEL                 optional tag (e.g. "pre-release", "daily", "weekly")
#   BACKUP_RETENTION_DAYS        days to keep old backups (default 30; 0 = no cleanup)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

: "${CONTROL_OWNER_DATABASE_URL:?CONTROL_OWNER_DATABASE_URL is required}"
: "${DOMAIN_OWNER_DATABASE_URL:?DOMAIN_OWNER_DATABASE_URL is required}"
: "${BACKUP_DIR:=/tmp/marketai-backups}"
: "${BACKUP_LABEL:=manual}"
: "${BACKUP_RETENTION_DAYS:=30}"

mkdir -p "$BACKUP_DIR"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
git_sha="$(git rev-parse --short HEAD 2>/dev/null || echo 'unknown')"
prefix="${timestamp}_${git_sha}"

# ---- Helper: dump one database and capture metadata ----
dump_database() {
  local label="$1" url="$2" db_name="$3" version_query="$4"
  local dump_file="${BACKUP_DIR}/${db_name}_${prefix}.dump"
  local meta_file="${BACKUP_DIR}/${db_name}_${prefix}.meta.json"

  echo "  Dumping ${db_name}..."
  pg_dump --format=custom --no-owner --no-privileges \
    --dbname="$url" \
    > "$dump_file"

  local dump_size
  dump_size="$(wc -c < "$dump_file" | tr -d ' ')"

  local migration_version
  migration_version="$(psql --dbname="$url" -At -c "$version_query" 2>/dev/null || echo 'unknown')"

  local db_identity
  db_identity="$(psql --dbname="$url" -At -c "SELECT current_database()" 2>/dev/null || echo 'unknown')"

  cat > "$meta_file" <<METAEOF
{
  "database": "${db_name}",
  "database_identity": "${db_identity}",
  "timestamp": "${timestamp}",
  "git_sha": "${git_sha}",
  "label": "${BACKUP_LABEL}",
  "migration_version": "${migration_version}",
  "dump_file": "$(basename "$dump_file")",
  "dump_bytes": ${dump_size}
}
METAEOF

  if [[ "${MARKETAI_BLOB_STORE_ADAPTER:-}" == "railway_bucket" ]]; then
    # Keep production dumps in the existing private deployment bucket. No
    # public URL is issued and the release aborts if persistence fails.
    ${PYTHON:-python3} "$here/persist_backup.py" "$dump_file" "$meta_file"
  fi
  echo "  ${db_name}: $(basename "$dump_file") (${dump_size} bytes, version=${migration_version})"
}

echo "==> Backup started: ${timestamp} [${BACKUP_LABEL}]"

dump_database "control" \
  "$CONTROL_OWNER_DATABASE_URL" \
  "marketai_control" \
  "SELECT MAX(finished_at)::text FROM _prisma_migrations WHERE finished_at IS NOT NULL"

dump_database "domain" \
  "$DOMAIN_OWNER_DATABASE_URL" \
  "marketai_domain" \
  "SELECT version_num FROM alembic_version LIMIT 1"

# ---- Retention cleanup ----
if [[ "$BACKUP_RETENTION_DAYS" -gt 0 ]]; then
  echo "  Cleaning backups older than ${BACKUP_RETENTION_DAYS} days..."
  find "$BACKUP_DIR" -name "marketai_*.dump" -mtime "+${BACKUP_RETENTION_DAYS}" -delete 2>/dev/null || true
  find "$BACKUP_DIR" -name "marketai_*.meta.json" -mtime "+${BACKUP_RETENTION_DAYS}" -delete 2>/dev/null || true
fi

echo "==> Backup complete: ${BACKUP_DIR}/${prefix}"
