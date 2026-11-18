"""The user's own product photos (#233; ADR 0018 §2, design doc §5.2 and §5.7).

The fictional Acme Charge Home page has no picture, so the first batch makes every ad without a product
photo. The user uploads a photo of their own the way any reference is uploaded -- an Upload Intent, then
``create_library_reference_object`` -- the real Worker gate releases it, and the user adds it to the
product. Until they check it, an image made with it is a draft; checked, the same image may be exported
and every later image uses it. An image the gate rejects is refused, saying why. Everything runs through
the real endpoints and the real Worker code, with a fake model and a fake image provider; no paid call is
made.
"""
import asyncio
import base64
import hashlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import OperationRecord, ReferenceObjectRecord
from app.library import references
from app.marketing import ideas, ideas_export, product_references
from app.marketing.ideas_contracts import IdeasPhotoDownloadGrant, IdeasProjectDetail
from app.marketing.models import MarketingProductPhotoRecord
from app.platform.blob_store import GeneratedArtifactSpec
from tests.support.library_fixtures import png_bytes
from tests.test_library_reference_commands import _provision_finalized_intent
from tests.test_marketing_ideas_export import ADS_OF
from tests.test_marketing_ideas_photos import PhotoFixtures, RecordingImages
from tests.test_marketing_ideas_revisions import RevisionFixtures

UNVERIFIED = product_references.UNVERIFIED
#: The user's own photos: the whole charger, and a close-up of its plug.
HOME = png_bytes((480, 480), (240, 120, 20))
PLUG = png_bytes((480, 480), (20, 120, 240))


class AddedPhotoFixtures(RevisionFixtures):
    def setUp(self):
        super().setUp()
        self.images = RecordingImages()

    # -- the upload, as the browser does it ------------------------------------------------------------

    def uploaded(self, raw=HOME, *, drain=True):
        """Upload ``raw`` like any reference and run the gate; the reference object id and the gate's operation."""
        key = f"committed/{self.context.workspace_id}/reference_asset/2026/11/18/{uuid4().hex}.png"
        with tempfile.TemporaryDirectory(prefix="added-photo-") as folder:
            path = Path(folder) / "photo.png"
            path.write_bytes(raw)
            ref = asyncio.run(self.blobs.commit_generated_artifact(path, GeneratedArtifactSpec(
                committed_key=key, content_type="image/png",
                expected_sha256_b64=base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii"))))
        intent_id, _ = _provision_finalized_intent(self.context, committed_key=ref.committed_key,
                                                   committed_version=ref.storage_version_id, raw=raw, write_file=False,
                                                   filename="acme-charge-home.png")
        response = self.client.post("/api/v1/library/reference-objects", headers={"Idempotency-Key": uuid4().hex},
                                    json={"upload_intent_id": intent_id, "filename": "acme-charge-home.png",
                                          "declared_mime": "image/png"})
        self.assertEqual(response.status_code, 202, response.text)
        operation_id = response.json()["operation_id"]
        if drain:
            self.drain(job_types=("library.reference_gate",))
        with tenant_session(self.context) as session:
            operation = session.get(OperationRecord, operation_id)
            obj = session.scalar(select(ReferenceObjectRecord).where(
                ReferenceObjectRecord.workspace_id == self.context.workspace_id,
                ReferenceObjectRecord.content_sha256 == hashlib.sha256(raw).hexdigest()))
            return obj.id, operation.state

    def add_photo(self, project, object_id, role="main", expect=201, key=None):
        response = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/photos", headers=self.headers(key=key),
                                    json={"reference_object_id": object_id, "role": role})
        self.assertEqual(response.status_code, expect, response.text)
        if expect == 201:
            IdeasProjectDetail.model_validate(response.json())
        return response.json()

    def own_photo(self, project, raw=HOME, role="main"):
        """The user's photo, uploaded, gated and added: as ``photos`` lists it."""
        object_id, state = self.uploaded(raw)
        self.assertEqual(state, "succeeded")
        detail = self.add_photo(project, object_id, role)
        with tenant_session(self.context) as session:
            derivative = session.get(ReferenceObjectRecord, object_id).derivative_sha256
        return next(photo for photo in detail["photos"] if photo["photo_id"] == derivative)

    def decide(self, project, photo_ids=(), rejected=(), fact_ids=(), accept=()):
        return PhotoFixtures.decide(self, project, photo_ids=photo_ids, rejected=rejected, fact_ids=fact_ids, accept=accept)

    def draft_references(self, draft_id):
        return PhotoFixtures.draft_references(self, draft_id)

    def recipe_references(self, asset_version_id):
        return PhotoFixtures.recipe_references(self, asset_version_id)


class AddingPhotosTests(AddedPhotoFixtures):
    def test_a_product_without_photos_gets_one_and_the_images_use_it_once_checked(self):
        project = self.checked_ads()
        self.assertEqual(project["photos"], [])
        self.assertTrue(all(not ad["used_product_photo"] for ad in project["ads"]))
        photo = self.own_photo(project)
        self.assertEqual((photo["status"], photo["source"], photo["role"]), ("unverified", "added", "main"))
        self.assertEqual((photo["intake_id"], photo["image_id"], photo["source_url"]), (None, None, None))
        self.assertEqual(photo["asset_version_ids"], [])
        # Nothing was added to the reading: the intake is the record of what the reading found.
        # A new image of an ad is drafted with it, and is a draft until the user checks the photo.
        prepared = self.feedback(project, "c1_1")
        references = self.draft_references(prepared["draft_id"])
        self.assertEqual([(ref["content_sha256"], ref["attestation"], ref["selection_policy"]) for ref in references],
                         [(photo["photo_id"], UNVERIFIED, product_references.IDEAS_POLICY)])
        self.assertTrue(references[0]["source"].startswith("product_photo:mpp_"))
        self.make(prepared["draft_id"])
        # A correction sends the ad it corrects first, then the product photos.
        self.assertEqual(self.images.sent[-1][1:], [photo["photo_id"]])
        redone = self.by_origin(project, "corrected")[0]
        self.assertTrue(redone["used_product_photo"])
        self.assertEqual(redone["photo_ids"], [photo["photo_id"]])
        self.assertEqual(redone["status"], "draft")
        self.assertIn("photo_unverified", self.reasons(redone))
        self.assertNotIn("appearance_unconfirmed", self.reasons(redone))
        # Checked -- this is the product's real photo, usable in ads -- the very same image no longer waits for
        # it, and the photo is part of the product's published version as the user's own upload.
        detail = self.decide(project, photo_ids=[photo["photo_id"]])
        self.assertEqual([p["status"] for p in detail["photos"]], ["verified"])
        redone = self.by_origin(project, "corrected")[0]
        self.assertNotIn("photo_unverified", self.reasons(redone))
        self.assertNotIn("photo_unavailable", self.reasons(redone))
        version, _ = self.published_version(project)
        bindings = version.content_json["reference_bindings"]
        self.assertEqual(len(bindings), 1)
        self.assertTrue(bindings[0]["note"].startswith("product_photo:mpp_"))
        self.assertEqual(bindings[0]["reference_version_id"], detail["photos"][0]["reference_version_id"])
        with tenant_session(self.context) as session:
            from app.adapters.persistence.models import LibraryVersionRecord
            rights = session.get(LibraryVersionRecord, bindings[0]["reference_version_id"]).content_json["rights"]
        self.assertEqual(rights["scope"], "user_upload")
        self.assertTrue(rights["evidence"].startswith("marketing_product_photo:mpp_"))
        # Every later image uses the checked photo, as the product's verified photo.
        again = self.feedback(project, "c2_1")
        later = self.draft_references(again["draft_id"])
        self.assertEqual([ref["content_sha256"] for ref in later], [photo["photo_id"]])
        self.assertEqual(later[0]["attestation"], "verified_physical_product")

    def published_version(self, project):
        return PhotoFixtures.published_version(self, project)

    def test_an_ad_made_with_an_unchecked_added_photo_cannot_be_downloaded(self):
        project = self.checked_ads()
        photo = self.own_photo(project)
        prepared = self.feedback(project, "c1_1")
        self.make(prepared["draft_id"])
        redone = self.by_origin(project, "corrected")[0]
        response = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/download-intent", headers=self.headers(),
                                    json={"asset_version_ids": [redone["asset_version_id"]]})
        self.assertEqual((response.status_code, response.json()["code"]), (409, "ads_not_exportable"), response.text)
        self.assertIn("photo_unverified", [r["code"] for r in response.json()["params"]["ads"][0]["reasons"]])
        self.assertEqual(self.read_ideas(project)["photos"][0]["photo_id"], photo["photo_id"])

    def test_rejecting_an_added_photo_makes_the_ads_made_with_it_need_redoing(self):
        project = self.checked_ads()
        photo = self.own_photo(project)
        self.make(self.feedback(project, "c1_1")["draft_id"])
        detail = self.decide(project, rejected=[photo["photo_id"]])
        self.assertEqual([p["status"] for p in detail["photos"]], ["rejected"])
        redone = self.by_origin(project, "corrected")[0]
        self.assertEqual(redone["status"], "needs_redo")
        self.assertIn("photo_unavailable", self.reasons(redone))
        # And nothing new is made with it: the next image goes without a photo again.
        self.assertEqual(self.draft_references(self.feedback(project, "c2_1")["draft_id"]), [])

    def test_the_gate_refuses_an_image_and_the_photo_is_refused_saying_why(self):
        project = self.checked_ads()
        object_id, state = self.uploaded(b"\x89PNG\r\n\x1a\n" + b"not really a picture" * 40)
        self.assertEqual(state, "failed")
        refused = self.add_photo(project, object_id, expect=409)
        self.assertEqual(refused["code"], "photo_upload_rejected")
        self.assertEqual(refused["params"]["failure_code"], references.FAILURE_GATE_REJECTED)
        # The library's own read says the same, by the same stable code the page shows.
        gate = self.client.get(f"/api/v1/library/reference-objects/{object_id}").json()
        self.assertEqual((gate["state"], gate["failure_code"]), ("rejected", references.FAILURE_GATE_REJECTED))
        self.assertEqual(self.read_ideas(project)["photos"], [])
        self.assertEqual(self.count(MarketingProductPhotoRecord), 0)

    def test_an_image_still_being_checked_cannot_be_added_yet(self):
        project = self.checked_ads()
        object_id, state = self.uploaded(drain=False)
        self.assertEqual(state, "queued")
        self.assertEqual(self.add_photo(project, object_id, expect=409)["code"], "photo_upload_checking")
        self.drain(job_types=("library.reference_gate",))
        self.assertEqual(len(self.add_photo(project, object_id)["photos"]), 1)

    def test_the_same_image_is_one_photo_of_the_product(self):
        project = self.checked_ads()
        object_id, _ = self.uploaded()
        key = uuid4().hex
        first = self.add_photo(project, object_id, key=key)
        self.assertEqual(self.add_photo(project, object_id, key=key), first)
        self.add_photo(project, object_id)
        self.assertEqual(self.count(MarketingProductPhotoRecord), 1)
        refused = self.add_photo(project, object_id, role="detail", expect=409)
        self.assertEqual((refused["code"], refused["params"]["photo_id"]), ("photo_already_listed", first["photos"][0]["photo_id"]))
        # Uploading the same bytes again finds the same reference object, so it is still one photo.
        again, _ = self.uploaded()
        self.assertEqual(again, object_id)

    def test_an_unknown_image_is_not_found(self):
        project = self.checked_ads()
        self.assertEqual(self.add_photo(project, "refobj_missing", expect=404)["code"], "photo_upload_not_found")

    def test_a_photo_added_in_one_ideas_is_the_products_in_every_ideas_of_it(self):
        project = self.checked_ads()
        photo = self.own_photo(project)
        response = self.client.post("/api/v1/marketing/ideas", headers=self.headers(), json={
            "product_id": project["product_id"], "source_text": "Acme Charge Home, a home charger.",
            "market": "United States", "ad_language": "en-US"})
        self.assertEqual(response.status_code, 201, response.text)
        other = response.json()
        self.assertEqual([p["photo_id"] for p in other["photos"]], [photo["photo_id"]])
        # A decision in one is the product's: the photo checked here is checked there.
        self.decide(project, photo_ids=[photo["photo_id"]])
        self.assertEqual([p["status"] for p in self.read_ideas(other)["photos"]], ["verified"])

    def test_a_photo_is_shown_through_its_released_derivative(self):
        project = self.checked_ads()
        photo = self.own_photo(project)
        base = f"/api/v1/marketing/ideas/{project['id']}/photos"
        response = self.client.post(f"{base}/{photo['photo_id']}/download-intent", headers=self.headers())
        self.assertEqual(response.status_code, 201, response.text)
        self.assertNotIn("location", {name.lower() for name in response.headers})
        grant = IdeasPhotoDownloadGrant.model_validate(response.json())
        self.assertEqual((grant.photo_id, grant.width, grant.height), (photo["photo_id"], 480, 480))
        self.assertTrue(grant.download_url)
        missing = self.client.post(f"{base}/{'0' * 64}/download-intent", headers=self.headers())
        self.assertEqual((missing.status_code, missing.json()["code"]), (404, "photo_not_found"), missing.text)


class PhotoOrderTests(PhotoFixtures, AddedPhotoFixtures):
    """The reading found the main photo, a close-up and another angle; the user adds their own main photo."""

    def test_the_users_photo_comes_first_among_photos_checked_alike(self):
        project = self.checked_ads()
        found = self.photos(project)
        own = AddedPhotoFixtures.own_photo(self, project)
        listed = self.read_ideas(project)["photos"]
        # The list keeps the reading's photos where they were; the added one follows.
        self.assertEqual([p["source"] for p in listed], ["product_page", "product_page", "product_page", "added"])
        # None checked: the user's own photo leads, then the reading's in the page's order.
        prepared = self.feedback(project, "c1_1")
        self.assertEqual([ref["content_sha256"] for ref in self.draft_references(prepared["draft_id"])],
                         [own["photo_id"], found["main"]["photo_id"], found["detail"]["photo_id"]])
        # A checked photo comes before every unchecked one: the reading's checked main photo leads now.
        self.decide(project, photo_ids=[found["main"]["photo_id"]])
        prepared = self.feedback(project, "c2_1")
        self.assertEqual([ref["content_sha256"] for ref in self.draft_references(prepared["draft_id"])],
                         [found["main"]["photo_id"], own["photo_id"], found["detail"]["photo_id"]])
        # Checked alike again, the user's own photo leads.
        self.decide(project, photo_ids=[own["photo_id"]])
        prepared = self.feedback(project, "c3_1")
        self.assertEqual([ref["content_sha256"] for ref in self.draft_references(prepared["draft_id"])][:2],
                         [own["photo_id"], found["main"]["photo_id"]])

    def test_a_close_up_the_user_adds_never_displaces_the_main_photo(self):
        project = self.checked_ads()
        found = self.photos(project)
        own = AddedPhotoFixtures.own_photo(self, project, raw=PLUG, role="detail")
        prepared = self.feedback(project, "c1_1")
        # The main photo is the first one called main, the reading's here; the user's close-up comes next.
        self.assertEqual([ref["content_sha256"] for ref in self.draft_references(prepared["draft_id"])],
                         [found["main"]["photo_id"], own["photo_id"], found["detail"]["photo_id"]])

    def test_adding_a_photo_the_reading_already_found_is_refused(self):
        project = self.checked_ads()
        with tenant_session(self.context) as session:
            row = ideas.get_ideas_project(session, self.context, project["id"])
            main = next(photo for photo in product_references.candidates(session, self.context, row)
                        if photo["role"] == "main")
        refused = self.add_photo(project, main["reference_object_id"], expect=409)
        self.assertEqual((refused["code"], refused["params"]["photo_id"]), ("photo_already_listed", main["photo_id"]))


class AddedPhotoIsolationTests(AddedPhotoFixtures):
    def test_another_workspace_can_neither_add_read_nor_show_these_photos(self):
        project = self.checked_ads()
        photo = self.own_photo(project)
        object_id, _ = self.uploaded(PLUG)
        owner = self.context
        self.context = self._make_context()
        base = f"/api/v1/marketing/ideas/{project['id']}"
        attempts = (self.client.post(f"{base}/photos", headers=self.headers(), json={"reference_object_id": object_id}),
                    self.client.post(f"{base}/photos/{photo['photo_id']}/download-intent", headers=self.headers()))
        for response in attempts:
            self.assertEqual((response.status_code, response.json()["code"]), (404, "marketing_project_not_found"), response.text)
        # Its own ideas cannot take the other Workspace's upload either: there is no such image here.
        mine = self.client.post("/api/v1/marketing/ideas", headers=self.headers(), json={
            "product_id": self.product(), "source_text": "Brightvolt Mini, a travel charger.", "market": "United States",
            "ad_language": "en-US"}).json()
        refused = self.client.post(f"/api/v1/marketing/ideas/{mine['id']}/photos", headers=self.headers(),
                                   json={"reference_object_id": object_id})
        self.assertEqual((refused.status_code, refused.json()["code"]), (404, "photo_upload_not_found"), refused.text)
        with tenant_session(self.context) as session:
            with tenant_session(owner) as theirs:
                row = ideas.get_ideas_project(theirs, owner, project["id"])
                theirs.expunge(row)
            # Even handed the row, its own session finds none of the product's added photos.
            self.assertEqual(product_references.added_photos(session, self.context, row.product_id), [])
            self.assertEqual(product_references.candidates(session, self.context, row), [])
            self.assertEqual(session.scalar(select(func.count()).select_from(MarketingProductPhotoRecord).where(
                MarketingProductPhotoRecord.workspace_id == self.context.workspace_id)), 0)
        self.context = owner
        self.assertEqual([p["photo_id"] for p in self.read_ideas(project)["photos"]], [photo["photo_id"]])
        self.assertEqual(self.count(MarketingProductPhotoRecord), 1)

    def test_a_plan_first_test_of_the_product_never_draws_on_an_unchecked_added_photo(self):
        project = self.checked_ads()
        self.own_photo(project)
        with tenant_session(self.context) as session:
            row = ideas.get_ideas_project(session, self.context, project["id"])
            # The plan-first selection reads only the product's published, verified photos.
            self.assertEqual(product_references.from_product_set(session, self.context, row), [])
            self.assertEqual(ideas_export.photo_states(session, self.context, row)[
                self.read_ideas(project)["photos"][0]["photo_id"]], "unverified")


class AddedPhotosMigrationTests(unittest.TestCase):
    """Dropping the added photos would lose which images the user added to which product: downgrade refuses."""
    ROOT = Path(__file__).resolve().parents[1]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="marketai-added-photos-migration-")
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

    def insert(self, role="main"):
        with sqlite3.connect(self.database) as connection:
            connection.execute("INSERT INTO marketing_product_photos (id, workspace_id, product_id, project_id,"
                               f" reference_object_id, role, created_at) VALUES ('mpp_1', 'ws_1', 'prod_1', 'mp_1',"
                               f" 'refobj_1', '{role}', '2026-11-18')")

    def test_downgrade_without_added_photos_drops_the_table(self):
        self.assertIn("marketing_product_photos", self.tables())
        downgraded = self.alembic("downgrade", "zzq200a1b2c3")
        self.assertEqual(downgraded.returncode, 0, downgraded.stderr)
        self.assertNotIn("marketing_product_photos", self.tables())

    def test_downgrade_refuses_while_an_added_photo_exists(self):
        self.insert()
        downgraded = self.alembic("downgrade", "zzq200a1b2c3")
        self.assertNotEqual(downgraded.returncode, 0)
        self.assertIn("photos added to products exist", downgraded.stderr)
        self.assertIn("marketing_product_photos", self.tables())

    def test_a_photo_shows_the_product_another_angle_a_close_up_or_the_logo(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert(role="in_use")
