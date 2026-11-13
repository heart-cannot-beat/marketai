"""The Beta default ScalePolicyVersions: code constants and publishing migrations
(v1 #127; v2 #132, #133).

* each constant covers every ``ScalePolicyVersionRecord`` column; the latest
  runs the deployed algorithm; the evaluator's snapshot reads the contract's
  values out of them (v2: the floor of the Episode's currency, ``None``
  outside the table); a published constant's content is pinned;
* on a SQLite database that only ran the migrations, each published row
  equals its constant column for column, ``eligible_live_policy`` returns v2
  for every mode it permits and ``None`` for the others, running a migration
  again adds no row, and downgrade deletes a version only while nothing
  names it.

``tests/test_scale_platform_policy_pg.py`` covers PostgreSQL and the real
roles. Fictional brand only: Acme Charge.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session

from app.adapters.persistence.models import WorkspaceRecord
from app.scale.evaluator import CURRENT_ALGORITHM_VERSION
from app.scale.evidence import _policy_snapshot
from app.scale.live_trigger import eligible_live_policy
from app.scale.models import ScaleEvaluationRunRecord, ScalePolicyVersionRecord
from app.scale.platform_policy import (
    BETA_DEFAULT_POLICY_V1,
    BETA_DEFAULT_POLICY_V2,
    PUBLISHED_POLICY_VERSIONS,
)
from app.scale.types import DataQualityIssueCode

ROOT = Path(__file__).resolve().parents[1]
REVISION = "zzg7d8e9f0a1"
PREVIOUS = "zzf6b7c8d9e0"
MIGRATION = importlib.import_module(f"migrations.versions.{REVISION}_publish_beta_default_scale_policy")
V2_REVISION = "zzi132a1b2c3"
V2_PREVIOUS = "zzh129a1b2c3"
V2_MIGRATION = importlib.import_module(f"migrations.versions.{V2_REVISION}_publish_beta_default_scale_policy_v2")
MODES = ("customer_sandbox", "internal_validation", "controlled_mock")
PERMITTED = {"customer_sandbox", "internal_validation"}  # #127 P1-B

#: The content of every published constant. A published version never
#: changes (contract §10.1): a new Policy is a new constant and a new
#: migration, and this map gains a line instead.
PINNED_DIGESTS = {
    "spol_beta_default_v1": "a08c4a3f6b0ae13376c535694ec69b177fa26ea9a8f4233dbaf742804c780d24",
    "spol_beta_default_v2": "2063af3d01698e426328f7baa5b6103ce116f79927fcce0b243b99eacbddaa6c",
}

#: #132 table A (confirmed 2026-11-11): each 7-day window's minimum spend.
V2_FLOORS = {
    "USD": Decimal("100"), "EUR": Decimal("90"), "GBP": Decimal("75"), "AUD": Decimal("150"),
    "CAD": Decimal("150"), "JPY": Decimal("16000"), "KRW": Decimal("140000"),
}


def _digest(row: dict) -> str:
    def encode(value):
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        raise TypeError(value)

    canonical = json.dumps(row, default=encode, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _as_utc(value):
    """SQLite returns a timezone-aware column without its zone."""
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class BetaDefaultPolicyConstant(unittest.TestCase):
    def test_every_column_has_a_value(self):
        for version in PUBLISHED_POLICY_VERSIONS:
            with self.subTest(version.id):
                self.assertEqual(set(version.row()), set(ScalePolicyVersionRecord.__table__.columns.keys()))

    def test_the_latest_published_version_runs_the_deployed_algorithm(self):
        self.assertEqual(PUBLISHED_POLICY_VERSIONS, (BETA_DEFAULT_POLICY_V1, BETA_DEFAULT_POLICY_V2))
        self.assertEqual(PUBLISHED_POLICY_VERSIONS[-1].algorithm_version, CURRENT_ALGORITHM_VERSION)
        self.assertEqual(BETA_DEFAULT_POLICY_V1.algorithm_version, "beta-v1")  # replays only (§11)
        self.assertEqual(len({version.id for version in PUBLISHED_POLICY_VERSIONS}),
                         len(PUBLISHED_POLICY_VERSIONS))
        published_at = [version.published_at for version in PUBLISHED_POLICY_VERSIONS]
        self.assertEqual(published_at, sorted(set(published_at)), "strictly later, oldest first")

    def test_the_evaluator_reads_the_contract_values(self):
        snapshot = _policy_snapshot(ScalePolicyVersionRecord(**BETA_DEFAULT_POLICY_V1.row()), "USD")
        expected = {
            "policy_version_id": "spol_beta_default_v1",
            "algorithm_version": "beta-v1",
            "decimal_precision": 28,                              # P3
            "decimal_rounding_mode": "ROUND_HALF_EVEN",           # P3
            "confidence_level": Decimal("0.95"),                  # §11
            "confidence_z": Decimal("1.96"),                      # P2
            "complete_day_floor": 14,                             # §9.1
            "active_day_floor": 10,                               # §9.1
            "impression_floor": 1000,                             # §9.1
            "click_floor": 20,                                    # §9.1
            "purchase_floor": 5,                                  # §9.2
            "spend_floor": Decimal("100"),                        # P4
            "frequency_level_threshold": Decimal("3.5"),          # §12.1
            "frequency_growth_threshold": Decimal("0.2"),         # §12.1
            "ctr_decline_threshold": Decimal("0.25"),             # §12.1
            "ctr_stability_tolerance": Decimal("0.05"),           # P8
            "cvr_decline_threshold": Decimal("0.2"),              # P6
            "cvr_stability_tolerance": Decimal("0.05"),           # P8
            "cpm_increase_threshold": Decimal("0.2"),             # P7
            "delivery_decline_threshold": Decimal("0.2"),         # P7
            "cpa_worsening_threshold": Decimal("0.2"),            # §12.1
            "roas_worsening_threshold": Decimal("0.2"),           # §12.1
            "consecutive_composite_fatigue_hits_required": 2,     # §12.1
            "consecutive_healthy_evaluations_required": 2,        # §14
            "bootstrap_prng_version": "bootstrap-prng-v1",        # spec §2.4
            "bootstrap_draw_count": 10000,                        # spec §2.4
            "bootstrap_min_valid_draw_ratio": Decimal("0.5"),     # P9
            "bootstrap_lower_percentile": Decimal("0.025"),       # spec §2.4
            "bootstrap_upper_percentile": Decimal("0.975"),       # spec §2.4
            "breakdown_min_spend_share": Decimal("0.15"),         # §13
            "breakdown_min_sample_impressions": 1000,             # §13
            "breakdown_min_sample_clicks": 20,                    # §13
            "breakdown_min_sample_purchases": 5,                  # §13
            "fdr_limit": Decimal("0.10"),                         # §13
            "quality_warning_permitted_codes": (),                # §8
            "streak_reset_reason_codes": ("quality_blocked",),    # §12.1
            "advice_construction_parameters": (),                 # P15
            "consecutive_breakdown_hits_required": None,          # v1 has no field (#133)
        }
        self.assertEqual({field: getattr(snapshot, field) for field in expected}, expected)
        self.assertNotIn(DataQualityIssueCode.FRESHNESS_SLO_EXCEEDED, snapshot.quality_warning_permitted_codes)
        # v1's floor carries no currency: every currency reads the same 100.
        jpy = _policy_snapshot(ScalePolicyVersionRecord(**BETA_DEFAULT_POLICY_V1.row()), "JPY")
        self.assertEqual(jpy, snapshot)

    def test_v2_changes_only_what_132_and_133_confirmed(self):
        v1, v2 = BETA_DEFAULT_POLICY_V1.row(), BETA_DEFAULT_POLICY_V2.row()
        changed = {column for column in v1 if v1[column] != v2[column]}
        self.assertEqual(changed, {"id", "algorithm_version", "canonical_schema_version", "signal_thresholds",
                                   "created_at", "published_at"})
        self.assertEqual((v2["id"], v2["algorithm_version"], v2["canonical_schema_version"]),
                         ("spol_beta_default_v2", "beta-v2", "scale-snapshot-v4"))
        added = {key for key in v2["signal_thresholds"] if key not in v1["signal_thresholds"]}
        self.assertEqual(added, {"economic_spend_floor_per_7d_window_by_currency",
                                 "consecutive_breakdown_hits_required"})
        self.assertEqual({key: value for key, value in v2["signal_thresholds"].items() if key not in added},
                         v1["signal_thresholds"])
        self.assertEqual(v2["signal_thresholds"]["consecutive_breakdown_hits_required"], 2)  # #133, §13
        self.assertEqual(v2["created_by"], "platform:migration")
        self.assertEqual(v2["created_at"], v2["published_at"])

    def test_v2_freezes_the_floor_of_the_episode_currency(self):
        record = ScalePolicyVersionRecord(**BETA_DEFAULT_POLICY_V2.row())
        floors = {currency: _policy_snapshot(record, currency).spend_floor for currency in V2_FLOORS}
        self.assertEqual(floors, V2_FLOORS)
        for currency in ("CHF", "usd", "XXX"):
            with self.subTest(currency):
                self.assertIsNone(_policy_snapshot(record, currency).spend_floor)  # exact ISO 4217 match
        snapshot = _policy_snapshot(record, "EUR")
        self.assertEqual((snapshot.policy_version_id, snapshot.algorithm_version,
                          snapshot.consecutive_breakdown_hits_required), ("spol_beta_default_v2", "beta-v2", 2))
        v1_snapshot = _policy_snapshot(ScalePolicyVersionRecord(**BETA_DEFAULT_POLICY_V1.row()), "EUR")
        unchanged = ("policy_version_id", "algorithm_version", "spend_floor", "consecutive_breakdown_hits_required")
        self.assertEqual({field: getattr(snapshot, field) for field in vars(snapshot) if field not in unchanged},
                         {field: getattr(v1_snapshot, field) for field in vars(v1_snapshot) if field not in unchanged})

    def test_v2_keeps_v1s_100_in_the_old_floor_columns(self):
        # #132 P-C A: NOT NULL, not read by beta-v2; the table's USD entry.
        policy = BETA_DEFAULT_POLICY_V2
        self.assertEqual(policy.spend_floor_minimum_daily_spend, Decimal("100"))
        self.assertEqual(policy.evidence_floor_minimum_spend, Decimal("100"))
        table = policy.signal_thresholds["economic_spend_floor_per_7d_window_by_currency"]
        self.assertEqual(Decimal(table["USD"]), policy.spend_floor_minimum_daily_spend)

    def test_columns_that_repeat_a_snapshot_value_hold_the_same_value(self):
        for policy in PUBLISHED_POLICY_VERSIONS:
            with self.subTest(policy.id):
                thresholds = policy.signal_thresholds
                self.assertEqual(policy.z_value, Decimal(thresholds["confidence_z"]))
                self.assertEqual(policy.breakdown_share_gate_minimum_ratio,
                                 Decimal(thresholds["breakdown_min_spend_share"]))
                self.assertEqual(policy.breakdown_sample_gate_minimum, thresholds["breakdown_min_sample_impressions"])
                self.assertEqual(policy.evidence_floor_minimum_spend, policy.spend_floor_minimum_daily_spend)
                self.assertEqual((policy.freshness_floor_maximum_lag_hours, policy.notification_cooldown_hours),
                                 (36, 168))
                self.assertEqual(dict(policy.breakdown_families), {})  # #101 Q6

    def test_it_permits_customer_sandbox_and_internal_validation_only(self):
        for policy in PUBLISHED_POLICY_VERSIONS:
            with self.subTest(policy.id):
                permitted = {mode for mode in MODES if policy.mode_permissions.get(mode) is True}
                self.assertEqual(permitted, PERMITTED)

    def test_a_published_version_never_changes(self):
        self.assertEqual({version.id: _digest(version.row()) for version in PUBLISHED_POLICY_VERSIONS},
                         PINNED_DIGESTS)
        self.assertTrue(all(version.is_published and version.published_at for version in PUBLISHED_POLICY_VERSIONS))

    def test_rows_are_copies_and_the_constant_is_read_only(self):
        row = BETA_DEFAULT_POLICY_V1.row()
        row["signal_thresholds"]["frequency_level_threshold"] = "1"
        row["mode_permissions"]["controlled_mock"] = True
        row["signal_thresholds"]["streak_reset_reason_codes"].append("x")
        self.assertEqual(BETA_DEFAULT_POLICY_V1.row()["signal_thresholds"]["frequency_level_threshold"], "3.5")
        self.assertNotIn("controlled_mock", BETA_DEFAULT_POLICY_V1.mode_permissions)
        with self.assertRaises(TypeError):
            BETA_DEFAULT_POLICY_V1.mode_permissions["controlled_mock"] = True  # type: ignore[index]
        with self.assertRaises(AttributeError):
            BETA_DEFAULT_POLICY_V1.bootstrap_draw_count = 1  # type: ignore[misc]


class PublishingMigration(unittest.TestCase):
    """A fresh SQLite database that only ran the migrations."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="marketai-scale-policy-migration-")
        self.addCleanup(self.temp.cleanup)
        database = Path(self.temp.name) / "migration.db"
        self.url = f"sqlite:///{database}"
        self.environment = dict(os.environ, MARKETAI_DATABASE_URL=self.url,
                                MARKETAI_DATA_DIR=str(Path(self.temp.name) / "data"))
        self._alembic("upgrade", "head")
        self.engine = create_engine(self.url)
        self.addCleanup(self.engine.dispose)

    def _alembic(self, *arguments: str) -> str:
        completed = subprocess.run([sys.executable, "-m", "alembic", *arguments], cwd=ROOT, env=self.environment,
                                   capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stderr

    def _rows(self, policy_id: str | None = None) -> list[dict]:
        statement = select(ScalePolicyVersionRecord.__table__).order_by(ScalePolicyVersionRecord.published_at)
        if policy_id is not None:
            statement = statement.where(ScalePolicyVersionRecord.id == policy_id)
        with self.engine.connect() as connection:
            return [dict(row._mapping) for row in connection.execute(statement)]

    def _run_upgrade_again(self, migration=MIGRATION) -> None:
        with self.engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()

    def test_the_published_row_is_the_constant_column_for_column(self):
        rows = self._rows()
        self.assertEqual([{column: _as_utc(value) for column, value in row.items()} for row in rows],
                         [version.row() for version in PUBLISHED_POLICY_VERSIONS])

    def test_eligible_live_policy_returns_v2_for_each_permitted_mode_and_none_otherwise(self):
        # After deploy, live runs select v2: v1 runs beta-v1, which is no
        # longer CURRENT_ALGORITHM_VERSION, though it stays published.
        with Session(self.engine) as session:
            selected = {mode: eligible_live_policy(session, mode) for mode in MODES}
        self.assertEqual({mode: policy.id for mode, policy in selected.items() if policy is not None},
                         {mode: "spol_beta_default_v2" for mode in PERMITTED})
        self.assertIsNone(selected["controlled_mock"])
        self.assertTrue(self._rows(BETA_DEFAULT_POLICY_V1.id)[0]["is_published"])

    def test_running_the_migration_again_adds_no_row(self):
        for migration in (MIGRATION, V2_MIGRATION):
            self._run_upgrade_again(migration)
            self._run_upgrade_again(migration)
        self.assertEqual([row["id"] for row in self._rows()], [BETA_DEFAULT_POLICY_V1.id, BETA_DEFAULT_POLICY_V2.id])

        self._alembic("downgrade", V2_PREVIOUS)
        self.assertEqual([row["id"] for row in self._rows()], [BETA_DEFAULT_POLICY_V1.id])
        self._alembic("upgrade", "head")
        self.assertEqual([row["id"] for row in self._rows()], [BETA_DEFAULT_POLICY_V1.id, BETA_DEFAULT_POLICY_V2.id])

    def test_downgrade_deletes_the_version_only_while_nothing_names_it(self):
        self._alembic("downgrade", PREVIOUS)
        self.assertEqual(self._rows(), [], "unreferenced: both deleted")
        self._alembic("upgrade", "head")

        from tests.support.scale_evaluation import seed_evaluable_run

        with Session(self.engine) as session, session.begin():
            session.add(WorkspaceRecord(id="ws_policy_migration", name="Acme Charge policy"))
            session.flush()
            seeded = seed_evaluable_run(session, "ws_policy_migration")
            session.execute(update(ScaleEvaluationRunRecord).where(ScaleEvaluationRunRecord.id == seeded.run_id)
                            .values(policy_version_id=BETA_DEFAULT_POLICY_V1.id))

        logged = self._alembic("downgrade", PREVIOUS)
        self.assertIn("scale_evaluation_runs.policy_version_id", logged)
        with self.engine.connect() as connection:
            kept = connection.execute(select(ScalePolicyVersionRecord.is_published)
                                      .where(ScalePolicyVersionRecord.id == BETA_DEFAULT_POLICY_V1.id)).scalar_one()
        self.assertTrue(kept, "referenced: kept, still published")
        self.assertEqual(self._rows(BETA_DEFAULT_POLICY_V2.id), [], "v2 unreferenced: deleted")

        self._alembic("upgrade", "head")
        with self.engine.connect() as connection:
            count = connection.execute(select(func.count()).select_from(ScalePolicyVersionRecord.__table__)
                                       .where(ScalePolicyVersionRecord.id == BETA_DEFAULT_POLICY_V1.id)).scalar_one()
        self.assertEqual(count, 1)

    def test_v2_downgrade_keeps_v2_while_a_run_names_it(self):
        from tests.support.scale_evaluation import seed_evaluable_run

        with Session(self.engine) as session, session.begin():
            session.add(WorkspaceRecord(id="ws_policy_v2_migration", name="Acme Charge policy v2"))
            session.flush()
            seeded = seed_evaluable_run(session, "ws_policy_v2_migration")
            session.execute(update(ScaleEvaluationRunRecord).where(ScaleEvaluationRunRecord.id == seeded.run_id)
                            .values(policy_version_id=BETA_DEFAULT_POLICY_V2.id))

        logged = self._alembic("downgrade", V2_PREVIOUS)
        self.assertIn("scale_evaluation_runs.policy_version_id", logged)
        [kept] = self._rows(BETA_DEFAULT_POLICY_V2.id)
        self.assertTrue(kept["is_published"], "referenced: kept, still published")

        self._alembic("upgrade", "head")
        self.assertEqual(len(self._rows(BETA_DEFAULT_POLICY_V2.id)), 1)


if __name__ == "__main__":
    unittest.main()
