"""scale: deterministic evidence and advice (Issue #25)

Adds all ~42 Scale-domain tables from app/scale/models.py (Tasks 2-5):
ScaleEnrollment/Episode lifecycle, Policy/Target/metric-contract-set
records, immutable source evidence and terminal manifests, sealed
EvidenceSnapshot, durable evaluation runs and immutable Evaluations, Advice
threads/occurrences/dispositions/outcomes, and conversion/export records.

DDL order matches spec Section 1 domain model, §1.4-§1.11:
  1. Policy (§1.6, platform-global)
  2. Enrollment (§1.4)
  3. Episode (§1.5)
  4. Target + metric-semantic-contract-set (§1.6, workspace-scoped)
  5. Source evidence + terminal manifests + readiness projection (§1.7)
  6. Sealed EvidenceSnapshot + children (§1.8)
  7. Evaluation runs/records + children + Episode projection (§1.9)
  8. Advice threads/occurrences/events/dispositions/snoozes/notifications (§1.10)
  9. Conversion, test-plan draft, outcome, unobserved factor, export (§1.11)
  10. Composite (workspace[, mode], id) foreign keys binding every direct
      Scale-to-Scale and Scale-to-cross-module relationship (spec §1.3),
      portable SQL applied via Alembic batch mode -- not gated behind
      ``_is_pg``.
  11. PostgreSQL-only: partial unique indexes, immutability triggers, RLS
      (§1.12), capability-scoped grants.

PostgreSQL-only DDL (partial unique indexes, immutability/sealing triggers,
Row Level Security, grants) is gated behind ``_is_pg(conn)`` and reuses the
platform's existing HMAC-verified tenant-context functions:
``marketai_context_is_current(workspace_id)`` for the two mode-independent
Target tables, and the meta-mode-aware
``marketai_meta_context_is_current(workspace_id, meta_mode)`` (established
in ``m23a_meta_tenant_context_foundation``, reused by the #23 Meta/Launch
domain migration ``m23b_meta_domain_tables``) for every other Scale table.
This is what the spec's simplified "workspace_id = current_setting(...)"
prose (§1.12) means in this codebase's actual security model: a naive
``current_setting`` comparison, without HMAC/epoch verification, would be a
regression from every other Scale-adjacent migration -- see the Task 6
report for this deliberate contract/implementation reconciliation.

Grants follow the spec §1.12 capability split: the Scale API role
(``marketai_domain_api``) owns explicit user commands (Enrollment/Episode
create and transition, Target/Policy authoring, Advice disposition/snooze/
conversion/outcome/unobserved-factor/export-request) and never gets INSERT
on Evaluations or system-only Advice lifecycle events; the Scale Worker role
(``marketai_domain_worker``) owns evidence ingestion, snapshot sealing,
evaluation, and system Advice lifecycle, and is never granted a Launch
table, a Meta mutation job, or a credential-secret table (this migration
grants only the 42 tables listed below). No role is granted DELETE:
every Scale table is either immutable/append-only or a CAS-maintained
current-state projection over an append-only history.

Revision ID: f5eeeab4e9b9
Revises: x1a2b3c4d5e6
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


revision = "f5eeeab4e9b9"
down_revision = "x1a2b3c4d5e6"
branch_labels = None
depends_on = None


def _is_pg(conn) -> bool:
    return conn.dialect.name == "postgresql"


# ---------------------------------------------------------------------------
# Table classification for RLS / triggers / grants (spec §1.12) and for the
# tests/test_postgres_rls.py CURRENT_AGGREGATE_TABLES / all_protected_tables
# registries.
# ---------------------------------------------------------------------------

# Platform-global: no workspace_id, no RLS (spec §1.6 -- Policy is the one
# platform-global exception among Scale's tables).
PLATFORM_TABLES = (
    "scale_policy_versions",
)

# Every other Scale table: workspace_id NOT NULL, gets RLS.
NEW_WORKSPACE_OWNED = (
    "scale_enrollments",
    "scale_enrollment_events",
    "scale_episodes",
    "scale_episode_events",
    "scale_target_versions",
    "scale_target_lifecycle_events",
    "scale_metric_semantic_contract_sets",
    "scale_metric_semantic_contract_set_members",
    "scale_daily_metric_sources",
    "scale_exact_window_sources",
    "scale_breakdown_sources",
    "scale_breakdown_daily_children",
    "scale_breakdown_privacy_records",
    "scale_delivery_config_sources",
    "scale_evidence_source_manifests",
    "scale_manifest_items",
    "scale_source_quality_issues",
    "scale_evidence_readiness_projections",
    "scale_evidence_snapshots",
    "scale_evidence_source_links",
    "scale_snapshot_quality_issues",
    "scale_snapshot_breakdown_segments",
    "scale_snapshot_breakdown_daily",
    "scale_evaluation_runs",
    "scale_evaluations",
    "scale_evaluation_signals",
    "scale_evaluation_advice_candidates",
    "scale_evaluation_streak_counters",
    "scale_evaluation_streak_resets",
    "scale_episode_projections",
    "scale_advice_threads",
    "scale_advice_occurrences",
    "scale_advice_thread_events",
    "scale_advice_dispositions",
    "scale_advice_snoozes",
    "scale_advice_notification_decisions",
    "scale_advice_conversions",
    "scale_test_plan_drafts",
    "scale_advice_outcomes",
    "scale_unobserved_factors",
    "scale_exports",
)

# The two Target tables are workspace-scoped but explicitly mode-independent
# (spec §1.6: "Target Values and applicability are immutable" / Target
# belongs to "one exact ProductVersion, OfferVersion, market, and currency
# context" -- no meta_mode column). Every other workspace table carries
# meta_mode and gets the meta-mode-aware policy function.
MODE_INDEPENDENT_TABLES = (
    "scale_target_versions",
    "scale_target_lifecycle_events",
)

# scale_metric_semantic_contract_set_members carries no workspace_id/
# meta_mode of its own -- a pure child of scale_metric_semantic_contract_sets,
# like dimension_values -> dimensions in f1a2c3d4e5b6_row_level_security.py.
# It is still "workspace-owned" (included in NEW_WORKSPACE_OWNED for the
# grants/immutability-trigger loops and for tests/test_postgres_rls.py's
# coverage check), but its RLS policy is the "child form": visible exactly
# when its parent row is visible, rather than a direct workspace_id compare.
CHILD_FORM_TABLES = {
    "scale_metric_semantic_contract_set_members": (
        "contract_set_id", "scale_metric_semantic_contract_sets",
    ),
}

# Fully immutable, append-only tables (no UPDATE, no DELETE, ever): source
# evidence records, manifest records, sealed-snapshot *children* (their
# parent scale_evidence_snapshots gets the special sealing-transition
# trigger below instead), evaluation records, occurrence records, thread
# event records, disposition records, outcome records -- plus every other
# table whose own docstring in app/scale/models.py calls it immutable/
# append-only (Enrollment/Episode/Target lifecycle events, the metric-
# semantic-contract-set parent+child, notification decisions, conversions,
# unobserved factors).
IMMUTABLE_TABLES = (
    "scale_enrollment_events",
    "scale_episode_events",
    "scale_target_lifecycle_events",
    "scale_metric_semantic_contract_sets",
    "scale_metric_semantic_contract_set_members",
    "scale_daily_metric_sources",
    "scale_exact_window_sources",
    "scale_breakdown_sources",
    "scale_breakdown_daily_children",
    "scale_breakdown_privacy_records",
    "scale_delivery_config_sources",
    "scale_evidence_source_manifests",
    "scale_manifest_items",
    "scale_source_quality_issues",
    "scale_evidence_source_links",
    "scale_snapshot_quality_issues",
    "scale_snapshot_breakdown_segments",
    "scale_snapshot_breakdown_daily",
    "scale_evaluations",
    "scale_evaluation_signals",
    "scale_evaluation_advice_candidates",
    "scale_evaluation_streak_counters",
    "scale_evaluation_streak_resets",
    "scale_advice_occurrences",
    "scale_advice_thread_events",
    "scale_advice_dispositions",
    "scale_advice_notification_decisions",
    "scale_advice_conversions",
    "scale_advice_outcomes",
    "scale_unobserved_factors",
)

# The sealed EvidenceSnapshot parent: exactly one legal UPDATE transition
# (unsealed -> sealed), DELETE always rejected. Spec §1.8.
SEALED_SNAPSHOT_TABLE = "scale_evidence_snapshots"

# Snapshot children that "permit INSERT only while the parent is unsealed"
# (spec §1.8) in addition to being immutable (IMMUTABLE_TABLES above). The
# first three carry snapshot_id directly; scale_snapshot_breakdown_daily
# only carries breakdown_segment_id and is checked through its segment.
SNAPSHOT_CHILDREN_BY_SNAPSHOT_ID = (
    "scale_evidence_source_links",
    "scale_snapshot_quality_issues",
    "scale_snapshot_breakdown_segments",
)
SNAPSHOT_CHILDREN_BY_BREAKDOWN_SEGMENT_ID = (
    "scale_snapshot_breakdown_daily",
)

# Grants: (api_ops, worker_ops) per table. SELECT is universal (api, worker,
# readonly) and granted separately below; only INSERT/UPDATE are listed
# here. Empty tuple means that role gets SELECT only on that table. No role
# is ever granted DELETE. See the module docstring for the capability
# rationale.
GRANT_MATRIX = {
    "scale_policy_versions": (("INSERT", "UPDATE"), ()),
    "scale_enrollments": (("INSERT", "UPDATE"), ("UPDATE",)),
    "scale_enrollment_events": (("INSERT",), ("INSERT",)),
    "scale_episodes": (("INSERT", "UPDATE"), ("UPDATE",)),
    "scale_episode_events": (("INSERT",), ("INSERT",)),
    "scale_target_versions": (("INSERT", "UPDATE"), ()),
    "scale_target_lifecycle_events": (("INSERT",), ()),
    "scale_metric_semantic_contract_sets": (("INSERT",), ()),
    "scale_metric_semantic_contract_set_members": (("INSERT",), ()),
    "scale_daily_metric_sources": ((), ("INSERT",)),
    "scale_exact_window_sources": ((), ("INSERT",)),
    "scale_breakdown_sources": ((), ("INSERT",)),
    "scale_breakdown_daily_children": ((), ("INSERT",)),
    "scale_breakdown_privacy_records": ((), ("INSERT",)),
    "scale_delivery_config_sources": ((), ("INSERT",)),
    "scale_evidence_source_manifests": ((), ("INSERT",)),
    "scale_manifest_items": ((), ("INSERT",)),
    "scale_source_quality_issues": ((), ("INSERT",)),
    "scale_evidence_readiness_projections": ((), ("INSERT", "UPDATE")),
    "scale_evidence_snapshots": ((), ("INSERT", "UPDATE")),
    "scale_evidence_source_links": ((), ("INSERT",)),
    "scale_snapshot_quality_issues": ((), ("INSERT",)),
    "scale_snapshot_breakdown_segments": ((), ("INSERT",)),
    "scale_snapshot_breakdown_daily": ((), ("INSERT",)),
    "scale_evaluation_runs": ((), ("INSERT", "UPDATE")),
    "scale_evaluations": ((), ("INSERT",)),
    "scale_evaluation_signals": ((), ("INSERT",)),
    "scale_evaluation_advice_candidates": ((), ("INSERT",)),
    "scale_evaluation_streak_counters": ((), ("INSERT",)),
    "scale_evaluation_streak_resets": ((), ("INSERT",)),
    "scale_episode_projections": ((), ("INSERT", "UPDATE")),
    "scale_advice_threads": (("UPDATE",), ("INSERT", "UPDATE")),
    "scale_advice_occurrences": ((), ("INSERT",)),
    "scale_advice_thread_events": ((), ("INSERT",)),
    "scale_advice_dispositions": (("INSERT",), ()),
    "scale_advice_snoozes": (("INSERT", "UPDATE"), ("UPDATE",)),
    "scale_advice_notification_decisions": ((), ("INSERT",)),
    "scale_advice_conversions": (("INSERT",), ()),
    "scale_test_plan_drafts": (("INSERT", "UPDATE"), ()),
    "scale_advice_outcomes": (("INSERT",), ()),
    "scale_unobserved_factors": (("INSERT",), ()),
    "scale_exports": (("INSERT",), ("UPDATE",)),
}
assert set(GRANT_MATRIX) == set(PLATFORM_TABLES) | set(NEW_WORKSPACE_OWNED)


def upgrade() -> None:
    conn = op.get_bind()

    # -- Table creation, ordered per spec §1.4-§1.11 (see module docstring) --
    _step1_platform()
    _step2_enrollment()
    _step3_episode()
    _step4_policy_target_contracts()
    _step5_source_evidence()
    _step6_snapshot()
    _step7_evaluation()
    _step8_advice()
    _step9_conversion_outcome_export()

    # -- Composite foreign keys (portable SQL, both dialects) --
    _step10_composite_foreign_keys()

    # -- PostgreSQL-only: partial unique indexes, triggers, RLS, grants --
    _step11_pg_enforcement(conn)


def _step1_platform():
    op.create_table('scale_policy_versions',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('algorithm_version', sa.String(length=20), nullable=False),
    sa.Column('canonical_schema_version', sa.String(length=20), nullable=False),
    sa.Column('decimal_precision', sa.Integer(), nullable=False),
    sa.Column('decimal_rounding', sa.String(length=30), nullable=False),
    sa.Column('z_value', sa.Numeric(precision=12, scale=8), nullable=False),
    sa.Column('evidence_floor_minimum_spend', sa.Numeric(precision=24, scale=12), nullable=False),
    sa.Column('evidence_floor_minimum_days', sa.Integer(), nullable=False),
    sa.Column('evidence_floor_minimum_conversions', sa.Integer(), nullable=False),
    sa.Column('activity_floor_minimum_impressions', sa.Integer(), nullable=False),
    sa.Column('sample_floor_minimum_clicks', sa.Integer(), nullable=False),
    sa.Column('spend_floor_minimum_daily_spend', sa.Numeric(precision=24, scale=12), nullable=False),
    sa.Column('freshness_floor_maximum_lag_hours', sa.Integer(), nullable=False),
    sa.Column('signal_thresholds', sa.JSON(), nullable=False),
    sa.Column('streak_rules', sa.JSON(), nullable=False),
    sa.Column('bootstrap_prng_algorithm', sa.String(length=40), nullable=False),
    sa.Column('bootstrap_draw_count', sa.Integer(), nullable=False),
    sa.Column('bootstrap_valid_draw_ratio', sa.Numeric(precision=6, scale=5), nullable=False),
    sa.Column('bootstrap_percentile_method', sa.String(length=30), nullable=False),
    sa.Column('bootstrap_draw_order', sa.String(length=20), nullable=False),
    sa.Column('breakdown_families', sa.JSON(), nullable=False),
    sa.Column('breakdown_sample_gate_minimum', sa.Integer(), nullable=False),
    sa.Column('breakdown_share_gate_minimum_ratio', sa.Numeric(precision=6, scale=5), nullable=False),
    sa.Column('bh_fdr_alpha', sa.Numeric(precision=6, scale=5), nullable=False),
    sa.Column('advice_severity_thresholds', sa.JSON(), nullable=False),
    sa.Column('notification_cooldown_hours', sa.Integer(), nullable=False),
    sa.Column('conversion_safe_values', sa.JSON(), nullable=False),
    sa.Column('mode_permissions', sa.JSON(), nullable=False),
    sa.Column('is_published', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_by', sa.String(length=100), nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("bootstrap_draw_order IN ('sequential', 'shuffled')", name=op.f('ck_scale_policy_versions_ck_spol_bootstrap_draw_order')),
    sa.CheckConstraint("bootstrap_percentile_method IN ('nearest_rank', 'linear_interpolation')", name=op.f('ck_scale_policy_versions_ck_spol_bootstrap_percentile_method')),
    sa.CheckConstraint("decimal_rounding IN ('round_half_even', 'round_half_up', 'round_down', 'round_ceiling')", name=op.f('ck_scale_policy_versions_ck_spol_decimal_rounding')),
    sa.CheckConstraint('(NOT is_published) OR (published_at IS NOT NULL)', name=op.f('ck_scale_policy_versions_ck_spol_published_at_shape')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_policy_versions'))
    )


def _step2_enrollment():
    op.create_table('scale_enrollments',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('meta_connection_id', sa.String(length=64), nullable=False),
    sa.Column('scenario_scope_key', sa.String(length=64), nullable=False),
    sa.Column('ad_account_id', sa.String(length=100), nullable=False),
    sa.Column('winner_decision_id', sa.String(length=64), nullable=False),
    sa.Column('winner_confirmation_id', sa.String(length=64), nullable=False),
    sa.Column('asset_version_id', sa.String(length=64), nullable=False),
    sa.Column('product_version_id', sa.String(length=64), nullable=False),
    sa.Column('offer_version_id', sa.String(length=64), nullable=False),
    sa.Column('market_code', sa.String(length=10), nullable=False),
    sa.Column('landing_page_version_id', sa.String(length=64), nullable=True),
    sa.Column('market_asset_binding_id', sa.String(length=64), nullable=True),
    sa.Column('campaign_remote_id', sa.String(length=100), nullable=True),
    sa.Column('ad_set_remote_id', sa.String(length=100), nullable=True),
    sa.Column('ad_remote_id', sa.String(length=100), nullable=True),
    sa.Column('remote_binding_key', sa.String(length=200), nullable=False),
    sa.Column('lifecycle', sa.String(length=20), nullable=False),
    sa.Column('transition_reason', sa.String(length=100), nullable=True),
    sa.Column('current_episode_id', sa.String(length=64), nullable=True),
    sa.Column('creator_id', sa.String(length=100), nullable=False),
    sa.Column('original_initiator_id', sa.String(length=100), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('evaluation_cancellation_generation', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("(meta_mode = 'controlled_mock' AND scenario_scope_key != '__no_scenario__') OR (meta_mode != 'controlled_mock' AND scenario_scope_key = '__no_scenario__')", name=op.f('ck_scale_enrollments_ck_senr_scenario_biconditional')),
    sa.CheckConstraint("lifecycle IN ('draft', 'active', 'paused', 'closed')", name=op.f('ck_scale_enrollments_ck_senr_lifecycle')),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_enrollments_ck_senr_meta_mode')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_enrollments_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_enrollments'))
    )
    with op.batch_alter_table('scale_enrollments', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_enrollments_workspace_id'), ['workspace_id'], unique=False)
    op.create_table('scale_enrollment_events',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('enrollment_id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('sequence', sa.Integer(), nullable=False),
    sa.Column('event_type', sa.String(length=60), nullable=False),
    sa.Column('previous_lifecycle', sa.String(length=20), nullable=True),
    sa.Column('current_lifecycle', sa.String(length=20), nullable=False),
    sa.Column('reason', sa.String(length=100), nullable=True),
    sa.Column('deduplication_key', sa.String(length=128), nullable=False),
    sa.Column('executor_id', sa.String(length=100), nullable=False),
    sa.Column('original_initiator_id', sa.String(length=100), nullable=False),
    sa.Column('revision_at_event', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("current_lifecycle IN ('draft', 'active', 'paused', 'closed')", name=op.f('ck_scale_enrollment_events_ck_sene_current_lifecycle')),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_enrollment_events_ck_sene_meta_mode')),
    sa.CheckConstraint("previous_lifecycle IS NULL OR previous_lifecycle IN ('draft', 'active', 'paused', 'closed')", name=op.f('ck_scale_enrollment_events_ck_sene_previous_lifecycle')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_enrollment_events_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_enrollment_events')),
    sa.UniqueConstraint('workspace_id', 'meta_mode', 'deduplication_key', name='uq_sene_workspace_mode_dedup')
    )
    with op.batch_alter_table('scale_enrollment_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_enrollment_events_enrollment_id'), ['enrollment_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_enrollment_events_workspace_id'), ['workspace_id'], unique=False)


def _step3_episode():
    op.create_table('scale_episodes',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('enrollment_id', sa.String(length=64), nullable=False),
    sa.Column('winner_decision_id', sa.String(length=64), nullable=False),
    sa.Column('winner_confirmation_id', sa.String(length=64), nullable=False),
    sa.Column('asset_version_id', sa.String(length=64), nullable=False),
    sa.Column('product_version_id', sa.String(length=64), nullable=False),
    sa.Column('offer_version_id', sa.String(length=64), nullable=False),
    sa.Column('market_code', sa.String(length=10), nullable=False),
    sa.Column('landing_page_version_id', sa.String(length=64), nullable=True),
    sa.Column('market_asset_binding_id', sa.String(length=64), nullable=True),
    sa.Column('meta_connection_id', sa.String(length=64), nullable=False),
    sa.Column('scenario_scope_key', sa.String(length=64), nullable=False),
    sa.Column('ad_account_id', sa.String(length=100), nullable=False),
    sa.Column('campaign_remote_id', sa.String(length=100), nullable=True),
    sa.Column('ad_set_remote_id', sa.String(length=100), nullable=True),
    sa.Column('ad_remote_id', sa.String(length=100), nullable=True),
    sa.Column('delivery_config_baseline_source_id', sa.String(length=64), nullable=False),
    sa.Column('delivery_evidence_hash', sa.String(length=64), nullable=False),
    sa.Column('delivery_comparability_hash', sa.String(length=64), nullable=False),
    sa.Column('metric_semantic_contract_set_id', sa.String(length=64), nullable=False),
    sa.Column('timezone_name', sa.String(length=64), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('attribution_semantics_hash', sa.String(length=64), nullable=False),
    sa.Column('phase', sa.String(length=20), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('started_local_day', sa.Date(), nullable=False),
    sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('closure_reason', sa.String(length=60), nullable=True),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('projection_revision', sa.Integer(), nullable=False),
    sa.Column('projection_as_of_local_day', sa.Date(), nullable=True),
    sa.Column('projection_source_revision', sa.Integer(), nullable=False),
    sa.Column('projection_semantic_event_sequence', sa.Integer(), nullable=False),
    sa.Column('projection_lineage_generation', sa.Integer(), nullable=False),
    sa.Column('accepted_evaluation_id', sa.String(length=64), nullable=True),
    sa.Column('latest_source_manifest_id', sa.String(length=64), nullable=True),
    sa.Column('phase_run_id', sa.String(length=64), nullable=True),
    sa.CheckConstraint("(meta_mode = 'controlled_mock' AND scenario_scope_key != '__no_scenario__') OR (meta_mode != 'controlled_mock' AND scenario_scope_key = '__no_scenario__')", name=op.f('ck_scale_episodes_ck_sep_scenario_biconditional')),
    sa.CheckConstraint("(phase = 'closed') = (closure_reason IS NOT NULL)", name=op.f('ck_scale_episodes_ck_sep_closed_closure_reason')),
    sa.CheckConstraint("(phase = 'closed') = (ended_at IS NOT NULL)", name=op.f('ck_scale_episodes_ck_sep_closed_ended_at')),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_episodes_ck_sep_meta_mode')),
    sa.CheckConstraint("phase IN ('collecting', 'evaluating', 'closed')", name=op.f('ck_scale_episodes_ck_sep_phase')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_episodes_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_episodes'))
    )
    with op.batch_alter_table('scale_episodes', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_episodes_enrollment_id'), ['enrollment_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_episodes_workspace_id'), ['workspace_id'], unique=False)
    op.create_table('scale_episode_events',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('episode_id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('enrollment_id', sa.String(length=64), nullable=False),
    sa.Column('event_type', sa.String(length=60), nullable=False),
    sa.Column('previous_phase', sa.String(length=20), nullable=True),
    sa.Column('current_phase', sa.String(length=20), nullable=False),
    sa.Column('closure_reason', sa.String(length=60), nullable=True),
    sa.Column('deduplication_key', sa.String(length=128), nullable=False),
    sa.Column('executor_id', sa.String(length=100), nullable=False),
    sa.Column('original_initiator_id', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("current_phase IN ('collecting', 'evaluating', 'closed')", name=op.f('ck_scale_episode_events_ck_sevt_current_phase')),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_episode_events_ck_sevt_meta_mode')),
    sa.CheckConstraint("previous_phase IS NULL OR previous_phase IN ('collecting', 'evaluating', 'closed')", name=op.f('ck_scale_episode_events_ck_sevt_previous_phase')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_episode_events_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_episode_events')),
    sa.UniqueConstraint('workspace_id', 'meta_mode', 'deduplication_key', name='uq_sevt_workspace_mode_dedup')
    )
    with op.batch_alter_table('scale_episode_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_episode_events_enrollment_id'), ['enrollment_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_episode_events_episode_id'), ['episode_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_episode_events_workspace_id'), ['workspace_id'], unique=False)


def _step4_policy_target_contracts():
    op.create_table('scale_target_versions',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('product_version_id', sa.String(length=64), nullable=False),
    sa.Column('offer_version_id', sa.String(length=64), nullable=False),
    sa.Column('market_code', sa.String(length=10), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('target_cpa', sa.Numeric(precision=24, scale=12), nullable=True),
    sa.Column('breakeven_roas', sa.Numeric(precision=10, scale=4), nullable=True),
    sa.Column('minimum_economic_spend', sa.Numeric(precision=24, scale=12), nullable=True),
    sa.Column('lifecycle_state', sa.String(length=20), nullable=False),
    sa.Column('creator_id', sa.String(length=100), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('withdrawn_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("lifecycle_state IN ('draft', 'active', 'withdrawn')", name=op.f('ck_scale_target_versions_ck_stgt_lifecycle_state')),
    sa.CheckConstraint('target_cpa IS NOT NULL OR breakeven_roas IS NOT NULL', name=op.f('ck_scale_target_versions_ck_stgt_at_least_one_target')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_target_versions_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_target_versions'))
    )
    with op.batch_alter_table('scale_target_versions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_target_versions_workspace_id'), ['workspace_id'], unique=False)
    op.create_table('scale_target_lifecycle_events',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('target_version_id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('event_type', sa.String(length=20), nullable=False),
    sa.Column('previous_lifecycle_state', sa.String(length=20), nullable=True),
    sa.Column('current_lifecycle_state', sa.String(length=20), nullable=False),
    sa.Column('reason', sa.String(length=100), nullable=True),
    sa.Column('semantic_event_sequence', sa.Integer(), nullable=False),
    sa.Column('executor_id', sa.String(length=100), nullable=False),
    sa.Column('original_initiator_id', sa.String(length=100), nullable=False),
    sa.Column('revision_at_event', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("current_lifecycle_state IN ('draft', 'active', 'withdrawn')", name=op.f('ck_scale_target_lifecycle_events_ck_stge_curr_state')),
    sa.CheckConstraint("event_type IN ('publish', 'withdraw')", name=op.f('ck_scale_target_lifecycle_events_ck_stge_event_type')),
    sa.CheckConstraint("previous_lifecycle_state IS NULL OR previous_lifecycle_state IN ('draft', 'active', 'withdrawn')", name=op.f('ck_scale_target_lifecycle_events_ck_stge_prev_state')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_target_lifecycle_events_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_target_lifecycle_events'))
    )
    with op.batch_alter_table('scale_target_lifecycle_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_target_lifecycle_events_target_version_id'), ['target_version_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_target_lifecycle_events_workspace_id'), ['workspace_id'], unique=False)
    op.create_table('scale_metric_semantic_contract_sets',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_by', sa.String(length=100), nullable=False),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_metric_semantic_contract_sets_ck_smcs_meta_mode')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_metric_semantic_contract_sets_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_metric_semantic_contract_sets'))
    )
    with op.batch_alter_table('scale_metric_semantic_contract_sets', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_metric_semantic_contract_sets_workspace_id'), ['workspace_id'], unique=False)
    op.create_table('scale_metric_semantic_contract_set_members',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('contract_set_id', sa.String(length=64), nullable=False),
    sa.Column('contract_type', sa.String(length=40), nullable=False),
    sa.Column('observation_contract_generation', sa.String(length=64), nullable=False),
    sa.Column('contract_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("contract_type IN ('daily', 'exact_window', 'breakdown_placement', 'breakdown_country', 'breakdown_age_gender', 'delivery_configuration')", name=op.f('ck_scale_metric_semantic_contract_set_members_ck_smcm_type')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_metric_semantic_contract_set_members')),
    sa.UniqueConstraint('contract_set_id', 'contract_type', name='uq_smcm_contract_set_contract_type')
    )
    with op.batch_alter_table('scale_metric_semantic_contract_set_members', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_metric_semantic_contract_set_members_contract_set_id'), ['contract_set_id'], unique=False)


def _step5_source_evidence():
    op.create_table('scale_daily_metric_sources',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('meta_connection_id', sa.String(length=64), nullable=False),
    sa.Column('scenario_scope_key', sa.String(length=64), nullable=False),
    sa.Column('ad_account_id', sa.String(length=100), nullable=False),
    sa.Column('episode_id', sa.String(length=64), nullable=False),
    sa.Column('source_revision', sa.Integer(), nullable=False),
    sa.Column('contract_generation_id', sa.String(length=64), nullable=False),
    sa.Column('local_day', sa.Date(), nullable=False),
    sa.Column('state', sa.String(length=20), nullable=False),
    sa.Column('delivered', sa.Boolean(), nullable=True),
    sa.Column('spend', sa.Numeric(precision=24, scale=12), nullable=True),
    sa.Column('impressions', sa.Integer(), nullable=True),
    sa.Column('link_clicks', sa.Integer(), nullable=True),
    sa.Column('purchases', sa.Integer(), nullable=True),
    sa.Column('purchase_value', sa.Numeric(precision=24, scale=12), nullable=True),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('timezone_name', sa.String(length=64), nullable=False),
    sa.Column('raw_response_hash', sa.String(length=64), nullable=False),
    sa.Column('sync_run_id', sa.String(length=64), nullable=False),
    sa.Column('occurrence_id', sa.String(length=200), nullable=False),
    sa.Column('call_intent_id', sa.String(length=64), nullable=False),
    sa.Column('page_attempt_reference', sa.String(length=100), nullable=True),
    sa.Column('reconciliation_id', sa.String(length=64), nullable=True),
    sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("(meta_mode = 'controlled_mock' AND scenario_scope_key != '__no_scenario__') OR (meta_mode != 'controlled_mock' AND scenario_scope_key = '__no_scenario__')", name=op.f('ck_scale_daily_metric_sources_ck_sdms_scenario_biconditional')),
    sa.CheckConstraint("(state = 'present' AND delivered IS NOT NULL AND spend IS NOT NULL AND impressions IS NOT NULL AND link_clicks IS NOT NULL AND purchases IS NOT NULL AND purchase_value IS NOT NULL) OR (state = 'missing' AND delivered IS NULL AND spend IS NULL AND impressions IS NULL AND link_clicks IS NULL AND purchases IS NULL AND purchase_value IS NULL)", name=op.f('ck_scale_daily_metric_sources_ck_sdms_state_shape')),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_daily_metric_sources_ck_sdms_meta_mode')),
    sa.CheckConstraint("state IN ('present', 'missing')", name=op.f('ck_scale_daily_metric_sources_ck_sdms_state')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_daily_metric_sources_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_daily_metric_sources')),
    sa.UniqueConstraint('workspace_id', 'meta_mode', 'episode_id', 'source_revision', 'local_day', name='uq_sdms_revision_day')
    )
    with op.batch_alter_table('scale_daily_metric_sources', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_daily_metric_sources_episode_id'), ['episode_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_daily_metric_sources_workspace_id'), ['workspace_id'], unique=False)
    op.create_table('scale_exact_window_sources',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('meta_connection_id', sa.String(length=64), nullable=False),
    sa.Column('scenario_scope_key', sa.String(length=64), nullable=False),
    sa.Column('ad_account_id', sa.String(length=100), nullable=False),
    sa.Column('episode_id', sa.String(length=64), nullable=False),
    sa.Column('source_revision', sa.Integer(), nullable=False),
    sa.Column('contract_generation_id', sa.String(length=64), nullable=False),
    sa.Column('window_role', sa.String(length=20), nullable=False),
    sa.Column('window_start_local_day', sa.Date(), nullable=False),
    sa.Column('window_end_local_day', sa.Date(), nullable=False),
    sa.Column('reach', sa.Integer(), nullable=False),
    sa.Column('frequency', sa.Numeric(precision=10, scale=4), nullable=False),
    sa.Column('spend', sa.Numeric(precision=24, scale=12), nullable=False),
    sa.Column('impressions', sa.Integer(), nullable=False),
    sa.Column('link_clicks', sa.Integer(), nullable=False),
    sa.Column('purchases', sa.Integer(), nullable=False),
    sa.Column('purchase_value', sa.Numeric(precision=24, scale=12), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('timezone_name', sa.String(length=64), nullable=False),
    sa.Column('raw_response_hash', sa.String(length=64), nullable=False),
    sa.Column('sync_run_id', sa.String(length=64), nullable=False),
    sa.Column('occurrence_id', sa.String(length=200), nullable=False),
    sa.Column('call_intent_id', sa.String(length=64), nullable=False),
    sa.Column('reconciliation_id', sa.String(length=64), nullable=True),
    sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("(meta_mode = 'controlled_mock' AND scenario_scope_key != '__no_scenario__') OR (meta_mode != 'controlled_mock' AND scenario_scope_key = '__no_scenario__')", name=op.f('ck_scale_exact_window_sources_ck_sews_scenario_biconditional')),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_exact_window_sources_ck_sews_meta_mode')),
    sa.CheckConstraint("window_role IN ('current_7d', 'baseline_7d', 'rolling_14d')", name=op.f('ck_scale_exact_window_sources_ck_sews_window_role')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_exact_window_sources_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_exact_window_sources')),
    sa.UniqueConstraint('workspace_id', 'meta_mode', 'episode_id', 'source_revision', 'window_role', name='uq_sews_revision_window')
    )
    with op.batch_alter_table('scale_exact_window_sources', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_exact_window_sources_episode_id'), ['episode_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_exact_window_sources_workspace_id'), ['workspace_id'], unique=False)
    op.create_table('scale_breakdown_sources',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('meta_connection_id', sa.String(length=64), nullable=False),
    sa.Column('scenario_scope_key', sa.String(length=64), nullable=False),
    sa.Column('ad_account_id', sa.String(length=100), nullable=False),
    sa.Column('episode_id', sa.String(length=64), nullable=False),
    sa.Column('source_revision', sa.Integer(), nullable=False),
    sa.Column('contract_generation_id', sa.String(length=64), nullable=False),
    sa.Column('contract_type', sa.String(length=40), nullable=False),
    sa.Column('window_role', sa.String(length=20), nullable=False),
    sa.Column('segment_key', sa.String(length=100), nullable=False),
    sa.Column('state', sa.String(length=20), nullable=False),
    sa.Column('raw_response_hash', sa.String(length=64), nullable=False),
    sa.Column('sync_run_id', sa.String(length=64), nullable=False),
    sa.Column('occurrence_id', sa.String(length=200), nullable=False),
    sa.Column('call_intent_id', sa.String(length=64), nullable=False),
    sa.Column('reconciliation_id', sa.String(length=64), nullable=True),
    sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("(meta_mode = 'controlled_mock' AND scenario_scope_key != '__no_scenario__') OR (meta_mode != 'controlled_mock' AND scenario_scope_key = '__no_scenario__')", name=op.f('ck_scale_breakdown_sources_ck_sbds_scenario_biconditional')),
    sa.CheckConstraint("contract_type IN ('breakdown_placement', 'breakdown_country', 'breakdown_age_gender')", name=op.f('ck_scale_breakdown_sources_ck_sbds_contract_type')),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_breakdown_sources_ck_sbds_meta_mode')),
    sa.CheckConstraint("state IN ('observed', 'privacy_suppressed')", name=op.f('ck_scale_breakdown_sources_ck_sbds_state')),
    sa.CheckConstraint("window_role IN ('current_7d', 'baseline_7d', 'rolling_14d')", name=op.f('ck_scale_breakdown_sources_ck_sbds_window_role')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_breakdown_sources_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_breakdown_sources')),
    sa.UniqueConstraint('workspace_id', 'meta_mode', 'episode_id', 'source_revision', 'contract_type', 'window_role', 'segment_key', name='uq_sbds_revision_segment')
    )
    with op.batch_alter_table('scale_breakdown_sources', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_breakdown_sources_episode_id'), ['episode_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_breakdown_sources_workspace_id'), ['workspace_id'], unique=False)
    op.create_table('scale_breakdown_daily_children',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('breakdown_source_id', sa.String(length=64), nullable=False),
    sa.Column('local_day', sa.Date(), nullable=False),
    sa.Column('state', sa.String(length=20), nullable=False),
    sa.Column('delivered', sa.Boolean(), nullable=True),
    sa.Column('spend', sa.Numeric(precision=24, scale=12), nullable=True),
    sa.Column('impressions', sa.Integer(), nullable=True),
    sa.Column('link_clicks', sa.Integer(), nullable=True),
    sa.Column('purchases', sa.Integer(), nullable=True),
    sa.Column('purchase_value', sa.Numeric(precision=24, scale=12), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("(state = 'present' AND delivered IS NOT NULL AND spend IS NOT NULL AND impressions IS NOT NULL AND link_clicks IS NOT NULL AND purchases IS NOT NULL AND purchase_value IS NOT NULL) OR (state = 'missing' AND delivered IS NULL AND spend IS NULL AND impressions IS NULL AND link_clicks IS NULL AND purchases IS NULL AND purchase_value IS NULL)", name=op.f('ck_scale_breakdown_daily_children_ck_sbdc_state_shape')),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_breakdown_daily_children_ck_sbdc_meta_mode')),
    sa.CheckConstraint("state IN ('present', 'missing')", name=op.f('ck_scale_breakdown_daily_children_ck_sbdc_state')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_breakdown_daily_children_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_breakdown_daily_children')),
    sa.UniqueConstraint('breakdown_source_id', 'local_day', name='uq_sbdc_source_day')
    )
    with op.batch_alter_table('scale_breakdown_daily_children', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_breakdown_daily_children_breakdown_source_id'), ['breakdown_source_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_breakdown_daily_children_workspace_id'), ['workspace_id'], unique=False)
    op.create_table('scale_breakdown_privacy_records',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('breakdown_source_id', sa.String(length=64), nullable=False),
    sa.Column('suppression_reason', sa.String(length=40), nullable=False),
    sa.Column('detail', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_breakdown_privacy_records_ck_sbpr_meta_mode')),
    sa.CheckConstraint("suppression_reason IN ('api_privacy_threshold', 'api_policy_omission')", name=op.f('ck_scale_breakdown_privacy_records_ck_sbpr_suppression_reason')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_breakdown_privacy_records_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_breakdown_privacy_records')),
    sa.UniqueConstraint('breakdown_source_id', name='uq_sbpr_breakdown_source')
    )
    with op.batch_alter_table('scale_breakdown_privacy_records', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_breakdown_privacy_records_breakdown_source_id'), ['breakdown_source_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_breakdown_privacy_records_workspace_id'), ['workspace_id'], unique=False)
    op.create_table('scale_delivery_config_sources',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('meta_connection_id', sa.String(length=64), nullable=False),
    sa.Column('scenario_scope_key', sa.String(length=64), nullable=False),
    sa.Column('ad_account_id', sa.String(length=100), nullable=False),
    sa.Column('episode_id', sa.String(length=64), nullable=False),
    sa.Column('source_revision', sa.Integer(), nullable=False),
    sa.Column('contract_generation_id', sa.String(length=64), nullable=False),
    sa.Column('ad_creative_id', sa.String(length=100), nullable=False),
    sa.Column('campaign_remote_id', sa.String(length=100), nullable=True),
    sa.Column('ad_set_remote_id', sa.String(length=100), nullable=True),
    sa.Column('ad_remote_id', sa.String(length=100), nullable=True),
    sa.Column('campaign_status', sa.String(length=40), nullable=False),
    sa.Column('ad_set_status', sa.String(length=40), nullable=False),
    sa.Column('ad_status', sa.String(length=40), nullable=False),
    sa.Column('budget_source', sa.String(length=40), nullable=False),
    sa.Column('budget_amount', sa.Numeric(precision=24, scale=12), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('optimization_goal', sa.String(length=60), nullable=False),
    sa.Column('bid_strategy', sa.String(length=60), nullable=False),
    sa.Column('attribution_setting', sa.String(length=60), nullable=False),
    sa.Column('targeting_safety_hash', sa.String(length=64), nullable=False),
    sa.Column('offer_version_id', sa.String(length=64), nullable=True),
    sa.Column('landing_page_version_id', sa.String(length=64), nullable=True),
    sa.Column('evidence_hash', sa.String(length=64), nullable=False),
    sa.Column('comparability_hash', sa.String(length=64), nullable=False),
    sa.Column('raw_response_hash', sa.String(length=64), nullable=False),
    sa.Column('sync_run_id', sa.String(length=64), nullable=False),
    sa.Column('occurrence_id', sa.String(length=200), nullable=False),
    sa.Column('call_intent_id', sa.String(length=64), nullable=False),
    sa.Column('reconciliation_id', sa.String(length=64), nullable=True),
    sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("(meta_mode = 'controlled_mock' AND scenario_scope_key != '__no_scenario__') OR (meta_mode != 'controlled_mock' AND scenario_scope_key = '__no_scenario__')", name=op.f('ck_scale_delivery_config_sources_ck_sdcs_scenario_biconditional')),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_delivery_config_sources_ck_sdcs_meta_mode')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_delivery_config_sources_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_delivery_config_sources')),
    sa.UniqueConstraint('workspace_id', 'meta_mode', 'episode_id', 'source_revision', name='uq_sdcs_revision')
    )
    with op.batch_alter_table('scale_delivery_config_sources', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_delivery_config_sources_episode_id'), ['episode_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_delivery_config_sources_workspace_id'), ['workspace_id'], unique=False)
    op.create_table('scale_evidence_source_manifests',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('meta_connection_id', sa.String(length=64), nullable=False),
    sa.Column('scenario_scope_key', sa.String(length=64), nullable=False),
    sa.Column('episode_id', sa.String(length=64), nullable=False),
    sa.Column('target_local_day', sa.Date(), nullable=False),
    sa.Column('source_revision', sa.Integer(), nullable=False),
    sa.Column('source_generation', sa.Integer(), nullable=False),
    sa.Column('state', sa.String(length=20), nullable=False),
    sa.Column('contract_request_set_hash', sa.String(length=64), nullable=False),
    sa.Column('last_complete_local_day', sa.Date(), nullable=True),
    sa.Column('safe_watermark', sa.Date(), nullable=False),
    sa.Column('terminal_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('canonical_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("(meta_mode = 'controlled_mock' AND scenario_scope_key != '__no_scenario__') OR (meta_mode != 'controlled_mock' AND scenario_scope_key = '__no_scenario__')", name=op.f('ck_scale_evidence_source_manifests_ck_sesm_scenario')),
    sa.CheckConstraint("(state != 'complete') OR (last_complete_local_day IS NOT NULL)", name=op.f('ck_scale_evidence_source_manifests_ck_sesm_complete_last_day')),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_evidence_source_manifests_ck_sesm_meta_mode')),
    sa.CheckConstraint("state IN ('complete', 'blocked')", name=op.f('ck_scale_evidence_source_manifests_ck_sesm_state')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_evidence_source_manifests_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_evidence_source_manifests')),
    sa.UniqueConstraint('workspace_id', 'meta_mode', 'episode_id', 'source_revision', name='uq_sesm_episode_revision')
    )
    with op.batch_alter_table('scale_evidence_source_manifests', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_evidence_source_manifests_episode_id'), ['episode_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_evidence_source_manifests_workspace_id'), ['workspace_id'], unique=False)
    op.create_table('scale_manifest_items',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('manifest_id', sa.String(length=64), nullable=False),
    sa.Column('contract_type', sa.String(length=40), nullable=False),
    sa.Column('window_role', sa.String(length=20), nullable=True),
    sa.Column('local_day', sa.Date(), nullable=True),
    sa.Column('segment_key', sa.String(length=100), nullable=True),
    sa.Column('state', sa.String(length=20), nullable=False),
    sa.Column('source_record_id', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("(state != 'fulfilled') OR (source_record_id IS NOT NULL)", name=op.f('ck_scale_manifest_items_ck_smi_fulfilled_source')),
    sa.CheckConstraint("contract_type IN ('daily', 'exact_window', 'breakdown_placement', 'breakdown_country', 'breakdown_age_gender', 'delivery_configuration')", name=op.f('ck_scale_manifest_items_ck_smi_contract_type')),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_manifest_items_ck_smi_meta_mode')),
    sa.CheckConstraint("state IN ('fulfilled', 'missing', 'failed')", name=op.f('ck_scale_manifest_items_ck_smi_state')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_manifest_items_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_manifest_items'))
    )
    with op.batch_alter_table('scale_manifest_items', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_manifest_items_manifest_id'), ['manifest_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_manifest_items_workspace_id'), ['workspace_id'], unique=False)
    op.create_table('scale_source_quality_issues',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('manifest_id', sa.String(length=64), nullable=False),
    sa.Column('code', sa.String(length=60), nullable=False),
    sa.Column('severity', sa.String(length=20), nullable=False),
    sa.Column('is_blocking', sa.Boolean(), nullable=False),
    sa.Column('contract_type', sa.String(length=40), nullable=True),
    sa.Column('window_role', sa.String(length=20), nullable=True),
    sa.Column('local_day', sa.Date(), nullable=True),
    sa.Column('segment_key', sa.String(length=100), nullable=True),
    sa.Column('detail', sa.Text(), nullable=True),
    sa.Column('recovery_action', sa.String(length=60), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("contract_type IS NULL OR contract_type IN ('daily', 'exact_window', 'breakdown_placement', 'breakdown_country', 'breakdown_age_gender', 'delivery_configuration')", name=op.f('ck_scale_source_quality_issues_ck_ssqi_contract_type')),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_source_quality_issues_ck_ssqi_meta_mode')),
    sa.CheckConstraint("severity IN ('warning', 'blocking')", name=op.f('ck_scale_source_quality_issues_ck_ssqi_severity')),
    sa.CheckConstraint("window_role IS NULL OR window_role IN ('current_7d', 'baseline_7d', 'rolling_14d')", name=op.f('ck_scale_source_quality_issues_ck_ssqi_window_role')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_source_quality_issues_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_source_quality_issues'))
    )
    with op.batch_alter_table('scale_source_quality_issues', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_source_quality_issues_manifest_id'), ['manifest_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_source_quality_issues_workspace_id'), ['workspace_id'], unique=False)
    op.create_table('scale_evidence_readiness_projections',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('episode_id', sa.String(length=64), nullable=False),
    sa.Column('latest_manifest_id', sa.String(length=64), nullable=True),
    sa.Column('maturity', sa.String(length=30), nullable=False),
    sa.Column('quality_state', sa.String(length=20), nullable=False),
    sa.Column('last_complete_local_day', sa.Date(), nullable=True),
    sa.Column('freshness_reference_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('blocking_issue_count', sa.Integer(), nullable=False),
    sa.Column('warning_issue_count', sa.Integer(), nullable=False),
    sa.Column('recovery_action', sa.String(length=60), nullable=True),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("maturity IN ('warming', 'monitorable', 'economics_ready', 'insufficient')", name=op.f('ck_scale_evidence_readiness_projections_ck_serp_maturity')),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_evidence_readiness_projections_ck_serp_meta_mode')),
    sa.CheckConstraint("quality_state IN ('pass', 'warning', 'blocked')", name=op.f('ck_scale_evidence_readiness_projections_ck_serp_quality')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_evidence_readiness_projections_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_evidence_readiness_projections')),
    sa.UniqueConstraint('workspace_id', 'meta_mode', 'episode_id', name='uq_serp_episode')
    )
    with op.batch_alter_table('scale_evidence_readiness_projections', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_evidence_readiness_projections_episode_id'), ['episode_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_evidence_readiness_projections_workspace_id'), ['workspace_id'], unique=False)


def _step6_snapshot():
    op.create_table('scale_evidence_snapshots',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('meta_connection_id', sa.String(length=64), nullable=False),
    sa.Column('scenario_scope_key', sa.String(length=64), nullable=False),
    sa.Column('ad_account_id', sa.String(length=100), nullable=False),
    sa.Column('episode_id', sa.String(length=64), nullable=False),
    sa.Column('remote_binding_key', sa.String(length=200), nullable=False),
    sa.Column('claim_hash', sa.String(length=64), nullable=False),
    sa.Column('source_manifest_id', sa.String(length=64), nullable=False),
    sa.Column('source_manifest_hash', sa.String(length=64), nullable=False),
    sa.Column('source_revision', sa.Integer(), nullable=False),
    sa.Column('safe_watermark', sa.Date(), nullable=False),
    sa.Column('policy_version_id', sa.String(length=64), nullable=False),
    sa.Column('target_version_id', sa.String(length=64), nullable=True),
    sa.Column('normalized_target_key', sa.String(length=64), nullable=False),
    sa.Column('algorithm_version', sa.String(length=20), nullable=False),
    sa.Column('canonical_schema_version', sa.String(length=20), nullable=False),
    sa.Column('metric_semantic_contract_set_id', sa.String(length=64), nullable=False),
    sa.Column('metric_semantic_contract_set_hash', sa.String(length=64), nullable=False),
    sa.Column('delivery_config_source_id', sa.String(length=64), nullable=False),
    sa.Column('delivery_evidence_hash', sa.String(length=64), nullable=False),
    sa.Column('delivery_comparability_hash', sa.String(length=64), nullable=False),
    sa.Column('timezone_name', sa.String(length=64), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('attribution_semantics_hash', sa.String(length=64), nullable=False),
    sa.Column('current_window_start_local_day', sa.Date(), nullable=False),
    sa.Column('current_window_end_local_day', sa.Date(), nullable=False),
    sa.Column('baseline_window_start_local_day', sa.Date(), nullable=False),
    sa.Column('baseline_window_end_local_day', sa.Date(), nullable=False),
    sa.Column('rolling_window_start_local_day', sa.Date(), nullable=False),
    sa.Column('rolling_window_end_local_day', sa.Date(), nullable=False),
    sa.Column('complete_local_days_in_episode', sa.Integer(), nullable=False),
    sa.Column('active_delivery_days_in_episode', sa.Integer(), nullable=False),
    sa.Column('data_continuity_generation', sa.String(length=64), nullable=False),
    sa.Column('prior_evaluation_id', sa.String(length=64), nullable=True),
    sa.Column('prior_evaluated_local_day', sa.Date(), nullable=True),
    sa.Column('prior_streak_compat_policy_version_id', sa.String(length=64), nullable=True),
    sa.Column('prior_streak_compat_target_version_id', sa.String(length=64), nullable=True),
    sa.Column('prior_streak_compat_metric_contract_set_hash', sa.String(length=64), nullable=True),
    sa.Column('prior_streak_compat_configuration_baseline_hash', sa.String(length=64), nullable=True),
    sa.Column('prior_streak_compat_data_continuity_generation', sa.String(length=64), nullable=True),
    sa.Column('prior_streak_compat_canonical_hash', sa.String(length=64), nullable=True),
    sa.Column('prior_health', sa.String(length=30), nullable=True),
    sa.Column('prior_healthy_evaluations', sa.Integer(), nullable=True),
    sa.Column('prior_streak_counters', sa.JSON(), nullable=True),
    sa.Column('prior_streak_absence_reason', sa.String(length=40), nullable=True),
    sa.Column('last_complete_local_day', sa.Date(), nullable=False),
    sa.Column('last_sync_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('freshness_reference_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('as_of_local_day', sa.Date(), nullable=False),
    sa.Column('canonical_hash', sa.String(length=64), nullable=True),
    sa.Column('sealed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("(meta_mode = 'controlled_mock' AND scenario_scope_key != '__no_scenario__') OR (meta_mode != 'controlled_mock' AND scenario_scope_key = '__no_scenario__')", name=op.f('ck_scale_evidence_snapshots_ck_sesn_scenario_biconditional')),
    sa.CheckConstraint("(prior_evaluation_id IS NOT NULL AND prior_evaluated_local_day IS NOT NULL AND prior_streak_compat_policy_version_id IS NOT NULL AND prior_streak_compat_metric_contract_set_hash IS NOT NULL AND prior_streak_compat_configuration_baseline_hash IS NOT NULL AND prior_streak_compat_data_continuity_generation IS NOT NULL AND prior_streak_compat_canonical_hash IS NOT NULL AND prior_healthy_evaluations IS NOT NULL AND prior_streak_absence_reason IS NULL) OR (prior_evaluation_id IS NULL AND prior_evaluated_local_day IS NULL AND prior_streak_compat_policy_version_id IS NULL AND prior_streak_compat_metric_contract_set_hash IS NULL AND prior_streak_compat_configuration_baseline_hash IS NULL AND prior_streak_compat_data_continuity_generation IS NULL AND prior_streak_compat_canonical_hash IS NULL AND prior_healthy_evaluations IS NULL AND prior_streak_absence_reason IN ('no_earlier_distinct_day'))", name=op.f('ck_scale_evidence_snapshots_ck_sesn_prior_streak_shape')),
    sa.CheckConstraint("(target_version_id IS NULL AND normalized_target_key = '__no_target__') OR (target_version_id IS NOT NULL AND normalized_target_key = target_version_id)", name=op.f('ck_scale_evidence_snapshots_ck_sesn_target_biconditional')),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_evidence_snapshots_ck_sesn_meta_mode')),
    sa.CheckConstraint('(sealed_at IS NULL) = (canonical_hash IS NULL)', name=op.f('ck_scale_evidence_snapshots_ck_sesn_sealed_shape')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_evidence_snapshots_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_evidence_snapshots')),
    sa.UniqueConstraint('workspace_id', 'meta_mode', 'canonical_schema_version', 'canonical_hash', name='uq_sesn_sealed_content'),
    sa.UniqueConstraint('workspace_id', 'meta_mode', 'claim_hash', name='uq_sesn_claim_hash')
    )
    with op.batch_alter_table('scale_evidence_snapshots', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_evidence_snapshots_episode_id'), ['episode_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_evidence_snapshots_workspace_id'), ['workspace_id'], unique=False)
    op.create_table('scale_evidence_source_links',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('snapshot_id', sa.String(length=64), nullable=False),
    sa.Column('contract_type', sa.String(length=40), nullable=False),
    sa.Column('window_role', sa.String(length=20), nullable=True),
    sa.Column('local_day', sa.Date(), nullable=True),
    sa.Column('segment_key', sa.String(length=100), nullable=True),
    sa.Column('source_record_id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("contract_type IN ('daily', 'exact_window', 'breakdown_placement', 'breakdown_country', 'breakdown_age_gender', 'delivery_configuration')", name=op.f('ck_scale_evidence_source_links_ck_sesl_contract_type')),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_evidence_source_links_ck_sesl_meta_mode')),
    sa.CheckConstraint("window_role IS NULL OR window_role IN ('current_7d', 'baseline_7d', 'rolling_14d')", name=op.f('ck_scale_evidence_source_links_ck_sesl_window_role')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_evidence_source_links_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_evidence_source_links')),
    sa.UniqueConstraint('snapshot_id', 'contract_type', 'window_role', 'local_day', 'segment_key', name='uq_sesl_snapshot_role')
    )
    with op.batch_alter_table('scale_evidence_source_links', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_evidence_source_links_snapshot_id'), ['snapshot_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_evidence_source_links_workspace_id'), ['workspace_id'], unique=False)
    op.create_table('scale_snapshot_quality_issues',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('snapshot_id', sa.String(length=64), nullable=False),
    sa.Column('code', sa.String(length=60), nullable=False),
    sa.Column('severity', sa.String(length=20), nullable=False),
    sa.Column('is_blocking', sa.Boolean(), nullable=False),
    sa.Column('contract_type', sa.String(length=40), nullable=True),
    sa.Column('window_role', sa.String(length=20), nullable=True),
    sa.Column('local_day', sa.Date(), nullable=True),
    sa.Column('segment_key', sa.String(length=100), nullable=True),
    sa.Column('detail', sa.Text(), nullable=True),
    sa.Column('recovery_action', sa.String(length=60), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("contract_type IS NULL OR contract_type IN ('daily', 'exact_window', 'breakdown_placement', 'breakdown_country', 'breakdown_age_gender', 'delivery_configuration')", name=op.f('ck_scale_snapshot_quality_issues_ck_snqi_contract_type')),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_snapshot_quality_issues_ck_snqi_meta_mode')),
    sa.CheckConstraint("severity IN ('warning', 'blocking')", name=op.f('ck_scale_snapshot_quality_issues_ck_snqi_severity')),
    sa.CheckConstraint("window_role IS NULL OR window_role IN ('current_7d', 'baseline_7d', 'rolling_14d')", name=op.f('ck_scale_snapshot_quality_issues_ck_snqi_window_role')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_snapshot_quality_issues_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_snapshot_quality_issues'))
    )
    with op.batch_alter_table('scale_snapshot_quality_issues', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_snapshot_quality_issues_snapshot_id'), ['snapshot_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_snapshot_quality_issues_workspace_id'), ['workspace_id'], unique=False)
    op.create_table('scale_snapshot_breakdown_segments',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('snapshot_id', sa.String(length=64), nullable=False),
    sa.Column('breakdown_type', sa.String(length=20), nullable=False),
    sa.Column('segment_key', sa.String(length=100), nullable=False),
    sa.Column('state', sa.String(length=20), nullable=False),
    sa.Column('suppression_reason', sa.String(length=40), nullable=True),
    sa.Column('source_record_id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("(state = 'observed' AND suppression_reason IS NULL) OR (state = 'privacy_suppressed' AND suppression_reason IN ('api_privacy_threshold', 'api_policy_omission'))", name=op.f('ck_scale_snapshot_breakdown_segments_ck_ssbs_state_shape')),
    sa.CheckConstraint("breakdown_type IN ('placement', 'country', 'age_gender')", name=op.f('ck_scale_snapshot_breakdown_segments_ck_ssbs_breakdown_type')),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_snapshot_breakdown_segments_ck_ssbs_meta_mode')),
    sa.CheckConstraint("state IN ('observed', 'privacy_suppressed')", name=op.f('ck_scale_snapshot_breakdown_segments_ck_ssbs_state')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_snapshot_breakdown_segments_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_snapshot_breakdown_segments')),
    sa.UniqueConstraint('snapshot_id', 'breakdown_type', 'segment_key', name='uq_ssbs_snapshot_segment')
    )
    with op.batch_alter_table('scale_snapshot_breakdown_segments', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_snapshot_breakdown_segments_snapshot_id'), ['snapshot_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_snapshot_breakdown_segments_workspace_id'), ['workspace_id'], unique=False)
    op.create_table('scale_snapshot_breakdown_daily',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('breakdown_segment_id', sa.String(length=64), nullable=False),
    sa.Column('local_day', sa.Date(), nullable=False),
    sa.Column('state', sa.String(length=20), nullable=False),
    sa.Column('delivered', sa.Boolean(), nullable=True),
    sa.Column('spend', sa.Numeric(precision=24, scale=12), nullable=True),
    sa.Column('impressions', sa.Integer(), nullable=True),
    sa.Column('link_clicks', sa.Integer(), nullable=True),
    sa.Column('purchases', sa.Integer(), nullable=True),
    sa.Column('purchase_value', sa.Numeric(precision=24, scale=12), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("(state = 'present' AND delivered IS NOT NULL AND spend IS NOT NULL AND impressions IS NOT NULL AND link_clicks IS NOT NULL AND purchases IS NOT NULL AND purchase_value IS NOT NULL) OR (state = 'missing' AND delivered IS NULL AND spend IS NULL AND impressions IS NULL AND link_clicks IS NULL AND purchases IS NULL AND purchase_value IS NULL)", name=op.f('ck_scale_snapshot_breakdown_daily_ck_ssbd_state_shape')),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_snapshot_breakdown_daily_ck_ssbd_meta_mode')),
    sa.CheckConstraint("state IN ('present', 'missing')", name=op.f('ck_scale_snapshot_breakdown_daily_ck_ssbd_state')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_snapshot_breakdown_daily_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_snapshot_breakdown_daily')),
    sa.UniqueConstraint('breakdown_segment_id', 'local_day', name='uq_ssbd_segment_day')
    )
    with op.batch_alter_table('scale_snapshot_breakdown_daily', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_snapshot_breakdown_daily_breakdown_segment_id'), ['breakdown_segment_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_snapshot_breakdown_daily_workspace_id'), ['workspace_id'], unique=False)


def _step7_evaluation():
    op.create_table('scale_evaluation_runs',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('meta_connection_id', sa.String(length=64), nullable=False),
    sa.Column('scenario_scope_key', sa.String(length=64), nullable=False),
    sa.Column('episode_id', sa.String(length=64), nullable=False),
    sa.Column('enrollment_id', sa.String(length=64), nullable=False),
    sa.Column('target_local_day', sa.Date(), nullable=False),
    sa.Column('trigger_kind', sa.String(length=20), nullable=False),
    sa.Column('source_manifest_id', sa.String(length=64), nullable=False),
    sa.Column('source_manifest_hash', sa.String(length=64), nullable=False),
    sa.Column('source_revision', sa.Integer(), nullable=False),
    sa.Column('freshness_reference_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('policy_version_id', sa.String(length=64), nullable=False),
    sa.Column('target_version_id', sa.String(length=64), nullable=True),
    sa.Column('normalized_target_key', sa.String(length=64), nullable=False),
    sa.Column('algorithm_version', sa.String(length=20), nullable=False),
    sa.Column('semantic_event_sequence', sa.Integer(), nullable=False),
    sa.Column('replay_generation', sa.Integer(), nullable=False),
    sa.Column('replay_base_evaluation_id', sa.String(length=64), nullable=True),
    sa.Column('triggering_sync_run_id', sa.String(length=64), nullable=True),
    sa.Column('triggering_semantic_event_id', sa.String(length=64), nullable=True),
    sa.Column('original_initiator_id', sa.String(length=100), nullable=False),
    sa.Column('cancellation_generation', sa.Integer(), nullable=False),
    sa.Column('run_state', sa.String(length=30), nullable=False),
    sa.Column('snapshot_id', sa.String(length=64), nullable=True),
    sa.Column('evaluation_id', sa.String(length=64), nullable=True),
    sa.Column('outcome_kind', sa.String(length=30), nullable=True),
    sa.Column('outcome_detail', sa.Text(), nullable=True),
    sa.Column('lease_owner', sa.String(length=100), nullable=True),
    sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("(meta_mode = 'controlled_mock' AND scenario_scope_key != '__no_scenario__') OR (meta_mode != 'controlled_mock' AND scenario_scope_key = '__no_scenario__')", name=op.f('ck_scale_evaluation_runs_ck_sevr_scenario_biconditional')),
    sa.CheckConstraint("(run_state != 'evaluated') OR (evaluation_id IS NOT NULL)", name=op.f('ck_scale_evaluation_runs_ck_sevr_evaluated_shape')),
    sa.CheckConstraint("(run_state = 'requested') OR (snapshot_id IS NOT NULL)", name=op.f('ck_scale_evaluation_runs_ck_sevr_snapshot_shape')),
    sa.CheckConstraint("(run_state IN ('requested', 'snapshot_sealed')) = (outcome_kind IS NULL)", name=op.f('ck_scale_evaluation_runs_ck_sevr_outcome_shape')),
    sa.CheckConstraint("(target_version_id IS NULL AND normalized_target_key = '__no_target__') OR (target_version_id IS NOT NULL AND normalized_target_key = target_version_id)", name=op.f('ck_scale_evaluation_runs_ck_sevr_target_biconditional')),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_evaluation_runs_ck_sevr_meta_mode')),
    sa.CheckConstraint("outcome_kind IS NULL OR outcome_kind IN ('evaluated', 'already_evaluated', 'episode_rolled_over', 'blocked', 'cancelled', 'failed_integrity')", name=op.f('ck_scale_evaluation_runs_ck_sevr_outcome_kind')),
    sa.CheckConstraint("run_state IN ('requested', 'snapshot_sealed', 'evaluated', 'episode_rolled_over', 'blocked', 'cancelled', 'failed_integrity')", name=op.f('ck_scale_evaluation_runs_ck_sevr_run_state')),
    sa.CheckConstraint("trigger_kind IN ('live', 'backfill', 'policy_change', 'target_change', 'forward_replay')", name=op.f('ck_scale_evaluation_runs_ck_sevr_trigger_kind')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_evaluation_runs_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_evaluation_runs')),
    sa.UniqueConstraint('workspace_id', 'meta_mode', 'episode_id', 'target_local_day', 'trigger_kind', 'source_manifest_hash', 'source_revision', 'freshness_reference_at', 'policy_version_id', 'normalized_target_key', 'algorithm_version', 'semantic_event_sequence', 'replay_generation', name='uq_sevr_semantic_key')
    )
    with op.batch_alter_table('scale_evaluation_runs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_evaluation_runs_enrollment_id'), ['enrollment_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_evaluation_runs_episode_id'), ['episode_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_evaluation_runs_workspace_id'), ['workspace_id'], unique=False)
    op.create_table('scale_evaluations',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('episode_id', sa.String(length=64), nullable=False),
    sa.Column('snapshot_id', sa.String(length=64), nullable=False),
    sa.Column('snapshot_canonical_hash', sa.String(length=64), nullable=False),
    sa.Column('metric_semantic_contract_set_hash', sa.String(length=64), nullable=False),
    sa.Column('policy_version_id', sa.String(length=64), nullable=False),
    sa.Column('target_version_id', sa.String(length=64), nullable=True),
    sa.Column('normalized_target_key', sa.String(length=64), nullable=False),
    sa.Column('algorithm_version', sa.String(length=20), nullable=False),
    sa.Column('canonical_schema_version', sa.String(length=20), nullable=False),
    sa.Column('maturity', sa.String(length=30), nullable=False),
    sa.Column('data_quality_state', sa.String(length=20), nullable=False),
    sa.Column('health', sa.String(length=30), nullable=True),
    sa.Column('health_reason', sa.String(length=60), nullable=False),
    sa.Column('streak_compat_policy_version_id', sa.String(length=64), nullable=False),
    sa.Column('streak_compat_target_version_id', sa.String(length=64), nullable=True),
    sa.Column('streak_compat_metric_contract_set_hash', sa.String(length=64), nullable=False),
    sa.Column('streak_compat_configuration_baseline_hash', sa.String(length=64), nullable=False),
    sa.Column('streak_compat_data_continuity_generation', sa.String(length=64), nullable=False),
    sa.Column('streak_compat_canonical_hash', sa.String(length=64), nullable=False),
    sa.Column('streak_healthy_evaluations', sa.Integer(), nullable=False),
    sa.Column('canonical_result_payload', sa.JSON(), nullable=False),
    sa.Column('result_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("(target_version_id IS NULL AND normalized_target_key = '__no_target__') OR (target_version_id IS NOT NULL AND normalized_target_key = target_version_id)", name=op.f('ck_scale_evaluations_ck_sev_target_biconditional')),
    sa.CheckConstraint("data_quality_state IN ('pass', 'warning', 'blocked')", name=op.f('ck_scale_evaluations_ck_sev_data_quality_state')),
    sa.CheckConstraint("health IS NULL OR health IN ('healthy', 'fatigue_watch', 'creative_fatigue', 'economics_decline', 'delivery_anomaly', 'not_delivering')", name=op.f('ck_scale_evaluations_ck_sev_health')),
    sa.CheckConstraint("maturity IN ('warming', 'monitorable', 'economics_ready', 'insufficient')", name=op.f('ck_scale_evaluations_ck_sev_maturity')),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_evaluations_ck_sev_meta_mode')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_evaluations_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_evaluations')),
    sa.UniqueConstraint('workspace_id', 'meta_mode', 'episode_id', 'snapshot_canonical_hash', 'metric_semantic_contract_set_hash', 'policy_version_id', 'normalized_target_key', 'algorithm_version', name='uq_sev_semantic_fingerprint')
    )
    with op.batch_alter_table('scale_evaluations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_evaluations_episode_id'), ['episode_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_evaluations_snapshot_id'), ['snapshot_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_evaluations_workspace_id'), ['workspace_id'], unique=False)
    op.create_table('scale_evaluation_signals',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('evaluation_id', sa.String(length=64), nullable=False),
    sa.Column('signal_id', sa.String(length=100), nullable=False),
    sa.Column('state', sa.String(length=20), nullable=False),
    sa.Column('expected', sa.Numeric(precision=24, scale=12), nullable=True),
    sa.Column('observed', sa.Numeric(precision=24, scale=12), nullable=True),
    sa.Column('interval_lower', sa.Numeric(precision=24, scale=12), nullable=True),
    sa.Column('interval_upper', sa.Numeric(precision=24, scale=12), nullable=True),
    sa.Column('threshold', sa.Numeric(precision=24, scale=12), nullable=True),
    sa.Column('p_value', sa.Numeric(precision=24, scale=12), nullable=True),
    sa.Column('adjusted_q_value', sa.Numeric(precision=24, scale=12), nullable=True),
    sa.Column('reason_code', sa.String(length=60), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_evaluation_signals_ck_sevs_meta_mode')),
    sa.CheckConstraint("state IN ('hit', 'miss', 'unavailable', 'blocked')", name=op.f('ck_scale_evaluation_signals_ck_sevs_state')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_evaluation_signals_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_evaluation_signals')),
    sa.UniqueConstraint('evaluation_id', 'signal_id', name='uq_sevs_evaluation_signal')
    )
    with op.batch_alter_table('scale_evaluation_signals', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_evaluation_signals_evaluation_id'), ['evaluation_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_evaluation_signals_workspace_id'), ['workspace_id'], unique=False)
    op.create_table('scale_evaluation_advice_candidates',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('evaluation_id', sa.String(length=64), nullable=False),
    sa.Column('advice_type', sa.String(length=40), nullable=False),
    sa.Column('reason_signature', sa.String(length=128), nullable=False),
    sa.Column('severity', sa.String(length=20), nullable=False),
    sa.Column('signal_ids', sa.JSON(), nullable=False),
    sa.Column('evidence', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("advice_type IN ('refresh_creative', 'review_offer_or_landing_page', 'review_delivery_mix', 'investigate_delivery_anomaly', 'consider_scale_test')", name=op.f('ck_scale_evaluation_advice_candidates_ck_seac_advice_type')),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_evaluation_advice_candidates_ck_seac_meta_mode')),
    sa.CheckConstraint("severity IN ('info', 'warning', 'critical')", name=op.f('ck_scale_evaluation_advice_candidates_ck_seac_severity')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_evaluation_advice_candidates_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_evaluation_advice_candidates')),
    sa.UniqueConstraint('evaluation_id', 'reason_signature', name='uq_seac_evaluation_reason')
    )
    with op.batch_alter_table('scale_evaluation_advice_candidates', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_evaluation_advice_candidates_evaluation_id'), ['evaluation_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_evaluation_advice_candidates_workspace_id'), ['workspace_id'], unique=False)
    op.create_table('scale_evaluation_streak_counters',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('evaluation_id', sa.String(length=64), nullable=False),
    sa.Column('signal_key', sa.String(length=100), nullable=False),
    sa.Column('consecutive_hits', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_evaluation_streak_counters_ck_sesc_meta_mode')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_evaluation_streak_counters_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_evaluation_streak_counters')),
    sa.UniqueConstraint('evaluation_id', 'signal_key', name='uq_sesc_evaluation_signal')
    )
    with op.batch_alter_table('scale_evaluation_streak_counters', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_evaluation_streak_counters_evaluation_id'), ['evaluation_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_evaluation_streak_counters_workspace_id'), ['workspace_id'], unique=False)
    op.create_table('scale_evaluation_streak_resets',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('evaluation_id', sa.String(length=64), nullable=False),
    sa.Column('streak_id', sa.String(length=100), nullable=False),
    sa.Column('previous_value', sa.Integer(), nullable=False),
    sa.Column('reason_code', sa.String(length=60), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_evaluation_streak_resets_ck_sesr_meta_mode')),
    sa.CheckConstraint('previous_value >= 0', name=op.f('ck_scale_evaluation_streak_resets_ck_sesr_prev_value')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_evaluation_streak_resets_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_evaluation_streak_resets')),
    sa.UniqueConstraint('evaluation_id', 'streak_id', 'reason_code', name='uq_sesr_evaluation_streak_reason')
    )
    with op.batch_alter_table('scale_evaluation_streak_resets', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_evaluation_streak_resets_evaluation_id'), ['evaluation_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_evaluation_streak_resets_workspace_id'), ['workspace_id'], unique=False)
    op.create_table('scale_episode_projections',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('episode_id', sa.String(length=64), nullable=False),
    sa.Column('evaluation_id', sa.String(length=64), nullable=False),
    sa.Column('maturity', sa.String(length=30), nullable=False),
    sa.Column('health', sa.String(length=30), nullable=True),
    sa.Column('health_reason', sa.String(length=60), nullable=False),
    sa.Column('healthy_evaluations', sa.Integer(), nullable=False),
    sa.Column('streak_counters', sa.JSON(), nullable=False),
    sa.Column('cursor_as_of_local_day', sa.Date(), nullable=False),
    sa.Column('cursor_source_revision', sa.Integer(), nullable=False),
    sa.Column('cursor_semantic_event_sequence', sa.Integer(), nullable=False),
    sa.Column('cursor_lineage_generation', sa.Integer(), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("health IS NULL OR health IN ('healthy', 'fatigue_watch', 'creative_fatigue', 'economics_decline', 'delivery_anomaly', 'not_delivering')", name=op.f('ck_scale_episode_projections_ck_sepp_health')),
    sa.CheckConstraint("maturity IN ('warming', 'monitorable', 'economics_ready', 'insufficient')", name=op.f('ck_scale_episode_projections_ck_sepp_maturity')),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_episode_projections_ck_sepp_meta_mode')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_episode_projections_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_episode_projections')),
    sa.UniqueConstraint('workspace_id', 'meta_mode', 'episode_id', name='uq_sepp_episode')
    )
    with op.batch_alter_table('scale_episode_projections', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_episode_projections_episode_id'), ['episode_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_episode_projections_workspace_id'), ['workspace_id'], unique=False)


def _step8_advice():
    op.create_table('scale_advice_threads',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('episode_id', sa.String(length=64), nullable=False),
    sa.Column('enrollment_id', sa.String(length=64), nullable=False),
    sa.Column('advice_type', sa.String(length=40), nullable=False),
    sa.Column('reason_signature', sa.String(length=128), nullable=False),
    sa.Column('severity', sa.String(length=20), nullable=False),
    sa.Column('lifecycle', sa.String(length=20), nullable=False),
    sa.Column('predecessor_thread_id', sa.String(length=64), nullable=True),
    sa.Column('first_occurrence_id', sa.String(length=64), nullable=False),
    sa.Column('latest_occurrence_id', sa.String(length=64), nullable=False),
    sa.Column('disposition_kind', sa.String(length=30), nullable=True),
    sa.Column('disposition_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('snooze_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('opened_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('lifecycle_reason', sa.String(length=100), nullable=True),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("(lifecycle = 'resolved') = (resolved_at IS NOT NULL)", name=op.f('ck_scale_advice_threads_ck_sat_resolved_shape')),
    sa.CheckConstraint("advice_type IN ('refresh_creative', 'review_offer_or_landing_page', 'review_delivery_mix', 'investigate_delivery_anomaly', 'consider_scale_test')", name=op.f('ck_scale_advice_threads_ck_sat_advice_type')),
    sa.CheckConstraint("disposition_kind IS NULL OR disposition_kind IN ('acknowledged', 'dismissed', 'converted_to_draft')", name=op.f('ck_scale_advice_threads_ck_sat_disposition_kind')),
    sa.CheckConstraint("lifecycle IN ('active', 'resolved', 'superseded', 'expired')", name=op.f('ck_scale_advice_threads_ck_sat_lifecycle')),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_advice_threads_ck_sat_meta_mode')),
    sa.CheckConstraint("severity IN ('info', 'warning', 'critical')", name=op.f('ck_scale_advice_threads_ck_sat_severity')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_advice_threads_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_advice_threads'))
    )
    with op.batch_alter_table('scale_advice_threads', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_advice_threads_enrollment_id'), ['enrollment_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_advice_threads_episode_id'), ['episode_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_advice_threads_workspace_id'), ['workspace_id'], unique=False)
    op.create_table('scale_advice_occurrences',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('episode_id', sa.String(length=64), nullable=False),
    sa.Column('enrollment_id', sa.String(length=64), nullable=False),
    sa.Column('evaluation_id', sa.String(length=64), nullable=False),
    sa.Column('candidate_id', sa.String(length=64), nullable=False),
    sa.Column('snapshot_id', sa.String(length=64), nullable=False),
    sa.Column('thread_id', sa.String(length=64), nullable=False),
    sa.Column('advice_type', sa.String(length=40), nullable=False),
    sa.Column('reason_signature', sa.String(length=128), nullable=False),
    sa.Column('severity', sa.String(length=20), nullable=False),
    sa.Column('signal_ids', sa.JSON(), nullable=False),
    sa.Column('evidence', sa.JSON(), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("advice_type IN ('refresh_creative', 'review_offer_or_landing_page', 'review_delivery_mix', 'investigate_delivery_anomaly', 'consider_scale_test')", name=op.f('ck_scale_advice_occurrences_ck_sao_advice_type')),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_advice_occurrences_ck_sao_meta_mode')),
    sa.CheckConstraint("severity IN ('info', 'warning', 'critical')", name=op.f('ck_scale_advice_occurrences_ck_sao_severity')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_advice_occurrences_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_advice_occurrences')),
    sa.UniqueConstraint('workspace_id', 'meta_mode', 'evaluation_id', 'advice_type', 'reason_signature', name='uq_sao_evaluation_reason')
    )
    with op.batch_alter_table('scale_advice_occurrences', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_advice_occurrences_enrollment_id'), ['enrollment_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_advice_occurrences_episode_id'), ['episode_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_advice_occurrences_evaluation_id'), ['evaluation_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_advice_occurrences_thread_id'), ['thread_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_advice_occurrences_workspace_id'), ['workspace_id'], unique=False)
    op.create_table('scale_advice_thread_events',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('episode_id', sa.String(length=64), nullable=False),
    sa.Column('thread_id', sa.String(length=64), nullable=False),
    sa.Column('occurrence_id', sa.String(length=64), nullable=True),
    sa.Column('event_type', sa.String(length=20), nullable=False),
    sa.Column('previous_lifecycle', sa.String(length=20), nullable=True),
    sa.Column('current_lifecycle', sa.String(length=20), nullable=False),
    sa.Column('successor_thread_id', sa.String(length=64), nullable=True),
    sa.Column('reason', sa.String(length=100), nullable=True),
    sa.Column('deduplication_key', sa.String(length=128), nullable=False),
    sa.Column('executor_id', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("(event_type != 'superseded') OR (successor_thread_id IS NOT NULL)", name=op.f('ck_scale_advice_thread_events_ck_sate_superseded_shape')),
    sa.CheckConstraint("current_lifecycle IN ('active', 'resolved', 'superseded', 'expired')", name=op.f('ck_scale_advice_thread_events_ck_sate_current_lifecycle')),
    sa.CheckConstraint("event_type IN ('created', 'resolved', 'superseded', 'expired')", name=op.f('ck_scale_advice_thread_events_ck_sate_event_type')),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_advice_thread_events_ck_sate_meta_mode')),
    sa.CheckConstraint("previous_lifecycle IS NULL OR previous_lifecycle IN ('active', 'resolved', 'superseded', 'expired')", name=op.f('ck_scale_advice_thread_events_ck_sate_previous_lifecycle')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_advice_thread_events_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_advice_thread_events')),
    sa.UniqueConstraint('workspace_id', 'meta_mode', 'deduplication_key', name='uq_sate_workspace_mode_dedup')
    )
    with op.batch_alter_table('scale_advice_thread_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_advice_thread_events_episode_id'), ['episode_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_advice_thread_events_thread_id'), ['thread_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_advice_thread_events_workspace_id'), ['workspace_id'], unique=False)
    op.create_table('scale_advice_dispositions',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('thread_id', sa.String(length=64), nullable=False),
    sa.Column('occurrence_id', sa.String(length=64), nullable=False),
    sa.Column('kind', sa.String(length=30), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('linked_conversion_id', sa.String(length=64), nullable=True),
    sa.Column('actor_id', sa.String(length=100), nullable=False),
    sa.Column('idempotency_key', sa.String(length=128), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("(kind = 'converted_to_draft') = (linked_conversion_id IS NOT NULL)", name=op.f('ck_scale_advice_dispositions_ck_sad_conversion_shape')),
    sa.CheckConstraint("kind IN ('acknowledged', 'dismissed', 'converted_to_draft')", name=op.f('ck_scale_advice_dispositions_ck_sad_kind')),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_advice_dispositions_ck_sad_meta_mode')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_advice_dispositions_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_advice_dispositions'))
    )
    with op.batch_alter_table('scale_advice_dispositions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_advice_dispositions_occurrence_id'), ['occurrence_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_advice_dispositions_thread_id'), ['thread_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_advice_dispositions_workspace_id'), ['workspace_id'], unique=False)
    op.create_table('scale_advice_snoozes',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('thread_id', sa.String(length=64), nullable=False),
    sa.Column('actor_id', sa.String(length=100), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('snooze_until', sa.DateTime(timezone=True), nullable=False),
    sa.Column('reason', sa.String(length=100), nullable=True),
    sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancelled_by', sa.String(length=100), nullable=True),
    sa.Column('cancellation_reason', sa.String(length=100), nullable=True),
    sa.Column('idempotency_key', sa.String(length=128), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_advice_snoozes_ck_sas_meta_mode')),
    sa.CheckConstraint('(cancelled_at IS NULL) = (cancelled_by IS NULL)', name=op.f('ck_scale_advice_snoozes_ck_sas_cancel_shape')),
    sa.CheckConstraint('snooze_until > started_at', name=op.f('ck_scale_advice_snoozes_ck_sas_until_after_start')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_advice_snoozes_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_advice_snoozes'))
    )
    with op.batch_alter_table('scale_advice_snoozes', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_advice_snoozes_thread_id'), ['thread_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_advice_snoozes_workspace_id'), ['workspace_id'], unique=False)
    op.create_table('scale_advice_notification_decisions',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('thread_id', sa.String(length=64), nullable=False),
    sa.Column('occurrence_id', sa.String(length=64), nullable=False),
    sa.Column('decision', sa.String(length=20), nullable=False),
    sa.Column('suppression_reason', sa.String(length=100), nullable=True),
    sa.Column('cooldown_hours_applied', sa.Integer(), nullable=True),
    sa.Column('notification_outbox_id', sa.String(length=64), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("(decision = 'send') = (notification_outbox_id IS NOT NULL)", name=op.f('ck_scale_advice_notification_decisions_ck_sand_send_shape')),
    sa.CheckConstraint("(decision = 'suppress') = (suppression_reason IS NOT NULL)", name=op.f('ck_scale_advice_notification_decisions_ck_sand_suppress_shape')),
    sa.CheckConstraint("decision IN ('send', 'suppress')", name=op.f('ck_scale_advice_notification_decisions_ck_sand_decision')),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_advice_notification_decisions_ck_sand_meta_mode')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_advice_notification_decisions_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_advice_notification_decisions')),
    sa.UniqueConstraint('workspace_id', 'meta_mode', 'occurrence_id', name='uq_sand_occurrence')
    )
    with op.batch_alter_table('scale_advice_notification_decisions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_advice_notification_decisions_thread_id'), ['thread_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_advice_notification_decisions_workspace_id'), ['workspace_id'], unique=False)


def _step9_conversion_outcome_export():
    op.create_table('scale_advice_conversions',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('thread_id', sa.String(length=64), nullable=False),
    sa.Column('occurrence_id', sa.String(length=64), nullable=False),
    sa.Column('generation_draft_id', sa.String(length=64), nullable=True),
    sa.Column('scale_test_plan_draft_id', sa.String(length=64), nullable=True),
    sa.Column('input_lineage_hash', sa.String(length=64), nullable=False),
    sa.Column('initiator_id', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_advice_conversions_ck_sacr_meta_mode')),
    sa.CheckConstraint('(generation_draft_id IS NOT NULL AND scale_test_plan_draft_id IS NULL) OR (generation_draft_id IS NULL AND scale_test_plan_draft_id IS NOT NULL)', name=op.f('ck_scale_advice_conversions_ck_sacr_exclusive_output')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_advice_conversions_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_advice_conversions')),
    sa.UniqueConstraint('workspace_id', 'meta_mode', 'occurrence_id', name='uq_sacr_occurrence')
    )
    with op.batch_alter_table('scale_advice_conversions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_advice_conversions_thread_id'), ['thread_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_advice_conversions_workspace_id'), ['workspace_id'], unique=False)
    op.create_table('scale_test_plan_drafts',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('conversion_id', sa.String(length=64), nullable=False),
    sa.Column('episode_id', sa.String(length=64), nullable=False),
    sa.Column('enrollment_id', sa.String(length=64), nullable=False),
    sa.Column('generated_criteria', sa.JSON(), nullable=False),
    sa.Column('generated_lineage_hash', sa.String(length=64), nullable=False),
    sa.Column('generated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('generated_by', sa.String(length=100), nullable=False),
    sa.Column('planned_changes', sa.Text(), nullable=True),
    sa.Column('observation_notes', sa.Text(), nullable=True),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('updated_by', sa.String(length=100), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_test_plan_drafts_ck_stpd_meta_mode')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_test_plan_drafts_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_test_plan_drafts')),
    sa.UniqueConstraint('workspace_id', 'meta_mode', 'conversion_id', name='uq_stpd_conversion')
    )
    with op.batch_alter_table('scale_test_plan_drafts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_test_plan_drafts_enrollment_id'), ['enrollment_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_test_plan_drafts_episode_id'), ['episode_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_test_plan_drafts_workspace_id'), ['workspace_id'], unique=False)
    op.create_table('scale_advice_outcomes',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('thread_id', sa.String(length=64), nullable=False),
    sa.Column('occurrence_id', sa.String(length=64), nullable=False),
    sa.Column('evidence_source_module', sa.String(length=60), nullable=False),
    sa.Column('evidence_source_kind', sa.String(length=60), nullable=False),
    sa.Column('evidence_source_record_id', sa.String(length=64), nullable=False),
    sa.Column('outcome_summary', sa.Text(), nullable=False),
    sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('recorded_by', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_advice_outcomes_ck_saor_meta_mode')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_advice_outcomes_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_advice_outcomes'))
    )
    with op.batch_alter_table('scale_advice_outcomes', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_advice_outcomes_occurrence_id'), ['occurrence_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_advice_outcomes_thread_id'), ['thread_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_advice_outcomes_workspace_id'), ['workspace_id'], unique=False)
    op.create_table('scale_unobserved_factors',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('episode_id', sa.String(length=64), nullable=False),
    sa.Column('factor_type', sa.String(length=60), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('object_reference_id', sa.String(length=64), nullable=True),
    sa.Column('corrects_factor_id', sa.String(length=64), nullable=True),
    sa.Column('recorded_by', sa.String(length=100), nullable=False),
    sa.Column('recorded_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_unobserved_factors_ck_suf_meta_mode')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_unobserved_factors_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_unobserved_factors'))
    )
    with op.batch_alter_table('scale_unobserved_factors', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_unobserved_factors_episode_id'), ['episode_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_unobserved_factors_workspace_id'), ['workspace_id'], unique=False)

    # ### end Alembic commands ###
    op.create_table('scale_exports',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.String(length=64), nullable=False),
    sa.Column('meta_mode', sa.String(length=32), nullable=False),
    sa.Column('operation_id', sa.String(length=64), nullable=False),
    sa.Column('enrollment_id', sa.String(length=64), nullable=False),
    sa.Column('episode_id', sa.String(length=64), nullable=False),
    sa.Column('snapshot_id', sa.String(length=64), nullable=True),
    sa.Column('evaluation_id', sa.String(length=64), nullable=True),
    sa.Column('export_kind', sa.String(length=60), nullable=False),
    sa.Column('requested_by', sa.String(length=100), nullable=False),
    sa.Column('requested_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('result_object_reference', sa.String(length=64), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("meta_mode IN ('customer_sandbox', 'controlled_mock', 'internal_validation')", name=op.f('ck_scale_exports_ck_sexp_meta_mode')),
    sa.CheckConstraint('(completed_at IS NULL) = (result_object_reference IS NULL)', name=op.f('ck_scale_exports_ck_sexp_result_shape')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_scale_exports_workspace_id_workspaces'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scale_exports'))
    )
    with op.batch_alter_table('scale_exports', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scale_exports_enrollment_id'), ['enrollment_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_exports_episode_id'), ['episode_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_exports_operation_id'), ['operation_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_scale_exports_workspace_id'), ['workspace_id'], unique=False)




# ---------------------------------------------------------------------------
# Step 10: composite (workspace[, mode], id) foreign keys
# ---------------------------------------------------------------------------

# Supporting composite-unique constraints installed on the referenced side
# of a composite foreign key. (table, constraint_name, columns).
_PARENT_UNIQUES = (
    ("scale_enrollments", "uq_senr_ws_mode_id", ("workspace_id", "meta_mode", "id")),
    ("scale_episodes", "uq_sep_ws_mode_id", ("workspace_id", "meta_mode", "id")),
    ("scale_breakdown_sources", "uq_sbds_ws_mode_id", ("workspace_id", "meta_mode", "id")),
    ("scale_evidence_source_manifests", "uq_sesm_ws_mode_id", ("workspace_id", "meta_mode", "id")),
    ("scale_evidence_snapshots", "uq_sesn_ws_mode_id", ("workspace_id", "meta_mode", "id")),
    ("scale_snapshot_breakdown_segments", "uq_ssbs_ws_mode_id", ("workspace_id", "meta_mode", "id")),
    # Semantic fingerprint already unique per-table (uq_sev_semantic_fingerprint)
    # is *not* reused here: these are separate, narrower supporting keys for FK
    # targets. uq_sev_ws_mode_id_snapshot additionally proves an occurrence's
    # snapshot_id equals its referenced Evaluation's snapshot_id (spec §1.10:
    # "Every occurrence has a composite foreign key proving its snapshot
    # equals its Evaluation snapshot").
    ("scale_evaluations", "uq_sev_ws_mode_id", ("workspace_id", "meta_mode", "id")),
    ("scale_evaluations", "uq_sev_ws_mode_id_snapshot", ("workspace_id", "meta_mode", "id", "snapshot_id")),
    ("scale_advice_threads", "uq_sat_ws_mode_id", ("workspace_id", "meta_mode", "id")),
    ("scale_advice_occurrences", "uq_sao_ws_mode_id", ("workspace_id", "meta_mode", "id")),
    ("scale_advice_conversions", "uq_sacr_ws_mode_id", ("workspace_id", "meta_mode", "id")),
    ("scale_target_versions", "uq_stgt_ws_id", ("workspace_id", "id")),
    ("scale_metric_semantic_contract_sets", "uq_smcs_ws_mode_id", ("workspace_id", "meta_mode", "id")),
    ("scale_unobserved_factors", "uq_suf_ws_mode_id", ("workspace_id", "meta_mode", "id")),
    ("scale_test_plan_drafts", "uq_stpd_ws_mode_id", ("workspace_id", "meta_mode", "id")),
)

# (child_table, child_columns, parent_table, parent_columns, fk_name).
# Polymorphic ``source_record_id`` columns (spec §1.7-§1.8: "the terminal
# ScaleDailyMetricSource/ScaleExactWindowSource/ScaleBreakdownSource/
# ScaleDeliveryConfigSource row id", one of four possible parent tables
# selected by contract_type) are intentionally NOT given a composite FK here
# -- a database-level polymorphic FK needs a per-branch CHECK/trigger
# spanning four tables, which is out of scope for this migration; see the
# Task 6 report.
_COMPOSITE_FOREIGN_KEYS = (
    # Enrollment / Episode chain
    ("scale_episodes", ("workspace_id", "meta_mode", "enrollment_id"), "scale_enrollments", ("workspace_id", "meta_mode", "id"), "fk_sep_enrollment"),
    ("scale_enrollment_events", ("workspace_id", "meta_mode", "enrollment_id"), "scale_enrollments", ("workspace_id", "meta_mode", "id"), "fk_sene_enrollment"),
    ("scale_episode_events", ("workspace_id", "meta_mode", "enrollment_id"), "scale_enrollments", ("workspace_id", "meta_mode", "id"), "fk_sevt_enrollment"),
    ("scale_episode_events", ("workspace_id", "meta_mode", "episode_id"), "scale_episodes", ("workspace_id", "meta_mode", "id"), "fk_sevt_episode"),
    # Source evidence -> Episode
    ("scale_daily_metric_sources", ("workspace_id", "meta_mode", "episode_id"), "scale_episodes", ("workspace_id", "meta_mode", "id"), "fk_sdms_episode"),
    ("scale_exact_window_sources", ("workspace_id", "meta_mode", "episode_id"), "scale_episodes", ("workspace_id", "meta_mode", "id"), "fk_sews_episode"),
    ("scale_breakdown_sources", ("workspace_id", "meta_mode", "episode_id"), "scale_episodes", ("workspace_id", "meta_mode", "id"), "fk_sbds_episode"),
    ("scale_delivery_config_sources", ("workspace_id", "meta_mode", "episode_id"), "scale_episodes", ("workspace_id", "meta_mode", "id"), "fk_sdcs_episode"),
    ("scale_evidence_source_manifests", ("workspace_id", "meta_mode", "episode_id"), "scale_episodes", ("workspace_id", "meta_mode", "id"), "fk_sesm_episode"),
    ("scale_evidence_readiness_projections", ("workspace_id", "meta_mode", "episode_id"), "scale_episodes", ("workspace_id", "meta_mode", "id"), "fk_serp_episode"),
    ("scale_evidence_readiness_projections", ("workspace_id", "meta_mode", "latest_manifest_id"), "scale_evidence_source_manifests", ("workspace_id", "meta_mode", "id"), "fk_serp_manifest"),
    # Breakdown source children
    ("scale_breakdown_daily_children", ("workspace_id", "meta_mode", "breakdown_source_id"), "scale_breakdown_sources", ("workspace_id", "meta_mode", "id"), "fk_sbdc_breakdown_source"),
    ("scale_breakdown_privacy_records", ("workspace_id", "meta_mode", "breakdown_source_id"), "scale_breakdown_sources", ("workspace_id", "meta_mode", "id"), "fk_sbpr_breakdown_source"),
    # Manifest children
    ("scale_manifest_items", ("workspace_id", "meta_mode", "manifest_id"), "scale_evidence_source_manifests", ("workspace_id", "meta_mode", "id"), "fk_smi_manifest"),
    ("scale_source_quality_issues", ("workspace_id", "meta_mode", "manifest_id"), "scale_evidence_source_manifests", ("workspace_id", "meta_mode", "id"), "fk_ssqi_manifest"),
    # Snapshot -> Episode, snapshot children
    ("scale_evidence_snapshots", ("workspace_id", "meta_mode", "episode_id"), "scale_episodes", ("workspace_id", "meta_mode", "id"), "fk_sesn_episode"),
    ("scale_evidence_source_links", ("workspace_id", "meta_mode", "snapshot_id"), "scale_evidence_snapshots", ("workspace_id", "meta_mode", "id"), "fk_sesl_snapshot"),
    ("scale_snapshot_quality_issues", ("workspace_id", "meta_mode", "snapshot_id"), "scale_evidence_snapshots", ("workspace_id", "meta_mode", "id"), "fk_snqi_snapshot"),
    ("scale_snapshot_breakdown_segments", ("workspace_id", "meta_mode", "snapshot_id"), "scale_evidence_snapshots", ("workspace_id", "meta_mode", "id"), "fk_ssbs_snapshot"),
    ("scale_snapshot_breakdown_daily", ("workspace_id", "meta_mode", "breakdown_segment_id"), "scale_snapshot_breakdown_segments", ("workspace_id", "meta_mode", "id"), "fk_ssbd_segment"),
    # Evaluation runs / records -> Episode / Enrollment / Evaluation
    ("scale_evaluation_runs", ("workspace_id", "meta_mode", "episode_id"), "scale_episodes", ("workspace_id", "meta_mode", "id"), "fk_sevr_episode"),
    ("scale_evaluation_runs", ("workspace_id", "meta_mode", "enrollment_id"), "scale_enrollments", ("workspace_id", "meta_mode", "id"), "fk_sevr_enrollment"),
    ("scale_evaluation_runs", ("workspace_id", "meta_mode", "evaluation_id"), "scale_evaluations", ("workspace_id", "meta_mode", "id"), "fk_sevr_evaluation"),
    ("scale_evaluations", ("workspace_id", "meta_mode", "episode_id"), "scale_episodes", ("workspace_id", "meta_mode", "id"), "fk_sev_episode"),
    ("scale_evaluation_signals", ("workspace_id", "meta_mode", "evaluation_id"), "scale_evaluations", ("workspace_id", "meta_mode", "id"), "fk_sevs_evaluation"),
    ("scale_evaluation_advice_candidates", ("workspace_id", "meta_mode", "evaluation_id"), "scale_evaluations", ("workspace_id", "meta_mode", "id"), "fk_seac_evaluation"),
    ("scale_evaluation_streak_counters", ("workspace_id", "meta_mode", "evaluation_id"), "scale_evaluations", ("workspace_id", "meta_mode", "id"), "fk_sesc_evaluation"),
    ("scale_evaluation_streak_resets", ("workspace_id", "meta_mode", "evaluation_id"), "scale_evaluations", ("workspace_id", "meta_mode", "id"), "fk_sesr_evaluation"),
    ("scale_episode_projections", ("workspace_id", "meta_mode", "episode_id"), "scale_episodes", ("workspace_id", "meta_mode", "id"), "fk_sepp_episode"),
    ("scale_episode_projections", ("workspace_id", "meta_mode", "evaluation_id"), "scale_evaluations", ("workspace_id", "meta_mode", "id"), "fk_sepp_evaluation"),
    # Advice threads -> Episode / Enrollment
    ("scale_advice_threads", ("workspace_id", "meta_mode", "episode_id"), "scale_episodes", ("workspace_id", "meta_mode", "id"), "fk_sat_episode"),
    ("scale_advice_threads", ("workspace_id", "meta_mode", "enrollment_id"), "scale_enrollments", ("workspace_id", "meta_mode", "id"), "fk_sat_enrollment"),
    # Advice occurrence: proves snapshot_id equals its Evaluation's snapshot_id
    ("scale_advice_occurrences", ("workspace_id", "meta_mode", "episode_id"), "scale_episodes", ("workspace_id", "meta_mode", "id"), "fk_sao_episode"),
    ("scale_advice_occurrences", ("workspace_id", "meta_mode", "enrollment_id"), "scale_enrollments", ("workspace_id", "meta_mode", "id"), "fk_sao_enrollment"),
    ("scale_advice_occurrences", ("workspace_id", "meta_mode", "evaluation_id", "snapshot_id"), "scale_evaluations", ("workspace_id", "meta_mode", "id", "snapshot_id"), "fk_sao_evaluation_snapshot"),
    ("scale_advice_occurrences", ("workspace_id", "meta_mode", "thread_id"), "scale_advice_threads", ("workspace_id", "meta_mode", "id"), "fk_sao_thread"),
    # Thread events / dispositions / snoozes / notifications -> thread
    ("scale_advice_thread_events", ("workspace_id", "meta_mode", "episode_id"), "scale_episodes", ("workspace_id", "meta_mode", "id"), "fk_sate_episode"),
    ("scale_advice_thread_events", ("workspace_id", "meta_mode", "thread_id"), "scale_advice_threads", ("workspace_id", "meta_mode", "id"), "fk_sate_thread"),
    ("scale_advice_dispositions", ("workspace_id", "meta_mode", "thread_id"), "scale_advice_threads", ("workspace_id", "meta_mode", "id"), "fk_sad_thread"),
    ("scale_advice_dispositions", ("workspace_id", "meta_mode", "occurrence_id"), "scale_advice_occurrences", ("workspace_id", "meta_mode", "id"), "fk_sad_occurrence"),
    ("scale_advice_snoozes", ("workspace_id", "meta_mode", "thread_id"), "scale_advice_threads", ("workspace_id", "meta_mode", "id"), "fk_sas_thread"),
    ("scale_advice_notification_decisions", ("workspace_id", "meta_mode", "thread_id"), "scale_advice_threads", ("workspace_id", "meta_mode", "id"), "fk_sand_thread"),
    ("scale_advice_notification_decisions", ("workspace_id", "meta_mode", "occurrence_id"), "scale_advice_occurrences", ("workspace_id", "meta_mode", "id"), "fk_sand_occurrence"),
    # Conversion / test-plan draft / outcome / unobserved factor / export
    ("scale_advice_conversions", ("workspace_id", "meta_mode", "thread_id"), "scale_advice_threads", ("workspace_id", "meta_mode", "id"), "fk_sacr_thread"),
    ("scale_advice_conversions", ("workspace_id", "meta_mode", "occurrence_id"), "scale_advice_occurrences", ("workspace_id", "meta_mode", "id"), "fk_sacr_occurrence_id"),
    ("scale_advice_conversions", ("workspace_id", "generation_draft_id"), "generation_drafts", ("workspace_id", "id"), "fk_sacr_generation_draft"),
    ("scale_test_plan_drafts", ("workspace_id", "meta_mode", "conversion_id"), "scale_advice_conversions", ("workspace_id", "meta_mode", "id"), "fk_stpd_conversion"),
    ("scale_test_plan_drafts", ("workspace_id", "meta_mode", "episode_id"), "scale_episodes", ("workspace_id", "meta_mode", "id"), "fk_stpd_episode"),
    ("scale_test_plan_drafts", ("workspace_id", "meta_mode", "enrollment_id"), "scale_enrollments", ("workspace_id", "meta_mode", "id"), "fk_stpd_enrollment"),
    ("scale_advice_outcomes", ("workspace_id", "meta_mode", "thread_id"), "scale_advice_threads", ("workspace_id", "meta_mode", "id"), "fk_saor_thread"),
    ("scale_advice_outcomes", ("workspace_id", "meta_mode", "occurrence_id"), "scale_advice_occurrences", ("workspace_id", "meta_mode", "id"), "fk_saor_occurrence"),
    ("scale_unobserved_factors", ("workspace_id", "meta_mode", "episode_id"), "scale_episodes", ("workspace_id", "meta_mode", "id"), "fk_suf_episode"),
    ("scale_unobserved_factors", ("workspace_id", "meta_mode", "corrects_factor_id"), "scale_unobserved_factors", ("workspace_id", "meta_mode", "id"), "fk_suf_corrects_factor"),
    ("scale_exports", ("workspace_id", "meta_mode", "episode_id"), "scale_episodes", ("workspace_id", "meta_mode", "id"), "fk_sexp_episode"),
    ("scale_exports", ("workspace_id", "meta_mode", "enrollment_id"), "scale_enrollments", ("workspace_id", "meta_mode", "id"), "fk_sexp_enrollment"),
    ("scale_exports", ("workspace_id", "operation_id"), "operation_records", ("workspace_id", "id"), "fk_sexp_operation"),
    # Target lifecycle -> Target version (mode-independent)
    ("scale_target_lifecycle_events", ("workspace_id", "target_version_id"), "scale_target_versions", ("workspace_id", "id"), "fk_stge_target_version"),
    # Metric semantic contract set members -> parent set. No workspace_id/
    # meta_mode on this table (child-only, like dimension_values ->
    # dimensions): a plain single-column FK is the whole story -- see
    # app/scale/models.py and the RLS child-form note below.
    ("scale_metric_semantic_contract_set_members", ("contract_set_id",), "scale_metric_semantic_contract_sets", ("id",), "fk_smcm_contract_set"),
)

# scale_advice_conversions.scale_test_plan_draft_id <-> scale_test_plan_drafts
# is circular (a conversion optionally points at the draft it created; the
# draft always points back at the conversion that created it). Added last,
# after the acyclic relations and the unique constraints both sides need.
_CIRCULAR_FK = (
    "scale_advice_conversions",
    ("workspace_id", "meta_mode", "scale_test_plan_draft_id"),
    "scale_test_plan_drafts",
    ("workspace_id", "meta_mode", "id"),
    "fk_sacr_test_plan_draft",
)


def _step10_composite_foreign_keys() -> None:
    from collections import defaultdict

    # Supporting unique constraints, grouped by table so each table is
    # rewritten once under SQLite batch mode.
    uniques_by_table: dict[str, list[tuple[str, tuple[str, ...]]]] = defaultdict(list)
    for table, name, columns in _PARENT_UNIQUES:
        uniques_by_table[table].append((name, columns))
    for table, uniques in uniques_by_table.items():
        with op.batch_alter_table(table, schema=None) as batch_op:
            for name, columns in uniques:
                batch_op.create_unique_constraint(name, list(columns))

    # Composite foreign keys, grouped by child table.
    all_fks = list(_COMPOSITE_FOREIGN_KEYS) + [_CIRCULAR_FK]
    fks_by_child: dict[str, list[tuple]] = defaultdict(list)
    for child, child_cols, parent, parent_cols, name in all_fks:
        fks_by_child[child].append((child_cols, parent, parent_cols, name))
    for child, fks in fks_by_child.items():
        with op.batch_alter_table(child, schema=None) as batch_op:
            for child_cols, parent, parent_cols, name in fks:
                batch_op.create_foreign_key(
                    name, parent, list(child_cols), list(parent_cols),
                    ondelete="RESTRICT",
                )


# ---------------------------------------------------------------------------
# Step 11: PostgreSQL-only DDL -- partial unique indexes, immutability
# triggers, Row Level Security, capability-scoped grants (spec §1.12).
# ---------------------------------------------------------------------------

def _step11_pg_enforcement(conn) -> None:
    if not _is_pg(conn):
        return

    _install_partial_unique_indexes(conn)
    _install_immutability_triggers(conn)
    _install_sealing_trigger(conn)
    _install_snapshot_child_insert_guards(conn)
    _install_rls(conn)
    _install_grants(conn)


def _install_partial_unique_indexes(conn) -> None:
    # One active/paused remote-binding reservation per identity (spec §1.4).
    conn.execute(text(
        "CREATE UNIQUE INDEX ix_senr_active_binding ON scale_enrollments "
        "(workspace_id, meta_mode, meta_connection_id, ad_account_id, remote_binding_key) "
        "WHERE lifecycle IN ('active', 'paused')"
    ))
    # One active Advice thread per semantic identity (spec §1.10).
    conn.execute(text(
        "CREATE UNIQUE INDEX ix_sat_active_thread ON scale_advice_threads "
        "(workspace_id, meta_mode, episode_id, advice_type, reason_signature) "
        "WHERE lifecycle = 'active'"
    ))
    # One active Target per exact context (spec §1.6).
    conn.execute(text(
        "CREATE UNIQUE INDEX ix_stgt_active_target ON scale_target_versions "
        "(workspace_id, product_version_id, offer_version_id, market_code, currency) "
        "WHERE lifecycle_state = 'active'"
    ))
    # Idempotent disposition/snooze replay (Issue #25 Task 15 fix round 1):
    # a client retry carrying the same Idempotency-Key against the same
    # thread must not insert a second row. NULL keys (rows written before
    # this column existed, or a caller that supplies none) are excluded from
    # the constraint so they keep working exactly as before.
    conn.execute(text(
        "CREATE UNIQUE INDEX ix_sad_thread_idempotency ON scale_advice_dispositions "
        "(workspace_id, meta_mode, thread_id, idempotency_key) "
        "WHERE idempotency_key IS NOT NULL"
    ))
    conn.execute(text(
        "CREATE UNIQUE INDEX ix_sas_thread_idempotency ON scale_advice_snoozes "
        "(workspace_id, meta_mode, thread_id, idempotency_key) "
        "WHERE idempotency_key IS NOT NULL"
    ))


def _install_immutability_triggers(conn) -> None:
    conn.execute(text("""
        CREATE OR REPLACE FUNCTION scale_reject_update_delete()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION '% rows are immutable and append-only; % is not permitted',
                TG_TABLE_NAME, TG_OP;
        END;
        $$
    """))
    for table_name in IMMUTABLE_TABLES:
        conn.execute(text(
            f'CREATE TRIGGER trg_{table_name}_immutable '
            f'BEFORE UPDATE OR DELETE ON {table_name} '
            'FOR EACH ROW EXECUTE FUNCTION scale_reject_update_delete()'
        ))


def _install_sealing_trigger(conn) -> None:
    """scale_evidence_snapshots: exactly one legal UPDATE transition
    (unsealed -> sealed); DELETE always rejected. Spec §1.8."""
    conn.execute(text("""
        CREATE OR REPLACE FUNCTION scale_evidence_snapshots_guard_sealing()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'scale_evidence_snapshots rows are immutable and cannot be deleted';
            END IF;
            IF OLD.sealed_at IS NOT NULL THEN
                RAISE EXCEPTION 'scale_evidence_snapshots row % is already sealed and cannot be updated', OLD.id;
            END IF;
            IF NEW.sealed_at IS NULL THEN
                RAISE EXCEPTION 'scale_evidence_snapshots row % may only be updated by the unsealed-to-sealed transition', OLD.id;
            END IF;
            RETURN NEW;
        END;
        $$
    """))
    conn.execute(text(
        f'CREATE TRIGGER trg_{SEALED_SNAPSHOT_TABLE}_guard_sealing '
        f'BEFORE UPDATE OR DELETE ON {SEALED_SNAPSHOT_TABLE} '
        'FOR EACH ROW EXECUTE FUNCTION scale_evidence_snapshots_guard_sealing()'
    ))


def _install_snapshot_child_insert_guards(conn) -> None:
    """Snapshot children "permit INSERT only while the parent is unsealed"
    (spec §1.8), in addition to being immutable once inserted (the reject
    trigger above already blocks UPDATE/DELETE on these same tables)."""
    conn.execute(text("""
        CREATE OR REPLACE FUNCTION scale_reject_insert_after_sealed()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM scale_evidence_snapshots
                WHERE id = NEW.snapshot_id AND sealed_at IS NOT NULL
            ) THEN
                RAISE EXCEPTION 'cannot insert into % after its parent snapshot is sealed', TG_TABLE_NAME;
            END IF;
            RETURN NEW;
        END;
        $$
    """))
    for table_name in SNAPSHOT_CHILDREN_BY_SNAPSHOT_ID:
        conn.execute(text(
            f'CREATE TRIGGER trg_{table_name}_insert_while_unsealed '
            f'BEFORE INSERT ON {table_name} '
            'FOR EACH ROW EXECUTE FUNCTION scale_reject_insert_after_sealed()'
        ))

    conn.execute(text("""
        CREATE OR REPLACE FUNCTION scale_reject_breakdown_daily_insert_after_sealed()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM scale_snapshot_breakdown_segments AS segment
                JOIN scale_evidence_snapshots AS snapshot ON snapshot.id = segment.snapshot_id
                WHERE segment.id = NEW.breakdown_segment_id AND snapshot.sealed_at IS NOT NULL
            ) THEN
                RAISE EXCEPTION 'cannot insert into scale_snapshot_breakdown_daily after its parent snapshot is sealed';
            END IF;
            RETURN NEW;
        END;
        $$
    """))
    for table_name in SNAPSHOT_CHILDREN_BY_BREAKDOWN_SEGMENT_ID:
        conn.execute(text(
            f'CREATE TRIGGER trg_{table_name}_insert_while_unsealed '
            f'BEFORE INSERT ON {table_name} '
            'FOR EACH ROW EXECUTE FUNCTION scale_reject_breakdown_daily_insert_after_sealed()'
        ))


def _install_rls(conn) -> None:
    del conn
    for table_name in NEW_WORKSPACE_OWNED:
        op.execute(f'ALTER TABLE "{table_name}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table_name}" FORCE ROW LEVEL SECURITY')
        if table_name in CHILD_FORM_TABLES:
            fk_column, parent = CHILD_FORM_TABLES[table_name]
            condition = (
                f'EXISTS (SELECT 1 FROM "{parent}" AS parent '
                f'WHERE parent.id = "{table_name}"."{fk_column}")'
            )
        elif table_name in MODE_INDEPENDENT_TABLES:
            condition = "marketai_context_is_current(workspace_id)"
        else:
            condition = "marketai_meta_context_is_current(workspace_id, meta_mode)"
        op.execute(
            f'CREATE POLICY tenant_isolation ON "{table_name}" '
            f"USING ({condition}) WITH CHECK ({condition})"
        )


def _install_grants(conn) -> None:
    del conn
    for table_name in PLATFORM_TABLES + NEW_WORKSPACE_OWNED:
        op.execute(
            f'GRANT SELECT ON TABLE "{table_name}" TO '
            "marketai_domain_api, marketai_domain_worker, marketai_domain_readonly"
        )
        api_ops, worker_ops = GRANT_MATRIX[table_name]
        for verb in api_ops:
            op.execute(f'GRANT {verb} ON TABLE "{table_name}" TO marketai_domain_api')
        for verb in worker_ops:
            op.execute(f'GRANT {verb} ON TABLE "{table_name}" TO marketai_domain_worker')


def downgrade() -> None:
    conn = op.get_bind()

    if _is_pg(conn):
        for table_name in PLATFORM_TABLES + NEW_WORKSPACE_OWNED:
            conn.execute(text(
                f'REVOKE ALL ON "{table_name}" FROM '
                "marketai_domain_api, marketai_domain_worker, marketai_domain_readonly"
            ))
        for table_name in NEW_WORKSPACE_OWNED:
            conn.execute(text(f'DROP POLICY IF EXISTS tenant_isolation ON "{table_name}"'))
            conn.execute(text(f'ALTER TABLE "{table_name}" NO FORCE ROW LEVEL SECURITY'))
            conn.execute(text(f'ALTER TABLE "{table_name}" DISABLE ROW LEVEL SECURITY'))
        for table_name in SNAPSHOT_CHILDREN_BY_SNAPSHOT_ID + SNAPSHOT_CHILDREN_BY_BREAKDOWN_SEGMENT_ID:
            conn.execute(text(f'DROP TRIGGER IF EXISTS trg_{table_name}_insert_while_unsealed ON {table_name}'))
        conn.execute(text(f'DROP TRIGGER IF EXISTS trg_{SEALED_SNAPSHOT_TABLE}_guard_sealing ON {SEALED_SNAPSHOT_TABLE}'))
        for table_name in IMMUTABLE_TABLES:
            conn.execute(text(f'DROP TRIGGER IF EXISTS trg_{table_name}_immutable ON {table_name}'))
        conn.execute(text("DROP FUNCTION IF EXISTS scale_reject_breakdown_daily_insert_after_sealed()"))
        conn.execute(text("DROP FUNCTION IF EXISTS scale_reject_insert_after_sealed()"))
        conn.execute(text("DROP FUNCTION IF EXISTS scale_evidence_snapshots_guard_sealing()"))
        conn.execute(text("DROP FUNCTION IF EXISTS scale_reject_update_delete()"))
        conn.execute(text(
            "DROP INDEX IF EXISTS ix_senr_active_binding, "
            "ix_sat_active_thread, ix_stgt_active_target, "
            "ix_sad_thread_idempotency, ix_sas_thread_idempotency"
        ))

    # Break the one circular composite FK before the reverse-order drop loop
    # below (scale_advice_conversions <-> scale_test_plan_drafts).
    with op.batch_alter_table("scale_advice_conversions", schema=None) as batch_op:
        batch_op.drop_constraint("fk_sacr_test_plan_draft", type_="foreignkey")

    for table in (
        # Step 9 (reverse)
        "scale_exports",
        "scale_unobserved_factors",
        "scale_advice_outcomes",
        "scale_test_plan_drafts",
        "scale_advice_conversions",
        # Step 8 (reverse)
        "scale_advice_notification_decisions",
        "scale_advice_snoozes",
        "scale_advice_dispositions",
        "scale_advice_thread_events",
        "scale_advice_occurrences",
        "scale_advice_threads",
        # Step 7 (reverse)
        "scale_episode_projections",
        "scale_evaluation_streak_resets",
        "scale_evaluation_streak_counters",
        "scale_evaluation_advice_candidates",
        "scale_evaluation_signals",
        "scale_evaluations",
        "scale_evaluation_runs",
        # Step 6 (reverse)
        "scale_snapshot_breakdown_daily",
        "scale_snapshot_breakdown_segments",
        "scale_snapshot_quality_issues",
        "scale_evidence_source_links",
        "scale_evidence_snapshots",
        # Step 5 (reverse)
        "scale_evidence_readiness_projections",
        "scale_source_quality_issues",
        "scale_manifest_items",
        "scale_evidence_source_manifests",
        "scale_delivery_config_sources",
        "scale_breakdown_privacy_records",
        "scale_breakdown_daily_children",
        "scale_breakdown_sources",
        "scale_exact_window_sources",
        "scale_daily_metric_sources",
        # Step 4 (reverse)
        "scale_metric_semantic_contract_set_members",
        "scale_metric_semantic_contract_sets",
        "scale_target_lifecycle_events",
        "scale_target_versions",
        # Step 3 (reverse)
        "scale_episode_events",
        "scale_episodes",
        # Step 2 (reverse)
        "scale_enrollment_events",
        "scale_enrollments",
        # Step 1 (reverse)
        "scale_policy_versions",
    ):
        op.drop_table(table)
