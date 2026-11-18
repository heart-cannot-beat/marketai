"""Changing and adding ads-first ad drafts without a round (#200; ADR 0018, design doc §5.3).

The ads come from a real first batch (fake page, fake model, fake image provider) on the fictional Acme
Charge Home page of ``test_marketing_ideas_export``: claim 1 cites f1, claim 2 cites f2, claim 3 cites f3
through the inference i1. Corrections and image tools go through the real generation preview, submission
and Worker; crops through the real image-edit Job. No paid call is made.
"""
import asyncio
import hashlib
from copy import deepcopy
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (AssetRecord, AssetVersionRecord, AuditEventRecord, ExternalCreativeRecord,
                                            JobRecord, ProductRecord, new_id)
from app.entitlements.cost_models import ProviderCallAttemptRecord, SpendReservationRecord
from app.external_creatives.worker import TRUTH_POLICY_VERSION
from app.generation.models import GenerationDraftRecord, GenerationRecipeSnapshotRecord
from app.marketing import ideas, planner
from app.marketing.ideas_contracts import IdeasCallSummary, IdeasFeedbackDraft, IdeasProjectDetail
from app.marketing.models import MarketingIdeasUploadRecord, MarketingProjectRecord, PlanningCallRecord
from app.marketing.workflow_models import MarketingFeedbackDraftRecord, MarketingImageEditRecord
from app.platform.canonical import canonical_hash
from tests.support.library_fixtures import png_bytes
from tests.test_marketing_ideas_checks import IMAGE_JOBS
from tests.test_marketing_first_batch import check_report
from tests.test_marketing_ideas_export import ADS_OF, IdeasExportFixtures

NOTE = "Make the background a little lighter."


class RevisionFixtures(IdeasExportFixtures):
    # -- endpoints ----------------------------------------------------------------------------------

    def feedback(self, project, combination_id=None, *, asset_version_id=None, note=NOTE, provider="mock", model="mock-v1",
                 image_tool=None, expect=201, key=None):
        asset_version_id = asset_version_id or self.ads(project)[combination_id]["asset_version_id"]
        body = {"asset_version_id": asset_version_id, "note": note, "provider": provider, "model": model,
                **({"image_tool": image_tool} if image_tool else {})}
        response = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/feedback-drafts", json=body,
                                    headers=self.headers(key=key))
        self.assertEqual(response.status_code, expect, response.text)
        if expect == 201:
            IdeasFeedbackDraft.model_validate(response.json())
        return response.json()

    def crop(self, project, combination_id=None, *, asset_version_id=None, crop=None, rotation=90, expect=202, key=None):
        asset_version_id = asset_version_id or self.ads(project)[combination_id]["asset_version_id"]
        body = {"asset_version_id": asset_version_id, "rotation": rotation, **({"crop": crop} if crop else {})}
        response = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/image-edits", json=body,
                                    headers=self.headers(key=key))
        self.assertEqual(response.status_code, expect, response.text)
        if expect == 202:
            IdeasProjectDetail.model_validate(response.json())
        return response.json()

    def place(self, project, creative_id, combination_id, expect=201, key=None):
        response = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/uploads", headers=self.headers(key=key),
                                    json={"external_creative_id": creative_id, "combination_id": combination_id})
        self.assertEqual(response.status_code, expect, response.text)
        if expect == 201:
            IdeasProjectDetail.model_validate(response.json())
        return response.json()

    def add(self, project, addition, expect=202, mode="mock", quote_hash="", key=None):
        current = self.read_ideas(project)
        response = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/planning",
                                    headers=self.headers(current["revision"], key),
                                    json={"kind": "ideas", "execution_mode": mode, "quote_hash": quote_hash, "addition": addition})
        self.assertEqual(response.status_code, expect, response.text)
        if expect == 202:
            IdeasCallSummary.model_validate(response.json())
        return response.json()

    # -- generation ---------------------------------------------------------------------------------

    def preview(self, draft_id):
        draft = self.client.get(f"/api/v1/generation/drafts/{draft_id}").json()
        return draft, self.client.post(f"/api/v1/generation/drafts/{draft_id}/previews", headers=self.headers(draft["revision"]))

    def make(self, draft_id):
        """Preview and submit one draft, then let the Worker make its image."""
        draft, preview = self.preview(draft_id)
        self.assertEqual(preview.status_code, 201, preview.text)
        run = self.client.post("/api/v1/generation/runs", headers=self.headers(draft["revision"]),
                               json={"preview_id": preview.json()["id"], "preview_hash": preview.json()["preview_hash"]})
        self.assertEqual(run.status_code, 202, run.text)
        self.drain(job_types=IMAGE_JOBS)
        return preview.json()

    def finished(self, mode="mock"):
        """Six checked ads and a first batch that has finished, so the ideas may be added to."""
        project = self.checked_ads(mode)
        self.drain()
        self.assertEqual(self.read_ideas(project)["first_batch"]["state"], "done")
        return self.read_ideas(project)

    # -- records ------------------------------------------------------------------------------------

    def by_origin(self, project, origin):
        return [ad for ad in self.read_ideas(project)["ads"] if ad["origin"] == origin]

    def recipe_intent(self, project, asset_version_id):
        with tenant_session(self.context) as session:
            row = session.get(MarketingProjectRecord, project["id"])
            return next(ideas.intent_of(recipe) for version, recipe in ideas.project_ads(session, self.context, row)
                        if version.id == asset_version_id)

    def uploaded(self, project, *, permission=True, status="ready", product_id=None):
        """An image the user uploaded for the product, processed like ``external_creatives`` does it."""
        from app.marketing.blob_read import write_generated
        self.uploads = getattr(self, "uploads", 0) + 1  # each upload is a different image
        content = png_bytes(colour=(200, 120, 40 + self.uploads))
        sha = hashlib.sha256(content).hexdigest()
        key = f"committed/{self.context.workspace_id}/external/{new_id('up')}.png"
        blob = asyncio.run(write_generated(self.blobs, key, content, "image/png"))
        evidence = {"determination": "external_creative_no_truth_package", "policy": TRUTH_POLICY_VERSION}
        with tenant_session(self.context) as session:
            product = product_id or project["product_id"]
            asset = AssetRecord(id=new_id("asset"), workspace_id=self.context.workspace_id, product_id=product, run_id=None,
                                source="external", combo_id="external", combo_code="external", tags_json={},
                                filename="acme-charge-driveway.png", provider="external", model="", prompt="", fingerprint=sha,
                                status="ok")
            session.add(asset)
            session.flush()
            version = AssetVersionRecord(id=new_id("av"), workspace_id=self.context.workspace_id, asset_id=asset.id,
                kind="external", aspect_ratio="4:3", object_key=blob.committed_key, committed_object_key=blob.committed_key,
                committed_storage_version_id=blob.storage_version_id, content_sha256=sha, byte_size=len(content),
                mime_type="image/png", origin_kind="upload", execution_mode=None, seed_unavailable_reason="external_creative",
                truth_dependency_expected=False, truth_dependency_policy_version=TRUTH_POLICY_VERSION,
                truth_dependency_evidence_json=evidence, truth_dependency_evidence_hash=canonical_hash(evidence))
            session.add(version)
            session.flush()
            creative = ExternalCreativeRecord(id=new_id("exc"), workspace_id=self.context.workspace_id, product_id=product,
                status=status, upload_intent_id=new_id("ui"), upload_key=key, upload_sha256=sha, upload_mime_type="image/png",
                upload_byte_size=len(content), filename="acme-charge-driveway.png", made_by="self",
                rights_json={"made_by": "self", "permission_confirmed": permission, "note": None,
                             "declared_by": self.context.initiator_id, "declared_at": "2026-11-18T09:00:00+00:00"},
                execution_mode="mock", vocabulary_json={}, vocabulary_hash="0" * 64, evidence_json={},
                asset_id=asset.id, asset_version_id=version.id if status != "queued" else None,
                batch_key_hash="1" * 64, request_fingerprint="2" * 64)
            session.add(creative)
            return creative.id, version.id


# -- corrections -------------------------------------------------------------------------------------


class CorrectionTests(RevisionFixtures):
    def test_a_correction_makes_a_new_version_and_keeps_the_old_one(self):
        project = self.checked_ads()
        old = self.ads(project)["c1_1"]
        prepared = self.feedback(project, "c1_1")
        self.assertEqual((prepared["kind"], prepared["combination_id"], prepared["source_asset_version_id"]),
                         ("correction", "c1_1", old["asset_version_id"]))
        # The correction carries the check's findings the user has not accepted.
        self.assertEqual(sorted(prepared["finding_ids"]), sorted(f["check_id"] for f in old["check"]["findings"]))
        self.make(prepared["draft_id"])
        ads = self.read_ideas(project)["ads"]
        self.assertEqual(len(ads), 7)
        new = next(ad for ad in ads if ad["origin"] == "corrected")
        kept_old = next(ad for ad in ads if ad["asset_version_id"] == old["asset_version_id"])
        # The old version is still an ad, unchanged; the new one is a draft of the same ad, made from it.
        self.assertEqual((kept_old["status"], kept_old["check"]["id"], kept_old["origin"]),
                         (old["status"], old["check"]["id"], "generated"))
        self.assertEqual((new["combination_id"], new["source_asset_version_id"], new["status"], new["check"]),
                         ("c1_1", old["asset_version_id"], "draft", None))
        self.assertIn("check_missing", self.reasons(new))
        with tenant_session(self.context) as session:
            version = session.get(AssetVersionRecord, new["asset_version_id"])
            source = session.get(AssetVersionRecord, old["asset_version_id"])
            self.assertEqual((version.parent_version_id, version.asset_id), (source.id, source.asset_id))
        # The intent still says what the image stands on, and what it was made from.
        intent = self.recipe_intent(project, new["asset_version_id"])
        self.assertEqual((intent["schema_version"], intent["flow"], intent["project_id"]), (2, "ads_first", project["id"]))
        self.assertEqual(intent["cited_fact_ids"], ["f1"])
        self.assertEqual(intent["revision"]["kind"], "correction")
        self.assertEqual((intent["revision"]["source_asset_version_id"], intent["revision"]["note"]),
                         (old["asset_version_id"], NOTE))

    def test_a_correction_after_a_cited_fact_changed_is_made_on_the_facts_as_they_are(self):
        """"Needs redoing" can be undone (#198): redone, the ad is made on the current fact and is a draft again."""
        project = self.checked_ads()
        self.edit_facts(project, self.set_fact("f1", statement="Charging can be scheduled overnight from the Acme Charge app."))
        old = self.ads(project)["c1_1"]
        self.assertEqual((old["status"], self.reasons(old)[0]), ("needs_redo", "facts_changed"))
        self.make(self.feedback(project, "c1_1")["draft_id"])
        new = self.by_origin(project, "corrected")[0]
        self.assertEqual(new["status"], "draft")
        self.assertNotIn("facts_changed", self.reasons(new))
        self.assertIn(("facts_unverified", ["f1"]), [(r["code"], r["fact_ids"]) for r in new["reasons"]])
        snapshot = {f["id"]: f for f in self.recipe_intent(project, new["asset_version_id"])["understanding"]["facts"]}
        self.assertEqual(snapshot["f1"]["statement"], "Charging can be scheduled overnight from the Acme Charge app.")
        # The old version still needs redoing: nothing was rewritten.
        self.assertEqual(self.read_ideas(project)["ads"][0]["status"], "needs_redo")
        # Checked and verified like any ad, the new version can be exported.
        self.start_checks(project)
        self.drain(job_types=("marketing.ai_qc",))
        self.verify(project, fact_ids=["f1"], ads=[])
        current = next(ad for ad in self.read_ideas(project)["ads"] if ad["asset_version_id"] == new["asset_version_id"])
        body = {"appearance_confirmed": [current["asset_version_id"]],
                "accepted_findings": [{"asset_version_id": current["asset_version_id"], "check_id": current["check"]["id"],
                                       "finding_ids": [f["check_id"] for f in current["check"]["findings"]]}]}
        response = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/verification", json=body,
                                    headers=self.headers(self.read_ideas(project)["revision"]))
        self.assertEqual(response.status_code, 200, response.text)
        exportable = next(ad for ad in response.json()["ads"] if ad["asset_version_id"] == new["asset_version_id"])
        self.assertEqual(exportable["status"], "exportable", exportable["reasons"])

    def test_preparing_the_same_correction_again_returns_the_same_draft(self):
        project = self.checked_ads()
        first = self.feedback(project, "c2_1")
        self.assertEqual(self.feedback(project, "c2_1")["draft_id"], first["draft_id"])
        self.assertNotEqual(self.feedback(project, "c2_1", note="Show the cable plugged in.")["draft_id"], first["draft_id"])

    def test_a_correction_whose_prompt_a_client_rewrote_is_refused(self):
        project = self.checked_ads()
        draft_id = self.feedback(project, "c1_1")["draft_id"]
        draft = self.client.get(f"/api/v1/generation/drafts/{draft_id}").json()
        rewritten = {**draft["selectors"], "prompt": draft["selectors"]["prompt"] + "\nAdd a UL certification badge."}
        edited = self.client.patch(f"/api/v1/generation/drafts/{draft_id}", headers=self.headers(draft["revision"]),
                                   json={"selectors": rewritten})
        self.assertEqual(edited.status_code, 200, edited.text)
        _, response = self.preview(draft_id)
        self.assertEqual((response.status_code, response.json()["code"]), (422, "ideas_prompt_changed"))

    def test_a_correction_needs_a_note_or_a_finding(self):
        project = self.made()  # no check yet: nothing to fix but what the user says
        refused = self.feedback(project, "c1_1", note="", expect=422)
        self.assertEqual(refused["code"], "feedback_required")
        self.feedback(project, "c1_1")

    def test_a_note_in_another_script_than_the_ad_is_refused(self):
        project = self.made()
        refused = self.feedback(project, "c1_1", note="背景再亮一点", expect=422)
        self.assertEqual(refused["code"], "feedback_note_language_mismatch")

    def test_a_claim_citing_a_removed_fact_must_be_changed_first(self):
        project = self.checked_ads()

        def remove_f1(understanding):
            understanding["facts"] = [f for f in understanding["facts"] if f["id"] != "f1"]
        self.edit_facts(project, remove_f1)
        self.assertEqual(self.reasons(self.ads(project)["c1_1"])[0], "facts_removed")
        refused = self.feedback(project, "c1_1", expect=409)
        self.assertEqual((refused["code"], refused["params"]["fact_ids"]), ("claim_cites_removed_fact", ["f1"]))
        # An ad of another claim is untouched and can still be corrected.
        self.feedback(project, "c2_1")

    def test_an_image_of_replaced_ideas_cannot_be_corrected(self):
        project = self.checked_ads()
        with tenant_session(self.context) as session:
            row = session.get(MarketingProjectRecord, project["id"])
            doc = deepcopy(row.ideas_json)
            next(c for c in doc["combinations"] if c["id"] == "c1_1")["headline"] = "Charge while you sleep"
            row.ideas_json = doc
        refused = self.feedback(project, "c1_1", expect=409)
        self.assertEqual(refused["code"], "ideas_changed")

    def test_mock_and_real_images_never_mix(self):
        project = self.checked_ads()
        refused = self.feedback(project, "c1_1", provider="openai", model="gpt-image-1", expect=422)
        self.assertEqual(refused["code"], "feedback_provider_invalid")

    def test_an_ad_of_another_project_is_not_found(self):
        project = self.checked_ads()
        refused = self.feedback(project, asset_version_id="av_missing", expect=404)
        self.assertEqual(refused["code"], "ad_not_found")


class ImageToolTests(RevisionFixtures):
    def tool(self, project, combination_id, kind="upscale"):
        """An image tool for the ad's image, sized as the image really is."""
        import io
        from PIL import Image
        from app.generation.paid_execution import asset_blob_ref
        from app.marketing.blob_read import read_committed
        with tenant_session(self.context) as session:
            ref = asset_blob_ref(session, self.context, self.ads(project)[combination_id]["asset_version_id"])
        with Image.open(io.BytesIO(asyncio.run(read_committed(self.blobs, ref, 20 * 1024 * 1024)))) as image:
            width, height = image.size
        return {"kind": kind, "source_width": width, "source_height": height, **({"scale": 2} if kind == "upscale" else {})}

    def test_an_image_tool_keeps_what_the_source_stood_on(self):
        project = self.checked_ads()
        old = self.ads(project)["c2_1"]
        prepared = self.feedback(project, "c2_1", note="", image_tool=self.tool(project, "c2_1"))
        self.assertEqual((prepared["kind"], prepared["finding_ids"]), ("image_tool", []))
        draft = self.client.get(f"/api/v1/generation/drafts/{prepared['draft_id']}").json()
        self.assertEqual((draft["format_config"]["image_size"], draft["source_asset_version_id"]), ("2K", old["asset_version_id"]))
        _, preview = self.preview(prepared["draft_id"])
        self.assertEqual(preview.status_code, 201, preview.text)
        intent = self.recipe_intent(project, old["asset_version_id"])
        stored = self.client.get(f"/api/v1/generation/drafts/{prepared['draft_id']}").json()["selectors"]["marketing_intent"]
        # The processed image stands on exactly what its source stood on, not on the facts as they are now.
        self.assertEqual(stored["understanding"], intent["understanding"])
        self.assertEqual(stored["cited_fact_ids"], intent["cited_fact_ids"])
        self.assertEqual((stored["revision"]["kind"], stored["revision"]["image_tool"]), ("image_tool", "upscale"))

    def test_an_image_tool_is_refused_for_an_ad_that_needs_redoing(self):
        project = self.checked_ads()
        self.edit_facts(project, self.set_fact("f2", statement="Load balancing keeps the circuit from tripping."))
        refused = self.feedback(project, "c2_1", note="", image_tool=self.tool(project, "c2_1"), expect=409)
        self.assertEqual((refused["code"], refused["params"]["reasons"][0]), ("ad_needs_redo", "facts_changed"))


# -- crop and rotate ---------------------------------------------------------------------------------


class CropTests(RevisionFixtures):
    def test_a_crop_makes_a_free_new_version_and_keeps_the_old_one(self):
        project = self.checked_ads()
        old = self.ads(project)["c2_1"]
        reservations, attempts = self.count(SpendReservationRecord), self.count(ProviderCallAttemptRecord)
        queued = self.crop(project, "c2_1", crop={"x": 0, "y": 0, "width": 0.5, "height": 0.5}, rotation=90)
        self.assertEqual([e["state"] for e in queued["image_edits"]], ["queued"])
        self.drain(job_types=("marketing.image_edit",))
        current = self.read_ideas(project)
        self.assertEqual([e["state"] for e in current["image_edits"]], ["succeeded"])
        new = next(ad for ad in current["ads"] if ad["origin"] == "image_edit")
        self.assertEqual((new["asset_version_id"], new["source_asset_version_id"], new["combination_id"]),
                         (current["image_edits"][0]["result_asset_version_id"], old["asset_version_id"], "c2_1"))
        self.assertEqual((new["status"], new["check"]), ("draft", None))
        self.assertIn("check_missing", self.reasons(new))
        self.assertEqual(next(ad for ad in current["ads"] if ad["asset_version_id"] == old["asset_version_id"])["check"]["id"],
                         old["check"]["id"])
        # Cropping and rotating cost nothing: no reservation, no provider call, no settlement.
        self.assertEqual((self.count(SpendReservationRecord), self.count(ProviderCallAttemptRecord)), (reservations, attempts))
        with tenant_session(self.context) as session:
            edit = session.scalar(select(MarketingImageEditRecord).where(MarketingImageEditRecord.workspace_id == self.context.workspace_id))
            self.assertEqual((edit.project_id, edit.round_id), (project["id"], None))
            self.assertEqual(session.scalar(select(func.count()).select_from(SpendReservationRecord).where(
                SpendReservationRecord.workspace_id == self.context.workspace_id,
                SpendReservationRecord.operation_id == edit.operation_id)), 0)
            # Usage counts the operation under its source image's mode, as a round's crop under the round's.
            from app.entitlements.operation_usage import operation_usage_mode
            self.assertEqual(operation_usage_mode(session, self.context.workspace_id, edit.operation_id), "mock")
        # The crop stands on what its source stood on.
        self.assertEqual(self.recipe_intent(project, new["asset_version_id"]), self.recipe_intent(project, old["asset_version_id"]))

    def test_the_new_version_gets_its_own_check(self):
        project = self.checked_ads()
        self.crop(project, "c2_1")
        self.drain(job_types=("marketing.image_edit",))
        new = self.by_origin(project, "image_edit")[0]
        quote = self.checks_quote(project)
        # Only the new version is counted: a finished check is never billed again.
        self.assertEqual((quote["quantity"], quote["asset_version_ids"]), (1, [new["asset_version_id"]]))
        self.start_checks(project, quote_hash=quote["quote_hash"])
        self.drain(job_types=("marketing.ai_qc",))
        self.assertEqual(self.by_origin(project, "image_edit")[0]["check"]["state"], "succeeded")

    def test_the_same_crop_again_is_the_same_edit(self):
        project = self.checked_ads()
        key = new_id("key")
        first = self.crop(project, "c3_1", key=key)
        self.assertEqual(self.crop(project, "c3_1", key=key)["image_edits"], first["image_edits"])
        self.crop(project, "c3_1")  # another key, the same edit
        self.assertEqual(self.count(MarketingImageEditRecord), 1)

    def test_an_ad_that_needs_redoing_is_not_cropped(self):
        project = self.checked_ads()
        self.edit_facts(project, self.set_fact("f3", statement="The enclosure is rated for outdoor use."))
        refused = self.crop(project, "c3_1", expect=409)
        self.assertEqual(refused["code"], "ad_needs_redo")

    def test_nothing_to_do_is_refused(self):
        project = self.checked_ads()
        refused = self.crop(project, "c1_1", rotation=0, expect=422)
        self.assertEqual(refused["code"], "image_edit_empty")


# -- additions ---------------------------------------------------------------------------------------


class AdditionTests(RevisionFixtures):
    def test_adding_a_way_of_telling_keeps_every_ad_as_it_was(self):
        project = self.finished()
        self.verify_everything(project)
        before = self.read_ideas(project)
        self.assertEqual(set(self.statuses(project).values()), {"exportable"})
        call = self.add(project, {"kind": "expression", "angle_id": "a1",
                                  "guiding_answers": [{"question": "Who is it for?", "answer": "Drivers who park outside."}]})
        self.assertEqual(call["addition"], {"kind": "expression", "angle_id": "a1"})
        self.drain(job_types=("marketing.plan",))
        after = self.read_ideas(project)
        self.assertEqual(after["latest_call"]["state"], "succeeded")
        angle = next(a for a in after["ideas"]["angles"] if a["id"] == "a1")
        self.assertEqual(len(angle["expressions"]), 3)
        new_expression = angle["expressions"][-1]
        self.assertNotIn(new_expression["visual_mechanism"], [e["visual_mechanism"] for e in angle["expressions"][:2]])
        added = [c for c in after["ideas"]["combinations"] if c["expression_id"] == new_expression["id"]]
        self.assertEqual(len(added), 1)
        old_ids = {c["id"] for c in before["ideas"]["combinations"]}
        self.assertNotIn(added[0]["id"], old_ids)
        # Nothing drafted before changed, and no ad changed status (#198 compares content by combination id).
        self.assertEqual([c for c in after["ideas"]["combinations"] if c["id"] in old_ids], before["ideas"]["combinations"])
        self.assertEqual(set(self.statuses(project).values()), {"exportable"})
        # The new ad is made through the ordinary generation endpoints.
        drafts = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/generation-drafts",
                                  headers=self.headers(after["revision"]), json={"provider": "mock", "model": "mock-v1"})
        self.assertEqual(drafts.status_code, 201, drafts.text)
        draft_id = next(d["draft_id"] for d in drafts.json()["drafts"] if d["combination_id"] == added[0]["id"])
        self.make(draft_id)
        made = next(ad for ad in self.read_ideas(project)["ads"] if ad["combination_id"] == added[0]["id"])
        self.assertEqual((made["origin"], made["status"], made["angle_id"]), ("generated", "draft", "a1"))

    def test_adding_a_claim_adds_its_ways_of_telling_under_new_ids(self):
        project = self.finished()
        before, statuses = self.read_ideas(project), self.statuses(project)
        self.add(project, {"kind": "claim", "note": "Ready the moment you get home."})
        self.drain(job_types=("marketing.plan",))
        after = self.read_ideas(project)
        self.assertEqual(len(after["ideas"]["angles"]), 4)
        claim = after["ideas"]["angles"][-1]
        self.assertEqual(claim["proposition"], "Ready the moment you get home.")
        self.assertEqual(len(claim["expressions"]), 2)
        self.assertEqual(len({e["visual_mechanism"] for e in claim["expressions"]}), 2)
        self.assertEqual(len(after["ideas"]["combinations"]), 8)
        self.assertEqual(after["ideas"]["angles"][:3], before["ideas"]["angles"])
        self.assertTrue(claim["id"] not in {a["id"] for a in before["ideas"]["angles"]})
        self.assertEqual(self.statuses(project), statuses)
        self.assertEqual(len(after["ads"]), 6)

    def test_a_way_of_telling_that_repeats_the_claims_picture_mechanism_is_refused(self):
        project = self.finished()
        before = self.read_ideas(project)
        repeated = next(a for a in before["ideas"]["angles"] if a["id"] == "a1")["expressions"][0]["visual_mechanism"]

        original = ideas.example_addition

        def same_mechanism(inputs):
            result = original(inputs)
            result["expression"]["visual_mechanism"] = repeated
            return result
        with patch("app.marketing.ideas.example_addition", side_effect=same_mechanism):
            self.add(project, {"kind": "expression", "angle_id": "a1"})
            self.drain(job_types=("marketing.plan",))
        refused = self.read_ideas(project)
        self.assertEqual((refused["latest_call"]["state"], refused["latest_call"]["error_code"]), ("failed", "planner_output_invalid"))
        self.assertEqual(refused["ideas"], before["ideas"])
        calls = self.calls(project["id"])
        errors = calls[-1].output_json["validation_errors"]
        self.assertEqual((errors[0]["type"], errors[0]["loc"]), ("expressions_not_distinct", ["expression", "visual_mechanism"]))
        # An explicit retry carries the refusal; a whole-ideas redraft would not.
        with tenant_session(self.context) as session:
            from app.marketing import service
            self.assertIsNone(service.planning_repair_context(
                session, self.context, session.get(MarketingProjectRecord, project["id"]), "ideas"))
        self.add(project, {"kind": "expression", "angle_id": "a1"})
        retry = self.calls(project["id"])[-1].input_json
        self.assertEqual((retry["rejected_plan_feedback"]["policy"], retry["rejected_plan_feedback"]["previous_call_id"]),
                         ("marketing-explicit-ideas-addition-repair-v1", calls[-1].id))
        self.assertIn("追加", planner.completion_messages("system", __import__("json").dumps(retry))[-1]["content"])

    def test_an_addition_waits_for_the_first_batch(self):
        project = self.made()
        refused = self.add(project, {"kind": "claim"}, expect=409)
        self.assertEqual(refused["code"], "first_batch_running")

    def test_an_addition_names_an_existing_claim(self):
        project = self.finished()
        self.assertEqual(self.add(project, {"kind": "expression", "angle_id": "a9"}, expect=404)["code"], "angle_not_found")
        self.add(project, {"kind": "expression"}, expect=422)
        self.add(project, {"kind": "claim", "angle_id": "a1"}, expect=422)
        response = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/planning",
                                    headers=self.headers(self.read_ideas(project)["revision"]),
                                    json={"kind": "understanding", "execution_mode": "mock", "addition": {"kind": "claim"}})
        self.assertEqual((response.status_code, response.json()["code"]), (422, "addition_kind_invalid"))

    def test_a_paid_addition_is_quoted_and_reserved_once(self):
        project = self.finished("byok")
        planner_quote = self.client.get("/api/v1/marketing/capabilities").json()["paid"]
        refused = self.add(project, {"kind": "claim"}, mode="byok", quote_hash="0" * 64, expect=422)
        self.assertEqual(refused["code"], "planner_unavailable")
        before = self.count(SpendReservationRecord)
        call = self.add(project, {"kind": "claim"}, mode="byok", quote_hash=planner_quote["quote_hash"])
        self.assertEqual(call["maximum_cost"], planner_quote["maximum_cost"])
        self.assertEqual(self.count(SpendReservationRecord), before + 1)


# -- uploads -----------------------------------------------------------------------------------------


class UploadTests(RevisionFixtures):
    def test_an_uploaded_image_appears_among_the_ads_once_placed(self):
        project = self.checked_ads()
        creative_id, version_id = self.uploaded(project)
        self.assertNotIn(version_id, {ad["asset_version_id"] for ad in self.read_ideas(project)["ads"]})
        placed = self.place(project, creative_id, "c2_2")
        ad = next(ad for ad in placed["ads"] if ad["asset_version_id"] == version_id)
        self.assertEqual((ad["origin"], ad["combination_id"], ad["angle_id"], ad["used_product_photo"], ad["status"]),
                         ("uploaded", "c2_2", "a2", False, "draft"))
        self.assertEqual(self.reasons(ad), ["facts_unverified", "appearance_unconfirmed", "check_missing"])
        self.assertEqual(ad["cited_fact_ids"], ["f2"])
        intent = self.recipe_intent(project, version_id)
        self.assertEqual((intent["upload"]["external_creative_id"], intent["upload"]["permission_confirmed"]), (creative_id, True))
        # Kept like any ad, and checked only by a real check: the Controlled Mock example never stands in for one.
        kept = self.client.put(f"/api/v1/marketing/ideas/{project['id']}/ads/{version_id}/kept", json={"kept": True},
                               headers=self.headers())
        self.assertEqual(kept.status_code, 200, kept.text)
        self.assertNotIn(version_id, self.checks_quote(project, "mock")["asset_version_ids"])
        # The same image under the same ad again is the same placement; under another ad it is refused.
        self.place(project, creative_id, "c2_2")
        self.assertEqual(self.place(project, creative_id, "c1_1", expect=409)["code"], "upload_already_placed")
        self.assertEqual(self.count(MarketingIdeasUploadRecord), 1)
        with tenant_session(self.context) as session:
            events = session.scalars(select(AuditEventRecord.event_type).where(
                AuditEventRecord.workspace_id == self.context.workspace_id, AuditEventRecord.resource_id == project["id"])).all()
        self.assertIn("marketing.ideas_upload_placed", events)

    def test_an_uploaded_image_is_checked_verified_and_downloaded_like_a_generated_ad(self):
        project = self.checked_ads("byok")
        creative_id, version_id = self.uploaded(project)
        self.place(project, creative_id, "c1_2")
        quote = self.checks_quote(project, "byok")
        self.assertEqual(quote["asset_version_ids"], [version_id])
        # A real check (a fake vision model here), admitted by the API and run by the Worker, against the claim.
        self.start_checks(project, "byok", quote_hash=quote["quote_hash"])
        calls = self.run_checks(project, AsyncMock(side_effect=lambda provider, model, credential, inputs, images:
                                                   check_report(inputs)))
        self.assertEqual(len(calls), 1)
        check = self.check_calls(project["id"], asset_version_id=version_id)[0]
        self.assertEqual((check.state, check.input_json["evidence_fact_ids"]), ("succeeded", ["f1"]))
        self.verify(project, fact_ids=["f1"], ads=["c1_1", "c1_2"])
        ad = next(ad for ad in self.read_ideas(project)["ads"] if ad["asset_version_id"] == version_id)
        self.assertEqual((ad["status"], ad["reasons"]), ("exportable", []))
        response = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/download-intent", headers=self.headers(),
                                    json={"asset_version_ids": [version_id]})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual([d["combination_id"] for d in response.json()["downloads"]], ["c1_2"])

    def test_an_upload_can_be_cropped_but_not_made_again(self):
        project = self.checked_ads()
        creative_id, version_id = self.uploaded(project)
        self.place(project, creative_id, "c3_1")
        refused = self.feedback(project, asset_version_id=version_id, expect=409)
        self.assertEqual(refused["code"], "external_creative_not_regenerable")
        self.crop(project, asset_version_id=version_id)
        self.drain(job_types=("marketing.image_edit",))
        cropped = self.by_origin(project, "image_edit")[0]
        self.assertEqual((cropped["source_asset_version_id"], cropped["combination_id"]), (version_id, "c3_1"))
        self.assertEqual(self.recipe_intent(project, cropped["asset_version_id"])["upload"]["external_creative_id"], creative_id)

    def test_only_a_ready_image_of_this_product_with_declared_rights_is_placed(self):
        project = self.checked_ads()
        unconfirmed, _ = self.uploaded(project, permission=False)
        self.assertEqual(self.place(project, unconfirmed, "c1_1", expect=422)["code"], "rights_confirmation_required")
        waiting, _ = self.uploaded(project, status="queued")
        self.assertEqual(self.place(project, waiting, "c1_1", expect=409)["code"], "upload_not_ready")
        with tenant_session(self.context) as session:
            other = ProductRecord(id=new_id("prod"), workspace_id=self.context.workspace_id, name="Kestrel Power Dock")
            session.add(other)
        elsewhere, _ = self.uploaded(project, product_id=other.id)
        self.assertEqual(self.place(project, elsewhere, "c1_1", expect=409)["code"], "upload_other_product")
        ready, _ = self.uploaded(project)
        self.assertEqual(self.place(project, ready, "c9_9", expect=404)["code"], "combination_not_found")
        self.assertEqual(self.place(project, "exc_missing", "c1_1", expect=404)["code"], "external_creative_not_found")
        self.assertEqual(self.count(MarketingIdeasUploadRecord), 0)


# -- isolation and the plan-first flow ----------------------------------------------------------------


class IsolationTests(RevisionFixtures):
    def test_another_workspace_can_neither_read_nor_change_any_of_this(self):
        project = self.checked_ads()
        creative_id, version_id = self.uploaded(project)
        self.place(project, creative_id, "c1_2")
        prepared = self.feedback(project, "c1_1")
        self.crop(project, "c2_1")
        asset_id = self.ads(project)["c1_1"]["asset_version_id"]
        counts = {model: self.count(model) for model in (MarketingFeedbackDraftRecord, MarketingImageEditRecord,
                                                          MarketingIdeasUploadRecord, PlanningCallRecord)}
        owner = self.context
        self.context = self._make_context()
        base = f"/api/v1/marketing/ideas/{project['id']}"
        attempts = (
            self.client.post(f"{base}/feedback-drafts", headers=self.headers(),
                             json={"asset_version_id": asset_id, "note": NOTE, "provider": "mock", "model": "mock-v1"}),
            self.client.post(f"{base}/image-edits", headers=self.headers(), json={"asset_version_id": asset_id, "rotation": 90}),
            self.client.post(f"{base}/uploads", headers=self.headers(), json={"external_creative_id": creative_id, "combination_id": "c1_1"}),
            self.client.post(f"{base}/planning", headers=self.headers(project["revision"]),
                             json={"kind": "ideas", "execution_mode": "mock", "addition": {"kind": "claim"}}),
        )
        for response in attempts:
            self.assertEqual((response.status_code, response.json()["code"]), (404, "marketing_project_not_found"), response.text)
        self.assertEqual(self.client.get(f"/api/v1/generation/drafts/{prepared['draft_id']}").status_code, 404)
        with tenant_session(self.context) as session:
            for model in (MarketingFeedbackDraftRecord, MarketingImageEditRecord, MarketingIdeasUploadRecord):
                self.assertEqual(session.scalar(select(func.count()).select_from(model).where(
                    model.workspace_id == self.context.workspace_id)), 0)
            # Its own projects never list the other workspace's upload or crop.
            self.assertEqual(ideas._ads_by_project(session, self.context, [project["id"]]), {project["id"]: []})
        self.context = owner
        self.assertEqual({model: self.count(model) for model in counts}, counts)
        self.assertIn(version_id, {ad["asset_version_id"] for ad in self.read_ideas(project)["ads"]})

    def test_a_plan_first_project_has_none_of_these_commands(self):
        old = self.create()
        base = f"/api/v1/marketing/ideas/{old['id']}"
        attempts = (
            self.client.post(f"{base}/feedback-drafts", headers=self.headers(),
                             json={"asset_version_id": "av_1", "note": NOTE, "provider": "mock", "model": "mock-v1"}),
            self.client.post(f"{base}/image-edits", headers=self.headers(), json={"asset_version_id": "av_1", "rotation": 90}),
            self.client.post(f"{base}/uploads", headers=self.headers(), json={"external_creative_id": "exc_1", "combination_id": "c1"}),
        )
        for response in attempts:
            self.assertEqual((response.status_code, response.json()["code"]), (404, "marketing_project_not_found"), response.text)


class RevisionsMigrationDowngradeTests(unittest.TestCase):
    """Dropping ads-first corrections, crops or uploads would lose the user's work, so downgrade refuses while any exists."""
    ROOT = Path(__file__).resolve().parents[1]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="marketai-ideas-revisions-migration-")
        self.addCleanup(self.temp.cleanup)
        self.database = f"{self.temp.name}/migration.db"
        self.environment = {**__import__("os").environ, "MARKETAI_DATABASE_URL": f"sqlite:///{self.database}",
                            "MARKETAI_DATA_DIR": f"{self.temp.name}/data"}
        upgraded = self.alembic("upgrade", "head")
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)

    def alembic(self, *arguments):
        return subprocess.run([sys.executable, "-m", "alembic", *arguments], cwd=self.ROOT, env=self.environment,
                              capture_output=True, text=True, check=False)

    def execute(self, *statements):
        with sqlite3.connect(self.database) as connection:
            for statement in statements:
                connection.execute(statement)

    def columns(self, table):
        with sqlite3.connect(self.database) as connection:
            return {row[1]: row[3] for row in connection.execute(f"PRAGMA table_info({table})")}

    def tables(self):
        with sqlite3.connect(self.database) as connection:
            return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

    def test_downgrade_without_ads_first_rows_restores_the_round_only_tables(self):
        self.assertIn("marketing_ideas_uploads", self.tables())
        self.assertEqual(self.columns("marketing_image_edits")["round_id"], 0)
        downgraded = self.alembic("downgrade", "zzp198a1b2c3")
        self.assertEqual(downgraded.returncode, 0, downgraded.stderr)
        self.assertNotIn("marketing_ideas_uploads", self.tables())
        for table in ("marketing_feedback_drafts", "marketing_image_edits"):
            columns = self.columns(table)
            self.assertNotIn("project_id", columns)
            self.assertEqual(columns["round_id"], 1)  # NOT NULL again

    def test_downgrade_refuses_while_an_ads_first_crop_exists(self):
        self.execute("INSERT INTO marketing_image_edits (id, workspace_id, round_id, project_id, source_asset_version_id,"
                     " operation_id, input_hash, input_json, state, created_at) VALUES ('mied_1', 'ws_1', NULL, 'mp_1',"
                     " 'av_1', 'op_1', 'h', '{}', 'queued', '2026-11-18')")
        downgraded = self.alembic("downgrade", "zzp198a1b2c3")
        self.assertNotEqual(downgraded.returncode, 0)
        self.assertIn("ads-first rows exist in marketing_image_edits", downgraded.stderr)
        self.assertIn("project_id", self.columns("marketing_image_edits"))

    def test_downgrade_refuses_while_an_upload_exists(self):
        self.execute("INSERT INTO marketing_ideas_uploads (id, workspace_id, project_id, combination_id, external_creative_id,"
                     " asset_version_id, intent_json, intent_hash, created_at) VALUES ('miu_1', 'ws_1', 'mp_1', 'c1_1', 'exc_1',"
                     " 'av_1', '{}', 'h', '2026-11-18')")
        downgraded = self.alembic("downgrade", "zzp198a1b2c3")
        self.assertNotEqual(downgraded.returncode, 0)
        self.assertIn("ads-first uploads exist", downgraded.stderr)
        self.assertIn("marketing_ideas_uploads", self.tables())

    def test_a_row_names_a_round_or_ideas_never_both(self):
        for subject in ("NULL, NULL", "'mr_1', 'mp_1'"):
            with self.assertRaises(sqlite3.IntegrityError):
                self.execute("INSERT INTO marketing_feedback_drafts (id, workspace_id, round_id, project_id,"
                             " source_asset_version_id, draft_id, feedback_hash, feedback_json, created_at)"
                             f" VALUES ('mfd_1', 'ws_1', {subject}, 'av_1', 'gd_1', 'h', '{{}}', '2026-11-18')")
