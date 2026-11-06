# Backup Runbook

## Backup Strategy

MarketAI uses three complementary backup mechanisms:

| Mechanism | RPO | Scope | Retention |
|-----------|-----|-------|-----------|
| Railway PITR | ~1 minute | Full cluster | Railway-managed |
| Pre-release logical dump | Per-release | Both databases | 30 days |
| Scheduled logical dump | Daily/weekly | Both databases | 30 days |

## Railway PITR (Point-in-Time Recovery)

Railway PostgreSQL plugins include continuous WAL archiving. PITR is always
enabled and provides the finest-grained recovery.

### Configuration

PITR is a Railway platform feature — no application-level configuration needed.
Verify it is active in the Railway dashboard under each PostgreSQL plugin's
settings.

### Restore via PITR

PITR restores to a new Railway PostgreSQL plugin (sibling), not in-place.
This is the correct behavior — it prevents accidental mutation of the live
database.

1. Open Railway dashboard → PostgreSQL plugin → Backups
2. Select a point in time (within 15 minutes of the target)
3. Railway provisions a new plugin with the restored data
4. Verify using `deploy/release/restore-rehearsal.sh` against the sibling
5. If valid, update service connection strings to point at the restored plugin
6. Decommission the old plugin after confirming health

## Pre-Release Backup

Every production release automatically takes a logical dump before any
migration runs. This is enforced by `deploy/release/release.sh` — setting
`RELEASE_SKIP_BACKUP=true` is rejected in production.

### Backup artifacts

Each backup produces two files per database in `$BACKUP_DIR`:

```
marketai_control_20260922T143000Z_abc1234.dump
marketai_control_20260922T143000Z_abc1234.meta.json
marketai_domain_20260922T143000Z_abc1234.dump
marketai_domain_20260922T143000Z_abc1234.meta.json
```

The `.meta.json` file records:
- Database identity (name, connected user)
- Timestamp and git SHA
- Migration version at backup time
- Dump file size

### Manual pre-release backup

```bash
CONTROL_OWNER_DATABASE_URL="..." \
DOMAIN_OWNER_DATABASE_URL="..." \
BACKUP_LABEL="manual" \
  bash deploy/release/backup.sh
```

## Scheduled Backups

Configure a Railway cron service or external scheduler to run:

```bash
CONTROL_OWNER_DATABASE_URL="..." \
DOMAIN_OWNER_DATABASE_URL="..." \
BACKUP_LABEL="daily" \
  bash deploy/release/backup.sh
```

### Recommended schedule

| Schedule | Label | Retention |
|----------|-------|-----------|
| Daily at 03:00 UTC | `daily` | 30 days |
| Weekly Sunday 03:00 UTC | `weekly` | 30 days |

### Retention

`BACKUP_RETENTION_DAYS` (default 30) controls automatic cleanup of old `.dump`
and `.meta.json` files. Set to `0` to disable cleanup (manage externally).

## Restore from Logical Dump

```bash
# 1. Create a fresh database (or use an existing empty one)
createdb -U postgres marketai_domain_restored

# 2. Restore
pg_restore --dbname=marketai_domain_restored \
  --no-owner --no-privileges \
  marketai_domain_20260922T143000Z_abc1234.dump

# 3. Verify
psql -d marketai_domain_restored -c "SELECT version_num FROM alembic_version"

# 4. If restoring to production: re-converge roles
SUPERUSER_DATABASE_URL="..." \
  bash deploy/postgres/apply-domain-roles.sh
```

## Backup Verification

Run the restore rehearsal monthly to prove backup integrity:

```bash
CONTROL_OWNER_DATABASE_URL="..." \
DOMAIN_OWNER_DATABASE_URL="..." \
SUPERUSER_DATABASE_URL="..." \
  bash deploy/release/restore-rehearsal.sh
```

The rehearsal verifies:
- Database identity and schema version
- Row count equality for every table
- Money column totals (cost_ledger, ad_daily_metrics)
- Critical table content hashes

## Customer Data Deletion

Online customer data is removed within 30 days of an approved deletion request.
Backup retention is 30 days, which means:

- Backups taken before a deletion request may contain the deleted data
- After 30 days, those backups are automatically removed by retention cleanup
- A restore from a backup older than the deletion request may reintroduce
  deleted data — verify against the deletion log before serving traffic

## Audit Evidence Retention

Audit evidence is retained for one year. This is longer than backup retention.
Audit tables should use append-only patterns and are not subject to the same
deletion timeline as customer data.
