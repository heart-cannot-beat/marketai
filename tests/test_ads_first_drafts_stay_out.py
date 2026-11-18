"""An ads-first draft goes into no round, no experiment and no regenerated image (#225; ADR 0018 §2).

#219 stopped the creative library from handing out a draft. Its review found two more ways out, both run
end to end on ``main`` before this change: a draft reused into a plan-first round of the same product card
left in that round's handoff ZIP, and an image regenerated from a draft in the library carried no ads-first
intent, so nothing stopped it. Here:

* reuse lists an ad of ads-first ideas that may not be exported, with the ``reuse_block`` it is refused
  for, and attaching it is refused (``ad_not_reusable``); a checked ad is reused like any creative;
* a handoff asks again when it is built, retried and linked (``ads_not_exportable``), as the library's
  export package does, so an ad checked when it was reused does not leave once a fact it stands on changed;
* the library regenerates no ad of ads-first ideas, checked or not (``ad_regenerate_on_ideas_page``), and the
  image's ``allowed_actions`` say so;
* a legacy experiment takes no such ad as an entry and does not freeze with one;
* every other image is reused, regenerated, handed off and entered exactly as before, never judged.

The ads come from a real first batch (fake page, fake model, fake image provider), the round from a real
confirmed plan of the same product card, and handoffs are built by the real Worker code. No paid call is
made. The product is the fictional Acme Charge Home.
"""
import asyncio
import hashlib
import io
import unittest
import uuid
import zipfile
from unittest.mock import patch

from sqlalchemy import func, select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (AssetRecord, AssetVersionRecord, AuditEventRecord, ExperimentEntryRecord,
                                            ExperimentRecord, JobRecord)
from app.generation import regeneration
from app.generation.models import GenerationDraftRecord
from app.marketing import ideas_export
from app.marketing.handoff_worker import execute_export
from app.marketing.models import MarketingProjectRecord
from app.marketing.reuse import ReuseCandidatePage
from app.marketing.workflow_models import MarketingCreativeReuseRecord, MarketingHandoffRecord, MarketingImageEditRecord
from app.platform.blob_store import CommittedBlobRef
import tests.test_marketing_evidence_handoff as evidence_tests
import tests.test_next_round as next_round_tests
from tests.test_experiment_lifecycle import _draft_context
from tests.test_library_export_gate import LibraryGateFixtures
from tests.test_marketing_ideas_export import FACTS
from tests.test_marketing_ideas_generation import IdeasGenerationFixtures
from tests.test_marketing_ideas_planning import SOURCE
from tests.test_marketing_workflow import MarketingWorkflowFixtures

#: The ads the fixture checks so the product card is published; the others stay drafts.
CHECKED = ["c3_1", "c3_2"]


class RoundGateFixtures(LibraryGateFixtures):
    card = None

    def create(self):
        """A plan-first test; started from ``card`` when one is set, as the product home starts it."""
        if not self.card:
            return super().create()
        response = self.client.post("/api/v1/marketing/projects?include_languages=true&include_plan_review=true"
                                    "&include_brand_style=true&include_execution_plan=true", headers=self.headers(),
                                    json={"source_text": "Acme Charge Home wall charger.", "product_id": self.card})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def ideas_and_round(self):
        """Ads-first ideas whose claim 3 is checked (so the product card is published) and a plan-first round
        of that card waiting for review. c1_* and c2_* are drafts; c3_* may be exported."""
        ideas = self.checked_ads()
        self.verify(ideas, fact_ids=["f3"], ads=CHECKED)
        statuses = self.statuses(ideas)
        self.assertEqual({c: statuses[c] for c in statuses if c not in CHECKED}, dict.fromkeys(
            [c for c in statuses if c not in CHECKED], "draft"))
        self.assertEqual({statuses[c] for c in CHECKED}, {"exportable"})
        self.card = ideas["product_id"]
        try:
            project = self.confirmed()
        finally:
            self.card = None
        self.generate(project)
        return ideas, project, self.start_round(project)

    def candidates(self, row, limit=50, cursor=None):
        response = self.client.get(f"/api/v1/marketing/rounds/{row['id']}/reuse-candidates",
                                   params={"limit": limit, **({"cursor": cursor} if cursor else {})})
        self.assertEqual(response.status_code, 200, response.text)
        ReuseCandidatePage.model_validate(response.json())
        return response.json()

    def listed(self, row):
        return {item["asset_version_id"]: item for item in self.candidates(row)["items"]}

    def reuse(self, row, asset_version_id, expect=201):
        response = self.client.post(f"/api/v1/marketing/rounds/{row['id']}/creatives/reuse", headers=self.headers(row["revision"]),
                                    json={"asset_version_id": asset_version_id, "combination_id": row["assets"][0]["combination_id"]})
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()["data"] if expect == 201 else response.json()

    def approved(self, row):
        """Every creative of the round approved, review by review."""
        for asset in row["assets"]:
            row = self.command(row, "reviews", {"asset_version_id": asset["asset_version_id"], "quality": "approved",
                "checks": {k: "pass" for k in asset["required_checks"]}, "note": "Checked product, proposition, copy and comparison"}, 201)
        return row

    def handoff(self, row, expect=202):
        return self.command(row, "handoff", {"asset_version_ids": [a["asset_version_id"] for a in row["assets"]]}, expect)

    def built(self, row):
        asyncio.run(execute_export(self.context, row["handoff"]["id"], self.blobs))
        return self.current(row)

    def current(self, row):
        response = self.client.get(f"/api/v1/marketing/rounds/{row['id']}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]

    def handoff_link(self, row, expect=201, key=None):
        response = self.client.post(f"/api/v1/marketing/rounds/{row['id']}/download-intent", headers=self.headers(key=key))
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def zipped(self, row):
        """The content hashes of the images in the round's handoff ZIP."""
        with tenant_session(self.context) as session:
            ref = CommittedBlobRef(**session.get(MarketingHandoffRecord, row["handoff"]["id"]).blob_json)
        archive = zipfile.ZipFile(io.BytesIO(asyncio.run(self.blobs.read_committed(ref, 200_000_000))))
        return {hashlib.sha256(archive.read(name)).hexdigest() for name in archive.namelist() if name.startswith("media/")}

    def sha(self, asset_version_id):
        with tenant_session(self.context) as session:
            return session.get(AssetVersionRecord, asset_version_id).content_sha256

    def refused(self, refusal, code):
        self.assertEqual(refusal["code"], code)
        self.assertEqual(refusal["params"]["count"], len(refusal["params"]["ads"]))
        return {item["asset_version_id"]: item for item in refusal["params"]["ads"]}

    def nothing_handed_off(self, row):
        self.assertEqual(self.count(MarketingHandoffRecord), 0)
        self.assertEqual(self.count(JobRecord, type="marketing.export"), 0)
        self.assertEqual(self.current(row)["state"], "reviewing")

    def put_back_unchecked(self, ideas, fact_id):
        """Change a checked fact and put it back: the ads citing it are drafts again (facts_unverified)."""
        original = next(fact for fact in FACTS if fact["id"] == fact_id)["statement"]
        self.edit_facts(ideas, self.set_fact(fact_id, statement="Charging starts by itself at night."))
        self.edit_facts(ideas, self.set_fact(fact_id, statement=original))


class ReuseCandidateTests(RoundGateFixtures):
    def test_drafts_are_listed_with_why_they_cannot_be_chosen_and_checked_ads_are_not_marked(self):
        ideas, _, row = self.ideas_and_round()
        ads = {ad["asset_version_id"]: ad for ad in self.read_ideas(ideas)["ads"]}
        listed = self.listed(row)
        # Every ad of the ideas is still a candidate, as before; only the ones that may not leave are marked.
        self.assertTrue(set(ads) <= set(listed))
        for asset_id, ad in ads.items():
            expected = None if ad["status"] == "exportable" else {"status": ad["status"], "reasons": ad["reasons"]}
            self.assertEqual(listed[asset_id]["reuse_block"], expected)
            self.assertEqual(listed[asset_id]["relation"], "product_card")
        self.assertEqual(sum(item["reuse_block"] is not None for item in listed.values()), 4)

    def test_an_ad_that_needs_redoing_is_marked_too(self):
        ideas, _, row = self.ideas_and_round()
        self.edit_facts(ideas, self.set_fact("f3", statement="The enclosure is rated for indoor use."))
        listed = self.listed(row)
        for asset_id in self.ids(ideas, CHECKED):
            self.assertEqual(listed[asset_id]["reuse_block"]["status"], "needs_redo")
            self.assertEqual([(r["code"], r["fact_ids"]) for r in listed[asset_id]["reuse_block"]["reasons"]][:1],
                             [("facts_changed", ["f3"])])

    def test_pages_are_as_full_as_before(self):
        _, _, row = self.ideas_and_round()
        whole = [item["asset_version_id"] for item in self.candidates(row)["items"]]
        paged, cursor = [], None
        while True:
            page = self.candidates(row, limit=2, cursor=cursor)
            paged += [item["asset_version_id"] for item in page["items"]]
            if not page["next_cursor"]:
                self.assertLessEqual(len(page["items"]), 2)
                break
            self.assertEqual(len(page["items"]), 2)
            cursor = page["next_cursor"]
        self.assertEqual(paged, whole)


class ReuseAttachTests(RoundGateFixtures):
    def test_a_draft_or_an_ad_that_needs_redoing_is_refused_whatever_the_page_offered(self):
        ideas, _, row = self.ideas_and_round()
        ads = self.ads(ideas)
        draft = ads["c1_1"]["asset_version_id"]
        refused = self.refused(self.reuse(row, draft, expect=409), "ad_not_reusable")
        self.assertEqual(refused, {draft: {"asset_version_id": draft, "status": "draft", "reasons": ads["c1_1"]["reasons"]}})
        self.edit_facts(ideas, self.set_fact("f3", statement="The enclosure is rated for indoor use."))
        stale = self.ids(ideas, ["c3_1"])[0]
        self.assertEqual(self.refused(self.reuse(row, stale, expect=409), "ad_not_reusable")[stale]["status"], "needs_redo")
        # Nothing joined the round and nothing was recorded as reused.
        self.assertEqual(self.count(MarketingCreativeReuseRecord), 0)
        self.assertEqual(self.count(AuditEventRecord, event_type="marketing.creative_reused"), 0)
        self.assertEqual({a["asset_version_id"] for a in self.current(row)["assets"]}, {a["asset_version_id"] for a in row["assets"]})

    def test_a_checked_ad_is_reused_like_any_creative(self):
        ideas, _, row = self.ideas_and_round()
        checked = self.ids(ideas, ["c3_1"])[0]
        row = self.reuse(row, checked)
        self.assertIn(checked, {a["asset_version_id"] for a in row["assets"]})
        self.assertNotIn(checked, self.listed(row))  # in the round now, so no longer a candidate

    def test_the_path_the_review_ran_is_closed(self):
        """Draft -> reuse -> round -> handoff link: on ``main`` before #225 the ZIP held the draft."""
        ideas, _, row = self.ideas_and_round()
        draft = self.ads(ideas)["c1_1"]["asset_version_id"]
        self.assertEqual(self.listed(row)[draft]["reuse_block"]["status"], "draft")
        self.refused(self.reuse(row, draft, expect=409), "ad_not_reusable")
        row = self.built(self.handoff(self.approved(self.current(row))))
        self.handoff_link(row)
        self.assertNotIn(self.sha(draft), self.zipped(row))
        self.assertNotIn(draft, [entry["asset_version_id"] for entry in row["handoff"]["entries"]])

    def test_a_draft_reused_before_this_change_does_not_leave_in_the_handoff(self):
        ideas, _, row = self.ideas_and_round()
        draft = self.ids(ideas, ["c1_1"])[0]
        # As if attached before the gate existed (the switch was off, so there should be none).
        with patch("app.marketing.ideas_export.library_refusals", return_value=[]):
            row = self.reuse(row, draft)
        row = self.approved(row)
        refused = self.refused(self.handoff(row, expect=409), "ads_not_exportable")
        self.assertEqual(list(refused), [draft])
        self.nothing_handed_off(row)


class HandoffTests(RoundGateFixtures):
    def reused_and_approved(self):
        ideas, _, row = self.ideas_and_round()
        checked = self.ids(ideas, ["c3_1"])[0]
        return ideas, checked, self.approved(self.reuse(row, checked))

    def test_an_ad_checked_when_reused_is_not_handed_off_once_a_fact_it_stands_on_changed(self):
        ideas, checked, row = self.reused_and_approved()
        self.edit_facts(ideas, self.set_fact("f3", statement="The enclosure is rated for indoor use."))
        refused = self.refused(self.handoff(row, expect=409), "ads_not_exportable")
        self.assertEqual((list(refused), refused[checked]["status"]), ([checked], "needs_redo"))
        self.nothing_handed_off(row)

    def test_a_handoff_link_is_refused_once_an_ad_in_it_stopped_being_exportable_and_given_again_once_checked(self):
        ideas, checked, row = self.reused_and_approved()
        row = self.built(self.handoff(row))
        self.assertIn(self.sha(checked), self.zipped(row))
        self.handoff_link(row, key="handoff-link")
        self.put_back_unchecked(ideas, "f3")
        refused = self.refused(self.handoff_link(row, expect=409), "ads_not_exportable")
        self.assertEqual(refused[checked]["status"], "draft")
        # The same request again is asked again; the package stays as history.
        self.refused(self.handoff_link(row, key="handoff-link", expect=409), "ads_not_exportable")
        self.assertEqual(self.current(row)["handoff"]["state"], "ready")
        self.verify(ideas, fact_ids=["f3"])
        self.handoff_link(row)

    def test_a_failed_handoff_is_not_retried_with_an_ad_that_stopped_being_exportable(self):
        ideas, checked, row = self.reused_and_approved()
        row = self.handoff(row)
        with tenant_session(self.context) as session:
            session.get(MarketingHandoffRecord, row["handoff"]["id"]).state = "failed"
        self.edit_facts(ideas, self.set_fact("f3", statement="The enclosure is rated for indoor use."))
        refused = self.refused(self.command(self.current(row), "handoff/retry", None, 409), "ads_not_exportable")
        self.assertEqual(list(refused), [checked])
        self.assertEqual(self.current(row)["handoff"]["state"], "failed")

    def test_the_worker_that_builds_the_zip_never_judges_an_ad(self):
        _, checked, row = self.reused_and_approved()
        row = self.handoff(row)
        with patch("app.marketing.ideas_export.statuses", side_effect=AssertionError("the Worker judged an ad")):
            row = self.built(row)
        self.assertEqual(row["handoff"]["state"], "ready")


class RegenerationTests(RoundGateFixtures):
    def regenerate(self, asset_version_id, intent="new_seed", expect=201):
        response = self.client.post(f"/api/v1/generation/asset-versions/{asset_version_id}/regeneration-drafts",
                                    json={"intent": intent, **({"prompt_overrides": {"prompt": "A brighter driveway."}}
                                                               if intent == "edit_prompt" else {})}, headers=self.headers())
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def detail_actions(self, asset_version_id):
        response = self.client.get(f"/api/v1/generation/asset-versions/{asset_version_id}")
        self.assertEqual(response.status_code, 200, response.text)
        return {item["action"]: (item["allowed"], item["reason_code"]) for item in response.json()["allowed_actions"]}

    def test_the_library_regenerates_no_ad_of_the_ideas_checked_or_not(self):
        ideas = self.checked_ads()
        self.verify(ideas, fact_ids=["f3"], ads=CHECKED)
        self.edit_facts(ideas, self.set_fact("f2", statement="Load balancing shares power with the dryer."))
        statuses = self.statuses(ideas)
        chosen = {status: self.ids(ideas, [next(c for c, s in statuses.items() if s == status)])[0]
                  for status in ("draft", "needs_redo", "exportable")}
        for status, asset_id in chosen.items():
            for intent in ("same_recipe", "new_seed", "edit_prompt"):
                refusal = self.regenerate(asset_id, intent, expect=409)
                self.assertEqual((refusal["code"], refusal["params"]), ("ad_regenerate_on_ideas_page", {"asset_version_ids": [asset_id]}))
        self.assertEqual(self.count(GenerationDraftRecord, source_asset_version_id=chosen["draft"]), 0)
        self.assertEqual({self.count(GenerationDraftRecord, source_asset_version_id=a) for a in chosen.values()}, {0})

    def test_a_review_summary_batch_with_an_ad_of_the_ideas_fails_as_a_whole(self):
        ideas = self.checked_ads()
        images = self.plan_first_images()
        ad = self.ids(ideas, ["c1_1"])[0]
        for asset_id in (*images, ad):
            self.approve(asset_id)
        drafts = self.count(GenerationDraftRecord)
        response = self.client.post("/api/v1/generation/regeneration-draft-batches", headers=self.headers(),
                                    json={"action": "supplement_approved", "candidate_version_ids": [images[0], ad]})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual((response.json()["code"], response.json()["params"]), ("ad_regenerate_on_ideas_page", {"asset_version_ids": [ad]}))
        self.assertEqual(self.count(GenerationDraftRecord), drafts)

    def test_every_caller_of_the_one_regeneration_is_refused(self):
        """The experiments' next-round regeneration calls the same function."""
        ideas = self.checked_ads()
        with tenant_session(self.context) as session:
            with self.assertRaises(regeneration.RegenerationError) as caught:
                regeneration.create_regeneration_draft(session, self.context, self.ids(ideas, ["c2_2"])[0], "same_recipe")
        self.assertEqual(caught.exception.code, "ad_regenerate_on_ideas_page")

    def test_the_image_says_it_is_changed_on_the_ideas_page_and_saved_only_once_checked(self):
        ideas = self.checked_ads()
        self.verify(ideas, fact_ids=["f3"], ads=CHECKED)
        draft, checked = self.ids(ideas, ["c1_1", "c3_1"])
        self.assertEqual(self.detail_actions(draft), {"review": (True, None),
            "regenerate": (False, "ad_regenerate_on_ideas_page"), "download": (False, "ads_not_exportable")})
        self.assertEqual(self.detail_actions(checked), {"review": (True, None),
            "regenerate": (False, "ad_regenerate_on_ideas_page"), "download": (True, None)})

    def test_old_flow_images_are_regenerated_and_described_as_before_and_never_judged(self):
        images = self.plan_first_images()
        with patch("app.marketing.ideas_export.statuses", wraps=ideas_export.statuses) as judged:
            for intent in ("same_recipe", "new_seed", "edit_prompt"):
                draft = self.regenerate(images[0], intent)
                self.assertEqual((draft["source_asset_version_id"], draft["regeneration_intent"]), (images[0], intent))
            self.assertEqual(self.detail_actions(images[0]), {"review": (True, None), "regenerate": (True, None),
                                                              "download": (True, None)})
            for asset_id in images:
                self.approve(asset_id)
            # A batch answers as it always did: these images carry no dimension signature to group by (a batch
            # of workbench images is tested in ``tests.test_generation_regeneration``).
            response = self.client.post("/api/v1/generation/regeneration-draft-batches", headers=self.headers(),
                                        json={"action": "supplement_approved", "candidate_version_ids": images})
            self.assertEqual((response.status_code, response.json()["code"]), (422, "missing_signature"))
        judged.assert_not_called()


class ExperimentTests(RoundGateFixtures):
    def experiment(self, product_id):
        response = self.client.post("/api/v1/experiments", headers=self.headers(),
                                    json={"product_id": product_id, "name": "Acme Charge Home ads", "draft_context": _draft_context()})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def entry(self, experiment_id, asset_version_id, revision, expect=201):
        response = self.client.post(f"/api/v1/experiments/{experiment_id}/entries", headers=self.headers(),
                                    json={"asset_version_id": asset_version_id, "expected_revision": revision, "marketing_vars": {}})
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def test_an_experiment_takes_no_draft_and_does_not_freeze_with_an_ad_that_stopped_being_exportable(self):
        ideas = self.checked_ads()
        self.verify(ideas, fact_ids=["f3"], ads=CHECKED)
        draft, first, second = self.ids(ideas, ["c1_1", "c3_1", "c3_2"])
        experiment = self.experiment(ideas["product_id"])
        refused = self.refused(self.entry(experiment["id"], draft, 1, expect=409), "ad_not_reusable")
        self.assertEqual(refused[draft]["status"], "draft")
        added = self.entry(experiment["id"], first, 1)
        added = self.entry(experiment["id"], second, added["revision"])
        # Swapping an entry for a draft is refused the same way.
        response = self.client.put(f"/api/v1/experiments/{experiment['id']}/entries/{added['id']}", headers=self.headers(),
                                   json={"asset_version_id": draft, "expected_revision": added["revision"]})
        self.assertEqual(response.status_code, 409, response.text)
        self.refused(response.json(), "ad_not_reusable")
        self.assertEqual(self.count(ExperimentEntryRecord, experiment_id=experiment["id"]), 2)
        # Ads-first images carry no concept identity, so an experiment never freezes with one, checked or not
        # (``concept_identity_missing``, before #225); a draft is now refused for what it is, before that.
        analysis = self.client.get(f"/api/v1/experiments/{experiment['id']}/duplicate-analysis")
        self.assertEqual((analysis.status_code, analysis.json()["code"]), (409, "concept_identity_missing"))
        freeze = {"expected_revision": added["revision"], "analyzed_candidate_set_hash": "0" * 64}
        self.put_back_unchecked(ideas, "f3")
        response = self.client.post(f"/api/v1/experiments/{experiment['id']}/freeze", headers=self.headers(), json=freeze)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(set(self.refused(response.json(), "ad_not_reusable")), {first, second})
        self.verify(ideas, fact_ids=["f3"])
        response = self.client.post(f"/api/v1/experiments/{experiment['id']}/freeze", headers=self.headers(), json=freeze)
        self.assertEqual((response.status_code, response.json()["code"]), (409, "concept_identity_missing"))

    def test_old_flow_images_enter_an_experiment_as_before_and_are_never_judged(self):
        images = self.plan_first_images()
        with tenant_session(self.context) as session:
            product_id = session.scalar(select(AssetRecord.product_id).join(
                AssetVersionRecord, AssetVersionRecord.asset_id == AssetRecord.id).where(AssetVersionRecord.id == images[0]))
        experiment = self.experiment(product_id)
        with patch("app.marketing.ideas_export.statuses", wraps=ideas_export.statuses) as judged:
            revision = 1
            for image in images:
                revision = self.entry(experiment["id"], image, revision)["revision"]
        judged.assert_not_called()


class OldFlowTests(RoundGateFixtures):
    def test_a_plan_first_round_reuses_and_hands_off_its_own_creatives_as_before_and_never_judges_them(self):
        project = self.confirmed()
        self.generate(project)
        first = self.start_round(project)
        self.command(first, "close", {"reason": "user_stopped", "note": "Prepare the next small round"})
        project = self.read(project)
        response = self.edit(project, "confirm_plan", plan=project["plan"])
        self.assertEqual(response.status_code, 200, response.text)
        project = response.json()
        self.generate(project, colour=(90, 12, 34))
        row = self.start_round(project)
        with patch("app.marketing.ideas_export.statuses", wraps=ideas_export.statuses) as judged:
            listed = self.listed(row)
            self.assertTrue(listed)
            self.assertEqual({item["reuse_block"] for item in listed.values()}, {None})
            earlier = next(iter(listed))
            row = self.reuse(row, earlier)
            row = self.built(self.handoff(self.approved(row)))
            self.handoff_link(row)
            self.assertIn(self.sha(earlier), self.zipped(row))
        judged.assert_not_called()


class TenantTests(RoundGateFixtures):
    def test_another_workspace_can_neither_reuse_regenerate_nor_enter_these_ads_nor_learn_they_are_ads(self):
        ideas, _, row = self.ideas_and_round()
        chosen = self.ids(ideas, ["c1_1", "c3_1"])
        owner = self.context
        self.context = self._make_context()
        # This round, its candidates and its handoff are not there for another workspace.
        response = self.client.get(f"/api/v1/marketing/rounds/{row['id']}/reuse-candidates")
        self.assertEqual(response.status_code, 404, response.text)
        response = self.client.post(f"/api/v1/marketing/rounds/{row['id']}/creatives/reuse", headers=self.headers(row["revision"]),
                                    json={"asset_version_id": chosen[1], "combination_id": row["assets"][0]["combination_id"]})
        self.assertEqual(response.status_code, 404, response.text)
        # Its own round of its own card cannot take these ads either: they are not there for it at all.
        _, _, other = self.ideas_and_round()
        for asset_id in chosen:
            self.assertEqual(self.reuse(other, asset_id, expect=404)["code"], "asset_not_found")
            response = self.client.post(f"/api/v1/generation/asset-versions/{asset_id}/regeneration-drafts",
                                        json={"intent": "new_seed"}, headers=self.headers())
            self.assertEqual((response.status_code, response.json()["code"]), (404, "source_not_found"))
            response = self.client.get(f"/api/v1/generation/asset-versions/{asset_id}")
            self.assertEqual(response.status_code, 404, response.text)
        experiment = self.client.post("/api/v1/experiments", headers=self.headers(), json={
            "product_id": self.ideas_product(), "name": "Other", "draft_context": _draft_context()}).json()
        response = self.client.post(f"/api/v1/experiments/{experiment['id']}/entries", headers=self.headers(),
                                    json={"asset_version_id": chosen[0], "expected_revision": 1, "marketing_vars": {}})
        self.assertEqual((response.status_code, response.json()["code"]), (422, "asset_version_not_found"))
        with tenant_session(self.context) as session:
            self.assertEqual(ideas_export.ads_of_ideas(session, self.context, chosen), set())
            self.assertEqual(ideas_export.library_refusals(session, self.context, chosen), [])
            self.assertEqual(session.scalar(select(func.count()).select_from(MarketingCreativeReuseRecord).where(
                MarketingCreativeReuseRecord.workspace_id == self.context.workspace_id)), 0)
        self.context = owner
        self.assertEqual(self.count(MarketingCreativeReuseRecord), 0)
        self.assertEqual({item["reuse_block"]["status"] if item["reuse_block"] else None
                          for asset_id, item in self.listed(row).items() if asset_id in chosen}, {"draft", None})

    def ideas_product(self):
        with tenant_session(self.context) as session:
            return session.scalar(select(MarketingProjectRecord.product_id).where(
                MarketingProjectRecord.workspace_id == self.context.workspace_id, MarketingProjectRecord.flow == "ads_first"))


class RoundEditTests(RoundGateFixtures):
    """A checked ad reused into a round is not cropped or revised there (#225 review): the new image would be a
    round image the ideas never judge, and it would leave in the handoff after the ad itself needs redoing."""
    prepare = IdeasGenerationFixtures.prepare
    prepared = IdeasGenerationFixtures.prepared
    preview = IdeasGenerationFixtures.preview
    previewed = IdeasGenerationFixtures.previewed
    submit = IdeasGenerationFixtures.submit
    submitted = IdeasGenerationFixtures.submitted
    execute_items = IdeasGenerationFixtures.execute_items

    def ideas_on_the_tests_own_product(self):
        """A plan-first test, and ads-first ideas started on that test's own product with one checked ad: the
        ad is a candidate of the round as ``this_test``, which is the case whose reused creatives are editable."""
        plan = self.confirmed()
        with tenant_session(self.context) as session:
            anchor = session.get(MarketingProjectRecord, plan["id"]).product_id
        response = self.client.post("/api/v1/marketing/ideas", headers=self.headers(), json={
            "product_id": anchor, "source_text": SOURCE, "market": "United States", "ad_language": "en-US"})
        self.assertEqual(response.status_code, 201, response.text)
        ideas = response.json()
        self.execute(self.drafted_call(ideas, "understanding"))
        ideas = self.read_ideas(ideas)
        self.execute(self.drafted_call(ideas, "ideas"))
        ideas = self.read_ideas(ideas)
        self.execute_items(self.submitted(self.prepared(ideas)["drafts"][0]["draft_id"]))
        ideas = self.read_ideas(ideas)
        self.start_checks(ideas)
        self.drain(job_types=("marketing.ai_qc",))
        ideas = self.read_ideas(ideas)
        (ad,) = ideas["ads"]
        self.verify(ideas, fact_ids=[f["id"] for f in ideas["understanding"]["facts"]], ads=[ad["combination_id"]])
        self.assertEqual(self.ads(ideas)[ad["combination_id"]]["status"], "exportable")
        self.generate(plan)
        return ideas, ad["asset_version_id"], self.start_round(plan)

    def test_a_reused_ad_is_neither_cropped_nor_revised_in_the_round_and_says_so(self):
        ideas, ad, row = self.ideas_on_the_tests_own_product()
        self.assertEqual((self.listed(row)[ad]["relation"], self.listed(row)[ad]["reuse_block"]), ("this_test", None))
        row = self.reuse(row, ad)
        own = next(a["asset_version_id"] for a in row["assets"] if a["asset_version_id"] != ad)
        response = self.client.get(f"/api/v1/marketing/rounds/{row['id']}", params={"include_reuse_lineage": True})
        editable = {a["asset_version_id"]: a["editable"] for a in response.json()["data"]["assets"]}
        self.assertEqual((editable[ad], editable[own]), (False, True))
        crop = self.command(row, "image-edits", {"asset_version_id": ad, "crop": {"x": 0, "y": 0, "width": 0.5, "height": 1}}, 409)
        self.assertEqual(crop["code"], "ad_edit_on_ideas_page")
        revision = self.command(self.current(row), "feedback-drafts", {"asset_version_id": ad, "provider": "mock", "model": "mock-v1",
                                                                      "note": "Make the background brighter."}, 409)
        self.assertEqual(revision["code"], "ad_edit_on_ideas_page")
        self.assertEqual(self.count(MarketingImageEditRecord), 0)
        # The round's own creative is cropped as before.
        self.command(self.current(row), "image-edits", {"asset_version_id": own, "crop": {"x": 0, "y": 0, "width": 0.5, "height": 1}}, 202)


class EvidenceHandoffGateTests(MarketingWorkflowFixtures):
    """The round's evidence handoff writes an experiment with the image as its winner (#225 review)."""
    confirmed = evidence_tests.TestMarketingEvidenceHandoff.confirmed
    commercial_versions = evidence_tests.TestMarketingEvidenceHandoff.commercial_versions
    evaluated_real_contract = evidence_tests.TestMarketingEvidenceHandoff.evaluated_real_contract
    handoff = evidence_tests.TestMarketingEvidenceHandoff.handoff

    def count(self, model):
        with tenant_session(self.context) as session:
            return session.scalar(select(func.count()).select_from(model).where(model.workspace_id == self.context.workspace_id))

    def test_an_ad_that_may_not_be_exported_is_not_handed_to_an_experiment_as_a_winner(self):
        row = self.evaluated_real_contract()
        _, offer = self.commercial_versions(row)
        winner = row["evaluation"]["qualified_asset_version_ids"][0]
        experiments = self.count(ExperimentRecord)
        # As if this image were an ad of ads-first ideas that stopped being exportable after the round's handoff.
        refusal = [{"asset_version_id": winner, "status": "needs_redo", "reasons": [{"code": "facts_changed", "fact_ids": ["f1"], "finding_ids": []}]}]
        with patch("app.marketing.ideas_export.library_refusals", return_value=refusal) as asked:
            response = self.handoff(row, offer)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual((response.json()["code"], response.json()["params"]), ("ad_not_reusable", {"ads": refusal, "count": 1}))
        asked.assert_called_once()
        self.assertEqual(self.count(ExperimentRecord), experiments)
        # Any other image is handed off as before.
        self.assertEqual(self.handoff(row, offer).status_code, 201)


class ExperimentAndLaunchGateTests(unittest.TestCase):
    """An experiment asks again when it starts, a launch preview before anything leaves for Meta, and the
    next round of a decision says why it does not regenerate an ad of ads-first ideas (#225 review). The
    images are the lifecycle fixtures' generated ones; being an unchecked ad is simulated."""

    @classmethod
    def setUpClass(cls):
        next_round_tests.NextRoundTests.setUpClass.__func__(cls)

    def refusing(self, asset_version_id):
        return [{"asset_version_id": asset_version_id, "status": "draft",
                 "reasons": [{"code": "facts_unverified", "fact_ids": ["f1"], "finding_ids": []}]}]

    def test_an_experiment_does_not_start_with_an_ad_that_stopped_being_exportable(self):
        from app.experiments.service import (ExperimentConflict, add_entry_v2, bind_candidate_v2, create_experiment_v2,
                                             freeze_experiment_v2, start_experiment_v2)
        ctx = next_round_tests.CTX
        with tenant_session(ctx) as session:
            images = next_round_tests._seed_asset_versions(session, f"s19start_{uuid.uuid4().hex[:6]}")
        experiment = create_experiment_v2(ctx, product_id=next_round_tests.PROD_ID, name="S19 start",
                                          draft_context=next_round_tests._draft_context())
        add_entry_v2(ctx, experiment["id"], asset_version_id=images[0], expected_revision=1)
        add_entry_v2(ctx, experiment["id"], asset_version_id=images[1], expected_revision=2)
        frozen = freeze_experiment_v2(ctx, experiment["id"], expected_revision=3)
        for candidate in frozen["candidates"]:
            bind_candidate_v2(ctx, experiment["id"], candidate["id"], ad_account_id=f"act_s19_{candidate['position']}",
                              ad_id=f"ad_s19_{uuid.uuid4().hex[:6]}", expected_revision=frozen["revision"])
        with patch("app.marketing.ideas_export.library_refusals", return_value=self.refusing(images[0])):
            with self.assertRaises(ExperimentConflict) as caught:
                start_experiment_v2(ctx, experiment["id"], expected_revision=frozen["revision"])
        self.assertEqual((caught.exception.code, caught.exception.params["count"]), ("ad_not_reusable", 1))
        self.assertEqual(start_experiment_v2(ctx, experiment["id"], expected_revision=frozen["revision"])["state"], "running")

    def test_a_launch_preview_does_not_send_an_ad_that_may_not_be_exported(self):
        from fastapi.testclient import TestClient
        from app.main import app
        from app.platform.http_identity import request_tenant_context
        app.dependency_overrides[request_tenant_context] = lambda: next_round_tests.CTX
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        client = TestClient(app)
        self.addCleanup(client.close)
        body = {"experiment_id": "exp_s19", "experiment_version_id": "expver_s19", "connection_id": "mc_s19",
                "meta_mode": "controlled_mock", "ad_account_id": "act_s19", "daily_budget_minor": 100, "currency": "USD",
                "campaign_config": {}, "candidates": [{"experiment_version_candidate_id": "evc_s19", "asset_version_id": "av_s19",
                                                       "creative_config": {}}]}
        with patch("app.marketing.ideas_export.library_refusals", return_value=self.refusing("av_s19")) as asked:
            response = client.post("/api/v1/launch/previews", json=body, headers={"Idempotency-Key": uuid.uuid4().hex})
        self.assertEqual(response.status_code, 409, response.text)
        detail = response.json()
        self.assertEqual((detail["code"], detail["params"]["count"]), ("ads_not_exportable", 1))
        asked.assert_called_once()
        # Without the refusal the same request goes on to the preview's own checks (no such experiment here).
        response = client.post("/api/v1/launch/previews", json=body, headers={"Idempotency-Key": uuid.uuid4().hex})
        self.assertNotEqual(response.json().get("code"), "ads_not_exportable")

    def test_the_next_round_of_a_decision_on_an_ad_of_ideas_is_refused_with_a_reason(self):
        from app.experiments.service import ExperimentConflict, create_next_round
        closed, decisions = next_round_tests._build_closed_with_decisions(f"s19nr_{uuid.uuid4().hex[:6]}")
        with patch("app.marketing.ideas_export.ads_of_ideas", side_effect=lambda session, ctx, ids: set(ids)):
            with self.assertRaises(ExperimentConflict) as caught:
                create_next_round(next_round_tests.CTX, closed["id"], decisions[0]["id"], intent_kind="same_recipe",
                                  expected_revision=closed["revision"])
        self.assertEqual(caught.exception.code, "ad_regenerate_on_ideas_page")
        self.assertTrue(create_next_round(next_round_tests.CTX, closed["id"], decisions[0]["id"], intent_kind="same_recipe",
                                          expected_revision=closed["revision"])["id"].startswith("enrr_"))
