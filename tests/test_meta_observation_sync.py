"""Scheduled Meta observation sync on the SQLite path (#107, #101 S1).

A ``customer_sandbox`` daily contract created through the API gets an active
schedule at the next 04:00 in its time zone. Once that slot is due, the
deployed ``WorkerRuntime`` alone takes it to evidence:

* the main lane dispatches one ``meta_observation_tick`` per
  ``(connection, fire time)`` (on SQLite through ``dispatch_schedules``);
* the tick advances the schedule and opens one Operation and one sync run for
  D, the fire time's local date minus one, then enqueues the day's
  ``meta_observation_sync`` batch;
* the batch pages the sandbox's read-only insights, records every page with
  its intent, outcome and fetch attempt, and commits the terminal page with
  the run's success, publication, attestation and coverage of D.

A daily contract made through the API is not built from a Scale read
contract, so it reads the one 14-day daily instance (occurrence
``obs:{generation}:{D}:daily``). The sandbox follows its timeline (#101 S2):
every ad is paused one day and idle one day in every 14, and those days have
no row. ``tests/test_meta_scale_observation_sync.py`` covers the Scale
contract set's several instances per day.

Replays change nothing, missed slots run only the latest one, a tick the
claim rejects as ``authorization_epoch_stale`` is dispatched again under the
new epoch instead of stalling its schedule, a Worker that dies after page 1
resumes at page 2, a controlled-mock read fault fails the run without
evidence, and retired connections or generations are never read again.
``tests/test_meta_observation_sync_pg.py`` runs the same path with the real
roles under FORCE RLS.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import time
import unittest
import uuid
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from app.adapters.meta.fake_adapter import (
    FakeMetaAdapter,
    InsightsRequestError,
    _decode_cursor,
)
from app.adapters.persistence.database import engine, platform_session, tenant_session
from app.adapters.persistence.models import AuditEventRecord, JobRecord, WorkspaceRecord, utcnow
from app.main import app
from app.meta import observation_sync
from app.meta.connections import create_connection
from app.meta.evidence import current_attestation
from app.meta.models import (
    EvidenceAttestationRecord,
    EvidencePublicationRecord,
    FakeMetaObjectRecord,
    MetaCallIntentRecord,
    MetaCallOutcomeRecord,
    MetaConnectionRecord,
    MetaEvidencePageRecord,
    MetaObservationSyncRunRecord,
    MetaObservationSyncScheduleRecord,
    MetaPageFetchAttemptRecord,
    ObservationContractGenerationRecord,
    ObservationCoverageLedgerRecord,
    ScenarioVersionRecord,
)
from app.meta.observation import (
    ContractError,
    DEFAULT_SYNC_CRON,
    create_contract_generation,
    ensure_observation_schedule,
    latest_fire_at_or_before,
    next_fire_after,
    observation_target_date,
    parse_daily_cron,
    retire_contract_generation,
)
from app.platform import jobs
from app.platform.http_identity import request_tenant_context
from app.platform.operations import OperationRecord
from app.platform.tenancy import TenantContext
from app.worker_runtime import WorkerRuntime, meta_write_capability_findings
from app.adapters.meta import sandbox_timeline
from tests.support.meta_observation import (
    AD_ACCOUNT,
    DAILY_FIELDS,
    TIMEZONE,
    create_through_api,
    daily_contract_body,
    seed_sandbox_ads,
    worker_context,
)
from tests.support.queue_hygiene import cancel_workspace_jobs, quarantine_foreign_jobs

LOS_ANGELES = ZoneInfo(TIMEZONE)


class _WorkerKilled(BaseException):
    """Stands in for SIGKILL: nothing in the Worker catches it."""


# ---------------------------------------------------------------------------
# Daily cron
# ---------------------------------------------------------------------------


class DailyCronTests(unittest.TestCase):
    def test_only_the_daily_form_is_accepted(self):
        self.assertEqual(parse_daily_cron("0 4 * * *"), (0, 4))
        self.assertEqual(parse_daily_cron("30 23 * * *"), (30, 23))
        for expression in ("*/15 * * * *", "0 4 * * 1", "0 4 1 * *", "60 4 * * *", "0 24 * * *",
                           "0 4 * *", "", " 0 4 * * *", "0 4 * * * *", "-1 4 * * *"):
            with self.subTest(expression=expression), self.assertRaises(ContractError) as raised:
                parse_daily_cron(expression)
            self.assertEqual(raised.exception.code, "meta_schedule_cron_invalid")

    def test_next_slot_is_computed_in_the_generations_time_zone(self):
        # 03:59 and 04:00 local on 2026-11-10 (PST, UTC-8).
        before = datetime(2026, 11, 10, 11, 59, tzinfo=timezone.utc)
        at = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(next_fire_after(DEFAULT_SYNC_CRON, TIMEZONE, before), at)
        self.assertEqual(next_fire_after(DEFAULT_SYNC_CRON, TIMEZONE, at), at + timedelta(days=1))
        self.assertEqual(next_fire_after(DEFAULT_SYNC_CRON, "Asia/Shanghai", at),
                         datetime(2026, 11, 10, 20, 0, tzinfo=timezone.utc))
        self.assertEqual(latest_fire_at_or_before(DEFAULT_SYNC_CRON, TIMEZONE, before), at - timedelta(days=1))
        self.assertEqual(latest_fire_at_or_before(DEFAULT_SYNC_CRON, TIMEZONE, at), at)

    def test_the_slot_follows_daylight_saving_time(self):
        # 2026-03-08: Los Angeles moves from UTC-8 to UTC-7 at 02:00.
        saturday = datetime(2026, 3, 7, 12, 0, tzinfo=timezone.utc)
        sunday = next_fire_after(DEFAULT_SYNC_CRON, TIMEZONE, saturday)
        self.assertEqual(sunday, datetime(2026, 3, 8, 11, 0, tzinfo=timezone.utc))
        self.assertEqual(sunday.astimezone(LOS_ANGELES).hour, 4)
        self.assertEqual(sunday - saturday, timedelta(hours=23))

    def test_target_day_is_the_fire_times_local_date_minus_one(self):
        fire_at = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)  # 04:00 PST
        self.assertEqual(observation_target_date(fire_at, TIMEZONE), date(2026, 11, 9))
        self.assertEqual(observation_target_date(fire_at, "Asia/Shanghai"), date(2026, 11, 9))
        self.assertEqual(observation_target_date(fire_at - timedelta(hours=5), "Asia/Shanghai"),
                         date(2026, 11, 9))

    def test_an_unknown_time_zone_is_refused(self):
        with self.assertRaises(ContractError) as raised:
            next_fire_after(DEFAULT_SYNC_CRON, "Mars/Olympus_Mons", utcnow())
        self.assertEqual(raised.exception.code, "meta_contract_timezone_invalid")


# ---------------------------------------------------------------------------
# The Worker path
# ---------------------------------------------------------------------------


class ObservationSyncTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        db_file = engine.url.database
        if db_file and db_file != ":memory:":
            Path(db_file).parent.mkdir(parents=True, exist_ok=True)
        command.upgrade(Config(str(Path(__file__).resolve().parents[1] / "alembic.ini")), "head")

    def setUp(self) -> None:
        self.workspace_id = f"ws_meta_obs_{uuid.uuid4().hex[:12]}"
        self._provision(self.workspace_id)
        self.owner = TenantContext(workspace_id=self.workspace_id, role="owner", initiator_id="usr_acme_owner")
        self.current = self.owner
        app.dependency_overrides[request_tenant_context] = lambda: self.current
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.addCleanup(self._quiesce, self.workspace_id)
        self.runtime = WorkerRuntime(owner=f"ci-only-meta-obs-{uuid.uuid4().hex[:6]}")

    # -- fixtures -----------------------------------------------------------

    def _provision(self, workspace_id: str, **columns) -> None:
        with platform_session() as session:
            session.add(WorkspaceRecord(id=workspace_id, name=f"Acme Charge {workspace_id[-4:]}", **columns))

    def _quiesce(self, workspace_id: str) -> None:
        """Leave no due schedule or open Job for a later test's Worker."""
        with platform_session() as session:
            session.execute(
                update(MetaObservationSyncScheduleRecord)
                .where(MetaObservationSyncScheduleRecord.workspace_id == workspace_id,
                       MetaObservationSyncScheduleRecord.state == "active")
                .values(state="paused")
            )
        cancel_workspace_jobs(workspace_id)

    def _sandbox(self, *, ads: int = 0, meta_mode: str = "customer_sandbox") -> tuple[dict, dict]:
        connection, generation = create_through_api(self, self.client, meta_mode)
        if ads:
            seed_sandbox_ads(worker_context(self.workspace_id, meta_mode), connection["id"], ads)
        return connection, generation

    def _schedule(self, generation_id: str) -> MetaObservationSyncScheduleRecord | None:
        with platform_session() as session:
            row = session.scalar(select(MetaObservationSyncScheduleRecord).where(
                MetaObservationSyncScheduleRecord.contract_generation_id == generation_id))
            if row is not None:
                session.expunge(row)
            return row

    @staticmethod
    def _utc(moment: datetime | None) -> datetime | None:
        return moment if moment is None or moment.tzinfo else moment.replace(tzinfo=timezone.utc)

    def _make_due(self, generation_id: str, fire_at: datetime | None = None) -> datetime:
        """Move the schedule's slot to the latest one already past."""
        fire_at = fire_at or latest_fire_at_or_before(DEFAULT_SYNC_CRON, TIMEZONE, utcnow())
        with platform_session() as session:
            session.execute(
                update(MetaObservationSyncScheduleRecord)
                .where(MetaObservationSyncScheduleRecord.contract_generation_id == generation_id)
                .values(next_fire_at=fire_at)
            )
        return fire_at

    def _drain(self, *, limit: int = 20, release_retries: bool = False) -> list[str]:
        """Run the deployed Worker's main lane until it finds nothing to do."""
        handled = []
        for _ in range(limit):
            quarantine_foreign_jobs(self.workspace_id)
            if release_retries:
                with platform_session() as session:
                    session.execute(
                        update(JobRecord)
                        .where(JobRecord.workspace_id == self.workspace_id, JobRecord.state == "retry_wait")
                        .values(next_attempt_at=utcnow() - timedelta(seconds=1))
                    )
            if not asyncio.run(self.runtime.run_once()):
                return handled
            handled.append("job")
        self.fail("the Worker did not go idle")

    def _jobs(self, job_type: str | None = None) -> list[JobRecord]:
        with platform_session() as session:
            stmt = select(JobRecord).where(JobRecord.workspace_id == self.workspace_id).order_by(JobRecord.created_at)
            if job_type:
                stmt = stmt.where(JobRecord.type == job_type)
            rows = list(session.scalars(stmt))
            for row in rows:
                session.expunge(row)
            return rows

    def _rows(self, model, **filters) -> list:
        with platform_session() as session:
            stmt = select(model).where(model.workspace_id == self.workspace_id).filter_by(**filters)
            rows = list(session.scalars(stmt))
            for row in rows:
                session.expunge(row)
            return rows

    def _count(self, model, **filters) -> int:
        return len(self._rows(model, **filters))

    def _ads(self) -> list[dict]:
        return [{"remote_object_id": row.remote_object_id}
                for row in self._rows(FakeMetaObjectRecord, object_type="ad")]


class ScheduleLifecycleTests(ObservationSyncTestCase):
    def test_a_sandbox_daily_contract_gets_an_active_schedule_at_the_next_local_0400(self):
        before = utcnow()
        connection, generation = self._sandbox()
        schedule = self._schedule(generation["id"])

        self.assertIsNotNone(schedule)
        self.assertEqual((schedule.state, schedule.cron_expression, schedule.meta_mode,
                          schedule.meta_connection_id, schedule.last_fired_at),
                         ("active", "0 4 * * *", "customer_sandbox", connection["id"], None))
        fire_at = self._utc(schedule.next_fire_at)
        self.assertEqual(fire_at, next_fire_after(DEFAULT_SYNC_CRON, TIMEZONE, before))
        local = fire_at.astimezone(LOS_ANGELES)
        self.assertEqual((local.hour, local.minute), (4, 0))
        self.assertTrue(before < fire_at <= before + timedelta(days=1))

    def test_creating_the_schedule_again_returns_the_same_row(self):
        _connection, generation = self._sandbox()
        context = worker_context(self.workspace_id, "customer_sandbox")
        with tenant_session(context) as session:
            row = session.get(ObservationContractGenerationRecord, generation["id"])
            first = ensure_observation_schedule(session, row).id
            second = ensure_observation_schedule(session, row).id
        self.assertEqual(first, second)
        self.assertEqual(self._count(MetaObservationSyncScheduleRecord), 1)

    def test_controlled_mock_and_other_contract_types_are_not_scheduled(self):
        _mock_connection, mock_generation = create_through_api(self, self.client, "controlled_mock")
        self.assertIsNone(self._schedule(mock_generation["id"]), "controlled_mock is explicit only")

        sandbox_connection, _daily = self._sandbox()
        response = self.client.post("/api/v1/meta/contracts", json=daily_contract_body(
            sandbox_connection["id"], "customer_sandbox", contract_type="delivery_configuration"))
        self.assertEqual(response.status_code, 201, response.text)
        self.assertIsNone(self._schedule(response.json()["id"]),
                          "only a Scale read contract of this type has a read plan (S2)")
        self.assertEqual(self._count(MetaObservationSyncScheduleRecord), 1, "only the sandbox daily contract")

    def test_an_internal_validation_generation_is_not_scheduled(self):
        platform_id = f"ws_meta_obs_platform_{uuid.uuid4().hex[:8]}"
        self._provision(platform_id, is_platform_workspace=True)
        self.addCleanup(self._quiesce, platform_id)
        context = replace(worker_context(platform_id, "internal_validation"), is_platform_workspace=True)
        with tenant_session(context) as session:
            session.add(MetaConnectionRecord(id=f"mcon_{uuid.uuid4().hex[:12]}", workspace_id=platform_id,
                                             meta_mode="internal_validation", ad_account_id=AD_ACCOUNT))
            session.flush()
            connection_id = session.scalars(select(MetaConnectionRecord.id).where(
                MetaConnectionRecord.workspace_id == platform_id)).one()
            generation = create_contract_generation(
                session, workspace_id=platform_id, meta_mode="internal_validation",
                connection_id=connection_id, contract_type="daily", ad_account_id=AD_ACCOUNT,
                fields=DAILY_FIELDS, attribution_window={}, breakdowns=[], time_window={},
                local_timezone=TIMEZONE,
            )
            generation_id = generation.id
        self.assertIsNone(self._schedule(generation_id), "internal_validation waits for S6")

    def test_a_sandbox_daily_contract_in_an_unknown_time_zone_is_refused(self):
        connection = self.client.post("/api/v1/meta/connections",
                                      json={"ad_account_id": AD_ACCOUNT, "meta_mode": "customer_sandbox"}).json()
        response = self.client.post("/api/v1/meta/contracts", json=daily_contract_body(
            connection["id"], "customer_sandbox", timezone="Mars/Olympus_Mons"))
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["code"], "meta_contract_timezone_invalid")
        self.assertEqual(self._count(ObservationContractGenerationRecord), 0, "nothing half-created")
        self.assertEqual(self._count(MetaObservationSyncScheduleRecord), 0)

    def test_retiring_the_connection_pauses_its_schedule_and_stops_dispatch(self):
        connection, generation = self._sandbox()
        self._make_due(generation["id"])
        response = self.client.post(f"/api/v1/meta/connections/{connection['id']}/retire",
                                    json={"meta_mode": "customer_sandbox"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["state"], "retired")
        self.assertEqual(self._schedule(generation["id"]).state, "paused")

        # Even a schedule left active on a retired connection is never dispatched.
        with platform_session() as session:
            session.execute(update(MetaObservationSyncScheduleRecord)
                            .where(MetaObservationSyncScheduleRecord.workspace_id == self.workspace_id)
                            .values(state="active"))
        self.assertEqual(observation_sync.dispatch_due_observation_ticks(), 0)
        self.assertEqual(self._jobs(), [])

    def test_retiring_the_generation_retires_its_schedule_and_stops_dispatch(self):
        _connection, generation = self._sandbox()
        self._make_due(generation["id"])
        with tenant_session(worker_context(self.workspace_id, "customer_sandbox")) as session:
            retired = retire_contract_generation(session, generation_id=generation["id"])
            self.assertEqual(retired.state, "retired")
            retire_contract_generation(session, generation_id=generation["id"])  # idempotent
        self.assertEqual(self._schedule(generation["id"]).state, "retired")

        with platform_session() as session:
            session.execute(update(MetaObservationSyncScheduleRecord)
                            .where(MetaObservationSyncScheduleRecord.workspace_id == self.workspace_id)
                            .values(state="active"))
        self.assertEqual(observation_sync.dispatch_due_observation_ticks(), 0)

    def test_a_suspended_workspace_is_not_dispatched(self):
        _connection, generation = self._sandbox()
        self._make_due(generation["id"])
        with platform_session() as session:
            session.get(WorkspaceRecord, self.workspace_id).suspended = True
        self.assertEqual(observation_sync.dispatch_due_observation_ticks(), 0)
        with platform_session() as session:
            session.get(WorkspaceRecord, self.workspace_id).suspended = False
        self.assertEqual(observation_sync.dispatch_due_observation_ticks(), 1)


class WorkerPathTests(ObservationSyncTestCase):
    def test_the_worker_alone_takes_a_due_schedule_to_attested_evidence(self):
        connection, generation = self._sandbox(ads=3)  # 3 ads x 12 delivering days = 36 rows, two pages
        fire_at = self._make_due(generation["id"])
        target = observation_target_date(fire_at, TIMEZONE)

        self._drain()

        tick, = self._jobs(observation_sync.TICK_JOB_TYPE)
        batch, = self._jobs(observation_sync.SYNC_JOB_TYPE)
        self.assertEqual((tick.state, batch.state), ("succeeded", "succeeded"))
        self.assertEqual(tick.payload_json, {"connection_id": connection["id"], "meta_mode": "customer_sandbox",
                                             "fire_at": observation_sync.fire_at_text(fire_at)})
        self.assertEqual(tick.idempotency_key, observation_sync.tick_idempotency_key(connection["id"], fire_at, authorization_epoch=0))
        self.assertTrue(tick.safe_to_retry_after_crash)
        self.assertEqual(batch.payload_json, {"connection_id": connection["id"], "meta_mode": "customer_sandbox",
                                              "target_date": target.isoformat(), "run_generation": 1})
        self.assertEqual(batch.idempotency_key, f"meta-obs-sync:{connection['id']}:{target.isoformat()}:1")
        self.assertLess(tick.created_at, batch.created_at)

        schedule = self._schedule(generation["id"])
        self.assertEqual(self._utc(schedule.last_fired_at), fire_at)
        self.assertEqual(self._utc(schedule.next_fire_at), next_fire_after(DEFAULT_SYNC_CRON, TIMEZONE, fire_at))

        run, = self._rows(MetaObservationSyncRunRecord)
        self.assertEqual((run.state, run.occurrence_id, run.run_generation, run.total_pages, run.total_rows,
                          run.last_completed_page_order, run.output_cursor),
                         ("succeeded", f"obs:{generation['id']}:{target.isoformat()}:daily", 1, 2, 36, 2, None))
        pages = sorted(self._rows(MetaEvidencePageRecord), key=lambda page: page.page_order)
        self.assertEqual([(p.page_order, p.row_count, p.is_terminal) for p in pages],
                         [(1, 25, False), (2, 11, True)])
        self.assertEqual(pages[1].input_cursor, pages[0].output_cursor)
        rows = [row for page in pages for row in json.loads(gzip.decompress(page.payload_compressed))["data"]]
        window = [target - timedelta(days=n) for n in range(14)]
        self.assertEqual({(row["ad_id"], row["date_start"]) for row in rows},
                         {(ad["remote_object_id"], day.isoformat()) for ad in self._ads() for day in window
                          if sandbox_timeline.day_state(ad["remote_object_id"], day) == sandbox_timeline.DELIVERING},
                         "a paused or idle day has no row")
        self.assertEqual(set(rows[0]) - {"account_id", "ad_id", "date_start", "date_stop"}, set(DAILY_FIELDS))

        intents = self._rows(MetaCallIntentRecord)
        self.assertEqual({(i.call_type, i.meta_api_method, i.write_permitted, i.network_dispatched_at)
                          for i in intents}, {("page_fetch", "GET", False, None)})
        self.assertEqual(len(intents), 2)
        self.assertEqual(intents[0].request_parameters["time_range"],
                         {"since": window[-1].isoformat(), "until": target.isoformat()})
        self.assertEqual(intents[0].request_parameters["contract"], {"instance": "daily"},
                         "an API-made daily contract is not a Scale read contract")
        outcomes = self._rows(MetaCallOutcomeRecord)
        self.assertEqual({(o.network_attempted, o.http_status, o.error_category) for o in outcomes},
                         {(False, None, None)}, "the sandbox reads this database, not the network")
        attempts = self._rows(MetaPageFetchAttemptRecord)
        self.assertEqual(sorted((a.page_order, a.attempt_number, a.state) for a in attempts),
                         [(1, 1, "succeeded"), (2, 1, "succeeded")])

        publication, = self._rows(EvidencePublicationRecord)
        attestation, = self._rows(EvidenceAttestationRecord)
        coverage, = self._rows(ObservationCoverageLedgerRecord)
        self.assertEqual((publication.sync_run_id, publication.state, publication.operation_id),
                         (run.id, "published", run.operation_id))
        self.assertEqual((attestation.state, attestation.publication_id, attestation.evidence_watermark),
                         ("valid", publication.id,
                          f"[{(target - timedelta(days=13)).isoformat()},{target.isoformat()}]"))
        self.assertEqual((coverage.coverage_date, coverage.sync_run_id, coverage.attestation_id),
                         (target, run.id, attestation.id))
        with tenant_session(worker_context(self.workspace_id, "customer_sandbox")) as session:
            self.assertEqual(current_attestation(session, contract_generation_id=generation["id"]).id,
                             attestation.id)
            self.assertIsNotNone(session.get(ObservationContractGenerationRecord,
                                             generation["id"]).clock_started_at)

        operation, = self._rows(OperationRecord)
        self.assertEqual((operation.command_type, operation.state, operation.result_resource_type,
                          operation.result_resource_id), ("meta.observation_sync", "succeeded",
                                                          "meta_sync_run", run.id))
        events = {e.event_type for e in self._rows(AuditEventRecord)}
        self.assertLessEqual({"job.enqueued", "evidence.published"}, events)
        self.assertEqual(meta_write_capability_findings(self.runtime), [], "no write port was left behind")

    def test_a_sandbox_without_ads_publishes_an_observed_empty_day(self):
        _connection, generation = self._sandbox()
        self._make_due(generation["id"])
        self._drain()
        run, = self._rows(MetaObservationSyncRunRecord)
        page, = self._rows(MetaEvidencePageRecord)
        self.assertEqual((run.state, run.total_pages, run.total_rows), ("succeeded", 1, 0))
        self.assertEqual((page.page_order, page.row_count, page.is_terminal), (1, 0, True))
        self.assertEqual(self._count(EvidenceAttestationRecord), 1)
        self.assertEqual(self._count(ObservationCoverageLedgerRecord), 1)

    def test_replayed_dispatch_and_ticks_change_nothing(self):
        connection, generation = self._sandbox(ads=1)
        fire_at = self._make_due(generation["id"])
        self.assertEqual(observation_sync.dispatch_due_observation_ticks(), 1)
        self.assertEqual(observation_sync.dispatch_due_observation_ticks(), 0, "the same slot, the same Job")
        self.assertEqual(len(self._jobs(observation_sync.TICK_JOB_TYPE)), 1)

        self._drain()
        self.assertEqual(observation_sync.dispatch_due_observation_ticks(), 0, "the slot moved on")
        context = worker_context(self.workspace_id, "customer_sandbox")
        with tenant_session(context) as session:
            replay = observation_sync.run_observation_tick(
                session, context, connection_id=connection["id"], fire_at=fire_at)
        self.assertEqual(replay.status, "not_due")
        self.assertEqual(observation_sync.run_observation_sync(
            context, connection_id=connection["id"], target_date=observation_target_date(fire_at, TIMEZONE),
            final_attempt=False).succeeded, (self._rows(MetaObservationSyncRunRecord)[0].id,))
        counts = [self._count(model) for model in (MetaObservationSyncRunRecord, OperationRecord,
                                                   MetaEvidencePageRecord, EvidencePublicationRecord,
                                                   EvidenceAttestationRecord, ObservationCoverageLedgerRecord)]
        self.assertEqual(counts, [1, 1, 1, 1, 1, 1])
        self.assertEqual(len(self._jobs()), 2)

    def test_after_an_outage_only_the_latest_slot_runs(self):
        _connection, generation = self._sandbox()
        latest = latest_fire_at_or_before(DEFAULT_SYNC_CRON, TIMEZONE, utcnow())
        self._make_due(generation["id"], latest - timedelta(days=3))

        self._drain()

        ticks = self._jobs(observation_sync.TICK_JOB_TYPE)
        self.assertEqual([tick.payload_json["fire_at"] for tick in ticks],
                         [observation_sync.fire_at_text(latest - timedelta(days=3)),
                          observation_sync.fire_at_text(latest)])
        run, = self._rows(MetaObservationSyncRunRecord)
        self.assertEqual(run.occurrence_id,
                         f"obs:{generation['id']}:{observation_target_date(latest, TIMEZONE).isoformat()}:daily")
        self.assertEqual(self._utc(self._schedule(generation["id"]).next_fire_at),
                         next_fire_after(DEFAULT_SYNC_CRON, TIMEZONE, latest))

    def test_a_worker_killed_after_page_one_resumes_at_page_two(self):
        _connection, generation = self._sandbox(ads=3)
        self._make_due(generation["id"])
        real_read = FakeMetaAdapter.read_insights

        def killed_on_page_two(adapter, **kwargs):
            if kwargs.get("after") is not None:
                raise _WorkerKilled()
            return real_read(adapter, **kwargs)

        with mock.patch.object(FakeMetaAdapter, "read_insights", killed_on_page_two):
            with self.assertRaises(_WorkerKilled):
                self._drain()
        batch, = self._jobs(observation_sync.SYNC_JOB_TYPE)
        run, = self._rows(MetaObservationSyncRunRecord)
        self.assertEqual((batch.state, run.state, run.last_completed_page_order), ("running", "running", 1))

        with platform_session() as session:  # the dead Worker's lease runs out
            session.execute(update(JobRecord).where(JobRecord.id == batch.id)
                            .values(lease_expires_at=utcnow() - timedelta(seconds=1)))
        self._drain()

        run, = self._rows(MetaObservationSyncRunRecord)
        self.assertEqual((run.state, run.total_pages, run.total_rows), ("succeeded", 2, 36))
        self.assertEqual(sorted(p.page_order for p in self._rows(MetaEvidencePageRecord)), [1, 2])
        self.assertEqual(sorted((a.page_order, a.attempt_number, a.state)
                                for a in self._rows(MetaPageFetchAttemptRecord)),
                         [(1, 1, "succeeded"), (2, 1, "started"), (2, 2, "succeeded")],
                         "the killed attempt stays as evidence; page 1 was not fetched again")
        self.assertEqual(self._jobs(observation_sync.SYNC_JOB_TYPE)[0].state, "succeeded")
        self.assertEqual(self._count(EvidenceAttestationRecord), 1)

    def test_a_controlled_mock_read_fault_fails_the_run_without_evidence(self):
        context = worker_context(self.workspace_id, "controlled_mock")
        owner = replace(self.owner, meta_mode="controlled_mock")
        with tenant_session(owner) as session:
            connection = create_connection(session, context=owner, ad_account_id=AD_ACCOUNT, scenario_config={
                "faults": {"rules": [{"operation": "read_insights", "effect": "read_error", "match_page": 1}]},
            })
            connection_id = connection.id
        response = self.client.post(f"/api/v1/meta/connections/{connection_id}/bindings",
                                    json={"ad_account_id": AD_ACCOUNT, "meta_mode": "controlled_mock"})
        self.assertEqual(response.status_code, 201, response.text)
        response = self.client.post("/api/v1/meta/contracts", json=daily_contract_body(connection_id,
                                                                                    "controlled_mock"))
        self.assertEqual(response.status_code, 201, response.text)
        generation_id = response.json()["id"]
        self.assertIsNone(self._schedule(generation_id))
        with tenant_session(context) as session:  # the explicit test entry (#101 §6)
            ensure_observation_schedule(session, session.get(ObservationContractGenerationRecord, generation_id))
        self._make_due(generation_id)

        self._drain(release_retries=True)

        batch, = self._jobs(observation_sync.SYNC_JOB_TYPE)
        self.assertEqual((batch.state, batch.error_code, batch.attempt), ("failed", "meta_observation_sync_failed", 3))
        run, = self._rows(MetaObservationSyncRunRecord)
        self.assertEqual(run.state, "failed")
        self.assertIsNotNone(run.completed_at)
        self.assertEqual(self._count(MetaEvidencePageRecord), 0)
        self.assertEqual(self._count(EvidencePublicationRecord), 0)
        self.assertEqual(self._count(EvidenceAttestationRecord), 0)
        self.assertEqual(self._count(ObservationCoverageLedgerRecord), 0)
        self.assertEqual({(o.error_category, o.http_status) for o in self._rows(MetaCallOutcomeRecord)},
                         {("adapter_read_failed", None)})
        self.assertEqual(sorted((a.attempt_number, a.state) for a in self._rows(MetaPageFetchAttemptRecord)),
                         [(1, "failed"), (2, "failed"), (3, "failed")])
        operation, = self._rows(OperationRecord)
        self.assertEqual(operation.state, "failed")
        self.assertIn("meta.sync_run.failed", {e.event_type for e in self._rows(AuditEventRecord)})
        with tenant_session(context) as session:
            self.assertIsNone(current_attestation(session, contract_generation_id=generation_id))

    def test_the_main_lane_dispatches_at_most_once_a_minute_busy_or_idle(self):
        busy = mock.Mock(id="job_ci_only_busy", type="ci_only_unknown", payload={}, attempt=1, max_attempts=1,
                         lease_owner=self.runtime.owner, context=self.owner)
        with mock.patch.object(observation_sync, "dispatch_due_observation_ticks", return_value=0) as dispatch, \
             mock.patch("app.platform.jobs.claim_next", return_value=busy), \
             mock.patch("app.platform.jobs.fail"):
            started = time.monotonic()
            for _ in range(3):  # a queue that never drains
                self.assertTrue(asyncio.run(self.runtime.run_once()))
            self.assertEqual(dispatch.call_count, 1, "once per interval, busy or not")
            self.assertGreaterEqual(self.runtime._next_observation_dispatch_at - started,
                                    observation_sync.DISPATCH_INTERVAL_SECONDS)
            self.runtime._next_observation_dispatch_at = time.monotonic() - 1  # the minute is up
            asyncio.run(self.runtime.run_once())
            self.assertEqual(dispatch.call_count, 2)
        with mock.patch.object(observation_sync, "dispatch_due_observation_ticks") as dispatch, \
             mock.patch("app.platform.jobs.claim_next", return_value=None):
            asyncio.run(WorkerRuntime(owner="ci-only-generation-lane").run_once(job_types=("generate_asset",)))
        dispatch.assert_not_called()

    def test_a_failing_dispatch_does_not_stop_the_lane(self):
        with mock.patch.object(observation_sync, "dispatch_due_observation_ticks",
                               side_effect=RuntimeError("dispatch broke")), \
             mock.patch("app.platform.jobs.claim_next", return_value=None) as claim:
            self.assertFalse(asyncio.run(self.runtime.run_once()))
        claim.assert_called_once()


class StaleAuthorizationEpochTests(ObservationSyncTestCase):
    """A tick rejected as ``authorization_epoch_stale`` must not stall its schedule.

    Only the tick moves ``next_fire_at``. When the Workspace's authorization
    epoch changes between dispatch and claim, the claim fails the tick, the
    slot stays due, and the next dispatch has to give it a tick the claim
    accepts -- or the connection is never read again.
    """

    def _bump_epoch(self) -> None:
        with platform_session() as session:  # a membership change, as the control plane makes it
            session.get(WorkspaceRecord, self.workspace_id).authorization_epoch += 1

    def _claim_tick(self) -> jobs.ClaimedJob | None:
        quarantine_foreign_jobs(self.workspace_id)
        return jobs.claim_next(self.runtime.owner, job_types=(observation_sync.TICK_JOB_TYPE,))

    def _ticks(self) -> list[tuple[str, str, int]]:
        return [(t.state, t.error_code, t.authorization_epoch) for t in self._jobs(observation_sync.TICK_JOB_TYPE)]

    def test_a_tick_rejected_for_a_stale_epoch_is_dispatched_again_and_its_slot_runs(self):
        connection, generation = self._sandbox(ads=1)
        fire_at = self._make_due(generation["id"])
        self.assertEqual(observation_sync.dispatch_due_observation_ticks(), 1)
        self._bump_epoch()
        self.assertIsNone(self._claim_tick(), "the claim rejects the tick")
        self.assertEqual(self._ticks(), [("failed", "authorization_epoch_stale", 0)])
        self.assertEqual(self._utc(self._schedule(generation["id"]).next_fire_at), fire_at, "still due")

        self.assertEqual(observation_sync.dispatch_due_observation_ticks(), 1, "the slot gets a new tick")
        self.assertEqual(observation_sync.dispatch_due_observation_ticks(), 0, "once per epoch")
        self._drain()

        schedule = self._schedule(generation["id"])
        self.assertEqual((self._utc(schedule.last_fired_at), self._utc(schedule.next_fire_at)),
                         (fire_at, next_fire_after(DEFAULT_SYNC_CRON, TIMEZONE, fire_at)))
        self.assertEqual(self._ticks(), [("failed", "authorization_epoch_stale", 0), ("succeeded", "", 1)])
        stale, fresh = self._jobs(observation_sync.TICK_JOB_TYPE)
        self.assertEqual(fresh.payload_json, stale.payload_json)
        self.assertEqual(
            (stale.idempotency_key, fresh.idempotency_key),
            (observation_sync.tick_idempotency_key(connection["id"], fire_at, authorization_epoch=0),
             observation_sync.tick_idempotency_key(connection["id"], fire_at, authorization_epoch=1)))
        run, = self._rows(MetaObservationSyncRunRecord)
        self.assertEqual(run.state, "succeeded")
        self.assertEqual(self._count(EvidenceAttestationRecord), 1)
        self.assertEqual(observation_sync.dispatch_due_observation_ticks(), 0, "the slot moved on")

    def test_an_epoch_change_before_the_claim_still_runs_the_slot_once(self):
        _connection, generation = self._sandbox(ads=1)
        fire_at = self._make_due(generation["id"])
        self.assertEqual(observation_sync.dispatch_due_observation_ticks(), 1)
        self._bump_epoch()
        self.assertEqual(observation_sync.dispatch_due_observation_ticks(), 1, "a tick the claim accepts")

        self._drain()

        self.assertEqual(self._ticks(), [("failed", "authorization_epoch_stale", 0), ("succeeded", "", 1)])
        self.assertEqual(self._utc(self._schedule(generation["id"]).last_fired_at), fire_at)
        self.assertEqual([run.state for run in self._rows(MetaObservationSyncRunRecord)], ["succeeded"])
        self.assertEqual(len(self._jobs(observation_sync.SYNC_JOB_TYPE)), 1)

    def test_a_fast_forward_tick_rejected_for_a_stale_epoch_is_dispatched_again(self):
        connection, generation = self._sandbox()
        latest = latest_fire_at_or_before(DEFAULT_SYNC_CRON, TIMEZONE, utcnow())
        missed = self._make_due(generation["id"], latest - timedelta(days=3))
        context = worker_context(self.workspace_id, "customer_sandbox")
        with tenant_session(context) as session:  # the Worker was down: the tick jumps to the latest slot
            jumped = observation_sync.run_observation_tick(
                session, context, connection_id=connection["id"], fire_at=missed)
        self.assertEqual((jumped.status, len(jumped.next_tick_job_ids)), ("fast_forwarded", 1))
        self._bump_epoch()
        self.assertIsNone(self._claim_tick())
        self.assertEqual(self._ticks(), [("failed", "authorization_epoch_stale", 0)])

        self.assertEqual(observation_sync.dispatch_due_observation_ticks(), 1)
        self._drain()

        run, = self._rows(MetaObservationSyncRunRecord)
        self.assertEqual((run.state, run.occurrence_id),
                         ("succeeded",
                          f"obs:{generation['id']}:{observation_target_date(latest, TIMEZONE).isoformat()}:daily"))
        self.assertEqual(self._utc(self._schedule(generation["id"]).next_fire_at),
                         next_fire_after(DEFAULT_SYNC_CRON, TIMEZONE, latest))


class TenantAndInputTests(ObservationSyncTestCase):
    def _other_workspace(self) -> str:
        other = f"ws_meta_obs_other_{uuid.uuid4().hex[:8]}"
        self._provision(other)
        self.addCleanup(self._quiesce, other)
        return other

    def test_a_tick_or_batch_in_another_workspace_touches_nothing_here(self):
        connection, generation = self._sandbox(ads=1)
        fire_at = self._make_due(generation["id"])
        other = worker_context(self._other_workspace(), "customer_sandbox")
        with tenant_session(other) as session:
            forged = observation_sync.run_observation_tick(
                session, other, connection_id=connection["id"], fire_at=fire_at)
        self.assertEqual(forged.status, "not_due")
        self.assertEqual(observation_sync.run_observation_sync(
            other, connection_id=connection["id"], target_date=observation_target_date(fire_at, TIMEZONE),
            final_attempt=True).run_ids, ())
        self.assertEqual(self._utc(self._schedule(generation["id"]).next_fire_at), fire_at, "not advanced")
        self.assertEqual(self._count(MetaObservationSyncRunRecord), 0)

        # Nor does the other mode of the same Workspace.
        mock_context = worker_context(self.workspace_id, "controlled_mock")
        with tenant_session(mock_context) as session:
            self.assertEqual(observation_sync.run_observation_tick(
                session, mock_context, connection_id=connection["id"], fire_at=fire_at).status, "not_due")

    def test_jobs_without_a_valid_mode_or_input_fail_as_missing_input(self):
        connection, generation = self._sandbox()
        fire_at = self._make_due(generation["id"])
        context = worker_context(self.workspace_id, "customer_sandbox")
        bad_payloads = [
            (observation_sync.TICK_JOB_TYPE, {"connection_id": connection["id"],
                                              "fire_at": observation_sync.fire_at_text(fire_at)}),
            (observation_sync.TICK_JOB_TYPE, {"connection_id": connection["id"], "meta_mode": "production",
                                              "fire_at": observation_sync.fire_at_text(fire_at)}),
            (observation_sync.TICK_JOB_TYPE, {"connection_id": connection["id"], "meta_mode": "customer_sandbox",
                                              "fire_at": "yesterday"}),
            (observation_sync.SYNC_JOB_TYPE, {"connection_id": connection["id"], "meta_mode": "customer_sandbox",
                                              "target_date": "not-a-date", "run_generation": 1}),
            (observation_sync.SYNC_JOB_TYPE, {"meta_mode": "customer_sandbox", "target_date": "2026-11-09"}),
        ]
        with platform_session() as session:  # only the forged Jobs are claimable
            session.execute(update(MetaObservationSyncScheduleRecord)
                            .where(MetaObservationSyncScheduleRecord.workspace_id == self.workspace_id)
                            .values(state="paused"))
        job_ids = []
        for job_type, payload in bad_payloads:
            with tenant_session(context) as session:
                job_ids.append(jobs.enqueue_in_session(
                    session, context=context, job_type=job_type, payload=payload,
                    idempotency_key=f"ci-only-{uuid.uuid4().hex}").id)
        self._drain()
        for job in self._jobs():
            self.assertIn(job.id, job_ids)
            self.assertEqual((job.state, job.error_code), ("failed", "missing_input"))
        self.assertEqual(self._count(MetaObservationSyncRunRecord), 0)


class FakeInsightsReadTests(ObservationSyncTestCase):
    def _adapter_session(self, ads: int):
        connection, _generation = self._sandbox()
        seed_sandbox_ads(worker_context(self.workspace_id, "customer_sandbox"), connection["id"], ads)
        return connection

    def _read_all(self, adapter, **request) -> list[dict]:
        rows, after = [], None
        while True:
            page = adapter.read_insights(after=after, **request)
            rows.extend(page.payload["data"])
            self.assertEqual(page.row_count, len(page.payload["data"]))
            if page.next_cursor is None:
                self.assertNotIn("next", page.payload["paging"])
                return rows
            after = page.next_cursor

    def test_reads_are_deterministic_paged_per_ad_and_day_and_write_nothing(self):
        connection = self._adapter_session(ads=3)
        context = worker_context(self.workspace_id, "customer_sandbox")
        request = dict(ad_account_id=AD_ACCOUNT, fields=["impressions", "spend", "reach"],
                       since=date(2026, 10, 27), until=date(2026, 11, 9), limit=10)
        with tenant_session(context) as session:
            scenario = session.query(ScenarioVersionRecord).filter_by(meta_connection_id=connection["id"]).one()
            adapter = FakeMetaAdapter(session, self.workspace_id, "customer_sandbox", scenario.id)
            objects = session.scalar(select(func.count()).select_from(FakeMetaObjectRecord))
            first, second = self._read_all(adapter, **request), self._read_all(adapter, **request)
            self.assertEqual(session.scalar(select(func.count()).select_from(FakeMetaObjectRecord)), objects)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 36, "3 ads x 12 delivering days of 14")
        self.assertEqual(len({(row["ad_id"], row["date_start"]) for row in first}), 36)
        self.assertEqual(set(first[0]), {"account_id", "ad_id", "date_start", "date_stop", "impressions", "spend"},
                         "a field the sandbox cannot answer is left out, not invented")
        for row in first:
            self.assertGreaterEqual(int(row["impressions"]), 500)

    def test_a_bad_request_is_refused(self):
        connection = self._adapter_session(ads=1)
        with tenant_session(worker_context(self.workspace_id, "customer_sandbox")) as session:
            scenario = session.query(ScenarioVersionRecord).filter_by(meta_connection_id=connection["id"]).one()
            adapter = FakeMetaAdapter(session, self.workspace_id, "customer_sandbox", scenario.id)
            for request in (dict(since=date(2026, 11, 9), until=date(2026, 11, 1)),
                            dict(since=date(2026, 11, 1), until=date(2026, 11, 2), after="not-a-cursor"),
                            dict(since=date(2026, 11, 1), until=date(2026, 11, 2), limit=0)):
                with self.subTest(request=request), self.assertRaises(InsightsRequestError):
                    adapter.read_insights(ad_account_id=AD_ACCOUNT, fields=["spend"], **request)
        self.assertEqual(_decode_cursor(None), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
