"""Database engine and Unit-of-Work primitives.

PostgreSQL is the hosted runtime and the only one that enforces tenant
isolation: Row Level Security reads the transaction-local settings written by
``TenantContext``.  SQLite remains a bounded local and import compatibility
path -- it has no RLS, so on SQLite the same context is carried but the
enforcement is the repository layer's alone.

Module code depends on sessions and repositories rather than on dialect
specific SQL, so which engine is in use changes who enforces isolation, not how
callers are written.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import MetaData, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy import create_engine

from ... import config
from ...platform.tenancy import TenantContext, TenantScopeError, apply_to_session


NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def is_postgres_url(url: str) -> bool:
    return url.startswith("postgresql")


def _engine_kwargs(url: str) -> dict:
    if url.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}}

    return {
        "pool_pre_ping": True,
        # Roll back whatever the previous checkout left open before the
        # connection is reused.  Combined with ``SET LOCAL`` this is what stops
        # one Workspace's tenant settings reaching the next request that
        # happens to be handed the same connection.
        "pool_reset_on_return": "rollback",
        # Names the connection in pg_stat_activity so a tenant-scoped
        # transaction can be attributed while it is open. Statement and lock
        # timeouts are not set here yet -- see the follow-up landing points on
        # #16 -- so a blocking statement still holds its transaction open.
        "connect_args": {
            "application_name": "marketai-domain",
        },
    }


engine = create_engine(config.DATABASE_URL, future=True, **_engine_kwargs(config.DATABASE_URL))
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, future=True)


@event.listens_for(Engine, "connect")
def _configure_sqlite(dbapi_connection, connection_record) -> None:  # pragma: no cover - dialect hook
    del connection_record
    module_name = type(dbapi_connection).__module__
    if not module_name.startswith("sqlite3"):
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=FULL")
    cursor.execute(f"PRAGMA busy_timeout={config.SQLITE_BUSY_TIMEOUT_MS}")
    cursor.close()


@contextmanager
def platform_session() -> Iterator[Session]:
    """Small Unit of Work: commit on success and roll back on failure.

    Untenanted.  Reserved for schema, import and platform-global work; domain
    reads and writes go through :func:`tenant_session`, which additionally binds
    the Workspace that RLS enforces.
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def tenant_session(context: TenantContext) -> Iterator[Session]:
    """Unit of Work scoped to one Workspace and one pair of actors.

    The tenant settings are written with ``SET LOCAL`` *inside* this
    transaction, so they are discarded when it ends.  There is deliberately no
    way to open a domain session without naming the Workspace: on PostgreSQL an
    unset ``marketai.workspace_id`` makes every RLS policy fail closed, and in
    the repository layer :func:`current_context` raises.
    """
    session = SessionLocal()
    try:
        # Opening the transaction before applying the context matters: SET
        # LOCAL outside a transaction block is silently scoped to the statement.
        session.begin()
        apply_to_session(session, context)
        if session.get_bind().dialect.name == "sqlite" and inspect(
            session.get_bind()
        ).has_table("workspaces"):
            actual_epoch = session.scalar(
                text("SELECT authorization_epoch FROM workspaces WHERE id = :workspace_id"),
                {"workspace_id": context.workspace_id},
            )
            if actual_epoch is not None and int(actual_epoch) != context.authorization_epoch:
                raise TenantScopeError("the TenantContext authorization epoch is stale")

        yield session

        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def database_ready() -> None:
    """Prove that the configured database is reachable without reading a tenant."""
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))


def assert_runtime_role(expected_role: str) -> None:
    """Fail startup when a hosted process is connected with the wrong identity.

    SQLite is the explicitly bounded local/import compatibility path and has no
    database roles.  PostgreSQL deployments, however, must not be able to start
    the API with Worker credentials (or vice versa), even if both roles happen
    to have enough grants for the first request to succeed.
    """
    if engine.dialect.name != "postgresql":
        return

    with engine.connect() as connection:
        actual = str(connection.scalar(text("SELECT current_user")) or "")
    if actual != expected_role:
        raise RuntimeError(
            f"database role mismatch: expected {expected_role!r}, connected as {actual!r}"
        )


def assert_local_identity_boundary() -> None:
    """Fail startup, not just the first request, when local identity is
    misconfigured for PostgreSQL.

    ``MARKETAI_LOCAL_IDENTITY_ENABLED`` bypasses delegated JWT verification
    with a fixed local Workspace and initiator (see ``http_identity.py``).
    That is only safe for the bounded SQLite development path. PostgreSQL is
    the hosted, potentially multi-tenant runtime; starting it with local
    identity enabled would silently disable the delegated identity boundary
    for every request rather than failing loudly. The dialect name is
    checked, not a URL prefix, so this also catches
    ``postgresql+psycopg://``, ``postgresql+asyncpg://``, and similar driver
    variants.
    """
    if config.LOCAL_IDENTITY_ENABLED and engine.dialect.name == "postgresql":
        raise RuntimeError(
            "MARKETAI_LOCAL_IDENTITY_ENABLED is not permitted when the "
            "configured database is PostgreSQL; local identity is a bounded "
            "SQLite development path only"
        )
