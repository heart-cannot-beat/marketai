"""Issue #270 against an isolated migrated PostgreSQL with the real API and Worker roles.

SQLite cannot see a missing grant (#284), so every scan write command runs
here as the role that serves it in production: the API previews, admits a
byok scan (both lines reserved), resumes it after a 402 and records the
Owner's reconciliation; the Worker fetches pages (observations and origins
appended, reservations settled and released), archives media, waits for
nothing, reconciles and settles. Then the privilege matrix itself, FORCE'd
RLS, the append-only triggers, and another Workspace being refused.

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
from unittest.mock import patch

from sqlalchemy import create_engine, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AuditEventRecord, JobRecord, utcnow
from app.competitors.models import CompetitorAdOriginRecord, CompetitorAdRecord
from app.entitlements.cost_models import ProviderCallAttemptRecord
from app.library.contracts import ROLE_MEMBER
from app.market_data.calls import market_data_call_key

URLS = [os.getenv(key) for key in ("DOMAIN_OWNER_DATABASE_URL", "DOMAIN_API_DATABASE_URL", "DOMAIN_WORKER_DATABASE_URL")]
TABLES = ("competitor_scans", "competitor_ads", "competitor_ad_origins")
#: table -> {role: privileges it must have}; every other DML privilege must be absent.
GRANTED = {
    "competitor_scans": {"marketai_domain_api": {"SELECT", "INSERT", "UPDATE"},
                         "marketai_domain_worker": {"SELECT", "UPDATE"}},
    "competitor_ads": {"marketai_domain_api": {"SELECT"}, "marketai_domain_worker": {"SELECT", "INSERT"}},
    "competitor_ad_origins": {"marketai_domain_api": {"SELECT"}, "marketai_domain_worker": {"SELECT", "INSERT"}},
}
DML = ("SELECT", "INSERT", "UPDATE", "DELETE")
ACME, BRIGHTVOLT, HALCYON = "100000000000010", "100000000000016", "100000000010002"


@unittest.skipUnless(all(URLS) and os.getenv("MARKETAI_TENANT_CONTEXT_SECRET"),
                     "requires isolated PostgreSQL runtime roles")
class CompetitorScanPostgresTests(unittest.TestCase):
    def setUp(self):
        self.owner, self.api, self.worker = [create_engine(url) for url in URLS]
        for engine in (self.owner, self.api, self.worker):
            self.addCleanup(engine.dispose)
        self.sessions = [sessionmaker(bind=e, expire_on_commit=False, autoflush=False)
                         for e in (self.owner, self.api, self.worker)]
        self.workspaces: list[str] = []
        self.addCleanup(self._retire_jobs)

    def _retire_jobs(self):
        with self.owner.begin() as connection:
            connection.execute(update(JobRecord).where(
                JobRecord.workspace_id.in_(self.workspaces),
                # Uncertain Jobs too: the lost-page scenario leaves them, and later CI
                # steps on the same cluster expect none (marketai_health_stats_v1).
                JobRecord.state.in_(("queued", "retry_wait", "leased", "running", "uncertain")),
            ).values(state="cancelled", finished_at=utcnow(), lease_owner=None, lease_expires_at=None,
                     heartbeat_at=None))

    def _as(self, index: int):
        return patch("app.adapters.persistence.database.SessionLocal", self.sessions[index])

    def _fixture(self):
        from tests.test_competitor_scans_api import ScanFixtures

        fixture = ScanFixtures()
        with self._as(0):
            fixture.setUp()
            fixture.ctx = fixture._make_context(f"ws_scan_pg_{uuid.uuid4().hex[:10]}", role=ROLE_MEMBER,
                                                actor="member_a")
            fixture.current = fixture.ctx
            self.workspaces.append(fixture.ctx.workspace_id)
            fixture._competitor_product = fixture._product()
            fixture._bind_both()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def _work(self, fixture) -> None:
        with self._as(2), patch("app.platform.jobs.engine", self.worker):
            runtime = fixture._runtime()
            for _ in range(20):
                if not asyncio.run(runtime.run_once(job_types=("competitor_scan.run",))):
                    break

    def test_every_scan_write_runs_under_its_runtime_role(self):
        fixture = self._fixture()
        with self._as(0):
            stranger = fixture._make_context(f"ws_scan_pg_other_{uuid.uuid4().hex[:8]}", role=ROLE_MEMBER,
                                             actor="member_b")
        self.workspaces.append(stranger.workspace_id)
        with self._as(1):
            competitors = [fixture._confirmed(ACME), fixture._confirmed(BRIGHTVOLT), fixture._confirmed(HALCYON)]
            body = fixture._body(competitors)
            quote = fixture._preview(body)
            self.assertEqual((quote["execution_mode"], quote["market_data"]["max_credits"]), ("byok", 23))
            fixture.mock_credits = 4  # Halcyon Motors' page gets a 402
            accepted = fixture._start(body)
            self.assertEqual(len(fixture._scan_reservations(accepted["job_id"])), 23 + 111)

        self._work(fixture)

        with self._as(1):
            scan = fixture._scan(accepted["scan_id"])
            self.assertEqual((scan["status"], scan["failure"]["code"]),
                             ("partial", "market_data_insufficient_credits"), scan)
            self.assertEqual(scan["observation_count"], 62)
            self.assertNotIn("reserved", {r.state for r in fixture._scan_reservations(accepted["job_id"]).values()})
            fixture.mock_credits = 100
            resumed = fixture._resume(accepted["scan_id"])

        self._work(fixture)

        with self._as(1):
            scan = fixture._scan(accepted["scan_id"])
            self.assertEqual((scan["status"], scan["observation_count"], scan["cost"]["market_data"]["usd"]),
                             ("completed", 128, "0.01692"), scan)
            self.assertNotIn("reserved", {r.state for r in fixture._scan_reservations(resumed["job_id"]).values()})
            ads = fixture._all_ads(accepted["scan_id"], competitor_id=competitors[0])
            self.assertEqual(len(ads), 50)
            self.assertTrue(all(ad["media_status"] == "available" for ad in ads))
            # The read-only projections (#332), read by the API role through RLS.
            listed = {item["competitor_id"]: item for item in fixture.client.get(
                "/api/v1/competitors", params={"product_id": fixture.product_id}).json()["items"]}
            self.assertEqual([(listed[cid]["observed_page_name"], listed[cid]["last_scanned_at"])
                              for cid in competitors],
                             [(name, scan["finished_at"]) for name in ("Acme Charge", "Brightvolt", "Halcyon Motors")])
            self.assertEqual([target["observed_page_name"] for target in scan["targets"]],
                             ["Acme Charge", "Brightvolt", "Halcyon Motors", None, None])

            # Another Workspace sees nothing and changes nothing.
            fixture.current = stranger
            fixture._problem(fixture.client.get(f"/api/v1/competitors/{competitors[0]}"), 404,
                             "competitor_not_found")
            for response in (
                fixture.client.get(f"/api/v1/competitor-scans/{accepted['scan_id']}"),
                fixture.client.get(f"/api/v1/competitor-scans/{accepted['scan_id']}/ads"),
                fixture.client.post(f"/api/v1/competitor-scans/{accepted['scan_id']}/resume",
                                    headers=fixture._headers()),
            ):
                fixture._problem(response, 404, "competitor_scan_not_found")
            fixture.current = fixture.owner_ctx(stranger)
            fixture._problem(fixture._reconcile(accepted["scan_id"], "pca_x", "charged"), 404,
                             "competitor_scan_not_found")
            self.assertEqual(fixture.client.get("/api/v1/competitor-scans").json()["items"], [])
            fixture.current = fixture.ctx

        with self.api.connect() as connection:
            for table in TABLES:
                visible = connection.execute(text(f"SELECT count(*) FROM {table}")).scalar()
                self.assertEqual(visible, 0, f"{table} leaked without a tenant context")

    def test_a_worker_killed_past_the_fence_is_recovered_by_the_dispatcher(self):
        """#327: ``marketai_claim_next_job_v2`` requeues the dead Worker's scan Job; the
        redelivered run leaves the page ``uncertain`` (never resent) and a resume settles it."""
        from tests.test_competitor_scans_api import Killed, expire_lease

        for balance, outcome, state in ((99, "not_charged", "released"), (98, "charged", "settled")):
            with self.subTest(balance=balance):
                fixture = self._fixture()
                with self._as(1):
                    acme = fixture._confirmed(ACME)
                    fixture.wrap = _kill_second_page
                    accepted = fixture._start(fixture._body([acme], keywords=[]))
                job_id = accepted["job_id"]
                lost_key = market_data_call_key(job_id, f"page:{acme}:2")
                with self._as(2), patch("app.platform.jobs.engine", self.worker):
                    runtime = fixture._runtime()
                    with self.assertRaises(Killed):
                        for _ in range(5):
                            asyncio.run(runtime.run_once(job_types=("competitor_scan.run",)))
                fixture.wrap = None
                with self._as(0):
                    expire_lease(fixture.ctx, job_id)
                sent_before = len(fixture.sources)
                self._work(fixture)
                with self._as(0), tenant_session(fixture.ctx) as session:
                    events = session.scalars(select(AuditEventRecord.event_type).where(
                        AuditEventRecord.resource_type == "job", AuditEventRecord.resource_id == job_id,
                    ).order_by(AuditEventRecord.created_at, AuditEventRecord.id)).all()
                    job = session.get(JobRecord, job_id)
                    attempts = session.scalars(select(ProviderCallAttemptRecord).where(
                        ProviderCallAttemptRecord.call_semantic_key_hash == lost_key)).all()
                self.assertIn("job.requeued", events)
                self.assertEqual((job.state, job.error_code), ("uncertain", "market_data_outcome_unknown"))
                self.assertEqual([attempt.dispatch_state for attempt in attempts], ["uncertain"])
                self.assertFalse([request for source in fixture.sources[sent_before:] for request in source.requests
                                  if request.page_id == ACME and request.cursor is not None])
                with self._as(1):
                    scan = fixture._scan(accepted["scan_id"])
                    self.assertEqual((scan["status"], scan["reconciliation"][0]["status"]), ("uncertain", "pending"))
                    reserved = [key for key, row in fixture._scan_reservations(job_id).items() if row.state == "reserved"]
                    self.assertEqual(reserved, [lost_key])
                    fixture.balance = balance
                    resumed = fixture._resume(accepted["scan_id"])
                self._work(fixture)
                with self._as(1):
                    scan = fixture._scan(accepted["scan_id"])
                    self.assertEqual((scan["status"], scan["reconciliation"][0]["status"]), ("completed", outcome))
                    self.assertEqual(fixture._scan_reservations(job_id)[lost_key].state, state)
                    for admitted in (job_id, resumed["job_id"]):
                        self.assertNotIn("reserved", {r.state for r in fixture._scan_reservations(admitted).values()})

    def test_an_owners_reconciliation_and_its_settlement_hold_under_the_roles(self):
        from tests.test_competitor_scans_api import Killed, expire_lease

        fixture = self._fixture()
        with self._as(1):
            acme = fixture._confirmed(ACME)
            fixture.wrap = _kill_second_page
            accepted = fixture._start(fixture._body([acme], keywords=[]))
        with self._as(2), patch("app.platform.jobs.engine", self.worker):
            runtime = fixture._runtime()
            with self.assertRaises(Killed):
                for _ in range(5):
                    asyncio.run(runtime.run_once(job_types=("competitor_scan.run",)))
        fixture.wrap = None
        with self._as(0):
            expire_lease(fixture.ctx, accepted["job_id"])
        self._work(fixture)  # the dispatcher's lease recovery redelivers the Job (#327)
        with self._as(1):
            scan = fixture._scan(accepted["scan_id"])
            self.assertEqual(scan["status"], "uncertain", scan)
            attempt_id = scan["reconciliation"][0]["attempt_id"]
            fixture.balance = 97  # neither 0 nor 1 off: the Owner decides
            resumed = fixture._resume(accepted["scan_id"])
        self._work(fixture)
        with self._as(1):
            scan = fixture._scan(accepted["scan_id"])
            self.assertEqual((scan["status"], scan["reconciliation"][0]["balance_check"]["outcome"]),
                             ("uncertain", "manual_required"))
            fixture._problem(fixture._reconcile(accepted["scan_id"], attempt_id, "not_charged"), 403,
                             "competitor_scan_reconcile_forbidden")
            fixture.current = fixture.owner_ctx(fixture.ctx)
            marked = fixture._reconcile(accepted["scan_id"], attempt_id, "not_charged")
            self.assertEqual((marked.status_code, marked.json()["status"]), (200, "partial"), marked.text)
            fixture.current = fixture.ctx
        self._work(fixture)
        with self._as(1):
            states = {r.state for r in fixture._scan_reservations(accepted["job_id"]).values()}
            self.assertNotIn("reserved", states)
            self.assertNotIn("reserved", {r.state for r in fixture._scan_reservations(resumed["job_id"]).values()})
            fixture.balance = None
            fixture._resume(accepted["scan_id"])
        self._work(fixture)
        with self._as(1):
            self.assertEqual(fixture._scan(accepted["scan_id"])["status"], "completed")

    def test_the_privilege_matrix_rls_and_append_only_triggers_hold(self):
        with self.owner.connect() as connection:
            for table in TABLES:
                forced = connection.execute(text(
                    "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE relname = :t"), {"t": table},
                ).scalar()
                self.assertTrue(forced, table)
                for role in ("marketai_domain_api", "marketai_domain_worker", "marketai_domain_readonly"):
                    expected = GRANTED[table].get(role, {"SELECT"})
                    for privilege in DML:
                        held = connection.execute(text("SELECT has_table_privilege(:r, :t, :p)"),
                                                  {"r": role, "t": table, "p": privilege}).scalar()
                        self.assertEqual(held, privilege in expected, f"{role} {privilege} {table}")
            for table in ("competitor_ads", "competitor_ad_origins"):
                trigger = connection.execute(text("SELECT count(*) FROM pg_trigger WHERE tgname = :n"),
                                             {"n": f"trg_{table}_append_only"}).scalar()
                self.assertEqual(trigger, 1, table)

        fixture = self._fixture()
        with self._as(1):
            accepted = fixture._start(fixture._body([fixture._confirmed(BRIGHTVOLT)], keywords=[]))
        self._work(fixture)
        with self._as(1):
            ad = fixture._all_ads(accepted["scan_id"])[0]
        # Even the schema owner cannot rewrite an observation or its origins.
        for statement in (
            update(CompetitorAdRecord).values(advertiser_name="x").where(CompetitorAdRecord.id == ad["ad_id"]),
            CompetitorAdRecord.__table__.delete().where(CompetitorAdRecord.id == ad["ad_id"]),
            update(CompetitorAdOriginRecord).values(origin="keyword:x").where(
                CompetitorAdOriginRecord.competitor_ad_id == ad["ad_id"]),
            CompetitorAdOriginRecord.__table__.delete().where(CompetitorAdOriginRecord.competitor_ad_id == ad["ad_id"]),
        ):
            with self.assertRaises(DBAPIError):
                with self._as(0), tenant_session(fixture.ctx) as session:
                    session.execute(statement)


def _kill_second_page(source):
    from tests.test_competitor_scans_api import Killed

    original = source._send

    async def send(request, *, credential):
        if request.page_id == ACME and request.cursor is not None:
            raise Killed("worker killed after the fence")
        return await original(request, credential=credential)

    source._send = send
    return source


if __name__ == "__main__":
    unittest.main()
