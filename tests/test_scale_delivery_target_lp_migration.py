"""The DeliveryTarget Landing Page migration on SQLite (#129).

A fresh install builds ``scale_delivery_targets`` from the current model, so
the old shape -- ``landing_page_version_id`` referencing ``library_versions``
-- exists only after downgrading this revision. Each test migrates its own
database file in a subprocess to head and down one revision. The backfill
test then holds rows as the seeds and the marketing handoff left them, with
only the columns the migration reads, and runs this revision's ``upgrade``
on a connection without foreign-key enforcement (the Alembic environment's
SQLite foreign-key check would refuse such partial rows).

Covers the backfill (a ``landing_page`` LibraryVersion yields the hash it
carries; an ``offer`` LibraryVersion yields the hash the linked Winner's
ExperimentVersion froze; anything else keeps its value and is counted), the
rebuilt constraints, the widened command-type check, and a downgrade that
keeps a hash no LibraryVersion has and leaves the foreign key off on SQLite.
Fictional brand only: Acme Charge.
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine

from app.adapters.persistence.database import Base

ROOT = Path(__file__).resolve().parents[1]
REVISION = "zzh129a1b2c3"
PREVIOUS = "zzg7d8e9f0a1"
WORKSPACE = "ws_acme_charge_lp"
LP_HASH = "a" * 64
WINNER_LP_HASH = "b" * 64
MIGRATION = importlib.import_module(f"migrations.versions.{REVISION}_delivery_target_landing_page_hash")


def _dummy(column: str, declared: str, row_id: str):
    declared = declared.upper()
    if "INT" in declared or "BOOL" in declared or "NUMERIC" in declared:
        return 1
    if "DATE" in declared or "TIME" in declared:
        return "2026-11-01 00:00:00.000000"
    if "JSON" in declared:
        return "{}"
    if "BLOB" in declared or "BINARY" in declared:
        return b"x"
    return f"{column}_{row_id}"[:60]


class LandingPageHashMigration(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="marketai-sdt-lp-migration-")
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "migration.db"
        self.environment = dict(os.environ, MARKETAI_DATABASE_URL=f"sqlite:///{self.database}",
                                MARKETAI_DATA_DIR=str(Path(self.temp.name) / "data"))
        self._alembic("upgrade", "head")
        self._alembic("downgrade", PREVIOUS)

    def _alembic(self, *arguments: str, ok: bool = True) -> subprocess.CompletedProcess:
        completed = subprocess.run([sys.executable, "-m", "alembic", *arguments], cwd=ROOT,
                                   env=self.environment, capture_output=True, text=True, check=False)
        if ok:
            self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        self.addCleanup(connection.close)
        # Rows below carry only what the migration reads and what the table
        # rebuild re-checks; their other references are not this test's subject.
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("PRAGMA ignore_check_constraints = ON")
        return connection

    def _columns(self, connection, table: str) -> list[tuple]:
        return list(connection.execute(f'PRAGMA table_info("{table}")'))

    def _insert(self, connection, table: str, **values) -> None:
        row_id = str(values.get("id", ""))
        for _cid, name, declared, notnull, default, primary in self._columns(connection, table):
            if name not in values and (notnull or primary) and default is None:
                values[name] = _dummy(name, declared or "", row_id)
        names = ", ".join(f'"{name}"' for name in values)
        marks = ", ".join("?" for _ in values)
        connection.execute(f'INSERT INTO "{table}" ({names}) VALUES ({marks})', list(values.values()))

    def _library_version(self, connection, version_id: str, entry_type: str, content: dict) -> None:
        self._insert(connection, "library_versions", id=version_id, workspace_id=WORKSPACE,
                     entry_id=f"le_{version_id}", entry_type=entry_type, version_no=1,
                     content_json=json.dumps(content), state="effective")

    def _target(self, connection, target_id: str, landing_page: str) -> None:
        self._insert(connection, "scale_delivery_targets", id=target_id, workspace_id=WORKSPACE,
                     meta_mode="customer_sandbox", scenario_scope_key="__no_scenario__",
                     landing_page_version_id=landing_page, source_type="curated_scenario",
                     scenario_version_id="scen_acme_charge", source_manifest="{}")

    def _seed_old_shape(self) -> None:
        connection = self._connect()
        self.assertIn("landing_page_version_id",
                      [column[1] for column in self._columns(connection, "scale_delivery_targets")])
        # The golden-path seed: a landing_page version keyed by the frozen hash.
        self._library_version(connection, LP_HASH, "landing_page",
                              {"source": "experiment_landing_page_evidence", "landing_page_evidence_hash": LP_HASH})
        self._target(connection, "sdt_seeded_lp", LP_HASH)
        # A marketing-handoff Winner: the target named its OfferVersion, and a
        # candidate ties the target to that Winner.
        self._library_version(connection, "lv_acme_offer", "offer",
                              {"landing_page_url": "https://acme-charge.example/dock"})
        self._target(connection, "sdt_marketing", "lv_acme_offer")
        self._insert(connection, "experiment_versions", id="ver_marketing", workspace_id=WORKSPACE,
                     experiment_id="exp_marketing", landing_page_evidence_hash=WINNER_LP_HASH)
        self._insert(connection, "experiment_decisions", id="dec_marketing", workspace_id=WORKSPACE,
                     experiment_version_id="ver_marketing")
        self._insert(connection, "scale_enrollment_candidates", id="cand_marketing", workspace_id=WORKSPACE,
                     meta_mode="customer_sandbox", scenario_scope_key="__no_scenario__",
                     winner_decision_id="dec_marketing", delivery_target_id="sdt_marketing")
        # An Offer target nothing links to a Winner, and one naming a product version.
        self._target(connection, "sdt_orphan_offer", "lv_acme_offer")
        self._library_version(connection, "lv_acme_product", "product", {})
        self._target(connection, "sdt_other", "lv_acme_product")
        connection.commit()

    def _values(self) -> dict[str, str]:
        connection = self._connect()
        return dict(connection.execute("SELECT id, landing_page_evidence_hash FROM scale_delivery_targets"))

    def _upgrade_in_process(self) -> str:
        engine = create_engine(f"sqlite:///{self.database}")
        self.addCleanup(engine.dispose)
        with self.assertLogs("alembic.runtime.migration", level=logging.INFO) as logs:
            with engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
                connection.commit()
                context = MigrationContext.configure(
                    connection, opts={"target_metadata": Base.metadata, "render_as_batch": True},
                )
                with connection.begin(), Operations.context(context):
                    MIGRATION.upgrade()
        return "\n".join(logs.output)

    def _downgrade_in_process(self) -> str:
        engine = create_engine(f"sqlite:///{self.database}")
        self.addCleanup(engine.dispose)
        with self.assertLogs("alembic.runtime.migration", level=logging.WARNING) as logs:
            with engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
                connection.commit()
                context = MigrationContext.configure(
                    connection, opts={"target_metadata": Base.metadata, "render_as_batch": True},
                )
                with connection.begin(), Operations.context(context):
                    MIGRATION.downgrade()
        return "\n".join(logs.output)

    def test_upgrade_renames_the_column_and_backfills_each_kind_of_row(self):
        self._seed_old_shape()

        log = self._upgrade_in_process()

        self.assertEqual(self._values(), {
            "sdt_seeded_lp": LP_HASH,
            "sdt_marketing": WINNER_LP_HASH,
            "sdt_orphan_offer": "lv_acme_offer",
            "sdt_other": "lv_acme_product",
        })
        self.assertIn("landing_page_version_resolved=1", log)
        self.assertIn("offer_version_resolved=1", log)
        self.assertIn("offer_version_kept=1", log)
        self.assertIn("other_kept=1", log)
        self.assertIn("2 DeliveryTarget(s) keep a landing page value no Winner froze", log)

        connection = self._connect()
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'scale_delivery_targets'").fetchone()[0]
        self.assertNotIn("fk_sdt_landing_page_version", table_sql)
        self.assertNotIn("landing_page_version_id", table_sql)
        self.assertIn("uq_sdt_exact_context", table_sql)
        self.assertIn("uq_sdt_enrollment_binding", table_sql)
        journal_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'scale_command_journal'").fetchone()[0]
        self.assertIn("'create_delivery_target'", journal_sql)

    def test_downgrade_keeps_a_hash_with_no_library_version_without_the_foreign_key(self):
        self._alembic("upgrade", "head")
        connection = self._connect()
        self._insert(connection, "scale_delivery_targets", id="sdt_hash_only", workspace_id=WORKSPACE,
                     meta_mode="customer_sandbox", scenario_scope_key="__no_scenario__",
                     landing_page_evidence_hash=LP_HASH, source_type="curated_scenario",
                     scenario_version_id="scen_acme_charge", source_manifest="{}")
        connection.commit()

        downgraded = self._downgrade_in_process()

        self.assertIn("1 DeliveryTarget(s) name a landing page evidence hash with no LibraryVersion", downgraded)
        connection = self._connect()
        self.assertEqual(dict(connection.execute(
            "SELECT id, landing_page_version_id FROM scale_delivery_targets")), {"sdt_hash_only": LP_HASH})
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'scale_delivery_targets'").fetchone()[0]
        self.assertNotIn("fk_sdt_landing_page_version", table_sql)
        self.assertIn("uq_sdt_exact_context", table_sql)

    def test_an_empty_table_migrates_both_ways(self):
        self._alembic("upgrade", "head")
        self.assertEqual(self._values(), {})
        self._alembic("downgrade", PREVIOUS)
        self._alembic("upgrade", "head")
        current = self._alembic("current")
        self.assertIn(REVISION, current.stdout)


if __name__ == "__main__":
    unittest.main()
