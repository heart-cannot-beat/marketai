"""Issue #271 against an isolated migrated PostgreSQL with the real API and Worker roles.

SQLite cannot see a missing grant (#284), so each relevance write runs here
as the role that serves it in production: the Worker appends a byok scan's
rule and AI judgments (``filter:<i>`` calls settled, the rest released), the
API appends a Member's and an Owner's flip and reads the history. Then the
privilege matrix of ``competitor_ad_assessments``, its FORCE'd RLS and
append-only trigger, and another Workspace (a non-member here) refused.

Run with ``DOMAIN_OWNER_DATABASE_URL``, ``DOMAIN_API_DATABASE_URL``,
``DOMAIN_WORKER_DATABASE_URL``, ``MARKETAI_DATABASE_URL`` (the owner) and
``MARKETAI_TENANT_CONTEXT_SECRET`` against a cluster released with
``make db-release``.
"""
from __future__ import annotations

import unittest
import uuid

from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError

from tests import test_competitor_scans_pg as scans_pg

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AuditEventRecord
from app.competitors.models import CompetitorAdAssessmentRecord
from app.library.contracts import ROLE_MEMBER
from app.providers.llm_chat import llm_call_key

HALCYON = "100000000010002"
BRIGHTVOLT = "100000000000016"
TABLE = "competitor_ad_assessments"
GRANTED = {"marketai_domain_api": {"SELECT", "INSERT"}, "marketai_domain_worker": {"SELECT", "INSERT"}}
DML = ("SELECT", "INSERT", "UPDATE", "DELETE")


class CompetitorRelevancePostgresTests(scans_pg.CompetitorScanPostgresTests):
    """Its setup, role sessions and Worker loop; the scan tests themselves run in
    ``tests.test_competitor_scans_pg``."""

    test_every_scan_write_runs_under_its_runtime_role = None
    test_an_owners_reconciliation_and_its_settlement_hold_under_the_roles = None
    test_the_privilege_matrix_rls_and_append_only_triggers_hold = None

    def test_judgments_and_flips_run_under_their_runtime_roles(self):
        fixture = self._fixture()
        with self._as(0):
            stranger = fixture._make_context(f"ws_rel_pg_other_{uuid.uuid4().hex[:8]}", role=ROLE_MEMBER,
                                             actor="member_b")
        self.workspaces.append(stranger.workspace_id)
        with self._as(1):
            accepted = fixture._start(fixture._body([fixture._confirmed(HALCYON)], keywords=["NACS adapter"]))

        self._work(fixture)  # the Worker appends the rule and AI judgments

        with self._as(1):
            scan = fixture._scan(accepted["scan_id"])
            self.assertEqual((scan["status"], scan["stage"]), ("completed", "freeze"), scan)  # freeze: #272
            ads = fixture._all_ads(accepted["scan_id"])
            self.assertTrue(ads and all(ad["relevance"] for ad in ads))
            sources = {ad["relevance"]["source"] for ad in ads}
            self.assertEqual(sources, {"ai", "rule"})
            reservations = fixture._scan_reservations(accepted["job_id"])
            self.assertNotIn("reserved", {row.state for row in reservations.values()})
            self.assertEqual(reservations[llm_call_key(accepted["job_id"], "filter:1")].state, "settled")
            halcyon = next(ad for ad in ads if ad["advertiser_name"] == "Halcyon Motors")
            self.assertEqual(halcyon["relevance"]["relevance"], "unrelated")

            # A Member and the Owner flip as the API; both are audited with the actor.
            flipped = fixture.client.post(
                f"/api/v1/competitor-scans/{accepted['scan_id']}/ads/{halcyon['ad_id']}/relevance",
                json={"relevance": "relevant"}, headers=fixture._headers())
            self.assertEqual((flipped.status_code, flipped.json()["revision"]), (200, 2), flipped.text)
            fixture.current = fixture.owner_ctx(fixture.ctx)
            back = fixture.client.post(
                f"/api/v1/competitor-scans/{accepted['scan_id']}/ads/{halcyon['ad_id']}/relevance",
                json={"relevance": "unrelated"}, headers=fixture._headers())
            self.assertEqual((back.status_code, back.json()["revision"], back.json()["created_by"]),
                             (200, 3, "owner_a"), back.text)
            fixture.current = fixture.ctx
            history = fixture.client.get(
                f"/api/v1/competitor-scans/{accepted['scan_id']}/ads/{halcyon['ad_id']}/assessments").json()
            self.assertEqual([(item["source"], item["relevance"]) for item in history["items"]],
                             [("ai", "unrelated"), ("user", "relevant"), ("user", "unrelated")])
            with tenant_session(fixture.ctx) as session:
                actors = [row.initiator_id for row in session.query(AuditEventRecord).filter(
                    AuditEventRecord.workspace_id == fixture.ctx.workspace_id,
                    AuditEventRecord.event_type == "competitor_ad.relevance_flipped",
                ).order_by(AuditEventRecord.created_at)]
            self.assertEqual(actors, ["member_a", "owner_a"])

            # Another Workspace sees nothing and changes nothing.
            for current in (stranger, fixture.owner_ctx(stranger)):
                fixture.current = current
                for response in (
                    fixture.client.post(
                        f"/api/v1/competitor-scans/{accepted['scan_id']}/ads/{halcyon['ad_id']}/relevance",
                        json={"relevance": "adjacent"}, headers=fixture._headers()),
                    fixture.client.get(
                        f"/api/v1/competitor-scans/{accepted['scan_id']}/ads/{halcyon['ad_id']}/assessments"),
                ):
                    fixture._problem(response, 404, "competitor_scan_not_found")
            fixture.current = fixture.ctx
            self.assertEqual(len(fixture.client.get(
                f"/api/v1/competitor-scans/{accepted['scan_id']}/ads/{halcyon['ad_id']}/assessments",
            ).json()["items"]), 3)

        with self.api.connect() as connection:
            visible = connection.execute(text(f"SELECT count(*) FROM {TABLE}")).scalar()
            self.assertEqual(visible, 0, f"{TABLE} leaked without a tenant context")

    def test_the_assessment_privileges_rls_and_append_only_trigger_hold(self):
        with self.owner.connect() as connection:
            forced = connection.execute(text(
                "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE relname = :t"), {"t": TABLE},
            ).scalar()
            self.assertTrue(forced)
            for role in ("marketai_domain_api", "marketai_domain_worker", "marketai_domain_readonly"):
                expected = GRANTED.get(role, {"SELECT"})
                for privilege in DML:
                    held = connection.execute(text("SELECT has_table_privilege(:r, :t, :p)"),
                                              {"r": role, "t": TABLE, "p": privilege}).scalar()
                    self.assertEqual(held, privilege in expected, f"{role} {privilege}")
            trigger = connection.execute(text("SELECT count(*) FROM pg_trigger WHERE tgname = :n"),
                                         {"n": f"trg_{TABLE}_append_only"}).scalar()
            self.assertEqual(trigger, 1)

        fixture = self._fixture()
        with self._as(1):
            accepted = fixture._start(fixture._body([fixture._confirmed(BRIGHTVOLT)], keywords=[]))
        self._work(fixture)
        with self._as(1):
            ad = fixture._all_ads(accepted["scan_id"])[0]
            self.assertIsNotNone(ad["relevance"])
        # Even the schema owner cannot rewrite or remove a judgment.
        for statement in (
            update(CompetitorAdAssessmentRecord).values(relevance="relevant").where(
                CompetitorAdAssessmentRecord.competitor_ad_id == ad["ad_id"]),
            CompetitorAdAssessmentRecord.__table__.delete().where(
                CompetitorAdAssessmentRecord.competitor_ad_id == ad["ad_id"]),
        ):
            with self.assertRaises(DBAPIError):
                with self._as(0), tenant_session(fixture.ctx) as session:
                    session.execute(statement)


if __name__ == "__main__":
    unittest.main()

