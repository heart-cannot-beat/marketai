"""Issue #269 against an isolated migrated PostgreSQL with the real API and Worker roles.

SQLite cannot see a missing grant (#284), so every competitor write command
runs here as the role that serves it in production: the API creates
competitors (link and name), admits a byok and a mock resolve, confirms and
archives; the Worker runs the resolves (resolve rows advanced, attempts,
settlement, an automatic confirmation). Then the privilege matrix itself,
FORCE'd RLS, the append-only trigger, and another Workspace and a
non-member being refused.

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

from sqlalchemy import create_engine, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import JobRecord, utcnow
from app.competitors.models import CompetitorIdentityConfirmationRecord
from app.library.contracts import ROLE_MEMBER
from tests.test_test_plans_api import PROTECTOR

URLS = [os.getenv(key) for key in ("DOMAIN_OWNER_DATABASE_URL", "DOMAIN_API_DATABASE_URL", "DOMAIN_WORKER_DATABASE_URL")]
TABLES = ("competitors", "competitor_resolutions", "competitor_identity_confirmations")
#: table -> {role: privileges it must have}; every other DML privilege must be absent.
GRANTED = {
    "competitors": {"marketai_domain_api": {"SELECT", "INSERT", "UPDATE"}, "marketai_domain_worker": {"SELECT"}},
    "competitor_resolutions": {"marketai_domain_api": {"SELECT", "INSERT"},
                               "marketai_domain_worker": {"SELECT", "UPDATE"}},
    "competitor_identity_confirmations": {"marketai_domain_api": {"SELECT", "INSERT"},
                                          "marketai_domain_worker": {"SELECT", "INSERT"}},
}
DML = ("SELECT", "INSERT", "UPDATE", "DELETE")


@unittest.skipUnless(all(URLS) and os.getenv("MARKETAI_TENANT_CONTEXT_SECRET"),
                     "requires isolated PostgreSQL runtime roles")
class CompetitorPostgresTests(unittest.TestCase):
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
                JobRecord.state.in_(("queued", "retry_wait", "leased", "running")),
            ).values(state="cancelled", finished_at=utcnow(), lease_owner=None, lease_expires_at=None,
                     heartbeat_at=None))

    def _as(self, index: int):
        return patch("app.adapters.persistence.database.SessionLocal", self.sessions[index])

    def _fixture(self):
        from tests.test_competitors_api import CompetitorFixtures

        fixture = CompetitorFixtures()
        with self._as(0):
            fixture.setUp()
            fixture.ctx = fixture._make_context(f"ws_competitor_pg_{uuid.uuid4().hex[:10]}",
                                                role=ROLE_MEMBER, actor="member_a")
            fixture.current = fixture.ctx
            self.workspaces.append(fixture.ctx.workspace_id)
            fixture._competitor_product = fixture._product()
            fixture._bind_market_data()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def _work(self, fixture) -> None:
        from app.worker_runtime import WorkerRuntime

        with self._as(2), patch("app.platform.jobs.engine", self.worker):
            runtime = WorkerRuntime(owner=f"competitor-pg-{uuid.uuid4().hex[:6]}")
            runtime.blob_store = fixture.blob_store
            runtime.ad_library_source = fixture._source
            runtime._market_data_protector = PROTECTOR
            for _ in range(20):
                if not asyncio.run(runtime.run_once(job_types=("competitor.resolve",))):
                    break

    def test_every_competitor_write_runs_under_its_runtime_role(self):
        fixture = self._fixture()
        with self._as(0):  # the control plane opens Workspaces, not the runtime roles
            stranger = fixture._make_context(f"ws_competitor_pg_other_{uuid.uuid4().hex[:8]}", role=ROLE_MEMBER,
                                             actor="member_b")
        self.workspaces.append(stranger.workspace_id)
        with self._as(1):
            linked = fixture._competitor(f"https://www.facebook.com/profile.php?id=100000000000010", role="self")
            self.assertEqual(linked["confirmation"]["source"], "link")
            named = fixture._competitor("Acme Charge", website="acme-charge.example")
            other = fixture._competitor("Brightvolt")
            quote = fixture._quote(named["competitor_id"])
            self.assertEqual((quote["execution_mode"], quote["max_credits"]), ("byok", 4))
            byok = fixture._resolve(named["competitor_id"])
            mock = fixture._resolve(other["competitor_id"], {"execution_mode": "mock"})
            self.assertEqual(len(fixture._reservations(byok["resolve_job_id"])), 4)

        self._work(fixture)

        with self._as(1):
            resolved = fixture._resolution(named["competitor_id"], byok["resolve_job_id"])
            self.assertEqual((resolved["status"], resolved["auto_confirmed"]), ("completed", True), resolved)
            self.assertEqual(resolved["cost"]["usd"], "0.00752")
            self.assertEqual(set(fixture._reservations(byok["resolve_job_id"]).values()), {"settled"})
            mocked = fixture._resolution(other["competitor_id"], mock["resolve_job_id"])
            self.assertEqual(mocked["status"], "completed", mocked)
            # The Worker archived the Mock's avatars into the resolve (#332); the byok
            # stand-in's links are not on Meta's CDN, so its cards keep placeholders.
            self.assertTrue(all(candidate["avatar"] for candidate in mocked["candidates"]))
            self.assertTrue(all(candidate["avatar"] is None for candidate in resolved["candidates"]))
            self.assertEqual(fixture._get(named["competitor_id"])["confirmation"]["source"], "auto")

            body = {"resolve_job_id": mock["resolve_job_id"], "page_id": "100000000000016"}
            first = fixture._confirm(other["competitor_id"], body)
            again = fixture._confirm(other["competitor_id"], body)
            self.assertEqual((first["confirmation"]["revision"], again["confirmation"]["revision"]), (1, 1))
            self.assertTrue(again["replayed"])

            fixture._problem(fixture.client.post(f"/api/v1/competitors/{other['competitor_id']}/archive",
                                                 headers=fixture._headers()), 403, "competitor_archive_forbidden")
            fixture.current = fixture.owner_ctx(fixture.ctx)
            archived = fixture.client.post(f"/api/v1/competitors/{other['competitor_id']}/archive",
                                           headers=fixture._headers())
            self.assertEqual((archived.status_code, archived.json()["status"]), (200, "archived"), archived.text)

            # Another Workspace (a non-member of this one) sees nothing and changes nothing.
            fixture.current = stranger
            for response in (
                fixture.client.get(f"/api/v1/competitors/{named['competitor_id']}"),
                fixture.client.get(f"/api/v1/competitors/{named['competitor_id']}/resolutions/{byok['resolve_job_id']}"),
                fixture.client.post(f"/api/v1/competitors/{named['competitor_id']}/confirm", json=body,
                                    headers=fixture._headers()),
                fixture.client.post(f"/api/v1/competitors/{named['competitor_id']}/resolve", json={},
                                    headers=fixture._headers()),
            ):
                fixture._problem(response, 404, "competitor_not_found")
            self.assertEqual(fixture.client.get("/api/v1/competitors").json()["items"], [])
            fixture._problem(fixture.client.post("/api/v1/competitors", headers=fixture._headers(),
                                                 json={"product_id": fixture.product_id, "input": "Halcyon Motors"}),
                             404, "product_not_found")

        with self.api.connect() as connection:
            for table in TABLES:
                visible = connection.execute(text(f"SELECT count(*) FROM {table}")).scalar()
                self.assertEqual(visible, 0, f"{table} leaked without a tenant context")

    def test_the_privilege_matrix_rls_and_append_only_trigger_hold(self):
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
            trigger = connection.execute(text(
                "SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_competitor_identity_confirmations_append_only'"
            )).scalar()
            self.assertEqual(trigger, 1)
            step_kind = connection.execute(text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_provider_call_attempts_ck_provider_call_attempt_step_kind'")).scalar()
            self.assertIn("market_data", step_kind)

        fixture = self._fixture()
        with self._as(1):
            competitor = fixture._competitor("https://www.facebook.com/profile.php?id=100000000000016")
        # Even the schema owner cannot rewrite a confirmation.
        for statement in (
            update(CompetitorIdentityConfirmationRecord).values(page_id="1").where(
                CompetitorIdentityConfirmationRecord.competitor_id == competitor["competitor_id"]),
            CompetitorIdentityConfirmationRecord.__table__.delete().where(
                CompetitorIdentityConfirmationRecord.competitor_id == competitor["competitor_id"]),
        ):
            with self.assertRaises(DBAPIError):
                with self._as(0), tenant_session(fixture.ctx) as session:
                    session.execute(statement)


if __name__ == "__main__":
    unittest.main()
