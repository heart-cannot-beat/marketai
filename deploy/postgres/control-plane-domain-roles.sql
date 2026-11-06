-- MarketAI PostgreSQL ownership boundary.
--
-- Two databases, four login roles, no cross-database CONNECT privilege:
--
--   marketai_control  owned by marketai_control_owner  (Prisma migrations)
--                     used   by marketai_control_app   (Next.js runtime)
--   marketai_domain   owned by marketai_domain_owner   (Alembic migrations)
--                     legacy marketai_domain_app login kept for upgrades only;
--                     API / Worker roles live in domain-tenant-roles.sql
--
-- The boundary is enforced at CONNECT, not at table grants: a control-plane
-- role cannot open a session against the domain database at all, and vice
-- versa. That is what `tests/test_database_role_isolation.py` and
-- `web/__tests__/integration/database-role-isolation.spec.ts` assert.
--
-- Run as a cluster superuser:
--
--   psql -v ON_ERROR_STOP=1 \
--        -v control_owner_password="$CONTROL_OWNER_PASSWORD" \
--        -v control_app_password="$CONTROL_APP_PASSWORD" \
--        -v domain_owner_password="$DOMAIN_OWNER_PASSWORD" \
--        -v domain_app_password="$DOMAIN_APP_PASSWORD" \
--        -f deploy/postgres/control-plane-domain-roles.sql
--
-- Passwords are supplied by the caller from a secret manager; none are stored
-- in this repository.
--
-- Every database switch below is `\connect -reuse-previous=on`. The flag is
-- stated rather than left to psql's default because it is what carries the
-- caller's `sslmode` (and every other connection parameter) into the new
-- connection. Reconstructing a per-database URL by string surgery instead --
-- which is what this file's caller used to do -- silently dropped the query
-- string, and this session transmits role passwords.

\set ON_ERROR_STOP on

-- ---------------------------------------------------------------- roles ----

SELECT 'CREATE ROLE marketai_control_owner LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'marketai_control_owner')
\gexec
ALTER ROLE marketai_control_owner LOGIN PASSWORD :'control_owner_password';

SELECT 'CREATE ROLE marketai_control_app LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'marketai_control_app')
\gexec
ALTER ROLE marketai_control_app LOGIN PASSWORD :'control_app_password';

SELECT 'CREATE ROLE marketai_domain_owner LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'marketai_domain_owner')
\gexec
ALTER ROLE marketai_domain_owner LOGIN PASSWORD :'domain_owner_password';

SELECT 'CREATE ROLE marketai_domain_app LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'marketai_domain_app')
\gexec
ALTER ROLE marketai_domain_app LOGIN PASSWORD :'domain_app_password';

-- No role inherits the other's grants.
REVOKE marketai_domain_owner FROM marketai_control_owner, marketai_control_app;
REVOKE marketai_domain_app FROM marketai_control_owner, marketai_control_app;
REVOKE marketai_control_owner FROM marketai_domain_owner, marketai_domain_app;
REVOKE marketai_control_app FROM marketai_domain_owner, marketai_domain_app;

-- ------------------------------------------------------------ databases ----

SELECT 'CREATE DATABASE marketai_control OWNER marketai_control_owner'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'marketai_control')
\gexec

SELECT 'CREATE DATABASE marketai_domain OWNER marketai_domain_owner'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'marketai_domain')
\gexec

-- PUBLIC gets CONNECT on every new database by default; take it away first so
-- that the explicit grants below are the only way in.
REVOKE ALL ON DATABASE marketai_control FROM PUBLIC;
REVOKE ALL ON DATABASE marketai_domain FROM PUBLIC;

GRANT CONNECT, TEMPORARY ON DATABASE marketai_control
  TO marketai_control_owner, marketai_control_app;
GRANT CONNECT, TEMPORARY ON DATABASE marketai_domain
  TO marketai_domain_owner, marketai_domain_app;

-- The denials that matter. Stated explicitly so that a future GRANT ... TO
-- PUBLIC or an inherited role does not silently reopen the boundary.
REVOKE ALL ON DATABASE marketai_domain
  FROM marketai_control_owner, marketai_control_app;
REVOKE ALL ON DATABASE marketai_control
  FROM marketai_domain_owner, marketai_domain_app;

-- ------------------------------------------- control-plane schema grants ----

\connect -reuse-previous=on marketai_control

REVOKE ALL ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO marketai_control_owner;

GRANT USAGE ON SCHEMA public TO marketai_control_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
  TO marketai_control_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO marketai_control_app;

-- Prisma migrations run as the owner, so future objects must be reachable by
-- the runtime role without another manual grant.
ALTER DEFAULT PRIVILEGES FOR ROLE marketai_control_owner IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO marketai_control_app;
ALTER DEFAULT PRIVILEGES FOR ROLE marketai_control_owner IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO marketai_control_app;

-- ------------------------------------------------- domain schema grants ----

\connect -reuse-previous=on marketai_domain

REVOKE ALL ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO marketai_domain_owner;

-- The former shared application role may still exist in deployed secret
-- stores, so convergence removes both existing-object and future-object
-- privileges. It keeps CONNECT only for upgrade diagnostics and cannot reach
-- the schema. Runtime access belongs exclusively to the API/Worker roles.
REVOKE ALL ON SCHEMA public FROM marketai_domain_app;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM marketai_domain_app;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM marketai_domain_app;
ALTER DEFAULT PRIVILEGES FOR ROLE marketai_domain_owner IN SCHEMA public
  REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM marketai_domain_app;
ALTER DEFAULT PRIVILEGES FOR ROLE marketai_domain_owner IN SCHEMA public
  REVOKE USAGE, SELECT ON SEQUENCES FROM marketai_domain_app;
