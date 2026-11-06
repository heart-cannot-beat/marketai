#!/usr/bin/env node
/**
 * Run Prisma migrations as the schema-owner role.
 *
 * `DATABASE_URL` holds the runtime role (`marketai_control_app`), which has
 * DML but no DDL privileges by design — pointing `prisma migrate` at it fails
 * with a permission error. Migrations therefore read `MIGRATION_DATABASE_URL`
 * (`marketai_control_owner`) and never fall back to the runtime role, so a
 * missing migration identity is a loud failure rather than a silent attempt
 * with the wrong privileges.
 *
 *   node scripts/prisma-migrate.mjs deploy|dev|status|resolve [...args]
 */
import { spawnSync } from 'child_process';

const args = process.argv.slice(2);

if (args.length === 0) {
  process.stderr.write(
    'usage: node scripts/prisma-migrate.mjs <deploy|dev|status|resolve> [...args]\n'
  );
  process.exit(2);
}

const migrationUrl = process.env.MIGRATION_DATABASE_URL;

if (!migrationUrl) {
  process.stderr.write(
    'MIGRATION_DATABASE_URL is not set.\n\n' +
      'Prisma migrations must run as the control-plane schema owner\n' +
      '(marketai_control_owner), not as the runtime role in DATABASE_URL\n' +
      '(marketai_control_app), which has no DDL privileges.\n\n' +
      'Provision roles with deploy/postgres/control-plane-domain-roles.sql and\n' +
      'set MIGRATION_DATABASE_URL. See web/.env.migrate.example.\n'
  );
  process.exit(1);
}

const result = spawnSync(
  process.platform === 'win32' ? 'prisma.cmd' : 'prisma',
  ['migrate', ...args],
  {
    stdio: 'inherit',
    env: { ...process.env, DATABASE_URL: migrationUrl },
  }
);

if (result.error) {
  process.stderr.write(`${result.error.message}\n`);
  process.exit(1);
}

process.exit(result.status ?? 1);
