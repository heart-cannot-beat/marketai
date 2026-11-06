"""Issue #272 against an isolated migrated PostgreSQL with the real API and Worker roles.

SQLite cannot see a missing grant (#284), so every C-23 write runs here as
the role that serves it in production:

* the Worker appends a byok scan's AI tag revisions (``tag:<i>`` settled,
  the ordinals outside the table released), its clusters revision and its
  snapshot, and points the scan at it;
* the API appends a Member's tag confirmation, refreezes (an unchanged hash
  makes no row), admits a paid recluster (``cluster:1`` reserved for its own
  Job) and switches the product's drafting flag (UPDATE on ``products``);
* the Worker runs that recluster (a second clusters revision, a new
  snapshot, the reservation settled);
* a scan whose AI key was refused mid-tag resumes under both roles with no
  reservation left open.

Then the privilege matrix of the two new tables and of the new column,
their FORCE'd RLS and append-only triggers, and another Workspace (a
non-member here) refused with 404 on every new route.

Run with ``DOMAIN_OWNER_DATABASE_URL``, ``DOMAIN_API_DATABASE_URL``,
``DOMAIN_WORKER_DATABASE_URL``, ``MARKETAI_DATABASE_URL`` (the owner) and
``MARKETAI_TENANT_CONTEXT_SECRET`` against a cluster released with
``make db-release``.
"""
from __future__ import annotations

import copy
import unittest
import uuid
from unittest.mock import patch

import httpx
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from tests import test_competitor_scans_pg as scans_pg
from tests.test_competitor_snapshots import VOCABULARY

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import ProductRecord
from app.competitors.models import CompetitorScanAssessmentRecord, CompetitorScanSnapshotRecord
from app.library.contracts import ROLE_MEMBER
from app.providers.llm_chat import llm_call_key

ACME, BRIGHTVOLT = scans_pg.ACME, scans_pg.BRIGHTVOLT
TABLES = ("competitor_scan_assessments", "competitor_scan_snapshots")
GRANTED = {
    "competitor_scan_assessments": {"marketai_domain_api": {"SELECT"},
                                    "marketai_domain_worker": {"SELECT", "INSERT"}},
    "competitor_scan_snapshots": {"marketai_domain_api": {"SELECT", "INSERT"},
                                  "marketai_domain_worker": {"SELECT", "INSERT"}},
}
DML = ("SELECT", "INSERT", "UPDATE", "DELETE")
REJECTED = httpx.Response(401, json={"error": {"message": "bad key", "code": "invalid_api_key"}})


class CompetitorSnapshotPostgresTests(scans_pg.CompetitorScanPostgresTests):
    """Its setup, role sessions and Worker loop; the scan tests themselves run in
    ``tests.test_competitor_scans_pg``."""

    test_every_scan_write_runs_under_its_runtime_role = None
    test_an_owners_reconciliation_and_its_settlement_hold_under_the_roles = None
    test_the_privilege_matrix_rls_and_append_only_triggers_hold = None

    def setUp(self):
        super().setUp()
        vocabulary = patch("app.external_creatives.vocabulary.build_vocabulary",
                           lambda *args, **kwargs: copy.deepcopy(VOCABULARY))
        vocabulary.start()
        self.addCleanup(vocabulary.stop)

    def _stranger(self, fixture):
        with self._as(0):
            stranger = fixture._make_context(f"ws_snap_pg_other_{uuid.uuid4().hex[:8]}", role=ROLE_MEMBER,
                                             actor="member_b")
        self.workspaces.append(stranger.workspace_id)
        return stranger

    def test_tags_clusters_snapshots_and_reclusters_run_under_their_runtime_roles(self):
        fixture = self._fixture()
        stranger = self._stranger(fixture)
        with self._as(1):
            ids = [fixture._confirmed(ACME), fixture._confirmed(BRIGHTVOLT, role="self")]
            accepted = fixture._start(fixture._body(ids, keywords=[]))
            scan_id, job_id = accepted["scan_id"], accepted["job_id"]

        self._work(fixture)  # tags, clusters and the snapshot, as the Worker

        with self._as(1):
            scan = fixture._scan(scan_id)
            self.assertEqual((scan["status"], scan["stage"]), ("completed", "freeze"), scan)
            steps = {step["step"]: step["status"] for step in scan["steps"]}
            self.assertEqual((steps["tag"], steps["cluster"], steps["freeze"]), ("completed", "completed", "completed"))
            reservations = fixture._scan_reservations(job_id)
            self.assertNotIn("reserved", {row.state for row in reservations.values()})
            self.assertEqual(reservations[llm_call_key(job_id, "tag:1")].state, "settled")
            self.assertEqual(reservations[llm_call_key(job_id, "cluster:1")].state, "settled")
            summary = fixture.client.get(f"/api/v1/competitor-scans/{scan_id}/summary").json()
            self.assertEqual(summary["snapshot_id"], scan["snapshot_id"])
            self.assertTrue(any(cluster["self_included"] for cluster in summary["clusters"]))
            ad = next(item for item in fixture._all_ads(scan_id) if item["tags"])

            # A Member confirms tags (an INSERT), then refreezes (INSERT + UPDATE of the scan).
            confirmed = fixture.client.post(f"/api/v1/competitor-scans/{scan_id}/ads/{ad['ad_id']}/tags",
                                            json={"tags": [{"factor_id": "hook", "value_id": "v2"}]},
                                            headers=fixture._headers())
            self.assertEqual((confirmed.status_code, confirmed.json()["revision"]), (200, 2), confirmed.text)
            refrozen = fixture.client.post(f"/api/v1/competitor-scans/{scan_id}/snapshots",
                                           headers=fixture._headers()).json()
            self.assertTrue(refrozen["created"])
            unchanged = fixture.client.post(f"/api/v1/competitor-scans/{scan_id}/snapshots",
                                            headers=fixture._headers()).json()
            self.assertEqual((unchanged["snapshot_id"], unchanged["created"]), (refrozen["snapshot_id"], False))

            # The product switch is an UPDATE on products as the API.
            switched = fixture.client.put(f"/api/v1/products/{fixture.product_id}/competitor-settings",
                                          json={"competitor_snapshot_in_drafts": False}, headers=fixture._headers())
            self.assertEqual((switched.status_code, switched.json()["competitor_snapshot_in_drafts"]), (200, False),
                             switched.text)

            # A paid recluster: quoted, then cluster:1 reserved for its own Job.
            quote = fixture.client.get(f"/api/v1/competitor-scans/{scan_id}/recluster-quote").json()
            self.assertEqual(quote["execution_mode"], "byok")
            admitted = fixture.client.post(f"/api/v1/competitor-scans/{scan_id}/recluster", headers=fixture._headers())
            self.assertEqual(admitted.status_code, 202, admitted.text)
            recluster_job = admitted.json()["reclusters"][0]["job_id"]
            self.assertEqual(set(fixture._scan_reservations(recluster_job)), {llm_call_key(recluster_job, "cluster:1")})

        self._work(fixture)  # the recluster, as the Worker

        with self._as(1):
            [entry] = fixture._scan(scan_id)["reclusters"]
            self.assertEqual((entry["status"], entry["cluster_revision"]), ("completed", 2), entry)
            self.assertEqual(fixture._scan_reservations(recluster_job)[llm_call_key(recluster_job, "cluster:1")].state,
                             "settled")
            self.assertNotEqual(entry["snapshot_id"], refrozen["snapshot_id"])
            with tenant_session(fixture.ctx) as session:
                revisions = session.scalars(select(CompetitorScanAssessmentRecord.revision).where(
                    CompetitorScanAssessmentRecord.scan_id == scan_id)).all()
                self.assertFalse(session.get(ProductRecord, fixture.product_id).competitor_snapshot_in_drafts)
            self.assertEqual(sorted(revisions), [1, 2])

            # Another Workspace sees nothing and changes nothing.
            for current in (stranger, fixture.owner_ctx(stranger)):
                fixture.current = current
                for response in (
                    fixture.client.get(f"/api/v1/competitor-scans/{scan_id}/summary"),
                    fixture.client.get(f"/api/v1/competitor-scans/{scan_id}/snapshots/{entry['snapshot_id']}"),
                    fixture.client.post(f"/api/v1/competitor-scans/{scan_id}/snapshots", headers=fixture._headers()),
                    fixture.client.post(f"/api/v1/competitor-scans/{scan_id}/ads/{ad['ad_id']}/tags",
                                        json={"tags": []}, headers=fixture._headers()),
                    fixture.client.get(f"/api/v1/competitor-scans/{scan_id}/recluster-quote"),
                    fixture.client.post(f"/api/v1/competitor-scans/{scan_id}/recluster", headers=fixture._headers()),
                ):
                    fixture._problem(response, 404, "competitor_scan_not_found")
                fixture._problem(fixture.client.put(
                    f"/api/v1/products/{fixture.product_id}/competitor-settings",
                    json={"competitor_snapshot_in_drafts": True}, headers=fixture._headers()), 404, "product_not_found")
            fixture.current = fixture.ctx
            self.assertFalse(fixture.client.get(
                f"/api/v1/products/{fixture.product_id}/competitor-settings").json()["competitor_snapshot_in_drafts"])

        with self.api.connect() as connection:
            for table in TABLES:
                visible = connection.execute(text(f"SELECT count(*) FROM {table}")).scalar()
                self.assertEqual(visible, 0, f"{table} leaked without a tenant context")

    def test_a_key_refused_mid_tag_resumes_under_both_roles(self):
        fixture = self._fixture()
        fixture.openai_answers = [None, None, REJECTED]  # filter:1 and tag:1 answered; tag:2 refused
        with self._as(1):
            accepted = fixture._start(fixture._body([fixture._confirmed(BRIGHTVOLT)], keywords=[]))
        self._work(fixture)
        with self._as(1):
            scan = fixture._scan(accepted["scan_id"])
            self.assertEqual(scan["status"], "completed")
            self.assertTrue(scan["can_resume"], scan["steps"])
            quote = fixture._resume_quote(accepted["scan_id"])
            self.assertEqual(quote["market_data"]["max_credits"], 0)  # judgments only: no page is left
            resumed = fixture._resume(accepted["scan_id"])
        self._work(fixture)
        with self._as(1):
            scan = fixture._scan(accepted["scan_id"])
            steps = {step["step"]: step["status"] for step in scan["steps"]}
            self.assertEqual((steps["tag"], steps["cluster"]), ("completed", "completed"), scan["steps"])
            self.assertFalse(scan["can_resume"])
            for job_id in (accepted["job_id"], resumed["job_id"]):
                self.assertNotIn("reserved", {row.state for row in fixture._scan_reservations(job_id).values()})

    def test_the_new_privileges_rls_and_append_only_triggers_hold(self):
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
                        self.assertEqual(held, privilege in expected, f"{table} {role} {privilege}")
                trigger = connection.execute(text("SELECT count(*) FROM pg_trigger WHERE tgname = :n"),
                                             {"n": f"trg_{table}_append_only"}).scalar()
                self.assertEqual(trigger, 1, table)
            # The scan's admission freezes the tag vocabulary as the API, which reads every table
            # it is built from (the Worker cannot read test_plan_runs).
            for table in ("test_plans", "test_plan_directions", "test_plan_runs", "generation_drafts",
                          "library_entries", "library_versions"):
                held = connection.execute(text("SELECT has_table_privilege('marketai_domain_api', :t, 'SELECT')"),
                                          {"t": table}).scalar()
                self.assertTrue(held, f"marketai_domain_api SELECT {table}")
            # The switch is written by the API; the Worker and readonly only read it.
            for role, privilege, expected in (
                ("marketai_domain_api", "UPDATE", True), ("marketai_domain_api", "SELECT", True),
                ("marketai_domain_worker", "SELECT", True), ("marketai_domain_readonly", "SELECT", True),
                ("marketai_domain_readonly", "UPDATE", False),
            ):
                held = connection.execute(text(
                    "SELECT has_column_privilege(:r, 'products', 'competitor_snapshot_in_drafts', :p)"),
                    {"r": role, "p": privilege}).scalar()
                self.assertEqual(held, expected, f"products.competitor_snapshot_in_drafts {role} {privilege}")

        fixture = self._fixture()
        with self._as(1):
            accepted = fixture._start(fixture._body([fixture._confirmed(BRIGHTVOLT)], keywords=[]))
        self._work(fixture)
        scan_id = accepted["scan_id"]
        # Even the schema owner cannot rewrite or remove a clusters revision or a snapshot.
        for model, column in ((CompetitorScanSnapshotRecord, "hash"), (CompetitorScanAssessmentRecord, "kind")):
            for statement in (
                update(model).values(**{column: "x"}).where(model.scan_id == scan_id),
                model.__table__.delete().where(model.scan_id == scan_id),
            ):
                with self.assertRaises(DBAPIError):
                    with self._as(0), tenant_session(fixture.ctx) as session:
                        session.execute(statement)


if __name__ == "__main__":
    unittest.main()
