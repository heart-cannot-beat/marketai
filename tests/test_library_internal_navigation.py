"""Tests for Gate 0.5 — Server-verified internal resource navigation.

Covers:
- Context registration (register_context idempotency)
- List internal entries under a root, filtered by type and parent
- Unscoped canonical metadata (get_internal_entry)
- Verified root/parent/resource context (get_internal_entry_context)
- Create internal entry with automatic context binding
- Cross-workspace isolation (navigation never leaks across tenants)
- Mismatched type / root / parent rejection
- Shared internal types (reference_asset) via any root
- Pagination is one server cursor stream
- HTTP routes exercise the navigation module end-to-end
"""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from tests.support.library_fixtures import LibraryFixtures

from app.library import navigation, service
from app.library.contracts import (
    CONTEXT_REGISTRY,
    ENTRY_DATA_SOURCE,
    ENTRY_DIMENSION_SCHEMA,
    ENTRY_MANUAL_FACT_SET,
    ENTRY_MARKET_MAPPING,
    ENTRY_OFFER,
    ENTRY_PERSON,
    ENTRY_PRODUCT,
    ENTRY_REFERENCE_ASSET,
    ENTRY_REFERENCE_GROUP,
    ENTRY_SELECTION_RULE,
    ENTRY_TRUTH_RESOLUTION_POLICY,
    INTERNAL_TYPES,
    ROOT_CATEGORIES,
    SHARED_INTERNAL_TYPES,
)
from app.library.navigation import (
    NavigationConflict,
    NavigationNotFound,
)
from app.main import app
from app.platform.http_identity import request_tenant_context


# ---------------------------------------------------------------------------
# Service-level tests
# ---------------------------------------------------------------------------


class TestContextRegistration(LibraryFixtures):

    def test_register_context_creates_record(self):
        ctx = self.ctx()
        stack = self._full_stack(ctx)
        product_entry_id = stack["product"][0]
        offer_entry_id = stack["offer"][0]

        from app.adapters.persistence.database import tenant_session

        with tenant_session(ctx) as session:
            record = navigation.register_context(
                session, ctx,
                root_entry_id=product_entry_id,
                resource_entry_id=offer_entry_id,
                relation_kind="product_offer",
                creation_evidence="test",
            )
            self.assertEqual(record.root_entry_id, product_entry_id)
            self.assertEqual(record.resource_entry_id, offer_entry_id)
            self.assertEqual(record.relation_kind, "product_offer")

    def test_register_context_is_idempotent(self):
        ctx = self.ctx()
        stack = self._full_stack(ctx)
        product_entry_id = stack["product"][0]
        offer_entry_id = stack["offer"][0]

        from app.adapters.persistence.database import tenant_session

        with tenant_session(ctx) as session:
            first = navigation.register_context(
                session, ctx,
                root_entry_id=product_entry_id,
                resource_entry_id=offer_entry_id,
                relation_kind="product_offer",
            )
            second = navigation.register_context(
                session, ctx,
                root_entry_id=product_entry_id,
                resource_entry_id=offer_entry_id,
                relation_kind="product_offer",
            )
            self.assertEqual(first.id, second.id)


class TestListInternalEntries(LibraryFixtures):

    def _setup_product_with_offer(self):
        ctx = self.ctx()
        owner = self.owner_ctx(ctx)
        stack = self._full_stack(ctx, owner)
        product_entry_id = stack["product"][0]
        offer_entry_id = stack["offer"][0]

        from app.adapters.persistence.database import tenant_session

        with tenant_session(ctx) as session:
            navigation.register_context(
                session, ctx,
                root_entry_id=product_entry_id,
                resource_entry_id=offer_entry_id,
                relation_kind="product_offer",
                creation_evidence="test_setup",
            )
        return ctx, product_entry_id, offer_entry_id

    def test_list_returns_registered_entries(self):
        ctx, product_id, offer_id = self._setup_product_with_offer()
        items = navigation.list_internal_entries(ctx, product_id, ENTRY_OFFER)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], offer_id)
        self.assertEqual(items[0]["entry_type"], ENTRY_OFFER)
        self.assertEqual(items[0]["root_entry_id"], product_id)

    def test_list_returns_empty_for_wrong_type(self):
        ctx, product_id, _ = self._setup_product_with_offer()
        items = navigation.list_internal_entries(ctx, product_id, ENTRY_REFERENCE_GROUP)
        self.assertEqual(items, [])

    def test_list_rejects_non_root_entry(self):
        ctx, _, offer_id = self._setup_product_with_offer()
        with self.assertRaises(NavigationConflict):
            navigation.list_internal_entries(ctx, offer_id, ENTRY_OFFER)

    def test_list_rejects_cross_workspace(self):
        ctx, product_id, _ = self._setup_product_with_offer()
        other = self.ctx()
        with self.assertRaises(NavigationNotFound):
            navigation.list_internal_entries(other, product_id, ENTRY_OFFER)

    def test_list_filters_by_parent(self):
        ctx = self.ctx()
        owner = self.owner_ctx(ctx)
        stack = self._full_stack(ctx, owner)
        product_id = stack["product"][0]
        offer_id = stack["offer"][0]
        mapping_id = stack["mapping"][0]

        from app.adapters.persistence.database import tenant_session

        with tenant_session(ctx) as session:
            navigation.register_context(
                session, ctx,
                root_entry_id=product_id,
                resource_entry_id=offer_id,
                relation_kind="product_offer",
            )
            navigation.register_context(
                session, ctx,
                root_entry_id=product_id,
                resource_entry_id=mapping_id,
                relation_kind="product_market_mapping",
                parent_entry_id=offer_id,
            )

        all_mappings = navigation.list_internal_entries(
            ctx, product_id, ENTRY_MARKET_MAPPING,
        )
        self.assertEqual(len(all_mappings), 1)
        self.assertEqual(all_mappings[0]["id"], mapping_id)

        filtered = navigation.list_internal_entries(
            ctx, product_id, ENTRY_MARKET_MAPPING,
            parent_type=ENTRY_OFFER, parent_id=offer_id,
        )
        self.assertEqual(len(filtered), 1)


class TestGetInternalEntry(LibraryFixtures):

    def test_returns_canonical_metadata(self):
        ctx = self.ctx()
        owner = self.owner_ctx(ctx)
        stack = self._full_stack(ctx, owner)
        offer_id = stack["offer"][0]
        product_id = stack["product"][0]

        from app.adapters.persistence.database import tenant_session

        with tenant_session(ctx) as session:
            navigation.register_context(
                session, ctx,
                root_entry_id=product_id,
                resource_entry_id=offer_id,
                relation_kind="product_offer",
            )

        meta = navigation.get_internal_entry(ctx, ENTRY_OFFER, offer_id)
        self.assertEqual(meta["id"], offer_id)
        self.assertEqual(meta["entry_type"], ENTRY_OFFER)
        self.assertIn("host_category", meta)
        self.assertEqual(meta["context_count"], 1)

    def test_reference_asset_reports_product_host_category(self):
        ctx = self.ctx()
        stack = self._full_stack(ctx)
        ref_id = stack["reference"][0]

        meta = navigation.get_internal_entry(ctx, ENTRY_REFERENCE_ASSET, ref_id)
        self.assertEqual(meta["host_category"], "product")

    def test_rejects_type_mismatch(self):
        ctx = self.ctx()
        stack = self._full_stack(ctx)
        offer_id = stack["offer"][0]
        with self.assertRaises(NavigationConflict):
            navigation.get_internal_entry(ctx, ENTRY_PRODUCT, offer_id)

    def test_rejects_cross_workspace(self):
        ctx = self.ctx()
        stack = self._full_stack(ctx)
        offer_id = stack["offer"][0]
        other = self.ctx()
        with self.assertRaises(NavigationNotFound):
            navigation.get_internal_entry(other, ENTRY_OFFER, offer_id)

    def test_rejects_nonexistent_entry(self):
        ctx = self.ctx()
        with self.assertRaises(NavigationNotFound):
            navigation.get_internal_entry(ctx, ENTRY_OFFER, "nonexistent_id")


class TestGetInternalEntryContext(LibraryFixtures):

    def _setup_context(self):
        ctx = self.ctx()
        owner = self.owner_ctx(ctx)
        stack = self._full_stack(ctx, owner)
        product_id = stack["product"][0]
        offer_id = stack["offer"][0]

        from app.adapters.persistence.database import tenant_session

        with tenant_session(ctx) as session:
            navigation.register_context(
                session, ctx,
                root_entry_id=product_id,
                resource_entry_id=offer_id,
                relation_kind="product_offer",
                creation_evidence="test_context",
            )
        return ctx, product_id, offer_id

    def test_returns_verified_context(self):
        ctx, product_id, offer_id = self._setup_context()
        result = navigation.get_internal_entry_context(
            ctx, product_id, ENTRY_OFFER, offer_id,
        )
        self.assertEqual(result["id"], offer_id)
        self.assertEqual(result["root_entry_id"], product_id)
        self.assertEqual(result["root_entry_type"], ENTRY_PRODUCT)
        self.assertEqual(result["relation_kind"], "product_offer")
        self.assertEqual(result["creation_evidence"], "test_context")

    def test_rejects_wrong_root(self):
        ctx, product_id, offer_id = self._setup_context()
        other_stack = self._full_stack(ctx)
        other_product_id = other_stack["product"][0]
        with self.assertRaises(NavigationNotFound):
            navigation.get_internal_entry_context(
                ctx, other_product_id, ENTRY_OFFER, offer_id,
            )

    def test_rejects_type_mismatch(self):
        ctx, product_id, offer_id = self._setup_context()
        with self.assertRaises(NavigationConflict):
            navigation.get_internal_entry_context(
                ctx, product_id, ENTRY_REFERENCE_GROUP, offer_id,
            )

    def test_rejects_cross_workspace(self):
        ctx, product_id, offer_id = self._setup_context()
        other = self.ctx()
        with self.assertRaises(NavigationNotFound):
            navigation.get_internal_entry_context(
                other, product_id, ENTRY_OFFER, offer_id,
            )


class TestCreateInternalEntry(LibraryFixtures):

    def test_create_offer_under_product_registers_context(self):
        ctx = self.ctx()
        owner = self.owner_ctx(ctx)
        stack = self._full_stack(ctx, owner)
        product_id = stack["product"][0]
        product_ver = stack["product"][1]

        entry = navigation.create_internal_entry(
            ctx, product_id,
            entry_type=ENTRY_OFFER,
            display_name="Nav Offer",
            content=self._offer_content(product_ver["id"]),
        )
        self.assertIn("id", entry)
        self.assertEqual(entry["root_entry_id"], product_id)

        items = navigation.list_internal_entries(ctx, product_id, ENTRY_OFFER)
        ids = [i["id"] for i in items]
        self.assertIn(entry["id"], ids)

    def test_rejects_root_type_as_internal(self):
        ctx = self.ctx()
        stack = self._full_stack(ctx)
        product_id = stack["product"][0]

        with self.assertRaises(NavigationConflict):
            navigation.create_internal_entry(
                ctx, product_id,
                entry_type=ENTRY_PRODUCT,
                display_name="Bad Product",
            )

    def test_rejects_mismatched_internal_type_for_root(self):
        ctx = self.ctx()
        stack = self._full_stack(ctx)
        product_id = stack["product"][0]

        with self.assertRaises(NavigationConflict):
            navigation.create_internal_entry(
                ctx, product_id,
                entry_type=ENTRY_MANUAL_FACT_SET,
                display_name="Wrong Type",
            )

    def test_rejects_cross_workspace_root(self):
        ctx = self.ctx()
        stack = self._full_stack(ctx)
        product_id = stack["product"][0]
        other = self.ctx()

        with self.assertRaises(NavigationNotFound):
            navigation.create_internal_entry(
                other, product_id,
                entry_type=ENTRY_OFFER,
                display_name="Cross WS Offer",
            )

    def test_shared_type_allowed_under_any_root(self):
        ctx = self.ctx()
        stack = self._full_stack(ctx)
        product_id = stack["product"][0]

        entry = navigation.create_internal_entry(
            ctx, product_id,
            entry_type=ENTRY_REFERENCE_ASSET,
            display_name="Shared Ref",
            content={},
        )
        self.assertIn("id", entry)

    def test_parent_type_and_id_must_be_paired(self):
        ctx = self.ctx()
        stack = self._full_stack(ctx)
        product_id = stack["product"][0]

        with self.assertRaises(NavigationConflict):
            navigation.create_internal_entry(
                ctx, product_id,
                entry_type=ENTRY_OFFER,
                display_name="Bad Parent",
                parent_type=ENTRY_OFFER,
                parent_id=None,
            )


class TestContextRegistry(LibraryFixtures):

    def test_product_hosts_expected_types(self):
        expected = {ENTRY_OFFER, ENTRY_REFERENCE_GROUP, ENTRY_SELECTION_RULE,
                    ENTRY_DIMENSION_SCHEMA, ENTRY_MARKET_MAPPING}
        actual = set(CONTEXT_REGISTRY[ENTRY_PRODUCT].keys())
        self.assertEqual(actual, expected)

    def test_data_source_hosts_expected_types(self):
        expected = {ENTRY_MANUAL_FACT_SET, ENTRY_TRUTH_RESOLUTION_POLICY}
        actual = set(CONTEXT_REGISTRY[ENTRY_DATA_SOURCE].keys())
        self.assertEqual(actual, expected)

    def test_shared_types_accepted_under_any_root(self):
        self.assertEqual(SHARED_INTERNAL_TYPES, frozenset({ENTRY_REFERENCE_ASSET}))

    def test_root_and_internal_are_disjoint(self):
        self.assertFalse(ROOT_CATEGORIES & INTERNAL_TYPES)


# ---------------------------------------------------------------------------
# HTTP route tests
# ---------------------------------------------------------------------------


class TestInternalNavigationRoutes(LibraryFixtures):

    def setUp(self):
        self.ctx = self._make_context()
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def _owner_client(self):
        owner_ctx = self.owner_ctx(self.ctx)
        app.dependency_overrides[request_tenant_context] = lambda: owner_ctx
        return owner_ctx

    def _create_entry_via_api(self, entry_type, name, content=None):
        resp = self.client.post(
            "/api/v1/library/entries",
            json={"entry_type": entry_type, "display_name": name, "content": content or {}},
            headers={"Idempotency-Key": f"ik-{name}-{id(self)}", "X-Command-Identity": f"cmd.nav.create.{id(self)}"},
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()

    def _setup_product_root(self):
        owner_ctx = self._owner_client()
        ref_id, ref_ver = self._reference_version(owner_ctx)
        group_id, group_ver = self._group_version(owner_ctx, ref_ver["id"])
        product_content = self._product_content(ref_ver["id"], group_ver["id"])
        product_entry = self._create_entry_via_api(ENTRY_PRODUCT, "Nav Product", product_content)
        product_id = product_entry["id"]
        revision = product_entry["revision"]
        service.validate_draft(owner_ctx, product_id, expected_revision=revision)
        service.publish(owner_ctx, product_id, revision)
        return product_id, ref_ver, group_ver, owner_ctx

    def test_create_internal_entry_returns_201(self):
        product_id, ref_ver, group_ver, owner_ctx = self._setup_product_root()
        product_entry = service.get_entry(owner_ctx, product_id)
        product_ver_id = product_entry["versions"][0]["id"]

        resp = self.client.post(
            f"/api/v1/library/entries/{product_id}/internal",
            json={
                "entry_type": ENTRY_OFFER,
                "display_name": "API Offer",
                "content": self._offer_content(product_ver_id),
            },
            headers={"Idempotency-Key": "ik-create-nav-offer", "X-Command-Identity": "cmd.nav.create.offer"},
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        body = resp.json()
        self.assertEqual(body["entry_type"], ENTRY_OFFER)
        self.assertIn("id", body)

    def test_list_internal_entries_returns_page_envelope(self):
        product_id, ref_ver, group_ver, owner_ctx = self._setup_product_root()
        product_entry = service.get_entry(owner_ctx, product_id)
        product_ver_id = product_entry["versions"][0]["id"]

        navigation.create_internal_entry(
            owner_ctx, product_id,
            entry_type=ENTRY_OFFER,
            display_name="Listed Offer",
            content=self._offer_content(product_ver_id),
        )

        resp = self.client.get(
            f"/api/v1/library/entries/{product_id}/internal",
            params={"entry_type": ENTRY_OFFER},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertIn("items", body)
        self.assertIsInstance(body["items"], list)
        self.assertGreaterEqual(len(body["items"]), 1)

    def test_get_internal_entry_returns_metadata(self):
        product_id, ref_ver, group_ver, owner_ctx = self._setup_product_root()
        product_entry = service.get_entry(owner_ctx, product_id)
        product_ver_id = product_entry["versions"][0]["id"]

        entry = navigation.create_internal_entry(
            owner_ctx, product_id,
            entry_type=ENTRY_OFFER,
            display_name="Meta Offer",
            content=self._offer_content(product_ver_id),
        )

        resp = self.client.get(f"/api/v1/library/internal/{ENTRY_OFFER}/{entry['id']}")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["id"], entry["id"])
        self.assertEqual(body["entry_type"], ENTRY_OFFER)
        self.assertIn("context_count", body)

    def test_get_internal_entry_context_returns_verified_relationship(self):
        product_id, ref_ver, group_ver, owner_ctx = self._setup_product_root()
        product_entry = service.get_entry(owner_ctx, product_id)
        product_ver_id = product_entry["versions"][0]["id"]

        entry = navigation.create_internal_entry(
            owner_ctx, product_id,
            entry_type=ENTRY_OFFER,
            display_name="Context Offer",
            content=self._offer_content(product_ver_id),
        )

        resp = self.client.get(
            f"/api/v1/library/entries/{product_id}/internal/{ENTRY_OFFER}/{entry['id']}",
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["root_entry_id"], product_id)
        self.assertEqual(body["root_entry_type"], ENTRY_PRODUCT)
        self.assertEqual(body["relation_kind"], "product_offer")

    def test_list_internal_rejects_invalid_root(self):
        resp = self.client.get(
            "/api/v1/library/entries/nonexistent_root/internal",
            params={"entry_type": ENTRY_OFFER},
        )
        self.assertIn(resp.status_code, (404, 422), resp.text)

    def test_create_internal_rejects_wrong_type_for_root(self):
        product_id, _, _, _ = self._setup_product_root()
        resp = self.client.post(
            f"/api/v1/library/entries/{product_id}/internal",
            json={
                "entry_type": ENTRY_MANUAL_FACT_SET,
                "display_name": "Wrong Type",
                "content": {},
            },
            headers={"Idempotency-Key": "ik-wrong-type", "X-Command-Identity": "cmd.nav.wrong.type"},
        )
        self.assertEqual(resp.status_code, 409, resp.text)

    def test_pagination_is_single_cursor_stream(self):
        product_id, ref_ver, group_ver, owner_ctx = self._setup_product_root()
        product_entry = service.get_entry(owner_ctx, product_id)
        product_ver_id = product_entry["versions"][0]["id"]

        for i in range(3):
            navigation.create_internal_entry(
                owner_ctx, product_id,
                entry_type=ENTRY_OFFER,
                display_name=f"Paginated Offer {i}",
                content=self._offer_content(product_ver_id),
            )

        all_ids = []
        cursor = None
        while True:
            params = {"entry_type": ENTRY_OFFER, "limit": 2}
            if cursor:
                params["cursor"] = cursor
            resp = self.client.get(
                f"/api/v1/library/entries/{product_id}/internal",
                params=params,
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            body = resp.json()
            page_ids = [item["id"] for item in body["items"]]
            all_ids.extend(page_ids)
            cursor = body.get("next_cursor")
            if not cursor:
                break

        self.assertEqual(len(all_ids), 3)
        self.assertEqual(len(set(all_ids)), 3, "Cursor stream must not duplicate entries")


if __name__ == "__main__":
    unittest.main()
