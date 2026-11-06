#!/usr/bin/env bash
#
# The supported way to release the domain database. `make db-release`.
#
# Roles first, then migrations, and the ordering is checked rather than
# documented. It matters because the RLS migration makes the tenant boundary
# real, while the role file retires the former shared runtime identity and
# grants an explicit matrix to API and Worker. Running Alembic alone could
# therefore leave new tables inaccessible or an old BYPASSRLS exemption alive.
# Nothing in Alembic can detect that cluster state, so the check lives here.
#
#   1. apply-domain-roles.sh   converge the domain roles and the bridge state
#   2. verify the target       on the connection Alembic is about to use
#   3. alembic upgrade head    as the schema owner
#   4. apply-domain-roles.sh   grant the explicit runtime matrix to new tables
#
# Only the domain half is converged: this must not re-set control-plane
# credentials, which `apply-roles.sh` would. Bootstrap the topology once with
# `make db-roles`, then release with this.
#
# Step 2 runs over MARKETAI_DATABASE_URL rather than the superuser connection,
# and that is the point. The two are independent strings: converging roles over
# one and migrating over the other means a check that passes on staging while
# the migration lands on production. Checking the database name, the role and
# the bridge state is necessary but not sufficient -- two clusters prepared the
# same way answer all three identically -- so step 1 stamps the database with a
# nonce generated here, and step 2 refuses any database not carrying it.
#
# Environment: the three domain runtime passwords, MARKETAI_TENANT_CONTEXT_SECRET,
# a superuser connection for step 1 (only a superuser may change BYPASSRLS), and MARKETAI_DATABASE_URL
# pointing at the domain database as marketai_domain_owner. PYTHON and ALEMBIC
# may name a specific interpreter, including multi-word commands such as
# "python -m alembic".
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"

: "${MARKETAI_DATABASE_URL:?MARKETAI_DATABASE_URL is required and must be the domain database as marketai_domain_owner}"

case "$MARKETAI_DATABASE_URL" in
  postgres://*|postgresql://*|postgresql+*://*) ;;
  *)
    echo "release.sh: MARKETAI_DATABASE_URL is not a PostgreSQL URL; this is the" \
         "hosted release path, and SQLite needs neither roles nor RLS" >&2
    exit 1
    ;;
esac

# Generated here, not inside apply-domain-roles.sh, so the same value is
# stamped by step 1 and demanded by step 2.
MARKETAI_RELEASE_NONCE="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
export MARKETAI_RELEASE_NONCE

echo "==> 1/4 domain roles"
"$here/apply-domain-roles.sh"

echo "==> 2/4 release target"
cd "$repo"

# Word-split deliberately: these are commands, not paths, so a caller may pass
# "python -m alembic" to pin the interpreter (CI does).
read -r -a python_command <<< "${PYTHON:-python3}"
read -r -a alembic_command <<< "${ALEMBIC:-alembic}"

"${python_command[@]}" -m scripts.verify_release_target

echo "==> 3/4 migrations"
"${alembic_command[@]}" upgrade head

echo "==> 4/4 runtime grants"
"$here/apply-domain-roles.sh"

echo "domain release complete: roles converged, target verified, schema at head"
