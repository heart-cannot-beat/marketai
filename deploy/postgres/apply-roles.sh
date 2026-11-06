#!/usr/bin/env bash
#
# Provision the whole MarketAI PostgreSQL role topology.
#
# This is the one supported entry point (`make db-roles`). It applies both
# layers, because applying only the first leaves a cluster that looks correct
# and cannot serve a request: the control-plane/domain boundary exists, but the
# roles FastAPI and the Worker actually connect as do not.
#
#   control-plane-domain-roles.sql   the two databases and the boundary between
#                                    them (control owner/app, domain owner/app)
#   domain-tenant-roles.sql          least-privilege runtime roles inside the
#                                    domain database (api, worker, readonly)
#   cross-database-denials.sql       the statements that name both databases,
#                                    which is why they are not in either one's
#                                    release path
#
# This is bootstrap. Because it spans both deployment units it needs the
# control-plane passwords too, which is exactly why a routine domain release
# must not run it: rolling the domain schema should not re-set
# `marketai_control_owner`'s password. `make db-release` converges only the
# domain half, through `apply-domain-roles.sh`.
#
# Both files run in a single psql process against the connection the caller
# supplied, and each switches database with `\connect -reuse-previous=on`.
# That is deliberate: this script must never take a connection string apart.
# Deriving a per-database URL by string surgery
# (`"${SUPERUSER_DATABASE_URL%/*}/marketai_domain"`) silently dropped a
# `?sslmode=require` query string, rewrote the tail of a parameter value that
# contained a slash (`sslrootcert=/etc/ca.pem`), and destroyed a URL with no
# path at all -- and this script transmits three role passwords, so a silent
# TLS downgrade here is a credential disclosure. `-reuse-previous=on` carries
# every parameter of the original connection, `sslmode` included, because it
# reuses the connection rather than reconstructing it.
#
# Requires a superuser connection in PGHOST/PGPORT/PGUSER/PGPASSWORD (or
# SUPERUSER_DATABASE_URL), the role passwords and the 32+ byte tenant-context
# HMAC secret in the environment. Secrets are never written to disk by this
# script.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

: "${CONTROL_OWNER_PASSWORD:?CONTROL_OWNER_PASSWORD is required}"
: "${CONTROL_APP_PASSWORD:?CONTROL_APP_PASSWORD is required}"
: "${DOMAIN_OWNER_PASSWORD:?DOMAIN_OWNER_PASSWORD is required}"
: "${DOMAIN_APP_PASSWORD:?DOMAIN_APP_PASSWORD is required}"
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
  # Passed through verbatim. Never rewritten, never split on '/'.
  psql_args+=(-d "$SUPERUSER_DATABASE_URL")
fi

psql "${psql_args[@]}" \
  -v control_owner_password="$CONTROL_OWNER_PASSWORD" \
  -v control_app_password="$CONTROL_APP_PASSWORD" \
  -v domain_owner_password="$DOMAIN_OWNER_PASSWORD" \
  -v domain_app_password="$DOMAIN_APP_PASSWORD" \
  -v api_password="$DOMAIN_API_PASSWORD" \
  -v worker_password="$DOMAIN_WORKER_PASSWORD" \
  -v readonly_password="$DOMAIN_READONLY_PASSWORD" \
  -v tenant_context_secret="$MARKETAI_TENANT_CONTEXT_SECRET" \
  -v release_nonce="$MARKETAI_RELEASE_NONCE" \
  -f "$here/control-plane-domain-roles.sql" \
  -f "$here/domain-tenant-roles.sql" \
  -f "$here/cross-database-denials.sql"

echo "control-plane, domain and domain runtime roles provisioned"
