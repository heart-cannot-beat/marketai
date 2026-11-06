"""Migration-level constraint tests for the Scale domain (Issue #25, Task 6).

Two layers:

* dialect-portable composite-foreign-key tests, using an in-memory/temp-file
  SQLite database built via ``Base.metadata.create_all`` -- these run in
  every environment, including a clean ``make verify`` checkout;
* ``@pytest.mark.postgres`` tests requiring a real PostgreSQL cluster
  migrated to head (RLS, partial unique indexes, immutability/sealing
  triggers). These follow the proven pattern in ``tests/test_postgres_rls.py``
  and ``tests/test_meta_rls_isolation.py`` -- connect as the real
  least-privilege roles, sign the tenant context by hand -- rather than the
  ``pg_session`` fixture referenced by ``tests/test_meta_tenant_isolation.py``,
  which does not exist anywhere in this repository (see that file's sibling
  ``test_meta_rls_isolation.py`` docstring) and would error at fixture
  resolution if its tests were ever un-skipped.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import tempfile
import unittest
import uuid
from datetime import date
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import WorkspaceRecord, new_id
from app.scale.models import (
    ScaleEnrollmentEventRecord,
    ScaleEnrollmentRecord,
    ScaleEpisodeRecord,
    ScaleTargetLifecycleEventRecord,
    ScaleTargetVersionRecord,
)


def _make_enrollment(workspace_id: str, meta_mode: str, **overrides) -> ScaleEnrollmentRecord:
    fields = dict(
        workspace_id=workspace_id,
        meta_mode=meta_mode,
        meta_connection_id="mcon_1",
        scenario_scope_key="__no_scenario__" if meta_mode != "controlled_mock" else "scenario_1",
        ad_account_id="act_1",
        winner_decision_id="wd_1",
        winner_confirmation_id="wc_1",
        asset_version_id="av_1",
        product_version_id="pv_1",
        offer_version_id="ov_1",
        market_code="US",
        remote_binding_key="mcon_1/act_1/camp_1",
        creator_id="user_1",
        original_initiator_id="user_1",
    )
    fields.update(overrides)
    return ScaleEnrollmentRecord(**fields)


def _make_episode(workspace_id: str, meta_mode: str, enrollment_id: str, **overrides) -> ScaleEpisodeRecord:
    fields = dict(
        workspace_id=workspace_id,
        meta_mode=meta_mode,
        enrollment_id=enrollment_id,
        winner_decision_id="wd_1",
        winner_confirmation_id="wc_1",
        asset_version_id="av_1",
        product_version_id="pv_1",
        offer_version_id="ov_1",
        market_code="US",
        meta_connection_id="mcon_1",
        scenario_scope_key="__no_scenario__" if meta_mode != "controlled_mock" else "scenario_1",
        ad_account_id="act_1",
        delivery_config_baseline_source_id="sdcs_1",
        delivery_evidence_hash="e" * 64,
        delivery_comparability_hash="c" * 64,
        metric_semantic_contract_set_id="smcs_1",
        timezone_name="America/Los_Angeles",
        currency="USD",
        attribution_semantics_hash="a" * 64,
        started_local_day=date(2026, 1, 1),
    )
    fields.update(overrides)
    return ScaleEpisodeRecord(**fields)


# ---------------------------------------------------------------------------
# Dialect-portable composite foreign key tests (SQLite, no PostgreSQL needed)
# ---------------------------------------------------------------------------


class ScaleCompositeForeignKeyTests(unittest.TestCase):
    """Composite (workspace_id[, meta_mode], id) foreign keys added by the
    Task 6 migration (mirrored onto app/scale/models.py __table_args__ so
    ``alembic check`` sees no drift) reject cross-Workspace and cross-mode
    references at the database layer -- portable to SQLite via Alembic/
    SQLAlchemy batch mode, so these run without a PostgreSQL cluster.
    """

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="marketai-scale-migration-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self.engine = create_engine(
            f"sqlite:///{self.tmp_dir / 'scale.db'}",
            connect_args={"check_same_thread": False},
        )
        self.addCleanup(self.engine.dispose)
        with self.engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        Base.metadata.create_all(
            bind=self.engine,
            tables=[
                WorkspaceRecord.__table__,
                ScaleEnrollmentRecord.__table__,
                ScaleEnrollmentEventRecord.__table__,
                ScaleEpisodeRecord.__table__,
                ScaleTargetVersionRecord.__table__,
                ScaleTargetLifecycleEventRecord.__table__,
            ],
        )
        self.session = Session(bind=self.engine)
        self.addCleanup(self.session.close)
        # SQLite enforces FKs per-connection; the session's own connection
        # needs the pragma too (the one above was a throwaway connection).
        self.session.execute(text("PRAGMA foreign_keys=ON"))

        self.ws_a = WorkspaceRecord(id=new_id("ws"), name="A")
        self.ws_b = WorkspaceRecord(id=new_id("ws"), name="B")
        self.session.add_all([self.ws_a, self.ws_b])
        self.session.flush()

    def test_enrollment_event_cannot_reference_enrollment_from_another_workspace(self) -> None:
        enrollment = _make_enrollment(self.ws_a.id, "customer_sandbox")
        self.session.add(enrollment)
        self.session.flush()

        # fk_sene_enrollment: (workspace_id, meta_mode, enrollment_id) must
        # resolve inside scale_enrollments -- a smuggled Workspace fails.
        event = ScaleEnrollmentEventRecord(
            enrollment_id=enrollment.id,
            workspace_id=self.ws_b.id,
            meta_mode="customer_sandbox",
            sequence=1,
            event_type="create",
            current_lifecycle="draft",
            deduplication_key="dedup_1",
            executor_id="user_1",
            original_initiator_id="user_1",
            revision_at_event=1,
        )
        self.session.add(event)
        with self.assertRaises(IntegrityError):
            self.session.flush()
        self.session.rollback()

    def test_enrollment_event_cannot_reference_enrollment_from_another_mode(self) -> None:
        enrollment = _make_enrollment(self.ws_a.id, "customer_sandbox")
        self.session.add(enrollment)
        self.session.flush()

        # Same Workspace, wrong meta_mode -- the composite FK still fails
        # because it does not match on workspace_id alone.
        event = ScaleEnrollmentEventRecord(
            enrollment_id=enrollment.id,
            workspace_id=self.ws_a.id,
            meta_mode="controlled_mock",
            sequence=1,
            event_type="create",
            current_lifecycle="draft",
            deduplication_key="dedup_1",
            executor_id="user_1",
            original_initiator_id="user_1",
            revision_at_event=1,
        )
        self.session.add(event)
        with self.assertRaises(IntegrityError):
            self.session.flush()
        self.session.rollback()

    def test_episode_cannot_reference_enrollment_from_another_workspace(self) -> None:
        enrollment = _make_enrollment(self.ws_a.id, "customer_sandbox")
        self.session.add(enrollment)
        self.session.flush()

        # fk_sep_enrollment
        episode = _make_episode(self.ws_b.id, "customer_sandbox", enrollment.id)
        self.session.add(episode)
        with self.assertRaises(IntegrityError):
            self.session.flush()
        self.session.rollback()

    def test_target_lifecycle_event_cannot_reference_target_from_another_workspace(self) -> None:
        target = ScaleTargetVersionRecord(
            workspace_id=self.ws_a.id,
            product_version_id="pv_1",
            offer_version_id="ov_1",
            market_code="US",
            currency="USD",
            target_cpa="10.00",
            creator_id="user_1",
        )
        self.session.add(target)
        self.session.flush()

        # fk_stge_target_version is mode-independent: (workspace_id,
        # target_version_id) -> scale_target_versions(workspace_id, id).
        event = ScaleTargetLifecycleEventRecord(
            target_version_id=target.id,
            workspace_id=self.ws_b.id,
            event_type="publish",
            current_lifecycle_state="active",
            semantic_event_sequence=1,
            executor_id="user_1",
            original_initiator_id="user_1",
            revision_at_event=1,
        )
        self.session.add(event)
        with self.assertRaises(IntegrityError):
            self.session.flush()
        self.session.rollback()

    def test_episode_can_reference_its_own_enrollment(self) -> None:
        """Positive control: the composite FK does not reject legitimate rows."""
        enrollment = _make_enrollment(self.ws_a.id, "customer_sandbox")
        self.session.add(enrollment)
        self.session.flush()

        episode = _make_episode(self.ws_a.id, "customer_sandbox", enrollment.id)
        self.session.add(episode)
        self.session.flush()  # must not raise

        self.assertEqual(episode.enrollment_id, enrollment.id)


# ---------------------------------------------------------------------------
# PostgreSQL-only tests: RLS, partial unique indexes, immutability triggers
# ---------------------------------------------------------------------------

OWNER_URL = os.environ.get("DOMAIN_OWNER_DATABASE_URL")
API_URL = os.environ.get("DOMAIN_API_DATABASE_URL")
CONTEXT_SECRET = os.environ.get("MARKETAI_TENANT_CONTEXT_SECRET")

CONFIGURED = all((OWNER_URL, API_URL, CONTEXT_SECRET))

SKIP_REASON = (
    "set DOMAIN_OWNER_DATABASE_URL, DOMAIN_API_DATABASE_URL and "
    "MARKETAI_TENANT_CONTEXT_SECRET against a cluster migrated to head with "
    "deploy/postgres/domain-tenant-roles.sql applied"
)

INSERT_WORKSPACE = """
INSERT INTO workspaces (id, name, default_currency, lifecycle, budget_currency, created_at)
VALUES (:id, :id, 'USD', 'active', 'USD', now())
ON CONFLICT (id) DO NOTHING
"""

INSERT_CONTRACT_SET = """
INSERT INTO scale_metric_semantic_contract_sets
    (id, workspace_id, meta_mode, description, created_at, created_by)
VALUES (:id, :workspace_id, :meta_mode, '', now(), 'test')
"""

INSERT_ENROLLMENT = """
INSERT INTO scale_enrollments
    (id, workspace_id, meta_mode, meta_connection_id, scenario_scope_key,
     ad_account_id, winner_decision_id, winner_confirmation_id, asset_version_id,
     product_version_id, offer_version_id, market_code, remote_binding_key,
     lifecycle, creator_id, original_initiator_id, revision,
     evaluation_cancellation_generation, created_at, updated_at)
VALUES
    (:id, :workspace_id, :meta_mode, :meta_connection_id, '__no_scenario__',
     :ad_account_id, 'wd_1', 'wc_1', 'av_1', 'pv_1', 'ov_1', 'US',
     :remote_binding_key, :lifecycle, 'user_1', 'user_1', 1, 0, now(), now())
"""

INSERT_EPISODE = """
INSERT INTO scale_episodes
    (id, workspace_id, meta_mode, enrollment_id, winner_decision_id,
     winner_confirmation_id, asset_version_id, product_version_id, offer_version_id,
     market_code, meta_connection_id, scenario_scope_key, ad_account_id,
     delivery_config_baseline_source_id, delivery_evidence_hash,
     delivery_comparability_hash, metric_semantic_contract_set_id, timezone_name,
     currency, attribution_semantics_hash, phase, started_at, started_local_day,
     revision, projection_revision, projection_source_revision,
     projection_semantic_event_sequence, projection_lineage_generation)
VALUES
    (:id, :workspace_id, :meta_mode, :enrollment_id, 'wd_1', 'wc_1', 'av_1', 'pv_1',
     'ov_1', 'US', :meta_connection_id, '__no_scenario__', :ad_account_id, 'sdcs_1',
     :hash1, :hash2, :contract_set_id, 'America/Los_Angeles', 'USD', :hash3,
     'collecting', now(), '2026-01-01', 1, 0, 0, 0, 1)
"""

INSERT_SNAPSHOT_UNSEALED = """
INSERT INTO scale_evidence_snapshots
    (id, workspace_id, meta_mode, meta_connection_id, scenario_scope_key,
     ad_account_id, episode_id, remote_binding_key, claim_hash, source_manifest_id,
     source_manifest_hash, source_revision, safe_watermark, policy_version_id,
     target_version_id, normalized_target_key, algorithm_version,
     canonical_schema_version, metric_semantic_contract_set_id,
     metric_semantic_contract_set_hash, delivery_config_source_id,
     delivery_evidence_hash, delivery_comparability_hash, timezone_name, currency,
     attribution_semantics_hash, current_window_start_local_day,
     current_window_end_local_day, baseline_window_start_local_day,
     baseline_window_end_local_day, rolling_window_start_local_day,
     rolling_window_end_local_day, complete_local_days_in_episode,
     active_delivery_days_in_episode, data_continuity_generation,
     prior_streak_absence_reason, last_complete_local_day, last_sync_at,
     freshness_reference_at, as_of_local_day, canonical_hash, sealed_at, created_at)
VALUES
    (:id, :workspace_id, :meta_mode, 'mcon_1', '__no_scenario__', 'act_1',
     :episode_id, 'mcon_1/act_1/camp_1', :claim_hash, 'sesm_1', :h64, 1,
     '2026-01-07', 'spol_1', NULL, '__no_target__', '1.0.0', '1.0.0', :contract_set_id,
     :h64, 'sdcs_1', :h64, :h64, 'America/Los_Angeles', 'USD', :h64,
     '2026-01-01', '2026-01-07', '2025-12-25', '2025-12-31', '2025-12-18',
     '2026-01-07', 7, 7, 'gen_1', 'no_earlier_distinct_day', '2026-01-07', now(),
     now(), '2026-01-07', NULL, NULL, now())
"""

INSERT_EVALUATION = """
INSERT INTO scale_evaluations
    (id, workspace_id, meta_mode, episode_id, snapshot_id, snapshot_canonical_hash,
     metric_semantic_contract_set_hash, policy_version_id, normalized_target_key,
     algorithm_version, canonical_schema_version, maturity, data_quality_state,
     health_reason, streak_compat_policy_version_id,
     streak_compat_metric_contract_set_hash,
     streak_compat_configuration_baseline_hash,
     streak_compat_data_continuity_generation, streak_compat_canonical_hash,
     streak_healthy_evaluations, canonical_result_payload, result_hash, created_at)
VALUES
    (:id, :workspace_id, :meta_mode, :episode_id, :snapshot_id, :hash, :hash,
     'spol_1', '__no_target__', '1.0.0', '1.0.0', 'monitorable', 'pass', 'ok',
     'spol_1', :hash, :hash, 'gen_1', :hash, 0, '{}', :hash, now())
"""


@unittest.skipUnless(CONFIGURED, SKIP_REASON)
@pytest.mark.postgres
class ScalePostgresConstraintTests(unittest.TestCase):
    """RLS, partial unique indexes, and immutability triggers -- require a
    real PostgreSQL cluster provisioned per ``deploy/postgres/domain-tenant-
    roles.sql`` and migrated to head. Skips (and, doubled, the ``postgres``
    marker is also skipped by ``tests/conftest.py``) on a SQLite-only
    checkout so ``make verify`` stays clean; CI supplies the cluster.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from sqlalchemy import create_engine, text

        cls.create_engine = staticmethod(create_engine)
        cls.text = staticmethod(text)
        cls.owner = create_engine(OWNER_URL, future=True)
        cls.workspace = f"ws_scale_{uuid.uuid4().hex[:8]}"
        with cls.owner.begin() as conn:
            conn.execute(text(INSERT_WORKSPACE), {"id": cls.workspace})

    @classmethod
    def tearDownClass(cls) -> None:
        with cls.owner.begin() as conn:
            for table in (
                "scale_evaluations", "scale_evidence_snapshots", "scale_episodes",
                "scale_enrollments", "scale_metric_semantic_contract_sets",
            ):
                conn.execute(
                    cls.text(f"DELETE FROM {table} WHERE workspace_id = :ws"),
                    {"ws": cls.workspace},
                )
            conn.execute(cls.text("DELETE FROM workspaces WHERE id = :ws"), {"ws": cls.workspace})
        cls.owner.dispose()

    def engine(self, url: str = API_URL):
        engine = self.create_engine(url, future=True)
        self.addCleanup(engine.dispose)
        return engine

    def scoped_connection(self, workspace: str, meta_mode: str | None = None):
        conn = self.engine().connect()
        self.addCleanup(conn.close)
        transaction = conn.begin()
        self.addCleanup(lambda: transaction.rollback() if transaction.is_active else None)
        self._set_scope(conn, workspace, meta_mode)
        return conn

    def _set_scope(self, connection, workspace: str, meta_mode: str | None) -> None:
        from app.platform.tenancy import TenantContext, tenant_context_signature

        context = TenantContext(
            workspace_id=workspace,
            role="owner",
            initiator_id="scale-migration-test",
            authorization_epoch=0,
            meta_mode=meta_mode,
        )
        connection.execute(
            self.text("SELECT set_config('marketai.workspace_id', :w, true)"),
            {"w": workspace},
        )
        connection.execute(
            self.text("SELECT set_config('marketai.authorization_epoch', '0', true)")
        )
        connection.execute(
            self.text("SELECT set_config('marketai.meta_mode', :m, true)"),
            {"m": meta_mode or ""},
        )
        connection.execute(
            self.text(
                "SELECT set_config('marketai.context_signature', :signature, true)"
            ),
            {"signature": tenant_context_signature(context, CONTEXT_SECRET)},
        )

    # -------------------------------------------------------------- RLS ----

    def _cleanup_workspace(self, workspace: str) -> None:
        with self.owner.begin() as conn:
            conn.execute(
                self.text("DELETE FROM scale_metric_semantic_contract_sets WHERE workspace_id = :ws"),
                {"ws": workspace},
            )
            conn.execute(self.text("DELETE FROM workspaces WHERE id = :ws"), {"ws": workspace})

    def test_cross_workspace_read_blocked_by_rls(self) -> None:
        other_workspace = f"ws_scale_other_{uuid.uuid4().hex[:8]}"
        contract_set_id = f"smcs_{uuid.uuid4().hex[:8]}"
        with self.owner.begin() as conn:
            conn.execute(self.text(INSERT_WORKSPACE), {"id": other_workspace})
            conn.execute(
                self.text(INSERT_CONTRACT_SET),
                {"id": contract_set_id, "workspace_id": other_workspace, "meta_mode": "customer_sandbox"},
            )
        self.addCleanup(self._cleanup_workspace, other_workspace)

        conn = self.scoped_connection(self.workspace, "customer_sandbox")
        rows = conn.execute(
            self.text(
                "SELECT id FROM scale_metric_semantic_contract_sets WHERE id = :id"
            ),
            {"id": contract_set_id},
        ).fetchall()
        self.assertEqual(rows, [])

    def test_cross_mode_read_blocked_by_rls(self) -> None:
        contract_set_id = f"smcs_{uuid.uuid4().hex[:8]}"
        with self.owner.begin() as conn:
            conn.execute(
                self.text(INSERT_CONTRACT_SET),
                {"id": contract_set_id, "workspace_id": self.workspace, "meta_mode": "customer_sandbox"},
            )

        # Same Workspace, wrong mode -- customer_sandbox rows must stay
        # invisible to a controlled_mock session.
        conn = self.scoped_connection(self.workspace, "controlled_mock")
        rows = conn.execute(
            self.text(
                "SELECT id FROM scale_metric_semantic_contract_sets WHERE id = :id"
            ),
            {"id": contract_set_id},
        ).fetchall()
        self.assertEqual(rows, [])

        # Positive control: the right mode sees it.
        matching = self.scoped_connection(self.workspace, "customer_sandbox")
        rows = matching.execute(
            self.text(
                "SELECT id FROM scale_metric_semantic_contract_sets WHERE id = :id"
            ),
            {"id": contract_set_id},
        ).fetchall()
        self.assertEqual([r[0] for r in rows], [contract_set_id])

    # ---------------------------------------------- partial unique index ----

    def test_partial_unique_index_enforces_binding_reservation(self) -> None:
        from sqlalchemy.exc import IntegrityError

        first_id = f"senr_{uuid.uuid4().hex[:8]}"
        with self.owner.begin() as conn:
            conn.execute(
                self.text(INSERT_ENROLLMENT),
                {
                    "id": first_id,
                    "workspace_id": self.workspace,
                    "meta_mode": "customer_sandbox",
                    "meta_connection_id": "mcon_binding_test",
                    "ad_account_id": "act_binding_test",
                    "remote_binding_key": "mcon_binding_test/act_binding_test/camp_1",
                    "lifecycle": "active",
                },
            )

        second_id = f"senr_{uuid.uuid4().hex[:8]}"
        with self.assertRaises(IntegrityError):
            with self.owner.begin() as conn:
                conn.execute(
                    self.text(INSERT_ENROLLMENT),
                    {
                        "id": second_id,
                        "workspace_id": self.workspace,
                        "meta_mode": "customer_sandbox",
                        "meta_connection_id": "mcon_binding_test",
                        "ad_account_id": "act_binding_test",
                        "remote_binding_key": "mcon_binding_test/act_binding_test/camp_1",
                        # 'paused' is also reserved by ix_senr_active_binding.
                        "lifecycle": "paused",
                    },
                )

    # ---------------------------------------------- immutability triggers ----

    def _seed_episode(self, suffix: str) -> tuple[str, str, str]:
        """Returns (enrollment_id, episode_id, contract_set_id)."""
        enrollment_id = f"senr_{suffix}"
        episode_id = f"sep_{suffix}"
        contract_set_id = f"smcs_{suffix}"
        with self.owner.begin() as conn:
            conn.execute(
                self.text(INSERT_CONTRACT_SET),
                {"id": contract_set_id, "workspace_id": self.workspace, "meta_mode": "customer_sandbox"},
            )
            conn.execute(
                self.text(INSERT_ENROLLMENT),
                {
                    "id": enrollment_id,
                    "workspace_id": self.workspace,
                    "meta_mode": "customer_sandbox",
                    "meta_connection_id": f"mcon_{suffix}",
                    "ad_account_id": f"act_{suffix}",
                    "remote_binding_key": f"mcon_{suffix}/act_{suffix}/camp_1",
                    "lifecycle": "active",
                },
            )
            conn.execute(
                self.text(INSERT_EPISODE),
                {
                    "id": episode_id,
                    "workspace_id": self.workspace,
                    "meta_mode": "customer_sandbox",
                    "enrollment_id": enrollment_id,
                    "meta_connection_id": f"mcon_{suffix}",
                    "ad_account_id": f"act_{suffix}",
                    "contract_set_id": contract_set_id,
                    "hash1": "e" * 64,
                    "hash2": "c" * 64,
                    "hash3": "a" * 64,
                },
            )
        return enrollment_id, episode_id, contract_set_id

    def test_immutability_trigger_rejects_update_on_sealed_snapshot(self) -> None:
        from sqlalchemy.exc import DBAPIError

        suffix = uuid.uuid4().hex[:8]
        _, episode_id, contract_set_id = self._seed_episode(suffix)
        snapshot_id = f"sesn_{suffix}"

        with self.owner.begin() as conn:
            conn.execute(
                self.text(INSERT_SNAPSHOT_UNSEALED),
                {
                    "id": snapshot_id,
                    "workspace_id": self.workspace,
                    "meta_mode": "customer_sandbox",
                    "episode_id": episode_id,
                    "claim_hash": f"claim_{suffix}",
                    "contract_set_id": contract_set_id,
                    "h64": "h" * 64,
                },
            )
            # First transition: unsealed -> sealed is legal.
            conn.execute(
                self.text(
                    "UPDATE scale_evidence_snapshots SET canonical_hash = :hash, "
                    "sealed_at = now() WHERE id = :id"
                ),
                {"id": snapshot_id, "hash": "s" * 64},
            )

        with self.assertRaises(DBAPIError):
            with self.owner.begin() as conn:
                conn.execute(
                    self.text(
                        "UPDATE scale_evidence_snapshots SET canonical_hash = :hash "
                        "WHERE id = :id"
                    ),
                    {"id": snapshot_id, "hash": "t" * 64},
                )

    def test_immutability_trigger_rejects_delete_on_evaluation(self) -> None:
        from sqlalchemy.exc import DBAPIError

        suffix = uuid.uuid4().hex[:8]
        _, episode_id, _ = self._seed_episode(suffix)
        evaluation_id = f"sev_{suffix}"

        with self.owner.begin() as conn:
            conn.execute(
                self.text(INSERT_EVALUATION),
                {
                    "id": evaluation_id,
                    "workspace_id": self.workspace,
                    "meta_mode": "customer_sandbox",
                    "episode_id": episode_id,
                    "snapshot_id": f"sesn_missing_{suffix}",
                    "hash": "e" * 64,
                },
            )

        with self.assertRaises(DBAPIError):
            with self.owner.begin() as conn:
                conn.execute(
                    self.text("DELETE FROM scale_evaluations WHERE id = :id"),
                    {"id": evaluation_id},
                )


# ---------------------------------------------------------------------------
# D18: enrollment lineage backfill (Task 12,
# ac1_scale_enrollment_lineage_columns.py) -- legacy Enrollment rows cannot
# be proven during backfill; the migration marks them, it never fabricates
# lineage.
# ---------------------------------------------------------------------------


def _load_ac1_migration():
    """Load the ac1 migration module by file path (``migrations/`` has no
    ``__init__.py``, so it is not an importable package) and run its actual
    ``upgrade``/``downgrade`` against a real connection, rather than
    reimplementing its SQL in the test."""
    path = (
        Path(__file__).resolve().parent.parent
        / "migrations" / "versions" / "ac1_scale_enrollment_lineage_columns.py"
    )
    spec = importlib.util.spec_from_file_location(
        "ac1_scale_enrollment_lineage_columns_under_test", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(CONFIGURED, SKIP_REASON)
@pytest.mark.postgres
class ScaleEnrollmentLineageBackfillTests(unittest.TestCase):
    """D18 (spec 11.7): ``ac1_scale_enrollment_lineage_columns``'s backfill
    marks a legacy Enrollment row -- one with no ``enrollment_candidate_id``,
    predating the Task 9 candidate-gated creation path -- with the stable
    ``legacy_scale_lineage_unverifiable`` diagnostic instead of fabricating a
    candidate/guard/delivery-target identity, and leaves an already-lineaged
    (candidate-gated) row untouched.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.owner = create_engine(OWNER_URL, future=True)
        cls.workspace = f"ws_scale_lineage_bf_{uuid.uuid4().hex[:8]}"
        with cls.owner.begin() as conn:
            conn.execute(text(INSERT_WORKSPACE), {"id": cls.workspace})

    @classmethod
    def tearDownClass(cls) -> None:
        with cls.owner.begin() as conn:
            conn.execute(
                text("DELETE FROM scale_enrollments WHERE workspace_id = :ws"),
                {"ws": cls.workspace},
            )
            conn.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": cls.workspace})
        cls.owner.dispose()

    def _insert_enrollment(
        self, enrollment_id: str, *, candidate_id: str | None = None, fingerprint: str | None = None
    ) -> None:
        with self.owner.begin() as conn:
            conn.execute(
                text(INSERT_ENROLLMENT),
                {
                    "id": enrollment_id,
                    "workspace_id": self.workspace,
                    "meta_mode": "customer_sandbox",
                    "meta_connection_id": f"mcon_{enrollment_id}",
                    "ad_account_id": f"act_{enrollment_id}",
                    "remote_binding_key": f"mcon_{enrollment_id}/act_{enrollment_id}/camp_1",
                    "lifecycle": "draft",
                },
            )
            if candidate_id is not None or fingerprint is not None:
                conn.execute(
                    text(
                        "UPDATE scale_enrollments SET enrollment_candidate_id = :cand, "
                        "candidate_fingerprint = :fp WHERE id = :id"
                    ),
                    {"cand": candidate_id, "fp": fingerprint, "id": enrollment_id},
                )

    def _fetch(self, enrollment_id: str):
        with self.owner.begin() as conn:
            return conn.execute(
                text(
                    "SELECT enrollment_candidate_id, delivery_target_id, lineage_guard_id, "
                    "guard_revision_at_draft, candidate_fingerprint FROM scale_enrollments "
                    "WHERE id = :id"
                ),
                {"id": enrollment_id},
            ).mappings().first()

    def _run_upgrade(self, module) -> None:
        with self.owner.begin() as conn:
            migration_context = MigrationContext.configure(conn)
            with Operations.context(migration_context):
                module.upgrade()

    def _run_downgrade(self, module) -> None:
        with self.owner.begin() as conn:
            migration_context = MigrationContext.configure(conn)
            with Operations.context(migration_context):
                module.downgrade()

    def test_expand_backfill_contract_enrollment_lineage(self) -> None:
        """D18: a legacy Enrollment cannot be proven during backfill -- it
        gets the diagnostic marker, never a fabricated binding. A row that
        already carries real candidate-gated lineage is left untouched."""
        suffix = uuid.uuid4().hex[:8]
        legacy_id = f"senr_legacy_{suffix}"
        gated_id = f"senr_gated_{suffix}"

        # Legacy: predates the Task 9 candidate-gated path -- no lineage
        # columns populated at all.
        self._insert_enrollment(legacy_id)
        # Already candidate-gated: has real lineage from the Task 9+
        # creation path already -- the backfill must not touch it.
        self._insert_enrollment(
            gated_id, candidate_id=f"secan_{suffix}", fingerprint=f"fp_{suffix}"
        )

        module = _load_ac1_migration()
        self._run_upgrade(module)

        legacy_row = self._fetch(legacy_id)
        self.assertEqual(
            legacy_row["candidate_fingerprint"], "legacy_scale_lineage_unverifiable"
        )
        # Never fabricated: every other lineage column stays NULL.
        self.assertIsNone(legacy_row["enrollment_candidate_id"])
        self.assertIsNone(legacy_row["delivery_target_id"])
        self.assertIsNone(legacy_row["lineage_guard_id"])
        self.assertIsNone(legacy_row["guard_revision_at_draft"])

        gated_row = self._fetch(gated_id)
        self.assertEqual(gated_row["enrollment_candidate_id"], f"secan_{suffix}")
        self.assertEqual(gated_row["candidate_fingerprint"], f"fp_{suffix}")

        # Idempotent: running the backfill again changes nothing further.
        self._run_upgrade(module)
        legacy_row_again = self._fetch(legacy_id)
        self.assertEqual(
            legacy_row_again["candidate_fingerprint"], "legacy_scale_lineage_unverifiable"
        )

    def test_downgrade_reverts_only_the_backfilled_diagnostic(self) -> None:
        suffix = uuid.uuid4().hex[:8]
        legacy_id = f"senr_legacy_dg_{suffix}"
        gated_id = f"senr_gated_dg_{suffix}"

        self._insert_enrollment(legacy_id)
        self._insert_enrollment(
            gated_id, candidate_id=f"secan_{suffix}", fingerprint=f"fp_{suffix}"
        )

        module = _load_ac1_migration()
        self._run_upgrade(module)
        self.assertEqual(
            self._fetch(legacy_id)["candidate_fingerprint"], "legacy_scale_lineage_unverifiable"
        )

        self._run_downgrade(module)

        self.assertIsNone(self._fetch(legacy_id)["candidate_fingerprint"])
        # A genuinely candidate-gated row's fingerprint is never touched by
        # either direction of this migration.
        self.assertEqual(self._fetch(gated_id)["candidate_fingerprint"], f"fp_{suffix}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
