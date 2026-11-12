"""Re-evaluation after a Target change, and backfill runs, on SQLite (#113, #101 S7).

The fixture is #110's (``tests/test_scale_live_evaluation.py``): a
``customer_sandbox`` connection, sandbox ads and an active Enrollment whose
Episode the Worker collects and evaluates day by day. The Policy in force
is the migrated Beta default v2, whose freshness lag is the 36 hours the
triage names. Targets are published and withdrawn through the domain
commands under the API role; only the Worker does the rest.

* a publish or withdraw enqueues one ``scale_target_change`` Job in its own
  transaction; with the latest complete manifest at most 36 hours older
  than the event, the Worker requests a ``target_change`` run on it that
  freezes the context's Target now and wins the projection over the live
  run; older evidence gets no run and waits for the next scheduled
  evaluation;
* Advice formed under another Target is superseded citing the event, with
  or without a re-evaluation; old Evaluations stay as they were;
* only Episodes in the Target's exact context, in its Workspace, are
  touched; a paused Enrollment is superseded but not re-evaluated;
* a complete manifest older than the Episode's latest gets a ``backfill``
  run that evaluates without moving the projection, the phase or Advice,
  notifies no one, and is never a later snapshot's predecessor;
* replaying the command, the Job, the collection or the evaluation adds
  nothing; Jobs that run out of order still leave the newest Target;
* only an Owner may change a Target, and a refused command enqueues nothing.

Fictional brand only: Acme Charge.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest import mock

from sqlalchemy import update

from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import JobRecord, new_id, utcnow
from app.meta import sync_pipeline
from app.platform.runtime_role import API_ROLE, WORKER_ROLE, entered_as
from app.platform.tenancy import TenantContext
from app.scale import service, target_change
from app.scale.advice import process_advice_candidates
from app.scale.models import (
    NO_TARGET_MARKER,
    ScaleAdviceNotificationDecisionRecord,
    ScaleAdviceOccurrenceRecord,
    ScaleAdviceThreadEventRecord,
    ScaleAdviceThreadRecord,
    ScaleEnrollmentRecord,
    ScaleEpisodeProjectionRecord,
    ScaleEpisodeRecord,
    ScaleEvaluationRecord,
    ScaleEvaluationRunRecord,
    ScaleEvidenceSnapshotRecord,
    ScaleTargetLifecycleEventRecord,
    ScaleTargetVersionRecord,
)
from app.scale.platform_policy import BETA_DEFAULT_POLICY_V2
from app.scale.targets import (
    TargetLifecycleError,
    create_publication_preview,
    create_withdrawal_preview,
    publish_target,
    withdraw_target,
)
from tests.test_scale_advice import _candidate, _candidate_row
from tests.test_scale_live_evaluation import EVALUATE, LiveEvaluationTestCase
from tests.test_scale_source_collection import OWNER_ID, SANDBOX, _local_today

TARGET_CHANGE = target_change.TARGET_CHANGE_JOB_TYPE
MEMBER_ID = "usr_acme_member"
#: The Beta default's freshness lag: the window a Target change re-evaluates in.
LAG = timedelta(hours=BETA_DEFAULT_POLICY_V2.freshness_floor_maximum_lag_hours)


class TargetChangeTestCase(LiveEvaluationTestCase):
    def setUp(self) -> None:
        super().setUp()
        # The Policy production runs: the migrated Beta default v2 (36 hours).
        self._unpublish(self.policy_id)
        self.assertEqual(LAG, timedelta(hours=36))

    # -- Target commands (the API's role) ------------------------------------

    def _context(self, role: str = "owner", workspace_id: str | None = None) -> TenantContext:
        workspace_id = workspace_id or self.workspace_id
        user = OWNER_ID if role == "owner" else MEMBER_ID
        return TenantContext(workspace_id=workspace_id, role=role, initiator_id=user, executor=user,
                             authorization_epoch=self._epoch(workspace_id), meta_mode=SANDBOX)

    def _publish(self, *, cpa: str = "25", market_code: str = "US", currency: str = "USD",
                 workspace_id: str | None = None, key: str | None = None) -> str:
        context = self._context(workspace_id=workspace_id)
        key = key or new_id("idem")
        with entered_as(API_ROLE), tenant_session(context) as session:
            preview = create_publication_preview(
                session, context, product_version_id="pv_acme", offer_version_id="ov_acme",
                market_code=market_code, currency=currency, target_cpa=Decimal(cpa),
                breakeven_roas=None, minimum_economic_spend=None, idempotency_key=f"{key}:preview")
            target = publish_target(session, context, preview_id=preview.id, idempotency_key=key)
            return target.id

    def _withdraw(self, target_id: str) -> None:
        context = self._context()
        key = new_id("idem")
        with entered_as(API_ROLE), tenant_session(context) as session:
            preview = create_withdrawal_preview(session, context, target_id=target_id,
                                                reason="Acme Charge repriced the offer",
                                                idempotency_key=f"{key}:preview")
            withdraw_target(session, context, preview_id=preview.id, idempotency_key=key)

    def _event(self, target_id: str, event_type: str, workspace_id: str | None = None):
        [event] = self._rows(ScaleTargetLifecycleEventRecord, workspace_id, target_version_id=target_id,
                             event_type=event_type)
        return event

    def _target_jobs(self, workspace_id: str | None = None) -> list[JobRecord]:
        return self._rows(JobRecord, workspace_id, type=TARGET_CHANGE)

    def _handle(self, workspace_id: str | None = None) -> int:
        """Only the Worker, with no observation slot due: the Target-change
        Jobs, and the evaluations they enqueue."""
        self._make_due(_local_today() + timedelta(days=30), workspace_id)
        return self._drain(workspace_id)

    # -- Episodes and Advice -------------------------------------------------

    @contextmanager
    def _manifests_finishing_at(self, moment: datetime):
        """The batch -- and so its manifest -- finishes at ``moment``."""
        with mock.patch.object(sync_pipeline, "utcnow", return_value=moment):
            yield

    def _evaluated_day(self, *, finished_at: datetime | None = None, **kwargs):
        """An Episode whose day D was collected and evaluated live."""
        day = _local_today() - timedelta(days=3)
        connection_id, _set, ad_ids, enrollment_id, episode_id = self._sandbox(started=day, **kwargs)
        if finished_at is None:
            self._steps(day)
        else:
            with self._manifests_finishing_at(finished_at):
                self._steps(day)
        [run] = self._runs_of(episode_id)
        self.assertEqual((run.trigger_kind, run.outcome_kind), ("live", "evaluated"))
        return connection_id, enrollment_id, episode_id, day, run

    def _open_thread(self, episode_id: str, evaluation_id: str, day: date) -> str:
        """An active Advice thread on ``evaluation_id``, opened the way Phase C's
        Advice pass opens one (the sandbox's warming days produce none)."""
        context = self._worker(SANDBOX)
        candidate = _candidate(reason_signature=f"rs-{new_id('acme')}")
        with entered_as(WORKER_ROLE), tenant_session(context) as session:
            session.add(_candidate_row(self.workspace_id, SANDBOX, evaluation_id, candidate))
            session.flush()
            [thread_id] = process_advice_candidates(
                session, context, episode_id, evaluation_id, [candidate],
                trigger_kind="live", evaluated_local_day=day, projection_eligible=True)
        return thread_id

    def _thread(self, thread_id: str) -> ScaleAdviceThreadRecord:
        [thread] = self._rows(ScaleAdviceThreadRecord, id=thread_id)
        return thread

    def _episode(self, episode_id: str) -> ScaleEpisodeRecord:
        [episode] = self._rows(ScaleEpisodeRecord, id=episode_id)
        return episode

    def _projection(self, episode_id: str) -> ScaleEpisodeProjectionRecord:
        [projection] = self._rows(ScaleEpisodeProjectionRecord, episode_id=episode_id)
        return projection

    def _target_change_runs(self, episode_id: str) -> list[ScaleEvaluationRunRecord]:
        return [run for run in self._runs_of(episode_id) if run.trigger_kind == "target_change"]

    def _advice_state(self) -> dict[str, list]:
        """Every Advice row, as plain values."""
        def values(model, *columns):
            return sorted(tuple(getattr(row, column) for column in columns) for row in self._rows(model))

        return {
            "threads": values(ScaleAdviceThreadRecord, "id", "lifecycle", "revision", "latest_occurrence_id"),
            "occurrences": values(ScaleAdviceOccurrenceRecord, "id"),
            "events": values(ScaleAdviceThreadEventRecord, "id"),
            "decisions": values(ScaleAdviceNotificationDecisionRecord, "id"),
            "expiry_jobs": sorted(job.id for job in self._rows(JobRecord, type="scale_advice_expire")),
        }

    def _episode_state(self, episode_id: str) -> tuple:
        episode = self._episode(episode_id)
        projection = self._projection(episode_id)
        return (episode.phase, episode.phase_run_id, episode.revision, episode.projection_revision,
                episode.projection_as_of_local_day, episode.projection_source_revision,
                episode.projection_semantic_event_sequence, episode.accepted_evaluation_id,
                episode.latest_source_manifest_id, projection.evaluation_id, projection.revision,
                projection.cursor_as_of_local_day, projection.streak_counters)

    def _counts(self) -> dict[str, int]:
        return super()._counts() | {
            model.__tablename__: len(self._rows(model)) for model in (
                ScaleAdviceThreadRecord, ScaleAdviceThreadEventRecord, ScaleAdviceOccurrenceRecord,
                ScaleTargetLifecycleEventRecord)}


class TargetChangeReevaluation(TargetChangeTestCase):
    def test_a_publish_within_36_hours_re_evaluates_the_latest_manifest_and_supersedes_the_old_advice(self):
        _connection, _enrollment_id, episode_id, day, live = self._evaluated_day()
        thread_id = self._open_thread(episode_id, live.evaluation_id, day)
        manifest = self._manifest_for(episode_id, day)

        target_id = self._publish()

        publish = self._event(target_id, "publish")
        [job] = self._target_jobs()
        self.assertEqual((job.state, job.payload_json, job.idempotency_key, job.resource_id),
                         ("queued", {"target_event_id": publish.id}, f"scale-target-change:{publish.id}",
                          target_id), "enqueued with the publication")
        self.assertEqual(self._target_change_runs(episode_id), [], "the API requests no run itself")

        self.assertEqual(self._handle(), 2, "the Target change, then its evaluation")

        [run] = self._target_change_runs(episode_id)
        self.assertEqual((run.target_local_day, run.source_manifest_id, run.source_revision, run.policy_version_id,
                          run.target_version_id, run.normalized_target_key, run.semantic_event_sequence,
                          run.triggering_semantic_event_id, run.original_initiator_id),
                         (day, manifest.id, manifest.source_revision, BETA_DEFAULT_POLICY_V2.id, target_id,
                          target_id, 1, publish.id, OWNER_ID))
        self.assertEqual(run.freshness_reference_at.replace(tzinfo=None), publish.created_at.replace(tzinfo=None),
                         "the persisted event time, never the clock")
        self.assertEqual((run.run_state, run.outcome_kind), ("evaluated", "evaluated"))
        self.assertEqual(self._rows(JobRecord, id=job.id)[0].state, "succeeded")
        self.assertEqual(self._job_of(run.id).state, "succeeded")

        # It won the projection over the live run of the same manifest (§18).
        episode = self._episode(episode_id)
        self.assertEqual((episode.accepted_evaluation_id, episode.projection_as_of_local_day,
                          episode.projection_semantic_event_sequence, episode.phase, episode.phase_run_id),
                         (run.evaluation_id, day, 1, "collecting", None))
        self.assertEqual(self._projection(episode_id).evaluation_id, run.evaluation_id)
        [evaluation] = self._rows(ScaleEvaluationRecord, id=run.evaluation_id)
        self.assertEqual(evaluation.normalized_target_key, target_id)
        [old] = self._rows(ScaleEvaluationRecord, id=live.evaluation_id)
        self.assertEqual((old.normalized_target_key, self._run_for_kind(episode_id, "live").evaluation_id),
                         (NO_TARGET_MARKER, live.evaluation_id), "the old result is not rewritten")

        # The Advice formed without a Target no longer applies.
        thread = self._thread(thread_id)
        self.assertEqual((thread.lifecycle, thread.lifecycle_reason), ("superseded", "target_changed"))
        [superseded] = self._rows(ScaleAdviceThreadEventRecord, thread_id=thread_id, event_type="superseded")
        self.assertEqual((superseded.semantic_event_id, superseded.successor_thread_id, superseded.reason),
                         (publish.id, None, "target_changed"))
        [requested] = self._events("scale.target_change_evaluation.requested")
        self.assertEqual((requested.resource_id, requested.details_json["target_event_id"],
                          requested.details_json["superseded_thread_ids"]), (run.id, publish.id, [thread_id]))
        [audit] = [event for event in self._events("scale_evaluation_evaluated") if event.resource_id == run.id]
        self.assertEqual((audit.details_json["trigger_kind"], audit.details_json["projected"],
                          audit.details_json["triggering_semantic_event_id"]), ("target_change", True, publish.id))

    def test_evidence_35_hours_older_than_the_publish_is_still_re_evaluated(self):
        _connection, _enrollment_id, episode_id, _day, _live = self._evaluated_day(
            finished_at=utcnow() - LAG + timedelta(hours=1))

        self._publish()
        self._handle()

        [run] = self._target_change_runs(episode_id)
        self.assertEqual(run.outcome_kind, "evaluated")
        self.assertEqual(self._episode(episode_id).accepted_evaluation_id, run.evaluation_id)
        self.assertEqual(self._events("scale.target_change_evaluation.skipped"), [])

    def test_evidence_more_than_36_hours_older_is_not_re_evaluated_and_waits_for_the_next_evaluation(self):
        _connection, _enrollment_id, episode_id, day, live = self._evaluated_day(
            finished_at=utcnow() - LAG - timedelta(hours=1))
        thread_id = self._open_thread(episode_id, live.evaluation_id, day)
        before = self._episode_state(episode_id)

        target_id = self._publish()
        self.assertEqual(self._handle(), 1, "the Target change only")

        self.assertEqual(self._target_change_runs(episode_id), [])
        self.assertEqual(self._episode_state(episode_id), before, "the projection waits")
        [skipped] = self._events("scale.target_change_evaluation.skipped")
        self.assertEqual((skipped.resource_id, skipped.details_json["reason"], skipped.details_json["freshness_lag_hours"],
                          skipped.details_json["superseded_thread_ids"]), (episode_id, "evidence_stale", 36, [thread_id]))
        self.assertEqual(self._thread(thread_id).lifecycle, "superseded", "superseded whether or not re-evaluated")

        # The next scheduled evaluation freezes the new Target, like any live run.
        self._steps(day + timedelta(days=1))
        later = self._run_for(episode_id, day + timedelta(days=1))
        self.assertEqual((later.trigger_kind, later.target_version_id, later.outcome_kind),
                         ("live", target_id, "evaluated"))
        self.assertEqual(self._episode(episode_id).accepted_evaluation_id, later.evaluation_id)

    def test_a_withdrawal_supersedes_the_targets_advice_and_re_evaluates_without_a_target(self):
        target_id = self._publish()
        self._handle()  # nothing to affect yet
        _connection, _enrollment_id, episode_id, day, live = self._evaluated_day()
        self.assertEqual(live.target_version_id, target_id)
        thread_id = self._open_thread(episode_id, live.evaluation_id, day)

        self._withdraw(target_id)
        withdraw = self._event(target_id, "withdraw")
        self.assertEqual(len(self._target_jobs()), 2)
        self._handle()

        thread = self._thread(thread_id)
        self.assertEqual((thread.lifecycle, thread.lifecycle_reason), ("superseded", "target_changed"))
        [superseded] = self._rows(ScaleAdviceThreadEventRecord, thread_id=thread_id, event_type="superseded")
        self.assertEqual((superseded.semantic_event_id, superseded.successor_thread_id), (withdraw.id, None))
        [run] = self._target_change_runs(episode_id)
        self.assertEqual((run.target_version_id, run.normalized_target_key, run.triggering_semantic_event_id,
                          run.outcome_kind), (None, NO_TARGET_MARKER, withdraw.id, "evaluated"))
        self.assertEqual(self._episode(episode_id).accepted_evaluation_id, run.evaluation_id)
        [old] = self._rows(ScaleEvaluationRecord, id=live.evaluation_id)
        self.assertEqual(old.normalized_target_key, target_id, "the old result is not rewritten")

    def test_advice_a_live_run_opens_under_the_old_target_is_superseded_by_the_target_change_run(self):
        """Spec §3.8: the event supersedes first; a live run that froze the
        old Target and evaluates after that may still open Advice under it.
        The target_change run, one semantic event later, supersedes it too."""
        _connection, _enrollment_id, episode_id, day, live = self._evaluated_day()
        target_id = self._publish()
        self._handle()
        [run] = self._target_change_runs(episode_id)
        late = self._open_thread(episode_id, live.evaluation_id, day)  # under no Target
        publish = self._event(target_id, "publish")

        context = self._worker(SANDBOX)
        with entered_as(WORKER_ROLE), tenant_session(context) as session:
            process_advice_candidates(session, context, episode_id, run.evaluation_id, [],
                                      trigger_kind="target_change", evaluated_local_day=day,
                                      projection_eligible=True, semantic_event_id=publish.id)

        self.assertEqual(self._thread(late).lifecycle, "superseded")

    def test_only_the_targets_exact_context_in_its_workspace_is_affected(self):
        day = _local_today() - timedelta(days=3)
        connection_id, established, ad_ids = self._establish(ads=2)
        _enrollment_id, episode_id = self._activated(connection_id, established, ad_ids[0], day)
        _euro_enrollment, euro_episode = self._activated(connection_id, established, ad_ids[1], day,
                                                         currency="EUR")
        self._steps(day)
        live = self._run_for(episode_id, day)
        thread_id = self._open_thread(episode_id, live.evaluation_id, day)
        other = self._provision("ws_target_change_b")
        runs_before = self._run_ids(episode_id, euro_episode)

        self._publish(workspace_id=other)  # the same context, in another Workspace
        self._handle(other)
        self._publish(market_code="CA")  # another market here
        self._handle()

        self.assertEqual(self._run_ids(episode_id, euro_episode), runs_before,
                         "nothing in another Workspace or market re-evaluates here")
        self.assertEqual(self._thread(thread_id).lifecycle, "active")
        self.assertEqual(self._events("scale.target_change_evaluation.skipped"), [])
        self.assertEqual(self._rows(ScaleEvaluationRunRecord, other), [])
        self.assertEqual([job.state for job in self._target_jobs(other)], ["succeeded"])

        self._publish(currency="EUR", cpa="30")  # here, the EUR Episode's context only
        self._handle()

        self.assertEqual(self._run_ids(episode_id, euro_episode), runs_before)
        self.assertEqual(self._thread(thread_id).lifecycle, "active", "another currency is another context")
        # The sandbox account reports USD, so the EUR Episode's manifest is
        # blocked: handled, with nothing complete to re-evaluate.
        self.assertEqual(self._manifest_for(euro_episode, day).state, "blocked")
        [skipped] = self._events("scale.target_change_evaluation.skipped")
        self.assertEqual((skipped.resource_id, skipped.details_json["reason"]),
                         (euro_episode, "no_complete_manifest"))

        # Workspace B's Job cannot name A's event: it reads only its own.
        [a_event, *_] = self._rows(ScaleTargetLifecycleEventRecord, event_type="publish")
        with self.assertRaises(target_change.TargetChangeError) as raised:
            target_change.run_target_change(self._worker(SANDBOX, other), a_event.id)
        self.assertEqual(raised.exception.code, "target_event_not_found")

    def test_a_paused_enrollment_is_superseded_but_not_re_evaluated(self):
        _connection, enrollment_id, episode_id, day, live = self._evaluated_day()
        thread_id = self._open_thread(episode_id, live.evaluation_id, day)
        with platform_session() as session:
            session.execute(update(ScaleEnrollmentRecord).where(ScaleEnrollmentRecord.id == enrollment_id)
                            .values(lifecycle="paused"))

        self._publish()
        self._handle()

        self.assertEqual(self._thread(thread_id).lifecycle, "superseded")
        self.assertEqual(self._target_change_runs(episode_id), [])
        [skipped] = self._events("scale.target_change_evaluation.skipped")
        self.assertEqual(skipped.details_json["reason"], "enrollment_not_active")

    def _run_ids(self, *episode_ids: str) -> dict[str, list[str]]:
        return {episode_id: [run.id for run in self._runs_of(episode_id)] for episode_id in episode_ids}

    def _run_for_kind(self, episode_id: str, trigger_kind: str) -> ScaleEvaluationRunRecord:
        [run] = [run for run in self._runs_of(episode_id) if run.trigger_kind == trigger_kind]
        return run


class Replays(TargetChangeTestCase):
    def test_replaying_the_command_the_job_and_the_evaluation_adds_nothing(self):
        _connection, _enrollment_id, episode_id, day, live = self._evaluated_day()
        thread_id = self._open_thread(episode_id, live.evaluation_id, day)
        target_id = self._publish(key="idem_acme_publish")
        self._handle()
        [run] = self._target_change_runs(episode_id)
        before, advice = self._counts(), self._advice_state()
        requested = len(self._events("scale.target_change_evaluation.requested"))

        self.assertEqual(self._publish(key="idem_acme_publish"), target_id, "the command replays")
        [job] = self._target_jobs()
        for replayed in (job.id, self._job_of(run.id).id):
            with self.subTest(job=replayed):
                self._requeue(replayed)
                self._handle()
                self.assertEqual(self._rows(JobRecord, id=replayed)[0].state, "succeeded")
                self.assertEqual(self._counts(), before)
                self.assertEqual(self._advice_state(), advice)
        self.assertEqual(len(self._events("scale.target_change_evaluation.requested")), requested)
        self.assertEqual(self._thread(thread_id).lifecycle, "superseded")

        # In-process: the run is found by its Episode and event.
        publish = self._event(target_id, "publish")
        [outcome] = target_change.run_target_change(self._worker(SANDBOX), publish.id)
        self.assertEqual((outcome.run_id, outcome.replayed, outcome.superseded_thread_ids), (run.id, True, ()))
        self.assertEqual(self._counts(), before)

    def test_jobs_that_run_out_of_order_still_leave_the_newest_target_on_the_projection(self):
        _connection, _enrollment_id, episode_id, _day, _live = self._evaluated_day()
        target_id = self._publish()
        self._withdraw(target_id)
        publish_job, withdraw_job = sorted(
            self._target_jobs(), key=lambda job: job.payload_json["target_event_id"]
            != self._event(target_id, "publish").id)
        with platform_session() as session:  # the publication's Job waits
            session.execute(update(JobRecord).where(JobRecord.id == publish_job.id)
                            .values(next_attempt_at=utcnow() + timedelta(hours=1)))
        self._handle()
        self._release(publish_job.id)
        self._handle()

        runs = self._target_change_runs(episode_id)
        self.assertEqual(sorted((run.semantic_event_sequence, run.normalized_target_key) for run in runs),
                         [(1, NO_TARGET_MARKER), (2, NO_TARGET_MARKER)],
                         "each froze the Target in force when it ran: none")
        episode = self._episode(episode_id)
        [last] = [run for run in runs if run.semantic_event_sequence == 2]
        self.assertEqual((episode.accepted_evaluation_id, episode.projection_semantic_event_sequence),
                         (last.evaluation_id, 2))
        self.assertEqual(last.triggering_semantic_event_id, self._event(target_id, "publish").id)
        self.assertEqual(self._rows(JobRecord, id=withdraw_job.id)[0].state, "succeeded")


class Backfill(TargetChangeTestCase):
    def _backfilled(self):
        """Day E read before the Enrollment existed, day L evaluated live, then
        E's collection retried after L's: a complete manifest older than the
        Episode's latest."""
        earlier = _local_today() - timedelta(days=4)
        later = earlier + timedelta(days=1)
        connection_id, established, ad_ids = self._establish(ads=1)
        self._steps(earlier)  # no Enrollment yet: read, not collected
        _enrollment_id, episode_id = self._activated(connection_id, established, ad_ids[0], earlier)
        self._steps(later)
        live = self._run_for(episode_id, later)
        thread_id = self._open_thread(episode_id, live.evaluation_id, later)
        return connection_id, episode_id, earlier, later, live, thread_id

    def test_a_backfill_run_evaluates_without_moving_the_projection_or_advice_and_notifies_no_one(self):
        connection_id, episode_id, earlier, later, live, thread_id = self._backfilled()
        before, advice = self._episode_state(episode_id), self._advice_state()

        with mock.patch.object(service, "process_advice_candidates",
                               wraps=service.process_advice_candidates) as advice_pass:
            collection = self._collect(connection_id, earlier)

        self.assertEqual(collection.state, "succeeded")
        manifest = self._manifest_for(episode_id, earlier)
        backfill = self._run_for(episode_id, earlier)
        self.assertEqual((backfill.trigger_kind, backfill.source_manifest_id, backfill.semantic_event_sequence,
                          backfill.run_state, backfill.outcome_kind),
                         ("backfill", manifest.id, 0, "evaluated", "evaluated"))
        self.assertEqual(backfill.freshness_reference_at.replace(tzinfo=None),
                         manifest.terminal_at.replace(tzinfo=None))
        self.assertEqual(self._job_of(backfill.id).state, "succeeded")
        [evaluation] = self._rows(ScaleEvaluationRecord, id=backfill.evaluation_id)
        self.assertIsNotNone(evaluation.result_hash, "persisted, immutable")

        self.assertEqual(self._episode_state(episode_id), before,
                         "projection, cursor, phase and streaks unchanged (§18)")
        self.assertEqual(self._episode(episode_id).accepted_evaluation_id, live.evaluation_id)
        advice_pass.assert_not_called()
        self.assertEqual(self._advice_state(), advice, "no thread, occurrence, event, decision or expiry Job")
        self.assertEqual(self._thread(thread_id).lifecycle, "active")
        [requested] = self._events("scale.backfill_evaluation.requested")
        self.assertEqual((requested.resource_id, requested.details_json["projection_as_of_local_day"]),
                         (backfill.id, later.isoformat()))
        [audit] = [event for event in self._events("scale_evaluation_evaluated") if event.resource_id == backfill.id]
        self.assertEqual((audit.details_json["trigger_kind"], audit.details_json["projected"]), ("backfill", False))

        # Replaying the collection and the evaluation adds nothing.
        counts = self._counts()
        for job_id in (collection.id, self._job_of(backfill.id).id):
            with self.subTest(job=job_id):
                self._requeue(job_id)
                self._handle()
                self.assertEqual(self._counts(), counts)
        self.assertEqual(self._episode_state(episode_id), before)

    def test_a_backfill_never_projects_even_before_the_live_run_does(self):
        earlier = _local_today() - timedelta(days=4)
        later = earlier + timedelta(days=1)
        connection_id, established, ad_ids = self._establish(ads=1)
        self._steps(earlier)  # no Enrollment yet: read, not collected
        _enrollment_id, episode_id = self._activated(connection_id, established, ad_ids[0], earlier)
        self._steps(later, exclude_types=(EVALUATE,))  # L's live run waits in the queue
        live = self._run_for(episode_id, later)
        with platform_session() as session:
            session.execute(update(JobRecord).where(JobRecord.id == self._job_of(live.id).id)
                            .values(next_attempt_at=utcnow() + timedelta(hours=1)))

        self._collect(connection_id, earlier)

        backfill = self._run_for(episode_id, earlier)
        self.assertEqual(backfill.outcome_kind, "evaluated")
        episode = self._episode(episode_id)
        self.assertEqual((episode.accepted_evaluation_id, episode.projection_revision), (None, 0),
                         "nothing had projected, and the backfill does not either")
        self.assertEqual(self._rows(ScaleEpisodeProjectionRecord, episode_id=episode_id), [])

        self._release(self._job_of(live.id).id)
        self._handle()
        self.assertEqual(self._episode(episode_id).accepted_evaluation_id,
                         self._run_for(episode_id, later).evaluation_id)

    def test_a_backfill_takes_no_phase_and_does_not_wait_for_a_live_run(self):
        connection_id, episode_id, earlier, _later, _live, _thread = self._backfilled()
        with platform_session() as session:  # a live run holds the Episode
            session.execute(update(ScaleEpisodeRecord).where(ScaleEpisodeRecord.id == episode_id)
                            .values(phase="evaluating", phase_run_id="sevr_acme_holder"))

        self._collect(connection_id, earlier)

        backfill = self._run_for(episode_id, earlier)
        self.assertEqual(backfill.outcome_kind, "evaluated", "not deferred behind the live run")
        episode = self._episode(episode_id)
        self.assertEqual((episode.phase, episode.phase_run_id), ("evaluating", "sevr_acme_holder"))

    def test_a_backfill_evaluation_is_never_a_later_snapshots_predecessor(self):
        connection_id, episode_id, earlier, later, _live, _thread = self._backfilled()
        self._collect(connection_id, earlier)
        backfill = self._run_for(episode_id, earlier)

        self._publish()  # re-evaluates the latest day, L, whose predecessor would be E
        self._handle()

        [run] = self._target_change_runs(episode_id)
        self.assertEqual(run.target_local_day, later)
        [snapshot] = self._rows(ScaleEvidenceSnapshotRecord, id=run.snapshot_id)
        self.assertNotEqual(snapshot.prior_evaluation_id, backfill.evaluation_id)
        self.assertEqual((snapshot.prior_evaluation_id, snapshot.prior_streak_absence_reason),
                         (None, "no_earlier_distinct_day"))

    def test_a_backfill_whose_configuration_differs_is_cancelled_and_rolls_nothing_over(self):
        connection_id, episode_id, earlier, _later, _live, _thread = self._backfilled()
        with platform_session() as session:  # the Episode's baseline moved since E
            session.execute(update(ScaleEpisodeRecord).where(ScaleEpisodeRecord.id == episode_id)
                            .values(delivery_comparability_hash="d" * 64))

        self._collect(connection_id, earlier)

        backfill = self._run_for(episode_id, earlier)
        self.assertEqual((backfill.outcome_kind, backfill.outcome_detail),
                         ("cancelled", "backfill_configuration_differs"))
        episode = self._episode(episode_id)
        self.assertEqual((episode.phase, episode.closure_reason), ("collecting", None), "not rolled over")


class Permissions(TargetChangeTestCase):
    def test_a_member_cannot_change_a_target_and_enqueues_nothing(self):
        target_id = self._publish()
        jobs_before = len(self._target_jobs())
        member = self._context("member")

        for name, command in (
            ("publication preview", lambda session: create_publication_preview(
                session, member, product_version_id="pv_acme", offer_version_id="ov_acme", market_code="US",
                currency="USD", target_cpa=Decimal("20"), breakeven_roas=None, minimum_economic_spend=None,
                idempotency_key=new_id("idem"))),
            ("withdrawal preview", lambda session: create_withdrawal_preview(
                session, member, target_id=target_id, reason="not mine", idempotency_key=new_id("idem"))),
            ("withdrawal", lambda session: withdraw_target(
                session, member, preview_id="stwp_acme", idempotency_key=new_id("idem"))),
            ("publication", lambda session: publish_target(
                session, member, preview_id="stpp_acme", idempotency_key=new_id("idem"))),
        ):
            with self.subTest(name), self.assertRaises(TargetLifecycleError) as raised, \
                    entered_as(API_ROLE), tenant_session(member) as session:
                command(session)
            self.assertEqual(raised.exception.code, "owner_required")
        self.assertEqual(len(self._target_jobs()), jobs_before)
        [target] = self._rows(ScaleTargetVersionRecord, id=target_id)
        self.assertEqual(target.lifecycle_state, "active")


class TargetChangeJobs(TargetChangeTestCase):
    def test_a_job_without_an_event_fails_without_retrying(self):
        context = self._worker(SANDBOX)
        with tenant_session(context) as session:
            from app.platform.jobs import enqueue_in_session

            job_id = enqueue_in_session(session, context=context, job_type=TARGET_CHANGE, payload={},
                                        idempotency_key=f"scale-target-change:{new_id('bad')}").id
        self._handle()

        [job] = self._rows(JobRecord, id=job_id)
        self.assertEqual((job.state, job.error_code), ("failed", "missing_input"))

    def test_an_episode_that_fails_is_retried_and_the_others_are_handled(self):
        _connection, _enrollment_id, episode_id, _day, _live = self._evaluated_day()

        self._publish()
        with mock.patch.object(target_change, "request_scale_evaluation", side_effect=RuntimeError("db down")):
            self._handle()
        [job] = self._target_jobs()
        self.assertEqual((job.state, job.error_code), ("retry_wait", "scale_target_change_failed"))
        self.assertEqual(self._target_change_runs(episode_id), [], "nothing half-committed")

        self._handle_with_retries()
        [run] = self._target_change_runs(episode_id)
        self.assertEqual(run.outcome_kind, "evaluated")
        self.assertEqual(self._rows(JobRecord, id=job.id)[0].state, "succeeded")

    def _handle_with_retries(self) -> None:
        self._make_due(_local_today() + timedelta(days=30))
        self._drain(release_retries=True)

