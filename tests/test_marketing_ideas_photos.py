"""Unchecked product photos make drafts; checking them needs no new image (#199; ADR 0018 §2-§3, design doc §5.2, §5.3, §6).

The fictional Acme Charge Home page shows four pictures: the main photo, a close-up, another angle and a scene
in use. The first batch reads it, sends the three photos that show the product's look (the scene in use never
stands in for it) with every image, and the ads are drafts until the user checks those photos. Checking a photo
makes the very same image exportable; rejecting it makes every image that used it need making again. Everything
runs through the real endpoints and the real Worker code, with a fake page, a fake model and a fake image
provider; no paid call is made.
"""
import hashlib
import json
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from sqlalchemy import func, select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (AssetVersionRecord, GenerationRunRecord, LibraryVersionRecord, ProductRecord,
                                            ReferenceObjectRecord)
from app.generation.models import GenerationDraftRecord, GenerationRecipeSnapshotRecord, RunItemRecord
from app.marketing import ideas, ideas_export, product_references
from app.marketing.ideas_contracts import IdeasProjectDetail
from app.marketing.models import MarketingIdeasExportRecord
from app.marketing.workflow_models import MarketingFeedbackDraftRecord
from app.products import intake_extraction as extraction
from app.products.reference_sets import published
from app.providers.adapter import ProviderCallResult
from app.providers.base import reference_delivery_metadata
from app.providers.openai_chat import OpenAIChatAdapter
from tests.support.library_fixtures import png_bytes
from tests.test_marketing_first_batch import FakeImages, PAGE, card, check_report
from tests.test_marketing_ideas_checks import IMAGE_JOBS
from tests.test_marketing_ideas_export import ADS_OF
from tests.test_marketing_ideas_revisions import RevisionFixtures

#: The Acme Charge page with its pictures; the text (and so every fact's quote) is the plain page's.
PHOTO_PAGE = PAGE.replace(
    "</head>", '<meta property="og:image" content="https://acmecharge.example/media/main.png"></head>').replace(
    "</body>", '<img src="/media/closeup.png" alt="Charging port" width="480" height="480">'
               '<img src="/media/side.png" alt="Side view" width="480" height="480">'
               '<img src="/media/driveway.png" alt="In the driveway" width="480" height="480"></body>')
PICTURES = {"/media/main.png": (20, 20, 20), "/media/closeup.png": (30, 200, 30), "/media/side.png": (200, 30, 30),
            "/media/driveway.png": (30, 30, 200)}
#: What the reading takes each picture for, in the order the page offers them.
ROLES = ("main", "detail", "angle", "in_use")
UNVERIFIED = product_references.UNVERIFIED


def photo_card():
    """The model's reading of the page, ranking the pictures in the page's order with ``ROLES``."""
    plain = card()

    def answer(request):
        result = plain(request)
        if request.schema_name == extraction.SCHEMA_NAME:
            result["image_ranking"] = [{"image_id": f"img_{n}", "role": ROLES[(n - 1) % len(ROLES)]}
                                       for n in range(1, len(request.images) + 1)]
        return result
    return answer


class RecordingImages(FakeImages):
    """The fake image provider, noting the product photos each request carried (by content hash)."""

    def __init__(self):
        super().__init__()
        self.sent = []

    async def execute(self, request):
        self.sent.append([hashlib.sha256(item).hexdigest() for item in request.reference_inputs])
        made = await super().execute(request)
        # A BYOK provider reports the reference bytes it received, as the real adapters do.
        return ProviderCallResult(made.output_bytes, made.provider_request_id, False,
                                  reference_delivery_metadata(request.reference_inputs) if request.reference_inputs else {})


class PhotoFixtures(RevisionFixtures):
    def setUp(self):
        super().setUp()
        self.web.routes[("acmecharge.example", "/products/home")] = httpx.Response(
            200, headers={"content-type": "text/html; charset=utf-8"}, text=PHOTO_PAGE)
        for path, colour in PICTURES.items():
            self.web.routes[("acmecharge.example", path)] = httpx.Response(
                200, headers={"content-type": "image/png"}, content=png_bytes((480, 480), colour))
        self.reading(photo_card())
        self.images = RecordingImages()

    # -- helpers ------------------------------------------------------------------------------------

    def photos(self, project):
        """The project's photos by the reading's role."""
        return {photo["role"]: photo for photo in self.read_ideas(project)["photos"]}

    def decide(self, project, photo_ids=(), rejected=(), fact_ids=(), accept=(), expect=200, revision=None):
        """One verification: photos checked or rejected, facts checked, every finding of ``accept``'s ads accepted."""
        current = self.read_ideas(project)
        chosen = [ad for ad in current["ads"] if ad["combination_id"] in accept]
        body = {"fact_ids": list(fact_ids), "photo_ids": list(photo_ids), "rejected_photo_ids": list(rejected),
                "accepted_findings": [{"asset_version_id": ad["asset_version_id"], "check_id": ad["check"]["id"],
                                       "finding_ids": [f["check_id"] for f in ad["check"]["findings"]]}
                                      for ad in chosen if ad["check"] and ad["check"]["findings"]]}
        response = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/verification", json=body,
                                    headers=self.headers(revision or current["revision"]))
        self.assertEqual(response.status_code, expect, response.text)
        if expect == 200:
            IdeasProjectDetail.model_validate(response.json())
        return response.json()

    def everything_but_photos(self, project):
        """Every cited fact checked and every finding accepted: only the photos are left."""
        return self.decide(project, fact_ids=["f1", "f2", "f3"], accept=[c for cs in ADS_OF.values() for c in cs])

    def recipe_references(self, asset_version_id):
        with tenant_session(self.context) as session:
            recipe = session.scalar(select(GenerationRecipeSnapshotRecord)
                .join(GenerationRunRecord, GenerationRunRecord.recipe_snapshot_id == GenerationRecipeSnapshotRecord.id)
                .join(RunItemRecord, RunItemRecord.generation_run_id == GenerationRunRecord.id)
                .join(AssetVersionRecord, AssetVersionRecord.producing_run_item_id == RunItemRecord.id)
                .where(AssetVersionRecord.id == asset_version_id))
            return recipe.canonical_json["selection"]["product_references"]

    def draft_references(self, draft_id):
        with tenant_session(self.context) as session:
            return session.get(GenerationDraftRecord, draft_id).selectors_json.get("product_references") or []

    def published_version(self, project):
        versions, draft = self.product_versions(project)
        return versions[-1] if versions else None, draft


class UncheckedPhotoDraftTests(PhotoFixtures):
    def test_the_first_batch_sends_the_unchecked_photos_and_its_ads_are_drafts(self):
        project = self.made()
        photos = self.read_ideas(project)["photos"]
        # The main photo, the close-up and the other angle; the scene in use never stands in for the product.
        self.assertEqual([photo["role"] for photo in photos], ["main", "detail", "angle"])
        self.assertEqual({photo["status"] for photo in photos}, {"unverified"})
        self.assertEqual({photo["source"] for photo in photos}, {"product_page"})
        self.assertTrue(all(photo["intake_id"] and photo["image_id"] for photo in photos))
        ids = [photo["photo_id"] for photo in photos]
        # Every image request carried the three photos, the main one first, exactly as stored.
        self.assertEqual(len(self.images.sent), 6)
        self.assertTrue(all(sent == ids for sent in self.images.sent), self.images.sent)
        with tenant_session(self.context) as session:
            stored = {obj.derivative_sha256 for obj in session.scalars(select(ReferenceObjectRecord).where(
                ReferenceObjectRecord.workspace_id == self.context.workspace_id)).all()}
        self.assertLessEqual(set(ids), stored)
        for ad in project["ads"]:
            self.assertTrue(ad["used_product_photo"])
            self.assertEqual(ad["photo_ids"], ids)
            self.assertEqual(ad["status"], "draft")
            self.assertIn("photo_unverified", self.reasons(ad))
            self.assertNotIn("appearance_unconfirmed", self.reasons(ad))
            references = self.recipe_references(ad["asset_version_id"])
            self.assertEqual({(ref["attestation"], ref["selection_policy"]) for ref in references},
                             {(UNVERIFIED, product_references.IDEAS_POLICY)})
            self.assertEqual([ref["reference_role"] for ref in references], ["primary", "detail", "detail"])
            self.assertTrue(all(ref["reference_version_id"] is None for ref in references))
        self.assertTrue(all(photo["asset_version_ids"] == [ad["asset_version_id"] for ad in project["ads"]]
                            for photo in photos))
        # Only a person's check makes a photo product truth: nothing went into the creative library.
        versions, _ = self.product_versions(project)
        self.assertEqual(versions, [])

    def test_an_ad_made_with_an_unchecked_photo_cannot_be_downloaded(self):
        project = self.checked_ads()
        self.everything_but_photos(project)
        ads = self.ads(project)
        self.assertEqual({ad["status"] for ad in ads.values()}, {"draft"})
        self.assertEqual({tuple(self.reasons(ad)) for ad in ads.values()}, {("photo_unverified",)})
        refused = self.download(project, ["c1_1"], expect=409)
        self.assertEqual(refused["code"], "ads_not_exportable")
        self.assertEqual([r["code"] for r in refused["params"]["ads"][0]["reasons"]], ["photo_unverified"])

    def test_the_check_compares_the_image_with_the_unchecked_photos_and_says_so(self):
        project = self.checked_ads()
        ids = [photo["photo_id"] for photo in self.read_ideas(project)["photos"]]
        for call in self.check_calls(project["id"]):
            sent = [image["content_sha256"] for image in call.input_json["images"] if image["role"] == "product_reference"]
            self.assertEqual(sent, ids)
            self.assertTrue(call.input_json["product_reference_available"])
            self.assertIn("has not checked them yet", call.input_json["system_prompt"])
            self.assertNotIn("whose source and product identity the user confirmed", call.input_json["system_prompt"])


class CheckingPhotosTests(PhotoFixtures):
    def test_checking_the_photos_makes_the_same_image_exportable_with_no_new_image_or_check(self):
        project = self.checked_ads()
        self.everything_but_photos(project)
        before = self.ads(project)
        runs, calls, checks = self.runs(), self.images.calls, {ad["asset_version_id"]: ad["check"]["id"] for ad in before.values()}
        _, draft_before = self.product_versions(project)
        ids = [photo["photo_id"] for photo in self.read_ideas(project)["photos"]]
        self.decide(project, photo_ids=ids)
        after = self.ads(project)
        self.assertEqual({cid: ad["asset_version_id"] for cid, ad in after.items()},
                         {cid: ad["asset_version_id"] for cid, ad in before.items()})
        self.assertEqual({ad["status"] for ad in after.values()}, {"exportable"})
        # No new image, no new check: the check compared the image with these very bytes (#199).
        self.assertEqual((self.runs(), self.images.calls), (runs, calls))
        self.assertEqual({ad["asset_version_id"]: ad["check"]["id"] for ad in after.values()}, checks)
        self.assertEqual(len(self.download(project, ["c1_1", "c2_2"])["downloads"]), 2)

        # The checked photos became creative-library references and part of the published product version.
        photos = self.read_ideas(project)["photos"]
        self.assertEqual({photo["status"] for photo in photos}, {"verified"})
        version, draft = self.published_version(project)
        self.assertEqual(version.origin_command, "ideas_verification")
        bindings = version.content_json["reference_bindings"]
        self.assertEqual([b["reference_version_id"] for b in bindings], [photo["reference_version_id"] for photo in photos])
        self.assertEqual([b["conditions"]["product_reference_item"]["role"] for b in bindings], ["primary", "detail", "detail"])
        self.assertEqual({b["conditions"]["product_reference_item"]["identity_status"] for b in bindings}, {"verified_physical"})
        with tenant_session(self.context) as session:
            for photo in photos:
                reference = session.get(LibraryVersionRecord, photo["reference_version_id"])
                self.assertEqual((reference.entry_type, reference.state), ("reference_asset", "effective"))
                self.assertEqual(reference.content_json["rights"]["scope"], "product_page")
                self.assertEqual(reference.content_json["rights"]["declared_by"], self.context.initiator_id)
                obj = session.get(ReferenceObjectRecord, reference.content_json["object_id"])
                self.assertEqual(obj.derivative_sha256, photo["photo_id"])
            current = published(session, self.context, session.get(ProductRecord, project["product_id"]))
        self.assertEqual({(item["identity_status"], item["available"]) for item in current["items"]}, {("verified_physical", True)})
        # The AI's proposal in the Draft is untouched.
        self.assertEqual((draft.revision, draft.content_json), (draft_before.revision, draft_before.content_json))
        record = self.records(project, "verification")[-1]
        self.assertEqual([(line["photo_id"], line["decision"]) for line in record.content_json["photos"]],
                         [(pid, "verified") for pid in ids])
        self.assertEqual(record.library_version_id, version.id)

    def test_checking_one_photo_leaves_the_ads_drafts_until_every_photo_they_used_is_checked(self):
        project = self.checked_ads()
        self.everything_but_photos(project)
        main = self.photos(project)["main"]["photo_id"]
        self.decide(project, photo_ids=[main])
        self.assertEqual({tuple(self.reasons(ad)) for ad in self.ads(project).values()}, {("photo_unverified",)})
        self.assertEqual({p["role"]: p["status"] for p in self.read_ideas(project)["photos"]},
                         {"main": "verified", "detail": "unverified", "angle": "unverified"})
        # The published version holds only what was checked.
        version, _ = self.published_version(project)
        self.assertEqual(len(version.content_json["reference_bindings"]), 1)

    def test_only_photos_can_be_checked_and_a_version_is_published_without_any_fact(self):
        project = self.made()
        main = self.photos(project)["main"]["photo_id"]
        self.decide(project, photo_ids=[main])
        version, _ = self.published_version(project)
        self.assertEqual(version.content_json["selling_points"], [])
        self.assertEqual(version.content_json["description"], version.content_json["name"])
        self.assertEqual(version.content_json["audience"], "United States")
        self.assertEqual(len(version.content_json["reference_bindings"]), 1)

    def test_new_images_use_the_checked_photos_from_the_creative_library(self):
        project = self.checked_ads()
        ids = [photo["photo_id"] for photo in self.read_ideas(project)["photos"]]
        self.decide(project, photo_ids=ids[:1])
        current = self.read_ideas(project)
        response = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/generation-drafts",
                                    headers=self.headers(current["revision"]), json={"provider": "mock", "model": "mock-v1"})
        self.assertEqual(response.status_code, 201, response.text)
        drafts = response.json()["drafts"]
        references = self.draft_references(drafts[0]["draft_id"])
        self.assertEqual([ref["content_sha256"] for ref in references], ids)
        # The checked main photo is now the product's verified reference; the other two are still unchecked.
        self.assertEqual([ref["attestation"] for ref in references],
                         ["verified_physical_product", UNVERIFIED, UNVERIFIED])
        self.assertEqual(references[0]["selection_policy"], product_references.SET_POLICY)


class PaidCheckPhotoTests(PhotoFixtures):
    def test_a_paid_check_looks_at_the_unchecked_photos_and_still_counts_once_they_are_checked(self):
        """BYOK: the fake vision model receives the image and its three photos; checking the photos later
        leaves that check the ad's answer (no new check, no ``check_outdated``)."""
        self.byok()
        # The fake OpenAI reading ranks the page's four pictures the way ``photo_card`` does.
        reading = photo_card()(SimpleNamespace(images=[None] * len(PICTURES), schema_name=extraction.SCHEMA_NAME,
                                               json_schema={}))
        self.llm_client._adapters["openai"] = OpenAIChatAdapter(
            base_url="https://openai.test/v1", timeout=5, transport=httpx.MockTransport(lambda request: httpx.Response(
                200, json={"id": "chatcmpl-photos", "model": "gpt-5-mini-2025-08-07",
                           "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps(reading)},
                                        "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 900, "completion_tokens": 300}})))
        quoted = {step["step"]: Decimal(step["maximum_cost"]) for step in self.quote("byok")["steps"]}
        batch = self.start("byok")
        self.drain(job_types=("products.intake", "marketing.plan", "marketing.first_batch"))
        self.drain(job_types=IMAGE_JOBS)
        project = self.read_ideas({"id": self.batch(batch["id"])["project_id"]})
        ids = [photo["photo_id"] for photo in project["photos"]]
        self.assertEqual(len(ids), 3)
        self.assertEqual(self.images.sent, [ids] * 6)
        # The photos cost nothing extra: the images were reserved at the quoted maximum, photos or not.
        self.assertEqual(Decimal(self.batch(batch["id"])["reserved_total"]),
                         quoted["reading"] + quoted["understanding"] + quoted["ideas"] + quoted["images"])
        self.start_checks(project, "byok")
        seen = []

        def answer(provider, model, credential, inputs, images):
            seen.append((inputs, images))
            return check_report(inputs)
        self.run_checks(project, AsyncMock(side_effect=answer))
        self.assertEqual(len(seen), 6)
        for inputs, images in seen:
            self.assertEqual(len(images), 4)  # the ad, then its three photos
            self.assertEqual([i["content_sha256"] for i in inputs["images"][1:]], ids)
            self.assertIn("has not checked them yet", inputs["system_prompt"])
        checks = {ad["asset_version_id"]: ad["check"]["id"] for ad in self.read_ideas(project)["ads"]}
        self.decide(project, fact_ids=["f1", "f2", "f3"])
        self.assertEqual({tuple(self.reasons(ad)) for ad in self.ads(project).values()}, {("photo_unverified",)})
        self.decide(project, photo_ids=ids)
        after = self.read_ideas(project)["ads"]
        self.assertEqual({ad["status"] for ad in after}, {"exportable"})
        self.assertEqual({ad["asset_version_id"]: ad["check"]["id"] for ad in after}, checks)
        self.assertEqual(len(self.check_calls(project["id"])), 6)


class RejectingPhotosTests(PhotoFixtures):
    def test_rejecting_a_photo_makes_every_ad_that_used_it_need_redoing(self):
        project = self.checked_ads()
        self.everything_but_photos(project)
        photos = self.photos(project)
        self.decide(project, photo_ids=[photos["main"]["photo_id"], photos["angle"]["photo_id"]],
                    rejected=[photos["detail"]["photo_id"]])
        ads = self.ads(project)
        self.assertEqual({ad["status"] for ad in ads.values()}, {"needs_redo"})
        self.assertTrue(all("photo_unavailable" in self.reasons(ad) for ad in ads.values()))
        self.assertEqual(self.photos(project)["detail"]["status"], "rejected")
        self.download(project, ["c1_1"], expect=409)
        # The rejected photo never joins the product's confirmed content.
        version, _ = self.published_version(project)
        self.assertEqual(len(version.content_json["reference_bindings"]), 2)

    def test_a_photo_checked_and_then_rejected_leaves_the_published_version(self):
        project = self.checked_ads()
        self.everything_but_photos(project)
        ids = [photo["photo_id"] for photo in self.read_ideas(project)["photos"]]
        self.decide(project, photo_ids=ids)
        self.assertEqual({ad["status"] for ad in self.ads(project).values()}, {"exportable"})
        self.decide(project, rejected=[ids[1]])
        self.assertEqual({ad["status"] for ad in self.ads(project).values()}, {"needs_redo"})
        version, _ = self.published_version(project)
        self.assertNotIn(ids[1], [b["viewpoint"] for b in version.content_json["reference_bindings"]])
        self.assertEqual(len(version.content_json["reference_bindings"]), 2)

    def test_redoing_an_ad_sends_the_photos_as_they_are_now(self):
        project = self.checked_ads()
        photos = self.photos(project)
        self.decide(project, rejected=[photos["main"]["photo_id"]])
        prepared = self.feedback(project, "c1_1")
        references = self.draft_references(prepared["draft_id"])
        # The rejected main photo is gone; the close-up leads, and nothing else stands in for the scene in use.
        self.assertEqual([ref["content_sha256"] for ref in references],
                         [photos["detail"]["photo_id"], photos["angle"]["photo_id"]])
        self.make(prepared["draft_id"])
        redone = [ad for ad in self.read_ideas(project)["ads"] if ad["origin"] == "corrected"]
        self.assertEqual(len(redone), 1)
        self.assertEqual(redone[0]["photo_ids"], [photos["detail"]["photo_id"], photos["angle"]["photo_id"]])
        self.assertNotIn("photo_unavailable", self.reasons(redone[0]))
        self.assertEqual(self.images.sent[-1][1:], [photos["detail"]["photo_id"], photos["angle"]["photo_id"]])

    def test_the_same_request_after_a_rejection_is_a_new_draft(self):
        project = self.checked_ads()
        first = self.feedback(project, "c2_1")
        again = self.feedback(project, "c2_1")
        self.assertEqual(first["draft_id"], again["draft_id"])
        self.decide(project, rejected=[self.photos(project)["angle"]["photo_id"]])
        after = self.feedback(project, "c2_1")
        self.assertNotEqual(after["draft_id"], first["draft_id"])
        with tenant_session(self.context) as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(MarketingFeedbackDraftRecord).where(
                MarketingFeedbackDraftRecord.project_id == project["id"])), 2)

    def test_a_draft_holding_a_rejected_photo_is_refused_before_anything_is_spent(self):
        project = self.checked_ads()
        prepared = self.feedback(project, "c1_1")
        self.decide(project, rejected=[self.photos(project)["main"]["photo_id"]])
        _, preview = self.preview(prepared["draft_id"])
        self.assertEqual((preview.status_code, preview.json()["code"]), (422, "product_reference_changed"), preview.text)
        self.assertEqual(self.images.calls, 6)

    def test_a_photo_rejected_before_the_first_images_never_reaches_them(self):
        batch = self.start("mock")
        # Read the page and draft the understanding; the ideas and the images are not admitted yet.
        self.drain(job_types=("products.intake", "marketing.first_batch"))
        self.drain(job_types=("marketing.plan",))
        project = {"id": self.batch(batch["id"])["project_id"]}
        main = self.photos(project)["main"]["photo_id"]
        self.decide(project, rejected=[main])
        # The rest runs in the Worker, which picks the photos itself.
        self.drain(job_types=("marketing.first_batch", "marketing.plan"))
        self.drain(job_types=IMAGE_JOBS)
        ads = self.read_ideas(project)["ads"]
        self.assertEqual(len(ads), 6)
        self.assertTrue(all(main not in ad["photo_ids"] and len(ad["photo_ids"]) == 2 for ad in ads))
        self.assertTrue(all(main not in sent for sent in self.images.sent))


class PhotoRefusalTests(PhotoFixtures):
    def test_a_photo_the_reading_did_not_find_cannot_be_decided(self):
        project = self.made()
        refused = self.decide(project, photo_ids=["a" * 64], expect=422)
        self.assertEqual((refused["code"], refused["params"]["photo_ids"]), ("photo_not_found", ["a" * 64]))
        main = self.photos(project)["main"]["photo_id"]
        both = self.decide(project, photo_ids=[main], rejected=[main], expect=422)
        self.assertEqual(both["code"], "validation_error")
        self.assertEqual([error["code"] for error in both["field_errors"]], ["photo_decision_conflict"])
        self.assertEqual(self.records(project, "verification"), [])

    def test_another_workspace_can_neither_read_nor_decide_these_photos(self):
        project = self.made()
        main = self.photos(project)["main"]["photo_id"]
        revision = self.read_ideas(project)["revision"]
        owner = self.context
        self.context = self._make_context()
        base = f"/api/v1/marketing/ideas/{project['id']}"
        attempts = (self.client.get(base),
                    self.client.post(f"{base}/verification", headers=self.headers(revision), json={"photo_ids": [main]}),
                    self.client.post(f"{base}/verification", headers=self.headers(revision), json={"rejected_photo_ids": [main]}))
        for response in attempts:
            self.assertEqual((response.status_code, response.json()["code"]), (404, "marketing_project_not_found"), response.text)
        with tenant_session(self.context) as session:
            # Even handed the row, its own session finds neither the reading nor the photos.
            with tenant_session(owner) as theirs:
                row = ideas.get_ideas_project(theirs, owner, project["id"])
                theirs.expunge(row)
            self.assertEqual(product_references.candidates(session, self.context, row), [])
            self.assertEqual(ideas_export.photo_decisions(session, self.context, row), {})
            for model in (MarketingIdeasExportRecord, LibraryVersionRecord):
                self.assertEqual(session.scalar(select(func.count()).select_from(model).where(
                    model.workspace_id == self.context.workspace_id)), 0)
        self.context = owner
        self.assertEqual(self.records(project, "verification"), [])
        self.assertEqual({photo["status"] for photo in self.read_ideas(project)["photos"]}, {"unverified"})

    def test_checking_the_same_picture_in_another_workspace_checks_nothing_here(self):
        project = self.made()
        main = self.photos(project)["main"]["photo_id"]
        owner = self.context
        self.context = self._make_context()
        self.paid_setup()
        theirs = self.made()
        # The same page read in another Workspace: the same bytes, so the same content hash.
        self.assertEqual(self.photos(theirs)["main"]["photo_id"], main)
        self.decide(theirs, photo_ids=[main])
        self.assertEqual(self.photos(theirs)["main"]["status"], "verified")
        self.context = owner
        # A content hash checked once counts only inside its own Workspace.
        self.assertEqual(self.photos(project)["main"]["status"], "unverified")
        self.assertTrue(all("photo_unverified" in self.reasons(ad) for ad in self.read_ideas(project)["ads"]))
        self.assertEqual(self.records(project, "verification"), [])


class PlanFirstPhotoTests(PhotoFixtures):
    def test_a_plan_first_image_never_stands_on_an_unchecked_photo(self):
        """Rounds and reuse accept an image only on photos that were verified references when it was made."""
        project = self.made()
        asset_id = project["ads"][0]["asset_version_id"]
        with tenant_session(self.context) as session:
            version = session.get(AssetVersionRecord, asset_id)
            lineage = version.reference_sources_json
            self.assertEqual({ref["attestation"] for ref in lineage["references"]}, {UNVERIFIED})
            self.assertFalse(product_references.lineage_current(session, self.context, lineage))
            with self.assertRaises(product_references.MarketingError) as refused:
                product_references.for_asset(session, self.context, version)
            self.assertEqual(refused.exception.code, "product_reference_changed")
            self.assertEqual(product_references.appearance(session, self.context, [], version)["status"], "pending_verification")

    def test_a_project_no_reading_opened_keeps_the_verified_photo_path(self):
        project = self.made()
        with tenant_session(self.context) as session:
            row = ideas.get_ideas_project(session, self.context, project["id"])
            self.assertEqual(len(product_references.candidates(session, self.context, row)), 3)
            plain = ideas.create_ideas_project(session, self.context, product_id=row.product_id,
                                               source_text="Acme Charge Home wall charger.", market="United States",
                                               ad_language="en-US")
            self.assertEqual(product_references.candidates(session, self.context, plain), [])
            self.assertEqual(product_references.for_ideas(session, self.context, plain),
                             product_references.from_product_set(session, self.context, plain))


if __name__ == "__main__":
    unittest.main()
