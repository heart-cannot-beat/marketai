"""Row Level Security is the tenant boundary on the hosted runtime.

These tests connect as the real least-privilege roles rather than as the schema
owner. That distinction is the point: the owner has BYPASSRLS so migrations can
see every Workspace, so a test that ran as the owner would prove nothing about
what a request can reach.

Requires a PostgreSQL cluster provisioned with
``deploy/postgres/control-plane-domain-roles.sql`` and
``deploy/postgres/domain-tenant-roles.sql``, migrated to head. Skips when the
connection strings are absent so ``make verify`` still runs on a clean checkout;
CI supplies them.
"""

from __future__ import annotations

import os
import unittest
import uuid

from app.marketing import models as _marketing, workflow_models as _delivery, ai_qc_models as _qc  # noqa: F401

OWNER_URL = os.environ.get("DOMAIN_OWNER_DATABASE_URL")
API_URL = os.environ.get("DOMAIN_API_DATABASE_URL")
WORKER_URL = os.environ.get("DOMAIN_WORKER_DATABASE_URL")
READONLY_URL = os.environ.get("DOMAIN_READONLY_DATABASE_URL")
CONTEXT_SECRET = os.environ.get("MARKETAI_TENANT_CONTEXT_SECRET")

CONFIGURED = all((OWNER_URL, API_URL, WORKER_URL, READONLY_URL, CONTEXT_SECRET))

SKIP_REASON = (
    "set DOMAIN_OWNER_DATABASE_URL, DOMAIN_API_DATABASE_URL, "
    "DOMAIN_WORKER_DATABASE_URL and DOMAIN_READONLY_DATABASE_URL against a "
    "cluster provisioned with deploy/postgres/domain-tenant-roles.sql"
)

# Explicit aggregate inventory for the acceptance matrix. A new mapped table
# must be classified here and protected before a runtime role can use it.
CURRENT_AGGREGATE_TABLES = {
    "marketing_testing": {
        "marketing_projects", "marketing_revisions", "marketing_planning_calls",
        "marketing_generation_links", "marketing_rounds", "marketing_qc_reviews",
        "marketing_handoffs", "marketing_ad_bindings", "marketing_metric_batches",
        "marketing_evaluations", "marketing_ai_qc_calls", "marketing_creative_reuses",
        "marketing_feedback_drafts",
        "marketing_image_edits",
        "product_brand_kits",
    },
    "platform": {
        "workspaces",
        "audit_events",
        "import_batches",
        "jobs",
    },
    "product_library": {
        "products",
        "dimension_schema_versions",
        "dimensions",
        "dimension_values",
        "plans",
        "plan_entries",
    },
    "generation": {
        "generation_runs",
        "assets",
        "asset_versions",
        "quality_reviews",
        "verdict_decisions",
        "cost_ledger",
        "onboarding_fact_outbox",
    },
    "meta_observation": {
        "meta_accounts",
        "meta_sync_runs",
        "meta_sync_schedules",
        "meta_ingestions",
        "meta_ingestion_pages",
        "ad_daily_metrics",
        "meta_entity_status_events",
        "meta_ad_bindings",
    },
    "experiments": {
        "experiments",
        "experiment_entries",
        "experiment_ad_bindings",
        "experiment_state_events",
        "judgements",
        "experiment_decisions",
    },
    "launch": {
        "launch_previews",
        "launch_confirmations",
        "launch_operations",
        "launch_steps",
        "launch_audit_log",
    },
    "operations": {
        "operation_records",
        "operation_state_events",
        "idempotency_records",
    },
    "blob_intents": {
        "upload_intent_records",
        "download_intent_records",
    },
    "creative_library": {
        "library_entries",
        "library_drafts",
        "library_versions",
        "library_dependencies",
        "reference_objects",
        "reference_bindings",
        "library_events",
        "library_audit",
        "library_idempotency",
        "library_command_impacts",
        "library_entry_contexts",
        "library_stepup_assertions",
    },
    "entitlements": {
        "entitlement_snapshot_inbox",
        "entitlement_projections",
        "credential_versions",
        "credential_bindings",
        "spend_guardrails",
        "spend_guardrail_events",
        "spend_guardrail_previews",
        "spend_reservations",
        "capacity_reservations",
        "provider_call_attempts",
        "provider_cost_ledger",
        "usage_events",
        "customer_notification_outbox",
        "guardrail_notification_states",
    },
    "library_templates_datasources": {
        "dimension_concept_keys",
        "prompt_baselines",
        "datasource_credential_bindings",
        "resolution_previews",
    },
    "generation_assets_expand": {
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
    },
    "immutable_contextual_decisions": {
        "experiment_versions",
        "experiment_version_candidates",
        "experiment_version_candidate_concepts",
        "experiment_version_concept_resolutions",
        "decision_proposals",
        "decision_proposal_state_events",
        "decision_confirmations",
        "experiment_eligibility_events",
        "experiment_eligibility_projections",
        "contextual_verdict_projections",
        "experiment_timeline_heads",
        "experiment_timeline_events",
        "experiment_notification_outbox",
        "experiment_next_round_records",
        "experiment_pattern_candidates",
    },
    "localization_truth_bounded_qc": {
        # Platform-scoped (no RLS, but grants)
        "market_policies",
        "market_policy_versions",
        "market_policy_version_events",
        # Workspace-scoped (RLS + grants)
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
    },
    "meta_domain": {
        "meta_connection_records",
        "meta_data_source_binding_records",
        "scenario_version_records",
        "fake_meta_object_records",
        "meta_call_intent_records",
        "meta_call_outcome_records",
        "meta_call_reconciliation_records",
        "meta_call_intent_binding_records",
        "observation_contract_generation_records",
        "meta_sync_run_records",
        "meta_evidence_page_records",
        "meta_page_fetch_attempt_records",
        "evidence_publication_records",
        "evidence_attestation_records",
        "meta_status_history_records",
        "observation_coverage_ledger_records",
        "meta_sync_schedule_records",
        "scale_verdict_taint_records",
        "verdict_supersession_records",
        "launch_preview_records",
        "launch_candidate_records",
        "launch_confirmation_records",
        "launch_provisioning_records",
        "launch_provisioning_status_history_records",
        "launch_saga_step_attempt_records",
        "launch_parity_check_records",
        "launch_reconciliation_records",
    },
    "scale": {
        # Platform-global (no RLS, but grants)
        "scale_policy_versions",
        # Workspace-scoped (RLS + grants)
        "scale_delivery_targets",
        "scale_command_journal",
        "scale_lineage_guards",
        "scale_enrollment_candidates",
        "scale_delivery_evidence_snapshots",
        "scale_activation_previews",
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
        "scale_target_publication_previews",
        "scale_target_withdrawal_previews",
    },
    "product_intake": {
        "product_intakes",
    },
    "test_plans": {
        "test_plans",
        "test_plan_directions",
        "test_plan_runs",
        "generation_defaults",
    },
    "learnings": {
        "result_imports",
        "result_observations",
        "learning_snapshots",
    },
    "external_creatives": {
        "external_creatives",
        "external_creative_tag_confirmations",
    },
    "competitor_scan": {
        "competitors",
        "competitor_resolutions",
        "competitor_identity_confirmations",
        "competitor_scans",
        "competitor_ads",
        "competitor_ad_origins",
        "competitor_ad_assessments",
        "competitor_scan_assessments",
        "competitor_scan_snapshots",
        "competitor_discovery_decisions",
    },
}


_LIBRARY_TABLES = (
    "library_entries",
    "library_drafts",
    "library_versions",
    "library_dependencies",
    "reference_objects",
    "reference_bindings",
    "library_events",
    "library_audit",
    "library_idempotency",
)


def all_global_domain_tables() -> tuple[str, ...]:
    from migrations.versions.l1a2b3c4d5e6_library_templates_datasources import (
        GLOBAL_TABLES as TEMPLATES_DATASOURCES_GLOBAL_TABLES,
    )
    from migrations.versions.u1a2b3c4d5e6_localization_truth_bounded_qc import (
        PLATFORM_TABLES as LOCALIZATION_PLATFORM_TABLES,
    )
    from migrations.versions.f5eeeab4e9b9_scale_deterministic_evidence_and_advice import (
        PLATFORM_TABLES as SCALE_PLATFORM_TABLES,
    )

    return (
        TEMPLATES_DATASOURCES_GLOBAL_TABLES
        + LOCALIZATION_PLATFORM_TABLES
        + SCALE_PLATFORM_TABLES
    )


def all_protected_tables() -> tuple[str, ...]:
    from migrations.versions.zv7c8d9e0f1a_marketing_image_edits import NEW_WORKSPACE_OWNED as IMAGE_EDITS
    from migrations.versions.zw8d9e0f1a2b_product_brand_kits import NEW_WORKSPACE_OWNED as BRAND_KITS
    from migrations.versions.zn9a0b1c2d3e_marketing_planning import NEW_WORKSPACE_OWNED as MARKETING_0
    from migrations.versions.zo0b1c2d3e4f_marketing_delivery import NEW_WORKSPACE_OWNED as MARKETING_1
    from migrations.versions.zp1c2d3e4f5a_marketing_ai_qc import NEW_WORKSPACE_OWNED as MARKETING_2
    from migrations.versions.zq2d3e4f5a6b_marketing_creative_reuse import NEW_WORKSPACE_OWNED as MARKETING_3
    from migrations.versions.zr3e4f5a6b_marketing_feedback_explanation import NEW_WORKSPACE_OWNED as MARKETING_4
    from migrations.versions.d8e9f0a1b2c3_append_only_audit_events import (
        NEW_WORKSPACE_OWNED,
    )
    from migrations.versions.f1a2c3d4e5b6_row_level_security import ALL_PROTECTED
    from migrations.versions.h1a2b3c4d5e6_operation_tables import (
        NEW_WORKSPACE_OWNED as OPERATION_TABLES,
    )
    from migrations.versions.i1a2b3c4d5e6_idempotency_records import (
        NEW_WORKSPACE_OWNED as IDEMPOTENCY_TABLES,
    )
    from migrations.versions.j1a2b3c4d5e6_intent_tables import (
        NEW_WORKSPACE_OWNED as INTENT_TABLES,
    )
    from migrations.versions.k1a2b3c4d5e6_entitlement_commercial_access import (
        NEW_TABLES as ENTITLEMENT_TABLES,
    )
    from migrations.versions.l1a2b3c4d5e6_library_templates_datasources import (
        NEW_WORKSPACE_OWNED as TEMPLATES_DATASOURCES_TABLES,
    )
    from migrations.versions.n1_generation_assets_expand import (
        NEW_WORKSPACE_OWNED as GENERATION_ASSETS_EXPAND_TABLES,
    )
    from migrations.versions.t1a2b3c4d5e6_immutable_contextual_decisions import (
        NEW_WORKSPACE_OWNED as IMMUTABLE_DECISIONS_TABLES,
    )
    from migrations.versions.r1a2b3c4d5e6_library_ui_gate0_contracts import (
        NEW_WORKSPACE_OWNED as LIBRARY_UI_GATE0_TABLES,
    )
    from migrations.versions.u1a2b3c4d5e6_localization_truth_bounded_qc import (
        NEW_WORKSPACE_OWNED as LOCALIZATION_TABLES,
    )
    from migrations.versions.m23b_meta_domain_tables import (
        NEW_WORKSPACE_OWNED as META_DOMAIN_TABLES,
    )
    from migrations.versions.f5eeeab4e9b9_scale_deterministic_evidence_and_advice import (
        NEW_WORKSPACE_OWNED as SCALE_TABLES,
    )
    from migrations.versions.a79b1c2d3e4f_multi_candidate_launch_confirmation import (
        NEW_WORKSPACE_OWNED as LAUNCH_CONFIRMATION_TABLES,
    )
    from migrations.versions.y1a2b3c4d5e6_onboarding_fact_outbox import (
        NEW_WORKSPACE_OWNED as ONBOARDING_FACT_TABLES,
    )
    from migrations.versions.ab1_scale_lineage_tables import (
        NEW_WORKSPACE_OWNED as SCALE_LINEAGE_TABLES,
    )
    from migrations.versions.ad2_scale_target_preview_and_command_identity import (
        NEW_WORKSPACE_OWNED as SCALE_TARGET_PREVIEW_TABLES,
    )
    from migrations.versions.zi4d5e6f7a8b_product_intakes import (
        NEW_WORKSPACE_OWNED as PRODUCT_INTAKE_TABLES,
    )
    from migrations.versions.zj5e6f7a8b9c_test_plans import (
        NEW_WORKSPACE_OWNED as TEST_PLAN_TABLES,
    )
    from migrations.versions.zk6f7a8b9c0d_results_learnings import (
        NEW_WORKSPACE_OWNED as LEARNINGS_TABLES,
    )
    from migrations.versions.zm8b9c0d1e2f_external_creatives import (
        NEW_WORKSPACE_OWNED as EXTERNAL_CREATIVE_TABLES,
    )
    from migrations.versions.zz1a2b3c4d5e_competitor_list_and_resolve import (
        NEW_WORKSPACE_OWNED as COMPETITOR_TABLES,
    )
    from migrations.versions.zz2b3c4d5e6f_competitor_scans import (
        NEW_WORKSPACE_OWNED as COMPETITOR_SCAN_TABLES,
    )
    from migrations.versions.zz3c4d5e6f7a_competitor_ad_assessments import (
        NEW_WORKSPACE_OWNED as COMPETITOR_ASSESSMENT_TABLES,
    )
    from migrations.versions.zz4d5e6f7a8b_competitor_clusters_and_snapshots import (
        NEW_WORKSPACE_OWNED as COMPETITOR_SNAPSHOT_TABLES,
    )
    from migrations.versions.zz5e6f7a8b9c_competitor_discovery_decisions import (
        NEW_WORKSPACE_OWNED as COMPETITOR_DISCOVERY_TABLES,
    )
    from migrations.versions.zz8b9c0d1e2f_spend_guardrail_previews import (
        NEW_WORKSPACE_OWNED as SPEND_GUARDRAIL_PREVIEW_TABLES,
    )

    return (
        ALL_PROTECTED
        + MARKETING_0
        + MARKETING_1
        + MARKETING_2
        + MARKETING_3
        + MARKETING_4
        + IMAGE_EDITS
        + BRAND_KITS
        + NEW_WORKSPACE_OWNED
        + OPERATION_TABLES
        + IDEMPOTENCY_TABLES
        + INTENT_TABLES
        + _LIBRARY_TABLES
        + ENTITLEMENT_TABLES
        + TEMPLATES_DATASOURCES_TABLES
        + GENERATION_ASSETS_EXPAND_TABLES
        + IMMUTABLE_DECISIONS_TABLES
        + LIBRARY_UI_GATE0_TABLES
        + LOCALIZATION_TABLES
        + META_DOMAIN_TABLES
        + SCALE_TABLES
        + LAUNCH_CONFIRMATION_TABLES
        + ONBOARDING_FACT_TABLES
        + SCALE_LINEAGE_TABLES
        + SCALE_TARGET_PREVIEW_TABLES
        + PRODUCT_INTAKE_TABLES
        + TEST_PLAN_TABLES
        + LEARNINGS_TABLES
        + EXTERNAL_CREATIVE_TABLES
        + COMPETITOR_TABLES
        + COMPETITOR_SCAN_TABLES
        + COMPETITOR_ASSESSMENT_TABLES
        + COMPETITOR_SNAPSHOT_TABLES
        + COMPETITOR_DISCOVERY_TABLES
        + SPEND_GUARDRAIL_PREVIEW_TABLES
    )

INSERT_PRODUCT = """
INSERT INTO products (id, workspace_id, name, description, selling_points,
                      audience, brand_notes, ref_images, economics, revision,
                      created_at, updated_at)
VALUES (:id, :workspace_id, :name, '', '[]', '', '', '[]', '{}', 1, now(), now())
"""

INSERT_WORKSPACE = """
INSERT INTO workspaces (id, name, default_currency, lifecycle, budget_currency, created_at)
VALUES (:id, :id, 'USD', 'active', 'USD', now())
ON CONFLICT (id) DO NOTHING
"""

INSERT_LIBRARY_ENTRY = """
INSERT INTO library_entries (id, workspace_id, entry_type, display_name,
                             lifecycle, created_at, updated_at)
VALUES (:id, :workspace_id, 'product', :name, 'active', now(), now())
"""


@unittest.skipUnless(CONFIGURED, SKIP_REASON)
class RowLevelSecurityTests(unittest.TestCase):
    """Isolation the application cannot forget, because it is not the enforcer."""

    @classmethod
    def setUpClass(cls) -> None:
        from sqlalchemy import create_engine, text

        cls.create_engine = staticmethod(create_engine)
        cls.text = staticmethod(text)

        cls.alpha = f"ws_alpha_{uuid.uuid4().hex[:8]}"
        cls.beta = f"ws_beta_{uuid.uuid4().hex[:8]}"
        cls.alpha_product = f"p_{uuid.uuid4().hex[:8]}"
        cls.beta_product = f"p_{uuid.uuid4().hex[:8]}"
        cls.alpha_audit = f"audit_{uuid.uuid4().hex}"
        cls.beta_audit = f"audit_{uuid.uuid4().hex}"
        cls.alpha_lib_entry = f"lib_{uuid.uuid4().hex[:8]}"
        cls.beta_lib_entry = f"lib_{uuid.uuid4().hex[:8]}"
        cls.alpha_credential = f"cv_{uuid.uuid4().hex[:8]}"
        cls.beta_credential = f"cv_{uuid.uuid4().hex[:8]}"
        cls.alpha_binding = f"cb_{uuid.uuid4().hex[:8]}"
        cls.beta_binding = f"cb_{uuid.uuid4().hex[:8]}"
        cls.alpha_draft = f"gd_{uuid.uuid4().hex[:8]}"
        cls.beta_draft = f"gd_{uuid.uuid4().hex[:8]}"

        cls.owner = create_engine(OWNER_URL, future=True)

        with cls.owner.begin() as conn:
            for workspace in (cls.alpha, cls.beta):
                conn.execute(text(INSERT_WORKSPACE), {"id": workspace})

            conn.execute(
                text(INSERT_PRODUCT),
                {
                    "id": cls.alpha_product,
                    "workspace_id": cls.alpha,
                    "name": "Alpha",
                },
            )
            conn.execute(
                text(INSERT_PRODUCT),
                {"id": cls.beta_product, "workspace_id": cls.beta, "name": "Beta"},
            )
            conn.execute(
                text(INSERT_LIBRARY_ENTRY),
                {"id": cls.alpha_lib_entry, "workspace_id": cls.alpha, "name": "Alpha Lib"},
            )
            conn.execute(
                text(INSERT_LIBRARY_ENTRY),
                {"id": cls.beta_lib_entry, "workspace_id": cls.beta, "name": "Beta Lib"},
            )
            for workspace, credential, binding in (
                (cls.alpha, cls.alpha_credential, cls.alpha_binding),
                (cls.beta, cls.beta_credential, cls.beta_binding),
            ):
                conn.execute(
                    text(
                        "INSERT INTO credential_versions "
                        "(id, workspace_id, credential_key, version_no, provider, state, "
                        "encrypted_secret, encrypted_data_key, encryption_key_id, "
                        "encryption_algorithm, nonce, aad_schema_version, created_by, created_at) "
                        "VALUES (:id, :workspace, 'META_API_KEY', 1, 'meta', 'active', "
                        "decode('00', 'hex'), decode('01', 'hex'), 'test-key', "
                        "'aes-256-gcm', decode('02', 'hex'), 1, 'rls-test', now())"
                    ),
                    {"id": credential, "workspace": workspace},
                )
                conn.execute(
                    text(
                        "INSERT INTO credential_bindings "
                        "(id, workspace_id, binding_scope, provider, credential_version_id, "
                        "revision, bound_by, created_at, updated_at) VALUES "
                        "(:id, :workspace, 'generation', 'meta', :credential, 1, "
                        "'rls-test', now(), now())"
                    ),
                    {"id": binding, "workspace": workspace, "credential": credential},
                )
            for workspace, draft, product in (
                (cls.alpha, cls.alpha_draft, cls.alpha_product),
                (cls.beta, cls.beta_draft, cls.beta_product),
            ):
                conn.execute(
                    text(
                        "INSERT INTO generation_drafts "
                        "(id, workspace_id, product_id, selectors_json, provider, model, "
                        "execution_mode, combination_strategy_json, format_json, "
                        "exclusions_json, revision, state, created_by, created_at, updated_at) "
                        "VALUES (:id, :workspace, :product, '{}', 'mock', 'mock-v1', "
                        "'mock', '{}', '{}', '{}', 1, 'editing', 'rls-test', now(), now())"
                    ),
                    {"id": draft, "workspace": workspace, "product": product},
                )
            for audit_id, workspace in (
                (cls.alpha_audit, cls.alpha),
                (cls.beta_audit, cls.beta),
            ):
                conn.execute(
                    text(
                        "INSERT INTO audit_events (id, workspace_id, initiator_id, "
                        "initiator_role, executor, request_id, trace_id, "
                        "authorization_epoch, event_type, resource_type, resource_id, "
                        "details_json, created_at) VALUES (:id, :workspace, NULL, "
                        "'system', 'system:migration', '', '', 0, 'rls.probe', "
                        "'probe', :id, '{}', now())"
                    ),
                    {"id": audit_id, "workspace": workspace},
                )

    @classmethod
    def tearDownClass(cls) -> None:
        from sqlalchemy import text

        with cls.owner.begin() as conn:
            conn.execute(text("ALTER TABLE audit_events DISABLE TRIGGER audit_events_reject_mutation"))
            conn.execute(
                text("DELETE FROM audit_events WHERE workspace_id IN (:a, :b)"),
                {"a": cls.alpha, "b": cls.beta},
            )
            conn.execute(text("ALTER TABLE audit_events ENABLE TRIGGER audit_events_reject_mutation"))
            conn.execute(
                text("DELETE FROM library_entries WHERE workspace_id IN (:a, :b)"),
                {"a": cls.alpha, "b": cls.beta},
            )
            conn.execute(
                text("DELETE FROM generation_drafts WHERE workspace_id IN (:a, :b)"),
                {"a": cls.alpha, "b": cls.beta},
            )
            conn.execute(
                text("DELETE FROM credential_bindings WHERE workspace_id IN (:a, :b)"),
                {"a": cls.alpha, "b": cls.beta},
            )
            conn.execute(
                text("DELETE FROM credential_versions WHERE workspace_id IN (:a, :b)"),
                {"a": cls.alpha, "b": cls.beta},
            )
            conn.execute(
                text("DELETE FROM generation_runs WHERE workspace_id IN (:a, :b)"),
                {"a": cls.alpha, "b": cls.beta},
            )
            conn.execute(
                text("DELETE FROM products WHERE workspace_id IN (:a, :b)"),
                {"a": cls.alpha, "b": cls.beta},
            )
            conn.execute(
                text("DELETE FROM workspaces WHERE id IN (:a, :b)"),
                {"a": cls.alpha, "b": cls.beta},
            )

        cls.owner.dispose()

    def engine(self, url: str, **kwargs):
        engine = self.create_engine(url, future=True, **kwargs)
        self.addCleanup(engine.dispose)
        return engine

    def scoped_connection(self, engine, workspace: str | None):
        """A connection inside a transaction carrying the tenant setting."""
        conn = engine.connect()
        self.addCleanup(conn.close)
        transaction = conn.begin()
        self.addCleanup(lambda: transaction.rollback() if transaction.is_active else None)

        if workspace is not None:
            self.set_scope(conn, workspace, 0)

        return conn

    def set_scope(
        self,
        connection,
        workspace: str,
        epoch: int,
        meta_mode: str | None = None,
    ) -> None:
        from app.platform.tenancy import TenantContext, tenant_context_signature

        context = TenantContext(
            workspace_id=workspace,
            role="owner",
            initiator_id="rls-test",
            authorization_epoch=epoch,
            meta_mode=meta_mode,
        )
        connection.execute(
            self.text("SELECT set_config('marketai.workspace_id', :w, true)"),
            {"w": workspace},
        )
        connection.execute(
            self.text(
                "SELECT set_config('marketai.authorization_epoch', :epoch, true)"
            ),
            {"epoch": str(epoch)},
        )
        connection.execute(
            self.text("SELECT set_config('marketai.meta_mode', :mode, true)"),
            {"mode": meta_mode or ""},
        )
        connection.execute(
            self.text(
                "SELECT set_config('marketai.context_signature', :signature, true)"
            ),
            {"signature": tenant_context_signature(context, CONTEXT_SECRET)},
        )

    # ------------------------------------------------------------ reads ----

    def test_each_role_reads_only_the_scoped_workspace(self):
        for url in (API_URL, WORKER_URL, READONLY_URL):
            with self.subTest(role=url.split("://", 1)[1].split(":", 1)[0]):
                conn = self.scoped_connection(self.engine(url), self.alpha)
                rows = [
                    r[0]
                    for r in conn.execute(self.text("SELECT id FROM products"))
                ]

                self.assertEqual(rows, [self.alpha_product])

    def test_an_unscoped_connection_sees_nothing(self):
        # `current_setting(..., true)` yields NULL when unset, and
        # `workspace_id = NULL` matches no row: absence of scope fails closed.
        conn = self.scoped_connection(self.engine(API_URL), None)

        self.assertEqual(
            conn.execute(self.text("SELECT count(*) FROM products")).scalar(), 0
        )

    def test_an_unknown_workspace_sees_nothing(self):
        """A Workspace that was never issued matches no row.

        This is the weaker of the two claims worth stating plainly. It proves
        the policy compares rather than defaults; it does not prove that a
        *real* other Workspace cannot be selected -- see
        ``test_the_scope_setting_is_trusted_input`` below.
        """
        conn = self.scoped_connection(self.engine(API_URL), "ws_does_not_exist")

        self.assertEqual(
            conn.execute(self.text("SELECT count(*) FROM products")).scalar(), 0
        )

    def test_a_runtime_role_cannot_forge_another_real_workspace(self):
        conn = self.scoped_connection(self.engine(API_URL), self.alpha)
        # The role may write custom GUCs, but changing the Workspace without a
        # matching owner-held HMAC makes the whole policy false.
        conn.execute(
            self.text("SELECT set_config('marketai.workspace_id', :w, true)"),
            {"w": self.beta},
        )

        rows = [r[0] for r in conn.execute(self.text("SELECT id FROM products"))]

        self.assertEqual(rows, [])

    def test_a_stale_authorization_epoch_sees_nothing(self):
        conn = self.scoped_connection(self.engine(API_URL), self.alpha)
        self.set_scope(conn, self.alpha, 99)

        self.assertEqual(
            conn.execute(self.text("SELECT count(*) FROM products")).scalar(), 0
        )

    def test_a_meta_mode_bound_scope_reads_workspace_owned_rows(self):
        """Launch binds ``meta_mode`` and still needs the frozen Experiment.

        ``tenant_context_signature`` covers the mode whenever one is bound, so
        the Workspace-owned policy must hash the same settings the API wrote
        or every meta-bound request loses sight of ``experiments``.
        """
        conn = self.scoped_connection(self.engine(API_URL), None)
        self.set_scope(conn, self.alpha, 0, meta_mode="customer_sandbox")

        rows = [r[0] for r in conn.execute(self.text("SELECT id FROM products"))]

        self.assertEqual(rows, [self.alpha_product])

    def test_a_meta_bound_signature_is_not_a_plain_signature(self):
        conn = self.scoped_connection(self.engine(API_URL), None)
        self.set_scope(conn, self.alpha, 0, meta_mode="customer_sandbox")
        # Dropping the mode setting must not promote the signature to the
        # unbound form it was never minted for.
        conn.execute(
            self.text("SELECT set_config('marketai.meta_mode', '', true)")
        )

        self.assertEqual(
            conn.execute(self.text("SELECT count(*) FROM products")).scalar(), 0
        )

    def test_a_plain_signature_cannot_claim_a_meta_mode(self):
        conn = self.scoped_connection(self.engine(API_URL), self.alpha)
        conn.execute(
            self.text("SELECT set_config('marketai.meta_mode', :mode, true)"),
            {"mode": "customer_sandbox"},
        )

        self.assertEqual(
            conn.execute(self.text("SELECT count(*) FROM products")).scalar(), 0
        )

    def test_identifier_guessing_does_not_reveal_existence(self):
        # The other Workspace's real primary key must be indistinguishable from
        # one that was never issued.
        conn = self.scoped_connection(self.engine(API_URL), self.alpha)

        known_other = conn.execute(
            self.text("SELECT count(*) FROM products WHERE id = :id"),
            {"id": self.beta_product},
        ).scalar()
        never_issued = conn.execute(
            self.text("SELECT count(*) FROM products WHERE id = :id"),
            {"id": "p_never_issued"},
        ).scalar()

        self.assertEqual(known_other, 0)
        self.assertEqual(known_other, never_issued)

    def test_library_entries_are_tenant_scoped(self):
        for url in (API_URL, WORKER_URL, READONLY_URL):
            with self.subTest(role=url.split("://", 1)[1].split(":", 1)[0]):
                conn = self.scoped_connection(self.engine(url), self.alpha)
                rows = [
                    r[0]
                    for r in conn.execute(self.text("SELECT id FROM library_entries"))
                ]
                self.assertEqual(rows, [self.alpha_lib_entry])

    def test_capability_credential_metadata_is_tenant_scoped(self):
        conn = self.scoped_connection(self.engine(API_URL), self.alpha)
        rows = conn.execute(
            self.text(
                "SELECT b.provider, b.credential_version_id, v.state "
                "FROM credential_bindings b "
                "JOIN credential_versions v "
                "ON b.credential_version_id = v.id "
                "AND b.workspace_id = v.workspace_id "
                "WHERE b.workspace_id = :workspace "
                "AND b.binding_scope = 'generation'"
            ),
            {"workspace": self.alpha},
        ).all()

        self.assertEqual(rows, [("meta", self.alpha_credential, "active")])

    def test_capability_grant_does_not_expose_credential_ciphertext(self):
        from sqlalchemy.exc import ProgrammingError

        for column in ("encrypted_secret", "encrypted_data_key", "nonce"):
            with self.subTest(column=column):
                conn = self.scoped_connection(self.engine(API_URL), self.alpha)
                with self.assertRaises(ProgrammingError):
                    conn.execute(self.text(f"SELECT {column} FROM credential_versions"))

    def test_the_api_role_serves_the_credential_lifecycle_from_metadata_columns(self):
        """``CredentialAdmin`` (create / rotate / revoke / bind / list) and the
        BYOK admission checks read every non-secret column of
        ``credential_versions`` and all of ``credential_bindings`` under RLS;
        the envelope itself stays Worker-only (previous test)."""
        conn = self.scoped_connection(self.engine(API_URL), self.alpha)
        rows = conn.execute(
            self.text(
                "SELECT id, credential_key, version_no, provider, display_label, "
                "state, created_by, created_at, rotated_at, revoked_at "
                "FROM credential_versions"
            )
        ).all()
        self.assertEqual([(r.id, r.state) for r in rows], [(self.alpha_credential, "active")])
        bindings = conn.execute(
            self.text("SELECT id, provider, revision, bound_by FROM credential_bindings")
        ).all()
        self.assertEqual([b.provider for b in bindings], ["meta"])

    def test_generation_drafts_are_tenant_scoped_and_api_writable(self):
        conn = self.scoped_connection(self.engine(API_URL), self.alpha)

        self.assertEqual(
            conn.execute(self.text("SELECT id FROM generation_drafts")).scalars().all(),
            [self.alpha_draft],
        )
        updated = conn.execute(
            self.text(
                "UPDATE generation_drafts SET model = 'mock-v2' "
                "WHERE id = :id"
            ),
            {"id": self.alpha_draft},
        )
        self.assertEqual(updated.rowcount, 1)

        inserted = conn.execute(
            self.text(
                "INSERT INTO generation_drafts "
                "(id, workspace_id, product_id, selectors_json, provider, model, "
                "execution_mode, combination_strategy_json, format_json, "
                "exclusions_json, revision, state, created_by, created_at, updated_at) "
                "VALUES (:id, :workspace, :product, '{}', 'mock', 'mock-v1', "
                "'mock', '{}', '{}', '{}', 1, 'editing', 'rls-test', now(), now())"
            ),
            {
                "id": f"gd_{uuid.uuid4().hex[:8]}",
                "workspace": self.alpha,
                "product": self.alpha_product,
            },
        )
        self.assertEqual(inserted.rowcount, 1)

    def test_library_cross_workspace_write_rejected(self):
        from sqlalchemy.exc import ProgrammingError

        conn = self.scoped_connection(self.engine(API_URL), self.alpha)
        with self.assertRaises(ProgrammingError):
            conn.execute(
                self.text(INSERT_LIBRARY_ENTRY),
                {
                    "id": f"lib_{uuid.uuid4().hex[:8]}",
                    "workspace_id": self.beta,
                    "name": "smuggled",
                },
            )

    def test_library_unscoped_sees_nothing(self):
        conn = self.scoped_connection(self.engine(API_URL), None)
        self.assertEqual(
            conn.execute(self.text("SELECT count(*) FROM library_entries")).scalar(), 0
        )

    def test_audit_events_are_tenant_scoped(self):
        conn = self.scoped_connection(self.engine(READONLY_URL), self.alpha)

        rows = conn.execute(
            self.text("SELECT id FROM audit_events WHERE event_type = 'rls.probe'")
        ).scalars().all()

        self.assertEqual(rows, [self.alpha_audit])

    def test_the_api_role_reads_the_audit_trail_it_serves_under_rls(self):
        """``GET /api/v1/audit`` runs over the API role (spec §8.8).

        Without SELECT the endpoint raised ``ProgrammingError`` on every
        production call; with it the rows are still tenant-scoped, and the
        role gains no way to rewrite the append-only trail.
        """
        conn = self.scoped_connection(self.engine(API_URL), self.alpha)

        rows = conn.execute(
            self.text("SELECT id FROM audit_events WHERE event_type = 'rls.probe'")
        ).scalars().all()
        self.assertEqual(rows, [self.alpha_audit])

        with self.engine(OWNER_URL).connect() as owner:
            for privilege, expected in (("SELECT", True), ("INSERT", True),
                                        ("UPDATE", False), ("DELETE", False)):
                with self.subTest(privilege=privilege):
                    self.assertEqual(
                        bool(owner.execute(
                            self.text(
                                "SELECT has_table_privilege('marketai_domain_api', "
                                "'public.audit_events', :privilege)"
                            ),
                            {"privilege": privilege},
                        ).scalar()),
                        expected,
                    )

    def test_the_workspace_registry_shows_only_the_current_workspace(self):
        conn = self.scoped_connection(self.engine(API_URL), self.alpha)
        rows = [r[0] for r in conn.execute(self.text("SELECT id FROM workspaces"))]

        self.assertEqual(rows, [self.alpha])

    # ----------------------------------------------------------- writes ----

    def test_a_row_cannot_be_written_into_another_workspace(self):
        # WITH CHECK, not just USING: without it the insert would succeed and
        # simply be invisible, which is a silent write into another tenant.
        from sqlalchemy.exc import ProgrammingError

        conn = self.scoped_connection(self.engine(API_URL), self.alpha)

        with self.assertRaises(ProgrammingError):
            conn.execute(
                self.text(INSERT_PRODUCT),
                {
                    "id": f"p_{uuid.uuid4().hex[:8]}",
                    "workspace_id": self.beta,
                    "name": "smuggled",
                },
            )

    def test_an_update_cannot_reach_another_workspace(self):
        conn = self.scoped_connection(self.engine(API_URL), self.alpha)

        result = conn.execute(
            self.text("UPDATE products SET name = 'hijacked' WHERE id = :id"),
            {"id": self.beta_product},
        )

        self.assertEqual(result.rowcount, 0)

    def test_a_delete_cannot_reach_another_workspace(self):
        from sqlalchemy.exc import ProgrammingError

        conn = self.scoped_connection(self.engine(API_URL), self.alpha)

        with self.assertRaises(ProgrammingError):
            conn.execute(
                self.text("DELETE FROM products WHERE id = :id"),
                {"id": self.beta_product},
            )

    # ------------------------------------------------------ child tables ----

    def test_a_child_row_is_visible_only_through_its_parent(self):
        # dimension_values -> dimensions -> dimension_schema_versions, where
        # only the last carries workspace_id. Neither child policy mentions the
        # Workspace; the chain composes because RLS applies inside a policy too.
        schema_id = f"dsv_{uuid.uuid4().hex[:8]}"
        dimension_id = f"dim_{uuid.uuid4().hex[:8]}"
        value_id = f"dv_{uuid.uuid4().hex[:8]}"

        with self.owner.begin() as conn:
            conn.execute(
                self.text(
                    "INSERT INTO dimension_schema_versions (id, workspace_id,"
                    " product_id, version, style_notes, source, snapshot_hash,"
                    " created_at) VALUES (:id, :ws, :p, 1, '', 'test', 'h', now())"
                ),
                {"id": schema_id, "ws": self.beta, "p": self.beta_product},
            )
            conn.execute(
                self.text(
                    "INSERT INTO dimensions (id, schema_id, stable_dimension_id,"
                    " label, position) VALUES (:id, :s, 'sd', 'label', 1)"
                ),
                {"id": dimension_id, "s": schema_id},
            )
            conn.execute(
                self.text(
                    "INSERT INTO dimension_values (id, dimension_id,"
                    " stable_value_id, label, prompt_fragment, position)"
                    " VALUES (:id, :d, 'sv', 'label', '', 1)"
                ),
                {"id": value_id, "d": dimension_id},
            )

        self.addCleanup(self._delete_dimension_chain, schema_id, dimension_id, value_id)

        alpha = self.scoped_connection(self.engine(API_URL), self.alpha)
        self.assertEqual(
            alpha.execute(
                self.text("SELECT count(*) FROM dimension_values WHERE id = :id"),
                {"id": value_id},
            ).scalar(),
            0,
        )
        self.assertEqual(
            alpha.execute(
                self.text("SELECT count(*) FROM dimensions WHERE id = :id"),
                {"id": dimension_id},
            ).scalar(),
            0,
        )

        beta = self.scoped_connection(self.engine(API_URL), self.beta)
        self.assertEqual(
            beta.execute(
                self.text("SELECT count(*) FROM dimension_values WHERE id = :id"),
                {"id": value_id},
            ).scalar(),
            1,
        )

    def _delete_dimension_chain(self, schema_id, dimension_id, value_id) -> None:
        with self.owner.begin() as conn:
            conn.execute(
                self.text("DELETE FROM dimension_values WHERE id = :id"),
                {"id": value_id},
            )
            conn.execute(
                self.text("DELETE FROM dimensions WHERE id = :id"),
                {"id": dimension_id},
            )
            conn.execute(
                self.text("DELETE FROM dimension_schema_versions WHERE id = :id"),
                {"id": schema_id},
            )

    def test_assets_cannot_reference_a_product_from_another_workspace(self):
        from sqlalchemy.exc import IntegrityError

        plan_id = f"plan_{uuid.uuid4().hex[:8]}"
        run_id = f"run_{uuid.uuid4().hex[:8]}"
        with self.owner.begin() as conn:
            conn.execute(
                self.text(
                    "INSERT INTO plans (id, workspace_id, product_id, mode, cap,"
                    " total_full, snapshot_hash, created_at) VALUES"
                    " (:id, :ws, :product, 'full', 0, 1, :hash, now())"
                ),
                {"id": plan_id, "ws": self.beta, "product": self.beta_product, "hash": "a" * 64},
            )
            conn.execute(
                self.text(
                    "INSERT INTO generation_runs (id, workspace_id, product_id, plan_id,"
                    " provider, model, aspect_ratio, image_size, variants, use_ref,"
                    " status, total, done, failed, est_cost, currency, revision,"
                    " created_at, updated_at) VALUES (:id, :ws, :product, :plan,"
                    " 'mock', 'mock', '1:1', '1K', 1, false, 'queued', 1, 0, 0,"
                    " 0, 'USD', 1, now(), now())"
                ),
                {"id": run_id, "ws": self.beta, "product": self.beta_product, "plan": plan_id},
            )
            with self.assertRaises(IntegrityError), conn.begin_nested():
                conn.execute(
                    self.text(
                        "INSERT INTO assets (id, workspace_id, product_id, run_id,"
                        " combo_id, combo_code, tags_json, filename, provider, model,"
                        " prompt, fingerprint, status, error_code, error_detail,"
                        " created_at, updated_at) VALUES (:id, :ws, :product, :run,"
                        " 'C01', 'A1', '{}', 'x.png', 'mock', 'mock', '', :fingerprint,"
                        " 'pending', '', '', now(), now())"
                    ),
                    {
                        "id": f"asset_{uuid.uuid4().hex[:8]}",
                        "ws": self.alpha,
                        "product": self.beta_product,
                        "run": run_id,
                        "fingerprint": "b" * 64,
                    },
                )

    def test_quality_review_cannot_reference_another_workspace_asset(self):
        from sqlalchemy.exc import IntegrityError

        plan_id = f"plan_{uuid.uuid4().hex[:8]}"
        run_id = f"run_{uuid.uuid4().hex[:8]}"
        asset_id = f"asset_{uuid.uuid4().hex[:8]}"
        with self.owner.begin() as conn:
            conn.execute(
                self.text(
                    "INSERT INTO plans (id, workspace_id, product_id, mode, cap,"
                    " total_full, snapshot_hash, created_at) VALUES"
                    " (:id, :ws, :product, 'full', 0, 1, :hash, now())"
                ),
                {"id": plan_id, "ws": self.beta, "product": self.beta_product, "hash": "c" * 64},
            )
            conn.execute(
                self.text(
                    "INSERT INTO generation_runs (id, workspace_id, product_id, plan_id,"
                    " provider, model, aspect_ratio, image_size, variants, use_ref,"
                    " status, total, done, failed, est_cost, currency, revision,"
                    " created_at, updated_at) VALUES (:id, :ws, :product, :plan,"
                    " 'mock', 'mock', '1:1', '1K', 1, false, 'done', 1, 1, 0,"
                    " 0, 'USD', 1, now(), now())"
                ),
                {"id": run_id, "ws": self.beta, "product": self.beta_product, "plan": plan_id},
            )
            conn.execute(
                self.text(
                    "INSERT INTO assets (id, workspace_id, product_id, run_id,"
                    " combo_id, combo_code, tags_json, filename, provider, model,"
                    " prompt, fingerprint, status, error_code, error_detail,"
                    " created_at, updated_at) VALUES (:id, :ws, :product, :run,"
                    " 'C01', 'A1', '{}', 'beta.png', 'mock', 'mock', '', :fingerprint,"
                    " 'ok', '', '', now(), now())"
                ),
                {
                    "id": asset_id,
                    "ws": self.beta,
                    "product": self.beta_product,
                    "run": run_id,
                    "fingerprint": "d" * 64,
                },
            )

        conn = self.scoped_connection(self.engine(API_URL), self.alpha)
        sql = self.text(
            "INSERT INTO quality_reviews (id, workspace_id, product_id, asset_id,"
            " quality, reason, source, actor_id, created_at) VALUES"
            " (:id, :workspace, :product, :asset, 'approved', '', 'human',"
            " 'reviewer', now())"
        )
        states = []
        for candidate in (asset_id, "asset_never_issued"):
            with self.assertRaises(IntegrityError) as caught, conn.begin_nested():
                conn.execute(
                    sql,
                    {
                        "id": f"qr_{uuid.uuid4().hex}",
                        "workspace": self.alpha,
                        "product": self.alpha_product,
                        "asset": candidate,
                    },
                )
            states.append(getattr(caught.exception.orig, "sqlstate", None))

        self.assertEqual(states, ["23503", "23503"])

    # ------------------------------------------------------- pool reuse ----

    def test_a_pooled_connection_does_not_retain_the_previous_scope(self):
        """The failure a session-level SET would introduce.

        One physical connection, checked out twice. The second checkout must see
        no scope at all -- not the previous request's Workspace.
        """
        from sqlalchemy.pool import QueuePool

        engine = self.engine(
            API_URL, poolclass=QueuePool, pool_size=1, max_overflow=0
        )

        conn = engine.connect()
        transaction = conn.begin()
        self.set_scope(conn, self.alpha, 0)
        self.assertEqual(
            conn.execute(self.text("SELECT count(*) FROM products")).scalar(), 1
        )
        transaction.commit()
        conn.close()  # back to the pool

        reused = engine.connect()
        try:
            leaked = reused.execute(
                self.text("SELECT current_setting('marketai.workspace_id', true)")
            ).scalar()

            self.assertIn(leaked, (None, ""))
            self.assertEqual(
                reused.execute(self.text("SELECT count(*) FROM products")).scalar(),
                0,
            )
        finally:
            reused.close()

    def test_a_scope_does_not_survive_a_rolled_back_transaction(self):
        from sqlalchemy.pool import QueuePool

        engine = self.engine(
            API_URL, poolclass=QueuePool, pool_size=1, max_overflow=0
        )

        conn = engine.connect()
        transaction = conn.begin()
        self.set_scope(conn, self.alpha, 0)
        transaction.rollback()

        self.assertIn(
            conn.execute(
                self.text("SELECT current_setting('marketai.workspace_id', true)")
            ).scalar(),
            (None, ""),
        )
        conn.close()

    # ----------------------------------------------------- tenant scope ----

    def orm_session(self, url: str = API_URL):
        """An ORM session with an open transaction, as tenant_session opens it."""
        from sqlalchemy.orm import Session

        session = Session(bind=self.engine(url), future=True)
        self.addCleanup(session.close)
        session.begin()
        self.addCleanup(session.rollback)
        return session

    def context(self, workspace: str, **overrides):
        from app.platform.tenancy import TenantContext

        values = {
            "workspace_id": workspace,
            "role": "owner",
            "initiator_id": "user_1",
        }
        values.update(overrides)
        return TenantContext(**values)

    def setting(self, session, name: str):
        return session.execute(
            self.text("SELECT current_setting(:name, true)"), {"name": name}
        ).scalar()

    def test_leaving_the_outermost_scope_clears_the_database_setting(self):
        """Restoring only the Python side would leave RLS filtering by the old
        Workspace: the process believes it is unscoped, the database keeps
        answering for a tenant."""
        from app.platform.tenancy import WORKSPACE_SETTING, tenant_scope

        session = self.orm_session()

        with tenant_scope(session, self.context(self.alpha)):
            self.assertEqual(
                session.execute(self.text("SELECT count(*) FROM products")).scalar(),
                1,
            )

        self.assertIn(self.setting(session, WORKSPACE_SETTING), (None, ""))
        self.assertEqual(
            session.execute(self.text("SELECT count(*) FROM products")).scalar(),
            0,
        )

    def test_leaving_a_nested_scope_restores_the_outer_scope_in_the_database(self):
        from app.platform.tenancy import (
            EXECUTOR_SETTING,
            ROLE_SETTING,
            SYSTEM_WORKER,
            WORKSPACE_SETTING,
            apply_to_session,
            tenant_scope,
        )

        session = self.orm_session()
        outer = self.context(self.alpha)
        apply_to_session(session, outer)

        with tenant_scope(session, outer.for_system_execution()):
            self.assertEqual(self.setting(session, EXECUTOR_SETTING), SYSTEM_WORKER)
            self.assertEqual(self.setting(session, ROLE_SETTING), "system")

        # The database, not only session.info, is back on the outer scope.
        self.assertEqual(self.setting(session, EXECUTOR_SETTING), "user")
        self.assertEqual(self.setting(session, ROLE_SETTING), "owner")
        self.assertEqual(self.setting(session, WORKSPACE_SETTING), self.alpha)
        self.assertEqual(
            session.execute(self.text("SELECT count(*) FROM products")).scalar(), 1
        )

    def test_a_second_workspace_cannot_be_nested_in_one_transaction(self):
        from app.platform.tenancy import (
            WORKSPACE_SETTING,
            TenantScopeError,
            apply_to_session,
            tenant_scope,
        )

        session = self.orm_session()
        apply_to_session(session, self.context(self.alpha))

        with self.assertRaises(TenantScopeError):
            with tenant_scope(session, self.context(self.beta)):
                pass  # pragma: no cover - the scope must not open

        # Refusing leaves the transaction on the Workspace it was already on.
        self.assertEqual(self.setting(session, WORKSPACE_SETTING), self.alpha)
        rows = [r[0] for r in session.execute(self.text("SELECT id FROM products"))]
        self.assertEqual(rows, [self.alpha_product])

    def test_a_scope_restores_the_outer_workspace_when_the_block_raises(self):
        from app.platform.tenancy import (
            WORKSPACE_SETTING,
            apply_to_session,
            tenant_scope,
        )

        session = self.orm_session()
        outer = self.context(self.alpha)
        apply_to_session(session, outer)

        with self.assertRaises(ValueError):
            with tenant_scope(session, outer.for_system_execution()):
                raise ValueError("boom")

        self.assertEqual(self.setting(session, WORKSPACE_SETTING), self.alpha)

    def test_a_failed_statement_is_not_masked_by_leaving_the_scope(self):
        """The ordinary way a scope exits early is a statement that failed.

        PostgreSQL then refuses everything until ROLLBACK, ``set_config``
        included, so restoring the outer scope from ``finally`` raises
        ``InFailedSqlTransaction`` -- and if that escapes, the caller sees it
        instead of the query that actually broke.
        """
        from sqlalchemy.exc import ProgrammingError

        from app.platform.tenancy import (
            WORKSPACE_SETTING,
            TenantScopeError,
            current_context,
            tenant_scope,
        )

        session = self.orm_session()

        with self.assertRaises(ProgrammingError) as caught:
            with tenant_scope(session, self.context(self.alpha)):
                session.execute(self.text("SELECT 1 FROM table_that_is_not_there"))

        message = str(caught.exception)
        self.assertIn("table_that_is_not_there", message)
        self.assertNotIn("current transaction is aborted", message)

        # The restore failure is reported, not swallowed silently.
        notes = " ".join(getattr(caught.exception, "__notes__", []))
        self.assertIn("tenant scope was not restored", notes)

        # And the session does not go on claiming a scope it could not write.
        with self.assertRaises(TenantScopeError):
            current_context(session)

        session.rollback()
        self.assertIn(self.setting(session, WORKSPACE_SETTING), (None, ""))

    def test_a_failed_statement_in_a_nested_scope_is_not_masked_either(self):
        from sqlalchemy.exc import ProgrammingError

        from app.platform.tenancy import (
            TenantScopeError,
            apply_to_session,
            current_context,
            tenant_scope,
        )

        session = self.orm_session()
        outer = self.context(self.alpha)
        apply_to_session(session, outer)

        with self.assertRaises(ProgrammingError) as caught:
            with tenant_scope(session, outer.for_system_execution()):
                session.execute(self.text("SELECT 1 FROM table_that_is_not_there"))

        self.assertIn("table_that_is_not_there", str(caught.exception))

        # The outer scope could not be rewritten, so it is not claimed either.
        with self.assertRaises(TenantScopeError):
            current_context(session)

        session.rollback()

    # ------------------------------------------------------- privileges ----

    def test_only_the_migration_role_may_change_the_schema(self):
        from sqlalchemy.exc import ProgrammingError

        for url in (API_URL, WORKER_URL, READONLY_URL):
            with self.subTest(role=url.split("://", 1)[1].split(":", 1)[0]):
                conn = self.scoped_connection(self.engine(url), self.alpha)

                with self.assertRaises(ProgrammingError):
                    conn.execute(
                        self.text("CREATE TABLE rls_probe (id text primary key)")
                    )

    def test_runtime_roles_cannot_read_the_context_signing_key(self):
        from sqlalchemy.exc import ProgrammingError

        for url in (API_URL, WORKER_URL, READONLY_URL):
            with self.subTest(role=url.split("://", 1)[1].split(":", 1)[0]):
                conn = self.scoped_connection(self.engine(url), self.alpha)
                with self.assertRaises(ProgrammingError):
                    conn.execute(self.text("SELECT secret FROM tenant_context_secrets"))

    def test_runtime_roles_may_lock_append_only_evidence_but_not_update_it(self):
        """SELECT ... FOR UPDATE needs UPDATE on one column, nothing more.

        Launch re-validates eligibility under row locks in both the API and
        the Worker. The lock-only grant sits on the identity column, so the
        table-level UPDATE matrix (and the append-only triggers) still say
        these rows are immutable.
        """
        roles = ("marketai_domain_api", "marketai_domain_worker")
        tables = (
            "experiment_versions",
            "experiment_version_candidates",
            "quality_reviews",
            "truth_packages",
        )
        with self.engine(OWNER_URL).connect() as conn:
            for role in roles:
                for table in tables:
                    with self.subTest(role=role, table=table):
                        self.assertTrue(conn.execute(
                            self.text(
                                "SELECT has_column_privilege(:role, :table, 'id', 'UPDATE')"
                            ),
                            {"role": role, "table": f"public.{table}"},
                        ).scalar())
                        self.assertFalse(conn.execute(
                            self.text(
                                "SELECT has_table_privilege(:role, :table, 'UPDATE')"
                            ),
                            {"role": role, "table": f"public.{table}"},
                        ).scalar())
            self.assertTrue(conn.execute(
                self.text(
                    "SELECT has_column_privilege('marketai_domain_worker', "
                    "'public.experiments', 'id', 'UPDATE')"
                )
            ).scalar())
            self.assertFalse(conn.execute(
                self.text(
                    "SELECT has_table_privilege('marketai_domain_worker', "
                    "'public.experiments', 'UPDATE')"
                )
            ).scalar())
            self.assertFalse(conn.execute(
                self.text(
                    "SELECT has_any_column_privilege('marketai_domain_readonly', "
                    "'public.experiment_versions', 'UPDATE')"
                )
            ).scalar())

    def test_scale_runtime_grants_match_the_migration_matrix(self):
        """A released cluster grants Scale exactly f5eeeab4e9b9's matrix.

        The role replay revokes every table grant on each release, so this
        checks the privileges that survive it rather than the migration's.
        """
        import importlib.util
        from pathlib import Path

        migration = (
            Path(__file__).resolve().parents[1] / "migrations" / "versions"
            / "f5eeeab4e9b9_scale_deterministic_evidence_and_advice.py"
        )
        spec = importlib.util.spec_from_file_location("scale_grant_matrix", migration)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        roles = {
            "marketai_domain_api": 0,
            "marketai_domain_worker": 1,
            "marketai_domain_readonly": None,
        }
        with self.engine(OWNER_URL).connect() as conn:
            def has(role: str, table: str, privilege: str) -> bool:
                return bool(conn.execute(
                    self.text("SELECT has_table_privilege(:role, :table, :privilege)"),
                    {"role": role, "table": f"public.{table}", "privilege": privilege},
                ).scalar())

            # Activation (an API command added after that matrix) records
            # the Episode's baseline delivery configuration.
            beyond_matrix = {
                ("marketai_domain_api", "scale_delivery_config_sources"): ("INSERT",),
            }
            for table, matrix in module.GRANT_MATRIX.items():
                for role, index in roles.items():
                    granted = (() if index is None else matrix[index]) + beyond_matrix.get(
                        (role, table), ()
                    )
                    with self.subTest(table=table, role=role):
                        self.assertTrue(has(role, table, "SELECT"))
                        for privilege in ("INSERT", "UPDATE", "DELETE"):
                            self.assertEqual(
                                has(role, table, privilege),
                                privilege in granted,
                                privilege,
                            )

    def test_api_may_lock_and_consume_scale_command_rows_without_rewriting_them(self):
        """Scale enrollment, activation and Advice conversion run in the API.

        They lock the lineage guard and the Advice occurrence (SELECT ...
        FOR UPDATE needs UPDATE on one column) and consume an Enrollment
        Candidate or activation preview through consumed_at -- column grants
        only, so no table-level UPDATE and nothing for the Worker.
        """
        with self.engine(OWNER_URL).connect() as conn:
            def scalar(sql: str, **params) -> bool:
                return bool(conn.execute(self.text(sql), params).scalar())

            for table, column in (
                ("scale_lineage_guards", "id"),
                ("scale_advice_occurrences", "id"),
                ("scale_enrollment_candidates", "consumed_at"),
                ("scale_activation_previews", "consumed_at"),
            ):
                with self.subTest(table=table):
                    relation = f"public.{table}"
                    self.assertTrue(scalar(
                        "SELECT has_column_privilege('marketai_domain_api', :t, :c, 'UPDATE')",
                        t=relation, c=column,
                    ))
                    self.assertFalse(scalar(
                        "SELECT has_table_privilege('marketai_domain_api', :t, 'UPDATE')",
                        t=relation,
                    ))
                    self.assertFalse(scalar(
                        "SELECT has_any_column_privilege('marketai_domain_worker', :t, 'UPDATE')",
                        t=relation,
                    ))
            self.assertTrue(scalar(
                "SELECT has_table_privilege('marketai_domain_api', "
                "'public.scale_delivery_config_sources', 'INSERT')"
            ))

    def test_commit_and_rebind_keeps_the_scope_across_a_mid_session_commit(self):
        """Scale services commit part-way through a tenant_session
        (app/scale/advice.py) and the route then reads what they wrote. SET
        LOCAL ends with that transaction, so after a plain commit the rest of
        the unit of work reads nothing; commit_and_rebind binds the same
        context to the next transaction."""
        from sqlalchemy.orm import Session

        from app.platform.tenancy import TenantContext, apply_to_session, commit_and_rebind

        context = TenantContext(
            workspace_id=self.alpha, role="owner", initiator_id="rls-test",
        )
        read = self.text("SELECT id FROM products WHERE id = :id")
        with Session(bind=self.engine(API_URL)) as session:
            session.begin()
            apply_to_session(session, context)
            self.assertEqual(
                session.execute(read, {"id": self.alpha_product}).scalar(),
                self.alpha_product,
            )

            commit_and_rebind(session)
            self.assertEqual(
                session.execute(read, {"id": self.alpha_product}).scalar(),
                self.alpha_product,
            )

            session.commit()
            self.assertIsNone(session.execute(read, {"id": self.alpha_product}).scalar())

    def test_worker_may_read_but_not_write_judgements(self):
        """Localization jobs re-validate their source Winner as the Worker.

        evaluate_winner_identity() reads the Judgement behind the Decision;
        the Worker never writes one.
        """
        with self.engine(OWNER_URL).connect() as conn:
            for privilege, expected in (
                ("SELECT", True),
                ("INSERT", False),
                ("UPDATE", False),
                ("DELETE", False),
            ):
                with self.subTest(privilege=privilege):
                    self.assertEqual(
                        conn.execute(
                            self.text(
                                "SELECT has_table_privilege("
                                "'marketai_domain_worker', 'public.judgements', :p)"
                            ),
                            {"p": privilege},
                        ).scalar(),
                        expected,
                    )

    def test_the_read_only_role_cannot_write(self):
        from sqlalchemy.exc import ProgrammingError

        conn = self.scoped_connection(self.engine(READONLY_URL), self.alpha)

        with self.assertRaises(ProgrammingError):
            conn.execute(
                self.text(INSERT_PRODUCT),
                {
                    "id": f"p_{uuid.uuid4().hex[:8]}",
                    "workspace_id": self.alpha,
                    "name": "nope",
                },
            )

    def test_runtime_roles_cannot_bypass_row_level_security(self):
        # A role with BYPASSRLS would read every Workspace regardless of policy.
        # The migration role holds it because a migration has to see every
        # Workspace; marketai_domain_app holds it only while the call sites are
        # untenanted, and tests/test_domain_app_transition.py retires that.
        from tests.test_domain_app_transition import declared_bridge_state

        with self.owner.begin() as conn:
            bypassing = sorted(
                r[0]
                for r in conn.execute(
                    self.text(
                        "SELECT rolname FROM pg_roles WHERE rolbypassrls"
                        " AND rolname LIKE 'marketai_domain_%'"
                    )
                )
            )

        expected = ["marketai_domain_owner"]
        if declared_bridge_state() == "BYPASSRLS":
            expected = sorted(expected + ["marketai_domain_app"])

        self.assertEqual(bypassing, expected)

        for role in ("api", "worker", "readonly"):
            self.assertNotIn(f"marketai_domain_{role}", bypassing)

    def test_every_protected_table_has_row_level_security_forced(self):
        protected_tables = all_protected_tables()

        with self.owner.begin() as conn:
            rows = {
                r[0]: (r[1], r[2])
                for r in conn.execute(
                    self.text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity"
                        " FROM pg_class WHERE relname = ANY(:names)"
                    ),
                    {"names": list(protected_tables)},
                )
            }

        missing = [t for t in protected_tables if t not in rows]
        self.assertEqual(missing, [], "protected table is absent from the schema")

        unprotected = [t for t, (enabled, _) in rows.items() if not enabled]
        unforced = [t for t, (_, forced) in rows.items() if not forced]

        self.assertEqual(unprotected, [])
        self.assertEqual(unforced, [])

    def test_every_protected_table_has_an_isolation_policy(self):
        protected_tables = all_protected_tables()

        with self.owner.begin() as conn:
            covered = {
                r[0]
                for r in conn.execute(
                    self.text(
                        "SELECT tablename FROM pg_policies"
                        " WHERE policyname = 'tenant_isolation'"
                    )
                )
            }

        self.assertEqual(sorted(set(protected_tables) - covered), [])


class RowLevelSecurityCoverageTests(unittest.TestCase):
    """Guards that hold without a PostgreSQL cluster."""

    def test_global_domain_tables_are_not_classified_as_rls_protected(self):
        global_tables = set(all_global_domain_tables())

        self.assertEqual(
            global_tables,
            {
                "prompt_baselines",
                "market_policies",
                "market_policy_versions",
                "market_policy_version_events",
                "scale_policy_versions",
            },
        )
        self.assertEqual(sorted(set(all_protected_tables()) & global_tables), [])

    def test_new_workspace_migrations_use_the_canonical_policy_name(self):
        import inspect

        from migrations.versions import (
            t1a2b3c4d5e6_immutable_contextual_decisions as decisions_migration,
        )
        from migrations.versions import (
            u1a2b3c4d5e6_localization_truth_bounded_qc as localization_migration,
        )

        for migration in (decisions_migration, localization_migration):
            with self.subTest(migration=migration.revision):
                source = inspect.getsource(migration)
                self.assertNotIn("CREATE POLICY tenant_isolation_", source)
                self.assertIn("CREATE POLICY tenant_isolation ON", source)

    def test_the_migration_covers_every_table_in_the_model(self):
        # A new table added without a policy would be readable across tenants
        # the moment a runtime role touched it.
        from app.adapters.persistence.database import Base
        from app.adapters.persistence import models  # noqa: F401
        from app.entitlements import models as _ent_m, credentials as _ent_c, cost_models as _ent_co, usage_models as _ent_u  # noqa: F401
        from app.scale import models as _scale_m  # noqa: F401
        mapped = {t.name for t in Base.metadata.sorted_tables}
        protected = set(all_protected_tables())

        from migrations.versions.e9f0a1b2c3d4_authenticate_tenant_context import (
            PLATFORM_GLOBAL_TABLES,
        )

        self.assertEqual(
            PLATFORM_GLOBAL_TABLES,
            ("alembic_version", "tenant_context_secrets"),
        )
        reviewed_global = set(PLATFORM_GLOBAL_TABLES) | set(all_global_domain_tables())
        unprotected = sorted(mapped - protected - reviewed_global)

        self.assertEqual(
            unprotected,
            [],
            "these tables have no RLS policy; add one to the migration",
        )

    def test_workspace_owned_foreign_keys_are_tenant_composite(self):
        """A matching child workspace must be part of referential integrity.

        PostgreSQL's FK triggers are not filtered by RLS. A naked ``asset_id``
        therefore both admits a cross-tenant relation and reveals whether the
        guessed ID exists. Every relationship between two direct tenant tables
        needs a composite sibling that binds the shared Workspace.
        """
        from app.adapters.persistence.database import Base
        from app.adapters.persistence import models  # noqa: F401
        from app.entitlements import models as _ent_m, credentials as _ent_c, cost_models as _ent_co, usage_models as _ent_u  # noqa: F401
        from app.scale import models as _scale_m  # noqa: F401

        unsafe: list[str] = []
        for child in Base.metadata.sorted_tables:
            if "workspace_id" not in child.c:
                continue
            for column in child.c:
                for fk in column.foreign_keys:
                    parent = fk.column.table
                    if "workspace_id" not in parent.c or column.name == "workspace_id":
                        continue
                    if parent.c["workspace_id"].nullable:
                        continue
                    protected = any(
                        {"workspace_id", column.name} <= set(constraint.column_keys)
                        and "workspace_id"
                        in {element.column.name for element in constraint.elements}
                        for constraint in child.foreign_key_constraints
                    )
                    if not protected:
                        unsafe.append(f"{child.name}.{column.name}->{parent.name}.id")

        self.assertEqual(unsafe, [])

    def test_every_current_table_is_classified_in_exactly_one_aggregate(self):
        from app.adapters.persistence.database import Base
        from app.adapters.persistence import models  # noqa: F401
        from app.entitlements import models as _ent_m, credentials as _ent_c, cost_models as _ent_co, usage_models as _ent_u  # noqa: F401
        from app.scale import models as _scale_m  # noqa: F401
        from app.competitors import models as _competitor_m  # noqa: F401

        memberships = [
            table
            for tables in CURRENT_AGGREGATE_TABLES.values()
            for table in tables
        ]
        mapped = {table.name for table in Base.metadata.sorted_tables}

        self.assertEqual(len(memberships), len(set(memberships)))
        self.assertEqual(set(memberships), mapped)

    def test_child_policies_name_a_real_foreign_key(self):
        from app.adapters.persistence.database import Base
        from app.adapters.persistence import models  # noqa: F401
        from app.entitlements import models as _ent_m, credentials as _ent_c, cost_models as _ent_co, usage_models as _ent_u  # noqa: F401
        from app.scale import models as _scale_m  # noqa: F401
        from migrations.versions.f1a2c3d4e5b6_row_level_security import CHILD_TABLES

        tables = {t.name: t for t in Base.metadata.sorted_tables}

        for table, column, parent in CHILD_TABLES:
            with self.subTest(table=table):
                self.assertIn(column, tables[table].c)

                targets = {
                    fk.column.table.name
                    for fk in tables[table].c[column].foreign_keys
                }
                self.assertIn(parent, targets)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
