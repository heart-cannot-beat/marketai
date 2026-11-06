#!/usr/bin/env bash
# deploy/docker/init-domain.sh
# Bootstraps the full PostgreSQL topology and runs domain migrations.
# Used as the entrypoint for the init-domain compose service.
set -euo pipefail

echo "==> Bootstrapping role topology"
deploy/postgres/apply-roles.sh

echo "==> Running domain release (roles → verify → alembic → re-grant)"
deploy/postgres/release.sh

echo "==> Domain init complete"
