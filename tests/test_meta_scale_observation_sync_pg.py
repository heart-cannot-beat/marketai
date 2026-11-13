"""Scale's metric contract set and its scheduled reads under the real roles on PostgreSQL (#108, #101 S2).

SQLite cannot see RLS, a missing grant or the advisory lock, so here the API
runs as ``marketai_domain_api`` and the Worker as ``marketai_domain_worker``:

* the main path: a ``customer_sandbox`` connection and binding made through
  the API; production code (``create_delivery_target_with_contract_set``),
  as the API role, builds the daily, exact-window and delivery-configuration
  generations, their schedules and the contract set, and a DeliveryTarget on
  the set's configuration generation -- Enrollment activation resolves that
  set. With the slot moved into the past, only ``WorkerRuntime.run_once``
  runs: one tick, one batch, five succeeded runs (one per query instance),
  five valid attestations, one coverage row per generation, and
  ``current_attestation`` for the DeliveryTarget's generation;
* a controlled-mock fault on the configuration instance fails that run
  without an attestation while the four metric runs finish;
* negatives: Workspace B's API context neither establishes a set on A's
  connection nor reads A's set, members, generations or runs; a Member may
  not establish one.

Workspaces are opened as the schema owner, as the control plane would. Each
test pauses its schedules and cancels its open Jobs when it ends. Run with
``DOMAIN_OWNER_DATABASE_URL``, ``DOMAIN_API_DATABASE_URL``,
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

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import JobRecord, WorkspaceRecord, utcnow
from app.main import app
from app.meta import observation_sync
from app.meta.connections import create_connection
from app.meta.evidence import current_attestation
from app.meta.models import (
    EvidenceAttestationRecord,
    EvidencePublicationRecord,
    MetaObservationSyncRunRecord,
    MetaObservationSyncScheduleRecord,
    ObservationContractGenerationRecord,
    ObservationCoverageLedgerRecord,
    ScenarioVersionRecord,
)
from app.meta.observation import DEFAULT_SYNC_CRON, ensure_observation_schedule, observation_target_date
from app.meta.scale_read_contracts import CONTRACTS
from app.platform.http_identity import request_tenant_context
from app.platform.operations import OperationRecord
from app.platform.tenancy import TenantContext
from app.scale.delivery_targets import (
    MetricContractSetError,
    create_delivery_target_with_contract_set,
    ensure_metric_contract_set,
)
from app.scale.enrollment import _resolve_metric_semantic_contract_set_id
from app.scale.models import (
    ScaleDeliveryTargetRecord,
    ScaleMetricSemanticContractSetMemberRecord,
    ScaleMetricSemanticContractSetRecord,
)
from app.worker_runtime import WorkerRuntime, meta_write_capability_findings
from tests.support.meta_observation import AD_ACCOUNT, seed_sandbox_ads, worker_context
from tests.support.scale_contract_set import (
    create_connection_through_api,
    delivery_target_request,
    sandbox_ad_hierarchy,
    seed_target_references,
)

URLS = [os.getenv(key) for key in (
    "DOMAIN_OWNER_DATABASE_URL", "DOMAIN_API_DATABASE_URL", "DOMAIN_WORKER_DATABASE_URL",
)]
OWNER, API, WORKER = range(3)
SANDBOX = "customer_sandbox"
MOCK = "controlled_mock"
INSTANCES = ["baseline_7d", "configuration", "current_7d", "daily", "rolling_14d"]


@unittest.skipUnless(all(URLS) and os.getenv("MARKETAI_TENANT_CONTEXT_SECRET"),
                     "requires isolated PostgreSQL runtime roles")
class ScaleObservationPostgresTests(unittest.TestCase):
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
        from unittest.mock import patch

        with patch("app.adapters.persistence.database.SessionLocal", self.sessions[index]), \
             patch("app.platform.jobs.engine", self.engines[index]):
            yield

    def _workspace(self, prefix: str) -> TenantContext:
        workspace_id = f"ws_scale_reads_pg_{prefix}_{uuid.uuid4().hex[:10]}"
        with Session(self.owner) as session, session.begin():  # the control plane opens it
            session.add(WorkspaceRecord(id=workspace_id, name=f"Acme Charge {workspace_id[-4:]}"))
        self.workspaces.append(workspace_id)
        return TenantContext(workspace_id=workspace_id, role="owner", initiator_id="usr_acme_owner",
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

    def _make_due(self, workspace_id: str):
        with Session(self.owner) as session, session.begin():
            rows = list(session.scalars(select(MetaObservationSyncScheduleRecord).where(
                MetaObservationSyncScheduleRecord.workspace_id == workspace_id)))
            self.assertTrue(rows)
            fire_at = rows[0].next_fire_at - timedelta(days=1)
            session.execute(update(MetaObservationSyncScheduleRecord)
                            .where(MetaObservationSyncScheduleRecord.workspace_id == workspace_id)
                            .values(next_fire_at=fire_at))
        return fire_at

    def _drain(self, workspace_id: str, *, release_retries: bool = False) -> int:
        runtime = WorkerRuntime(owner=f"ci-only-scale-reads-pg-{uuid.uuid4().hex[:6]}")
        handled = 0
        for _ in range(30):
            with Session(self.owner) as session, session.begin():
                session.execute(update(JobRecord)
                                .where(JobRecord.workspace_id != workspace_id,
                                       JobRecord.state.in_(("queued", "retry_wait")))
                                .values(next_attempt_at=utcnow() + timedelta(days=1)))
                if release_retries:
                    session.execute(update(JobRecord)
                                    .where(JobRecord.workspace_id == workspace_id, JobRecord.state == "retry_wait")
                                    .values(next_attempt_at=utcnow() - timedelta(seconds=1)))
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

    def _jobs(self, workspace_id: str, job_type: str) -> list[JobRecord]:
        return self._rows(JobRecord, workspace_id, type=job_type)

    # -- the main path ------------------------------------------------------

    def test_production_code_builds_the_set_and_one_tick_reads_and_attests_every_instance(self):
        owner = self._workspace("main")
        self.current = owner
        with self._as(API):
            connection_id = create_connection_through_api(self, self.client, SANDBOX)["id"]
        with self._as(WORKER):  # a sandbox Launch's ads
            ad_ids = seed_sandbox_ads(worker_context(owner.workspace_id, SANDBOX), connection_id, 3)
        with Session(self.owner) as session, session.begin():  # #63 makes these in the product
            references = seed_target_references(session, owner.workspace_id, "pg")
            scenario_id = session.scalar(select(ScenarioVersionRecord.id).where(
                ScenarioVersionRecord.meta_connection_id == connection_id))
            hierarchy = sandbox_ad_hierarchy(session, workspace_id=owner.workspace_id, meta_mode=SANDBOX,
                                             connection_id=connection_id, ad_id=ad_ids[0], as_of=utcnow().date())
        context = replace(owner, meta_mode=SANDBOX)
        request = delivery_target_request(connection_id=connection_id, scenario_version_id=scenario_id,
                                          meta_mode=SANDBOX, hierarchy=hierarchy, references=references)
        with self._as(API), tenant_session(context) as session:
            target, contract_set = create_delivery_target_with_contract_set(session, context, request)
            target_id, target_generation = target.id, target.contract_generation_id
        with self._as(API), tenant_session(context) as session:
            again = ensure_metric_contract_set(session, context, connection_id=connection_id)
            self.assertEqual((again.contract_set_id, again.created), (contract_set.contract_set_id, False))
            self.assertEqual(_resolve_metric_semantic_contract_set_id(
                session, context, contract_generation_id=target_generation), contract_set.contract_set_id)

        generations = {g.contract_type: g for g in self._rows(ObservationContractGenerationRecord, owner.workspace_id)}
        self.assertEqual(set(generations), {"daily", "exact_window", "delivery_configuration"})
        self.assertEqual(target_generation, generations["delivery_configuration"].id)
        self.assertEqual({g.time_window["definition_hash"] for g in generations.values()},
                         {CONTRACTS[kind].contract_hash for kind in generations})
        schedules = self._rows(MetaObservationSyncScheduleRecord, owner.workspace_id)
        self.assertEqual({(s.state, s.cron_expression, s.next_fire_at) for s in schedules},
                         {("active", DEFAULT_SYNC_CRON, schedules[0].next_fire_at)})
        self.assertEqual(len(schedules), 3)

        self.assertEqual(len(ad_ids), 3)
        fire_at = self._make_due(owner.workspace_id)
        target_date = observation_target_date(fire_at, "UTC")

        self.assertEqual(self._drain(owner.workspace_id), 2, "one tick, one batch")

        tick, = self._jobs(owner.workspace_id, observation_sync.TICK_JOB_TYPE)
        batch, = self._jobs(owner.workspace_id, observation_sync.SYNC_JOB_TYPE)
        self.assertEqual((tick.state, batch.state), ("succeeded", "succeeded"))
        self.assertTrue(tick.id.startswith("job_mobs_"), "inserted by the dispatch function")
        runs = self._rows(MetaObservationSyncRunRecord, owner.workspace_id)
        self.assertEqual(sorted(run.occurrence_id.rsplit(":", 1)[1] for run in runs), INSTANCES)
        self.assertEqual({(run.state, run.occurrence_id.split(":")[2]) for run in runs},
                         {("succeeded", target_date.isoformat())})
        publications = {p.sync_run_id: p.id for p in self._rows(EvidencePublicationRecord, owner.workspace_id)}
        attestations = {a.publication_id: a for a in self._rows(EvidenceAttestationRecord, owner.workspace_id)}
        self.assertEqual({attestations[publications[run.id]].state for run in runs}, {"valid"})
        self.assertEqual(len(attestations), 5)
        coverage = self._rows(ObservationCoverageLedgerRecord, owner.workspace_id)
        self.assertEqual(sorted(c.contract_generation_id for c in coverage),
                         sorted(g.id for g in generations.values()))
        self.assertEqual({op.state for op in self._rows(OperationRecord, owner.workspace_id)}, {"succeeded"})
        for role in (API, WORKER):
            with self.subTest(role=role), self._as(role), \
                 tenant_session(worker_context(owner.workspace_id, SANDBOX)) as session:
                self.assertIsNotNone(current_attestation(session, contract_generation_id=target_generation))
        self.assertEqual(len(self._rows(ScaleDeliveryTargetRecord, owner.workspace_id, id=target_id)), 1)

    def test_a_controlled_mock_fault_fails_one_instance_and_the_others_finish(self):
        owner = self._workspace("mock")
        context = replace(owner, meta_mode=MOCK)
        with self._as(API), tenant_session(context) as session:
            connection_id = create_connection(session, context=context, ad_account_id=AD_ACCOUNT, scenario_config={
                "faults": {"rules": [{"operation": "read_delivery_configuration", "effect": "read_error",
                                      "match_page": 1, "match_edge": "ads"}]}}).id
        self.current = owner
        with self._as(API):
            binding = self.client.post(f"/api/v1/meta/connections/{connection_id}/bindings",
                                       json={"ad_account_id": AD_ACCOUNT, "meta_mode": MOCK})
            self.assertEqual(binding.status_code, 201, binding.text)
        with self._as(API), tenant_session(context) as session:
            established = ensure_metric_contract_set(session, context, connection_id=connection_id)
        self.assertEqual(self._rows(MetaObservationSyncScheduleRecord, owner.workspace_id), [],
                         "controlled_mock is never scheduled on its own")
        with self._as(API), tenant_session(worker_context(owner.workspace_id, MOCK)) as session:
            for generation_id in established.generation_ids.values():
                ensure_observation_schedule(session, session.get(ObservationContractGenerationRecord, generation_id))
        with self._as(WORKER):
            seed_sandbox_ads(worker_context(owner.workspace_id, MOCK), connection_id, 2)
        self._make_due(owner.workspace_id)

        self._drain(owner.workspace_id, release_retries=True)

        batch, = self._jobs(owner.workspace_id, observation_sync.SYNC_JOB_TYPE)
        self.assertEqual((batch.state, batch.error_code, batch.attempt), ("failed", "meta_observation_sync_failed", 3))
        runs = {run.occurrence_id.rsplit(":", 1)[1]: run for run in self._rows(MetaObservationSyncRunRecord,
                                                                               owner.workspace_id)}
        self.assertEqual({name: run.state for name, run in runs.items()},
                         {name: "failed" if name == "configuration" else "succeeded" for name in INSTANCES})
        published = {p.sync_run_id for p in self._rows(EvidencePublicationRecord, owner.workspace_id)}
        self.assertEqual(published, {run.id for name, run in runs.items() if name != "configuration"})
        with self._as(WORKER), tenant_session(worker_context(owner.workspace_id, MOCK)) as session:
            self.assertIsNone(current_attestation(
                session, contract_generation_id=established.delivery_configuration_generation_id))
            self.assertIsNotNone(current_attestation(session, contract_generation_id=established.generation_ids["daily"]))

    # -- negatives ----------------------------------------------------------

    def test_another_workspace_or_a_member_neither_establishes_nor_reads_the_set(self):
        a, b = self._workspace("a"), self._workspace("b")
        self.current = a
        with self._as(API):
            connection_id = create_connection_through_api(self, self.client, SANDBOX)["id"]
        for name, context, code in (("Workspace B", replace(b, meta_mode=SANDBOX), "connection_not_found"),
                                    ("a Member", replace(a, role="member", meta_mode=SANDBOX), "forbidden")):
            with self.subTest(name), self.assertRaises(MetricContractSetError) as raised, self._as(API), \
                 tenant_session(context) as session:
                ensure_metric_contract_set(session, context, connection_id=connection_id)
            self.assertEqual(raised.exception.code, code)
        self.assertEqual(self._rows(ObservationContractGenerationRecord, a.workspace_id), [])

        with self._as(API), tenant_session(replace(a, meta_mode=SANDBOX)) as session:
            established = ensure_metric_contract_set(session, replace(a, meta_mode=SANDBOX),
                                                     connection_id=connection_id)
        for role in (API, WORKER):
            for context, expected in ((replace(a, meta_mode=SANDBOX), 1), (replace(b, meta_mode=SANDBOX), 0),
                                      (replace(a, meta_mode=MOCK), 0)):
                with self.subTest(role=role, workspace=context.workspace_id, mode=context.meta_mode), \
                     self._as(role), tenant_session(context) as session:
                    sets = session.scalars(select(ScaleMetricSemanticContractSetRecord.id)).all()
                    self.assertEqual(len(sets), expected)
                    members = session.scalars(select(ScaleMetricSemanticContractSetMemberRecord.id).where(
                        ScaleMetricSemanticContractSetMemberRecord.contract_set_id
                        == established.contract_set_id)).all()
                    self.assertEqual(len(members), 3 * expected)
                    self.assertEqual(len(session.scalars(select(ObservationContractGenerationRecord.id)).all()),
                                     3 * expected)


if __name__ == "__main__":
    unittest.main()
