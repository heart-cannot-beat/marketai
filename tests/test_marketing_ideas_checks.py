"""Automatic checks of ads-first ad drafts (#197): a single-image check per ad, with no round.

The ads come from a real first batch (fake page, fake model, fake image provider); the checks run through
the real endpoints and the real Worker code, with a fake vision model for BYOK. No paid call is made.
``IdeasChecks*Tests`` start the checks through the API on images a first batch made but has not checked
yet; ``FirstBatchCheckTests`` let the first batch check its own images (maintainer decision on #197).
"""
import asyncio
from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AuditEventRecord, JobRecord, OperationRecord, QualityReviewRecord, new_id
from app.entitlements.cost_models import ProviderCallAttemptRecord, SpendReservationRecord
from app.entitlements.credential_admin import CredentialAdmin
from app.entitlements.credential_protector import CredentialProtector
from app.marketing import ai_qc
from app.marketing.ai_qc_models import MarketingAIQCRecord
from app.marketing.ai_qc_worker import execute_ai_qc
from app.marketing.ideas import intent_of, project_ads
from app.marketing.ideas_contracts import IdeasChecksQuote, IdeasProjectDetail
from app.marketing.models import MarketingProjectRecord
from app.marketing.service import digest
from app.marketing.workflow_models import MarketingQCRecord, MarketingRoundRecord

from tests.test_marketing_first_batch import FirstBatchFixtures, check_report as report

#: The Worker jobs that make a first batch's images, without the advance that would check them.
IMAGE_JOBS = ("generate_asset", "generation.run_item", "generation.qc_asset_version")


class IdeasChecksFixtures(FirstBatchFixtures):
    def made(self, mode="mock"):
        """An ads-first project with the six ads a first batch made, none checked yet: the batch's own
        advance after the images is held back, so the checks are the API's to start."""
        if mode == "byok":
            self.byok()
        batch = self.start(mode)
        self.drain(job_types=("products.intake", "marketing.plan", "marketing.first_batch"))
        self.assertEqual(self.batch(batch["id"])["state"], "generating")
        self.drain(job_types=IMAGE_JOBS)
        project = self.read_ideas({"id": self.batch(batch["id"])["project_id"]})
        self.assertEqual(len(project["ads"]), 6)
        self.assertTrue(all(ad["check"] is None for ad in project["ads"]))
        return project

    def checks_quote(self, project, mode="mock", expect=200):
        response = self.client.get(f"/api/v1/marketing/ideas/{project['id']}/checks/quote", params={"execution_mode": mode})
        self.assertEqual(response.status_code, expect, response.text)
        if expect == 200:
            IdeasChecksQuote.model_validate(response.json())
        return response.json()

    def start_checks(self, project, mode="mock", quote_hash=None, expect=202, key=None):
        quote_hash = quote_hash or self.checks_quote(project, mode)["quote_hash"]
        response = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/checks", headers=self.headers(key=key),
                                    json={"execution_mode": mode, "quote_hash": quote_hash})
        self.assertEqual(response.status_code, expect, response.text)
        if expect == 202:
            IdeasProjectDetail.model_validate(response.json())
        return response.json()

    def check_calls(self, project_id, **filters):
        with tenant_session(self.context) as session:
            query = select(MarketingAIQCRecord).where(MarketingAIQCRecord.workspace_id == self.context.workspace_id,
                                                      MarketingAIQCRecord.project_id == project_id)
            for name, value in filters.items():
                query = query.where(getattr(MarketingAIQCRecord, name) == value)
            rows = session.scalars(query.order_by(MarketingAIQCRecord.asset_version_id)).all()
            for row in rows:
                session.expunge(row)
            return rows

    def run_checks(self, project, complete):
        """Execute the queued checks of ``project`` as the Worker would, with a fake vision model."""
        calls = self.check_calls(project["id"], state="queued")
        with tenant_session(self.context) as session:
            for call in calls:
                session.get(JobRecord, call.job_id).state = "running"
        for call in calls:
            asyncio.run(execute_ai_qc(self.context, call.id, self.blobs, complete=complete))
        return [call.id for call in calls]

    def by_asset(self, project):
        return {ad["asset_version_id"]: ad for ad in self.read_ideas(project)["ads"]}

    @contextmanager
    def no_key(self):
        """The workspace's OpenAI key revoked: nothing paid may be admitted."""
        with tenant_session(self.context) as session:
            CredentialAdmin(CredentialProtector.local_only()).revoke(session, self.owner_ctx(self.context),
                                                                    credential_key="planning", reason="test")
        yield


class IdeasChecksMockTests(IdeasChecksFixtures):
    def test_every_ad_is_checked_without_a_round_and_the_result_is_only_a_suggestion(self):
        project = self.made("mock")
        self.assertTrue(all(ad["check"] is None for ad in project["ads"]))
        quote = self.checks_quote(project)
        self.assertEqual((quote["available"], quote["quantity"], quote["maximum_cost"], quote["provider"]),
                         (True, 6, "0", "mock"))
        started = self.start_checks(project, quote_hash=quote["quote_hash"])
        self.assertTrue(all(ad["check"]["state"] == "queued" for ad in started["ads"]))
        # The real Worker loop: nothing waits for a round in review.
        self.drain(job_types=("marketing.ai_qc",))
        ads = self.read_ideas(project)["ads"]
        for ad in ads:
            check = ad["check"]
            self.assertEqual((check["state"], check["source"], check["execution_mode"]),
                             ("succeeded", "controlled_mock", "mock"))
            # The example never reports an image as good: every check is left for a person.
            self.assertEqual(check["suggestion"], "needs_confirmation")
            self.assertTrue(check["findings"])
            self.assertTrue(all(f["status"] == "unknown" for f in check["findings"]))
        with tenant_session(self.context) as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(MarketingRoundRecord).where(
                MarketingRoundRecord.workspace_id == self.context.workspace_id)), 0)
            # A suggestion, never a quality decision.
            self.assertEqual(session.scalar(select(func.count()).select_from(MarketingQCRecord).where(
                MarketingQCRecord.workspace_id == self.context.workspace_id)), 0)
            self.assertEqual(session.scalar(select(func.count()).select_from(QualityReviewRecord).where(
                QualityReviewRecord.workspace_id == self.context.workspace_id)), 0)
        calls = self.check_calls(project["id"])
        self.assertEqual(len(calls), 6)
        self.assertTrue(all((c.round_id, c.kind, c.policy_version) == (None, "single", ai_qc.IDEAS_POLICY) for c in calls))

    def test_a_check_records_the_understanding_it_was_made_against(self):
        project = self.made("mock")
        self.start_checks(project)
        with tenant_session(self.context) as session:
            row = session.get(MarketingProjectRecord, project["id"])
            intents = {version.id: intent_of(recipe) for version, recipe in project_ads(session, self.context, row)}
        for call in self.check_calls(project["id"]):
            intent = intents[call.asset_version_id]
            self.assertEqual(call.input_json["understanding"], intent["understanding"])
            self.assertEqual(call.input_json["understanding_hash"], digest(intent["understanding"]))
            self.assertEqual(call.input_json["evidence_fact_ids"], intent["cited_fact_ids"])
            self.assertEqual(call.input_json["combination"], intent["combination"])
            # Single-image checks only: nothing is compared with another ad.
            self.assertFalse(set(call.input_json["required_checks"]) & {"belongs_to_intended_combination",
                                                                        "expressions_distinct", "comparison_faithful"})
            self.assertEqual([i["role"] for i in call.input_json["images"]][:1], ["target"])
        ads = self.by_asset(project)
        for call in self.check_calls(project["id"]):
            self.assertEqual(ads[call.asset_version_id]["check"]["understanding_hash"], call.input_json["understanding_hash"])
            self.assertEqual(ads[call.asset_version_id]["check"]["fact_ids"], intents[call.asset_version_id]["cited_fact_ids"])

    def test_the_same_start_again_admits_nothing_more(self):
        project = self.made("mock")
        quote = self.checks_quote(project)
        key = new_id("key")
        first = self.start_checks(project, quote_hash=quote["quote_hash"], key=key)
        replay = self.start_checks(project, quote_hash=quote["quote_hash"], key=key)
        self.assertEqual([ad["check"]["id"] for ad in first["ads"]], [ad["check"]["id"] for ad in replay["ads"]])
        again = self.start_checks(project, quote_hash=quote["quote_hash"], expect=409)
        self.assertEqual((again["code"], again["params"]["reason"]), ("checks_unavailable", "checks_in_progress"))
        self.assertEqual(len(self.check_calls(project["id"])), 6)
        self.drain(job_types=("marketing.ai_qc",))
        # A finished check stays the image's answer: nothing is billed again.
        self.assertEqual(self.checks_quote(project)["unavailable_reason"], "nothing_to_check")

    def test_a_quote_the_user_did_not_see_is_refused(self):
        project = self.made("mock")
        refused = self.start_checks(project, quote_hash="0" * 64, expect=409)
        self.assertEqual(refused["code"], "checks_quote_changed")
        self.assertEqual(self.check_calls(project["id"]), [])

    def test_a_mock_check_never_stands_in_for_a_real_one(self):
        project = self.made("mock")
        quote = self.checks_quote(project, "byok")
        self.assertFalse(quote["available"])
        self.assertEqual(quote["quantity"], 0)

    def test_another_workspace_can_neither_quote_nor_start_nor_see_the_checks(self):
        project = self.made("mock")
        quote = self.checks_quote(project)
        self.start_checks(project, quote_hash=quote["quote_hash"])
        owner = self.context
        self.context = self._make_context()
        self.checks_quote(project, expect=404)
        refused = self.start_checks(project, quote_hash=quote["quote_hash"], expect=404)
        self.assertEqual(refused["code"], "marketing_project_not_found")
        self.assertEqual(self.client.get(f"/api/v1/marketing/ideas/{project['id']}").status_code, 404)
        with tenant_session(self.context) as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(MarketingAIQCRecord).where(
                MarketingAIQCRecord.workspace_id == self.context.workspace_id)), 0)
        self.context = owner
        self.assertEqual(len(self.check_calls(project["id"])), 6)

    def test_a_round_never_sees_an_ideas_check(self):
        project = self.made("mock")
        self.start_checks(project)
        with tenant_session(self.context) as session:
            rounds_see = session.scalar(select(func.count()).select_from(MarketingAIQCRecord).where(
                MarketingAIQCRecord.workspace_id == self.context.workspace_id, MarketingAIQCRecord.round_id.is_not(None)))
        self.assertEqual(rounds_see, 0)


class IdeasChecksPaidTests(IdeasChecksFixtures):
    def test_copy_rendered_differently_from_the_ad_is_a_finding(self):
        project = self.made("byok")
        self.start_checks(project, "byok")

        async def misspelled(provider, model, credential, inputs, images):
            self.assertEqual(inputs["policy_version"], ai_qc.IDEAS_POLICY)
            headline = inputs["combination"]["headline"]
            return report(inputs, rendered_text={"headline": headline.replace("product", "prodcut")},
                          copy_consistent={"status": "pass"})
        self.run_checks(project, AsyncMock(side_effect=misspelled))
        for ad in self.read_ideas(project)["ads"]:
            finding = next(f for f in ad["check"]["findings"] if f["check_id"] == "copy_consistent")
            self.assertEqual((finding["status"], finding["gate_reason"]), ("fail", "rendered_text_mismatch"))
            self.assertEqual(finding["rendered_text_diff"][0]["field"], "headline")
            self.assertIn("prodcut", finding["suggested_fix"])
            self.assertEqual(ad["check"]["suggestion"], "needs_changes")

    def test_a_claim_the_cited_facts_do_not_support_is_a_finding(self):
        project = self.made("byok")
        self.start_checks(project, "byok")

        async def certified(provider, model, credential, inputs, images):
            return report(inputs, facts_supported={
                "visual_evidence": "Visible UL 2252 certification and 500 A / 1000 V ratings beside the charger."})
        self.run_checks(project, AsyncMock(side_effect=certified))
        for ad in self.read_ideas(project)["ads"]:
            finding = next(f for f in ad["check"]["findings"] if f["check_id"] == "facts_supported")
            self.assertEqual((finding["status"], finding["gate_reason"]), ("unknown", "claim_evidence_missing"))
            self.assertTrue(finding["unsupported_claims"])

    def test_passes_are_not_shown_and_settlement_is_per_image(self):
        project = self.made("byok")
        quote = self.checks_quote(project, "byok")
        self.assertEqual((quote["available"], quote["quantity"], quote["maximum_cost"]), (True, 6, "0.30"))
        self.start_checks(project, "byok", quote_hash=quote["quote_hash"])
        provider = AsyncMock(side_effect=lambda provider, model, credential, inputs, images: report(inputs))
        ids = self.run_checks(project, provider)
        self.assertEqual(provider.await_count, 6)
        for ad in self.read_ideas(project)["ads"]:
            check = ad["check"]
            self.assertEqual((check["state"], check["source"]), ("succeeded", "ai_visual_suggestion"))
            # No real product photo was sent, so product identity is left for a person; every pass is left out.
            self.assertEqual([f["check_id"] for f in check["findings"]], ["product_fidelity"])
        with tenant_session(self.context) as session:
            for call_id in ids:
                call = session.get(MarketingAIQCRecord, call_id)
                reservation = session.scalar(select(SpendReservationRecord).where(SpendReservationRecord.job_id == call.job_id))
                self.assertEqual((reservation.state, str(reservation.maximum_budget_amount.normalize())), ("settled", "0.05"))
                attempt = session.scalar(select(ProviderCallAttemptRecord).where(ProviderCallAttemptRecord.job_id == call.job_id))
                self.assertEqual(attempt.dispatch_state, "succeeded")
                self.assertEqual(session.get(OperationRecord, call.operation_id).state, "succeeded")
            events = session.scalars(select(AuditEventRecord.event_type).where(
                AuditEventRecord.workspace_id == self.context.workspace_id,
                AuditEventRecord.resource_id == project["id"])).all()
        self.assertIn("marketing.ideas_checks_requested", events)
        self.assertEqual(events.count("marketing.ideas_check_completed"), 6)

    def test_without_a_key_nothing_is_admitted(self):
        project = self.made("byok")
        with self.no_key():
            quote = self.checks_quote(project, "byok")
            self.assertEqual((quote["available"], quote["unavailable_reason"]), (False, "credential_required"))
            with tenant_session(self.context) as session:
                reservations = session.scalar(select(func.count()).select_from(SpendReservationRecord).where(
                    SpendReservationRecord.workspace_id == self.context.workspace_id))
            refused = self.start_checks(project, "byok", quote_hash=quote["quote_hash"], expect=409)
            self.assertEqual((refused["code"], refused["params"]["reason"]), ("checks_unavailable", "credential_required"))
        self.assertEqual(self.check_calls(project["id"]), [])
        with tenant_session(self.context) as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(SpendReservationRecord).where(
                SpendReservationRecord.workspace_id == self.context.workspace_id)), reservations)

    def test_an_uncertain_check_is_never_sent_again(self):
        project = self.made("byok")
        self.start_checks(project, "byok")
        provider = AsyncMock(side_effect=TimeoutError("provider response missing"))
        ids = self.run_checks(project, provider)
        for call_id in ids:  # the Job runs again after a crash: the call is not resent
            asyncio.run(execute_ai_qc(self.context, call_id, self.blobs, complete=provider))
        self.assertEqual(provider.await_count, 6)
        self.assertTrue(all(c.state == "uncertain" for c in self.check_calls(project["id"])))
        self.assertTrue(all(ad["check"]["state"] == "uncertain" for ad in self.read_ideas(project)["ads"]))
        self.assertEqual(self.checks_quote(project, "byok")["unavailable_reason"], "checks_in_progress")

    def test_a_failed_check_can_be_asked_again_under_a_new_quote(self):
        project = self.made("byok")
        self.start_checks(project, "byok")
        self.run_checks(project, AsyncMock(return_value={"summary": "Everything passes", "findings": []}))
        self.assertTrue(all(ad["check"]["state"] == "failed" for ad in self.read_ideas(project)["ads"]))
        quote = self.checks_quote(project, "byok")
        self.assertEqual((quote["available"], quote["quantity"]), (True, 6))
        self.start_checks(project, "byok", quote_hash=quote["quote_hash"])
        retried = self.check_calls(project["id"], state="queued")
        self.assertEqual(len(retried), 6)
        self.assertTrue(all(c.retry_of_id and c.sequence == 2 for c in retried))

    def test_an_ideas_check_cited_outside_the_ads_facts_is_refused(self):
        project = self.made("byok")
        self.start_checks(project, "byok")

        async def stray(provider, model, credential, inputs, images):
            return report(inputs, facts_supported={"fact_ids": ["f_not_cited"]})
        self.run_checks(project, AsyncMock(side_effect=stray))
        for call in self.check_calls(project["id"]):
            self.assertEqual((call.state, call.error_code), ("failed", "qc_output_invalid"))
            self.assertEqual(call.error_detail["reason"], "qc_fact_reference_invalid")


class FirstBatchCheckTests(IdeasChecksFixtures):
    """The first batch checks the images it made, under the total the user confirmed (maintainer decision on #197)."""

    def until_checking(self, mode="mock"):
        """A first batch whose images are made and whose checks were just admitted, none executed yet."""
        batch = self.start(mode)
        self.drain(job_types=("products.intake", "marketing.plan", "marketing.first_batch"))
        self.drain(job_types=IMAGE_JOBS)
        self.drain(job_types=("marketing.first_batch",))
        return self.batch(batch["id"])

    def test_the_images_are_checked_before_the_batch_is_done_and_shown_meanwhile(self):
        checking = self.until_checking()
        self.assertEqual((checking["state"], checking["needs_attention"]), ("checking", False))
        # The images are on the ideas page while they are being checked.
        ads = self.read_ideas({"id": checking["project_id"]})["ads"]
        self.assertEqual(len(ads), 6)
        self.assertTrue(all(ad["check"]["state"] == "queued" for ad in ads))
        steps = [step["step"] for step in self.record(checking["id"]).progress_json["steps"]]
        self.assertEqual(steps[-1], "checks")
        self.drain()
        done = self.batch(checking["id"])
        self.assertEqual(done["state"], "done", done["attention"])
        ads = self.read_ideas({"id": done["project_id"]})["ads"]
        self.assertTrue(all((ad["check"]["state"], ad["check"]["source"]) == ("succeeded", "controlled_mock") for ad in ads))
        self.assertEqual(len(self.check_calls(done["project_id"])), 6)

    def test_the_same_advance_twice_admits_no_second_check(self):
        checking = self.until_checking()
        steps = self.record(checking["id"]).progress_json["steps"]
        for _ in range(3):
            self.advance_again(checking["id"])
        self.assertEqual(len(self.check_calls(checking["project_id"])), 6)
        self.assertEqual(self.record(checking["id"]).progress_json["steps"], steps)

    def test_the_checks_are_reserved_within_the_confirmed_total(self):
        self.byok()
        quoted = self.quote("byok")
        checks = next(step for step in quoted["steps"] if step["step"] == "checks")
        self.assertEqual((checks["quantity"], checks["maximum_cost"]), (6, "0.3"))
        done = self.started_and_drained("byok")
        self.assertEqual(done["state"], "done", done["attention"])
        self.assertEqual(self.qc_complete.await_count, 6)
        self.assertEqual(Decimal(done["reserved_total"]), self.reserved(done))
        self.assertEqual(Decimal(done["confirmed_max_cost"]) - Decimal(done["reserved_total"]), Decimal("0.2"),
                         "only the two unused extra draftings (understanding and ideas, #267) are left")
        with tenant_session(self.context) as session:
            for call in self.check_calls(done["project_id"]):
                reservation = session.scalar(select(SpendReservationRecord).where(SpendReservationRecord.job_id == call.job_id))
                self.assertEqual(reservation.state, "settled")

    def test_a_failed_check_stops_and_continuing_checks_only_that_image(self):
        self.byok()
        answers = iter([{"summary": "Everything passes", "findings": []}])

        def first_invalid(provider, model, credential, inputs, images):
            return next(answers, None) or report(inputs)
        self.qc_complete = AsyncMock(side_effect=first_invalid)
        stopped = self.started_and_drained("byok")
        self.assertEqual((stopped["state"], stopped["attention"]["reason"], stopped["attention"]["step"]),
                         ("needs_attention", "step_failed", "checks"))
        self.assertEqual((stopped["attention"]["code"], len(stopped["attention"]["combination_ids"])),
                         ("qc_output_invalid", 1))
        # The images stay on the ideas page; only the one check is missing.
        ads = self.read_ideas({"id": stopped["project_id"]})["ads"]
        self.assertEqual(sorted(ad["check"]["state"] for ad in ads), ["failed"] + ["succeeded"] * 5)
        self.assertEqual(stopped["resume"]["requires"], [])
        self.assertLessEqual(Decimal(stopped["resume"]["maximum_cost"]),
                             max(Decimal(stopped["reserved_total"]) + Decimal("0.05"), Decimal(stopped["confirmed_max_cost"])))
        self.resume(stopped)
        self.drain()
        done = self.batch(stopped["id"])
        self.assertEqual(done["state"], "done", done["attention"])
        self.assertEqual(self.qc_complete.await_count, 7)
        self.assertEqual(len(self.check_calls(done["project_id"])), 7)

    def test_an_uncertain_check_is_never_resent_and_waits_for_reconciliation(self):
        from app.marketing.recovery import execute_resolution
        self.byok()
        calls = []

        async def one_lost(provider, model, credential, inputs, images):
            calls.append(inputs["asset_version_id"])
            if len(calls) == 1:
                raise TimeoutError("provider response missing")
            return report(inputs)
        self.qc_complete = AsyncMock(side_effect=one_lost)
        stopped = self.started_and_drained("byok")
        self.assertEqual((stopped["state"], stopped["attention"]["reason"], stopped["attention"]["step"]),
                         ("needs_attention", "uncertain", "checks"))
        self.assertIsNone(stopped["resume"], "an uncertain check waits for its reconciliation")
        self.drain()
        self.assertEqual(len(calls), 6, "the lost check is not sent again")
        lost = next(c for c in self.check_calls(stopped["project_id"]) if c.state == "uncertain")
        self.context = self.owner_ctx(self.context)
        response = self.client.post(f"/api/v1/marketing/calls/{lost.id}/resolve", headers=self.headers(), json={
            "kind": "ai_qc", "maximum_cost": "0.05", "acknowledge_estimated_cost": True,
            "note": "Reviewed the unknown provider outcome; keep the maximum estimate."})
        self.assertEqual(response.status_code, 202, response.text)
        execute_resolution(self.context, response.json()["operation_id"])
        reconciled = self.batch(stopped["id"])
        self.assertEqual(reconciled["attention"]["reason"], "uncertain")
        self.assertIsNotNone(reconciled["resume"], "once reconciled, the user may check that image again")

    def test_a_check_past_the_confirmed_total_stops_before_any_check(self):
        self.byok()
        batch = self.start("byok")
        self.drain(job_types=("products.intake", "marketing.plan", "marketing.first_batch"))
        self.drain(job_types=IMAGE_JOBS)
        # The check price went up after the user confirmed the start button's maximum (from 0.05 to 0.10 an image).
        with patch.dict("os.environ", {"MARKETAI_MARKETING_QC_MAX_COST_USD": "0.10"}):
            self.drain()
            stopped = self.batch(batch["id"])
            self.assertEqual((stopped["state"], stopped["attention"]["reason"], stopped["attention"]["step"]),
                             ("needs_attention", "over_budget", "checks"))
            self.assertGreater(Decimal(stopped["attention"]["needed"]), Decimal(stopped["confirmed_max_cost"]))
            self.assertEqual(self.check_calls(stopped["project_id"]), [])
            self.assertEqual(len(self.read_ideas({"id": stopped["project_id"]})["ads"]), 6)
            offer = stopped["resume"]
            self.assertEqual(Decimal(offer["maximum_cost"]), Decimal(stopped["reserved_total"]) + Decimal("0.6"))
            self.resume(stopped)
            self.drain()
        done = self.batch(batch["id"])
        self.assertEqual(done["state"], "done", done["attention"])
        self.assertEqual(len(self.check_calls(done["project_id"])), 6)
        self.assertLessEqual(Decimal(done["reserved_total"]), Decimal(done["confirmed_max_cost"]))
