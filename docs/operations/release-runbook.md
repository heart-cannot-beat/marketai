# Release Runbook

## Overview

Every production change follows the same path: staging validation, explicit
approval, production promotion. The Release service (`deploy/release/release.sh`)
runs as a Railway one-off deploy command and owns all schema migrations.
Runtime services never receive migration credentials.

## Prerequisites

| Item | Where |
|------|-------|
| Release service deployed | Railway → Release service |
| Schema-owner credentials | Railway secrets (Release service only) |
| Runtime role passwords | Railway secrets (Release service only) |
| `SUPERUSER_DATABASE_URL` | Railway secrets (Release service only) |
| `MARKETAI_TENANT_CONTEXT_SECRET` | Shared with API/Worker |

## Staging Release

1. **Start the staging environment** in Railway (it is normally stopped).

2. **Trigger the Release service** against staging:
   ```
   RAILWAY_ENVIRONMENT=staging → Release service runs
   ```

3. The Release service executes in order:
   - Pre-flight: verifies database identity, connectivity, role names
   - Pre-migration backup: `pg_dump -Fc` of both databases
   - Control-plane migration: `prisma migrate deploy`
   - Domain migration: roles → nonce verify → `alembic upgrade head` → re-grant
   - Post-migration verification: no drift, no pending migrations

4. **Run the golden-path validation** against staging:
   ```
   # From CI or manually
   make golden-path-e2e
   ```

5. **Review evidence output** from the Release service logs. Confirm:
   - Both databases at expected versions
   - No migration failures
   - Golden-path tests pass

## Production Promotion

Production promotion requires explicit approval from the repository owner.

1. **Confirm staging passed**: golden-path tests, release evidence clean.

2. **Approve production release**: the repository owner approves the deployment
   in Railway or merges the release PR.

3. **The Release service runs against production** with the same sequence.
   `RELEASE_SKIP_BACKUP=true` is rejected in production — backup is mandatory.

4. **Verify production health**:
   - Check API health endpoint: `GET /api/health/ready`
   - Check Web health: `GET /api/auth/providers`
   - Review Release service exit code (must be 0)

## Rollback

### Application rollback (safe)

Redeploy the previous Railway deployment. Runtime services roll back; the
database stays at the new schema version. This is safe when:
- The new migrations only added columns, tables, or indexes
- No data was written in a new-format column

### Forward database repair

When the migration introduced a destructive change (dropped column, altered type)
and new-format data now exists, rolling back the application is not enough.
Write a corrective migration that restores compatibility. Never `alembic downgrade`
past data written in the new format.

### When to choose each path

| Scenario | Action |
|----------|--------|
| Bug in application code, schema is fine | Application rollback |
| Schema migration broke a query | Forward repair migration |
| New credential or financial evidence exists | Forward-only; see DR runbook |
| Data corruption | Disaster recovery (see DR runbook) |

## Release Ordering

The control-plane schema advances first. Domain services may depend on
control-plane tables (entitlement projections, workspace provisioning).
Rolling back is also cleaner when the domain migration has not yet run.

```
Control-plane (Prisma) → Domain (Alembic)
```

## Credential Isolation

| Credential | Available to |
|------------|-------------|
| `SUPERUSER_DATABASE_URL` | Release service only |
| `CONTROL_OWNER_DATABASE_URL` | Release service only |
| `DOMAIN_OWNER_DATABASE_URL` | Release service only |
| `MIGRATION_DATABASE_URL` | Release service only |
| `DOMAIN_API_PASSWORD` | Release service (for role convergence) |
| `DOMAIN_WORKER_PASSWORD` | Release service (for role convergence) |
| `DOMAIN_READONLY_PASSWORD` | Release service (for role convergence) |
| `DATABASE_URL` (control app) | Web service only |
| `MARKETAI_DATABASE_URL` (domain api) | API service only |
| `MARKETAI_DATABASE_URL` (domain worker) | Worker service only |

Runtime services receive only their own least-privilege role connection string.
