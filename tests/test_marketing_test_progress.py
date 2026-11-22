"""A test round's numbers so far, recorded deviations and changed-ad hints (#254, design §5.7, §5.8).

The commands that make a test round's ad package come with #249 and #250. Until then the numbers and
deviations take the round of a confirmed plan-first test -- its package made, every ad matched to a Meta ad --
and turn its project into a test project, as ``tests.test_marketing_test_schedule`` does. The changed-ad hints
take a real checked ad of ads-first ideas (fake page, fake model, fake image provider), reused into a round of
the same product card and packed by the real Worker, then name the ideas on the round's reuse record as the
start command writes it (plan E4). The products are the fictional Acme Charge ones. No paid call is made.
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import func, select

from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import AuditEventRecord, WorkspaceRecord, new_id
from app.marketing import ad_tests, ideas, workflow
from app.marketing.ad_test_progress_contracts import MarketingTestProgress
from app.marketing.models import MarketingIdeasExportRecord, MarketingProjectRecord
from app.marketing.service import digest
from app.marketing.workflow_models import (MarketingAdBindingRecord, MarketingCreativeReuseRecord,
    MarketingEvaluationRecord, MarketingRoundDeviationRecord)
from tests.test_ads_first_drafts_stay_out import RoundGateFixtures
from tests.test_marketing_ideas_copy import PLAIN, CopyCommands
from tests.test_marketing_test_schedule import ScheduleFixtures, days

NOTE = "Raised the daily budget in Meta on the third day"


class ProgressCommands:
    def progress(self, test_id, round_id, status=200):
        response = self.client.get(f"/api/v1/marketing/tests/{test_id}/rounds/{round_id}/progress")
        self.assertEqual(response.status_code, status, response.text)
        if status == 200:
            MarketingTestProgress.model_validate(response.json())
            self.assertEqual(response.headers["etag"], f'"{response.json()["revision"]}"')
        return response.json()

    def minimal_result(self, test_id, round_id):
        response = self.client.get(f"/api/v1/marketing/tests/{test_id}/rounds/{round_id}/result")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def tally(self, model, ctx=None, **filters):
        ctx = ctx or self.context
        with tenant_session(ctx) as session:
            return session.scalar(select(func.count()).select_from(model).where(model.workspace_id == ctx.workspace_id,
                *(getattr(model, key) == value for key, value in filters.items())))


class ProgressFixtures(ProgressCommands, ScheduleFixtures):
    def deviate(self, test_id, round_id, asset_version_ids, status=201, *, note=NOTE, revision=None, key=None):
        revision = revision if revision is not None else self.round_row(round_id)["revision"]
        response = self.client.post(f"/api/v1/marketing/tests/{test_id}/rounds/{round_id}/deviations",
                                    headers=self.headers(revision, key), json={"asset_version_ids": asset_version_ids, "note": note})
        self.assertEqual(response.status_code, status, response.text)
        if status == 201:
            MarketingTestProgress.model_validate(response.json())
            self.assertNotIn("location", response.headers)
        return response.json()

    def latest(self, round_id):
        with tenant_session(self.context) as session:
            evaluation = workflow.latest_evaluation(session, self.context, workflow.get_round(session, self.context, round_id))
            session.expunge(evaluation)
            return evaluation

    def packaged_test(self):
        """A test round, 2025-01-01 to 2025-01-07, with its ad package made and no ad matched to a Meta ad yet."""
        project, row = self.ready_handoff()
        with tenant_session(self.context) as session:
            record = session.get(MarketingProjectRecord, project["id"])
            record.flow, record.source_project_id = ad_tests.FLOW, self.ideas_project()
        return project["id"], row


class NumbersSoFarTests(ProgressFixtures):
    def test_the_numbers_so_far_are_read_without_evaluating(self):
        test_id, row = self.bound_test()
        round_id = row["id"]
        entries = {e["asset_version_id"] for e in row["handoff"]["entries"]}
        groups = [g["id"] for g in row["snapshot"]["plan"]["groups"]]

        # Before any report: every group with its ads, nothing spent, every order still missing.
        empty = self.progress(test_id, round_id)
        self.assertEqual({k: empty[k] for k in ("provisional", "currency", "min_purchases", "effective_start_date",
                                                 "effective_end_date", "result_date", "data_through", "deviations",
                                                 "changed_ads")},
                         {"provisional": True, "currency": "USD", "min_purchases": 20, "effective_start_date": "2025-01-01",
                          "effective_end_date": "2025-01-07", "result_date": "2025-01-16", "data_through": None,
                          "deviations": [], "changed_ads": []})
        self.assertEqual(Decimal(empty["target_cost"]), Decimal(str(row["snapshot"]["plan"]["rules"]["threshold"])))
        self.assertEqual([c["id"] for c in empty["comparisons"]], groups)
        self.assertEqual({v for c in empty["comparisons"] for v in c["asset_version_ids"]}, entries)
        for comparison in empty["comparisons"]:
            self.assertEqual((comparison["spend"], comparison["purchases"], comparison["cost_per_purchase"],
                              comparison["remaining_purchases"], comparison["conditions_changed"]),
                             ("0.00", "0", None, 20, False))

        # Three days in, two orders a day per ad: the numbers so far, still short of the minimum.
        self.upload(round_id, days(date(2025, 1, 1), 3), purchases=2)
        evaluations, events, revision = (self.tally(MarketingEvaluationRecord), self.tally(AuditEventRecord),
                                         self.round_row(round_id)["revision"])
        first = self.progress(test_id, round_id)
        self.assertEqual(self.progress(test_id, round_id), first)
        # Reading them wrote nothing: no evaluation, no event, the same revision.
        self.assertEqual((self.tally(MarketingEvaluationRecord), self.tally(AuditEventRecord),
                          self.round_row(round_id)["revision"]), (evaluations, events, revision))
        self.assertEqual(evaluations, 0)
        self.assertEqual(first["data_through"], "2025-01-03")
        for comparison in first["comparisons"]:
            ads = len(comparison["asset_version_ids"])  # one Meta ad each
            purchases = 2 * 3 * ads
            self.assertEqual((comparison["spend"], comparison["purchases"], comparison["cost_per_purchase"],
                              comparison["remaining_purchases"]),
                             (f"{(10 + 11 + 12) * ads}.00", str(purchases), "5.50", max(0, 20 - purchases)))

        # The full report up to day seven replaces those days: past the minimum, nothing is missing any more.
        self.upload(round_id, days(date(2025, 1, 1), 7), purchases=10)
        later = self.progress(test_id, round_id)
        self.assertEqual(later["data_through"], "2025-01-07")
        self.assertEqual({c["remaining_purchases"] for c in later["comparisons"]}, {0})
        self.assertEqual(self.tally(MarketingEvaluationRecord), 0)

        # "See the result" evaluates once; the numbers so far still add no evaluation.
        self.evaluate(round_id)
        self.assertEqual(self.tally(MarketingEvaluationRecord), 1)
        self.assertEqual(self.progress(test_id, round_id)["comparisons"], later["comparisons"])
        self.assertEqual(self.tally(MarketingEvaluationRecord), 1)

    def test_a_round_before_its_package_shows_its_groups_with_nothing_counted(self):
        test_id, round_id = self.make_test()
        progress = self.progress(test_id, round_id)
        self.assertTrue(progress["comparisons"])
        self.assertEqual({(c["spend"], c["remaining_purchases"]) for c in progress["comparisons"]}, {("0.00", 20)})
        self.assertEqual({v for c in progress["comparisons"] for v in c["asset_version_ids"]}, set())


class DeviationTests(ProgressFixtures):
    def test_a_recorded_deviation_leaves_its_group_not_clear_this_round(self):
        test_id, row = self.bound_test()
        round_id = row["id"]
        self.upload(round_id, days(date(2025, 1, 1), 7), purchases=10)
        self.evaluate(round_id)
        before = self.minimal_result(test_id, round_id)
        self.assertTrue(before["current"])
        self.assertEqual({c["status"] for c in before["comparisons"]} - {"meets_goal", "misses_goal"}, set())
        inputs = self.latest(round_id).input_json
        self.assertNotIn("deviation_ids", inputs)  # nothing recorded: the inputs are what they always were

        deviated, *others = before["comparisons"]
        ad = deviated["asset_version_ids"][0]
        revision = self.round_row(round_id)["revision"]
        recorded = self.deviate(test_id, round_id, [ad])
        (deviation,) = recorded["deviations"]
        self.assertEqual({k: deviation[k] for k in ("asset_version_ids", "note", "comparison_ids", "actor_id")},
                         {"asset_version_ids": [ad], "note": NOTE, "comparison_ids": [deviated["id"]],
                          "actor_id": self.context.initiator_id})
        self.assertEqual({c["id"]: c["conditions_changed"] for c in recorded["comparisons"]},
                         {c["id"]: c["id"] == deviated["id"] for c in before["comparisons"]})
        self.assertEqual(recorded["revision"], revision + 1)
        # The evaluation made before it no longer stands.
        self.assertFalse(self.minimal_result(test_id, round_id)["current"])

        # Evaluated again, the group holding that ad cannot tell yet; the others keep their result.
        self.evaluate(round_id)
        after = self.minimal_result(test_id, round_id)
        self.assertTrue(after["current"])
        self.assertEqual({c["id"]: c["status"] for c in after["comparisons"]},
                         {**{c["id"]: c["status"] for c in others}, deviated["id"]: "not_clear"})
        evaluation = self.latest(round_id)
        self.assertEqual(evaluation.input_json["deviation_ids"], [deviation["id"]])
        with tenant_session(self.context) as session:
            ad_ids = {b.ad_id for b in workflow.scoped(session, self.context, MarketingAdBindingRecord, round_id=round_id)
                      if b.entry_id in {e["entry_id"] for e in row["handoff"]["entries"] if e["asset_version_id"] == ad}}
        # It reads as a deviation noted when that ad's Meta ad was matched; the binding itself is not rewritten.
        self.assertEqual([b["deviations"] for b in evaluation.input_json["bindings"] if b["ad_id"] in ad_ids], [[NOTE]])
        self.assertEqual({b.conditions_json["deviations"] == [] for b in self.bindings(round_id)}, {True})
        group = next(r for r in evaluation.result_json["results"] if r["id"] == "group:" + deviated["id"])
        self.assertIn("delivery_conditions_changed", group["reason_codes"])

        # A second note is appended; the first stays as it was.
        both = self.deviate(test_id, round_id, [ad, others[0]["asset_version_ids"][0]] if others else [ad],
                            note="Moved part of the budget to Advantage+ placements")
        self.assertEqual(both["deviations"][0], deviation)
        self.assertEqual(len(both["deviations"]), 2)
        self.assertEqual(self.tally(MarketingRoundDeviationRecord, round_id=round_id), 2)

    def bindings(self, round_id):
        with tenant_session(self.context) as session:
            found = workflow.scoped(session, self.context, MarketingAdBindingRecord, round_id=round_id)
            for item in found:
                session.expunge(item)
            return found

    def test_a_deviation_recorded_before_its_ad_was_matched_still_counts(self):
        test_id, row = self.packaged_test()
        round_id = row["id"]
        entry = row["handoff"]["entries"][0]
        self.deviate(test_id, round_id, [entry["asset_version_id"]])
        # The Meta ads are matched afterwards (#252 matches a test's ads; here they are written directly).
        with tenant_session(self.context) as session:
            for index, item in enumerate(row["handoff"]["entries"]):
                session.add(MarketingAdBindingRecord(id=new_id("mab"), workspace_id=self.context.workspace_id,
                    round_id=round_id, handoff_id=row["handoff"]["id"], entry_id=item["entry_id"], ad_account_id="123",
                    ad_id=str(1000 + index), start_date=date(2025, 1, 1), end_date=date(2025, 1, 7),
                    conditions_json={"conditions_match": True, "deviations": []}))
        self.upload(round_id, days(date(2025, 1, 1), 7), purchases=10)
        self.evaluate(round_id)
        evaluation = self.latest(round_id)
        self.assertEqual([b["deviations"] for b in evaluation.input_json["bindings"] if b["entry_id"] == entry["entry_id"]],
                         [[NOTE]])
        result = {c["id"]: c for c in self.minimal_result(test_id, round_id)["comparisons"]}
        holding = next(c for c in result.values() if entry["asset_version_id"] in c["asset_version_ids"])
        self.assertEqual(holding["status"], "not_clear")

    def test_a_deviation_is_refused_for_what_is_not_in_the_package_and_kept_once_per_request(self):
        test_id, round_id = self.make_test()
        # Nothing ran before the ad package was made.
        self.assertEqual(self.deviate(test_id, round_id, ["av_unknown"], 409)["code"], "handoff_not_ready")

        test_id, row = self.bound_test()
        round_id, ad = row["id"], row["handoff"]["entries"][0]["asset_version_id"]
        self.assertEqual(self.deviate(test_id, round_id, ["av_unknown"], 422)["code"], "ad_not_in_package")
        self.assertEqual(self.deviate(test_id, round_id, [ad, ad], 422)["code"], "duplicate_asset")
        self.deviate(test_id, round_id, [], 422)
        self.deviate(test_id, round_id, [ad], 422, note="")
        response = self.client.post(f"/api/v1/marketing/tests/{test_id}/rounds/{round_id}/deviations",
                                    headers=self.headers(), json={"asset_version_ids": [ad], "note": NOTE})
        self.assertEqual(response.status_code, 428, response.text)
        stale = self.deviate(test_id, round_id, [ad], 409, revision=self.round_row(round_id)["revision"] - 1)
        self.assertEqual(stale["code"], "stale_round_revision")
        self.assertEqual(self.tally(MarketingRoundDeviationRecord), 0)

        # The same request again returns the same record.
        key, revision = new_id("key"), self.round_row(round_id)["revision"]
        first = self.deviate(test_id, round_id, [ad], key=key, revision=revision)
        self.assertEqual(self.deviate(test_id, round_id, [ad], key=key, revision=revision)["deviations"], first["deviations"])
        self.assertEqual(self.tally(MarketingRoundDeviationRecord), 1)
        self.assertEqual(self.tally(AuditEventRecord, event_type="marketing.round_deviation_recorded"), 1)

        # What ran differently may come to light after the round ended.
        self.close(round_id)
        self.assertEqual(len(self.deviate(test_id, round_id, [ad])["deviations"]), 2)


class EarlierTestTests(ProgressFixtures):
    def test_a_round_of_an_earlier_test_has_neither_and_is_evaluated_exactly_as_before(self):
        project, row = self.bound()
        round_id, ad = row["id"], row["handoff"]["entries"][0]["asset_version_id"]
        self.upload(round_id, days(date(2025, 1, 1), 7), purchases=10)
        self.evaluate(round_id)
        first = self.latest(round_id)
        self.assertNotIn("deviation_ids", first.input_json)
        for response in (self.client.get(f"/api/v1/marketing/tests/{project['id']}/rounds/{round_id}/progress"),
                         self.client.post(f"/api/v1/marketing/tests/{project['id']}/rounds/{round_id}/deviations",
                                          headers=self.headers(self.round_row(round_id)["revision"]),
                                          json={"asset_version_ids": [ad], "note": NOTE})):
            self.assertEqual((response.status_code, response.json()["code"]), (404, "marketing_test_not_found"))
        # Even a deviation row written beside it changes nothing: its evaluation keeps its own bindings.
        with tenant_session(self.context) as session:
            session.add(MarketingRoundDeviationRecord(id=new_id("mrd"), workspace_id=self.context.workspace_id,
                round_id=round_id, asset_version_ids=[ad], note=NOTE, actor_id="user_a"))
        self.evaluate(round_id)
        self.assertEqual(self.tally(MarketingEvaluationRecord, round_id=round_id), 1)
        with tenant_session(self.context) as session:
            frozen = workflow.get_round(session, self.context, round_id)
            self.assertTrue(workflow.evaluation_current(session, self.context, frozen, workflow.latest_evaluation(
                session, self.context, frozen)))


class ProgressAccessTests(ProgressFixtures):
    def test_another_workspace_can_neither_read_nor_record(self):
        test_id, row = self.bound_test()
        round_id, ad = row["id"], row["handoff"]["entries"][0]["asset_version_id"]
        revision = self.round_row(round_id)["revision"]
        other = self._make_context()
        self.use(other)
        responses = {
            "progress": self.client.get(f"/api/v1/marketing/tests/{test_id}/rounds/{round_id}/progress"),
            "deviation": self.client.post(f"/api/v1/marketing/tests/{test_id}/rounds/{round_id}/deviations",
                                          headers=self.headers(revision), json={"asset_version_ids": [ad], "note": NOTE}),
        }
        missing = self.client.get(f"/api/v1/marketing/tests/{new_id('mp')}/rounds/{round_id}/progress")
        for name, response in responses.items():
            self.assertEqual((response.status_code, response.json()["code"], response.json()["detail"]),
                             (404, "marketing_test_not_found", missing.json()["detail"]), name)
        self.assertEqual(self.tally(MarketingRoundDeviationRecord, other), 0)
        self.use(self.context)
        self.assertEqual(self.tally(MarketingRoundDeviationRecord), 0)
        self.assertEqual(self.round_row(round_id)["revision"], revision)

    def test_a_round_of_another_test_is_not_found_under_this_one(self):
        test_id, _ = self.make_test()
        other_test, row = self.bound_test()
        round_id = row["id"]
        self.assertEqual(self.progress(test_id, round_id, 404)["code"], "round_not_found")
        self.assertEqual(self.deviate(test_id, round_id, [row["handoff"]["entries"][0]["asset_version_id"]], 404)["code"],
                         "round_not_found")
        self.assertEqual(self.tally(MarketingRoundDeviationRecord), 0)
        self.assertEqual(self.progress(other_test, round_id)["round_id"], round_id)

    def test_members_and_owners_record_and_a_workspace_without_current_authorization_cannot(self):
        test_id, row = self.bound_test()
        round_id, ad = row["id"], row["handoff"]["entries"][0]["asset_version_id"]
        with platform_session() as session:
            session.get(WorkspaceRecord, self.context.workspace_id).lifecycle = "suspended"
        self.assertEqual(self.deviate(test_id, round_id, [ad], 403)["code"], "workspace_inactive")
        with platform_session() as session:
            session.get(WorkspaceRecord, self.context.workspace_id).lifecycle = "active"
        self.assertEqual(self.tally(MarketingRoundDeviationRecord), 0)
        self.deviate(test_id, round_id, [ad])
        owner = self.owner_ctx(self.context)
        self.use(owner)
        recorded = self.deviate(test_id, round_id, [ad], note="Paused the ad for a day")
        self.assertEqual([d["actor_id"] for d in recorded["deviations"]], [self.context.initiator_id, owner.initiator_id])
        self.use(self.context)


class ChangedAdTests(ProgressCommands, CopyCommands, RoundGateFixtures):
    """A checked ad of ads-first ideas, taken to a test whose ad package is made, then changed on the ideas page."""
    PHOTO = "photo-sha-acme-charge-home"

    def taken_to_test(self, *, photo=False):
        ideas_row, project, row = self.ideas_and_round()
        checked = self.ids(ideas_row, ["c3_1"])[0]
        if photo:
            self.with_checked_photo(checked)
        row = self.built(self.handoff(self.approved(self.reuse(row, checked))))
        self.assertEqual(row["handoff"]["state"], "ready")
        # As the start command (#249) leaves it: a test project of the ideas, the kept ad's reuse record naming
        # the ideas and the intent the ad was made on (plan E4).
        with tenant_session(self.context) as session:
            record = session.get(MarketingProjectRecord, project["id"])
            record.flow, record.source_project_id = ad_tests.FLOW, ideas_row["id"]
            recipe = dict(ideas.project_ads(session, self.context, session.get(MarketingProjectRecord, ideas_row["id"])))
            link = session.scalars(select(MarketingCreativeReuseRecord).where(
                MarketingCreativeReuseRecord.workspace_id == self.context.workspace_id,
                MarketingCreativeReuseRecord.round_id == row["id"])).one()
            version = next(v for v in recipe if v.id == checked)
            source = {**link.source_json, "kind": "ideas", "ideas_project_id": ideas_row["id"], "asset_version_id": checked,
                      "intent_hash": digest(ideas.intent_of(recipe[version])),
                      "verification_record_id": self.records(ideas_row, "verification")[-1].id}
            link.source_json, link.source_hash = source, digest(source)
        return ideas_row, project["id"], self.current(row), checked

    def with_checked_photo(self, asset_version_id):
        """As if the ad had been made with a checked product photo whose library reference is in use."""
        patcher = patch("app.marketing.ideas_export.references_current", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        from app.adapters.persistence.models import AssetVersionRecord, GenerationRunRecord
        from app.generation.models import GenerationRecipeSnapshotRecord, RunItemRecord
        with tenant_session(self.context) as session:
            item = session.get(RunItemRecord, session.get(AssetVersionRecord, asset_version_id).producing_run_item_id)
            recipe = session.get(GenerationRecipeSnapshotRecord, session.get(GenerationRunRecord, item.generation_run_id).recipe_snapshot_id)
            canonical = deepcopy(recipe.canonical_json)
            canonical["selection"]["product_references"] = [{"reference_version_id": "libver_photo", "content_hash": "h",
                "role": "product_reference", "asset_version_id": "libver_photo", "content_sha256": self.PHOTO,
                "attestation": "verified_physical_product"}]
            recipe.canonical_json = canonical

    def reject_photo(self, ideas_row):
        """The user's decision on the photo, as the check before download records it (#199)."""
        content = {"photos": [{"photo_id": self.PHOTO, "decision": "rejected"}]}
        with tenant_session(self.context) as session:
            session.add(MarketingIdeasExportRecord(id=new_id("mier"), workspace_id=self.context.workspace_id,
                project_id=ideas_row["id"], kind="verification", project_revision=self.read_ideas(ideas_row)["revision"],
                actor_id=self.context.initiator_id, content_json=content, content_hash=digest(content)))

    def evaluated(self, row):
        self.command(self.current(row), "evaluate")
        with tenant_session(self.context) as session:
            frozen = workflow.get_round(session, self.context, row["id"])
            evaluation = workflow.latest_evaluation(session, self.context, frozen)
            return evaluation.id, workflow.evaluation_current(session, self.context, frozen, evaluation)

    def assert_test_unchanged(self, test_id, row, evaluation_id):
        """The running test keeps what it started on: the same evaluation, still current, and its package."""
        self.assertEqual(self.evaluated(row), (evaluation_id, True))
        self.assertEqual(self.tally(MarketingEvaluationRecord, round_id=row["id"]), 1)
        self.assertTrue(self.minimal_result(test_id, row["id"])["current"])
        self.handoff_link(self.current(row))

    def test_a_changed_fact_shows_on_the_test_and_changes_neither_its_evaluation_nor_its_package(self):
        ideas_row, test_id, row, checked = self.taken_to_test()
        self.assertEqual(self.progress(test_id, row["id"])["changed_ads"], [])
        evaluation_id, current = self.evaluated(row)
        self.assertTrue(current)
        self.handoff_link(row)

        self.edit_facts(ideas_row, self.set_fact("f3", statement="The enclosure is rated for indoor use."))
        self.assertEqual(self.statuses(ideas_row)["c3_1"], "needs_redo")
        self.assertEqual(self.progress(test_id, row["id"])["changed_ads"],
                         [{"asset_version_id": checked, "changes": ["fact"], "fact_ids": ["f3"]}])
        self.assert_test_unchanged(test_id, row, evaluation_id)

    def test_a_rejected_photo_shows_on_the_test_and_changes_neither_its_evaluation_nor_its_package(self):
        ideas_row, test_id, row, checked = self.taken_to_test(photo=True)
        self.assertEqual(self.progress(test_id, row["id"])["changed_ads"], [])
        evaluation_id, _ = self.evaluated(row)

        self.reject_photo(ideas_row)
        self.assertEqual(self.statuses(ideas_row)["c3_1"], "needs_redo")
        self.assertEqual(self.progress(test_id, row["id"])["changed_ads"],
                         [{"asset_version_id": checked, "changes": ["photo"], "fact_ids": []}])
        self.assert_test_unchanged(test_id, row, evaluation_id)

    def test_changed_copy_shows_on_the_test_and_changes_neither_its_evaluation_nor_its_package(self):
        ideas_row, test_id, row, checked = self.taken_to_test()
        evaluation_id, _ = self.evaluated(row)
        self.drain()  # the first batch finishes, so its ads' copy may be changed
        self.assertEqual(self.read_ideas(ideas_row)["first_batch"]["state"], "done")
        self.edit_copy(ideas_row, "c3_1", *PLAIN)
        self.assertEqual(self.progress(test_id, row["id"])["changed_ads"],
                         [{"asset_version_id": checked, "changes": ["copy"], "fact_ids": []}])
        self.assert_test_unchanged(test_id, row, evaluation_id)

    def test_an_earlier_tests_package_is_still_refused_once_an_ad_in_it_changed(self):
        ideas_row, _, row = self.ideas_and_round()
        checked = self.ids(ideas_row, ["c3_1"])[0]
        row = self.built(self.handoff(self.approved(self.reuse(row, checked))))
        self.edit_facts(ideas_row, self.set_fact("f3", statement="The enclosure is rated for indoor use."))
        refused = self.refused(self.handoff_link(row, expect=409), "ads_not_exportable")
        self.assertEqual(list(refused), [checked])


class DeviationMigrationDowngradeTests(unittest.TestCase):
    """Dropping recorded deviations would let the groups they name conclude, so downgrade refuses while one exists."""
    ROOT = Path(__file__).resolve().parents[1]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="marketai-round-deviations-migration-")
        self.addCleanup(self.temp.cleanup)
        self.database = f"{self.temp.name}/migration.db"
        self.environment = {**os.environ, "MARKETAI_DATABASE_URL": f"sqlite:///{self.database}",
                            "MARKETAI_DATA_DIR": f"{self.temp.name}/data"}
        upgraded = self.alembic("upgrade", "head")
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)

    def alembic(self, *arguments):
        return subprocess.run([sys.executable, "-m", "alembic", *arguments], cwd=self.ROOT, env=self.environment,
                              capture_output=True, text=True, check=False)

    def tables(self):
        with sqlite3.connect(self.database) as connection:
            return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

    def test_downgrade_without_deviations_drops_the_table(self):
        downgraded = self.alembic("downgrade", "zzu253a1b2c3")
        self.assertEqual(downgraded.returncode, 0, downgraded.stderr)
        self.assertNotIn("marketing_round_deviations", self.tables())
        self.assertIn("marketing_round_schedule_changes", self.tables())

    def test_downgrade_refuses_while_a_deviation_exists(self):
        with sqlite3.connect(self.database) as connection:
            connection.execute("INSERT INTO marketing_round_deviations (id, workspace_id, round_id, asset_version_ids, note,"
                               " actor_id, created_at) VALUES ('mrd_1', 'ws_1', 'mround_1', '[\"av_1\"]', 'Raised the budget',"
                               " 'user_a', '2026-11-22')")
        downgraded = self.alembic("downgrade", "zzu253a1b2c3")
        self.assertNotEqual(downgraded.returncode, 0)
        self.assertIn("round deviations exist", downgraded.stderr)
        self.assertIn("marketing_round_deviations", self.tables())
