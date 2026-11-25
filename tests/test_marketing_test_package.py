"""After the "look together" checks (ADR 0019, #250): the round's own review of each kept ad, its ad package, or
cancelling the test before the package.

Once every check of a started test (#249) has its result, one idempotent command records the round's review of
each kept ad -- every passed check pointing at the evidence it stands on (plan E5) -- and requests the ad package
as any round does. Nothing is written on the Workspace's shared review chain. A check that did not pass waits
for the user to accept it with a reason; a check with nothing behind it refuses the package. Before the package
the test can be cancelled: its round ends as stopped by the user and the kept ads can be taken to a new test.

The ads are the ones a Controlled Mock (or fake BYOK) first batch made for Acme Charge Home
(``tests.test_marketing_ideas_export``). The "look together" checks are answered as the fake model would, or by
the Controlled Mock Worker itself. No paid call is made.
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import select

from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import QualityReviewRecord, WorkspaceRecord
from app.generation.review import create_review
from app.marketing import ad_tests, ai_qc, workflow
from app.marketing.ad_tests_contracts import MarketingTestDetail, MarketingTestPackage
from app.marketing.ai_qc_models import MarketingAIQCRecord
from app.marketing.workflow_contracts import ResourceEnvelope
from app.marketing.workflow_models import MarketingHandoffRecord, MarketingQCRecord, MarketingRoundRecord
from tests.test_ads_first_drafts_stay_out import CHECKED, RoundGateFixtures
from tests.test_marketing_test_start import TestStartFixtures

NOTE = "Placeholder image in the Controlled Mock example; the comparison reads as planned."


class PackageCommands:
    """The commands and reads of these tests, for any fixtures with the ads-first ideas and the API client."""

    def take_to_test(self, project, *combination_ids):
        """Keep ``combination_ids`` of ``project`` and start a test of them with the quote the page shows."""
        ads = {ad["combination_id"]: ad for ad in self.read_ideas(project)["ads"]}
        for combination_id in combination_ids:
            response = self.client.put(f"/api/v1/marketing/ideas/{project['id']}/ads/{ads[combination_id]['asset_version_id']}/kept",
                                       headers=self.headers(), json={"kept": True})
            self.assertEqual(response.status_code, 200, response.text)
        answers = {"target_cost": "10", "round_budget": "400", "duration_days": 7}
        quote = self.client.get(f"/api/v1/marketing/ideas/{project['id']}/test-quote", params=answers).json()
        response = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/tests",
                                    headers=self.headers(self.read_ideas(project)["revision"]),
                                    json={"answers": answers, "quote_hash": quote["quote_hash"],
                                          "asset_version_ids": sorted(i for s in quote["subjects"] for i in s["asset_version_ids"])})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def paired_check_calls(self, round_id):
        with tenant_session(self.context) as session:
            rows = session.scalars(select(MarketingAIQCRecord).where(
                MarketingAIQCRecord.workspace_id == self.context.workspace_id, MarketingAIQCRecord.round_id == round_id)
                .order_by(MarketingAIQCRecord.asset_version_id)).all()
            for row in rows:
                session.expunge(row)
            return {row.asset_version_id: row for row in rows}

    def answer_checks(self, test, changes=None, states=None):
        """Each "look together" check answers as the fake model would: every finding passes, except ``changes``
        (``{(asset_version_id, check_id): status}``); ``states`` leaves a call in another state instead."""
        changes, states = changes or {}, states or {}
        with tenant_session(self.context) as session:
            for call in session.scalars(select(MarketingAIQCRecord).where(
                    MarketingAIQCRecord.workspace_id == self.context.workspace_id,
                    MarketingAIQCRecord.round_id == test["current_round_id"])).all():
                if call.asset_version_id in states:
                    call.state = states[call.asset_version_id]
                    if call.state == "failed":
                        call.error_code = "qc_output_invalid"
                    continue
                answer = {"summary": "The ads read as planned.", "findings": [
                    {"check_id": key, "status": changes.get((call.asset_version_id, key), "pass"), "confidence": "high",
                     "visual_evidence": "The Acme Charge charger and its headline are clearly visible.",
                     "rationale": "As the plan says.", "suggested_fix": "", "fact_ids": []}
                    for key in call.input_json["required_checks"]]}
                call.state = "succeeded"
                call.result_json = {**ai_qc.normalize_report(answer, call.input_json), "source": "controlled_mock"}

    def round_revision(self, round_id):
        with tenant_session(self.context) as session:
            return session.get(MarketingRoundRecord, round_id).revision

    def package_test(self, test, accepted=(), expect=200, key=None, revision=None):
        round_id = test["rounds"][-1]["id"]
        response = self.client.post(f"/api/v1/marketing/tests/{test['id']}/package",
                                    headers=self.headers(revision or self.round_revision(round_id), key),
                                    json={"accepted_findings": list(accepted)})
        self.assertEqual(response.status_code, expect, response.text)
        if expect == 200:
            MarketingTestPackage.model_validate(response.json())
            self.assertEqual(response.headers["etag"], f'"{self.round_revision(round_id)}"')
            self.assertNotIn("location", response.headers)
        return response.json()

    def cancel_test(self, test, expect=200, key=None, revision=None, note=None):
        round_id = test["rounds"][-1]["id"]
        response = self.client.post(f"/api/v1/marketing/tests/{test['id']}/cancel",
                                    headers=self.headers(revision or self.round_revision(round_id), key),
                                    json={"note": note} if note else {})
        self.assertEqual(response.status_code, expect, response.text)
        if expect == 200:
            MarketingTestDetail.model_validate(response.json())
            self.assertEqual(response.headers["etag"], '"1"')
        return response.json()

    def review_records(self, round_id):
        with tenant_session(self.context) as session:
            rows = session.scalars(select(MarketingQCRecord).where(
                MarketingQCRecord.workspace_id == self.context.workspace_id, MarketingQCRecord.round_id == round_id)).all()
            for row in rows:
                session.expunge(row)
            return {row.asset_version_id: row for row in rows}

    def handoff_ids(self, round_id):
        with tenant_session(self.context) as session:
            return session.scalars(select(MarketingHandoffRecord.id).where(
                MarketingHandoffRecord.workspace_id == self.context.workspace_id,
                MarketingHandoffRecord.round_id == round_id)).all()

    def shared_reviews(self, asset_version_ids):
        with tenant_session(self.context) as session:
            return sorted(session.scalars(select(QualityReviewRecord.id).where(
                QualityReviewRecord.workspace_id == self.context.workspace_id,
                QualityReviewRecord.asset_version_id.in_(list(asset_version_ids)))).all())

    def round_read(self, round_id, query=""):
        """The round as ``get_marketing_round`` returns it: still valid for its strict readers."""
        response = self.client.get(f"/api/v1/marketing/rounds/{round_id}{query}")
        self.assertEqual(response.status_code, 200, response.text)
        ResourceEnvelope.model_validate(response.json())
        return response.json()["data"]

    def nothing_written(self, test):
        round_id = test["rounds"][-1]["id"]
        self.assertEqual(self.review_records(round_id), {})
        self.assertEqual(self.handoff_ids(round_id), [])
        with tenant_session(self.context) as session:
            self.assertEqual(session.get(MarketingRoundRecord, round_id).state, "reviewing")

    def kept_ids(self, test):
        return sorted(self.paired_check_calls(test["current_round_id"]))


class PackageFixtures(PackageCommands, TestStartFixtures):
    def started(self, *combination_ids, mode="mock"):
        """A started test of Acme Charge Home's kept ``combination_ids`` (a claims test by default)."""
        project = self.exportable(mode)
        return project, self.take_to_test(project, *(combination_ids or ("c1_1", "c2_1", "c2_2")))


class PackageTests(PackageFixtures):
    def test_all_checks_passed_gives_one_review_per_ad_with_evidence_for_every_check_and_the_package(self):
        project, test = self.started()
        kept = self.kept_ids(test)
        round_id = test["current_round_id"]
        self.answer_checks(test)
        package = self.package_test(test)

        self.assertEqual((package["test_id"], package["round_id"], package["handoff_state"]), (test["id"], round_id, "queued"))
        self.assertEqual([r["asset_version_id"] for r in package["reviews"]], kept)
        reviews, calls = self.review_records(round_id), self.paired_check_calls(round_id)
        self.assertEqual(sorted(reviews), kept)
        with tenant_session(self.context) as session:
            row = session.get(MarketingRoundRecord, round_id)
            combos = {v.id: c for v, _, c in workflow.creative_assets(session, self.context, row)}
            self.assertEqual(row.state, "handoff")
            [handoff] = session.scalars(select(MarketingHandoffRecord).where(
                MarketingHandoffRecord.workspace_id == self.context.workspace_id, MarketingHandoffRecord.round_id == round_id)).all()
            self.assertEqual(sorted(e["asset_version_id"] for e in handoff.manifest_json["entries"]), kept)
            self.assertTrue(all(workflow.ready(session, self.context, row, ad) for ad in kept))
            plan = row.snapshot_json["plan"]
        for ad, record in reviews.items():
            findings = record.findings_json
            asked = workflow.required_checks(combos[ad], plan)
            # The round's own record only: nothing on the shared review chain.
            self.assertIsNone(record.quality_review_id)
            self.assertEqual((findings["quality"], findings["source"], findings["paired_ai_qc_id"]),
                             ("approved", workflow.TEST_REVIEW, calls[ad].id))
            self.assertEqual(findings["checks"], dict.fromkeys(asked, "pass"))
            self.assertEqual(set(findings["evidence"]), set(asked))
            for check, evidence in findings["evidence"].items():
                self.assertTrue(evidence, check)
                if check in workflow.PAIRED_CHECKS:
                    self.assertEqual(evidence, [{"source": "paired_check", "id": calls[ad].id, "finding_id": check}])
                else:
                    # The Controlled Mock ideas check judged nothing; the user accepted its findings before download.
                    self.assertEqual((evidence[0]["source"], evidence[0]["finding_id"]), ("verification", check))
                    self.assertTrue(evidence[0]["id"].startswith("mie"))
            self.assertTrue(any("fact_id" in e for e in findings["evidence"]["facts_supported"]))
            self.assertEqual(findings["accepted_findings"], [])
            self.assertEqual([r for r in package["reviews"] if r["asset_version_id"] == ad][0]["evidence"], findings["evidence"])
        self.assertEqual(self.shared_reviews(kept), [])
        # Each step writes its own event, as when they were separate.
        self.assertEqual(self.events(round_id).count("marketing.creative_reviewed"), len(kept))
        self.assertIn("marketing.handoff_requested", self.events(round_id))
        self.assertIn("marketing.test_package_requested", self.events(test["id"]))
        # The round still reads as before for its strict readers; the ads show as ready, the package as queued.
        for query in ("", "?include_qc_kinds=true&include_reuse_lineage=true&include_board=true&include_image_edits=true"
                          "&include_languages=true&include_execution_plan=true&include_qc_evidence=true"):
            data = self.round_read(round_id, query)
            self.assertEqual({a["asset_version_id"]: a["ready"] for a in data["assets"]}, dict.fromkeys(kept, True))
            self.assertEqual({a["quality"]["source"] for a in data["assets"]}, {"ai_assisted_human_review"})
            self.assertEqual({e["provenance"]["kind"] for e in data["handoff"]["entries"]}, {"existing_creative"})
        lineage = self.round_read(round_id, "?include_reuse_lineage=true")
        self.assertEqual({e["provenance"]["source_project_id"] for e in lineage["handoff"]["entries"]}, {project["id"]})
        # The Worker builds the package as for any round, and its link is handed out.
        self.drain(job_types=("marketing.export",))
        self.assertEqual(self.round_read(round_id)["handoff"]["state"], "ready")
        self.assertEqual(self.package_test(test)["handoff_state"], "ready")
        link = self.client.post(f"/api/v1/marketing/rounds/{round_id}/download-intent", headers=self.headers())
        self.assertEqual(link.status_code, 201, link.text)

    def test_a_passed_check_of_the_ad_itself_is_its_evidence(self):
        _, test = self.started("c1_1", "c2_1", mode="byok")
        self.answer_checks(test)
        package = self.package_test(test)
        for review in package["reviews"]:
            # The fake model passes the claim; the look of an ad made without a photo the user confirmed instead.
            claim = review["evidence"]["proposition_clear"]
            self.assertEqual([(e["source"], e["finding_id"]) for e in claim], [("ideas_check", "proposition_clear")])
            self.assertTrue(claim[0]["id"].startswith("maqc"))

    def test_the_mock_checks_findings_are_listed_until_the_user_accepts_them(self):
        """The Controlled Mock Worker never reports a placeholder image as good: every comparison finding needs
        the user's word, which the round's review records with the reason."""
        _, test = self.started()
        round_id = test["current_round_id"]
        self.drain(job_types=("marketing.ai_qc",))
        calls = self.paired_check_calls(round_id)
        self.assertEqual({c.state for c in calls.values()}, {"succeeded"})
        refused = self.package_test(test, expect=409)
        self.assertEqual(refused["code"], "check_findings_open")
        listed = refused["params"]["findings"]
        self.assertEqual([(f["asset_version_id"], f["check_id"]) for f in listed], [(ad, calls[ad].id) for ad in sorted(calls)])
        self.nothing_written(test)
        package = self.package_test(test, [{**item, "note": NOTE} for item in listed])
        for review in package["reviews"]:
            item = next(f for f in listed if f["asset_version_id"] == review["asset_version_id"])
            self.assertEqual(sorted((a["finding_id"], a["status"], a["note"]) for a in review["accepted_findings"]),
                             [(finding, "unknown", NOTE) for finding in item["finding_ids"]])
        record = self.review_records(round_id)[listed[0]["asset_version_id"]].findings_json
        self.assertEqual((record["override_note"], set(record["overrides"])), (NOTE, set(listed[0]["finding_ids"])))


class OpenFindingTests(PackageFixtures):
    def test_a_check_that_did_not_pass_is_refused_until_the_user_accepts_it(self):
        _, test = self.started()
        ad = self.kept_ids(test)[0]
        self.answer_checks(test, {(ad, "comparison_faithful"): "fail"})
        call = self.paired_check_calls(test["current_round_id"])[ad]
        refused = self.package_test(test, expect=409)
        self.assertEqual((refused["code"], refused["params"]["findings"]),
                         ("check_findings_open", [{"asset_version_id": ad, "check_id": call.id, "finding_ids": ["comparison_faithful"]}]))
        self.nothing_written(test)
        # Accepting it needs the check the user looked at, and a finding that check has.
        stale = self.package_test(test, [{"asset_version_id": ad, "check_id": "maqc_older", "finding_ids": ["comparison_faithful"],
                                      "note": NOTE}], expect=409)
        self.assertEqual(stale["code"], "check_changed")
        unknown = self.package_test(test, [{"asset_version_id": ad, "check_id": call.id, "finding_ids": ["proposition_clear"],
                                        "note": NOTE}], expect=422)
        self.assertEqual((unknown["code"], unknown["params"]["finding_ids"]), ("finding_not_found", ["proposition_clear"]))
        no_reason = self.client.post(f"/api/v1/marketing/tests/{test['id']}/package",
                                     headers=self.headers(self.round_revision(test["current_round_id"])),
                                     json={"accepted_findings": [{"asset_version_id": ad, "check_id": call.id,
                                                                  "finding_ids": ["comparison_faithful"], "note": ""}]})
        self.assertEqual(no_reason.status_code, 422, no_reason.text)
        self.nothing_written(test)

        package = self.package_test(test, [{"asset_version_id": ad, "check_id": call.id, "finding_ids": ["comparison_faithful"],
                                        "note": NOTE}])
        review = next(r for r in package["reviews"] if r["asset_version_id"] == ad)
        self.assertEqual(review["accepted_findings"], [{"check_id": call.id, "finding_id": "comparison_faithful",
                                                         "status": "fail", "note": NOTE}])
        self.assertEqual(review["evidence"]["comparison_faithful"],
                         [{"source": "paired_check", "id": call.id, "finding_id": "comparison_faithful"}])
        self.assertEqual(self.review_records(test["current_round_id"])[ad].findings_json["overrides"],
                         {"comparison_faithful": {"ai": "fail", "human": "pass"}})

    def test_a_check_with_nothing_behind_it_is_refused_and_no_package_is_made(self):
        _, test = self.started()
        ad = self.kept_ids(test)[1]
        self.answer_checks(test, states={ad: "failed"})
        refused = self.package_test(test, expect=409)
        self.assertEqual(refused["code"], "check_evidence_missing")
        [missing] = refused["params"]["ads"]
        with tenant_session(self.context) as session:
            row = session.get(MarketingRoundRecord, test["current_round_id"])
            combo = next(c for v, _, c in workflow.creative_assets(session, self.context, row) if v.id == ad)
            paired = workflow.kind_checks(workflow.required_checks(combo, row.snapshot_json["plan"]), "paired")
        self.assertEqual((missing["asset_version_id"], missing["check_ids"], missing["reason"]), (ad, sorted(paired), "check_failed"))
        self.nothing_written(test)
        # Nothing to accept there: the finding is not in any check.
        call = self.paired_check_calls(test["current_round_id"])[ad]
        refused = self.package_test(test, [{"asset_version_id": ad, "check_id": call.id, "finding_ids": sorted(paired)[:1],
                                        "note": NOTE}], expect=409)
        self.assertEqual(refused["code"], "check_changed")
        self.nothing_written(test)

    def test_a_check_still_running_or_uncertain_is_waited_for_and_never_sent_again(self):
        """Review Focus 4: the page closed while the checks ran; opened again, it asks again."""
        _, test = self.started()
        kept = self.kept_ids(test)
        refused = self.package_test(test, expect=409)
        self.assertEqual((refused["code"], refused["params"]), ("checks_running", {"running": kept, "uncertain": []}))
        self.answer_checks(test, states={kept[0]: "uncertain"})
        refused = self.package_test(test, expect=409)
        self.assertEqual((refused["code"], refused["params"]), ("checks_running", {"running": [], "uncertain": [kept[0]]}))
        self.assertEqual(len(self.paired_check_calls(test["current_round_id"])), len(kept))
        self.nothing_written(test)

    def test_an_ad_rejected_meanwhile_on_the_shared_chain_is_refused(self):
        _, test = self.started()
        ad = self.kept_ids(test)[0]
        self.answer_checks(test)
        with tenant_session(self.context) as session:
            create_review(session, self.context, ad, "rejected", "The charger's port is drawn wrong.", self.context.initiator_id)
        refused = self.package_test(test, expect=409)
        self.assertEqual((refused["code"], refused["params"]["asset_version_ids"]), ("ad_review_rejected", [ad]))
        self.nothing_written(test)

    def test_an_ad_no_longer_exportable_is_refused(self):
        project, test = self.started()
        self.answer_checks(test)
        self.edit_facts(project, lambda u: [fact.update(statement=fact["statement"] + " As of this year.") for fact in u["facts"]])
        refused = self.package_test(test, expect=409)
        self.assertEqual(refused["code"], "ads_not_exportable")
        self.nothing_written(test)


class ReplayTests(PackageFixtures):
    def test_the_same_command_again_returns_the_same_package(self):
        """Review Focus 4: the page closed after the package was asked for; opened again, it asks again."""
        _, test = self.started()
        round_id = test["current_round_id"]
        self.answer_checks(test)
        revision = self.round_revision(round_id)
        first = self.package_test(test, key="key-package", revision=revision)
        again = self.package_test(test, key="key-package", revision=revision)
        reopened = self.package_test(test, revision=1)  # a new key and the revision the page read before
        anything = self.package_test(test, [{"asset_version_id": "av_none", "check_id": "maqc_none", "finding_ids": ["x"],
                                         "note": NOTE}])
        self.assertEqual(first, again)
        self.assertEqual(first, reopened)
        self.assertEqual(first, anything)
        self.assertEqual(len(self.handoff_ids(round_id)), 1)
        self.assertEqual(len(self.review_records(round_id)), len(self.kept_ids(test)))

    def test_the_round_revision_must_match(self):
        _, test = self.started()
        self.answer_checks(test)
        stale = self.package_test(test, expect=409, revision=self.round_revision(test["current_round_id"]) + 1)
        self.assertEqual(stale["code"], "stale_round_revision")
        missing = self.client.post(f"/api/v1/marketing/tests/{test['id']}/package", headers=self.headers(), json={})
        self.assertEqual(missing.status_code, 428, missing.text)
        self.nothing_written(test)


class CancelTests(PackageFixtures):
    def test_a_cancelled_test_leaves_nothing_in_progress_and_the_ads_can_be_tested_again(self):
        project, test = self.started()
        round_id = test["current_round_id"]
        cancelled = self.cancel_test(test, note="Swap the second claim's ad for a newer one.")
        self.assertEqual((cancelled["id"], cancelled["current_round_id"], cancelled["rounds"][0]["state"]),
                         (test["id"], None, "closed"))
        with tenant_session(self.context) as session:
            row = session.get(MarketingRoundRecord, round_id)
            self.assertEqual((row.close_json["reason"], row.close_json["note"]),
                             ("user_stopped", "Swap the second claim's ad for a newer one."))
            self.assertIsNone(ad_tests.in_progress(session, self.context, project["product_id"]))
        self.assertIn("marketing.test_cancelled", self.events(test["id"]))
        self.assertIn("marketing.round_closed", self.events(round_id))
        self.assertEqual(self.round_read(round_id)["close"]["reason"], "user_stopped")
        # A check not yet sent is never sent: the Worker refuses a round no longer in review.
        self.drain(job_types=("marketing.ai_qc",))
        self.assertEqual({(c.state, c.error_code) for c in self.paired_check_calls(round_id).values()}, {("failed", "qc_admission_changed")})
        # Neither completed nor cancelled again.
        self.assertEqual(self.package_test(test, expect=409)["code"], "round_closed")
        self.assertEqual(self.cancel_test(test, expect=409)["code"], "round_closed")
        # The same ads start a new test.
        again = self.start_test(self.read_ideas(project))
        self.assertNotEqual(again["id"], test["id"])
        self.assertEqual(len(self.started_tests(project)), 2)

    def test_without_a_note_the_round_says_it_was_cancelled_before_the_package(self):
        _, test = self.started()
        self.cancel_test(test)
        with tenant_session(self.context) as session:
            self.assertEqual(session.get(MarketingRoundRecord, test["current_round_id"]).close_json["note"],
                             ad_tests.CANCEL_NOTE["en-US"])

    def test_once_the_package_is_made_the_test_is_not_cancelled(self):
        _, test = self.started()
        self.answer_checks(test)
        self.package_test(test)
        refused = self.cancel_test(test, expect=409)
        self.assertEqual(refused["code"], "test_package_exists")
        with tenant_session(self.context) as session:
            self.assertEqual(session.get(MarketingRoundRecord, test["current_round_id"]).state, "handoff")


class PackageAccessTests(PackageFixtures):
    def test_another_workspace_can_neither_complete_nor_cancel(self):
        _, test = self.started()
        self.answer_checks(test)
        revision = self.round_revision(test["current_round_id"])
        other = self._make_context()
        self.use(other)
        for path in ("package", "cancel"):
            response = self.client.post(f"/api/v1/marketing/tests/{test['id']}/{path}", headers=self.headers(revision), json={})
            self.assertEqual((response.status_code, response.json()["code"]), (404, "marketing_test_not_found"), path)
        self.use(self.context)
        self.nothing_written(test)

    def test_a_workspace_without_current_authorization_cannot(self):
        _, test = self.started()
        self.answer_checks(test)
        with platform_session() as session:
            session.get(WorkspaceRecord, self.context.workspace_id).lifecycle = "suspended"
        self.assertEqual(self.package_test(test, expect=403)["code"], "workspace_inactive")
        self.assertEqual(self.cancel_test(test, expect=403)["code"], "workspace_inactive")
        with platform_session() as session:
            session.get(WorkspaceRecord, self.context.workspace_id).lifecycle = "active"
        self.nothing_written(test)

    def test_the_owner_and_a_member_may(self):
        project, test = self.started()
        self.use(self.owner_ctx(self.context))
        self.cancel_test(test)
        self.use(self.context)
        again = self.take_to_test(project)
        self.answer_checks(again)
        self.package_test(again)

    def test_the_round_path_records_no_review_of_a_kept_ad(self):
        """Plan E5: only the test's command reviews a kept ad, so nothing reaches the shared chain."""
        _, test = self.started()
        ad = self.kept_ids(test)[0]
        response = self.client.post(f"/api/v1/marketing/rounds/{test['current_round_id']}/reviews",
                                    headers=self.headers(self.round_revision(test["current_round_id"])),
                                    json={"asset_version_id": ad, "quality": "approved", "checks": {}, "note": NOTE})
        self.assertEqual((response.status_code, response.json()["code"]), (409, "test_review_by_checks"))
        self.assertEqual(self.shared_reviews([ad]), [])


class CheckTableTests(PackageFixtures):
    def test_every_round_check_has_one_source(self):
        self.assertEqual(set(ad_tests.CHECK_SOURCES), set(workflow.CHECKS))
        self.assertEqual({k for k, v in ad_tests.CHECK_SOURCES.items() if v == "paired_check"}, set(workflow.PAIRED_CHECKS))
        # Each check of the image alone is one the ads-first check asks of the same intent.
        self.assertEqual({k for k, v in ad_tests.CHECK_SOURCES.items() if v == "ideas_check"},
                         set(workflow.CHECKS) - set(workflow.PAIRED_CHECKS))


class EarlierTestTests(PackageCommands, RoundGateFixtures):
    """The same checked ad, approved in an earlier (plan-first) test and then taken to a new test."""

    def test_the_earlier_tests_review_and_ready_do_not_change(self):
        ideas, project, row = self.ideas_and_round()
        ad = self.ads(ideas)[CHECKED[0]]["asset_version_id"]
        earlier = self.approved(self.reuse(row, ad))
        earlier_round = earlier["id"]
        self.assertTrue(next(a for a in earlier["assets"] if a["asset_version_id"] == ad)["ready"])
        before = self.shared_reviews([ad])
        with tenant_session(self.context) as session:
            frozen = session.get(MarketingRoundRecord, earlier_round)
            self.assertTrue(workflow.ready(session, self.context, frozen, ad))

        test = self.take_to_test(ideas, CHECKED[0])
        self.answer_checks(test)
        package = self.package_test(test)
        self.assertEqual([r["asset_version_id"] for r in package["reviews"]], [ad])

        self.assertEqual(self.shared_reviews([ad]), before)
        with tenant_session(self.context) as session:
            frozen = session.get(MarketingRoundRecord, earlier_round)
            self.assertTrue(workflow.ready(session, self.context, frozen, ad))
            self.assertEqual(sorted(r.id for r in workflow.active_quality(session, self.context, ad)), before)
        self.assertTrue(next(a for a in self.current(earlier)["assets"] if a["asset_version_id"] == ad)["ready"])


class ReviewMigrationDowngradeTests(unittest.TestCase):
    """A test's review record names no shared review; downgrade cannot make the column required while one exists."""
    ROOT = Path(__file__).resolve().parents[1]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="marketai-test-review-migration-")
        self.addCleanup(self.temp.cleanup)
        self.database = f"{self.temp.name}/migration.db"
        self.environment = {**os.environ, "MARKETAI_DATABASE_URL": f"sqlite:///{self.database}",
                            "MARKETAI_DATA_DIR": f"{self.temp.name}/data"}
        upgraded = self.alembic("upgrade", "head")
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)

    def alembic(self, *arguments):
        return subprocess.run([sys.executable, "-m", "alembic", *arguments], cwd=self.ROOT, env=self.environment,
                              capture_output=True, text=True, check=False)

    def required(self):
        with sqlite3.connect(self.database) as connection:
            return {row[1]: bool(row[3]) for row in connection.execute("PRAGMA table_info(marketing_qc_reviews)")}["quality_review_id"]

    def test_the_column_is_optional_and_downgrade_without_such_records_requires_it_again(self):
        self.assertFalse(self.required())
        downgraded = self.alembic("downgrade", "zzv252a1b2c3")
        self.assertEqual(downgraded.returncode, 0, downgraded.stderr)
        self.assertTrue(self.required())

    def test_downgrade_refuses_while_a_record_names_no_shared_review(self):
        with sqlite3.connect(self.database) as connection:
            connection.execute("INSERT INTO marketing_qc_reviews (id, workspace_id, round_id, asset_version_id, findings_json,"
                               " quality_review_id, actor_id, created_at) VALUES ('mqc_1', 'ws_1', 'mround_1', 'av_1', '{}',"
                               " NULL, 'user_a', '2026-11-24')")
        downgraded = self.alembic("downgrade", "zzv252a1b2c3")
        self.assertNotEqual(downgraded.returncode, 0)
        self.assertIn("without a shared review exist", downgraded.stderr)
        self.assertFalse(self.required())
