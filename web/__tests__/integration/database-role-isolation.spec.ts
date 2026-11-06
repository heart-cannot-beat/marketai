/**
 * Database-role negative test (Prisma half).
 *
 * Proves that Prisma cannot open a connection to the MarketAI domain database
 * with the control-plane role, and that the domain role cannot open a
 * connection to the control-plane database. The SQLAlchemy half lives in
 * `tests/test_database_role_isolation.py`.
 *
 * Requires a PostgreSQL cluster provisioned with
 * `deploy/postgres/control-plane-domain-roles.sql`. The suite skips when the
 * connection strings are absent so that `npm test` stays runnable on a clean
 * checkout without a database; CI supplies them.
 */
import { PrismaClient } from '@prisma/client';

const controlUrl = process.env.CONTROL_PLANE_DATABASE_URL;
const controlRoleOnDomainDb = process.env.CONTROL_ROLE_ON_DOMAIN_DB_URL;
const domainRoleOnControlDb = process.env.DOMAIN_ROLE_ON_CONTROL_DB_URL;

const configured = Boolean(
  controlUrl && controlRoleOnDomainDb && domainRoleOnControlDb
);

const client = (url: string) =>
  new PrismaClient({ datasources: { db: { url } } });

const describeIfConfigured = configured ? describe : describe.skip;

describeIfConfigured('PostgreSQL ownership boundary (Prisma)', () => {
  jest.setTimeout(30_000);

  it('connects to the control-plane database with the control-plane role', async () => {
    const prisma = client(controlUrl as string);

    try {
      await expect(prisma.$queryRaw`SELECT 1`).resolves.toBeDefined();
      // The control plane owns its own tables.
      await expect(prisma.team.count()).resolves.toBeGreaterThanOrEqual(0);
    } finally {
      await prisma.$disconnect();
    }
  });

  it('cannot run DDL with the runtime role, which is why migrations use the owner role', async () => {
    const prisma = client(controlUrl as string);

    try {
      await expect(
        prisma.$executeRawUnsafe(
          'CREATE TABLE "role_isolation_probe" ("id" TEXT NOT NULL)'
        )
      ).rejects.toThrow();
    } finally {
      await prisma.$disconnect();
    }
  });

  it('refuses an undefined filter instead of widening the query', async () => {
    // The backstop behind lib/requireSession.ts: without
    // `strictUndefinedChecks`, Prisma drops this filter and returns every row.
    const prisma = client(controlUrl as string);

    try {
      await expect(
        prisma.session.findMany({ where: { userId: undefined } })
      ).rejects.toThrow(/undefined/i);
    } finally {
      await prisma.$disconnect();
    }
  });

  it('cannot connect to the domain database with the control-plane role', async () => {
    const prisma = client(controlRoleOnDomainDb as string);

    try {
      await expect(prisma.$queryRaw`SELECT 1`).rejects.toThrow();
    } finally {
      await prisma.$disconnect().catch(() => undefined);
    }
  });

  it('cannot connect to the control-plane database with the domain role', async () => {
    const prisma = client(domainRoleOnControlDb as string);

    try {
      await expect(prisma.$queryRaw`SELECT 1`).rejects.toThrow();
    } finally {
      await prisma.$disconnect().catch(() => undefined);
    }
  });
});

if (!configured) {
  describe('PostgreSQL ownership boundary (Prisma)', () => {
    it('is skipped without CONTROL_PLANE_DATABASE_URL, CONTROL_ROLE_ON_DOMAIN_DB_URL and DOMAIN_ROLE_ON_CONTROL_DB_URL', () => {
      expect(configured).toBe(false);
    });
  });
}
