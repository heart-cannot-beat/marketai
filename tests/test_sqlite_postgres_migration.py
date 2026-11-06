"""Real SQLite snapshot -> PostgreSQL count, money and hash rehearsal."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch


TARGET_URL = os.environ.get("DOMAIN_OWNER_DATABASE_URL")


class SelfReferenceOrderingTests(unittest.TestCase):
    def test_composite_self_fk_orders_a_parent_before_its_child(self) -> None:
        from app.adapters.persistence.models import AssetRecord
        from app.adapters.persistence.sqlite_postgres_migration import _insert_rows

        class RecordingConnection:
            def __init__(self) -> None:
                self.batches: list[list[str]] = []

            def execute(self, _statement, rows) -> None:
                self.batches.append([row["id"] for row in rows])

        connection = RecordingConnection()
        _insert_rows(
            connection,
            AssetRecord.__table__,
            [
                {
                    "id": "asset_child",
                    "workspace_id": "ws_alpha",
                    "reused_from_asset_id": "asset_parent",
                },
                {
                    "id": "asset_parent",
                    "workspace_id": "ws_alpha",
                    "reused_from_asset_id": None,
                },
            ],
        )

        self.assertEqual(connection.batches, [["asset_parent"], ["asset_child"]])


@unittest.skipUnless(
    TARGET_URL,
    "set DOMAIN_OWNER_DATABASE_URL to an empty, migrated PostgreSQL domain database",
)
class SqlitePostgresMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        from sqlalchemy import create_engine

        self.root = Path(__file__).resolve().parents[1]
        self.temp = tempfile.TemporaryDirectory(prefix="marketai-sqlite-pg-")
        self.source = Path(self.temp.name) / "legacy.db"
        environment = dict(os.environ)
        environment["MARKETAI_DATABASE_URL"] = f"sqlite:///{self.source.as_posix()}"
        completed = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "f1a2c3d4e5b6"],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        # Keep the writer open so committed rows remain in a real WAL sidecar.
        # The rehearsal must snapshot that durable content without modifying
        # either the database file or the WAL.
        self.source_connection = sqlite3.connect(self.source)
        connection = self.source_connection
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "INSERT INTO workspaces (id, name, default_currency, created_at) "
            "VALUES ('ws_migrate', 'Migration source', 'USD', '2026-08-28 00:00:00')"
        )
        connection.execute(
            "INSERT INTO products "
            "(id, workspace_id, name, description, selling_points, audience, "
            "brand_notes, ref_images, economics, revision, created_at, updated_at) "
            "VALUES ('prod_migrate', 'ws_migrate', 'Migrated product', '', '[]', '', "
            "'', '[]', '{}', 1, '2026-08-28 00:00:00', '2026-08-28 00:00:00')"
        )
        connection.execute(
            "INSERT INTO plans "
            "(id, workspace_id, product_id, schema_id, mode, cap, total_full, "
            "snapshot_hash, created_at) VALUES ('plan_migrate', 'ws_migrate', "
            "'prod_migrate', NULL, 'full', 1, 1, 'snapshot', '2026-08-28 00:00:00')"
        )
        connection.execute(
            "INSERT INTO generation_runs "
            "(id, workspace_id, product_id, plan_id, provider, model, aspect_ratio, "
            "image_size, variants, use_ref, status, total, done, failed, est_cost, "
            "currency, revision, created_at, updated_at) VALUES ('run_migrate', "
            "'ws_migrate', 'prod_migrate', 'plan_migrate', 'mock', 'mock', '1:1', "
            "'1024', 1, 0, 'done', 1, 1, 0, 12.340000, 'USD', 1, "
            "'2026-08-28 00:00:00', '2026-08-28 00:00:00')"
        )
        connection.execute(
            "INSERT INTO assets "
            "(id, workspace_id, product_id, run_id, plan_entry_id, combo_id, combo_code, "
            "tags_json, filename, provider, model, prompt, fingerprint, reused_from_asset_id, "
            "status, error_code, error_detail, created_at, updated_at) VALUES "
            "('asset_migrate', 'ws_migrate', 'prod_migrate', 'run_migrate', NULL, 'combo', "
            "'A1', '{}', 'asset.png', 'mock', 'mock', 'prompt', 'fingerprint', NULL, "
            "'ok', '', '', '2026-08-28 00:00:00', '2026-08-28 00:00:00')"
        )
        connection.execute(
            "INSERT INTO asset_versions "
            "(id, workspace_id, asset_id, parent_version_id, kind, aspect_ratio, language, "
            "object_key, content_sha256, byte_size, mime_type, currency, truth_package_id, "
            "created_at) VALUES ('version_migrate', 'ws_migrate', 'asset_migrate', NULL, "
            "'generated', '1:1', 'en', 'asset.png', 'content', 4, 'image/png', 'USD', "
            "NULL, '2026-08-28 00:00:00')"
        )
        connection.execute(
            "INSERT INTO asset_versions "
            "(id, workspace_id, asset_id, parent_version_id, kind, aspect_ratio, language, "
            "object_key, content_sha256, byte_size, mime_type, currency, truth_package_id, "
            "created_at) VALUES ('version_migrate_child', 'ws_migrate', 'asset_migrate', "
            "'version_migrate', 'edited', '1:1', 'en', 'asset-edited.png', "
            "'content-edited', 5, 'image/png', 'USD', NULL, '2026-08-28 00:01:00')"
        )
        connection.execute(
            "INSERT INTO cost_ledger "
            "(id, workspace_id, run_id, job_id, entry_type, amount, currency, note, created_at) "
            "VALUES ('cost_migrate', 'ws_migrate', 'run_migrate', NULL, 'actual', "
            "12.340000, 'USD', 'migration proof', '2026-08-28 00:00:00')"
        )
        connection.execute(
            "INSERT INTO cost_ledger "
            "(id, workspace_id, run_id, job_id, entry_type, amount, currency, note, created_at) "
            "VALUES ('cost_migrate_eur', 'ws_migrate', NULL, NULL, 'actual', "
            "5.500000, 'EUR', 'currency boundary proof', '2026-08-28 00:00:01')"
        )
        connection.commit()
        self.before_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.wal = Path(f"{self.source}-wal")
        self.assertTrue(self.wal.is_file())
        self.before_wal_hash = hashlib.sha256(self.wal.read_bytes()).hexdigest()
        self.target = create_engine(TARGET_URL, future=True)

    def tearDown(self) -> None:
        from app.adapters.persistence import models  # noqa: F401
        from app.adapters.persistence.sqlite_postgres_migration import (
            _migration_tables,
        )

        with self.target.begin() as connection:
            for table in reversed(_migration_tables()):
                connection.execute(table.delete())
        self.target.dispose()
        self.source_connection.close()
        self.temp.cleanup()

    def test_snapshot_is_upgraded_copied_verified_and_reported(self) -> None:
        from app.adapters.persistence.sqlite_postgres_migration import (
            MigrationValidationError,
            _head_revision,
            migrate_sqlite_to_postgres,
            write_report,
        )

        report = migrate_sqlite_to_postgres(self.source, TARGET_URL)
        expected_head = _head_revision()

        self.assertTrue(report["ok"])
        self.assertEqual(report["schema_version"], 2)
        self.assertTrue(report["source"]["unchanged"])
        self.assertEqual(report["source"]["revision"], "f1a2c3d4e5b6")
        self.assertEqual(report["staging_revision"], expected_head)
        self.assertEqual(report["target"]["revision"], expected_head)
        self.assertEqual(report["target"]["role"], "marketai_domain_owner")
        self.assertEqual(report["tables"]["workspaces"]["source_count"], 1)
        self.assertEqual(report["tables"]["products"]["target_count"], 1)
        self.assertEqual(report["tables"]["assets"]["target_count"], 1)
        self.assertEqual(report["tables"]["asset_versions"]["target_count"], 2)
        self.assertEqual(
            report["tables"]["cost_ledger"]["source_money_totals"],
            {"amount": {"EUR": "5.5", "USD": "12.34"}},
        )
        self.assertEqual(
            report["tables"]["generation_runs"]["source_money_totals"],
            {"est_cost": {"USD": "12.34"}},
        )
        self.assertEqual(
            report["tables"]["cost_ledger"]["source_hash"],
            report["tables"]["cost_ledger"]["target_hash"],
        )
        self.assertTrue(all(row["verified"] for row in report["tables"].values()))
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.before_hash)
        self.assertEqual(hashlib.sha256(self.wal.read_bytes()).hexdigest(), self.before_wal_hash)

        report_path = Path(self.temp.name) / "report.json"
        write_report(report, report_path)
        persisted = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["summary"], report["summary"])

        with self.assertRaisesRegex(MigrationValidationError, "target is not empty"):
            migrate_sqlite_to_postgres(self.source, TARGET_URL)

    def test_a_source_change_rolls_the_target_transaction_back(self) -> None:
        from sqlalchemy import func, select

        from app.adapters.persistence.models import WorkspaceRecord
        from app.adapters.persistence import sqlite_postgres_migration as migration

        actual_state = migration._source_files_state
        calls = 0

        def changed_on_final_check(path):
            nonlocal calls
            calls += 1
            state = actual_state(path)
            if calls == 2:
                state = deepcopy(state)
                state["database"]["sha256"] = "changed-during-copy"
            return state

        with patch.object(
            migration, "_source_files_state", side_effect=changed_on_final_check
        ):
            with self.assertRaisesRegex(
                migration.MigrationValidationError, "source SQLite file changed"
            ):
                migration.migrate_sqlite_to_postgres(self.source, TARGET_URL)

        with self.target.connect() as connection:
            self.assertEqual(
                connection.scalar(select(func.count()).select_from(WorkspaceRecord)),
                0,
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
