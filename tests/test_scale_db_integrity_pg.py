"""PostgreSQL-only database integrity tests for the six new Scale
mode-scoped tables added by issue #86's hardening work (Tasks 7-10):

* ``ScaleDeliveryTargetRecord`` (Task 7, ``scale_delivery_targets``)
* ``ScaleCommandJournalRecord`` (Task 8, ``scale_command_journal``)
* ``ScaleLineageGuardRecord`` (Task 8, ``scale_lineage_guards``)
* ``ScaleEnrollmentCandidateRecord`` (Task 9, ``scale_enrollment_candidates``)
* ``ScaleDeliveryEvidenceSnapshotRecord`` (Task 10, ``scale_delivery_evidence_snapshots``)
* ``ScaleActivationPreviewRecord`` (Task 10, ``scale_activation_previews``)

Everything these tables enforce at the database layer -- composite FKs
across Workspace/Meta-mode/scenario boundaries, RLS, CHECK constraints, and
the ``marketai_bump_scale_lineage_guards()`` trigger family installed by
``migrations/versions/ab1_scale_lineage_tables.py`` -- needs a real
PostgreSQL cluster to prove; none of it is portable to SQLite (RLS and
triggers have no SQLite equivalent, and even the composite FKs, while
dialect-portable in principle, are exercised end-to-end here against the
real migrated schema rather than a hand-picked ``Base.metadata.create_all``
subset). This file is test-only -- no production code changes.

Pattern: this repo has no ``pg_session`` fixture. It follows the proven
pattern in ``tests/test_scale_migration.py``'s ``ScalePostgresConstraintTests``
(itself following ``tests/test_postgres_rls.py`` and
``tests/test_meta_rls_isolation.py``): connect as the real least-privilege
roles (``marketai_domain_owner`` for setup/teardown -- BYPASSRLS, per
``deploy/postgres/domain-tenant-roles.sql`` -- and ``marketai_domain_api``
for RLS-scoped reads), sign the tenant context by hand via
``app.platform.tenancy.tenant_context_signature``, and skip the whole
module when ``DOMAIN_OWNER_DATABASE_URL``/``DOMAIN_API_DATABASE_URL``/
``MARKETAI_TENANT_CONTEXT_SECRET`` are not configured, so ``make verify``
stays clean on a SQLite-only checkout and CI supplies the cluster.

Every INSERT here is raw SQL via ``text()`` (never the ORM), including for
the deep upstream fixture chain (Product -> Plan -> GenerationRun -> Asset
-> AssetVersion; LibraryEntry -> LibraryVersion; MetaConnection ->
ObservationContractGeneration -> ScenarioVersion) that
``ScaleDeliveryTargetRecord``'s and ``ScaleLineageGuardRecord``'s composite
FKs reach into -- so a raised ``IntegrityError``/``DBAPIError`` proves the
database's own constraint fired, not an application-level guard. Negative
(expected-to-fail) inserts run inside ``conn.begin_nested()`` (a SAVEPOINT),
mirroring ``tests/test_postgres_rls.py``'s
``test_assets_cannot_reference_a_product_from_another_workspace``, so one
test's expected violation never poisons the surrounding transaction other
fixture rows in the same test still need.

``winner_decision_id``/``winner_confirmation_id``/``market_asset_binding_id``
on ``ScaleLineageGuardRecord`` (and ``winner_decision_id`` on
``ScaleEnrollmentCandidateRecord``) carry no database-level FK by design
(see those models' docstrings in ``app/scale/models.py``), so plain
fabricated ids are used for those fields here, exactly as
``tests/test_scale_lineage_guard.py`` already does for its own (SQLite)
unit tests.
"""
from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from app.platform.tenancy import TenantContext, tenant_context_signature
from tests.test_meta_rls_isolation import INSERT_CONNECTION
from tests.test_scale_migration import (
    API_URL,
    CONFIGURED,
    CONTEXT_SECRET,
    INSERT_WORKSPACE,
    OWNER_URL,
    SKIP_REASON,
)

H64 = "a" * 64
NO_SCENARIO = "__no_scenario__"
OBSERVED_AT = datetime(2026, 9, 1, tzinfo=timezone.utc)
FAR_FUTURE = OBSERVED_AT + timedelta(days=365)


def _scenario_scope_for(meta_mode: str, tag: str) -> str:
    """Mirrors ``SCENARIO_BICONDITIONAL_SQL``: a real key under
    ``controlled_mock``, the reserved sentinel everywhere else."""
    return f"scn_{tag}" if meta_mode == "controlled_mock" else NO_SCENARIO


# ---------------------------------------------------------------------------
# Raw-SQL INSERT templates for the deep upstream fixture chain. Every
# NOT NULL column is supplied explicitly (never relying on a Python-side
# ORM ``default=`` a raw INSERT would never see).
# ---------------------------------------------------------------------------

INSERT_PRODUCT = """
INSERT INTO products
    (id, workspace_id, name, description, selling_points, audience, brand_notes,
     ref_images, economics, revision, created_at, updated_at)
VALUES
    (:id, :workspace_id, 'Test product', '', '[]', '', '', '[]', '{}', 1, now(), now())
"""

INSERT_PLAN = """
INSERT INTO plans
    (id, workspace_id, product_id, mode, cap, total_full, snapshot_hash, created_at)
VALUES
    (:id, :workspace_id, :product_id, 'full', 0, 1, :h64, now())
"""

INSERT_GENERATION_RUN = """
INSERT INTO generation_runs
    (id, workspace_id, product_id, plan_id, provider, model, aspect_ratio, image_size,
     variants, use_ref, status, total, done, failed, est_cost, currency, revision,
     created_at, updated_at)
VALUES
    (:id, :workspace_id, :product_id, :plan_id, 'mock', 'mock', '1:1', '1024x1024',
     1, false, 'succeeded', 1, 1, 0, 0, 'USD', 1, now(), now())
"""

INSERT_ASSET = """
INSERT INTO assets
    (id, workspace_id, product_id, run_id, combo_id, combo_code, tags_json, filename,
     provider, model, prompt, fingerprint, status, error_code, error_detail,
     created_at, updated_at)
VALUES
    (:id, :workspace_id, :product_id, :run_id, 'combo_1', 'combo-1', '{}', 'asset.png',
     'mock', 'mock', 'a test asset', :fingerprint, 'succeeded', '', '', now(), now())
"""

INSERT_ASSET_VERSION = """
INSERT INTO asset_versions
    (id, workspace_id, asset_id, kind, aspect_ratio, language, object_key,
     content_sha256, byte_size, mime_type, origin_kind, created_at)
VALUES
    (:id, :workspace_id, :asset_id, 'image', '1:1', 'en', :object_key,
     :h64, 1024, 'image/png', 'legacy', now())
"""

INSERT_LIBRARY_ENTRY = """
INSERT INTO library_entries
    (id, workspace_id, entry_type, display_name, lifecycle, created_at, updated_at)
VALUES
    (:id, :workspace_id, :entry_type, :name, 'active', now(), now())
"""

INSERT_LIBRARY_VERSION = """
INSERT INTO library_versions
    (id, workspace_id, entry_id, entry_type, version_no, content_json, content_hash,
     content_schema_version, lifecycle_revision, state, eligibility, validation_json,
     origin_command, published_by, published_at, withdrawn_by, withdraw_reason)
VALUES
    (:id, :workspace_id, :entry_id, :entry_type, 1, '{}', :h64,
     1, 1, 'effective', 'eligible', '{}',
     'create', 'test-fixture', now(), '', '')
"""

INSERT_CONTRACT_GENERATION = """
INSERT INTO observation_contract_generation_records
    (id, workspace_id, meta_mode, meta_connection_id, contract_type, generation_number,
     ad_account_id, fields, attribution_window, breakdowns, time_window, contract_hash,
     local_timezone, state, created_at)
VALUES
    (:id, :workspace_id, :meta_mode, :meta_connection_id, 'delivery_configuration', 1,
     :ad_account_id, '[]', '{}', '[]', '{}', :h64,
     'UTC', 'active', now())
"""

INSERT_SCENARIO_VERSION = """
INSERT INTO scenario_version_records
    (id, workspace_id, meta_mode, meta_connection_id, scenario_config, scenario_hash,
     is_default, created_at)
VALUES
    (:id, :workspace_id, :meta_mode, :meta_connection_id, '{}', :h64, false, now())
"""

# A local, ``scenario_scope_key``-parameterized copy of
# ``tests/test_scale_migration.py``'s ``INSERT_ENROLLMENT``: that file's
# version hardcodes ``'__no_scenario__'`` in the VALUES clause because
# every one of its own call sites only ever seeds ``customer_sandbox``
# enrollments. This file's activation-preview fixtures need
# ``controlled_mock`` enrollments too, which requires a real scenario key
# (``ck_senr_scenario_biconditional``), so the column is bound here instead.
INSERT_ENROLLMENT = """
INSERT INTO scale_enrollments
    (id, workspace_id, meta_mode, meta_connection_id, scenario_scope_key,
     ad_account_id, winner_decision_id, winner_confirmation_id, asset_version_id,
     product_version_id, offer_version_id, market_code, remote_binding_key,
     lifecycle, creator_id, original_initiator_id, revision,
     evaluation_cancellation_generation, created_at, updated_at)
VALUES
    (:id, :workspace_id, :meta_mode, :meta_connection_id, :scenario_scope_key,
     :ad_account_id, 'wd_1', 'wc_1', 'av_1', 'pv_1', 'ov_1', 'US',
     :remote_binding_key, :lifecycle, 'user_1', 'user_1', 1, 0, now(), now())
"""

# ---------------------------------------------------------------------------
# Raw-SQL INSERT templates for the six Task 7-10 tables under test. Every
# column each model declares NOT NULL is present; nullable provenance
# columns (``source_sync_run_id``/``source_publication_id``/
# ``source_attestation_id``) default to NULL via the params dict so a
# caller can override just the one(s) a test needs.
# ---------------------------------------------------------------------------

INSERT_DELIVERY_TARGET = """
INSERT INTO scale_delivery_targets
    (id, workspace_id, meta_mode, meta_connection_id, ad_account_id, scenario_scope_key,
     campaign_remote_id, adset_remote_id, ad_remote_id, ad_creative_id,
     creative_asset_version_id, creative_binding_evidence_hash, market_code, placement,
     attribution_setting, contract_generation_id, offer_version_id, landing_page_evidence_hash,
     source_type, scenario_version_id, source_sync_run_id, source_publication_id,
     source_attestation_id, source_manifest, source_manifest_hash,
     target_fingerprint, remote_binding_key, observed_at, expires_at, created_at)
VALUES
    (:id, :workspace_id, :meta_mode, :meta_connection_id, :ad_account_id, :scenario_scope_key,
     :campaign_remote_id, :adset_remote_id, :ad_remote_id, :ad_creative_id,
     :creative_asset_version_id, :h64, :market_code, :placement,
     :attribution_setting, :contract_generation_id, :offer_version_id, :landing_page_evidence_hash,
     :source_type, :scenario_version_id, :source_sync_run_id, :source_publication_id,
     :source_attestation_id, '{}', :h64,
     :h64, :remote_binding_key, now(), :expires_at, now())
"""

INSERT_COMMAND_JOURNAL = """
INSERT INTO scale_command_journal
    (id, workspace_id, meta_mode, scenario_scope_key, command_type, dedup_key,
     request_fingerprint, result_resource_id, result_resource_type, initiator_id, created_at)
VALUES
    (:id, :workspace_id, :meta_mode, :scenario_scope_key, :command_type, :dedup_key,
     :request_fingerprint, :result_resource_id, :result_resource_type, 'test', now())
"""

INSERT_LINEAGE_GUARD = """
INSERT INTO scale_lineage_guards
    (id, workspace_id, meta_mode, scenario_scope_key, winner_decision_id,
     winner_confirmation_id, asset_version_id, product_version_id, offer_version_id,
     market_asset_binding_id, delivery_target_id, meta_connection_id, contract_generation_id,
     revision, updated_at, created_at)
VALUES
    (:id, :workspace_id, :meta_mode, :scenario_scope_key, :winner_decision_id,
     :winner_confirmation_id, :asset_version_id, :product_version_id, :offer_version_id,
     :market_asset_binding_id, :delivery_target_id, :meta_connection_id, :contract_generation_id,
     1, now(), now())
"""

INSERT_ENROLLMENT_CANDIDATE = """
INSERT INTO scale_enrollment_candidates
    (id, workspace_id, meta_mode, scenario_scope_key, winner_decision_id, delivery_target_id,
     lineage_guard_id, candidate_fingerprint, guard_revision_at_creation, created_by,
     expires_at, created_at)
VALUES
    (:id, :workspace_id, :meta_mode, :scenario_scope_key, :winner_decision_id, :delivery_target_id,
     :lineage_guard_id, :candidate_fingerprint, 1, 'test', :expires_at, now())
"""

INSERT_EVIDENCE_SNAPSHOT = """
INSERT INTO scale_delivery_evidence_snapshots
    (id, workspace_id, meta_mode, scenario_scope_key, delivery_target_id,
     delivery_target_fingerprint, contract_generation_id, attestation_id,
     evidence_watermark, observed_at, created_at)
VALUES
    (:id, :workspace_id, :meta_mode, :scenario_scope_key, :delivery_target_id,
     :delivery_target_fingerprint, :contract_generation_id, :attestation_id,
     :evidence_watermark, now(), now())
"""

INSERT_ACTIVATION_PREVIEW = """
INSERT INTO scale_activation_previews
    (id, workspace_id, meta_mode, scenario_scope_key, enrollment_id, transition_kind,
     delivery_evidence_snapshot_id, lineage_guard_id, candidate_fingerprint, guard_revision,
     lineage_hash, enrollment_revision, expires_at, created_at)
VALUES
    (:id, :workspace_id, :meta_mode, :scenario_scope_key, :enrollment_id, :transition_kind,
     :delivery_evidence_snapshot_id, :lineage_guard_id, :candidate_fingerprint, 1,
     :lineage_hash, 1, :expires_at, now())
"""

# Children-before-parents so FK ondelete rules never block cleanup,
# regardless of each table's own ondelete policy.
_CLEANUP_TABLES_IN_ORDER = (
    "scale_activation_previews",
    "scale_delivery_evidence_snapshots",
    "scale_enrollment_candidates",
    "scale_lineage_guards",
    "scale_command_journal",
    "scale_delivery_targets",
    "scale_enrollments",
    "scenario_version_records",
    "observation_contract_generation_records",
    "meta_connection_records",
    "asset_versions",
    "assets",
    "generation_runs",
    "plans",
    "library_versions",
    "library_entries",
    "products",
)


# ---------------------------------------------------------------------------
# Fixture builders -- free functions taking a live ``Connection`` (owner
# role, BYPASSRLS) so every test method can compose exactly the chain it
# needs without a class hierarchy of overrides.
# ---------------------------------------------------------------------------


def _seed_asset_version(conn, workspace_id: str) -> str:
    n = uuid.uuid4().hex[:10]
    product_id, plan_id, run_id, asset_id, version_id = (
        f"prod_{n}", f"plan_{n}", f"run_{n}", f"asset_{n}", f"astv_{n}",
    )
    conn.execute(text(INSERT_PRODUCT), {"id": product_id, "workspace_id": workspace_id})
    conn.execute(
        text(INSERT_PLAN),
        {"id": plan_id, "workspace_id": workspace_id, "product_id": product_id, "h64": H64},
    )
    conn.execute(
        text(INSERT_GENERATION_RUN),
        {"id": run_id, "workspace_id": workspace_id, "product_id": product_id, "plan_id": plan_id},
    )
    conn.execute(
        text(INSERT_ASSET),
        {
            "id": asset_id, "workspace_id": workspace_id, "product_id": product_id,
            "run_id": run_id, "fingerprint": f"fp_{n}",
        },
    )
    conn.execute(
        text(INSERT_ASSET_VERSION),
        {
            "id": version_id, "workspace_id": workspace_id, "asset_id": asset_id,
            "object_key": f"assets/{n}.png", "h64": H64,
        },
    )
    return version_id


def _seed_library_version(conn, workspace_id: str, entry_type: str) -> str:
    n = uuid.uuid4().hex[:10]
    entry_id, version_id = f"lent_{n}", f"lver_{n}"
    conn.execute(
        text(INSERT_LIBRARY_ENTRY),
        {
            "id": entry_id, "workspace_id": workspace_id, "entry_type": entry_type,
            "name": f"{entry_type}-{n}",
        },
    )
    conn.execute(
        text(INSERT_LIBRARY_VERSION),
        {
            "id": version_id, "workspace_id": workspace_id, "entry_id": entry_id,
            "entry_type": entry_type, "h64": H64,
        },
    )
    return version_id


def _seed_connection(conn, workspace_id: str, meta_mode: str, ad_account_id: str = "act_1") -> str:
    connection_id = f"mcon_{uuid.uuid4().hex[:10]}"
    conn.execute(
        text(INSERT_CONNECTION),
        {
            "id": connection_id, "workspace_id": workspace_id, "meta_mode": meta_mode,
            "ad_account_id": ad_account_id,
        },
    )
    return connection_id


def _seed_curated_scenario_context(
    conn, workspace_id: str, meta_mode: str, ad_account_id: str = "act_1",
) -> dict:
    """A connection + contract generation + scenario version, all in
    ``meta_mode`` -- the upstream trio ``curated_scenario`` delivery
    targets require (spec §4.3)."""
    connection_id = _seed_connection(conn, workspace_id, meta_mode, ad_account_id)
    gen_id = f"ocg_{uuid.uuid4().hex[:10]}"
    conn.execute(
        text(INSERT_CONTRACT_GENERATION),
        {
            "id": gen_id, "workspace_id": workspace_id, "meta_mode": meta_mode,
            "meta_connection_id": connection_id, "ad_account_id": ad_account_id, "h64": H64,
        },
    )
    scenario_id = f"scv_{uuid.uuid4().hex[:10]}"
    conn.execute(
        text(INSERT_SCENARIO_VERSION),
        {
            "id": scenario_id, "workspace_id": workspace_id, "meta_mode": meta_mode,
            "meta_connection_id": connection_id, "h64": H64,
        },
    )
    return {
        "meta_connection_id": connection_id,
        "ad_account_id": ad_account_id,
        "contract_generation_id": gen_id,
        "scenario_version_id": scenario_id,
    }


def _seed_delivery_target(
    conn, workspace_id: str, meta_mode: str, scenario_scope_key: str, upstream: dict,
    **overrides,
) -> str:
    """A valid ``curated_scenario`` delivery target. ``overrides`` lets a
    negative test corrupt exactly one field (e.g. ``workspace_id`` or
    ``meta_connection_id``) while keeping every other FK-satisfying."""
    n = uuid.uuid4().hex[:10]
    target_id = f"sdt_{n}"
    offer_id = _seed_library_version(conn, workspace_id, "offer")
    landing_id = "f" * 64  # the Landing Page evidence hash a Winner froze (#129)
    asset_version_id = _seed_asset_version(conn, workspace_id)
    params = dict(
        id=target_id,
        workspace_id=workspace_id,
        meta_mode=meta_mode,
        meta_connection_id=upstream["meta_connection_id"],
        ad_account_id=upstream["ad_account_id"],
        scenario_scope_key=scenario_scope_key,
        campaign_remote_id=f"camp_{n}",
        adset_remote_id=f"adset_{n}",
        ad_remote_id=f"ad_{n}",
        ad_creative_id=f"creative_{n}",
        creative_asset_version_id=asset_version_id,
        market_code="US",
        placement="feed",
        attribution_setting="7d_click_1d_view",
        contract_generation_id=upstream["contract_generation_id"],
        offer_version_id=offer_id,
        landing_page_evidence_hash=landing_id,
        source_type="curated_scenario",
        scenario_version_id=upstream["scenario_version_id"],
        source_sync_run_id=None,
        source_publication_id=None,
        source_attestation_id=None,
        remote_binding_key=(
            f"{upstream['meta_connection_id']}/{upstream['ad_account_id']}/"
            f"camp_{n}/adset_{n}/ad_{n}"
        ),
        expires_at=FAR_FUTURE,
        h64=H64,
    )
    params.update(overrides)
    conn.execute(text(INSERT_DELIVERY_TARGET), params)
    return target_id


def _seed_lineage_guard(
    conn, workspace_id: str, meta_mode: str, scenario_scope_key: str,
    delivery_target_id: str, meta_connection_id: str, contract_generation_id: str,
    **overrides,
) -> str:
    n = uuid.uuid4().hex[:10]
    guard_id = f"slg_{n}"
    params = dict(
        id=guard_id,
        workspace_id=workspace_id,
        meta_mode=meta_mode,
        scenario_scope_key=scenario_scope_key,
        winner_decision_id=f"wd_{n}",
        winner_confirmation_id=f"wc_{n}",
        asset_version_id=_seed_asset_version(conn, workspace_id),
        product_version_id=_seed_library_version(conn, workspace_id, "product"),
        offer_version_id=_seed_library_version(conn, workspace_id, "offer"),
        market_asset_binding_id=f"mab_{n}",
        delivery_target_id=delivery_target_id,
        meta_connection_id=meta_connection_id,
        contract_generation_id=contract_generation_id,
    )
    params.update(overrides)
    conn.execute(text(INSERT_LINEAGE_GUARD), params)
    return guard_id


def _seed_enrollment_candidate(
    conn, workspace_id: str, meta_mode: str, scenario_scope_key: str,
    delivery_target_id: str, lineage_guard_id: str, **overrides,
) -> str:
    n = uuid.uuid4().hex[:10]
    candidate_id = f"secand_{n}"
    params = dict(
        id=candidate_id,
        workspace_id=workspace_id,
        meta_mode=meta_mode,
        scenario_scope_key=scenario_scope_key,
        winner_decision_id=f"wd_{n}",
        delivery_target_id=delivery_target_id,
        lineage_guard_id=lineage_guard_id,
        candidate_fingerprint=H64,
        expires_at=FAR_FUTURE,
    )
    params.update(overrides)
    conn.execute(text(INSERT_ENROLLMENT_CANDIDATE), params)
    return candidate_id


def _seed_evidence_snapshot(
    conn, workspace_id: str, meta_mode: str, scenario_scope_key: str,
    delivery_target_id: str, contract_generation_id: str, **overrides,
) -> str:
    n = uuid.uuid4().hex[:10]
    snapshot_id = f"sdes_{n}"
    params = dict(
        id=snapshot_id,
        workspace_id=workspace_id,
        meta_mode=meta_mode,
        scenario_scope_key=scenario_scope_key,
        delivery_target_id=delivery_target_id,
        delivery_target_fingerprint=H64,
        contract_generation_id=contract_generation_id,
        attestation_id=f"eatt_{n}",
        evidence_watermark="[2026-09-01,2026-09-01]",
    )
    params.update(overrides)
    conn.execute(text(INSERT_EVIDENCE_SNAPSHOT), params)
    return snapshot_id


def _seed_enrollment(conn, workspace_id: str, meta_mode: str, **overrides) -> str:
    n = uuid.uuid4().hex[:10]
    enrollment_id = f"senr_{n}"
    params = dict(
        id=enrollment_id,
        workspace_id=workspace_id,
        meta_mode=meta_mode,
        meta_connection_id=f"mcon_{n}",
        scenario_scope_key=_scenario_scope_for(meta_mode, n),
        ad_account_id=f"act_{n}",
        remote_binding_key=f"mcon_{n}/act_{n}/camp_1",
        lifecycle="draft",
    )
    params.update(overrides)
    conn.execute(text(INSERT_ENROLLMENT), params)
    return enrollment_id


def _seed_activation_preview(
    conn, workspace_id: str, meta_mode: str, scenario_scope_key: str,
    enrollment_id: str, delivery_evidence_snapshot_id: str, lineage_guard_id: str,
    **overrides,
) -> str:
    n = uuid.uuid4().hex[:10]
    preview_id = f"saprev_{n}"
    params = dict(
        id=preview_id,
        workspace_id=workspace_id,
        meta_mode=meta_mode,
        scenario_scope_key=scenario_scope_key,
        enrollment_id=enrollment_id,
        transition_kind="activate",
        delivery_evidence_snapshot_id=delivery_evidence_snapshot_id,
        lineage_guard_id=lineage_guard_id,
        candidate_fingerprint=H64,
        lineage_hash=H64,
        expires_at=FAR_FUTURE,
    )
    params.update(overrides)
    conn.execute(text(INSERT_ACTIVATION_PREVIEW), params)
    return preview_id


# ---------------------------------------------------------------------------
# Base test case: owner-role connection plumbing + per-test Workspace
# tracking/cleanup, shared by every class below.
# ---------------------------------------------------------------------------


@pytest.mark.postgres
@unittest.skipUnless(CONFIGURED, SKIP_REASON)
class _ScaleDbIntegrityTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.owner_engine = create_engine(OWNER_URL, future=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.owner_engine.dispose()

    def setUp(self) -> None:
        self._workspaces: list[str] = []
        self.addCleanup(self._cleanup_workspaces)

    def make_workspace(self) -> str:
        workspace_id = f"ws_sdbi_{uuid.uuid4().hex[:10]}"
        with self.owner_engine.begin() as conn:
            conn.execute(text(INSERT_WORKSPACE), {"id": workspace_id})
        self._workspaces.append(workspace_id)
        return workspace_id

    def _cleanup_workspaces(self) -> None:
        if not self._workspaces:
            return
        with self.owner_engine.begin() as conn:
            for table in _CLEANUP_TABLES_IN_ORDER:
                for workspace_id in self._workspaces:
                    conn.execute(
                        text(f"DELETE FROM {table} WHERE workspace_id = :ws"),
                        {"ws": workspace_id},
                    )
            for workspace_id in self._workspaces:
                conn.execute(
                    text("DELETE FROM workspaces WHERE id = :ws"), {"ws": workspace_id},
                )

    # ---------------------------------------------------------- RLS scope --

    def api_engine(self):
        engine = create_engine(API_URL, future=True)
        self.addCleanup(engine.dispose)
        return engine

    def scoped_connection(self, workspace: str, meta_mode: str | None):
        """An API-role connection, inside its own rolled-back transaction,
        carrying a signed tenant context for RLS reads."""
        conn = self.api_engine().connect()
        self.addCleanup(conn.close)
        transaction = conn.begin()
        self.addCleanup(lambda: transaction.rollback() if transaction.is_active else None)
        self._set_scope(conn, workspace, meta_mode)
        return conn

    def _set_scope(self, connection, workspace: str, meta_mode: str | None) -> None:
        context = TenantContext(
            workspace_id=workspace, role="owner", initiator_id="scale-db-integrity-test",
            authorization_epoch=0, meta_mode=meta_mode,
        )
        connection.execute(
            text("SELECT set_config('marketai.workspace_id', :w, true)"), {"w": workspace},
        )
        connection.execute(
            text("SELECT set_config('marketai.authorization_epoch', '0', true)"),
        )
        connection.execute(
            text("SELECT set_config('marketai.meta_mode', :m, true)"), {"m": meta_mode or ""},
        )
        connection.execute(
            text("SELECT set_config('marketai.context_signature', :signature, true)"),
            {"signature": tenant_context_signature(context, CONTEXT_SECRET)},
        )


# ---------------------------------------------------------------------------
# D1-D4: composite FK violations across Workspace/Meta-mode boundaries.
# ---------------------------------------------------------------------------


@pytest.mark.postgres
@unittest.skipUnless(CONFIGURED, SKIP_REASON)
class ScaleCompositeForeignKeyViolationTests(_ScaleDbIntegrityTestCase):
    def test_delivery_target_rejects_connection_from_another_workspace(self) -> None:
        """D1: ``fk_sdt_connection`` is scoped by ``workspace_id`` too, not
        just ``(meta_mode, id, ad_account_id)`` -- a connection that exists,
        just in a different Workspace, is not a legal reference."""
        ws_a = self.make_workspace()
        ws_b = self.make_workspace()
        with self.owner_engine.begin() as conn:
            upstream = _seed_curated_scenario_context(conn, ws_a, "controlled_mock")

            with self.assertRaises(IntegrityError), conn.begin_nested():
                _seed_delivery_target(
                    conn, ws_b, "controlled_mock", "scn_1", upstream,
                )

    def test_delivery_target_rejects_connection_from_another_mode(self) -> None:
        """D2: same Workspace, wrong ``meta_mode`` -- ``fk_sdt_connection``
        still rejects it because the connection's own ``meta_mode`` differs
        from the row being inserted."""
        ws = self.make_workspace()
        with self.owner_engine.begin() as conn:
            upstream = _seed_curated_scenario_context(conn, ws, "controlled_mock")

            with self.assertRaises(IntegrityError), conn.begin_nested():
                _seed_delivery_target(
                    conn, ws, "customer_sandbox", NO_SCENARIO, upstream,
                )

    def test_lineage_guard_rejects_delivery_target_from_another_workspace(self) -> None:
        """D1 variant: ``fk_slg_delivery_target`` is a 4-column composite FK
        (workspace_id, meta_mode, delivery_target_id, scenario_scope_key) --
        a target that exists in a different Workspace is not a legal
        reference even when every other field matches."""
        ws_a = self.make_workspace()
        ws_b = self.make_workspace()
        with self.owner_engine.begin() as conn:
            upstream = _seed_curated_scenario_context(conn, ws_a, "controlled_mock")
            target_id = _seed_delivery_target(
                conn, ws_a, "controlled_mock", "scn_1", upstream,
            )

            with self.assertRaises(IntegrityError), conn.begin_nested():
                _seed_lineage_guard(
                    conn, ws_b, "controlled_mock", "scn_1",
                    target_id, upstream["meta_connection_id"],
                    upstream["contract_generation_id"],
                )

    def test_lineage_guard_rejects_delivery_target_from_another_mode(self) -> None:
        """D2 variant: same Workspace, but the target's real ``meta_mode``
        (``controlled_mock``) differs from the guard row's declared mode."""
        ws = self.make_workspace()
        with self.owner_engine.begin() as conn:
            upstream = _seed_curated_scenario_context(conn, ws, "controlled_mock")
            target_id = _seed_delivery_target(
                conn, ws, "controlled_mock", "scn_1", upstream,
            )

            with self.assertRaises(IntegrityError), conn.begin_nested():
                _seed_lineage_guard(
                    conn, ws, "customer_sandbox", NO_SCENARIO,
                    target_id, upstream["meta_connection_id"],
                    upstream["contract_generation_id"],
                )

    def test_enrollment_candidate_rejects_guard_from_another_workspace(self) -> None:
        """D1 variant on ``fk_secand_guard``."""
        ws_a = self.make_workspace()
        ws_b = self.make_workspace()
        with self.owner_engine.begin() as conn:
            upstream = _seed_curated_scenario_context(conn, ws_a, "controlled_mock")
            target_id = _seed_delivery_target(
                conn, ws_a, "controlled_mock", "scn_1", upstream,
            )
            guard_id = _seed_lineage_guard(
                conn, ws_a, "controlled_mock", "scn_1", target_id,
                upstream["meta_connection_id"], upstream["contract_generation_id"],
            )

            with self.assertRaises(IntegrityError), conn.begin_nested():
                _seed_enrollment_candidate(
                    conn, ws_b, "controlled_mock", "scn_1",
                    target_id, guard_id,
                )

    def test_enrollment_candidate_rejects_delivery_target_from_another_mode(self) -> None:
        """D3: ``fk_secand_target`` -- a candidate cannot bind to a delivery
        target that exists, but under a different ``meta_mode``."""
        ws = self.make_workspace()
        with self.owner_engine.begin() as conn:
            upstream = _seed_curated_scenario_context(conn, ws, "controlled_mock")
            target_id = _seed_delivery_target(
                conn, ws, "controlled_mock", "scn_1", upstream,
            )
            guard_id = _seed_lineage_guard(
                conn, ws, "controlled_mock", "scn_1", target_id,
                upstream["meta_connection_id"], upstream["contract_generation_id"],
            )

            with self.assertRaises(IntegrityError), conn.begin_nested():
                _seed_enrollment_candidate(
                    conn, ws, "customer_sandbox", NO_SCENARIO,
                    target_id, guard_id,
                )

    def test_evidence_snapshot_rejects_delivery_target_from_another_workspace(self) -> None:
        """D4: ``fk_sdes_target``."""
        ws_a = self.make_workspace()
        ws_b = self.make_workspace()
        with self.owner_engine.begin() as conn:
            upstream = _seed_curated_scenario_context(conn, ws_a, "controlled_mock")
            target_id = _seed_delivery_target(
                conn, ws_a, "controlled_mock", "scn_1", upstream,
            )

            with self.assertRaises(IntegrityError), conn.begin_nested():
                _seed_evidence_snapshot(
                    conn, ws_b, "controlled_mock", "scn_1",
                    target_id, upstream["contract_generation_id"],
                )

    def test_activation_preview_rejects_enrollment_from_another_workspace(self) -> None:
        """``fk_saprev_enrollment``."""
        ws_a = self.make_workspace()
        ws_b = self.make_workspace()
        with self.owner_engine.begin() as conn:
            upstream = _seed_curated_scenario_context(conn, ws_a, "controlled_mock")
            target_id = _seed_delivery_target(
                conn, ws_a, "controlled_mock", "scn_1", upstream,
            )
            guard_id = _seed_lineage_guard(
                conn, ws_a, "controlled_mock", "scn_1", target_id,
                upstream["meta_connection_id"], upstream["contract_generation_id"],
            )
            snapshot_id = _seed_evidence_snapshot(
                conn, ws_a, "controlled_mock", "scn_1", target_id,
                upstream["contract_generation_id"],
            )
            enrollment_id = _seed_enrollment(conn, ws_b, "controlled_mock")

            with self.assertRaises(IntegrityError), conn.begin_nested():
                _seed_activation_preview(
                    conn, ws_b, "controlled_mock", "scn_1",
                    enrollment_id, snapshot_id, guard_id,
                )

    def test_activation_preview_rejects_evidence_snapshot_from_another_mode(self) -> None:
        """``fk_saprev_evidence``: same Workspace, snapshot's real mode
        (``controlled_mock``) differs from the preview row's declared mode."""
        ws = self.make_workspace()
        with self.owner_engine.begin() as conn:
            upstream = _seed_curated_scenario_context(conn, ws, "controlled_mock")
            target_id = _seed_delivery_target(
                conn, ws, "controlled_mock", "scn_1", upstream,
            )
            snapshot_id = _seed_evidence_snapshot(
                conn, ws, "controlled_mock", "scn_1", target_id,
                upstream["contract_generation_id"],
            )
            enrollment_id = _seed_enrollment(conn, ws, "customer_sandbox")

            with self.assertRaises(IntegrityError), conn.begin_nested():
                _seed_activation_preview(
                    conn, ws, "customer_sandbox", NO_SCENARIO,
                    enrollment_id, snapshot_id, f"slg_{uuid.uuid4().hex[:10]}",
                )

    def test_full_valid_chain_succeeds_across_all_six_tables(self) -> None:
        """Positive control (mirrors
        ``ScaleCompositeForeignKeyTests.test_episode_can_reference_its_own_enrollment``
        in ``tests/test_scale_migration.py``): every FK above is real, not
        just strict -- a fully consistent chain across all six tables is
        never rejected."""
        ws = self.make_workspace()
        with self.owner_engine.begin() as conn:
            upstream = _seed_curated_scenario_context(conn, ws, "controlled_mock")
            target_id = _seed_delivery_target(
                conn, ws, "controlled_mock", "scn_1", upstream,
            )
            guard_id = _seed_lineage_guard(
                conn, ws, "controlled_mock", "scn_1", target_id,
                upstream["meta_connection_id"], upstream["contract_generation_id"],
            )
            _seed_enrollment_candidate(
                conn, ws, "controlled_mock", "scn_1", target_id, guard_id,
            )
            snapshot_id = _seed_evidence_snapshot(
                conn, ws, "controlled_mock", "scn_1", target_id,
                upstream["contract_generation_id"],
            )
            enrollment_id = _seed_enrollment(conn, ws, "controlled_mock", scenario_scope_key="scn_1")
            _seed_activation_preview(
                conn, ws, "controlled_mock", "scn_1",
                enrollment_id, snapshot_id, guard_id,
            )  # must not raise


# ---------------------------------------------------------------------------
# D8/D9 + other CHECK constraints.
# ---------------------------------------------------------------------------


@pytest.mark.postgres
@unittest.skipUnless(CONFIGURED, SKIP_REASON)
class ScaleCheckConstraintTests(_ScaleDbIntegrityTestCase):
    def test_scenario_biconditional_rejects_controlled_mock_with_sentinel(self) -> None:
        """D8: ``controlled_mock`` + the ``__no_scenario__`` sentinel is
        illegal -- ``ck_scj_scenario_biconditional``."""
        ws = self.make_workspace()
        with self.owner_engine.begin() as conn:
            with self.assertRaises(IntegrityError), conn.begin_nested():
                conn.execute(
                    text(INSERT_COMMAND_JOURNAL),
                    dict(
                        id=f"scj_{uuid.uuid4().hex[:10]}", workspace_id=ws,
                        meta_mode="controlled_mock", scenario_scope_key=NO_SCENARIO,
                        command_type="create_candidate", dedup_key="dedup_1",
                        request_fingerprint="fp_1", result_resource_id="secand_1",
                        result_resource_type="candidate",
                    ),
                )

    def test_scenario_biconditional_rejects_non_mock_with_real_scenario(self) -> None:
        """D9: ``customer_sandbox`` (or ``internal_validation``) with a real
        scenario key is illegal."""
        ws = self.make_workspace()
        with self.owner_engine.begin() as conn:
            with self.assertRaises(IntegrityError), conn.begin_nested():
                conn.execute(
                    text(INSERT_COMMAND_JOURNAL),
                    dict(
                        id=f"scj_{uuid.uuid4().hex[:10]}", workspace_id=ws,
                        meta_mode="customer_sandbox", scenario_scope_key="scn_1",
                        command_type="create_candidate", dedup_key="dedup_1",
                        request_fingerprint="fp_1", result_resource_id="secand_1",
                        result_resource_type="candidate",
                    ),
                )

    def test_scenario_biconditional_accepts_correct_shape_for_both_modes(self) -> None:
        """Positive control for D8/D9: the legal shape in each mode is
        accepted without error."""
        ws = self.make_workspace()
        with self.owner_engine.begin() as conn:
            conn.execute(
                text(INSERT_COMMAND_JOURNAL),
                dict(
                    id=f"scj_{uuid.uuid4().hex[:10]}", workspace_id=ws,
                    meta_mode="controlled_mock", scenario_scope_key="scn_1",
                    command_type="create_candidate", dedup_key="dedup_mock",
                    request_fingerprint="fp_1", result_resource_id="secand_1",
                    result_resource_type="candidate",
                ),
            )
            conn.execute(
                text(INSERT_COMMAND_JOURNAL),
                dict(
                    id=f"scj_{uuid.uuid4().hex[:10]}", workspace_id=ws,
                    meta_mode="customer_sandbox", scenario_scope_key=NO_SCENARIO,
                    command_type="create_candidate", dedup_key="dedup_sandbox",
                    request_fingerprint="fp_2", result_resource_id="secand_2",
                    result_resource_type="candidate",
                ),
            )  # neither insert must raise

    def test_meta_mode_check_rejects_unknown_mode(self) -> None:
        """``ck_scj_meta_mode``: only the three canonical modes are legal."""
        ws = self.make_workspace()
        with self.owner_engine.begin() as conn:
            with self.assertRaises(IntegrityError), conn.begin_nested():
                conn.execute(
                    text(INSERT_COMMAND_JOURNAL),
                    dict(
                        id=f"scj_{uuid.uuid4().hex[:10]}", workspace_id=ws,
                        meta_mode="production", scenario_scope_key=NO_SCENARIO,
                        command_type="create_candidate", dedup_key="dedup_1",
                        request_fingerprint="fp_1", result_resource_id="secand_1",
                        result_resource_type="candidate",
                    ),
                )

    def test_command_type_check_rejects_unknown_command(self) -> None:
        """``ck_scj_command_type``."""
        ws = self.make_workspace()
        with self.owner_engine.begin() as conn:
            with self.assertRaises(IntegrityError), conn.begin_nested():
                conn.execute(
                    text(INSERT_COMMAND_JOURNAL),
                    dict(
                        id=f"scj_{uuid.uuid4().hex[:10]}", workspace_id=ws,
                        meta_mode="customer_sandbox", scenario_scope_key=NO_SCENARIO,
                        command_type="delete_everything", dedup_key="dedup_1",
                        request_fingerprint="fp_1", result_resource_id="secand_1",
                        result_resource_type="candidate",
                    ),
                )

    def test_source_type_biconditional_rejects_curated_scenario_with_sync_run(self) -> None:
        """``ck_sdt_source_type_biconditional``: ``curated_scenario`` must
        carry a ``scenario_version_id`` and nothing else; supplying an
        ``evidence_observation``-only field alongside it is illegal even
        though the row is otherwise well-formed."""
        ws = self.make_workspace()
        with self.owner_engine.begin() as conn:
            upstream = _seed_curated_scenario_context(conn, ws, "controlled_mock")

            with self.assertRaises(IntegrityError), conn.begin_nested():
                _seed_delivery_target(
                    conn, ws, "controlled_mock", "scn_1", upstream,
                    source_sync_run_id="sync_run_never_issued",
                )

    def test_source_type_mode_check_rejects_evidence_observation_in_sandbox(self) -> None:
        """``ck_sdt_source_type_mode``: ``evidence_observation`` is only
        legal under ``internal_validation``."""
        ws = self.make_workspace()
        with self.owner_engine.begin() as conn:
            upstream = _seed_curated_scenario_context(conn, ws, "customer_sandbox")

            with self.assertRaises(IntegrityError), conn.begin_nested():
                _seed_delivery_target(
                    conn, ws, "customer_sandbox", NO_SCENARIO, upstream,
                    source_type="evidence_observation",
                    scenario_version_id=None,
                    source_sync_run_id="sync_1",
                    source_publication_id="pub_1",
                    source_attestation_id="att_1",
                )

    def test_hierarchy_non_empty_rejects_blank_adset_remote_id(self) -> None:
        """``ck_sdt_hierarchy_non_empty``: NOT NULL alone does not stop an
        empty string masquerading as a partial remote hierarchy."""
        ws = self.make_workspace()
        with self.owner_engine.begin() as conn:
            upstream = _seed_curated_scenario_context(conn, ws, "controlled_mock")

            with self.assertRaises(IntegrityError), conn.begin_nested():
                _seed_delivery_target(
                    conn, ws, "controlled_mock", "scn_1", upstream,
                    adset_remote_id="",
                )

    def test_activation_preview_transition_kind_check(self) -> None:
        """``ck_saprev_transition_kind``: only ``activate``/``resume`` are
        legal ``transition_kind`` values."""
        ws = self.make_workspace()
        with self.owner_engine.begin() as conn:
            upstream = _seed_curated_scenario_context(conn, ws, "controlled_mock")
            target_id = _seed_delivery_target(
                conn, ws, "controlled_mock", "scn_1", upstream,
            )
            snapshot_id = _seed_evidence_snapshot(
                conn, ws, "controlled_mock", "scn_1", target_id,
                upstream["contract_generation_id"],
            )
            enrollment_id = _seed_enrollment(conn, ws, "controlled_mock", scenario_scope_key="scn_1")

            with self.assertRaises(IntegrityError), conn.begin_nested():
                _seed_activation_preview(
                    conn, ws, "controlled_mock", "scn_1",
                    enrollment_id, snapshot_id, f"slg_{uuid.uuid4().hex[:10]}",
                    transition_kind="reactivate",
                )

    def test_command_journal_rejects_duplicate_dedup_key(self) -> None:
        """``uq_scj_ws_mode_type_dedup``: the raw unique constraint itself,
        independent of ``claim_command()``'s ON CONFLICT handling."""
        ws = self.make_workspace()
        with self.owner_engine.begin() as conn:
            conn.execute(
                text(INSERT_COMMAND_JOURNAL),
                dict(
                    id=f"scj_{uuid.uuid4().hex[:10]}", workspace_id=ws,
                    meta_mode="customer_sandbox", scenario_scope_key=NO_SCENARIO,
                    command_type="create_candidate", dedup_key="dedup_dupe",
                    request_fingerprint="fp_1", result_resource_id="secand_1",
                    result_resource_type="candidate",
                ),
            )

            with self.assertRaises(IntegrityError), conn.begin_nested():
                conn.execute(
                    text(INSERT_COMMAND_JOURNAL),
                    dict(
                        id=f"scj_{uuid.uuid4().hex[:10]}", workspace_id=ws,
                        meta_mode="customer_sandbox", scenario_scope_key=NO_SCENARIO,
                        command_type="create_candidate", dedup_key="dedup_dupe",
                        request_fingerprint="fp_2", result_resource_id="secand_2",
                        result_resource_type="candidate",
                    ),
                )


# ---------------------------------------------------------------------------
# D5/D6/D16: RLS workspace and Meta-mode isolation on all six new tables.
# ---------------------------------------------------------------------------


@pytest.mark.postgres
@unittest.skipUnless(CONFIGURED, SKIP_REASON)
class ScaleNewTablesRlsIsolationTests(_ScaleDbIntegrityTestCase):
    """Each test builds one valid row in one of the six tables, then proves
    ``tenant_isolation`` (``marketai_meta_context_is_current(workspace_id,
    meta_mode)``, installed by ``ab1_scale_lineage_tables``) enforces both
    dimensions: a session scoped to the wrong Workspace sees nothing (D5),
    a session scoped to the right Workspace but the wrong Meta-mode sees
    nothing either (D6), and the matching scope sees exactly the row."""

    def _assert_isolated(self, table: str, row_id: str, ws_a: str, ws_b: str, meta_mode: str) -> None:
        select_sql = text(f"SELECT id FROM {table} WHERE id = :id")

        matching = self.scoped_connection(ws_a, meta_mode)
        self.assertEqual(
            [r[0] for r in matching.execute(select_sql, {"id": row_id})], [row_id],
        )

        wrong_workspace = self.scoped_connection(ws_b, meta_mode)
        self.assertEqual(
            list(wrong_workspace.execute(select_sql, {"id": row_id})), [],
        )

        other_mode = "customer_sandbox" if meta_mode != "customer_sandbox" else "controlled_mock"
        wrong_mode = self.scoped_connection(ws_a, other_mode)
        self.assertEqual(
            list(wrong_mode.execute(select_sql, {"id": row_id})), [],
        )

    def test_delivery_target_rls_isolation(self) -> None:
        ws_a, ws_b = self.make_workspace(), self.make_workspace()
        with self.owner_engine.begin() as conn:
            upstream = _seed_curated_scenario_context(conn, ws_a, "controlled_mock")
            target_id = _seed_delivery_target(
                conn, ws_a, "controlled_mock", "scn_1", upstream,
            )
        self._assert_isolated("scale_delivery_targets", target_id, ws_a, ws_b, "controlled_mock")

    def test_command_journal_rls_isolation(self) -> None:
        ws_a, ws_b = self.make_workspace(), self.make_workspace()
        journal_id = f"scj_{uuid.uuid4().hex[:10]}"
        with self.owner_engine.begin() as conn:
            conn.execute(
                text(INSERT_COMMAND_JOURNAL),
                dict(
                    id=journal_id, workspace_id=ws_a, meta_mode="controlled_mock",
                    scenario_scope_key="scn_1", command_type="create_candidate",
                    dedup_key="dedup_1", request_fingerprint="fp_1",
                    result_resource_id="secand_1", result_resource_type="candidate",
                ),
            )
        self._assert_isolated("scale_command_journal", journal_id, ws_a, ws_b, "controlled_mock")

    def test_lineage_guard_rls_isolation(self) -> None:
        ws_a, ws_b = self.make_workspace(), self.make_workspace()
        with self.owner_engine.begin() as conn:
            upstream = _seed_curated_scenario_context(conn, ws_a, "controlled_mock")
            target_id = _seed_delivery_target(
                conn, ws_a, "controlled_mock", "scn_1", upstream,
            )
            guard_id = _seed_lineage_guard(
                conn, ws_a, "controlled_mock", "scn_1", target_id,
                upstream["meta_connection_id"], upstream["contract_generation_id"],
            )
        self._assert_isolated("scale_lineage_guards", guard_id, ws_a, ws_b, "controlled_mock")

    def test_enrollment_candidate_rls_isolation(self) -> None:
        ws_a, ws_b = self.make_workspace(), self.make_workspace()
        with self.owner_engine.begin() as conn:
            upstream = _seed_curated_scenario_context(conn, ws_a, "controlled_mock")
            target_id = _seed_delivery_target(
                conn, ws_a, "controlled_mock", "scn_1", upstream,
            )
            guard_id = _seed_lineage_guard(
                conn, ws_a, "controlled_mock", "scn_1", target_id,
                upstream["meta_connection_id"], upstream["contract_generation_id"],
            )
            candidate_id = _seed_enrollment_candidate(
                conn, ws_a, "controlled_mock", "scn_1", target_id, guard_id,
            )
        self._assert_isolated(
            "scale_enrollment_candidates", candidate_id, ws_a, ws_b, "controlled_mock",
        )

    def test_evidence_snapshot_rls_isolation(self) -> None:
        ws_a, ws_b = self.make_workspace(), self.make_workspace()
        with self.owner_engine.begin() as conn:
            upstream = _seed_curated_scenario_context(conn, ws_a, "controlled_mock")
            target_id = _seed_delivery_target(
                conn, ws_a, "controlled_mock", "scn_1", upstream,
            )
            snapshot_id = _seed_evidence_snapshot(
                conn, ws_a, "controlled_mock", "scn_1", target_id,
                upstream["contract_generation_id"],
            )
        self._assert_isolated(
            "scale_delivery_evidence_snapshots", snapshot_id, ws_a, ws_b, "controlled_mock",
        )

    def test_activation_preview_rls_isolation(self) -> None:
        ws_a, ws_b = self.make_workspace(), self.make_workspace()
        with self.owner_engine.begin() as conn:
            upstream = _seed_curated_scenario_context(conn, ws_a, "controlled_mock")
            target_id = _seed_delivery_target(
                conn, ws_a, "controlled_mock", "scn_1", upstream,
            )
            guard_id = _seed_lineage_guard(
                conn, ws_a, "controlled_mock", "scn_1", target_id,
                upstream["meta_connection_id"], upstream["contract_generation_id"],
            )
            snapshot_id = _seed_evidence_snapshot(
                conn, ws_a, "controlled_mock", "scn_1", target_id,
                upstream["contract_generation_id"],
            )
            enrollment_id = _seed_enrollment(
                conn, ws_a, "controlled_mock", scenario_scope_key="scn_1",
            )
            preview_id = _seed_activation_preview(
                conn, ws_a, "controlled_mock", "scn_1",
                enrollment_id, snapshot_id, guard_id,
            )
        self._assert_isolated(
            "scale_activation_previews", preview_id, ws_a, ws_b, "controlled_mock",
        )


# ---------------------------------------------------------------------------
# D17: marketai_bump_scale_lineage_guards() trigger family.
# ---------------------------------------------------------------------------


@pytest.mark.postgres
@unittest.skipUnless(CONFIGURED, SKIP_REASON)
class ScaleLineageGuardBumpTriggerTests(_ScaleDbIntegrityTestCase):
    """Proves the SECURITY DEFINER trigger installed by
    ``ab1_scale_lineage_tables`` actually fires and bumps ``revision`` in
    the same transaction as the covered upstream mutation (spec §7.3/§7.4).

    Only two of the nine dispatch branches
    (``meta_connection_records``/``library_versions``, both AFTER UPDATE)
    are exercised here: every branch shares the exact same tested trigger
    function and dispatch mechanism (``TG_TABLE_NAME``-based branching, see
    the migration's ``_BUMP_FUNCTION_SQL``), and the other seven
    (``experiment_decisions``, ``decision_confirmations``,
    ``experiment_eligibility_events``, ``quality_reviews``,
    ``market_asset_bindings``, ``meta_connection_records``,
    ``evidence_attestation_records``) each require reproducing a full
    Experiment/Judgement/Localization pipeline this task does not own --
    the same scope boundary ``ScaleLineageGuardRecord``'s own docstring
    already draws for its deferred FKs.
    """

    def _seed_guard(self, ws: str, conn) -> tuple[str, dict, str]:
        upstream = _seed_curated_scenario_context(conn, ws, "controlled_mock")
        target_id = _seed_delivery_target(conn, ws, "controlled_mock", "scn_1", upstream)
        guard_id = _seed_lineage_guard(
            conn, ws, "controlled_mock", "scn_1", target_id,
            upstream["meta_connection_id"], upstream["contract_generation_id"],
        )
        return guard_id, upstream, target_id

    def _guard_revision(self, conn, guard_id: str) -> int:
        row = conn.execute(
            text("SELECT revision FROM scale_lineage_guards WHERE id = :id"),
            {"id": guard_id},
        ).fetchone()
        return row[0]

    def test_bump_on_meta_connection_update(self) -> None:
        ws = self.make_workspace()
        with self.owner_engine.begin() as conn:
            guard_id, upstream, _ = self._seed_guard(ws, conn)
            self.assertEqual(self._guard_revision(conn, guard_id), 1)

            conn.execute(
                text("UPDATE meta_connection_records SET updated_at = now() WHERE id = :id"),
                {"id": upstream["meta_connection_id"]},
            )

            self.assertEqual(self._guard_revision(conn, guard_id), 2)

    def test_bump_on_library_version_update(self) -> None:
        ws = self.make_workspace()
        with self.owner_engine.begin() as conn:
            upstream = _seed_curated_scenario_context(conn, ws, "controlled_mock")
            target_id = _seed_delivery_target(conn, ws, "controlled_mock", "scn_1", upstream)
            offer_version_id = _seed_library_version(conn, ws, "offer")
            guard_id = _seed_lineage_guard(
                conn, ws, "controlled_mock", "scn_1", target_id,
                upstream["meta_connection_id"], upstream["contract_generation_id"],
                offer_version_id=offer_version_id,
            )
            self.assertEqual(self._guard_revision(conn, guard_id), 1)

            conn.execute(
                text(
                    "UPDATE library_versions SET lifecycle_revision = lifecycle_revision + 1 "
                    "WHERE id = :id"
                ),
                {"id": offer_version_id},
            )

            self.assertEqual(self._guard_revision(conn, guard_id), 2)

    def test_bump_does_not_affect_an_unrelated_guard(self) -> None:
        """Negative control: the trigger's ``WHERE`` clause is scoped to
        the exact matching guard(s), not every row in the table."""
        ws = self.make_workspace()
        with self.owner_engine.begin() as conn:
            guard_id, _upstream, _target_id = self._seed_guard(ws, conn)
            unrelated_connection_id = _seed_connection(conn, ws, "customer_sandbox")

            conn.execute(
                text("UPDATE meta_connection_records SET updated_at = now() WHERE id = :id"),
                {"id": unrelated_connection_id},
            )

            self.assertEqual(self._guard_revision(conn, guard_id), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
