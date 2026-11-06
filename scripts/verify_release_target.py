"""Refuse to migrate a database that is not the one this release prepared.

``deploy/postgres/release.sh`` converges the roles over one connection and runs
Alembic over another: the superuser connection and ``MARKETAI_DATABASE_URL`` are
independent strings. Nothing stops them naming different clusters, and the
failure is silent and severe -- roles converge on staging, the check passes on
staging, and the RLS migration lands on a production cluster whose roles predate
this release. Every read from the application then comes back empty.

So the check runs on the migration target itself, over the connection Alembic
will use. What it asserts is what the migration assumes:

* it is carrying the nonce this run just stamped on it. Name, role and bridge
  state cannot separate two clusters prepared the same way -- a staging and a
  production database answer all three identically -- so identity has to come
  from a mark only this convergence could have left;
* it is the domain database, not the control-plane one and not a scratch copy;
* the connection holds the migration role, the only one with DDL;
* ``marketai_domain_app`` is in the retired state
  ``domain-tenant-roles.sql`` declares, so a persistent exemption from an older
  deployment cannot survive the release.

Exits non-zero with the mismatch named. Run through ``make db-release``.
"""

from __future__ import annotations

import os
import sys

from scripts.domain_bridge import (
    BRIDGED_ROLE,
    DOMAIN_DATABASE,
    MIGRATION_ROLE,
    declared_bridge_state,
    expected_rolbypassrls,
)

TARGET_URL_VAR = "MARKETAI_DATABASE_URL"
NONCE_VAR = "MARKETAI_RELEASE_NONCE"
NONCE_SETTING = "marketai.release_nonce"
NONCE_PREFIX = f"{NONCE_SETTING}="


def _sqlalchemy_url(url: str) -> str:
    """Select psycopg 3 when Railway supplies a portable PostgreSQL URL."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def main() -> int:
    url = os.environ.get(TARGET_URL_VAR, "").strip()

    if not url:
        print(f"{TARGET_URL_VAR} is required", file=sys.stderr)
        return 1

    nonce = os.environ.get(NONCE_VAR, "").strip()

    if not nonce:
        print(
            f"{NONCE_VAR} is required: it is what proves the database about to be"
            " migrated is the one whose roles were just converged. Run this"
            " through make db-release rather than on its own.",
            file=sys.stderr,
        )
        return 1

    from sqlalchemy import create_engine, text

    engine = create_engine(_sqlalchemy_url(url), future=True)

    try:
        with engine.connect() as connection:
            database, user, stamped = connection.execute(
                text(
                    "SELECT current_database(), current_user, ("
                    " SELECT substring(item.setting"
                    "                  FROM char_length(:nonce_prefix) + 1)"
                    " FROM pg_db_role_setting AS configured"
                    " JOIN pg_database AS configured_database"
                    "   ON configured_database.oid = configured.setdatabase"
                    " CROSS JOIN LATERAL unnest(configured.setconfig)"
                    "   AS item(setting)"
                    " WHERE configured_database.datname = current_database()"
                    "   AND configured.setrole = 0"
                    "   AND left(item.setting, char_length(:nonce_prefix))"
                    "       = :nonce_prefix"
                    ")"
                ),
                {"nonce_prefix": NONCE_PREFIX},
            ).one()

            bypassing = connection.execute(
                text(
                    "SELECT rolbypassrls FROM pg_roles WHERE rolname = :role"
                ),
                {"role": BRIDGED_ROLE},
            ).scalar()
    finally:
        engine.dispose()

    problems = []

    if stamped != nonce:
        problems.append(
            f"this database is not the one this release converged: it carries"
            f" {NONCE_SETTING}={stamped!r}, and this run stamped {nonce!r}."
            " The roles converged somewhere else -- check that"
            f" SUPERUSER_DATABASE_URL and {TARGET_URL_VAR} name the same cluster."
        )

    if database != DOMAIN_DATABASE:
        problems.append(
            f"{TARGET_URL_VAR} connects to {database!r}, not {DOMAIN_DATABASE!r}"
        )

    if user != MIGRATION_ROLE:
        problems.append(
            f"{TARGET_URL_VAR} connects as {user!r}, not {MIGRATION_ROLE!r};"
            " only the migration role may change the schema"
        )

    if bypassing is None:
        problems.append(
            f"{BRIDGED_ROLE} does not exist on this cluster: its roles were never"
            " provisioned, or this is not the cluster they were provisioned on"
        )
    elif bool(bypassing) != expected_rolbypassrls():
        problems.append(
            f"{BRIDGED_ROLE} has rolbypassrls={bool(bypassing)} on this cluster,"
            f" but deploy/postgres/domain-tenant-roles.sql declares"
            f" {declared_bridge_state()}. The roles converged somewhere else."
        )

    if problems:
        print(
            "release target check failed -- refusing to migrate:", file=sys.stderr
        )
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(
        f"release target ok: {database} as {user},"
        f" {BRIDGED_ROLE} {declared_bridge_state()}, stamped by this run"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
