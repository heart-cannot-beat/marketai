"""Issues #337 and #343 against an isolated migrated PostgreSQL with the real API and Worker roles.

The legacy ``create``, ``revise`` and ``runs`` commands are frozen. Here they
are called as the API role that serves them in production, by a Member, an
Owner and another Workspace, on a plan that already carries a sample Run
(production's two ready plans do) and its product: every call answers ``410
legacy_test_plans_frozen`` and no plan, direction, run, Generation, Job,
operation, reservation, Library or audit row appears or changes in either
Workspace. The plan stays readable through RLS, and another Workspace still
reads it as not found.

Run like ``tests.test_competitor_scans_pg``.
"""
from __future__ import annotations

import asyncio
import os
import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import create_engine, update
from sqlalchemy.orm import sessionmaker

from app.adapters.persistence.models import JobRecord, utcnow
from app.library.contracts import ROLE_MEMBER, ROLE_OWNER
from app.plans.service import JOB_TYPE

URLS = [os.getenv(key) for key in ("DOMAIN_OWNER_DATABASE_URL", "DOMAIN_API_DATABASE_URL", "DOMAIN_WORKER_DATABASE_URL")]


@unittest.skipUnless(all(URLS) and os.getenv("MARKETAI_TENANT_CONTEXT_SECRET"),
                     "requires isolated PostgreSQL runtime roles")
class FrozenTestPlanPostgresTests(unittest.TestCase):
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
        from tests.test_test_plans_api import FrozenCommandFixtures

        fixture = FrozenCommandFixtures()
        with self._as(0):
            fixture.setUp()
            fixture.ctx = fixture._make_context(f"ws_plans_pg_{uuid.uuid4().hex[:10]}", role=ROLE_MEMBER,
                                                actor="member_a")
            fixture.current = fixture.ctx
            self.workspaces.append(fixture.ctx.workspace_id)
        self.addCleanup(fixture.doCleanups)
        return fixture

    def _draft(self) -> None:
        from app.worker_runtime import WorkerRuntime

        with self._as(2), patch("app.platform.jobs.engine", self.worker):
            runtime = WorkerRuntime(owner=f"plans-pg-{uuid.uuid4().hex[:6]}")
            for _ in range(10):
                if not asyncio.run(runtime.run_once(job_types=(JOB_TYPE,))):
                    break

    def test_frozen_commands_answer_410_and_write_nothing_under_the_api_role(self):
        fixture = self._fixture()
        with self._as(0):  # the control plane opens Workspaces, not the runtime roles
            stranger = fixture._make_context(f"ws_plans_pg_other_{uuid.uuid4().hex[:8]}", role=ROLE_OWNER,
                                             actor="owner_b")
        self.workspaces.append(stranger.workspace_id)

        with self._as(1):
            # The domain admits the plan as the API role (the route answers 410, #343)...
            accepted = fixture._create({"product_id": fixture._product()})
        self._draft()  # ...and the Worker drafts it.
        with self._as(1):
            plan = fixture._get(accepted["plan_id"])
            self.assertEqual(plan["status"], "ready", plan.get("failure"))
            # History built through the domain, as production's ready plans have.
            sample = fixture._run(plan["plan_id"], "sample")
            self.assertEqual(sample["status"], "submitted")

        with self._as(0):
            before = fixture._footprint(fixture.ctx, stranger)

        with self._as(1):
            for caller in (fixture.ctx, fixture.owner_ctx(fixture.ctx), stranger):
                fixture.current = caller
                fixture.assert_frozen(fixture._post_frozen(plan["plan_id"], plan["product_id"]))
            fixture.assert_frozen(fixture._post_frozen("atp_never_issued", "prd_never_issued"))

        with self._as(0):
            self.assertEqual(fixture._footprint(fixture.ctx, stranger), before)

        with self._as(1):
            fixture.current = fixture.ctx
            detail = fixture._get(plan["plan_id"])
            self.assertEqual(detail["revision"], plan["revision"])
            self.assertEqual([(run["stage"], run["run_id"]) for run in detail["runs"]],
                             [("sample", sample["run_id"])])
            fixture.current = stranger
            self.assertEqual(fixture._get(plan["plan_id"], expect=404)["code"], "test_plan_not_found")


if __name__ == "__main__":
    unittest.main()
