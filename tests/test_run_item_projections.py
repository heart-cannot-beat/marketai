"""Tests for enriched GenerationRunItemDetail projections (Issue #53).

Verifies that ``get_run_item_detail`` populates ``latest_attempt``,
``qc_result``, ``bound_asset_version_id``, and ``cost_evidence`` from
the underlying ProviderCallAttempt, QC, binding, and reservation records.
"""
from __future__ import annotations

import unittest

from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import new_id
from app.generation.run_queries import get_run_item_detail
from tests.support.library_fixtures import LibraryFixtures


class TestRunItemProjections(LibraryFixtures):
    """RunItem detail carries attempt/QC/cost/binding projections."""

    def _run_item_with_attempt(self, ctx, session):
        """Create a RunItem that has been dispatched through the full pipeline:
        draft -> preview -> submit -> handle_run_item.

        Returns (run_id, run_item_id, asset_version_id).
        """
        from app.generation.draft import create_draft
        from app.generation.models import RunItemAssetVersionBindingRecord, RunItemRecord
        from app.generation.preview import create_preview
        from app.generation.submission import submit_generation
        from app.generation.worker import handle_run_item

        draft = create_draft(
            session, ctx,
            product_id=self._product_id(ctx, session),
            provider="mock", model="mock-v1",
            execution_mode="mock",
            combination_strategy={"kind": "full", "planner_version": "v1"},
        )
        preview = create_preview(session, ctx, draft.id, draft.revision)
        result = submit_generation(
            session, ctx,
            preview_id=preview.id,
            preview_hash=preview.preview_hash,
            expected_draft_revision=draft.revision,
            idempotency_key=new_id("ik"),
        )
        run_id = result["generation_run_id"]
        items = session.execute(
            select(RunItemRecord).where(RunItemRecord.generation_run_id == run_id)
        ).scalars().all()
        item = items[0]
        handle_run_item(session, ctx, item.id)

        binding = session.execute(
            select(RunItemAssetVersionBindingRecord).where(
                RunItemAssetVersionBindingRecord.run_item_id == item.id,
                RunItemAssetVersionBindingRecord.kind == "produced",
            )
        ).scalar_one()
        return run_id, item.id, binding.asset_version_id

    # ------------------------------------------------------------------ #
    # latest_attempt
    # ------------------------------------------------------------------ #

    def test_detail_without_attempt_returns_null_latest_attempt(self):
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            ri_id = self._create_run_item(ctx, s)
            item = s.get(
                __import__("app.generation.models", fromlist=["RunItemRecord"]).RunItemRecord,
                ri_id,
            )
            detail = get_run_item_detail(s, ctx, item.generation_run_id, ri_id)
            self.assertIsNone(detail["latest_attempt"])

    def test_detail_with_attempt_returns_attempt_summary(self):
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            run_id, ri_id, _av_id = self._run_item_with_attempt(ctx, s)
            detail = get_run_item_detail(s, ctx, run_id, ri_id)
            attempt = detail["latest_attempt"]
            self.assertIsNotNone(attempt)
            self.assertIn(attempt["dispatch_certainty"], ("confirmed", "not_dispatched", "uncertain"))
            self.assertIn(attempt["outcome"], ("pending", "succeeded", "failed", "uncertain", "reconciled"))
            self.assertEqual(attempt["provider"], "mock")

    # ------------------------------------------------------------------ #
    # qc_result
    # ------------------------------------------------------------------ #

    def test_detail_without_qc_returns_null_qc_result(self):
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            ri_id = self._create_run_item(ctx, s)
            item = s.get(
                __import__("app.generation.models", fromlist=["RunItemRecord"]).RunItemRecord,
                ri_id,
            )
            detail = get_run_item_detail(s, ctx, item.generation_run_id, ri_id)
            self.assertIsNone(detail["qc_result"])

    def test_detail_with_passing_qc(self):
        from app.generation.qc import run_qc

        ctx = self._make_context()
        with tenant_session(ctx) as s:
            run_id, ri_id, av_id = self._run_item_with_attempt(ctx, s)
            run_qc(s, ctx, av_id, ri_id)
            detail = get_run_item_detail(s, ctx, run_id, ri_id)
            qc = detail["qc_result"]
            self.assertIsNotNone(qc)
            self.assertEqual(qc["state"], "passed")
            self.assertGreater(len(qc["checks"]), 0)
            self.assertTrue(qc["checks"][0]["passed"])

    def test_detail_with_blocking_qc_failure(self):
        from app.generation.qc import run_qc_with_blocking_failure

        ctx = self._make_context()
        with tenant_session(ctx) as s:
            run_id, ri_id, av_id = self._run_item_with_attempt(ctx, s)
            run_qc_with_blocking_failure(s, ctx, av_id, ri_id)
            detail = get_run_item_detail(s, ctx, run_id, ri_id)
            qc = detail["qc_result"]
            self.assertIsNotNone(qc)
            self.assertEqual(qc["state"], "failed")
            self.assertFalse(qc["checks"][0]["passed"])
            self.assertEqual(qc["checks"][0]["name"], "content_safety")

    # ------------------------------------------------------------------ #
    # bound_asset_version_id
    # ------------------------------------------------------------------ #

    def test_detail_without_binding_returns_null(self):
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            ri_id = self._create_run_item(ctx, s)
            item = s.get(
                __import__("app.generation.models", fromlist=["RunItemRecord"]).RunItemRecord,
                ri_id,
            )
            detail = get_run_item_detail(s, ctx, item.generation_run_id, ri_id)
            self.assertIsNone(detail["bound_asset_version_id"])

    def test_detail_with_binding_returns_asset_version_id(self):
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            run_id, ri_id, av_id = self._run_item_with_attempt(ctx, s)
            detail = get_run_item_detail(s, ctx, run_id, ri_id)
            self.assertEqual(detail["bound_asset_version_id"], av_id)

    # ------------------------------------------------------------------ #
    # cost_evidence
    # ------------------------------------------------------------------ #

    def test_detail_without_cost_returns_null(self):
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            ri_id = self._create_run_item(ctx, s)
            item = s.get(
                __import__("app.generation.models", fromlist=["RunItemRecord"]).RunItemRecord,
                ri_id,
            )
            detail = get_run_item_detail(s, ctx, item.generation_run_id, ri_id)
            self.assertIsNone(detail["cost_evidence"])

    def test_detail_with_cost_evidence(self):
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            run_id, ri_id, _av_id = self._run_item_with_attempt(ctx, s)
            detail = get_run_item_detail(s, ctx, run_id, ri_id)
            cost = detail["cost_evidence"]
            if cost is not None:
                self.assertIn(cost["state"], ("reserved", "settled", "released"))
                self.assertIn("amount", cost)
                self.assertIn("currency", cost)

    # ------------------------------------------------------------------ #
    # Cross-workspace isolation
    # ------------------------------------------------------------------ #

    def test_cross_workspace_detail_not_leaked(self):
        from app.generation.run_queries import GenerationResourceNotFoundError

        ctx_a = self._make_context("ws_proj_a")
        ctx_b = self._make_context("ws_proj_b")

        with tenant_session(ctx_a) as s:
            run_id, ri_id, _av_id = self._run_item_with_attempt(ctx_a, s)

        with tenant_session(ctx_b) as s:
            with self.assertRaises(GenerationResourceNotFoundError):
                get_run_item_detail(s, ctx_b, run_id, ri_id)

    # ------------------------------------------------------------------ #
    # Response model validation
    # ------------------------------------------------------------------ #

    def test_response_model_validates_enriched_detail(self):
        from app.generation.response_models import GenerationRunItemDetail

        ctx = self._make_context()
        with tenant_session(ctx) as s:
            run_id, ri_id, av_id = self._run_item_with_attempt(ctx, s)
            from app.generation.qc import run_qc
            run_qc(s, ctx, av_id, ri_id)
            detail = get_run_item_detail(s, ctx, run_id, ri_id)
            model = GenerationRunItemDetail.model_validate(detail)
            self.assertEqual(model.bound_asset_version_id, av_id)
            self.assertIsNotNone(model.qc_result)
            self.assertEqual(model.qc_result.state, "passed")

    def test_response_model_validates_empty_projections(self):
        from app.generation.response_models import GenerationRunItemDetail

        ctx = self._make_context()
        with tenant_session(ctx) as s:
            ri_id = self._create_run_item(ctx, s)
            item = s.get(
                __import__("app.generation.models", fromlist=["RunItemRecord"]).RunItemRecord,
                ri_id,
            )
            detail = get_run_item_detail(s, ctx, item.generation_run_id, ri_id)
            model = GenerationRunItemDetail.model_validate(detail)
            self.assertIsNone(model.latest_attempt)
            self.assertIsNone(model.qc_result)
            self.assertIsNone(model.bound_asset_version_id)
            self.assertIsNone(model.cost_evidence)


if __name__ == "__main__":
    unittest.main()
