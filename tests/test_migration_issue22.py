"""Issue #22 migration tests: upgrade, schema verification, backfill and downgrade.

Tests are prefixed MU (upgrade), MD (downgrade) and MC (constraint) per the
implementation plan.
"""
from __future__ import annotations

import ast
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION_FILE = (
    ROOT
    / "migrations"
    / "versions"
    / "t1a2b3c4d5e6_immutable_contextual_decisions.py"
)
PREV_REVISION = "s1a2b3c4d5e6"
THIS_REVISION = "t1a2b3c4d5e6"

NEW_TABLES = [
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
]

EXISTING_TABLES_WITH_NEW_COLUMNS = {
    "products": ["product_profile_entry_id"],
    "experiments": ["record_schema_version", "started_at"],
    "experiment_entries": ["workspace_id"],
    "experiment_ad_bindings": ["workspace_id", "experiment_version_candidate_id", "record_schema_version"],
    "experiment_state_events": ["workspace_id", "evidence_json"],
    "judgements": ["workspace_id", "experiment_version_id", "record_schema_version"],
    "experiment_decisions": [
        "workspace_id", "experiment_version_id", "experiment_version_candidate_id",
        "proposal_id", "confirmation_id", "actor_id", "confirmed_at",
        "context_hash", "decision_schema_version",
    ],
    "asset_versions": [
        "truth_dependency_expected", "truth_dependency_policy_version",
        "truth_dependency_evidence_json", "truth_dependency_evidence_hash",
    ],
    "asset_qc_attempts": ["attempt_sequence", "terminal_at", "remediates_attempt_id"],
    "asset_qc_check_results": ["evidence_hash"],
}


def _setup_test_env(db_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["MARKETAI_DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    env["MARKETAI_DATA_DIR"] = str(db_path.parent / "data")
    env["MARKETAI_WORKSPACE_ID"] = "ws_test"
    env["MAX_COST_PER_RUN"] = "5"
    env["MAX_COST_PER_DAY"] = "20"
    env["JOB_LEASE_SECONDS"] = "1"
    return env


def _run_alembic_inprocess(db_path: Path, target: str, *, cmd: str = "upgrade") -> None:
    import importlib
    import app.config as _cfg
    saved_attrs = {k: getattr(_cfg, k) for k in dir(_cfg) if k.isupper()}
    env = _setup_test_env(db_path)
    original_env = {}
    for k, v in env.items():
        original_env[k] = os.environ.get(k)
        os.environ[k] = v
    try:
        importlib.reload(_cfg)
        from alembic import command
        from alembic.config import Config
        cfg = Config(str(ROOT / "alembic.ini"))
        getattr(command, cmd)(cfg, target)
    finally:
        for k, v in original_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(_cfg)
        for k, v in saved_attrs.items():
            setattr(_cfg, k, v)


def _sqlite_tables(db_path: Path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'alembic%'"
        )}
    finally:
        conn.close()


def _sqlite_columns(db_path: Path, table: str) -> dict[str, str]:
    conn = sqlite3.connect(db_path)
    try:
        return {row[1]: row[2] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


class MigrationUpgradeTests(unittest.TestCase):
    """MU-01..MU-10: Upgrade path tests."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory(prefix="marketai-mig22-")
        cls.db_path = Path(cls.temp.name) / "marketai.db"
        Path(cls.temp.name, "data").mkdir()

        _run_alembic_inprocess(cls.db_path, PREV_REVISION)
        cls._seed_pre_migration_data()
        _run_alembic_inprocess(cls.db_path, THIS_REVISION)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    @classmethod
    def _seed_pre_migration_data(cls) -> None:
        """Seed data at the previous revision to exercise workspace-id backfills.

        Uses raw SQL against the schema at revision o1a2b3c4d5e6 (before our migration).
        """
        conn = sqlite3.connect(cls.db_path)
        now = "2026-09-03T00:00:00"
        try:
            conn.execute(
                "INSERT INTO workspaces (id, name, default_currency, lifecycle, budget_currency, created_at) "
                "VALUES ('ws_test', 'Test WS', 'USD', 'active', 'USD', ?)", (now,)
            )
            conn.execute(
                "INSERT INTO products "
                "(id, workspace_id, name, description, selling_points, audience, "
                "brand_notes, ref_images, economics, revision, created_at, updated_at) "
                "VALUES ('prod1', 'ws_test', 'P', '', '[]', '', '', '[]', '{}', 1, ?, ?)",
                (now, now),
            )
            conn.execute(
                "INSERT INTO experiments "
                "(id, workspace_id, product_id, name, state, revision, config_json, "
                "metric_contract_hash, created_at) "
                "VALUES ('exp1', 'ws_test', 'prod1', 'Test', 'frozen', 1, '{}', 'mch', ?)",
                (now,),
            )
            # FK chain: workspace -> generation_run -> asset -> asset_version
            conn.executescript(f"""
                INSERT INTO generation_runs (id, workspace_id, product_id, plan_id,
                    provider, model, aspect_ratio, image_size, variants, use_ref,
                    status, total, done, failed, est_cost, currency, revision,
                    created_at, updated_at)
                VALUES ('run1', 'ws_test', 'prod1', 'plan1', 'mock', 'mock',
                    '1:1', '1024x1024', 1, 0, 'completed', 1, 1, 0, 0, 'USD', 1,
                    '{now}', '{now}');
                INSERT INTO plans (id, workspace_id, product_id, schema_id, mode,
                    cap, total_full, snapshot_hash, created_at)
                VALUES ('plan1', 'ws_test', 'prod1', NULL, 'full', 1, 1, 'h', '{now}');
                INSERT INTO assets (id, workspace_id, product_id, run_id, combo_id,
                    combo_code, tags_json, filename, provider, model, prompt,
                    fingerprint, status, error_code, error_detail, created_at, updated_at)
                VALUES ('asset1', 'ws_test', 'prod1', 'run1', 'c1', 'C1', '[]',
                    'img.png', 'mock', 'mock', 'test', 'fp1', 'active', '', '',
                    '{now}', '{now}');
                INSERT INTO asset_versions (id, workspace_id, asset_id, kind,
                    aspect_ratio, language, object_key, content_sha256, byte_size,
                    mime_type, created_at)
                VALUES ('av1', 'ws_test', 'asset1', 'image', '1:1', 'en', 'k1',
                    'sha1', 1024, 'image/png', '{now}');
                INSERT INTO asset_versions (id, workspace_id, asset_id, kind,
                    aspect_ratio, language, object_key, content_sha256, byte_size,
                    mime_type, truth_package_id, created_at)
                VALUES ('av2', 'ws_test', 'asset1', 'image', '1:1', 'en', 'k2',
                    'sha2', 2048, 'image/png', 'tp1', '{now}');
            """)

            conn.execute(
                "INSERT INTO experiment_entries "
                "(id, experiment_id, asset_version_id, marketing_vars_json, position, created_at) "
                "VALUES ('ee1', 'exp1', 'av1', '{}', 1, ?)",
                (now,),
            )
            conn.execute(
                "INSERT INTO experiment_ad_bindings "
                "(id, experiment_entry_id, ad_account_id, ad_id, source, created_at) "
                "VALUES ('eab1', 'ee1', 'acct1', 'ad1', 'manual', ?)",
                (now,),
            )
            conn.execute(
                "INSERT INTO judgements "
                "(id, experiment_id, algorithm_version, as_of, snapshot_hash, "
                "input_snapshot_json, result_json, created_at) "
                "VALUES ('j1', 'exp1', 'v1', ?, 'sh1', '{}', '{}', ?)",
                (now, now),
            )
            conn.execute(
                "INSERT INTO experiment_decisions "
                "(id, experiment_id, judgement_id, experiment_entry_id, verdict, "
                "metric, reason_codes_json, created_at) "
                "VALUES ('ed1', 'exp1', 'j1', 'ee1', 'win', 'cpa', '[]', ?)",
                (now,),
            )
            conn.execute(
                "INSERT INTO experiment_state_events "
                "(id, experiment_id, from_state, to_state, revision, actor_id, reason, created_at) "
                "VALUES ('ese1', 'exp1', 'draft', 'frozen', 1, 'user_test', 'test', ?)",
                (now,),
            )
            conn.commit()
        finally:
            conn.close()

    def test_mu01_new_tables_created(self) -> None:
        tables = _sqlite_tables(self.db_path)
        for table in NEW_TABLES:
            with self.subTest(table=table):
                self.assertIn(table, tables)

    def test_mu02_existing_tables_have_new_columns(self) -> None:
        for table, columns in EXISTING_TABLES_WITH_NEW_COLUMNS.items():
            cols = _sqlite_columns(self.db_path, table)
            for col in columns:
                with self.subTest(table=table, column=col):
                    self.assertIn(col, cols, f"{table}.{col} missing after migration")

    def test_mu03_workspace_id_backfilled_for_child_tables(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            for table in ("experiment_entries", "experiment_ad_bindings",
                          "experiment_state_events", "judgements", "experiment_decisions"):
                with self.subTest(table=table):
                    null_count = conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE workspace_id IS NULL"
                    ).fetchone()[0]
                    self.assertEqual(null_count, 0, f"{table} has {null_count} NULL workspace_id rows")
                    ws_id = conn.execute(
                        f"SELECT workspace_id FROM {table} LIMIT 1"
                    ).fetchone()[0]
                    self.assertEqual(ws_id, "ws_test")
        finally:
            conn.close()

    def test_mu04_truth_dependency_backfilled(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            # av1: no truth_package_id -> expected=false
            row = conn.execute(
                "SELECT truth_dependency_expected, truth_dependency_policy_version, "
                "truth_dependency_evidence_hash FROM asset_versions WHERE id = 'av1'"
            ).fetchone()
            self.assertEqual(row[0], 0)
            self.assertEqual(row[1], "v1-bootstrap")
            self.assertIsNotNone(row[2])

            # av2: has truth_package_id -> expected=true
            row = conn.execute(
                "SELECT truth_dependency_expected, truth_dependency_policy_version, "
                "truth_dependency_evidence_hash FROM asset_versions WHERE id = 'av2'"
            ).fetchone()
            self.assertEqual(row[0], 1)
            self.assertEqual(row[1], "v1-bootstrap")
            self.assertIsNotNone(row[2])

            # Different hashes
            h1 = conn.execute("SELECT truth_dependency_evidence_hash FROM asset_versions WHERE id='av1'").fetchone()[0]
            h2 = conn.execute("SELECT truth_dependency_evidence_hash FROM asset_versions WHERE id='av2'").fetchone()[0]
            self.assertNotEqual(h1, h2)
        finally:
            conn.close()

    def test_mu05_canonical_encoder_produces_deterministic_hashes(self) -> None:
        from migrations.versions.t1a2b3c4d5e6_immutable_contextual_decisions import (
            _canonical_bytes, _sha256,
        )
        h1 = _sha256(_canonical_bytes(("v1-bootstrap", False, None, "grandfathered_pre_truth_system")))
        h2 = _sha256(_canonical_bytes(("v1-bootstrap", True, "tp1", "grandfathered_pre_truth_system")))
        self.assertNotEqual(h1, h2)
        self.assertEqual(len(h1), 64)

    def test_mu06_fk_integrity_holds_after_upgrade(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            self.assertEqual(violations, [], f"FK violations: {violations}")
        finally:
            conn.close()

    def test_mu07_experiment_version_table_has_expected_columns(self) -> None:
        cols = _sqlite_columns(self.db_path, "experiment_versions")
        expected = {
            "id", "workspace_id", "experiment_id", "canonicalization_version",
            "frozen_snapshot_json", "frozen_snapshot_hash", "metric_contract_hash",
            "market", "placement", "landing_page_evidence_hash",
            "audience_snapshot_json", "audience_snapshot_hash",
            "daily_budget_amount", "total_budget_cap", "budget_currency",
            "schedule_start_at", "schedule_end_at", "schedule_timezone",
            "attribution_click_window_days", "attribution_view_window_days",
            "statistical_plan_json", "statistical_plan_hash",
            "marketing_vars_schema_version", "product_version_id",
            "offer_version_id", "candidates_hash", "concept_resolutions_hash",
            "created_at",
        }
        for col in expected:
            with self.subTest(col=col):
                self.assertIn(col, cols)

    def test_mu08_timeline_tables_exist_with_sequence(self) -> None:
        cols = _sqlite_columns(self.db_path, "experiment_timeline_events")
        self.assertIn("timeline_sequence", cols)
        self.assertIn("event_type", cols)
        self.assertIn("source_resource_type", cols)

    def test_mu09_notification_outbox_has_payload_hash(self) -> None:
        cols = _sqlite_columns(self.db_path, "experiment_notification_outbox")
        self.assertIn("payload_hash", cols)
        self.assertIn("payload_canonical_json", cols)
        self.assertIn("payload_schema_version", cols)

    def test_mu10_pattern_candidates_has_verdict(self) -> None:
        cols = _sqlite_columns(self.db_path, "experiment_pattern_candidates")
        self.assertIn("verdict", cols)
        self.assertIn("source_concepts_hash", cols)
        self.assertIn("pattern_evidence_hash", cols)


class MigrationDowngradeTests(unittest.TestCase):
    """MD-01..MD-11: Downgrade path tests."""

    def test_md01_downgrade_succeeds_on_empty_tables(self) -> None:
        with tempfile.TemporaryDirectory(prefix="marketai-down22-") as temp:
            db_path = Path(temp) / "marketai.db"
            Path(temp, "data").mkdir()
            _run_alembic_inprocess(db_path, THIS_REVISION)
            _run_alembic_inprocess(db_path, PREV_REVISION, cmd="downgrade")

    def test_md02_new_tables_removed_after_downgrade(self) -> None:
        with tempfile.TemporaryDirectory(prefix="marketai-down22-") as temp:
            db_path = Path(temp) / "marketai.db"
            Path(temp, "data").mkdir()
            _run_alembic_inprocess(db_path, THIS_REVISION)
            _run_alembic_inprocess(db_path, PREV_REVISION, cmd="downgrade")
            tables = _sqlite_tables(db_path)
            for table in NEW_TABLES:
                with self.subTest(table=table):
                    self.assertNotIn(table, tables)

    def test_md03_added_columns_removed_after_downgrade(self) -> None:
        with tempfile.TemporaryDirectory(prefix="marketai-down22-") as temp:
            db_path = Path(temp) / "marketai.db"
            Path(temp, "data").mkdir()
            _run_alembic_inprocess(db_path, THIS_REVISION)
            _run_alembic_inprocess(db_path, PREV_REVISION, cmd="downgrade")
            for table, columns in EXISTING_TABLES_WITH_NEW_COLUMNS.items():
                cols = _sqlite_columns(db_path, table)
                for col in columns:
                    with self.subTest(table=table, column=col):
                        self.assertNotIn(col, cols, f"{table}.{col} still exists after downgrade")

    def test_md04_round_trip_preserves_existing_data(self) -> None:
        with tempfile.TemporaryDirectory(prefix="marketai-rt22-") as temp:
            db_path = Path(temp) / "marketai.db"
            Path(temp, "data").mkdir()
            _run_alembic_inprocess(db_path, PREV_REVISION)

            conn = sqlite3.connect(db_path)
            now = "2026-09-03 00:00:00"
            conn.execute(
                "INSERT INTO workspaces (id, name, default_currency, lifecycle, budget_currency, created_at) "
                "VALUES ('ws_rt', 'Roundtrip', 'EUR', 'active', 'EUR', ?)", (now,)
            )
            conn.execute(
                "INSERT INTO products "
                "(id, workspace_id, name, description, selling_points, audience, "
                "brand_notes, ref_images, economics, revision, created_at, updated_at) "
                "VALUES ('prod_rt', 'ws_rt', 'RT', '', '[]', '', '', '[]', '{}', 1, ?, ?)",
                (now, now),
            )
            conn.commit()
            conn.close()

            _run_alembic_inprocess(db_path, THIS_REVISION)
            _run_alembic_inprocess(db_path, PREV_REVISION, cmd="downgrade")

            conn = sqlite3.connect(db_path)
            prod = conn.execute("SELECT name FROM products WHERE id = 'prod_rt'").fetchone()
            conn.close()
            self.assertIsNotNone(prod)
            self.assertEqual(prod[0], "RT")


class MigrationConstraintTests(unittest.TestCase):
    """MC-01..MC-06: Constraint and naming convention tests."""

    def test_mc01_migration_identifiers_fit_postgresql_63char_limit(self) -> None:
        tree = ast.parse(MIGRATION_FILE.read_text(encoding="utf-8"))
        keyword_identifiers = [
            keyword.value.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            for keyword in node.keywords
            if keyword.arg == "name"
            and isinstance(keyword.value, ast.Constant)
            and isinstance(keyword.value.value, str)
        ]
        positional_name_operations = {
            "create_check_constraint",
            "create_foreign_key",
            "create_index",
            "create_unique_constraint",
            "drop_constraint",
        }
        positional_identifiers = [
            node.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in positional_name_operations
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ]
        identifiers = keyword_identifiers + positional_identifiers
        oversized = [(name, len(name)) for name in identifiers if len(name) > 63]
        self.assertEqual(
            oversized, [],
            f"Constraint names exceed PostgreSQL 63-char limit: {oversized}",
        )

    def test_mc02_revision_chain_is_correct(self) -> None:
        source = MIGRATION_FILE.read_text(encoding="utf-8")
        self.assertIn(f'revision = "{THIS_REVISION}"', source)
        self.assertIn(f'down_revision = "{PREV_REVISION}"', source)

    def test_mc03_rls_uses_authenticated_context_function(self) -> None:
        source = MIGRATION_FILE.read_text(encoding="utf-8")
        self.assertIn("marketai_context_is_current(workspace_id)", source)
        self.assertNotIn("current_setting('marketai.workspace_id')", source)

    def test_mc04_canonical_encoder_frozen_in_migration(self) -> None:
        source = MIGRATION_FILE.read_text(encoding="utf-8")
        self.assertIn('_MIGRATION_CANON_VERSION = "p1-migration-v1"', source)
        self.assertIn("def _canonical_json(", source)
        self.assertIn("def _reject_floats(", source)

    def test_mc07_postgres_lineage_foreign_keys_are_in_orm_metadata(self) -> None:
        from app.adapters.persistence import models as _persistence_models
        from app.adapters.persistence.database import Base
        from app.generation import models as _generation_models

        del _persistence_models, _generation_models
        expected = {
            "experiment_next_round_records": {
                "fk_enrr_decision_lineage": (
                    (
                        "workspace_id", "decision_id", "experiment_id",
                        "experiment_version_id", "source_candidate_id",
                    ),
                    (
                        "experiment_decisions.workspace_id",
                        "experiment_decisions.id",
                        "experiment_decisions.experiment_id",
                        "experiment_decisions.experiment_version_id",
                        "experiment_decisions.experiment_version_candidate_id",
                    ),
                ),
                "fk_enrr_candidate_asset": (
                    (
                        "workspace_id", "experiment_version_id",
                        "source_candidate_id", "source_asset_version_id",
                    ),
                    (
                        "experiment_version_candidates.workspace_id",
                        "experiment_version_candidates.experiment_version_id",
                        "experiment_version_candidates.id",
                        "experiment_version_candidates.asset_version_id",
                    ),
                ),
                "fk_enrr_qc_attempt": (
                    ("workspace_id", "qc_attempt_id", "source_asset_version_id"),
                    (
                        "asset_qc_attempts.workspace_id",
                        "asset_qc_attempts.id",
                        "asset_qc_attempts.asset_version_id",
                    ),
                ),
            },
            "experiment_pattern_candidates": {
                "fk_epc_decision_lineage": (
                    (
                        "workspace_id", "decision_id", "experiment_id",
                        "experiment_version_id", "candidate_id",
                    ),
                    (
                        "experiment_decisions.workspace_id",
                        "experiment_decisions.id",
                        "experiment_decisions.experiment_id",
                        "experiment_decisions.experiment_version_id",
                        "experiment_decisions.experiment_version_candidate_id",
                    ),
                ),
            },
        }

        for table_name, expected_constraints in expected.items():
            actual_constraints = {
                constraint.name: constraint
                for constraint in Base.metadata.tables[table_name].foreign_key_constraints
            }
            for constraint_name, (local_columns, remote_columns) in expected_constraints.items():
                with self.subTest(table=table_name, constraint=constraint_name):
                    constraint = actual_constraints.get(constraint_name)
                    self.assertIsNotNone(constraint)
                    assert constraint is not None
                    self.assertEqual(tuple(constraint.column_keys), local_columns)
                    self.assertEqual(
                        tuple(element.target_fullname for element in constraint.elements),
                        remote_columns,
                    )
                    self.assertEqual(constraint.ondelete, "RESTRICT")

    def test_mc05_all_new_tables_have_rls_in_pg_block(self) -> None:
        import re
        source = MIGRATION_FILE.read_text(encoding="utf-8")
        self.assertIn(
            "CREATE POLICY tenant_isolation ON",
            source,
            "No canonical RLS policy pattern found",
        )
        m = re.search(r'NEW_WORKSPACE_OWNED\s*=\s*\((.*?)\)', source, re.DOTALL)
        self.assertIsNotNone(m, "NEW_WORKSPACE_OWNED tuple not found")
        owned_block = m.group(1)
        for table in NEW_TABLES:
            with self.subTest(table=table):
                self.assertIn(
                    f'"{table}"',
                    owned_block,
                    f"{table} not in NEW_WORKSPACE_OWNED for RLS",
                )

    def test_mc06_erasure_bypass_pattern_present(self) -> None:
        source = MIGRATION_FILE.read_text(encoding="utf-8")
        self.assertIn("marketai_erasure_principal", source)
        self.assertIn("marketai.erasure_workspace_id", source)


if __name__ == "__main__":
    unittest.main()
