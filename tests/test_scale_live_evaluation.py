"""Live Scale evaluations triggered by complete source manifests, on SQLite (#110, #101 S4).

The fixture is #109's: a ``customer_sandbox`` connection whose Scale contract
set production code built, sandbox ads, and an active Enrollment whose
Episode carries the baseline activation synthesizes (``unverified``, the
DeliveryTarget's creative). A published Policy permits the mode: the
migrated Beta default (#127), or one a test publishes later. With the day's
slot due, only the Worker runs: the tick, the batch, the collection --
whose complete manifest requests the live run and enqueues ``scale_evaluate``
in its own transaction -- and the evaluation.

* the run freezes the manifest, its ``terminal_at``, the Policy (#101 Q8;
  without a test's own, the migrated Beta default, #127),
  the active Target or none, and the batch's last sync run; the first
  complete manifest adopts the real configuration as the Episode's baseline,
  so the first evaluation does not roll the Episode over (a different
  creative still does);
* manifest, run and Job commit together; replaying the tick, batch,
  collection and evaluation Jobs adds nothing;
* no run for a blocked manifest, a day that has not ended, a day older than
  the Episode's latest complete one (#101 Q9), a paused Enrollment, or when
  no Policy is eligible;
* adjacent days: the later run waits while the earlier one holds the
  Episode, and evaluates against it once it is done;
* a stuck run is redriven twice, then alerted on, and the Episode gets no
  new run (#101 Q10);
* Workspace and mode isolation.

``tests/test_scale_live_evaluation_pg.py`` runs fourteen days through the API
and the Worker under the real roles. Fictional brand only: Acme Charge.
"""
from __future__ import annotations

import asyncio
import gzip
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest import mock

from sqlalchemy import select, update

from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import AuditEventRecord, JobRecord, new_id, utcnow
from app.meta import observation_sync, sync_pipeline
from app.meta.models import MetaEvidencePageRecord, MetaObservationSyncRunRecord
from app.platform.ops_alerts import AlertCategory, Severity
from app.scale import collector, live_trigger
from app.scale.enrollment import UNVERIFIED_SYNC_PREFIX
from app.scale.evaluator import CURRENT_ALGORITHM_VERSION
from app.scale.models import (
    NO_TARGET_MARKER,
    ScaleDeliveryConfigSourceRecord,
    ScaleEnrollmentRecord,
    ScaleEpisodeProjectionRecord,
    ScaleEpisodeRecord,
    ScaleEvaluationRecord,
    ScaleEvaluationRunRecord,
    ScaleEvidenceSnapshotRecord,
    ScaleEvidenceSourceManifestRecord,
    ScalePolicyVersionRecord,
    ScaleTargetVersionRecord,
)
from app.scale.platform_policy import BETA_DEFAULT_POLICY_V2, PUBLISHED_POLICY_VERSIONS
from app.scale.service import (
    EVALUATION_JOB_MAX_ATTEMPTS,
    ScaleOrchestrationError,
    enqueue_scale_evaluation,
)
from app.worker_runtime import WorkerRuntime
from tests.support.queue_hygiene import quarantine_foreign_jobs
from tests.test_scale_evidence import _SIGNAL_THRESHOLDS, _policy
from tests.test_scale_source_collection import (
    MOCK,
    OWNER_ID,
    SANDBOX,
    ZONE,
    CollectionTestCase,
    _local_today,
)

EVALUATE = "scale_evaluate"


class _WorkerKilled(BaseException):
    """The Worker process dies: nothing in the handler catches it."""


class LiveEvaluationTestCase(CollectionTestCase):
    LIVE_TRIGGER = True

    def setUp(self) -> None:
        super().setUp()
        self.policy_id = self._publish_policy()

    # -- fixtures -----------------------------------------------------------

    def _publish_policy(self, **overrides) -> str:
        """A Policy published after the migrated Beta default, so the live
        trigger prefers it; unpublished again when the test ends, so no other
        file's collection finds it in the shared database. It runs the
        deployed algorithm, as the live trigger requires (beta-v2 also reads
        the consecutive breakdown hits, #133)."""
        fields = dict(mode_permissions={SANDBOX: True, MOCK: True}, published_at=utcnow(),
                      algorithm_version=CURRENT_ALGORITHM_VERSION, canonical_schema_version="scale-snapshot-v4",
                      signal_thresholds={**_SIGNAL_THRESHOLDS, "consecutive_breakdown_hits_required": 2})
        fields.update(overrides)
        policy = _policy(**fields)
        with platform_session() as session:
            session.add(policy)
        self.addCleanup(self._unpublish, policy.id)
        return policy.id

    def _unpublish(self, policy_id: str) -> None:
        with platform_session() as session:
            session.execute(update(ScalePolicyVersionRecord).where(ScalePolicyVersionRecord.id == policy_id)
                            .values(is_published=False))

    def _withdraw_platform_policy(self) -> None:
        """Take the migrated Beta defaults (#127, #132) out of selection for
        this test only, so no Policy is eligible; published again when the
        test ends."""
        for version in PUBLISHED_POLICY_VERSIONS:
            self._unpublish(version.id)
        self.addCleanup(self._republish_platform_policy)

    def _republish_platform_policy(self) -> None:
        with platform_session() as session:
            session.execute(update(ScalePolicyVersionRecord)
                            .where(ScalePolicyVersionRecord.id.in_([v.id for v in PUBLISHED_POLICY_VERSIONS]))
                            .values(is_published=True))

    def _activated(self, connection_id: str, established, ad_id: str, started: date, *,
                   creative_id: str | None = None, **kwargs) -> tuple[str, str]:
        """An Enrollment whose Episode carries the baseline activation
        synthesizes: no configuration behind it, the DeliveryTarget's creative."""
        enrollment_id, episode_id = self._enroll(connection_id, established, ad_id, started, **kwargs)
        creative = creative_id or self._ad_configuration(SANDBOX, connection_id, ad_id, started)["creative"]["id"]
        with platform_session() as session:
            episode = session.get(ScaleEpisodeRecord, episode_id)
            baseline = session.get(ScaleDeliveryConfigSourceRecord, episode.delivery_config_baseline_source_id)
            baseline.sync_run_id = f"{UNVERIFIED_SYNC_PREFIX}sdes_{uuid.uuid4().hex[:8]}"
            baseline.ad_creative_id = creative
            baseline.campaign_status = baseline.ad_set_status = baseline.ad_status = "unverified"
        return enrollment_id, episode_id

    def _sandbox(self, *, ads: int = 1, started: date, **kwargs):
        connection_id, established, ad_ids = self._establish(ads=ads)
        enrollment_id, episode_id = self._activated(connection_id, established, ad_ids[0], started, **kwargs)
        return connection_id, established, ad_ids, enrollment_id, episode_id

    # -- driving ------------------------------------------------------------

    def _steps(self, day: date, *, exclude_types: tuple[str, ...] = (), runtime=None) -> int:
        """Only the Worker, with D's slot due a minute ago, leaving ``exclude_types`` queued."""
        fire_at = self._make_due(day)
        runtime = runtime or WorkerRuntime(owner=f"ci-only-live-eval-{uuid.uuid4().hex[:6]}")
        handled = 0
        with mock.patch.object(observation_sync, "utcnow", return_value=fire_at + timedelta(minutes=1)):
            for _ in range(40):
                quarantine_foreign_jobs(self.workspace_id)
                if not asyncio.run(runtime.run_once(exclude_types=exclude_types)):
                    return handled
                handled += 1
        self.fail("the Worker did not go idle")

    def _requeue(self, job_id: str) -> None:
        """A replay: the Job runs again as if its success had never been recorded."""
        with platform_session() as session:
            session.execute(update(JobRecord).where(JobRecord.id == job_id).values(
                state="queued", next_attempt_at=None, finished_at=None, lease_owner=None, lease_expires_at=None))

    def _release(self, *job_ids: str, expire_leases: bool = False) -> None:
        with platform_session() as session:
            values = dict(next_attempt_at=utcnow() - timedelta(seconds=1))
            if expire_leases:
                values["lease_expires_at"] = utcnow() - timedelta(seconds=1)
            session.execute(update(JobRecord).where(JobRecord.id.in_(job_ids)).values(**values))

    # -- reading back -------------------------------------------------------

    def _runs_of(self, episode_id: str) -> list[ScaleEvaluationRunRecord]:
        return sorted(self._rows(ScaleEvaluationRunRecord, episode_id=episode_id),
                      key=lambda run: (run.target_local_day, run.created_at))

    def _run_for(self, episode_id: str, day: date) -> ScaleEvaluationRunRecord:
        [run] = [run for run in self._runs_of(episode_id) if run.target_local_day == day]
        return run

    def _job_of(self, run_id: str, key_suffix: str = "") -> JobRecord:
        [job] = self._rows(JobRecord, idempotency_key=f"scale-eval:{run_id}:{CURRENT_ALGORITHM_VERSION}{key_suffix}")
        return job

    def _events(self, event_type: str) -> list[AuditEventRecord]:
        return self._rows(AuditEventRecord, event_type=event_type)

    def _counts(self) -> dict[str, int]:
        return {model.__tablename__: len(self._rows(model)) for model in (
            ScaleEvidenceSourceManifestRecord, ScaleEvaluationRunRecord, ScaleEvidenceSnapshotRecord,
            ScaleEvaluationRecord)} | {"jobs": len(self._rows(JobRecord))}


class LiveTrigger(LiveEvaluationTestCase):
    def test_a_complete_manifest_requests_and_enqueues_one_live_run_that_evaluates(self):
        day = _local_today() - timedelta(days=3)
        _connection, _set, ad_ids, enrollment_id, episode_id = self._sandbox(started=day - timedelta(days=2))
        with platform_session() as session:
            unverified = session.get(ScaleEpisodeRecord, episode_id).delivery_config_baseline_source_id

        self.assertEqual(self._steps(day), 4, "tick, batch, collection, evaluation")

        manifest = self._manifest_for(episode_id, day)
        [run] = self._runs_of(episode_id)
        self.assertEqual((run.trigger_kind, run.target_local_day, run.source_manifest_id, run.source_revision,
                          run.source_manifest_hash),
                         ("live", day, manifest.id, manifest.source_revision, manifest.canonical_hash))
        self.assertEqual(run.freshness_reference_at.replace(tzinfo=None), manifest.terminal_at.replace(tzinfo=None),
                         "the persisted terminal_at, never the clock")
        self.assertEqual((run.policy_version_id, run.normalized_target_key, run.target_version_id,
                          run.algorithm_version, run.original_initiator_id),
                         (self.policy_id, NO_TARGET_MARKER, None, CURRENT_ALGORITHM_VERSION, OWNER_ID))
        [configuration] = self._rows(ScaleDeliveryConfigSourceRecord, episode_id=episode_id,
                                     source_revision=manifest.source_revision)
        last = max((r for r in self._rows(MetaObservationSyncRunRecord)
                    if r.occurrence_id.split(":")[2] == day.isoformat()),
                   key=lambda r: (r.completed_at, r.id))
        self.assertEqual(run.triggering_sync_run_id, last.id, "the batch's last run triggered it")
        self.assertEqual((run.run_state, run.outcome_kind), ("evaluated", "evaluated"))

        job = self._job_of(run.id)
        self.assertEqual((job.state, job.payload_json, job.max_attempts, job.resource_id),
                         ("succeeded", {"evaluation_run_id": run.id, "meta_mode": SANDBOX},
                          EVALUATION_JOB_MAX_ATTEMPTS, run.id))

        # The first real read became the baseline: no rollover.
        with platform_session() as session:
            enrollment = session.get(ScaleEnrollmentRecord, enrollment_id)
            episode = session.get(ScaleEpisodeRecord, episode_id)
            self.assertEqual(enrollment.current_episode_id, episode_id)
            self.assertEqual((episode.phase, episode.phase_run_id, episode.delivery_config_baseline_source_id,
                              episode.delivery_comparability_hash, episode.accepted_evaluation_id),
                             ("collecting", None, configuration.id, configuration.comparability_hash,
                              run.evaluation_id))
            self.assertIsNotNone(session.get(ScaleDeliveryConfigSourceRecord, unverified), "kept, append-only")
        [verified] = self._events("scale.episode.baseline_verified")
        self.assertEqual((verified.resource_id, verified.details_json["unverified_source_id"],
                          verified.details_json["observed_source_id"]), (episode_id, unverified, configuration.id))
        [requested] = self._events("scale.live_evaluation.requested")
        self.assertEqual((requested.resource_id, requested.details_json["job_id"]), (run.id, job.id))
        [projection] = self._rows(ScaleEpisodeProjectionRecord, episode_id=episode_id)
        self.assertEqual((projection.evaluation_id, projection.maturity), (run.evaluation_id, "warming"))
        self.assertEqual(self._readiness(episode_id).maturity, "warming")

    def test_without_a_test_policy_the_run_freezes_the_migrated_beta_default(self):
        """#127, #132: the database only ran the migrations, and the live
        trigger finds the latest Beta default they published that runs the
        deployed algorithm, v2 -- no ``no_eligible_policy``."""
        self._unpublish(self.policy_id)
        with platform_session() as session:
            self.assertEqual(live_trigger.eligible_live_policy(session, SANDBOX).id, BETA_DEFAULT_POLICY_V2.id)
            self.assertIsNone(live_trigger.eligible_live_policy(session, MOCK), "#127 P1: not controlled_mock")
        day = _local_today() - timedelta(days=3)
        _connection, _set, _ads, _enrollment_id, episode_id = self._sandbox(started=day - timedelta(days=2))

        self.assertEqual(self._steps(day), 4, "tick, batch, collection, evaluation")

        [run] = self._runs_of(episode_id)
        self.assertEqual((run.policy_version_id, run.algorithm_version, run.run_state, run.outcome_kind),
                         (BETA_DEFAULT_POLICY_V2.id, CURRENT_ALGORITHM_VERSION, "evaluated", "evaluated"))
        self.assertEqual(self._events("scale.live_evaluation.skipped"), [])
        [requested] = self._events("scale.live_evaluation.requested")
        self.assertEqual(requested.details_json["policy_version_id"], BETA_DEFAULT_POLICY_V2.id)
        [snapshot] = self._rows(ScaleEvidenceSnapshotRecord, episode_id=episode_id)
        self.assertEqual((snapshot.policy_version_id, snapshot.canonical_schema_version),
                         (BETA_DEFAULT_POLICY_V2.id, "scale-snapshot-v4"))

    def test_another_creative_than_the_target_named_is_not_adopted_and_rolls_the_episode_over(self):
        day = _local_today() - timedelta(days=3)
        _connection, _set, _ads, enrollment_id, episode_id = self._sandbox(
            started=day, creative_id="creative_fake_not_the_ads")

        self._steps(day)

        [event] = self._events("scale.episode.baseline_not_adopted")
        self.assertEqual((event.resource_id, event.details_json["reason"]), (episode_id, "creative_differs"))
        [run] = self._runs_of(episode_id)
        self.assertEqual((run.outcome_kind, run.outcome_detail),
                         ("episode_rolled_over", "delivery_configuration_changed"), "contract §4.3")
        with platform_session() as session:
            self.assertNotEqual(session.get(ScaleEnrollmentRecord, enrollment_id).current_episode_id, episode_id)

    def test_an_active_target_is_frozen_into_the_run(self):
        day = _local_today() - timedelta(days=3)
        _connection, _set, _ads, _enrollment_id, episode_id = self._sandbox(started=day)
        target = ScaleTargetVersionRecord(
            id=new_id("stgt"), workspace_id=self.workspace_id, product_version_id="pv_acme",
            offer_version_id="ov_acme", market_code="US", currency="USD", target_cpa=Decimal("25"),
            lifecycle_state="active", creator_id=OWNER_ID)
        with platform_session() as session:
            session.add(target)

        self._steps(day)

        [run] = self._runs_of(episode_id)
        self.assertEqual((run.target_version_id, run.normalized_target_key), (target.id, target.id))

    def test_the_manifest_the_run_and_the_job_commit_together(self):
        day = _local_today() - timedelta(days=3)
        _connection, _set, _ads, _enrollment_id, episode_id = self._sandbox(started=day)

        with mock.patch.object(live_trigger, "enqueue_scale_evaluation", side_effect=RuntimeError("queue down")):
            self._steps(day)

        [collect] = self._rows(JobRecord, type=collector.COLLECT_JOB_TYPE)
        self.assertEqual((collect.state, collect.error_code), ("retry_wait", "scale_collect_sources_failed"))
        self.assertEqual((self._manifests(episode_id), self._runs_of(episode_id),
                          self._rows(JobRecord, type=EVALUATE)), ([], [], []), "nothing half-committed")

        self._release(collect.id)
        self._steps(day)

        [manifest] = self._manifests(episode_id)
        [run] = self._runs_of(episode_id)
        self.assertEqual((manifest.state, run.source_manifest_id, run.outcome_kind),
                         ("complete", manifest.id, "evaluated"))

    def test_replaying_every_layers_job_adds_no_run_snapshot_evaluation_or_job(self):
        day = _local_today() - timedelta(days=3)
        _connection, _set, _ads, _enrollment_id, episode_id = self._sandbox(started=day)
        self._steps(day)
        [run] = self._runs_of(episode_id)
        before = self._counts()
        recorded = ("scale.live_evaluation.requested", "scale.source_manifest.recorded", "scale_evaluation_evaluated",
                    "scale.episode.baseline_verified")
        events = {event_type: len(self._events(event_type)) for event_type in recorded}

        for job_type in (observation_sync.TICK_JOB_TYPE, observation_sync.SYNC_JOB_TYPE,
                         collector.COLLECT_JOB_TYPE, EVALUATE):
            with self.subTest(job_type=job_type):
                [job] = self._rows(JobRecord, type=job_type)
                self._requeue(job.id)
                self._steps(day)
                self.assertEqual(self._rows(JobRecord, id=job.id)[0].state, "succeeded")
                self.assertEqual(self._counts(), before)
        self.assertEqual(self._run_for(episode_id, day).evaluation_id, run.evaluation_id)
        self.assertEqual({event_type: len(self._events(event_type)) for event_type in recorded}, events)

        # A collection replayed in-process finds its manifest and triggers nothing.
        with mock.patch.object(live_trigger, "request_scale_evaluation") as requested:
            replay = collector.run_source_collection(self._worker(SANDBOX), connection_id=_connection,
                                                     target_date=day)
        requested.assert_not_called()
        self.assertEqual([(m.replayed, m.evaluation_run_id) for m in replay.manifests], [(True, None)])


class NoLiveRun(LiveEvaluationTestCase):
    def test_a_blocked_day_gets_no_run_and_the_next_day_evaluates(self):
        first = _local_today() - timedelta(days=4)
        connection_id, established, _ads, _enrollment_id, episode_id = self._sandbox(started=first)

        self._steps(first, exclude_types=(collector.COLLECT_JOB_TYPE,))
        run = self._runs(established.generation_ids["delivery_configuration"], first)["configuration"]
        with platform_session() as session:  # the stored page no longer matches its checksum
            page = session.scalar(select(MetaEvidencePageRecord).where(MetaEvidencePageRecord.sync_run_id == run.id))
            page.payload_compressed = gzip.compress(b'{"data":[]}', mtime=0)
        self._steps(first)

        self.assertEqual(self._manifest_for(episode_id, first).state, "blocked")
        self.assertEqual((self._runs_of(episode_id), self._rows(JobRecord, type=EVALUATE)), ([], []))
        self.assertEqual(self._events("scale.live_evaluation.skipped"), [], "blocked: nothing to skip")

        second = first + timedelta(days=1)
        self._steps(second)

        self.assertEqual(self._manifest_for(episode_id, second).state, "complete")
        [run] = self._runs_of(episode_id)
        self.assertEqual((run.target_local_day, run.outcome_kind), (second, "evaluated"))
        self.assertEqual(self._readiness(episode_id).quality_state, "pass")

    def test_a_paused_enrollment_gets_no_manifest_and_no_run(self):
        day = _local_today() - timedelta(days=3)
        self._sandbox(started=day, lifecycle="paused")

        self._steps(day)

        self.assertEqual(self._rows(ScaleEvidenceSourceManifestRecord), [])
        self.assertEqual((self._rows(ScaleEvaluationRunRecord), self._rows(JobRecord, type=EVALUATE)), ([], []))

    def test_without_an_eligible_policy_the_manifest_is_collected_and_no_run_is_requested(self):
        self._unpublish(self.policy_id)
        self._withdraw_platform_policy()
        later = utcnow() + timedelta(minutes=5)
        for overrides in ({"mode_permissions": {}}, {"mode_permissions": {MOCK: True}},
                          {"mode_permissions": {SANDBOX: "yes"}},
                          {"algorithm_version": "beta-v0", "mode_permissions": {SANDBOX: True}},
                          {"algorithm_version": "beta-v1", "mode_permissions": {SANDBOX: True}},  # replays only
                          {"is_published": False, "published_at": None, "mode_permissions": {SANDBOX: True}}):
            self._publish_policy(**{"published_at": later, **overrides})
        day = _local_today() - timedelta(days=3)
        _connection, _set, _ads, _enrollment_id, episode_id = self._sandbox(started=day)

        self._steps(day)

        self.assertEqual(self._manifest_for(episode_id, day).state, "complete")
        self.assertEqual(self._readiness(episode_id).maturity, "warming")
        self.assertEqual(self._runs_of(episode_id), [])
        [skipped] = self._events("scale.live_evaluation.skipped")
        self.assertEqual(skipped.details_json["reason"], "no_eligible_policy")

        # Q8: the latest published Policy that permits the mode and runs the
        # deployed algorithm -- an older eligible one beats the newer others.
        older = self._publish_policy(published_at=utcnow() - timedelta(days=1))
        with platform_session() as session:
            self.assertEqual(live_trigger.eligible_live_policy(session, SANDBOX).id, older)
            self.assertIsNone(live_trigger.eligible_live_policy(session, "internal_validation"))

    def test_a_day_that_has_not_ended_when_the_batch_finished_gets_no_run(self):
        day = _local_today() - timedelta(days=3)
        _connection, _set, _ads, _enrollment_id, episode_id = self._sandbox(started=day)
        evening = datetime.combine(day, time(23), tzinfo=ZONE)

        with mock.patch.object(sync_pipeline, "utcnow", return_value=evening):  # a skewed clock
            self._steps(day)

        self.assertEqual(self._manifest_for(episode_id, day).state, "complete")
        self.assertEqual(self._runs_of(episode_id), [])
        [skipped] = self._events("scale.live_evaluation.skipped")
        self.assertEqual(skipped.details_json["reason"], "day_not_ended")

    def test_an_earlier_day_collected_after_a_later_one_gets_no_live_run(self):
        earlier = _local_today() - timedelta(days=4)
        later = earlier + timedelta(days=1)
        connection_id, established, ad_ids = self._establish(ads=1)
        self._steps(earlier)  # no Enrollment yet: read, not collected
        _enrollment_id, episode_id = self._activated(connection_id, established, ad_ids[0], earlier)
        self._steps(later)

        self._collect(connection_id, earlier)  # e.g. a collection retried after the next day's

        self.assertEqual(self._manifest_for(episode_id, earlier).state, "complete")
        self.assertEqual([run.target_local_day for run in self._runs_of(episode_id)], [later],
                         "only the latest slot is live (#101 Q9); older days are backfill (S7)")
        [skipped] = self._events("scale.live_evaluation.skipped")
        self.assertEqual(skipped.details_json["reason"], "superseded_by_later_day")


class Recovery(LiveEvaluationTestCase):
    def test_adjacent_days_overlap_and_the_later_run_waits_for_the_earlier(self):
        first = _local_today() - timedelta(days=4)
        second = first + timedelta(days=1)
        _connection, _set, _ads, _enrollment_id, episode_id = self._sandbox(started=first)

        with mock.patch("app.scale.service.evaluate_scale", side_effect=_WorkerKilled()), \
                self.assertRaises(_WorkerKilled):
            self._steps(first)  # the Worker dies in Phase B: the run holds the Episode
        held = self._run_for(episode_id, first)
        held_job = self._job_of(held.id)
        self.assertEqual((held.run_state, held_job.state), ("snapshot_sealed", "running"))
        with platform_session() as session:  # a slow Worker still holds the lease
            session.execute(update(JobRecord).where(JobRecord.id == held_job.id)
                            .values(lease_expires_at=utcnow() + timedelta(hours=1)))

        self._steps(second)

        waiting = self._run_for(episode_id, second)
        waiting_job = self._job_of(waiting.id)
        self.assertEqual((waiting.run_state, waiting.outcome_kind, waiting.snapshot_id), ("requested", None, None),
                         "waits, not failed_integrity")
        self.assertEqual((waiting_job.state, waiting_job.error_code, waiting_job.attempt, waiting_job.max_attempts),
                         ("retry_wait", "episode_phase_conflict", 1, EVALUATION_JOB_MAX_ATTEMPTS))
        with platform_session() as session:
            self.assertEqual(session.get(ScaleEpisodeRecord, episode_id).phase_run_id, held.id)
        self.assertEqual(len(self._rows(ScaleEvidenceSnapshotRecord, episode_id=episode_id)), 1,
                         "nothing sealed for the waiting run")

        self._release(held_job.id, expire_leases=True)  # the lease expires; the Job goes back to the queue
        self._release(waiting_job.id)
        self._steps(second)

        held, waiting = self._run_for(episode_id, first), self._run_for(episode_id, second)
        self.assertEqual((held.outcome_kind, waiting.outcome_kind), ("evaluated", "evaluated"))
        with platform_session() as session:
            snapshot = session.get(ScaleEvidenceSnapshotRecord, waiting.snapshot_id)
            self.assertEqual(snapshot.prior_evaluation_id, held.evaluation_id,
                             "sealed after the earlier run, against its Evaluation")
            self.assertEqual(session.get(ScaleEpisodeRecord, episode_id).accepted_evaluation_id,
                             waiting.evaluation_id)
        self.assertEqual(self._rows(ScaleEvaluationRunRecord, outcome_kind="failed_integrity"), [])

    def test_a_stuck_run_is_redriven_twice_then_alerted_and_the_episode_gets_no_new_run(self):
        first = _local_today() - timedelta(days=6)
        _connection, _set, _ads, _enrollment_id, episode_id = self._sandbox(started=first)
        days = [first + timedelta(days=offset) for offset in range(4)]
        alerts = []

        with mock.patch("app.scale.service.evaluate_scale", side_effect=RuntimeError("evaluator crashed")), \
                mock.patch.object(live_trigger, "emit_alert", side_effect=lambda *a, **k: alerts.append((a, k))):
            for day in days:
                self._steps(day)

        held = self._run_for(episode_id, first)
        self.assertEqual((held.run_state, held.outcome_kind), ("snapshot_sealed", None))
        self.assertEqual(self._job_of(held.id).state, "failed")
        for redrive in (1, 2):
            job = self._job_of(held.id, f":redrive-{redrive}")
            self.assertEqual((job.state, job.payload_json["evaluation_run_id"]), ("failed", held.id))
        self.assertEqual([e.details_json["redrive"] for e in self._events("scale.evaluation_run.redriven")], [1, 2])
        [stuck] = self._events("scale.evaluation_run.stuck")
        self.assertEqual((stuck.resource_id, stuck.details_json["redrives"]), (held.id, 2))
        [(args, kwargs)] = alerts
        self.assertEqual((args[0], args[1], kwargs["workspace_id"], kwargs["details"]["evaluation_run_id"]),
                         (Severity.CRITICAL, AlertCategory.SCALE_EVALUATION, self.workspace_id, held.id))

        self.assertEqual(sorted(run.target_local_day for run in self._runs_of(episode_id)), days[:3],
                         "no run for the last day")
        self.assertEqual(self._manifest_for(episode_id, days[3]).state, "complete", "still collected")
        self.assertEqual(self._events("scale.live_evaluation.skipped")[0].details_json["reason"], "phase_run_stuck")


class Isolation(LiveEvaluationTestCase):
    def test_another_workspace_or_mode_neither_triggers_nor_enqueues_the_run(self):
        day = _local_today() - timedelta(days=3)
        connection_id, _set, _ads, _enrollment_id, episode_id = self._sandbox(started=day)
        self._steps(day)
        [run] = self._runs_of(episode_id)
        before = self._counts()

        other = self._provision("ws_live_eval_b")
        crossed = collector.run_source_collection(self._worker(SANDBOX, other), connection_id=connection_id,
                                                  target_date=day)
        self.assertEqual((crossed.manifests, crossed.skipped), ((), ()))
        self.assertEqual((self._rows(ScaleEvaluationRunRecord, other), self._rows(JobRecord, other, type=EVALUATE)),
                         ([], []))
        for name, context in (("Workspace B", self._worker(SANDBOX, other)),
                              ("controlled_mock", self._worker(MOCK))):
            with self.subTest(name), self.assertRaises(ScaleOrchestrationError) as raised, \
                    tenant_session(context) as session:
                enqueue_scale_evaluation(session, context, run.id)
            self.assertEqual(raised.exception.code, "run_not_found")
        self.assertEqual(self._counts(), before)
