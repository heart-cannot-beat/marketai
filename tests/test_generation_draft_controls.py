"""Draft controls that must actually take effect in a preview (Issue #157).

Covers the five "written but never read" controls end to end against the
real preview path: ``combination_strategy`` (full vs. coverage, legacy
shape), published template constraints, ``exclusions.rules`` plus the
``exclusions.invalid`` write-back, the ``variants_per_combination`` ceiling
at preview and submission, and the ``prod_`` identity bridge for
``fork_version`` / ``clone_product`` (with the publish-time name sync).
"""
from __future__ import annotations

import unittest
from unittest import mock

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from tests.support.library_fixtures import LibraryFixtures

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import ProductRecord, new_id
from app.generation.capabilities import PLATFORM_ADAPTER_REGISTRY
from app.generation.draft import create_draft, get_draft
from app.generation.preview import PreviewError, create_preview
from app.generation.submission import SubmissionError, submit_generation
from app.library import service
from app.library.contracts import ENTRY_DIMENSION_TEMPLATE, ENTRY_PRODUCT
from app.main import app
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext


def _value(value_id: str) -> dict:
    return {
        "id": value_id,
        "name": value_id.upper(),
        "positive_fragments": [value_id],
        "negative_fragments": [],
    }


def template_content(constraints: list[dict] | None = None) -> dict:
    """A 2x3x2x2 dimension template (24 combinations)."""
    return {
        "dimensions": [
            {"id": "a", "name": "A", "values": [_value("a1"), _value("a2")]},
            {"id": "b", "name": "B", "values": [_value("b1"), _value("b2"), _value("b3")]},
            {"id": "c", "name": "C", "values": [_value("c1"), _value("c2")]},
            {"id": "d", "name": "D", "values": [_value("d1"), _value("d2")]},
        ],
        "constraints": list(constraints or []),
        "concept_key_mappings": [],
        "combination_cardinality": "one_per_dimension",
    }


class DraftControlFixtures(LibraryFixtures):
    def _template(self, ctx: TenantContext, constraints: list[dict] | None = None) -> str:
        entry_id, _version = self._publish(
            ctx, ENTRY_DIMENSION_TEMPLATE, "Grid 2x3x2x2", template_content(constraints)
        )
        return entry_id

    def _draft(self, session, ctx: TenantContext, template_id: str, **overrides):
        kwargs = dict(
            product_id=self._product_id(ctx, session),
            provider="mock", model="mock-v1", execution_mode="mock",
            combination_strategy={"kind": "full"},
            selectors={"dimension_template_ids": [template_id]},
        )
        kwargs.update(overrides)
        return create_draft(session, ctx, **kwargs)


class CombinationStrategyPreviewTests(DraftControlFixtures):
    def test_full_strategy_enumerates_the_whole_template_product(self):
        ctx = self._make_context()
        template_id = self._template(ctx)
        with tenant_session(ctx) as s:
            draft = self._draft(s, ctx, template_id)
            preview = create_preview(s, ctx, draft.id, draft.revision)
            proj = preview.snapshot_json["ui_projections"]

            self.assertEqual(proj["combination_count"], 24)
            self.assertEqual(proj["call_count"], 24)
            self.assertEqual(
                proj["strategy_applied"],
                {
                    "kind": "full", "deprecated_shape": False,
                    "full_count": 24, "candidate_count": 24, "selected_count": 24,
                },
            )
            self.assertEqual(proj["combinations_dropped_by_constraints"], 0)
            self.assertEqual(preview.snapshot_json["recipe"]["combination_strategy"], {"kind": "full"})

    def test_coverage_cap_12_previews_12_combinations_times_variants(self):
        ctx = self._make_context()
        template_id = self._template(ctx)
        with tenant_session(ctx) as s:
            draft = self._draft(
                s, ctx, template_id,
                combination_strategy={"kind": "coverage", "cap": 12},
                format_config={"variants_per_combination": 2},
            )
            preview = create_preview(s, ctx, draft.id, draft.revision)
            proj = preview.snapshot_json["ui_projections"]

            self.assertEqual(proj["combination_count"], 12)
            self.assertEqual(proj["call_count"], 24)  # 12 x variants
            self.assertEqual(proj["strategy_applied"]["kind"], "coverage")
            self.assertEqual(proj["strategy_applied"]["cap"], 12)
            self.assertEqual(proj["strategy_applied"]["full_count"], 24)
            self.assertEqual(proj["strategy_applied"]["selected_count"], 12)
            frozen = preview.snapshot_json["recipe"]["selected_combinations"]
            self.assertEqual(len(frozen), 12)
            # Every one of the 9 single values is represented in the sample.
            covered = {cell for combo in frozen for cell in combo["dimensions"].items()}
            self.assertEqual(len(covered), 9)
            self.assertEqual(
                preview.snapshot_json["recipe"]["combination_strategy"],
                {"kind": "coverage", "cap": 12},
            )

    def test_coverage_sampling_is_stable_across_previews(self):
        ctx = self._make_context()
        template_id = self._template(ctx)
        with tenant_session(ctx) as s:
            draft = self._draft(s, ctx, template_id, combination_strategy={"kind": "coverage", "cap": 12})
            first = create_preview(s, ctx, draft.id, draft.revision)
            second = create_preview(s, ctx, draft.id, draft.revision)
            self.assertEqual(first.recipe_input_hash, second.recipe_input_hash)
            self.assertEqual(
                [c["fingerprint"] for c in first.snapshot_json["recipe"]["selected_combinations"]],
                [c["fingerprint"] for c in second.snapshot_json["recipe"]["selected_combinations"]],
            )

    def test_legacy_sampled_shape_still_samples_and_is_flagged_deprecated(self):
        ctx = self._make_context()
        template_id = self._template(ctx)
        with tenant_session(ctx) as s:
            legacy = self._draft(
                s, ctx, template_id, combination_strategy={"mode": "sampled", "sample_cap": 12},
            )
            canonical = self._draft(
                s, ctx, template_id, combination_strategy={"kind": "coverage", "cap": 12},
            )
            legacy_preview = create_preview(s, ctx, legacy.id, legacy.revision)
            canonical_preview = create_preview(s, ctx, canonical.id, canonical.revision)

            legacy_proj = legacy_preview.snapshot_json["ui_projections"]
            self.assertEqual(legacy_proj["combination_count"], 12)
            self.assertTrue(legacy_proj["strategy_applied"]["deprecated_shape"])
            self.assertFalse(
                canonical_preview.snapshot_json["ui_projections"]["strategy_applied"]["deprecated_shape"]
            )
            # Same meaning, same frozen recipe: the legacy shape never leaks
            # into recipe identity.
            self.assertEqual(legacy_preview.recipe_input_hash, canonical_preview.recipe_input_hash)

    def test_unknown_strategy_kind_is_rejected(self):
        ctx = self._make_context()
        template_id = self._template(ctx)
        with tenant_session(ctx) as s:
            draft = self._draft(s, ctx, template_id, combination_strategy={"kind": "sampled"})
            with self.assertRaises(PreviewError) as caught:
                create_preview(s, ctx, draft.id, draft.revision)
            self.assertEqual(caught.exception.code, "combination_strategy_invalid")


class TemplateConstraintPreviewTests(DraftControlFixtures):
    EXCLUDES_A1_B1 = {
        "type": "excludes",
        "dimension_id_a": "a", "value_id_a": "a1",
        "dimension_id_b": "b", "value_id_b": "b1",
    }
    REQUIRES_A2_C1 = {
        "type": "requires",
        "from_dimension_id": "a", "from_value_id": "a2",
        "to_dimension_id": "c", "to_value_id": "c1",
    }

    def test_published_constraints_drop_combinations_before_cost(self):
        ctx = self._make_context()
        template_id = self._template(ctx, [self.EXCLUDES_A1_B1, self.REQUIRES_A2_C1])
        with tenant_session(ctx) as s:
            draft = self._draft(s, ctx, template_id)
            preview = create_preview(s, ctx, draft.id, draft.revision)
            proj = preview.snapshot_json["ui_projections"]

            # excludes: a1 x b1 x 2 x 2 = 4; requires: a2 x 3 x c2 x 2 = 6.
            self.assertEqual(proj["combinations_dropped_by_constraints"], 10)
            self.assertEqual(proj["combination_count"], 14)
            self.assertEqual(proj["call_count"], 14)
            self.assertEqual(len(preview.snapshot_json["recipe"]["selected_combinations"]), 14)

            dropped_items = [i for i in proj["items"] if i["exclusion_reason"] == "template_constraint"]
            self.assertEqual(len(dropped_items), 10)
            self.assertTrue(all(i["excluded"] for i in dropped_items))
            self.assertIn("template_constraint", proj["exclusion_summary"]["reasons"])
            self.assertEqual(proj["exclusion_summary"]["excluded_count"], 10)
            self.assertEqual(len(proj["constraint_skips"]), 10)
            self.assertTrue(all(skip["violations"] for skip in proj["constraint_skips"]))
            # The constraints that were applied are frozen with the preview.
            frozen_constraints = preview.snapshot_json["selectors"]["dimension_template_constraints"]
            self.assertEqual(frozen_constraints[0]["template_entry_id"], template_id)
            self.assertEqual(len(frozen_constraints[0]["constraints"]), 2)

    def test_coverage_samples_only_from_constraint_valid_combinations(self):
        ctx = self._make_context()
        template_id = self._template(ctx, [self.EXCLUDES_A1_B1])
        with tenant_session(ctx) as s:
            draft = self._draft(s, ctx, template_id, combination_strategy={"kind": "coverage", "cap": 12})
            preview = create_preview(s, ctx, draft.id, draft.revision)
            proj = preview.snapshot_json["ui_projections"]

            self.assertEqual(proj["combinations_dropped_by_constraints"], 4)
            self.assertEqual(proj["strategy_applied"]["candidate_count"], 20)
            self.assertEqual(proj["combination_count"], 12)
            for combo in preview.snapshot_json["recipe"]["selected_combinations"]:
                tags = combo["dimensions"]
                self.assertFalse(tags[f"{template_id}:a"] == "a1" and tags[f"{template_id}:b"] == "b1")


class ExclusionRulePreviewTests(DraftControlFixtures):
    def test_valid_rule_excludes_and_invalid_rules_are_written_back(self):
        ctx = self._make_context()
        template_id = self._template(ctx)
        with tenant_session(ctx) as s:
            draft = self._draft(
                s, ctx, template_id,
                exclusions={
                    "rules": [
                        {"dimensions": {f"{template_id}:a": "a1", f"{template_id}:b": "b1"}, "reason": "clash"},
                        {"dimensions": {"nope:x": "a1"}, "reason": "typo"},
                        {"dimensions": {f"{template_id}:a": "a9"}},
                        "free text",
                    ],
                    "manual": ["A1 with B1"],
                },
            )
            revision_before = draft.revision
            preview = create_preview(s, ctx, draft.id, draft.revision)
            proj = preview.snapshot_json["ui_projections"]

            self.assertEqual(proj["combination_count"], 20)  # 24 - (a1 x b1 x 2 x 2)
            self.assertEqual(proj["exclusion_summary"], {"excluded_count": 4, "reasons": ["clash"]})
            # Only the applicable rule is frozen into the recipe.
            self.assertEqual(
                preview.snapshot_json["recipe"]["exclusions"],
                {"rules": [{"dimensions": {f"{template_id}:a": "a1", f"{template_id}:b": "b1"}, "reason": "clash"}]},
            )

            s.flush()
            reloaded = get_draft(s, ctx, draft.id)
            s.refresh(reloaded)
            invalid = reloaded.exclusions_json["invalid"]
            self.assertEqual(
                [entry["code"] for entry in invalid],
                ["unknown_dimension", "unknown_value", "unparseable_rule", "legacy_manual_exclusion"],
            )
            self.assertEqual(invalid[0]["rule"], {"dimensions": {"nope:x": "a1"}, "reason": "typo"})
            # A server annotation, not a user edit: the revision the preview
            # was frozen against is still the Draft's revision.
            self.assertEqual(reloaded.revision, revision_before)
            self.assertEqual(preview.draft_revision, reloaded.revision)

    def test_invalid_annotation_is_cleared_once_rules_are_fixed(self):
        ctx = self._make_context()
        template_id = self._template(ctx)
        with tenant_session(ctx) as s:
            draft = self._draft(
                s, ctx, template_id,
                exclusions={"rules": [{"dimensions": {"nope:x": "a1"}}]},
            )
            create_preview(s, ctx, draft.id, draft.revision)
            self.assertIn("invalid", get_draft(s, ctx, draft.id).exclusions_json)

            from app.generation.draft import update_draft

            draft = update_draft(
                s, ctx, draft.id, expected_revision=draft.revision,
                exclusions={"rules": [{"dimensions": {f"{template_id}:d": "d2"}, "reason": "no_d2"}]},
            )
            preview = create_preview(s, ctx, draft.id, draft.revision)
            self.assertEqual(preview.snapshot_json["ui_projections"]["combination_count"], 12)
            self.assertNotIn("invalid", get_draft(s, ctx, draft.id).exclusions_json)

    def test_excluding_everything_is_refused_instead_of_a_phantom_default_item(self):
        ctx = self._make_context()
        template_id = self._template(ctx)
        with tenant_session(ctx) as s:
            draft = self._draft(
                s, ctx, template_id,
                exclusions={"rules": [
                    {"dimensions": {f"{template_id}:a": "a1"}},
                    {"dimensions": {f"{template_id}:a": "a2"}},
                ]},
            )
            with self.assertRaises(PreviewError) as caught:
                create_preview(s, ctx, draft.id, draft.revision)
            self.assertEqual(caught.exception.code, "empty_preview")


class VariantLimitTests(DraftControlFixtures):
    def test_preview_rejects_variants_over_the_model_limit(self):
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            draft = create_draft(
                s, ctx,
                product_id=self._product_id(ctx, s),
                provider="mock", model="mock-v1", execution_mode="mock",
                combination_strategy={"kind": "full"},
                format_config={"variants_per_combination": 5},  # mock-v1 allows 4
            )
            with self.assertRaises(PreviewError) as caught:
                create_preview(s, ctx, draft.id, draft.revision)
            self.assertEqual(caught.exception.code, "variants_exceed_model_limit")
            self.assertIn("4", caught.exception.detail)

    def test_preview_rejects_non_positive_or_non_integer_variants(self):
        ctx = self._make_context()
        for bad in (0, -1, "2", 1.5):
            with self.subTest(variants=bad), tenant_session(ctx) as s:
                draft = create_draft(
                    s, ctx,
                    product_id=self._product_id(ctx, s),
                    provider="mock", model="mock-v1", execution_mode="mock",
                    combination_strategy={"kind": "full"},
                    format_config={"variants_per_combination": bad},
                )
                with self.assertRaises(PreviewError) as caught:
                    create_preview(s, ctx, draft.id, draft.revision)
                self.assertEqual(caught.exception.code, "variants_invalid")

    def test_submission_rechecks_the_limit_against_the_deployed_registry(self):
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            draft = create_draft(
                s, ctx,
                product_id=self._product_id(ctx, s),
                provider="mock", model="mock-v1", execution_mode="mock",
                combination_strategy={"kind": "full"},
                format_config={"variants_per_combination": 3},
            )
            preview = create_preview(s, ctx, draft.id, draft.revision)

            shrunk = {
                **PLATFORM_ADAPTER_REGISTRY,
                "mock": {
                    **PLATFORM_ADAPTER_REGISTRY["mock"],
                    "models": {"mock-v1": {"display_name": "Mock", "format_limits": {"max_variants_per_combination": 2}}},
                },
            }
            with mock.patch("app.generation.capabilities.PLATFORM_ADAPTER_REGISTRY", shrunk):
                with self.assertRaises(SubmissionError) as caught:
                    submit_generation(
                        s, ctx,
                        preview_id=preview.id, preview_hash=preview.preview_hash,
                        expected_draft_revision=draft.revision, idempotency_key=new_id("ik"),
                    )
            self.assertEqual(caught.exception.code, "variants_exceed_model_limit")


class ProductBridgeTests(DraftControlFixtures):
    def _published_product(self, ctx: TenantContext, name: str = "Alpha") -> tuple[str, dict]:
        return self._publish(
            ctx, ENTRY_PRODUCT, name,
            {"name": name, "description": "A product", "audience": "everyone"},
        )

    def _bridged_product(self, ctx: TenantContext, entry_id: str) -> ProductRecord | None:
        with tenant_session(ctx) as s:
            product = s.scalar(
                select(ProductRecord).where(
                    ProductRecord.workspace_id == ctx.workspace_id,
                    ProductRecord.product_profile_entry_id == entry_id,
                )
            )
            if product is not None:
                s.expunge(product)
            return product

    def test_forked_product_is_bridged_and_previews_resolve(self):
        ctx = self.ctx()
        _entry, version = self._published_product(ctx)
        forked = service.fork_version(ctx, version["id"], display_name="Alpha Fork")

        product = self._bridged_product(ctx, forked["id"])
        self.assertIsNotNone(product)
        self.assertEqual(product.name, "Alpha Fork")

        with tenant_session(ctx) as s:
            draft = create_draft(
                s, ctx, product_id=forked["id"],
                provider="mock", model="mock-v1", execution_mode="mock",
                combination_strategy={"kind": "full"},
            )
            self.assertEqual(draft.product_id, product.id)
            preview = create_preview(s, ctx, draft.id, draft.revision)
            self.assertEqual(preview.snapshot_json["product_id"], product.id)

    def test_cloned_product_is_bridged_and_previews_resolve(self):
        ctx = self.ctx()
        _entry, version = self._published_product(ctx)
        cloned = service.clone_product(ctx, version["id"], display_name="Alpha Clone")

        product = self._bridged_product(ctx, cloned["id"])
        self.assertIsNotNone(product)
        self.assertEqual(product.name, "Alpha Clone")

        with tenant_session(ctx) as s:
            draft = create_draft(
                s, ctx, product_id=cloned["id"],
                provider="mock", model="mock-v1", execution_mode="mock",
                combination_strategy={"kind": "full"},
            )
            preview = create_preview(s, ctx, draft.id, draft.revision)
            self.assertEqual(preview.snapshot_json["product_id"], product.id)

    def test_forking_a_non_product_creates_no_bridge(self):
        ctx = self.ctx()
        template_id, template_version = self._publish(
            ctx, ENTRY_DIMENSION_TEMPLATE, "Grid", template_content(),
        )
        forked = service.fork_version(ctx, template_version["id"], display_name="Grid Fork")
        self.assertIsNone(self._bridged_product(ctx, forked["id"]))
        self.assertNotEqual(forked["id"], template_id)

    def test_bridge_is_workspace_scoped(self):
        """Negative tenant: another Workspace cannot draft against a forked
        product entry it does not own, even with the exact entry id -- the
        bridge lookup is Workspace-scoped, so the id stays unresolved and the
        Draft's product foreign key refuses it."""
        ctx = self.ctx()
        _entry, version = self._published_product(ctx)
        forked = service.fork_version(ctx, version["id"], display_name="Alpha Fork")

        other = self.ctx()
        self.assertIsNone(self._bridged_product(other, forked["id"]))
        with self.assertRaises(IntegrityError):
            with tenant_session(other) as s:
                create_draft(
                    s, other, product_id=forked["id"],
                    provider="mock", model="mock-v1", execution_mode="mock",
                    combination_strategy={"kind": "full"},
                )
        # The owning Workspace still resolves it.
        with tenant_session(ctx) as s:
            draft = create_draft(
                s, ctx, product_id=forked["id"],
                provider="mock", model="mock-v1", execution_mode="mock",
                combination_strategy={"kind": "full"},
            )
            self.assertNotEqual(draft.product_id, forked["id"])

    def test_publish_syncs_bridged_product_name_from_content(self):
        ctx = self.ctx()
        entry = service.create_entry(
            ctx, entry_type=ENTRY_PRODUCT, display_name="Working title",
            content={"name": "Alpha 65W", "description": "A product", "audience": "everyone"},
        )
        self.assertEqual(self._bridged_product(ctx, entry["id"]).name, "Working title")

        revision = entry["draft"]["revision"]
        service.publish(ctx, entry["id"], revision)
        self.assertEqual(self._bridged_product(ctx, entry["id"]).name, "Alpha 65W")
        self.assertEqual(service.get_entry(ctx, entry["id"])["display_name"], "Alpha 65W")


class DraftControlsHTTPTests(DraftControlFixtures):
    def setUp(self):
        self.ctx = self._make_context()
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def _ik(self, tag: str) -> dict:
        return {"Idempotency-Key": f"ik-157-{tag}-{new_id('k')}"}

    def _create_draft(self, **overrides) -> dict:
        body = {
            "product_id": self._product_id(self.ctx),
            "provider": "mock",
            "model": "mock-v1",
            "execution_mode": "mock",
            "combination_strategy": {"kind": "full"},
        }
        body.update(overrides)
        resp = self.client.post("/api/v1/generation/drafts", json=body, headers=self._ik("draft"))
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()

    def _preview(self, draft: dict):
        return self.client.post(
            f"/api/v1/generation/drafts/{draft['id']}/previews",
            headers={**self._ik("preview"), "If-Match": f'"{draft["revision"]}"'},
        )

    def test_variants_over_limit_is_a_422_problem_detail(self):
        draft = self._create_draft(format_config={"variants_per_combination": 5})
        resp = self._preview(draft)
        self.assertEqual(resp.status_code, 422, resp.text)
        self.assertEqual(resp.headers["content-type"], "application/problem+json")
        body = resp.json()
        self.assertEqual(body["code"], "variants_exceed_model_limit")
        self.assertIn("4", body["detail"])

    def test_invalid_strategy_is_a_422_problem_detail(self):
        draft = self._create_draft(combination_strategy={"kind": "coverage"})
        resp = self._preview(draft)
        self.assertEqual(resp.status_code, 422, resp.text)
        self.assertEqual(resp.json()["code"], "combination_strategy_invalid")

    def test_preview_response_reports_strategy_and_constraint_drops(self):
        template_id = self._template(
            self.ctx, [TemplateConstraintPreviewTests.EXCLUDES_A1_B1],
        )
        draft = self._create_draft(
            combination_strategy={"kind": "coverage", "cap": 12},
            format_config={"variants_per_combination": 2},
            selectors={"dimension_template_ids": [template_id]},
        )
        resp = self._preview(draft)
        self.assertEqual(resp.status_code, 201, resp.text)
        body = resp.json()
        self.assertEqual(body["combination_count"], 12)
        self.assertEqual(body["call_count"], 24)
        self.assertEqual(body["combinations_dropped_by_constraints"], 4)
        self.assertEqual(
            body["strategy_applied"],
            {
                "kind": "coverage", "cap": 12, "deprecated_shape": False,
                "full_count": 24, "candidate_count": 20, "selected_count": 12,
            },
        )

        fetched = self.client.get(f"/api/v1/generation/previews/{body['id']}")
        self.assertEqual(fetched.status_code, 200, fetched.text)
        self.assertEqual(fetched.json()["strategy_applied"], body["strategy_applied"])
        self.assertEqual(fetched.json()["combinations_dropped_by_constraints"], 4)

    def test_invalid_rules_show_up_on_the_draft_after_preview(self):
        template_id = self._template(self.ctx)
        draft = self._create_draft(
            selectors={"dimension_template_ids": [template_id]},
            exclusions={"rules": [{"dimensions": {"ghost:dim": "x"}, "reason": "typo"}]},
        )
        resp = self._preview(draft)
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual(resp.json()["combination_count"], 24)

        fetched = self.client.get(f"/api/v1/generation/drafts/{draft['id']}")
        self.assertEqual(fetched.status_code, 200, fetched.text)
        body = fetched.json()
        self.assertEqual(body["revision"], draft["revision"])
        self.assertEqual(body["exclusions"]["invalid"][0]["code"], "unknown_dimension")
        self.assertEqual(body["exclusions"]["invalid"][0]["rule"]["reason"], "typo")


if __name__ == "__main__":
    unittest.main()
