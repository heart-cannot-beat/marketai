"""Issue #276 against an isolated migrated PostgreSQL with the real API and Worker roles.

SQLite cannot see a missing grant (#284), so each discovery read and write
runs here as the role that serves it in production: the Worker freezes and
answers a byok scan's ``discover:<i>`` calls (reading the product's
competitors, confirmations and decisions), the API lists the
recommendations and appends a Member's accept (with the competitor and its
link confirmation) and an Owner's reject, and the next scan's Worker leaves
both advertisers out. Then the privilege matrix of
``competitor_discovery_decisions``, its FORCE'd RLS and append-only
trigger, and another Workspace (a non-member here) refused.

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
from app.competitors import discovery
from app.competitors.models import CompetitorDiscoveryDecisionRecord, CompetitorScanRecord
from app.library.contracts import ROLE_MEMBER
from app.providers.llm_chat import llm_call_key

ACME, BRIGHTVOLT = "100000000000010", "100000000000016"
NORTHPEAK, KESTREL = "100000000000017", "100000000011001"
TABLE = "competitor_discovery_decisions"
GRANTED = {"marketai_domain_api": {"SELECT", "INSERT"}, "marketai_domain_worker": {"SELECT"}}
DML = ("SELECT", "INSERT", "UPDATE", "DELETE")


class CompetitorDiscoveryPostgresTests(scans_pg.CompetitorScanPostgresTests):
    """Its setup, role sessions and Worker loop; the scan tests themselves run in
    ``tests.test_competitor_scans_pg``."""

    test_every_scan_write_runs_under_its_runtime_role = None
    test_an_owners_reconciliation_and_its_settlement_hold_under_the_roles = None
    test_the_privilege_matrix_rls_and_append_only_triggers_hold = None

    def _decide(self, fixture, scan_id: str, page_id: str, decision: str):
        return fixture.client.post(f"/api/v1/competitor-scans/{scan_id}/discoveries/{page_id}/decision",
                                   json={"decision": decision}, headers=fixture._headers())

    def _table(self, fixture, scan_id: str) -> dict:
        with tenant_session(fixture.ctx) as session:
            record = session.get(CompetitorScanRecord, scan_id)
            return dict((record.evidence_json or {}).get(discovery.STEP) or {})

    def test_discovery_runs_under_its_runtime_roles(self):
        fixture = self._fixture()
        with self._as(0):
            stranger = fixture._make_context(f"ws_disc_pg_other_{uuid.uuid4().hex[:8]}", role=ROLE_MEMBER,
                                             actor="member_b")
        self.workspaces.append(stranger.workspace_id)
        with self._as(1):
            competitors = [fixture._confirmed(ACME), fixture._confirmed(BRIGHTVOLT)]
            accepted = fixture._start(fixture._body(competitors))

        self._work(fixture)  # the Worker freezes the table and answers discover:<i>

        with self._as(1):
            scan = fixture._scan(accepted["scan_id"])
            self.assertEqual(scan["status"], "completed", scan["failure"])
            table = self._table(fixture, accepted["scan_id"])
            self.assertEqual(table["status"], "completed")
            names = list(table["assignments"])
            reservations = fixture._scan_reservations(accepted["job_id"])
            self.assertNotIn("reserved", {row.state for row in reservations.values()})
            self.assertEqual({name for name in (discovery.call_name(i) for i in range(1, 31))
                              if reservations[llm_call_key(accepted["job_id"], name)].state == "settled"}, set(names))
            listed = fixture.client.get(f"/api/v1/competitor-scans/{accepted['scan_id']}/discoveries").json()
            pages = {item["advertiser_page_id"] for item in listed["items"]}
            self.assertLessEqual({NORTHPEAK, KESTREL}, pages)
            self.assertFalse({ACME, BRIGHTVOLT} & pages)

            # A Member accepts (competitor + link confirmation + decision, as the API); the Owner rejects.
            member = self._decide(fixture, accepted["scan_id"], NORTHPEAK, "accept")
            self.assertEqual(member.status_code, 200, member.text)
            new_competitor = member.json()["competitor"]["competitor_id"]
            self.assertEqual(member.json()["competitor"]["confirmation"]["source"], "link")
            fixture.current = fixture.owner_ctx(fixture.ctx)
            owner = self._decide(fixture, accepted["scan_id"], KESTREL, "reject")
            self.assertEqual((owner.status_code, owner.json()["decision"]["decided_by"]), (200, "owner_a"), owner.text)
            fixture.current = fixture.ctx
            with tenant_session(fixture.ctx) as session:
                actors = [(row.initiator_id, row.details_json["decision"]) for row in session.query(
                    AuditEventRecord).filter(
                    AuditEventRecord.workspace_id == fixture.ctx.workspace_id,
                    AuditEventRecord.event_type == "competitor_discovery.decided",
                ).order_by(AuditEventRecord.created_at)]
            self.assertEqual(actors, [("member_a", "accept"), ("owner_a", "reject")])

            # Another Workspace sees nothing and decides nothing.
            for current in (stranger, fixture.owner_ctx(stranger)):
                fixture.current = current
                fixture._problem(fixture.client.get(f"/api/v1/competitor-scans/{accepted['scan_id']}/discoveries"),
                                 404, "competitor_scan_not_found")
                fixture._problem(self._decide(fixture, accepted["scan_id"], NORTHPEAK, "reject"), 404,
                                 "competitor_scan_not_found")
            fixture.current = fixture.ctx

            again = fixture._start(fixture._body([*competitors, new_competitor]))

        self._work(fixture)  # the Worker reads both decisions under RLS

        with self._as(1):
            scan = fixture._scan(again["scan_id"])
            self.assertEqual(scan["status"], "completed", scan["failure"])
            target = next(t for t in scan["targets"] if t["competitor_id"] == new_competitor)
            self.assertEqual((target["page_id"], target["status"]), (NORTHPEAK, "completed"))
            table = self._table(fixture, again["scan_id"])
            judged = {entry["page_id"] for entry in table["assignments"].values()}
            self.assertFalse({NORTHPEAK, KESTREL} & judged)
            self.assertEqual(table["excluded"], {"competitor_pages": 3, "rejected_pages": 1})

        with self.api.connect() as connection:
            visible = connection.execute(text(f"SELECT count(*) FROM {TABLE}")).scalar()
            self.assertEqual(visible, 0, f"{TABLE} leaked without a tenant context")

    def test_the_decision_privileges_rls_and_append_only_trigger_hold(self):
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
            accepted = fixture._start(fixture._body([fixture._confirmed(ACME)], keywords=["CCS1 adapter"]))
        self._work(fixture)
        with self._as(1):
            response = self._decide(fixture, accepted["scan_id"], NORTHPEAK, "reject")
            self.assertEqual(response.status_code, 200, response.text)
        # Even the schema owner cannot rewrite or remove a decision.
        for statement in (
            update(CompetitorDiscoveryDecisionRecord).values(decision="accept").where(
                CompetitorDiscoveryDecisionRecord.advertiser_page_id == NORTHPEAK),
            CompetitorDiscoveryDecisionRecord.__table__.delete().where(
                CompetitorDiscoveryDecisionRecord.advertiser_page_id == NORTHPEAK),
        ):
            with self.assertRaises(DBAPIError):
                with self._as(0), tenant_session(fixture.ctx) as session:
                    session.execute(statement)


if __name__ == "__main__":
    unittest.main()
