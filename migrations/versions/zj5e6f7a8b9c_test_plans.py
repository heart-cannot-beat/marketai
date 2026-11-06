"""Test plans: provider-neutral ad-test plans, their directions and stage runs.

Revision ID: zj5e6f7a8b9c
Revises: zi4d5e6f7a8b

Issue #163 (front-flow contract §3.2, §4, §5.1, §5.3).

* ``test_plans`` -- one "set the ad test" plan per product: goal, audience,
  scale, status, If-Match revision and the provider-neutral recipe snapshot
  (frozen product facts, factor catalog, constraints). No provider, model or
  prompt.
* ``test_plan_directions`` -- the 3-5 direction cards: title, what it tests,
  hypothesis, scene description, copy, factor values, combination
  signatures, ``removed`` / ``user_added`` flags.
* ``test_plan_runs`` -- the one generation Run each stage (``sample`` /
  ``full``) materialised into; unique per (plan, stage) so a replay returns
  the same Run.
* ``generation_defaults`` -- the Workspace setting「生成默认值」(provider,
  model, execution mode, aspect ratio, variants per scale), Owner-written.

Tenancy: RLS ``tenant_isolation`` on all four. Grants: the API creates and
updates plans, directions, runs and defaults; the Worker drafts a plan
(SELECT/UPDATE ``test_plans``, SELECT/INSERT ``test_plan_directions``);
readonly reads. No role gets DELETE (a removed direction is a flag).
"""
from alembic import op
import sqlalchemy as sa


revision = "zj5e6f7a8b9c"
down_revision = "zi4d5e6f7a8b"
branch_labels = None
depends_on = None


NEW_WORKSPACE_OWNED = (
    "test_plans",
    "test_plan_directions",
    "test_plan_runs",
    "generation_defaults",
)

#: (table, API privileges, Worker privileges); readonly gets SELECT on all.
GRANTS = (
    ("test_plans", "SELECT, INSERT, UPDATE", "SELECT, UPDATE"),
    ("test_plan_directions", "SELECT, INSERT, UPDATE", "SELECT, INSERT"),
    ("test_plan_runs", "SELECT, INSERT, UPDATE", None),
    ("generation_defaults", "SELECT, INSERT, UPDATE", None),
)


def _in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    op.create_table(
        "test_plans",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("product_id", sa.String(length=64), nullable=False),
        sa.Column("product_entry_id", sa.String(length=64), nullable=False),
        sa.Column("product_version_id", sa.String(length=64), nullable=False),
        sa.Column("goal", sa.String(length=16), nullable=False),
        sa.Column("audience_text", sa.Text(), nullable=False),
        sa.Column("audience_card_index", sa.Integer(), nullable=True),
        sa.Column("scale", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("recipe_json", sa.JSON(), nullable=False),
        sa.Column("seed_asset_version_ids", sa.JSON(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        sa.Column("operation_id", sa.String(length=64), nullable=True),
        sa.Column("job_id", sa.String(length=64), nullable=True),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=True),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("template_entry_id", sa.String(length=64), nullable=True),
        sa.Column("created_by", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("drafted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN " + _in(("drafting", "ready", "failed")), name="ck_test_plans_status"),
        sa.CheckConstraint("goal IN " + _in(("explore", "optimize")), name="ck_test_plans_goal"),
        sa.CheckConstraint("scale IN " + _in(("sample", "small", "full")), name="ck_test_plans_scale"),
        sa.CheckConstraint("revision >= 1", name="ck_test_plans_revision"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_test_plans_workspace_id_id"),
        sa.UniqueConstraint("workspace_id", "idempotency_key_hash", name="uq_test_plans_idempotency"),
    )
    op.create_index("ix_test_plans_workspace_id", "test_plans", ["workspace_id"], unique=False)
    op.create_index(
        "ix_test_plans_product", "test_plans", ["workspace_id", "product_id", "created_at"], unique=False,
    )

    op.create_table(
        "test_plan_directions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("plan_id", sa.String(length=64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("direction_key", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("what_it_tests", sa.Text(), nullable=False),
        sa.Column("hypothesis", sa.Text(), nullable=False),
        sa.Column("scene_description", sa.Text(), nullable=False),
        sa.Column("copy_json", sa.JSON(), nullable=False),
        sa.Column("factor_values_json", sa.JSON(), nullable=False),
        sa.Column("fragments_json", sa.JSON(), nullable=False),
        sa.Column("combination_signatures_json", sa.JSON(), nullable=False),
        sa.Column("removed", sa.Boolean(), nullable=False),
        sa.Column("user_added", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("ordinal >= 0", name="ck_test_plan_direction_ordinal"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "plan_id"],
            ["test_plans.workspace_id", "test_plans.id"],
            name="fk_test_plan_directions_workspace_plan",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_id", "direction_key", name="uq_test_plan_direction_key"),
        sa.UniqueConstraint("plan_id", "ordinal", name="uq_test_plan_direction_ordinal"),
    )
    op.create_index(
        "ix_test_plan_directions_workspace_id", "test_plan_directions", ["workspace_id"], unique=False,
    )
    op.create_index(
        "ix_test_plan_directions_plan_id", "test_plan_directions", ["plan_id"], unique=False,
    )

    op.create_table(
        "test_plan_runs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("plan_id", sa.String(length=64), nullable=False),
        sa.Column("stage", sa.String(length=16), nullable=False),
        sa.Column("target_scale", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("plan_revision", sa.Integer(), nullable=False),
        sa.Column("defaults_json", sa.JSON(), nullable=False),
        sa.Column("selection_json", sa.JSON(), nullable=False),
        sa.Column("template_entry_id", sa.String(length=64), nullable=True),
        sa.Column("template_version_id", sa.String(length=64), nullable=True),
        sa.Column("generation_draft_id", sa.String(length=64), nullable=True),
        sa.Column("preview_id", sa.String(length=64), nullable=True),
        sa.Column("generation_run_id", sa.String(length=64), nullable=True),
        sa.Column("operation_id", sa.String(length=64), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("stage IN " + _in(("sample", "full")), name="ck_test_plan_runs_stage"),
        sa.CheckConstraint(
            "status IN " + _in(("materializing", "submitted", "failed")), name="ck_test_plan_runs_status",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "plan_id"],
            ["test_plans.workspace_id", "test_plans.id"],
            name="fk_test_plan_runs_workspace_plan",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "plan_id", "stage", name="uq_test_plan_run_stage"),
    )
    op.create_index("ix_test_plan_runs_workspace_id", "test_plan_runs", ["workspace_id"], unique=False)
    op.create_index("ix_test_plan_runs_plan_id", "test_plan_runs", ["plan_id"], unique=False)

    op.create_table(
        "generation_defaults",
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("execution_mode", sa.String(length=16), nullable=False),
        sa.Column("aspect_ratio", sa.String(length=16), nullable=False),
        sa.Column("variants_sample", sa.Integer(), nullable=False),
        sa.Column("variants_small", sa.Integer(), nullable=False),
        sa.Column("variants_full", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("execution_mode IN ('mock', 'byok')", name="ck_generation_defaults_mode"),
        sa.CheckConstraint(
            "variants_sample >= 1 AND variants_small >= 1 AND variants_full >= 1",
            name="ck_generation_defaults_variants",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_generation_defaults_revision"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id"),
    )

    if op.get_bind().dialect.name == "postgresql":
        for table, api_privileges, worker_privileges in GRANTS:
            op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
            op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
            op.execute(
                f"CREATE POLICY tenant_isolation ON {table} "
                "USING (marketai_context_is_current(workspace_id)) "
                "WITH CHECK (marketai_context_is_current(workspace_id))"
            )
            op.execute(f"GRANT {api_privileges} ON TABLE {table} TO marketai_domain_api")
            if worker_privileges:
                op.execute(f"GRANT {worker_privileges} ON TABLE {table} TO marketai_domain_worker")
            op.execute(f"GRANT SELECT ON TABLE {table} TO marketai_domain_readonly")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table, _api, _worker in reversed(GRANTS):
            op.execute(
                f"REVOKE ALL ON TABLE {table} FROM "
                "marketai_domain_api, marketai_domain_worker, marketai_domain_readonly"
            )
            op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
            op.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY')
            op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
    op.drop_table("generation_defaults")
    op.drop_index("ix_test_plan_runs_plan_id", table_name="test_plan_runs")
    op.drop_index("ix_test_plan_runs_workspace_id", table_name="test_plan_runs")
    op.drop_table("test_plan_runs")
    op.drop_index("ix_test_plan_directions_plan_id", table_name="test_plan_directions")
    op.drop_index("ix_test_plan_directions_workspace_id", table_name="test_plan_directions")
    op.drop_table("test_plan_directions")
    op.drop_index("ix_test_plans_product", table_name="test_plans")
    op.drop_index("ix_test_plans_workspace_id", table_name="test_plans")
    op.drop_table("test_plans")
