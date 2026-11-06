"""Converge PostgreSQL schema details with the Scale ORM contract.

Revision ID: ad1_check_names
Revises: ac1_enrollment_lineage

The Localization migration installed six widened PostgreSQL-only checks with
their short logical names.  ``Base.metadata`` applies the repository naming
convention to those same logical names, so Alembic subsequently reported each
physical constraint as removed and added even though its predicate matched.

Renaming preserves the already-validated predicates and makes fresh installs
and upgraded databases converge without rewriting either table. The same
revision widens the two ``attribution_setting`` columns from 60 to 64
characters: they store a SHA-256 attribution-semantics hash, which a real
PostgreSQL ``varchar(60)`` rejected even though SQLite accepted it.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "ad1_check_names"
down_revision = "ac1_enrollment_lineage"
branch_labels = None
depends_on = None


_CHECK_NAME_PAIRS = (
    (
        "asset_versions",
        "ck_asset_version_origin_kind",
        "ck_asset_versions_ck_asset_version_origin_kind",
    ),
    (
        "asset_versions",
        "ck_av_localized_requires_truth_parent",
        "ck_asset_versions_ck_av_localized_requires_truth_parent",
    ),
    (
        "provider_call_attempts",
        "ck_provider_call_attempt_step_kind",
        "ck_provider_call_attempts_ck_provider_call_attempt_step_kind",
    ),
    (
        "provider_call_attempts",
        "chargeability",
        "ck_provider_call_attempts_chargeability",
    ),
    (
        "provider_call_attempts",
        "chargeability_consistency",
        "ck_provider_call_attempts_chargeability_consistency",
    ),
    (
        "provider_call_attempts",
        "truth_required",
        "ck_provider_call_attempts_truth_required",
    ),
)


def _rename_check(table_name: str, source_name: str, target_name: str) -> None:
    existing = {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_check_constraints(table_name)
    }
    if target_name in existing:
        if source_name in existing:
            raise RuntimeError(
                f"both {source_name!r} and {target_name!r} exist on {table_name!r}"
            )
        return
    if source_name not in existing:
        raise RuntimeError(
            f"neither {source_name!r} nor {target_name!r} exists on {table_name!r}"
        )

    op.execute(
        sa.text(
            f'ALTER TABLE "{table_name}" '
            f'RENAME CONSTRAINT "{source_name}" TO "{target_name}"'
        )
    )


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table_name, source_name, target_name in _CHECK_NAME_PAIRS:
            _rename_check(table_name, source_name, target_name)
    for table_name in ("scale_delivery_config_sources", "scale_delivery_targets"):
        with op.batch_alter_table(table_name, schema=None) as batch_op:
            batch_op.alter_column(
                "attribution_setting",
                existing_type=sa.String(60),
                type_=sa.String(64),
                existing_nullable=False,
            )


def downgrade() -> None:
    for table_name in reversed(
        ("scale_delivery_config_sources", "scale_delivery_targets")
    ):
        with op.batch_alter_table(table_name, schema=None) as batch_op:
            batch_op.alter_column(
                "attribution_setting",
                existing_type=sa.String(64),
                type_=sa.String(60),
                existing_nullable=False,
            )
    if op.get_bind().dialect.name == "postgresql":
        for table_name, source_name, target_name in reversed(_CHECK_NAME_PAIRS):
            _rename_check(table_name, target_name, source_name)
