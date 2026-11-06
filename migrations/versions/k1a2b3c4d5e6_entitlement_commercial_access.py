"""Entitlement, credential, cost, usage tables and commercial access pipeline.

Revision ID: k1a2b3c4d5e6
Revises: b9c0d1e2f3a4
Create Date: 2026-08-31 14:00:00.000000

Creates 13 new workspace-owned tables for:
- Entitlement inbox and projection (spec sections 2.1-2.2)
- BYOK credential versions and bindings (spec sections 2.4-2.5)
- Spend guardrails and guardrail events (spec sections 2.7-2.8)
- Spend and capacity reservations (spec sections 2.9-2.10)
- Provider call attempts (spec section 3.1)
- Provider cost ledger (spec section 2.11)
- Usage events (spec section 2.12)
- Customer notification outbox and guardrail notification state (spec section 2.13)

Also extends workspaces (lifecycle, budget_currency), operation_records
(semantic_key_hash, request_fingerprint_hash, admission_contract_version),
and jobs (operation_id, entitlement_version_at_enqueue, execution_contract_version).

Installs append-only triggers on four tables, the
``marketai_apply_workspace_projection_v2`` SECURITY DEFINER function,
RLS policies, and least-privilege role grants -- all PostgreSQL only.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "k1a2b3c4d5e6"
down_revision: str | Sequence[str] | None = "b9c0d1e2f3a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


NEW_TABLES = (
    "entitlement_snapshot_inbox",
    "entitlement_projections",
    "credential_versions",
    "credential_bindings",
    "spend_guardrails",
    "spend_guardrail_events",
    "spend_reservations",
    "capacity_reservations",
    "provider_call_attempts",
    "provider_cost_ledger",
    "usage_events",
    "customer_notification_outbox",
    "guardrail_notification_states",
)

APPEND_ONLY_TABLES = (
    "entitlement_snapshot_inbox",
    "spend_guardrail_events",
    "provider_cost_ledger",
    "usage_events",
)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. Extend workspaces: lifecycle and budget_currency with backfill
    # ------------------------------------------------------------------
    with op.batch_alter_table("workspaces", schema=None) as batch_op:
        batch_op.add_column(sa.Column("lifecycle", sa.String(32), nullable=True))
        batch_op.add_column(sa.Column("budget_currency", sa.String(3), nullable=True))

    op.execute(
        "UPDATE workspaces SET lifecycle = CASE "
        "WHEN suspended = true THEN 'suspended' ELSE 'active' END"
    )
    op.execute("UPDATE workspaces SET budget_currency = default_currency")

    with op.batch_alter_table("workspaces", schema=None) as batch_op:
        batch_op.alter_column("lifecycle", nullable=False)
        batch_op.alter_column("budget_currency", nullable=False)
        # CHECK: lifecycle must be one of the known values
        batch_op.create_check_constraint(
            "workspace_lifecycle_valid",
            condition="lifecycle IN ('provisioning', 'active', 'suspended', "
            "'deletion_pending', 'deleted')",
        )
        batch_op.create_check_constraint(
            "workspace_budget_currency_format",
            condition="length(budget_currency) = 3 "
            "AND substr(budget_currency, 1, 1) BETWEEN 'A' AND 'Z' "
            "AND substr(budget_currency, 2, 1) BETWEEN 'A' AND 'Z' "
            "AND substr(budget_currency, 3, 1) BETWEEN 'A' AND 'Z'",
        )

    # ------------------------------------------------------------------
    # 2. Extend operation_records
    # ------------------------------------------------------------------
    with op.batch_alter_table("operation_records", schema=None) as batch_op:
        batch_op.add_column(sa.Column("semantic_key_hash", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("request_fingerprint_hash", sa.String(64), nullable=True))
        batch_op.add_column(
            sa.Column("admission_contract_version", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.create_unique_constraint(
            "uq_operation_semantic_key", ["workspace_id", "semantic_key_hash"]
        )

    # ------------------------------------------------------------------
    # 3. Extend jobs
    # ------------------------------------------------------------------
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("operation_id", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("entitlement_version_at_enqueue", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("execution_contract_version", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.create_foreign_key(
            "fk_jobs_workspace_operation_operation_records",
            "operation_records",
            ["workspace_id", "operation_id"],
            ["workspace_id", "id"],
            ondelete="RESTRICT",
        )

    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.create_check_constraint(
            "jobs_v1_requires_operation_and_entitlement_version",
            condition="execution_contract_version = 0 OR "
            "(operation_id IS NOT NULL AND entitlement_version_at_enqueue IS NOT NULL)",
        )

    # ------------------------------------------------------------------
    # 3b. Extend operation_state_events: add workspace_id with backfill
    # ------------------------------------------------------------------
    with op.batch_alter_table("operation_state_events", schema=None) as batch_op:
        batch_op.add_column(sa.Column("workspace_id", sa.String(64), nullable=True))

    # Disable append-only trigger to allow backfill UPDATE
    if _is_postgres():
        op.execute(
            "ALTER TABLE operation_state_events DISABLE TRIGGER "
            "operation_state_events_reject_mutation"
        )

    op.execute(
        "UPDATE operation_state_events SET workspace_id = "
        "(SELECT workspace_id FROM operation_records "
        "WHERE operation_records.id = operation_state_events.operation_id)"
    )

    if _is_postgres():
        op.execute(
            "ALTER TABLE operation_state_events ENABLE TRIGGER "
            "operation_state_events_reject_mutation"
        )

    with op.batch_alter_table("operation_state_events", schema=None) as batch_op:
        batch_op.alter_column("workspace_id", nullable=False)
        batch_op.create_index(
            "ix_operation_state_events_workspace_id", ["workspace_id"], unique=False
        )
        batch_op.create_foreign_key(
            "fk_operation_state_events_workspace_id_workspaces",
            "workspaces",
            ["workspace_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.drop_constraint(
            "fk_operation_state_events_operation_id_operation_records",
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            "fk_operation_state_events_workspace_operation_operation_records",
            "operation_records",
            ["workspace_id", "operation_id"],
            ["workspace_id", "id"],
            ondelete="CASCADE",
        )

    # On SQLite, batch_alter_table rebuilds the table and loses triggers.
    # Re-create the append-only guards.
    if not _is_postgres():
        op.execute(
            "CREATE TRIGGER IF NOT EXISTS operation_state_events_reject_update "
            "BEFORE UPDATE ON operation_state_events "
            "BEGIN SELECT RAISE(ABORT, 'operation_state_events is append-only'); END"
        )
        op.execute(
            "CREATE TRIGGER IF NOT EXISTS operation_state_events_reject_delete "
            "BEFORE DELETE ON operation_state_events "
            "BEGIN SELECT RAISE(ABORT, 'operation_state_events is append-only'); END"
        )

    # ------------------------------------------------------------------
    # 4-16. Create new tables (autogenerated, dependency-ordered)
    # ------------------------------------------------------------------
    op.create_table('credential_versions',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('credential_key', sa.String(length=128), nullable=False),
    sa.Column('version_no', sa.Integer(), nullable=False),
    sa.Column('provider', sa.String(length=64), nullable=False),
    sa.Column('display_label', sa.String(length=256), nullable=True),
    sa.Column('state', sa.String(length=32), nullable=False, server_default='active'),
    sa.Column('encrypted_secret', sa.LargeBinary(), nullable=False),
    sa.Column('encrypted_data_key', sa.LargeBinary(), nullable=False),
    sa.Column('encryption_key_id', sa.String(length=128), nullable=False),
    sa.Column('encryption_algorithm', sa.String(length=32), nullable=False, server_default='aes-256-gcm'),
    sa.Column('nonce', sa.LargeBinary(), nullable=False),
    sa.Column('aad_schema_version', sa.Integer(), nullable=False, server_default='1'),
    sa.Column('created_by', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('rotated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('rotated_by', sa.String(length=100), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_by', sa.String(length=100), nullable=True),
    sa.Column('revoke_reason', sa.String(length=256), nullable=True),
    sa.CheckConstraint("state IN ('active', 'rotated', 'revoked')", name='credential_version_state_valid'),
    sa.CheckConstraint('version_no >= 1', name='credential_version_no_positive'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_credential_versions_workspace_id_workspaces', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'credential_key', 'version_no', name='uq_credential_version_workspace_key_version'),
    sa.UniqueConstraint('workspace_id', 'id', name='uq_credential_versions_workspace_id_id'),
    )
    with op.batch_alter_table('credential_versions', schema=None) as batch_op:
        batch_op.create_index('ix_credential_versions_workspace_id', ['workspace_id'], unique=False)
        batch_op.create_index('uq_credential_version_active', ['workspace_id', 'credential_key'], unique=True, sqlite_where=sa.text("state = 'active'"), postgresql_where=sa.text("state = 'active'"))

    op.create_table('customer_notification_outbox',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('notification_type', sa.String(length=64), nullable=False),
    sa.Column('dimension', sa.String(length=64), nullable=False),
    sa.Column('threshold', sa.Numeric(precision=24, scale=12), nullable=False),
    sa.Column('guardrail_revision', sa.Integer(), nullable=False),
    sa.Column('platform_policy_version', sa.Integer(), nullable=False),
    sa.Column('window_kind', sa.String(length=32), nullable=False),
    sa.Column('window_key', sa.String(length=64), nullable=False),
    sa.Column('period_start', sa.DateTime(timezone=True), nullable=True),
    sa.Column('period_end', sa.DateTime(timezone=True), nullable=True),
    sa.Column('crossing_sequence', sa.Integer(), nullable=False, server_default='0'),
    sa.Column('deduplication_key', sa.String(length=256), nullable=False),
    sa.Column('customer_payload_json', sa.JSON(), nullable=False),
    sa.Column('delivery_state', sa.String(length=32), nullable=False, server_default='pending'),
    sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('delivery_lease_id', sa.String(length=64), nullable=True),
    sa.Column('delivery_lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("delivery_state IN ('pending', 'delivering', 'delivered', 'failed', 'dead_letter')", name='delivery_state_valid'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_customer_notification_outbox_workspace_id_workspaces', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'deduplication_key', name='uq_notification_outbox_workspace_dedup_key'),
    sa.UniqueConstraint('workspace_id', 'id', name='uq_notification_outbox_workspace_id_id'),
    )
    with op.batch_alter_table('customer_notification_outbox', schema=None) as batch_op:
        batch_op.create_index('ix_customer_notification_outbox_workspace_id', ['workspace_id'], unique=False)

    op.create_table('entitlement_snapshot_inbox',
    sa.Column('snapshot_id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('entitlement_version', sa.Integer(), nullable=False),
    sa.Column('lease_sequence', sa.Integer(), nullable=False),
    sa.Column('projection_version', sa.Integer(), nullable=False),
    sa.Column('schema_version', sa.Integer(), nullable=False),
    sa.Column('delivery_kind', sa.String(length=32), nullable=False),
    sa.Column('canonical_envelope', sa.LargeBinary(), nullable=False),
    sa.Column('envelope_sha256', sa.String(length=64), nullable=False),
    sa.Column('semantic_hash', sa.String(length=64), nullable=False),
    sa.Column('signature', sa.String(length=128), nullable=False),
    sa.Column('signature_key_id', sa.String(length=64), nullable=False),
    sa.Column('signature_algorithm', sa.String(length=32), nullable=False, server_default='hmac-sha256'),
    sa.Column('outcome', sa.String(length=32), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('projection_applied_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("delivery_kind IN ('push', 'full_repair')", name='delivery_kind_valid'),
    sa.CheckConstraint("outcome IN ('applied', 'duplicate', 'stale', 'version_jump_paused', 'conflict')", name='entitlement_inbox_outcome_valid'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_entitlement_snapshot_inbox_workspace_id_workspaces', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('snapshot_id'),
    sa.UniqueConstraint('workspace_id', 'entitlement_version', 'lease_sequence', name='uq_entitlement_inbox_workspace_version_lease'),
    sa.UniqueConstraint('workspace_id', 'snapshot_id', name='uq_entitlement_inbox_workspace_snapshot'),
    )
    with op.batch_alter_table('entitlement_snapshot_inbox', schema=None) as batch_op:
        batch_op.create_index('ix_entitlement_snapshot_inbox_workspace_id', ['workspace_id'], unique=False)

    op.create_table('guardrail_notification_states',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('dimension', sa.String(length=64), nullable=False),
    sa.Column('threshold', sa.Numeric(precision=24, scale=12), nullable=False),
    sa.Column('guardrail_revision', sa.Integer(), nullable=False),
    sa.Column('platform_policy_version', sa.Integer(), nullable=False),
    sa.Column('window_kind', sa.String(length=32), nullable=False),
    sa.Column('window_key', sa.String(length=64), nullable=False),
    sa.Column('period_start', sa.DateTime(timezone=True), nullable=True),
    sa.Column('period_end', sa.DateTime(timezone=True), nullable=True),
    sa.Column('state', sa.String(length=32), nullable=False, server_default='below'),
    sa.Column('crossing_sequence', sa.Integer(), nullable=False, server_default='0'),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("state IN ('below', 'notified')", name='state_valid'),
    sa.CheckConstraint('crossing_sequence >= 0', name='crossing_seq_nonnegative'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_guardrail_notification_states_workspace_id_workspaces', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'guardrail_revision', 'platform_policy_version', 'dimension', 'window_key', 'threshold', name='uq_guardrail_notification_state_composite'),
    sa.UniqueConstraint('workspace_id', 'id', name='uq_guardrail_notification_state_workspace_id_id'),
    )
    with op.batch_alter_table('guardrail_notification_states', schema=None) as batch_op:
        batch_op.create_index('ix_guardrail_notification_states_workspace_id', ['workspace_id'], unique=False)

    op.create_table('spend_guardrail_events',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('revision_before', sa.Integer(), nullable=True),
    sa.Column('revision_after', sa.Integer(), nullable=False),
    sa.Column('old_values_json', sa.JSON(), nullable=True),
    sa.Column('new_values_json', sa.JSON(), nullable=False),
    sa.Column('old_budget_currency', sa.String(length=3), nullable=True),
    sa.Column('new_budget_currency', sa.String(length=3), nullable=False),
    sa.Column('reason', sa.String(length=256), nullable=False),
    sa.Column('actor_id', sa.String(length=100), nullable=False),
    sa.Column('audit_event_id', sa.String(length=64), nullable=True),
    sa.Column('mfa_evidence_ref', sa.String(length=256), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_spend_guardrail_events_workspace_id_workspaces', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('spend_guardrail_events', schema=None) as batch_op:
        batch_op.create_index('ix_spend_guardrail_events_workspace_id', ['workspace_id'], unique=False)

    op.create_table('spend_guardrails',
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False, server_default='1'),
    sa.Column('budget_currency', sa.String(length=3), nullable=False, server_default='USD'),
    sa.Column('per_operation_limit', sa.Numeric(precision=24, scale=12), nullable=False),
    sa.Column('rolling_24h_limit', sa.Numeric(precision=24, scale=12), nullable=False),
    sa.Column('period_limit', sa.Numeric(precision=24, scale=12), nullable=False),
    sa.Column('outstanding_exposure_limit', sa.Numeric(precision=24, scale=12), nullable=False),
    sa.Column('platform_policy_version', sa.Integer(), nullable=False, server_default='1'),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_by', sa.String(length=100), nullable=False),
    sa.CheckConstraint('per_operation_limit >= 0 AND rolling_24h_limit >= 0 AND period_limit >= 0 AND outstanding_exposure_limit >= 0', name='spend_guardrail_amounts_nonneg'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_spend_guardrails_workspace_id_workspaces', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('workspace_id'),
    )

    op.create_table('usage_events',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('metric_key', sa.String(length=128), nullable=False),
    sa.Column('measurement_kind', sa.String(length=16), nullable=False),
    sa.Column('direction', sa.String(length=16), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=24, scale=12), nullable=False),
    sa.Column('unit', sa.String(length=32), nullable=False),
    sa.Column('execution_mode', sa.String(length=32), nullable=False),
    sa.Column('source_resource_type', sa.String(length=64), nullable=True),
    sa.Column('source_resource_id', sa.String(length=64), nullable=True),
    sa.Column('semantic_key_hash', sa.String(length=64), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_usage_events_workspace_id_workspaces', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'semantic_key_hash', name='uq_usage_events_workspace_semantic_key'),
    )
    with op.batch_alter_table('usage_events', schema=None) as batch_op:
        batch_op.create_index('ix_usage_events_workspace_id', ['workspace_id'], unique=False)

    op.create_table('credential_bindings',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('binding_scope', sa.String(length=64), nullable=False),
    sa.Column('provider', sa.String(length=64), nullable=False),
    sa.Column('credential_version_id', sa.String(length=64), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False, server_default='1'),
    sa.Column('bound_by', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id', 'credential_version_id'], ['credential_versions.workspace_id', 'credential_versions.id'], name='fk_credential_bindings_workspace_cred_credential_versions', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_credential_bindings_workspace_id_workspaces', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'binding_scope', 'provider', name='uq_credential_binding_workspace_scope_provider'),
    )
    with op.batch_alter_table('credential_bindings', schema=None) as batch_op:
        batch_op.create_index('ix_credential_bindings_workspace_id', ['workspace_id'], unique=False)

    op.create_table('entitlement_projections',
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('current_snapshot_id', sa.String(length=64), nullable=True),
    sa.Column('entitlement_version', sa.Integer(), nullable=False, server_default='0'),
    sa.Column('lease_sequence', sa.Integer(), nullable=False, server_default='0'),
    sa.Column('projection_version', sa.Integer(), nullable=False, server_default='0'),
    sa.Column('schema_version', sa.Integer(), nullable=False, server_default='2'),
    sa.Column('plan_version_id', sa.String(length=64), nullable=True),
    sa.Column('source_refs_json', sa.JSON(), nullable=True),
    sa.Column('period_start', sa.DateTime(timezone=True), nullable=True),
    sa.Column('period_end', sa.DateTime(timezone=True), nullable=True),
    sa.Column('period_anchor', sa.DateTime(timezone=True), nullable=True),
    sa.Column('resolved_entitlements_json', sa.JSON(), nullable=True),
    sa.Column('restriction_summary_json', sa.JSON(), nullable=True),
    sa.Column('semantic_hash', sa.String(length=64), nullable=False, server_default=''),
    sa.Column('entitlement_registry_version', sa.Integer(), nullable=False, server_default='1'),
    sa.Column('resolution_policy_version', sa.Integer(), nullable=False, server_default='1'),
    sa.Column('platform_limits_version', sa.Integer(), nullable=False, server_default='1'),
    sa.Column('issued_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('valid_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('control_plane_audit_event_id', sa.String(length=64), nullable=True),
    sa.Column('signature', sa.String(length=128), nullable=True),
    sa.Column('signature_key_id', sa.String(length=64), nullable=True),
    sa.Column('signature_algorithm', sa.String(length=32), nullable=True),
    sa.Column('applied_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('entitlement_version >= 0 AND lease_sequence >= 0 AND projection_version >= 0 AND schema_version >= 0 AND entitlement_registry_version >= 0 AND resolution_policy_version >= 0 AND platform_limits_version >= 0', name='versions_nonnegative'),
    sa.CheckConstraint('valid_until IS NULL OR issued_at IS NULL OR valid_until > issued_at', name='entitlement_projection_valid_window'),
    sa.ForeignKeyConstraint(['workspace_id', 'current_snapshot_id'], ['entitlement_snapshot_inbox.workspace_id', 'entitlement_snapshot_inbox.snapshot_id'], name='fk_entitlement_projections_workspace_snapshot_inbox'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_entitlement_projections_workspace_id_workspaces', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('workspace_id'),
    )

    op.create_table('capacity_reservations',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('operation_id', sa.String(length=64), nullable=False),
    sa.Column('job_id', sa.String(length=64), nullable=False),
    sa.Column('entitlement_key', sa.String(length=128), nullable=False),
    sa.Column('quantity', sa.Integer(), nullable=False, server_default='1'),
    sa.Column('unit', sa.String(length=32), nullable=False),
    sa.Column('acquisition_phase', sa.String(length=32), nullable=False),
    sa.Column('semantic_key_hash', sa.String(length=64), nullable=False),
    sa.Column('state', sa.String(length=32), nullable=False, server_default='reserved'),
    sa.Column('entitlement_version', sa.Integer(), nullable=False),
    sa.Column('lease_sequence', sa.Integer(), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("acquisition_phase IN ('admission', 'execution')", name='capacity_reservation_phase_valid'),
    sa.CheckConstraint("state IN ('reserved', 'committed', 'released', 'expired')", name='capacity_reservation_state_valid'),
    sa.CheckConstraint('quantity >= 1', name='capacity_reservation_quantity_positive'),
    sa.ForeignKeyConstraint(['workspace_id', 'job_id'], ['jobs.workspace_id', 'jobs.id'], name='fk_capacity_reservations_workspace_job_jobs', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['workspace_id', 'operation_id'], ['operation_records.workspace_id', 'operation_records.id'], name='fk_capacity_reservations_workspace_operation_operation_records', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_capacity_reservations_workspace_id_workspaces', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'id', name='uq_capacity_reservation_workspace_id_id'),
    sa.UniqueConstraint('workspace_id', 'semantic_key_hash', name='uq_capacity_reservation_workspace_semantic_key'),
    )
    with op.batch_alter_table('capacity_reservations', schema=None) as batch_op:
        batch_op.create_index('ix_capacity_reservations_workspace_id', ['workspace_id'], unique=False)

    op.create_table('spend_reservations',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('operation_id', sa.String(length=64), nullable=False),
    sa.Column('job_id', sa.String(length=64), nullable=False),
    sa.Column('semantic_key_hash', sa.String(length=64), nullable=False),
    sa.Column('state', sa.String(length=32), nullable=False, server_default='reserved'),
    sa.Column('provider', sa.String(length=64), nullable=False),
    sa.Column('execution_mode', sa.String(length=32), nullable=False),
    sa.Column('provider_currency', sa.String(length=3), nullable=False),
    sa.Column('maximum_provider_amount', sa.Numeric(precision=24, scale=12), nullable=False),
    sa.Column('budget_currency', sa.String(length=3), nullable=False),
    sa.Column('maximum_budget_amount', sa.Numeric(precision=24, scale=12), nullable=False),
    sa.Column('outstanding_budget_amount', sa.Numeric(precision=24, scale=12), nullable=False),
    sa.Column('period_start', sa.DateTime(timezone=True), nullable=True),
    sa.Column('period_end', sa.DateTime(timezone=True), nullable=True),
    sa.Column('entitlement_version', sa.Integer(), nullable=False),
    sa.Column('lease_sequence', sa.Integer(), nullable=False),
    sa.Column('guardrail_revision', sa.Integer(), nullable=False),
    sa.Column('platform_policy_version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('settled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('released_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("state IN ('reserved', 'partially_settled', 'settled', 'released', 'uncertain')", name='spend_reservation_state_valid'),
    sa.CheckConstraint('maximum_budget_amount >= 0', name='spend_reservation_max_budget_nonneg'),
    sa.CheckConstraint('maximum_provider_amount >= 0', name='spend_reservation_max_provider_nonneg'),
    sa.CheckConstraint('outstanding_budget_amount <= maximum_budget_amount', name='spend_reservation_outstanding_le_maximum'),
    sa.CheckConstraint('outstanding_budget_amount >= 0', name='spend_reservation_outstanding_nonneg'),
    sa.ForeignKeyConstraint(['workspace_id', 'job_id'], ['jobs.workspace_id', 'jobs.id'], name='fk_spend_reservations_workspace_job_jobs', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['workspace_id', 'operation_id'], ['operation_records.workspace_id', 'operation_records.id'], name='fk_spend_reservations_workspace_operation_operation_records', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_spend_reservations_workspace_id_workspaces', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'id', name='uq_spend_reservation_workspace_id_id'),
    sa.UniqueConstraint('workspace_id', 'semantic_key_hash', name='uq_spend_reservation_workspace_semantic_key'),
    )
    with op.batch_alter_table('spend_reservations', schema=None) as batch_op:
        batch_op.create_index('ix_spend_reservations_workspace_id', ['workspace_id'], unique=False)

    op.create_table('provider_call_attempts',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('operation_id', sa.String(length=64), nullable=False),
    sa.Column('job_id', sa.String(length=64), nullable=False),
    sa.Column('spend_reservation_id', sa.String(length=64), nullable=False),
    sa.Column('call_semantic_key_hash', sa.String(length=64), nullable=False),
    sa.Column('attempt_no', sa.Integer(), nullable=False),
    sa.Column('execution_mode', sa.String(length=32), nullable=False),
    sa.Column('provider', sa.String(length=64), nullable=False),
    sa.Column('model', sa.String(length=128), nullable=True),
    sa.Column('request_fingerprint_hash', sa.String(length=64), nullable=True),
    sa.Column('provider_idempotency_key', sa.String(length=128), nullable=True),
    sa.Column('credential_version_id', sa.String(length=64), nullable=True),
    sa.Column('dispatch_state', sa.String(length=32), nullable=False, server_default='prepared'),
    sa.Column('provider_request_id', sa.String(length=128), nullable=True),
    sa.Column('provider_evidence_ref', sa.String(length=256), nullable=True),
    sa.Column('error_code', sa.String(length=64), nullable=True),
    sa.Column('error_detail', sa.Text(), nullable=True),
    sa.Column('entitlement_version', sa.Integer(), nullable=True),
    sa.Column('lease_sequence', sa.Integer(), nullable=True),
    sa.Column('prepared_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('dispatching_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("dispatch_state IN ('prepared', 'dispatching', 'succeeded', 'failed', 'uncertain')", name='dispatch_state_valid'),
    sa.CheckConstraint('attempt_no >= 0', name='provider_call_attempt_no_nonneg'),
    sa.ForeignKeyConstraint(['workspace_id', 'credential_version_id'], ['credential_versions.workspace_id', 'credential_versions.id'], name='fk_provider_call_attempts_workspace_cred_credential_versions', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['workspace_id', 'job_id'], ['jobs.workspace_id', 'jobs.id'], name='fk_provider_call_attempts_workspace_job_jobs', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['workspace_id', 'operation_id'], ['operation_records.workspace_id', 'operation_records.id'], name='fk_provider_call_attempts_workspace_operation_operation_records', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['workspace_id', 'spend_reservation_id'], ['spend_reservations.workspace_id', 'spend_reservations.id'], name='fk_provider_call_attempts_ws_spend_reservation', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_provider_call_attempts_workspace_id_workspaces', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'call_semantic_key_hash', 'attempt_no', name='uq_provider_call_attempt_workspace_key_attempt'),
    sa.UniqueConstraint('workspace_id', 'id', name='uq_provider_call_attempt_workspace_id_id'),
    )
    with op.batch_alter_table('provider_call_attempts', schema=None) as batch_op:
        batch_op.create_index('ix_provider_call_attempts_workspace_id', ['workspace_id'], unique=False)

    op.create_table('provider_cost_ledger',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('operation_id', sa.String(length=64), nullable=False),
    sa.Column('job_id', sa.String(length=64), nullable=False),
    sa.Column('spend_reservation_id', sa.String(length=64), nullable=False),
    sa.Column('credential_version_id', sa.String(length=64), nullable=True),
    sa.Column('entry_type', sa.String(length=32), nullable=False),
    sa.Column('direction', sa.String(length=16), nullable=False),
    sa.Column('execution_mode', sa.String(length=32), nullable=False),
    sa.Column('semantic_key_hash', sa.String(length=64), nullable=False),
    sa.Column('provider', sa.String(length=64), nullable=False),
    sa.Column('model', sa.String(length=128), nullable=True),
    sa.Column('provider_amount', sa.Numeric(precision=24, scale=12), nullable=False),
    sa.Column('provider_currency', sa.String(length=3), nullable=False),
    sa.Column('budget_amount', sa.Numeric(precision=24, scale=12), nullable=False),
    sa.Column('budget_currency', sa.String(length=3), nullable=False),
    sa.Column('exchange_rate', sa.Numeric(precision=24, scale=12), nullable=False, server_default='1'),
    sa.Column('exchange_rate_source', sa.String(length=64), nullable=True),
    sa.Column('exchange_rate_observed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('price_rule_version', sa.Integer(), nullable=True),
    sa.Column('related_entry_id', sa.String(length=64), nullable=True),
    sa.Column('adjusts_entry_id', sa.String(length=64), nullable=True),
    sa.Column('provider_evidence_ref', sa.String(length=256), nullable=True),
    sa.Column('reconciliation_evidence_ref', sa.String(length=256), nullable=True),
    sa.Column('audit_event_id', sa.String(length=64), nullable=True),
    sa.Column('audit_actor_id', sa.String(length=100), nullable=True),
    sa.Column('simulated_amount', sa.Numeric(precision=24, scale=12), nullable=True),
    sa.Column('simulated_currency', sa.String(length=3), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("direction IN ('increase', 'decrease')", name='provider_cost_ledger_direction_valid'),
    sa.CheckConstraint("entry_type IN ('quote', 'reservation', 'release', 'reported_cost', 'estimated_settlement', 'adjustment')", name='provider_cost_ledger_entry_type_valid'),
    sa.CheckConstraint("execution_mode IN ('byok', 'sandbox', 'mock')", name='execution_mode_valid'),
    sa.CheckConstraint('budget_amount >= 0', name='budget_amount_nonnegative'),
    sa.CheckConstraint('exchange_rate > 0', name='exchange_rate_positive'),
    sa.CheckConstraint('provider_amount >= 0', name='provider_amount_nonnegative'),
    sa.ForeignKeyConstraint(['workspace_id', 'credential_version_id'], ['credential_versions.workspace_id', 'credential_versions.id'], name='fk_provider_cost_ledger_workspace_cred_credential_versions', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['workspace_id', 'job_id'], ['jobs.workspace_id', 'jobs.id'], name='fk_provider_cost_ledger_workspace_job_jobs', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['workspace_id', 'operation_id'], ['operation_records.workspace_id', 'operation_records.id'], name='fk_provider_cost_ledger_workspace_operation_operation_records', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['workspace_id', 'spend_reservation_id'], ['spend_reservations.workspace_id', 'spend_reservations.id'], name='fk_provider_cost_ledger_ws_spend_reservation', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_provider_cost_ledger_workspace_id_workspaces', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'id', name='uq_provider_cost_ledger_workspace_id_id'),
    sa.UniqueConstraint('workspace_id', 'semantic_key_hash', name='uq_provider_cost_ledger_workspace_semantic_key'),
    )
    with op.batch_alter_table('provider_cost_ledger', schema=None) as batch_op:
        batch_op.create_index('ix_provider_cost_ledger_workspace_id', ['workspace_id'], unique=False)

    # ==================================================================
    # PostgreSQL-only: triggers, function, RLS, grants
    # ==================================================================
    if _is_postgres():
        _install_append_only_triggers()
        _install_projection_v2_function()
        _install_rls_policies()
        _install_credential_metadata_view()
        _install_role_grants()


def _install_append_only_triggers() -> None:
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


def _install_projection_v2_function() -> None:
    op.execute("""
CREATE FUNCTION marketai_apply_workspace_projection_v2(
    p_snapshot_id TEXT, p_workspace_id TEXT, p_workspace_name TEXT,
    p_authorization_epoch INT, p_entitlement_version INT, p_lease_sequence INT,
    p_projection_version INT, p_lifecycle TEXT, p_suspended BOOLEAN,
    p_schema_version INT, p_delivery_kind TEXT, p_canonical_envelope BYTEA,
    p_envelope_sha256 TEXT, p_semantic_hash TEXT, p_signature TEXT,
    p_signature_key_id TEXT, p_signature_algorithm TEXT, p_plan_version_id TEXT,
    p_source_refs_json JSONB, p_period_start TIMESTAMPTZ, p_period_end TIMESTAMPTZ,
    p_period_anchor TIMESTAMPTZ, p_resolved_entitlements_json JSONB,
    p_restriction_summary_json JSONB, p_entitlement_registry_version INT,
    p_resolution_policy_version INT, p_platform_limits_version INT,
    p_issued_at TIMESTAMPTZ, p_valid_until TIMESTAMPTZ,
    p_control_plane_audit_event_id TEXT
) RETURNS TEXT SECURITY DEFINER SET search_path = '' LANGUAGE plpgsql AS $$
DECLARE
    v_current_ev INT; v_current_ls INT; v_current_pv INT;
    v_existing_sha TEXT; v_result TEXT;
BEGIN
    SELECT entitlement_version, lease_sequence, projection_version
    INTO v_current_ev, v_current_ls, v_current_pv
    FROM public.entitlement_projections WHERE workspace_id = p_workspace_id FOR UPDATE;

    IF NOT FOUND THEN v_result := 'applied';
    ELSIF p_entitlement_version < v_current_ev THEN v_result := 'stale';
    ELSIF p_entitlement_version = v_current_ev AND p_lease_sequence <= v_current_ls THEN
        SELECT envelope_sha256 INTO v_existing_sha FROM public.entitlement_snapshot_inbox
        WHERE workspace_id = p_workspace_id AND entitlement_version = p_entitlement_version
          AND lease_sequence = p_lease_sequence;
        IF FOUND AND v_existing_sha != p_envelope_sha256 THEN v_result := 'conflict';
        ELSE v_result := 'duplicate'; END IF;
    ELSIF p_entitlement_version = v_current_ev AND p_lease_sequence > v_current_ls THEN
        IF p_semantic_hash != (SELECT semantic_hash FROM public.entitlement_projections WHERE workspace_id = p_workspace_id) THEN
            v_result := 'conflict';
        ELSE v_result := 'applied'; END IF;
    ELSIF p_entitlement_version = v_current_ev + 1 THEN v_result := 'applied';
    ELSIF p_delivery_kind = 'full_repair' THEN v_result := 'applied';
    ELSE v_result := 'version_jump_paused'; END IF;

    INSERT INTO public.entitlement_snapshot_inbox (
        snapshot_id, workspace_id, entitlement_version, lease_sequence,
        projection_version, schema_version, delivery_kind,
        canonical_envelope, envelope_sha256, semantic_hash,
        signature, signature_key_id, signature_algorithm,
        outcome, received_at, processed_at, projection_applied_at
    ) VALUES (
        p_snapshot_id, p_workspace_id, p_entitlement_version, p_lease_sequence,
        p_projection_version, p_schema_version, p_delivery_kind,
        p_canonical_envelope, p_envelope_sha256, p_semantic_hash,
        p_signature, p_signature_key_id, p_signature_algorithm,
        v_result, now(), now(),
        CASE WHEN v_result = 'applied' THEN now() ELSE NULL END
    ) ON CONFLICT (workspace_id, entitlement_version, lease_sequence) DO NOTHING;

    IF v_result = 'applied' THEN
        INSERT INTO public.entitlement_projections (
            workspace_id, current_snapshot_id, entitlement_version, lease_sequence,
            projection_version, schema_version, plan_version_id, source_refs_json,
            period_start, period_end, period_anchor, resolved_entitlements_json,
            restriction_summary_json, semantic_hash, entitlement_registry_version,
            resolution_policy_version, platform_limits_version, issued_at, valid_until,
            control_plane_audit_event_id, signature, signature_key_id,
            signature_algorithm, applied_at
        ) VALUES (
            p_workspace_id, p_snapshot_id, p_entitlement_version, p_lease_sequence,
            p_projection_version, p_schema_version, p_plan_version_id, p_source_refs_json,
            p_period_start, p_period_end, p_period_anchor, p_resolved_entitlements_json,
            p_restriction_summary_json, p_semantic_hash, p_entitlement_registry_version,
            p_resolution_policy_version, p_platform_limits_version, p_issued_at, p_valid_until,
            p_control_plane_audit_event_id, p_signature, p_signature_key_id,
            p_signature_algorithm, now()
        ) ON CONFLICT (workspace_id) DO UPDATE SET
            current_snapshot_id = EXCLUDED.current_snapshot_id,
            entitlement_version = EXCLUDED.entitlement_version,
            lease_sequence = EXCLUDED.lease_sequence,
            projection_version = EXCLUDED.projection_version,
            schema_version = EXCLUDED.schema_version,
            plan_version_id = EXCLUDED.plan_version_id,
            source_refs_json = EXCLUDED.source_refs_json,
            period_start = EXCLUDED.period_start, period_end = EXCLUDED.period_end,
            period_anchor = EXCLUDED.period_anchor,
            resolved_entitlements_json = EXCLUDED.resolved_entitlements_json,
            restriction_summary_json = EXCLUDED.restriction_summary_json,
            semantic_hash = EXCLUDED.semantic_hash,
            entitlement_registry_version = EXCLUDED.entitlement_registry_version,
            resolution_policy_version = EXCLUDED.resolution_policy_version,
            platform_limits_version = EXCLUDED.platform_limits_version,
            issued_at = EXCLUDED.issued_at, valid_until = EXCLUDED.valid_until,
            control_plane_audit_event_id = EXCLUDED.control_plane_audit_event_id,
            signature = EXCLUDED.signature, signature_key_id = EXCLUDED.signature_key_id,
            signature_algorithm = EXCLUDED.signature_algorithm, applied_at = now();

        UPDATE public.workspaces SET
            name = p_workspace_name,
            authorization_epoch = GREATEST(authorization_epoch, p_authorization_epoch),
            entitlement_version = p_entitlement_version,
            suspended = p_suspended, lifecycle = p_lifecycle,
            projection_version = p_projection_version
        WHERE id = p_workspace_id;
    END IF;
    RETURN v_result;
END;
$$;
    """)


def _install_credential_metadata_view() -> None:
    """Security-barrier view exposing only non-secret credential columns."""
    op.execute("""
CREATE VIEW credential_metadata WITH (security_barrier = true) AS
SELECT id, workspace_id, credential_key, version_no, provider, display_label,
       state, encryption_key_id, encryption_algorithm, aad_schema_version,
       created_by, created_at, rotated_at, rotated_by, revoked_at, revoked_by, revoke_reason
FROM credential_versions;
    """)


def _install_rls_policies() -> None:
    for table in NEW_TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"')
        op.execute(
            f'CREATE POLICY tenant_isolation ON "{table}" '
            "USING (marketai_context_is_current(workspace_id)) "
            "WITH CHECK (marketai_context_is_current(workspace_id))"
        )


def _install_role_grants() -> None:
    # Function grants
    op.execute(
        "REVOKE ALL ON FUNCTION marketai_apply_workspace_projection_v2 FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION marketai_apply_workspace_projection_v2 "
        "TO marketai_domain_api"
    )

    # -- API role --
    # SELECT: no direct access to credential_versions / credential_bindings
    # base tables (ciphertext exposure); use credential_metadata view instead.
    # entitlement_snapshot_inbox and entitlement_projections are written only
    # through the SECURITY DEFINER function.
    for table in (
        "spend_guardrails",
        "spend_reservations",
        "capacity_reservations",
        "customer_notification_outbox",
        "guardrail_notification_states",
        "entitlement_projections",
    ):
        op.execute(f"GRANT SELECT ON TABLE {table} TO marketai_domain_api")
    op.execute("GRANT SELECT ON credential_metadata TO marketai_domain_api")

    # INSERT: no direct INSERT on entitlement_snapshot_inbox or
    # entitlement_projections -- those are written only through
    # marketai_apply_workspace_projection_v2.
    for table in (
        "credential_versions",
        "credential_bindings",
        "spend_guardrails",
        "spend_guardrail_events",
        "spend_reservations",
        "capacity_reservations",
        "usage_events",
        "customer_notification_outbox",
        "guardrail_notification_states",
        "provider_cost_ledger",
    ):
        op.execute(f"GRANT INSERT ON TABLE {table} TO marketai_domain_api")

    # UPDATE: no UPDATE on entitlement_projections (projection function only),
    # provider_call_attempts (Worker-only CAS UPDATE), or
    # spend_reservations/capacity_reservations (API creates; Worker CAS updates).
    for table in (
        "credential_versions",
        "credential_bindings",
        "spend_guardrails",
        "customer_notification_outbox",
        "guardrail_notification_states",
    ):
        op.execute(f"GRANT UPDATE ON TABLE {table} TO marketai_domain_api")

    # -- Worker role --
    # SELECT: credential_versions for envelope access at execution time,
    # plus execution-relevant state tables.
    for table in (
        "credential_versions",
        "spend_reservations",
        "capacity_reservations",
        "entitlement_projections",
        "provider_call_attempts",
    ):
        op.execute(f"GRANT SELECT ON TABLE {table} TO marketai_domain_worker")

    for table in (
        "provider_call_attempts",
        "provider_cost_ledger",
        "usage_events",
    ):
        op.execute(f"GRANT INSERT ON TABLE {table} TO marketai_domain_worker")

    # UPDATE: CAS state transitions only; no customer_notification_outbox.
    for table in (
        "spend_reservations",
        "capacity_reservations",
        "provider_call_attempts",
    ):
        op.execute(f"GRANT UPDATE ON TABLE {table} TO marketai_domain_worker")

    # -- Readonly role: SELECT on safe tables only (no ciphertext) --
    for table in (
        "spend_guardrails",
        "entitlement_projections",
        "usage_events",
        "customer_notification_outbox",
        "guardrail_notification_states",
        "provider_cost_ledger",
    ):
        op.execute(f"GRANT SELECT ON TABLE {table} TO marketai_domain_readonly")
    op.execute("GRANT SELECT ON credential_metadata TO marketai_domain_readonly")


def downgrade() -> None:
    if _is_postgres():
        # Revoke grants (including credential_metadata view)
        for table in NEW_TABLES:
            op.execute(
                f"REVOKE ALL ON TABLE {table} FROM "
                "marketai_domain_api, marketai_domain_worker, marketai_domain_readonly"
            )
        op.execute(
            "REVOKE ALL ON credential_metadata FROM "
            "marketai_domain_api, marketai_domain_readonly"
        )
        op.execute(
            "REVOKE ALL ON FUNCTION marketai_apply_workspace_projection_v2 "
            "FROM marketai_domain_api"
        )

        # Drop credential_metadata view (before dropping credential_versions)
        op.execute("DROP VIEW IF EXISTS credential_metadata")

        # Drop RLS policies
        for table in NEW_TABLES:
            op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"')
            op.execute(
                f'DROP POLICY IF EXISTS workspace_isolation_{table} ON "{table}"'
            )
            op.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY')
            op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')

        # Drop function
        op.execute("DROP FUNCTION IF EXISTS marketai_apply_workspace_projection_v2")

        # Drop triggers and functions
        for table in APPEND_ONLY_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table}")
            op.execute(f"DROP FUNCTION IF EXISTS reject_update_delete_{table}")

    with op.batch_alter_table('provider_cost_ledger', schema=None) as batch_op:
        batch_op.drop_index('ix_provider_cost_ledger_workspace_id')
    op.drop_table('provider_cost_ledger')

    with op.batch_alter_table('provider_call_attempts', schema=None) as batch_op:
        batch_op.drop_index('ix_provider_call_attempts_workspace_id')
    op.drop_table('provider_call_attempts')

    with op.batch_alter_table('spend_reservations', schema=None) as batch_op:
        batch_op.drop_index('ix_spend_reservations_workspace_id')
    op.drop_table('spend_reservations')

    with op.batch_alter_table('capacity_reservations', schema=None) as batch_op:
        batch_op.drop_index('ix_capacity_reservations_workspace_id')
    op.drop_table('capacity_reservations')

    op.drop_table('entitlement_projections')

    with op.batch_alter_table('credential_bindings', schema=None) as batch_op:
        batch_op.drop_index('ix_credential_bindings_workspace_id')
    op.drop_table('credential_bindings')

    with op.batch_alter_table('usage_events', schema=None) as batch_op:
        batch_op.drop_index('ix_usage_events_workspace_id')
    op.drop_table('usage_events')

    op.drop_table('spend_guardrails')

    with op.batch_alter_table('spend_guardrail_events', schema=None) as batch_op:
        batch_op.drop_index('ix_spend_guardrail_events_workspace_id')
    op.drop_table('spend_guardrail_events')

    with op.batch_alter_table('guardrail_notification_states', schema=None) as batch_op:
        batch_op.drop_index('ix_guardrail_notification_states_workspace_id')
    op.drop_table('guardrail_notification_states')

    with op.batch_alter_table('entitlement_snapshot_inbox', schema=None) as batch_op:
        batch_op.drop_index('ix_entitlement_snapshot_inbox_workspace_id')
    op.drop_table('entitlement_snapshot_inbox')

    with op.batch_alter_table('customer_notification_outbox', schema=None) as batch_op:
        batch_op.drop_index('ix_customer_notification_outbox_workspace_id')
    op.drop_table('customer_notification_outbox')

    with op.batch_alter_table('credential_versions', schema=None) as batch_op:
        batch_op.drop_index('uq_credential_version_active', sqlite_where=sa.text("state = 'active'"), postgresql_where=sa.text("state = 'active'"))
        batch_op.drop_index('ix_credential_versions_workspace_id')
    op.drop_table('credential_versions')

    # Reverse operation_state_events extension: restore naked FK, drop workspace_id
    with op.batch_alter_table("operation_state_events", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_operation_state_events_workspace_operation_operation_records",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_operation_state_events_workspace_id_workspaces",
            type_="foreignkey",
        )
        batch_op.drop_index("ix_operation_state_events_workspace_id")
        batch_op.create_foreign_key(
            "fk_operation_state_events_operation_id_operation_records",
            "operation_records",
            ["operation_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.drop_column("workspace_id")

    # SQLite batch_alter_table loses triggers; re-create.
    if not _is_postgres():
        op.execute(
            "CREATE TRIGGER IF NOT EXISTS operation_state_events_reject_update "
            "BEFORE UPDATE ON operation_state_events "
            "BEGIN SELECT RAISE(ABORT, 'operation_state_events is append-only'); END"
        )
        op.execute(
            "CREATE TRIGGER IF NOT EXISTS operation_state_events_reject_delete "
            "BEFORE DELETE ON operation_state_events "
            "BEGIN SELECT RAISE(ABORT, 'operation_state_events is append-only'); END"
        )

    # Drop jobs CHECK constraint (batch mode required for SQLite)
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.drop_constraint(
            "jobs_v1_requires_operation_and_entitlement_version", type_="check"
        )

    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_constraint('fk_jobs_workspace_operation_operation_records', type_='foreignkey')
        batch_op.drop_column('execution_contract_version')
        batch_op.drop_column('entitlement_version_at_enqueue')
        batch_op.drop_column('operation_id')

    with op.batch_alter_table('operation_records', schema=None) as batch_op:
        batch_op.drop_constraint('uq_operation_semantic_key', type_='unique')
        batch_op.drop_column('admission_contract_version')
        batch_op.drop_column('request_fingerprint_hash')
        batch_op.drop_column('semantic_key_hash')

    # Drop the portable currency constraint.  The unprefixed name covers
    # databases created by the pre-review draft of this migration.
    if _is_postgres():
        op.execute(
            "ALTER TABLE workspaces DROP CONSTRAINT IF EXISTS workspace_budget_currency_format"
        )
        op.execute(
            "ALTER TABLE workspaces DROP CONSTRAINT IF EXISTS "
            "ck_workspaces_workspace_budget_currency_format"
        )
    else:
        with op.batch_alter_table('workspaces', schema=None) as batch_op:
            batch_op.drop_constraint("workspace_budget_currency_format", type_="check")

    with op.batch_alter_table('workspaces', schema=None) as batch_op:
        batch_op.drop_constraint("workspace_lifecycle_valid", type_="check")
        batch_op.drop_column('budget_currency')
        batch_op.drop_column('lifecycle')
