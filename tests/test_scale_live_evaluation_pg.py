"""Live Scale evaluation end to end under the real roles on PostgreSQL (#110, #101 S4).

This is #101's acceptance. On a cluster released with ``make db-release`` the
API runs as ``marketai_domain_api`` and the Worker as
``marketai_domain_worker``. The Winner and the rows a DeliveryTarget refers
to are fixtures (their product entries are #63), seeded as the schema owner
the way the control plane opens a Workspace. The Policy is the Beta default
the migrations published (#127): nothing else publishes one, and the live
trigger selects it under the Worker's role, so no day is skipped as
``no_eligible_policy``. The connection and its binding go through the API,
and production code builds the Scale contract set and the DeliveryTarget on
the sandbox ad, with the campaign, ad set and creative the sandbox returns.

1. After the batch for A-1 attested the configuration, the Enrollment is
   activated through the API on day A: Candidate, draft, preview, activate.
2. Day by day the slot is set into the past and only
   ``WorkerRuntime.run_once`` runs: the tick, the batch, the collection --
   whose complete manifest requests the live run and enqueues it in the
   same transaction -- and the evaluation. Day k reads target day A+k-1.
   Each day writes one complete manifest, one live run and one Evaluation,
   and the API reads the Episode projection and health: ``warming`` on days
   1-13, ``monitorable`` from day 14. Day 1 does not roll the Episode over:
   its manifest adopts the real configuration as the baseline activation
   could only synthesize.
3. Replaying the tick, batch, collection and evaluation Jobs adds no run,
   snapshot, Evaluation, Occurrence or notification.
4. The Worker dies after Phase C committed and before the Job is marked
   succeeded: the retry returns the recorded outcome.
5. A blocked day gets no run; the next day evaluates.
6. Two adjacent days overlap: the later run waits in ``retry_wait``
   (``episode_phase_conflict``) while the earlier holds the Episode, then
   evaluates against it -- never ``failed_integrity``.
7. A paused Enrollment gets no manifest and no run.
8. Workspace B and A's controlled_mock context read nothing of it.

The clock: the tick reads ``observation_sync.utcnow``, patched per day as
#107's tests do, and the preview and activation read ``datetime.now``,
frozen at day A. The database's clock, the sync runs' completion and
everything else are real, after every simulated slot, so every slot is due
and every target day has ended.

Scale evidence tables refuse DELETE, so every Workspace id is new; the test
pauses its schedules and cancels its open Jobs when it ends. Run with
``DOMAIN_OWNER_DATABASE_URL``, ``DOMAIN_API_DATABASE_URL``,
``DOMAIN_WORKER_DATABASE_URL``, ``MARKETAI_DATABASE_URL`` (the owner) and
``MARKETAI_TENANT_CONTEXT_SECRET``. Fictional brand only: Acme Charge.
"""
from __future__ import annotations

import asyncio
import gzip
import os
import unittest
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AuditEventRecord, JobRecord, WorkspaceRecord, utcnow
from app.main import app
from app.meta import observation_sync
from app.meta.models import (
    MetaEvidencePageRecord,
    MetaObservationSyncRunRecord,
    MetaObservationSyncScheduleRecord,
    ScenarioVersionRecord,
)
from app.platform import jobs
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext
from app.scale.collector import COLLECT_JOB_TYPE
from app.scale.delivery_targets import create_delivery_target_with_contract_set
from app.scale.live_trigger import eligible_live_policy
from app.scale.models import (
    ScaleAdviceNotificationDecisionRecord,
    ScaleAdviceOccurrenceRecord,
    ScaleEpisodeRecord,
    ScaleEvaluationRecord,
    ScaleEvaluationRunRecord,
    ScaleEvidenceReadinessProjectionRecord,
    ScaleEvidenceSnapshotRecord,
    ScaleEvidenceSourceManifestRecord,
)
from app.scale.platform_policy import BETA_DEFAULT_POLICY_V2
from app.scale.service import EVALUATION_JOB_MAX_ATTEMPTS
from app.worker_runtime import WorkerRuntime, meta_write_capability_findings
from tests.support.meta_observation import seed_sandbox_ads, worker_context
from tests.support.scale_candidate_fixtures import (
    build_ready_winner,
    ensure_governance_prereqs,
)
from tests.support.scale_contract_set import (
    create_connection_through_api,
    delivery_target_request,
    sandbox_ad_hierarchy,
)

URLS = [os.getenv(key) for key in (
    "DOMAIN_OWNER_DATABASE_URL", "DOMAIN_API_DATABASE_URL", "DOMAIN_WORKER_DATABASE_URL",
)]
OWNER, API, WORKER = range(3)
SANDBOX = "customer_sandbox"
MOCK = "controlled_mock"
OWNER_ID = "usr_acme_owner"
EVALUATE = "scale_evaluate"
#: Contract §9.1: fourteen complete account-local days before monitorable.
MONITORABLE_DAY = 14
#: The account time zone the sandbox reports without a scenario setting (#108).
ZONE = timezone.utc


class _WorkerKilled(BaseException):
    """The Worker process dies: no handler catches it, the Job keeps its lease."""


class _FrozenClock(datetime):
    """``datetime`` whose ``now`` is the simulated activation time."""

    moment = datetime(2026, 1, 1, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.moment if tz is None else cls.moment.astimezone(tz)


def _fire_at(day: date) -> datetime:
    """The slot that reads target day ``day``: 04:00 local the next day (#101 Q3)."""
    return datetime.combine(day + timedelta(days=1), time(4), tzinfo=ZONE)


@unittest.skipUnless(all(URLS) and os.getenv("MARKETAI_TENANT_CONTEXT_SECRET"),
                     "requires isolated PostgreSQL runtime roles")
class LiveEvaluationEndToEndPostgresTests(unittest.TestCase):
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

    # -- roles, Workspaces and cleanup --------------------------------------

    @contextmanager
    def _as(self, index: int):
        with patch("app.adapters.persistence.database.SessionLocal", self.sessions[index]), \
             patch("app.platform.jobs.engine", self.engines[index]):
            yield

    def _workspace(self, prefix: str) -> TenantContext:
        workspace_id = f"ws_live_eval_pg_{prefix}_{uuid.uuid4().hex[:10]}"
        with Session(self.owner) as session, session.begin():  # the control plane opens it
            session.add(WorkspaceRecord(id=workspace_id, name=f"Acme Charge {workspace_id[-4:]}"))
        self.workspaces.append(workspace_id)
        return TenantContext(workspace_id=workspace_id, role="owner", initiator_id=OWNER_ID,
                             request_id="test-req", trace_id="trace-test")

    def _quiesce(self) -> None:
        with Session(self.owner) as session, session.begin():
            if self.workspaces:
                session.execute(update(MetaObservationSyncScheduleRecord)
                                .where(MetaObservationSyncScheduleRecord.workspace_id.in_(self.workspaces),
                                       MetaObservationSyncScheduleRecord.state == "active")
                                .values(state="paused"))
                session.execute(update(JobRecord)
                                .where(JobRecord.workspace_id.in_(self.workspaces),
                                       JobRecord.state.in_(("queued", "retry_wait", "leased", "running")))
                                .values(state="cancelled", finished_at=utcnow(), lease_owner=None,
                                        lease_expires_at=None))

    def _epoch(self, workspace_id: str) -> int:
        with Session(self.owner) as session:
            return int(session.get(WorkspaceRecord, workspace_id).authorization_epoch)

    def _worker(self, workspace_id: str, meta_mode: str = SANDBOX) -> TenantContext:
        return worker_context(workspace_id, meta_mode, authorization_epoch=self._epoch(workspace_id))

    # -- the Worker ---------------------------------------------------------

    def _set_slot(self, workspace_id: str, fire_at: datetime) -> None:
        with Session(self.owner) as session, session.begin():
            session.execute(update(MetaObservationSyncScheduleRecord)
                            .where(MetaObservationSyncScheduleRecord.workspace_id == workspace_id,
                                   MetaObservationSyncScheduleRecord.state == "active")
                            .values(next_fire_at=fire_at))

    def _drain(self, workspace_id: str, *, now: datetime | None = None, exclude_types: tuple[str, ...] = ()) -> int:
        """Only the Worker, as its role, until it is idle; other Workspaces' Jobs wait."""
        runtime = WorkerRuntime(owner=f"ci-only-live-eval-pg-{uuid.uuid4().hex[:6]}")
        clock = patch.object(observation_sync, "utcnow", return_value=now) if now else _noop()
        handled = 0
        with clock:
            for _ in range(60):
                with Session(self.owner) as session, session.begin():
                    session.execute(update(JobRecord)
                                    .where(JobRecord.workspace_id != workspace_id,
                                           JobRecord.state.in_(("queued", "retry_wait")))
                                    .values(next_attempt_at=utcnow() + timedelta(days=1)))
                with self._as(WORKER):
                    if not asyncio.run(runtime.run_once(exclude_types=exclude_types)):
                        self.assertEqual(meta_write_capability_findings(runtime), [])
                        return handled
                handled += 1
        self.fail("the Worker did not go idle")

    def _day(self, workspace_id: str, day: date, **kwargs) -> int:
        """Target day ``day``: its slot set a minute into the past, then only the Worker."""
        fire_at = _fire_at(day)
        self._set_slot(workspace_id, fire_at)
        return self._drain(workspace_id, now=fire_at + timedelta(minutes=1), **kwargs)

    def _park(self, workspace_id: str) -> None:
        """Move the next slot far ahead, so a drain replays Jobs without starting a new day."""
        self._set_slot(workspace_id, utcnow() + timedelta(days=60))

    # -- reading back -------------------------------------------------------

    def _rows(self, model, workspace_id: str, **filters) -> list:
        with Session(self.owner) as session:
            rows = list(session.scalars(select(model).where(model.workspace_id == workspace_id).filter_by(**filters)))
            for row in rows:
                session.expunge(row)
            return rows

    def _count(self, model, workspace_id: str, **filters) -> int:
        return len(self._rows(model, workspace_id, **filters))

    def _run_for(self, workspace_id: str, episode_id: str, day: date) -> ScaleEvaluationRunRecord:
        [run] = self._rows(ScaleEvaluationRunRecord, workspace_id, episode_id=episode_id, target_local_day=day)
        return run

    def _job(self, workspace_id: str, run_id: str) -> JobRecord:
        [job] = [job for job in self._rows(JobRecord, workspace_id, type=EVALUATE) if job.resource_id == run_id]
        return job

    def _api(self, method: str, path: str, *, expect: int = 200, **kwargs) -> dict:
        with self._as(API):
            response = self.client.request(method, f"/api/v1/scale/{SANDBOX}{path}", **kwargs)
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def _totals(self, workspace_id: str) -> dict[str, int]:
        return {model.__tablename__: self._count(model, workspace_id) for model in (
            MetaObservationSyncRunRecord, ScaleEvidenceSourceManifestRecord, ScaleEvaluationRunRecord,
            ScaleEvidenceSnapshotRecord, ScaleEvaluationRecord, ScaleAdviceOccurrenceRecord,
            ScaleAdviceNotificationDecisionRecord, JobRecord)}

    # -- the fixture --------------------------------------------------------

    def _migrated_policy(self) -> str:
        """The Policy a live sandbox run freezes, as the Worker selects it: the
        latest Beta default the migrations published, v2 (#127, #132)."""
        with Session(self.engines[WORKER]) as session:
            policy = eligible_live_policy(session, SANDBOX)
        self.assertIsNotNone(policy, "no_eligible_policy: the release published no Policy")
        self.assertEqual(policy.id, BETA_DEFAULT_POLICY_V2.id)
        return policy.id

    def _activated_enrollment(self, owner: TenantContext, activation_day: date) -> dict:
        """The Winner (fixture), the connection and DeliveryTarget (API and
        production code), A-1's batch, then activation through the API."""
        workspace_id = owner.workspace_id
        tag = uuid.uuid4().hex[:8]
        product_id = f"prod_live_eval_{tag}"
        sandbox = replace(owner, meta_mode=SANDBOX)
        with self._as(OWNER):  # what #63 will make in the product
            ensure_governance_prereqs(sandbox, product_id)
            chain = build_ready_winner(sandbox, product_id, tag)

        self.current = owner
        with self._as(API):
            connection_id = create_connection_through_api(self, self.client, SANDBOX)["id"]
        with self._as(WORKER):  # what a sandbox Launch leaves behind
            [ad_id] = seed_sandbox_ads(self._worker(workspace_id), connection_id, 1)
        with Session(self.owner) as session:
            scenario_id = session.scalar(select(ScenarioVersionRecord.id).where(
                ScenarioVersionRecord.meta_connection_id == connection_id))
            hierarchy = sandbox_ad_hierarchy(session, workspace_id=workspace_id, meta_mode=SANDBOX,
                                             connection_id=connection_id, ad_id=ad_id, as_of=activation_day)
        request = delivery_target_request(
            connection_id=connection_id, scenario_version_id=scenario_id, meta_mode=SANDBOX, hierarchy=hierarchy,
            references={"asset_version_id": chain["asset_version_id"], "offer_version_id": chain["offer_version_id"],
                        "landing_page_evidence_hash": chain["landing_page_evidence_hash"]},
            market_code=chain["market"], attribution_setting=chain["attribution_semantics_hash"])
        with self._as(API), tenant_session(sandbox) as session:
            target, _contract_set = create_delivery_target_with_contract_set(session, sandbox, request)
            target_id = target.id

        # The Winner fixture built its Localization chain synchronously; the
        # Jobs it left queued are not this test's and would re-run that chain.
        with Session(self.owner) as session, session.begin():
            session.execute(update(JobRecord)
                            .where(JobRecord.workspace_id == workspace_id, JobRecord.type.like("localization.%"),
                                   JobRecord.state.in_(("queued", "retry_wait")))
                            .values(state="cancelled", finished_at=utcnow()))
        # A-1's batch attests the configuration activation's preview reads.
        self.assertEqual(self._day(workspace_id, activation_day - timedelta(days=1)), 2,
                         "tick and batch; no Enrollment yet, so no collection")

        candidate = self._api("POST", "/enrollment-candidates", expect=201,
                              json={"winner_decision_id": chain["decision_id"], "delivery_target_id": target_id},
                              headers={"Idempotency-Key": f"ik-candidate-{tag}"})
        draft = self._api("POST", "/enrollments", expect=201, json={"enrollment_candidate_id": candidate["id"]},
                          headers={"Idempotency-Key": f"ik-draft-{tag}"})
        _FrozenClock.moment = datetime.combine(activation_day, time(10), tzinfo=ZONE)
        with patch("app.scale.activation_preview.datetime", _FrozenClock), \
                patch("app.scale.enrollment.datetime", _FrozenClock):
            preview = self._api("POST", f"/enrollments/{draft['id']}/activation-previews", expect=201,
                                json={"expected_revision": draft["revision"]},
                                headers={"Idempotency-Key": f"ik-preview-{tag}"})
            active = self._api("PUT", f"/enrollments/{draft['id']}/activate",
                               json={"activation_preview_id": preview["id"]},
                               headers={"If-Match": f'"{draft["revision"]}"', "Idempotency-Key": f"ik-activate-{tag}"})
        self.assertEqual(active["lifecycle"], "active")
        return {"enrollment": active, "connection_id": connection_id, "ad_id": ad_id, "tag": tag}

    # -- the acceptance -----------------------------------------------------

    def test_an_api_activated_sandbox_enrollment_is_evaluated_daily_by_the_worker_alone(self):
        owner = self._workspace("main")
        workspace_id = owner.workspace_id
        policy_id = self._migrated_policy()
        activation_day = utcnow().astimezone(ZONE).date() - timedelta(days=25)
        made = self._activated_enrollment(owner, activation_day)
        enrollment_id = made["enrollment"]["id"]
        episode_id = made["enrollment"]["current_episode_id"]
        episode = self._api("GET", f"/enrollments/{enrollment_id}/episodes/{episode_id}")
        self.assertEqual((episode["phase"], episode["started_local_day"]), ("collecting", activation_day.isoformat()))
        self._api("GET", f"/enrollments/{enrollment_id}/evidence", expect=404)  # nothing evaluated yet

        def day(k: int) -> date:
            return activation_day + timedelta(days=k - 1)

        # 1. Fourteen days, the Worker alone.
        for k in range(1, MONITORABLE_DAY + 1):
            with self.subTest(day=k):
                self.assertEqual(self._day(workspace_id, day(k)), 4, "tick, batch, collection, evaluation")
                manifests = self._rows(ScaleEvidenceSourceManifestRecord, workspace_id, episode_id=episode_id)
                runs = self._rows(ScaleEvaluationRunRecord, workspace_id, episode_id=episode_id)
                self.assertEqual((len(manifests), {m.state for m in manifests}), (k, {"complete"}))
                self.assertEqual((len(runs), {(r.trigger_kind, r.outcome_kind) for r in runs}),
                                 (k, {("live", "evaluated")}))
                self.assertEqual(self._count(ScaleEvaluationRecord, workspace_id, episode_id=episode_id), k)
                run = self._run_for(workspace_id, episode_id, day(k))
                self.assertEqual(run.policy_version_id, policy_id, "#101 Q8")
                evidence = self._api("GET", f"/enrollments/{enrollment_id}/evidence")
                self.assertEqual((evidence["episode_id"], evidence["evaluation_id"],
                                  evidence["cursor_as_of_local_day"]),
                                 (episode_id, run.evaluation_id, day(k).isoformat()),
                                 "the Episode activation opened, projected to day k")
                self.assertIn("health", evidence)  # e.g. not_delivering on a zero-delivery day
                if k < MONITORABLE_DAY:
                    self.assertEqual(evidence["maturity"], "warming")
                else:
                    # §9.1's gates hold; the sandbox's purchases and spend also
                    # clear §9.2's, which reads one step past monitorable.
                    self.assertIn(evidence["maturity"], ("monitorable", "economics_ready"))
                    # Health is decided now; with mixed signals it is none,
                    # and the reason says why (spec §12).
                    self.assertNotEqual(evidence["health_reason"], "episode_window_incomplete")
        self.assertEqual(self._rows(AuditEventRecord, workspace_id, event_type="scale.live_evaluation.skipped"), [],
                         "no day skipped: never no_eligible_policy (#127)")
        episode = self._api("GET", f"/enrollments/{enrollment_id}/episodes/{episode_id}")
        self.assertEqual((episode["phase"], episode["closure_reason"]), ("collecting", None))
        [adopted] = self._rows(AuditEventRecord, workspace_id, event_type="scale.episode.baseline_verified")
        self.assertEqual(adopted.resource_id, episode_id, "day 1 adopted the real configuration; no rollover")
        self.assertEqual(self._rows(ScaleEpisodeRecord, workspace_id, enrollment_id=enrollment_id)[0].id, episode_id)
        first = self._run_for(workspace_id, episode_id, day(1))
        self.assertEqual((self._job(workspace_id, first.id).payload_json["meta_mode"],
                          self._job(workspace_id, first.id).idempotency_key),
                         (SANDBOX, f"scale-eval:{first.id}:{first.algorithm_version}"))

        # 2. Replaying every layer's Job adds nothing.
        self._park(workspace_id)
        totals = self._totals(workspace_id)
        latest = {job_type: max(self._rows(JobRecord, workspace_id, type=job_type), key=lambda j: j.created_at)
                  for job_type in (observation_sync.TICK_JOB_TYPE, observation_sync.SYNC_JOB_TYPE,
                                   COLLECT_JOB_TYPE, EVALUATE)}
        for job_type, job in latest.items():
            with self.subTest(replay=job_type):
                with Session(self.owner) as session, session.begin():
                    session.execute(update(JobRecord).where(JobRecord.id == job.id).values(
                        state="queued", next_attempt_at=None, finished_at=None))
                self.assertEqual(self._drain(workspace_id, now=_fire_at(day(MONITORABLE_DAY)) + timedelta(minutes=1)),
                                 1)
                self.assertEqual(self._rows(JobRecord, workspace_id, id=job.id)[0].state, "succeeded")
                self.assertEqual(self._totals(workspace_id), totals)

        # 3. The Worker dies after Phase C committed, before the Job is marked succeeded.
        k = MONITORABLE_DAY + 1
        succeed = jobs.succeed
        killed: list[str] = []

        def dying_succeed(context, job_id, lease_owner):
            with Session(self.owner) as session:
                job_type = session.get(JobRecord, job_id).type
            if job_type == EVALUATE and not killed:
                killed.append(job_id)
                raise _WorkerKilled()
            return succeed(context, job_id, lease_owner)

        with patch("app.platform.jobs.succeed", dying_succeed), self.assertRaises(_WorkerKilled):
            self._day(workspace_id, day(k))
        run = self._run_for(workspace_id, episode_id, day(k))
        [job_id] = killed
        self.assertEqual((run.outcome_kind, self._rows(JobRecord, workspace_id, id=job_id)[0].state),
                         ("evaluated", "running"), "Phase C committed; the Job still holds its lease")
        evaluations = self._count(ScaleEvaluationRecord, workspace_id)
        with Session(self.owner) as session, session.begin():  # the lease runs out
            session.execute(update(JobRecord).where(JobRecord.id == job_id)
                            .values(lease_expires_at=utcnow() - timedelta(seconds=1)))
        self._park(workspace_id)
        self.assertEqual(self._drain(workspace_id), 1, "the claim function requeues it; the retry runs")
        retried = self._rows(JobRecord, workspace_id, id=job_id)[0]
        self.assertEqual((retried.state, retried.attempt), ("succeeded", 2))
        self.assertEqual(self._run_for(workspace_id, episode_id, day(k)).evaluation_id, run.evaluation_id)
        self.assertEqual(self._count(ScaleEvaluationRecord, workspace_id), evaluations, "the recorded outcome")
        self.assertEqual(len([e for e in self._rows(AuditEventRecord, workspace_id, resource_id=run.id)
                              if e.event_type.startswith("scale_evaluation_")]), 1)

        # 4. A blocked day gets no run; the next day evaluates.
        k += 1
        self.assertEqual(self._day(workspace_id, day(k), exclude_types=(COLLECT_JOB_TYPE,)), 2)
        configuration = next(r for r in self._rows(MetaObservationSyncRunRecord, workspace_id)
                             if r.occurrence_id.endswith(f":{day(k).isoformat()}:configuration"))
        with Session(self.owner) as session, session.begin():  # a stored page that no longer matches its checksum
            page = session.scalar(select(MetaEvidencePageRecord).where(
                MetaEvidencePageRecord.sync_run_id == configuration.id))
            page.payload_compressed = gzip.compress(b'{"data":[]}', mtime=0)
        self._park(workspace_id)
        self.assertEqual(self._drain(workspace_id), 1, "the collection alone")
        [blocked] = self._rows(ScaleEvidenceSourceManifestRecord, workspace_id, episode_id=episode_id,
                               target_local_day=day(k))
        self.assertEqual(blocked.state, "blocked")
        self.assertEqual(self._rows(ScaleEvaluationRunRecord, workspace_id, episode_id=episode_id,
                                    target_local_day=day(k)), [])
        [readiness] = self._rows(ScaleEvidenceReadinessProjectionRecord, workspace_id, episode_id=episode_id)
        self.assertEqual((readiness.maturity, readiness.quality_state), ("insufficient", "blocked"))
        k += 1
        self.assertEqual(self._day(workspace_id, day(k)), 4)
        self.assertEqual(self._run_for(workspace_id, episode_id, day(k)).outcome_kind, "evaluated")
        self.assertEqual(self._api("GET", f"/enrollments/{enrollment_id}/evidence")["cursor_as_of_local_day"],
                         day(k).isoformat())

        # 6. Adjacent days overlap: the later run waits for the earlier.
        k += 1
        with patch("app.scale.service.evaluate_scale", side_effect=_WorkerKilled()), \
                self.assertRaises(_WorkerKilled):
            self._day(workspace_id, day(k))  # dies in Phase B, holding the Episode
        held = self._run_for(workspace_id, episode_id, day(k))
        held_job = self._job(workspace_id, held.id)
        self.assertEqual((held.run_state, held_job.state), ("snapshot_sealed", "running"))
        with Session(self.owner) as session, session.begin():  # a slow Worker, still within its lease
            session.execute(update(JobRecord).where(JobRecord.id == held_job.id)
                            .values(lease_expires_at=utcnow() + timedelta(hours=1)))
        self.assertEqual(self._day(workspace_id, day(k + 1)), 4)
        waiting = self._run_for(workspace_id, episode_id, day(k + 1))
        waiting_job = self._job(workspace_id, waiting.id)
        self.assertEqual((waiting.run_state, waiting.outcome_kind), ("requested", None))
        self.assertEqual((waiting_job.state, waiting_job.error_code, waiting_job.max_attempts),
                         ("retry_wait", "episode_phase_conflict", EVALUATION_JOB_MAX_ATTEMPTS))
        with Session(self.owner) as session, session.begin():
            session.execute(update(JobRecord).where(JobRecord.id == held_job.id)
                            .values(lease_expires_at=utcnow() - timedelta(seconds=1)))
            session.execute(update(JobRecord).where(JobRecord.id == waiting_job.id)
                            .values(next_attempt_at=utcnow() - timedelta(seconds=1)))
        self._park(workspace_id)
        self.assertEqual(self._drain(workspace_id), 2)
        held = self._run_for(workspace_id, episode_id, day(k))
        waiting = self._run_for(workspace_id, episode_id, day(k + 1))
        self.assertEqual((held.outcome_kind, waiting.outcome_kind), ("evaluated", "evaluated"))
        [snapshot] = self._rows(ScaleEvidenceSnapshotRecord, workspace_id, id=waiting.snapshot_id)
        self.assertEqual(snapshot.prior_evaluation_id, held.evaluation_id, "sealed after the earlier run")
        self.assertEqual(self._rows(ScaleEvaluationRunRecord, workspace_id, outcome_kind="failed_integrity"), [])

        # 5. A paused Enrollment gets no manifest and no run.
        k += 2
        current = self._api("GET", f"/enrollments/{enrollment_id}")
        self._api("PUT", f"/enrollments/{enrollment_id}/pause", json={"reason": "user_requested"},
                  headers={"If-Match": f'"{current["revision"]}"', "Idempotency-Key": f"ik-pause-{made['tag']}"})
        totals = self._totals(workspace_id)
        self.assertEqual(self._day(workspace_id, day(k)), 3, "tick, batch, and the collection a paused one still gets")
        self.assertEqual(self._rows(ScaleEvidenceSourceManifestRecord, workspace_id, target_local_day=day(k)), [])
        self.assertEqual(self._rows(ScaleEvaluationRunRecord, workspace_id, target_local_day=day(k)), [])
        after = self._totals(workspace_id)
        self.assertEqual({key: after[key] - totals[key] for key in after if after[key] != totals[key]},
                         {"meta_sync_run_records": 5, "jobs": 3}, "only the day's read")

        # 8. Another Workspace, or A's other mode, reads nothing of it.
        other = self._workspace("other")
        self.current = other
        self._api("GET", f"/enrollments/{enrollment_id}/evidence", expect=404)
        self._api("GET", f"/enrollments/{enrollment_id}/episodes/{episode_id}", expect=404)
        for name, context in (("Workspace B", self._worker(other.workspace_id)),
                              ("controlled_mock", self._worker(workspace_id, MOCK))):
            with self.subTest(name), self._as(WORKER), tenant_session(context) as session:
                self.assertEqual(session.scalar(select(func.count()).select_from(ScaleEvaluationRunRecord)), 0)
                self.assertEqual(session.scalar(select(func.count()).select_from(
                    ScaleEvidenceSourceManifestRecord)), 0)


@contextmanager
def _noop():
    yield


if __name__ == "__main__":
    unittest.main()
