import copy
import unittest
from tests.support.library_fixtures import LibraryFixtures, FUTURE
from app.library.contracts import (
    ENTRY_DIMENSION_TEMPLATE, ENTRY_DIMENSION_SCHEMA, ENTRY_PRODUCT,
    ROLE_MEMBER, VERSION_EFFECTIVE,
)
from app.library import service
from app.library.templates import apply_template, reapply_template


class TestApplyTemplate(LibraryFixtures):

    def _publish_template(self, ctx):
        content = {
            "dimensions": [{
                "id": "dim-color", "name": "Color",
                "values": [
                    {"id": "val-red", "name": "Red",
                     "positive_fragments": ["red"], "negative_fragments": []},
                    {"id": "val-blue", "name": "Blue",
                     "positive_fragments": ["blue"], "negative_fragments": []},
                ],
            }],
            "constraints": [],
            "concept_key_mappings": [],
            "combination_cardinality": "one_per_dimension",
        }
        return self._publish(ctx, ENTRY_DIMENSION_TEMPLATE, "Color Template", content)

    def _publish_product(self, ctx):
        content = {
            "name": "Test Product", "description": "A product", "audience": "everyone",
        }
        return self._publish(ctx, ENTRY_PRODUCT, "Test Product", content)

    def test_apply_creates_schema_draft(self):
        ctx = self.ctx()
        tmpl_entry_id, tmpl_version = self._publish_template(ctx)
        prod_entry_id, _ = self._publish_product(ctx)
        result = apply_template(
            ctx, tmpl_version["id"], prod_entry_id,
            idempotency_key="apply-1",
            request_fingerprint="fp1",
        )
        self.assertEqual(result["entry_type"], ENTRY_DIMENSION_SCHEMA)
        draft = result["draft"]
        self.assertEqual(draft["content"]["product_entry_id"], prod_entry_id)
        self.assertEqual(draft["content"]["derived_from_template_version_id"], tmpl_version["id"])
        # Product-local IDs are different from template IDs
        local_dim_id = draft["content"]["dimensions"][0]["id"]
        self.assertNotEqual(local_dim_id, "dim-color")
        # Provenance preserved
        self.assertEqual(draft["content"]["dimensions"][0]["source_dimension_id"], "dim-color")

    def test_a_derived_schema_name_stays_inside_its_column(self):
        # library_entries.display_name is varchar(300) -- the bound the API
        # puts on a product name -- so "Schema for <name>" must be cut to fit.
        ctx = self.ctx()
        _tmpl_entry_id, tmpl_version = self._publish_template(ctx)
        long_name = "P" * 300
        prod_entry_id, _ = self._publish(ctx, ENTRY_PRODUCT, long_name, {
            "name": long_name, "description": "A product", "audience": "everyone",
        })
        result = apply_template(ctx, tmpl_version["id"], prod_entry_id)
        self.assertEqual(len(result["display_name"]), service.DISPLAY_NAME_MAX_LENGTH)
        self.assertTrue(result["display_name"].startswith("Schema for PPP"))
        self.assert_stored_widths(ctx)

    def test_apply_deep_copies(self):
        """Later template changes cannot mutate applied schemas."""
        ctx = self.ctx()
        tmpl_entry_id, tmpl_v1 = self._publish_template(ctx)
        prod_entry_id, _ = self._publish_product(ctx)
        result = apply_template(
            ctx, tmpl_v1["id"], prod_entry_id,
            idempotency_key="apply-2", request_fingerprint="fp2",
        )
        original_dims = result["draft"]["content"]["dimensions"]
        # Publish a new template version with a changed name
        service.save_draft(ctx, tmpl_entry_id, {
            "dimensions": [{
                "id": "dim-color", "name": "Color CHANGED",
                "values": [{"id": "val-red", "name": "Red CHANGED",
                            "positive_fragments": ["red"], "negative_fragments": []}],
            }],
            "constraints": [], "concept_key_mappings": [],
            "combination_cardinality": "one_per_dimension",
        }, expected_revision=2)
        # Applied schema is unchanged
        self.assertEqual(original_dims[0]["values"][0]["name"], "Red")

    def test_apply_idempotent(self):
        ctx = self.ctx()
        tmpl_entry_id, tmpl_version = self._publish_template(ctx)
        prod_entry_id, _ = self._publish_product(ctx)
        r1 = apply_template(ctx, tmpl_version["id"], prod_entry_id,
                            idempotency_key="apply-idem", request_fingerprint="fp3")
        r2 = apply_template(ctx, tmpl_version["id"], prod_entry_id,
                            idempotency_key="apply-idem", request_fingerprint="fp3")
        self.assertEqual(r1["id"], r2["id"])

    def test_apply_cross_workspace_blocked(self):
        ctx_a = self.ctx()
        ctx_b = self.ctx()
        tmpl_entry_id, tmpl_version = self._publish_template(ctx_a)
        prod_entry_id, _ = self._publish_product(ctx_b)
        with self.assertRaises(Exception):
            apply_template(ctx_b, tmpl_version["id"], prod_entry_id,
                           idempotency_key="apply-cross", request_fingerprint="fp4")

    def test_apply_nonexistent_product_blocked(self):
        ctx = self.ctx()
        _, tmpl_version = self._publish_template(ctx)
        with self.assertRaises(Exception):
            apply_template(ctx, tmpl_version["id"], "nonexistent",
                           idempotency_key="apply-noent", request_fingerprint="fp5")

    def test_product_entry_id_immutable_across_versions(self):
        ctx = self.ctx()
        _, tmpl_version = self._publish_template(ctx)
        prod1_id, _ = self._publish_product(ctx)
        prod2_id, _ = self._publish_product(ctx)
        result = apply_template(ctx, tmpl_version["id"], prod1_id,
                                idempotency_key="apply-imm", request_fingerprint="fp6")
        schema_entry_id = result["id"]
        # Publish the schema
        rev = result["draft"]["revision"]
        service.validate_draft(ctx, schema_entry_id, expected_revision=rev)
        service.publish(ctx, schema_entry_id, rev)
        # Try to save draft with different product_entry_id
        new_content = dict(result["draft"]["content"])
        new_content["product_entry_id"] = prod2_id
        service.save_draft(ctx, schema_entry_id, new_content, expected_revision=rev + 1)
        report = service.validate_draft(ctx, schema_entry_id, expected_revision=rev + 2)
        self.assertFalse(report["ok"])


class TestReapplyTemplate(LibraryFixtures):

    def _template_content(self, *, red_name: str = "Red", red_fragments: list[str] | None = None) -> dict:
        return {
            "dimensions": [
                {
                    "id": "dim-color", "name": "Color",
                    "values": [
                        {"id": "val-red", "name": red_name,
                         "positive_fragments": red_fragments or ["red"], "negative_fragments": []},
                        {"id": "val-blue", "name": "Blue",
                         "positive_fragments": ["blue"], "negative_fragments": []},
                    ],
                },
                {
                    "id": "dim-size", "name": "Size",
                    "values": [
                        {"id": "val-small", "name": "Small",
                         "positive_fragments": ["small"], "negative_fragments": []},
                        {"id": "val-large", "name": "Large",
                         "positive_fragments": ["large"], "negative_fragments": []},
                    ],
                },
            ],
            "constraints": [],
            "concept_key_mappings": [],
            "combination_cardinality": "one_per_dimension",
        }

    def _publish_product(self, ctx):
        content = {
            "name": "Test Product", "description": "A product", "audience": "everyone",
        }
        return self._publish(ctx, ENTRY_PRODUCT, "Test Product", content)

    def _publish_and_apply(self, ctx, tmpl_content: dict) -> dict:
        """Publish a template v1, apply it to a fresh product, and publish the
        resulting schema — returning everything a reapply test needs to drive
        further template/schema edits."""
        tmpl_entry_id, tmpl_v1 = self._publish(ctx, ENTRY_DIMENSION_TEMPLATE, "Color Template", tmpl_content)
        prod_entry_id, _ = self._publish_product(ctx)
        apply_result = apply_template(ctx, tmpl_v1["id"], prod_entry_id)
        schema_entry_id = apply_result["id"]
        schema_content = apply_result["draft"]["content"]
        schema_rev = apply_result["draft"]["revision"]
        report = service.validate_draft(ctx, schema_entry_id, expected_revision=schema_rev)
        self.assertTrue(report["ok"], f"schema 校验未通过: {report['blocking']}")
        schema_v1 = service.publish(ctx, schema_entry_id, schema_rev)
        return {
            "tmpl_entry_id": tmpl_entry_id,
            "tmpl_v1": tmpl_v1,
            "prod_entry_id": prod_entry_id,
            "schema_entry_id": schema_entry_id,
            "schema_v1": schema_v1,
            "schema_content": schema_content,
            # publish() bumps the draft's revision by one for the next edit.
            "schema_next_rev": schema_rev + 1,
        }

    def _publish_template_v2(self, ctx, tmpl_entry_id: str, content: dict) -> dict:
        service.save_draft(ctx, tmpl_entry_id, content, expected_revision=2)
        report = service.validate_draft(ctx, tmpl_entry_id, expected_revision=3)
        self.assertTrue(report["ok"], f"模板 v2 校验未通过: {report['blocking']}")
        return service.publish(ctx, tmpl_entry_id, 3)

    def _save_and_publish_schema(self, ctx, fixture: dict, schema_content: dict) -> dict:
        rev = fixture["schema_next_rev"]
        service.save_draft(ctx, fixture["schema_entry_id"], schema_content, expected_revision=rev)
        report = service.validate_draft(ctx, fixture["schema_entry_id"], expected_revision=rev + 1)
        self.assertTrue(report["ok"], f"schema v2 校验未通过: {report['blocking']}")
        return service.publish(ctx, fixture["schema_entry_id"], rev + 1)

    def test_reapply_clean_update(self):
        """A template-only change, with no local edits, must diff as
        template_only rather than conflict (regression for the ID-hashing bug)."""
        ctx = self.ctx()
        fixture = self._publish_and_apply(ctx, self._template_content())

        v2_content = self._template_content(red_name="Red Updated", red_fragments=["red", "crimson"])
        tmpl_v2 = self._publish_template_v2(ctx, fixture["tmpl_entry_id"], v2_content)

        diff = reapply_template(ctx, tmpl_v2["id"], fixture["schema_v1"]["id"])
        items = {item["dimension_id"]: item for item in diff["items"]}
        self.assertEqual(items["dim-color"]["status"], "template_only")
        self.assertEqual(items["dim-color"]["template_value"]["values"][0]["name"], "Red Updated")
        self.assertEqual(items["dim-size"]["status"], "unchanged")

    def test_reapply_conflict(self):
        """When both the template and the local schema diverge from the base,
        the dimension must diff as a conflict."""
        ctx = self.ctx()
        fixture = self._publish_and_apply(ctx, self._template_content())

        # Locally edit the schema's Color dimension.
        schema_content = copy.deepcopy(fixture["schema_content"])
        color_dim = next(
            d for d in schema_content["dimensions"] if d.get("source_dimension_id") == "dim-color"
        )
        color_dim["values"][0]["name"] = "Local Red Edit"
        schema_v2 = self._save_and_publish_schema(ctx, fixture, schema_content)

        # Move the template forward too.
        v2_content = self._template_content(red_name="Red Updated", red_fragments=["red", "crimson"])
        tmpl_v2 = self._publish_template_v2(ctx, fixture["tmpl_entry_id"], v2_content)

        diff = reapply_template(ctx, tmpl_v2["id"], schema_v2["id"])
        items = {item["dimension_id"]: item for item in diff["items"]}
        self.assertEqual(items["dim-color"]["status"], "conflict")
        self.assertEqual(items["dim-color"]["template_value"]["values"][0]["name"], "Red Updated")
        self.assertEqual(items["dim-color"]["local_value"]["values"][0]["name"], "Local Red Edit")
        self.assertEqual(items["dim-size"]["status"], "unchanged")

    def test_reapply_locally_removed(self):
        """A dimension removed locally, while still present in both the base and
        the new template version, must diff as locally_removed — not local_only."""
        ctx = self.ctx()
        fixture = self._publish_and_apply(ctx, self._template_content())

        schema_content = copy.deepcopy(fixture["schema_content"])
        schema_content["dimensions"] = [
            d for d in schema_content["dimensions"] if d.get("source_dimension_id") != "dim-size"
        ]
        schema_v2 = self._save_and_publish_schema(ctx, fixture, schema_content)

        # Reapply against the same template version (no template change) so any
        # divergence found is purely local.
        diff = reapply_template(ctx, fixture["tmpl_v1"]["id"], schema_v2["id"])
        items = {item["dimension_id"]: item for item in diff["items"]}
        self.assertEqual(items["dim-size"]["status"], "locally_removed")
        self.assertEqual(items["dim-color"]["status"], "unchanged")

    def test_reapply_genuinely_local_only_dimension(self):
        """A dimension added directly on the schema, with no template source,
        must diff as local_only and be keyed by its own local ID."""
        ctx = self.ctx()
        fixture = self._publish_and_apply(ctx, self._template_content())

        schema_content = copy.deepcopy(fixture["schema_content"])
        schema_content["dimensions"].append({
            "id": "ldim_custom", "name": "Finish",
            "values": [{"id": "lval_custom", "name": "Matte",
                        "positive_fragments": ["matte"], "negative_fragments": []}],
        })
        schema_v2 = self._save_and_publish_schema(ctx, fixture, schema_content)

        diff = reapply_template(ctx, fixture["tmpl_v1"]["id"], schema_v2["id"])
        items = {item["dimension_id"]: item for item in diff["items"]}
        self.assertIn("ldim_custom", items)
        self.assertEqual(items["ldim_custom"]["status"], "local_only")
        self.assertEqual(items["ldim_custom"]["local_value"]["name"], "Finish")
        self.assertEqual(items["dim-color"]["status"], "unchanged")
        self.assertEqual(items["dim-size"]["status"], "unchanged")
