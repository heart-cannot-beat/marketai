"""Issue #273 against an isolated migrated PostgreSQL with the real API and Worker roles.

SQLite cannot see a missing grant or a tenant leak, so the drafting path runs
here as the roles that serve it in production: the API admits the draft
(reading the product switch, the scans and the snapshot it cites) and the
Worker drafts it (reading the snapshot again to cite it). Another Workspace
can neither read the plan nor resolve the snapshot it cites, even by ID.

Run like ``tests.test_competitor_scans_pg``.
"""
from __future__ import annotations

import asyncio
import uuid
from copy import deepcopy
from unittest.mock import patch

from sqlalchemy import text

from tests import test_competitor_snapshots_pg as snapshots_pg
from tests.test_marketing_languages import WORKFLOW_READ

from app.adapters.persistence.database import tenant_session
from app.library.contracts import ROLE_MEMBER
from app.marketing import competitor_review

ACME, BRIGHTVOLT = snapshots_pg.ACME, snapshots_pg.BRIGHTVOLT


class CompetitorPlanPostgresTests(snapshots_pg.CompetitorSnapshotPostgresTests):
    """Its setup, role sessions and Worker loop; the snapshot tests themselves run in
    ``tests.test_competitor_snapshots_pg``."""

    test_tags_clusters_snapshots_and_reclusters_run_under_their_runtime_roles = None
    test_a_key_refused_mid_tag_resumes_under_both_roles = None
    test_the_new_privileges_rls_and_append_only_triggers_hold = None

    def _plan_fixture(self):
        from tests.test_marketing_competitor_review import CompetitorPlanFixtures

        fixture = CompetitorPlanFixtures()
        with self._as(0):
            fixture.setUp()
            fixture.ctx = fixture._make_context(f"ws_plan_pg_{uuid.uuid4().hex[:10]}", role=ROLE_MEMBER,
                                                actor="member_a")
            fixture.current = fixture.ctx
            self.workspaces.append(fixture.ctx.workspace_id)
        self.addCleanup(fixture.doCleanups)
        return fixture

    def _work_all(self, fixture) -> None:
        with self._as(2), patch("app.platform.jobs.engine", self.worker):
            runtime = fixture._runtime()
            for _ in range(20):
                if not asyncio.run(runtime.run_once(job_types=("competitor_scan.run", "marketing.plan"))):
                    break

    def _cited_draft(self, fixture) -> tuple[dict, str, dict]:
        """A project drafted by the Worker citing the US snapshot its scan made: (project, snapshot ID, draft)."""
        with self._as(1):
            created = fixture.client.post("/api/v1/marketing/projects", headers=fixture._marketing_headers(),
                                          json={"source_text": "Brightvolt NACS Adapter\nCharges CCS1 cars at Superchargers."})
            self.assertEqual(created.status_code, 201, created.text)
            project = created.json()
            fixture._planning(project, "understanding")
        self._work_all(fixture)
        with self._as(1):
            project = fixture._read(project)
            understanding = project["understanding"]
            for fact in understanding["facts"]:
                fact["confirmation"] = "confirmed"
            confirmed = fixture._edit(project, "confirm_understanding", understanding=understanding)
            self.assertEqual(confirmed.status_code, 200, confirmed.text)
            project = confirmed.json()
            fixture._competitor_product = project["product_id"]
            accepted = fixture._start(fixture._body([fixture._confirmed(ACME), fixture._confirmed(BRIGHTVOLT, role="self")]))
        self._work_all(fixture)  # the scan and its snapshot, as the Worker
        with self._as(1):
            snapshot_id = fixture._scan(accepted["scan_id"])["snapshot_id"]
            self.assertTrue(snapshot_id)
            fixture._planning(project, "plan")  # the API reads the switch, the scans and the snapshot
        self._work_all(fixture)  # the Worker drafts and cites it
        with self._as(1):
            drafted = fixture._read(project)
            self.assertEqual(drafted["latest_call"]["state"], "succeeded", drafted["latest_call"])
        return project, snapshot_id, drafted

    def test_a_draft_cites_its_snapshot_under_the_runtime_roles_and_nowhere_else(self):
        fixture = self._plan_fixture()
        stranger = self._stranger(fixture)
        project, snapshot_id, drafted = self._cited_draft(fixture)
        with self._as(1):
            self.assertEqual(drafted["plan"]["competitor_snapshot_id"], snapshot_id)
            kinds = {item["kind"] for item in drafted["plan"]["plan_review"]}
            self.assertTrue({"competitor_crowded", "competitor_gap"} <= kinds, kinds)
            self.assertFalse(drafted["competitor_snapshot_stale"])

            plan = deepcopy(drafted["plan"])
            plan["combinations"][0]["headline"] = "Tired of Compatibility Headaches? Not Anymore."
            saved = fixture._saved(drafted, plan)
            self.assertEqual([r["kind"] for r in saved["plan"]["plan_review"] if r["severity"] == "blocking"],
                             ["competitor_copy_reuse"])

            # Another Workspace reads no plan and resolves no snapshot, even by its ID.
            for current in (stranger, fixture.owner_ctx(stranger)):
                fixture.current = current
                response = fixture.client.get(f"/api/v1/marketing/projects/{project['id']}")
                self.assertEqual(response.status_code, 404, response.text)
                with tenant_session(current) as session:
                    self.assertIsNone(competitor_review.snapshot(session, current, snapshot_id, project["product_id"]))
                    self.assertIsNone(competitor_review.draft_snapshot(
                        session, current, type("Row", (), {"product_id": project["product_id"]})(), None))
            fixture.current = fixture.ctx

        with self.api.connect() as connection:
            visible = connection.execute(text("SELECT count(*) FROM competitor_scan_snapshots")).scalar()
            self.assertEqual(visible, 0, "competitor_scan_snapshots leaked without a tenant context")

    def test_a_cited_plan_reads_like_its_ended_round_from_postgresql(self):
        """#367: the JSONB plan and the JSONB round snapshot serialize key for key alike, competitor review aside."""
        fixture = self._plan_fixture()
        stranger = self._stranger(fixture)
        project, _, drafted = self._cited_draft(fixture)
        with self._as(1):
            confirmed = fixture._saved(drafted, {**drafted["plan"], "executions_per_combination": 1}, "confirm_plan")
            row = fixture._ended_round(confirmed)
            snapshot = fixture._frozen_snapshot(row)
            fixture.assert_reads_like_its_round(confirmed, row)
            self.assertEqual(fixture._frozen_snapshot(row), snapshot)
            for current in (stranger, fixture.owner_ctx(stranger)):
                fixture.current = current
                response = fixture.client.get(f"/api/v1/marketing/projects/{project['id']}/workflow?{WORKFLOW_READ}")
                self.assertEqual(response.status_code, 404, response.text)
            fixture.current = fixture.ctx
            self.assertEqual(fixture._frozen_snapshot(row), snapshot)
