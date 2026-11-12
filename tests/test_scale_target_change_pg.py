"""Target-change re-evaluation and backfill under the real roles on PostgreSQL (#113, #101 S7).

On a cluster released with ``make db-release`` the API runs as
``marketai_domain_api`` and the Worker as ``marketai_domain_worker``. The
fixture is #110's acceptance (``tests/test_scale_live_evaluation_pg.py``,
whose helpers this class reuses): a ``customer_sandbox`` Enrollment
activated through the API, then days run by the Worker alone under the
Beta default Policy the migrations published (36-hour freshness lag).

1. Day 1's collection waits while day 2 is collected and evaluated live;
   then day 1's complete manifest -- older than the Episode's latest -- gets
   a ``backfill`` run. The Worker seals and evaluates it without taking the
   Episode's phase, and the projection the API reads stays on day 2.
2. A Member cannot publish a Target, and the refusal enqueues nothing.
3. The Owner publishes a Target through the API: the API's transaction
   enqueues ``scale_target_change``; the Worker supersedes the Advice formed
   without a Target (citing the publish event, no successor thread) and
   requests a ``target_change`` run on day 2's manifest, which wins the
   projection. The snapshot does not freeze the backfill Evaluation as its
   predecessor.
4. The Owner withdraws it: the run re-evaluates without a Target, one
   semantic event later, and the Advice formed under the Target is
   superseded by the withdraw event.
5. Replaying the Target-change and evaluation Jobs adds nothing.
6. Workspace B publishes a Target for the same context: nothing of A is
   read or written.

Run with ``DOMAIN_OWNER_DATABASE_URL``, ``DOMAIN_API_DATABASE_URL``,
``DOMAIN_WORKER_DATABASE_URL``, ``MARKETAI_DATABASE_URL`` (the owner) and
``MARKETAI_TENANT_CONTEXT_SECRET``. Fictional brand only: Acme Charge.
"""
from __future__ import annotations

import os
import unittest
import uuid
from dataclasses import replace
from datetime import timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AuditEventRecord, JobRecord, utcnow
from app.platform.runtime_role import WORKER_ROLE, entered_as
from app.scale.advice import process_advice_candidates
from app.scale.collector import COLLECT_JOB_TYPE
from app.scale.models import (
    NO_TARGET_MARKER,
    ScaleAdviceThreadEventRecord,
    ScaleAdviceThreadRecord,
    ScaleEpisodeRecord,
    ScaleEvaluationRunRecord,
    ScaleEvidenceSnapshotRecord,
    ScaleTargetLifecycleEventRecord,
)
from app.scale.target_change import TARGET_CHANGE_JOB_TYPE
from tests.test_scale_advice import _candidate, _candidate_row
# The module, not the class: a TestCase imported here would run again here.
from tests import test_scale_live_evaluation_pg as live_pg
from tests.test_scale_live_evaluation_pg import SANDBOX, URLS, WORKER, ZONE, _fire_at


@unittest.skipUnless(all(URLS) and os.getenv("MARKETAI_TENANT_CONTEXT_SECRET"),
                     "requires isolated PostgreSQL runtime roles")
class TargetChangePostgresTests(live_pg.LiveEvaluationEndToEndPostgresTests):
    # The S4 acceptance runs in its own file; this class borrows its fixture.
    test_an_api_activated_sandbox_enrollment_is_evaluated_daily_by_the_worker_alone = None

    def _open_thread(self, workspace_id: str, episode_id: str, evaluation_id: str, day) -> str:
        """An active thread on ``evaluation_id``, written as Phase C's Advice pass
        writes it, as the Worker (the sandbox's warming days produce none)."""
        context = self._worker(workspace_id)
        candidate = _candidate(reason_signature=f"rs-{uuid.uuid4().hex[:12]}")
        with self._as(WORKER), entered_as(WORKER_ROLE), tenant_session(context) as session:
            session.add(_candidate_row(workspace_id, SANDBOX, evaluation_id, candidate))
            session.flush()
            [thread_id] = process_advice_candidates(
                session, context, episode_id, evaluation_id, [candidate],
                trigger_kind="live", evaluated_local_day=day, projection_eligible=True)
        return thread_id

    def _preview(self, episode: ScaleEpisodeRecord, tag: str, *, expect: int = 201) -> dict:
        return self._api("POST", "/target-publication-previews", expect=expect,
                         json={"product_version_id": episode.product_version_id,
                               "offer_version_id": episode.offer_version_id,
                               "market_code": episode.market_code, "currency": episode.currency,
                               "target_cpa": "25"},
                         headers={"Idempotency-Key": f"ik-target-preview-{tag}"})

    def _publish(self, episode: ScaleEpisodeRecord, tag: str) -> dict:
        preview = self._preview(episode, tag)
        return self._api("POST", "/targets", expect=201, json={"publication_preview_id": preview["id"]},
                         headers={"Idempotency-Key": f"ik-target-{tag}"})

    def _event(self, workspace_id: str, target_id: str, event_type: str) -> ScaleTargetLifecycleEventRecord:
        [event] = self._rows(ScaleTargetLifecycleEventRecord, workspace_id, target_version_id=target_id,
                             event_type=event_type)
        return event

    def _target_change_runs(self, workspace_id: str, episode_id: str) -> list[ScaleEvaluationRunRecord]:
        return sorted(self._rows(ScaleEvaluationRunRecord, workspace_id, episode_id=episode_id,
                                 trigger_kind="target_change"), key=lambda run: run.semantic_event_sequence)

    def _thread(self, workspace_id: str, thread_id: str) -> ScaleAdviceThreadRecord:
        [thread] = self._rows(ScaleAdviceThreadRecord, workspace_id, id=thread_id)
        return thread

    def _superseded_by(self, workspace_id: str, thread_id: str) -> tuple:
        [event] = self._rows(ScaleAdviceThreadEventRecord, workspace_id, thread_id=thread_id,
                             event_type="superseded")
        return event.semantic_event_id, event.successor_thread_id, event.reason

    def _episode_row(self, workspace_id: str, episode_id: str) -> ScaleEpisodeRecord:
        [episode] = self._rows(ScaleEpisodeRecord, workspace_id, id=episode_id)
        return episode

    def test_a_target_change_re_evaluates_and_a_backfill_never_projects_under_the_real_roles(self):
        owner = self._workspace("target")
        workspace_id = owner.workspace_id
        self._migrated_policy()
        activation_day = utcnow().astimezone(ZONE).date() - timedelta(days=4)
        made = self._activated_enrollment(owner, activation_day)
        enrollment_id = made["enrollment"]["id"]
        episode_id = made["enrollment"]["current_episode_id"]
        first, second = activation_day, activation_day + timedelta(days=1)

        # 1. Day 1's collection waits until day 2 was evaluated: a backfill.
        self.assertEqual(self._day(workspace_id, first, exclude_types=(COLLECT_JOB_TYPE,)), 2, "tick and batch")
        [held] = self._rows(JobRecord, workspace_id, type=COLLECT_JOB_TYPE)
        with Session(self.owner) as session, session.begin():
            session.execute(update(JobRecord).where(JobRecord.id == held.id)
                            .values(next_attempt_at=utcnow() + timedelta(days=1)))
        self.assertEqual(self._day(workspace_id, second), 4, "tick, batch, collection, evaluation")
        live = self._run_for(workspace_id, episode_id, second)
        self.assertEqual((live.trigger_kind, live.outcome_kind), ("live", "evaluated"))
        projected = self._api("GET", f"/enrollments/{enrollment_id}/evidence")
        self.assertEqual(projected["evaluation_id"], live.evaluation_id)

        self._park(workspace_id)
        with Session(self.owner) as session, session.begin():
            session.execute(update(JobRecord).where(JobRecord.id == held.id).values(next_attempt_at=None))
        self.assertEqual(self._drain(workspace_id, now=_fire_at(second) + timedelta(minutes=1)), 2,
                         "day 1's collection, then its backfill evaluation")
        backfill = self._run_for(workspace_id, episode_id, first)
        self.assertEqual((backfill.trigger_kind, backfill.run_state, backfill.outcome_kind),
                         ("backfill", "evaluated", "evaluated"))
        episode = self._episode_row(workspace_id, episode_id)
        self.assertEqual((episode.accepted_evaluation_id, episode.projection_as_of_local_day, episode.phase,
                          episode.phase_run_id), (live.evaluation_id, second, "collecting", None))
        evidence = self._api("GET", f"/enrollments/{enrollment_id}/evidence")
        self.assertEqual((evidence["evaluation_id"], evidence["cursor_as_of_local_day"]),
                         (live.evaluation_id, second.isoformat()), "the API still reads day 2")

        thread_id = self._open_thread(workspace_id, episode_id, live.evaluation_id, second)

        # 2. A Member cannot change a Target; nothing is enqueued.
        self.current = replace(owner, role="member", initiator_id="usr_acme_member")
        refused = self._preview(episode, "member", expect=403)
        self.assertEqual(refused["code"], "owner_required")
        self.assertEqual(self._rows(JobRecord, workspace_id, type=TARGET_CHANGE_JOB_TYPE), [])
        self.current = owner

        # 3. The Owner publishes: the API enqueues, the Worker re-evaluates.
        target = self._publish(episode, "publish")
        publish = self._event(workspace_id, target["id"], "publish")
        [job] = self._rows(JobRecord, workspace_id, type=TARGET_CHANGE_JOB_TYPE)
        self.assertEqual((job.state, job.payload_json, job.idempotency_key),
                         ("queued", {"target_event_id": publish.id}, f"scale-target-change:{publish.id}"))
        self.assertEqual(self._drain(workspace_id), 2, "the Target change, then its evaluation")

        [run] = self._target_change_runs(workspace_id, episode_id)
        self.assertEqual((run.target_local_day, run.source_manifest_id, run.target_version_id,
                          run.semantic_event_sequence, run.triggering_semantic_event_id, run.outcome_kind),
                         (second, live.source_manifest_id, target["id"], 1, publish.id, "evaluated"))
        self.assertEqual(self._thread(workspace_id, thread_id).lifecycle, "superseded")
        self.assertEqual(self._superseded_by(workspace_id, thread_id), (publish.id, None, "target_changed"))
        evidence = self._api("GET", f"/enrollments/{enrollment_id}/evidence")
        self.assertEqual(evidence["evaluation_id"], run.evaluation_id, "the re-evaluation won the projection")
        [snapshot] = self._rows(ScaleEvidenceSnapshotRecord, workspace_id, id=run.snapshot_id)
        self.assertNotEqual(snapshot.prior_evaluation_id, backfill.evaluation_id, "never a backfill's")

        # 4. The Owner withdraws: no Target, one semantic event later.
        targeted_thread = self._open_thread(workspace_id, episode_id, run.evaluation_id, second)
        withdrawal = self._api("POST", f"/targets/{target['id']}/withdrawal-previews", expect=201,
                               json={"reason": "Acme Charge repriced the offer"},
                               headers={"Idempotency-Key": "ik-withdraw-preview"})
        self._api("POST", f"/targets/{target['id']}/withdraw", json={"withdrawal_preview_id": withdrawal["id"]},
                  headers={"Idempotency-Key": "ik-withdraw"})
        withdraw = self._event(workspace_id, target["id"], "withdraw")
        self.assertEqual(self._drain(workspace_id), 2)

        _published, withdrawn = self._target_change_runs(workspace_id, episode_id)
        self.assertEqual((withdrawn.normalized_target_key, withdrawn.semantic_event_sequence,
                          withdrawn.triggering_semantic_event_id, withdrawn.outcome_kind),
                         (NO_TARGET_MARKER, 2, withdraw.id, "evaluated"))
        self.assertEqual(self._superseded_by(workspace_id, targeted_thread), (withdraw.id, None, "target_changed"))
        self.assertEqual(self._api("GET", f"/enrollments/{enrollment_id}/evidence")["evaluation_id"],
                         withdrawn.evaluation_id)

        # 5. Replays add nothing.
        totals = self._totals(workspace_id)
        requested = self._rows(AuditEventRecord, workspace_id, event_type="scale.target_change_evaluation.requested")
        for replayed in [*self._rows(JobRecord, workspace_id, type=TARGET_CHANGE_JOB_TYPE),
                         self._job(workspace_id, withdrawn.id)]:
            with self.subTest(replay=replayed.type):
                with Session(self.owner) as session, session.begin():
                    session.execute(update(JobRecord).where(JobRecord.id == replayed.id).values(
                        state="queued", next_attempt_at=None, finished_at=None))
                self._drain(workspace_id)
                self.assertEqual(self._rows(JobRecord, workspace_id, id=replayed.id)[0].state, "succeeded")
                self.assertEqual(self._totals(workspace_id), totals)
        self.assertEqual(len(self._rows(AuditEventRecord, workspace_id,
                                        event_type="scale.target_change_evaluation.requested")), len(requested))

        # 6. Workspace B's Target for the same context reads nothing of A.
        other = self._workspace("other")
        self.current = other
        self._publish(episode, "other")
        self.assertEqual(self._drain(other.workspace_id), 1, "the Target change only: B has no Episode")
        self.assertEqual(self._rows(ScaleEvaluationRunRecord, other.workspace_id), [])
        self.assertEqual(self._totals(workspace_id), totals)
        self.assertEqual(len(self._target_change_runs(workspace_id, episode_id)), 2)

