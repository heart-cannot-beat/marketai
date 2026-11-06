"""Read-only SQLite snapshot migration with PostgreSQL content verification."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, func, select, text

from .database import Base
from . import models  # noqa: F401 - register every mapped table
from app.entitlements import credentials as _credentials  # noqa: F401
from app.entitlements import models as _entitlement_models  # noqa: F401
from app.entitlements import usage_models as _usage_models  # noqa: F401


class MigrationValidationError(RuntimeError):
    """The source, target or post-copy evidence did not satisfy the contract."""


_PLATFORM_MANAGED_TABLES = frozenset(
    {
        "market_policies",
        "market_policy_versions",
        "market_policy_version_events",
    }
)


def _migration_tables():
    """Return tenant-data tables, excluding target-managed platform catalogs."""
    return tuple(
        table
        for table in Base.metadata.sorted_tables
        if table.name not in _PLATFORM_MANAGED_TABLES
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    stat = path.stat()
    return {
        "sha256": _file_sha256(path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _source_files_state(path: Path) -> dict[str, dict[str, Any] | None]:
    # WAL is durable source content; SHM is transient coordination state and may
    # legitimately change when a read-only connection acquires a lock.
    return {
        "database": _file_state(path),
        "wal": _file_state(Path(f"{path}-wal")),
    }


def _sqlite_revision(path: Path) -> str | None:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        row = connection.execute(
            "SELECT version_num FROM alembic_version LIMIT 1"
        ).fetchone()
    return str(row[0]) if row else None


def _snapshot_sqlite(source: Path, destination: Path) -> None:
    uri = f"file:{source.resolve().as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as source_connection:
        with closing(sqlite3.connect(destination)) as destination_connection:
            source_connection.backup(destination_connection)


def _upgrade_staging_copy(path: Path) -> None:
    environment = dict(os.environ)
    environment["MARKETAI_DATABASE_URL"] = f"sqlite:///{path.as_posix()}"
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env=environment,
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise MigrationValidationError(f"staging SQLite upgrade failed: {detail}")


def _head_revision() -> str:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    return str(ScriptDirectory.from_config(config).get_current_head())


def _normalise(value: Any) -> Any:
    if isinstance(value, Decimal):
        if value == 0:
            return "0"
        return format(value.normalize(), "f")
    if isinstance(value, datetime):
        aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
        return aware.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {str(key): _normalise(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_normalise(item) for item in value]
    return value


def _primary_key(table, row: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        json.dumps(_normalise(row[column.name]), ensure_ascii=False, sort_keys=True)
        for column in table.primary_key.columns
    )


def _rows_hash(table, rows: Iterable[dict[str, Any]]) -> str:
    payload = [
        {column.name: _normalise(row[column.name]) for column in table.columns}
        for row in sorted(rows, key=lambda item: _primary_key(table, item))
    ]
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


_MONEY_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "generation_runs": (("est_cost", "currency"),),
    "cost_ledger": (("amount", "currency"),),
    "ad_daily_metrics": (("spend", "currency"), ("purchase_value", "currency")),
}


def _money_totals(
    table, rows: Iterable[dict[str, Any]]
) -> dict[str, dict[str, str]]:
    """Sum only monetary columns, keeping unlike currencies separate."""
    materialized = list(rows)
    totals: dict[str, dict[str, str]] = {}
    for amount_column, currency_column in _MONEY_FIELDS.get(table.name, ()):
        by_currency: dict[str, Decimal] = {}
        for row in materialized:
            amount = row.get(amount_column)
            if amount is None:
                continue
            currency = str(row.get(currency_column) or "").upper()
            by_currency[currency] = by_currency.get(currency, Decimal("0")) + Decimal(
                str(amount)
            )
        totals[amount_column] = {
            currency: str(_normalise(total))
            for currency, total in sorted(by_currency.items())
        }
    return totals


def _self_reference_constraints(
    table,
) -> list[tuple[tuple[str, ...], tuple[str, ...]]]:
    """Return each self-FK as one dependency, including composite keys."""
    references: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    for constraint in table.foreign_key_constraints:
        elements = tuple(constraint.elements)
        if elements and all(element.column.table is table for element in elements):
            references.append(
                (
                    tuple(element.parent.name for element in elements),
                    tuple(element.column.name for element in elements),
                )
            )
    return sorted(references)


def _reference_key(row: dict[str, Any], columns: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        json.dumps(_normalise(row[column]), ensure_ascii=False, sort_keys=True)
        for column in columns
    )


def _reference_is_ready(
    row: dict[str, Any],
    reference: tuple[tuple[str, ...], tuple[str, ...]],
    inserted_keys: set[tuple[str, ...]],
) -> bool:
    local_columns, _remote_columns = reference
    return any(row[column] is None for column in local_columns) or _reference_key(
        row, local_columns
    ) in inserted_keys


def _insert_rows(connection, table, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    references = _self_reference_constraints(table)
    if not references:
        connection.execute(table.insert(), rows)
        return

    pending = list(rows)
    inserted: dict[
        tuple[tuple[str, ...], tuple[str, ...]], set[tuple[str, ...]]
    ] = {reference: set() for reference in references}
    while pending:
        ready: list[dict[str, Any]] = []
        for row in pending:
            if all(
                _reference_is_ready(row, reference, inserted[reference])
                for reference in references
            ):
                ready.append(row)
        if not ready:
            raise MigrationValidationError(
                f"{table.name} contains an unresolved self-reference cycle"
            )
        connection.execute(table.insert(), ready)
        for row in ready:
            for reference in references:
                _local_columns, remote_columns = reference
                inserted[reference].add(_reference_key(row, remote_columns))
        ready_ids = {id(row) for row in ready}
        pending = [row for row in pending if id(row) not in ready_ids]


def migrate_sqlite_to_postgres(source_path: Path, target_url: str) -> dict[str, Any]:
    """Copy one immutable SQLite snapshot and prove target equality before commit."""
    source = source_path.resolve()
    if not source.is_file():
        raise MigrationValidationError(f"SQLite source does not exist: {source}")
    if not target_url.startswith("postgresql"):
        raise MigrationValidationError("the migration target must be PostgreSQL")

    # Read the revision first. A database whose persistent journal mode is WAL
    # may briefly create an empty sidecar when the first reader connects; that
    # is connection bookkeeping, not a source mutation.
    source_revision = _sqlite_revision(source)
    source_files_before = _source_files_state(source)
    database_before = source_files_before["database"]
    assert database_before is not None
    source_before = {
        **database_before,
        "revision": source_revision,
        "files": source_files_before,
    }
    head = _head_revision()

    descriptor, staging_name = tempfile.mkstemp(prefix="marketai-sqlite-stage-", suffix=".db")
    os.close(descriptor)
    staging = Path(staging_name)
    try:
        _snapshot_sqlite(source, staging)
        _upgrade_staging_copy(staging)
        staging_revision = _sqlite_revision(staging)
        if staging_revision != head:
            raise MigrationValidationError(
                f"staging revision is {staging_revision!r}, expected {head!r}"
            )

        source_engine = create_engine(f"sqlite:///{staging.as_posix()}", future=True)
        target_engine = create_engine(target_url, future=True, pool_pre_ping=True)
        try:
            migration_tables = _migration_tables()
            with source_engine.connect() as source_connection:
                source_rows = {
                    table.name: [dict(row) for row in source_connection.execute(select(table)).mappings()]
                    for table in migration_tables
                }

            with target_engine.begin() as target_connection:
                identity = target_connection.execute(
                    text("SELECT current_database(), current_user")
                ).one()
                if identity[1] != "marketai_domain_owner":
                    raise MigrationValidationError(
                        "target must be reached as marketai_domain_owner; "
                        f"connected as {identity[1]!r}"
                    )
                target_revision = target_connection.scalar(
                    text("SELECT version_num FROM alembic_version LIMIT 1")
                )
                if target_revision != head:
                    raise MigrationValidationError(
                        f"target revision is {target_revision!r}, expected {head!r}"
                    )

                nonempty = {
                    table.name: int(target_connection.scalar(select(func.count()).select_from(table)) or 0)
                    for table in migration_tables
                }
                occupied = {name: count for name, count in nonempty.items() if count}
                if occupied:
                    raise MigrationValidationError(
                        f"target is not empty; refusing to merge datasets: {occupied}"
                    )

                # Rows grandfathered by the #24 upgrade carry the reserved
                # v1-bootstrap truth marker.  The live INSERT trigger correctly
                # rejects that marker, so the owner-only migration transaction
                # suspends just that trigger while copying the immutable
                # snapshot.  Both statements are transactional in PostgreSQL:
                # any copy failure rolls the suspension back with the rows.
                target_connection.execute(text(
                    "ALTER TABLE asset_versions DISABLE TRIGGER "
                    "trg_asset_versions_truth_write_once"
                ))
                for table in migration_tables:
                    _insert_rows(target_connection, table, source_rows[table.name])
                target_connection.execute(text(
                    "ALTER TABLE asset_versions ENABLE TRIGGER "
                    "trg_asset_versions_truth_write_once"
                ))

                tables: dict[str, Any] = {}
                for table in migration_tables:
                    before_rows = source_rows[table.name]
                    after_rows = [
                        dict(row)
                        for row in target_connection.execute(select(table)).mappings()
                    ]
                    source_hash = _rows_hash(table, before_rows)
                    target_hash = _rows_hash(table, after_rows)
                    source_money = _money_totals(table, before_rows)
                    target_money = _money_totals(table, after_rows)
                    verified = (
                        len(before_rows) == len(after_rows)
                        and source_hash == target_hash
                        and source_money == target_money
                    )
                    tables[table.name] = {
                        "source_count": len(before_rows),
                        "target_count": len(after_rows),
                        "source_hash": source_hash,
                        "target_hash": target_hash,
                        "source_money_totals": source_money,
                        "target_money_totals": target_money,
                        "verified": verified,
                    }
                    if not verified:
                        raise MigrationValidationError(
                            f"post-copy validation failed for {table.name}"
                        )

                # Check the immutable-source promise before the target transaction
                # is allowed to commit. A source mutation must roll the copy back.
                source_files_after = _source_files_state(source)
                unchanged = source_files_before == source_files_after
                if not unchanged:
                    raise MigrationValidationError(
                        "the source SQLite file changed during migration"
                    )

            return {
                "schema_version": 2,
                "ok": True,
                "source": {
                    "path": str(source),
                    **source_before,
                    "unchanged": unchanged,
                },
                "staging_revision": staging_revision,
                "target": {
                    "database": identity[0],
                    "role": identity[1],
                    "revision": target_revision,
                },
                "tables": tables,
                "summary": {
                    "tables_verified": len(tables),
                    "source_rows": sum(item["source_count"] for item in tables.values()),
                    "target_rows": sum(item["target_count"] for item in tables.values()),
                },
            }
        finally:
            target_engine.dispose()
            source_engine.dispose()
    finally:
        staging.unlink(missing_ok=True)


def write_report(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
