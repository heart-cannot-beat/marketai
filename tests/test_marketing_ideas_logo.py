"""The brand's logo on an ads-first product (#233; design doc §5.7 "参考照片、Logo → 产品资料：产品照片、品牌").

The user uploads the Acme Charge logo through the same reference upload gate as a product photo and adds it
to the product as its logo. It is listed apart from the photos and never sent as the product's look. Checked
-- this is the brand's logo and may be used in ads -- it becomes part of the product's published version as a
logo, and the product's brand baseline can choose it. Rejected, it leaves the version again. Everything runs
through the real endpoints and the real Worker gate, with a fake model and a fake image provider.
"""
import dataclasses
from uuid import uuid4

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import LibraryVersionRecord
from app.marketing import ideas, ideas_export, product_references
from tests.support.library_fixtures import png_bytes
from tests.test_marketing_ideas_added_photos import HOME, AddedPhotoFixtures
from tests.test_marketing_ideas_photos import PhotoFixtures

#: The fictional Acme Charge logo: a small mark on white.
LOGO = png_bytes((300, 120), (250, 250, 250))
KIT = {"fonts": {"primary": "Inter", "secondary": "Source Serif"}, "palette": ["#0A3D62", "#F5F6FA"]}


class LogoFixtures(AddedPhotoFixtures):
    def own_logo(self, project):
        object_id, state = self.uploaded(LOGO)
        self.assertEqual(state, "succeeded")
        detail = self.add_photo(project, object_id, role="logo")
        self.assertEqual(len(detail["logos"]), 1)
        return detail["logos"][0]

    def published_version(self, project):
        return PhotoFixtures.published_version(self, project)

    def as_owner(self):
        return dataclasses.replace(self.context, role="owner", initiator_role="owner")

    def references(self, product_id):
        response = self.client.get(f"/api/v1/products/{product_id}/references")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["items"]

    def save_kit(self, product_id, logo_item_id, expect=200):
        member, self.context = self.context, self.as_owner()
        try:
            path = f"/api/v1/marketing/products/{product_id}/brand-kit"
            revision = self.client.get(path).json()["revision"]
            response = self.client.put(path, json={**KIT, "logo": {"item_id": logo_item_id, "placement": "top_right"}},
                                       headers={"Idempotency-Key": uuid4().hex, "If-Match": f'"{revision}"'})
            self.assertEqual(response.status_code, expect, response.text)
            return response.json()
        finally:
            self.context = member


class LogoTests(LogoFixtures):
    def test_a_logo_is_listed_apart_and_never_stands_in_for_the_products_look(self):
        project = self.checked_ads()
        logo = self.own_logo(project)
        self.assertEqual((logo["status"], logo["reference_version_id"]), ("unverified", None))
        detail = self.read_ideas(project)
        self.assertEqual(detail["photos"], [])
        # A new image of an ad is still made without a product photo: a logo is not the product.
        self.assertEqual(self.draft_references(self.feedback(project, "c1_1")["draft_id"]), [])
        with tenant_session(self.context) as session:
            row = ideas.get_ideas_project(session, self.context, project["id"])
            self.assertEqual(product_references.candidates(session, self.context, row), [])
            self.assertEqual(product_references.for_ideas(session, self.context, row), [])
        # It is shown like a photo, from its released copy.
        shown = self.client.post(f"/api/v1/marketing/ideas/{project['id']}/photos/{logo['photo_id']}/download-intent",
                                 headers=self.headers())
        self.assertEqual(shown.status_code, 201, shown.text)

    def test_a_checked_logo_can_be_chosen_in_the_brand_baseline(self):
        project = self.checked_ads()
        logo = self.own_logo(project)
        # Nothing checked yet: the product has no published version, so no logo to choose.
        self.assertEqual(self.client.get(f"/api/v1/products/{project['product_id']}/references").status_code, 409)
        detail = self.decide(project, photo_ids=[logo["photo_id"]])
        checked = detail["logos"][0]
        self.assertEqual(checked["status"], "verified")
        self.assertTrue(checked["reference_version_id"])
        # The product's version holds it as a logo, declared as the brand's own design.
        version, _ = self.published_version(project)
        bindings = version.content_json["reference_bindings"]
        self.assertEqual([(b["purpose"], b["reference_version_id"]) for b in bindings], [("logo", checked["reference_version_id"])])
        with tenant_session(self.context) as session:
            rights = session.get(LibraryVersionRecord, checked["reference_version_id"]).content_json["rights"]
        self.assertEqual(rights["scope"], "owned_brand_asset")
        # The brand baseline lists it and takes it.
        items = self.references(project["product_id"])
        self.assertEqual([(item["role"], item["available"], item["note"]) for item in items], [("logo", True, "")])
        saved = self.save_kit(project["product_id"], items[0]["item_id"])
        self.assertEqual(saved["current"]["content"]["logo"]["reference_version_id"], checked["reference_version_id"])
        self.assertTrue(saved["current"]["logo_available"])
        # Checking the logo checked no photo: the ads still say they were made without one.
        self.assertTrue(all(not ad["used_product_photo"] for ad in self.read_ideas(project)["ads"]))

    def test_a_logo_and_a_photo_are_both_bound_each_as_what_it_is(self):
        project = self.checked_ads()
        logo = self.own_logo(project)
        photo = self.own_photo(project, raw=HOME)
        self.decide(project, photo_ids=[logo["photo_id"], photo["photo_id"]])
        version, _ = self.published_version(project)
        purposes = [(b["purpose"], b["conditions"]["product_reference_item"]["role"])
                    for b in version.content_json["reference_bindings"]]
        # The photo is the product's main photo even though the logo was added first.
        self.assertEqual(purposes, [("product_reference", "primary"), ("logo", "logo")])
        self.assertEqual([item["role"] for item in self.references(project["product_id"])], ["primary", "logo"])

    def test_a_rejected_logo_leaves_the_product_and_can_no_longer_be_chosen(self):
        project = self.checked_ads()
        logo = self.own_logo(project)
        self.decide(project, photo_ids=[logo["photo_id"]])
        item = self.references(project["product_id"])[0]
        self.save_kit(project["product_id"], item["item_id"])
        detail = self.decide(project, rejected=[logo["photo_id"]])
        self.assertEqual(detail["logos"][0]["status"], "rejected")
        self.assertEqual(self.references(project["product_id"]), [])
        # A baseline saved before is an immutable version the Owner saved: it keeps its logo until the Owner
        # changes it, as when a logo is taken off a product's reference settings (the rejection does not
        # withdraw the library reference). The profile says so before the user rejects it.
        kit = self.client.get(f"/api/v1/marketing/products/{project['product_id']}/brand-kit").json()
        self.assertEqual(kit["current"]["content"]["logo"]["item_id"], item["item_id"])
        self.save_kit(project["product_id"], item["item_id"], expect=422)

    def test_the_same_image_is_either_a_photo_or_the_logo(self):
        project = self.checked_ads()
        logo = self.own_logo(project)
        object_id, _ = self.uploaded(LOGO)
        refused = self.add_photo(project, object_id, role="main", expect=409)
        self.assertEqual((refused["code"], refused["params"]["photo_id"]), ("photo_already_listed", logo["photo_id"]))

    def test_another_workspace_sees_no_logo(self):
        project = self.checked_ads()
        self.own_logo(project)
        owner = self.context
        self.context = self._make_context()
        with tenant_session(self.context) as session:
            with tenant_session(owner) as theirs:
                row = ideas.get_ideas_project(theirs, owner, project["id"])
                theirs.expunge(row)
            self.assertEqual(product_references.added_logos(session, self.context, row.product_id), [])
            self.assertEqual(ideas_export.logo_list(session, self.context, row), [])
        self.context = owner
        self.assertEqual(len(self.read_ideas(project)["logos"]), 1)
