"""Scheduled Meta observation sync under the real roles on PostgreSQL (#107, #101 S1).

The Meta tables are FORCE RLS behind ``marketai_meta_context_is_current``
and the Worker has no tenant to read schedules under before it dispatches.
SQLite sees none of that, so here the API runs as ``marketai_domain_api``
through ``TestClient`` and the Worker as ``marketai_domain_worker``:

* the main path: a ``customer_sandbox`` connection, binding and daily
  contract created through the API get an active schedule at the next 04:00
  in the contract's time zone; with that slot moved into the past, only the
  deployed ``WorkerRuntime.run_once`` runs -- it dispatches through
  ``marketai_dispatch_meta_observation_ticks_v1``, runs the tick and then the
  batch, and the run ends ``succeeded`` with its terminal page, publication,
  valid attestation and one coverage-ledger row, which ``current_attestation``
  returns;
* two Workers dispatching at once create one tick for a slot (``SKIP LOCKED``
  and the tick key), and dispatching again creates none;
* a tick the claim rejects as ``authorization_epoch_stale`` does not stall
  its schedule (#119): the next dispatch gives the still-due slot one tick
  under the new epoch, and the Worker runs it to that day's attested run;
  below migration ``zzf6b7c8d9e0`` the function is ``zze4f5a6b7c8``'s again
  and the slot stalls, and upgrading gives the stalled slot its tick;
* a Worker process killed with SIGKILL after page 1 leaves page 1 and its
  checkpoint; the next Worker resumes at page 2 and writes no page twice;
* negatives: only the Worker may execute the function; Workspace B's context
  sees none of A's schedules or runs; an ``internal_validation`` generation
  gets no schedule; a retired connection is not dispatched; a
  ``controlled_mock`` read fault fails the run with no attestation.

Workspaces are opened as the schema owner, as the control plane would. The
evidence tables are append-only, so rows stay; every Workspace id is new and
each test pauses its schedules and cancels its open Jobs when it ends.

Run with ``DOMAIN_OWNER_DATABASE_URL``, ``DOMAIN_API_DATABASE_URL``,
``DOMAIN_WORKER_DATABASE_URL``, ``MARKETAI_DATABASE_URL`` (the owner) and
``MARKETAI_TENANT_CONTEXT_SECRET`` against a cluster released with
``make db-release``; ``DOMAIN_READONLY_DATABASE_URL`` adds the read-only role
to the refusal checks.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import subprocess
import sys
import textwrap
import unittest
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AuditEventRecord, JobRecord, WorkspaceRecord, utcnow
from app.main import app
from app.meta import observation_sync
from app.meta.connections import create_connection
from app.meta.evidence import current_attestation
from app.meta.models import (
    EvidenceAttestationRecord,
    EvidencePublicationRecord,
    MetaConnectionRecord,
    MetaEvidencePageRecord,
    MetaObservationSyncRunRecord,
    MetaObservationSyncScheduleRecord,
    MetaPageFetchAttemptRecord,
    ObservationContractGenerationRecord,
    ObservationCoverageLedgerRecord,
)
from app.meta.observation import (
    DEFAULT_SYNC_CRON,
    create_contract_generation,
    ensure_observation_schedule,
    latest_fire_at_or_before,
    next_fire_after,
    observation_target_date,
)
from app.platform import jobs
from app.platform.http_identity import request_tenant_context
from app.platform.operations import OperationRecord
from app.platform.tenancy import TenantContext
from app.worker_runtime import WorkerRuntime, meta_write_capability_findings
from tests.support.meta_observation import (
    AD_ACCOUNT,
    DAILY_FIELDS,
    TIMEZONE,
    create_through_api,
    daily_contract_body,
    seed_sandbox_ads,
    worker_context,
)

URLS = [os.getenv(key) for key in (
    "DOMAIN_OWNER_DATABASE_URL", "DOMAIN_API_DATABASE_URL", "DOMAIN_WORKER_DATABASE_URL",
)]
OWNER, API, WORKER = range(3)
ROOT = Path(__file__).resolve().parents[1]
DISPATCH = "marketai_dispatch_meta_observation_ticks_v1(integer)"
SANDBOX = "customer_sandbox"
#: The migration that keys ticks by authorization epoch (#119) and the one below it.
REVISION, DOWN_REVISION = "zzf6b7c8d9e0", "zzf5a6b7c8d9"
MIGRATIONS = ROOT / "migrations" / "versions"


def _function_body(source: str) -> str:
    """The dispatch function's body in a migration's SQL, whitespace-normalised."""
    body = re.search(r"FUNCTION public\.marketai_dispatch_meta_observation_ticks_v1\(.*?\$\$(.*?)\$\$", source, re.S)
    return " ".join(body.group(1).split())


def _epoch_key_migration():
    spec = importlib.util.spec_from_file_location(
        "migration_zzf6b7c8d9e0", MIGRATIONS / "zzf6b7c8d9e0_meta_observation_tick_epoch_key.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(all(URLS) and os.getenv("MARKETAI_TENANT_CONTEXT_SECRET"),
                     "requires isolated PostgreSQL runtime roles")
class MetaObservationSyncPostgresTests(unittest.TestCase):
    def setUp(self):
        self.engines = [create_engine(url) for url in URLS]
        for engine in self.engines:
            self.addCleanup(engine.dispose)
        self.owner, self.api, self.worker = self.engines
        self.sessions = [sessionmaker(bind=e, expire_on_commit=False, autoflush=False) for e in self.engines]
        self.current: TenantContext | None = None
        app.dependency_overrides[request_tenant_context] = lambda: self.current
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.workspaces: list[str] = []
        self.addCleanup(self._quiesce)

    # -- roles --------------------------------------------------------------

    @contextmanager
    def _as(self, index: int):
        """Every session the code under test opens, and the Job claim, as one role."""
        with patch("app.adapters.persistence.database.SessionLocal", self.sessions[index]), \
             patch("app.platform.jobs.engine", self.engines[index]):
            yield

    def _workspace(self, prefix: str, **columns) -> TenantContext:
        workspace_id = f"ws_meta_obs_pg_{prefix}_{uuid.uuid4().hex[:10]}"
        with Session(self.owner) as session, session.begin():  # the control plane opens it
            session.add(WorkspaceRecord(id=workspace_id, name=f"Acme Charge {workspace_id[-4:]}", **columns))
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

    # -- the API and the Worker ---------------------------------------------

    def _create(self, owner: TenantContext, meta_mode: str = SANDBOX) -> tuple[dict, dict]:
        self.current = owner
        with self._as(API):
            return create_through_api(self, self.client, meta_mode)

    def _make_due(self, generation_id: str, fire_at=None):
        fire_at = fire_at or latest_fire_at_or_before(DEFAULT_SYNC_CRON, TIMEZONE, utcnow())
        with Session(self.owner) as session, session.begin():
            session.execute(update(MetaObservationSyncScheduleRecord)
                            .where(MetaObservationSyncScheduleRecord.contract_generation_id == generation_id)
                            .values(next_fire_at=fire_at))
        return fire_at

    def _quarantine_others(self, workspace_id: str, release_retries: bool = False) -> None:
        """Earlier steps' open Jobs wait; this Workspace's retries are due now."""
        with Session(self.owner) as session, session.begin():
            session.execute(update(JobRecord)
                            .where(JobRecord.workspace_id != workspace_id,
                                   JobRecord.state.in_(("queued", "retry_wait")))
                            .values(next_attempt_at=utcnow() + timedelta(days=1)))
            if release_retries:
                session.execute(update(JobRecord)
                                .where(JobRecord.workspace_id == workspace_id, JobRecord.state == "retry_wait")
                                .values(next_attempt_at=utcnow() - timedelta(seconds=1)))

    def _drain(self, runtime: WorkerRuntime, workspace_id: str, *, release_retries: bool = False) -> int:
        handled = 0
        for _ in range(20):
            self._quarantine_others(workspace_id, release_retries)
            with self._as(WORKER):
                if not asyncio.run(runtime.run_once()):
                    return handled
            handled += 1
        self.fail("the Worker did not go idle")

    # -- reading back, as the owner -----------------------------------------

    def _rows(self, model, workspace_id: str, **filters) -> list:
        with Session(self.owner) as session:
            rows = list(session.scalars(select(model).where(model.workspace_id == workspace_id).filter_by(**filters)))
            for row in rows:
                session.expunge(row)
            return rows

    def _jobs(self, workspace_id: str, job_type: str) -> list[JobRecord]:
        with Session(self.owner) as session:
            rows = list(session.scalars(select(JobRecord).where(
                JobRecord.workspace_id == workspace_id, JobRecord.type == job_type).order_by(JobRecord.created_at)))
            for row in rows:
                session.expunge(row)
            return rows

    # -- the main path ------------------------------------------------------

    def test_api_then_worker_only_takes_a_sandbox_contract_to_a_current_attestation(self):
        owner = self._workspace("main")
        before = utcnow()
        connection, generation = self._create(owner)

        schedule, = self._rows(MetaObservationSyncScheduleRecord, owner.workspace_id)
        self.assertEqual((schedule.state, schedule.cron_expression, schedule.contract_generation_id),
                         ("active", DEFAULT_SYNC_CRON, generation["id"]))
        self.assertEqual(schedule.next_fire_at, next_fire_after(DEFAULT_SYNC_CRON, TIMEZONE, before))
        local = schedule.next_fire_at.astimezone(ZoneInfo(TIMEZONE))
        self.assertEqual((local.hour, local.minute), (4, 0))

        with self._as(WORKER):  # a sandbox Launch's ads
            seed_sandbox_ads(worker_context(owner.workspace_id, SANDBOX), connection["id"], 3)
        fire_at = self._make_due(generation["id"])
        target = observation_target_date(fire_at, TIMEZONE)

        runtime = WorkerRuntime(owner=f"ci-only-meta-obs-pg-{uuid.uuid4().hex[:6]}")
        self.assertEqual(self._drain(runtime, owner.workspace_id), 2, "the tick, then the batch")

        tick, = self._jobs(owner.workspace_id, observation_sync.TICK_JOB_TYPE)
        batch, = self._jobs(owner.workspace_id, observation_sync.SYNC_JOB_TYPE)
        self.assertEqual((tick.state, batch.state), ("succeeded", "succeeded"))
        self.assertTrue(tick.id.startswith("job_mobs_"), "inserted by the dispatch function")
        self.assertEqual(tick.payload_json, {"connection_id": connection["id"], "meta_mode": SANDBOX,
                                             "fire_at": observation_sync.fire_at_text(fire_at)})
        self.assertEqual(tick.idempotency_key,
                         observation_sync.tick_idempotency_key(connection["id"], fire_at, authorization_epoch=0))
        self.assertEqual((tick.initiator_role, tick.safe_to_retry_after_crash, tick.authorization_epoch),
                         ("system", True, 0))
        self.assertEqual(batch.payload_json["target_date"], target.isoformat())
        with Session(self.owner) as session:
            enqueued = session.scalars(select(AuditEventRecord).where(
                AuditEventRecord.resource_id == tick.id, AuditEventRecord.event_type == "job.enqueued")).one()
        self.assertEqual((enqueued.executor, enqueued.details_json["job_type"]),
                         ("system:worker", observation_sync.TICK_JOB_TYPE))

        schedule, = self._rows(MetaObservationSyncScheduleRecord, owner.workspace_id)
        self.assertEqual((schedule.last_fired_at, schedule.next_fire_at),
                         (fire_at, next_fire_after(DEFAULT_SYNC_CRON, TIMEZONE, fire_at)))

        run, = self._rows(MetaObservationSyncRunRecord, owner.workspace_id)
        self.assertEqual((run.state, run.occurrence_id, run.total_pages, run.total_rows),
                         ("succeeded", f"obs:{generation['id']}:{target.isoformat()}:daily", 2, 36))
        pages = sorted(self._rows(MetaEvidencePageRecord, owner.workspace_id), key=lambda p: p.page_order)
        self.assertEqual([(p.page_order, p.is_terminal) for p in pages], [(1, False), (2, True)])
        publication, = self._rows(EvidencePublicationRecord, owner.workspace_id)
        attestation, = self._rows(EvidenceAttestationRecord, owner.workspace_id)
        coverage, = self._rows(ObservationCoverageLedgerRecord, owner.workspace_id)
        self.assertEqual((publication.state, publication.sync_run_id), ("published", run.id))
        self.assertEqual((attestation.state, attestation.publication_id), ("valid", publication.id))
        self.assertEqual((coverage.coverage_date, coverage.attestation_id), (target, attestation.id))
        operation, = self._rows(OperationRecord, owner.workspace_id)
        self.assertEqual((operation.command_type, operation.state), ("meta.observation_sync", "succeeded"))

        for role in (API, WORKER):
            with self.subTest(role=role), self._as(role), \
                 tenant_session(worker_context(owner.workspace_id, SANDBOX)) as session:
                self.assertEqual(current_attestation(session, contract_generation_id=generation["id"]).id,
                                 attestation.id)
        self.assertEqual(meta_write_capability_findings(runtime), [])

    # -- concurrency and replay ---------------------------------------------

    def test_two_workers_dispatching_at_once_create_one_tick_per_slot(self):
        owner = self._workspace("race")
        connection, generation = self._create(owner)
        fire_at = self._make_due(generation["id"])
        dispatch = text("SELECT public.marketai_dispatch_meta_observation_ticks_v1(500)")

        first, second = self.worker.connect(), self.worker.connect()
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        held = first.begin()
        self.assertGreaterEqual(first.execute(dispatch).scalar_one(), 1)
        # The first dispatcher still holds the schedule: the second skips it.
        with second.begin():
            second.execute(dispatch).scalar_one()
        held.commit()
        with second.begin():
            second.execute(dispatch).scalar_one()  # committed: the tick key answers
        with self.worker.begin() as again:
            again.execute(dispatch).scalar_one()

        ticks = self._jobs(owner.workspace_id, observation_sync.TICK_JOB_TYPE)
        self.assertEqual([t.idempotency_key for t in ticks],
                         [observation_sync.tick_idempotency_key(connection["id"], fire_at, authorization_epoch=0)])
        with Session(self.owner) as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(AuditEventRecord).where(
                AuditEventRecord.resource_id == ticks[0].id, AuditEventRecord.event_type == "job.enqueued")), 1)
        schedule, = self._rows(MetaObservationSyncScheduleRecord, owner.workspace_id)
        self.assertEqual(schedule.next_fire_at, fire_at, "dispatch never moves the slot")

    def test_a_tick_rejected_for_a_stale_epoch_is_dispatched_again_and_its_slot_runs(self):
        owner = self._workspace("stale")
        connection, generation = self._create(owner)
        fire_at = self._make_due(generation["id"])
        dispatch = text("SELECT public.marketai_dispatch_meta_observation_ticks_v1(500)")
        with self.worker.begin() as connection_:
            connection_.execute(dispatch)
        with Session(self.owner) as session, session.begin():  # a membership change, as the control plane makes it
            session.execute(update(WorkspaceRecord).where(WorkspaceRecord.id == owner.workspace_id)
                            .values(authorization_epoch=WorkspaceRecord.authorization_epoch + 1))
        self._quarantine_others(owner.workspace_id)
        with self._as(WORKER):
            self.assertIsNone(jobs.claim_next("ci-only-meta-obs-pg-stale",
                                              job_types=(observation_sync.TICK_JOB_TYPE,)))
        stale, = self._jobs(owner.workspace_id, observation_sync.TICK_JOB_TYPE)
        self.assertEqual((stale.state, stale.error_code, stale.authorization_epoch),
                         ("failed", "authorization_epoch_stale", 0))

        for _ in range(2):  # the slot is still due; once per epoch
            with self.worker.begin() as connection_:
                connection_.execute(dispatch)
        ticks = self._jobs(owner.workspace_id, observation_sync.TICK_JOB_TYPE)
        self.assertEqual(len(ticks), 2, "the stale tick's slot gets a new tick")
        fresh = ticks[1]
        self.assertEqual((fresh.state, fresh.authorization_epoch, fresh.payload_json),
                         ("queued", 1, stale.payload_json))
        self.assertEqual(
            (stale.idempotency_key, fresh.idempotency_key),
            (observation_sync.tick_idempotency_key(connection["id"], fire_at, authorization_epoch=0),
             observation_sync.tick_idempotency_key(connection["id"], fire_at, authorization_epoch=1)))
        self.assertTrue(fresh.id.startswith("job_mobs_") and fresh.id != stale.id)
        with Session(self.owner) as session:
            enqueued = session.scalars(select(AuditEventRecord).where(
                AuditEventRecord.resource_id == fresh.id, AuditEventRecord.event_type == "job.enqueued")).one()
        self.assertEqual((enqueued.authorization_epoch, enqueued.executor), (1, "system:worker"))

        self._drain(WorkerRuntime(owner=f"ci-only-meta-obs-pg-{uuid.uuid4().hex[:6]}"), owner.workspace_id)

        self.assertEqual([(t.state, t.authorization_epoch)
                          for t in self._jobs(owner.workspace_id, observation_sync.TICK_JOB_TYPE)],
                         [("failed", 0), ("succeeded", 1)])
        schedule, = self._rows(MetaObservationSyncScheduleRecord, owner.workspace_id)
        self.assertEqual((schedule.last_fired_at, schedule.next_fire_at),
                         (fire_at, next_fire_after(DEFAULT_SYNC_CRON, TIMEZONE, fire_at)))
        target = observation_target_date(fire_at, TIMEZONE)  # the day the stale tick was for
        batch, = self._jobs(owner.workspace_id, observation_sync.SYNC_JOB_TYPE)
        self.assertEqual((batch.state, batch.authorization_epoch, batch.payload_json["target_date"]),
                         ("succeeded", 1, target.isoformat()))
        run, = self._rows(MetaObservationSyncRunRecord, owner.workspace_id)
        self.assertEqual((run.state, run.occurrence_id),
                         ("succeeded", f"obs:{generation['id']}:{target.isoformat()}:daily"))
        attestation, = self._rows(EvidenceAttestationRecord, owner.workspace_id)
        self.assertEqual(attestation.state, "valid")
        with self.worker.begin() as connection_:
            connection_.execute(dispatch)
        self.assertEqual(len(self._jobs(owner.workspace_id, observation_sync.TICK_JOB_TYPE)), 2, "the slot moved on")

    # -- the migration ------------------------------------------------------

    def _alembic(self, *arguments: str) -> str:
        completed = subprocess.run(
            [sys.executable, "-m", "alembic", *arguments], cwd=ROOT, capture_output=True, text=True,
            env={**os.environ, "MARKETAI_DATABASE_URL": URLS[OWNER]}, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout

    def _deployed_body(self) -> str:
        with self.owner.connect() as connection:
            source = connection.scalar(text("SELECT prosrc FROM pg_proc WHERE oid = CAST(:signature AS regprocedure)"),
                                       {"signature": f"public.{DISPATCH}"})
        return " ".join(source.split())

    def _tick_rows(self, workspace_id: str) -> list[tuple]:
        """This Workspace's ticks in plain SQL: below head the ORM may not match the schema."""
        with self.owner.connect() as connection:
            return [tuple(row) for row in connection.execute(text(
                "SELECT idempotency_key, state, error_code, authorization_epoch FROM jobs"
                " WHERE workspace_id = :workspace AND type = 'meta_observation_tick' ORDER BY created_at, id"
            ), {"workspace": workspace_id})]

    def test_below_the_migration_a_stale_tick_stalls_its_slot_and_the_upgrade_releases_it(self):
        head = self._alembic("current")  # later migrations may follow REVISION
        self.assertIn("(head)", head)
        self.addCleanup(self._alembic, "upgrade", "head")  # never leave the cluster below head
        owner = self._workspace("migration")
        connection, generation = self._create(owner)  # everything the ORM writes, at head
        fire_at = self._make_due(generation["id"])
        slot = f"meta-obs-tick:{connection['id']}:{observation_sync.fire_at_text(fire_at)}"
        dispatch = text("SELECT public.marketai_dispatch_meta_observation_ticks_v1(500)")
        migration = _epoch_key_migration()

        # Downgrade puts zze4f5a6b7c8's function back, and with it #119's stall.
        self._alembic("downgrade", DOWN_REVISION)
        self.assertIn(DOWN_REVISION, self._alembic("current"))
        self.assertEqual(self._deployed_body(),
                         _function_body((MIGRATIONS / "zze4f5a6b7c8_meta_observation_dispatch.py").read_text()))
        with self.worker.begin() as connection_:
            connection_.execute(dispatch)
        with self.owner.begin() as connection_:  # a membership change, as the control plane makes it
            connection_.execute(text("UPDATE workspaces SET authorization_epoch = authorization_epoch + 1"
                                     " WHERE id = :workspace"), {"workspace": owner.workspace_id})
        self._quarantine_others(owner.workspace_id)
        with self._as(WORKER):
            self.assertIsNone(jobs.claim_next("ci-only-meta-obs-pg-migration",
                                              job_types=(observation_sync.TICK_JOB_TYPE,)))
        for _ in range(2):
            with self.worker.begin() as connection_:
                connection_.execute(dispatch)
        self.assertEqual(self._tick_rows(owner.workspace_id), [(slot, "failed", "authorization_epoch_stale", 0)],
                         "the old key is taken: the still-due slot gets nothing")

        # The upgrade keys by epoch: the stalled slot gets its tick at the next dispatch.
        self._alembic("upgrade", REVISION)
        self.assertIn(REVISION, self._alembic("current"))
        self.assertEqual(self._deployed_body(), _function_body(migration._dispatch_function(*migration._EPOCH_KEYED)))
        for _ in range(2):
            with self.worker.begin() as connection_:
                connection_.execute(dispatch)
        self.assertEqual(self._tick_rows(owner.workspace_id),
                         [(slot, "failed", "authorization_epoch_stale", 0), (f"{slot}:1", "queued", "", 1)])
        self._alembic("upgrade", "head")
        self.assertEqual(self._alembic("current"), head)
        self.test_only_the_worker_may_execute_the_dispatch_function()

    def test_a_worker_killed_after_page_one_resumes_at_page_two_without_a_duplicate_page(self):
        owner = self._workspace("kill")
        connection, generation = self._create(owner)
        with self._as(WORKER):
            seed_sandbox_ads(worker_context(owner.workspace_id, SANDBOX), connection["id"], 3)
        self._make_due(generation["id"])
        self._quarantine_others(owner.workspace_id)

        script = textwrap.dedent("""
            import asyncio, os, signal
            from app.adapters.meta.fake_adapter import FakeMetaAdapter
            from app.worker_runtime import WorkerRuntime

            real_read = FakeMetaAdapter.read_insights

            def read(adapter, **kwargs):
                if kwargs.get("after") is not None:
                    os.kill(os.getpid(), signal.SIGKILL)
                return real_read(adapter, **kwargs)

            FakeMetaAdapter.read_insights = read
            runtime = WorkerRuntime(owner="ci-only-meta-obs-pg-killed")
            for _ in range(10):
                asyncio.run(runtime.run_once())
        """)
        env = {**os.environ, "MARKETAI_DATABASE_URL": URLS[WORKER]}
        killed = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(killed.returncode, -9, killed.stderr[-2000:])

        batch, = self._jobs(owner.workspace_id, observation_sync.SYNC_JOB_TYPE)
        run, = self._rows(MetaObservationSyncRunRecord, owner.workspace_id)
        self.assertEqual((batch.state, run.state, run.last_completed_page_order), ("running", "running", 1))
        self.assertEqual([p.page_order for p in self._rows(MetaEvidencePageRecord, owner.workspace_id)], [1])

        with Session(self.owner) as session, session.begin():  # its lease runs out
            session.execute(update(JobRecord).where(JobRecord.id == batch.id)
                            .values(lease_expires_at=utcnow() - timedelta(seconds=1)))
        runtime = WorkerRuntime(owner=f"ci-only-meta-obs-pg-{uuid.uuid4().hex[:6]}")
        self._drain(runtime, owner.workspace_id)

        run, = self._rows(MetaObservationSyncRunRecord, owner.workspace_id)
        self.assertEqual((run.state, run.total_pages, run.total_rows), ("succeeded", 2, 36))
        self.assertEqual(sorted(p.page_order for p in self._rows(MetaEvidencePageRecord, owner.workspace_id)),
                         [1, 2])
        self.assertEqual(sorted((a.page_order, a.attempt_number, a.state)
                                for a in self._rows(MetaPageFetchAttemptRecord, owner.workspace_id)),
                         [(1, 1, "succeeded"), (2, 1, "started"), (2, 2, "succeeded")])
        self.assertEqual(len(self._rows(EvidenceAttestationRecord, owner.workspace_id)), 1)

    # -- negatives ----------------------------------------------------------

    def test_only_the_worker_may_execute_the_dispatch_function(self):
        with self.owner.connect() as connection:
            definer, config, volatility, result = connection.execute(text(
                "SELECT p.prosecdef, p.proconfig, p.provolatile, pg_get_function_result(p.oid) "
                "  FROM pg_proc p WHERE p.oid = CAST(:signature AS regprocedure)"
            ), {"signature": f"public.{DISPATCH}"}).one()
            self.assertEqual((definer, config, volatility, result), (True, ['search_path=""'], "v", "integer"))
            public = connection.scalar(text(
                "SELECT coalesce(bool_or(acl.privilege_type = 'EXECUTE'), false) "
                "  FROM pg_proc p, aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) acl "
                " WHERE p.oid = CAST(:signature AS regprocedure) AND acl.grantee = 0"
            ), {"signature": f"public.{DISPATCH}"})
            self.assertFalse(public, "PUBLIC may not execute it")
            for role, allowed in (("marketai_domain_api", False), ("marketai_domain_readonly", False),
                                  ("marketai_domain_worker", True)):
                with self.subTest(role=role):
                    self.assertEqual(connection.scalar(text(
                        "SELECT has_function_privilege(CAST(:role AS text), "
                        "CAST(:signature AS regprocedure), 'EXECUTE')"
                    ), {"role": role, "signature": f"public.{DISPATCH}"}), allowed)
        engines = [self.api]
        if os.getenv("DOMAIN_READONLY_DATABASE_URL"):
            engines.append(create_engine(os.environ["DOMAIN_READONLY_DATABASE_URL"]))
            self.addCleanup(engines[-1].dispose)
        for engine in engines:
            with self.subTest(role=engine.url.username), engine.connect() as connection, \
                 self.assertRaisesRegex(Exception, "permission denied"):
                connection.execute(text(f"SELECT public.{DISPATCH.replace('integer', '1')}"))

    def test_workspace_b_reads_none_of_workspace_as_schedules_or_runs(self):
        a = self._workspace("a")
        b = self._workspace("b")
        _connection, generation = self._create(a)
        self._make_due(generation["id"])
        self._drain(WorkerRuntime(owner=f"ci-only-meta-obs-pg-{uuid.uuid4().hex[:6]}"), a.workspace_id)
        self.assertEqual(len(self._rows(MetaObservationSyncRunRecord, a.workspace_id)), 1)

        for role in (API, WORKER):
            for context, expected in ((worker_context(a.workspace_id, SANDBOX), 1),
                                      (worker_context(b.workspace_id, SANDBOX), 0),
                                      (worker_context(a.workspace_id, "controlled_mock"), 0)):
                with self.subTest(role=role, workspace=context.workspace_id, mode=context.meta_mode), \
                     self._as(role), tenant_session(context) as session:
                    for model in (MetaObservationSyncScheduleRecord, MetaObservationSyncRunRecord,
                                  EvidenceAttestationRecord, ObservationCoverageLedgerRecord):
                        self.assertEqual(len(session.scalars(select(model.id)).all()), expected, model.__name__)

    def test_an_internal_validation_generation_gets_no_schedule(self):
        platform = self._workspace("platform", is_platform_workspace=True)
        context = replace(worker_context(platform.workspace_id, "internal_validation"), is_platform_workspace=True)
        with self._as(API), tenant_session(context) as session:
            connection = MetaConnectionRecord(id=f"mcon_{uuid.uuid4().hex[:12]}", workspace_id=platform.workspace_id,
                                              meta_mode="internal_validation", ad_account_id=AD_ACCOUNT)
            session.add(connection)
            session.flush()
            create_contract_generation(
                session, workspace_id=platform.workspace_id, meta_mode="internal_validation",
                connection_id=connection.id, contract_type="daily", ad_account_id=AD_ACCOUNT,
                fields=DAILY_FIELDS, attribution_window={}, breakdowns=[], time_window={}, local_timezone=TIMEZONE,
            )
        self.assertEqual(len(self._rows(ObservationContractGenerationRecord, platform.workspace_id)), 1)
        self.assertEqual(self._rows(MetaObservationSyncScheduleRecord, platform.workspace_id), [])

    def test_a_retired_connection_is_not_dispatched(self):
        owner = self._workspace("retired")
        connection, generation = self._create(owner)
        self._make_due(generation["id"])
        self.current = owner
        with self._as(API):
            response = self.client.post(f"/api/v1/meta/connections/{connection['id']}/retire",
                                        json={"meta_mode": SANDBOX})
        self.assertEqual(response.status_code, 200, response.text)
        schedule, = self._rows(MetaObservationSyncScheduleRecord, owner.workspace_id)
        self.assertEqual(schedule.state, "paused")
        with Session(self.owner) as session, session.begin():  # left active by an older release
            session.execute(update(MetaObservationSyncScheduleRecord)
                            .where(MetaObservationSyncScheduleRecord.workspace_id == owner.workspace_id)
                            .values(state="active"))
        with self.worker.begin() as connection_:
            connection_.execute(text("SELECT public.marketai_dispatch_meta_observation_ticks_v1(500)"))
        self.assertEqual(self._jobs(owner.workspace_id, observation_sync.TICK_JOB_TYPE), [])

    def test_a_controlled_mock_read_fault_fails_the_run_without_an_attestation(self):
        owner = self._workspace("mock")
        mode = "controlled_mock"
        owner_in_mode = replace(owner, meta_mode=mode)
        with self._as(API), tenant_session(owner_in_mode) as session:
            connection_id = create_connection(session, context=owner_in_mode, ad_account_id=AD_ACCOUNT,
                                              scenario_config={"faults": {"rules": [
                                                  {"operation": "read_insights", "effect": "read_error",
                                                   "match_page": 1}]}}).id
        self.current = owner
        with self._as(API):
            binding = self.client.post(f"/api/v1/meta/connections/{connection_id}/bindings",
                                       json={"ad_account_id": AD_ACCOUNT, "meta_mode": mode})
            self.assertEqual(binding.status_code, 201, binding.text)
            created = self.client.post("/api/v1/meta/contracts", json=daily_contract_body(connection_id, mode))
            self.assertEqual(created.status_code, 201, created.text)
        generation_id = created.json()["id"]
        self.assertEqual(self._rows(MetaObservationSyncScheduleRecord, owner.workspace_id), [],
                         "controlled_mock is never scheduled on its own")
        with self._as(API), tenant_session(worker_context(owner.workspace_id, mode)) as session:
            ensure_observation_schedule(session, session.get(ObservationContractGenerationRecord, generation_id))
        self._make_due(generation_id)

        self._drain(WorkerRuntime(owner=f"ci-only-meta-obs-pg-{uuid.uuid4().hex[:6]}"), owner.workspace_id,
                    release_retries=True)

        batch, = self._jobs(owner.workspace_id, observation_sync.SYNC_JOB_TYPE)
        self.assertEqual((batch.state, batch.error_code, batch.attempt), ("failed", "meta_observation_sync_failed", 3))
        run, = self._rows(MetaObservationSyncRunRecord, owner.workspace_id)
        self.assertEqual(run.state, "failed")
        for model in (MetaEvidencePageRecord, EvidencePublicationRecord, EvidenceAttestationRecord,
                      ObservationCoverageLedgerRecord):
            self.assertEqual(self._rows(model, owner.workspace_id), [], model.__name__)
        operation, = self._rows(OperationRecord, owner.workspace_id)
        self.assertEqual(operation.state, "failed")
        with self._as(WORKER), tenant_session(worker_context(owner.workspace_id, mode)) as session:
            self.assertIsNone(current_attestation(session, contract_generation_id=generation_id))


if __name__ == "__main__":
    unittest.main()
