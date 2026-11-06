-- Cross-database denials, applied once by the whole-topology bootstrap.
--
-- These statements name both databases, so they belong to bootstrap and not to
-- either deployment unit's release. `domain-tenant-roles.sql` runs on every
-- domain release and must stay inside `marketai_domain`: a release that
-- rewrote control-plane ACLs would change the other unit on every deploy, and
-- on a domain-only cluster -- where `marketai_control` does not exist -- it
-- would simply fail.
--
-- The runtime roles are created by `domain-tenant-roles.sql`, so this runs
-- after it. PUBLIC has already lost CONNECT on both databases; this is the
-- explicit half, stated so that a later GRANT ... TO PUBLIC cannot quietly
-- reopen the boundary.
--
-- Applied by `apply-roles.sh`. Requires a superuser connection.

\set ON_ERROR_STOP on

\connect -reuse-previous=on marketai_control

REVOKE ALL ON DATABASE marketai_control FROM
  marketai_domain_api, marketai_domain_worker, marketai_domain_readonly;
