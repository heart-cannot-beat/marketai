"""Bind every direct tenant relationship to its Workspace.

Revision ID: f0a1b2c3d4e5
Revises: e9f0a1b2c3d4
"""
from __future__ import annotations

from collections import defaultdict

from alembic import op
import sqlalchemy as sa


revision = "f0a1b2c3d4e5"
down_revision = "e9f0a1b2c3d4"
branch_labels = None
depends_on = None


# child, column, parent, old constraint, new constraint, old ON DELETE,
# tenant-safe ON DELETE. The four formerly-SET-NULL relations become RESTRICT:
# a composite SET NULL would also null the non-null Workspace discriminator.
RELATIONS = (
    ("dimension_schema_versions", "product_id", "products", "fk_dimension_schema_versions_product_id_products", "fk_dimension_schema_versions_workspace_product_products", "CASCADE", "CASCADE"),
    ("experiments", "product_id", "products", "fk_experiments_product_id_products", "fk_experiments_workspace_product_products", "RESTRICT", "RESTRICT"),
    ("meta_sync_runs", "account_id", "meta_accounts", "fk_meta_sync_runs_account_id_meta_accounts", "fk_meta_sync_runs_workspace_account_meta_accounts", "CASCADE", "CASCADE"),
    ("meta_sync_schedules", "account_id", "meta_accounts", "fk_meta_sync_schedules_account_id_meta_accounts", "fk_meta_sync_schedules_workspace_account_meta_accounts", "CASCADE", "CASCADE"),
    ("plans", "product_id", "products", "fk_plans_product_id_products", "fk_plans_workspace_product_products", "CASCADE", "CASCADE"),
    ("plans", "schema_id", "dimension_schema_versions", "fk_plans_schema_id_dimension_schema_versions", "fk_plans_workspace_schema_dimension_schema_versions", "RESTRICT", "RESTRICT"),
    ("generation_runs", "product_id", "products", "fk_generation_runs_product_id_products", "fk_generation_runs_workspace_product_products", "RESTRICT", "RESTRICT"),
    ("generation_runs", "plan_id", "plans", "fk_generation_runs_plan_id_plans", "fk_generation_runs_workspace_plan_plans", "RESTRICT", "RESTRICT"),
    ("assets", "run_id", "generation_runs", "fk_assets_run_id_generation_runs", "fk_assets_workspace_run_generation_runs", "CASCADE", "CASCADE"),
    ("assets", "reused_from_asset_id", "assets", "fk_assets_reused_from_asset_id_assets", "fk_assets_workspace_reused_from_assets", "SET NULL", "RESTRICT"),
    ("cost_ledger", "job_id", "jobs", "fk_cost_ledger_job_id_jobs", "fk_cost_ledger_workspace_job_jobs", "SET NULL", "RESTRICT"),
    ("cost_ledger", "run_id", "generation_runs", "fk_cost_ledger_run_id_generation_runs", "fk_cost_ledger_workspace_run_generation_runs", "SET NULL", "RESTRICT"),
    ("asset_versions", "parent_version_id", "asset_versions", "fk_asset_versions_parent_version_id_asset_versions", "fk_asset_versions_workspace_parent_version_asset_versions", "SET NULL", "RESTRICT"),
    ("asset_versions", "asset_id", "assets", "fk_asset_versions_asset_id_assets", "fk_asset_versions_workspace_asset_assets", "CASCADE", "CASCADE"),
    ("quality_reviews", "asset_id", "assets", "fk_quality_reviews_asset_id_assets", "fk_quality_reviews_workspace_asset_assets", "CASCADE", "CASCADE"),
    ("quality_reviews", "product_id", "products", "fk_quality_reviews_product_id_products", "fk_quality_reviews_workspace_product_products", "CASCADE", "CASCADE"),
    ("verdict_decisions", "asset_id", "assets", "fk_verdict_decisions_asset_id_assets", "fk_verdict_decisions_workspace_asset_assets", "CASCADE", "CASCADE"),
    ("verdict_decisions", "product_id", "products", "fk_verdict_decisions_product_id_products", "fk_verdict_decisions_workspace_product_products", "CASCADE", "CASCADE"),
    ("launch_previews", "asset_version_id", "asset_versions", "fk_launch_previews_asset_version_id_asset_versions", "fk_launch_previews_workspace_asset_version_asset_versions", "RESTRICT", "RESTRICT"),
    ("launch_previews", "experiment_id", "experiments", "fk_launch_previews_experiment_id_experiments", "fk_launch_previews_workspace_experiment_experiments", "RESTRICT", "RESTRICT"),
    ("launch_operations", "experiment_id", "experiments", "fk_launch_operations_experiment_id_experiments", "fk_launch_operations_workspace_experiment_experiments", "RESTRICT", "RESTRICT"),
    ("launch_operations", "preview_id", "launch_previews", "fk_launch_operations_preview_id_launch_previews", "fk_launch_operations_workspace_preview_launch_previews", "RESTRICT", "RESTRICT"),
    ("launch_audit_log", "operation_id", "launch_operations", "fk_launch_audit_log_operation_id_launch_operations", "fk_launch_audit_log_workspace_operation_launch_operations", "SET NULL", "RESTRICT"),
)

PARENT_IDENTITIES = (
    "dimension_schema_versions",
    "plans",
    "generation_runs",
    "assets",
    "jobs",
    "asset_versions",
    "meta_accounts",
    "meta_sync_runs",
    "experiments",
    "launch_previews",
    "launch_operations",
)


def _assert_existing_rows_are_tenant_consistent() -> None:
    connection = op.get_bind()
    for child, column, parent, *_rest in RELATIONS:
        mismatch = connection.execute(
            sa.text(
                f'SELECT child.id FROM "{child}" AS child '
                f'JOIN "{parent}" AS parent ON parent.id = child."{column}" '
                f'WHERE child."{column}" IS NOT NULL '
                'AND child.workspace_id <> parent.workspace_id LIMIT 1'
            )
        ).scalar()
        if mismatch is not None:
            raise RuntimeError(
                f"cannot install tenant-safe FK {child}.{column}: "
                f"existing row {mismatch!r} points across Workspaces"
            )


def upgrade() -> None:
    _assert_existing_rows_are_tenant_consistent()

    for parent in PARENT_IDENTITIES:
        with op.batch_alter_table(parent) as batch_op:
            batch_op.create_unique_constraint(
                f"uq_{parent}_workspace_id_id", ["workspace_id", "id"]
            )

    grouped: dict[str, list[tuple[str, ...]]] = defaultdict(list)
    for relation in RELATIONS:
        grouped[relation[0]].append(relation)
    for child, relations in grouped.items():
        with op.batch_alter_table(child) as batch_op:
            for _child, column, parent, old_name, new_name, _old_delete, new_delete in relations:
                batch_op.drop_constraint(old_name, type_="foreignkey")
                batch_op.create_foreign_key(
                    new_name,
                    parent,
                    ["workspace_id", column],
                    ["workspace_id", "id"],
                    ondelete=new_delete,
                )


def downgrade() -> None:
    grouped: dict[str, list[tuple[str, ...]]] = defaultdict(list)
    for relation in RELATIONS:
        grouped[relation[0]].append(relation)
    for child, relations in grouped.items():
        with op.batch_alter_table(child) as batch_op:
            for _child, column, parent, old_name, new_name, old_delete, _new_delete in relations:
                batch_op.drop_constraint(new_name, type_="foreignkey")
                batch_op.create_foreign_key(
                    old_name,
                    parent,
                    [column],
                    ["id"],
                    ondelete=old_delete,
                )

    for parent in reversed(PARENT_IDENTITIES):
        with op.batch_alter_table(parent) as batch_op:
            batch_op.drop_constraint(
                f"uq_{parent}_workspace_id_id", type_="unique"
            )
