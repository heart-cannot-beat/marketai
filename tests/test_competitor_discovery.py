"""Issue #276 (C-33) -- competitor discovery: keywords -> advertisers -> recommendations, accepted or rejected.

Real HTTP routes (``TestClient``) and the real Worker (``WorkerRuntime``)
with the Controlled Mock over the P0 recordings ("NACS adapter" and "CCS1
adapter", two pages each). "byok" runs answer the discover calls with a
fake OpenAI the way the Mock does: reservations, attempts and settlement
run for real, no request leaves the process, no real key exists.

* P0: Northpeak, Lumenline and Kestrel Power are recommended; the dealers, Bazaar Wholesale
  and the personal accounts never are (§7.1 rules), nor are the product's
  own competitors (Acme Charge, Brightvolt, Halcyon Motors).
* The discover step calls ``discover:<i>`` -- the names C-21 reserved --
  after freezing "ordinal -> advertiser" (most observations first, then
  page ID); the ordinals outside the table are released when the step
  ends; nothing stays reserved when the scan finishes.
* Accept: a competitor confirmed by the page ID from the ads (a ``link``
  confirmation; no resolve, no ``page_search`` / ``probe``); the next scan
  fetches its page. Reject: later scans leave the advertiser out.
* An archived competitor's page is recommended again as ``open`` (Issue
  #330); accepting it again adds a new competitor and appends a decision.
* Replays, conflicts, a Member and the Owner deciding (audited with the
  actor), another Workspace answered 404, and a resume after the AI key
  was refused mid-step re-asking only the unanswered ordinals.
"""
from __future__ import annotations

import dataclasses
from unittest.mock import patch

from sqlalchemy import select, update
from sqlalchemy.exc import DatabaseError

from tests.test_competitor_scans_api import HALCYON, ScanFixtures
from tests.test_competitor_snapshots import REJECTED
from tests.test_competitors_api import BRIGHTVOLT, ACME

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AuditEventRecord
from app.competitors import discovery
from app.competitors.models import (
    CompetitorDiscoveryDecisionRecord,
    CompetitorIdentityConfirmationRecord,
    CompetitorResolutionRecord,
)
from app.entitlements.cost_models import ProviderCallAttemptRecord
from app.providers.llm_chat import llm_call_key

NORTHPEAK = "100000000000017"
LUMENLINE = "100000000000022"
KESTREL = "100000000011001"
BAZAAR = "10000000001"
DEALERS = ("1000000000000321", "100000000011045")  # Springfield Motors Naples, Springfield Motors of Joplin
PEOPLE = ("900000000000004", "900000000000005", "900000000000006")  # the fixtures' pseudonymous profiles


class DiscoveryFixtures(ScanFixtures):
    def setUp(self) -> None:
        super().setUp()
        self.discovery_answers: list = []

    def _discoveries(self, scan_id: str, expect: int = 200) -> dict:
        response = self.client.get(f"/api/v1/competitor-scans/{scan_id}/discoveries")
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def _decide_response(self, scan_id: str, page_id: str, decision: str, *, key: str | None = None,
                         reason: str | None = None):
        body = {"decision": decision, **({"reason": reason} if reason is not None else {})}
        return self.client.post(f"/api/v1/competitor-scans/{scan_id}/discoveries/{page_id}/decision",
                                json=body, headers=self._headers(key))

    def _decide(self, scan_id: str, page_id: str, decision: str, *, key: str | None = None,
                reason: str | None = None) -> dict:
        response = self._decide_response(scan_id, page_id, decision, key=key, reason=reason)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _p0_scan(self, competitor_ids: list[str] | None = None, **overrides) -> dict:
        accepted = self._start(self._body(competitor_ids, **overrides))
        self._drain()
        return accepted

    def _discover_state(self, scan_id: str) -> dict:
        return self._record(scan_id).evidence_json.get(discovery.STEP) or {}

    def _decisions(self) -> list[CompetitorDiscoveryDecisionRecord]:
        with tenant_session(self.ctx) as session:
            rows = session.scalars(select(CompetitorDiscoveryDecisionRecord).where(
                CompetitorDiscoveryDecisionRecord.workspace_id == self.ctx.workspace_id,
            ).order_by(CompetitorDiscoveryDecisionRecord.decided_at)).all()
            for row in rows:
                session.expunge(row)
            return rows

    def _discover_attempts(self, job_id: str) -> list[ProviderCallAttemptRecord]:
        names = {llm_call_key(job_id, discovery.call_name(i)): discovery.call_name(i) for i in range(1, 101)}
        with tenant_session(self.ctx) as session:
            rows = [row for row in session.scalars(select(ProviderCallAttemptRecord).where(
                ProviderCallAttemptRecord.workspace_id == self.ctx.workspace_id,
                ProviderCallAttemptRecord.job_id == job_id,
                ProviderCallAttemptRecord.step_kind == "llm_chat",
            )).all() if row.call_semantic_key_hash in names]
            for row in rows:
                session.expunge(row)
            return rows


class P0DiscoveryTests(DiscoveryFixtures):
    def test_p0_recommends_northpeak_lumenline_kestrel_and_never_dealers_bazaar_or_people(self):
        accepted = self._p0_scan()
        scan = self._scan(accepted["scan_id"])
        self.assertEqual((scan["status"], scan["stage"]), ("completed", "freeze"), scan["failure"])
        # The scan's own steps keep their contract (an older web build validates them); discovery
        # reports its step on the discoveries instead.
        self.assertNotIn("discover", [step["step"] for step in scan["steps"]])

        listed = self._discoveries(accepted["scan_id"])
        self.assertEqual((listed["status"], listed["execution_mode"]), ("completed", "mock"))
        by_page = {item["advertiser_page_id"]: item for item in listed["items"]}
        self.assertLessEqual({NORTHPEAK, LUMENLINE, KESTREL}, set(by_page))
        self.assertEqual({by_page[NORTHPEAK]["relevance"], by_page[LUMENLINE]["relevance"]}, {"relevant"})
        self.assertEqual(by_page[KESTREL]["relevance"], "adjacent")
        northpeak = by_page[NORTHPEAK]
        self.assertEqual(
            (northpeak["advertiser_name"], northpeak["categories"], northpeak["landing_domains"],
             northpeak["observation_count"], northpeak["keywords"], northpeak["state"]),
            ("Northpeak", ["Brand"], ["northpeak.example"], 5, ["CCS1 adapter"], "open"),
        )
        self.assertIn("Mock", northpeak["reason"])
        self.assertIn(f"view_all_page_id={NORTHPEAK}", northpeak["library_url"])
        self.assertIn("country=US", northpeak["library_url"])

        # The rules, and the list, keep these out of the table itself: never judged, never recommended.
        state = self._discover_state(accepted["scan_id"])
        judged = {entry["page_id"] for entry in state["assignments"].values()}
        for page_id in (BAZAAR, *DEALERS, *PEOPLE, ACME, BRIGHTVOLT, HALCYON):
            self.assertNotIn(page_id, judged)
            self.assertNotIn(page_id, by_page)
        self.assertEqual(state["excluded"], {"competitor_pages": 3, "rejected_pages": 0})
        # Only keyword-only observations count, and each recommended one is.
        origins = {}
        for row in self._origins(accepted["scan_id"]):
            origins.setdefault(row.competitor_ad_id, []).append(row.origin)
        for entry in state["assignments"].values():
            for ad_id in entry["ad_ids"]:
                self.assertTrue(all(origin.startswith("keyword:") for origin in origins[ad_id]), entry)

        # "ordinal -> advertiser": most observations first, then page ID, from discover:1.
        table = [(entry["observations"], entry["page_id"]) for entry in state["assignments"].values()]
        self.assertEqual(table, sorted(table, key=lambda item: (-item[0], item[1])))
        self.assertEqual(list(state["assignments"]), [discovery.call_name(i) for i in range(1, len(table) + 1)])
        self.assertEqual(sorted(state["answered"], key=lambda name: int(name.split(":")[1])), list(state["assignments"]))
        self.assertEqual([item["call_name"] for item in listed["items"]],
                         sorted((item["call_name"] for item in listed["items"]), key=lambda n: int(n.split(":")[1])))
        self.assertEqual(listed["advertisers"], len(table))
        self.assertEqual(listed["judged"], len(table))
        self.assertEqual(listed["recommended"] + listed["unrelated"], len(table))

    def test_a_zh_cn_scan_reasons_in_chinese(self):
        self.current = dataclasses.replace(self.ctx, ui_language="zh-CN")
        accepted = self._p0_scan()
        listed = self._discoveries(accepted["scan_id"])
        self.assertEqual(listed["language"], "zh-CN")
        self.assertTrue(listed["items"])
        self.assertTrue(all(item["reason"].startswith("受控 Mock") for item in listed["items"]))
        self.assertEqual({item["language"] for item in listed["items"]}, {"zh-CN"})

    def test_a_scan_without_keywords_has_no_discover_step(self):
        accepted = self._p0_scan([self._confirmed(BRIGHTVOLT)], keywords=[])
        scan = self._scan(accepted["scan_id"])
        self.assertNotIn("discover", [step["step"] for step in scan["steps"]])
        self.assertNotIn(discovery.STEP, self._record(accepted["scan_id"]).evidence_json)
        listed = self._discoveries(accepted["scan_id"])
        self.assertEqual((listed["status"], listed["reason"], listed["items"]), ("skipped", "no_keywords", []))

    def test_the_table_is_capped_by_max_discover_advertisers(self):
        accepted = self._p0_scan(max_discover_advertisers=2)
        state = self._discover_state(accepted["scan_id"])
        self.assertEqual(list(state["assignments"]), ["discover:1", "discover:2"])
        self.assertEqual(state["over_cap"], state["candidates"] - 2)
        listed = self._discoveries(accepted["scan_id"])
        self.assertEqual((listed["advertisers"], listed["over_cap"]), (2, state["candidates"] - 2))


class ByokDiscoveryTests(DiscoveryFixtures):
    def setUp(self) -> None:
        super().setUp()
        self._bind_both()

    def test_reserved_and_called_under_the_same_names_and_nothing_left_open(self):
        accepted = self._start(self._body())
        job_id = accepted["job_id"]
        reservations = self._scan_reservations(job_id)
        reserved = {llm_call_key(job_id, discovery.call_name(i)) for i in range(1, 31)}
        self.assertLessEqual(reserved, set(reservations))  # C-21's discover:1..30, verbatim
        self._drain()
        state = self._discover_state(accepted["scan_id"])
        table = list(state["assignments"])
        attempts = self._discover_attempts(job_id)
        self.assertEqual({a.call_semantic_key_hash for a in attempts}, {llm_call_key(job_id, n) for n in table})
        self.assertEqual(len(attempts), len(table))
        reservations = self._scan_reservations(job_id)
        states = {discovery.call_name(i): reservations[llm_call_key(job_id, discovery.call_name(i))].state
                  for i in range(1, 31)}
        self.assertEqual({name for name, value in states.items() if value == "settled"}, set(table))
        outside = {discovery.call_name(i) for i in range(len(table) + 1, 31)}
        self.assertEqual({name for name, value in states.items() if value == "released"}, outside)
        self.assertEqual(set(state["released"]), outside)  # released when the step ended, not at finish
        self.assertNotIn("reserved", {row.state for row in reservations.values()})
        # Each request was assembled within LLM_BUDGETS['discover'] and in the scan's language.
        discover_requests = [body for body in self.openai_requests
                             if body["response_format"]["json_schema"]["name"] == discovery.SCHEMA_NAME]
        self.assertEqual(len(discover_requests), len(table))
        self.assertTrue(all(body["max_completion_tokens"] <= 1000 if "max_completion_tokens" in body
                            else body.get("max_tokens", 0) <= 1000 for body in discover_requests))
        listed = self._discoveries(accepted["scan_id"])
        self.assertLessEqual({NORTHPEAK, LUMENLINE, KESTREL}, {item["advertiser_page_id"] for item in listed["items"]})

    def test_a_refused_key_mid_step_resumes_only_the_unanswered_ordinals(self):
        accepted = self._start(self._body())
        self.discovery_answers = [None, None, REJECTED]  # the 3rd discover call: the AI key is refused
        self._drain()
        scan = self._scan(accepted["scan_id"])
        self.assertEqual(scan["status"], "completed")
        listed = self._discoveries(accepted["scan_id"])
        self.assertEqual((listed["status"], listed["reason"], listed["judged"]), ("partial", "credential_rejected", 2))
        state = self._discover_state(accepted["scan_id"])
        table = list(state["assignments"])
        self.assertEqual(state["answered"], ["discover:1", "discover:2"])
        self.assertNotIn("reserved", {row.state for row in self._scan_reservations(accepted["job_id"]).values()})
        self.assertTrue(scan["can_resume"])

        quote = self._resume_quote(accepted["scan_id"])
        discover_line = next(line for line in quote["llm"]["lines"] if line["step"] == "discover")
        self.assertEqual(discover_line["calls"], len(table) - 2)
        resumed = self._resume(accepted["scan_id"])
        reservations = self._scan_reservations(resumed["job_id"])
        self.assertEqual({key for key in reservations if key in {llm_call_key(resumed["job_id"], n) for n in table}},
                         {llm_call_key(resumed["job_id"], n) for n in table[2:]})
        self._drain()
        state = self._discover_state(accepted["scan_id"])
        self.assertEqual(sorted(state["answered"], key=lambda n: int(n.split(":")[1])), table)
        self.assertEqual(list(state["assignments"]), table)  # the frozen table is not recomputed
        self.assertEqual(len(self._discover_attempts(resumed["job_id"])), len(table) - 2)
        self.assertNotIn("reserved", {row.state for row in self._scan_reservations(resumed["job_id"]).values()})
        self.assertEqual(self._discoveries(accepted["scan_id"])["status"], "completed")
        self.assertFalse(self._scan(accepted["scan_id"])["can_resume"])


class DecisionTests(DiscoveryFixtures):
    def test_accept_confirms_the_ads_page_id_and_the_next_scan_fetches_it_without_a_resolve(self):
        accepted = self._p0_scan()
        result = self._decide(accepted["scan_id"], NORTHPEAK, "accept")
        self.assertFalse(result["replayed"])
        competitor = result["competitor"]
        self.assertEqual((competitor["role"], competitor["status"], competitor["input_kind"],
                          competitor["confirmation_status"]), ("competitor", "active", "ad_library", "confirmed"))
        confirmation = competitor["confirmation"]
        self.assertEqual((confirmation["page_id"], confirmation["page_name"], confirmation["source"],
                          confirmation["link_kind"], confirmation["resolve_job_id"]),
                         (NORTHPEAK, "Northpeak", "link", "ad_library", None))
        self.assertEqual((result["decision"]["decision"], result["decision"]["competitor_id"]),
                         ("accept", competitor["competitor_id"]))
        self.assertEqual((result["discovery"]["state"], result["discovery"]["competitor_id"]),
                         ("accepted", competitor["competitor_id"]))
        with tenant_session(self.ctx) as session:
            self.assertEqual(session.scalars(select(CompetitorResolutionRecord).where(
                CompetitorResolutionRecord.competitor_id == competitor["competitor_id"])).all(), [])
            row = session.scalars(select(CompetitorIdentityConfirmationRecord).where(
                CompetitorIdentityConfirmationRecord.competitor_id == competitor["competitor_id"])).one()
            self.assertEqual(row.evidence_json["discovery"]["scan_id"], accepted["scan_id"])

        # The next scan fetches its page by the confirmed ID; no page_search, no probe, anywhere.
        again = self._p0_scan([*self._three(), competitor["competitor_id"]])
        scan = self._scan(again["scan_id"])
        target = next(t for t in scan["targets"] if t["competitor_id"] == competitor["competitor_id"])
        self.assertEqual((target["page_id"], target["status"]), (NORTHPEAK, "completed"))
        record = self._record(again["scan_id"])
        calls = [page["call_name"] for t in record.evidence_json["targets"] if t["origin"] == target["origin"]
                 for page in t["pages"]]
        self.assertEqual(calls[0], f"page:{competitor['competitor_id']}:1")
        operations = {request.operation.value for source in self.sources for request in source.requests}
        self.assertIn("page", operations)
        self.assertFalse({"page_search", "probe"} & operations, operations)
        page_requests = [request for source in self.sources for request in source.requests
                         if request.operation.value == "page"]
        self.assertIn(NORTHPEAK, {request.page_id for request in page_requests})
        # It is a competitor now: the next scan does not recommend it again.
        state = self._discover_state(again["scan_id"])
        self.assertNotIn(NORTHPEAK, {entry["page_id"] for entry in state["assignments"].values()})
        self.assertEqual(state["excluded"]["competitor_pages"], 4)

    def test_reject_keeps_the_advertiser_out_of_later_scans(self):
        accepted = self._p0_scan()
        result = self._decide(accepted["scan_id"], KESTREL, "reject", reason="  sells V2L, not adapters ")
        self.assertIsNone(result["competitor"])
        self.assertEqual((result["decision"]["decision"], result["decision"]["reason"], result["discovery"]["state"]),
                         ("reject", "sells V2L, not adapters", "rejected"))
        self.assertEqual(self._get_listed_state(accepted["scan_id"], KESTREL), "rejected")
        again = self._p0_scan()
        state = self._discover_state(again["scan_id"])
        self.assertNotIn(KESTREL, {entry["page_id"] for entry in state["assignments"].values()})
        self.assertEqual(state["excluded"]["rejected_pages"], 1)
        self.assertNotIn(KESTREL, {item["advertiser_page_id"] for item in self._discoveries(again["scan_id"])["items"]})
        # Changing one's mind is a new row: accepting it later from the first scan still works.
        later = self._decide(accepted["scan_id"], KESTREL, "accept")
        self.assertEqual(later["discovery"]["state"], "accepted")
        self.assertEqual([row.decision for row in self._decisions()], ["reject", "accept"])

    def test_an_archived_competitor_is_recommended_again_and_accepting_it_adds_a_new_one(self):
        # Issue #330: archiving takes the page off the list, so the next scan recommends it again, open to decide.
        accepted = self._p0_scan()
        first = self._decide(accepted["scan_id"], NORTHPEAK, "accept")
        archived_id = first["competitor"]["competitor_id"]
        self.current = self.owner_ctx(self.ctx)
        archived = self.client.post(f"/api/v1/competitors/{archived_id}/archive", headers=self._headers())
        self.assertEqual((archived.status_code, archived.json()["status"]), (200, "archived"), archived.text)
        self.current = self.ctx

        again = self._p0_scan()
        state = self._discover_state(again["scan_id"])
        self.assertIn(NORTHPEAK, {entry["page_id"] for entry in state["assignments"].values()})
        listed = next(item for item in self._discoveries(again["scan_id"])["items"]
                      if item["advertiser_page_id"] == NORTHPEAK)
        # Open, not "accepted": the decision shown is the earlier accept, its competitor the archived one.
        self.assertEqual((listed["state"], listed["competitor_id"]), ("open", None))
        self.assertEqual((listed["decision"]["decision"], listed["decision"]["competitor_id"]), ("accept", archived_id))
        # The state follows the product's list, so the first scan reads the same.
        self.assertEqual(self._get_listed_state(accepted["scan_id"], NORTHPEAK), "open")

        # Another Workspace neither reads nor decides it.
        stranger = self._make_context(role="member", actor="member_b")
        for current in (stranger, self.owner_ctx(stranger)):
            self.current = current
            self._problem(self.client.get(f"/api/v1/competitor-scans/{again['scan_id']}/discoveries"), 404,
                          "competitor_scan_not_found")
            self._problem(self._decide_response(again["scan_id"], NORTHPEAK, "accept"), 404,
                          "competitor_scan_not_found")
        self.current = self.ctx
        self.assertEqual(len(self._decisions()), 1)

        # Accepting again adds a new competitor and appends a decision; the earlier ones stay as they were.
        later = self._decide(again["scan_id"], NORTHPEAK, "accept")
        competitor_id = later["competitor"]["competitor_id"]
        self.assertFalse(later["replayed"])
        self.assertNotEqual(competitor_id, archived_id)
        self.assertEqual((later["competitor"]["status"], later["competitor"]["confirmation"]["page_id"]),
                         ("active", NORTHPEAK))
        self.assertEqual((later["discovery"]["state"], later["discovery"]["competitor_id"],
                          later["decision"]["competitor_id"]), ("accepted", competitor_id, competitor_id))
        decisions = self._decisions()
        self.assertEqual([(row.id, row.scan_id, row.decision, row.competitor_id) for row in decisions], [
            (first["decision"]["decision_id"], accepted["scan_id"], "accept", archived_id),
            (later["decision"]["decision_id"], again["scan_id"], "accept", competitor_id),
        ])
        self.assertEqual(self._get_listed_state(accepted["scan_id"], NORTHPEAK), "accepted")
        archived_view = self.client.get(f"/api/v1/competitors/{archived_id}")
        self.assertEqual(archived_view.json()["status"], "archived", archived_view.text)
        # Accepting once more answers the decision in effect.
        repeat = self._decide(again["scan_id"], NORTHPEAK, "accept")
        self.assertEqual((repeat["replayed"], repeat["competitor"]["competitor_id"]), (True, competitor_id))
        self.assertEqual(len(self._decisions()), 2)

    def _get_listed_state(self, scan_id: str, page_id: str) -> str:
        return next(item["state"] for item in self._discoveries(scan_id)["items"]
                    if item["advertiser_page_id"] == page_id)

    def test_replays_and_conflicts(self):
        accepted = self._p0_scan()
        scan_id = accepted["scan_id"]
        first = self._decide(scan_id, LUMENLINE, "reject", key="ik-lumenline")
        replay = self._decide(scan_id, LUMENLINE, "reject", key="ik-lumenline")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["decision"]["decision_id"], first["decision"]["decision_id"])
        self._problem(self._decide_response(scan_id, LUMENLINE, "accept", key="ik-lumenline"), 409,
                      "idempotency_key_reused")
        # Rejecting what is rejected answers the decision in effect, no new row.
        again = self._decide(scan_id, LUMENLINE, "reject")
        self.assertEqual((again["replayed"], again["decision"]["decision_id"]),
                         (True, first["decision"]["decision_id"]))

        accepted_northpeak = self._decide(scan_id, NORTHPEAK, "accept")
        repeat = self._decide(scan_id, NORTHPEAK, "accept")
        self.assertEqual((repeat["replayed"], repeat["competitor"]["competitor_id"]),
                         (True, accepted_northpeak["competitor"]["competitor_id"]))
        problem = self._problem(self._decide_response(scan_id, NORTHPEAK, "reject"), 409,
                                "competitor_discovery_already_competitor")
        self.assertEqual(problem["params"]["competitor_id"], accepted_northpeak["competitor"]["competitor_id"])
        self.assertEqual(len(self._decisions()), 2)

        # An advertiser added another way is "competitor" and cannot be decided on.
        self._confirmed(KESTREL)
        self.assertEqual(self._get_listed_state(scan_id, KESTREL), "competitor")
        self._problem(self._decide_response(scan_id, KESTREL, "accept"), 409, "competitor_discovery_already_competitor")
        # Anything the scan did not recommend is not found: a dealer, a personal account, an unknown page.
        for page_id in (DEALERS[0], PEOPLE[0], "123"):
            self._problem(self._decide_response(scan_id, page_id, "accept"), 404, "competitor_discovery_not_found")
        response = self.client.post(f"/api/v1/competitor-scans/{scan_id}/discoveries/{NORTHPEAK}/decision",
                                    json={"decision": "maybe"}, headers=self._headers())
        self.assertEqual(response.status_code, 422)

    def test_a_member_and_the_owner_decide_and_the_audit_names_each(self):
        accepted = self._p0_scan()
        scan_id = accepted["scan_id"]
        member = self._decide(scan_id, NORTHPEAK, "accept")
        self.assertEqual((member["decision"]["decided_by"], member["competitor"]["created_by"],
                          member["competitor"]["confirmation"]["confirmed_by"]), ("member_a",) * 3)
        self.current = self.owner_ctx(self.ctx)
        owner = self._decide(scan_id, KESTREL, "reject")
        self.assertEqual(owner["decision"]["decided_by"], "owner_a")
        self.current = self.ctx
        with tenant_session(self.ctx) as session:
            events = session.scalars(select(AuditEventRecord).where(
                AuditEventRecord.workspace_id == self.ctx.workspace_id,
                AuditEventRecord.event_type == "competitor_discovery.decided",
            ).order_by(AuditEventRecord.created_at)).all()
            self.assertEqual([(e.initiator_id, e.details_json["decision"], e.details_json["advertiser_page_id"])
                              for e in events],
                             [("member_a", "accept", NORTHPEAK), ("owner_a", "reject", KESTREL)])

    def test_another_workspace_reads_and_decides_nothing(self):
        accepted = self._p0_scan()
        scan_id = accepted["scan_id"]
        stranger = self._make_context(role="member", actor="member_b")
        for current in (stranger, self.owner_ctx(stranger)):
            self.current = current
            self._problem(self.client.get(f"/api/v1/competitor-scans/{scan_id}/discoveries"), 404,
                          "competitor_scan_not_found")
            for decision in ("accept", "reject"):
                self._problem(self._decide_response(scan_id, NORTHPEAK, decision), 404, "competitor_scan_not_found")
        self.current = self.ctx
        self.assertEqual(self._decisions(), [])
        self.assertEqual(self._get_listed_state(scan_id, NORTHPEAK), "open")

    def test_decisions_are_append_only(self):
        accepted = self._p0_scan()
        self._decide(accepted["scan_id"], KESTREL, "reject")
        for statement in (
            update(CompetitorDiscoveryDecisionRecord).values(decision="accept"),
            CompetitorDiscoveryDecisionRecord.__table__.delete(),
        ):
            with self.assertRaises(DatabaseError):
                with tenant_session(self.ctx) as session:
                    session.execute(statement)


class DiscoveryUnitTests(DiscoveryFixtures):
    def test_requests_fit_the_discover_reservation(self):
        accepted = self._p0_scan()
        state = self._discover_state(accepted["scan_id"])
        from app.competitors.models import CompetitorAdRecord
        from app.providers.llm_chat import estimate_request_input_tokens

        with tenant_session(self.ctx) as session:
            for entry in state["assignments"].values():
                ads = session.scalars(select(CompetitorAdRecord).where(
                    CompetitorAdRecord.id.in_(entry["ad_ids"]))).all()
                request = discovery.build_request(entry, ads, state["product"], language="en", execution_mode="byok")
                self.assertLessEqual(estimate_request_input_tokens(request, execution_mode="byok"), 8_000)
                self.assertEqual(request.max_output_tokens, 1_000)
                # Huge texts still fit: fewer ads and shorter fields are tried until it does.
                for ad in ads:
                    session.expunge(ad)
                    ad.cards_json = [{**card, "title": "T" * 5000, "body": "B" * 20000} for card in ad.cards_json]
                request = discovery.build_request({**entry, "advertiser_name": "N" * 5000}, ads * 20,
                                                  state["product"], language="zh-CN", execution_mode="byok")
                self.assertLessEqual(estimate_request_input_tokens(request, execution_mode="byok"), 8_000)
                break

    def test_a_table_not_migrated_yet_reads_as_no_decision(self):
        """The API and Worker start while the Release service migrates (#283)."""
        accepted = self._p0_scan()
        self._decide(accepted["scan_id"], KESTREL, "reject")

        class Unmigrated:
            def has_table(self, name):
                return False

        with patch.object(discovery, "_decisions_ready", False), \
                patch.object(discovery, "inspect", lambda connection: Unmigrated()):
            with tenant_session(self.ctx) as session:
                self.assertEqual(discovery.latest_decisions(session, self.ctx, self.product_id), {})
            self.assertEqual(self._scan(accepted["scan_id"])["status"], "completed")
        with tenant_session(self.ctx) as session:
            self.assertEqual(list(discovery.latest_decisions(session, self.ctx, self.product_id)), [KESTREL])

    def test_normalize_drops_answers_outside_the_schema(self):
        self.assertIsNone(discovery.normalize({"relevance": "maybe", "reason": "x"}))
        self.assertEqual(discovery.normalize({"relevance": "adjacent", "reason": " a  b " + "c" * 400}),
                         {"relevance": "adjacent", "reason": ("a b " + "c" * 400)[:160]})
