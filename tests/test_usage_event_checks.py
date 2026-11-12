"""The CHECK constraints of ``usage_events`` on SQLite (Issue #145, #8 slice S3).

Migration ``zzk145a1b2c3`` adds four checks (spec §2.12): the measurement
kind is ``delta`` or ``gauge``; a ``delta`` goes ``increase`` or
``decrease`` and a ``gauge`` is ``set``; the quantity is not negative; the
execution mode is one of the four Usage buckets. Units and which metric uses
which kind stay in the registry.

On a migrated SQLite database:

* head carries exactly the checks ``UsageEventRecord`` declares, word for
  word, so the model and the migration cannot drift apart unnoticed
  (``alembic check`` does not compare CHECK constraints);
* rows written by every S1 and S2 writer before the checks exist, and every
  registered metric in every execution mode, survive the upgrade's rebuild
  unchanged, then a downgrade and an upgrade again;
* each check refuses the row that breaks it, through the statement
  ``record_many`` sends: ``ON CONFLICT DO NOTHING`` skips unique violations
  only, so a bad row is an error, never a counted duplicate;
* a row that breaks a check refuses the upgrade before the rebuild starts
  and leaves the database as it was;
* a trigger on the table survives the rebuild both ways.

The PostgreSQL side (``NOT VALID`` then ``VALIDATE``, the real roles, the
append-only trigger and the privilege matrix) is in ``tests/test_usage_pg.py``.
"""
from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import CheckConstraint, create_engine, inspect
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import WorkspaceRecord
from app.entitlements.usage import (
    METRICS,
    USAGE_EXECUTION_MODES,
    UsageFact,
    UsageRecorder,
    record_asset_version_generated,
    record_generation_item_failed,
    record_provider_call,
    usage_semantic_key,
)
from app.entitlements.usage_models import UsageEventRecord
from app.platform.operations import OperationState, create_operation, transition_operation
from app.platform.tenancy import TenantContext

ROOT = Path(__file__).resolve().parents[1]
REVISION, DOWN_REVISION = "zzk145a1b2c3", "zzj113a1b2c3"
WORKSPACE = "ws_usage_checks"
CTX = TenantContext(workspace_id=WORKSPACE, role="owner", initiator_id="owner_c",
                    request_id="req-checks", trace_id="trace-checks")

#: (constraint it breaks, columns that break it). Each bad row breaks exactly one.
BAD_ROWS = (
    ("measurement_kind_valid", {"measurement_kind": "counter"}),
    ("direction_matches_kind", {"direction": "set"}),
    ("direction_matches_kind", {"measurement_kind": "gauge", "direction": "increase"}),
    ("quantity_nonnegative", {"quantity": Decimal("-1")}),
    ("execution_mode_valid", {"execution_mode": "controlled_mock"}),
)
COLUMNS = ", ".join(column.name for column in UsageEventRecord.__table__.columns)


def model_checks() -> dict[str, str]:
    """``UsageEventRecord``'s checks: full name -> predicate, whitespace collapsed."""
    return {
        constraint.name: " ".join(str(constraint.sqltext).split())
        for constraint in UsageEventRecord.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }


def valid_row(**overrides) -> dict:
    now = datetime.now(timezone.utc)
    row = {
        "id": f"ue_{uuid.uuid4().hex}", "workspace_id": WORKSPACE, "metric_key": "provider_calls",
        "measurement_kind": "delta", "direction": "increase", "quantity": Decimal("1"), "unit": "call",
        "execution_mode": "byok", "source_resource_type": "provider_call_attempt",
        "source_resource_id": f"pca_{uuid.uuid4().hex[:8]}", "semantic_key_hash": uuid.uuid4().hex * 2,
        "occurred_at": now, "created_at": now,
    }
    row.update(overrides)
    return row


class UsageEventChecksMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="marketai-usage-checks-")
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "usage.db"
        self.environment = {
            **os.environ,
            "MARKETAI_DATABASE_URL": f"sqlite:///{self.database}",
            "MARKETAI_DATA_DIR": str(Path(self.temp.name) / "data"),
        }
        self._alembic("upgrade", "head")
        self.engine = create_engine(f"sqlite:///{self.database}")
        self.addCleanup(self.engine.dispose)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False, autoflush=False)
        session_local = patch("app.adapters.persistence.database.SessionLocal", self.sessions)
        session_local.start()
        self.addCleanup(session_local.stop)
        with platform_session() as session:
            session.add(WorkspaceRecord(id=WORKSPACE, name=WORKSPACE))

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "alembic", *arguments], cwd=ROOT, env=self.environment,
            capture_output=True, text=True, check=False,
        )

    def _alembic(self, *arguments: str) -> str:
        completed = self._run(*arguments)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout

    def _revision(self) -> str:
        with sqlite3.connect(self.database) as connection:
            return connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]

    def _checks(self) -> dict[str, str]:
        self.engine.dispose()  # the inspector must see the rebuilt table
        return {
            check["name"]: " ".join(check["sqltext"].split())
            for check in inspect(self.engine).get_check_constraints("usage_events")
        }

    def _triggers(self) -> list[tuple[str, str]]:
        with sqlite3.connect(self.database) as connection:
            return connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'usage_events' "
                "ORDER BY name").fetchall()

    def _rows(self) -> list[tuple]:
        with sqlite3.connect(self.database) as connection:
            return connection.execute(f"SELECT {COLUMNS} FROM usage_events ORDER BY id").fetchall()

    def _write_as_every_writer(self) -> None:
        """Rows the way S1 (#69) and S2 (#87) write them, plus every metric in every mode."""
        now = datetime.now(timezone.utc)
        with tenant_session(CTX) as session:
            for mode in ("byok", "sandbox", "mock", "controlled_mock"):
                attempt = SimpleNamespace(id=f"pca_{mode}", workspace_id=WORKSPACE, execution_mode=mode)
                self.assertEqual(record_provider_call(session, CTX, attempt, occurred_at=now).written, 1)
            version = SimpleNamespace(id="av_checks", workspace_id=WORKSPACE, origin_kind="generation",
                                      execution_mode="byok", created_at=now)
            self.assertEqual(record_asset_version_generated(session, CTX, version).written, 1)
            self.assertEqual(record_generation_item_failed(
                session, CTX, run_item_id="ri_checks", revision=2, execution_mode="controlled_mock",
                occurred_at=now).written, 1)
            facts = [
                UsageFact(metric_key=metric.key, quantity=Decimal(quantity), unit=metric.unit,
                          execution_mode=mode, occurred_at=now,
                          semantic_key_hash=usage_semantic_key(metric.key, "test", f"{mode}_{quantity}"))
                for metric in METRICS.values()
                for mode in USAGE_EXECUTION_MODES
                for quantity in ("0", "2.5")
            ]
            self.assertEqual(UsageRecorder().record_many(session, CTX, facts).written, len(facts))
        with tenant_session(CTX) as session:
            operation_id = create_operation(session, command_type="usage.checks_test", context=CTX).id
        for state in (OperationState.RUNNING, OperationState.SUCCEEDED):
            with tenant_session(CTX) as session:
                transition_operation(session, operation_id, state, reason="checks test", actor="tester")

    def test_head_carries_exactly_the_checks_the_model_declares(self):
        self.assertEqual(self._revision(), REVISION)
        self.assertEqual(self._checks(), model_checks())
        self.assertEqual(set(model_checks()), {
            "ck_usage_events_measurement_kind_valid", "ck_usage_events_direction_matches_kind",
            "ck_usage_events_quantity_nonnegative", "ck_usage_events_execution_mode_valid",
        })

    def test_rows_every_writer_wrote_before_the_checks_survive_the_upgrade(self):
        self._alembic("downgrade", DOWN_REVISION)
        self.assertEqual(self._checks(), {})
        self._write_as_every_writer()
        before = self._rows()
        self.assertEqual(len(before), 4 + 1 + 1 + len(METRICS) * len(USAGE_EXECUTION_MODES) * 2 + 2)
        self.assertEqual({(kind, direction) for _, _, _, kind, direction, *_ in before}, {("delta", "increase")})

        self._alembic("upgrade", REVISION)
        self.assertEqual(self._checks(), model_checks())
        self.assertEqual(self._rows(), before)
        self._alembic("downgrade", DOWN_REVISION)
        self.assertEqual((self._checks(), self._rows()), ({}, before))
        self._alembic("upgrade", "head")
        self.assertEqual((self._checks(), self._rows()), (model_checks(), before))

    def test_each_check_refuses_the_row_that_breaks_it_through_the_record_many_statement(self):
        accepted = sqlite_dialect.insert(UsageEventRecord.__table__).values(**valid_row()).on_conflict_do_nothing()
        with tenant_session(CTX) as session:
            self.assertEqual(session.execute(accepted, execution_options={"preserve_rowcount": True}).rowcount, 1)
        for name, overrides in BAD_ROWS:
            with self.subTest(check=name, row=overrides):
                statement = (sqlite_dialect.insert(UsageEventRecord.__table__)
                             .values(**valid_row(**overrides)).on_conflict_do_nothing())
                with self.assertRaises(IntegrityError) as caught:
                    with tenant_session(CTX) as session:
                        session.execute(statement)
                self.assertIn(f"CHECK constraint failed: ck_usage_events_{name}", str(caught.exception))
        self.assertEqual(len(self._rows()), 1)

    def test_a_row_that_breaks_a_check_refuses_the_upgrade_and_changes_nothing(self):
        self._alembic("downgrade", DOWN_REVISION)
        with tenant_session(CTX) as session:
            session.add(UsageEventRecord(**valid_row(quantity=Decimal("-2"))))
            session.add(UsageEventRecord(**valid_row(execution_mode="controlled_mock")))
        before = self._rows()

        refused = self._run("upgrade", REVISION)
        self.assertNotEqual(refused.returncode, 0)
        self.assertRegex(refused.stderr, re.escape(
            "nothing was changed: ck_usage_events_quantity_nonnegative: 1, "
            "ck_usage_events_execution_mode_valid: 1"))
        self.assertEqual(self._revision(), DOWN_REVISION)
        self.assertEqual((self._checks(), self._rows()), ({}, before))
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE name LIKE '_alembic_tmp%'").fetchone()[0], 0)

    def test_a_trigger_on_the_table_survives_the_rebuild_both_ways(self):
        self._alembic("downgrade", DOWN_REVISION)
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "CREATE TRIGGER trg_usage_events_immutable BEFORE UPDATE ON usage_events "
                "BEGIN SELECT RAISE(ABORT, 'usage_events is append-only'); END")
        trigger = self._triggers()
        self.assertEqual([name for name, _ in trigger], ["trg_usage_events_immutable"])
        with tenant_session(CTX) as session:
            session.add(UsageEventRecord(**valid_row()))

        self._alembic("upgrade", REVISION)
        self.assertEqual(self._triggers(), trigger)
        self._alembic("downgrade", DOWN_REVISION)
        self.assertEqual(self._triggers(), trigger)
        self._alembic("upgrade", "head")
        self.assertEqual(self._triggers(), trigger)
        with sqlite3.connect(self.database) as connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "usage_events is append-only"):
                connection.execute("UPDATE usage_events SET quantity = 3")


if __name__ == "__main__":
    unittest.main()
