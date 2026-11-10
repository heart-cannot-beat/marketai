"""Migration safety checks for the creative-library schema."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LibraryMigrationDowngradeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="marketai-library-migration-")
        self.database = Path(self.temp.name) / "migration.db"
        self.environment = dict(os.environ)
        self.environment["MARKETAI_DATABASE_URL"] = f"sqlite:///{self.database}"
        self.environment["MARKETAI_DATA_DIR"] = str(Path(self.temp.name) / "data")
        upgraded = self._alembic("upgrade", "head")
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _alembic(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "alembic", *arguments],
            cwd=ROOT,
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_safe_downgrade_restores_the_previous_event_type_width(self) -> None:
        downgraded = self._alembic("downgrade", "245a7b96664e")
        self.assertEqual(downgraded.returncode, 0, downgraded.stderr)
        with sqlite3.connect(self.database) as connection:
            columns = {
                row[1]: row[2]
                for row in connection.execute("PRAGMA table_info(library_events)")
            }
            revisions = {
                row[0]
                for row in connection.execute("SELECT version_num FROM alembic_version")
            }
        self.assertEqual(columns["event_type"].upper(), "VARCHAR(40)")
        # j1a2b3c4d5e6 is the intent-tables head that the library event
        # migration merges with; 245a7b96664e is the target downgrade point.
        self.assertEqual(revisions, {"245a7b96664e", "j1a2b3c4d5e6"})

    def test_downgrade_refuses_to_corrupt_long_immutable_event_names(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "INSERT INTO workspaces "
                "(id, name, default_currency, lifecycle, budget_currency, created_at) "
                "VALUES ('ws_migration_guard', 'guard', 'USD', 'active', 'USD', '2026-08-30 00:00:00')"
            )
            connection.execute(
                "INSERT INTO library_entries "
                "(id, workspace_id, entry_type, display_name, lifecycle, current_version_id, "
                "archived_at, created_at, updated_at) VALUES "
                "('lib_migration_guard', 'ws_migration_guard', 'product', 'guard', 'active', "
                "NULL, NULL, '2026-08-30 00:00:00', '2026-08-30 00:00:00')"
            )
            connection.execute(
                "INSERT INTO library_events "
                "(id, workspace_id, entry_id, version_id, sequence, event_type, from_state, "
                "to_state, initiator_id, executor, payload_json, created_at) VALUES "
                "('libev_migration_guard', 'ws_migration_guard', 'lib_migration_guard', NULL, 1, "
                "'library.withdrawal.affects_market_asset_binding', '', '', 'owner', 'user', "
                "'{}', '2026-08-30 00:00:00')"
            )
            connection.commit()

        downgraded = self._alembic("downgrade", "245a7b96664e")
        self.assertNotEqual(downgraded.returncode, 0)
        self.assertIn("event_type values longer than 40", downgraded.stderr)
        with sqlite3.connect(self.database) as connection:
            revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            event_type = connection.execute("SELECT event_type FROM library_events").fetchone()[0]
        # The whole multi-step downgrade runs in one transaction; when an
        # intermediate step raises (the event_type length guard below), the
        # entire chain rolls back to whatever revision was head when the
        # command was invoked -- ad2_target_preview (Issue #87 Task 1: Scale
        # target preview tables and command identity hash), which sits on
        # ad1_check_names (post-#92 PostgreSQL schema convergence), which
        # sits on ac1_enrollment_lineage (Issue #86 Task 12: Enrollment
        # lineage backfill), which sits on ab1_scale_lineage (Issue #86 Task
        # 7: Scale Enrollment lineage tables), which sits on
        # aa1_identity_kind (Task 4: is_platform_workspace on
        # marketai_workspace_admission()), which sits on c80a1b2c3d4e
        # (Issue #80), which sits on z1a2b3c4d5e6 / y1a2b3c4d5e6 (#76
        # onboarding fact delivery), b79c2d3e4f5a (Issue #79),
        # x1a2b3c4d5e6 (workspace projection repair), w1a2b3c4d5e6
        # (contextual conclusion revocation), v1a2b3c4d5e6 (committed blob
        # refs), and u1a2b3c4d5e6 (Issue #24 localization).
        # The head now also carries zb1c2d3e4f6a (wider Operation result
        # resource ids), zc1d2e3f4a5b (export package storage version),
        # zd1e2f3a4b5c (meta-mode-aware RLS context signature),
        # ze1f2a3b4c5d (unbounded combination fingerprints),
        # zf1a2b3c4d5e (asset_versions thumbnail columns, Issue #159),
        # zg2b3c4d5e6f (reference object BlobStore refs + promoted entry,
        # Issue #158), zh3c4d5e6f7a (llm_chat step kind, Issue #160),
        # zi4d5e6f7a8b (product_intakes, Issue #161), zj5e6f7a8b9c
        # (test plans + generation defaults, Issue #163) and zk6f7a8b9c0d
        # (result imports + learning snapshots, Issue #166) and zl7a8b9c0d1e
        # (asset_versions reference lineage, Issue #167) and zm8b9c0d1e2f
        # (external creatives + tag confirmations, Issue #168) and
        # zw8d9e0f1a2b (versioned product brand kits, Issue #228) and
        # zx9e0f1a2b3c (single/paired AI QC kinds, Issue #229) and
        # zy0f1a2b3c4d (AI QC refused-output detail, Issue #295) and
        # zz1a2b3c4d5e (competitor list and resolves, Issue #269) and
        # zz2b3c4d5e6f (competitor scans and observations, Issue #270) and
        # zz3c4d5e6f7a (competitor observation judgments, Issue #271) and
        # zz4d5e6f7a8b (competitor clusters and snapshots, Issue #272) and
        # zz5e6f7a8b9c (competitor discovery decisions, Issue #276) and
        # zz6f7a8b9c0d (soft-removable experiment entries, Issue #284) and
        # zz7a8b9c0d1e (the API reads usage events, Issue #347) and
        # zz8b9c0d1e2f (stored spend guardrail previews, Issue #386) and
        # zz9c0d1e2f3a (the API publishes adopted localized versions, Issue #46) and
        # zza0d1e2f3a4 (Worker-only Operation reconciliation candidates, Issue #22) and
        # zzb1e2f3a4b5 (the Worker rolls Scale Episodes over, Issue #92) and
        # zzc2f3a4b5c6 (platform Workspace creation and credential ownership, #91 and #94) and
        # zzd3a4b5c6d7 (platform Meta connection reads across Workspaces, #102) and
        # zze4f5a6b7c8 (the Worker dispatches Meta observation schedules, #107).
        self.assertEqual(revision, "zze4f5a6b7c8")
        self.assertEqual(event_type, "library.withdrawal.affects_market_asset_binding")


class LibraryTemplatesDatasourcesMigrationTests(unittest.TestCase):
    """l1a2b3c4d5e6: dimension templates, prompt policy, datasource bindings (#20 Task 1)."""

    NEW_TABLES = (
        "dimension_concept_keys",
        "prompt_baselines",
        "datasource_credential_bindings",
        "resolution_previews",
    )

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="marketai-library-templates-migration-")
        self.database = Path(self.temp.name) / "migration.db"
        self.environment = dict(os.environ)
        self.environment["MARKETAI_DATABASE_URL"] = f"sqlite:///{self.database}"
        self.environment["MARKETAI_DATA_DIR"] = str(Path(self.temp.name) / "data")
        upgraded = self._alembic("upgrade", "head")
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _alembic(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "alembic", *arguments],
            cwd=ROOT,
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def _table_names(self, connection: sqlite3.Connection) -> set[str]:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        return {row[0] for row in rows}

    def test_head_is_l1a2b3c4d5e6(self) -> None:
        # NOTE: despite the method name (kept to avoid rewriting this
        # class's identity), this asserts the *current* overall head --
        # ad2_target_preview (Issue #87 Task 1: Scale target preview tables
        # and command identity hash), which sits on ad1_check_names
        # (post-#92 PostgreSQL schema convergence), which sits on
        # ac1_enrollment_lineage (Issue #86 Task 12: Enrollment lineage
        # backfill), which sits on ab1_scale_lineage (Issue #86 Task 7:
        # Scale Enrollment lineage tables), which sits on aa1_identity_kind
        # (Task 4: is_platform_workspace on marketai_workspace_admission())
        # sits on c80a1b2c3d4e (Issue #80 Localization release guards),
        # which sits on z1a2b3c4d5e6 / y1a2b3c4d5e6 (#76 onboarding fact
        # delivery), which sit on b79c2d3e4f5a (Issue #79 launch crash
        # consistency), a79b1c2d3e4f (Issue #79 launch confirmations),
        # f5eeeab4e9b9 (Issue #25 Scale), and x1a2b3c4d5e6 (workspace
        # projection repair). w1a2b3c4d5e6 (contextual conclusion
        # revocation), v1a2b3c4d5e6 (committed blob refs), u1a2b3c4d5e6
        # (Issue #24 localization), and the Issue #23 Meta/Launch
        # revisions.
        with sqlite3.connect(self.database) as connection:
            revision = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0]
        # The head now also carries zb1c2d3e4f6a (wider Operation result
        # resource ids), zc1d2e3f4a5b (export package storage version),
        # zd1e2f3a4b5c (meta-mode-aware RLS context signature),
        # ze1f2a3b4c5d (unbounded combination fingerprints),
        # zf1a2b3c4d5e (asset_versions thumbnail columns, Issue #159),
        # zg2b3c4d5e6f (reference object BlobStore refs + promoted entry,
        # Issue #158), zh3c4d5e6f7a (llm_chat step kind, Issue #160),
        # zi4d5e6f7a8b (product_intakes, Issue #161), zj5e6f7a8b9c
        # (test plans + generation defaults, Issue #163) and zk6f7a8b9c0d
        # (result imports + learning snapshots, Issue #166) and zl7a8b9c0d1e
        # (asset_versions reference lineage, Issue #167) and zm8b9c0d1e2f
        # (external creatives + tag confirmations, Issue #168) and
        # zw8d9e0f1a2b (versioned product brand kits, Issue #228) and
        # zx9e0f1a2b3c (single/paired AI QC kinds, Issue #229) and
        # zy0f1a2b3c4d (AI QC refused-output detail, Issue #295) and
        # zz1a2b3c4d5e (competitor list and resolves, Issue #269) and
        # zz2b3c4d5e6f (competitor scans and observations, Issue #270) and
        # zz3c4d5e6f7a (competitor observation judgments, Issue #271) and
        # zz4d5e6f7a8b (competitor clusters and snapshots, Issue #272) and
        # zz5e6f7a8b9c (competitor discovery decisions, Issue #276) and
        # zz6f7a8b9c0d (soft-removable experiment entries, Issue #284) and
        # zz7a8b9c0d1e (the API reads usage events, Issue #347) and
        # zz8b9c0d1e2f (stored spend guardrail previews, Issue #386) and
        # zz9c0d1e2f3a (the API publishes adopted localized versions, Issue #46) and
        # zza0d1e2f3a4 (Worker-only Operation reconciliation candidates, Issue #22) and
        # zzb1e2f3a4b5 (the Worker rolls Scale Episodes over, Issue #92) and
        # zzc2f3a4b5c6 (platform Workspace creation and credential ownership, #91 and #94) and
        # zzd3a4b5c6d7 (platform Meta connection reads across Workspaces, #102) and
        # zze4f5a6b7c8 (the Worker dispatches Meta observation schedules, #107).
        self.assertEqual(revision, "zze4f5a6b7c8")

    def test_new_tables_exist_at_head(self) -> None:
        with sqlite3.connect(self.database) as connection:
            tables = self._table_names(connection)
        for table in self.NEW_TABLES:
            self.assertIn(table, tables)

    def test_dimension_concept_keys_scope_check_constraint_is_enforced(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "INSERT INTO workspaces "
                "(id, name, default_currency, lifecycle, budget_currency, created_at) "
                "VALUES ('ws_concept_guard', 'guard', 'USD', 'active', 'USD', '2026-08-30 00:00:00')"
            )
            # platform-scope row must not carry a workspace_id.
            connection.execute(
                "INSERT INTO dimension_concept_keys "
                "(id, workspace_id, scope, name, description, created_by, created_at) VALUES "
                "('dck_platform', NULL, 'platform', 'material', '', 'owner', '2026-08-30 00:00:00')"
            )
            # workspace-scope row must carry a workspace_id.
            connection.execute(
                "INSERT INTO dimension_concept_keys "
                "(id, workspace_id, scope, name, description, created_by, created_at) VALUES "
                "('dck_workspace', 'ws_concept_guard', 'workspace', 'finish', '', 'owner', "
                "'2026-08-30 00:00:00')"
            )
            connection.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO dimension_concept_keys "
                    "(id, workspace_id, scope, name, description, created_by, created_at) VALUES "
                    "('dck_bad', NULL, 'workspace', 'bad', '', 'owner', '2026-08-30 00:00:00')"
                )

    def test_prompt_baselines_one_effective_partial_index_is_enforced(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "INSERT INTO prompt_baselines "
                "(id, version_no, content_json, content_hash, state, published_by, "
                "published_at, superseded_at, withdrawn_at, withdraw_reason) VALUES "
                "('ppb_1', 1, '{}', 'hash1', 'effective', 'owner', "
                "'2026-08-30 00:00:00', NULL, NULL, '')"
            )
            connection.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO prompt_baselines "
                    "(id, version_no, content_json, content_hash, state, published_by, "
                    "published_at, superseded_at, withdrawn_at, withdraw_reason) VALUES "
                    "('ppb_2', 2, '{}', 'hash2', 'effective', 'owner', "
                    "'2026-08-30 00:01:00', NULL, NULL, '')"
                )

        # Two 'superseded' rows are not constrained by the partial index --
        # only 'effective' rows are.
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "INSERT INTO prompt_baselines "
                "(id, version_no, content_json, content_hash, state, published_by, "
                "published_at, superseded_at, withdrawn_at, withdraw_reason) VALUES "
                "('ppb_3', 3, '{}', 'hash3', 'superseded', 'owner', "
                "'2026-08-30 00:02:00', '2026-08-30 00:03:00', NULL, '')"
            )
            connection.execute(
                "INSERT INTO prompt_baselines "
                "(id, version_no, content_json, content_hash, state, published_by, "
                "published_at, superseded_at, withdrawn_at, withdraw_reason) VALUES "
                "('ppb_4', 4, '{}', 'hash4', 'superseded', 'owner', "
                "'2026-08-30 00:04:00', '2026-08-30 00:05:00', NULL, '')"
            )
            connection.commit()

    def test_datasource_credential_bindings_one_active_partial_index_is_enforced(
        self,
    ) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "INSERT INTO workspaces "
                "(id, name, default_currency, lifecycle, budget_currency, created_at) "
                "VALUES ('ws_dcb_guard', 'guard', 'USD', 'active', 'USD', '2026-08-30 00:00:00')"
            )
            connection.execute(
                "INSERT INTO library_entries "
                "(id, workspace_id, entry_type, display_name, lifecycle, current_version_id, "
                "archived_at, created_at, updated_at) VALUES "
                "('lib_dcb_guard', 'ws_dcb_guard', 'data_source', 'guard', 'active', "
                "NULL, NULL, '2026-08-30 00:00:00', '2026-08-30 00:00:00')"
            )
            connection.execute(
                "INSERT INTO credential_versions "
                "(id, workspace_id, credential_key, version_no, provider, display_label, "
                "state, encrypted_secret, encrypted_data_key, encryption_key_id, "
                "encryption_algorithm, nonce, aad_schema_version, created_by, created_at) "
                "VALUES ('cred_dcb_guard', 'ws_dcb_guard', 'DATA_SOURCE_API_KEY', 1, "
                "'generic', NULL, 'active', 'secret', 'datakey', 'kek1', 'aes-256-gcm', "
                "'nonce1', 1, 'owner', '2026-08-30 00:00:00')"
            )
            connection.commit()
            connection.execute(
                "INSERT INTO datasource_credential_bindings "
                "(id, workspace_id, data_source_entry_id, credential_version_id, revision, "
                "state, created_at, updated_at) VALUES "
                "('dcb_1', 'ws_dcb_guard', 'lib_dcb_guard', 'cred_dcb_guard', 1, 'active', "
                "'2026-08-30 00:00:00', '2026-08-30 00:00:00')"
            )
            connection.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO datasource_credential_bindings "
                    "(id, workspace_id, data_source_entry_id, credential_version_id, revision, "
                    "state, created_at, updated_at) VALUES "
                    "('dcb_2', 'ws_dcb_guard', 'lib_dcb_guard', 'cred_dcb_guard', 2, 'active', "
                    "'2026-08-30 00:01:00', '2026-08-30 00:01:00')"
                )

        # Two 'superseded' rows for the same entry are not constrained by the
        # partial index -- only 'active' rows are.
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "INSERT INTO datasource_credential_bindings "
                "(id, workspace_id, data_source_entry_id, credential_version_id, revision, "
                "state, created_at, updated_at) VALUES "
                "('dcb_3', 'ws_dcb_guard', 'lib_dcb_guard', 'cred_dcb_guard', 3, 'superseded', "
                "'2026-08-30 00:02:00', '2026-08-30 00:02:00')"
            )
            connection.execute(
                "INSERT INTO datasource_credential_bindings "
                "(id, workspace_id, data_source_entry_id, credential_version_id, revision, "
                "state, created_at, updated_at) VALUES "
                "('dcb_4', 'ws_dcb_guard', 'lib_dcb_guard', 'cred_dcb_guard', 4, 'superseded', "
                "'2026-08-30 00:03:00', '2026-08-30 00:03:00')"
            )
            connection.commit()

    def test_downgrade_drops_the_new_tables_and_restores_previous_head(self) -> None:
        downgraded = self._alembic("downgrade", "k1a2b3c4d5e6")
        self.assertEqual(downgraded.returncode, 0, downgraded.stderr)
        with sqlite3.connect(self.database) as connection:
            tables = self._table_names(connection)
            revision = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0]
        for table in self.NEW_TABLES:
            self.assertNotIn(table, tables)
        self.assertEqual(revision, "k1a2b3c4d5e6")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
