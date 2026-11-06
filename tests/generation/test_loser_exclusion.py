"""Loser exclusion in the generation preview (Issue #49 Task 8).

Adapted from the plan brief's pytest-flavored pseudocode
(``.superpowers/sdd/2026-09-08-assets-server-contracts/task-8-brief.md``) to
this repo's real conventions -- ``unittest.TestCase`` + ``LibraryFixtures``,
matching ``tests/test_preview_projections.py`` (the existing preview UI
projection tests) and ``tests/test_experiment_conclusions.py`` (the Task 6
conclusion-projection fixture chain). There is no ``db_session``/
``tenant_ctx``/``make_draft_with_losers`` pytest fixture ecosystem in this
repo, and ``create_preview``'s real signature is
``create_preview(session, ctx, draft_id, draft_revision, *, ...,
experiment_context=None, skip_losers=None)`` -- not
``create_preview(db_session, tenant_ctx, draft_id=..., expected_revision=...,
experiment_context_id=...)`` the brief's pseudocode names, and it returns the
persisted ``ResolutionPreviewRecord`` (its UI projections live in
``preview.snapshot_json["ui_projections"]``), not a bare dataclass with
``.items``/``.skipped_signature_count`` attributes.

``stable_dimension_signature`` has no column on ``AssetVersionRecord``, nor
on a not-yet-generated preview combination -- it lives on the *producing*
``RunItemRecord`` (see ``app.generation.regeneration``'s module docstring).
A preview combination's own signature is its dimension-only fingerprint
(``app.generation.preview._enumerate_combinations``'s "key=value" join,
already exercised by ``tests/test_preview_projections.py``) -- the same
identity a RunItem produced from that combination would carry forward.
Loser exclusion therefore joins: draft dimensions -> combo fingerprint ->
the newest produced AssetVersion carrying that same
``stable_dimension_signature`` (scoped to the draft's Product) -> that
AssetVersion's contextual conclusion in the active experiment context
(Task 6's ``project_contextual_conclusion_for_context``).
"""
from __future__ import annotations

import unittest
from datetime import timedelta
from decimal import Decimal

from tests.support.library_fixtures import LibraryFixtures

from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    AssetRecord,
    AssetVersionRecord,
    ContextualVerdictProjectionRecord,
    ExperimentRecord,
    ExperimentVersionRecord,
    GenerationRunRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    new_id,
    utcnow,
)
from app.generation.draft import create_draft
from app.generation.models import RunItemAssetVersionBindingRecord, RunItemRecord
from app.generation.preview import create_preview
from app.platform.tenancy import TenantContext

H64 = "a" * 64


class _LoserExclusionFixtures(LibraryFixtures):
    """Shared fixture helpers: an Experiment/ExperimentVersion context plus
    AssetVersion+RunItem+ContextualVerdictProjectionRecord chains carrying a
    ``stable_dimension_signature`` -- the same shape
    ``tests/test_regeneration_replay.py`` and
    ``tests/test_experiment_conclusions.py`` build, merged so both a
    Product-scoped signature and a real ``experiment_id`` context line up."""

    def _library_version_id(self, ctx: TenantContext, session) -> str:
        entry = LibraryEntryRecord(
            id=new_id("lentry"), workspace_id=ctx.workspace_id,
            entry_type="product", display_name="Test",
        )
        session.add(entry)
        session.flush()
        version = LibraryVersionRecord(
            id=new_id("lver"), workspace_id=ctx.workspace_id, entry_id=entry.id,
            entry_type="product", version_no=1, content_json={},
            content_hash=H64, published_by="test",
        )
        session.add(version)
        session.flush()
        return version.id

    def _seed_experiment_context(
        self, ctx: TenantContext, session, *, product_id: str,
    ) -> str:
        """A minimal frozen Experiment/ExperimentVersion so
        ``ContextualVerdictProjectionRecord`` rows can anchor to a real
        ``experiment_id`` (Task 6's ``context_id``/``experiment_context``).
        Returns the ``experiment_id``."""
        experiment_id = new_id("exp")
        experiment = ExperimentRecord(
            id=experiment_id, workspace_id=ctx.workspace_id, product_id=product_id,
            name="Test experiment", state="frozen", revision=1,
            config_json={}, metric_contract_hash=H64,
        )
        session.add(experiment)
        session.flush()

        library_version_id = self._library_version_id(ctx, session)
        now = utcnow()
        experiment_version = ExperimentVersionRecord(
            id=new_id("ev"), workspace_id=ctx.workspace_id, experiment_id=experiment_id,
            canonicalization_version="p1-migration-v1", frozen_snapshot_json={},
            frozen_snapshot_hash=H64, metric_contract_hash=H64,
            market="US", placement="feed", landing_page_evidence_hash=H64,
            audience_snapshot_json={}, audience_snapshot_hash=H64,
            daily_budget_amount=Decimal("100"), total_budget_cap=Decimal("3000"),
            budget_currency="USD",
            schedule_start_at=now, schedule_end_at=now + timedelta(days=30),
            schedule_timezone="UTC",
            attribution_click_window_days=7, attribution_view_window_days=1,
            statistical_plan_json={}, statistical_plan_hash=H64,
            marketing_vars_schema_version="1",
            product_version_id=library_version_id, offer_version_id=library_version_id,
            candidates_hash=H64, concept_resolutions_hash=H64,
        )
        session.add(experiment_version)
        session.flush()
        return experiment_id

    def _make_signature_version(
        self, ctx: TenantContext, session, *, product_id: str, signature: str,
    ) -> str:
        """A full AssetVersion + ``produced`` RunItem binding carrying
        ``signature``, scoped to a caller-chosen ``product_id`` so it lines
        up with the preview draft's own Product (``_resolve_loser_signatures``
        filters by it)."""
        run = GenerationRunRecord(
            id=new_id("gr"), workspace_id=ctx.workspace_id, product_id=product_id,
            plan_id=self._plan_id(ctx, session), provider="mock", model="mock-v1",
            aspect_ratio="1:1", image_size="1024x1024", variants=1, use_ref=False,
            status="running", total=1, done=0, failed=0,
            est_cost=Decimal("0"), currency="USD", revision=1,
        )
        session.add(run)
        session.flush()

        asset = AssetRecord(
            id=new_id("asset"), workspace_id=ctx.workspace_id, product_id=product_id,
            run_id=run.id, combo_id="combo_1", combo_code="combo_1", tags_json={},
            filename="test.png", provider="mock", model="mock-v1", prompt="test",
            fingerprint="fp_" + new_id("h"), status="succeeded",
        )
        session.add(asset)
        session.flush()

        item = RunItemRecord(
            id=new_id("ri"), workspace_id=ctx.workspace_id, generation_run_id=run.id,
            ordinal=0, combination_fingerprint="fp_" + new_id("h"),
            semantic_fingerprint="sf_" + new_id("h"), stable_dimension_signature=signature,
            execution_mode="mock", state="succeeded", revision=1,
            created_at=utcnow(), updated_at=utcnow(),
        )
        session.add(item)
        session.flush()

        version = AssetVersionRecord(
            id=new_id("ver"), workspace_id=ctx.workspace_id, asset_id=asset.id,
            kind="generated", aspect_ratio="1:1", object_key=f"legacy/{asset.id}.png",
            content_sha256="c" * 64, byte_size=1024, mime_type="image/png",
            origin_kind="generation", execution_mode="mock", created_at=utcnow(),
        )
        session.add(version)
        session.flush()

        session.add(RunItemAssetVersionBindingRecord(
            id=new_id("rib"), workspace_id=ctx.workspace_id, run_item_id=item.id,
            asset_version_id=version.id, kind="produced",
        ))
        session.flush()
        return version.id

    def _seed_conclusion(
        self, ctx: TenantContext, session, *,
        experiment_id: str, asset_version_id: str, verdict: str,
    ) -> None:
        """A ``ContextualVerdictProjectionRecord`` anchored to the
        ExperimentVersion ``_seed_experiment_context`` created, reusing its
        market/placement/product/offer version -- the way a real Experiment
        anchors several candidates (Winner and Loser alike) to one context."""
        experiment_version = session.execute(
            select(ExperimentVersionRecord).where(
                ExperimentVersionRecord.workspace_id == ctx.workspace_id,
                ExperimentVersionRecord.experiment_id == experiment_id,
            )
        ).scalar_one()
        session.add(ContextualVerdictProjectionRecord(
            id=new_id("cvp"), workspace_id=ctx.workspace_id, experiment_id=experiment_id,
            experiment_version_id=experiment_version.id, candidate_id=new_id("cand"),
            decision_id=new_id("dec"), confirmation_id=new_id("conf"),
            verdict=verdict, metric="ctr", estimate=None,
            asset_version_id=asset_version_id,
            product_version_id=experiment_version.product_version_id,
            offer_version_id=experiment_version.offer_version_id,
            market=experiment_version.market, placement=experiment_version.placement,
            landing_page_evidence_hash=H64, confirmed_at=utcnow(),
        ))
        session.flush()

    def _make_draft_with_losers(
        self, ctx: TenantContext, session, *,
        loser_labels: list[str], winner_labels: list[str],
        seed_context: bool = True,
    ):
        """Create a Draft whose single ``variant`` dimension carries one
        value per label, plus a produced AssetVersion+RunItem per label with
        a matching ``stable_dimension_signature`` and a Loser/Winner
        conclusion anchored to a fresh experiment context (unless
        ``seed_context`` is ``False``, matching the brief's
        ``experiment_context_id=None`` case). Returns
        ``(draft, experiment_id_or_None)``."""
        product_id = self._product_id(ctx, session)
        experiment_id = (
            self._seed_experiment_context(ctx, session, product_id=product_id)
            if seed_context else None
        )

        labels = [*loser_labels, *winner_labels]
        draft = create_draft(
            session, ctx,
            product_id=product_id, provider="mock", model="mock-v1",
            execution_mode="mock",
            combination_strategy={"kind": "full", "planner_version": "v1"},
            selectors={"dimensions": {"variant": labels}},
        )
        session.flush()

        for label in loser_labels:
            signature = f"variant={label}"
            version_id = self._make_signature_version(
                ctx, session, product_id=product_id, signature=signature,
            )
            if experiment_id:
                self._seed_conclusion(
                    ctx, session, experiment_id=experiment_id,
                    asset_version_id=version_id, verdict="lose",
                )
        for label in winner_labels:
            signature = f"variant={label}"
            version_id = self._make_signature_version(
                ctx, session, product_id=product_id, signature=signature,
            )
            if experiment_id:
                self._seed_conclusion(
                    ctx, session, experiment_id=experiment_id,
                    asset_version_id=version_id, verdict="win",
                )

        return draft, experiment_id


class TestLoserExclusion(_LoserExclusionFixtures):
    def test_preview_excludes_loser_signatures(self):
        """Preview with an active experiment context skips Loser dimension
        signatures and reports how many signatures/calls were skipped."""
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            draft, experiment_id = self._make_draft_with_losers(
                ctx, s,
                loser_labels=["loser-a", "loser-b"],
                winner_labels=["winner-a"],
            )
            preview = create_preview(
                s, ctx, draft.id, draft.revision,
                experiment_context=experiment_id,
            )
            proj = preview.snapshot_json["ui_projections"]

        self.assertEqual(proj["skipped_signature_count"], 2)
        self.assertGreater(proj["skipped_call_count"], 0)

        included_signatures = {
            i["stable_dimension_signature"] for i in proj["items"] if not i["excluded"]
        }
        self.assertNotIn("variant=loser-a", included_signatures)
        self.assertNotIn("variant=loser-b", included_signatures)
        self.assertIn("variant=winner-a", included_signatures)

        self.assertEqual(proj["combination_count"], 1)
        self.assertEqual(proj["call_count"], 1)

    def test_preview_includes_all_without_context(self):
        """Preview without an experiment context includes every signature,
        even ones that carry a Loser conclusion in some other context."""
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            draft, _experiment_id = self._make_draft_with_losers(
                ctx, s,
                loser_labels=["loser-a"],
                winner_labels=["winner-a"],
            )
            preview = create_preview(s, ctx, draft.id, draft.revision)
            proj = preview.snapshot_json["ui_projections"]

        self.assertEqual(proj["skipped_signature_count"], 0)
        signatures = {i["stable_dimension_signature"] for i in proj["items"]}
        self.assertIn("variant=loser-a", signatures)
        self.assertIn("variant=winner-a", signatures)

    def test_preview_loser_exclusion_opt_out(self):
        """A caller can explicitly disable Loser exclusion even with an
        active experiment context (``skip_losers=False``)."""
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            draft, experiment_id = self._make_draft_with_losers(
                ctx, s,
                loser_labels=["loser-a"],
                winner_labels=["winner-a"],
            )
            preview = create_preview(
                s, ctx, draft.id, draft.revision,
                experiment_context=experiment_id,
                skip_losers=False,
            )
            proj = preview.snapshot_json["ui_projections"]

        self.assertEqual(proj["skipped_signature_count"], 0)
        signatures = {i["stable_dimension_signature"] for i in proj["items"]}
        self.assertIn("variant=loser-a", signatures)

    def test_skip_losers_defaults_true_when_context_present(self):
        """``skip_losers`` need not be passed explicitly -- supplying
        ``experiment_context`` alone is enough to enable exclusion."""
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            draft, experiment_id = self._make_draft_with_losers(
                ctx, s,
                loser_labels=["loser-a"],
                winner_labels=["winner-a"],
            )
            preview = create_preview(
                s, ctx, draft.id, draft.revision,
                experiment_context=experiment_id,
            )
            proj = preview.snapshot_json["ui_projections"]

        self.assertEqual(proj["skipped_signature_count"], 1)

    def test_stale_conclusion_does_not_exclude(self):
        """Only the *newest* AssetVersion for a signature decides exclusion
        -- an older Loser call for a signature that has since won no longer
        excludes it."""
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            product_id = self._product_id(ctx, s)
            experiment_id = self._seed_experiment_context(ctx, s, product_id=product_id)

            draft = create_draft(
                s, ctx,
                product_id=product_id, provider="mock", model="mock-v1",
                execution_mode="mock",
                combination_strategy={"kind": "full", "planner_version": "v1"},
                selectors={"dimensions": {"variant": ["reversed"]}},
            )
            s.flush()

            signature = "variant=reversed"
            older_version_id = self._make_signature_version(
                ctx, s, product_id=product_id, signature=signature,
            )
            self._seed_conclusion(
                ctx, s, experiment_id=experiment_id,
                asset_version_id=older_version_id, verdict="lose",
            )
            newer_version_id = self._make_signature_version(
                ctx, s, product_id=product_id, signature=signature,
            )
            self._seed_conclusion(
                ctx, s, experiment_id=experiment_id,
                asset_version_id=newer_version_id, verdict="win",
            )

            preview = create_preview(
                s, ctx, draft.id, draft.revision,
                experiment_context=experiment_id,
            )
            proj = preview.snapshot_json["ui_projections"]

        self.assertEqual(proj["skipped_signature_count"], 0)
        signatures = {i["stable_dimension_signature"] for i in proj["items"]}
        self.assertIn(signature, signatures)

    def test_estimated_cost_saving_reflects_skipped_calls(self):
        """``estimated_cost_saving`` uses the same per-call cost the
        included items' ``maximum_cost`` is computed from."""
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            product_id = self._product_id(ctx, s)
            experiment_id = self._seed_experiment_context(ctx, s, product_id=product_id)

            draft = create_draft(
                s, ctx,
                product_id=product_id, provider="acme", model="acme-v1",
                execution_mode="sandbox",
                combination_strategy={"kind": "full", "planner_version": "v1"},
                selectors={"dimensions": {"variant": ["loser-a", "winner-a"]}},
            )
            s.flush()

            loser_version_id = self._make_signature_version(
                ctx, s, product_id=product_id, signature="variant=loser-a",
            )
            self._seed_conclusion(
                ctx, s, experiment_id=experiment_id,
                asset_version_id=loser_version_id, verdict="lose",
            )
            winner_version_id = self._make_signature_version(
                ctx, s, product_id=product_id, signature="variant=winner-a",
            )
            self._seed_conclusion(
                ctx, s, experiment_id=experiment_id,
                asset_version_id=winner_version_id, verdict="win",
            )

            registry = {
                "acme_sandbox": {
                    "advertised": True,
                    "version": "v1",
                    "capabilities": {"image_generation": True},
                    "cost_policy": {
                        "true_payable": "provider_quoted",
                        "estimated_per_call": "2.50",
                        "currency": "USD",
                    },
                },
            }
            preview = create_preview(
                s, ctx, draft.id, draft.revision,
                adapter_registry=registry,
                experiment_context=experiment_id,
            )
            proj = preview.snapshot_json["ui_projections"]

        self.assertEqual(proj["skipped_signature_count"], 1)
        self.assertEqual(proj["skipped_call_count"], 1)
        self.assertEqual(proj["estimated_cost_saving"]["amount"], "2.5")
        self.assertEqual(proj["estimated_cost_saving"]["currency"], "USD")
        self.assertEqual(proj["maximum_cost"]["amount"], "2.5")

    def test_http_preview_route_accepts_experiment_context_query_params(self):
        """The ``POST /drafts/{id}/previews`` route forwards
        ``experiment_context``/``skip_losers`` query params through to the
        domain function and surfaces the new fields in the response."""
        from fastapi.testclient import TestClient

        from app.main import app
        from app.platform.http_identity import request_tenant_context

        ctx = self._make_context()
        app.dependency_overrides[request_tenant_context] = lambda: ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(client.close)

        with tenant_session(ctx) as s:
            draft, experiment_id = self._make_draft_with_losers(
                ctx, s,
                loser_labels=["loser-a"],
                winner_labels=["winner-a"],
            )
            draft_id, draft_revision = draft.id, draft.revision

        response = client.post(
            f"/api/v1/generation/drafts/{draft_id}/previews",
            params={"experiment_context": experiment_id},
            headers={
                "Idempotency-Key": f"ik-loser-{new_id('k')}",
                "If-Match": f'"{draft_revision}"',
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["skipped_signature_count"], 1)
        self.assertGreater(body["skipped_call_count"], 0)
        self.assertIsNotNone(body["estimated_cost_saving"])
        signatures = {i["stable_dimension_signature"] for i in body["items"] if not i["excluded"]}
        self.assertNotIn("variant=loser-a", signatures)
        self.assertIn("variant=winner-a", signatures)


if __name__ == "__main__":
    unittest.main()
