"""internal_validation daily reads through the Worker, on SQLite (#112, #101 S6).

The platform Workspace's internal_validation connection -- two sealed
credential versions and a binding, as the platform bootstrap leaves them --
gets its Scale read contracts through the platform route
``POST /api/platform/v1/scale/internal_validation/metric-contract-sets``.
Each new generation gets an active daily schedule at account-local 04:00
(14-day lookback), the same lifecycle S1 gave ``customer_sandbox``. With the
slot due, the deployed ``WorkerRuntime`` alone dispatches the tick, runs the
batch through ``HttpMetaReadAdapter`` against a simulated Graph API
(``tests/support/meta_graph_simulator.py``: no network, no real token) and
attests every query instance; #129's observed ads then list the ads of the
attested configuration read.

Also covered: pagination; usage past the threshold and a rate-limit error
defer the batch Job without spending attempts and without failing a run,
and it resumes at the next page; a deferral that would outlast the next
day's sync fails the runs; an expired token or a revoked credential fails
every run at once with no attestation and no retry; a Workspace that is not
the platform Workspace reads nothing; retiring a generation retires its
schedule. No credential appears in any intent, outcome, page, attempt, Job,
audit event, Operation or log line.

``tests/test_meta_internal_validation_sync_pg.py`` runs the main path with
the real roles under FORCE RLS.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import unittest
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.adapters.meta import http_read_adapter
from app.adapters.persistence.database import engine, platform_session, tenant_session
from app.adapters.persistence.models import AuditEventRecord, JobRecord, WorkspaceRecord, utcnow
from app.entitlements.credential_admin import CredentialAdmin
from app.entitlements.credential_protector import LocalKmsAdapter
from app.main import app
from app.meta import observation_sync
from app.meta.evidence import current_attestation
from app.meta.models import (
    EvidenceAttestationRecord,
    EvidencePublicationRecord,
    MetaCallIntentRecord,
    MetaCallOutcomeRecord,
    MetaEvidencePageRecord,
    MetaObservationSyncRunRecord,
    MetaObservationSyncScheduleRecord,
    MetaPageFetchAttemptRecord,
    ObservationContractGenerationRecord,
    ObservationCoverageLedgerRecord,
)
from app.meta.observation import (
    DEFAULT_SYNC_CRON,
    latest_fire_at_or_before,
    next_fire_after,
    observation_target_date,
    retire_contract_generation,
)
from app.meta.scale_read_contracts import AccountBinding
from app.meta.scale_read_parsers import parse_daily, parse_delivery_configuration, parse_window
from app.platform.operations import OperationRecord
from app.platform.platform_operator_identity import PlatformOperatorIdentity
from app.platform.workspace_admission import WorkspaceAdmission
from app.scale.observed_ads import list_observed_ads
from app.worker_runtime import WorkerRuntime, meta_write_capability_findings
from tests.support.meta_graph_simulator import (
    ACCESS_TOKEN,
    AD_ACCOUNT,
    APP_SECRET,
    CURRENCY,
    TIMEZONE,
    GraphSimulator,
    business_usage,
    graph_error,
    platform_context,
    provision_connection,
)
from tests.support.queue_hygiene import cancel_workspace_jobs, quarantine_foreign_jobs

MODE = "internal_validation"
INSTANCES = {"daily": ("daily",), "exact_window": ("current_7d", "baseline_7d", "rolling_14d"),
             "delivery_configuration": ("configuration",)}
#: Reads of one day with three ads: daily 42 rows in two pages, three windows
#: of one page each, and the four configuration edges.
READS_PER_DAY = 9


class InternalValidationSyncTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        db_file = engine.url.database
        if db_file and db_file != ":memory:":
            Path(db_file).parent.mkdir(parents=True, exist_ok=True)
        command.upgrade(Config(str(Path(__file__).resolve().parents[1] / "alembic.ini")), "head")

    def setUp(self) -> None:
        self.workspace_id = f"ws_meta_iv_{uuid.uuid4().hex[:12]}"
        with platform_session() as session:
            session.add(WorkspaceRecord(id=self.workspace_id, name="Acme Charge platform",
                                        is_platform_workspace=True))
        self.addCleanup(self._quiesce)
        self.context = platform_context(self.workspace_id)
        self.provisioned = provision_connection(self.context)
        self.connection_id = self.provisioned["connection_id"]
        self.graph = GraphSimulator()
        patcher = mock.patch.object(http_read_adapter, "read_gateway", self.graph.gateway)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._platform_operator(is_platform_workspace=True)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.runtime = WorkerRuntime(owner=f"ci-only-meta-iv-{uuid.uuid4().hex[:6]}")

    # -- fixtures -----------------------------------------------------------

    def _platform_operator(self, *, is_platform_workspace: bool) -> None:
        """A verified platform-operator token for the platform Workspace."""
        originals = (app.state.platform_operator_jwt_verifier, getattr(app.state, "admission_reader", None))

        def restore():
            app.state.platform_operator_jwt_verifier, app.state.admission_reader = originals

        self.addCleanup(restore)
        identity = PlatformOperatorIdentity(sub="usr_acme_platform_operator", workspace_id=self.workspace_id,
                                            session_id="sess_acme", iat=1, exp=2, jti=f"jti_{uuid.uuid4().hex}")
        app.state.platform_operator_jwt_verifier = AsyncMock(verify=AsyncMock(return_value=identity))
        app.state.admission_reader = SimpleNamespace(read=AsyncMock(return_value=WorkspaceAdmission(
            workspace_id=self.workspace_id, authorization_epoch=0, entitlement_version=1,
            suspended=False, is_platform_workspace=is_platform_workspace,
        )))

    def _quiesce(self) -> None:
        with platform_session() as session:
            session.execute(update(MetaObservationSyncScheduleRecord)
                            .where(MetaObservationSyncScheduleRecord.workspace_id == self.workspace_id,
                                   MetaObservationSyncScheduleRecord.state == "active")
                            .values(state="paused"))
        cancel_workspace_jobs(self.workspace_id)

    def _establish(self) -> dict:
        response = self.client.post(
            f"/api/platform/v1/scale/{MODE}/metric-contract-sets",
            headers={"Authorization": "Bearer ci-only-platform-operator-token"},
            json={"meta_connection_id": self.connection_id, "currency": CURRENCY, "timezone_name": TIMEZONE},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _make_due(self):
        fire_at = latest_fire_at_or_before(DEFAULT_SYNC_CRON, TIMEZONE, utcnow())
        with platform_session() as session:
            session.execute(update(MetaObservationSyncScheduleRecord)
                            .where(MetaObservationSyncScheduleRecord.workspace_id == self.workspace_id,
                                   MetaObservationSyncScheduleRecord.state == "active")
                            .values(next_fire_at=fire_at))
        return fire_at

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

    def _resume_once(self) -> None:
        """The deferred batch's wait is over: one Worker pass claims it."""
        quarantine_foreign_jobs(self.workspace_id)
        with platform_session() as session:
            session.execute(update(JobRecord)
                            .where(JobRecord.workspace_id == self.workspace_id, JobRecord.state == "retry_wait")
                            .values(next_attempt_at=utcnow() - timedelta(seconds=1)))
        self.assertTrue(asyncio.run(self.runtime.run_once()))

    def _rows(self, model, **filters) -> list:
        with platform_session() as session:
            rows = list(session.scalars(select(model).where(model.workspace_id == self.workspace_id)
                                        .filter_by(**filters)))
            for row in rows:
                session.expunge(row)
            return rows

    def _runs(self) -> dict[str, MetaObservationSyncRunRecord]:
        return {run.occurrence_id.rsplit(":", 1)[1]: run for run in self._rows(MetaObservationSyncRunRecord)}

    def _batch(self) -> JobRecord:
        batch, = self._rows(JobRecord, type=observation_sync.SYNC_JOB_TYPE)
        return batch

    def _pages(self, run_id: str) -> list[tuple[dict, dict]]:
        pages = sorted(self._rows(MetaEvidencePageRecord, sync_run_id=run_id), key=lambda page: page.page_order)
        intents = {intent.id: intent for intent in self._rows(MetaCallIntentRecord)}
        return [(intents[page.intent_id].request_parameters, json.loads(gzip.decompress(page.payload_compressed)))
                for page in pages]

    def _assert_nothing_kept_a_credential(self, logged: list[str]) -> None:
        """Every row this Workspace wrote, and every log line, without a secret."""
        proof = __import__("hmac").new(APP_SECRET.encode(), ACCESS_TOKEN.encode(), "sha256").hexdigest()
        kept = []
        for model in (MetaCallIntentRecord, MetaCallOutcomeRecord, MetaPageFetchAttemptRecord,
                      MetaObservationSyncRunRecord, JobRecord, AuditEventRecord, OperationRecord,
                      EvidencePublicationRecord, EvidenceAttestationRecord):
            for row in self._rows(model):
                kept.append(json.dumps({column.name: getattr(row, column.key, None)
                                        for column in model.__table__.columns}, default=repr))
        for row in self._rows(MetaEvidencePageRecord):
            kept.append(gzip.decompress(row.payload_compressed).decode())
        self.assertTrue(kept)
        for text in kept + logged:
            for secret in (ACCESS_TOKEN, APP_SECRET, proof):
                self.assertNotIn(secret, text)

    @contextmanager
    def _logs(self):
        lines: list[str] = []

        class Capture(logging.Handler):
            def emit(self, record):
                lines.append(record.getMessage())

        handler = Capture(level=logging.DEBUG)
        root, httpx_logger = logging.getLogger(), logging.getLogger("httpx")
        previous = (root.level, httpx_logger.level, httpx_logger.disabled)
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        httpx_logger.setLevel(logging.DEBUG)
        httpx_logger.disabled = False  # alembic's fileConfig disabled it
        try:
            yield lines
        finally:
            root.removeHandler(handler)
            root.setLevel(previous[0])
            httpx_logger.setLevel(previous[1])
            httpx_logger.disabled = previous[2]


# ---------------------------------------------------------------------------
# Schedule lifecycle
# ---------------------------------------------------------------------------


class ScheduleLifecycleTests(InternalValidationSyncTestCase):
    def test_the_platform_route_sets_up_three_contracts_each_with_a_daily_schedule(self):
        before = utcnow()
        established = self._establish()

        self.assertEqual((established["meta_mode"], established["meta_connection_id"], established["created"]),
                         (MODE, self.connection_id, True))
        generations = {item["contract_type"]: item for item in established["generations"]}
        self.assertEqual(set(generations), set(INSTANCES))
        expected_fire = next_fire_after(DEFAULT_SYNC_CRON, TIMEZONE, before)
        schedules = {row.contract_generation_id: row for row in self._rows(MetaObservationSyncScheduleRecord)}
        self.assertEqual(set(schedules), {item["generation_id"] for item in generations.values()})
        for item in generations.values():
            schedule = schedules[item["generation_id"]]
            self.assertEqual((schedule.state, schedule.cron_expression, schedule.meta_mode,
                              schedule.meta_connection_id), ("active", DEFAULT_SYNC_CRON, MODE, self.connection_id))
            fire_at = schedule.next_fire_at.replace(tzinfo=expected_fire.tzinfo)
            self.assertEqual(fire_at, expected_fire)
            self.assertEqual(fire_at.astimezone(ZoneInfo(TIMEZONE)).hour, 4)
            self.assertEqual(item["schedule_state"], "active")
        for generation in self._rows(ObservationContractGenerationRecord):
            self.assertEqual((generation.time_window["account_currency"], generation.time_window["account_timezone"],
                              generation.local_timezone), (CURRENCY, TIMEZONE, TIMEZONE))

        again = self._establish()
        self.assertEqual((again["id"], again["created"]), (established["id"], False))
        self.assertEqual(len(self._rows(MetaObservationSyncScheduleRecord)), 3, "idempotent: no second schedule")

    def test_retiring_a_generation_retires_its_schedule_and_its_reads_stop(self):
        established = self._establish()
        daily = next(item["generation_id"] for item in established["generations"] if item["contract_type"] == "daily")
        with tenant_session(self.context) as session:
            retire_contract_generation(session, generation_id=daily)
        states = {row.contract_generation_id: row.state for row in self._rows(MetaObservationSyncScheduleRecord)}
        self.assertEqual(states[daily], "retired")
        self.assertEqual(sorted(states.values()), ["active", "active", "retired"])

        self._make_due()
        self._drain()
        self.assertEqual(set(self._runs()), {"current_7d", "baseline_7d", "rolling_14d", "configuration"})
        self.assertNotIn("1", {request.params.get("time_increment") for request in self.graph.requests},
                          "the retired daily contract is not read")

    def test_a_workspace_that_is_not_the_platform_workspace_cannot_set_up_reads(self):
        self._platform_operator(is_platform_workspace=False)
        response = self.client.post(
            f"/api/platform/v1/scale/{MODE}/metric-contract-sets",
            headers={"Authorization": "Bearer ci-only-platform-operator-token"},
            json={"meta_connection_id": self.connection_id, "currency": CURRENCY, "timezone_name": TIMEZONE},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["code"], "meta_mode_not_permitted")
        self.assertEqual(self._rows(MetaObservationSyncScheduleRecord), [])

    def test_a_real_account_needs_its_currency_and_time_zone(self):
        response = self.client.post(
            f"/api/platform/v1/scale/{MODE}/metric-contract-sets",
            headers={"Authorization": "Bearer ci-only-platform-operator-token"},
            json={"meta_connection_id": self.connection_id},
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("scale_contract_account_required", response.text)
        self.assertEqual(self._rows(ObservationContractGenerationRecord), [])


# ---------------------------------------------------------------------------
# The Worker reads Meta
# ---------------------------------------------------------------------------


class DailyReadTests(InternalValidationSyncTestCase):
    def test_a_due_schedule_reads_meta_attests_every_instance_and_feeds_the_ad_picker(self):
        established = self._establish()
        generation_ids = {item["contract_type"]: item["generation_id"] for item in established["generations"]}
        fire_at = self._make_due()
        target = observation_target_date(fire_at, TIMEZONE)

        with self._logs() as logged:
            self.assertEqual(self._drain(), 2, "one tick, one batch")

        tick, = self._rows(JobRecord, type=observation_sync.TICK_JOB_TYPE)
        batch = self._batch()
        self.assertEqual((tick.state, batch.state, batch.attempt), ("succeeded", "succeeded", 1))
        runs = self._runs()
        self.assertEqual(set(runs), {name for names in INSTANCES.values() for name in names})
        self.assertEqual({run.state for run in runs.values()}, {"succeeded"})

        # Every request was a GET with the decrypted token; the daily read paged.
        self.assertEqual(len(self.graph.requests), READS_PER_DAY)
        self.assertEqual({request.method for request in self.graph.requests}, {"GET"})
        self.assertEqual({request.authorization for request in self.graph.requests}, {f"Bearer {ACCESS_TOKEN}"})
        daily_pages = self._pages(runs["daily"].id)
        self.assertEqual([body.get("paging", {}).get("cursors", {}).keys() >= {"after"} for _r, body in daily_pages],
                         [True, True])
        self.assertEqual([request.get("after") for request, _body in daily_pages],
                         [None, daily_pages[0][1]["paging"]["cursors"]["after"]])
        self.assertEqual((runs["daily"].total_pages, runs["daily"].total_rows), (2, 42))

        # Evidence: intents GET with the credential versions, outcomes with the usage reading.
        intents = self._rows(MetaCallIntentRecord)
        self.assertEqual(len(intents), READS_PER_DAY)
        self.assertEqual({(i.meta_api_method, i.access_token_version_id, i.app_secret_version_id, i.write_permitted,
                           i.network_dispatched_at is not None) for i in intents},
                         {("GET", self.provisioned["access_token_version_id"],
                           self.provisioned["app_secret_version_id"], False, True)})
        outcomes = self._rows(MetaCallOutcomeRecord)
        self.assertEqual({(o.network_attempted, o.http_status, o.error_category) for o in outcomes},
                         {(True, 200, None)})
        self.assertTrue(all("usage" in o.response_summary for o in outcomes))

        attestations = {a.publication_id: a for a in self._rows(EvidenceAttestationRecord)}
        publications = {p.sync_run_id: p for p in self._rows(EvidencePublicationRecord)}
        self.assertEqual({attestations[publications[run.id].id].state for run in runs.values()}, {"valid"})
        coverage = self._rows(ObservationCoverageLedgerRecord)
        self.assertEqual(sorted((c.contract_generation_id, c.coverage_date) for c in coverage),
                         sorted((generation_id, target) for generation_id in generation_ids.values()))

        # Scale reads these pages as it reads the sandbox's.
        binding = AccountBinding(AD_ACCOUNT, CURRENCY, TIMEZONE)
        daily = parse_daily([body for _r, body in daily_pages], expected=binding, ad_id="acme_ad_2",
                            since=target - timedelta(days=13), until=target)
        self.assertEqual([day.impressions for day in daily.days], [2000] * 14)
        baseline = parse_window([body for _r, body in self._pages(runs["baseline_7d"].id)], expected=binding,
                                ad_id="acme_ad_1", window_role="baseline_7d",
                                since=target - timedelta(days=13), until=target - timedelta(days=7))
        self.assertEqual(baseline.impressions, 7000)
        configuration = self._pages(runs["configuration"].id)
        self.assertEqual([request["edge"] for request, _body in configuration], ["account", "ads", "adsets", "campaigns"])
        observed = parse_delivery_configuration([(request["edge"], body) for request, body in configuration],
                                                expected=binding, ad_id="acme_ad_3")
        self.assertEqual(observed.ad_creative_id, "cr_acme_ad_3")

        # #129: the ads of the attested configuration read can be picked.
        with tenant_session(self.context) as session:
            self.assertIsNotNone(current_attestation(
                session, contract_generation_id=generation_ids["delivery_configuration"]))
            listing = list_observed_ads(session, self.context, connection_id=self.connection_id)
        self.assertEqual(listing.unavailable, ())
        self.assertEqual(sorted((ad.ad_remote_id, ad.source_type) for ad in listing.ads),
                         [(f"acme_ad_{n}", "evidence_observation") for n in (1, 2, 3)])

        self._assert_nothing_kept_a_credential(logged)
        self.assertTrue(any("graph.facebook.com" in line for line in logged), "the httpx lines were captured")
        self.assertEqual(meta_write_capability_findings(self.runtime), [])


class RateLimitTests(InternalValidationSyncTestCase):
    def _deferred(self) -> JobRecord:
        batch = self._batch()
        self.assertEqual((batch.state, batch.error_code), ("retry_wait", "meta_read_rate_limited"))
        return batch

    def test_usage_past_the_threshold_defers_the_batch_without_failing_or_spending_attempts(self):
        self._establish()
        self._make_due()
        # The first four reads report 80 % of the account's insights budget.
        self.graph.usage = lambda index, _request: business_usage(80 if index <= 4 else 12)

        self._drain()
        batch = self._deferred()
        self.assertEqual(batch.attempt, 0, "a deferral gives back the attempt its claim took")
        wait = (batch.next_attempt_at.replace(tzinfo=utcnow().tzinfo) - utcnow()).total_seconds()
        self.assertAlmostEqual(wait, 15 * 60, delta=60)
        self.assertEqual(len(self.graph.requests), 1, "no read after the one that crossed the line")
        self.assertEqual(len(self._rows(MetaEvidencePageRecord)), 1, "the page that was read is kept")
        self.assertEqual(self._rows(MetaObservationSyncRunRecord, state="failed"), [], "nothing failed")
        deferred, = self._rows(AuditEventRecord, event_type="job.deferred", resource_id=batch.id)
        self.assertEqual((deferred.details_json["error_code"], deferred.details_json["attempt"]),
                         ("meta_read_rate_limited", 0))

        for _ in range(3):  # three more high readings: still deferred, never failed
            self._resume_once()
            self.assertEqual(self._deferred().attempt, 0)
        self.assertEqual(len(self.graph.requests), 4)
        self.assertEqual(len(self._rows(AuditEventRecord, event_type="job.deferred", resource_id=batch.id)), 4)

        self._drain(release_retries=True)
        batch = self._batch()
        self.assertEqual((batch.state, batch.attempt), ("succeeded", 1))
        self.assertEqual({run.state for run in self._runs().values()}, {"succeeded"})
        self.assertEqual(len(self.graph.requests), READS_PER_DAY, "every page read once, resuming at the next")
        pages = self._rows(MetaEvidencePageRecord)
        self.assertEqual(len({(page.sync_run_id, page.page_order) for page in pages}), len(pages))
        summaries = [o.response_summary["usage"] for o in self._rows(MetaCallOutcomeRecord)]
        self.assertEqual(sorted(s["max_utilisation_pct"] for s in summaries), [12.0] * 5 + [80.0] * 4)

    def test_a_rate_limit_error_defers_and_keeps_the_refused_read_as_evidence(self):
        self._establish()
        self._make_due()
        self.graph.fault = lambda index, _request: graph_error(
            400, 80004, "There have been too many calls to this ad account",
            headers=business_usage(100, regain_minutes=20)) if index == 1 else None

        self._drain()
        batch = self._deferred()
        wait = (batch.next_attempt_at.replace(tzinfo=utcnow().tzinfo) - utcnow()).total_seconds()
        self.assertAlmostEqual(wait, 20 * 60, delta=60)
        outcome, = self._rows(MetaCallOutcomeRecord)
        self.assertEqual((outcome.network_attempted, outcome.http_status, outcome.error_category),
                         (True, 400, "rate_limit"))
        self.assertEqual(outcome.response_summary["error_code"], "meta_read_rate_limited")
        attempt, = self._rows(MetaPageFetchAttemptRecord)
        self.assertEqual(attempt.state, "failed")
        self.assertEqual(self._rows(MetaObservationSyncRunRecord, state="failed"), [])

        self._drain(release_retries=True)
        self.assertEqual(self._batch().state, "succeeded")
        self.assertEqual({run.state for run in self._runs().values()}, {"succeeded"})
        retried = sorted(a.attempt_number for a in self._rows(MetaPageFetchAttemptRecord)
                         if a.intent_id in {o.intent_id for o in self._rows(MetaCallOutcomeRecord)
                                            if o.error_category == "rate_limit"} or a.attempt_number > 1)
        self.assertEqual(retried, [1, 2], "the refused page was read again, as attempt 2")

    def test_a_deferral_that_would_outlast_the_next_days_sync_fails_the_runs(self):
        self._establish()
        self._make_due()
        self.graph.fault = lambda _index, _request: graph_error(400, 17, "User request limit reached")
        with mock.patch.object(observation_sync, "_deferral_deadline", lambda *_args: utcnow()):
            self._drain()
        batch = self._batch()
        self.assertEqual((batch.state, batch.error_code), ("failed", "meta_observation_sync_failed"))
        self.assertEqual({run.state for run in self._runs().values()}, {"failed"})
        self.assertEqual(len(self.graph.requests), 1, "the rest are not read")
        self.assertEqual(self._rows(EvidenceAttestationRecord), [])


class RefusedCredentialTests(InternalValidationSyncTestCase):
    def _assert_failed_without_evidence(self) -> None:
        batch = self._batch()
        self.assertEqual((batch.state, batch.attempt), ("failed", 1), "no retry can change it")
        self.assertEqual({run.state for run in self._runs().values()}, {"failed"})
        self.assertEqual(len(self._runs()), 5)
        for model in (EvidencePublicationRecord, EvidenceAttestationRecord, ObservationCoverageLedgerRecord):
            self.assertEqual(self._rows(model), [], model.__name__)
        self.assertEqual({op.state for op in self._rows(OperationRecord)}, {"failed"})
        self.assertEqual(len(self._rows(AuditEventRecord, event_type="meta.sync_run.failed")), 5)

    def test_an_expired_token_fails_every_run_at_once_with_no_attestation(self):
        established = self._establish()
        self._make_due()
        self.graph.fault = lambda _index, _request: graph_error(
            400, 190, f"Error validating access token {ACCESS_TOKEN}: Session has expired", subcode=463)
        with self._logs() as logged:
            self._drain()
        self._assert_failed_without_evidence()
        self.assertEqual(len(self.graph.requests), 1, "the other runs would present the same token")
        outcome, = self._rows(MetaCallOutcomeRecord)
        self.assertEqual((outcome.network_attempted, outcome.http_status, outcome.error_category),
                         (True, 400, "auth_failure"))
        self.assertIn("meta_read_auth_failed", outcome.error_detail)
        with tenant_session(self.context) as session:
            generation_id = next(item["generation_id"] for item in established["generations"]
                                 if item["contract_type"] == "delivery_configuration")
            self.assertIsNone(current_attestation(session, contract_generation_id=generation_id))
        self._assert_nothing_kept_a_credential(logged)

    def test_a_revoked_credential_fails_every_run_without_a_request(self):
        self._establish()
        with tenant_session(self.context) as session:
            CredentialAdmin(LocalKmsAdapter.local_only()).revoke(
                session, self.context, credential_key="meta_access_token", reason="compromised (drill)")
        self._make_due()
        self._drain()
        self._assert_failed_without_evidence()
        self.assertEqual(self.graph.requests, [])
        outcome, = self._rows(MetaCallOutcomeRecord)
        self.assertEqual((outcome.network_attempted, outcome.http_status, outcome.error_category),
                         (False, None, "auth_failure"))
        self.assertIn("meta_read_credential_unavailable", outcome.error_detail)

    def test_a_workspace_that_is_no_longer_the_platform_workspace_reads_nothing(self):
        self._establish()
        with platform_session() as session:
            session.execute(update(WorkspaceRecord).where(WorkspaceRecord.id == self.workspace_id)
                            .values(is_platform_workspace=False))
        self._make_due()
        self._drain()
        self._assert_failed_without_evidence()
        self.assertEqual(self.graph.requests, [])
        self.assertEqual(self._rows(MetaCallIntentRecord), [], "refused before any call intent")
        self.assertIn("meta_platform_workspace_required", self._batch().error_detail)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
