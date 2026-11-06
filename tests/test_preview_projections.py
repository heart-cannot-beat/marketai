"""Unit tests for GenerationPreviewDetail UI projections (Issue #52).

Covers the enriched response fields: combination_count, call_count,
maximum_cost, final_prompt, items, capability_decisions, exclusion_summary.
Tests both the domain-level projection computation and the HTTP-level
contract via TestClient.
"""
from __future__ import annotations

import unittest
from contextlib import contextmanager

from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.support.library_fixtures import LibraryFixtures

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    DimensionConceptKeyRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    new_id,
    utcnow,
)
from app.main import app
from app.platform.http_identity import request_tenant_context


class TestPreviewProjectionsDomain(LibraryFixtures):
    """Domain-level tests for _compute_ui_projections."""

    def test_single_combination_default(self):
        from app.generation.draft import create_draft
        from app.generation.preview import create_preview

        ctx = self._make_context()
        with tenant_session(ctx) as s:
            draft = create_draft(
                s, ctx,
                product_id=self._product_id(ctx, s),
                provider="mock", model="mock-v1",
                execution_mode="mock",
                combination_strategy={"kind": "full", "planner_version": "v1"},
            )
            preview = create_preview(s, ctx, draft.id, draft.revision)
            proj = preview.snapshot_json.get("ui_projections", {})

            self.assertEqual(proj["combination_count"], 1)
            self.assertEqual(proj["call_count"], 1)
            self.assertIsNotNone(proj["maximum_cost"])
            self.assertEqual(proj["maximum_cost"]["amount"], "0")
            self.assertEqual(proj["maximum_cost"]["currency"], "USD")
            self.assertIsInstance(proj["items"], list)
            self.assertEqual(len(proj["items"]), 1)
            self.assertFalse(proj["items"][0]["excluded"])

    def test_multi_dimension_combinations(self):
        from app.generation.draft import create_draft
        from app.generation.preview import create_preview

        ctx = self._make_context()
        with tenant_session(ctx) as s:
            draft = create_draft(
                s, ctx,
                product_id=self._product_id(ctx, s),
                provider="mock", model="mock-v1",
                execution_mode="mock",
                combination_strategy={"kind": "full", "planner_version": "v1"},
                selectors={
                    "dimensions": {
                        "angle": ["lifestyle", "product_shot"],
                        "tone": ["warm", "cool", "neutral"],
                    }
                },
            )
            preview = create_preview(s, ctx, draft.id, draft.revision)
            proj = preview.snapshot_json.get("ui_projections", {})

            self.assertEqual(proj["combination_count"], 6)
            self.assertEqual(proj["call_count"], 6)
            included = [i for i in proj["items"] if not i["excluded"]]
            self.assertEqual(len(included), 6)

    def test_dimension_template_selection_freezes_governed_concepts(self):
        from app.generation.draft import create_draft
        from app.generation.preview import create_preview

        ctx = self._make_context()
        with tenant_session(ctx) as s:
            entry = LibraryEntryRecord(
                id=new_id("lib"),
                workspace_id=ctx.workspace_id,
                entry_type="dimension_template",
                display_name="Experiment angles",
                lifecycle="active",
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            s.add(entry)
            s.flush()
            mapped_concept = DimensionConceptKeyRecord(
                id=new_id("dck"),
                workspace_id=ctx.workspace_id,
                scope="workspace",
                name="Lifestyle",
                created_by=ctx.initiator_id or "test",
            )
            s.add(mapped_concept)
            s.flush()
            version = LibraryVersionRecord(
                id=new_id("libv"),
                workspace_id=ctx.workspace_id,
                entry_id=entry.id,
                entry_type="dimension_template",
                version_no=1,
                content_json={
                    "dimensions": [{
                        "id": "angle",
                        "values": [
                            {"id": "lifestyle", "positive_fragments": ["in use"]},
                            {"id": "studio", "positive_fragments": ["on white"]},
                        ],
                    }],
                    "concept_key_mappings": [{
                        "dimension_id": "angle",
                        "value_id": "lifestyle",
                        "concept_key_id": mapped_concept.id,
                    }],
                },
                content_hash="a" * 64,
                content_schema_version=1,
                lifecycle_revision=1,
                state="effective",
                eligibility="eligible",
                validation_json={},
                origin_command="create",
                published_by=ctx.initiator_id or "test",
                published_at=utcnow(),
            )
            s.add(version)
            s.flush()
            entry.current_version_id = version.id

            draft = create_draft(
                s, ctx,
                product_id=self._product_id(ctx, s),
                provider="mock", model="mock-v1",
                execution_mode="mock",
                combination_strategy={"kind": "full", "planner_version": "v1"},
                selectors={"dimension_template_ids": [entry.id]},
            )
            preview = create_preview(s, ctx, draft.id, draft.revision)

            frozen = preview.snapshot_json
            self.assertEqual(
                frozen["selectors"]["dimension_template_version_ids"],
                [version.id],
            )
            combinations = frozen["recipe"]["selected_combinations"]
            self.assertEqual(len(combinations), 2)
            concept_ids = {
                concept["concept_key_id"]
                for combination in combinations
                for concept in combination["concepts"]
            }
            self.assertEqual(len(concept_ids), 2)
            self.assertIn(mapped_concept.id, concept_ids)
            governed = s.scalars(
                select(DimensionConceptKeyRecord).where(
                    DimensionConceptKeyRecord.id.in_(concept_ids)
                )
            ).all()
            self.assertEqual({row.id for row in governed}, concept_ids)
            self.assertTrue(all(row.workspace_id == ctx.workspace_id for row in governed))

    def test_variants_multiply_call_count(self):
        from app.generation.draft import create_draft
        from app.generation.preview import create_preview

        ctx = self._make_context()
        with tenant_session(ctx) as s:
            draft = create_draft(
                s, ctx,
                product_id=self._product_id(ctx, s),
                provider="mock", model="mock-v1",
                execution_mode="mock",
                combination_strategy={"kind": "full", "planner_version": "v1"},
                format_config={"variants_per_combination": 3},
                selectors={"dimensions": {"angle": ["a", "b"]}},
            )
            preview = create_preview(s, ctx, draft.id, draft.revision)
            proj = preview.snapshot_json["ui_projections"]

            self.assertEqual(proj["combination_count"], 2)
            self.assertEqual(proj["call_count"], 6)

    def test_exclusions_reduce_combination_count(self):
        from app.generation.draft import create_draft
        from app.generation.preview import create_preview

        ctx = self._make_context()
        with tenant_session(ctx) as s:
            draft = create_draft(
                s, ctx,
                product_id=self._product_id(ctx, s),
                provider="mock", model="mock-v1",
                execution_mode="mock",
                combination_strategy={"kind": "full", "planner_version": "v1"},
                selectors={"dimensions": {"angle": ["a", "b", "c"]}},
                exclusions={
                    "rules": [
                        {"dimensions": {"angle": "b"}, "reason": "low_quality"},
                    ]
                },
            )
            preview = create_preview(s, ctx, draft.id, draft.revision)
            proj = preview.snapshot_json["ui_projections"]

            self.assertEqual(proj["combination_count"], 2)
            self.assertEqual(proj["call_count"], 2)
            self.assertEqual(proj["exclusion_summary"]["excluded_count"], 1)
            self.assertIn("low_quality", proj["exclusion_summary"]["reasons"])

            excluded_items = [i for i in proj["items"] if i["excluded"]]
            self.assertEqual(len(excluded_items), 1)
            self.assertEqual(excluded_items[0]["exclusion_reason"], "low_quality")

    def test_capability_decisions_from_resolution(self):
        from app.generation.draft import create_draft
        from app.generation.preview import create_preview

        ctx = self._make_context()
        with tenant_session(ctx) as s:
            draft = create_draft(
                s, ctx,
                product_id=self._product_id(ctx, s),
                provider="mock", model="mock-v1",
                execution_mode="mock",
                combination_strategy={"kind": "full", "planner_version": "v1"},
            )
            preview = create_preview(s, ctx, draft.id, draft.revision)
            proj = preview.snapshot_json["ui_projections"]

            self.assertIsInstance(proj["capability_decisions"], list)
            self.assertTrue(len(proj["capability_decisions"]) > 0)
            decision = proj["capability_decisions"][0]
            self.assertIn("capability", decision)
            self.assertIn("supported", decision)

    def test_maximum_cost_is_decimal_string(self):
        from app.generation.draft import create_draft
        from app.generation.preview import create_preview

        ctx = self._make_context()
        with tenant_session(ctx) as s:
            draft = create_draft(
                s, ctx,
                product_id=self._product_id(ctx, s),
                provider="mock", model="mock-v1",
                execution_mode="mock",
                combination_strategy={"kind": "full", "planner_version": "v1"},
            )
            preview = create_preview(s, ctx, draft.id, draft.revision)
            cost = preview.snapshot_json["ui_projections"]["maximum_cost"]

            self.assertIsInstance(cost["amount"], str)
            self.assertIsInstance(cost["currency"], str)
            self.assertEqual(len(cost["currency"]), 3)

    def test_final_prompt_assembled_from_template(self):
        from app.generation.draft import create_draft
        from app.generation.preview import create_preview

        ctx = self._make_context()
        with tenant_session(ctx) as s:
            draft = create_draft(
                s, ctx,
                product_id=self._product_id(ctx, s),
                provider="mock", model="mock-v1",
                execution_mode="mock",
                combination_strategy={"kind": "full", "planner_version": "v1"},
                selectors={
                    "prompt_template": "A {style} photo of {product}",
                    "prompt_variables": {"style": "cinematic", "product": "sneakers"},
                },
            )
            preview = create_preview(s, ctx, draft.id, draft.revision)
            proj = preview.snapshot_json["ui_projections"]

            self.assertEqual(proj["final_prompt"], "A cinematic photo of sneakers")

    def test_final_prompt_none_without_template(self):
        from app.generation.draft import create_draft
        from app.generation.preview import create_preview

        ctx = self._make_context()
        with tenant_session(ctx) as s:
            draft = create_draft(
                s, ctx,
                product_id=self._product_id(ctx, s),
                provider="mock", model="mock-v1",
                execution_mode="mock",
                combination_strategy={"kind": "full", "planner_version": "v1"},
            )
            preview = create_preview(s, ctx, draft.id, draft.revision)
            proj = preview.snapshot_json["ui_projections"]

            self.assertIsNone(proj["final_prompt"])

    def test_preview_hash_unchanged_by_projections(self):
        """UI projections are stored after hash computation; adding them
        must not change the preview_hash or recipe_input_hash."""
        from app.generation.draft import create_draft
        from app.generation.preview import create_preview

        ctx = self._make_context()
        with tenant_session(ctx) as s:
            draft = create_draft(
                s, ctx,
                product_id=self._product_id(ctx, s),
                provider="mock", model="mock-v1",
                execution_mode="mock",
                combination_strategy={"kind": "full", "planner_version": "v1"},
            )
            p1 = create_preview(s, ctx, draft.id, draft.revision)
            hash1 = p1.preview_hash
            recipe_hash1 = p1.recipe_input_hash

        with tenant_session(ctx) as s:
            draft2 = create_draft(
                s, ctx,
                product_id=self._product_id(ctx, s),
                provider="mock", model="mock-v1",
                execution_mode="mock",
                combination_strategy={"kind": "full", "planner_version": "v1"},
            )
            p2 = create_preview(s, ctx, draft2.id, draft2.revision)

            self.assertEqual(p2.preview_hash, hash1)
            self.assertEqual(p2.recipe_input_hash, recipe_hash1)


class TestPreviewProjectionsHTTP(LibraryFixtures):
    """HTTP-level tests verifying the enriched fields in the API contract."""

    def setUp(self):
        self.ctx = self._make_context()
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def _ik(self, tag: str) -> dict:
        return {"Idempotency-Key": f"ik-proj-{tag}-{new_id('k')}"}

    def _create_draft(self, **overrides) -> dict:
        product_id = overrides.pop("product_id", None) or self._product_id(self.ctx)
        body = {
            "product_id": product_id,
            "provider": "mock",
            "model": "mock-v1",
            "execution_mode": "mock",
            "combination_strategy": {"kind": "full", "planner_version": "v1"},
        }
        body.update(overrides)
        resp = self.client.post(
            "/api/v1/generation/drafts", json=body, headers=self._ik("create-draft"),
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()

    def _create_preview(self, draft: dict) -> dict:
        resp = self.client.post(
            f"/api/v1/generation/drafts/{draft['id']}/previews",
            headers={**self._ik("preview"), "If-Match": f'"{draft["revision"]}"'},
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()

    def test_preview_response_contains_all_new_fields(self):
        draft = self._create_draft()
        preview = self._create_preview(draft)

        self.assertIn("combination_count", preview)
        self.assertIn("call_count", preview)
        self.assertIn("maximum_cost", preview)
        self.assertIn("final_prompt", preview)
        self.assertIn("items", preview)
        self.assertIn("capability_decisions", preview)
        self.assertIn("exclusion_summary", preview)

    def test_preview_get_route_contains_projections(self):
        draft = self._create_draft()
        preview = self._create_preview(draft)

        resp = self.client.get(f"/api/v1/generation/previews/{preview['id']}")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()

        self.assertIn("combination_count", body)
        self.assertIn("call_count", body)
        self.assertIn("maximum_cost", body)
        self.assertIn("items", body)
        self.assertIn("capability_decisions", body)
        self.assertIn("exclusion_summary", body)

    def test_maximum_cost_shape(self):
        draft = self._create_draft()
        preview = self._create_preview(draft)

        cost = preview["maximum_cost"]
        self.assertIsNotNone(cost)
        self.assertIn("amount", cost)
        self.assertIn("currency", cost)
        self.assertIsInstance(cost["amount"], str)
        self.assertIsInstance(cost["currency"], str)

    def test_combination_count_with_dimensions(self):
        draft = self._create_draft(
            selectors={
                "dimensions": {
                    "angle": ["a", "b"],
                    "tone": ["warm", "cool"],
                },
            },
        )
        preview = self._create_preview(draft)

        self.assertEqual(preview["combination_count"], 4)
        self.assertEqual(preview["call_count"], 4)
        self.assertEqual(len([i for i in preview["items"] if not i["excluded"]]), 4)

    def test_exclusion_summary_in_response(self):
        draft = self._create_draft(
            selectors={"dimensions": {"angle": ["a", "b", "c"]}},
            exclusions={
                "rules": [{"dimensions": {"angle": "c"}, "reason": "test_exclude"}],
            },
        )
        preview = self._create_preview(draft)

        self.assertEqual(preview["combination_count"], 2)
        summary = preview["exclusion_summary"]
        self.assertIsNotNone(summary)
        self.assertEqual(summary["excluded_count"], 1)
        self.assertIn("test_exclude", summary["reasons"])

    def test_capability_decisions_shape(self):
        draft = self._create_draft()
        preview = self._create_preview(draft)

        decisions = preview["capability_decisions"]
        self.assertIsInstance(decisions, list)
        self.assertGreater(len(decisions), 0)
        for d in decisions:
            self.assertIn("capability", d)
            self.assertIn("supported", d)
            self.assertIn("detail", d)

    def test_items_shape(self):
        draft = self._create_draft(
            selectors={"dimensions": {"angle": ["lifestyle"]}},
        )
        preview = self._create_preview(draft)

        items = preview["items"]
        self.assertIsInstance(items, list)
        self.assertGreater(len(items), 0)
        for item in items:
            self.assertIn("ordinal", item)
            self.assertIn("dimension_tags", item)
            self.assertIn("reference_asset_ids", item)
            self.assertIn("excluded", item)
            self.assertIn("exclusion_reason", item)


if __name__ == "__main__":
    unittest.main()
