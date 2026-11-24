"""Starting a test of kept ads (ADR 0019, #249): one idempotent command opens the test.

In one transaction the command creates the test project with its confirmed plan, freezes the round, puts each
kept ad into it as it is (a reuse record that names the ideas and the ad, plan E4) and admits the "look
together" check of each ad with its reservation (plan E6). The ideas and their ads stay as they were.

The ads are the ones a Controlled Mock (or fake BYOK) first batch made for Acme Charge Home
(``tests.test_marketing_ideas_export``); the photo tests use the Acme Charge page with pictures
(``tests.test_marketing_ideas_photos``). No paid call is made.
"""
import threading
from unittest.mock import patch

from sqlalchemy import func, select

from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import AuditEventRecord, WorkspaceRecord, new_id
from app.entitlements.cost_models import SpendReservationRecord
from app.generation.review import create_review
from app.marketing import ad_tests, ai_qc, ideas_export, product_references, workflow
from app.marketing.ad_tests_contracts import MarketingTestDetail
from app.marketing.ai_qc_models import MarketingAIQCRecord
from app.marketing.models import MarketingProjectRecord, MarketingRevisionRecord
from app.marketing.workflow_models import MarketingCreativeReuseRecord, MarketingRoundRecord
from app.platform.operations import create_operation
from tests.test_marketing_ideas_photos import PhotoFixtures
from tests.test_marketing_test_quote import TestQuoteFixtures


class TestStartFixtures(TestQuoteFixtures):
    def start_test(self, project, quote=None, expect=201, key=None, revision=None, answers=None, **params):
        """POST the start command for the quote the page shows (``params`` change the answers)."""
        answers = {"target_cost": "10", "round_budget": "400", "duration_days": 7, **(answers or params)}
        quote = quote or self.quote_of(project, **params)
        body = {"answers": answers, "quote_hash": quote["quote_hash"],
                "asset_version_ids": sorted(i for s in quote["subjects"] for i in s["asset_version_ids"])}
        headers = self.headers(revision or self.read_ideas(project)["revision"], key)
        response = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/tests", headers=headers, json=body)
        self.assertEqual(response.status_code, expect, response.text)
        if expect == 201:
            MarketingTestDetail.model_validate(response.json())
            self.assertEqual(response.headers["etag"], '"1"')
            self.assertNotIn("location", response.headers)
        return response.json()

    def started_tests(self, project):
        with tenant_session(self.context) as session:
            rows = session.scalars(select(MarketingProjectRecord).where(
                MarketingProjectRecord.workspace_id == self.context.workspace_id,
                MarketingProjectRecord.source_project_id == project["id"])).all()
            for row in rows:
                session.expunge(row)
            return rows

    def paired_calls(self, round_id):
        with tenant_session(self.context) as session:
            rows = session.scalars(select(MarketingAIQCRecord).where(
                MarketingAIQCRecord.workspace_id == self.context.workspace_id, MarketingAIQCRecord.round_id == round_id)
                .order_by(MarketingAIQCRecord.asset_version_id)).all()
            for row in rows:
                session.expunge(row)
            return rows

    def reservations(self):
        with tenant_session(self.context) as session:
            return session.scalar(select(func.count()).select_from(SpendReservationRecord).where(
                SpendReservationRecord.workspace_id == self.context.workspace_id))

    def events(self, resource_id):
        with tenant_session(self.context) as session:
            return session.scalars(select(AuditEventRecord.event_type).where(
                AuditEventRecord.workspace_id == self.context.workspace_id, AuditEventRecord.resource_id == resource_id)
                .order_by(AuditEventRecord.created_at, AuditEventRecord.id)).all()

    def nothing_started(self, project):
        self.assertEqual(self.started_tests(project), [])
        with tenant_session(self.context) as session:
            self.assertIsNone(ad_tests.in_progress(session, self.context, project["product_id"]))

    def ideas_as_seen(self, project):
        """What the ideas page shows of the ideas and their ads."""
        current = self.read_ideas(project)
        return (current["revision"], current["state"],
                [(ad["asset_version_id"], ad["status"], ad["kept"], [r["code"] for r in ad["reasons"]]) for ad in current["ads"]])


class StartTests(TestStartFixtures):
    def test_one_start_opens_one_test_with_one_round_a_reuse_per_ad_and_its_checks(self):
        project = self.exportable()
        self.keep(project, "c1_1", "c2_1", "c2_2")
        quote = self.quote_of(project)
        ads = self.ads(project)
        kept = sorted(ads[cid]["asset_version_id"] for cid in ("c1_1", "c2_1", "c2_2"))
        reservations = self.reservations()
        started = self.start_test(project, quote)

        [test] = self.started_tests(project)
        self.assertEqual((started["id"], started["revision"], started["source_project_id"], started["product_id"]),
                         (test.id, 1, project["id"], project["product_id"]))
        self.assertEqual((test.flow, test.state, test.revision), ("ads_test", "plan_confirmed", 1))
        self.assertEqual((started["comparison"], started["plan_summary"]),
                         ("claims", {"target_cost": "10", "round_budget": "400", "duration_days": 7,
                                     "currency": "USD", "landing_page": "https://acmecharge.example/products/home"}))
        # One round, frozen with the quoted dates, mode and protocol.
        [summary] = started["rounds"]
        self.assertEqual(started["current_round_id"], summary["id"])
        self.assertEqual((summary["state"], summary["start_date"], summary["end_date"]),
                         ("reviewing", quote["start_date"], quote["end_date"]))
        with tenant_session(self.context) as session:
            row = workflow.get_round(session, self.context, summary["id"])
            self.assertEqual((row.execution_mode, row.timezone, row.plan_revision, row.snapshot_json["attribution_window"]),
                             ("mock", quote["account_timezone"], 1, quote["attribution_window"]))
            self.assertEqual(row.snapshot_json["plan"]["comparison_kind"], "whole_combinations")
            # The plan is confirmed as revision 1, with the ideas' understanding in the plan-first shape.
            [revision] = session.scalars(select(MarketingRevisionRecord).where(
                MarketingRevisionRecord.workspace_id == self.context.workspace_id,
                MarketingRevisionRecord.project_id == test.id)).all()
            self.assertEqual((revision.revision, revision.reason), (1, "confirm_plan"))
            self.assertNotIn("unsupported_claims", test.understanding_json)
            # Every kept ad is in the round, as it is, under the combination its image was made from.
            reuses = session.scalars(select(MarketingCreativeReuseRecord).where(
                MarketingCreativeReuseRecord.workspace_id == self.context.workspace_id,
                MarketingCreativeReuseRecord.round_id == row.id)).all()
            self.assertEqual(sorted(r.asset_version_id for r in reuses), kept)
            for reuse in reuses:
                ad = next(a for a in ads.values() if a["asset_version_id"] == reuse.asset_version_id)
                source = reuse.source_json
                self.assertEqual((reuse.combination_id, source["kind"], source["ideas_project_id"], source["asset_version_id"]),
                                 (ad["combination_id"], "ideas", project["id"], ad["asset_version_id"]))
                self.assertRegex(source["intent_hash"], r"^[0-9a-f]{64}$")
                self.assertTrue(source["verification_record_id"].startswith("mie"))
            self.assertEqual(sorted(v.id for v, _, _ in workflow.creative_assets(session, self.context, row)), kept)
        # One "look together" check per kept ad, Controlled Mock as the ads were made, nothing reserved.
        calls = self.paired_calls(summary["id"])
        self.assertEqual(sorted(c.asset_version_id for c in calls), kept)
        self.assertEqual({(c.kind, c.state, c.execution_mode, c.policy_version) for c in calls},
                         {("paired", "queued", "mock", ai_qc.POLICIES["paired"])})
        self.assertTrue(all(any(i["role"] == "comparison" for i in c.input_json["images"]) for c in calls))
        self.assertEqual(self.reservations(), reservations)

        # Each step wrote its own audit event.
        self.assertEqual(self.events(test.id), ["marketing.confirm_plan", "marketing.test_started"])
        self.assertEqual(sorted(set(self.events(summary["id"]))),
                         ["marketing.ai_qc_requested", "marketing.creative_reused", "marketing.frozen"])
        self.assertEqual(self.events(summary["id"]).count("marketing.creative_reused"), 3)
        with tenant_session(self.context) as session:
            details = session.scalar(select(AuditEventRecord.details_json).where(
                AuditEventRecord.workspace_id == self.context.workspace_id, AuditEventRecord.resource_id == test.id,
                AuditEventRecord.event_type == "marketing.test_started"))
        self.assertEqual((details["source_project_id"], details["quote_hash"], details["round_id"], details["asset_version_ids"]),
                         (project["id"], quote["quote_hash"], summary["id"], kept))
        self.assertEqual(sorted(details["check_ids"]), sorted(c.id for c in calls))

        # The checks run on the Controlled Mock example, like any round check.
        self.drain(job_types=("marketing.ai_qc",))
        self.assertEqual({c.state for c in self.paired_calls(summary["id"])}, {"succeeded"})

    def test_the_ideas_and_their_ads_stay_as_they_were(self):
        project = self.exportable()
        self.keep(project, "c1_1", "c2_1")
        before = self.ideas_as_seen(project)
        self.start_test(project)
        self.assertEqual(self.ideas_as_seen(project), before)
        # The ideas are still ideas: the test project is not one of them.
        listed = [item["id"] for item in self.client.get("/api/v1/marketing/ideas").json()["items"]]
        self.assertIn(project["id"], listed)
        self.assertEqual(len([i for i in listed if i in {t.id for t in self.started_tests(project)}]), 0)

    def test_the_same_key_replays_the_same_test(self):
        project = self.exportable()
        self.keep(project, "c1_1", "c2_1")
        quote = self.quote_of(project)
        key = new_id("key")
        first = self.start_test(project, quote, key=key)
        again = self.start_test(project, quote, key=key)
        self.assertEqual(again, first)
        self.assertEqual(len(self.started_tests(project)), 1)
        self.assertEqual(len(self.paired_calls(first["current_round_id"])), 2)

    def test_a_product_with_a_test_in_progress_starts_no_other(self):
        project = self.exportable()
        self.keep(project, "c1_1", "c2_1")
        quote = self.quote_of(project)
        first = self.start_test(project, quote)
        refused = self.start_test(project, quote, expect=409)
        self.assertEqual((refused["code"], refused["params"]["test_id"]), ("test_in_progress", first["id"]))
        self.assertEqual(len(self.started_tests(project)), 1)
        # Once its round ends, the product may start the next test.
        self.close(first["current_round_id"])
        second = self.start_test(project, quote)
        self.assertNotEqual(second["id"], first["id"])

    def test_two_starts_at_once_open_one_test(self):
        project = self.exportable()
        self.keep(project, "c1_1", "c2_1")
        quote = self.quote_of(project)
        revision = self.read_ideas(project)["revision"]
        barrier, results = threading.Barrier(2), []
        body = {"answers": {"target_cost": "10", "round_budget": "400", "duration_days": 7}, "quote_hash": quote["quote_hash"],
                "asset_version_ids": sorted(i for s in quote["subjects"] for i in s["asset_version_ids"])}

        def click():
            barrier.wait(timeout=10)
            response = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/tests", json=body,
                                        headers=self.headers(revision))
            results.append((response.status_code, response.json().get("code")))
        threads = [threading.Thread(target=click) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive(), "a start hung")
        self.assertEqual(sorted(results, key=lambda r: r[0]), [(201, None), (409, "test_in_progress")])
        [test] = self.started_tests(project)
        with tenant_session(self.context) as session:
            rounds = session.scalars(select(MarketingRoundRecord).where(
                MarketingRoundRecord.workspace_id == self.context.workspace_id, MarketingRoundRecord.project_id == test.id)).all()
        self.assertEqual(len(rounds), 1)
        self.assertEqual(len(self.paired_calls(rounds[0].id)), 2)

    def test_one_kept_ad_is_checked_against_its_own_intent_with_nothing_to_wait_for(self):
        project = self.exportable()
        self.keep(project, "c1_1")
        quote = self.quote_of(project)
        self.assertEqual((quote["comparison"], quote["checks"]["quantity"]), ("single", 1))
        started = self.start_test(project, quote)
        [call] = self.paired_calls(started["current_round_id"])
        # Nothing is compared: the check asks whether the ad shows its own claim and way of telling it.
        self.assertEqual([i["role"] for i in call.input_json["images"]], ["target"])
        self.assertIn("belongs_to_intended_combination", call.input_json["required_checks"])
        self.assertEqual(call.input_json["comparison_kind"], "business_retest")
        # A test makes no image, so the board waits for none.
        board = self.client.get(f"/api/v1/marketing/rounds/{started['current_round_id']}?include_board=true"
                                "&include_qc_kinds=true&include_execution_plan=true").json()["data"]["board"]
        nodes = [n for a in board["tree"]["angles"] for x in a["expressions"] for c in x["combinations"] for n in c["executions"]]
        self.assertEqual([(n["asset_version_id"], n["awaiting_combination_ids"]) for n in nodes],
                         [(self.ads(project)["c1_1"]["asset_version_id"], [])])
        self.assertEqual(board["primary_comparison"]["original_asset_version_ids"], [])

    def test_one_claims_two_ways_are_checked_against_each_other(self):
        project = self.exportable()
        self.keep(project, "c1_1", "c1_2")
        started = self.start_test(project)
        self.assertEqual(started["comparison"], "expressions")
        calls = self.paired_calls(started["current_round_id"])
        self.assertEqual(len(calls), 2)
        ads = self.ads(project)
        for call in calls:
            other = ({ads["c1_1"]["asset_version_id"], ads["c1_2"]["asset_version_id"]} - {call.asset_version_id}).pop()
            self.assertEqual([i["asset_version_id"] for i in call.input_json["images"] if i["role"] == "comparison"], [other])
            self.assertIn("expressions_distinct", call.input_json["required_checks"])


class StartRefusalTests(TestStartFixtures):
    def test_an_ad_no_longer_kept_since_the_quote_is_refused(self):
        project = self.exportable()
        self.keep(project, "c1_1", "c2_1", "c2_2")
        quote = self.quote_of(project)
        self.keep(project, "c2_2", kept=False)
        refused = self.start_test(project, quote, expect=409)
        self.assertEqual(refused["code"], "test_quote_changed")
        self.nothing_started(project)

    def test_answers_other_than_the_quoted_ones_are_refused(self):
        project = self.exportable()
        self.keep(project, "c1_1", "c2_1")
        quote = self.quote_of(project)
        refused = self.start_test(project, quote, expect=409, answers={"target_cost": "10", "round_budget": "500",
                                                                       "duration_days": 7})
        self.assertEqual(refused["code"], "test_quote_changed")
        # The kept ads the page showed must be the ones kept now.
        response = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/tests",
                                    headers=self.headers(self.read_ideas(project)["revision"]),
                                    json={"answers": {"target_cost": "10", "round_budget": "400", "duration_days": 7},
                                          "quote_hash": quote["quote_hash"], "asset_version_ids": [new_id("av")]})
        self.assertEqual((response.status_code, response.json()["code"]), (409, "test_quote_changed"))
        self.nothing_started(project)

    def test_ads_that_may_not_be_exported_are_refused_and_named(self):
        project = self.checked_ads()  # nothing confirmed by the user: every ad is a draft
        self.keep(project, "c1_1", "c2_1")
        quote = self.quote_of(project)
        self.assertEqual(quote["unavailable_reason"], "ads_not_exportable")
        refused = self.start_test(project, quote, expect=409)
        self.assertEqual(refused["code"], "ads_not_exportable")
        ads = self.ads(project)
        self.assertEqual(sorted(item["asset_version_id"] for item in refused["params"]["ads"]),
                         sorted(ads[cid]["asset_version_id"] for cid in ("c1_1", "c2_1")))
        self.nothing_started(project)

    def test_an_ad_rejected_on_the_shared_review_chain_cannot_be_tested(self):
        project = self.exportable()
        self.keep(project, "c1_1", "c2_1")
        rejected = self.ads(project)["c2_1"]["asset_version_id"]
        with tenant_session(self.context) as session:
            create_review(session, self.context, rejected, "rejected", "The charger's port is drawn wrong.",
                          self.context.initiator_id)
        refused = self.start_test(project, expect=409)
        self.assertEqual((refused["code"], refused["params"]["asset_version_ids"]), ("ad_review_rejected", [rejected]))
        self.nothing_started(project)

    def test_the_ideas_revision_must_match(self):
        project = self.exportable()
        self.keep(project, "c1_1", "c2_1")
        quote = self.quote_of(project)
        stale = self.start_test(project, quote, expect=409, revision=self.read_ideas(project)["revision"] + 1)
        self.assertEqual(stale["code"], "stale_marketing_revision")
        response = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/tests", headers=self.headers(),
                                    json={"answers": {"target_cost": "10", "round_budget": "400", "duration_days": 7},
                                          "quote_hash": quote["quote_hash"], "asset_version_ids": []})
        self.assertEqual(response.status_code, 428, response.text)
        self.nothing_started(project)

    def test_nothing_kept_starts_nothing(self):
        project = self.exportable()
        refused = self.start_test(project, expect=409)
        self.assertEqual(refused["code"], "no_kept_ads")
        self.nothing_started(project)


class PaidStartTests(TestStartFixtures):
    def test_each_paid_check_reserves_its_maximum_when_the_test_starts(self):
        project = self.exportable("byok")
        self.keep(project, "c1_1", "c2_1", "c3_1")
        quote = self.quote_of(project)
        self.assertEqual((quote["execution_mode"], quote["checks"]["maximum_cost"]), ("byok", "0.15"))
        before = self.reservations()
        started = self.start_test(project, quote)
        calls = self.paired_calls(started["current_round_id"])
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.reservations(), before + 3)
        with tenant_session(self.context) as session:
            for call in calls:
                reservation = session.scalar(select(SpendReservationRecord).where(SpendReservationRecord.job_id == call.job_id))
                self.assertEqual((reservation.state, str(reservation.maximum_budget_amount.normalize())), ("reserved", "0.05"))
                self.assertEqual((call.execution_mode, call.maximum_cost), ("byok", "0.05"))
            row = session.get(MarketingRoundRecord, started["current_round_id"])
            self.assertEqual(row.execution_mode, "byok")

    def test_without_a_key_no_test_starts_and_nothing_is_reserved(self):
        project = self.exportable("byok")
        self.keep(project, "c1_1", "c2_1")
        before = self.reservations()
        with self.no_key():
            quote = self.quote_of(project)
            self.assertEqual(quote["unavailable_reason"], "checks_unavailable")
            refused = self.start_test(project, quote, expect=409)
        self.assertEqual(refused["code"], "checks_unavailable")
        self.assertTrue(refused["params"]["reason"])
        self.nothing_started(project)
        self.assertEqual(self.reservations(), before)


class StartAccessTests(TestStartFixtures):
    def test_another_workspace_cannot_start_a_test_of_these_ideas(self):
        project = self.exportable()
        self.keep(project, "c1_1", "c2_1")
        quote = self.quote_of(project)
        revision = self.read_ideas(project)["revision"]
        body = {"answers": {"target_cost": "10", "round_budget": "400", "duration_days": 7}, "quote_hash": quote["quote_hash"],
                "asset_version_ids": sorted(i for s in quote["subjects"] for i in s["asset_version_ids"])}
        other = self._make_context()
        self.use(other)
        theirs = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/tests", headers=self.headers(revision), json=body)
        missing = self.client.post(f"/api/v1/marketing/ideas/{new_id('mp')}/tests", headers=self.headers(revision), json=body)
        self.assertEqual((theirs.status_code, theirs.json()["code"]), (404, "marketing_project_not_found"))
        self.assertEqual((theirs.json()["code"], theirs.json()["detail"]), (missing.json()["code"], missing.json()["detail"]))
        self.use(self.context)
        self.nothing_started(project)

    def test_only_ads_first_ideas_start_a_test(self):
        project = self.exportable()
        self.keep(project, "c1_1", "c2_1")
        quote = self.quote_of(project)
        plan_first, _, _ = self.confirmed_plan()
        test_id, _ = self.make_test(source=project["id"], freeze=False)
        body = {"answers": {"target_cost": "10", "round_budget": "400", "duration_days": 7}, "quote_hash": quote["quote_hash"],
                "asset_version_ids": []}
        for project_id in (plan_first, test_id):
            response = self.client.post(f"/api/v1/marketing/ideas/{project_id}/tests", headers=self.headers(1), json=body)
            self.assertEqual((response.status_code, response.json()["code"]), (404, "marketing_project_not_found"), project_id)

    def test_a_workspace_without_current_authorization_starts_nothing(self):
        project = self.exportable()
        self.keep(project, "c1_1", "c2_1")
        quote = self.quote_of(project)
        with platform_session() as session:
            session.get(WorkspaceRecord, self.context.workspace_id).lifecycle = "suspended"
        refused = self.start_test(project, quote, expect=403, revision=self.read_ideas(project)["revision"])
        self.assertEqual(refused["code"], "workspace_inactive")
        with platform_session() as session:
            session.get(WorkspaceRecord, self.context.workspace_id).lifecycle = "active"
        self.nothing_started(project)

    def test_the_owner_and_a_member_may_start_one(self):
        project = self.exportable()
        self.keep(project, "c1_1", "c2_1")
        self.use(self.owner_ctx(self.context))
        started = self.start_test(project)
        self.use(self.context)
        self.assertEqual(self.client.get(f"/api/v1/marketing/tests/{started['id']}").json()["id"], started["id"])


class KeptAdPhotoTests(PhotoFixtures, TestStartFixtures):
    """A kept ad made with photos the user checked before download (#199) is judged by the ads-first photo rule
    in the round (plan E4), not by the creative-library rule its frozen references fail."""

    def checked_with_photos(self):
        project = self.checked_ads()
        self.everything_but_photos(project)
        self.decide(project, photo_ids=[photo["photo_id"] for photo in self.read_ideas(project)["photos"]])
        self.assertEqual({ad["status"] for ad in self.ads(project).values()}, {"exportable"})
        return self.read_ideas(project)

    def test_ads_made_with_checked_photos_are_current_in_the_round_and_compared(self):
        project = self.checked_with_photos()
        self.keep(project, "c1_1", "c2_1")
        ads = self.ads(project)
        with tenant_session(self.context) as session:
            # Frozen as unchecked photos, so the creative-library rule alone would never take them.
            version = session.get(workflow.AssetVersionRecord, ads["c1_1"]["asset_version_id"])
            self.assertFalse(product_references.lineage_current(session, self.context, version.reference_sources_json))
        started = self.start_test(project)
        round_id = started["current_round_id"]
        calls = self.paired_calls(round_id)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(any(i["role"] == "comparison" for i in c.input_json["images"]) for c in calls))
        with tenant_session(self.context) as session:
            row = workflow.get_round(session, self.context, round_id)
            facts = workflow.execution_facts(session, self.context, row, workflow.creative_assets(session, self.context, row))
            self.assertEqual({f["references_current"] for f in facts}, {True})
            self.assertEqual({f["reused"] for f in facts}, {False})

        # Rejecting a photo before the ad package: the ads that used it are not current in the round either.
        main = self.photos(project)["main"]["photo_id"]
        self.decide(project, rejected=[main])
        with tenant_session(self.context) as session:
            row = workflow.get_round(session, self.context, round_id)
            facts = workflow.execution_facts(session, self.context, row, workflow.creative_assets(session, self.context, row))
            self.assertEqual({f["references_current"] for f in facts}, {False})
            self.assertFalse(workflow.ready(session, self.context, row, ads["c1_1"]["asset_version_id"]))
            # Once the round's package exists, the ads are judged as they were when it was made (plan E4).
            operation = create_operation(session, command_type="marketing.export")
            session.add(workflow.Handoff(id=new_id("mh"), workspace_id=self.context.workspace_id, round_id=row.id,
                                         operation_id=operation.id, state="queued", manifest_json={"entries": []}))
            session.flush()
            facts = workflow.execution_facts(session, self.context, row, workflow.creative_assets(session, self.context, row))
            self.assertEqual({f["references_current"] for f in facts}, {True})


class PlanFirstRoundTests(TestStartFixtures):
    def test_a_plan_first_rounds_images_keep_the_creative_library_rule(self):
        project = self.confirmed()
        self.generate(project)
        row = self.start_round(project)
        # The ads-first photo rule is never asked about a plan-first round's images.
        with patch.object(ideas_export, "photos_current", side_effect=AssertionError("an ads-first rule")), \
                patch.object(product_references, "lineage_current", return_value=False) as library_rule:
            with tenant_session(self.context) as session:
                frozen = workflow.get_round(session, self.context, row["id"])
                assets = workflow.creative_assets(session, self.context, frozen)
                facts = workflow.execution_facts(session, self.context, frozen, assets)
                self.assertTrue(facts)
                self.assertEqual({(f["references_current"], f["reused"]) for f in facts}, {(False, False)})
                self.assertFalse(workflow.ready(session, self.context, frozen, assets[0][0].id))
        self.assertTrue(library_rule.called)
