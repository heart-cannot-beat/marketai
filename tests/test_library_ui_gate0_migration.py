"""Gate 0.2 migration tests for the Creative Library workspace UI (#47).

Covers:
- New tables exist at head (library_command_impacts, library_entry_contexts,
  library_stepup_assertions)
- New columns on library_idempotency, library_drafts, library_versions
- CHECK constraints on lifecycle_state, response_kind, command_kind,
  consumption_state, lifecycle_revision
- Unique constraint on command_identity_hash rejects duplicates within
  the same workspace/actor/command
- Self-referencing context entries are rejected
- Duplicate consumed step-up JTIs are rejected
- Cross-workspace context links are rejected by unique constraint
- Downgrade restores the previous head
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Gate0MigrationTests(unittest.TestCase):
    """r1a2b3c4d5e6: Gate 0.2 Creative Library workspace UI contracts."""

    NEW_TABLES = (
        "library_command_impacts",
        "library_entry_contexts",
        "library_stepup_assertions",
    )

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="marketai-gate0-migration-")
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

    def _columns(self, connection: sqlite3.Connection, table: str) -> dict[str, str]:
        return {
            row[1]: row[2]
            for row in connection.execute(f"PRAGMA table_info({table})")
        }

    def _seed_workspace(self, connection: sqlite3.Connection, ws_id: str = "ws_g0") -> None:
        connection.execute(
            "INSERT OR IGNORE INTO workspaces "
            "(id, name, default_currency, lifecycle, budget_currency, created_at) "
            f"VALUES ('{ws_id}', 'gate0', 'USD', 'active', 'USD', '2026-09-03 00:00:00')"
        )
        connection.commit()

    def _seed_entry(
        self, connection: sqlite3.Connection, entry_id: str, ws_id: str = "ws_g0"
    ) -> None:
        self._seed_workspace(connection, ws_id)
        connection.execute(
            "INSERT OR IGNORE INTO library_entries "
            "(id, workspace_id, entry_type, display_name, lifecycle, "
            "current_version_id, archived_at, created_at, updated_at) VALUES "
            f"('{entry_id}', '{ws_id}', 'product', 'test', 'active', "
            "NULL, NULL, '2026-09-03 00:00:00', '2026-09-03 00:00:00')"
        )
        connection.commit()

    # -- Structural tests ------------------------------------------------

    def test_head_is_u1a2b3c4d5e6(self) -> None:
        # NOTE: despite the method name (kept to avoid rewriting this
        # class's identity), this asserts the *current* overall head --
        # ad2_target_preview (Issue #87 Task 1: Scale target preview tables
        # and command identity hash), which sits on ad1_check_names
        # (post-#92 PostgreSQL schema convergence), which sits on
        # ac1_enrollment_lineage (Issue #86 Task 12: Enrollment lineage
        # backfill), which sits on ab1_scale_lineage (Issue #86 Task 7:
        # Scale Enrollment lineage tables), which sits on aa1_identity_kind
        # (Task 4: is_platform_workspace on marketai_workspace_admission())
        # now sits on c80a1b2c3d4e (Issue #80 Localization release guards),
        # which sits on z1a2b3c4d5e6 / y1a2b3c4d5e6 (#76 onboarding fact
        # delivery), which sit on b79c2d3e4f5a (Issue #79 launch crash
        # consistency), a79b1c2d3e4f (Issue #79 launch confirmations),
        # f5eeeab4e9b9 (Issue #25 Scale), and x1a2b3c4d5e6 (workspace
        # projection repair). on the Issue #49 revisions, u1a2b3c4d5e6, the
        # Issue #23 Meta revisions, and t1a2b3c4d5e6.
        with sqlite3.connect(self.database) as conn:
            revision = conn.execute(
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
        # zze4f5a6b7c8 (the Worker dispatches Meta observation schedules, #107) and
        # zzf5a6b7c8d9 (Scale source collection key and delivery diagnostics, #109) and
        # zzf6b7c8d9e0 (observation ticks keyed by authorization epoch, #119) and
        # zzg7d8e9f0a1 (the Beta default ScalePolicyVersion is published, #127) and
        # zzg126a1b2c3 (provisioning creates the guardrail at the Beta defaults, #126) and
        # zzh129a1b2c3 (DeliveryTargets name the Landing Page by its frozen hash, #129) and
        # zzi132a1b2c3 (Beta default ScalePolicyVersion v2, #132 and #133).
        self.assertEqual(revision, "zzi132a1b2c3")

    def test_new_tables_exist_at_head(self) -> None:
        with sqlite3.connect(self.database) as conn:
            tables = self._table_names(conn)
        for table in self.NEW_TABLES:
            self.assertIn(table, tables)

    def test_library_idempotency_has_new_columns(self) -> None:
        with sqlite3.connect(self.database) as conn:
            cols = self._columns(conn, "library_idempotency")
        for col in (
            "contract_schema_version",
            "command_identity_hash",
            "lifecycle_state",
            "response_kind",
            "updated_at",
            "reconciled_at",
        ):
            self.assertIn(col, cols, f"missing column: {col}")

    def test_library_drafts_has_content_schema_version(self) -> None:
        with sqlite3.connect(self.database) as conn:
            cols = self._columns(conn, "library_drafts")
        self.assertIn("content_schema_version", cols)

    def test_library_versions_has_new_columns(self) -> None:
        with sqlite3.connect(self.database) as conn:
            cols = self._columns(conn, "library_versions")
        self.assertIn("content_schema_version", cols)
        self.assertIn("lifecycle_revision", cols)

    # -- CHECK constraint enforcement ------------------------------------

    def test_idempotency_lifecycle_state_check_constraint(self) -> None:
        with sqlite3.connect(self.database) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            self._seed_workspace(conn)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO library_idempotency "
                    "(id, workspace_id, idempotency_key, command, initiator_id, "
                    "request_fingerprint, response_json, contract_schema_version, "
                    "command_identity_hash, lifecycle_state, response_kind, "
                    "created_at, updated_at) VALUES "
                    "('idem_bad_state', 'ws_g0', 'key1', 'create', 'actor1', "
                    "'fp1', '{}', 1, 'hash_bad_state', 'invalid_state', 'pending', "
                    "'2026-09-03 00:00:00', '2026-09-03 00:00:00')"
                )

    def test_idempotency_response_kind_check_constraint(self) -> None:
        with sqlite3.connect(self.database) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            self._seed_workspace(conn)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO library_idempotency "
                    "(id, workspace_id, idempotency_key, command, initiator_id, "
                    "request_fingerprint, response_json, contract_schema_version, "
                    "command_identity_hash, lifecycle_state, response_kind, "
                    "created_at, updated_at) VALUES "
                    "('idem_bad_kind', 'ws_g0', 'key2', 'create', 'actor1', "
                    "'fp2', '{}', 1, 'hash_bad_kind', 'pending', 'bad_kind', "
                    "'2026-09-03 00:00:00', '2026-09-03 00:00:00')"
                )

    def test_command_impact_kind_check_constraint(self) -> None:
        with sqlite3.connect(self.database) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            self._seed_workspace(conn)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO library_command_impacts "
                    "(id, workspace_id, actor_id, command_kind, target_entry_id, "
                    "impact_hash, projection_json, consumption_state, "
                    "expires_at, created_at) VALUES "
                    "('imp_bad', 'ws_g0', 'actor1', 'invalid_kind', 'entry1', "
                    "'hash1', '{}', 'available', '2026-09-04 00:00:00', "
                    "'2026-09-03 00:00:00')"
                )

    def test_command_impact_consumption_check_constraint(self) -> None:
        with sqlite3.connect(self.database) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            self._seed_workspace(conn)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO library_command_impacts "
                    "(id, workspace_id, actor_id, command_kind, target_entry_id, "
                    "impact_hash, projection_json, consumption_state, "
                    "expires_at, created_at) VALUES "
                    "('imp_bad2', 'ws_g0', 'actor1', 'publish', 'entry1', "
                    "'hash2', '{}', 'bad_state', '2026-09-04 00:00:00', "
                    "'2026-09-03 00:00:00')"
                )

    def test_lifecycle_revision_must_be_positive(self) -> None:
        with sqlite3.connect(self.database) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            self._seed_entry(conn, "lib_rev_guard")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO library_versions "
                    "(id, workspace_id, entry_id, entry_type, version_no, "
                    "content_json, content_hash, content_schema_version, "
                    "lifecycle_revision, state, eligibility, validation_json, "
                    "origin_command, published_by, published_at) VALUES "
                    "('ver_bad_rev', 'ws_g0', 'lib_rev_guard', 'product', 1, "
                    "'{}', 'hash1', 1, 0, 'effective', 'eligible', '{}', "
                    "'create', 'owner', '2026-09-03 00:00:00')"
                )

    # -- Unique constraint enforcement -----------------------------------

    def test_duplicate_command_identity_hash_within_workspace_is_rejected(self) -> None:
        with sqlite3.connect(self.database) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            self._seed_workspace(conn)
            conn.execute(
                "INSERT INTO library_idempotency "
                "(id, workspace_id, idempotency_key, command, initiator_id, "
                "request_fingerprint, response_json, contract_schema_version, "
                "command_identity_hash, lifecycle_state, response_kind, "
                "created_at, updated_at) VALUES "
                "('idem_dup1', 'ws_g0', 'key_dup1', 'create', 'actor1', "
                "'fp1', '{}', 1, 'same_hash', 'terminal', 'success', "
                "'2026-09-03 00:00:00', '2026-09-03 00:00:00')"
            )
            conn.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO library_idempotency "
                    "(id, workspace_id, idempotency_key, command, initiator_id, "
                    "request_fingerprint, response_json, contract_schema_version, "
                    "command_identity_hash, lifecycle_state, response_kind, "
                    "created_at, updated_at) VALUES "
                    "('idem_dup2', 'ws_g0', 'key_dup2', 'create', 'actor1', "
                    "'fp2', '{}', 1, 'same_hash', 'pending', 'pending', "
                    "'2026-09-03 00:00:01', '2026-09-03 00:00:01')"
                )

    def test_same_command_identity_hash_in_different_workspaces_is_allowed(self) -> None:
        with sqlite3.connect(self.database) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            self._seed_workspace(conn, "ws_g0a")
            self._seed_workspace(conn, "ws_g0b")
            conn.execute(
                "INSERT INTO library_idempotency "
                "(id, workspace_id, idempotency_key, command, initiator_id, "
                "request_fingerprint, response_json, contract_schema_version, "
                "command_identity_hash, lifecycle_state, response_kind, "
                "created_at, updated_at) VALUES "
                "('idem_ws1', 'ws_g0a', 'key_ws1', 'create', 'actor1', "
                "'fp1', '{}', 1, 'cross_ws_hash', 'terminal', 'success', "
                "'2026-09-03 00:00:00', '2026-09-03 00:00:00')"
            )
            conn.execute(
                "INSERT INTO library_idempotency "
                "(id, workspace_id, idempotency_key, command, initiator_id, "
                "request_fingerprint, response_json, contract_schema_version, "
                "command_identity_hash, lifecycle_state, response_kind, "
                "created_at, updated_at) VALUES "
                "('idem_ws2', 'ws_g0b', 'key_ws2', 'create', 'actor1', "
                "'fp2', '{}', 1, 'cross_ws_hash', 'terminal', 'success', "
                "'2026-09-03 00:00:00', '2026-09-03 00:00:00')"
            )
            conn.commit()

    def test_duplicate_stepup_jti_within_workspace_is_rejected(self) -> None:
        with sqlite3.connect(self.database) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            self._seed_workspace(conn)
            conn.execute(
                "INSERT INTO library_stepup_assertions "
                "(id, workspace_id, actor_id, jti_hash, command_kind, "
                "consumed_at, expires_at) VALUES "
                "('step_1', 'ws_g0', 'actor1', 'jti_hash_dup', 'publish', "
                "'2026-09-03 00:00:00', '2026-09-03 01:00:00')"
            )
            conn.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO library_stepup_assertions "
                    "(id, workspace_id, actor_id, jti_hash, command_kind, "
                    "consumed_at, expires_at) VALUES "
                    "('step_2', 'ws_g0', 'actor1', 'jti_hash_dup', 'publish', "
                    "'2026-09-03 00:00:01', '2026-09-03 01:00:00')"
                )

    def test_self_referencing_entry_context_is_rejected(self) -> None:
        with sqlite3.connect(self.database) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            self._seed_entry(conn, "lib_self_ref")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO library_entry_contexts "
                    "(id, workspace_id, root_entry_id, resource_entry_id, "
                    "relation_kind, created_at) VALUES "
                    "('ctx_self', 'ws_g0', 'lib_self_ref', 'lib_self_ref', "
                    "'owns', '2026-09-03 00:00:00')"
                )

    def test_duplicate_context_within_workspace_is_rejected(self) -> None:
        with sqlite3.connect(self.database) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            self._seed_entry(conn, "lib_root")
            self._seed_entry(conn, "lib_res")
            conn.execute(
                "INSERT INTO library_entry_contexts "
                "(id, workspace_id, root_entry_id, resource_entry_id, "
                "relation_kind, created_at) VALUES "
                "('ctx_1', 'ws_g0', 'lib_root', 'lib_res', "
                "'owns', '2026-09-03 00:00:00')"
            )
            conn.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO library_entry_contexts "
                    "(id, workspace_id, root_entry_id, resource_entry_id, "
                    "relation_kind, created_at) VALUES "
                    "('ctx_2', 'ws_g0', 'lib_root', 'lib_res', "
                    "'owns', '2026-09-03 00:00:01')"
                )

    def test_different_relation_kinds_for_same_pair_are_allowed(self) -> None:
        with sqlite3.connect(self.database) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            self._seed_entry(conn, "lib_root2")
            self._seed_entry(conn, "lib_res2")
            conn.execute(
                "INSERT INTO library_entry_contexts "
                "(id, workspace_id, root_entry_id, resource_entry_id, "
                "relation_kind, created_at) VALUES "
                "('ctx_rk1', 'ws_g0', 'lib_root2', 'lib_res2', "
                "'owns', '2026-09-03 00:00:00')"
            )
            conn.execute(
                "INSERT INTO library_entry_contexts "
                "(id, workspace_id, root_entry_id, resource_entry_id, "
                "relation_kind, created_at) VALUES "
                "('ctx_rk2', 'ws_g0', 'lib_root2', 'lib_res2', "
                "'references', '2026-09-03 00:00:00')"
            )
            conn.commit()

    def test_cross_workspace_context_link_is_rejected(self) -> None:
        with sqlite3.connect(self.database) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            self._seed_entry(conn, "lib_root_ws_a", "ws_context_a")
            self._seed_entry(conn, "lib_resource_ws_b", "ws_context_b")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO library_entry_contexts "
                    "(id, workspace_id, root_entry_id, resource_entry_id, "
                    "relation_kind, created_at) VALUES "
                    "('ctx_cross_ws', 'ws_context_a', 'lib_root_ws_a', "
                    "'lib_resource_ws_b', 'owns', '2026-09-03 00:00:00')"
                )

    def test_valid_impact_can_be_inserted_and_unique_hash_enforced(self) -> None:
        with sqlite3.connect(self.database) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            self._seed_workspace(conn)
            conn.execute(
                "INSERT INTO library_command_impacts "
                "(id, workspace_id, actor_id, command_kind, target_entry_id, "
                "impact_hash, projection_json, consumption_state, "
                "expires_at, created_at) VALUES "
                "('imp_ok', 'ws_g0', 'actor1', 'publish', 'entry1', "
                "'unique_hash_1', '{}', 'available', '2026-09-04 00:00:00', "
                "'2026-09-03 00:00:00')"
            )
            conn.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO library_command_impacts "
                    "(id, workspace_id, actor_id, command_kind, target_entry_id, "
                    "impact_hash, projection_json, consumption_state, "
                    "expires_at, created_at) VALUES "
                    "('imp_dup', 'ws_g0', 'actor1', 'archive', 'entry2', "
                    "'unique_hash_1', '{}', 'available', '2026-09-04 00:00:00', "
                    "'2026-09-03 00:00:00')"
                )

    # -- Downgrade test --------------------------------------------------

    def test_downgrade_restores_previous_head(self) -> None:
        downgraded = self._alembic("downgrade", "o1a2b3c4d5e6")
        self.assertEqual(downgraded.returncode, 0, downgraded.stderr)
        with sqlite3.connect(self.database) as conn:
            tables = self._table_names(conn)
            revision = conn.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0]
            idem_cols = self._columns(conn, "library_idempotency")
            draft_cols = self._columns(conn, "library_drafts")
            version_cols = self._columns(conn, "library_versions")
        for table in self.NEW_TABLES:
            self.assertNotIn(table, tables)
        self.assertEqual(revision, "o1a2b3c4d5e6")
        self.assertNotIn("command_identity_hash", idem_cols)
        self.assertNotIn("lifecycle_state", idem_cols)
        self.assertNotIn("content_schema_version", draft_cols)
        self.assertNotIn("lifecycle_revision", version_cols)


class Gate0BackfillTests(unittest.TestCase):
    """Verify backfill of pre-existing rows during upgrade."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="marketai-gate0-backfill-")
        self.database = Path(self.temp.name) / "migration.db"
        self.environment = dict(os.environ)
        self.environment["MARKETAI_DATABASE_URL"] = f"sqlite:///{self.database}"
        self.environment["MARKETAI_DATA_DIR"] = str(Path(self.temp.name) / "data")

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

    def test_existing_idempotency_rows_are_backfilled_on_upgrade(self) -> None:
        upgraded_to_prev = self._alembic("upgrade", "o1a2b3c4d5e6")
        self.assertEqual(upgraded_to_prev.returncode, 0, upgraded_to_prev.stderr)

        with sqlite3.connect(self.database) as conn:
            conn.execute(
                "INSERT INTO workspaces "
                "(id, name, default_currency, lifecycle, budget_currency, created_at) "
                "VALUES ('ws_bf', 'backfill', 'USD', 'active', 'USD', '2026-09-03 00:00:00')"
            )
            conn.execute(
                "INSERT INTO library_idempotency "
                "(id, workspace_id, idempotency_key, command, initiator_id, "
                "request_fingerprint, response_json, created_at) VALUES "
                "('idem_bf1', 'ws_bf', 'key_bf1', 'create', 'actor1', "
                "'fp1', '{\"status\": 201}', '2026-09-03 00:00:00')"
            )
            conn.commit()

        upgraded = self._alembic("upgrade", "head")
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)

        with sqlite3.connect(self.database) as conn:
            row = conn.execute(
                "SELECT lifecycle_state, response_kind, command_identity_hash, "
                "contract_schema_version, updated_at "
                "FROM library_idempotency WHERE id = 'idem_bf1'"
            ).fetchone()
        self.assertIsNotNone(row)
        lifecycle_state, response_kind, cmd_hash, schema_ver, updated_at = row
        self.assertEqual(lifecycle_state, "terminal")
        self.assertEqual(response_kind, "success")
        self.assertEqual(cmd_hash, "idem_bf1")
        self.assertEqual(schema_ver, 1)
        self.assertIsNotNone(updated_at)

    def test_pending_idempotency_row_remains_pending_on_upgrade(self) -> None:
        upgraded_to_prev = self._alembic("upgrade", "o1a2b3c4d5e6")
        self.assertEqual(upgraded_to_prev.returncode, 0, upgraded_to_prev.stderr)

        with sqlite3.connect(self.database) as conn:
            conn.execute(
                "INSERT INTO workspaces "
                "(id, name, default_currency, lifecycle, budget_currency, created_at) "
                "VALUES ('ws_pending', 'pending', 'USD', 'active', 'USD', "
                "'2026-09-03 00:00:00')"
            )
            conn.execute(
                "INSERT INTO library_idempotency "
                "(id, workspace_id, idempotency_key, command, initiator_id, "
                "request_fingerprint, response_json, created_at) VALUES "
                "('idem_pending', 'ws_pending', 'key_pending', 'fork', 'actor1', "
                "'fp1', '{\"_state\": \"pending\"}', '2026-09-03 00:00:00')"
            )
            conn.commit()

        upgraded = self._alembic("upgrade", "head")
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
        with sqlite3.connect(self.database) as conn:
            state = conn.execute(
                "SELECT lifecycle_state, response_kind "
                "FROM library_idempotency WHERE id = 'idem_pending'"
            ).fetchone()
        self.assertEqual(state, ("pending", "pending"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
