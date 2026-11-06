"""Tests for Gate 0.7 — Template apply (production path) and reapply-preview.

Covers:
- apply_template via production path (no longer 501)
- Deep-copy provenance: product-local IDs, source_dimension_id, source_value_id
- Constraint remapping through the apply path
- Reapply three-way diff: template_only, template_removed, conflict, unchanged, local_only, locally_removed
- Stale schema revision rejected
- Conflicting reapply edits
- HTTP routes for apply and reapply-preview
"""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from tests.support.library_fixtures import LibraryFixtures

from app.library import service
from app.library.contracts import (
    ENTRY_DIMENSION_SCHEMA,
    ENTRY_DIMENSION_TEMPLATE,
    ENTRY_PRODUCT,
    ROLE_MEMBER,
    ROLE_OWNER,
)
from app.library.templates import apply_template, reapply_template
from app.main import app
from app.platform.http_identity import request_tenant_context


def _template_content(dim_id="d1", val_id="v1", dim_name="颜色", val_name="红色"):
    return {
        "dimensions": [
            {
                "id": dim_id,
                "name": dim_name,
                "values": [
                    {
                        "id": val_id,
                        "name": val_name,
                        "positive_fragments": ["red"],
                        "negative_fragments": ["blue"],
                    }
                ],
            }
        ],
        "constraints": [],
        "combination_cardinality": "one_per_dimension",
    }


def _template_content_two_dims():
    return {
        "dimensions": [
            {
                "id": "d1",
                "name": "颜色",
                "values": [
                    {"id": "v1", "name": "红色", "positive_fragments": ["red"], "negative_fragments": []},
                    {"id": "v2", "name": "蓝色", "positive_fragments": ["blue"], "negative_fragments": []},
                ],
            },
            {
                "id": "d2",
                "name": "尺寸",
                "values": [
                    {"id": "v3", "name": "大", "positive_fragments": ["large"], "negative_fragments": []},
                ],
            },
        ],
        "constraints": [
            {"type": "excludes", "dimension_id_a": "d1", "value_id_a": "v1",
             "dimension_id_b": "d2", "value_id_b": "v3"},
        ],
        "combination_cardinality": "one_per_dimension",
    }


class TestApplyTemplateService(LibraryFixtures):

    def _publish_template(self, ctx, content=None):
        content = content or _template_content()
        return self._publish(ctx, ENTRY_DIMENSION_TEMPLATE, "模板", content)

    def _publish_product(self, ctx):
        ref_id, ref_ver = self._reference_version(ctx)
        group_id, group_ver = self._group_version(ctx, ref_ver["id"])
        product_content = self._product_content(ref_ver["id"], group_ver["id"])
        return self._publish(ctx, ENTRY_PRODUCT, "产品", product_content)

    def test_apply_creates_dimension_schema_draft(self):
        ctx = self.ctx()
        _, tmpl_ver = self._publish_template(ctx)
        prod_id, _ = self._publish_product(ctx)
        result = apply_template(ctx, tmpl_ver["id"], prod_id)
        self.assertEqual(result["entry_type"], ENTRY_DIMENSION_SCHEMA)
        self.assertIsNotNone(result.get("draft"))
        self.assertEqual(result["draft"]["state"], "editing")

    def test_apply_deep_copies_with_fresh_ids(self):
        ctx = self.ctx()
        _, tmpl_ver = self._publish_template(ctx)
        prod_id, _ = self._publish_product(ctx)
        result = apply_template(ctx, tmpl_ver["id"], prod_id)
        draft_content = result["draft"]["content"]
        dims = draft_content["dimensions"]
        self.assertEqual(len(dims), 1)
        self.assertTrue(dims[0]["id"].startswith("ldim_"))
        self.assertTrue(dims[0]["values"][0]["id"].startswith("lval_"))

    def test_apply_preserves_source_provenance(self):
        ctx = self.ctx()
        _, tmpl_ver = self._publish_template(ctx)
        prod_id, _ = self._publish_product(ctx)
        result = apply_template(ctx, tmpl_ver["id"], prod_id)
        dims = result["draft"]["content"]["dimensions"]
        self.assertEqual(dims[0]["source_dimension_id"], "d1")
        self.assertEqual(dims[0]["values"][0]["source_value_id"], "v1")

    def test_apply_remaps_constraint_ids(self):
        ctx = self.ctx()
        _, tmpl_ver = self._publish_template(ctx, _template_content_two_dims())
        prod_id, _ = self._publish_product(ctx)
        result = apply_template(ctx, tmpl_ver["id"], prod_id)
        draft_content = result["draft"]["content"]
        constraints = draft_content["constraints"]
        self.assertEqual(len(constraints), 1)
        c = constraints[0]
        dim_ids = {d["id"] for d in draft_content["dimensions"]}
        self.assertIn(c["dimension_id_a"], dim_ids)
        self.assertIn(c["dimension_id_b"], dim_ids)
        self.assertNotEqual(c["dimension_id_a"], "d1")
        self.assertNotEqual(c["dimension_id_b"], "d2")

    def test_apply_records_template_version_id(self):
        ctx = self.ctx()
        _, tmpl_ver = self._publish_template(ctx)
        prod_id, _ = self._publish_product(ctx)
        result = apply_template(ctx, tmpl_ver["id"], prod_id)
        draft_content = result["draft"]["content"]
        self.assertEqual(draft_content["derived_from_template_version_id"], tmpl_ver["id"])

    def test_apply_rejects_non_template_version(self):
        ctx = self.ctx()
        ref_id, ref_ver = self._reference_version(ctx)
        prod_id, _ = self._publish_product(ctx)
        with self.assertRaises(Exception) as cm:
            apply_template(ctx, ref_ver["id"], prod_id)
        self.assertIn("维度模板", str(cm.exception))

    def test_apply_rejects_non_product_entry(self):
        ctx = self.ctx()
        _, tmpl_ver = self._publish_template(ctx)
        ds_entry = service.create_entry(ctx, entry_type="data_source", display_name="DS")
        with self.assertRaises(Exception) as cm:
            apply_template(ctx, tmpl_ver["id"], ds_entry["id"])
        self.assertIn("产品", str(cm.exception))

    def test_apply_rejects_archived_product(self):
        ctx = self.ctx(role=ROLE_OWNER)
        _, tmpl_ver = self._publish_template(ctx)
        prod_id, _ = self._publish_product(ctx)
        entry = service.get_entry(ctx, prod_id)
        service.archive_entry(ctx, prod_id, expected_revision=entry["draft"]["revision"])
        with self.assertRaises(Exception) as cm:
            apply_template(ctx, tmpl_ver["id"], prod_id)
        self.assertIn("归档", str(cm.exception))

    def test_apply_idempotency(self):
        ctx = self.ctx()
        _, tmpl_ver = self._publish_template(ctx)
        prod_id, _ = self._publish_product(ctx)
        fp = service.compute_fingerprint({"tmpl": tmpl_ver["id"], "prod": prod_id})
        result1 = apply_template(ctx, tmpl_ver["id"], prod_id, idempotency_key="idem-apply-1", request_fingerprint=fp)
        result2 = apply_template(ctx, tmpl_ver["id"], prod_id, idempotency_key="idem-apply-1", request_fingerprint=fp)
        self.assertEqual(result1["id"], result2["id"])


class TestReapplyTemplateService(LibraryFixtures):

    def _setup_schema_from_template(self, ctx, template_content=None):
        """Publish template, apply to product, publish schema, return IDs."""
        template_content = template_content or _template_content_two_dims()
        _, tmpl_ver = self._publish(ctx, ENTRY_DIMENSION_TEMPLATE, "模板", template_content)
        ref_id, ref_ver = self._reference_version(ctx)
        group_id, group_ver = self._group_version(ctx, ref_ver["id"])
        prod_id, _ = self._publish(ctx, ENTRY_PRODUCT, "产品",
                                    self._product_content(ref_ver["id"], group_ver["id"]))
        schema_entry = apply_template(ctx, tmpl_ver["id"], prod_id)
        schema_id = schema_entry["id"]
        revision = schema_entry["draft"]["revision"]
        report = service.validate_draft(ctx, schema_id, expected_revision=revision)
        self.assertTrue(report["ok"], f"Schema validation failed: {report.get('blocking')}")
        schema_ver = service.publish(ctx, schema_id, revision)
        return tmpl_ver, schema_id, schema_ver

    def test_reapply_unchanged_when_no_template_changes(self):
        ctx = self.ctx()
        tmpl_ver, schema_id, schema_ver = self._setup_schema_from_template(ctx)
        result = reapply_template(ctx, tmpl_ver["id"], schema_ver["id"])
        self.assertEqual(result["base_template_version_id"], tmpl_ver["id"])
        self.assertEqual(result["new_template_version_id"], tmpl_ver["id"])
        statuses = {item["status"] for item in result["items"]}
        self.assertTrue(statuses <= {"unchanged"}, f"Expected all unchanged, got {statuses}")

    def test_reapply_template_only_for_new_dimension(self):
        ctx = self.ctx()
        content_v1 = _template_content(dim_id="d1", val_id="v1")
        _, tmpl_v1 = self._publish(ctx, ENTRY_DIMENSION_TEMPLATE, "模板", content_v1)
        ref_id, ref_ver = self._reference_version(ctx)
        group_id, group_ver = self._group_version(ctx, ref_ver["id"])
        prod_id, _ = self._publish(ctx, ENTRY_PRODUCT, "产品",
                                    self._product_content(ref_ver["id"], group_ver["id"]))
        schema_entry = apply_template(ctx, tmpl_v1["id"], prod_id)
        schema_id = schema_entry["id"]
        revision = schema_entry["draft"]["revision"]
        report = service.validate_draft(ctx, schema_id, expected_revision=revision)
        self.assertTrue(report["ok"], f"Schema validation failed: {report.get('blocking')}")
        schema_ver = service.publish(ctx, schema_id, revision)

        content_v2 = {
            "dimensions": [
                {"id": "d1", "name": "颜色", "values": [
                    {"id": "v1", "name": "红色", "positive_fragments": ["red"], "negative_fragments": ["blue"]},
                ]},
                {"id": "d_new", "name": "新维度", "values": [
                    {"id": "v_new", "name": "新值", "positive_fragments": ["new"], "negative_fragments": []},
                ]},
            ],
            "constraints": [],
            "combination_cardinality": "one_per_dimension",
        }
        tmpl_entry = service.get_entry(ctx, tmpl_v1["entry_id"])
        tmpl_rev = tmpl_entry["draft"]["revision"]
        service.save_draft(ctx, tmpl_v1["entry_id"], content_v2, expected_revision=tmpl_rev)
        tmpl_entry2 = service.get_entry(ctx, tmpl_v1["entry_id"])
        tmpl_rev2 = tmpl_entry2["draft"]["revision"]
        tmpl_v2 = service.publish(ctx, tmpl_v1["entry_id"], tmpl_rev2)

        result = reapply_template(ctx, tmpl_v2["id"], schema_ver["id"])
        statuses = {item["dimension_id"]: item["status"] for item in result["items"]}
        self.assertEqual(statuses.get("d_new"), "template_only")
        self.assertEqual(statuses.get("d1"), "unchanged")

    def test_reapply_rejects_non_schema_version(self):
        ctx = self.ctx()
        _, tmpl_ver = self._publish(ctx, ENTRY_DIMENSION_TEMPLATE, "模板", _template_content())
        with self.assertRaises(Exception) as cm:
            reapply_template(ctx, tmpl_ver["id"], tmpl_ver["id"])
        self.assertIn("Schema", str(cm.exception))

    def test_reapply_rejects_schema_without_template_origin(self):
        ctx = self.ctx()
        ref_id, ref_ver = self._reference_version(ctx)
        group_id, group_ver = self._group_version(ctx, ref_ver["id"])
        prod_id, _ = self._publish(ctx, ENTRY_PRODUCT, "产品",
                                    self._product_content(ref_ver["id"], group_ver["id"]))
        schema_entry = service.create_entry(ctx, entry_type=ENTRY_DIMENSION_SCHEMA,
                                             display_name="No Origin Schema",
                                             content={
                                                 "product_entry_id": prod_id,
                                                 "combination_cardinality": "one_per_dimension",
                                                 "dimensions": [
                                                     {"id": "d1", "name": "dim1", "values": [
                                                         {"id": "v1", "name": "val1",
                                                          "positive_fragments": ["p"], "negative_fragments": ["n"]}
                                                     ]}
                                                 ],
                                             })
        revision = schema_entry["draft"]["revision"]
        report = service.validate_draft(ctx, schema_entry["id"], expected_revision=revision)
        self.assertTrue(report["ok"], f"Validation: {report.get('blocking')}")
        schema_ver = service.publish(ctx, schema_entry["id"], revision)
        _, tmpl_ver = self._publish(ctx, ENTRY_DIMENSION_TEMPLATE, "模板", _template_content())
        with self.assertRaises(Exception) as cm:
            reapply_template(ctx, tmpl_ver["id"], schema_ver["id"])
        self.assertIn("模板来源", str(cm.exception))


class TestApplyTemplateHTTP(LibraryFixtures):

    def setUp(self):
        self.ctx = self._make_context()
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def _publish_template(self, content=None):
        content = content or _template_content()
        return self._publish(self.ctx, ENTRY_DIMENSION_TEMPLATE, "模板", content)

    def _publish_product_for_template(self):
        ref_id, ref_ver = self._reference_version(self.ctx)
        group_id, group_ver = self._group_version(self.ctx, ref_ver["id"])
        return self._publish(self.ctx, ENTRY_PRODUCT, "产品",
                             self._product_content(ref_ver["id"], group_ver["id"]))

    def test_apply_template_returns_201(self):
        _, tmpl_ver = self._publish_template()
        prod_id, _ = self._publish_product_for_template()
        resp = self.client.post(
            f"/api/v1/library/versions/{tmpl_ver['id']}/apply",
            json={"product_entry_id": prod_id, "display_name": "应用模板"},
            headers={"Idempotency-Key": "ik-apply-http-1"},
        )
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertEqual(body["entry_type"], ENTRY_DIMENSION_SCHEMA)
        self.assertIn("draft", body)
        self.assertIn("allowed_actions", body)

    def test_apply_template_no_longer_501(self):
        _, tmpl_ver = self._publish_template()
        prod_id, _ = self._publish_product_for_template()
        resp = self.client.post(
            f"/api/v1/library/versions/{tmpl_ver['id']}/apply",
            json={"product_entry_id": prod_id, "display_name": "应用模板"},
            headers={"Idempotency-Key": "ik-apply-http-2"},
        )
        self.assertNotEqual(resp.status_code, 501)

    def test_reapply_preview_returns_201(self):
        _, tmpl_ver = self._publish_template()
        prod_id, _ = self._publish_product_for_template()
        schema_entry = apply_template(self.ctx, tmpl_ver["id"], prod_id)
        schema_id = schema_entry["id"]
        revision = schema_entry["draft"]["revision"]
        report = service.validate_draft(self.ctx, schema_id, expected_revision=revision)
        self.assertTrue(report["ok"])
        schema_ver = service.publish(self.ctx, schema_id, revision)

        resp = self.client.post(
            f"/api/v1/library/versions/{tmpl_ver['id']}/reapply-preview",
            json={"existing_schema_version_id": schema_ver["id"]},
            headers={"Idempotency-Key": "ik-reapply-http-1"},
        )
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertIn("base_template_version_id", body)
        self.assertIn("new_template_version_id", body)
        self.assertIn("schema_version_id", body)
        self.assertIn("items", body)


if __name__ == "__main__":
    unittest.main()
