# Forward / Rollback / Restore Procedure

## Domain database (Alembic)

### Forward (deploy)

The supported entry point is `make db-release`, which runs `deploy/postgres/release.sh`:

1. `apply-domain-roles.sh` — converge domain roles and stamp a release nonce
2. `verify_release_target` — confirm the migration target carries this release's nonce
3. `alembic upgrade head` — apply pending migrations as `marketai_domain_owner`
4. `apply-domain-roles.sh` — re-converge grants for any newly created tables

**Constraint:** Never run `alembic upgrade head` outside of `release.sh`. The role convergence and nonce verification are safety-critical.

### Rollback

```bash
# Roll back one migration (domain database only)
MARKETAI_DATABASE_URL=postgresql+psycopg://marketai_domain_owner:<password>@<host>:5432/marketai_domain \
  alembic downgrade -1

# Then re-converge roles (new tables may have been dropped)
./deploy/postgres/apply-domain-roles.sh
```

**Constraints:**
- RLS policies are created in migrations. Rolling back a migration that introduced RLS on a table removes that table's tenant isolation. Re-deploy promptly.
- Migrations that drop columns or alter types may not be reversible if the downgrade path was not implemented. Check the migration's `downgrade()` function before attempting.
- Always take a backup before rollback: `pg_dump -Fc marketai_domain > pre-rollback.dump`

### Restore

```bash
# Stop all services first
docker compose down

# Restore from custom-format dump
pg_restore -d marketai_domain --clean --if-exists pre-rollback.dump

# Re-converge roles and verify
make db-release
```

## Control-plane database (Prisma)

### Forward (deploy)

```bash
cd web && npm run db:migrate:deploy
# Equivalent to: npx prisma migrate deploy
```

### Rollback

Prisma does not support automated rollback. The procedure is:

1. Take a backup: `pg_dump -Fc marketai_control > pre-rollback-control.dump`
2. Write a corrective migration: `npx prisma migrate dev --name rollback_<description>`
3. Deploy: `npm run db:migrate:deploy`

### Restore

```bash
pg_restore -d marketai_control --clean --if-exists pre-rollback-control.dump
cd web && npm run db:migrate:deploy
```

## Evidence

The CI `domain-rls` job proves the forward path on every push:
- Bootstrap topology → release → re-release (idempotent)
- Cross-cluster safety (nonce prevents migrating the wrong cluster)
- No drift (`alembic current` + `alembic check`)
- Role convergence in both directions (BYPASSRLS bit)

The compose backup/restore rehearsal (`scripts/compose-migration-rehearsal.sh`) proves the restore path against a live stack.
