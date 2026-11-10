"""Meta observation batches into Scale source manifests on SQLite (#109, #101 S3).

A ``customer_sandbox`` connection whose Scale contract set production code
built (``ensure_metric_contract_set``), sandbox ads, and an active Enrollment
on one of them. With the day's slot due, only the Worker runs: the tick, the
batch, and -- once the batch is terminal -- ``scale_collect_sources``, which
writes one immutable source manifest for the Enrollment's current Episode and
moves its readiness projection.

* complete: daily slots from the Episode's start only, three exact windows,
  the configuration with its status history and delivery diagnostic; a
  replay writes nothing and allocates no revision;
* the snapshot schema v3 seals that history and diagnostic (a paused day is
  explained), a snapshot sealed as v2 from the same source still verifies;
* blocked, one test per §1.7 code (plus the account-currency mismatch): no
  source record, no snapshot, run or Evaluation; readiness ``insufficient`` /
  ``blocked`` with the recovery action;
* freshness, a window without delivery, a rolled-over Episode, a paused
  Enrollment, Workspace and mode isolation, and the Meta tick closing the
  runs of a batch Job rejected for a stale authorization epoch.

``tests/test_scale_source_collection_pg.py`` runs the main path under the
real roles. A complete manifest also requests a live evaluation when a
Policy is eligible (#110, ``tests/test_scale_live_evaluation.py``); these
tests cover the collection alone, so they run with no eligible Policy,
whatever Policies other files left in the shared database. Fictional brand
only: Acme Charge.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import unittest
import uuid
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, update

import app.scale.evidence as evidence_module
from app.adapters.meta import sandbox_timeline
from app.adapters.meta.fake_adapter import FakeMetaAdapter
from app.adapters.persistence.database import engine, platform_session, tenant_session
from app.adapters.persistence.models import AuditEventRecord, JobRecord, WorkspaceRecord, new_id, utcnow
from app.meta import observation_sync
from app.meta.connections import create_connection
from app.meta.models import (
    MetaCallIntentRecord,
    MetaCallOutcomeRecord,
    MetaCallReconciliationRecord,
    MetaConnectionRecord,
    MetaEvidencePageRecord,
    MetaObservationSyncRunRecord,
    MetaObservationSyncScheduleRecord,
    MetaPageFetchAttemptRecord,
    ObservationContractGenerationRecord,
    ScenarioVersionRecord,
)
from app.meta.observation import ensure_observation_schedule, occurrence_id
from app.platform.operations import OperationRecord
from app.platform.runtime_role import API_ROLE, entered_as
from app.platform.tenancy import TenantContext
from app.scale import collector, live_trigger
from app.scale.delivery_targets import ensure_metric_contract_set
from app.scale.evaluator import _is_pause_explained
from app.scale.evidence import assemble_snapshot, seal_snapshot
from app.scale.models import (
    NO_TARGET_MARKER,
    ScaleDailyMetricSourceRecord,
    ScaleDeliveryConfigSourceRecord,
    ScaleEnrollmentRecord,
    ScaleEpisodeRecord,
    ScaleEvaluationRecord,
    ScaleEvaluationRunRecord,
    ScaleEvidenceReadinessProjectionRecord,
    ScaleEvidenceSnapshotRecord,
    ScaleEvidenceSourceManifestRecord,
    ScaleExactWindowSourceRecord,
    ScaleManifestItemRecord,
    ScaleMetricSemanticContractSetMemberRecord,
    ScaleMetricSemanticContractSetRecord,
    ScaleSnapshotQualityIssueRecord,
    ScaleSourceQualityIssueRecord,
)
from app.scale.types import DeliveryDiagnosticState
from app.scale.worker import ScaleWorkerRuntime
from app.worker_runtime import WorkerRuntime, meta_write_capability_findings
from tests.support.meta_observation import AD_ACCOUNT, seed_sandbox_ads, worker_context
from tests.support.queue_hygiene import cancel_workspace_jobs, quarantine_foreign_jobs
from tests.test_scale_evidence import _delivery_source, _policy

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = "customer_sandbox"
MOCK = "controlled_mock"
TIMEZONE = "America/Los_Angeles"
ZONE = ZoneInfo(TIMEZONE)
OWNER_ID = "usr_acme_owner"
LEARNING = {"SUCCESS": "none", "LEARNING": "learning", "FAIL": "learning_limited"}


def _local_today() -> date:
    return utcnow().astimezone(ZONE).date()


def _fire_at(day: date) -> datetime:
    """The slot that reads target day ``day``: 04:00 local the day after."""
    return datetime.combine(day + timedelta(days=1), time(4), tzinfo=ZONE).astimezone(timezone.utc)


class CollectionTestCase(unittest.TestCase):
    #: Whether a complete manifest may request its live evaluation (#110).
    LIVE_TRIGGER = False

    @classmethod
    def setUpClass(cls) -> None:
        db_file = engine.url.database
        if db_file and db_file != ":memory:":
            Path(db_file).parent.mkdir(parents=True, exist_ok=True)
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")

    def setUp(self) -> None:
        if not self.LIVE_TRIGGER:
            patcher = mock.patch.object(live_trigger, "eligible_live_policy", return_value=None)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.workspace_id = self._provision("ws_scale_collect")
        self.owner = TenantContext(workspace_id=self.workspace_id, role="owner", initiator_id=OWNER_ID)

    def _provision(self, prefix: str) -> str:
        workspace_id = f"{prefix}_{uuid.uuid4().hex[:12]}"
        with platform_session() as session:
            session.add(WorkspaceRecord(id=workspace_id, name=f"Acme Charge {workspace_id[-4:]}"))
        self.addCleanup(self._quiesce, workspace_id)
        return workspace_id

    def _quiesce(self, workspace_id: str) -> None:
        with platform_session() as session:
            session.execute(update(MetaObservationSyncScheduleRecord)
                            .where(MetaObservationSyncScheduleRecord.workspace_id == workspace_id,
                                   MetaObservationSyncScheduleRecord.state == "active")
                            .values(state="paused"))
        cancel_workspace_jobs(workspace_id)

    def _epoch(self, workspace_id: str) -> int:
        with platform_session() as session:
            return int(session.get(WorkspaceRecord, workspace_id).authorization_epoch)

    def _worker(self, meta_mode: str, workspace_id: str | None = None) -> TenantContext:
        workspace_id = workspace_id or self.workspace_id
        return worker_context(workspace_id, meta_mode, authorization_epoch=self._epoch(workspace_id))

    # -- fixtures -----------------------------------------------------------

    def _establish(self, meta_mode: str = SANDBOX, *, workspace_id: str | None = None, ads: int = 2,
                   scenario_config: dict | None = None) -> tuple[str, object, list[str]]:
        """A connection with its binding, its Scale contract set (production
        code) and ``ads`` sandbox ads. Returns ``(connection_id, set, ad_ids)``."""
        workspace_id = workspace_id or self.workspace_id
        owner = TenantContext(workspace_id=workspace_id, role="owner", initiator_id=OWNER_ID,
                              authorization_epoch=self._epoch(workspace_id), meta_mode=meta_mode)
        config = scenario_config or {"account": {"timezone_name": TIMEZONE}}
        with tenant_session(owner) as session:
            connection = create_connection(session, context=owner, ad_account_id=AD_ACCOUNT,
                                           scenario_config=config)
            connection_id = connection.id
        from app.meta.connections import create_binding

        with tenant_session(owner) as session:
            create_binding(session, context=owner, connection_id=connection_id, ad_account_id=AD_ACCOUNT)
        with entered_as(API_ROLE), tenant_session(owner) as session:
            established = ensure_metric_contract_set(session, owner, connection_id=connection_id)
        worker = self._worker(meta_mode, workspace_id)
        if meta_mode == MOCK:  # the explicit test entry (#101 §6)
            with tenant_session(worker) as session:
                for generation_id in established.generation_ids.values():
                    ensure_observation_schedule(session, session.get(ObservationContractGenerationRecord,
                                                                     generation_id))
        ad_ids = seed_sandbox_ads(worker, connection_id, ads)
        return connection_id, established, ad_ids

    def _ad_configuration(self, meta_mode: str, connection_id: str, ad_id: str, day: date,
                          workspace_id: str | None = None) -> dict:
        """The ad's row on the ``ads`` edge, as the sandbox answers it."""
        context = self._worker(meta_mode, workspace_id)
        with tenant_session(context) as session:
            scenario = session.scalar(select(ScenarioVersionRecord).where(
                ScenarioVersionRecord.workspace_id == context.workspace_id,
                ScenarioVersionRecord.meta_connection_id == connection_id,
                ScenarioVersionRecord.is_default == True))  # noqa: E712
            page = FakeMetaAdapter(session, context.workspace_id, meta_mode, scenario.id).read_delivery_configuration(
                ad_account_id=AD_ACCOUNT, edge="ads", fields=["id", "adset_id", "campaign_id", "creative"],
                as_of=day, limit=100)
            return next(row for row in page.payload["data"] if row["id"] == ad_id)

    def _enroll(self, connection_id: str, established, ad_id: str, started: date, *,
                meta_mode: str = SANDBOX, workspace_id: str | None = None, currency: str = "USD",
                lifecycle: str = "active", ad_set_remote_id: str | None = "__sandbox__",
                ad_remote_id: str | None = "__same__") -> tuple[str, str]:
        """An Enrollment and its current Episode on ``ad_id``, frozen to the
        connection's contract set, with activation's baseline configuration
        source at revision 1. Enrollment activation itself is #63's path."""
        workspace_id = workspace_id or self.workspace_id
        row = self._ad_configuration(meta_mode, connection_id, ad_id, started, workspace_id)
        scenario = self._scenario_key(meta_mode, connection_id, workspace_id)
        common = dict(
            workspace_id=workspace_id, meta_mode=meta_mode, meta_connection_id=connection_id,
            scenario_scope_key=scenario, ad_account_id=AD_ACCOUNT, winner_decision_id="wd_acme",
            winner_confirmation_id="wc_acme", asset_version_id="av_acme", product_version_id="pv_acme",
            offer_version_id="ov_acme", market_code="US", campaign_remote_id=row["campaign_id"],
            ad_set_remote_id=row["adset_id"] if ad_set_remote_id == "__sandbox__" else ad_set_remote_id,
            ad_remote_id=ad_id if ad_remote_id == "__same__" else ad_remote_id,
        )
        enrollment = ScaleEnrollmentRecord(
            id=new_id("senr"), remote_binding_key=f"{connection_id}/{AD_ACCOUNT}/{ad_id}/{uuid.uuid4().hex[:6]}",
            lifecycle=lifecycle, creator_id=OWNER_ID, original_initiator_id=OWNER_ID, **common,
        )
        episode = ScaleEpisodeRecord(
            id=new_id("sep"), enrollment_id=enrollment.id, delivery_config_baseline_source_id="sdcs_pending",
            delivery_evidence_hash="e" * 64, delivery_comparability_hash="c" * 64,
            metric_semantic_contract_set_id=established.contract_set_id, timezone_name=TIMEZONE,
            currency=currency, attribution_semantics_hash="a" * 64, phase="collecting", started_local_day=started,
            **{key: value for key, value in common.items() if key != "lifecycle"},
        )
        with platform_session() as session:
            session.add(enrollment)
            session.flush()
            session.add(episode)
            session.flush()
            baseline = _delivery_source(workspace_id, meta_mode, episode.id, revision=1)
            baseline.meta_connection_id, baseline.scenario_scope_key = connection_id, scenario
            baseline.ad_account_id = AD_ACCOUNT
            session.add(baseline)
            session.flush()
            episode.delivery_config_baseline_source_id = baseline.id
            enrollment.current_episode_id = episode.id
        return enrollment.id, episode.id

    def _scenario_key(self, meta_mode: str, connection_id: str, workspace_id: str) -> str:
        if meta_mode != MOCK:
            return "__no_scenario__"
        with platform_session() as session:
            return session.scalar(select(ScenarioVersionRecord.id).where(
                ScenarioVersionRecord.workspace_id == workspace_id,
                ScenarioVersionRecord.meta_connection_id == connection_id,
                ScenarioVersionRecord.is_default == True))  # noqa: E712

    # -- driving the Worker -------------------------------------------------

    def _drain(self, workspace_id: str | None = None, *, release_retries: bool = False,
               runtime=None) -> int:
        workspace_id = workspace_id or self.workspace_id
        runtime = runtime or WorkerRuntime(owner=f"ci-only-scale-collect-{uuid.uuid4().hex[:6]}")
        handled = 0
        for _ in range(40):
            quarantine_foreign_jobs(workspace_id)
            if release_retries:
                with platform_session() as session:
                    session.execute(update(JobRecord)
                                    .where(JobRecord.workspace_id == workspace_id, JobRecord.state == "retry_wait")
                                    .values(next_attempt_at=utcnow() - timedelta(seconds=1)))
            if not asyncio.run(runtime.run_once()):
                self.assertEqual(meta_write_capability_findings(runtime), [])
                return handled
            handled += 1
        self.fail("the Worker did not go idle")

    def _make_due(self, day: date, workspace_id: str | None = None) -> datetime:
        workspace_id = workspace_id or self.workspace_id
        fire_at = _fire_at(day)
        with platform_session() as session:
            session.execute(update(MetaObservationSyncScheduleRecord)
                            .where(MetaObservationSyncScheduleRecord.workspace_id == workspace_id,
                                   MetaObservationSyncScheduleRecord.state == "active")
                            .values(next_fire_at=fire_at))
        return fire_at

    def _run_day(self, day: date, workspace_id: str | None = None, *, release_retries: bool = False) -> int:
        """Only the Worker, with the slot reading ``day`` due a minute ago."""
        fire_at = self._make_due(day, workspace_id)
        with mock.patch.object(observation_sync, "utcnow", return_value=fire_at + timedelta(minutes=1)):
            return self._drain(workspace_id, release_retries=release_retries)

    def _collect(self, connection_id: str, day: date, meta_mode: str = SANDBOX,
                 workspace_id: str | None = None) -> JobRecord:
        """Enqueue the collection the batch would have, and let the Worker run it.

        The schedules are pushed out first: the slot after D's is already in
        the past, and a fresh Worker would otherwise read and collect D+1 too.
        """
        self._make_due(_local_today() + timedelta(days=30), workspace_id)
        context = self._worker(meta_mode, workspace_id)
        with tenant_session(context) as session:
            job = collector.enqueue_source_collection(session, context, connection_id=connection_id,
                                                      target_date=day)
            self.assertIsNotNone(job)
            job_id = job.id
        self._drain(workspace_id)
        return self._rows(JobRecord, workspace_id, id=job_id)[0]

    # -- reading back -------------------------------------------------------

    def _rows(self, model, workspace_id: str | None = None, **filters) -> list:
        with platform_session() as session:
            rows = list(session.scalars(select(model).where(model.workspace_id == (workspace_id or self.workspace_id))
                                        .filter_by(**filters)))
            for row in rows:
                session.expunge(row)
            return rows

    def _manifests(self, episode_id: str) -> list[ScaleEvidenceSourceManifestRecord]:
        return sorted(self._rows(ScaleEvidenceSourceManifestRecord, episode_id=episode_id),
                      key=lambda manifest: manifest.source_revision)

    def _manifest_for(self, episode_id: str, day: date) -> ScaleEvidenceSourceManifestRecord:
        [manifest] = [m for m in self._manifests(episode_id) if m.target_local_day == day]
        return manifest

    def _items(self, manifest_id: str) -> list[ScaleManifestItemRecord]:
        return self._rows(ScaleManifestItemRecord, manifest_id=manifest_id)

    def _issues(self, manifest_id: str) -> list[tuple]:
        return sorted((issue.code, issue.is_blocking, issue.contract_type, issue.window_role, issue.recovery_action)
                      for issue in self._rows(ScaleSourceQualityIssueRecord, manifest_id=manifest_id))

    def _readiness(self, episode_id: str) -> ScaleEvidenceReadinessProjectionRecord:
        [projection] = self._rows(ScaleEvidenceReadinessProjectionRecord, episode_id=episode_id)
        return projection

    def _runs(self, generation_id: str, day: date) -> dict[str, MetaObservationSyncRunRecord]:
        prefix = f"obs:{generation_id}:{day.isoformat()}:"
        return {run.occurrence_id.removeprefix(prefix): run for run in self._rows(MetaObservationSyncRunRecord)
                if run.occurrence_id.startswith(prefix)}

    def _assert_no_evaluation(self, workspace_id: str | None = None) -> None:
        for model in (ScaleEvidenceSnapshotRecord, ScaleEvaluationRunRecord, ScaleEvaluationRecord):
            self.assertEqual(self._rows(model, workspace_id), [], f"S3 creates no {model.__tablename__}")
        self.assertEqual(self._rows(JobRecord, workspace_id, type="scale_evaluate"), [])

    def _assert_blocked(self, episode_id: str, day: date, code: str, recovery_action: str,
                        contract_type: str | None = None) -> ScaleEvidenceSourceManifestRecord:
        manifest = self._manifest_for(episode_id, day)
        self.assertEqual(manifest.state, "blocked")
        self.assertIsNone(manifest.last_complete_local_day)
        codes = {(issue.code, issue.is_blocking, issue.recovery_action)
                 for issue in self._rows(ScaleSourceQualityIssueRecord, manifest_id=manifest.id)}
        self.assertIn((code, True, recovery_action), codes)
        if contract_type is not None:
            self.assertIn(contract_type, {issue.contract_type for issue in self._rows(
                ScaleSourceQualityIssueRecord, manifest_id=manifest.id) if issue.code == code})
        items = self._items(manifest.id)
        self.assertTrue(items, "a blocked manifest still enumerates what it requested")
        self.assertEqual({item.state for item in items}, {"failed"})
        self.assertEqual({item.source_record_id for item in items}, {None})
        for model in (ScaleDailyMetricSourceRecord, ScaleExactWindowSourceRecord):
            self.assertEqual(self._rows(model, episode_id=episode_id, source_revision=manifest.source_revision), [],
                             "a blocked manifest invents no source row")
        self.assertEqual(self._rows(ScaleDeliveryConfigSourceRecord, episode_id=episode_id,
                                    source_revision=manifest.source_revision), [])
        readiness = self._readiness(episode_id)
        self.assertEqual((readiness.latest_manifest_id, readiness.maturity, readiness.quality_state,
                          readiness.recovery_action),
                         (manifest.id, "insufficient", "blocked", recovery_action))
        self.assertGreaterEqual(readiness.blocking_issue_count, 1)
        self._assert_no_evaluation()
        return manifest


class CompleteCollection(CollectionTestCase):
    def test_the_worker_collects_a_complete_manifest_for_the_current_episode(self):
        connection_id, established, ad_ids = self._establish()
        ad_id = ad_ids[0]
        day = _local_today() - timedelta(days=3)
        started = day - timedelta(days=3)
        enrollment_id, episode_id = self._enroll(connection_id, established, ad_id, started)

        self.assertEqual(self._run_day(day), 3, "one tick, one batch, one collection")

        [job] = self._rows(JobRecord, type="scale_collect_sources")
        self.assertEqual((job.state, job.idempotency_key, job.payload_json["meta_mode"]),
                         ("succeeded", f"scale-collect:{connection_id}:{day.isoformat()}:1", SANDBOX))
        [manifest] = self._manifests(episode_id)
        self.assertEqual((manifest.state, manifest.target_local_day, manifest.last_complete_local_day,
                          manifest.safe_watermark, manifest.collection_key),
                         ("complete", day, day, day, job.idempotency_key))
        self.assertEqual(manifest.source_revision, 2, "activation's baseline source holds revision 1")
        self.assertRegex(manifest.canonical_hash, r"^[0-9a-f]{64}$")
        self.assertEqual(self._issues(manifest.id), [])

        runs = {**self._runs(established.generation_ids["daily"], day),
                **self._runs(established.generation_ids["exact_window"], day),
                **self._runs(established.generation_ids["delivery_configuration"], day)}
        self.assertEqual(manifest.terminal_at.replace(tzinfo=None),
                         max(run.completed_at for run in runs.values()).replace(tzinfo=None),
                         "terminal_at is when the batch's last run finished")

        # Date boundary: the read covers D-13..D, the Episode only its own days.
        items = self._items(manifest.id)
        daily_items = sorted((item.local_day, item.state) for item in items if item.contract_type == "daily")
        self.assertEqual(daily_items, [(started + timedelta(days=offset), "fulfilled") for offset in range(4)])
        daily = {row.local_day: row for row in self._rows(ScaleDailyMetricSourceRecord, episode_id=episode_id)}
        self.assertEqual(sorted(daily), [started + timedelta(days=offset) for offset in range(4)])
        for local_day, row in daily.items():
            values = sandbox_timeline.day_values(ad_id, local_day)
            self.assertEqual(row.state, "present")
            if values is None:  # observed zero delivery, not a missing slot (#101 Q7)
                self.assertEqual((row.delivered, row.impressions, row.spend), (False, 0, Decimal(0)))
            else:
                self.assertEqual((row.delivered, row.impressions, row.link_clicks),
                                 (True, values.impressions, values.link_clicks))
            self.assertEqual((row.sync_run_id, row.currency, row.timezone_name, row.source_revision),
                             (runs["daily"].id, "USD", TIMEZONE, 2))

        windows = {row.window_role: row for row in self._rows(ScaleExactWindowSourceRecord, episode_id=episode_id)}
        self.assertEqual(sorted(windows), ["baseline_7d", "current_7d", "rolling_14d"])
        self.assertEqual((windows["current_7d"].window_start_local_day, windows["current_7d"].window_end_local_day),
                         (day - timedelta(days=6), day + timedelta(days=1)), "half-open, as the snapshot's")
        self.assertEqual(windows["baseline_7d"].sync_run_id, runs["baseline_7d"].id)
        self.assertEqual({item.window_role: item.state for item in items if item.contract_type == "exact_window"},
                         {"current_7d": "fulfilled", "baseline_7d": "fulfilled", "rolling_14d": "fulfilled"})

        [configuration] = self._rows(ScaleDeliveryConfigSourceRecord, episode_id=episode_id, source_revision=2)
        row = self._ad_configuration(SANDBOX, connection_id, ad_id, day)
        settings = sandbox_timeline.configuration_for_ad(ad_id, day)
        self.assertEqual((configuration.ad_remote_id, configuration.ad_set_remote_id,
                          configuration.campaign_remote_id, configuration.ad_creative_id),
                         (ad_id, row["adset_id"], row["campaign_id"], row["creative"]["id"]))
        self.assertEqual(configuration.ad_status, settings["status"])
        self.assertEqual(configuration.budget_amount, Decimal(settings["daily_budget"]) / 100)
        self.assertEqual(configuration.diagnostic_state, LEARNING[settings["learning_stage"]])
        self.assertEqual(configuration.diagnostic_source_field, "adsets.learning_stage_info.status")
        self.assertIsNotNone(configuration.diagnostic_observed_at)
        self.assertEqual(configuration.status_history, [
            {"local_day": day.isoformat(), "entity_level": level, "status": status}
            for level, status in (("ad", settings["status"]), ("ad_set", "ACTIVE"), ("campaign", "ACTIVE"))
        ])
        self.assertEqual(configuration.sync_run_id, runs["configuration"].id)

        readiness = self._readiness(episode_id)
        self.assertEqual((readiness.latest_manifest_id, readiness.maturity, readiness.quality_state,
                          readiness.last_complete_local_day, readiness.recovery_action, readiness.revision),
                         (manifest.id, "warming", "pass", day, None, 1))
        self.assertEqual(readiness.freshness_reference_at.replace(tzinfo=None),
                         manifest.terminal_at.replace(tzinfo=None))
        [event] = self._rows(AuditEventRecord, event_type="scale.source_manifest.recorded")
        self.assertEqual((event.resource_id, event.details_json["state"], event.details_json["episode_id"]),
                         (manifest.id, "complete", episode_id))
        self._assert_no_evaluation()

    def test_a_replayed_collection_finds_the_same_manifest_and_allocates_no_revision(self):
        connection_id, established, ad_ids = self._establish(ads=1)
        day = _local_today() - timedelta(days=3)
        _enrollment_id, episode_id = self._enroll(connection_id, established, ad_ids[0], day - timedelta(days=1))
        self._run_day(day)
        [first] = self._manifests(episode_id)
        counts = [len(self._rows(model)) for model in (
            ScaleDailyMetricSourceRecord, ScaleExactWindowSourceRecord, ScaleDeliveryConfigSourceRecord,
            ScaleManifestItemRecord, ScaleSourceQualityIssueRecord)]
        readiness_revision = self._readiness(episode_id).revision

        result = collector.run_source_collection(self._worker(SANDBOX), connection_id=connection_id,
                                                 target_date=day)

        self.assertEqual([(m.manifest_id, m.source_revision, m.replayed) for m in result.manifests],
                         [(first.id, first.source_revision, True)])
        job = self._collect(connection_id, day)  # the same key: the same, finished Job
        self.assertEqual(job.state, "succeeded")
        self.assertEqual([m.id for m in self._manifests(episode_id)], [first.id])
        self.assertEqual([len(self._rows(model)) for model in (
            ScaleDailyMetricSourceRecord, ScaleExactWindowSourceRecord, ScaleDeliveryConfigSourceRecord,
            ScaleManifestItemRecord, ScaleSourceQualityIssueRecord)], counts)
        self.assertEqual(self._readiness(episode_id).revision, readiness_revision)

        # The next day is a new batch: the next revision.
        self._run_day(day + timedelta(days=1))
        self.assertEqual([m.source_revision for m in self._manifests(episode_id)], [2, 3])

    def test_only_the_current_episode_is_collected_after_a_rollover(self):
        connection_id, established, ad_ids = self._establish(ads=1)
        day = _local_today() - timedelta(days=3)
        enrollment_id, old_episode = self._enroll(connection_id, established, ad_ids[0], day - timedelta(days=5))
        new_episode = new_id("sep")
        with platform_session() as session:  # what _apply_episode_rollover leaves behind
            old = session.get(ScaleEpisodeRecord, old_episode)
            old.phase, old.ended_at, old.closure_reason = "closed", utcnow(), "delivery_configuration_changed"
            fields = {column.name: getattr(old, column.name) for column in ScaleEpisodeRecord.__table__.columns}
            fields.update(id=new_episode, phase="collecting", ended_at=None, closure_reason=None,
                          started_local_day=day - timedelta(days=1))
            session.add(ScaleEpisodeRecord(**fields))
            session.flush()
            session.get(ScaleEnrollmentRecord, enrollment_id).current_episode_id = new_episode

        self._run_day(day)

        self.assertEqual(self._manifests(old_episode), [], "the closed Episode collects nothing")
        [manifest] = self._manifests(new_episode)
        self.assertEqual(manifest.source_revision, 1, "the new Episode's own revisions")
        self.assertEqual(sorted(item.local_day for item in self._items(manifest.id) if item.contract_type == "daily"),
                         [day - timedelta(days=1), day])
        before_start = collector.run_source_collection(
            self._worker(SANDBOX), connection_id=connection_id, target_date=day - timedelta(days=2))
        self.assertEqual((before_start.manifests, before_start.skipped), ((), (enrollment_id,)),
                         "a day before the Episode is not collected")

    def test_a_paused_enrollment_gets_the_job_but_no_manifest(self):
        connection_id, established, ad_ids = self._establish(ads=1)
        day = _local_today() - timedelta(days=3)
        _enrollment_id, episode_id = self._enroll(connection_id, established, ad_ids[0], day, lifecycle="paused")

        self._run_day(day)

        [job] = self._rows(JobRecord, type="scale_collect_sources")
        self.assertEqual(job.state, "succeeded", "enqueued: #111's fallback sweep runs for paused Enrollments")
        self.assertEqual(self._manifests(episode_id), [])

    def test_a_connection_without_an_enrollment_gets_no_collection(self):
        self._establish(ads=1)
        self._run_day(_local_today() - timedelta(days=3))
        self.assertEqual(self._rows(JobRecord, type="scale_collect_sources"), [])
        self.assertEqual(self._rows(ScaleEvidenceSourceManifestRecord), [])


class SealedDeliveryHistory(CollectionTestCase):
    """Snapshot schema v3 seals what the collector recorded."""

    def _paused_day(self, ad_id: str) -> date:
        for back in range(3, 20):
            day = _local_today() - timedelta(days=back)
            if sandbox_timeline.day_state(ad_id, day) == sandbox_timeline.PAUSED:
                return day
        self.fail("every 14 days hold a paused day")

    def _seal(self, enrollment_id: str, manifest: ScaleEvidenceSourceManifestRecord, policy_id: str,
              sequence: int, *, freshness_reference_at: datetime | None = None) -> str:
        with platform_session() as session:
            run = ScaleEvaluationRunRecord(
                id=new_id("sevr"), workspace_id=self.workspace_id, meta_mode=SANDBOX,
                meta_connection_id=manifest.meta_connection_id, scenario_scope_key=manifest.scenario_scope_key,
                episode_id=manifest.episode_id, enrollment_id=enrollment_id,
                target_local_day=manifest.target_local_day, trigger_kind="live", source_manifest_id=manifest.id,
                source_manifest_hash=manifest.canonical_hash, source_revision=manifest.source_revision,
                freshness_reference_at=freshness_reference_at or manifest.terminal_at, policy_version_id=policy_id,
                target_version_id=None, normalized_target_key=NO_TARGET_MARKER, algorithm_version="beta-v1",
                semantic_event_sequence=sequence, replay_generation=0, original_initiator_id=OWNER_ID,
                run_state="requested",
            )
            session.add(run)
            run_id = run.id
        context = self._worker(SANDBOX)
        with tenant_session(context) as session:
            snapshot_id = seal_snapshot(session, context, run_id)
        with platform_session() as session:  # Phase C would hand the phase back
            episode = session.get(ScaleEpisodeRecord, manifest.episode_id)
            episode.phase, episode.phase_run_id = "collecting", None
        return snapshot_id

    def test_a_paused_day_is_explained_by_the_sealed_status_history(self):
        connection_id, established, ad_ids = self._establish(ads=1)
        ad_id = ad_ids[0]
        paused = self._paused_day(ad_id)
        enrollment_id, episode_id = self._enroll(connection_id, established, ad_id, paused - timedelta(days=1))
        policy = _policy()
        with platform_session() as session:
            session.add(policy)
        self._run_day(paused - timedelta(days=1))
        self._run_day(paused)

        manifest = self._manifest_for(episode_id, paused)
        [daily] = [row for row in self._rows(ScaleDailyMetricSourceRecord, episode_id=episode_id,
                                             source_revision=manifest.source_revision) if row.local_day == paused]
        self.assertEqual((daily.state, daily.delivered), ("present", False), "a paused day is observed zero delivery")
        [configuration] = self._rows(ScaleDeliveryConfigSourceRecord, episode_id=episode_id,
                                     source_revision=manifest.source_revision)
        self.assertEqual([(entry["local_day"], entry["status"]) for entry in configuration.status_history
                          if entry["entity_level"] == "ad"],
                         [((paused - timedelta(days=1)).isoformat(), "ACTIVE"), (paused.isoformat(), "PAUSED")],
                         "each Episode day's configuration read, in order")

        context = self._worker(SANDBOX)
        snapshot_id = self._seal(enrollment_id, manifest, policy.id, 0)
        with tenant_session(context) as session:
            snapshot = assemble_snapshot(session, context, snapshot_id)
            row = session.get(ScaleEvidenceSnapshotRecord, snapshot_id)
            self.assertEqual(row.canonical_schema_version, "scale-snapshot-v3")
        history = snapshot.delivery_config.status_history
        self.assertIn(("ad", "PAUSED", paused), {(e.entity_level, e.status, e.observed_at.date()) for e in history})
        self.assertTrue(all(e.observed_at.utcoffset() == ZONE.utcoffset(e.observed_at.replace(tzinfo=None))
                            for e in history), "events stand in the account's time zone")
        self.assertTrue(_is_pause_explained(snapshot), "the evaluator explains the paused day by the history")
        settings = sandbox_timeline.configuration_for_ad(ad_id, paused)
        self.assertEqual(snapshot.delivery_config.diagnostic_state,
                         DeliveryDiagnosticState(LEARNING[settings["learning_stage"]]))
        self.assertEqual({item.key: item.value for item in snapshot.delivery_config.diagnostic_detail}["source_field"],
                         "adsets.learning_stage_info.status")

        # A snapshot sealed under the old schema from the very same source
        # still reassembles to its hash: v2 never read history or diagnostic.
        with mock.patch.object(evidence_module, "CANONICAL_SCHEMA_VERSION", "scale-snapshot-v2"):
            old_id = self._seal(enrollment_id, manifest, policy.id, 1)
        with tenant_session(context) as session:
            old = assemble_snapshot(session, context, old_id)
            stored = session.get(ScaleEvidenceSnapshotRecord, old_id)
        self.assertEqual((stored.canonical_schema_version, old.canonical_hash),
                         ("scale-snapshot-v2", stored.canonical_hash))
        self.assertEqual((old.delivery_config.status_history, old.delivery_config.diagnostic_state),
                         ((), DeliveryDiagnosticState.NONE_))
        self.assertFalse(_is_pause_explained(old))

    def test_a_stale_freshness_reference_is_sealed_as_freshness_slo_exceeded(self):
        connection_id, established, ad_ids = self._establish(ads=1)
        day = _local_today() - timedelta(days=3)
        enrollment_id, episode_id = self._enroll(connection_id, established, ad_ids[0], day)
        strict, lenient = _policy(), _policy()
        strict.signal_thresholds = {**strict.signal_thresholds, "quality_warning_permitted_codes": []}
        with platform_session() as session:
            session.add_all([strict, lenient])
        self._run_day(day)
        manifest = self._manifest_for(episode_id, day)
        late = manifest.terminal_at.replace(tzinfo=timezone.utc) + timedelta(hours=25)  # Policy lag: 24 h

        fresh = self._seal(enrollment_id, manifest, strict.id, 0)
        blocking = self._seal(enrollment_id, manifest, strict.id, 1, freshness_reference_at=late)
        warning = self._seal(enrollment_id, manifest, lenient.id, 2, freshness_reference_at=late)

        def codes(snapshot_id):
            return {(row.code, row.severity) for row in self._rows(ScaleSnapshotQualityIssueRecord,
                                                                   snapshot_id=snapshot_id)}
        self.assertEqual(codes(fresh), set())
        self.assertEqual(codes(blocking), {("freshness_slo_exceeded", "blocking")}, "contract §8 by default")
        self.assertEqual(codes(warning), {("freshness_slo_exceeded", "warning")}, "the Policy permits it")


class BlockedManifests(CollectionTestCase):
    """One test per spec §1.7 terminal-source code (#101 §3 table)."""

    def _collected_day(self, *, ads: int = 1, meta_mode: str = SANDBOX, scenario_config: dict | None = None,
                       release_retries: bool = False):
        connection_id, established, ad_ids = self._establish(meta_mode, ads=ads, scenario_config=scenario_config)
        day = _local_today() - timedelta(days=3)
        self._run_day(day, release_retries=release_retries)  # no Enrollment yet: no collection
        self.assertEqual(self._rows(JobRecord, type="scale_collect_sources"), [])
        return connection_id, established, ad_ids, day

    def test_transport_exhausted_when_a_run_failed(self):
        faults = {"rules": [{"operation": "read_insights", "effect": "read_error", "match_page": 1,
                             "match_time_increment": "all_days", "match_window_days": 7}]}
        connection_id, established, ad_ids = self._establish(MOCK, ads=1, scenario_config={
            "faults": faults, "account": {"timezone_name": TIMEZONE}})
        day = _local_today() - timedelta(days=3)
        _enrollment_id, episode_id = self._enroll(connection_id, established, ad_ids[0], day, meta_mode=MOCK)

        self._run_day(day, release_retries=True)

        batch = self._rows(JobRecord, type=observation_sync.SYNC_JOB_TYPE)[0]
        self.assertEqual((batch.state, batch.attempt), ("failed", 3))
        [job] = self._rows(JobRecord, type="scale_collect_sources")
        self.assertEqual(job.state, "succeeded", "the batch's last attempt still enqueued the collection")
        manifest = self._assert_blocked(episode_id, day, "transport_exhausted", "wait_for_next_sync", "exact_window")
        self.assertEqual({issue.window_role for issue in self._rows(ScaleSourceQualityIssueRecord,
                                                                     manifest_id=manifest.id)},
                         {"current_7d", "baseline_7d"}, "both 7-day windows failed; rolling_14d did not")

    def test_incomplete_pagination_when_a_page_is_missing(self):
        connection_id, established, ad_ids, day = self._collected_day()
        _enrollment_id, episode_id = self._enroll(connection_id, established, ad_ids[0], day)
        daily_run = self._runs(established.generation_ids["daily"], day)["daily"]
        with platform_session() as session:  # a terminal page that never got written
            session.get(MetaObservationSyncRunRecord, daily_run.id).total_pages += 1
            session.get(MetaObservationSyncRunRecord, daily_run.id).last_completed_page_order += 1

        self._collect(connection_id, day)

        self._assert_blocked(episode_id, day, "incomplete_pagination", "retry_meta_sync", "daily")

    def _orphan_intent(self, run: MetaObservationSyncRunRecord) -> str:
        """A page call the network saw and nobody recorded the answer to."""
        with platform_session() as session:
            page = session.scalar(select(MetaEvidencePageRecord).where(
                MetaEvidencePageRecord.sync_run_id == run.id, MetaEvidencePageRecord.page_order == 1))
            original = session.get(MetaCallIntentRecord, page.intent_id)
            fields = {c.name: getattr(original, c.name) for c in MetaCallIntentRecord.__table__.columns}
            fields.update(id=new_id("mci"), network_dispatched_at=utcnow())
            session.add(MetaCallIntentRecord(**fields))
            session.flush()
            session.add(MetaPageFetchAttemptRecord(
                id=new_id("mpfa"), workspace_id=run.workspace_id, meta_mode=run.meta_mode, sync_run_id=run.id,
                intent_id=fields["id"], page_order=1, attempt_number=99, state="started"))
            return fields["id"]

    def test_reconciliation_unresolved_when_a_dispatched_call_has_no_outcome(self):
        connection_id, established, ad_ids, day = self._collected_day()
        _enrollment_id, episode_id = self._enroll(connection_id, established, ad_ids[0], day)
        self._orphan_intent(self._runs(established.generation_ids["exact_window"], day)["rolling_14d"])

        self._collect(connection_id, day)

        self._assert_blocked(episode_id, day, "reconciliation_unresolved", "reconcile_meta_sync", "exact_window")

    def test_a_confirmed_reconciliation_resolves_the_uncertain_call(self):
        connection_id, established, ad_ids, day = self._collected_day()
        _enrollment_id, episode_id = self._enroll(connection_id, established, ad_ids[0], day)
        run = self._runs(established.generation_ids["exact_window"], day)["rolling_14d"]
        orphan = self._orphan_intent(run)
        with platform_session() as session:
            page = session.scalar(select(MetaEvidencePageRecord).where(MetaEvidencePageRecord.sync_run_id == run.id))
            reconciliation = MetaCallReconciliationRecord(
                id=new_id("mrec"), workspace_id=run.workspace_id, meta_mode=run.meta_mode,
                uncertain_intent_id=orphan, inspection_intent_id=page.intent_id,
                inspection_outcome_id=page.outcome_id, resolution="confirmed_failed")
            session.add(reconciliation)
            reconciliation_id = reconciliation.id

        self._collect(connection_id, day)

        manifest = self._manifest_for(episode_id, day)
        self.assertEqual(manifest.state, "complete")
        [window] = self._rows(ScaleExactWindowSourceRecord, episode_id=episode_id, window_role="rolling_14d")
        self.assertEqual(window.reconciliation_id, reconciliation_id)

    def test_an_undispatched_call_without_outcome_is_not_uncertain(self):
        connection_id, established, ad_ids, day = self._collected_day()
        _enrollment_id, episode_id = self._enroll(connection_id, established, ad_ids[0], day)
        orphan = self._orphan_intent(self._runs(established.generation_ids["daily"], day)["daily"])
        with platform_session() as session:  # a sandbox read that died before answering never left the process
            session.get(MetaCallIntentRecord, orphan).network_dispatched_at = None

        self._collect(connection_id, day)

        self.assertEqual(self._manifest_for(episode_id, day).state, "complete")

    def test_contract_instance_failed_when_the_generation_hash_differs(self):
        connection_id, established, ad_ids, day = self._collected_day()
        drifted = new_id("smcs")
        with platform_session() as session:  # a set whose exact-window member names another hash
            session.add(ScaleMetricSemanticContractSetRecord(id=drifted, workspace_id=self.workspace_id,
                                                             meta_mode=SANDBOX, created_by=OWNER_ID))
            session.flush()
            for member in session.scalars(select(ScaleMetricSemanticContractSetMemberRecord).where(
                    ScaleMetricSemanticContractSetMemberRecord.contract_set_id == established.contract_set_id)):
                session.add(ScaleMetricSemanticContractSetMemberRecord(
                    id=new_id("smcm"), contract_set_id=drifted, contract_type=member.contract_type,
                    observation_contract_generation=member.observation_contract_generation,
                    contract_hash=("f" * 64) if member.contract_type == "exact_window" else member.contract_hash))
        _enrollment_id, episode_id = self._enroll(connection_id, replace(established, contract_set_id=drifted),
                                                  ad_ids[0], day)

        self._collect(connection_id, day)

        self._assert_blocked(episode_id, day, "contract_instance_failed", "wait_for_next_sync", "exact_window")

    def test_source_provenance_mismatch_when_the_ad_is_not_in_the_account(self):
        connection_id, established, ad_ids, day = self._collected_day()
        _enrollment_id, episode_id = self._enroll(connection_id, established, ad_ids[0], day,
                                                  ad_remote_id="ad_not_in_this_account")

        self._collect(connection_id, day)

        manifest = self._assert_blocked(episode_id, day, "source_provenance_mismatch", "repair_connection")
        self.assertIn("ad_not_found", " ".join(issue.detail or "" for issue in self._rows(
            ScaleSourceQualityIssueRecord, manifest_id=manifest.id)))

    def test_source_provenance_mismatch_when_the_ad_resolves_to_another_ad_set(self):
        connection_id, established, ad_ids, day = self._collected_day()
        _enrollment_id, episode_id = self._enroll(connection_id, established, ad_ids[0], day,
                                                  ad_set_remote_id="set_fake_not_this_one")

        self._collect(connection_id, day)

        self._assert_blocked(episode_id, day, "source_provenance_mismatch", "repair_connection",
                             "delivery_configuration")

    def test_an_account_currency_unlike_the_episodes_is_blocked_not_converted(self):
        connection_id, established, ad_ids, day = self._collected_day()
        _enrollment_id, episode_id = self._enroll(connection_id, established, ad_ids[0], day, currency="EUR")

        self._collect(connection_id, day)

        manifest = self._assert_blocked(episode_id, day, "source_provenance_mismatch", "repair_connection")
        details = {issue.contract_type: issue.detail for issue in self._rows(
            ScaleSourceQualityIssueRecord, manifest_id=manifest.id)}
        self.assertEqual(set(details), {"daily", "exact_window", "delivery_configuration"},
                         "every contract is frozen to the account's currency")
        self.assertTrue(all("account currency 'USD', Episode 'EUR'" in detail for detail in details.values()))

    def test_connection_ineligible_when_the_connection_was_retired(self):
        connection_id, established, ad_ids, day = self._collected_day()
        _enrollment_id, episode_id = self._enroll(connection_id, established, ad_ids[0], day)
        with platform_session() as session:
            connection = session.get(MetaConnectionRecord, connection_id)
            connection.state, connection.retired_at = "retired", utcnow()

        self._collect(connection_id, day)

        self._assert_blocked(episode_id, day, "connection_ineligible", "repair_connection")

    def test_a_later_complete_manifest_moves_readiness_on_and_keeps_the_last_complete_day(self):
        connection_id, established, ad_ids, day = self._collected_day()
        _enrollment_id, episode_id = self._enroll(connection_id, established, ad_ids[0], day - timedelta(days=1))
        with platform_session() as session:
            connection = session.get(MetaConnectionRecord, connection_id)
            connection.state, connection.retired_at = "retired", utcnow()
        self._collect(connection_id, day)
        blocked = self._manifest_for(episode_id, day)
        self.assertEqual(blocked.safe_watermark, day - timedelta(days=2), "nothing vouched for before the Episode")
        with platform_session() as session:
            connection = session.get(MetaConnectionRecord, connection_id)
            connection.state, connection.retired_at = "active", None

        self._run_day(day + timedelta(days=1))

        complete = self._manifest_for(episode_id, day + timedelta(days=1))
        self.assertEqual((complete.state, complete.source_revision), ("complete", blocked.source_revision + 1))
        readiness = self._readiness(episode_id)
        self.assertEqual((readiness.latest_manifest_id, readiness.quality_state, readiness.maturity,
                          readiness.last_complete_local_day, readiness.recovery_action),
                         (complete.id, "pass", "warming", day + timedelta(days=1), None))


class QualityOfCompleteManifests(CollectionTestCase):
    def _day_without_collection(self):
        connection_id, established, ad_ids = self._establish(ads=2)
        day = _local_today() - timedelta(days=3)
        self._run_day(day)
        return connection_id, established, ad_ids, day

    def test_a_run_older_than_the_slo_is_freshness_slo_exceeded(self):
        connection_id, established, ad_ids, day = self._day_without_collection()
        _enrollment_id, episode_id = self._enroll(connection_id, established, ad_ids[0], day)
        runs = self._runs(established.generation_ids["daily"], day)
        with platform_session() as session:
            run = session.get(MetaObservationSyncRunRecord, runs["daily"].id)
            run.completed_at = run.completed_at - timedelta(hours=37)

        self._collect(connection_id, day)

        manifest = self._manifest_for(episode_id, day)
        self.assertEqual(manifest.state, "complete", "stale evidence is quality, not a broken source")
        self.assertEqual(self._issues(manifest.id),
                         [("freshness_slo_exceeded", True, "daily", None, "wait_for_next_sync")])
        readiness = self._readiness(episode_id)
        self.assertEqual((readiness.quality_state, readiness.maturity, readiness.recovery_action,
                          readiness.last_complete_local_day), ("blocked", "insufficient", "wait_for_next_sync", day))

    def test_an_exact_window_without_delivery_is_missing_with_its_quality_issue(self):
        connection_id, established, ad_ids, day = self._day_without_collection()
        _enrollment_id, episode_id = self._enroll(connection_id, established, ad_ids[0], day)
        run = self._runs(established.generation_ids["exact_window"], day)["current_7d"]
        with platform_session() as session:  # Meta returns no row for an ad that did not deliver
            for page in session.scalars(select(MetaEvidencePageRecord).where(
                    MetaEvidencePageRecord.sync_run_id == run.id)):
                body = json.loads(gzip.decompress(page.payload_compressed))
                body["data"] = [row for row in body["data"] if row.get("ad_id") != ad_ids[0]]
                raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
                page.payload_compressed = gzip.compress(raw, mtime=0)
                page.payload_checksum = hashlib.sha256(page.payload_compressed).hexdigest()

        self._collect(connection_id, day)

        manifest = self._manifest_for(episode_id, day)
        self.assertEqual(manifest.state, "complete")
        self.assertEqual({item.window_role: item.state for item in self._items(manifest.id)
                          if item.contract_type == "exact_window"},
                         {"current_7d": "missing", "baseline_7d": "fulfilled", "rolling_14d": "fulfilled"})
        self.assertEqual(self._issues(manifest.id), [
            ("exact_window_value_unavailable", True, "exact_window", "current_7d", "wait_for_next_sync")])
        self.assertEqual(self._readiness(episode_id).quality_state, "blocked")

    def test_a_tampered_page_is_incomplete_pagination(self):
        connection_id, established, ad_ids, day = self._day_without_collection()
        _enrollment_id, episode_id = self._enroll(connection_id, established, ad_ids[0], day)
        run = self._runs(established.generation_ids["delivery_configuration"], day)["configuration"]
        with platform_session() as session:
            page = session.scalar(select(MetaEvidencePageRecord).where(MetaEvidencePageRecord.sync_run_id == run.id))
            page.payload_compressed = gzip.compress(b'{"data":[]}', mtime=0)  # checksum no longer matches

        self._collect(connection_id, day)

        self._assert_blocked(episode_id, day, "incomplete_pagination", "retry_meta_sync", "delivery_configuration")


class Isolation(CollectionTestCase):
    def test_workspace_a_collects_nothing_of_workspace_b(self):
        day = _local_today() - timedelta(days=3)
        a_connection, a_set, a_ads = self._establish(ads=1)
        _a_enrollment, a_episode = self._enroll(a_connection, a_set, a_ads[0], day)
        other = self._provision("ws_scale_collect_b")
        b_connection, b_set, b_ads = self._establish(workspace_id=other, ads=1)
        _b_enrollment, b_episode = self._enroll(b_connection, b_set, b_ads[0], day, workspace_id=other)

        self._run_day(day)

        self.assertEqual(len(self._manifests(a_episode)), 1)
        self.assertEqual(self._rows(ScaleEvidenceSourceManifestRecord, other), [], "B's Episode is untouched")
        self.assertEqual(self._rows(ScaleEvidenceReadinessProjectionRecord, other), [])
        crossed = collector.run_source_collection(self._worker(SANDBOX), connection_id=b_connection, target_date=day)
        self.assertEqual((crossed.manifests, crossed.skipped), ((), ()), "A's context finds no Enrollment of B")
        self.assertEqual(self._rows(ScaleEvidenceSourceManifestRecord, other), [])

        self._run_day(day, other)
        [b_manifest] = self._rows(ScaleEvidenceSourceManifestRecord, other)
        self.assertEqual(b_manifest.episode_id, b_episode)
        b_runs = {run.id for run in self._rows(MetaObservationSyncRunRecord, other)}
        self.assertLessEqual({row.sync_run_id for row in self._rows(ScaleDailyMetricSourceRecord, other)}, b_runs)
        self.assertEqual(len(self._manifests(a_episode)), 1)

    def test_a_sandbox_collection_reads_no_controlled_mock_page(self):
        day = _local_today() - timedelta(days=3)
        sandbox_connection, sandbox_set, sandbox_ads = self._establish(SANDBOX, ads=1)
        mock_connection, mock_set, mock_ads = self._establish(MOCK, ads=1)
        _s, sandbox_episode = self._enroll(sandbox_connection, sandbox_set, sandbox_ads[0], day)
        _m, mock_episode = self._enroll(mock_connection, mock_set, mock_ads[0], day, meta_mode=MOCK)

        self._run_day(day)

        sandbox_runs = {run.id for run in self._rows(MetaObservationSyncRunRecord, meta_mode=SANDBOX)}
        mock_runs = {run.id for run in self._rows(MetaObservationSyncRunRecord, meta_mode=MOCK)}
        self.assertEqual((len(sandbox_runs), len(mock_runs)), (5, 5))
        for episode_id, mode, runs in ((sandbox_episode, SANDBOX, sandbox_runs), (mock_episode, MOCK, mock_runs)):
            [manifest] = self._manifests(episode_id)
            self.assertEqual((manifest.meta_mode, manifest.state), (mode, "complete"))
            used = {row.sync_run_id for model in (ScaleDailyMetricSourceRecord, ScaleExactWindowSourceRecord,
                                                  ScaleDeliveryConfigSourceRecord)
                    for row in self._rows(model, episode_id=episode_id, source_revision=manifest.source_revision)}
            self.assertLessEqual(used, runs, f"the {mode} manifest reads only {mode} runs")
        crossed = collector.run_source_collection(self._worker(SANDBOX), connection_id=mock_connection,
                                                  target_date=day)
        self.assertEqual(crossed.manifests, (), "the sandbox context finds no controlled-mock Enrollment")
        self.assertEqual(len(self._manifests(mock_episode)), 1)


class AbandonedRuns(CollectionTestCase):
    def test_the_next_tick_fails_the_runs_of_a_batch_rejected_for_a_stale_epoch(self):
        connection_id, established, _ads = self._establish(ads=1)
        day = _local_today() - timedelta(days=4)
        fire_at = self._make_due(day)
        runtime = WorkerRuntime(owner=f"ci-only-scale-collect-{uuid.uuid4().hex[:6]}")
        with mock.patch.object(observation_sync, "utcnow", return_value=fire_at + timedelta(minutes=1)):
            quarantine_foreign_jobs(self.workspace_id)
            self.assertTrue(asyncio.run(runtime.run_once()), "the tick")
        with platform_session() as session:  # a role change after the batch was enqueued
            session.get(WorkspaceRecord, self.workspace_id).authorization_epoch += 1
        quarantine_foreign_jobs(self.workspace_id)
        self.assertFalse(asyncio.run(runtime.run_once()), "the claim rejects the batch")
        [batch] = self._rows(JobRecord, type=observation_sync.SYNC_JOB_TYPE)
        self.assertEqual((batch.state, batch.error_code), ("failed", "authorization_epoch_stale"))
        stranded = [run for generation_id in established.generation_ids.values()
                    for run in self._runs(generation_id, day).values()]
        self.assertEqual({run.state for run in stranded}, {"queued"})
        operations = {op.id: op for op in self._rows(OperationRecord)}
        self.assertEqual({operations[run.operation_id].state for run in stranded}, {"queued"})

        self._run_day(day + timedelta(days=1))

        closed = {run.id: run for run in self._rows(MetaObservationSyncRunRecord) if run.id in
                  {s.id for s in stranded}}
        self.assertEqual({run.state for run in closed.values()}, {"failed"})
        self.assertTrue(all(run.completed_at is not None for run in closed.values()))
        operations = {op.id: op for op in self._rows(OperationRecord)}
        self.assertEqual({operations[run.operation_id].state for run in stranded}, {"failed"})
        events = self._rows(AuditEventRecord, event_type="meta.sync_run.abandoned")
        self.assertEqual({event.resource_id for event in events}, set(closed))
        self.assertEqual({(e.details_json["batch_job_state"], e.details_json["batch_job_error_code"],
                           e.details_json["target_date"]) for e in events},
                         {("failed", "authorization_epoch_stale", day.isoformat())})
        next_day = [run for generation_id in established.generation_ids.values()
                    for run in self._runs(generation_id, day + timedelta(days=1)).values()]
        self.assertEqual({run.state for run in next_day}, {"succeeded"}, "the next day reads as usual")

    def test_a_run_whose_batch_may_still_run_is_left_alone(self):
        connection_id, established, _ads = self._establish(ads=1)
        day = _local_today() - timedelta(days=4)
        context = self._worker(SANDBOX)
        fire_at = self._make_due(day)
        with tenant_session(context) as session:
            observation_sync.run_observation_tick(session, context, connection_id=connection_id, fire_at=fire_at,
                                                  now=fire_at + timedelta(minutes=1))
        later = self._make_due(day + timedelta(days=1))
        with tenant_session(context) as session:
            observation_sync.run_observation_tick(session, context, connection_id=connection_id, fire_at=later,
                                                  now=later + timedelta(minutes=1))
        earlier = [run for generation_id in established.generation_ids.values()
                   for run in self._runs(generation_id, day).values()]
        self.assertEqual({run.state for run in earlier}, {"queued"}, "its batch Job is still queued")
        self.assertEqual(self._rows(AuditEventRecord, event_type="meta.sync_run.abandoned"), [])


class CollectionJobs(CollectionTestCase):
    def _bad_job(self, payload: dict) -> str:
        context = self._worker(SANDBOX)
        from app.platform import jobs

        with tenant_session(context) as session:
            return jobs.enqueue_in_session(session, context=context, job_type="scale_collect_sources",
                                           payload=payload, idempotency_key=f"ci-only-{uuid.uuid4().hex}").id

    def test_a_job_without_a_mode_or_day_fails_as_missing_input(self):
        for payload in ({"connection_id": "mcon_acme", "target_date": "2026-11-01"},
                        {"connection_id": "mcon_acme", "meta_mode": SANDBOX, "target_date": "not-a-day"},
                        {"meta_mode": SANDBOX, "target_date": "2026-11-01"}):
            with self.subTest(payload=payload):
                job_id = self._bad_job(payload)
                self._drain()
                [job] = self._rows(JobRecord, id=job_id)
                self.assertEqual((job.state, job.error_code), ("failed", "missing_input"))

    def test_the_scale_worker_runtime_runs_the_collection_too(self):
        connection_id, established, ad_ids = self._establish(ads=1)
        day = _local_today() - timedelta(days=3)
        self._run_day(day)
        _enrollment_id, episode_id = self._enroll(connection_id, established, ad_ids[0], day)
        context = self._worker(SANDBOX)
        with tenant_session(context) as session:
            job_id = collector.enqueue_source_collection(session, context, connection_id=connection_id,
                                                         target_date=day).id
        quarantine_foreign_jobs(self.workspace_id)

        self.assertTrue(ScaleWorkerRuntime(owner="ci-only-scale-runtime").run_once())

        [job] = self._rows(JobRecord, id=job_id)
        self.assertEqual(job.state, "succeeded")
        self.assertEqual(self._manifest_for(episode_id, day).state, "complete")

    def test_one_failing_episode_does_not_hold_back_the_others_and_the_retry_writes_once(self):
        connection_id, established, ad_ids = self._establish(ads=2)
        day = _local_today() - timedelta(days=3)
        self._run_day(day)
        first = self._enroll(connection_id, established, ad_ids[0], day)
        second = self._enroll(connection_id, established, ad_ids[1], day)
        failing = sorted([first, second])[0]
        original = collector._write

        def flaky(session, context, episode, *args, **kwargs):
            if episode.id == failing[1]:
                raise RuntimeError("simulated database hiccup")
            return original(session, context, episode, *args, **kwargs)

        with mock.patch.object(collector, "_write", flaky):
            with self.assertRaises(collector.SourceCollectionFailed) as raised:
                collector.run_source_collection(self._worker(SANDBOX), connection_id=connection_id, target_date=day)
        self.assertEqual(set(raised.exception.failures), {failing[0]})
        other = first if failing == second else second
        self.assertEqual(len(self._manifests(other[1])), 1)
        self.assertEqual(self._manifests(failing[1]), [])

        retry = collector.run_source_collection(self._worker(SANDBOX), connection_id=connection_id, target_date=day)

        self.assertEqual(sorted((m.episode_id, m.replayed) for m in retry.manifests),
                         sorted([(other[1], True), (failing[1], False)]))
        self.assertEqual([len(self._manifests(episode)) for _e, episode in (first, second)], [1, 1])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
