"""J-32: a test started from a product card carries the confirmed card and its verified photos."""
import re

from sqlalchemy import func, select

from app.adapters.persistence import models as m
from app.adapters.persistence.database import tenant_session
from app.library import service as library
from app.library.contracts import ENTRY_PRODUCT
from app.marketing.product_references import from_product_set
from app.marketing.service import get_project
from tests.test_marketing_product_references import AUTO, ProductPhotoFixtures

INTERNAL_ID = re.compile(r"\b(?:lib|libver|prod|ref|refobj|mm|mp|mr|av)_[A-Za-z0-9]+")


class TestProductCardEntry(ProductPhotoFixtures):
    MODEL = "Acme Charge 65W"

    def setUp(self):
        super().setUp()
        _, hero = self._reference_version(self.context, name="Legacy hero")
        _, group = self._group_version(self.context, hero["id"])
        content = self._product_content(hero["id"], group["id"], model_variants=[{"label": self.MODEL, "specs": {"Power": "65W"}}],
            audiences=[{"who": "Business travellers", "why": "One charger for every device", "pain": "Too many bricks"}])
        self.entry_id, _ = self._publish(self.context, ENTRY_PRODUCT, "Acme Charge", content)
        with tenant_session(self.context) as s:
            self.product_id = s.scalar(select(m.ProductRecord.id).where(m.ProductRecord.product_profile_entry_id == self.entry_id))

    def start(self, product_id, source_text=""):
        return self.client.post("/api/v1/marketing/projects?include_languages=true&include_plan_review=true",
            headers=self.headers(), json={"source_text": source_text, "product_id": product_id})

    def products(self):
        with tenant_session(self.context) as s:
            return s.scalar(select(func.count()).select_from(m.ProductRecord).where(m.ProductRecord.workspace_id == self.context.workspace_id))

    def test_the_product_card_alone_starts_a_test_with_its_confirmed_card_and_no_internal_ids(self):
        before = self.products()
        response = self.start(self.product_id)  # a Member, with nothing pasted
        self.assertEqual(response.status_code, 201, response.text)
        project = response.json()
        text = project["source_text"]
        self.assertEqual(text.splitlines()[0], "Alpha 充电器")
        for expected in ("Model: Acme Charge 65W", "Description: 65W 氮化镓充电器", "Audience: 差旅办公人群", "- 体积小",
                         "- Power: 65W", "- Who: Business travellers; Why they buy: One charger for every device; Pain point: Too many bricks",
                         "Must match in every image:\n- 正面 Logo 完整可读"):
            self.assertIn(expected, text)
        self.assertIsNone(INTERNAL_ID.search(text), text)
        self.assertEqual(project["product_id"], self.product_id)
        self.assertEqual(self.products(), before)  # the bridge is the anchor; no second product card appears

    def test_a_library_entry_id_reuses_the_bridge_instead_of_minting_another_product(self):
        before = self.products()
        response = self.start(self.entry_id, "Launch test for travellers")
        self.assertEqual(response.status_code, 201, response.text)
        project = response.json()
        self.assertEqual(project["product_id"], self.product_id)
        self.assertTrue(project["source_text"].startswith("Launch test for travellers\n\nConfirmed product card:\nAlpha 充电器\n"))
        self.assertEqual(self.products(), before)
        self.assertEqual([p["product_id"] for p in self.client.get("/api/v1/products").json()["items"]].count(self.entry_id), 0)

    def test_verified_photos_of_the_started_product_are_frozen_automatically(self):
        photo, _ = self.photo((14, 24, 34))
        path = f"/api/v1/products/{self.product_id}/references"
        bound = self.client.post(path, headers={"Idempotency-Key": m.new_id("key"), "If-Match": str(self.client.get(path).json()["revision"])},
            json={"reference_version_id": photo, "model_variant": self.MODEL, "role": "primary", "identity_status": "verified_physical", "note": ""})
        self.assertEqual(bound.status_code, 200, bound.text)
        project = self.start(self.product_id).json()
        with tenant_session(self.context) as s:
            self.assertEqual([r["reference_version_id"] for r in from_product_set(s, self.context, get_project(s, self.context, project["id"]))], [photo])

    def test_other_workspaces_and_unpublished_products_cannot_start_a_test(self):
        mine = self.context
        self.context = self._make_context()
        refused = self.start(self.product_id)
        self.assertGreaterEqual(refused.status_code, 400, refused.text)
        self.assertEqual(refused.json()["code"], "product_unavailable")
        self.assertEqual(self.client.get("/api/v1/marketing/projects").json()["items"], [])
        self.context = mine
        draft = library.create_entry(self.context, entry_type=ENTRY_PRODUCT, display_name="Draft only", content={})
        with tenant_session(self.context) as s:
            unpublished = s.scalar(select(m.ProductRecord.id).where(m.ProductRecord.product_profile_entry_id == draft["id"]))
        refused = self.start(unpublished)
        self.assertEqual(refused.status_code, 422, refused.text)
        self.assertEqual(refused.json()["code"], "product_unavailable")
