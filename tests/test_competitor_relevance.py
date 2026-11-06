"""Issue #271 (C-22) -- relevance of competitor observations: rules, the filter step, flips.

Real HTTP routes (``TestClient``) and the real Worker (``WorkerRuntime``)
over the Controlled Mock P0 recordings. "byok" scans answer the filter
batches with a fake OpenAI that judges like the Mock: no real key, no real
request, but the ``filter:<i>`` reservations C-21 made, the attempts, the
settlement and the releases run for real.

Covered: spec scenario 7 (first half -- Halcyon Motors' self-driving ads are ``unrelated``
and nothing is tagged); the §7.1 rules (keyword-only observations on a
personal account, a wholesale site or a dealer page are ``unrelated``
without the LLM); collation variants judged once; the frozen batch table
replayed on redelivery with no new LLM attempt; a resume judging only what
no batch holds; every reservation named like its call and none left open;
flips (Owner and Member, back and forth = two new revisions, audited with
the actor, idempotent); history; the relevance filter; another Workspace
refused with 404; the table append-only.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
from collections import Counter
from datetime import timedelta
from unittest.mock import patch

import httpx
from sqlalchemy import select, update
from sqlalchemy.exc import DatabaseError

from tests.test_competitor_scans_api import KEYWORDS, HALCYON, Killed, ScanFixtures, _fixture_ads
from tests.test_competitors_api import BRIGHTVOLT, ACME

from app import config
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AuditEventRecord, JobRecord, utcnow
from app.competitors import relevance
from app.competitors.models import CompetitorAdAssessmentRecord, CompetitorAdRecord
from app.entitlements.cost_models import ProviderCallAttemptRecord
from app.marketing.models import MarketingProjectRecord
from app.providers.llm_chat import llm_call_key

KESTREL = "Kestrel Power"
NORTHPEAK = "Northpeak"


class RelevanceFixtures(ScanFixtures):
    def _ads_by_archive(self, scan_id: str, **params) -> dict[str, dict]:
        return {ad["ad_archive_id"]: ad for ad in self._all_ads(scan_id, **params)}

    def _assessments(self, scan_id: str) -> list[CompetitorAdAssessmentRecord]:
        """The scan's relevance judgments (its tags, #272/#329, are another kind)."""
        with tenant_session(self.ctx) as session:
            rows = session.scalars(select(CompetitorAdAssessmentRecord).where(
                CompetitorAdAssessmentRecord.scan_id == scan_id,
                CompetitorAdAssessmentRecord.kind == "relevance")).all()
            for row in rows:
                session.expunge(row)
            return rows

    def _llm_attempts(self, job_id: str) -> list[ProviderCallAttemptRecord]:
        with tenant_session(self.ctx) as session:
            rows = session.scalars(select(ProviderCallAttemptRecord).where(
                ProviderCallAttemptRecord.job_id == job_id,
                ProviderCallAttemptRecord.step_kind == "llm_chat")).all()
            for row in rows:
                session.expunge(row)
            return rows

    def _filter_attempts(self, job_id: str) -> list[ProviderCallAttemptRecord]:
        """The ``filter:<i>`` attempts only (the same Job also runs ``tag:<i>`` and ``cluster:1``, #272)."""
        keys = {llm_call_key(job_id, f"filter:{i}") for i in range(1, 200)}
        return [row for row in self._llm_attempts(job_id) if row.call_semantic_key_hash in keys]

    def _filter(self, scan_id: str) -> dict:
        return self._record(scan_id).evidence_json["filter"]

    def _flip(self, scan_id: str, ad_id: str, value: str, *, key: str | None = None):
        return self.client.post(f"/api/v1/competitor-scans/{scan_id}/ads/{ad_id}/relevance",
                                json={"relevance": value}, headers=self._headers(key))

    def _history(self, scan_id: str, ad_id: str):
        return self.client.get(f"/api/v1/competitor-scans/{scan_id}/ads/{ad_id}/assessments")

    def _redeliver(self, job_id: str) -> None:
        """The dead Worker's lease lapses; the Job is picked up again."""
        with tenant_session(self.ctx) as session:
            session.execute(update(JobRecord).where(JobRecord.id == job_id).values(
                state="retry_wait", lease_owner=None, lease_expires_at=None,
                next_attempt_at=utcnow() - timedelta(seconds=1)))

    @staticmethod
    def _names(raw: dict) -> dict[str, str]:
        """``ad_archive_id`` -> advertiser for every recorded ad."""
        return {ad_archive_id: name for name, ids in raw.items() for ad_archive_id in ids}


class FilterStepTests(RelevanceFixtures):
    def test_a_mock_scan_judges_every_observation_rules_first(self):
        accepted = self._start(self._body())
        self._drain()
        scan = self._scan(accepted["scan_id"])
        self.assertEqual((scan["status"], scan["stage"]), ("completed", "freeze"))  # freeze: #272
        ads = self._ads_by_archive(accepted["scan_id"])
        self.assertEqual(len(ads), 128)
        self.assertTrue(all(ad["relevance"] is not None for ad in ads.values()))
        self.assertEqual(self._all_ads(accepted["scan_id"], relevance="unassessed"), [])

        by_name: dict[str, set[tuple]] = {}
        for ad in ads.values():
            judgment = ad["relevance"]
            by_name.setdefault(ad["advertiser_name"], set()).add(
                (judgment["relevance"], judgment["source"], judgment["rule"]))
        # The Mock LLM fixtures (#271): Halcyon self-driving unrelated, Kestrel Power V2L adjacent, Northpeak relevant.
        self.assertEqual(by_name["Halcyon Motors"], {("unrelated", "ai", None)})
        self.assertEqual(by_name[KESTREL], {("adjacent", "ai", None)})
        self.assertEqual(by_name[NORTHPEAK], {("relevant", "ai", None)})
        # The §7.1 rules judge keyword-only discoveries without the LLM.
        self.assertEqual(by_name["Bazaar Wholesale"], {("unrelated", "rule", "wholesale_site")})
        self.assertEqual(by_name["Springfield Motors Naples"], {("unrelated", "rule", "dealer")})
        self.assertEqual(by_name["Springfield Motors of Joplin"], {("unrelated", "rule", "dealer")})
        for name in ("Person Example 4", "Person Example 5", "Person Example 6"):
            self.assertEqual(by_name[name], {("unrelated", "rule", "personal_account")})
        self.assertTrue(all("Controlled Mock" in ad["relevance"]["reason"]
                            for ad in ads.values() if ad["relevance"]["source"] == "ai"))

        state = self._filter(accepted["scan_id"])
        rows = self._assessments(accepted["scan_id"])
        self.assertEqual(Counter(row.revision for row in rows), {1: 128})  # one judgment each
        rule_ids = {row.competitor_ad_id for row in rows if row.source == "rule"}
        assigned = {ad_id for entry in state["assignments"].values() for group in entry["groups"]
                    for ad_id in group["ad_ids"]}
        self.assertFalse(rule_ids & assigned)  # never sent to the LLM
        self.assertEqual(sum(state["rules"].values()), len(rule_ids))
        self.assertTrue(all(row.attempt_id is None for row in rows if row.source == "rule"))
        # Variants share one judgment: Halcyon Motors' 20 ads are one collation group, one item of one call.
        halcyon = [row for row in rows if ads[_archive(ads, row.competitor_ad_id)]["advertiser_name"] == "Halcyon Motors"]
        self.assertEqual(len(halcyon), 20)
        self.assertEqual(len({(row.model_evidence_json["call_name"], row.model_evidence_json["item"])
                              for row in halcyon}), 1)
        self.assertTrue(all(len(entry["groups"]) <= relevance.MAX_BATCH for entry in state["assignments"].values()))
        groups = sum(len(entry["groups"]) for entry in state["assignments"].values())
        self.assertEqual(len(state["assignments"]), -(-groups // relevance.MAX_BATCH))
        self.assertEqual(state["answered"], list(state["assignments"]))
        self.assertEqual(list(state["assignments"]), [f"filter:{i}" for i in range(1, len(state["assignments"]) + 1)])
        self.assertEqual(state["product"]["source"], "product")
        evidence = self._record(accepted["scan_id"]).evidence_json
        tagged, discovered = evidence["tag"]["assignments"], evidence["discover"]["assignments"]
        # + one tag:<i> per tagged observation and cluster:1 (#272; every product has the expression
        # catalog's factor, #329) and one discover:<i> per advertiser (#276)
        self.assertTrue(tagged)
        self.assertEqual(scan["cost"]["llm"]["calls"], len(state["assignments"]) + len(tagged) + 1 + len(discovered))

        # Filtering by relevance partitions the observations.
        parts = {value: {ad["ad_id"] for ad in self._all_ads(accepted["scan_id"], relevance=value)}
                 for value in relevance.RELEVANCE}
        self.assertEqual(sum(len(part) for part in parts.values()), 128)
        self.assertEqual({ad["ad_id"] for ad in ads.values() if ad["advertiser_name"] == "Halcyon Motors"} - parts["unrelated"],
                         set())

    def test_scenario_7_first_half_fsd_is_unrelated_and_nothing_is_tagged(self):
        accepted = self._start(self._body([self._confirmed(HALCYON)], keywords=[]))
        self._drain()
        ads = self._all_ads(accepted["scan_id"])
        self.assertEqual(len(ads), 20)
        self.assertTrue(all(ad["cards"][0]["title"] == "Explore Full Self-Driving" for ad in ads))
        self.assertEqual({(ad["relevance"]["relevance"], ad["relevance"]["source"]) for ad in ads},
                         {("unrelated", "ai")})
        self.assertEqual(self._all_ads(accepted["scan_id"], relevance="relevant"), [])  # no tag candidate
        attempts = self._llm_attempts(accepted["job_id"])
        self.assertEqual(len(attempts), 1)  # one batch: the 20 variants are one group
        with tenant_session(self.ctx) as session:
            names = set(session.scalars(select(ProviderCallAttemptRecord.call_semantic_key_hash).where(
                ProviderCallAttemptRecord.job_id == accepted["job_id"],
                ProviderCallAttemptRecord.step_kind == "llm_chat")).all())
        self.assertEqual(names, {llm_call_key(accepted["job_id"], "filter:1")})  # no tag:<i> call
        steps = {step["step"]: (step["status"], step["reason"]) for step in self._scan(accepted["scan_id"])["steps"]}
        self.assertEqual(steps["tag"][0], "skipped")  # no relevant observation to tag (#272)

    def test_a_zh_cn_scan_reasons_in_chinese(self):
        self.current = dataclasses.replace(self.ctx, ui_language="zh-CN")
        accepted = self._start(self._body([self._confirmed(HALCYON)], keywords=[]))
        self.current = self.ctx
        self._drain()
        ads = self._all_ads(accepted["scan_id"])
        self.assertTrue(all(ad["relevance"]["reason"].startswith("受控 Mock") for ad in ads))
        [row] = {row.payload_json["language"] for row in self._assessments(accepted["scan_id"])}
        self.assertEqual(row, "zh-CN")
        self.assertEqual(self._filter(accepted["scan_id"])["language"], "zh-CN")

    def test_the_product_understanding_is_frozen_into_the_step(self):
        with tenant_session(self.owner_ctx(self.ctx)) as session:
            session.add(MarketingProjectRecord(
                id=f"mp_{self.ctx.workspace_id}", workspace_id=self.ctx.workspace_id, product_id=self.product_id,
                state="understanding_confirmed", source_text="VoltNest", understanding_json={
                    "product_name": "VoltNest 7kW", "category_need": "Charge an EV at home overnight",
                    "situations": ["Home garage", "Overnight charging"],
                },
            ))
        accepted = self._start(self._body([self._confirmed(HALCYON)], keywords=[]))
        self._drain()
        product = self._filter(accepted["scan_id"])["product"]
        self.assertEqual((product["source"], product["product_name"], product["category_need"], product["situations"]),
                         ("product_understanding", "VoltNest 7kW", "Charge an EV at home overnight",
                          ["Home garage", "Overnight charging"]))


class ByokFilterTests(RelevanceFixtures):
    def setUp(self) -> None:
        super().setUp()
        self._bind_both()

    def _filter_reservations(self, job_id: str) -> dict[str, str]:
        reservations = self._scan_reservations(job_id)
        names = {llm_call_key(job_id, f"filter:{i}"): f"filter:{i}" for i in range(1, 200)}
        return {names[key]: row.state for key, row in reservations.items() if key in names}

    def test_reservations_are_named_like_the_calls_and_none_stays_open(self):
        accepted = self._start(self._body())
        self._drain()
        state = self._filter(accepted["scan_id"])
        batches = set(state["assignments"])
        reservations = self._filter_reservations(accepted["job_id"])
        self.assertEqual(len(reservations), 20)  # ceil((3 x 3 + 2 x 2) planned pages x 30 / 20), reserved by C-21
        self.assertEqual({name for name, value in reservations.items() if value == "settled"}, batches)
        self.assertEqual({name for name, value in reservations.items() if value == "released"},
                         set(reservations) - batches)
        self.assertEqual(set(state["released"]), set(reservations) - batches)  # released when the step ended
        self.assertNotIn("reserved", {row.state for row in self._scan_reservations(accepted["job_id"]).values()})
        attempts = self._filter_attempts(accepted["job_id"])
        self.assertEqual({a.call_semantic_key_hash for a in attempts},
                         {llm_call_key(accepted["job_id"], name) for name in batches})
        self.assertEqual({(a.dispatch_state, a.chargeability) for a in attempts}, {("succeeded", "charged")})
        # The model saw text only, in the scan's language, within the filter reservation.
        filter_requests = [body for body in self.openai_requests
                           if body["response_format"]["json_schema"]["name"] == relevance.SCHEMA_NAME]
        self.assertEqual(len(filter_requests), len(batches))
        for body in filter_requests:
            self.assertTrue(all(isinstance(message["content"], str) for message in body["messages"]))
            self.assertEqual(body["max_completion_tokens"], 4_000)
            self.assertIn("never its images", body["messages"][0]["content"])

    def test_a_redelivered_job_replays_its_batches_without_new_llm_calls(self):
        original = relevance.normalize
        calls = []

        def die_after_the_first_answer(data, ids):
            calls.append(1)
            if len(calls) == 1:
                raise Killed("worker killed after the answer, before the judgments")
            return original(data, ids)

        accepted = self._start(self._body())
        runtime = self._runtime()
        with patch.object(relevance, "normalize", die_after_the_first_answer), self.assertRaises(Killed):
            for _ in range(5):
                asyncio.run(runtime.run_once())
        frozen = self._filter(accepted["scan_id"])
        self.assertEqual(frozen["answered"], [])
        self.assertEqual(self._assessments(accepted["scan_id"]) and {
            row.source for row in self._assessments(accepted["scan_id"])}, {"rule"})
        first = {a.call_semantic_key_hash: a.id for a in self._filter_attempts(accepted["job_id"])}
        self.assertEqual(set(first), {llm_call_key(accepted["job_id"], "filter:1")})
        sent = len(self.openai_requests)

        self._redeliver(accepted["job_id"])
        with patch.object(relevance, "normalize", die_after_the_first_answer):
            self._drain()
        state = self._filter(accepted["scan_id"])
        self.assertEqual(state["assignments"], frozen["assignments"])  # read, not recomputed
        attempts = self._filter_attempts(accepted["job_id"])
        self.assertEqual(len(attempts), len(state["assignments"]))  # one attempt per call name, ever
        self.assertEqual(Counter(a.call_semantic_key_hash for a in attempts).most_common(1)[0][1], 1)
        self.assertEqual({a.call_semantic_key_hash: a.id for a in attempts}[llm_call_key(accepted["job_id"],
                                                                                         "filter:1")],
                         first[llm_call_key(accepted["job_id"], "filter:1")])
        self.assertTrue(state["calls"]["filter:1"]["replayed"])
        # filter:1 is not resent; each tag:<i> (#272; the expression catalog's factor, #329) and
        # cluster:1 are asked once, after the filter step, then each discover:<i> once (#276).
        evidence = self._record(accepted["scan_id"]).evidence_json
        tagged, discovered = evidence["tag"]["assignments"], evidence["discover"]["assignments"]
        self.assertTrue(tagged)
        self.assertEqual(len(self.openai_requests),
                         sent + len(state["assignments"]) - 1 + len(tagged) + 1 + len(discovered))
        self.assertEqual(Counter(row.revision for row in self._assessments(accepted["scan_id"])), {1: 128})
        self.assertNotIn("reserved", {row.state for row in self._scan_reservations(accepted["job_id"]).values()})

    def test_a_resume_judges_only_what_no_batch_holds_under_its_own_names(self):
        self.mock_credits = 4  # Acme Charge's three pages and Brightvolt's one; Halcyon Motors' first page gets a 402
        accepted = self._start(self._body())
        self._drain()
        first = self._filter(accepted["scan_id"])
        self.assertEqual(self._scan(accepted["scan_id"])["status"], "partial")
        self.assertEqual(len(self._assessments(accepted["scan_id"])), 62)  # what was fetched is judged
        answered = set(first["answered"])
        self.assertEqual(answered, set(first["assignments"]))

        self.mock_credits = 100
        resumed = self._resume(accepted["scan_id"])
        reserved = self._filter_reservations(resumed["job_id"])
        self.assertFalse(answered & set(reserved))  # answered ordinals are not reserved again
        self._drain()
        state = self._filter(accepted["scan_id"])
        for name, entry in first["assignments"].items():
            self.assertEqual(state["assignments"][name], entry)  # the first run's table is untouched
        added = set(state["assignments"]) - set(first["assignments"])
        self.assertTrue(added)
        self.assertTrue(all(state["assignments"][name]["job_id"] == resumed["job_id"] for name in added))
        self.assertEqual({a.call_semantic_key_hash for a in self._filter_attempts(resumed["job_id"])},
                         {llm_call_key(resumed["job_id"], name) for name in added})
        rows = self._assessments(accepted["scan_id"])
        self.assertEqual(Counter(row.revision for row in rows), {1: 128})
        self.assertNotIn("reserved", {row.state for row in self._scan_reservations(resumed["job_id"]).values()})

    def test_a_rejected_ai_key_stops_the_step_and_releases_the_rest(self):
        self.openai_answers = [httpx.Response(401, json={"error": {"message": "bad key", "code": "invalid_api_key"}})]
        accepted = self._start(self._body([self._confirmed(ACME), self._confirmed(BRIGHTVOLT)], keywords=[]))
        self._drain()
        scan = self._scan(accepted["scan_id"])
        self.assertEqual(scan["status"], "completed")  # the pages are fetched; only the judgments are missing
        steps = {step["step"]: (step["status"], step["reason"]) for step in scan["steps"]}
        self.assertEqual(steps["filter"], ("failed", "credential_rejected"))
        self.assertEqual(scan["stage"], "media")
        state = self._filter(accepted["scan_id"])
        self.assertEqual(state["answered"], [])
        self.assertEqual(state["calls"]["filter:1"]["error_code"], "credential_rejected")
        self.assertEqual(len(self.openai_requests), 1)  # no later batch is tried
        self.assertEqual(len(self._all_ads(accepted["scan_id"], relevance="unassessed")), 62)
        self.assertNotIn("reserved", {row.state for row in self._scan_reservations(accepted["job_id"]).values()})


class FlipTests(RelevanceFixtures):
    def _judged_scan(self) -> tuple[str, dict]:
        accepted = self._start(self._body([self._confirmed(HALCYON)], keywords=[]))
        self._drain()
        return accepted["scan_id"], self._all_ads(accepted["scan_id"])[0]

    def test_flipping_twice_back_appends_two_revisions_kept_in_history(self):
        scan_id, ad = self._judged_scan()
        self.assertEqual(ad["relevance"]["relevance"], "unrelated")
        first = self._flip(scan_id, ad["ad_id"], "relevant")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual((first.json()["revision"], first.json()["source"], first.json()["created_by"]),
                         (2, "user", "member_a"))
        self.assertEqual(first.json()["previous"], {"revision": 1, "relevance": "unrelated", "source": "ai"})
        self.current = self.owner_ctx(self.ctx)  # the Owner may flip as well
        back = self._flip(scan_id, ad["ad_id"], "unrelated")
        self.assertEqual((back.status_code, back.json()["revision"], back.json()["created_by"]), (200, 3, "owner_a"))
        self.current = self.ctx

        history = self._history(scan_id, ad["ad_id"]).json()
        self.assertEqual([(item["revision"], item["source"], item["relevance"]) for item in history["items"]],
                         [(1, "ai", "unrelated"), (2, "user", "relevant"), (3, "user", "unrelated")])
        self.assertEqual(history["current"]["revision"], 3)
        self.assertEqual(history["items"][0]["evidence"]["call_name"], "filter:1")
        self.assertIsNone(history["items"][1]["evidence"])
        listed = {item["ad_id"]: item for item in self._all_ads(scan_id)}[ad["ad_id"]]
        self.assertEqual((listed["relevance"]["relevance"], listed["relevance"]["revision"],
                          listed["relevance"]["source"]), ("unrelated", 3, "user"))
        with tenant_session(self.ctx) as session:
            events = session.scalars(select(AuditEventRecord).where(
                AuditEventRecord.workspace_id == self.ctx.workspace_id,
                AuditEventRecord.event_type == "competitor_ad.relevance_flipped",
            ).order_by(AuditEventRecord.created_at)).all()
            self.assertEqual([(e.initiator_id, e.initiator_role, e.resource_id, e.details_json["relevance"])
                              for e in events],
                             [("member_a", "member", ad["ad_id"], "relevant"),
                              ("owner_a", "owner", ad["ad_id"], "unrelated")])

    def test_a_flip_shows_in_the_relevance_filter(self):
        scan_id, ad = self._judged_scan()
        self.assertEqual(self._flip(scan_id, ad["ad_id"], "relevant").status_code, 200)
        self.assertEqual([item["ad_id"] for item in self._all_ads(scan_id, relevance="relevant")], [ad["ad_id"]])
        self.assertEqual(len(self._all_ads(scan_id, relevance="unrelated")), 19)

    def test_a_flip_replays_by_key_and_refuses_the_key_for_another_verdict(self):
        scan_id, ad = self._judged_scan()
        first = self._flip(scan_id, ad["ad_id"], "adjacent", key="flip-1")
        again = self._flip(scan_id, ad["ad_id"], "adjacent", key="flip-1")
        self.assertEqual((again.json()["assessment_id"], again.json()["replayed"]),
                         (first.json()["assessment_id"], True))
        self._problem(self._flip(scan_id, ad["ad_id"], "relevant", key="flip-1"), 409, "idempotency_key_reused")
        self.assertEqual(len(self._history(scan_id, ad["ad_id"]).json()["items"]), 2)
        bad = self._flip(scan_id, ad["ad_id"], "maybe")
        self.assertEqual(bad.status_code, 422, bad.text)

    def test_a_flip_before_any_judgment_is_revision_one_and_the_ai_keeps_it(self):
        accepted = self._start(self._body([self._confirmed(BRIGHTVOLT)], keywords=[]))
        runtime = self._runtime()
        with patch.object(relevance, "build_request", side_effect=Killed("killed before the first call")):
            with self.assertRaises(Killed):
                for _ in range(5):
                    asyncio.run(runtime.run_once())
        ad = self._all_ads(accepted["scan_id"])[0]
        self.assertIsNone(ad["relevance"])
        self.assertEqual(self._flip(accepted["scan_id"], ad["ad_id"], "adjacent").json()["revision"], 1)
        self._redeliver(accepted["job_id"])
        self._drain()
        history = self._history(accepted["scan_id"], ad["ad_id"]).json()["items"]
        self.assertEqual([(item["source"], item["relevance"]) for item in history], [("user", "adjacent")])

    def test_unknown_or_foreign_observations_are_not_found(self):
        scan_id, ad = self._judged_scan()
        other = self._start(self._body([self._confirmed(BRIGHTVOLT)], keywords=[]))
        self._drain()
        foreign_ad = self._all_ads(other["scan_id"])[0]
        # An observation of another scan, or none at all, in this Workspace.
        for response in (self._flip(scan_id, foreign_ad["ad_id"], "relevant"), self._history(scan_id, "cad_missing")):
            self._problem(response, 404, "competitor_ad_not_found")
        # Another Workspace (a non-member here) sees nothing and changes nothing.
        self.current = self._make_context(role="member", actor="member_b")
        for response in (self._flip(scan_id, ad["ad_id"], "relevant"), self._history(scan_id, ad["ad_id"]),
                         self.client.get(f"/api/v1/competitor-scans/{scan_id}/ads", params={"relevance": "unrelated"})):
            self._problem(response, 404, "competitor_scan_not_found")
        self.current = self.owner_ctx(self.current)
        self._problem(self._flip(scan_id, ad["ad_id"], "relevant"), 404, "competitor_scan_not_found")
        self.current = self.ctx
        self.assertEqual(len(self._history(scan_id, ad["ad_id"]).json()["items"]), 1)

    def test_assessments_are_append_only(self):
        scan_id, ad = self._judged_scan()
        for statement in (
            update(CompetitorAdAssessmentRecord).where(CompetitorAdAssessmentRecord.competitor_ad_id == ad["ad_id"])
            .values(relevance="relevant"),
            CompetitorAdAssessmentRecord.__table__.delete().where(
                CompetitorAdAssessmentRecord.competitor_ad_id == ad["ad_id"]),
        ):
            with self.assertRaises(DatabaseError):
                with tenant_session(self.ctx) as session:
                    session.execute(statement)


class RuleAndBatchTests(RelevanceFixtures):
    @staticmethod
    def _ad(**fields) -> CompetitorAdRecord:
        defaults = {"id": "cad_x", "ad_archive_id": "1", "collation_id": None, "advertiser_page_id": "p",
                    "page_categories_json": [], "landing_domain": None, "cards_json": []}
        return CompetitorAdRecord(**{**defaults, **fields})

    def test_rules_apply_to_keyword_only_observations(self):
        dealer = self._ad(page_categories_json=["Auto Dealer"], landing_domain="dealer.example")
        self.assertEqual(relevance.rule_verdict(dealer, ["keyword:NACS adapter"])["rule"], "dealer")
        self.assertIsNone(relevance.rule_verdict(dealer, ["keyword:NACS adapter", "page:cmp_1"]))
        wholesale = self._ad(landing_domain="made-in-china.com")
        self.assertEqual(relevance.rule_verdict(wholesale, ["keyword:x"]),
                         {"rule": "wholesale_site", "matched": "made-in-china.com"})
        with patch.object(config, "COMPETITOR_WHOLESALE_DOMAINS", "example-wholesale.com"):
            self.assertIsNone(relevance.rule_verdict(wholesale, ["keyword:x"]))
            self.assertEqual(relevance.rule_verdict(self._ad(landing_domain="example-wholesale.com"),
                                                    ["keyword:x"])["rule"], "wholesale_site")
        listing = self._ad(landing_domain="facebook.com",
                           cards_json=[{"link_url": "https://facebook.com/marketplace/item/1/"}])
        self.assertEqual(relevance.rule_verdict(listing, ["keyword:x"])["rule"], "personal_account")
        instant_experience = self._ad(landing_domain="fb.com", page_categories_json=["Tech"],
                                      cards_json=[{"link_url": "https://fb.com/canvas_doc/1"}])
        self.assertIsNone(relevance.rule_verdict(instant_experience, ["keyword:x"]))
        self.assertEqual(relevance.rule_verdict(self._ad(page_categories_json=["汽车经销商"]), ["keyword:x"])["rule"],
                         "dealer")

    def test_batches_hold_at_most_thirty_groups_evenly(self):
        groups = [{"key": str(i), "ad_ids": [str(i)], "representative": str(i)} for i in range(61)]
        self.assertEqual([len(batch) for batch in relevance.batches_of(groups)], [21, 20, 20])
        self.assertEqual([len(batch) for batch in relevance.batches_of(groups[:30])], [30])
        self.assertEqual([len(batch) for batch in relevance.batches_of(groups[:31])], [16, 15])
        self.assertEqual(relevance.batches_of([]), [])

    def test_a_full_batch_of_long_texts_fits_the_filter_reservation(self):
        long_zh = "这款充电适配器" * 400
        ads = {
            f"cad_{i}": self._ad(id=f"cad_{i}", ad_archive_id=str(i), page_categories_json=["Brand"] * 5,
                                  cards_json=[{"title": long_zh, "body": long_zh, "link_description": long_zh,
                                               "link_url": f"https://example.com/{'p' * 500}?utm=x"}])
            for i in range(relevance.MAX_BATCH)
        }
        batch = [{"key": key, "ad_ids": [key], "representative": key} for key in ads]
        product = {"product_name": "产品" * 100, "category_need": "需求" * 300, "situations": ["场景" * 100] * 8}
        for language in ("en", "zh-CN"):
            request, items = relevance.build_request(batch, ads, product, language=language, execution_mode="byok")
            from app.providers.llm_chat import estimate_request_input_tokens

            self.assertLessEqual(estimate_request_input_tokens(request, execution_mode="byok"), 40_000)
            self.assertEqual(request.images, ())
            self.assertEqual([item["id"] for item in items], [f"a{i}" for i in range(1, 31)])
            self.assertNotIn("utm", json.dumps(items))
        answer = relevance.mock_answer(request.messages[-1].text)
        self.assertEqual(len(answer["judgments"]), 30)
        request.output_validator(answer)
        with self.assertRaises(Exception):
            request.output_validator({"judgments": answer["judgments"][:-1]})


def _archive(ads: dict[str, dict], ad_id: str) -> str:
    return next(archive for archive, ad in ads.items() if ad["ad_id"] == ad_id)


if __name__ == "__main__":
    import unittest

    unittest.main()
