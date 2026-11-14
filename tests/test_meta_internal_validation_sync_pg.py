"""internal_validation daily reads under the real roles on PostgreSQL (#112, #101 S6).

The platform Workspace is opened as the schema owner, as the control plane
would. As ``marketai_domain_api`` the platform operator's bootstrap leaves two
sealed credential versions, an internal_validation connection and its
binding, and the platform route
``POST /api/platform/v1/scale/internal_validation/metric-contract-sets``
sets up the Scale read contracts; each generation gets an active daily
schedule at account-local 04:00. With the slots due, only the deployed
``WorkerRuntime`` runs, as ``marketai_domain_worker``: it dispatches through
the worker-only function, decrypts the credentials inside the platform
Workspace on every read, reads a simulated Graph API (no network, no real
token), defers the batch once when the usage reading crosses the threshold
-- its Job waits in ``retry_wait`` without spending an attempt -- and
resumes to five attested query instances.

Negatives under FORCE RLS: a customer Workspace's context, as the API and as
the Worker, reads none of the platform Workspace's schedules, runs, call
intents, pages, attestations or coverage, in internal_validation or in its
own sandbox mode; and the platform Workspace's own sandbox context does not
see its internal_validation rows either.

Run with ``DOMAIN_OWNER_DATABASE_URL``, ``DOMAIN_API_DATABASE_URL``,
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
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.meta import http_read_adapter
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AuditEventRecord, JobRecord, WorkspaceRecord, utcnow
from app.main import app
from app.meta import observation_sync
from app.meta.evidence import current_attestation
from app.meta.models import (
    EvidenceAttestationRecord,
    MetaCallIntentRecord,
    MetaEvidencePageRecord,
    MetaObservationSyncRunRecord,
    MetaObservationSyncScheduleRecord,
    ObservationCoverageLedgerRecord,
)
from app.meta.observation import DEFAULT_SYNC_CRON, latest_fire_at_or_before
from app.platform.platform_operator_identity import PlatformOperatorIdentity
from app.platform.tenancy import TenantContext
from app.platform.workspace_admission import WorkspaceAdmission
from app.scale.observed_ads import list_observed_ads
from app.worker_runtime import WorkerRuntime, meta_write_capability_findings
from tests.support.meta_graph_simulator import (
    ACCESS_TOKEN,
    CURRENCY,
    TIMEZONE,
    GraphSimulator,
    business_usage,
    platform_context,
    provision_connection,
)
from tests.support.meta_observation import worker_context as system_meta_context

URLS = [os.getenv(key) for key in (
    "DOMAIN_OWNER_DATABASE_URL", "DOMAIN_API_DATABASE_URL", "DOMAIN_WORKER_DATABASE_URL",
)]
OWNER, API, WORKER = range(3)
MODE = "internal_validation"
READS_PER_DAY = 9
EVIDENCE = (MetaObservationSyncScheduleRecord, MetaObservationSyncRunRecord, MetaCallIntentRecord,
            MetaEvidencePageRecord, EvidenceAttestationRecord, ObservationCoverageLedgerRecord)


@unittest.skipUnless(all(URLS) and os.getenv("MARKETAI_TENANT_CONTEXT_SECRET"),
                     "requires isolated PostgreSQL runtime roles")
class InternalValidationSyncPostgresTests(unittest.TestCase):
    def setUp(self):
        self.engines = [create_engine(url) for url in URLS]
        for engine in self.engines:
            self.addCleanup(engine.dispose)
        self.owner = self.engines[OWNER]
        self.sessions = [sessionmaker(bind=e, expire_on_commit=False, autoflush=False) for e in self.engines]
        self.workspaces: list[str] = []
        self.addCleanup(self._quiesce)
        originals = (app.state.platform_operator_jwt_verifier, getattr(app.state, "admission_reader", None))
        self.addCleanup(lambda: setattr(app.state, "platform_operator_jwt_verifier", originals[0]))
        self.addCleanup(lambda: setattr(app.state, "admission_reader", originals[1]))
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.graph = GraphSimulator()
        gateway = patch.object(http_read_adapter, "read_gateway", self.graph.gateway)
        gateway.start()
        self.addCleanup(gateway.stop)

    # -- roles --------------------------------------------------------------

    @contextmanager
    def _as(self, index: int):
        with patch("app.adapters.persistence.database.SessionLocal", self.sessions[index]), \
             patch("app.platform.jobs.engine", self.engines[index]):
            yield

    def _workspace(self, prefix: str, **columns) -> str:
        workspace_id = f"ws_meta_iv_pg_{prefix}_{uuid.uuid4().hex[:10]}"
        with Session(self.owner) as session, session.begin():  # the control plane opens it
            session.add(WorkspaceRecord(id=workspace_id, name=f"Acme Charge {workspace_id[-4:]}", **columns))
        self.workspaces.append(workspace_id)
        return workspace_id

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

    def _platform_operator(self, workspace_id: str) -> None:
        identity = PlatformOperatorIdentity(sub="usr_acme_platform_operator", workspace_id=workspace_id,
                                            session_id="sess_acme", iat=1, exp=2, jti=f"jti_{uuid.uuid4().hex}")
        app.state.platform_operator_jwt_verifier = AsyncMock(verify=AsyncMock(return_value=identity))
        app.state.admission_reader = SimpleNamespace(read=AsyncMock(return_value=WorkspaceAdmission(
            workspace_id=workspace_id, authorization_epoch=0, entitlement_version=1,
            suspended=False, is_platform_workspace=True,
        )))

    def _quarantine_others(self, workspace_id: str, *, release_retries: bool) -> None:
        with Session(self.owner) as session, session.begin():
            session.execute(update(JobRecord)
                            .where(JobRecord.workspace_id != workspace_id,
                                   JobRecord.state.in_(("queued", "retry_wait")))
                            .values(next_attempt_at=utcnow() + timedelta(days=1)))
            if release_retries:
                session.execute(update(JobRecord)
                                .where(JobRecord.workspace_id == workspace_id, JobRecord.state == "retry_wait")
                                .values(next_attempt_at=utcnow() - timedelta(seconds=1)))

    def _run_until_idle(self, runtime: WorkerRuntime, workspace_id: str, *, release_retries: bool = False) -> int:
        handled = 0
        for _ in range(20):
            self._quarantine_others(workspace_id, release_retries=release_retries)
            with self._as(WORKER):
                if not asyncio.run(runtime.run_once()):
                    return handled
            handled += 1
        self.fail("the Worker did not go idle")

    def _rows(self, model, workspace_id: str, **filters) -> list:
        with Session(self.owner) as session:
            rows = list(session.scalars(select(model).where(model.workspace_id == workspace_id).filter_by(**filters)))
            for row in rows:
                session.expunge(row)
            return rows

    # -- the main path ------------------------------------------------------

    def test_api_then_worker_only_read_meta_for_the_platform_workspace_and_defer_on_usage(self):
        platform_id = self._workspace("platform", is_platform_workspace=True)
        context = platform_context(platform_id)
        with self._as(API):
            provisioned = provision_connection(context)
        self._platform_operator(platform_id)
        with self._as(API):
            response = self.client.post(
                f"/api/platform/v1/scale/{MODE}/metric-contract-sets",
                headers={"Authorization": "Bearer ci-only-platform-operator-token"},
                json={"meta_connection_id": provisioned["connection_id"], "currency": CURRENCY,
                      "timezone_name": TIMEZONE},
            )
        self.assertEqual(response.status_code, 200, response.text)
        established = response.json()
        schedules = self._rows(MetaObservationSyncScheduleRecord, platform_id)
        self.assertEqual(sorted(row.contract_generation_id for row in schedules),
                         sorted(item["generation_id"] for item in established["generations"]))
        self.assertEqual({(row.state, row.cron_expression, row.meta_mode) for row in schedules},
                         {("active", DEFAULT_SYNC_CRON, MODE)})

        fire_at = latest_fire_at_or_before(DEFAULT_SYNC_CRON, TIMEZONE, utcnow())
        with Session(self.owner) as session, session.begin():
            session.execute(update(MetaObservationSyncScheduleRecord)
                            .where(MetaObservationSyncScheduleRecord.workspace_id == platform_id)
                            .values(next_fire_at=fire_at))
        # The scheduler reads its clock a minute after the slot, so the 15-minute
        # back-off ends well before the next day's slot whatever the time of day (#176).
        clock = patch.object(observation_sync, "utcnow", return_value=fire_at + timedelta(minutes=1))
        clock.start()
        self.addCleanup(clock.stop)
        self.graph.usage = lambda index, _request: business_usage(85 if index == 1 else 10)

        runtime = WorkerRuntime(owner=f"ci-only-meta-iv-pg-{uuid.uuid4().hex[:6]}")
        self.assertEqual(self._run_until_idle(runtime, platform_id), 2, "the tick, then the batch until it defers")
        batch, = self._rows(JobRecord, platform_id, type=observation_sync.SYNC_JOB_TYPE)
        self.assertEqual((batch.state, batch.error_code, batch.attempt), ("retry_wait", "meta_read_rate_limited", 0))
        self.assertGreater(batch.next_attempt_at, utcnow() + timedelta(minutes=14))
        self.assertEqual(len(self._rows(AuditEventRecord, platform_id, event_type="job.deferred")), 1)
        self.assertEqual(len(self.graph.requests), 1)

        self.assertEqual(self._run_until_idle(runtime, platform_id, release_retries=True), 1)
        batch, = self._rows(JobRecord, platform_id, type=observation_sync.SYNC_JOB_TYPE)
        self.assertEqual((batch.state, batch.attempt), ("succeeded", 1))
        runs = self._rows(MetaObservationSyncRunRecord, platform_id)
        self.assertEqual((len(runs), {run.state for run in runs}), (5, {"succeeded"}))
        self.assertEqual(len(self.graph.requests), READS_PER_DAY)
        self.assertEqual({request.method for request in self.graph.requests}, {"GET"})
        self.assertEqual({request.authorization for request in self.graph.requests}, {f"Bearer {ACCESS_TOKEN}"})
        self.assertEqual({a.state for a in self._rows(EvidenceAttestationRecord, platform_id)}, {"valid"})
        self.assertEqual(len(self._rows(ObservationCoverageLedgerRecord, platform_id)), 3)
        self.assertEqual(meta_write_capability_findings(runtime), [])

        configuration = next(item["generation_id"] for item in established["generations"]
                             if item["contract_type"] == "delivery_configuration")
        for role, ctx in ((API, context), (WORKER, system_meta_context(platform_id, MODE))):
            with self.subTest(role=role), self._as(role), tenant_session(ctx) as session:
                self.assertIsNotNone(current_attestation(session, contract_generation_id=configuration))
        with self._as(API), tenant_session(context) as session:
            listing = list_observed_ads(session, context, connection_id=provisioned["connection_id"])
        self.assertEqual(sorted(ad.ad_remote_id for ad in listing.ads), ["acme_ad_1", "acme_ad_2", "acme_ad_3"])

        # Another Workspace, and the platform Workspace's other modes, read none of it.
        customer_id = self._workspace("customer")
        expected_counts = {model: len(self._rows(model, platform_id)) for model in EVIDENCE}
        self.assertTrue(all(expected_counts.values()), expected_counts)
        contexts = [
            (TenantContext(workspace_id=customer_id, role="owner", initiator_id="usr_acme_owner",
                           meta_mode=MODE), 0),
            (TenantContext(workspace_id=customer_id, role="owner", initiator_id="usr_acme_owner",
                           meta_mode="customer_sandbox"), 0),
            (replace(context, meta_mode="customer_sandbox"), 0),
            (context, None),  # the positive control
        ]
        for role in (API, WORKER):
            for ctx, expected in contexts:
                if role == WORKER:
                    ctx = replace(system_meta_context(ctx.workspace_id, ctx.meta_mode),
                                  is_platform_workspace=ctx.is_platform_workspace)
                with self.subTest(role=role, workspace=ctx.workspace_id, mode=ctx.meta_mode), \
                        self._as(role), tenant_session(ctx) as session:
                    for model in EVIDENCE:
                        seen = len(session.scalars(select(model.id).where(
                            model.workspace_id == platform_id)).all())
                        self.assertEqual(seen, expected_counts[model] if expected is None else expected,
                                         model.__name__)
                        if expected == 0:
                            self.assertEqual(len(session.scalars(select(model.id)).all()), 0, model.__name__)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
