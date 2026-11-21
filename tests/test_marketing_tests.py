"""Test projects (ADR 0019, #247): kept ads taken to a test open a project of their own.

A test project is a marketing project with ``flow = "ads_test"`` that names the ads-first ideas it came from.
These tests build one directly -- its plan copied from a confirmed plan-first test, so it passes every plan
check -- with a round frozen by the ordinary freeze; the command that creates one comes with #249.
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import new_id, utcnow
from app.main import app
from app.marketing import ad_tests, ideas, service, workflow
from app.marketing.ad_tests_contracts import MarketingTestDetail
from app.marketing.models import MarketingProjectRecord
from app.marketing.workflow_contracts import EvaluationProtocol, FreezeRound
from app.platform.http_identity import request_tenant_context
from tests.test_marketing_ideas import MarketingIdeasFixtures
from tests.test_marketing_workflow import MarketingWorkflowFixtures

SOURCE = "Acme Charge Travel Charger\n65W, three ports, for business travellers."


class MarketingTestFixtures(MarketingWorkflowFixtures, MarketingIdeasFixtures):
    def create(self):
        # The plan-first test whose confirmed plan the test projects copy: an Acme Charge charger.
        response = self.client.post("/api/v1/marketing/projects?include_languages=true&include_plan_review=true"
                                    "&include_brand_style=true&include_execution_plan=true", headers=self.headers(),
                                    json={"source_text": SOURCE})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def confirmed_plan(self):
        """The plan and understanding of a confirmed plan-first test, as stored; one per test."""
        if not hasattr(self, "_confirmed"):
            project = self.confirmed()
            with tenant_session(self.context) as session:
                row = session.get(MarketingProjectRecord, project["id"])
                self._confirmed = (project["id"], row.plan_json, row.understanding_json)
        return self._confirmed

    def use(self, ctx):
        app.dependency_overrides[request_tenant_context] = lambda: ctx

    def ideas_project(self, ctx=None):
        ctx = ctx or self.context
        with tenant_session(ctx) as session:
            return ideas.create_ideas_project(session, ctx, product_id=self.product(ctx), source_text=SOURCE,
                                              market="United States", ad_language="en-US").id

    def make_test(self, ctx=None, *, source=None, plan=None, freeze=True):
        """A plan-confirmed test project of ``source`` (new ideas when omitted) and, with ``freeze``, its round."""
        ctx = ctx or self.context
        _, stored_plan, understanding = self.confirmed_plan()
        plan = plan or stored_plan
        source = source or self.ideas_project(ctx)
        with tenant_session(ctx) as session:
            ideas_row = session.get(MarketingProjectRecord, source)
            row = MarketingProjectRecord(id=new_id("mp"), workspace_id=ctx.workspace_id, product_id=ideas_row.product_id,
                flow=ad_tests.FLOW, state="plan_confirmed", revision=1, source_text=ideas_row.source_text,
                understanding_json=understanding, plan_json=plan, market=ideas_row.market,
                ad_language=ideas_row.ad_language, source_project_id=ideas_row.id)
            session.add(row)
            session.flush()
            service.checkpoint(session, ctx, row, "confirm_plan")
            round_id = None
            if freeze:
                start = utcnow().date() + timedelta(days=1)
                round_id = workflow.freeze(session, ctx, row, row.revision, FreezeRound(
                    start_date=start, end_date=start + timedelta(days=plan["duration_days"] - 1),
                    account_timezone="UTC", execution_mode="byok", protocol=EvaluationProtocol())).id
            return row.id, round_id

    def close(self, round_id):
        row = self.client.get(f"/api/v1/marketing/rounds/{round_id}").json()["data"]
        return self.command(row, "close", {"reason": "user_stopped", "note": "Stopped before the ads went live"})


class TestProjectSchemaTests(MarketingTestFixtures):
    def test_a_test_project_names_the_ideas_it_came_from(self):
        ideas_id = self.ideas_project()
        test_id, _ = self.make_test(source=ideas_id, freeze=False)
        with tenant_session(self.context) as session:
            row = session.get(MarketingProjectRecord, test_id)
            self.assertEqual((row.flow, row.state, row.source_project_id), ("ads_test", "plan_confirmed", ideas_id))
            self.assertEqual(row.product_id, session.get(MarketingProjectRecord, ideas_id).product_id)

        # The source is bound in the test's own Workspace: another Workspace cannot point at these ideas.
        other = self._make_context()
        product = self.product(other)
        with self.assertRaises(IntegrityError):
            with tenant_session(other) as session:
                session.add(MarketingProjectRecord(id=new_id("mp"), workspace_id=other.workspace_id, product_id=product,
                    flow=ad_tests.FLOW, state="plan_confirmed", revision=1, source_text=SOURCE, source_project_id=ideas_id))
                session.flush()
        with tenant_session(other) as session:
            self.assertEqual(session.scalars(select(MarketingProjectRecord).where(
                MarketingProjectRecord.workspace_id == other.workspace_id,
                MarketingProjectRecord.flow == ad_tests.FLOW)).all(), [])

    def test_a_test_project_must_name_its_ideas(self):
        product = self.product()
        with self.assertRaises(IntegrityError):
            with tenant_session(self.context) as session:
                session.add(MarketingProjectRecord(id=new_id("mp"), workspace_id=self.context.workspace_id, product_id=product,
                    flow=ad_tests.FLOW, state="plan_confirmed", revision=1, source_text=SOURCE))
                session.flush()

    def test_existing_projects_name_no_source(self):
        plan_first, _, _ = self.confirmed_plan()
        ideas_id = self.ideas_project()
        with tenant_session(self.context) as session:
            for project_id in (plan_first, ideas_id):
                self.assertIsNone(session.get(MarketingProjectRecord, project_id).source_project_id)


class TestProjectRoundPathTests(MarketingTestFixtures):
    def test_round_paths_accept_a_test_project_and_plan_paths_do_not(self):
        plan_first, plan, _ = self.confirmed_plan()
        ideas_id = self.ideas_project()
        test_id, round_id = self.make_test(source=ideas_id)

        # The round, read by its id, like any small round; its board offers no generation (a test's plan
        # never generates images).
        response = self.client.get(f"/api/v1/marketing/rounds/{round_id}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()["data"]["state"], response.json()["data"]["ordinal"]), ("reviewing", 1))
        response = self.client.get(f"/api/v1/marketing/rounds/{round_id}?include_board=true&include_reuse_lineage=true"
                                   "&include_qc_kinds=true&include_execution_plan=true")
        self.assertEqual(response.status_code, 200, response.text)
        slots = [execution for angle in response.json()["data"]["board"]["tree"]["angles"]
                 for expression in angle["expressions"] for combination in expression["combinations"]
                 for execution in combination["executions"]]
        self.assertTrue(slots)
        self.assertEqual({(slot["stage"], slot["next_action"]) for slot in slots}, {("pending_generation", "none")})
        with tenant_session(self.context) as session:
            self.assertEqual(service.get_round_project(session, self.context, test_id).id, test_id)
            self.assertEqual(service.get_round_project(session, self.context, plan_first).id, plan_first)
            with self.assertRaises(service.MarketingError) as caught:
                service.get_round_project(session, self.context, ideas_id)
            self.assertEqual((caught.exception.code, caught.exception.status), ("marketing_project_not_found", 404))

        # The plan-first project paths -- read, edit the plan, redraft, change the understanding, generate from
        # the plan, freeze, read the workflow -- do not find it.
        refused = {
            "read": self.client.get(f"/api/v1/marketing/projects/{test_id}"),
            "workflow": self.client.get(f"/api/v1/marketing/projects/{test_id}/workflow"),
            "edit": self.client.patch(f"/api/v1/marketing/projects/{test_id}", headers=self.headers(1),
                                           json={"command": "revise_source", "source_text": SOURCE}),
            "redraft": self.client.post(f"/api/v1/marketing/projects/{test_id}/planning", headers=self.headers(1),
                                        json={"kind": "plan", "execution_mode": "mock", "quote_hash": "",
                                              "ad_language": "en-US", "strategy_language": "en-US"}),
            "understanding": self.client.post(f"/api/v1/marketing/projects/{test_id}/planning", headers=self.headers(1),
                                              json={"kind": "understanding", "execution_mode": "mock", "quote_hash": ""}),
            "generate": self.client.post(f"/api/v1/marketing/projects/{test_id}/generation-drafts", headers=self.headers(1),
                                         json={"provider": "mock", "model": "mock-v1"}),
            "freeze": self.client.post(f"/api/v1/marketing/projects/{test_id}/rounds", headers=self.headers(1), json={
                "start_date": "2030-01-01", "end_date": f"2030-01-{plan['duration_days']:02d}", "account_timezone": "UTC",
                "execution_mode": "byok", "protocol": {}}),
            "ideas": self.client.get(f"/api/v1/marketing/ideas/{test_id}"),
        }
        self.assertEqual({name: (r.status_code, r.json().get("code")) for name, r in refused.items()},
                         {name: (404, "marketing_project_not_found") for name in refused})

        # Neither list returns it: the plan-first list keeps its test, the ideas list its ideas.
        listed = [item["id"] for item in self.client.get("/api/v1/marketing/projects").json()["items"]]
        self.assertIn(plan_first, listed)
        self.assertNotIn(test_id, listed)
        listed = [item["id"] for item in self.client.get("/api/v1/marketing/ideas").json()["items"]]
        self.assertIn(ideas_id, listed)
        self.assertNotIn(test_id, listed)

        # A round command by round id works on it: here, ending the round.
        closed = self.close(round_id)
        self.assertEqual((closed["state"], closed["close"]["reason"]), ("closed", "user_stopped"))

    def test_another_workspace_cannot_read_a_test_or_its_rounds(self):
        test_id, round_id = self.make_test()
        with tenant_session(self.context) as session:
            product_id = session.get(MarketingProjectRecord, test_id).product_id
        other = self._make_context()
        self.use(other)
        missing_test = self.client.get(f"/api/v1/marketing/tests/{new_id('mp')}")
        missing_round = self.client.get(f"/api/v1/marketing/rounds/{new_id('mround')}")
        responses = {
            "test": (self.client.get(f"/api/v1/marketing/tests/{test_id}"), missing_test),
            "round": (self.client.get(f"/api/v1/marketing/rounds/{round_id}"), missing_round),
            "board": (self.client.get(f"/api/v1/marketing/rounds/{round_id}?include_board=true"), missing_round),
        }
        for name, (response, missing) in responses.items():
            # Indistinguishable from an id that never existed: the test's existence does not leak.
            self.assertEqual((response.status_code, response.json()["code"]), (missing.status_code, missing.json()["code"]), name)
            self.assertEqual(response.json()["detail"], missing.json()["detail"], name)
        self.assertEqual((missing_test.status_code, missing_test.json()["code"]), (404, "marketing_test_not_found"))
        self.assertEqual((missing_round.status_code, missing_round.json()["code"]), (404, "round_not_found"))
        close = self.client.post(f"/api/v1/marketing/rounds/{round_id}/close", headers=self.headers(1),
                                 json={"reason": "user_stopped", "note": "Not mine"})
        self.assertEqual((close.status_code, close.json()["code"]), (404, "round_not_found"))
        with tenant_session(other) as session:
            self.assertIsNone(ad_tests.in_progress(session, other, product_id))
            for lookup in (ad_tests.get_test, service.get_round_project):
                with self.assertRaises(service.MarketingError) as caught:
                    lookup(session, other, test_id)
                self.assertEqual(caught.exception.status, 404)
        # Its own Workspace still reads it, and the round is still open.
        self.use(self.context)
        self.assertEqual(self.client.get(f"/api/v1/marketing/tests/{test_id}").json()["current_round_id"], round_id)


class TestProjectReadTests(MarketingTestFixtures):
    def test_a_test_reads_its_source_plan_summary_and_rounds(self):
        _, plan, _ = self.confirmed_plan()
        ideas_id = self.ideas_project()
        test_id, round_id = self.make_test(source=ideas_id)
        with tenant_session(self.context) as session:
            row = session.get(MarketingProjectRecord, test_id)
            frozen = workflow.get_round(session, self.context, round_id)
            expected_dates = (str(frozen.start_date), str(frozen.end_date))
            product_id, created_at = row.product_id, row.created_at.isoformat()

        for ctx in (self.context, self.owner_ctx(self.context)):  # a Member and the Owner both read it
            self.use(ctx)
            response = self.client.get(f"/api/v1/marketing/tests/{test_id}")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers["etag"], '"1"')
            body = response.json()
            MarketingTestDetail.model_validate(body)
            self.assertEqual(body, {
                "id": test_id, "revision": 1, "product_id": product_id, "source_project_id": ideas_id,
                "comparison": "claims",
                "plan_summary": {"target_cost": str(plan["rules"]["threshold"]), "round_budget": str(plan["small_round_budget"]),
                                 "duration_days": plan["duration_days"], "currency": plan["currency"],
                                 "landing_page": plan["landing_page"]},
                "rounds": [{"id": round_id, "ordinal": 1, "state": "reviewing",
                            "start_date": expected_dates[0], "end_date": expected_dates[1],
                            "effective_start_date": expected_dates[0], "effective_end_date": expected_dates[1]}],
                "current_round_id": round_id, "created_at": created_at})

        self.close(round_id)
        body = self.client.get(f"/api/v1/marketing/tests/{test_id}").json()
        self.assertEqual(([r["state"] for r in body["rounds"]], body["current_round_id"]), (["closed"], None))

    def test_only_a_test_project_is_read_as_a_test(self):
        plan_first, _, _ = self.confirmed_plan()
        ideas_id = self.ideas_project()
        for project_id in (plan_first, ideas_id, "mp_missing"):
            response = self.client.get(f"/api/v1/marketing/tests/{project_id}")
            self.assertEqual((response.status_code, response.json()["code"]), (404, "marketing_test_not_found"), project_id)

    def test_the_comparison_follows_the_plan(self):
        combinations = [{"id": "c1", "angle_id": "a1"}, {"id": "c2", "angle_id": "a1"}, {"id": "c3", "angle_id": "a2"}]

        def plan(kind, *groups):
            return {"comparison_kind": kind, "combinations": combinations,
                    "groups": [{"combination_ids": list(ids)} for ids in groups]}
        self.assertEqual(ad_tests.comparison(plan("whole_combinations", ["c1"], ["c3"])), "claims")
        self.assertEqual(ad_tests.comparison(plan("whole_combinations", ["c1", "c2"], ["c3"])), "claims")
        self.assertEqual(ad_tests.comparison(plan("whole_combinations", ["c1"], ["c2"])), "expressions")
        self.assertEqual(ad_tests.comparison(plan("business_retest", ["c1", "c3"])), "single")

    def test_a_product_has_a_test_in_progress_until_its_round_closes(self):
        ideas_id = self.ideas_project()
        with tenant_session(self.context) as session:
            product_id = session.get(MarketingProjectRecord, ideas_id).product_id
            self.assertIsNone(ad_tests.in_progress(session, self.context, product_id))
        self.make_test(source=ideas_id, freeze=False)
        with tenant_session(self.context) as session:  # a test project without a round is not under way
            self.assertIsNone(ad_tests.in_progress(session, self.context, product_id))
        test_id, round_id = self.make_test(source=ideas_id)
        elsewhere, _ = self.make_test()  # another product's test is not this product's
        with tenant_session(self.context) as session:
            self.assertEqual(ad_tests.in_progress(session, self.context, product_id).id, test_id)
            self.assertIsNone(ad_tests.in_progress(session, self.context, "prod_missing"))
        self.close(round_id)
        with tenant_session(self.context) as session:
            self.assertIsNone(ad_tests.in_progress(session, self.context, product_id))
            self.assertEqual(ad_tests.in_progress(session, self.context, session.get(
                MarketingProjectRecord, elsewhere).product_id).id, elsewhere)


class TestProjectMigrationDowngradeTests(unittest.TestCase):
    """Dropping ``source_project_id`` would lose which ideas each test came from, so downgrade refuses."""
    ROOT = Path(__file__).resolve().parents[1]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="marketai-test-projects-migration-")
        self.addCleanup(self.temp.cleanup)
        self.database = f"{self.temp.name}/migration.db"
        self.environment = {**os.environ, "MARKETAI_DATABASE_URL": f"sqlite:///{self.database}",
                            "MARKETAI_DATA_DIR": f"{self.temp.name}/data"}
        upgraded = self.alembic("upgrade", "head")
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)

    def alembic(self, *arguments):
        return subprocess.run([sys.executable, "-m", "alembic", *arguments], cwd=self.ROOT, env=self.environment,
                              capture_output=True, text=True, check=False)

    def columns(self):
        with sqlite3.connect(self.database) as connection:
            return {row[1] for row in connection.execute("PRAGMA table_info(marketing_projects)")}

    def test_downgrade_without_test_projects_drops_the_column(self):
        downgraded = self.alembic("downgrade", "zzs241a1b2c3")
        self.assertEqual(downgraded.returncode, 0, downgraded.stderr)
        self.assertNotIn("source_project_id", self.columns())
        with sqlite3.connect(self.database) as connection:
            indexes = {row[1] for row in connection.execute("PRAGMA index_list(marketing_projects)")}
        self.assertNotIn("ix_marketing_projects_source", indexes)

    def test_downgrade_refuses_while_a_test_project_exists(self):
        with sqlite3.connect(self.database) as connection:
            connection.execute("INSERT INTO marketing_projects (id, workspace_id, revision, state, source_text, flow,"
                               " created_at, updated_at) VALUES ('mp_1', 'ws_1', 1, 'ideas_ready', 'Acme Charge',"
                               " 'ads_first', '2026-11-21', '2026-11-21')")
            connection.execute("INSERT INTO marketing_projects (id, workspace_id, revision, state, source_text, flow,"
                               " source_project_id, created_at, updated_at) VALUES ('mp_2', 'ws_1', 1, 'plan_confirmed',"
                               " 'Acme Charge', 'ads_test', 'mp_1', '2026-11-21', '2026-11-21')")
        downgraded = self.alembic("downgrade", "zzs241a1b2c3")
        self.assertNotEqual(downgraded.returncode, 0)
        self.assertIn("ads_test projects exist", downgraded.stderr)
        self.assertIn("source_project_id", self.columns())
