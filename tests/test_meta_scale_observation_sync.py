"""Scale's metric contract set and its scheduled reads on SQLite (#108, #101 S2).

Production code (``ensure_metric_contract_set``) builds, for the connection a
DeliveryTarget lives on, the daily, exact-window and delivery-configuration
generations from the code contracts and one
``ScaleMetricSemanticContractSetRecord`` over them -- idempotently, all on the
connection's one cron and time zone. Once the slot is due the deployed Worker
alone reads every query instance in one tick and one batch: five runs
(``daily``; ``current_7d``, ``baseline_7d``, ``rolling_14d``;
``configuration``), each with its own page chain and attestation, and the
stored pages parse into what the sandbox timeline says. A controlled-mock
fault fails one instance while the others finish; a drifted generation is
audited, not read; a #107 run without an instance still runs. Negatives:
another Workspace or mode, a Member, an internal_validation account without
its currency and time zone, a conflicting generation, a drifted set.
``tests/test_meta_scale_observation_sync_pg.py`` runs the main path under
the real roles.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import subprocess
import sys
import unittest
import uuid
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.adapters.meta import sandbox_timeline
from app.adapters.persistence.database import engine, platform_session, tenant_session
from app.adapters.persistence.models import AuditEventRecord, JobRecord, WorkspaceRecord, utcnow
from app.main import app
from app.meta import observation_sync
from app.meta.connections import create_connection
from app.meta.evidence import current_attestation
from app.meta.models import (
    EvidenceAttestationRecord,
    EvidencePublicationRecord,
    MetaCallIntentRecord,
    MetaEvidencePageRecord,
    MetaObservationSyncRunRecord,
    MetaObservationSyncScheduleRecord,
    ObservationContractGenerationRecord,
    ObservationCoverageLedgerRecord,
    ScenarioVersionRecord,
)
from app.meta.observation import (
    ContractError,
    DEFAULT_SYNC_CRON,
    create_contract_generation,
    ensure_observation_schedule,
    observation_read_plan,
    observation_target_date,
    retire_contract_generation,
)
from app.meta.scale_read_contracts import CONTRACTS, AccountBinding, generation_spec
from app.meta.scale_read_parsers import parse_daily, parse_delivery_configuration, parse_window
from app.meta.sync_pipeline import create_sync_run
from app.platform.http_identity import request_tenant_context
from app.platform.operations import OperationRecord, create_operation
from app.platform.runtime_role import API_ROLE, entered_as
from app.platform.tenancy import TenantContext
from app.scale.delivery_targets import (
    MetricContractSetError,
    create_delivery_target_with_contract_set,
    ensure_metric_contract_set,
)
from app.scale.enrollment import EnrollmentLifecycleError, _resolve_metric_semantic_contract_set_id
from app.scale.models import ScaleMetricSemanticContractSetMemberRecord, ScaleMetricSemanticContractSetRecord
from app.worker_runtime import WorkerRuntime, meta_write_capability_findings
from tests.support.meta_observation import AD_ACCOUNT, daily_contract_body, seed_sandbox_ads, worker_context
from tests.support.queue_hygiene import cancel_workspace_jobs, quarantine_foreign_jobs
from tests.support.scale_contract_set import (
    create_connection_through_api,
    delivery_target_request,
    sandbox_ad_hierarchy,
    seed_target_references,
)

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = "customer_sandbox"
MOCK = "controlled_mock"
TIMEZONE = "America/Los_Angeles"
INSTANCES = {"daily": ["daily"], "exact_window": ["current_7d", "baseline_7d", "rolling_14d"],
             "delivery_configuration": ["configuration"]}


class ScaleObservationTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        db_file = engine.url.database
        if db_file and db_file != ":memory:":
            Path(db_file).parent.mkdir(parents=True, exist_ok=True)
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")

    def setUp(self) -> None:
        self.workspace_id = self._provision("ws_scale_reads")
        self.owner = TenantContext(workspace_id=self.workspace_id, role="owner", initiator_id="usr_acme_owner")
        self.current = self.owner
        app.dependency_overrides[request_tenant_context] = lambda: self.current
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.runtime = WorkerRuntime(owner=f"ci-only-scale-reads-{uuid.uuid4().hex[:6]}")

    def _provision(self, prefix: str, **columns) -> str:
        workspace_id = f"{prefix}_{uuid.uuid4().hex[:12]}"
        with platform_session() as session:
            session.add(WorkspaceRecord(id=workspace_id, name=f"Acme Charge {workspace_id[-4:]}", **columns))
        self.addCleanup(self._quiesce, workspace_id)
        return workspace_id

    def _quiesce(self, workspace_id: str) -> None:
        with platform_session() as session:
            session.execute(update(MetaObservationSyncScheduleRecord)
                            .where(MetaObservationSyncScheduleRecord.workspace_id == workspace_id,
                                   MetaObservationSyncScheduleRecord.state == "active")
                            .values(state="paused"))
        cancel_workspace_jobs(workspace_id)

    # -- fixtures -----------------------------------------------------------

    def _connection(self, meta_mode: str = SANDBOX, *, scenario_config: dict | None = None) -> str:
        if scenario_config is None:
            self.current = replace(self.owner, meta_mode=None)
            return create_connection_through_api(self, self.client, meta_mode)["id"]
        owner = replace(self.owner, meta_mode=meta_mode)
        with tenant_session(owner) as session:
            connection_id = create_connection(session, context=owner, ad_account_id=AD_ACCOUNT,
                                              scenario_config=scenario_config).id
        response = self.client.post(f"/api/v1/meta/connections/{connection_id}/bindings",
                                    json={"ad_account_id": AD_ACCOUNT, "meta_mode": meta_mode})
        self.assertEqual(response.status_code, 201, response.text)
        return connection_id

    def _ensure(self, connection_id: str, meta_mode: str = SANDBOX, context: TenantContext | None = None, **kwargs):
        """The production entry, as the API role would run it."""
        context = replace(context or self.owner, meta_mode=meta_mode)
        with entered_as(API_ROLE), tenant_session(context) as session:
            return ensure_metric_contract_set(session, context, connection_id=connection_id, **kwargs)

    def _make_due(self, fire_at=None):
        with platform_session() as session:
            rows = list(session.scalars(select(MetaObservationSyncScheduleRecord).where(
                MetaObservationSyncScheduleRecord.workspace_id == self.workspace_id)))
            fire_at = fire_at or rows[0].next_fire_at - timedelta(days=1)
            session.execute(update(MetaObservationSyncScheduleRecord)
                            .where(MetaObservationSyncScheduleRecord.workspace_id == self.workspace_id)
                            .values(next_fire_at=fire_at))
        return fire_at if fire_at.tzinfo else fire_at.replace(tzinfo=utcnow().tzinfo)

    def _drain(self, *, release_retries: bool = False) -> int:
        handled = 0
        for _ in range(30):
            quarantine_foreign_jobs(self.workspace_id)
            if release_retries:
                with platform_session() as session:
                    session.execute(update(JobRecord)
                                    .where(JobRecord.workspace_id == self.workspace_id,
                                           JobRecord.state == "retry_wait")
                                    .values(next_attempt_at=utcnow() - timedelta(seconds=1)))
            if not asyncio.run(self.runtime.run_once()):
                return handled
            handled += 1
        self.fail("the Worker did not go idle")

    def _rows(self, model, workspace_id: str | None = None, **filters) -> list:
        with platform_session() as session:
            rows = list(session.scalars(select(model).where(model.workspace_id == (workspace_id or self.workspace_id))
                                        .filter_by(**filters)))
            for row in rows:
                session.expunge(row)
            return rows

    def _members(self, contract_set_id: str) -> dict[str, tuple[str, str]]:
        with platform_session() as session:
            return {row.contract_type: (row.observation_contract_generation, row.contract_hash)
                    for row in session.scalars(select(ScaleMetricSemanticContractSetMemberRecord).where(
                        ScaleMetricSemanticContractSetMemberRecord.contract_set_id == contract_set_id))}

    def _pages(self, run_id: str) -> list[tuple[dict, dict]]:
        """(request parameters, raw body) of a run's pages, in order -- what S3 will read."""
        pages = sorted(self._rows(MetaEvidencePageRecord, sync_run_id=run_id), key=lambda page: page.page_order)
        intents = {intent.id: intent for intent in self._rows(MetaCallIntentRecord)}
        return [(intents[page.intent_id].request_parameters, json.loads(gzip.decompress(page.payload_compressed)))
                for page in pages]


class ContractSetEstablishment(ScaleObservationTestCase):
    def test_every_required_generation_and_one_set_idempotently(self):
        connection_id = self._connection(scenario_config={"account": {"timezone_name": TIMEZONE}})
        first = self._ensure(connection_id)
        second = self._ensure(connection_id)

        self.assertTrue(first.created)
        self.assertEqual((second.contract_set_id, second.generation_ids, second.created),
                         (first.contract_set_id, first.generation_ids, False))
        generations = {row.contract_type: row for row in self._rows(ObservationContractGenerationRecord)}
        self.assertEqual(set(generations), {"daily", "exact_window", "delivery_configuration"},
                         "no breakdown family in the first batch")
        for kind, row in generations.items():
            plan = observation_read_plan(row)
            self.assertIs(plan.contract, CONTRACTS[kind])
            self.assertEqual(plan.binding, AccountBinding(AD_ACCOUNT, "USD", TIMEZONE))
            self.assertEqual([instance.name for instance in plan.instances], INSTANCES[kind])
        self.assertEqual(self._members(first.contract_set_id),
                         {kind: (row.id, row.contract_hash) for kind, row in generations.items()})
        contract_set, = self._rows(ScaleMetricSemanticContractSetRecord)
        self.assertEqual((contract_set.meta_mode, contract_set.created_by), (SANDBOX, "usr_acme_owner"))

        schedules = self._rows(MetaObservationSyncScheduleRecord)
        self.assertEqual(len(schedules), 3, "a customer_sandbox generation is scheduled as it is created")
        self.assertEqual({(s.state, s.cron_expression, s.next_fire_at) for s in schedules},
                         {("active", DEFAULT_SYNC_CRON, schedules[0].next_fire_at)},
                         "one cron, one time zone, one slot per connection: one batch per day")
        events = [e for e in self._rows(AuditEventRecord) if e.event_type == "scale.metric_contract_set.created"]
        self.assertEqual(len(events), 1)

    def test_a_delivery_target_binds_to_the_sets_configuration_generation(self):
        connection_id = self._connection()
        context = replace(self.owner, meta_mode=SANDBOX)
        [ad_id] = seed_sandbox_ads(worker_context(self.workspace_id, SANDBOX), connection_id, 1)
        with platform_session() as session:
            references = seed_target_references(session, self.workspace_id, "dt")
            scenario_id = session.scalar(select(ScenarioVersionRecord.id).where(
                ScenarioVersionRecord.meta_connection_id == connection_id))
            hierarchy = sandbox_ad_hierarchy(session, workspace_id=self.workspace_id, meta_mode=SANDBOX,
                                             connection_id=connection_id, ad_id=ad_id, as_of=utcnow().date())
        request = delivery_target_request(connection_id=connection_id, scenario_version_id=scenario_id,
                                          meta_mode=SANDBOX, hierarchy=hierarchy, references=references)
        with entered_as(API_ROLE), tenant_session(context) as session:
            target, contract_set = create_delivery_target_with_contract_set(session, context, request)
            self.assertEqual(target.contract_generation_id, contract_set.delivery_configuration_generation_id)
            self.assertEqual(_resolve_metric_semantic_contract_set_id(
                session, context, contract_generation_id=target.contract_generation_id),
                contract_set.contract_set_id, "what Enrollment activation resolves (spec §6.11 step 6)")
        with self.assertRaises(MetricContractSetError) as raised, entered_as(API_ROLE), \
                tenant_session(context) as session:
            create_delivery_target_with_contract_set(
                session, context, replace(request, contract_generation_id=contract_set.generation_ids["daily"]))
        self.assertEqual(raised.exception.code, "scale_contract_generation_conflict")

    def test_an_active_generation_that_is_not_the_contract_is_a_conflict(self):
        connection_id = self._connection()
        response = self.client.post("/api/v1/meta/contracts", json=daily_contract_body(connection_id, SANDBOX))
        self.assertEqual(response.status_code, 201, response.text)
        with self.assertRaises(MetricContractSetError) as raised:
            self._ensure(connection_id)
        self.assertEqual(raised.exception.code, "scale_contract_generation_conflict")
        self.assertEqual(len(self._rows(ObservationContractGenerationRecord)), 1, "nothing half-created")
        self.assertEqual(self._rows(ScaleMetricSemanticContractSetRecord), [])

    def test_a_drifted_set_is_not_joined_by_a_second_one(self):
        connection_id = self._connection()
        established = self._ensure(connection_id)
        with tenant_session(worker_context(self.workspace_id, SANDBOX)) as session:
            retire_contract_generation(session, generation_id=established.generation_ids["daily"])
        with self.assertRaises(MetricContractSetError) as raised:
            self._ensure(connection_id)
        self.assertEqual(raised.exception.code, "scale_contract_set_drift",
                         "activation would find two sets for one configuration generation")
        self.assertEqual(len(self._rows(ScaleMetricSemanticContractSetRecord)), 1)

    def test_schedules_on_one_connection_share_one_cron_and_time_zone(self):
        connection_id = self._connection(MOCK, scenario_config={})
        established = self._ensure(connection_id, MOCK)
        self.assertEqual(self._rows(MetaObservationSyncScheduleRecord), [], "controlled_mock is explicit only")
        context = worker_context(self.workspace_id, MOCK)
        with tenant_session(context) as session:
            daily = session.get(ObservationContractGenerationRecord, established.generation_ids["daily"])
            ensure_observation_schedule(session, daily)
        with self.assertRaises(ContractError) as raised, tenant_session(context) as session:
            window = session.get(ObservationContractGenerationRecord, established.generation_ids["exact_window"])
            ensure_observation_schedule(session, window, cron_expression="30 5 * * *")
        self.assertEqual(raised.exception.code, "meta_schedule_connection_mismatch")
        self.assertEqual(len(self._rows(MetaObservationSyncScheduleRecord)), 1)

    def test_breakdown_contracts_are_defined_but_not_scheduled(self):
        connection_id = self._connection()
        spec = generation_spec(CONTRACTS["breakdown_country"], AccountBinding(AD_ACCOUNT, "USD", "UTC"))
        context = worker_context(self.workspace_id, SANDBOX)
        with tenant_session(context) as session:
            generation = create_contract_generation(session, workspace_id=self.workspace_id, meta_mode=SANDBOX,
                                                    connection_id=connection_id, **spec)
            self.assertIsNone(observation_read_plan(generation), "the read policy declares no breakdown family")
        self.assertEqual(self._rows(MetaObservationSyncScheduleRecord), [])

    def test_internal_validation_needs_the_accounts_currency_and_time_zone(self):
        platform_id = self._provision("ws_scale_reads_platform", is_platform_workspace=True)
        owner = TenantContext(workspace_id=platform_id, role="owner", initiator_id="usr_acme_owner",
                              meta_mode="internal_validation", is_platform_workspace=True)
        with tenant_session(owner) as session:
            connection_id = create_connection(session, context=owner, ad_account_id=AD_ACCOUNT).id
        with self.assertRaises(MetricContractSetError) as raised:
            self._ensure(connection_id, "internal_validation", context=owner)
        self.assertEqual(raised.exception.code, "scale_contract_account_required")
        established = self._ensure(connection_id, "internal_validation", context=owner, currency="USD",
                                   timezone_name=TIMEZONE)
        self.assertTrue(established.created)
        self.assertEqual(self._rows(MetaObservationSyncScheduleRecord, platform_id), [], "waits for S6")

    def test_another_workspace_mode_or_a_member_cannot_establish_it(self):
        connection_id = self._connection()
        other = TenantContext(workspace_id=self._provision("ws_scale_reads_other"), role="owner",
                              initiator_id="usr_northpeak_owner")
        cases = {
            "another Workspace": (other, SANDBOX, "connection_not_found"),
            "another mode": (self.owner, MOCK, "connection_not_found"),
            "a Member": (replace(self.owner, role="member"), SANDBOX, "forbidden"),
        }
        for name, (context, meta_mode, code) in cases.items():
            with self.subTest(name), self.assertRaises(MetricContractSetError) as raised:
                self._ensure(connection_id, meta_mode, context=context)
            self.assertEqual(raised.exception.code, code)
        self.assertEqual(self._rows(ObservationContractGenerationRecord), [])
        established = self._ensure(connection_id)
        with tenant_session(replace(other, meta_mode=SANDBOX)) as session:  # RLS itself: the PG test
            with self.assertRaises(EnrollmentLifecycleError) as raised:
                _resolve_metric_semantic_contract_set_id(
                    session, replace(other, meta_mode=SANDBOX),
                    contract_generation_id=established.delivery_configuration_generation_id)
            self.assertEqual(raised.exception.code, "metric_contract_set_missing")


class ScheduledScaleReads(ScaleObservationTestCase):
    def _establish(self, meta_mode: str = SANDBOX, *, ads: int = 3, scenario_config: dict | None = None):
        connection_id = self._connection(meta_mode, scenario_config=scenario_config or {
            "account": {"timezone_name": TIMEZONE}})
        established = self._ensure(connection_id, meta_mode)
        context = worker_context(self.workspace_id, meta_mode)
        if meta_mode == MOCK:  # the explicit test entry (#101 §6)
            with tenant_session(context) as session:
                for generation_id in established.generation_ids.values():
                    ensure_observation_schedule(session, session.get(ObservationContractGenerationRecord,
                                                                     generation_id))
        ad_ids = seed_sandbox_ads(context, connection_id, ads)
        return connection_id, established, ad_ids

    def test_one_tick_reads_every_instance_and_attests_each(self):
        connection_id, established, ad_ids = self._establish()
        fire_at = self._make_due()
        target = observation_target_date(fire_at, TIMEZONE)

        self.assertEqual(self._drain(), 2, "one tick, one batch")

        tick, = self._rows(JobRecord, type=observation_sync.TICK_JOB_TYPE)
        batch, = self._rows(JobRecord, type=observation_sync.SYNC_JOB_TYPE)
        self.assertEqual((tick.state, batch.state), ("succeeded", "succeeded"))
        runs = {run.occurrence_id: run for run in self._rows(MetaObservationSyncRunRecord)}
        expected = {f"obs:{established.generation_ids[kind]}:{target.isoformat()}:{name}"
                    for kind, names in INSTANCES.items() for name in names}
        self.assertEqual(set(runs), expected)
        self.assertEqual({run.state for run in runs.values()}, {"succeeded"})
        self.assertEqual(len({run.operation_id for run in runs.values()}), 5, "one Operation per instance")

        attestations = {a.publication_id: a for a in self._rows(EvidenceAttestationRecord)}
        publications = {p.sync_run_id: p for p in self._rows(EvidencePublicationRecord)}
        windows = {}
        for occurrence, run in runs.items():
            attestation = attestations[publications[run.id].id]
            windows[occurrence.rsplit(":", 1)[1]] = attestation.evidence_watermark
            self.assertEqual((attestation.state, attestation.contract_generation_id),
                             ("valid", run.contract_generation_id))
        day = lambda offset: (target + timedelta(days=offset)).isoformat()  # noqa: E731
        self.assertEqual(windows, {
            "daily": f"[{day(-13)},{day(0)}]", "current_7d": f"[{day(-6)},{day(0)}]",
            "baseline_7d": f"[{day(-13)},{day(-7)}]", "rolling_14d": f"[{day(-13)},{day(0)}]",
            "configuration": f"[{day(0)},{day(0)}]",
        })
        coverage = self._rows(ObservationCoverageLedgerRecord)
        self.assertEqual(sorted((c.contract_generation_id, c.coverage_date) for c in coverage),
                         sorted((generation_id, target) for generation_id in established.generation_ids.values()),
                         "each generation covers D once, when all its instances succeeded")
        with tenant_session(worker_context(self.workspace_id, SANDBOX)) as session:
            self.assertIsNotNone(current_attestation(
                session, contract_generation_id=established.delivery_configuration_generation_id),
                "what Enrollment activation needs for the DeliveryTarget's generation")

        # Each instance has its own page chain; the configuration one walks its edges.
        configuration = runs[f"obs:{established.delivery_configuration_generation_id}:{target}:configuration"]
        config_pages = self._pages(configuration.id)
        self.assertEqual([request["edge"] for request, _body in config_pages],
                         ["account", "ads", "adsets", "campaigns"])
        self.assertEqual({request["contract"]["definition_hash"] for request, _body in config_pages},
                         {CONTRACTS["delivery_configuration"].contract_hash})
        intents = {i.request_parameters.get("edge"): i for i in self._rows(MetaCallIntentRecord)
                   if i.request_parameters.get("edge")}
        self.assertEqual((intents["account"].meta_api_endpoint, intents["ads"].meta_api_endpoint),
                         (f"/{AD_ACCOUNT}", f"/{AD_ACCOUNT}/ads"))
        window_pages = self._pages(runs[f"obs:{established.generation_ids['exact_window']}:{target}:baseline_7d"].id)
        request = window_pages[0][0]
        self.assertEqual((request["time_range"], request["time_increment"], request["attribution"],
                          request["contract"]["instance"]),
                         ({"since": day(-13), "until": day(-7)}, "all_days", CONTRACTS["daily"].attribution(),
                          "baseline_7d"))

        # The stored pages parse into what the sandbox timeline says (S3 reads them this way).
        binding = AccountBinding(AD_ACCOUNT, "USD", TIMEZONE)
        daily_bodies = [body for _r, body in self._pages(runs[f"obs:{established.generation_ids['daily']}:"
                                                              f"{target}:daily"].id)]
        window_bodies = [body for _r, body in window_pages]
        for ad_id in ad_ids:
            daily = parse_daily(daily_bodies, expected=binding, ad_id=ad_id,
                                since=target - timedelta(days=13), until=target)
            self.assertEqual([d.delivered for d in daily.days],
                             [sandbox_timeline.day_state(ad_id, d.local_day) == sandbox_timeline.DELIVERING
                              for d in daily.days])
            baseline = parse_window(window_bodies, expected=binding, ad_id=ad_id, window_role="baseline_7d",
                                    since=target - timedelta(days=13), until=target - timedelta(days=7))
            self.assertEqual(baseline.impressions,
                             sum(d.impressions for d in daily.days[:7]), "the window covers D-13..D-7")
            configured = parse_delivery_configuration(
                [(request["edge"], body) for request, body in config_pages], expected=binding, ad_id=ad_id)
            paused = sandbox_timeline.day_state(ad_id, target) == sandbox_timeline.PAUSED
            self.assertEqual(configured.ad_effective_status, "PAUSED" if paused else "ACTIVE")
        self.assertEqual(meta_write_capability_findings(self.runtime), [], "no write port was left behind")

    def test_replaying_the_tick_and_the_batch_changes_nothing(self):
        connection_id, established, _ads = self._establish(ads=1)
        fire_at = self._make_due()
        self._drain()
        models = (MetaObservationSyncRunRecord, OperationRecord, MetaEvidencePageRecord, EvidencePublicationRecord,
                  EvidenceAttestationRecord, ObservationCoverageLedgerRecord)
        before = [len(self._rows(model)) for model in models]
        context = worker_context(self.workspace_id, SANDBOX)
        with tenant_session(context) as session:
            self.assertEqual(observation_sync.run_observation_tick(
                session, context, connection_id=connection_id, fire_at=fire_at).status, "not_due")
        replay = observation_sync.run_observation_sync(
            context, connection_id=connection_id, target_date=observation_target_date(fire_at, TIMEZONE),
            final_attempt=False)
        self.assertEqual(len(replay.run_ids), 5)
        self.assertEqual([len(self._rows(model)) for model in models], before)
        self.assertEqual(before[0], 5)

    def test_a_controlled_mock_fault_fails_one_instance_and_the_others_finish(self):
        faults = {"rules": [{"operation": "read_delivery_configuration", "effect": "read_error",
                             "match_page": 1, "match_edge": "adsets"}]}
        _connection_id, established, _ads = self._establish(MOCK, scenario_config={"faults": faults})
        fire_at = self._make_due()
        target = observation_target_date(fire_at, "UTC")

        self._drain(release_retries=True)

        batch, = self._rows(JobRecord, type=observation_sync.SYNC_JOB_TYPE)
        self.assertEqual((batch.state, batch.error_code, batch.attempt), ("failed", "meta_observation_sync_failed", 3))
        runs = {run.occurrence_id.rsplit(":", 1)[1]: run for run in self._rows(MetaObservationSyncRunRecord)}
        self.assertEqual({name: run.state for name, run in runs.items()}, {
            "daily": "succeeded", "current_7d": "succeeded", "baseline_7d": "succeeded",
            "rolling_14d": "succeeded", "configuration": "failed",
        })
        attested = {p.sync_run_id for p in self._rows(EvidencePublicationRecord)}
        self.assertEqual(attested, {run.id for name, run in runs.items() if name != "configuration"})
        self.assertEqual(len(self._rows(EvidenceAttestationRecord)), 4)
        config_pages = self._pages(runs["configuration"].id)
        self.assertEqual([request["edge"] for request, _body in config_pages], ["account", "ads"],
                         "the pages before the fault stay, without a terminal page")
        self.assertEqual(sorted(c.contract_generation_id for c in self._rows(ObservationCoverageLedgerRecord)),
                         sorted([established.generation_ids["daily"], established.generation_ids["exact_window"]]))
        operations = {op.id: op.state for op in self._rows(OperationRecord)}
        self.assertEqual(operations[runs["configuration"].operation_id], "failed")
        self.assertEqual({operations[run.operation_id] for name, run in runs.items() if name != "configuration"},
                         {"succeeded"})
        with tenant_session(worker_context(self.workspace_id, MOCK)) as session:
            self.assertIsNone(current_attestation(
                session, contract_generation_id=established.delivery_configuration_generation_id))
        self.assertEqual(runs["daily"].occurrence_id.split(":")[2], target.isoformat())

    def test_a_generation_covers_d_only_once_all_its_instances_succeeded(self):
        faults = {"rules": [{"operation": "read_insights", "effect": "read_error", "match_page": 1,
                             "match_time_increment": "all_days", "match_window_days": 14}]}
        _connection_id, established, _ads = self._establish(MOCK, ads=1, scenario_config={"faults": faults})
        self._make_due()

        self._drain(release_retries=True)

        runs = {run.occurrence_id.rsplit(":", 1)[1]: run.state for run in self._rows(MetaObservationSyncRunRecord)}
        self.assertEqual(runs, {"daily": "succeeded", "current_7d": "succeeded", "baseline_7d": "succeeded",
                                "rolling_14d": "failed", "configuration": "succeeded"})
        self.assertEqual(len(self._rows(EvidenceAttestationRecord)), 4, "each finished instance is attested")
        self.assertEqual(sorted(c.contract_generation_id for c in self._rows(ObservationCoverageLedgerRecord)),
                         sorted([established.generation_ids["daily"],
                                 established.delivery_configuration_generation_id]),
                         "two of three windows do not cover D for the exact-window generation")

    def test_a_drifted_generation_is_audited_and_not_read(self):
        _connection_id, established, _ads = self._establish(ads=1)
        with platform_session() as session:  # as if the code contract had moved on under it
            session.execute(update(ObservationContractGenerationRecord)
                            .where(ObservationContractGenerationRecord.id == established.generation_ids["exact_window"])
                            .values(fields=["account_id", "ad_id", "spend"]))
        self._make_due()
        self._drain()
        runs = self._rows(MetaObservationSyncRunRecord)
        self.assertEqual({run.contract_generation_id for run in runs},
                         {established.generation_ids["daily"], established.delivery_configuration_generation_id})
        self.assertEqual({run.state for run in runs}, {"succeeded"})
        audit, = [e for e in self._rows(AuditEventRecord) if e.event_type == "meta.observation.contract_mismatch"]
        self.assertEqual((audit.resource_id, audit.details_json["code"]),
                         (established.generation_ids["exact_window"], "meta_read_contract_mismatch"))

    def test_a_run_made_before_query_instances_still_runs(self):
        connection_id = self._connection()
        response = self.client.post("/api/v1/meta/contracts", json=daily_contract_body(connection_id, SANDBOX))
        self.assertEqual(response.status_code, 201, response.text)
        generation_id = response.json()["id"]
        context = worker_context(self.workspace_id, SANDBOX)
        seed_sandbox_ads(context, connection_id, 1)
        target = utcnow().date() - timedelta(days=1)
        with tenant_session(context) as session:  # what the #107 tick left: occurrence obs:{gen}:{D}
            operation = create_operation(session, command_type=observation_sync.SYNC_COMMAND_TYPE,
                                         actor=observation_sync.SCHEDULER_EXECUTOR, context=context,
                                         semantic_key_hash=uuid.uuid4().hex, reason="scheduled Meta observation sync")
            run_id = create_sync_run(session, workspace_id=self.workspace_id, meta_mode=SANDBOX,
                                     contract_generation_id=generation_id,
                                     occurrence_id=observation_sync.occurrence_id(generation_id, target),
                                     operation_id=operation.id, executor=observation_sync.SCHEDULER_EXECUTOR).id
        result = observation_sync.run_observation_sync(context, connection_id=connection_id, target_date=target,
                                                       final_attempt=True)
        self.assertEqual(result.succeeded, (run_id,))
        attestation, = self._rows(EvidenceAttestationRecord)
        self.assertEqual(attestation.evidence_watermark,
                         f"[{(target - timedelta(days=13)).isoformat()},{target.isoformat()}]")

    def test_another_workspace_reads_nothing_of_this_connection(self):
        connection_id, _established, _ads = self._establish(ads=1)
        fire_at = self._make_due()
        other = worker_context(self._provision("ws_scale_reads_other"), SANDBOX)
        with tenant_session(other) as session:
            self.assertEqual(observation_sync.run_observation_tick(
                session, other, connection_id=connection_id, fire_at=fire_at).status, "not_due")
        self.assertEqual(observation_sync.run_observation_sync(
            other, connection_id=connection_id, target_date=observation_target_date(fire_at, TIMEZONE),
            final_attempt=True).run_ids, ())
        self.assertEqual(self._rows(MetaObservationSyncRunRecord), [])


class ScaleImportBoundary(unittest.TestCase):
    """The contract set lives in Scale and reaches Meta only through models and
    pure contract code: no adapter, no credential resolution (ADR 0017)."""

    def test_establishing_the_set_loads_no_meta_adapter(self):
        probe = (
            "import sys\n"
            "import app.scale.delivery_targets\n"
            "print(','.join(sorted(m for m in sys.modules if m.startswith('app.adapters.meta')"
            " or m in ('app.meta.credential_resolution', 'app.meta.observation_sync'))))\n"
        )
        result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60,
                                cwd=str(ROOT))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
