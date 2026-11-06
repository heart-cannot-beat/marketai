"""M1: Generation → Assets schema expansion.

New tables: generation_drafts, resolution_preview_confirmations,
generation_recipe_snapshots, run_items, provider_attempt_events,
provider_attempt_projections, run_item_asset_version_bindings,
asset_qc_attempts, asset_qc_check_results, generation_run_commands,
export_packages.

Extended tables: generation_runs, asset_versions, quality_reviews,
resolution_previews, provider_call_attempts, spend_reservations,
provider_cost_ledger.

Tables are created and existing tables extended in FK-dependency order
so that ``ALTER ... ADD COLUMN`` / constraint additions run through
``op.batch_alter_table`` (SQLite has no ``ALTER ... ADD CONSTRAINT``; on
PostgreSQL the same call issues a plain in-place ``ALTER TABLE``, so this
is dialect-portable rather than SQLite-only).

``resolution_previews`` predates the ``(workspace_id, id)`` unique-anchor
convention used by every other Workspace-owned table; this migration adds
that constraint before ``resolution_preview_confirmations`` (which FKs to
it) is created.

RLS (spec invariant, tests/test_postgres_rls.py): every new table here is
an ordinary workspace-owned row and gets the same
``marketai_context_is_current(workspace_id)`` tenant_isolation policy as
every prior workspace-owned table.
"""
from alembic import op
import sqlalchemy as sa

revision = "n1a2b3c4d5e6"
down_revision = "l1a2b3c4d5e6"
branch_labels = None
depends_on = None


#: Picked up by the postgres RLS acceptance sweep alongside every other
#: migration's NEW_WORKSPACE_OWNED -- see tests/test_postgres_rls.py.
#: Every table here is an ordinary workspace-owned row (workspace_id
#: NOT NULL, no hybrid platform/workspace scoping), so they all get the
#: same plain tenant_isolation policy.
NEW_WORKSPACE_OWNED = (
    "generation_drafts",
    "generation_recipe_snapshots",
    "resolution_preview_confirmations",
    "run_items",
    "provider_attempt_events",
    "provider_attempt_projections",
    "run_item_asset_version_bindings",
    "asset_qc_attempts",
    "asset_qc_check_results",
    "generation_run_commands",
    "export_packages",
)

#: Append-only tables (no `revision`/`updated_at` columns -- rows are
#: written once and never mutated). PostgreSQL-only reject-update-delete
#: triggers, same pattern as k1a2b3c4d5e6.APPEND_ONLY_TABLES.
APPEND_ONLY_TABLES = (
    "provider_attempt_events",
    "generation_recipe_snapshots",
    "resolution_preview_confirmations",
    "run_item_asset_version_bindings",
    "asset_qc_check_results",
    "generation_run_commands",
)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    # --- New tables with no dependency on this migration's other changes ---
    op.create_table(
        "generation_drafts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("product_id", sa.String(64), nullable=False),
        sa.Column("selectors_json", sa.JSON, nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("execution_mode", sa.String(20), nullable=False),
        sa.Column("combination_strategy_json", sa.JSON, nullable=False),
        sa.Column("format_json", sa.JSON, nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("exclusions_json", sa.JSON, nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("regeneration_intent", sa.String(40), nullable=False,
                  server_default="new_generation"),
        sa.Column("source_asset_version_id", sa.String(64), nullable=True),
        sa.Column("source_recipe_identity", sa.String(128), nullable=True),
        sa.Column("revision", sa.Integer, nullable=False, server_default="1"),
        sa.Column("state", sa.String(20), nullable=False, server_default="editing"),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("workspace_id", "id",
                            name="uq_generation_drafts_workspace_id"),
        sa.CheckConstraint(
            "state IN ('editing', 'previewing', 'archived')",
            name="ck_generation_draft_state"),
        sa.CheckConstraint(
            "execution_mode IN ('byok', 'sandbox', 'mock')",
            name="ck_generation_draft_execution_mode"),
        sa.CheckConstraint("revision >= 1",
                            name="ck_generation_draft_revision"),
        sa.CheckConstraint(
            "regeneration_intent IN ('new_generation', 'same_recipe', "
            "'new_seed', 'edit_prompt', 'quality_feedback')",
            name="ck_generation_draft_regen_intent"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "product_id"],
            ["products.workspace_id", "products.id"],
            name="fk_generation_drafts_workspace_product",
            ondelete="RESTRICT"),
    )

    op.create_table(
        "generation_recipe_snapshots",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("canonicalization_version", sa.String(40), nullable=False),
        sa.Column("recipe_hash", sa.String(64), nullable=False),
        sa.Column("canonical_json", sa.JSON, nullable=False),
        sa.Column("execution_mode", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id",
                            name="uq_generation_recipe_snapshots_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id", "canonicalization_version", "recipe_hash",
            name="uq_recipe_snapshot_workspace_canon_hash"),
        sa.CheckConstraint(
            "execution_mode IN ('byok', 'sandbox', 'mock')",
            name="ck_recipe_snapshot_exec_mode"),
    )

    # resolution_previews predates the (workspace_id, id) unique-anchor
    # convention used elsewhere; resolution_preview_confirmations needs it
    # as an FK target, so add it here before that table is created.
    with op.batch_alter_table("resolution_previews", schema=None) as batch_op:
        batch_op.create_unique_constraint(
            "uq_resolution_previews_workspace_id",
            ["workspace_id", "id"],
        )

    op.create_table(
        "resolution_preview_confirmations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("preview_id", sa.String(64), nullable=False),
        sa.Column("preview_hash", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("quoted_max_provider_amount", sa.Numeric(18, 6),
                  nullable=False),
        sa.Column("quoted_max_budget_amount", sa.Numeric(18, 6),
                  nullable=False),
        sa.Column("quoted_currency", sa.String(3), nullable=False),
        sa.Column("pricing_version", sa.String(40), nullable=False,
                  server_default=""),
        sa.Column("authorization_context_json", sa.JSON, nullable=False,
                  server_default=sa.text("'{}'")),
        sa.UniqueConstraint(
            "workspace_id", "id",
            name="uq_resolution_preview_confirmations_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id", "preview_id",
            name="uq_resolution_preview_confirmations_workspace_preview"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "preview_id"],
            ["resolution_previews.workspace_id", "resolution_previews.id"],
            name="fk_rpc_workspace_preview", ondelete="RESTRICT"),
    )

    # --- Extend generation_runs ---
    # Must run before run_items is created: run_items carries a composite
    # FK to generation_runs(workspace_id, id, execution_mode), and on
    # PostgreSQL the referenced columns must already be covered by a
    # unique constraint at FK-creation time.
    with op.batch_alter_table("generation_runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("recipe_snapshot_id", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("creation_preview_confirmation_id", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("creation_operation_id", sa.String(64), nullable=True))
        batch_op.add_column(
            sa.Column("submission_kind", sa.String(32), nullable=False,
                      server_default="legacy_migrated"))
        batch_op.add_column(
            sa.Column("execution_mode", sa.String(20), nullable=False,
                      server_default="mock"))
        batch_op.create_unique_constraint(
            "uq_generation_runs_workspace_id_mode",
            ["workspace_id", "id", "execution_mode"],
        )
        batch_op.create_check_constraint(
            "ck_generation_run_submission_kind",
            "submission_kind IN ('confirmed_preview', 'legacy_migrated')",
        )
        batch_op.create_check_constraint(
            "ck_generation_run_execution_mode",
            "execution_mode IN ('byok', 'sandbox', 'mock')",
        )
        batch_op.create_foreign_key(
            "fk_generation_runs_workspace_recipe_snapshot",
            "generation_recipe_snapshots",
            ["workspace_id", "recipe_snapshot_id"],
            ["workspace_id", "id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_generation_runs_workspace_confirmation",
            "resolution_preview_confirmations",
            ["workspace_id", "creation_preview_confirmation_id"],
            ["workspace_id", "id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_generation_runs_workspace_creation_operation",
            "operation_records",
            ["workspace_id", "creation_operation_id"],
            ["workspace_id", "id"],
            ondelete="RESTRICT",
        )

    # run_items depends on generation_runs (which already exists, and now
    # carries the (workspace_id, id, execution_mode) unique constraint).
    op.create_table(
        "run_items",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("generation_run_id", sa.String(64), nullable=False,
                  index=True),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("combination_identity_json", sa.JSON, nullable=False),
        sa.Column("combination_fingerprint", sa.String(64), nullable=False,
                  index=True),
        sa.Column("stable_dimension_signature", sa.String(200),
                  nullable=False, server_default=""),
        sa.Column("tags_json", sa.JSON, nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("variant_index", sa.Integer, nullable=False,
                  server_default="0"),
        sa.Column("requested_seed", sa.String(128), nullable=True),
        sa.Column("effective_seed", sa.String(128), nullable=True),
        sa.Column("seed_unavailable_reason", sa.String(100), nullable=True),
        sa.Column("semantic_fingerprint", sa.String(64), nullable=False),
        sa.Column("intent_kind", sa.String(40), nullable=False,
                  server_default="new_generation"),
        sa.Column("source_asset_version_id", sa.String(64), nullable=True),
        sa.Column("source_run_item_id", sa.String(64), nullable=True),
        sa.Column("execution_mode", sa.String(20), nullable=False),
        sa.Column("state", sa.String(20), nullable=False,
                  server_default="pending"),
        sa.Column("error_summary", sa.Text, nullable=False,
                  server_default=""),
        sa.Column("legacy_asset_id", sa.String(100), nullable=True),
        sa.Column("revision", sa.Integer, nullable=False,
                  server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id",
                            name="uq_run_items_workspace_id"),
        sa.UniqueConstraint("workspace_id", "generation_run_id", "ordinal",
                            name="uq_run_items_workspace_run_ordinal"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "generation_run_id"],
            ["generation_runs.workspace_id", "generation_runs.id"],
            name="fk_run_items_workspace_run", ondelete="RESTRICT"),
        sa.CheckConstraint(
            "state IN ('pending', 'queued', 'running', 'qc_pending', "
            "'succeeded', 'reused', 'failed', 'cancelled', 'paused', "
            "'uncertain')",
            name="ck_run_item_state"),
        sa.CheckConstraint(
            "execution_mode IN ('byok', 'sandbox', 'mock')",
            name="ck_run_item_execution_mode"),
        sa.CheckConstraint("ordinal >= 0", name="ck_run_item_ordinal"),
        sa.CheckConstraint("revision >= 1", name="ck_run_item_revision"),
        sa.CheckConstraint(
            "intent_kind IN ('new_generation', 'same_recipe', 'new_seed', "
            "'edit_prompt', 'quality_feedback')",
            name="ck_run_item_intent_kind"),
    )
    op.create_index(
        "ix_run_items_run_state",
        "run_items",
        ["workspace_id", "generation_run_id", "state"],
    )

    # --- Extend provider_call_attempts ---
    # Depends on run_items existing (new FK target).
    with op.batch_alter_table("provider_call_attempts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("run_item_id", sa.String(64), nullable=True))
        batch_op.add_column(
            sa.Column("step_kind", sa.String(20), nullable=False,
                      server_default="generation"))
        batch_op.add_column(sa.Column("attempt_number", sa.Integer, nullable=True))
        batch_op.add_column(sa.Column("input_asset_version_id", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("redacted_request_hash", sa.String(64), nullable=True))
        batch_op.create_foreign_key(
            "fk_provider_call_attempts_workspace_run_item",
            "run_items",
            ["workspace_id", "run_item_id"],
            ["workspace_id", "id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_provider_call_attempt_step_kind",
            "step_kind IN ('generation', 'visual_qc')",
        )

    op.create_table(
        "provider_attempt_events",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("provider_call_attempt_id", sa.String(64), nullable=False,
                  index=True),
        sa.Column("sequence", sa.Integer, nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("payload_json", sa.JSON, nullable=False),
        sa.Column("evidence_refs_json", sa.JSON, nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("executor", sa.String(100), nullable=False),
        sa.Column("trace_id", sa.String(100), nullable=False,
                  server_default=""),
        sa.Column("request_id", sa.String(100), nullable=False,
                  server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "workspace_id", "provider_call_attempt_id", "sequence",
            name="uq_provider_attempt_events_workspace_attempt_seq"),
        sa.UniqueConstraint(
            "workspace_id", "provider_call_attempt_id", "idempotency_key",
            name="uq_provider_attempt_events_workspace_attempt_idem"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "provider_call_attempt_id"],
            ["provider_call_attempts.workspace_id",
             "provider_call_attempts.id"],
            name="fk_provider_attempt_events_workspace_attempt",
            ondelete="RESTRICT"),
        sa.CheckConstraint(
            "event_type IN ('dispatch_started', 'response_received', "
            "'failure_classified', 'usage_reported', "
            "'certainty_resolved', 'reconciled', "
            "'legacy_state_imported')",
            name="ck_provider_attempt_event_type"),
        sa.CheckConstraint("sequence >= 1",
                            name="ck_provider_attempt_event_seq"),
    )

    op.create_table(
        "provider_attempt_projections",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("provider_call_attempt_id", sa.String(64), nullable=False),
        sa.Column("dispatch_certainty", sa.String(20), nullable=False,
                  server_default="not_dispatched"),
        sa.Column("outcome", sa.String(20), nullable=False,
                  server_default="pending"),
        sa.Column("usage_summary_json", sa.JSON, nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("settlement_state", sa.String(20), nullable=False,
                  server_default="pending"),
        sa.Column("reconciliation_state", sa.String(20), nullable=False,
                  server_default="none"),
        sa.Column("last_event_sequence", sa.Integer, nullable=False,
                  server_default="0"),
        sa.Column("revision", sa.Integer, nullable=False,
                  server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "workspace_id", "provider_call_attempt_id",
            name="uq_provider_attempt_projections_workspace_attempt"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "provider_call_attempt_id"],
            ["provider_call_attempts.workspace_id",
             "provider_call_attempts.id"],
            name="fk_provider_attempt_projections_workspace_attempt",
            ondelete="RESTRICT"),
        sa.CheckConstraint(
            "dispatch_certainty IN ('not_dispatched', 'dispatched', "
            "'uncertain')",
            name="ck_projection_dispatch_cert"),
        sa.CheckConstraint(
            "outcome IN ('pending', 'succeeded', 'failed', 'uncertain', "
            "'reconciled')",
            name="ck_projection_outcome"),
        sa.CheckConstraint("last_event_sequence >= 0",
                            name="ck_projection_last_seq"),
        sa.CheckConstraint("revision >= 1", name="ck_projection_revision"),
        sa.CheckConstraint(
            "settlement_state IN ('pending', 'settled', 'voided')",
            name="ck_projection_settlement"),
        sa.CheckConstraint(
            "reconciliation_state IN ('none', 'needed', 'reconciled')",
            name="ck_projection_reconcile"),
    )

    op.create_table(
        "run_item_asset_version_bindings",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("run_item_id", sa.String(64), nullable=False, index=True),
        sa.Column("asset_version_id", sa.String(64), nullable=False,
                  index=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("producing_provider_attempt_id", sa.String(64),
                  nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id",
                            name="uq_run_item_av_bindings_workspace_id"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_item_id"],
            ["run_items.workspace_id", "run_items.id"],
            name="fk_run_item_av_bindings_workspace_run_item",
            ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "asset_version_id"],
            ["asset_versions.workspace_id", "asset_versions.id"],
            name="fk_run_item_av_bindings_workspace_av",
            ondelete="RESTRICT"),
        sa.CheckConstraint("kind IN ('produced', 'reused')",
                            name="ck_run_item_av_binding_kind"),
    )

    op.create_table(
        "asset_qc_attempts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("asset_version_id", sa.String(64), nullable=False,
                  index=True),
        sa.Column("run_item_id", sa.String(64), nullable=False, index=True),
        sa.Column("feedback_round", sa.Integer, nullable=False,
                  server_default="0"),
        sa.Column("qc_policy_version", sa.String(40), nullable=False),
        sa.Column("rule_set_version", sa.String(40), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("provider_attempt_id", sa.String(64), nullable=True),
        sa.Column("execution_mode", sa.String(20), nullable=False),
        sa.Column("state", sa.String(20), nullable=False,
                  server_default="pending"),
        sa.Column("feedback_summary_json", sa.JSON, nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("revision", sa.Integer, nullable=False,
                  server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id",
                            name="uq_asset_qc_attempts_workspace_id"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "asset_version_id"],
            ["asset_versions.workspace_id", "asset_versions.id"],
            name="fk_asset_qc_attempts_workspace_av",
            ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_item_id"],
            ["run_items.workspace_id", "run_items.id"],
            name="fk_asset_qc_attempts_workspace_run_item",
            ondelete="RESTRICT"),
        sa.CheckConstraint(
            "state IN ('pending', 'running', 'passed', 'failed_blocking', "
            "'failed_nonblocking', 'uncertain')",
            name="ck_asset_qc_attempt_state"),
        sa.CheckConstraint(
            "execution_mode IN ('byok', 'sandbox', 'mock')",
            name="ck_asset_qc_attempt_mode"),
        sa.CheckConstraint("feedback_round >= 0",
                            name="ck_asset_qc_feedback_round"),
        sa.CheckConstraint("revision >= 1", name="ck_asset_qc_revision"),
    )

    op.create_table(
        "asset_qc_check_results",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("qc_attempt_id", sa.String(64), nullable=False,
                  index=True),
        sa.Column("rule_id", sa.String(64), nullable=False),
        sa.Column("rule_version", sa.String(40), nullable=False),
        sa.Column("check_type", sa.String(40), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("outcome", sa.String(20), nullable=False),
        sa.Column("evidence_json", sa.JSON, nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("feedback_detail", sa.Text, nullable=False,
                  server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "qc_attempt_id"],
            ["asset_qc_attempts.workspace_id", "asset_qc_attempts.id"],
            name="fk_asset_qc_check_results_workspace_attempt",
            ondelete="RESTRICT"),
        sa.CheckConstraint(
            "severity IN ('must_match', 'should_match', 'informational')",
            name="ck_qc_check_severity"),
        sa.CheckConstraint(
            "outcome IN ('pass', 'fail', 'skip', 'error')",
            name="ck_qc_check_outcome"),
    )

    op.create_table(
        "generation_run_commands",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("generation_run_id", sa.String(64), nullable=False,
                  index=True),
        sa.Column("operation_id", sa.String(64), nullable=False),
        sa.Column("command_kind", sa.String(40), nullable=False),
        sa.Column("target_selection_json", sa.JSON, nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("target_selection_hash", sa.String(64), nullable=False,
                  server_default=""),
        sa.Column("expected_run_revision", sa.Integer, nullable=False),
        sa.Column("preview_confirmation_id", sa.String(64), nullable=True),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("authorization_epoch", sa.Integer, nullable=False,
                  server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id",
                            name="uq_generation_run_commands_workspace_id"),
        sa.UniqueConstraint("workspace_id", "operation_id",
                            name="uq_generation_run_commands_workspace_operation"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "generation_run_id"],
            ["generation_runs.workspace_id", "generation_runs.id"],
            name="fk_generation_run_commands_workspace_run",
            ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "operation_id"],
            ["operation_records.workspace_id", "operation_records.id"],
            name="fk_generation_run_commands_workspace_operation",
            ondelete="RESTRICT"),
        sa.CheckConstraint(
            "command_kind IN ('submit', 'retry_failed_items', "
            "'resume_paused_items', 'cancel', 'reconcile')",
            name="ck_generation_run_command_kind"),
    )

    op.create_table(
        "export_packages",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("operation_id", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("selector_json", sa.JSON, nullable=False),
        sa.Column("selected_versions_json", sa.JSON, nullable=False),
        sa.Column("execution_mode", sa.String(20), nullable=False),
        sa.Column("state", sa.String(20), nullable=False,
                  server_default="pending"),
        sa.Column("committed_object_key", sa.String(1000), nullable=False,
                  server_default=""),
        sa.Column("byte_size", sa.Integer, nullable=False,
                  server_default="0"),
        sa.Column("zip_hash", sa.String(64), nullable=False,
                  server_default=""),
        sa.Column("manifest_hash", sa.String(64), nullable=False,
                  server_default=""),
        sa.Column("counts_json", sa.JSON, nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("failure_evidence", sa.Text, nullable=False,
                  server_default=""),
        sa.Column("revision", sa.Integer, nullable=False,
                  server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("workspace_id", "id",
                            name="uq_export_packages_workspace_id"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "operation_id"],
            ["operation_records.workspace_id", "operation_records.id"],
            name="fk_export_packages_workspace_operation",
            ondelete="RESTRICT"),
        sa.CheckConstraint(
            "execution_mode IN ('byok', 'sandbox', 'mock')",
            name="ck_export_package_mode"),
        sa.CheckConstraint(
            "state IN ('pending', 'building', 'ready', 'failed', "
            "'expired')",
            name="ck_export_package_state"),
        sa.CheckConstraint("revision >= 1",
                            name="ck_export_package_revision"),
    )

    # --- Extend remaining existing tables (no ordering dependency) ---
    with op.batch_alter_table("asset_versions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("producing_run_item_id", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("provider_attempt_id", sa.String(64), nullable=True))
        batch_op.add_column(
            sa.Column("origin_kind", sa.String(32), nullable=False,
                      server_default="legacy"))
        batch_op.add_column(sa.Column("execution_mode", sa.String(20), nullable=True))
        batch_op.add_column(sa.Column("requested_seed", sa.String(128), nullable=True))
        batch_op.add_column(sa.Column("effective_seed", sa.String(128), nullable=True))
        batch_op.add_column(sa.Column("seed_unavailable_reason", sa.String(100), nullable=True))
        batch_op.create_check_constraint(
            "ck_asset_version_origin_kind",
            "origin_kind IN ('generation', 'upload', 'reuse', 'legacy')",
        )
        batch_op.create_check_constraint(
            "ck_asset_version_execution_mode",
            "execution_mode IS NULL OR execution_mode IN ('byok', 'sandbox', 'mock')",
        )

    with op.batch_alter_table("quality_reviews", schema=None) as batch_op:
        batch_op.add_column(sa.Column("asset_version_id", sa.String(64), nullable=True))
        batch_op.add_column(
            sa.Column("legacy_unscoped", sa.Boolean, nullable=False,
                      server_default=sa.false()))
        batch_op.add_column(sa.Column("legacy_asset_id", sa.String(100), nullable=True))
        batch_op.add_column(sa.Column("review_value", sa.String(32), nullable=True))
        batch_op.create_check_constraint(
            "ck_quality_review_review_value",
            "review_value IS NULL OR review_value IN ('approved', 'rejected', "
            "'needs_changes')",
        )

    with op.batch_alter_table("resolution_previews", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("purpose", sa.String(40), nullable=False,
                      server_default="initial_generation"))
        batch_op.add_column(sa.Column("recovery_run_id", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("recovery_command_kind", sa.String(40), nullable=True))
        batch_op.add_column(
            sa.Column("recovery_target_json", sa.JSON, nullable=False,
                      server_default=sa.text("'{}'")))
        batch_op.add_column(
            sa.Column("recovery_max_additional_cost", sa.Numeric(18, 6), nullable=True))
        batch_op.create_check_constraint(
            "ck_resolution_preview_purpose",
            "purpose IN ('initial_generation', 'run_recovery')",
        )
        batch_op.create_check_constraint(
            "ck_resolution_preview_recovery_cmd_kind",
            "recovery_command_kind IS NULL OR recovery_command_kind IN "
            "('retry_failed_items', 'resume_paused_items')",
        )

    with op.batch_alter_table("spend_reservations", schema=None) as batch_op:
        batch_op.add_column(sa.Column("run_item_id", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("preview_confirmation_id", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("recipe_snapshot_id", sa.String(64), nullable=True))

    with op.batch_alter_table("provider_cost_ledger", schema=None) as batch_op:
        batch_op.add_column(sa.Column("run_item_id", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("provider_call_attempt_id", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("preview_confirmation_id", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("recipe_snapshot_id", sa.String(64), nullable=True))

    _install_rls_and_grants()
    _install_append_only_triggers()


def _install_append_only_triggers() -> None:
    if not _is_postgres():
        return
    for table in APPEND_ONLY_TABLES:
        fn_name = f"reject_update_delete_{table}"
        trg_name = f"trg_{table}_immutable"
        op.execute(
            f"CREATE FUNCTION {fn_name}() RETURNS trigger AS $$ "
            f"BEGIN RAISE EXCEPTION '{table} is append-only'; END; "
            f"$$ LANGUAGE plpgsql SET search_path = '';"
        )
        op.execute(
            f"CREATE TRIGGER {trg_name} "
            f"BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {fn_name}();"
        )


def _revoke_append_only_triggers() -> None:
    if not _is_postgres():
        return
    for table in APPEND_ONLY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table}")
        op.execute(f"DROP FUNCTION IF EXISTS reject_update_delete_{table}")


def _install_rls_and_grants() -> None:
    if not _is_postgres():
        # SQLite: the repository layer is the only enforcement available,
        # same as every other tenant-owned table (see h1a2b3c4d5e6).
        return

    for table in NEW_WORKSPACE_OWNED:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"')
        op.execute(
            f'''CREATE POLICY tenant_isolation ON "{table}"
               USING (marketai_context_is_current(workspace_id))
               WITH CHECK (marketai_context_is_current(workspace_id))'''
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON TABLE {table} TO marketai_domain_api")
        op.execute(f"GRANT SELECT ON TABLE {table} TO marketai_domain_worker")
        op.execute(f"GRANT SELECT ON TABLE {table} TO marketai_domain_readonly")

    # -- Worker role: INSERT --
    # The Worker records provider dispatch/response events, projects them,
    # binds produced assets to their run item, runs QC and reacts to
    # operator commands -- all of that is Worker-authored INSERT.
    for table in (
        "provider_attempt_events",
        "provider_attempt_projections",
        "run_item_asset_version_bindings",
        "asset_qc_attempts",
        "asset_qc_check_results",
        "generation_run_commands",
    ):
        op.execute(f"GRANT INSERT ON TABLE {table} TO marketai_domain_worker")

    # -- Worker role: UPDATE --
    # CAS state transitions only: run item / QC attempt / attempt
    # projection lifecycle state, and the generation run's rollup counters.
    for table in (
        "run_items",
        "provider_attempt_projections",
        "asset_qc_attempts",
        "generation_runs",
    ):
        op.execute(f"GRANT UPDATE ON TABLE {table} TO marketai_domain_worker")


def _revoke_rls_and_grants() -> None:
    if not _is_postgres():
        return
    # generation_runs predates this migration (and NEW_WORKSPACE_OWNED), so
    # its own REVOKE ALL below would strip grants this migration did not
    # issue -- undo only the UPDATE grant added in _install_rls_and_grants.
    op.execute("REVOKE UPDATE ON TABLE generation_runs FROM marketai_domain_worker")
    for table in NEW_WORKSPACE_OWNED:
        op.execute(
            f"REVOKE ALL ON TABLE {table} FROM "
            "marketai_domain_api, marketai_domain_worker, marketai_domain_readonly"
        )
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"')
        op.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    _revoke_append_only_triggers()
    _revoke_rls_and_grants()

    # provider_cost_ledger
    with op.batch_alter_table("provider_cost_ledger", schema=None) as batch_op:
        batch_op.drop_column("recipe_snapshot_id")
        batch_op.drop_column("preview_confirmation_id")
        batch_op.drop_column("provider_call_attempt_id")
        batch_op.drop_column("run_item_id")

    # spend_reservations
    with op.batch_alter_table("spend_reservations", schema=None) as batch_op:
        batch_op.drop_column("recipe_snapshot_id")
        batch_op.drop_column("preview_confirmation_id")
        batch_op.drop_column("run_item_id")

    # resolution_previews
    with op.batch_alter_table("resolution_previews", schema=None) as batch_op:
        batch_op.drop_constraint(
            "ck_resolution_preview_recovery_cmd_kind", type_="check")
        batch_op.drop_constraint("ck_resolution_preview_purpose", type_="check")
        batch_op.drop_column("recovery_max_additional_cost")
        batch_op.drop_column("recovery_target_json")
        batch_op.drop_column("recovery_command_kind")
        batch_op.drop_column("recovery_run_id")
        batch_op.drop_column("purpose")

    # quality_reviews
    with op.batch_alter_table("quality_reviews", schema=None) as batch_op:
        batch_op.drop_constraint("ck_quality_review_review_value", type_="check")
        batch_op.drop_column("review_value")
        batch_op.drop_column("legacy_asset_id")
        batch_op.drop_column("legacy_unscoped")
        batch_op.drop_column("asset_version_id")

    # asset_versions
    with op.batch_alter_table("asset_versions", schema=None) as batch_op:
        batch_op.drop_constraint("ck_asset_version_execution_mode", type_="check")
        batch_op.drop_constraint("ck_asset_version_origin_kind", type_="check")
        batch_op.drop_column("seed_unavailable_reason")
        batch_op.drop_column("effective_seed")
        batch_op.drop_column("requested_seed")
        batch_op.drop_column("execution_mode")
        batch_op.drop_column("origin_kind")
        batch_op.drop_column("provider_attempt_id")
        batch_op.drop_column("producing_run_item_id")

    # new tables (reverse dependency order)
    op.drop_table("export_packages")
    op.drop_table("generation_run_commands")
    op.drop_table("asset_qc_check_results")
    op.drop_table("asset_qc_attempts")
    op.drop_table("run_item_asset_version_bindings")
    op.drop_table("provider_attempt_projections")
    op.drop_table("provider_attempt_events")

    # provider_call_attempts (drop the run_item FK/columns before run_items
    # is dropped)
    with op.batch_alter_table("provider_call_attempts", schema=None) as batch_op:
        batch_op.drop_constraint(
            "ck_provider_call_attempt_step_kind", type_="check")
        batch_op.drop_constraint(
            "fk_provider_call_attempts_workspace_run_item", type_="foreignkey")
        batch_op.drop_column("redacted_request_hash")
        batch_op.drop_column("input_asset_version_id")
        batch_op.drop_column("attempt_number")
        batch_op.drop_column("step_kind")
        batch_op.drop_column("run_item_id")

    op.drop_index("ix_run_items_run_state", "run_items")
    op.drop_table("run_items")

    # generation_runs
    with op.batch_alter_table("generation_runs", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_generation_runs_workspace_creation_operation", type_="foreignkey")
        batch_op.drop_constraint(
            "fk_generation_runs_workspace_confirmation", type_="foreignkey")
        batch_op.drop_constraint(
            "fk_generation_runs_workspace_recipe_snapshot", type_="foreignkey")
        batch_op.drop_constraint(
            "ck_generation_run_execution_mode", type_="check")
        batch_op.drop_constraint(
            "ck_generation_run_submission_kind", type_="check")
        batch_op.drop_constraint(
            "uq_generation_runs_workspace_id_mode", type_="unique")
        batch_op.drop_column("execution_mode")
        batch_op.drop_column("submission_kind")
        batch_op.drop_column("creation_operation_id")
        batch_op.drop_column("creation_preview_confirmation_id")
        batch_op.drop_column("recipe_snapshot_id")

    op.drop_table("resolution_preview_confirmations")

    with op.batch_alter_table("resolution_previews", schema=None) as batch_op:
        batch_op.drop_constraint(
            "uq_resolution_previews_workspace_id", type_="unique")

    op.drop_table("generation_recipe_snapshots")
    op.drop_table("generation_drafts")
