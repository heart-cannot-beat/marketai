"""Issue #162 -- ``GET /api/v1/products``: the product list the front flow reads.

The product home, the product names on Generation pages and the New Draft
picker must never show ``prod_`` ids (front-flow contract §1 principle 2),
so this read projects the name, single confirmed model, main image and the
latest Generation Run for every product of the Workspace.

* A published product carries its version's name, single variant label and
  first-priority reference binding; it is ``ready`` for a new draft.
* A product entry still in Draft is listed but not ``ready``; a legacy
  product without a Library profile is ``ready``; an archived one is gone.
* The latest Run of each product is summarized.
* Negative tenant: another Workspace's products never appear.
"""
from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from tests.support.library_fixtures import LibraryFixtures

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    GenerationRunRecord,
    PlanRecord,
    ProductRecord,
    new_id,
)
from app.library import service
from app.library.contracts import ENTRY_PRODUCT
from app.main import app
from app.platform.http_identity import request_tenant_context


class ProductCatalogTests(LibraryFixtures):
    def setUp(self) -> None:
        self.member = self.ctx()
        self.owner = self.owner_ctx(self.member)
        self.current = self.member
        app.dependency_overrides[request_tenant_context] = lambda: self.current
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    # -- helpers ----------------------------------------------------------

    def _list(self) -> list[dict]:
        response = self.client.get("/api/v1/products")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["items"]

    def _published_product(self, name: str, **overrides) -> tuple[str, str, str]:
        _, reference = self._reference_version(self.owner, name=f"{name} ref")
        _, group = self._group_version(self.owner, reference["id"])
        content = self._product_content(reference["id"], group["id"], name=name, **overrides)
        entry_id, _ = self._publish(self.owner, ENTRY_PRODUCT, name, content)
        with tenant_session(self.owner) as session:
            product_id = session.query(ProductRecord).filter_by(
                workspace_id=self.owner.workspace_id, product_profile_entry_id=entry_id,
            ).one().id
        return entry_id, product_id, reference["id"]

    def _run(self, product_id: str, *, status: str, done: int, total: int) -> str:
        with tenant_session(self.owner) as session:
            plan = PlanRecord(
                id=new_id("plan"), workspace_id=self.owner.workspace_id, product_id=product_id,
                mode="full", cap=0, total_full=0, snapshot_hash="snap_" + new_id("h"),
            )
            session.add(plan)
            session.flush()
            run = GenerationRunRecord(
                id=new_id("gr"), workspace_id=self.owner.workspace_id, product_id=product_id,
                plan_id=plan.id, provider="mock", model="mock-v1", aspect_ratio="1:1",
                image_size="1024x1024", variants=1, use_ref=False, status=status,
                total=total, done=done, failed=0, est_cost=Decimal("0"), currency="USD", revision=1,
            )
            session.add(run)
            session.flush()
            return run.id

    # -- tests ------------------------------------------------------------

    def test_published_product_lists_name_variant_main_image_and_is_ready(self):
        entry_id, product_id, reference_id = self._published_product(
            "VoltNest 壁挂充电桩",
            model_variants=[{"label": "11 kW", "specs": {"功率": "11 kW"}}],
        )

        [item] = self._list()

        self.assertEqual(item["product_id"], product_id)
        self.assertEqual(item["entry_id"], entry_id)
        self.assertEqual(item["name"], "VoltNest 壁挂充电桩")
        self.assertEqual(item["variant_label"], "11 kW")
        self.assertTrue(item["ready"])
        self.assertEqual(item["primary_reference_version_id"], reference_id)
        self.assertIsNone(item["latest_run"])

    def test_several_variants_have_no_single_label(self):
        self._published_product(
            "双款",
            model_variants=[{"label": "A", "specs": {}}, {"label": "B", "specs": {}}],
        )
        [item] = self._list()
        self.assertIsNone(item["variant_label"])

    def test_draft_only_entry_is_listed_but_not_ready(self):
        entry = service.create_entry(self.member, entry_type=ENTRY_PRODUCT, display_name="还没发布", content={})

        [item] = self._list()

        self.assertEqual(item["entry_id"], entry["id"])
        self.assertEqual(item["name"], "还没发布")
        self.assertFalse(item["ready"])
        self.assertIsNone(item["primary_reference_version_id"])

    def test_legacy_product_without_profile_is_ready(self):
        product_id = self._product_id(self.member)
        [item] = self._list()
        self.assertEqual(item["product_id"], product_id)
        self.assertIsNone(item["entry_id"])
        self.assertEqual(item["name"], "Test Product")
        self.assertTrue(item["ready"])

    def test_archived_product_is_not_listed(self):
        entry_id, _, _ = self._published_product("要归档的")
        entry = service.get_entry(self.owner, entry_id)
        service.archive_entry(self.owner, entry_id, expected_revision=entry["draft"]["revision"])
        self.assertEqual(self._list(), [])

    def test_latest_run_is_summarized(self):
        _, product_id, _ = self._published_product("有运行的")
        self._run(product_id, status="succeeded", done=4, total=4)
        self._run(product_id, status="running", done=1, total=6)

        [item] = self._list()

        self.assertEqual(item["latest_run"]["status"], "running")
        self.assertEqual(item["latest_run"]["done"], 1)
        self.assertEqual(item["latest_run"]["total"], 6)

    def test_another_workspace_never_sees_these_products(self):
        self._published_product("别人的产品")
        self._product_id(self.member)
        self.assertEqual(len(self._list()), 2)

        self.current = self.ctx()  # a fresh Workspace, member of nothing above
        self.assertEqual(self._list(), [])

    def test_owner_and_member_read_the_same_list(self):
        self._published_product("共享")
        member_view = self._list()
        self.current = self.owner
        self.assertEqual(self._list(), member_view)


class ProductCardTestAnchorTests(ProductCatalogTests):
    """J-32: a marketing test started from a product card adds no second card."""

    def _start_test(self, product_id: str, *, confirm: bool = True, revise: bool = False) -> str:
        from app.marketing import service as marketing
        from app.marketing.contracts import Understanding
        with tenant_session(self.member) as session:
            row = marketing.create_project(session, self.member, "", product_id)
            if revise:
                marketing.edit_project(session, self.member, row, row.revision, "revise_source",
                                       source_text="A different charger described from scratch.")
            if confirm:
                quote = row.source_text.splitlines()[0]
                understanding = Understanding.model_validate({
                    "product_name": "Alpha in the test's words", "category_need": "Charge at home",
                    "product_choice_reason": "Small and fast", "audience": "Commuters", "situations": ["Home"],
                    "objections": [], "facts": [{"id": "f1", "statement": quote, "source_quote": quote,
                                                 "confirmation": "confirmed"}],
                    "inferences": [{"id": "i1", "statement": "Commuters may value home charging.",
                                    "rationale": "Example only.", "fact_ids": ["f1"]}], "unknowns": []})
                marketing.edit_project(session, self.member, row, row.revision, "confirm_understanding",
                                       understanding=understanding)
            return row.product_id

    def test_the_tests_anchor_stays_off_the_list_and_its_run_counts_for_the_card(self):
        _, product_id, _ = self._published_product("Alpha")
        anchor = self._start_test(product_id)
        self.assertNotEqual(anchor, product_id)
        self._run(anchor, status="succeeded", done=2, total=2)
        items = self._list()
        self.assertEqual([item["product_id"] for item in items], [product_id])
        self.assertEqual(items[0]["name"], "Alpha")
        self.assertEqual(items[0]["latest_run"]["done"], 2)

    def test_a_pasted_test_and_a_revised_one_still_show_their_own_product(self):
        _, product_id, _ = self._published_product("Alpha")
        revised = self._start_test(product_id, revise=True)
        with tenant_session(self.member) as session:
            from app.marketing import service as marketing
            pasted = marketing.create_project(session, self.member, "Beta charger, pasted text")
        self.assertIn(revised, [item["product_id"] for item in self._list()])
        self.assertIsNone(pasted.product_id)

    def test_another_workspace_never_hides_or_borrows_these_products(self):
        _, product_id, _ = self._published_product("Alpha")
        anchor = self._start_test(product_id)
        self._run(anchor, status="succeeded", done=1, total=1)
        self.current = self.ctx()
        self.assertEqual(self._list(), [])
