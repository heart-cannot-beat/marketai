"""Localization truth, bounded QC, and asset lineage (Issue #24).

DDL order per spec §1.11:
  1. MarketPolicy tables + events (platform-scoped)
  2. TruthPackage/Fact/dependency tables (workspace-scoped)
  3. Preflight + asset_versions evolution + provider_call_attempts evolution
  4. Batch core, source/target, commands (defer item FK)
  5. Inventories, confirmations, ClaimDecisionSets/decisions, CopyVersions/approvals
  6. Items, deferred FK from batch_commands, Attempts, outputs, publications,
     QC records, semantic claims, provider-call join tables
  7. Bindings, binding events, lineage
  8. PostgreSQL triggers, RLS, grants, metadata assertions, nine-market seed

Revision ID: u1a2b3c4d5e6
Revises: m23b_meta_domain_tables
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision = "u1a2b3c4d5e6"
down_revision = "m23b_meta_domain_tables"
branch_labels = None
depends_on = None


def _is_pg(conn) -> bool:
    return conn.dialect.name == "postgresql"


# ---------------------------------------------------------------------------
# Table lists for triggers / RLS / grants
# ---------------------------------------------------------------------------

# All new workspace-scoped tables that need RLS.
# Platform-scoped tables (market_policies, market_policy_versions,
# market_policy_version_events) do NOT get RLS.
NEW_WORKSPACE_OWNED = (
    "truth_packages",
    "truth_package_markets",
    "truth_source_attempts",
    "truth_facts",
    "truth_derived_facts",
    "truth_derived_fact_inputs",
    "fact_library_dependencies",
    "fact_market_policy_dependencies",
    "localization_batches",
    "localization_batch_sources",
    "localization_batch_commands",
    "localization_batch_market_targets",
    "source_claim_inventories",
    "source_claim_confirmations",
    "localization_claim_decision_sets",
    "localization_claim_decisions",
    "localized_copy_versions",
    "localized_copy_approvals",
    "localization_items",
    "localization_attempts",
    "localization_attempt_outputs",
    "localization_output_publications",
    "localization_qc_records",
    "localization_semantic_claims",
    "localization_item_provider_calls",
    "localization_preparation_provider_calls",
    "market_asset_bindings",
    "market_asset_binding_events",
    "localized_asset_lineages",
)

# Platform-scoped tables (no RLS, but still need grants + triggers).
PLATFORM_TABLES = (
    "market_policies",
    "market_policy_versions",
    "market_policy_version_events",
)

# Append-only (no UPDATE, no DELETE) tables.
APPEND_ONLY_TABLES = (
    "truth_packages",
    "truth_package_markets",
    "truth_source_attempts",
    "truth_facts",
    "truth_derived_facts",
    "truth_derived_fact_inputs",
    "fact_library_dependencies",
    "fact_market_policy_dependencies",
    "localization_batch_sources",
    "localization_batch_commands",
    "localization_batch_market_targets",
    "source_claim_inventories",
    "source_claim_confirmations",
    "localization_claim_decision_sets",
    "localization_claim_decisions",
    "localized_copy_versions",
    "localized_copy_approvals",
    "localization_attempts",
    "localization_attempt_outputs",
    "localization_output_publications",
    "localization_qc_records",
    "localization_item_provider_calls",
    "market_asset_binding_events",
    "localized_asset_lineages",
    # Platform append-only
    "market_policy_version_events",
)


# =========================================================================
# upgrade()
# =========================================================================

def upgrade() -> None:
    conn = op.get_bind()

    # -----------------------------------------------------------------------
    # Step 1: Platform tables (market_policies, versions, events)
    # -----------------------------------------------------------------------
    _step1_platform_tables()

    # -----------------------------------------------------------------------
    # Step 2: Truth resolution tables
    # -----------------------------------------------------------------------
    _step2_truth_tables()

    # -----------------------------------------------------------------------
    # Step 3: Existing table evolution (asset_versions, provider_call_attempts)
    # -----------------------------------------------------------------------
    _step3_existing_table_evolution(conn)

    # -----------------------------------------------------------------------
    # Step 4: Batch core tables
    # -----------------------------------------------------------------------
    _step4_batch_tables()

    # -----------------------------------------------------------------------
    # Step 5: Inventories, confirmations, claim decision sets, copy versions
    # -----------------------------------------------------------------------
    _step5_claim_copy_tables()

    # -----------------------------------------------------------------------
    # Step 6: Items, deferred FK, attempts, outputs, publications, QC,
    #         semantic claims, provider-call join tables
    # -----------------------------------------------------------------------
    _step6_item_attempt_tables(conn)

    # -----------------------------------------------------------------------
    # Step 7: Bindings, binding events, lineage
    # -----------------------------------------------------------------------
    _step7_binding_lineage_tables()

    # -----------------------------------------------------------------------
    # Step 8: PostgreSQL triggers, RLS, grants, seed, verification
    # -----------------------------------------------------------------------
    _step8_pg_enforcement(conn)


# =========================================================================
# Step 1: Platform-scoped tables
# =========================================================================

def _step1_platform_tables():
    op.create_table(
        "market_policies",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("market_id", sa.String(10), nullable=False),
        sa.Column("locale", sa.String(10), nullable=False),
        sa.Column("country", sa.String(2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("market_id", name="uq_mp_market_id"),
    )

    op.create_table(
        "market_policy_versions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("market_policy_id", sa.String(64),
                  sa.ForeignKey("market_policies.id"), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("version_state", sa.String(20), nullable=False),
        sa.Column("locale", sa.String(10), nullable=False),
        sa.Column("country", sa.String(2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("pricing_context_json", sa.JSON(), nullable=False),
        sa.Column("digit_format_json", sa.JSON(), nullable=False),
        sa.Column("base_tone", sa.String(40), nullable=False),
        sa.Column("reserved_words_json", sa.JSON(), nullable=False),
        sa.Column("restricted_claim_types_json", sa.JSON(), nullable=False),
        sa.Column("requires_local_copy", sa.Boolean(), nullable=False),
        sa.Column("shipping_address_granularity", sa.String(20), nullable=False),
        sa.Column("max_fact_staleness_seconds", sa.Integer(), nullable=False),
        sa.Column("qc_rules_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("market_policy_id", "version_number",
                            name="uq_mpv_policy_version"),
        sa.UniqueConstraint("id", "market_policy_id",
                            name="uq_mpv_id_policy"),
        sa.CheckConstraint(
            "version_state IN ('effective', 'superseded')",
            name="ck_mpv_version_state",
        ),
        sa.CheckConstraint(
            "shipping_address_granularity IN ('country', 'region', 'postal_code')",
            name="ck_mpv_shipping_granularity",
        ),
    )

    op.create_table(
        "market_policy_version_events",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("market_policy_id", sa.String(64),
                  sa.ForeignKey("market_policies.id"), nullable=False),
        sa.Column("market_policy_version_id", sa.String(64),
                  sa.ForeignKey("market_policy_versions.id"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(20), nullable=False),
        sa.Column("previous_state", sa.String(20), nullable=True),
        sa.Column("current_state", sa.String(20), nullable=False),
        sa.Column("deduplication_key", sa.String(128), nullable=False),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("market_policy_id", "sequence",
                            name="uq_mpve_policy_sequence"),
        sa.UniqueConstraint("market_policy_version_id", "event_type",
                            name="uq_mpve_version_event_type"),
        sa.UniqueConstraint("deduplication_key",
                            name="uq_mpve_dedup_key"),
        sa.UniqueConstraint("id", "market_policy_version_id",
                            name="uq_mpve_id_version"),
        sa.ForeignKeyConstraint(
            ["market_policy_version_id", "market_policy_id"],
            ["market_policy_versions.id", "market_policy_versions.market_policy_id"],
            name="fk_mpve_version_policy_mpv",
        ),
        sa.CheckConstraint(
            "event_type IN ('published', 'superseded')",
            name="ck_mpve_event_type",
        ),
        sa.CheckConstraint(
            "current_state IN ('effective', 'superseded')",
            name="ck_mpve_current_state",
        ),
        sa.CheckConstraint("sequence >= 1", name="ck_mpve_sequence_positive"),
        sa.CheckConstraint(
            "(event_type = 'published' AND previous_state IS NULL "
            "AND current_state = 'effective') OR "
            "(event_type = 'superseded' AND previous_state = 'effective' "
            "AND current_state = 'superseded')",
            name="ck_mpve_state_consistency",
        ),
    )


# =========================================================================
# Step 2: Truth resolution tables
# =========================================================================

def _step2_truth_tables():
    op.create_table(
        "truth_packages",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("product_version_id", sa.String(64), nullable=False),
        sa.Column("offer_version_id", sa.String(64), nullable=False),
        sa.Column("truth_resolution_policy_version_id", sa.String(64), nullable=False),
        sa.Column("truth_data_mode", sa.String(20), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_tp_workspace_id"),
        sa.UniqueConstraint("workspace_id", "id", "truth_data_mode",
                            name="uq_tp_workspace_id_mode"),
        sa.UniqueConstraint("workspace_id", "canonical_hash",
                            name="uq_tp_workspace_hash"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "product_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_tp_product_version",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "offer_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_tp_offer_version",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "truth_resolution_policy_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_tp_truth_resolution_policy_version",
        ),
        sa.CheckConstraint(
            "truth_data_mode IN ('real', 'mock')",
            name="ck_tp_truth_data_mode",
        ),
        sa.CheckConstraint(
            "status IN ('full', 'degraded', 'failed')",
            name="ck_tp_status",
        ),
    )

    op.create_table(
        "truth_package_markets",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("truth_package_id", sa.String(64), nullable=False),
        sa.Column("market_policy_version_id", sa.String(64),
                  sa.ForeignKey("market_policy_versions.id"), nullable=False),
        sa.Column("market", sa.String(10), nullable=False),
        sa.Column("readiness", sa.String(20), nullable=False),
        sa.Column("readiness_detail_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_tpm_workspace_id"),
        sa.UniqueConstraint("workspace_id", "truth_package_id", "market",
                            name="uq_tpm_workspace_package_market"),
        sa.UniqueConstraint("workspace_id", "id", "truth_package_id",
                            name="uq_tpm_workspace_id_package"),
        sa.UniqueConstraint("workspace_id", "id", "truth_package_id",
                            "market_policy_version_id",
                            name="uq_tpm_workspace_id_package_mpv"),
        sa.UniqueConstraint("workspace_id", "id", "truth_package_id", "market",
                            "market_policy_version_id",
                            name="uq_tpm_workspace_id_package_market_mpv"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "truth_package_id"],
            ["truth_packages.workspace_id", "truth_packages.id"],
            name="fk_tpm_workspace_package",
        ),
        sa.CheckConstraint(
            "readiness IN ('ready', 'degraded', 'failed', 'unavailable')",
            name="ck_tpm_readiness",
        ),
    )

    op.create_table(
        "truth_source_attempts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("truth_package_id", sa.String(64), nullable=False),
        sa.Column("truth_package_market_id", sa.String(64), nullable=False),
        sa.Column("fact_type", sa.String(40), nullable=False),
        sa.Column("scope_key", sa.String(120), nullable=False),
        sa.Column("priority_ordinal", sa.Integer(), nullable=False),
        sa.Column("data_source_version_id", sa.String(64), nullable=False),
        sa.Column("market_source_mapping_version_id", sa.String(64), nullable=True),
        sa.Column("adapter_kind", sa.String(40), nullable=False),
        sa.Column("adapter_version", sa.String(40), nullable=False),
        sa.Column("manual_fact_set_version_id", sa.String(64), nullable=True),
        sa.Column("resolution_rule_version", sa.String(40), nullable=False),
        sa.Column("truth_data_mode", sa.String(20), nullable=False),
        sa.Column("requested_context_json", sa.JSON(), nullable=False),
        sa.Column("mock_scenario", sa.String(100), nullable=True),
        sa.Column("mock_version", sa.String(40), nullable=True),
        sa.Column("outcome", sa.String(20), nullable=False),
        sa.Column("failure_code", sa.String(60), nullable=True),
        sa.Column("skip_code", sa.String(60), nullable=True),
        sa.Column("evidence_hash", sa.String(64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "workspace_id", "truth_package_market_id", "fact_type",
            "scope_key", "priority_ordinal",
            name="uq_tsa_workspace_market_fact_scope_priority",
        ),
        sa.UniqueConstraint(
            "workspace_id", "id", "truth_package_id", "truth_package_market_id",
            "fact_type", "scope_key",
            name="uq_tsa_workspace_id_package_market_fact_scope",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "truth_package_market_id", "truth_package_id"],
            [
                "truth_package_markets.workspace_id",
                "truth_package_markets.id",
                "truth_package_markets.truth_package_id",
            ],
            name="fk_tsa_market",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "data_source_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_tsa_data_source_version",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "market_source_mapping_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_tsa_market_source_mapping_version",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "manual_fact_set_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_tsa_manual_fact_set_version",
        ),
        sa.CheckConstraint(
            "truth_data_mode IN ('real', 'mock')",
            name="ck_tsa_truth_data_mode",
        ),
        sa.CheckConstraint(
            "outcome IN ('accepted', 'failed', 'skipped')",
            name="ck_tsa_outcome",
        ),
        sa.CheckConstraint(
            "(outcome = 'failed' AND failure_code IS NOT NULL) OR "
            "(outcome != 'failed' AND failure_code IS NULL)",
            name="ck_tsa_failure_code",
        ),
        sa.CheckConstraint(
            "(outcome = 'skipped' AND skip_code IS NOT NULL) OR "
            "(outcome != 'skipped' AND skip_code IS NULL)",
            name="ck_tsa_skip_code",
        ),
        sa.CheckConstraint(
            "(outcome = 'accepted' AND evidence_hash IS NOT NULL) OR "
            "(outcome IN ('failed', 'skipped') AND evidence_hash IS NULL)",
            name="ck_tsa_evidence_hash",
        ),
        sa.CheckConstraint(
            "(adapter_kind = 'manual' AND manual_fact_set_version_id IS NOT NULL "
            "AND truth_data_mode = 'real') OR "
            "(adapter_kind != 'manual' AND manual_fact_set_version_id IS NULL)",
            name="ck_tsa_manual_adapter",
        ),
        sa.CheckConstraint(
            "(truth_data_mode = 'mock' AND mock_scenario IS NOT NULL "
            "AND mock_version IS NOT NULL) OR "
            "(truth_data_mode = 'real' AND mock_scenario IS NULL "
            "AND mock_version IS NULL)",
            name="ck_tsa_mock_fields",
        ),
    )

    op.create_table(
        "truth_facts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("truth_package_id", sa.String(64), nullable=False),
        sa.Column("truth_package_market_id", sa.String(64), nullable=False),
        sa.Column("fact_type", sa.String(40), nullable=False),
        sa.Column("scope_key", sa.String(120), nullable=False),
        sa.Column("accepted_source_attempt_id", sa.String(64), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column("external_object_ref", sa.String(256), nullable=False),
        sa.Column("requested_context_json", sa.JSON(), nullable=False),
        sa.Column("mock_scenario", sa.String(100), nullable=True),
        sa.Column("mock_version", sa.String(40), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_tf_workspace_id"),
        sa.UniqueConstraint("workspace_id", "id", "truth_package_id",
                            name="uq_tf_workspace_id_package"),
        sa.UniqueConstraint("workspace_id", "id", "truth_package_id",
                            "truth_package_market_id",
                            name="uq_tf_workspace_id_package_market"),
        sa.UniqueConstraint("workspace_id", "id", "truth_package_id",
                            "truth_package_market_id", "scope_key",
                            name="uq_tf_workspace_id_package_market_scope"),
        sa.UniqueConstraint("workspace_id", "id", "truth_package_id",
                            "truth_package_market_id", "fact_type", "scope_key",
                            name="uq_tf_workspace_id_package_market_type_scope"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "truth_package_market_id", "truth_package_id"],
            [
                "truth_package_markets.workspace_id",
                "truth_package_markets.id",
                "truth_package_markets.truth_package_id",
            ],
            name="fk_tf_market",
        ),
        sa.ForeignKeyConstraint(
            [
                "workspace_id", "accepted_source_attempt_id", "truth_package_id",
                "truth_package_market_id", "fact_type", "scope_key",
            ],
            [
                "truth_source_attempts.workspace_id",
                "truth_source_attempts.id",
                "truth_source_attempts.truth_package_id",
                "truth_source_attempts.truth_package_market_id",
                "truth_source_attempts.fact_type",
                "truth_source_attempts.scope_key",
            ],
            name="fk_tf_accepted_attempt",
        ),
    )

    op.create_table(
        "truth_derived_facts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("truth_package_id", sa.String(64), nullable=False),
        sa.Column("truth_package_market_id", sa.String(64), nullable=False),
        sa.Column("derived_type", sa.String(40), nullable=False),
        sa.Column("scope_key", sa.String(120), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=False),
        sa.Column("derivation_rule_version", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_tdf_workspace_id"),
        sa.UniqueConstraint("workspace_id", "id", "truth_package_id",
                            "truth_package_market_id",
                            name="uq_tdf_workspace_id_package_market"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "truth_package_market_id", "truth_package_id"],
            [
                "truth_package_markets.workspace_id",
                "truth_package_markets.id",
                "truth_package_markets.truth_package_id",
            ],
            name="fk_tdf_market",
        ),
    )

    op.create_table(
        "truth_derived_fact_inputs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("truth_package_id", sa.String(64), nullable=False),
        sa.Column("truth_package_market_id", sa.String(64), nullable=False),
        sa.Column("derived_fact_id", sa.String(64), nullable=False),
        sa.Column("input_fact_id", sa.String(64), nullable=False),
        sa.UniqueConstraint("workspace_id", "derived_fact_id", "input_fact_id",
                            name="uq_tdfi_workspace_derived_input"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "derived_fact_id", "truth_package_id",
             "truth_package_market_id"],
            [
                "truth_derived_facts.workspace_id",
                "truth_derived_facts.id",
                "truth_derived_facts.truth_package_id",
                "truth_derived_facts.truth_package_market_id",
            ],
            name="fk_tdfi_derived_fact",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "input_fact_id", "truth_package_id",
             "truth_package_market_id"],
            [
                "truth_facts.workspace_id",
                "truth_facts.id",
                "truth_facts.truth_package_id",
                "truth_facts.truth_package_market_id",
            ],
            name="fk_tdfi_input_fact",
        ),
    )

    op.create_table(
        "fact_library_dependencies",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("truth_package_id", sa.String(64), nullable=False),
        sa.Column("truth_fact_id", sa.String(64), nullable=True),
        sa.Column("dependency_kind", sa.String(40), nullable=False),
        sa.Column("dependency_version_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "truth_package_id"],
            ["truth_packages.workspace_id", "truth_packages.id"],
            name="fk_fld_package",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "truth_fact_id", "truth_package_id"],
            [
                "truth_facts.workspace_id",
                "truth_facts.id",
                "truth_facts.truth_package_id",
            ],
            name="fk_fld_fact",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "dependency_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_fld_dependency_version",
        ),
        sa.CheckConstraint(
            "(truth_fact_id IS NULL AND dependency_kind IN "
            "('product_version', 'offer_version', "
            "'truth_resolution_policy_version')) OR "
            "(truth_fact_id IS NOT NULL AND dependency_kind IN "
            "('data_source_version', 'manual_fact_set_version', "
            "'market_source_mapping_version'))",
            name="ck_fld_dependency_kind",
        ),
    )

    # Partial unique indexes for fact_library_dependencies
    op.create_index(
        "ix_fld_fact_level_dep",
        "fact_library_dependencies",
        ["workspace_id", "truth_package_id", "truth_fact_id",
         "dependency_kind", "dependency_version_id"],
        unique=True,
        postgresql_where=text("truth_fact_id IS NOT NULL"),
        sqlite_where=text("truth_fact_id IS NOT NULL"),
    )
    op.create_index(
        "ix_fld_package_level_dep",
        "fact_library_dependencies",
        ["workspace_id", "truth_package_id",
         "dependency_kind", "dependency_version_id"],
        unique=True,
        postgresql_where=text("truth_fact_id IS NULL"),
        sqlite_where=text("truth_fact_id IS NULL"),
    )

    op.create_table(
        "fact_market_policy_dependencies",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("truth_package_id", sa.String(64), nullable=False),
        sa.Column("truth_package_market_id", sa.String(64), nullable=False),
        sa.Column("market_policy_version_id", sa.String(64),
                  sa.ForeignKey("market_policy_versions.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "workspace_id", "truth_package_id", "truth_package_market_id",
            "market_policy_version_id",
            name="uq_fmpd_workspace_package_market_mpv",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "truth_package_id"],
            ["truth_packages.workspace_id", "truth_packages.id"],
            name="fk_fmpd_package",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "truth_package_market_id", "truth_package_id",
             "market_policy_version_id"],
            [
                "truth_package_markets.workspace_id",
                "truth_package_markets.id",
                "truth_package_markets.truth_package_id",
                "truth_package_markets.market_policy_version_id",
            ],
            name="fk_fmpd_market_mpv",
        ),
    )


# =========================================================================
# Step 3: Existing table evolution
# =========================================================================

def _step3_existing_table_evolution(conn):
    # --- asset_versions evolution ---
    # Add the UQ constraint needed for composite FKs from lineage tables.
    with op.batch_alter_table("asset_versions", schema=None) as batch_op:
        batch_op.create_unique_constraint(
            "uq_av_workspace_truth_parent",
            ["workspace_id", "id", "truth_package_id", "parent_version_id"],
        )
        batch_op.create_foreign_key(
            "fk_asset_versions_workspace_truth_pkg_truth_packages",
            "truth_packages",
            ["workspace_id", "truth_package_id"],
            ["workspace_id", "id"],
            ondelete="RESTRICT",
        )

    if _is_pg(conn):
        # C1: Widen origin_kind CHECK to include 'localized'.
        # The existing CHECK from n1_generation_assets_expand only allows
        # ('generation', 'upload', 'reuse', 'legacy'). We need 'localized'.
        conn.execute(text(
            "ALTER TABLE asset_versions "
            "DROP CONSTRAINT IF EXISTS ck_asset_version_origin_kind"
        ))
        conn.execute(text(
            "ALTER TABLE asset_versions ADD CONSTRAINT "
            "ck_asset_version_origin_kind CHECK ("
            "origin_kind IN ('generation', 'upload', 'reuse', 'legacy', 'localized'))"
        ))

        # Localized assets must carry both truth_package_id and parent_version_id.
        conn.execute(text(
            "ALTER TABLE asset_versions ADD CONSTRAINT "
            "ck_av_localized_requires_truth_parent CHECK ("
            "origin_kind != 'localized' OR "
            "(truth_package_id IS NOT NULL AND parent_version_id IS NOT NULL))"
        ))

    # --- provider_call_attempts: widen step_kind, nullable spend_reservation,
    #     add chargeability + truth_package_id ---
    with op.batch_alter_table("provider_call_attempts", schema=None) as batch_op:
        batch_op.alter_column("step_kind", type_=sa.String(40))
        batch_op.alter_column("spend_reservation_id", nullable=True)
        batch_op.add_column(sa.Column(
            "chargeability", sa.String(24), nullable=True,
        ))
        batch_op.add_column(sa.Column(
            "truth_package_id", sa.String(64), nullable=True,
        ))

    # Backfill chargeability on existing rows
    conn.execute(text(
        "UPDATE provider_call_attempts SET chargeability = CASE "
        "WHEN execution_mode = 'byok' THEN 'charged' "
        "WHEN spend_reservation_id IS NOT NULL THEN 'legacy_reserved' "
        "WHEN execution_mode = 'mock' THEN 'free_mock' "
        "WHEN execution_mode = 'sandbox' THEN 'free_sandbox' "
        "ELSE 'charged' END "
        "WHERE chargeability IS NULL"
    ))

    # Set chargeability NOT NULL after backfill + add the alt-key UQ
    # The alt-key UQ must exist on both dialects because
    # localization_item_provider_calls and localization_preparation_provider_calls
    # reference it with composite FKs.
    with op.batch_alter_table("provider_call_attempts", schema=None) as batch_op:
        batch_op.alter_column("chargeability", nullable=False)
        batch_op.create_unique_constraint(
            "uq_provider_call_attempt_alt_key",
            ["workspace_id", "id", "operation_id", "truth_package_id",
             "step_kind", "execution_mode"],
        )
        batch_op.create_foreign_key(
            "fk_provider_call_attempts_workspace_truth_pkg_truth_packages",
            "truth_packages",
            ["workspace_id", "truth_package_id"],
            ["workspace_id", "id"],
            ondelete="RESTRICT",
        )

    # PG-only: CHECK constraints
    if _is_pg(conn):
        # Drop old step_kind CHECK if it exists and add the wider one
        conn.execute(text(
            "ALTER TABLE provider_call_attempts "
            "DROP CONSTRAINT IF EXISTS ck_provider_call_attempt_step_kind"
        ))
        conn.execute(text(
            "ALTER TABLE provider_call_attempts ADD CONSTRAINT "
            "ck_provider_call_attempt_step_kind CHECK ("
            "step_kind IN ('generation', 'visual_qc', 'source_analysis', "
            "'translation', 'localization_edit', 'localization_ocr', "
            "'localization_visual_qc'))"
        ))
        conn.execute(text(
            "ALTER TABLE provider_call_attempts ADD CONSTRAINT "
            "chargeability CHECK ("
            "chargeability IN ('charged', 'free_mock', 'free_sandbox', "
            "'legacy_reserved'))"
        ))
        conn.execute(text(
            "ALTER TABLE provider_call_attempts ADD CONSTRAINT "
            "chargeability_consistency CHECK ("
            "(chargeability = 'charged' AND execution_mode = 'byok' "
            "AND spend_reservation_id IS NOT NULL) OR "
            "(chargeability = 'free_mock' AND execution_mode = 'mock' "
            "AND spend_reservation_id IS NULL) OR "
            "(chargeability = 'free_sandbox' AND execution_mode = 'sandbox' "
            "AND spend_reservation_id IS NULL) OR "
            "(chargeability = 'legacy_reserved' AND execution_mode IN "
            "('mock', 'sandbox') AND spend_reservation_id IS NOT NULL "
            "AND step_kind IN ('generation', 'visual_qc')))"
        ))
        conn.execute(text(
            "ALTER TABLE provider_call_attempts ADD CONSTRAINT "
            "truth_required CHECK ("
            "step_kind NOT IN ('source_analysis', 'translation', "
            "'localization_edit', 'localization_ocr', "
            "'localization_visual_qc') OR truth_package_id IS NOT NULL)"
        ))


# =========================================================================
# Step 4: Batch core tables
# =========================================================================

def _step4_batch_tables():
    op.create_table(
        "localization_batches",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("product_version_id", sa.String(64), nullable=False),
        sa.Column("offer_version_id", sa.String(64), nullable=False),
        sa.Column("source_market_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("truth_package_id", sa.String(64), nullable=True),
        sa.Column("truth_data_mode", sa.String(20), nullable=False),
        sa.Column("provider_execution_mode", sa.String(20), nullable=False),
        sa.Column("evidence_tier", sa.String(20), nullable=True),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("preparation_stage", sa.String(20), nullable=True),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("truth_freshness", sa.String(20), nullable=True),
        sa.Column("frozen_recipe_json", sa.JSON(), nullable=True),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_lb_workspace_id"),
        sa.UniqueConstraint("workspace_id", "id", "truth_package_id",
                            name="uq_lb_workspace_id_truth"),
        sa.UniqueConstraint("workspace_id", "id", "truth_package_id",
                            "offer_version_id",
                            name="uq_lb_workspace_id_truth_offer"),
        sa.UniqueConstraint("workspace_id", "id", "truth_package_id",
                            "product_version_id", "offer_version_id",
                            name="uq_lb_workspace_id_truth_product_offer"),
        sa.UniqueConstraint("workspace_id", "id", "truth_package_id",
                            "provider_execution_mode",
                            name="uq_lb_workspace_id_truth_provider"),
        sa.UniqueConstraint("workspace_id", "id", "truth_package_id",
                            "product_version_id", "offer_version_id",
                            "provider_execution_mode",
                            name="uq_lb_workspace_id_truth_product_offer_provider"),
        sa.UniqueConstraint("workspace_id", "id", "truth_package_id",
                            "offer_version_id", "evidence_tier",
                            name="uq_lb_workspace_id_truth_offer_tier"),
        sa.UniqueConstraint("workspace_id", "id", "truth_package_id",
                            "product_version_id", "offer_version_id",
                            "truth_data_mode", "provider_execution_mode",
                            "evidence_tier",
                            name="uq_lb_workspace_id_full_provenance"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "truth_package_id"],
            ["truth_packages.workspace_id", "truth_packages.id"],
            name="fk_lb_truth_package",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "product_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_lb_product_version",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "offer_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_lb_offer_version",
        ),
        sa.CheckConstraint(
            "truth_data_mode IN ('real', 'mock')",
            name="ck_lb_truth_data_mode",
        ),
        sa.CheckConstraint(
            "provider_execution_mode IN ('byok', 'sandbox', 'mock')",
            name="ck_lb_provider_execution_mode",
        ),
        sa.CheckConstraint(
            "evidence_tier IS NULL OR "
            "evidence_tier IN ('production', 'sandbox', 'mock')",
            name="ck_lb_evidence_tier",
        ),
        sa.CheckConstraint(
            "phase IN ('draft', 'preparing', 'awaiting_copy_approval', 'ready', "
            "'running', 'closed', 'cancelled')",
            name="ck_lb_phase",
        ),
        sa.CheckConstraint(
            "preparation_stage IS NULL OR preparation_stage IN "
            "('truth', 'analysis', 'claim_review', 'copy', 'complete')",
            name="ck_lb_preparation_stage",
        ),
        sa.CheckConstraint(
            "outcome IN ('none', 'succeeded', 'partially_succeeded', 'failed')",
            name="ck_lb_outcome",
        ),
        sa.CheckConstraint(
            "truth_freshness IS NULL OR "
            "truth_freshness IN ('current', 'superseded', 'expired')",
            name="ck_lb_truth_freshness",
        ),
        sa.CheckConstraint(
            "max_attempts BETWEEN 1 AND 3",
            name="ck_lb_max_attempts",
        ),
    )

    op.create_table(
        "localization_batch_sources",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("batch_id", sa.String(64), nullable=False),
        sa.Column("asset_version_id", sa.String(64), nullable=False),
        sa.Column("decision_id", sa.String(64), nullable=False),
        sa.Column("experiment_id", sa.String(64), nullable=False),
        sa.Column("experiment_version_id", sa.String(64), nullable=False),
        sa.Column("experiment_version_candidate_id", sa.String(64), nullable=False),
        sa.Column("source_generation_mode", sa.String(20), nullable=False),
        sa.Column("experiment_evidence_mode", sa.String(20), nullable=False),
        sa.Column("source_market_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_lbs_workspace_id"),
        sa.UniqueConstraint("workspace_id", "id", "batch_id", "asset_version_id",
                            name="uq_lbs_workspace_id_batch_asset"),
        sa.UniqueConstraint("workspace_id", "id", "batch_id", "asset_version_id",
                            "decision_id",
                            name="uq_lbs_workspace_id_batch_asset_decision"),
        sa.UniqueConstraint("workspace_id", "id", "batch_id", "asset_version_id",
                            "decision_id", "source_generation_mode",
                            "experiment_evidence_mode",
                            name="uq_lbs_workspace_id_full_lineage"),
        sa.UniqueConstraint("workspace_id", "batch_id", "asset_version_id",
                            name="uq_lbs_batch_asset_unique"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "batch_id"],
            ["localization_batches.workspace_id", "localization_batches.id"],
            name="fk_lbs_batch",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "asset_version_id"],
            ["asset_versions.workspace_id", "asset_versions.id"],
            name="fk_lbs_asset_version",
        ),
        sa.ForeignKeyConstraint(
            [
                "workspace_id", "decision_id", "experiment_id",
                "experiment_version_id", "experiment_version_candidate_id",
            ],
            [
                "experiment_decisions.workspace_id",
                "experiment_decisions.id",
                "experiment_decisions.experiment_id",
                "experiment_decisions.experiment_version_id",
                "experiment_decisions.experiment_version_candidate_id",
            ],
            name="fk_lbs_decision",
        ),
        sa.ForeignKeyConstraint(
            [
                "workspace_id", "experiment_version_id",
                "experiment_version_candidate_id", "asset_version_id",
            ],
            [
                "experiment_version_candidates.workspace_id",
                "experiment_version_candidates.experiment_version_id",
                "experiment_version_candidates.id",
                "experiment_version_candidates.asset_version_id",
            ],
            name="fk_lbs_candidate",
        ),
        sa.CheckConstraint(
            "source_generation_mode IN ('byok', 'sandbox', 'mock')",
            name="ck_lbs_source_generation_mode",
        ),
        sa.CheckConstraint(
            "experiment_evidence_mode IN ('production', 'sandbox', 'mock')",
            name="ck_lbs_experiment_evidence_mode",
        ),
    )

    # Create localization_batch_commands WITHOUT the item FK (deferred).
    op.create_table(
        "localization_batch_commands",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("batch_id", sa.String(64), nullable=False),
        sa.Column("item_id", sa.String(64), nullable=True),
        sa.Column("command_kind", sa.String(40), nullable=False),
        sa.Column("operation_id", sa.String(64), nullable=True),
        sa.Column("semantic_key_hash", sa.String(64), nullable=False),
        sa.Column("request_fingerprint_hash", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_lbc_workspace_id"),
        sa.UniqueConstraint("workspace_id", "id", "batch_id",
                            name="uq_lbc_workspace_id_batch"),
        sa.UniqueConstraint("workspace_id", "id", "batch_id", "operation_id",
                            name="uq_lbc_workspace_id_batch_operation"),
        sa.UniqueConstraint("workspace_id", "semantic_key_hash",
                            name="uq_lbc_semantic_key"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "batch_id"],
            ["localization_batches.workspace_id", "localization_batches.id"],
            name="fk_lbc_batch",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "operation_id"],
            ["operation_records.workspace_id", "operation_records.id"],
            name="fk_lbc_operation",
        ),
        # NOTE: FK to localization_items is DEFERRED to step 6.
    )

    # Partial unique index for operation_id
    op.create_index(
        "uq_lbc_operation_id",
        "localization_batch_commands",
        ["workspace_id", "operation_id"],
        unique=True,
        postgresql_where=text("operation_id IS NOT NULL"),
        sqlite_where=text("operation_id IS NOT NULL"),
    )

    op.create_table(
        "localization_batch_market_targets",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("batch_id", sa.String(64), nullable=False),
        sa.Column("market_policy_version_id", sa.String(64),
                  sa.ForeignKey("market_policy_versions.id"), nullable=False),
        sa.Column("market", sa.String(10), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_lbmt_workspace_id"),
        sa.UniqueConstraint("workspace_id", "batch_id", "market",
                            name="uq_lbmt_batch_market"),
        sa.UniqueConstraint("workspace_id", "id", "batch_id", "market",
                            "market_policy_version_id",
                            name="uq_lbmt_workspace_id_batch_market_mpv"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "batch_id"],
            ["localization_batches.workspace_id", "localization_batches.id"],
            name="fk_lbmt_batch",
        ),
    )


# =========================================================================
# Step 5: Inventories, confirmations, claim decisions, copy versions
# =========================================================================

def _step5_claim_copy_tables():
    op.create_table(
        "source_claim_inventories",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("asset_version_id", sa.String(64), nullable=False),
        sa.Column("analysis_recipe_hash", sa.String(64), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("claims_json", sa.JSON(), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("rule_version", sa.String(40), nullable=False),
        sa.Column("analysis_execution_mode", sa.String(20), nullable=False),
        sa.Column("adapter_version", sa.String(40), nullable=False),
        sa.Column("mock_scenario", sa.String(100), nullable=True),
        sa.Column("mock_version", sa.String(40), nullable=True),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_sci_workspace_id"),
        sa.UniqueConstraint("workspace_id", "id", "asset_version_id",
                            name="uq_sci_workspace_id_asset_version"),
        sa.UniqueConstraint("workspace_id", "id", "asset_version_id",
                            "analysis_execution_mode",
                            name="uq_sci_workspace_id_asset_version_mode"),
        sa.UniqueConstraint("workspace_id", "asset_version_id",
                            "analysis_recipe_hash", "version_number",
                            name="uq_sci_workspace_asset_recipe_version"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "asset_version_id"],
            ["asset_versions.workspace_id", "asset_versions.id"],
            name="fk_sci_asset_version",
        ),
        sa.CheckConstraint(
            "analysis_execution_mode IN ('byok', 'sandbox', 'mock')",
            name="ck_sci_analysis_execution_mode",
        ),
    )

    op.create_table(
        "source_claim_confirmations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("inventory_id", sa.String(64), nullable=False),
        sa.Column("claim_index", sa.Integer(), nullable=False),
        sa.Column("confirmation_kind", sa.String(20), nullable=False),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_scc_workspace_id"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "inventory_id"],
            ["source_claim_inventories.workspace_id",
             "source_claim_inventories.id"],
            name="fk_scc_inventory",
        ),
        sa.CheckConstraint(
            "confirmation_kind IN ('accepted', 'rejected', 'needs_review')",
            name="ck_scc_confirmation_kind",
        ),
        sa.CheckConstraint(
            "role IN ('owner', 'member')",
            name="ck_scc_role",
        ),
    )

    op.create_table(
        "localization_claim_decision_sets",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("origin_batch_id", sa.String(64), nullable=False),
        sa.Column("inventory_id", sa.String(64), nullable=False),
        sa.Column("source_asset_version_id", sa.String(64), nullable=False),
        sa.Column("truth_package_id", sa.String(64), nullable=False),
        sa.Column("truth_package_market_id", sa.String(64), nullable=False),
        sa.Column("target_market", sa.String(10), nullable=False),
        sa.Column("market_policy_version_id", sa.String(64), nullable=False),
        sa.Column("decision_count", sa.Integer(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_lcds_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id", "id", "inventory_id", "truth_package_id",
            "truth_package_market_id", "target_market", "market_policy_version_id",
            name="uq_lcds_workspace_id_context",
        ),
        sa.UniqueConstraint(
            "workspace_id", "id", "source_asset_version_id", "inventory_id",
            "truth_package_id", "truth_package_market_id", "target_market",
            "market_policy_version_id",
            name="uq_lcds_workspace_id_source_context",
        ),
        sa.UniqueConstraint("workspace_id", "canonical_hash",
                            name="uq_lcds_workspace_hash"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "origin_batch_id", "truth_package_id"],
            [
                "localization_batches.workspace_id",
                "localization_batches.id",
                "localization_batches.truth_package_id",
            ],
            name="fk_lcds_origin_batch",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "inventory_id", "source_asset_version_id"],
            [
                "source_claim_inventories.workspace_id",
                "source_claim_inventories.id",
                "source_claim_inventories.asset_version_id",
            ],
            name="fk_lcds_inventory",
        ),
        sa.ForeignKeyConstraint(
            [
                "workspace_id", "truth_package_market_id", "truth_package_id",
                "target_market", "market_policy_version_id",
            ],
            [
                "truth_package_markets.workspace_id",
                "truth_package_markets.id",
                "truth_package_markets.truth_package_id",
                "truth_package_markets.market",
                "truth_package_markets.market_policy_version_id",
            ],
            name="fk_lcds_market",
        ),
        sa.CheckConstraint("decision_count >= 0", name="ck_lcds_decision_count"),
    )

    op.create_table(
        "localization_claim_decisions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("decision_set_id", sa.String(64), nullable=False),
        sa.Column("inventory_id", sa.String(64), nullable=False),
        sa.Column("claim_index", sa.Integer(), nullable=False),
        sa.Column("claim_scope_key", sa.String(120), nullable=False),
        sa.Column("target_market", sa.String(10), nullable=False),
        sa.Column("truth_package_id", sa.String(64), nullable=False),
        sa.Column("truth_package_market_id", sa.String(64), nullable=False),
        sa.Column("market_policy_version_id", sa.String(64), nullable=False),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("replacement_fact_id", sa.String(64), nullable=True),
        sa.Column("replacement_fact_scope_key", sa.String(120), nullable=True),
        sa.Column("removal_strategy_json", sa.JSON(), nullable=True),
        sa.Column("removal_strategy_hash", sa.String(64), nullable=True),
        sa.Column("preserve_policy_evidence_hash", sa.String(64), nullable=True),
        sa.Column("rationale", sa.String(500), nullable=True),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_lcd_workspace_id"),
        sa.UniqueConstraint("workspace_id", "decision_set_id", "claim_index",
                            name="uq_lcd_set_claim_index"),
        sa.ForeignKeyConstraint(
            [
                "workspace_id", "decision_set_id", "inventory_id",
                "truth_package_id", "truth_package_market_id", "target_market",
                "market_policy_version_id",
            ],
            [
                "localization_claim_decision_sets.workspace_id",
                "localization_claim_decision_sets.id",
                "localization_claim_decision_sets.inventory_id",
                "localization_claim_decision_sets.truth_package_id",
                "localization_claim_decision_sets.truth_package_market_id",
                "localization_claim_decision_sets.target_market",
                "localization_claim_decision_sets.market_policy_version_id",
            ],
            name="fk_lcd_decision_set",
        ),
        sa.ForeignKeyConstraint(
            [
                "workspace_id", "replacement_fact_id", "truth_package_id",
                "truth_package_market_id", "replacement_fact_scope_key",
            ],
            [
                "truth_facts.workspace_id",
                "truth_facts.id",
                "truth_facts.truth_package_id",
                "truth_facts.truth_package_market_id",
                "truth_facts.scope_key",
            ],
            name="fk_lcd_replacement_fact",
        ),
        sa.CheckConstraint(
            "action IN ('replace', 'remove', 'preserve')",
            name="ck_lcd_action",
        ),
        sa.CheckConstraint(
            "role IN ('owner', 'member')",
            name="ck_lcd_role",
        ),
        sa.CheckConstraint(
            "(action = 'replace' AND replacement_fact_id IS NOT NULL "
            "AND replacement_fact_scope_key = claim_scope_key "
            "AND removal_strategy_json IS NULL AND removal_strategy_hash IS NULL "
            "AND preserve_policy_evidence_hash IS NULL) OR "
            "(action != 'replace' AND replacement_fact_id IS NULL "
            "AND replacement_fact_scope_key IS NULL)",
            name="ck_lcd_action_replace",
        ),
        sa.CheckConstraint(
            "(action = 'remove' AND removal_strategy_json IS NOT NULL "
            "AND removal_strategy_hash IS NOT NULL "
            "AND preserve_policy_evidence_hash IS NULL) OR "
            "(action != 'remove' AND removal_strategy_json IS NULL "
            "AND removal_strategy_hash IS NULL)",
            name="ck_lcd_action_remove",
        ),
        sa.CheckConstraint(
            "(action = 'preserve' AND preserve_policy_evidence_hash IS NOT NULL) OR "
            "(action != 'preserve' AND preserve_policy_evidence_hash IS NULL)",
            name="ck_lcd_action_preserve",
        ),
    )

    op.create_table(
        "localized_copy_versions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("copy_series_hash", sa.String(64), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("source_asset_version_id", sa.String(64), nullable=False),
        sa.Column("inventory_id", sa.String(64), nullable=False),
        sa.Column("truth_package_id", sa.String(64), nullable=False),
        sa.Column("truth_package_market_id", sa.String(64), nullable=False),
        sa.Column("claim_decision_set_id", sa.String(64), nullable=False),
        sa.Column("target_market", sa.String(10), nullable=False),
        sa.Column("market_policy_version_id", sa.String(64), nullable=False),
        sa.Column("content_json", sa.JSON(), nullable=False),
        sa.Column("copy_origin", sa.String(32), nullable=False),
        sa.Column("translation_execution_mode", sa.String(20), nullable=True),
        sa.Column("translation_provider", sa.String(64), nullable=True),
        sa.Column("translation_model", sa.String(200), nullable=True),
        sa.Column("translation_adapter_version", sa.String(40), nullable=True),
        sa.Column("mock_scenario", sa.String(100), nullable=True),
        sa.Column("mock_version", sa.String(40), nullable=True),
        sa.Column("brand_localization_policy_version_id", sa.String(64), nullable=True),
        sa.Column("copy_policy_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("copy_policy_snapshot_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_lcv_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id", "id", "source_asset_version_id", "inventory_id",
            "truth_package_id", "truth_package_market_id", "claim_decision_set_id",
            "target_market", "market_policy_version_id",
            name="uq_lcv_workspace_id_parent",
        ),
        sa.UniqueConstraint(
            "workspace_id", "id", "source_asset_version_id", "inventory_id",
            "truth_package_id", "truth_package_market_id", "claim_decision_set_id",
            "target_market", "market_policy_version_id", "translation_execution_mode",
            name="uq_lcv_workspace_id_translated",
        ),
        sa.UniqueConstraint("workspace_id", "copy_series_hash", "version_number",
                            name="uq_lcv_series_version"),
        sa.ForeignKeyConstraint(
            [
                "workspace_id", "truth_package_market_id", "truth_package_id",
                "target_market", "market_policy_version_id",
            ],
            [
                "truth_package_markets.workspace_id",
                "truth_package_markets.id",
                "truth_package_markets.truth_package_id",
                "truth_package_markets.market",
                "truth_package_markets.market_policy_version_id",
            ],
            name="fk_lcv_market",
        ),
        sa.ForeignKeyConstraint(
            [
                "workspace_id", "claim_decision_set_id", "source_asset_version_id",
                "inventory_id", "truth_package_id", "truth_package_market_id",
                "target_market", "market_policy_version_id",
            ],
            [
                "localization_claim_decision_sets.workspace_id",
                "localization_claim_decision_sets.id",
                "localization_claim_decision_sets.source_asset_version_id",
                "localization_claim_decision_sets.inventory_id",
                "localization_claim_decision_sets.truth_package_id",
                "localization_claim_decision_sets.truth_package_market_id",
                "localization_claim_decision_sets.target_market",
                "localization_claim_decision_sets.market_policy_version_id",
            ],
            name="fk_lcv_decision_set",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_asset_version_id"],
            ["asset_versions.workspace_id", "asset_versions.id"],
            name="fk_lcv_source_asset_version",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "inventory_id", "source_asset_version_id"],
            [
                "source_claim_inventories.workspace_id",
                "source_claim_inventories.id",
                "source_claim_inventories.asset_version_id",
            ],
            name="fk_lcv_inventory",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "truth_package_id"],
            ["truth_packages.workspace_id", "truth_packages.id"],
            name="fk_lcv_truth_package",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "brand_localization_policy_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_lcv_brand_policy_version",
        ),
        sa.CheckConstraint(
            "copy_origin IN ('translated', 'source_locale_projection')",
            name="ck_lcv_copy_origin",
        ),
        sa.CheckConstraint(
            "translation_execution_mode IS NULL OR "
            "translation_execution_mode IN ('byok', 'sandbox', 'mock')",
            name="ck_lcv_translation_execution_mode",
        ),
        sa.CheckConstraint(
            "state IN ('draft', 'needs_review', 'approved', 'superseded')",
            name="ck_lcv_state",
        ),
        sa.CheckConstraint(
            "(copy_origin = 'translated' AND translation_execution_mode IS NOT NULL "
            "AND translation_provider IS NOT NULL "
            "AND translation_model IS NOT NULL "
            "AND translation_adapter_version IS NOT NULL) OR "
            "(copy_origin = 'source_locale_projection' AND "
            "translation_execution_mode IS NULL AND translation_provider IS NULL "
            "AND translation_model IS NULL AND translation_adapter_version IS NULL)",
            name="ck_lcv_translation_required",
        ),
        sa.CheckConstraint(
            "(translation_execution_mode = 'mock' AND mock_scenario IS NOT NULL "
            "AND mock_version IS NOT NULL) OR "
            "(translation_execution_mode IN ('byok', 'sandbox') AND "
            "mock_scenario IS NULL AND mock_version IS NULL) OR "
            "(translation_execution_mode IS NULL AND mock_scenario IS NULL "
            "AND mock_version IS NULL)",
            name="ck_lcv_translation_mock",
        ),
    )

    op.create_table(
        "localized_copy_approvals",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("copy_version_id", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_lca_workspace_id"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "copy_version_id"],
            ["localized_copy_versions.workspace_id", "localized_copy_versions.id"],
            name="fk_lca_copy_version",
        ),
        sa.CheckConstraint(
            "role IN ('owner', 'member')",
            name="ck_lca_role",
        ),
    )


# =========================================================================
# Step 6: Items, deferred FK, attempts, outputs, publications, QC,
#          semantic claims, provider-call join tables
# =========================================================================

def _step6_item_attempt_tables(conn):
    op.create_table(
        "localization_items",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("batch_id", sa.String(64), nullable=False),
        sa.Column("batch_source_id", sa.String(64), nullable=False),
        sa.Column("batch_market_target_id", sa.String(64), nullable=False),
        sa.Column("source_asset_version_id", sa.String(64), nullable=False),
        sa.Column("decision_id", sa.String(64), nullable=False),
        sa.Column("target_market", sa.String(10), nullable=False),
        sa.Column("market_policy_version_id", sa.String(64),
                  sa.ForeignKey("market_policy_versions.id"), nullable=False),
        sa.Column("truth_package_id", sa.String(64), nullable=False),
        sa.Column("truth_package_market_id", sa.String(64), nullable=False),
        sa.Column("inventory_id", sa.String(64), nullable=False),
        sa.Column("claim_decision_set_id", sa.String(64), nullable=False),
        sa.Column("copy_version_id", sa.String(64), nullable=True),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("reason_code", sa.String(60), nullable=True),
        sa.Column("reason_detail", sa.String(500), nullable=True),
        sa.Column("recoverable", sa.Boolean(), nullable=True),
        sa.Column("next_action", sa.String(60), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("item_fingerprint_hash", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_li_workspace_id"),
        sa.UniqueConstraint("workspace_id", "id", "batch_id",
                            name="uq_li_workspace_id_batch"),
        sa.UniqueConstraint("workspace_id", "id", "batch_id", "truth_package_id",
                            name="uq_li_workspace_id_batch_truth"),
        sa.UniqueConstraint(
            "workspace_id", "id", "batch_id", "source_asset_version_id",
            "target_market", "market_policy_version_id", "truth_package_id",
            name="uq_li_workspace_id_binding_context",
        ),
        sa.UniqueConstraint(
            "workspace_id", "id", "batch_id", "batch_source_id",
            "source_asset_version_id", "decision_id", "target_market",
            "market_policy_version_id", "truth_package_id",
            "truth_package_market_id", "inventory_id",
            "claim_decision_set_id", "copy_version_id",
            name="uq_li_lineage_target",
        ),
        sa.UniqueConstraint("workspace_id", "batch_id", "batch_source_id",
                            "target_market",
                            name="uq_li_batch_source_market"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "batch_source_id", "batch_id",
             "source_asset_version_id", "decision_id"],
            [
                "localization_batch_sources.workspace_id",
                "localization_batch_sources.id",
                "localization_batch_sources.batch_id",
                "localization_batch_sources.asset_version_id",
                "localization_batch_sources.decision_id",
            ],
            name="fk_li_batch_source",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "batch_market_target_id", "batch_id",
             "target_market", "market_policy_version_id"],
            [
                "localization_batch_market_targets.workspace_id",
                "localization_batch_market_targets.id",
                "localization_batch_market_targets.batch_id",
                "localization_batch_market_targets.market",
                "localization_batch_market_targets.market_policy_version_id",
            ],
            name="fk_li_batch_market_target",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "batch_id", "truth_package_id"],
            [
                "localization_batches.workspace_id",
                "localization_batches.id",
                "localization_batches.truth_package_id",
            ],
            name="fk_li_batch",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "truth_package_market_id", "truth_package_id",
             "target_market", "market_policy_version_id"],
            [
                "truth_package_markets.workspace_id",
                "truth_package_markets.id",
                "truth_package_markets.truth_package_id",
                "truth_package_markets.market",
                "truth_package_markets.market_policy_version_id",
            ],
            name="fk_li_truth_market",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "claim_decision_set_id",
             "source_asset_version_id", "inventory_id", "truth_package_id",
             "truth_package_market_id", "target_market",
             "market_policy_version_id"],
            [
                "localization_claim_decision_sets.workspace_id",
                "localization_claim_decision_sets.id",
                "localization_claim_decision_sets.source_asset_version_id",
                "localization_claim_decision_sets.inventory_id",
                "localization_claim_decision_sets.truth_package_id",
                "localization_claim_decision_sets.truth_package_market_id",
                "localization_claim_decision_sets.target_market",
                "localization_claim_decision_sets.market_policy_version_id",
            ],
            name="fk_li_claim_decision_set",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "copy_version_id", "source_asset_version_id",
             "inventory_id", "truth_package_id", "truth_package_market_id",
             "claim_decision_set_id", "target_market",
             "market_policy_version_id"],
            [
                "localized_copy_versions.workspace_id",
                "localized_copy_versions.id",
                "localized_copy_versions.source_asset_version_id",
                "localized_copy_versions.inventory_id",
                "localized_copy_versions.truth_package_id",
                "localized_copy_versions.truth_package_market_id",
                "localized_copy_versions.claim_decision_set_id",
                "localized_copy_versions.target_market",
                "localized_copy_versions.market_policy_version_id",
            ],
            name="fk_li_copy_version",
        ),
        sa.CheckConstraint(
            "state IN ('queued', 'running', 'retry_wait', 'paused', "
            "'succeeded', 'failed', 'skipped', 'cancelled', 'uncertain')",
            name="ck_li_state",
        ),
    )

    # Deferred FK: localization_batch_commands.item_id -> localization_items
    with op.batch_alter_table("localization_batch_commands", schema=None) as batch_op:
        batch_op.create_foreign_key(
            "fk_lbc_item",
            "localization_items",
            ["workspace_id", "item_id", "batch_id"],
            ["workspace_id", "id", "batch_id"],
        )

    op.create_table(
        "localization_attempts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("item_id", sa.String(64), nullable=False),
        sa.Column("batch_id", sa.String(64), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("prompt_params_hash", sa.String(64), nullable=False),
        sa.Column("input_versions_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_la_workspace_id"),
        sa.UniqueConstraint("workspace_id", "id", "item_id", "batch_id",
                            name="uq_la_workspace_id_item_batch"),
        sa.UniqueConstraint("workspace_id", "item_id", "attempt_number",
                            name="uq_la_item_attempt_number"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "item_id", "batch_id"],
            [
                "localization_items.workspace_id",
                "localization_items.id",
                "localization_items.batch_id",
            ],
            name="fk_la_item",
        ),
        sa.CheckConstraint(
            "attempt_number BETWEEN 1 AND 3",
            name="ck_la_attempt_number",
        ),
        sa.CheckConstraint(
            "state IN ('authorized', 'dispatching', 'completed', 'failed', "
            "'uncertain')",
            name="ck_la_state",
        ),
    )

    op.create_table(
        "localization_attempt_outputs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("attempt_id", sa.String(64), nullable=False),
        sa.Column("item_id", sa.String(64), nullable=False),
        sa.Column("batch_id", sa.String(64), nullable=False),
        sa.Column("object_key", sa.String(1000), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("mime_type", sa.String(100), nullable=False),
        sa.Column("output_evidence_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_lao_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id", "id", "attempt_id", "item_id", "batch_id",
            name="uq_lao_workspace_id_attempt_item_batch",
        ),
        sa.UniqueConstraint("workspace_id", "attempt_id",
                            name="uq_lao_workspace_id_attempt"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "attempt_id", "item_id", "batch_id"],
            [
                "localization_attempts.workspace_id",
                "localization_attempts.id",
                "localization_attempts.item_id",
                "localization_attempts.batch_id",
            ],
            name="fk_lao_attempt",
        ),
    )

    op.create_table(
        "localization_output_publications",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("output_id", sa.String(64), nullable=False),
        sa.Column("attempt_id", sa.String(64), nullable=False),
        sa.Column("item_id", sa.String(64), nullable=False),
        sa.Column("batch_id", sa.String(64), nullable=False),
        sa.Column("publication_kind", sa.String(20), nullable=False),
        sa.Column("asset_version_id", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("recheck_hash", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_lop_workspace_id"),
        sa.UniqueConstraint("workspace_id", "output_id",
                            name="uq_lop_workspace_id_output"),
        sa.UniqueConstraint("workspace_id", "asset_version_id",
                            name="uq_lop_workspace_id_asset_version"),
        sa.UniqueConstraint(
            "workspace_id", "id", "asset_version_id", "item_id", "batch_id",
            name="uq_lop_binding_target",
        ),
        sa.UniqueConstraint(
            "workspace_id", "asset_version_id", "output_id", "attempt_id",
            "item_id", "batch_id",
            name="uq_lop_lineage_target",
        ),
        sa.UniqueConstraint(
            "workspace_id", "id", "asset_version_id", "output_id",
            "attempt_id", "item_id", "batch_id",
            name="uq_lop_identity_lineage_target",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "output_id", "attempt_id", "item_id", "batch_id"],
            [
                "localization_attempt_outputs.workspace_id",
                "localization_attempt_outputs.id",
                "localization_attempt_outputs.attempt_id",
                "localization_attempt_outputs.item_id",
                "localization_attempt_outputs.batch_id",
            ],
            name="fk_lop_output",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "asset_version_id"],
            ["asset_versions.workspace_id", "asset_versions.id"],
            name="fk_lop_asset_version",
        ),
        sa.CheckConstraint(
            "publication_kind IN ('normal', 'adopted')",
            name="ck_lop_publication_kind",
        ),
        sa.CheckConstraint(
            "role IN ('owner', 'member')",
            name="ck_lop_role",
        ),
        sa.CheckConstraint(
            "(publication_kind = 'adopted' AND recheck_hash IS NOT NULL) OR "
            "(publication_kind = 'normal' AND recheck_hash IS NULL)",
            name="ck_lop_recheck_hash",
        ),
    )

    op.create_table(
        "localization_qc_records",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("attempt_id", sa.String(64), nullable=False),
        sa.Column("item_id", sa.String(64), nullable=False),
        sa.Column("batch_id", sa.String(64), nullable=False),
        sa.Column("check_kind", sa.String(40), nullable=False),
        sa.Column("check_slot_key", sa.String(120), nullable=False),
        sa.Column("expected_json", sa.JSON(), nullable=False),
        sa.Column("observed_json", sa.JSON(), nullable=False),
        sa.Column("evidence_location_json", sa.JSON(), nullable=True),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("result", sa.String(10), nullable=False),
        sa.Column("rule_version", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_lqr_workspace_id"),
        sa.UniqueConstraint("workspace_id", "attempt_id", "check_slot_key",
                            name="uq_lqr_attempt_slot"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "attempt_id", "item_id", "batch_id"],
            [
                "localization_attempts.workspace_id",
                "localization_attempts.id",
                "localization_attempts.item_id",
                "localization_attempts.batch_id",
            ],
            name="fk_lqr_attempt",
        ),
        sa.CheckConstraint(
            "severity IN ('blocking', 'warning')",
            name="ck_lqr_severity",
        ),
        sa.CheckConstraint(
            "result IN ('pass', 'fail')",
            name="ck_lqr_result",
        ),
    )

    op.create_table(
        "localization_semantic_claims",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("item_fingerprint_hash", sa.String(64), nullable=False),
        sa.Column("batch_id", sa.String(64), nullable=False),
        sa.Column("item_id", sa.String(64), nullable=False),
        sa.Column("current_operation_id", sa.String(64), nullable=True),
        sa.Column("claim_state", sa.String(20), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_lsc_workspace_id"),
        sa.UniqueConstraint("workspace_id", "item_fingerprint_hash",
                            name="uq_lsc_item_fingerprint"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "batch_id"],
            ["localization_batches.workspace_id", "localization_batches.id"],
            name="fk_lsc_batch",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "item_id", "batch_id"],
            [
                "localization_items.workspace_id",
                "localization_items.id",
                "localization_items.batch_id",
            ],
            name="fk_lsc_item",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "current_operation_id"],
            ["operation_records.workspace_id", "operation_records.id"],
            name="fk_lsc_current_operation",
        ),
        sa.CheckConstraint(
            "claim_state IN ('active', 'succeeded', 'failed', 'cancelled')",
            name="ck_lsc_claim_state",
        ),
    )

    op.create_table(
        "localization_item_provider_calls",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("item_id", sa.String(64), nullable=False),
        sa.Column("attempt_id", sa.String(64), nullable=False),
        sa.Column("batch_id", sa.String(64), nullable=False),
        sa.Column("batch_command_id", sa.String(64), nullable=False),
        sa.Column("operation_id", sa.String(64), nullable=False),
        sa.Column("truth_package_id", sa.String(64), nullable=False),
        sa.Column("provider_execution_mode", sa.String(20), nullable=False),
        sa.Column("provider_step_kind", sa.String(40), nullable=False),
        sa.Column("provider_call_attempt_id", sa.String(64), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_lipc_workspace_id"),
        sa.UniqueConstraint("workspace_id", "provider_call_attempt_id",
                            name="uq_lipc_provider_call_attempt"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "attempt_id", "item_id", "batch_id"],
            [
                "localization_attempts.workspace_id",
                "localization_attempts.id",
                "localization_attempts.item_id",
                "localization_attempts.batch_id",
            ],
            name="fk_lipc_attempt",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "item_id", "batch_id", "truth_package_id"],
            [
                "localization_items.workspace_id",
                "localization_items.id",
                "localization_items.batch_id",
                "localization_items.truth_package_id",
            ],
            name="fk_lipc_item",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "batch_id", "truth_package_id",
             "provider_execution_mode"],
            [
                "localization_batches.workspace_id",
                "localization_batches.id",
                "localization_batches.truth_package_id",
                "localization_batches.provider_execution_mode",
            ],
            name="fk_lipc_batch",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "batch_command_id", "batch_id", "operation_id"],
            [
                "localization_batch_commands.workspace_id",
                "localization_batch_commands.id",
                "localization_batch_commands.batch_id",
                "localization_batch_commands.operation_id",
            ],
            name="fk_lipc_batch_command",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "provider_call_attempt_id", "operation_id",
             "truth_package_id", "provider_step_kind",
             "provider_execution_mode"],
            [
                "provider_call_attempts.workspace_id",
                "provider_call_attempts.id",
                "provider_call_attempts.operation_id",
                "provider_call_attempts.truth_package_id",
                "provider_call_attempts.step_kind",
                "provider_call_attempts.execution_mode",
            ],
            name="fk_lipc_provider_call_attempt",
        ),
        sa.CheckConstraint(
            "provider_execution_mode IN ('byok', 'sandbox', 'mock')",
            name="ck_lipc_exec_mode",
        ),
        sa.CheckConstraint(
            "provider_step_kind IN ('localization_edit', 'localization_ocr', "
            "'localization_visual_qc')",
            name="ck_lipc_provider_step_kind",
        ),
    )

    op.create_table(
        "localization_preparation_provider_calls",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("batch_id", sa.String(64), nullable=False),
        sa.Column("batch_source_id", sa.String(64), nullable=False),
        sa.Column("source_asset_version_id", sa.String(64), nullable=False),
        sa.Column("batch_command_id", sa.String(64), nullable=False),
        sa.Column("operation_id", sa.String(64), nullable=False),
        sa.Column("truth_package_id", sa.String(64), nullable=False),
        sa.Column("provider_execution_mode", sa.String(20), nullable=False),
        sa.Column("work_kind", sa.String(20), nullable=False),
        sa.Column("provider_step_kind", sa.String(40), nullable=False),
        sa.Column("input_inventory_id", sa.String(64), nullable=True),
        sa.Column("truth_package_market_id", sa.String(64), nullable=True),
        sa.Column("target_market", sa.String(10), nullable=True),
        sa.Column("market_policy_version_id", sa.String(64), nullable=True),
        sa.Column("claim_decision_set_id", sa.String(64), nullable=True),
        sa.Column("subject_semantic_key_hash", sa.String(64), nullable=False),
        sa.Column("provider_call_attempt_id", sa.String(64), nullable=False),
        sa.Column("result_inventory_id", sa.String(64), nullable=True),
        sa.Column("result_copy_version_id", sa.String(64), nullable=True),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("workspace_id", "id", name="uq_lppc_workspace_id"),
        sa.UniqueConstraint("workspace_id", "subject_semantic_key_hash",
                            name="uq_lppc_subject_semantic_key"),
        sa.UniqueConstraint("workspace_id", "provider_call_attempt_id",
                            name="uq_lppc_provider_call_attempt"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "batch_id", "truth_package_id",
             "provider_execution_mode"],
            [
                "localization_batches.workspace_id",
                "localization_batches.id",
                "localization_batches.truth_package_id",
                "localization_batches.provider_execution_mode",
            ],
            name="fk_lppc_batch",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "batch_source_id", "batch_id",
             "source_asset_version_id"],
            [
                "localization_batch_sources.workspace_id",
                "localization_batch_sources.id",
                "localization_batch_sources.batch_id",
                "localization_batch_sources.asset_version_id",
            ],
            name="fk_lppc_batch_source",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "batch_command_id", "batch_id", "operation_id"],
            [
                "localization_batch_commands.workspace_id",
                "localization_batch_commands.id",
                "localization_batch_commands.batch_id",
                "localization_batch_commands.operation_id",
            ],
            name="fk_lppc_batch_command",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "input_inventory_id", "source_asset_version_id"],
            [
                "source_claim_inventories.workspace_id",
                "source_claim_inventories.id",
                "source_claim_inventories.asset_version_id",
            ],
            name="fk_lppc_input_inventory",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "result_inventory_id", "source_asset_version_id",
             "provider_execution_mode"],
            [
                "source_claim_inventories.workspace_id",
                "source_claim_inventories.id",
                "source_claim_inventories.asset_version_id",
                "source_claim_inventories.analysis_execution_mode",
            ],
            name="fk_lppc_result_inventory",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "truth_package_market_id", "truth_package_id",
             "target_market", "market_policy_version_id"],
            [
                "truth_package_markets.workspace_id",
                "truth_package_markets.id",
                "truth_package_markets.truth_package_id",
                "truth_package_markets.market",
                "truth_package_markets.market_policy_version_id",
            ],
            name="fk_lppc_truth_market",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "claim_decision_set_id",
             "source_asset_version_id", "input_inventory_id",
             "truth_package_id", "truth_package_market_id", "target_market",
             "market_policy_version_id"],
            [
                "localization_claim_decision_sets.workspace_id",
                "localization_claim_decision_sets.id",
                "localization_claim_decision_sets.source_asset_version_id",
                "localization_claim_decision_sets.inventory_id",
                "localization_claim_decision_sets.truth_package_id",
                "localization_claim_decision_sets.truth_package_market_id",
                "localization_claim_decision_sets.target_market",
                "localization_claim_decision_sets.market_policy_version_id",
            ],
            name="fk_lppc_claim_decision_set",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "result_copy_version_id",
             "source_asset_version_id", "input_inventory_id",
             "truth_package_id", "truth_package_market_id",
             "claim_decision_set_id", "target_market",
             "market_policy_version_id", "provider_execution_mode"],
            [
                "localized_copy_versions.workspace_id",
                "localized_copy_versions.id",
                "localized_copy_versions.source_asset_version_id",
                "localized_copy_versions.inventory_id",
                "localized_copy_versions.truth_package_id",
                "localized_copy_versions.truth_package_market_id",
                "localized_copy_versions.claim_decision_set_id",
                "localized_copy_versions.target_market",
                "localized_copy_versions.market_policy_version_id",
                "localized_copy_versions.translation_execution_mode",
            ],
            name="fk_lppc_result_copy_version",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "provider_call_attempt_id", "operation_id",
             "truth_package_id", "provider_step_kind",
             "provider_execution_mode"],
            [
                "provider_call_attempts.workspace_id",
                "provider_call_attempts.id",
                "provider_call_attempts.operation_id",
                "provider_call_attempts.truth_package_id",
                "provider_call_attempts.step_kind",
                "provider_call_attempts.execution_mode",
            ],
            name="fk_lppc_provider_call_attempt",
        ),
        sa.CheckConstraint(
            "provider_execution_mode IN ('byok', 'sandbox', 'mock')",
            name="ck_lppc_exec_mode",
        ),
        sa.CheckConstraint(
            "work_kind IN ('analysis', 'translation')",
            name="ck_lppc_work_kind",
        ),
        sa.CheckConstraint(
            "provider_step_kind IN ('source_analysis', 'translation')",
            name="ck_lppc_step_kind",
        ),
        sa.CheckConstraint(
            "state IN ('prepared', 'succeeded', 'failed', 'uncertain')",
            name="ck_lppc_state",
        ),
        sa.CheckConstraint(
            "(work_kind = 'analysis' AND provider_step_kind = 'source_analysis' "
            "AND input_inventory_id IS NULL AND truth_package_market_id IS NULL "
            "AND target_market IS NULL AND market_policy_version_id IS NULL "
            "AND claim_decision_set_id IS NULL) OR "
            "(work_kind = 'translation' AND provider_step_kind = 'translation' "
            "AND input_inventory_id IS NOT NULL "
            "AND truth_package_market_id IS NOT NULL "
            "AND target_market IS NOT NULL "
            "AND market_policy_version_id IS NOT NULL "
            "AND claim_decision_set_id IS NOT NULL)",
            name="ck_lppc_kind_check",
        ),
        sa.CheckConstraint(
            "(state = 'succeeded' AND completed_at IS NOT NULL AND "
            "((work_kind = 'analysis' AND result_inventory_id IS NOT NULL "
            "AND result_copy_version_id IS NULL) OR "
            "(work_kind = 'translation' AND result_copy_version_id IS NOT NULL "
            "AND result_inventory_id IS NULL))) OR "
            "(state != 'succeeded' AND result_inventory_id IS NULL AND "
            "result_copy_version_id IS NULL)",
            name="ck_lppc_done_check",
        ),
    )


# =========================================================================
# Step 7: Bindings, binding events, lineage
# =========================================================================

def _step7_binding_lineage_tables():
    op.create_table(
        "market_asset_bindings",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("source_asset_version_id", sa.String(64), nullable=False),
        sa.Column("target_market", sa.String(10), nullable=False),
        sa.Column("offer_version_id", sa.String(64), nullable=False),
        sa.Column("bound_asset_version_id", sa.String(64), nullable=False),
        sa.Column("binding_kind", sa.String(20), nullable=False),
        sa.Column("publication_id", sa.String(64), nullable=True),
        sa.Column("review_state", sa.String(20), nullable=False),
        sa.Column("truth_freshness", sa.String(20), nullable=False),
        sa.Column("ready_for_experiment", sa.Boolean(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("batch_id", sa.String(64), nullable=False),
        sa.Column("item_id", sa.String(64), nullable=False),
        sa.Column("truth_package_id", sa.String(64), nullable=False),
        sa.Column("market_policy_version_id", sa.String(64), nullable=False),
        sa.Column("evidence_tier", sa.String(20), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_mab_workspace_id"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "item_id", "batch_id", "source_asset_version_id",
             "target_market", "market_policy_version_id", "truth_package_id"],
            [
                "localization_items.workspace_id",
                "localization_items.id",
                "localization_items.batch_id",
                "localization_items.source_asset_version_id",
                "localization_items.target_market",
                "localization_items.market_policy_version_id",
                "localization_items.truth_package_id",
            ],
            name="fk_mab_item",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "publication_id", "bound_asset_version_id",
             "item_id", "batch_id"],
            [
                "localization_output_publications.workspace_id",
                "localization_output_publications.id",
                "localization_output_publications.asset_version_id",
                "localization_output_publications.item_id",
                "localization_output_publications.batch_id",
            ],
            name="fk_mab_publication",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_asset_version_id"],
            ["asset_versions.workspace_id", "asset_versions.id"],
            name="fk_mab_source_asset_version",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "bound_asset_version_id"],
            ["asset_versions.workspace_id", "asset_versions.id"],
            name="fk_mab_bound_asset_version",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "batch_id", "truth_package_id",
             "offer_version_id", "evidence_tier"],
            [
                "localization_batches.workspace_id",
                "localization_batches.id",
                "localization_batches.truth_package_id",
                "localization_batches.offer_version_id",
                "localization_batches.evidence_tier",
            ],
            name="fk_mab_batch",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "truth_package_id"],
            ["truth_packages.workspace_id", "truth_packages.id"],
            name="fk_mab_truth_package",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "offer_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_mab_offer_version",
        ),
        sa.CheckConstraint(
            "binding_kind IN ('localized', 'source_reuse')",
            name="ck_mab_binding_kind",
        ),
        sa.CheckConstraint(
            "review_state IN ('pending_review', 'approved', 'rejected', "
            "'needs_changes')",
            name="ck_mab_review_state",
        ),
        sa.CheckConstraint(
            "truth_freshness IN ('current', 'stale')",
            name="ck_mab_truth_freshness",
        ),
        sa.CheckConstraint(
            "evidence_tier IN ('production', 'sandbox', 'mock')",
            name="ck_mab_evidence_tier",
        ),
        sa.CheckConstraint(
            "(binding_kind = 'localized' AND publication_id IS NOT NULL) OR "
            "(binding_kind = 'source_reuse' AND publication_id IS NULL AND "
            "bound_asset_version_id = source_asset_version_id)",
            name="ck_mab_binding_kind_consistency",
        ),
    )

    # Partial unique indexes for market_asset_bindings
    op.create_index(
        "uq_mab_active_binding",
        "market_asset_bindings",
        ["workspace_id", "source_asset_version_id", "target_market",
         "offer_version_id"],
        unique=True,
        postgresql_where=text("active = true"),
        sqlite_where=text("active = true"),
    )
    op.create_index(
        "uq_mab_publication",
        "market_asset_bindings",
        ["workspace_id", "publication_id"],
        unique=True,
        postgresql_where=text("publication_id IS NOT NULL"),
        sqlite_where=text("publication_id IS NOT NULL"),
    )

    op.create_table(
        "market_asset_binding_events",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("binding_id", sa.String(64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("dependency_change_id", sa.String(64), nullable=True),
        sa.Column("previous_state_json", sa.JSON(), nullable=False),
        sa.Column("new_state_json", sa.JSON(), nullable=False),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_mabe_workspace_id"),
        sa.UniqueConstraint("workspace_id", "binding_id", "sequence",
                            name="uq_mabe_binding_sequence"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "binding_id"],
            ["market_asset_bindings.workspace_id", "market_asset_bindings.id"],
            name="fk_mabe_binding",
        ),
        sa.CheckConstraint(
            "sequence >= 1",
            name="ck_mabe_sequence",
        ),
    )

    # Partial unique index for dependency_change_id
    op.create_index(
        "uq_mabe_dependency_change",
        "market_asset_binding_events",
        ["workspace_id", "binding_id", "dependency_change_id"],
        unique=True,
        postgresql_where=text("dependency_change_id IS NOT NULL"),
        sqlite_where=text("dependency_change_id IS NOT NULL"),
    )

    op.create_table(
        "localized_asset_lineages",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("asset_version_id", sa.String(64), nullable=False),
        sa.Column("source_asset_version_id", sa.String(64), nullable=False),
        sa.Column("batch_id", sa.String(64), nullable=False),
        sa.Column("batch_source_id", sa.String(64), nullable=False),
        sa.Column("item_id", sa.String(64), nullable=False),
        sa.Column("attempt_id", sa.String(64), nullable=False),
        sa.Column("output_id", sa.String(64), nullable=False),
        sa.Column("publication_id", sa.String(64), nullable=False),
        sa.Column("decision_id", sa.String(64), nullable=False),
        sa.Column("claim_decision_set_id", sa.String(64), nullable=False),
        sa.Column("product_version_id", sa.String(64), nullable=False),
        sa.Column("offer_version_id", sa.String(64), nullable=False),
        sa.Column("source_market", sa.String(10), nullable=False),
        sa.Column("target_market", sa.String(10), nullable=False),
        sa.Column("truth_package_id", sa.String(64), nullable=False),
        sa.Column("truth_package_market_id", sa.String(64), nullable=False),
        sa.Column("inventory_id", sa.String(64), nullable=False),
        sa.Column("copy_version_id", sa.String(64), nullable=False),
        sa.Column("market_policy_version_id", sa.String(64), nullable=False),
        sa.Column("source_generation_mode", sa.String(20), nullable=False),
        sa.Column("experiment_evidence_mode", sa.String(20), nullable=False),
        sa.Column("truth_data_mode", sa.String(20), nullable=False),
        sa.Column("provider_execution_mode", sa.String(20), nullable=False),
        sa.Column("evidence_tier", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_lal_workspace_id"),
        sa.UniqueConstraint("workspace_id", "asset_version_id",
                            name="uq_lal_workspace_id_asset_version"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "batch_id", "truth_package_id",
             "product_version_id", "offer_version_id", "truth_data_mode",
             "provider_execution_mode", "evidence_tier"],
            [
                "localization_batches.workspace_id",
                "localization_batches.id",
                "localization_batches.truth_package_id",
                "localization_batches.product_version_id",
                "localization_batches.offer_version_id",
                "localization_batches.truth_data_mode",
                "localization_batches.provider_execution_mode",
                "localization_batches.evidence_tier",
            ],
            name="fk_lal_batch",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "batch_source_id", "batch_id",
             "source_asset_version_id", "decision_id",
             "source_generation_mode", "experiment_evidence_mode"],
            [
                "localization_batch_sources.workspace_id",
                "localization_batch_sources.id",
                "localization_batch_sources.batch_id",
                "localization_batch_sources.asset_version_id",
                "localization_batch_sources.decision_id",
                "localization_batch_sources.source_generation_mode",
                "localization_batch_sources.experiment_evidence_mode",
            ],
            name="fk_lal_batch_source",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "item_id", "batch_id", "batch_source_id",
             "source_asset_version_id", "decision_id", "target_market",
             "market_policy_version_id", "truth_package_id",
             "truth_package_market_id", "inventory_id",
             "claim_decision_set_id", "copy_version_id"],
            [
                "localization_items.workspace_id",
                "localization_items.id",
                "localization_items.batch_id",
                "localization_items.batch_source_id",
                "localization_items.source_asset_version_id",
                "localization_items.decision_id",
                "localization_items.target_market",
                "localization_items.market_policy_version_id",
                "localization_items.truth_package_id",
                "localization_items.truth_package_market_id",
                "localization_items.inventory_id",
                "localization_items.claim_decision_set_id",
                "localization_items.copy_version_id",
            ],
            name="fk_lal_item",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "attempt_id", "item_id", "batch_id"],
            [
                "localization_attempts.workspace_id",
                "localization_attempts.id",
                "localization_attempts.item_id",
                "localization_attempts.batch_id",
            ],
            name="fk_lal_attempt",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "output_id", "attempt_id", "item_id", "batch_id"],
            [
                "localization_attempt_outputs.workspace_id",
                "localization_attempt_outputs.id",
                "localization_attempt_outputs.attempt_id",
                "localization_attempt_outputs.item_id",
                "localization_attempt_outputs.batch_id",
            ],
            name="fk_lal_output",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "publication_id", "asset_version_id", "output_id",
             "attempt_id", "item_id", "batch_id"],
            [
                "localization_output_publications.workspace_id",
                "localization_output_publications.id",
                "localization_output_publications.asset_version_id",
                "localization_output_publications.output_id",
                "localization_output_publications.attempt_id",
                "localization_output_publications.item_id",
                "localization_output_publications.batch_id",
            ],
            name="fk_lal_publication",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "truth_package_market_id", "truth_package_id",
             "target_market", "market_policy_version_id"],
            [
                "truth_package_markets.workspace_id",
                "truth_package_markets.id",
                "truth_package_markets.truth_package_id",
                "truth_package_markets.market",
                "truth_package_markets.market_policy_version_id",
            ],
            name="fk_lal_truth_market",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "claim_decision_set_id",
             "source_asset_version_id", "inventory_id", "truth_package_id",
             "truth_package_market_id", "target_market",
             "market_policy_version_id"],
            [
                "localization_claim_decision_sets.workspace_id",
                "localization_claim_decision_sets.id",
                "localization_claim_decision_sets.source_asset_version_id",
                "localization_claim_decision_sets.inventory_id",
                "localization_claim_decision_sets.truth_package_id",
                "localization_claim_decision_sets.truth_package_market_id",
                "localization_claim_decision_sets.target_market",
                "localization_claim_decision_sets.market_policy_version_id",
            ],
            name="fk_lal_claim_decision_set",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "copy_version_id", "source_asset_version_id",
             "inventory_id", "truth_package_id", "truth_package_market_id",
             "claim_decision_set_id", "target_market",
             "market_policy_version_id"],
            [
                "localized_copy_versions.workspace_id",
                "localized_copy_versions.id",
                "localized_copy_versions.source_asset_version_id",
                "localized_copy_versions.inventory_id",
                "localized_copy_versions.truth_package_id",
                "localized_copy_versions.truth_package_market_id",
                "localized_copy_versions.claim_decision_set_id",
                "localized_copy_versions.target_market",
                "localized_copy_versions.market_policy_version_id",
            ],
            name="fk_lal_copy_version",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "asset_version_id", "truth_package_id",
             "source_asset_version_id"],
            [
                "asset_versions.workspace_id",
                "asset_versions.id",
                "asset_versions.truth_package_id",
                "asset_versions.parent_version_id",
            ],
            name="fk_lal_localized_asset_version",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_asset_version_id"],
            ["asset_versions.workspace_id", "asset_versions.id"],
            name="fk_lal_source_asset_version",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "decision_id"],
            ["experiment_decisions.workspace_id", "experiment_decisions.id"],
            name="fk_lal_decision",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "truth_package_id", "truth_data_mode"],
            ["truth_packages.workspace_id", "truth_packages.id",
             "truth_packages.truth_data_mode"],
            name="fk_lal_truth_package",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "inventory_id", "source_asset_version_id"],
            [
                "source_claim_inventories.workspace_id",
                "source_claim_inventories.id",
                "source_claim_inventories.asset_version_id",
            ],
            name="fk_lal_inventory",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "product_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_lal_product_version",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "offer_version_id"],
            ["library_versions.workspace_id", "library_versions.id"],
            name="fk_lal_offer_version",
        ),
        sa.CheckConstraint(
            "source_generation_mode IN ('byok', 'sandbox', 'mock')",
            name="ck_lal_source_generation_mode",
        ),
        sa.CheckConstraint(
            "experiment_evidence_mode IN ('production', 'sandbox', 'mock')",
            name="ck_lal_experiment_evidence_mode",
        ),
        sa.CheckConstraint(
            "truth_data_mode IN ('real', 'mock')",
            name="ck_lal_truth_data_mode",
        ),
        sa.CheckConstraint(
            "provider_execution_mode IN ('byok', 'sandbox', 'mock')",
            name="ck_lal_provider_execution_mode",
        ),
        sa.CheckConstraint(
            "evidence_tier IN ('production', 'sandbox', 'mock')",
            name="ck_lal_evidence_tier",
        ),
    )


# =========================================================================
# Step 8: PostgreSQL triggers, RLS, grants, nine-market seed, verification
# =========================================================================

def _step8_pg_enforcement(conn):
    if not _is_pg(conn):
        return

    # --- Append-only triggers ---
    # Reuse the shared function from the previous migration
    for table_name in APPEND_ONLY_TABLES:
        _create_append_only_trigger(conn, table_name)

    # --- RLS on all workspace-scoped tables ---
    for table_name in NEW_WORKSPACE_OWNED:
        conn.execute(text(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY"))
        conn.execute(text(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY"))
        conn.execute(text(f"""
            CREATE POLICY tenant_isolation ON {table_name}
            USING (marketai_context_is_current(workspace_id))
            WITH CHECK (marketai_context_is_current(workspace_id))
        """))

    # --- Grants ---
    _apply_grants(conn)

    # --- Nine-market seed ---
    _seed_nine_markets(conn)


def _create_append_only_trigger(conn, table_name):
    trigger_name = f"trg_{table_name}_append_only"
    exists = conn.execute(text(
        "SELECT 1 FROM pg_trigger "
        "WHERE tgname = :name AND tgrelid = to_regclass(:table)"
    ), {"name": trigger_name, "table": table_name}).scalar()
    if exists:
        return
    conn.execute(text(f"""
        CREATE TRIGGER {trigger_name}
        BEFORE UPDATE OR DELETE ON {table_name}
        FOR EACH ROW EXECUTE FUNCTION marketai_reject_update_or_delete()
    """))


def _apply_grants(conn):
    # Immutable (append-only) workspace tables
    immutable_ws = (
        "truth_packages", "truth_package_markets", "truth_source_attempts",
        "truth_facts", "truth_derived_facts", "truth_derived_fact_inputs",
        "fact_library_dependencies", "fact_market_policy_dependencies",
        "localization_batch_sources", "localization_batch_commands",
        "localization_batch_market_targets",
        "source_claim_inventories", "source_claim_confirmations",
        "localization_claim_decision_sets", "localization_claim_decisions",
        "localized_copy_versions", "localized_copy_approvals",
        "localization_attempts", "localization_attempt_outputs",
        "localization_output_publications", "localization_qc_records",
        "localization_item_provider_calls",
        "market_asset_binding_events", "localized_asset_lineages",
    )
    for t in immutable_ws:
        conn.execute(text(f"GRANT SELECT, INSERT ON {t} TO marketai_domain_api"))
        conn.execute(text(f"GRANT SELECT, INSERT ON {t} TO marketai_domain_worker"))
        conn.execute(text(f"GRANT SELECT ON {t} TO marketai_projection_writer"))
        conn.execute(text(f"GRANT SELECT ON {t} TO marketai_domain_readonly"))

    # Mutable (CAS-updatable) workspace tables
    mutable_ws = (
        "localization_batches", "localization_items",
        "localization_semantic_claims",
        "localization_preparation_provider_calls",
        "market_asset_bindings",
    )
    for t in mutable_ws:
        conn.execute(text(f"GRANT SELECT, INSERT, UPDATE ON {t} TO marketai_domain_api"))
        conn.execute(text(f"GRANT SELECT, INSERT, UPDATE ON {t} TO marketai_domain_worker"))
        conn.execute(text(f"GRANT SELECT ON {t} TO marketai_projection_writer"))
        conn.execute(text(f"GRANT SELECT ON {t} TO marketai_domain_readonly"))

    # Platform tables (readable by all, writable by api/worker)
    for t in PLATFORM_TABLES:
        conn.execute(text(f"GRANT SELECT ON {t} TO marketai_domain_api"))
        conn.execute(text(f"GRANT SELECT ON {t} TO marketai_domain_worker"))
        conn.execute(text(f"GRANT SELECT ON {t} TO marketai_projection_writer"))
        conn.execute(text(f"GRANT SELECT ON {t} TO marketai_domain_readonly"))
    # Only api can seed/publish platform data
    conn.execute(text("GRANT INSERT ON market_policies TO marketai_domain_api"))
    conn.execute(text("GRANT INSERT ON market_policy_versions TO marketai_domain_api"))
    conn.execute(text("GRANT INSERT ON market_policy_version_events TO marketai_domain_api"))


def _seed_nine_markets(conn):
    """Idempotent seed of nine v9 Beta markets.

    Uses raw SQL to avoid importing ORM models in a migration.
    """
    import uuid
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    catalog = (
        ("en-GB", "en-GB", "GB", "GBP"),
        ("en-AU", "en-AU", "AU", "AUD"),
        ("en-CA", "en-CA", "CA", "CAD"),
        ("de-DE", "de-DE", "DE", "EUR"),
        ("fr-FR", "fr-FR", "FR", "EUR"),
        ("es-ES", "es-ES", "ES", "EUR"),
        ("it-IT", "it-IT", "IT", "EUR"),
        ("ja-JP", "ja-JP", "JP", "JPY"),
        ("ko-KR", "ko-KR", "KR", "KRW"),
    )

    def _digit_format(locale):
        if locale.startswith(("de", "fr", "es", "it")):
            return '{"decimal_separator": ",", "grouping_separator": ".", "currency_position": "suffix"}'
        if locale.startswith(("ja", "ko")):
            return '{"decimal_separator": ".", "grouping_separator": ",", "currency_position": "prefix"}'
        return '{"decimal_separator": ".", "grouping_separator": ",", "currency_position": "prefix"}'

    for market_id, locale, country, currency in catalog:
        # Check if already seeded
        exists = conn.execute(text(
            "SELECT 1 FROM market_policies WHERE market_id = :mid"
        ), {"mid": market_id}).scalar()
        if exists:
            continue

        policy_id = f"mp-{uuid.uuid4().hex[:16]}"
        version_id = f"mpv-{uuid.uuid4().hex[:16]}"
        event_id = f"mpve-{uuid.uuid4().hex[:16]}"
        requires_local = "false" if locale.startswith("en") else "true"
        df = _digit_format(locale)

        conn.execute(text(
            "INSERT INTO market_policies (id, market_id, locale, country, currency, created_at) "
            "VALUES (:id, :market_id, :locale, :country, :currency, :now)"
        ), {"id": policy_id, "market_id": market_id, "locale": locale,
            "country": country, "currency": currency, "now": now})

        conn.execute(text(
            "INSERT INTO market_policy_versions "
            "(id, market_policy_id, version_number, version_state, locale, country, currency, "
            "pricing_context_json, digit_format_json, base_tone, reserved_words_json, "
            "restricted_claim_types_json, requires_local_copy, shipping_address_granularity, "
            "max_fact_staleness_seconds, qc_rules_json, created_at) "
            "VALUES (:id, :policy_id, 1, 'effective', :locale, :country, :currency, "
            ":pcj, CAST(:dfj AS json), 'neutral', '[]', '[]', "
            f"{requires_local}, 'country', 86400, " + "'{}', :now)"
        ), {"id": version_id, "policy_id": policy_id, "locale": locale,
            "country": country, "currency": currency, "pcj": "{}",
            "dfj": df, "now": now})

        conn.execute(text(
            "INSERT INTO market_policy_version_events "
            "(id, market_policy_id, market_policy_version_id, sequence, event_type, "
            "previous_state, current_state, deduplication_key, actor_id, created_at) "
            "VALUES (:id, :policy_id, :version_id, 1, 'published', NULL, 'effective', "
            ":dedup, 'system', :now)"
        ), {"id": event_id, "policy_id": policy_id, "version_id": version_id,
            "dedup": f"mpve-{version_id}-published", "now": now})


# =========================================================================
# downgrade()
# =========================================================================

def downgrade() -> None:
    conn = op.get_bind()

    if _is_pg(conn):
        # Drop append-only triggers
        for table_name in APPEND_ONLY_TABLES:
            conn.execute(text(
                f"DROP TRIGGER IF EXISTS trg_{table_name}_append_only ON {table_name}"
            ))

        # Drop RLS policies
        for table_name in NEW_WORKSPACE_OWNED:
            conn.execute(text(
                f"DROP POLICY IF EXISTS tenant_isolation ON {table_name}"
            ))

        # Revoke grants
        for table_name in NEW_WORKSPACE_OWNED:
            conn.execute(text(f"REVOKE ALL ON {table_name} FROM marketai_domain_api"))
            conn.execute(text(f"REVOKE ALL ON {table_name} FROM marketai_domain_worker"))
            conn.execute(text(f"REVOKE ALL ON {table_name} FROM marketai_projection_writer"))
            conn.execute(text(f"REVOKE ALL ON {table_name} FROM marketai_domain_readonly"))
        for table_name in PLATFORM_TABLES:
            conn.execute(text(f"REVOKE ALL ON {table_name} FROM marketai_domain_api"))
            conn.execute(text(f"REVOKE ALL ON {table_name} FROM marketai_domain_worker"))
            conn.execute(text(f"REVOKE ALL ON {table_name} FROM marketai_projection_writer"))
            conn.execute(text(f"REVOKE ALL ON {table_name} FROM marketai_domain_readonly"))

    # Reverse existing table evolution (Step 3) BEFORE dropping new tables,
    # because SQLite batch mode reflects FK targets during table recreation.
    if _is_pg(conn):
        conn.execute(text(
            "ALTER TABLE provider_call_attempts "
            "DROP CONSTRAINT IF EXISTS truth_required"
        ))
        conn.execute(text(
            "ALTER TABLE provider_call_attempts "
            "DROP CONSTRAINT IF EXISTS chargeability_consistency"
        ))
        conn.execute(text(
            "ALTER TABLE provider_call_attempts "
            "DROP CONSTRAINT IF EXISTS chargeability"
        ))
        conn.execute(text(
            "ALTER TABLE provider_call_attempts "
            "DROP CONSTRAINT IF EXISTS ck_provider_call_attempt_step_kind"
        ))
        # Restore original step_kind CHECK
        conn.execute(text(
            "ALTER TABLE provider_call_attempts ADD CONSTRAINT "
            "ck_provider_call_attempt_step_kind CHECK ("
            "step_kind IN ('generation', 'visual_qc'))"
        ))

    # Drop FK, alt key, and columns via batch (works on both dialects)
    with op.batch_alter_table("provider_call_attempts", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_provider_call_attempts_workspace_truth_pkg_truth_packages",
            type_="foreignkey",
        )
        batch_op.drop_constraint("uq_provider_call_attempt_alt_key", type_="unique")
        batch_op.drop_column("truth_package_id")
        batch_op.drop_column("chargeability")
        batch_op.alter_column("spend_reservation_id", nullable=False)
        batch_op.alter_column("step_kind", type_=sa.String(20))

    if _is_pg(conn):
        conn.execute(text(
            "ALTER TABLE asset_versions "
            "DROP CONSTRAINT IF EXISTS ck_av_localized_requires_truth_parent"
        ))
        # C1 reverse: restore original origin_kind CHECK without 'localized'
        conn.execute(text(
            "ALTER TABLE asset_versions "
            "DROP CONSTRAINT IF EXISTS ck_asset_version_origin_kind"
        ))
        conn.execute(text(
            "ALTER TABLE asset_versions ADD CONSTRAINT "
            "ck_asset_version_origin_kind CHECK ("
            "origin_kind IN ('generation', 'upload', 'reuse', 'legacy'))"
        ))

    with op.batch_alter_table("asset_versions", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_asset_versions_workspace_truth_pkg_truth_packages",
            type_="foreignkey",
        )
        batch_op.drop_constraint("uq_av_workspace_truth_parent", type_="unique")

    # Drop tables in reverse dependency order
    for table in (
        # Step 7 (reverse)
        "localized_asset_lineages",
        "market_asset_binding_events",
        "market_asset_bindings",
        # Step 6 (reverse)
        "localization_preparation_provider_calls",
        "localization_item_provider_calls",
        "localization_semantic_claims",
        "localization_qc_records",
        "localization_output_publications",
        "localization_attempt_outputs",
        "localization_attempts",
        "localization_items",
        # Step 5 (reverse)
        "localized_copy_approvals",
        "localized_copy_versions",
        "localization_claim_decisions",
        "localization_claim_decision_sets",
        "source_claim_confirmations",
        "source_claim_inventories",
        # Step 4 (reverse)
        "localization_batch_market_targets",
        "localization_batch_commands",
        "localization_batch_sources",
        "localization_batches",
        # Step 2 (reverse)
        "fact_market_policy_dependencies",
        "fact_library_dependencies",
        "truth_derived_fact_inputs",
        "truth_derived_facts",
        "truth_facts",
        "truth_source_attempts",
        "truth_package_markets",
        "truth_packages",
        # Step 1 (reverse)
        "market_policy_version_events",
        "market_policy_versions",
        "market_policies",
    ):
        op.drop_table(table)
