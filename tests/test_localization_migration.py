"""Localization migration test (Issue #24, Task 7).

Validates the Alembic migration for 32 new tables + 2 existing table
evolutions can be applied, rolled back, and re-applied cleanly.
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


class TestLocalizationMigration(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="marketai-loc-migration-")
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

    def test_upgrade_from_scratch(self):
        result = self._alembic("upgrade", "head")
        self.assertEqual(result.returncode, 0, result.stderr)

        with sqlite3.connect(self.database) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            # Spot-check key new tables exist
            for expected in (
                "market_policies",
                "market_policy_versions",
                "market_policy_version_events",
                "truth_packages",
                "truth_package_markets",
                "truth_facts",
                "localization_batches",
                "localization_items",
                "localization_attempts",
                "market_asset_bindings",
                "localized_asset_lineages",
            ):
                self.assertIn(expected, tables, f"missing table {expected}")

            # Verify Alembic landed at the current overall head --
            # ad2_target_preview (Issue #87 Task 1: Scale target preview
            # tables and command identity hash), which sits on
            # ad1_check_names (post-#92 PostgreSQL schema convergence), which
            # sits on ac1_enrollment_lineage (Issue #86 Task 12: Enrollment
            # lineage backfill), which sits on ab1_scale_lineage (Issue #86 Task 7:
            # Scale Enrollment lineage tables), which sits on
            # aa1_identity_kind (Task 4: is_platform_workspace on
            # marketai_workspace_admission()), which sits on c80a1b2c3d4e
            # (Issue #80 Localization release guards), which sits on
            # z1a2b3c4d5e6 / y1a2b3c4d5e6 (#76 onboarding fact delivery),
            # then b79c2d3e4f5a (Issue #79 launch crash consistency),
            # a79b1c2d3e4f (Issue #79 launch confirmations), and
            # f5eeeab4e9b9 (Issue #25 Scale), which sits on x1a2b3c4d5e6
            # (workspace projection repair), the Issue #49 revisions, and
            # u1a2b3c4d5e6 (this migration's own revision).
            revisions = {
                row[0]
                for row in conn.execute("SELECT version_num FROM alembic_version")
            }
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
            self.assertIn("zzi132a1b2c3", revisions)

    def test_downgrade_and_upgrade(self):
        up = self._alembic("upgrade", "head")
        self.assertEqual(up.returncode, 0, up.stderr)

        down = self._alembic("downgrade", "t1a2b3c4d5e6")
        self.assertEqual(down.returncode, 0, down.stderr)

        with sqlite3.connect(self.database) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            # Our tables should be gone
            self.assertNotIn("market_policies", tables)
            self.assertNotIn("localization_items", tables)
            self.assertNotIn("truth_packages", tables)

            revisions = {
                row[0]
                for row in conn.execute("SELECT version_num FROM alembic_version")
            }
            self.assertIn("t1a2b3c4d5e6", revisions)
            self.assertNotIn("u1a2b3c4d5e6", revisions)

        # Re-upgrade
        reup = self._alembic("upgrade", "head")
        self.assertEqual(reup.returncode, 0, reup.stderr)


if __name__ == "__main__":
    unittest.main()
