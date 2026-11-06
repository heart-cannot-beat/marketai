"""PostgreSQL portability checks for the #18 commercial-access migration."""
from __future__ import annotations

import ast
from pathlib import Path

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.adapters.persistence import models as _persistence_models  # noqa: F401
from app.adapters.persistence.database import Base
from app.entitlements import credentials as _credentials  # noqa: F401
from app.entitlements import models as _entitlement_models  # noqa: F401
from app.entitlements import usage_models as _usage_models  # noqa: F401
from app.entitlements.cost_models import (
    ProviderCallAttemptRecord,
    ProviderCostLedgerEntryRecord,
)


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    ROOT
    / "migrations"
    / "versions"
    / "k1a2b3c4d5e6_entitlement_commercial_access.py"
)


def test_provider_cost_tables_compile_for_postgresql() -> None:
    dialect = postgresql.dialect()
    for model in (ProviderCallAttemptRecord, ProviderCostLedgerEntryRecord):
        CreateTable(model.__table__).compile(dialect=dialect)


def test_explicit_migration_identifiers_fit_postgresql_limit() -> None:
    tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
    identifiers = [
        keyword.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "name"
        and isinstance(keyword.value, ast.Constant)
        and isinstance(keyword.value.value, str)
    ]
    oversized = [name for name in identifiers if len(name) > 63]
    assert oversized == []


def test_check_constraint_names_fit_postgresql_limit_after_naming_convention() -> None:
    oversized = [
        (table.name, constraint.name)
        for table in Base.metadata.sorted_tables
        for constraint in table.constraints
        if constraint.__class__.__name__ == "CheckConstraint"
        and constraint.name is not None
        and len(constraint.name) > 63
    ]
    assert oversized == []


def test_new_tables_use_the_authenticated_tenant_policy() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_install_rls_policies"
    )
    policy_source = ast.get_source_segment(source, function)

    assert policy_source is not None
    assert "CREATE POLICY tenant_isolation" in policy_source
    assert "marketai_context_is_current(workspace_id)" in policy_source
    assert "current_setting('marketai.workspace_id')" not in policy_source
