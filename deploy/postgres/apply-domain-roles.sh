#!/usr/bin/env bash
#
# Converge the domain database's own roles. Nothing else.
#
# `apply-roles.sh` bootstraps the whole topology, control plane included, and
# needs the control-plane passwords to do it. A domain release must not: the
# two databases are separately owned and separately credentialed on purpose, so
# rolling the domain schema should never re-set `marketai_control_owner`'s
# password, and a domain deploy should never fail -- or worse, succeed with a
# stale secret -- because a control-plane credential was rotated somewhere else.
#
# Use `make db-roles` once, to create the databases and the four owner/app
# roles. Use this (via `make db-release`) every time after that.
#
# Requires a superuser connection -- only a superuser may change BYPASSRLS --
# in PGHOST/PGPORT/PGUSER/PGPASSWORD or SUPERUSER_DATABASE_URL, plus the three
# domain runtime passwords and the 32+ byte tenant-context HMAC secret shared
# with API/Worker. The connection string is passed to psql untouched;
# `domain-tenant-roles.sql` reaches the domain database with
# `\connect -reuse-previous=on`, which keeps sslmode and every other parameter.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

: "${DOMAIN_API_PASSWORD:?DOMAIN_API_PASSWORD is required}"
: "${DOMAIN_WORKER_PASSWORD:?DOMAIN_WORKER_PASSWORD is required}"
: "${DOMAIN_READONLY_PASSWORD:?DOMAIN_READONLY_PASSWORD is required}"
: "${MARKETAI_TENANT_CONTEXT_SECRET:?MARKETAI_TENANT_CONTEXT_SECRET is required}"
if (( ${#MARKETAI_TENANT_CONTEXT_SECRET} < 32 )); then
  echo "MARKETAI_TENANT_CONTEXT_SECRET must be at least 32 characters" >&2
  exit 1
fi

# One nonce per run, stamped onto the database the roles are converged on. The
# release then refuses to migrate any database that is not carrying it -- which
# is the only way to tell two identically prepared clusters apart.
: "${MARKETAI_RELEASE_NONCE:=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')}"
export MARKETAI_RELEASE_NONCE

psql_args=(-v ON_ERROR_STOP=1)
if [[ -n "${SUPERUSER_DATABASE_URL:-}" ]]; then
  psql_args+=(-d "$SUPERUSER_DATABASE_URL")
fi

psql "${psql_args[@]}" \
  -v api_password="$DOMAIN_API_PASSWORD" \
  -v worker_password="$DOMAIN_WORKER_PASSWORD" \
  -v readonly_password="$DOMAIN_READONLY_PASSWORD" \
  -v tenant_context_secret="$MARKETAI_TENANT_CONTEXT_SECRET" \
  -v release_nonce="$MARKETAI_RELEASE_NONCE" \
  -f "$here/domain-tenant-roles.sql"

echo "domain runtime roles converged"
