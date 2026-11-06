# Disaster Recovery Runbook

## Recovery Objectives

| Objective | Target | Mechanism |
|-----------|--------|-----------|
| RPO (Recovery Point Objective) | 15 minutes | Railway PITR + pre-release backups |
| RTO (Recovery Time Objective) | 4 hours | Documented procedure below |

## Recovery Decision Tree

```
Incident detected
  │
  ├── Application bug, database intact?
  │     → Application rollback (redeploy previous version)
  │     → RTO: minutes
  │
  ├── Schema migration broke queries?
  │     → Forward repair migration
  │     → RTO: 1-2 hours (write + test + deploy corrective migration)
  │
  ├── New-format credential or financial evidence exists?
  │     → Forward-only repair (never roll back past financial evidence)
  │     → RTO: 1-4 hours
  │
  ├── Data corruption (partial)?
  │     → PITR to sibling, selective repair
  │     → RTO: 1-2 hours
  │
  └── Full database loss?
        → PITR or logical restore to new plugin
        → RTO: 2-4 hours
```

## Procedure: PITR Recovery

Use when: data corruption, accidental deletion, or full loss within the last
15 minutes.

### Steps

1. **Assess the incident**: determine the latest safe point in time.
   Check application logs and the last known-good timestamp.

2. **Initiate PITR** in Railway dashboard:
   - PostgreSQL plugin → Backups → Restore to point in time
   - Select a timestamp before the incident (within 15-minute RPO)
   - Railway creates a new sibling plugin

3. **Verify the sibling** with the restore rehearsal:
   ```bash
   # Point the rehearsal at the sibling
   DOMAIN_OWNER_DATABASE_URL="<sibling-domain-url>" \
   CONTROL_OWNER_DATABASE_URL="<sibling-control-url>" \
   SUPERUSER_DATABASE_URL="<sibling-superuser-url>" \
     bash deploy/release/restore-rehearsal.sh
   ```

4. **Verify critical invariants**:
   - Row counts match expected (from pre-incident monitoring)
   - Money totals in `cost_ledger` are consistent
   - No customer data from after a pending deletion request was reintroduced
   - Alembic and Prisma versions match the deployed application

5. **Switch traffic**:
   - Update Railway service environment variables to point at the sibling
   - Restart API, Worker, Web services
   - Verify health endpoints

6. **Decommission the damaged database** after confirming the sibling is
   healthy for at least 1 hour under production traffic.

## Procedure: Logical Dump Recovery

Use when: PITR is unavailable (Railway outage, backup window exceeded) or
when restoring to a different Railway project.

### Steps

1. **Locate the most recent backup**:
   ```bash
   ls -lt $BACKUP_DIR/marketai_*.dump
   # Check .meta.json for migration versions
   ```

2. **Create fresh databases** in the target Railway project.

3. **Restore both databases**:
   ```bash
   pg_restore --dbname="$NEW_CONTROL_URL" \
     --no-owner --no-privileges \
     marketai_control_<timestamp>.dump

   pg_restore --dbname="$NEW_DOMAIN_URL" \
     --no-owner --no-privileges \
     marketai_domain_<timestamp>.dump
   ```

4. **Re-converge roles** (the dump does not include role definitions):
   ```bash
   SUPERUSER_DATABASE_URL="<new-superuser-url>" \
     bash deploy/postgres/apply-roles.sh
   ```

5. **Apply any migrations** between the backup and the current deployment:
   ```bash
   # Check gap
   psql --dbname="$NEW_DOMAIN_URL" -c "SELECT version_num FROM alembic_version"
   # If behind, run the Release service against the new databases
   ```

6. **Verify and switch** as in PITR recovery above.

## Procedure: Forward-Only Repair

Use when: the database contains new-format credential evidence, financial
records (cost_ledger entries), or other immutable evidence that must not be
lost or replayed.

### Constraints

- Never `alembic downgrade` past a migration that wrote financial evidence
- Never restore a backup from before the evidence was created
- The only safe path is a forward corrective migration

### Steps

1. Write a corrective Alembic migration that fixes the schema issue without
   losing the new evidence.

2. Test in staging first (mandatory).

3. Deploy via the normal release path (`deploy/release/release.sh`).

## Rollback Rules Summary

| Scenario | Allowed? | Method |
|----------|----------|--------|
| Application rollback, schema compatible | Yes | Redeploy previous version |
| Schema rollback, no new data | Yes | `alembic downgrade -1` + role re-convergence |
| Schema rollback, new-format data exists | No | Forward repair only |
| Schema rollback, financial evidence exists | No | Forward repair only |
| Full restore from backup | Yes | PITR or logical dump |
| Restore past a deletion request | Conditional | Verify deletion log first |

## Monthly Rehearsal

Run the restore rehearsal monthly to maintain operational readiness:

```bash
CONTROL_OWNER_DATABASE_URL="..." \
DOMAIN_OWNER_DATABASE_URL="..." \
SUPERUSER_DATABASE_URL="..." \
  bash deploy/release/restore-rehearsal.sh
```

Document the result in the team's incident log with:
- Date and operator
- Pass/fail count
- Any anomalies observed
- Time taken

## Escalation

| Severity | Response |
|----------|----------|
| Application bug | On-call engineer → application rollback |
| Schema issue | On-call + repository owner → forward repair |
| Data loss | Repository owner + infrastructure → PITR or logical restore |
| Railway platform outage | Logical restore to alternative host |

## Contact

Repository owner approval is required for:
- Production database restores
- Forward-only schema repairs
- Any recovery that may reintroduce deleted customer data
