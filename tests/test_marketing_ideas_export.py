"""Ads-first verification, ad status and download (#198; ADR 0018 §2-§3, design doc §5.3 and §6).

A draft may be made from unchecked facts, and only a checked ad may be downloaded. The ads come from a real
first batch (fake page, fake model, fake image provider) and are checked through the real endpoints and the
real Worker code; BYOK checks answer from a fake vision model. No paid call is made.

The product is the fictional Acme Charge Home. Its understanding has four facts and its ideas three claims:
claim 1 cites f1, claim 2 cites f2, claim 3 cites only an inference that rests on f3, and no ad cites f4.
"""
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (AssetVersionRecord, AuditEventRecord, DownloadIntentRecord, LibraryDraftRecord,
                                            LibraryEntryRecord, LibraryVersionRecord, ProductRecord, new_id)
from app.generation.models import GenerationRecipeSnapshotRecord, RunItemRecord
from app.library import service as library
from app.library.contracts import ENTRY_PRODUCT
from app.marketing import ideas, ideas_export, planner
from app.marketing.ai_qc_models import MarketingAIQCRecord
from app.marketing.ideas_contracts import IdeasDownload, IdeasProjectDetail
from app.marketing.models import MarketingIdeasExportRecord, MarketingProjectRecord, MarketingRevisionRecord
from tests.test_marketing_first_batch import EXAMPLE, check_report, one_claim
from tests.test_marketing_ideas_checks import IdeasChecksFixtures
from tests.test_marketing_ideas_generation import IdeasGenerationFixtures
from tests.test_marketing_product_references import ProductPhotoFixtures
from tests.test_marketing_ideas_planning import SOURCE

EXAMPLE_RESULT = planner.example_result
FACTS = [
    {"id": "f1", "statement": "Charging can be scheduled from the Acme Charge app.",
     "source_quote": "Schedule charging from the Acme Charge app so the car charges overnight."},
    {"id": "f2", "statement": "Load balancing keeps the home circuit from tripping.",
     "source_quote": "Load balancing keeps the home circuit from tripping while the car charges."},
    {"id": "f3", "statement": "The enclosure is IP55 rated for outdoor installation.",
     "source_quote": "IP55 enclosure for outdoor installation in a driveway or carport."},
    {"id": "f4", "statement": "It is a wall charger for electric cars.",
     "source_quote": "Wall charger for electric cars with app scheduling and load balancing."},
]
#: Which ads stand on which fact: the third claim cites f3 only through the inference i1.
ADS_OF = {"f1": ["c1_1", "c1_2"], "f2": ["c2_1", "c2_2"], "f3": ["c3_1", "c3_2"]}


def understood(kind, inputs):
    """The Controlled Mock understanding, with four separate facts of the Acme Charge page."""
    result = EXAMPLE_RESULT(kind, inputs)
    # Only for the Acme Charge page the first batch reads; any other source keeps the plain example.
    if kind == "understanding" and all(fact["source_quote"] in inputs["source_text"] for fact in FACTS):
        result["product_name"] = "Acme Charge Home"
        result["facts"] = [{**fact, "confirmation": "unconfirmed"} for fact in FACTS]
        result["inferences"] = [{"id": "i1", "statement": "Drivers who park outside worry about the weather.",
                                 "rationale": "An unvalidated hypothesis.", "fact_ids": ["f3"]}]
    return result


def claims(inputs, headline=None):
    """The Controlled Mock ideas with each claim standing on its own fact (see the module docstring)."""
    drafted = EXAMPLE(inputs)
    if len(inputs["understanding"]["facts"]) != len(FACTS):
        return drafted
    for angle, fact_ids, inference_ids in zip(drafted["angles"], (["f1"], ["f2"], []), ([], [], ["i1"])):
        angle["fact_ids"], angle["inference_ids"] = fact_ids, inference_ids
        for expression in angle["expressions"]:
            expression["evidence_basis"] = ({"fact_ids": fact_ids, "gap": ""} if fact_ids
                                            else {"fact_ids": [], "gap": "Rests on a guess about outdoor parking."})
    if headline:
        for combination in drafted["combinations"]:
            combination["headline"] = headline
    return drafted


class IdeasExportFixtures(IdeasChecksFixtures):
    def setUp(self):
        super().setUp()
        # The Controlled Mock path reads the examples from the application; the fake BYOK model of the
        # first-batch fixtures reads the copies that module imported.
        for target, replacement in (("app.marketing.planner.example_result", understood), ("app.marketing.ideas.example", claims),
                                    ("tests.test_marketing_first_batch.example_result", understood),
                                    ("tests.test_marketing_first_batch.EXAMPLE", claims)):
            patcher = patch(target, side_effect=replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def checked_ads(self, mode="mock"):
        """Six ads of a first batch, each with a finished automatic check; nothing checked by the user."""
        project = self.made(mode)
        self.start_checks(project, mode)
        if mode == "mock":
            self.drain(job_types=("marketing.ai_qc",))
        else:
            self.run_checks(project, AsyncMock(side_effect=lambda provider, model, credential, inputs, images: check_report(inputs)))
        return self.read_ideas(project)

    def ads(self, project):
        """The project's ads by combination id, as the API reports them now."""
        return {ad["combination_id"]: ad for ad in self.read_ideas(project)["ads"]}

    def statuses(self, project):
        return {combination_id: ad["status"] for combination_id, ad in self.ads(project).items()}

    def reasons(self, ad):
        return [reason["code"] for reason in ad["reasons"]]

    def verify(self, project, fact_ids=(), ads=(), findings=None, expect=200, key=None, revision=None):
        """Check ``fact_ids``; for each of ``ads`` confirm its look and accept every finding of its check."""
        current = self.read_ideas(project)
        chosen = [ad for ad in current["ads"] if ad["combination_id"] in ads]
        body = {"fact_ids": list(fact_ids), "appearance_confirmed": [ad["asset_version_id"] for ad in chosen],
                "accepted_findings": findings if findings is not None else [
                    {"asset_version_id": ad["asset_version_id"], "check_id": ad["check"]["id"],
                     "finding_ids": [f["check_id"] for f in ad["check"]["findings"]]}
                    for ad in chosen if ad["check"] and ad["check"]["findings"]]}
        response = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/verification", json=body,
                                    headers=self.headers(revision or current["revision"], key))
        self.assertEqual(response.status_code, expect, response.text)
        if expect == 200:
            IdeasProjectDetail.model_validate(response.json())
        return response.json()

    def verify_everything(self, project):
        return self.verify(project, fact_ids=["f1", "f2", "f3"], ads=[c for cs in ADS_OF.values() for c in cs])

    def edit_facts(self, project, change, expect=200, source_addition=None, revision=None):
        """PATCH the understanding after ``change`` altered a copy of it."""
        current = self.read_ideas(project)
        understanding = deepcopy(current["understanding"])
        change(understanding)
        body = {"understanding": understanding, **({"source_addition": source_addition} if source_addition else {})}
        response = self.client.patch(f"/api/v1/marketing/ideas/{project['id']}/understanding", json=body,
                                     headers=self.headers(revision or current["revision"]))
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def download(self, project, combination_ids, expect=201, key=None):
        ads = self.ads(project)
        response = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/download-intent", headers=self.headers(key=key),
                                    json={"asset_version_ids": [ads[cid]["asset_version_id"] for cid in combination_ids]})
        self.assertEqual(response.status_code, expect, response.text)
        if expect == 201:
            IdeasDownload.model_validate(response.json())
        return response.json()

    def records(self, project, kind):
        with tenant_session(self.context) as session:
            rows = session.scalars(select(MarketingIdeasExportRecord).where(
                MarketingIdeasExportRecord.workspace_id == self.context.workspace_id,
                MarketingIdeasExportRecord.project_id == project["id"], MarketingIdeasExportRecord.kind == kind)
                .order_by(MarketingIdeasExportRecord.created_at, MarketingIdeasExportRecord.id)).all()
            for row in rows:
                session.expunge(row)
            return rows

    def product_versions(self, project):
        """The product's creative-library versions, oldest first, and its library Draft."""
        with tenant_session(self.context) as session:
            product = session.get(ProductRecord, project["product_id"])
            versions = session.scalars(select(LibraryVersionRecord).where(
                LibraryVersionRecord.entry_id == product.product_profile_entry_id).order_by(LibraryVersionRecord.version_no)).all()
            draft = session.scalar(select(LibraryDraftRecord).where(LibraryDraftRecord.entry_id == product.product_profile_entry_id))
            for row in (*versions, draft):
                session.expunge(row)
            return versions, draft

    def set_fact(self, fact_id, **values):
        def change(understanding):
            next(fact for fact in understanding["facts"] if fact["id"] == fact_id).update(values)
        return change


class DraftAndExportableTests(IdeasExportFixtures):
    def test_a_draft_cannot_be_downloaded_and_the_refusal_names_each_ads_reasons(self):
        project = self.checked_ads()
        self.assertEqual(set(self.statuses(project).values()), {"draft"})
        ads = self.ads(project)
        refused = self.download(project, ["c1_1", "c2_1"], expect=409)
        self.assertEqual(refused["code"], "ads_not_exportable")
        listed = {item["asset_version_id"]: item for item in refused["params"]["ads"]}
        self.assertEqual(set(listed), {ads["c1_1"]["asset_version_id"], ads["c2_1"]["asset_version_id"]})
        first = listed[ads["c1_1"]["asset_version_id"]]
        self.assertEqual(first["status"], "draft")
        self.assertEqual([(r["code"], r["fact_ids"]) for r in first["reasons"]][:2],
                         [("facts_unverified", ["f1"]), ("appearance_unconfirmed", [])])
        self.assertEqual(self.reasons(first), ["facts_unverified", "appearance_unconfirmed", "findings_open"])
        self.assertTrue(first["reasons"][2]["finding_ids"])
        self.assertEqual(listed[ads["c2_1"]["asset_version_id"]]["reasons"][0]["fact_ids"], ["f2"])
        # Nothing was handed out and nothing recorded.
        self.assertEqual(self.records(project, "download"), [])
        self.assertEqual(self.count(DownloadIntentRecord), 0)

    def test_after_checking_only_the_requested_ads_are_downloaded(self):
        project = self.checked_ads()
        self.verify_everything(project)
        self.assertEqual(set(self.statuses(project).values()), {"exportable"})
        ads = self.ads(project)
        self.assertTrue(all(ad["reasons"] == [] and ad["appearance_confirmed"] for ad in ads.values()))
        wanted = ["c2_2", "c1_1"]
        downloaded = self.download(project, wanted)
        # Exactly the ads asked for, in the order asked, each with its own short-lived signed link.
        self.assertEqual([d["combination_id"] for d in downloaded["downloads"]], wanted)
        self.assertEqual([d["asset_version_id"] for d in downloaded["downloads"]], [ads[c]["asset_version_id"] for c in wanted])
        for item in downloaded["downloads"]:
            self.assertTrue(item["download_grant"]["url"].startswith("http"))
            self.assertRegex(item["filename"], r"^ad-MK-[0-9A-Z]{6}\.png$")
        with tenant_session(self.context) as session:
            intents = session.scalars(select(DownloadIntentRecord).where(
                DownloadIntentRecord.workspace_id == self.context.workspace_id)).all()
            self.assertEqual(sorted(i.owner_id for i in intents), sorted(ads[c]["asset_version_id"] for c in wanted))
            self.assertEqual({i.owner_kind for i in intents}, {"asset_version"})
            events = session.scalars(select(AuditEventRecord.event_type).where(
                AuditEventRecord.workspace_id == self.context.workspace_id, AuditEventRecord.resource_id == project["id"])).all()
        self.assertIn("marketing.ideas_verified", events)
        self.assertIn("marketing.ideas_downloaded", events)
        record, = self.records(project, "download")
        self.assertEqual([item["asset_version_id"] for item in record.content_json["ads"]],
                         [ads[c]["asset_version_id"] for c in wanted])
        self.assertEqual(record.content_json["ads"][0]["cited_fact_ids"], ["f2"])
        self.assertEqual(record.content_json["ads"][0]["check_id"], ads["c2_2"]["check"]["id"])

    def test_one_draft_among_the_requested_ads_refuses_the_whole_download(self):
        project = self.checked_ads()
        self.verify(project, fact_ids=["f1"], ads=ADS_OF["f1"])
        refused = self.download(project, ["c1_1", "c2_1"], expect=409)
        self.assertEqual([item["asset_version_id"] for item in refused["params"]["ads"]],
                         [self.ads(project)["c2_1"]["asset_version_id"]])
        self.assertEqual(self.count(DownloadIntentRecord), 0)
        self.assertEqual(len(self.download(project, ["c1_1"])["downloads"]), 1)

    def test_checking_some_facts_makes_only_the_ads_that_cite_them_exportable(self):
        project = self.checked_ads()
        everything = [c for cs in ADS_OF.values() for c in cs]
        # Every look and finding is settled; only f1 is checked.
        self.verify(project, fact_ids=["f1"], ads=everything)
        ads = self.ads(project)
        self.assertEqual({c: ads[c]["status"] for c in everything},
                         {"c1_1": "exportable", "c1_2": "exportable", "c2_1": "draft", "c2_2": "draft",
                          "c3_1": "draft", "c3_2": "draft"})
        self.assertEqual([(r["code"], r["fact_ids"]) for r in ads["c2_1"]["reasons"]], [("facts_unverified", ["f2"])])
        # The third claim cites no fact itself: its ads stand on the fact behind the inference it cites (#211).
        self.assertEqual(ads["c3_1"]["cited_fact_ids"], ["f3"])
        self.assertEqual([(r["code"], r["fact_ids"]) for r in ads["c3_1"]["reasons"]], [("facts_unverified", ["f3"])])
        # Only the checked fact is confirmed; the others stay the AI's proposal.
        facts = {f["id"]: f["confirmation"] for f in self.read_ideas(project)["understanding"]["facts"]}
        self.assertEqual(facts, {"f1": "confirmed", "f2": "unconfirmed", "f3": "unconfirmed", "f4": "unconfirmed"})

    def test_checked_facts_alone_are_not_enough_without_the_look_and_the_findings(self):
        project = self.checked_ads()
        self.verify(project, fact_ids=["f1", "f2", "f3"])
        ad = self.ads(project)["c1_1"]
        self.assertEqual((ad["status"], self.reasons(ad)), ("draft", ["appearance_unconfirmed", "findings_open"]))
        self.assertFalse(ad["appearance_confirmed"])


class FactChangeTests(IdeasExportFixtures):
    def test_changing_a_cited_fact_makes_its_ads_need_redoing_and_touches_no_other(self):
        project = self.checked_ads()
        self.verify_everything(project)
        self.edit_facts(project, self.set_fact("f1", statement="Charging can be scheduled for cheaper night hours from the Acme Charge app."))
        ads = self.ads(project)
        self.assertEqual(self.statuses(project), {"c1_1": "needs_redo", "c1_2": "needs_redo", "c2_1": "exportable",
                                                  "c2_2": "exportable", "c3_1": "exportable", "c3_2": "exportable"})
        self.assertEqual([(r["code"], r["fact_ids"]) for r in ads["c1_1"]["reasons"]],
                         [("facts_changed", ["f1"]), ("check_outdated", [])])
        refused = self.download(project, ["c1_1"], expect=409)
        self.assertEqual(refused["params"]["ads"][0]["status"], "needs_redo")
        self.assertEqual(len(self.download(project, ["c2_1", "c3_2"])["downloads"]), 2)
        # The changed fact is unchecked again; the facts nobody touched keep their checking.
        facts = {f["id"]: f["confirmation"] for f in self.read_ideas(project)["understanding"]["facts"]}
        self.assertEqual(facts, {"f1": "unconfirmed", "f2": "confirmed", "f3": "confirmed", "f4": "unconfirmed"})

    def test_a_fact_put_back_as_it_was_needs_checking_again_but_no_new_image(self):
        project = self.checked_ads()
        self.verify_everything(project)
        self.edit_facts(project, self.set_fact("f1", statement="Charging starts by itself at night."))
        self.assertEqual(self.statuses(project)["c1_1"], "needs_redo")
        self.edit_facts(project, self.set_fact("f1", statement=FACTS[0]["statement"]))
        ad = self.ads(project)["c1_1"]
        self.assertEqual((ad["status"], [(r["code"], r["fact_ids"]) for r in ad["reasons"]]),
                         ("draft", [("facts_unverified", ["f1"])]))
        self.verify(project, fact_ids=["f1"])
        self.assertEqual(self.statuses(project)["c1_1"], "exportable")

    def test_changing_a_fact_no_ad_cites_touches_no_ad(self):
        project = self.checked_ads()
        self.verify_everything(project)
        before = self.read_ideas(project)
        edited = self.edit_facts(project, self.set_fact("f4", statement="It is a wall-mounted charger for electric cars."))
        self.assertEqual(edited["revision"], before["revision"] + 1)
        after = self.read_ideas(project)
        self.assertEqual([(ad["asset_version_id"], ad["status"], ad["reasons"]) for ad in after["ads"]],
                         [(ad["asset_version_id"], ad["status"], ad["reasons"]) for ad in before["ads"]])
        self.assertEqual(set(self.statuses(project).values()), {"exportable"})
        self.assertEqual(len(self.download(project, ["c1_1", "c2_1", "c3_1"])["downloads"]), 3)

    def test_removing_a_cited_fact_makes_its_ads_need_redoing(self):
        project = self.checked_ads()
        self.verify_everything(project)

        def without_f3(understanding):
            understanding["facts"] = [fact for fact in understanding["facts"] if fact["id"] != "f3"]
            understanding["inferences"][0]["fact_ids"] = []
        self.edit_facts(project, without_f3)
        ads = self.ads(project)
        self.assertEqual(self.statuses(project), {"c1_1": "exportable", "c1_2": "exportable", "c2_1": "exportable",
                                                  "c2_2": "exportable", "c3_1": "needs_redo", "c3_2": "needs_redo"})
        self.assertEqual([(r["code"], r["fact_ids"]) for r in ads["c3_1"]["reasons"]],
                         [("facts_removed", ["f3"]), ("check_outdated", [])])

    def test_a_claim_that_still_cites_a_removed_fact_stays_unusable_for_later_images(self):
        """The ideas are not rewritten when a fact goes, so an image made afterwards from the same claim
        would cite one fact fewer; the claim's own dangling citation still stops it."""
        project = self.checked_ads()
        self.verify_everything(project)

        def without_f2(understanding):
            understanding["facts"] = [fact for fact in understanding["facts"] if fact["id"] != "f2"]
        self.edit_facts(project, without_f2)
        ad = self.ads(project)["c2_1"]
        with tenant_session(self.context) as session:  # as an image made after the removal would be recorded
            version = session.get(AssetVersionRecord, ad["asset_version_id"])
            item = session.get(RunItemRecord, version.producing_run_item_id)
            from app.adapters.persistence.models import GenerationRunRecord
            recipe = session.get(GenerationRecipeSnapshotRecord, session.get(GenerationRunRecord, item.generation_run_id).recipe_snapshot_id)
            canonical = deepcopy(recipe.canonical_json)
            intent = canonical["selection"]["marketing_intent"]
            intent["cited_fact_ids"] = []
            intent["understanding"]["facts"] = [f for f in intent["understanding"]["facts"] if f["id"] != "f2"]
            recipe.canonical_json = canonical
        ad = self.ads(project)["c2_1"]
        self.assertEqual(ad["status"], "needs_redo")
        self.assertIn(("facts_removed", ["f2"]), [(r["code"], r["fact_ids"]) for r in ad["reasons"]])
        # The id comes back as another fact: the claim cites it, this image was never made on it.

        def another_f2(understanding):
            understanding["facts"].append({"id": "f2", "statement": "It is a wall charger for electric cars.",
                                           "source_quote": FACTS[3]["source_quote"]})
        self.edit_facts(project, another_f2)
        self.verify(project, fact_ids=["f2"])
        ad = self.ads(project)["c2_1"]
        self.assertEqual(ad["status"], "needs_redo")
        self.assertIn(("facts_changed", ["f2"]), [(r["code"], r["fact_ids"]) for r in ad["reasons"]])
        self.assertNotIn("facts_unverified", self.reasons(ad))
        self.download(project, ["c2_1"], expect=409)

    def test_an_inference_pointed_at_another_fact_makes_the_ads_that_cite_it_need_redoing(self):
        project = self.checked_ads()
        self.verify_everything(project)

        def onto_f4(understanding):
            understanding["inferences"][0]["fact_ids"] = ["f4"]
        self.edit_facts(project, onto_f4)
        ads = self.ads(project)
        # The third claim now rests on f4, which its images were not made on and nobody checked, and no longer
        # on f3, which they were made on (#224: an image stands on the facts its claim names now, no fewer).
        self.assertEqual((ads["c3_1"]["status"], [(r["code"], r["fact_ids"]) for r in ads["c3_1"]["reasons"]]),
                         ("needs_redo", [("ideas_changed", []), ("facts_changed", ["f4"])]))
        self.assertEqual((ads["c1_1"]["status"], ads["c2_1"]["status"]), ("exportable", "exportable"))

    def test_a_claim_moved_onto_other_facts_in_the_same_words_makes_its_ads_need_redoing(self):
        """An ad's content does not include which facts its claim cites, so redrafted ideas can keep the
        words and change the facts; the images made before stand on other facts."""
        project = self.checked_ads()
        self.verify_everything(project)
        with tenant_session(self.context) as session:
            row = session.get(MarketingProjectRecord, project["id"])
            doc = deepcopy(row.ideas_json)
            doc["angles"][0]["fact_ids"] = ["f1", "f4"]
            row.ideas_json = doc
        ads = self.ads(project)
        self.assertEqual((ads["c1_1"]["status"], [(r["code"], r["fact_ids"]) for r in ads["c1_1"]["reasons"]]),
                         ("needs_redo", [("facts_changed", ["f4"])]))
        self.assertEqual(ads["c2_1"]["status"], "exportable")

    def test_editing_never_checks_a_fact(self):
        project = self.checked_ads()
        edited = self.edit_facts(project, self.set_fact("f2", confirmation="confirmed"))
        self.assertEqual({f["id"]: f["confirmation"] for f in edited["understanding"]["facts"]}["f2"], "unconfirmed")
        # Nothing changed, so no revision was spent.
        self.assertEqual(edited["revision"], project["revision"])

    def test_a_new_facts_quote_must_be_in_the_source_or_come_with_new_material(self):
        project = self.checked_ads()
        quote = "Charges at up to 11 kW on three-phase power."

        def add(understanding):
            understanding["facts"].append({"id": "f5", "statement": "It charges at up to 11 kW.", "source_quote": quote})
        refused = self.edit_facts(project, add, expect=422)
        self.assertEqual(refused["code"], "fact_source_missing")
        edited = self.edit_facts(project, add, source_addition=f"From the specification sheet:\n{quote}")
        added = next(fact for fact in edited["understanding"]["facts"] if fact["id"] == "f5")
        self.assertEqual(added["confirmation"], "unconfirmed")
        with tenant_session(self.context) as session:
            row = session.get(MarketingProjectRecord, project["id"])
            self.assertTrue(row.source_text.endswith(quote))
            self.assertIn("Schedule charging from the Acme Charge app", row.source_text)
            revision = session.scalar(select(MarketingRevisionRecord).where(
                MarketingRevisionRecord.project_id == project["id"], MarketingRevisionRecord.revision == edited["revision"]))
            self.assertEqual((revision.reason, revision.content_json["source_text"]), ("understanding_edited", row.source_text))
        self.assertEqual(set(self.statuses(project).values()), {"draft"})

    def test_the_edit_needs_the_current_revision(self):
        project = self.checked_ads()
        change = self.set_fact("f4", statement="It is a wall-mounted charger for electric cars.")
        self.edit_facts(project, change)
        stale = self.edit_facts(project, self.set_fact("f4", statement="It is a charger."), expect=409, revision=project["revision"])
        self.assertEqual(stale["code"], "stale_marketing_revision")


class IdeasContentTests(IdeasExportFixtures):
    def test_an_image_whose_content_is_no_longer_in_the_ideas_needs_redoing(self):
        """Redrafted ideas reuse the same combination ids (#212): the old image still hangs on that id, so it
        is marked as needing redoing and cannot be downloaded as the new claim's ad."""
        project = self.checked_ads()
        self.verify_everything(project)
        self.assertEqual(set(self.statuses(project).values()), {"exportable"})
        current = self.read_ideas(project)
        with patch("app.marketing.ideas.example", side_effect=lambda inputs: claims(inputs, headline="Charge while you sleep")):
            self.execute(self.drafted_call(current, "ideas"))
        redrafted = self.read_ideas(project)
        self.assertEqual([c["id"] for c in redrafted["ideas"]["combinations"]], [c["id"] for c in current["ideas"]["combinations"]])
        self.assertEqual(len(redrafted["ads"]), 6)
        for ad in redrafted["ads"]:
            self.assertEqual((ad["status"], self.reasons(ad)), ("needs_redo", ["ideas_changed"]))
        refused = self.download(project, ["c1_1"], expect=409)
        self.assertEqual(self.reasons(refused["params"]["ads"][0]), ["ideas_changed"])

    def test_drafting_the_understanding_again_leaves_every_old_image_to_redo(self):
        project = self.checked_ads()
        self.verify_everything(project)
        self.execute(self.drafted_call(self.read_ideas(project), "understanding"))
        after = self.read_ideas(project)
        self.assertEqual((after["state"], after["ideas"]), ("understanding_draft", None))
        self.assertEqual({ad["status"] for ad in after["ads"]}, {"needs_redo"})
        self.assertTrue(all(f["confirmation"] == "unconfirmed" for f in after["understanding"]["facts"]))


class CheckAndImageTests(IdeasExportFixtures):
    def test_an_ad_waiting_for_its_check_or_without_a_usable_one_is_a_draft(self):
        project = self.made("mock")
        self.verify(project, fact_ids=["f1"], ads=["c1_1"])
        ad = self.ads(project)["c1_1"]
        self.assertEqual((ad["status"], self.reasons(ad)), ("draft", ["check_missing"]))
        self.start_checks(project)
        self.assertEqual(self.reasons(self.ads(project)["c1_1"]), ["check_in_progress"])
        for state, code in (("uncertain", "check_uncertain"), ("failed", "check_failed")):
            with tenant_session(self.context) as session:
                session.scalar(select(MarketingAIQCRecord).where(
                    MarketingAIQCRecord.asset_version_id == ad["asset_version_id"])).state = state
            ad = self.ads(project)["c1_1"]
            self.assertEqual((ad["status"], self.reasons(ad)), ("draft", [code]))
        self.download(project, ["c1_1"], expect=409)

    def test_an_image_its_generation_check_stopped_needs_redoing_and_one_still_being_checked_waits(self):
        project = self.checked_ads()
        self.verify_everything(project)
        ad = self.ads(project)["c1_1"]

        def item_state(state):
            with tenant_session(self.context) as session:
                version = session.get(AssetVersionRecord, ad["asset_version_id"])
                session.get(RunItemRecord, version.producing_run_item_id).state = state
        item_state("failed")
        stopped = self.ads(project)["c1_1"]
        self.assertEqual((stopped["status"], self.reasons(stopped)), ("needs_redo", ["image_blocked"]))
        self.assertEqual(self.download(project, ["c1_1"], expect=409)["params"]["ads"][0]["status"], "needs_redo")
        item_state("qc_pending")
        waiting = self.ads(project)["c1_1"]
        self.assertEqual((waiting["status"], self.reasons(waiting)), ("draft", ["image_check_in_progress"]))
        # The ad is still listed while it waits or after it was stopped (#211).
        self.assertEqual(len(self.read_ideas(project)["ads"]), 6)
        self.assertEqual(self.statuses(project)["c1_2"], "exportable")

    def test_a_finding_is_handled_only_by_accepting_it_on_the_check_the_user_saw(self):
        project = self.made("byok")
        self.start_checks(project, "byok")

        async def misspelled(provider, model, credential, inputs, images):
            headline = inputs["combination"]["headline"]
            return check_report(inputs, rendered_text={"headline": headline.replace("product", "prodcut")})
        self.run_checks(project, AsyncMock(side_effect=misspelled))
        ad = self.ads(project)["c1_1"]
        self.assertEqual(sorted(f["check_id"] for f in ad["check"]["findings"]), ["copy_consistent", "product_fidelity"])
        # Confirming the look settles what the check could not judge without a product photo, nothing else.
        self.verify(project, fact_ids=["f1"], ads=["c1_1"], findings=[])
        ad = self.ads(project)["c1_1"]
        self.assertEqual(ad["status"], "draft")
        self.assertEqual([(r["code"], r["finding_ids"]) for r in ad["reasons"]], [("findings_open", ["copy_consistent"])])
        accept = {"asset_version_id": ad["asset_version_id"], "check_id": ad["check"]["id"], "finding_ids": ["copy_consistent"]}
        changed = self.verify(project, findings=[{**accept, "check_id": "qc_another"}], expect=409)
        self.assertEqual(changed["code"], "check_changed")
        unknown = self.verify(project, findings=[{**accept, "finding_ids": ["language_correct"]}], expect=422)
        self.assertEqual(unknown["code"], "finding_not_found")
        self.assertEqual(self.ads(project)["c1_1"]["status"], "draft")
        self.verify(project, findings=[accept])
        self.assertEqual(self.ads(project)["c1_1"]["status"], "exportable")
        # The other ad of the claim has the same finding and was not accepted.
        other = self.ads(project)["c1_2"]
        self.assertEqual((other["status"], self.reasons(other)), ("draft", ["appearance_unconfirmed", "findings_open"]))
        record = self.records(project, "verification")[-1]
        self.assertEqual(record.content_json["findings"], [accept])

    def test_a_real_check_without_a_photo_leaves_only_the_look_for_the_user(self):
        project = self.checked_ads("byok")
        ad = self.ads(project)["c2_1"]
        self.assertEqual([f["check_id"] for f in ad["check"]["findings"]], ["product_fidelity"])
        self.verify(project, fact_ids=["f2"], findings=[])
        self.assertEqual(self.reasons(self.ads(project)["c2_1"]), ["appearance_unconfirmed", "findings_open"])
        self.verify(project, ads=["c2_1"], findings=[])
        ad = self.ads(project)["c2_1"]
        self.assertEqual((ad["status"], ad["appearance_confirmed"]), ("exportable", True))


class PhotoTests(IdeasExportFixtures):
    def with_photo(self, project, combination_id, attestation):
        """As if the image request had sent a product photo: the recipe names it (photos in drafts come with #199)."""
        asset_id = self.ads(project)[combination_id]["asset_version_id"]
        with tenant_session(self.context) as session:
            from app.adapters.persistence.models import GenerationRunRecord
            item = session.get(RunItemRecord, session.get(AssetVersionRecord, asset_id).producing_run_item_id)
            recipe = session.get(GenerationRecipeSnapshotRecord, session.get(GenerationRunRecord, item.generation_run_id).recipe_snapshot_id)
            canonical = deepcopy(recipe.canonical_json)
            canonical["selection"]["product_references"] = [{"reference_version_id": "libver_photo", "content_hash": "h",
                "role": "product_reference", "asset_version_id": "libver_photo", "content_sha256": "s", "attestation": attestation}]
            recipe.canonical_json = canonical
        return asset_id

    def test_an_ad_that_used_a_checked_photo_needs_no_confirmation_of_its_look(self):
        project = self.checked_ads()
        self.verify_everything(project)
        asset_id = self.with_photo(project, "c1_1", "verified_physical_product")
        with patch("app.marketing.ideas_export.references_current", return_value=True):
            ad = self.ads(project)["c1_1"]
            self.assertEqual((ad["status"], ad["used_product_photo"]), ("exportable", True))
            refused = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/verification",
                                       headers=self.headers(self.read_ideas(project)["revision"]),
                                       json={"appearance_confirmed": [asset_id]})
        self.assertEqual((refused.status_code, refused.json()["code"]), (422, "appearance_confirmation_not_needed"))

    def test_an_unchecked_photo_keeps_the_ad_a_draft_and_an_unusable_one_needs_redoing(self):
        project = self.checked_ads()
        self.verify_everything(project)
        self.with_photo(project, "c1_1", "unverified_product_photo")
        with patch("app.marketing.ideas_export.references_current", return_value=True):
            ad = self.ads(project)["c1_1"]
            self.assertEqual((ad["status"], self.reasons(ad)), ("draft", ["photo_unverified"]))
        # The photo named by the recipe is not a usable reference of this workspace: withdrawn, in effect.
        ad = self.ads(project)["c1_1"]
        self.assertEqual((ad["status"], self.reasons(ad)), ("needs_redo", ["photo_unavailable"]))
        self.assertEqual(self.statuses(project)["c1_2"], "exportable")


class ProductPhotoSetTests(IdeasGenerationFixtures, ProductPhotoFixtures):
    """An ad made with the product's own checked photos (real photo set, real generation), not a planted recipe."""
    MODEL = "Acme Charge 65W"

    def setUp(self):
        super().setUp()
        _, hero = self._reference_version(self.context, name="Legacy hero")
        _, group = self._group_version(self.context, hero["id"])
        self.entry_id, _ = self._publish(self.context, ENTRY_PRODUCT, "Acme Charge", self._product_content(
            hero["id"], group["id"], model_variants=[{"label": self.MODEL, "specs": {}}]))
        with tenant_session(self.context) as session:
            self.product_id = session.scalar(select(ProductRecord.id).where(ProductRecord.product_profile_entry_id == self.entry_id))

    def references(self, method, body, item_id=None):
        path = f"/api/v1/products/{self.product_id}/references"
        response = self.client.request(method, f"{path}/{item_id}" if item_id else path, json=body, headers={
            "Idempotency-Key": new_id("key"), "If-Match": str(self.client.get(path).json()["revision"])})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def ad_with_photo(self):
        photo, _ = self.photo()
        bound = self.references("POST", {"reference_version_id": photo, "model_variant": self.MODEL, "role": "primary",
                                         "identity_status": "verified_physical", "note": "Studio photo of the exact unit"})
        item = next(i for i in bound["items"] if i["reference_version_id"] == photo)
        response = self.client.post("/api/v1/marketing/ideas", headers=self.headers(), json={
            "product_id": self.entry_id, "source_text": SOURCE, "market": "United States", "ad_language": "en-US"})
        self.assertEqual(response.status_code, 201, response.text)
        project = response.json()
        self.execute(self.drafted_call(project, "understanding"))
        self.execute(self.drafted_call(self.read_ideas(project), "ideas"))
        project = self.read_ideas(project)
        self.execute_items(self.submitted(self.prepared(project)["drafts"][0]["draft_id"]))
        return project, item

    def reasons(self, project):
        (ad,) = self.read_ideas(project)["ads"]
        return ad, [reason["code"] for reason in ad["reasons"]]

    def test_a_checked_photo_satisfies_the_photo_condition_until_the_user_rejects_it(self):
        project, item = self.ad_with_photo()
        ad, reasons = self.reasons(project)
        self.assertTrue(ad["used_product_photo"])
        # No look to confirm and nothing wrong with the photo: only the facts and the check are left.
        self.assertEqual((ad["status"], reasons), ("draft", ["facts_unverified", "check_missing"]))
        # The user marks the photo as not a verified photo of the real product: the file is untouched,
        # the ad that used it has to be made again.
        self.references("PATCH", {"identity_status": "unverified"}, item["item_id"])
        ad, reasons = self.reasons(project)
        self.assertEqual((ad["status"], reasons), ("needs_redo", ["facts_unverified", "photo_unavailable", "check_missing"]))

    def test_taking_the_photo_off_the_product_rejects_it_too(self):
        project, item = self.ad_with_photo()
        self.references("DELETE", None, item["item_id"])
        ad, reasons = self.reasons(project)
        self.assertEqual((ad["status"], "photo_unavailable" in reasons), ("needs_redo", True))


class ProductVersionTests(IdeasExportFixtures):
    def test_only_what_the_user_checked_is_published_as_the_product_version(self):
        project = self.checked_ads()
        versions, draft = self.product_versions(project)
        self.assertEqual(versions, [])
        proposal, revision = deepcopy(draft.content_json), draft.revision
        self.assertTrue(proposal["must_match"] and proposal["specs"])
        self.verify(project, fact_ids=["f1"])
        (first,), draft = self.product_versions(project)
        self.assertEqual(first.content_json, {
            "name": "Acme Charge Home", "description": FACTS[0]["statement"], "selling_points": [FACTS[0]["statement"]],
            "audience": "United States", "source_url": "https://acmecharge.example/products/home"})
        self.assertEqual((first.state, first.origin_command), ("effective", "ideas_verification"))
        # The AI's proposal -- specs, buyers, appearance requirements -- stays in the Draft, unpublished and unchanged.
        self.assertEqual((draft.content_json, draft.revision), (proposal, revision))
        self.assertEqual(self.records(project, "verification")[-1].library_version_id, first.id)
        # Checking more facts makes a new version; the earlier one stays as history.
        self.verify(project, fact_ids=["f3"])
        (first, second), _ = self.product_versions(project)
        self.assertEqual((first.state, second.state), ("superseded", "effective"))
        self.assertEqual(second.content_json["selling_points"], [FACTS[0]["statement"], FACTS[2]["statement"]])
        self.assertEqual(second.base_version_id, first.id)
        with tenant_session(self.context) as session:
            entry = session.get(LibraryEntryRecord, first.entry_id)
            self.assertEqual((entry.current_version_id, entry.lifecycle), (second.id, "active"))
        record = self.records(project, "verification")[-1]
        self.assertEqual(record.content_json["facts"], [{"id": "f3", "hash": ideas_export.fact_hash(FACTS[2])}])

    def test_a_checked_fact_that_is_changed_leaves_the_published_version(self):
        project = self.checked_ads()
        self.verify(project, fact_ids=["f1", "f2"])
        self.edit_facts(project, self.set_fact("f1", statement="Charging starts by itself at night."))
        versions, _ = self.product_versions(project)
        self.assertEqual([v.state for v in versions], ["superseded", "effective"])
        self.assertEqual(versions[-1].content_json["selling_points"], [FACTS[1]["statement"]])
        self.assertEqual(versions[-1].origin_command, "ideas_verification")

    def test_a_withdrawn_verified_version_is_not_published_again(self):
        """Withdrawing a version is a person's decision (creative-library contract §6): checking more facts
        afterwards does not bring the product's published content back."""
        project = self.checked_ads()
        self.verify(project, fact_ids=["f1"])
        (first,), _ = self.product_versions(project)
        with tenant_session(self.context) as session:
            session.get(LibraryVersionRecord, first.id).state = "withdrawn"
        self.verify(project, fact_ids=["f2"])
        (only,), _ = self.product_versions(project)
        self.assertEqual((only.id, only.state), (first.id, "withdrawn"))
        self.assertIsNone(self.records(project, "verification")[-1].library_version_id)
        # The facts are checked all the same.
        facts = {f["id"]: f["confirmation"] for f in self.read_ideas(project)["understanding"]["facts"]}
        self.assertEqual((facts["f1"], facts["f2"]), ("confirmed", "confirmed"))

    def test_another_project_of_the_product_never_drops_what_this_one_checked(self):
        project = self.checked_ads()
        self.verify(project, fact_ids=["f1", "f2"])
        (first,), _ = self.product_versions(project)
        response = self.client.post("/api/v1/marketing/ideas", headers=self.headers(), json={
            "product_id": project["product_id"], "source_text": SOURCE, "market": "Germany", "ad_language": "de-DE"})
        self.assertEqual(response.status_code, 201, response.text)
        other = response.json()
        self.execute(self.drafted_call(other, "understanding"))
        other = self.read_ideas(other)
        (fact,) = other["understanding"]["facts"]
        # Editing a fact there, with nothing checked there, leaves the product's version as it is.
        self.edit_facts(other, lambda understanding: understanding.update(audience="Drivers with a garage"))
        (still,), _ = self.product_versions(project)
        self.assertEqual((still.id, still.state), (first.id, "effective"))
        # Checking a fact there adds it: the version holds what both projects checked, and both markets.
        self.verify(other, fact_ids=[fact["id"]])
        versions, _ = self.product_versions(project)
        self.assertEqual(versions[-1].content_json["selling_points"],
                         [FACTS[0]["statement"], FACTS[1]["statement"], fact["statement"]])
        self.assertEqual(versions[-1].content_json["audience"], "United States, Germany")

    def test_asking_for_a_new_understanding_takes_this_projects_facts_out_of_the_version(self):
        """A new understanding comes back unchecked; the published version must not keep saying otherwise."""
        project = self.checked_ads()
        self.verify(project, fact_ids=["f1", "f2"])
        call = self.drafted_call(self.read_ideas(project), "understanding")
        versions, _ = self.product_versions(project)
        self.assertEqual([v.state for v in versions], ["superseded", "effective"])
        self.assertEqual((versions[-1].content_json["selling_points"], versions[-1].content_json["description"]),
                         ([], "Acme Charge Home"))
        self.execute(call)
        after = self.read_ideas(project)
        self.assertTrue(all(fact["confirmation"] == "unconfirmed" for fact in after["understanding"]["facts"]))
        self.assertEqual(len(self.product_versions(project)[0]), 2)

    def test_continuing_a_stopped_first_batch_with_more_material_takes_its_checked_facts_out_of_the_version(self):
        """More material means a new understanding, drafted from scratch and unchecked (#193): what the user
        had checked in the old one must leave the product's published version when they continue."""
        with patch("app.marketing.ideas.example", side_effect=one_claim):
            stopped = self.started_and_drained()
        self.assertEqual((stopped["state"], stopped["attention"]["reason"]), ("needs_attention", "too_few_claims"))
        project = self.read_ideas({"id": stopped["project_id"]})
        self.verify(project, fact_ids=["f1"])
        (first,), _ = self.product_versions(project)
        self.assertEqual(first.content_json["selling_points"], [FACTS[0]["statement"]])
        resumed = self.resume(stopped, additional_text="Acme Charge Home fits a 32 A home circuit.")
        self.assertEqual(resumed["state"], "understanding")
        versions, _ = self.product_versions(project)
        self.assertEqual([version.state for version in versions], ["superseded", "effective"])
        self.assertEqual((versions[-1].content_json["selling_points"], versions[-1].content_json["description"]),
                         ([], "Acme Charge Home"))
        # The batch goes on as before: a new understanding, every fact unchecked, and no further version.
        self.drain()
        self.assertEqual(self.batch(stopped["id"])["state"], "done")
        after = self.read_ideas(project)
        self.assertTrue(all(fact["confirmation"] == "unconfirmed" for fact in after["understanding"]["facts"]))
        self.assertEqual(len(self.product_versions(project)[0]), 2)

    def test_confirming_only_looks_and_findings_publishes_nothing(self):
        project = self.checked_ads()
        self.verify(project, ads=["c1_1"])
        self.assertEqual(self.product_versions(project)[0], [])
        self.assertIsNone(self.records(project, "verification")[-1].library_version_id)

    def test_a_product_card_the_user_published_is_left_alone(self):
        """A product the user confirmed and published from its card keeps that version: a verification
        publishes only where nothing but an earlier verification was published."""
        card = {"name": "Acme Charge Home", "description": "Wall charger with app scheduling.", "audience": "Drivers who charge at home",
                "selling_points": ["Schedule charging from the app"], "specs": {"Enclosure": "IP55"}}
        entry = library.create_entry(self.context, entry_type=ENTRY_PRODUCT, display_name="Acme Charge Home", content=card)
        published = library.publish(self.context, entry["id"], expected_revision=1)
        response = self.client.post("/api/v1/marketing/ideas", headers=self.headers(), json={
            "product_id": entry["id"], "source_text": SOURCE, "market": "United States", "ad_language": "en-US"})
        self.assertEqual(response.status_code, 201, response.text)
        project = response.json()
        self.execute(self.drafted_call(project, "understanding"))
        project = self.read_ideas(project)
        fact_id = project["understanding"]["facts"][0]["id"]
        verified = self.verify(project, fact_ids=[fact_id])
        self.assertEqual(verified["understanding"]["facts"][0]["confirmation"], "confirmed")
        (still,), _ = self.product_versions(project)
        self.assertEqual((still.id, still.state, still.content_json), (published["id"], "effective", card))
        self.assertIsNone(self.records(project, "verification")[-1].library_version_id)
        # Withdrawn by its owner, it is still theirs to republish: checking a fact does not bring the product back.
        with tenant_session(self.context) as session:
            session.get(LibraryVersionRecord, published["id"]).state = "withdrawn"
        self.execute(self.drafted_call(self.read_ideas(project), "understanding"))
        self.verify(project, fact_ids=[fact_id])
        (still,), _ = self.product_versions(project)
        self.assertEqual((still.id, still.state), (published["id"], "withdrawn"))

    def test_a_product_without_a_library_entry_is_verified_without_publishing(self):
        project = self.understood_ideas()  # a plain product row, as tests and older data have
        self.verify(project, fact_ids=[project["understanding"]["facts"][0]["id"]])
        self.assertIsNone(self.records(project, "verification")[-1].library_version_id)
        self.assertEqual(self.count(LibraryVersionRecord), 0)


class CommandTests(IdeasExportFixtures):
    def test_verification_needs_the_current_revision_and_happens_once_per_key(self):
        project = self.checked_ads()
        key = new_id("key")
        first = self.verify(project, fact_ids=["f1"], key=key)
        self.assertEqual(first["revision"], project["revision"] + 1)
        replay = self.verify(project, fact_ids=["f1"], key=key, revision=project["revision"])
        self.assertEqual(replay["revision"], first["revision"])
        self.assertEqual(len(self.records(project, "verification")), 1)
        stale = self.verify(project, fact_ids=["f2"], expect=409, revision=project["revision"])
        self.assertEqual(stale["code"], "stale_marketing_revision")
        with tenant_session(self.context) as session:
            revision = session.scalar(select(MarketingRevisionRecord).where(
                MarketingRevisionRecord.project_id == project["id"], MarketingRevisionRecord.revision == first["revision"]))
            self.assertEqual(revision.reason, "verified")

    def test_verification_refuses_what_it_cannot_record(self):
        project = self.checked_ads()
        self.assertEqual(self.verify(project, expect=422)["code"], "verification_empty")
        missing = self.verify(project, fact_ids=["f9"], expect=422)
        self.assertEqual((missing["code"], missing["params"]["fact_ids"]), ("fact_not_found", ["f9"]))
        response = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/verification",
                                    headers=self.headers(project["revision"]), json={"appearance_confirmed": ["av_other"]})
        self.assertEqual((response.status_code, response.json()["code"]), (404, "ad_not_found"))
        self.assertEqual(self.records(project, "verification"), [])
        self.assertEqual(self.read_ideas(project)["revision"], project["revision"])

    def test_the_same_download_key_returns_the_same_download_and_a_changed_ad_is_refused_even_then(self):
        project = self.checked_ads()
        self.verify_everything(project)
        key = new_id("key")
        first = self.download(project, ["c1_1", "c1_2"], key=key)
        again = self.download(project, ["c1_1", "c1_2"], key=key)
        self.assertEqual((again["id"], [d["intent_id"] for d in again["downloads"]]),
                         (first["id"], [d["intent_id"] for d in first["downloads"]]))
        self.assertEqual((len(self.records(project, "download")), self.count(DownloadIntentRecord)), (1, 2))
        self.edit_facts(project, self.set_fact("f1", statement="Charging starts by itself at night."))
        refused = self.download(project, ["c1_1", "c1_2"], expect=409, key=key)
        self.assertEqual(refused["code"], "ads_not_exportable")

    def test_a_download_names_each_ad_once_and_only_ads_of_these_ideas(self):
        project = self.checked_ads()
        asset_id = self.ads(project)["c1_1"]["asset_version_id"]
        url = f"/api/v1/marketing/ideas/{project['id']}/download-intent"
        self.assertEqual(self.client.post(url, headers=self.headers(), json={"asset_version_ids": [asset_id, asset_id]}).status_code, 422)
        self.assertEqual(self.client.post(url, headers=self.headers(), json={"asset_version_ids": []}).status_code, 422)
        foreign = self.client.post(url, headers=self.headers(), json={"asset_version_ids": ["av_other"]})
        self.assertEqual((foreign.status_code, foreign.json()["code"]), (404, "ad_not_found"))

    def test_the_service_reports_one_ads_status(self):
        project = self.checked_ads()
        asset_id = self.ads(project)["c1_1"]["asset_version_id"]
        with tenant_session(self.context) as session:
            row = ideas.get_ideas_project(session, self.context, project["id"])
            self.assertEqual(ideas_export.ad_status(session, self.context, row, asset_id)["status"], "draft")
            with self.assertRaises(ideas_export.MarketingError) as refused:
                ideas_export.ad_status(session, self.context, row, "av_other")
        self.assertEqual(refused.exception.code, "ad_not_found")


class BoundaryTests(IdeasExportFixtures):
    def test_another_workspace_can_neither_read_edit_verify_nor_download(self):
        project = self.checked_ads()
        self.verify(project, fact_ids=["f1"], ads=ADS_OF["f1"])
        verified = self.read_ideas(project)
        asset_id = self.ads(project)["c1_1"]["asset_version_id"]
        owner = self.context
        self.context = self._make_context()
        base = f"/api/v1/marketing/ideas/{project['id']}"
        attempts = (
            self.client.get(base),
            self.client.patch(f"{base}/understanding", headers=self.headers(verified["revision"]),
                              json={"understanding": verified["understanding"]}),
            self.client.post(f"{base}/verification", headers=self.headers(verified["revision"]), json={"fact_ids": ["f2"]}),
            self.client.post(f"{base}/download-intent", headers=self.headers(), json={"asset_version_ids": [asset_id]}),
        )
        for response in attempts:
            self.assertEqual((response.status_code, response.json()["code"]), (404, "marketing_project_not_found"), response.text)
        with tenant_session(self.context) as session:
            for model in (MarketingIdeasExportRecord, DownloadIntentRecord, LibraryVersionRecord):
                self.assertEqual(session.scalar(select(func.count()).select_from(model).where(
                    model.workspace_id == self.context.workspace_id)), 0)
            # Its own session cannot reach the other workspace's project through the service either.
            with self.assertRaises(ideas_export.MarketingError) as refused:
                ideas.get_ideas_project(session, self.context, project["id"])
            self.assertEqual(refused.exception.code, "marketing_project_not_found")
        self.context = owner
        after = self.read_ideas(project)
        self.assertEqual((after["revision"], after["understanding"]), (verified["revision"], verified["understanding"]))
        self.assertEqual(len(self.records(project, "verification")), 1)
        self.assertEqual(self.records(project, "download"), [])

    def test_a_plan_first_project_has_none_of_these_commands(self):
        old = self.create()
        base = f"/api/v1/marketing/ideas/{old['id']}"
        understanding = planner.example_result("understanding", {"source_text": "Acme Charge Home"})
        attempts = (
            self.client.patch(f"{base}/understanding", headers=self.headers(old["revision"]), json={"understanding": understanding}),
            self.client.post(f"{base}/verification", headers=self.headers(old["revision"]), json={"fact_ids": ["f1"]}),
            self.client.post(f"{base}/download-intent", headers=self.headers(), json={"asset_version_ids": ["av_1"]}),
        )
        for response in attempts:
            self.assertEqual((response.status_code, response.json()["code"]), (404, "marketing_project_not_found"), response.text)
        # The plan-first project still confirms its understanding the old way.
        self.assertEqual(self.client.get(f"/api/v1/marketing/projects/{old['id']}").json()["revision"], old["revision"])


class ExportRecordsMigrationDowngradeTests(unittest.TestCase):
    """Dropping the records would lose who checked which fact, so downgrade refuses while any exists."""
    ROOT = Path(__file__).resolve().parents[1]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="marketai-ideas-export-migration-")
        self.addCleanup(self.temp.cleanup)
        self.database = f"{self.temp.name}/migration.db"
        self.environment = {**__import__("os").environ, "MARKETAI_DATABASE_URL": f"sqlite:///{self.database}",
                            "MARKETAI_DATA_DIR": f"{self.temp.name}/data"}
        upgraded = self.alembic("upgrade", "head")
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)

    def alembic(self, *arguments):
        return subprocess.run([sys.executable, "-m", "alembic", *arguments], cwd=self.ROOT, env=self.environment,
                              capture_output=True, text=True, check=False)

    def tables(self):
        with sqlite3.connect(self.database) as connection:
            return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

    def test_downgrade_without_records_drops_the_table(self):
        self.assertIn("marketing_ideas_export_records", self.tables())
        downgraded = self.alembic("downgrade", "zzo197a1b2c3")
        self.assertEqual(downgraded.returncode, 0, downgraded.stderr)
        self.assertNotIn("marketing_ideas_export_records", self.tables())

    def test_downgrade_refuses_while_a_verification_exists(self):
        with sqlite3.connect(self.database) as connection:
            connection.execute("INSERT INTO marketing_projects (id, workspace_id, revision, state, source_text, flow, created_at, updated_at)"
                               " VALUES ('mp_1', 'ws_1', 1, 'ideas_ready', 'Acme Charge', 'ads_first', '2026-11-17', '2026-11-17')")
            connection.execute("INSERT INTO marketing_ideas_export_records (id, workspace_id, project_id, kind, project_revision,"
                               " content_json, content_hash, created_at) VALUES ('mie_1', 'ws_1', 'mp_1', 'verification', 2,"
                               " '{}', 'h', '2026-11-17')")
        downgraded = self.alembic("downgrade", "zzo197a1b2c3")
        self.assertNotEqual(downgraded.returncode, 0)
        self.assertIn("verifications or downloads exist", downgraded.stderr)
        self.assertIn("marketing_ideas_export_records", self.tables())
