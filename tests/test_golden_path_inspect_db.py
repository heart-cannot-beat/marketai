"""Regression tests for the container golden-path database inspector."""

from __future__ import annotations

import importlib.util
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.meta.models import MetaConnectionRecord
from app.platform.tenancy import TenantContext, tenant_context_signature


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "web"
    / "e2e"
    / "golden-path"
    / "scripts"
    / "inspect_db.py"
)
SPEC = importlib.util.spec_from_file_location("golden_path_inspect_db", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
inspect_db = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inspect_db)


TENANT_CONTEXT_SECRET = "test-tenant-context-secret-at-least-32-bytes"


class _Cursor:
    def __init__(self, row: tuple[str] | None) -> None:
        self._row = row

    def fetchone(self) -> tuple[str] | None:
        return self._row


class _Connection:
    def __init__(self) -> None:
        self.closed = False
        self.in_transaction = False
        self.context_params: tuple[Any, ...] | None = None

    @contextmanager
    def transaction(self):
        self.in_transaction = True
        try:
            yield
        finally:
            self.in_transaction = False

    def execute(self, query: str, params: tuple[Any, ...]) -> _Cursor:
        if "set_config('marketai.workspace_id'" in query:
            assert self.in_transaction
            self.context_params = params
            return _Cursor(None)

        expected_relation = f"FROM {MetaConnectionRecord.__tablename__} "
        if expected_relation not in query:
            relation = query.split(" FROM ", 1)[1].split(" ", 1)[0]
            raise RuntimeError(f'relation "{relation}" does not exist')
        assert params == ("workspace-1",)
        if not self.in_transaction or self.context_params is None:
            return _Cursor(None)
        return _Cursor(("customer_sandbox",))

    def close(self) -> None:
        self.closed = True


class _BusinessConnection:
    def __init__(self, *, missing_relation: str | None = None) -> None:
        self.closed = False
        self.in_transaction = False
        self.context_params: tuple[Any, ...] | None = None
        self.missing_relation = missing_relation

    @contextmanager
    def transaction(self):
        self.in_transaction = True
        try:
            yield
        finally:
            self.in_transaction = False

    def execute(self, query: str, params: tuple[Any, ...]) -> _Cursor:
        if "set_config('marketai.workspace_id'" in query:
            assert self.in_transaction
            self.context_params = params
            return _Cursor(None)

        assert self.in_transaction
        assert self.context_params is not None
        assert params == ("workspace-1",)
        relation = query.split(" FROM ", 1)[1].split(" ", 1)[0]
        if relation == self.missing_relation:
            raise RuntimeError(f'relation "{relation}" does not exist')
        return _Cursor((0,))

    def close(self) -> None:
        self.closed = True


def test_workspace_meta_provenance_queries_the_migrated_connection_table(
    monkeypatch: Any,
) -> None:
    connection = _Connection()
    monkeypatch.setattr(inspect_db, "get_connection", lambda _: connection)
    monkeypatch.setenv("MARKETAI_TENANT_CONTEXT_SECRET", TENANT_CONTEXT_SECRET)

    result = inspect_db.assert_workspace_meta_provenance(
        {
            "database_url": "postgresql://unused",
            "workspace_ref": "workspace-1",
            "expected_mode": "customer_sandbox",
        }
    )

    assert result == {"ok": True, "data": {"meta_mode": "customer_sandbox"}}
    expected_context = TenantContext(
        workspace_id="workspace-1",
        role="system",
        executor="system:golden-path-inspector",
        authorization_epoch=0,
        meta_mode="customer_sandbox",
    )
    assert connection.context_params == (
        "workspace-1",
        "0",
        "customer_sandbox",
        tenant_context_signature(expected_context, TENANT_CONTEXT_SECRET),
    )
    assert connection.closed


def test_zero_business_entities_uses_current_tables_and_signed_tenant_context(
    monkeypatch: Any,
) -> None:
    connection = _BusinessConnection()
    monkeypatch.setattr(inspect_db, "get_connection", lambda _: connection)
    monkeypatch.setenv("MARKETAI_TENANT_CONTEXT_SECRET", TENANT_CONTEXT_SECRET)

    result = inspect_db.assert_zero_business_entities(
        {
            "database_url": "postgresql://unused",
            "workspace_ref": "workspace-1",
        }
    )

    assert result == {
        "ok": True,
        "data": {table: 0 for table in inspect_db.BUSINESS_TABLES},
    }
    assert inspect_db.BUSINESS_TABLES == [
        "library_entries",
        "library_versions",
        "generation_runs",
        "assets",
        "asset_versions",
        "experiments",
        "localization_batches",
        "scale_enrollments",
        "scale_advice_threads",
    ]
    expected_context = TenantContext(
        workspace_id="workspace-1",
        role="system",
        executor="system:golden-path-inspector",
        authorization_epoch=0,
    )
    assert connection.context_params == (
        "workspace-1",
        "0",
        "",
        tenant_context_signature(expected_context, TENANT_CONTEXT_SECRET),
    )
    assert connection.closed


def test_zero_business_entities_fails_closed_when_a_relation_is_missing(
    monkeypatch: Any,
) -> None:
    connection = _BusinessConnection(missing_relation="generation_runs")
    monkeypatch.setattr(inspect_db, "get_connection", lambda _: connection)
    monkeypatch.setenv("MARKETAI_TENANT_CONTEXT_SECRET", TENANT_CONTEXT_SECRET)

    result = inspect_db.assert_zero_business_entities(
        {
            "database_url": "postgresql://unused",
            "workspace_ref": "workspace-1",
        }
    )

    assert result == {
        "ok": False,
        "error": 'relation "generation_runs" does not exist',
    }
    assert connection.closed
