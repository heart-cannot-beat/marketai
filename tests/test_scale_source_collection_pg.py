"""Scale source collection under the real roles on PostgreSQL (#109, #101 S3).

SQLite cannot see RLS, a missing grant or a row lock without UPDATE, so here
the API runs as ``marketai_domain_api`` and the Worker as
``marketai_domain_worker``:

* the main path: a ``customer_sandbox`` connection and binding made through
  the API, the Scale contract set built by production code as the API role,
  and two active Enrollments on it (activation is #63's path, so the owner
  seeds them). With the slot due, only ``WorkerRuntime.run_once`` runs: the
  tick, the batch, then ``scale_collect_sources``. The Enrollment in the
  account's currency gets a ``complete`` manifest with its source records and
  a readiness projection; the one whose Episode is in another currency gets a
  ``blocked`` one (``source_provenance_mismatch``) and readiness
  ``insufficient`` -- no snapshot, run or Evaluation either way. A replay as
  the Worker finds the same manifest and allocates no revision;
* isolation: a controlled_mock connection collected in the same drain reads
  only its own runs; the Worker's sandbox context sees only sandbox pages;
  Workspace B's context collects nothing of A's connection;
* #107's residual: a batch Job the claim function rejects for a stale
  authorization epoch leaves its runs and Operations queued until the next
  tick of the connection fails them.

Workspaces are opened as the schema owner (BYPASSRLS), as the control plane
would. The Scale evidence tables refuse DELETE, so every Workspace id is new;
each test pauses its schedules and cancels its open Jobs when it ends. Run
with ``DOMAIN_OWNER_DATABASE_URL``, ``DOMAIN_API_DATABASE_URL``,
``DOMAIN_WORKER_DATABASE_URL``, ``MARKETAI_DATABASE_URL`` (the owner) and
``MARKETAI_TENANT_CONTEXT_SECRET`` against a cluster released with
``make db-release``.
"""
from __future__ import annotations

import asyncio
import os
import unittest
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.meta.fake_adapter import FakeMetaAdapter
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AuditEventRecord, JobRecord, WorkspaceRecord, new_id, utcnow
from app.main import app
from app.meta import observation_sync
from app.meta.connections import create_connection
from app.meta.models import (
    MetaEvidencePageRecord,
    MetaObservationSyncRunRecord,
    MetaObservationSyncScheduleRecord,
    ObservationContractGenerationRecord,
    ScenarioVersionRecord,
)
from app.meta.observation import ensure_observation_schedule, observation_target_date
from app.platform.http_identity import request_tenant_context
from app.platform.operations import OperationRecord
from app.platform.tenancy import TenantContext
from app.scale import collector
from app.scale.delivery_targets import ensure_metric_contract_set
from app.scale.models import (
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
    ScaleSourceQualityIssueRecord,
)
from app.worker_runtime import WorkerRuntime, meta_write_capability_findings
from tests.support.meta_observation import AD_ACCOUNT, seed_sandbox_ads, worker_context
from tests.support.scale_contract_set import create_connection_through_api
from tests.test_scale_evidence import _delivery_source

URLS = [os.getenv(key) for key in (
    "DOMAIN_OWNER_DATABASE_URL", "DOMAIN_API_DATABASE_URL", "DOMAIN_WORKER_DATABASE_URL",
)]
OWNER, API, WORKER = range(3)
SANDBOX = "customer_sandbox"
MOCK = "controlled_mock"
OWNER_ID = "usr_acme_owner"


@unittest.skipUnless(all(URLS) and os.getenv("MARKETAI_TENANT_CONTEXT_SECRET"),
                     "requires isolated PostgreSQL runtime roles")
class ScaleSourceCollectionPostgresTests(unittest.TestCase):
    def setUp(self):
        self.engines = [create_engine(url) for url in URLS]
        for engine in self.engines:
            self.addCleanup(engine.dispose)
        self.owner = self.engines[OWNER]
        self.sessions = [sessionmaker(bind=e, expire_on_commit=False, autoflush=False) for e in self.engines]
        self.current: TenantContext | None = None
        app.dependency_overrides[request_tenant_context] = lambda: self.current
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.workspaces: list[str] = []
        self.addCleanup(self._quiesce)

    @contextmanager
    def _as(self, index: int):
        with patch("app.adapters.persistence.database.SessionLocal", self.sessions[index]), \
             patch("app.platform.jobs.engine", self.engines[index]):
            yield

    def _workspace(self, prefix: str) -> TenantContext:
        workspace_id = f"ws_scale_collect_pg_{prefix}_{uuid.uuid4().hex[:10]}"
        with Session(self.owner) as session, session.begin():  # the control plane opens it
            session.add(WorkspaceRecord(id=workspace_id, name=f"Acme Charge {workspace_id[-4:]}"))
        self.workspaces.append(workspace_id)
        return TenantContext(workspace_id=workspace_id, role="owner", initiator_id=OWNER_ID,
                             request_id="test-req", trace_id="trace-test")

    def _quiesce(self) -> None:
        if not self.workspaces:
            return
        with Session(self.owner) as session, session.begin():
            session.execute(update(MetaObservationSyncScheduleRecord)
                            .where(MetaObservationSyncScheduleRecord.workspace_id.in_(self.workspaces),
                                   MetaObservationSyncScheduleRecord.state == "active")
                            .values(state="paused"))
            session.execute(update(JobRecord)
                            .where(JobRecord.workspace_id.in_(self.workspaces),
                                   JobRecord.state.in_(("queued", "retry_wait")))
                            .values(state="cancelled", finished_at=utcnow()))

    def _epoch(self, workspace_id: str) -> int:
        with Session(self.owner) as session:
            return int(session.get(WorkspaceRecord, workspace_id).authorization_epoch)

    def _worker(self, workspace_id: str, meta_mode: str = SANDBOX) -> TenantContext:
        return worker_context(workspace_id, meta_mode, authorization_epoch=self._epoch(workspace_id))

    def _make_due(self, workspace_id: str, days_back: int = 1):
        with Session(self.owner) as session, session.begin():
            rows = list(session.scalars(select(MetaObservationSyncScheduleRecord).where(
                MetaObservationSyncScheduleRecord.workspace_id == workspace_id,
                MetaObservationSyncScheduleRecord.state == "active")))
            self.assertTrue(rows)
            fire_at = max(row.next_fire_at for row in rows) - timedelta(days=days_back)
            session.execute(update(MetaObservationSyncScheduleRecord)
                            .where(MetaObservationSyncScheduleRecord.workspace_id == workspace_id,
                                   MetaObservationSyncScheduleRecord.state == "active")
                            .values(next_fire_at=fire_at))
        return fire_at

    def _drain(self, workspace_id: str, runtime: WorkerRuntime | None = None) -> int:
        runtime = runtime or WorkerRuntime(owner=f"ci-only-scale-collect-pg-{uuid.uuid4().hex[:6]}")
        handled = 0
        for _ in range(40):
            with Session(self.owner) as session, session.begin():
                session.execute(update(JobRecord)
                                .where(JobRecord.workspace_id != workspace_id,
                                       JobRecord.state.in_(("queued", "retry_wait")))
                                .values(next_attempt_at=utcnow() + timedelta(days=1)))
            with self._as(WORKER):
                if not asyncio.run(runtime.run_once()):
                    self.assertEqual(meta_write_capability_findings(runtime), [])
                    return handled
            handled += 1
        self.fail("the Worker did not go idle")

    def _rows(self, model, workspace_id: str, **filters) -> list:
        with Session(self.owner) as session:
            rows = list(session.scalars(select(model).where(model.workspace_id == workspace_id).filter_by(**filters)))
            for row in rows:
                session.expunge(row)
            return rows

    # -- fixtures -----------------------------------------------------------

    def _sandbox_connection(self, owner: TenantContext) -> tuple[str, object]:
        """Connection and binding through the API; the contract set by production code as the API role."""
        self.current = owner
        with self._as(API):
            connection_id = create_connection_through_api(self, self.client, SANDBOX)["id"]
        context = replace(owner, meta_mode=SANDBOX)
        with self._as(API), tenant_session(context) as session:
            established = ensure_metric_contract_set(session, context, connection_id=connection_id)
        return connection_id, established

    def _mock_connection(self, owner: TenantContext) -> tuple[str, object]:
        context = replace(owner, meta_mode=MOCK)
        with self._as(API), tenant_session(context) as session:
            connection_id = create_connection(session, context=context, ad_account_id=AD_ACCOUNT).id
        self.current = owner
        with self._as(API):
            binding = self.client.post(f"/api/v1/meta/connections/{connection_id}/bindings",
                                       json={"ad_account_id": AD_ACCOUNT, "meta_mode": MOCK})
            self.assertEqual(binding.status_code, 201, binding.text)
        with self._as(API), tenant_session(context) as session:
            established = ensure_metric_contract_set(session, context, connection_id=connection_id)
        with self._as(API), tenant_session(worker_context(owner.workspace_id, MOCK)) as session:
            for generation_id in established.generation_ids.values():  # the explicit test entry (#101 §6)
                ensure_observation_schedule(session, session.get(ObservationContractGenerationRecord, generation_id))
        return connection_id, established

    def _enroll(self, workspace_id: str, meta_mode: str, connection_id: str, established, ad_id: str,
                started, *, currency: str = "USD") -> tuple[str, str]:
        """An active Enrollment and its Episode on the ad, seeded as the owner."""
        with self._as(WORKER), tenant_session(self._worker(workspace_id, meta_mode)) as session:
            scenario = session.scalar(select(ScenarioVersionRecord).where(
                ScenarioVersionRecord.meta_connection_id == connection_id,
                ScenarioVersionRecord.is_default == True))  # noqa: E712
            page = FakeMetaAdapter(session, workspace_id, meta_mode, scenario.id).read_delivery_configuration(
                ad_account_id=AD_ACCOUNT, edge="ads", fields=["id", "adset_id", "campaign_id"], as_of=started,
                limit=100)
            row = next(item for item in page.payload["data"] if item["id"] == ad_id)
            scenario_key = scenario.id if meta_mode == MOCK else "__no_scenario__"
        common = dict(
            workspace_id=workspace_id, meta_mode=meta_mode, meta_connection_id=connection_id,
            scenario_scope_key=scenario_key, ad_account_id=AD_ACCOUNT, winner_decision_id="wd_acme",
            winner_confirmation_id="wc_acme", asset_version_id="av_acme", product_version_id="pv_acme",
            offer_version_id="ov_acme", market_code="US", campaign_remote_id=row["campaign_id"],
            ad_set_remote_id=row["adset_id"], ad_remote_id=ad_id,
        )
        enrollment = ScaleEnrollmentRecord(
            id=new_id("senr"), remote_binding_key=f"{connection_id}/{AD_ACCOUNT}/{ad_id}", lifecycle="active",
            creator_id=OWNER_ID, original_initiator_id=OWNER_ID, **common,
        )
        episode = ScaleEpisodeRecord(
            id=new_id("sep"), enrollment_id=enrollment.id, delivery_config_baseline_source_id="sdcs_pending",
            delivery_evidence_hash="e" * 64, delivery_comparability_hash="c" * 64,
            metric_semantic_contract_set_id=established.contract_set_id, timezone_name="UTC", currency=currency,
            attribution_semantics_hash="a" * 64, phase="collecting", started_local_day=started, **common,
        )
        with Session(self.owner) as session, session.begin():
            session.add(enrollment)
            session.flush()
            session.add(episode)
            session.flush()
            baseline = _delivery_source(workspace_id, meta_mode, episode.id, revision=1)
            baseline.meta_connection_id, baseline.scenario_scope_key = connection_id, scenario_key
            baseline.ad_account_id = AD_ACCOUNT
            session.add(baseline)
            session.flush()
            episode.delivery_config_baseline_source_id = baseline.id
            enrollment.current_episode_id = episode.id
            ids = enrollment.id, episode.id
        return ids

    # -- the main path ------------------------------------------------------

    def test_the_worker_collects_complete_and_blocked_manifests_under_rls(self):
        owner = self._workspace("main")
        connection_id, established = self._sandbox_connection(owner)
        mock_connection_id, mock_established = self._mock_connection(owner)
        workspace_id = owner.workspace_id
        with self._as(WORKER):  # a sandbox Launch's ads
            ad_ids = seed_sandbox_ads(self._worker(workspace_id), connection_id, 2)
            [mock_ad] = seed_sandbox_ads(self._worker(workspace_id, MOCK), mock_connection_id, 1)
        fire_at = self._make_due(workspace_id)
        day = observation_target_date(fire_at, "UTC")
        _usd, usd_episode = self._enroll(workspace_id, SANDBOX, connection_id, established, ad_ids[0],
                                         day - timedelta(days=2))
        _eur, eur_episode = self._enroll(workspace_id, SANDBOX, connection_id, established, ad_ids[1], day,
                                         currency="EUR")
        _mock, mock_episode = self._enroll(workspace_id, MOCK, mock_connection_id, mock_established, mock_ad, day)

        self.assertEqual(self._drain(workspace_id), 6, "per connection: one tick, one batch, one collection")

        jobs = self._rows(JobRecord, workspace_id, type="scale_collect_sources")
        self.assertEqual({(job.state, job.payload_json["meta_mode"]) for job in jobs},
                         {("succeeded", SANDBOX), ("succeeded", MOCK)})
        sandbox_runs = {run.id for run in self._rows(MetaObservationSyncRunRecord, workspace_id, meta_mode=SANDBOX)}
        mock_runs = {run.id for run in self._rows(MetaObservationSyncRunRecord, workspace_id, meta_mode=MOCK)}

        [complete] = self._rows(ScaleEvidenceSourceManifestRecord, workspace_id, episode_id=usd_episode)
        self.assertEqual((complete.state, complete.source_revision, complete.target_local_day,
                          complete.collection_key),
                         ("complete", 2, day, f"scale-collect:{connection_id}:{day.isoformat()}:1"))
        daily = self._rows(ScaleDailyMetricSourceRecord, workspace_id, episode_id=usd_episode)
        self.assertEqual(sorted(row.local_day for row in daily), [day - timedelta(days=k) for k in (2, 1, 0)],
                         "only the Episode's own days")
        self.assertLessEqual({row.sync_run_id for row in daily}, sandbox_runs)
        self.assertEqual(len(self._rows(ScaleExactWindowSourceRecord, workspace_id, episode_id=usd_episode)), 3)
        [configuration] = self._rows(ScaleDeliveryConfigSourceRecord, workspace_id, episode_id=usd_episode,
                                     source_revision=2)
        self.assertNotEqual(configuration.diagnostic_state, "unknown", "the sandbox returns learning_stage_info")
        self.assertEqual({entry["local_day"] for entry in configuration.status_history}, {day.isoformat()})
        [readiness] = self._rows(ScaleEvidenceReadinessProjectionRecord, workspace_id, episode_id=usd_episode)
        self.assertEqual((readiness.latest_manifest_id, readiness.maturity, readiness.quality_state),
                         (complete.id, "warming", "pass"))

        [blocked] = self._rows(ScaleEvidenceSourceManifestRecord, workspace_id, episode_id=eur_episode)
        self.assertEqual((blocked.state, blocked.last_complete_local_day), ("blocked", None))
        self.assertEqual({(issue.code, issue.recovery_action) for issue in self._rows(
            ScaleSourceQualityIssueRecord, workspace_id, manifest_id=blocked.id)},
            {("source_provenance_mismatch", "repair_connection")})
        self.assertEqual({item.state for item in self._rows(ScaleManifestItemRecord, workspace_id,
                                                            manifest_id=blocked.id)}, {"failed"})
        self.assertEqual(self._rows(ScaleDailyMetricSourceRecord, workspace_id, episode_id=eur_episode), [])
        [blocked_readiness] = self._rows(ScaleEvidenceReadinessProjectionRecord, workspace_id,
                                         episode_id=eur_episode)
        self.assertEqual((blocked_readiness.maturity, blocked_readiness.quality_state,
                          blocked_readiness.recovery_action), ("insufficient", "blocked", "repair_connection"))
        for model in (ScaleEvidenceSnapshotRecord, ScaleEvaluationRunRecord, ScaleEvaluationRecord):
            self.assertEqual(self._rows(model, workspace_id), [], "S3 creates no snapshot, run or Evaluation")

        [mock_manifest] = self._rows(ScaleEvidenceSourceManifestRecord, workspace_id, episode_id=mock_episode)
        self.assertEqual((mock_manifest.meta_mode, mock_manifest.state), (MOCK, "complete"))
        mock_daily = self._rows(ScaleDailyMetricSourceRecord, workspace_id, episode_id=mock_episode)
        self.assertLessEqual({row.sync_run_id for row in mock_daily}, mock_runs,
                             "the controlled_mock manifest reads only controlled_mock runs")

        # RLS, not only the collector's filters: the Worker's sandbox context
        # sees exactly the sandbox pages.
        with self._as(WORKER), tenant_session(self._worker(workspace_id)) as session:
            visible = set(session.scalars(select(MetaEvidencePageRecord.sync_run_id)))
        self.assertEqual(visible, sandbox_runs)

        # Replay as the Worker: the same manifest, no new revision.
        with self._as(WORKER):
            replay = collector.run_source_collection(self._worker(workspace_id), connection_id=connection_id,
                                                     target_date=day)
        self.assertEqual(sorted((m.manifest_id, m.replayed) for m in replay.manifests),
                         sorted([(complete.id, True), (blocked.id, True)]))
        self.assertEqual(len(self._rows(ScaleEvidenceSourceManifestRecord, workspace_id)), 3)

        # Workspace B's context collects nothing of A's connection.
        other = self._workspace("other")
        with self._as(WORKER):
            crossed = collector.run_source_collection(self._worker(other.workspace_id), connection_id=connection_id,
                                                      target_date=day)
        self.assertEqual((crossed.manifests, crossed.skipped), ((), ()))
        self.assertEqual(self._rows(ScaleEvidenceSourceManifestRecord, other.workspace_id), [])
        self.assertEqual(len(self._rows(ScaleEvidenceSourceManifestRecord, workspace_id)), 3)
        with self._as(WORKER), tenant_session(self._worker(workspace_id, MOCK)) as session:
            self.assertIsNone(collector.collect_episode_sources(
                session, self._worker(workspace_id, MOCK), enrollment_id=_usd, connection_id=connection_id,
                target_date=day), "the controlled_mock context cannot see the sandbox Enrollment")

    def test_the_next_tick_fails_the_runs_of_a_batch_rejected_for_a_stale_epoch(self):
        owner = self._workspace("epoch")
        connection_id, established = self._sandbox_connection(owner)
        workspace_id = owner.workspace_id
        with self._as(WORKER):
            seed_sandbox_ads(self._worker(workspace_id), connection_id, 1)
        first = self._make_due(workspace_id, days_back=2)
        runtime = WorkerRuntime(owner=f"ci-only-scale-collect-pg-{uuid.uuid4().hex[:6]}")
        with self._as(WORKER), patch.object(observation_sync, "utcnow", return_value=first + timedelta(minutes=1)):
            self.assertTrue(asyncio.run(runtime.run_once()), "the tick")
        with Session(self.owner) as session, session.begin():  # a role change after the batch was enqueued
            session.get(WorkspaceRecord, workspace_id).authorization_epoch += 1
        with self._as(WORKER):
            self.assertFalse(asyncio.run(runtime.run_once()), "marketai_claim_next_job_v2 rejects the batch")
        [batch] = self._rows(JobRecord, workspace_id, type=observation_sync.SYNC_JOB_TYPE)
        self.assertEqual((batch.state, batch.error_code), ("failed", "authorization_epoch_stale"))
        stranded = self._rows(MetaObservationSyncRunRecord, workspace_id)
        self.assertEqual((len(stranded), {run.state for run in stranded}), (5, {"queued"}))

        second = self._make_due(workspace_id, days_back=0)  # the slot the tick moved the schedule to
        with patch.object(observation_sync, "utcnow", return_value=second + timedelta(minutes=1)):
            self._drain(workspace_id)

        runs = {run.id: run for run in self._rows(MetaObservationSyncRunRecord, workspace_id)}
        self.assertEqual({runs[run.id].state for run in stranded}, {"failed"})
        operations = {op.id: op.state for op in self._rows(OperationRecord, workspace_id)}
        self.assertEqual({operations[run.operation_id] for run in stranded}, {"failed"})
        events = self._rows(AuditEventRecord, workspace_id, event_type="meta.sync_run.abandoned")
        self.assertEqual({event.resource_id for event in events}, {run.id for run in stranded})
        self.assertEqual({event.details_json["batch_job_error_code"] for event in events},
                         {"authorization_epoch_stale"})
        self.assertEqual({run.state for run_id, run in runs.items() if run_id not in {s.id for s in stranded}},
                         {"succeeded"}, "the next day reads as usual")
        with Session(self.owner) as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(JobRecord).where(
                JobRecord.workspace_id == workspace_id, JobRecord.type == "scale_collect_sources")), 0,
                "no Enrollment: nothing for Scale to collect")


if __name__ == "__main__":
    unittest.main()
