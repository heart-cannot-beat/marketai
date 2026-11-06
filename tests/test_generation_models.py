import unittest
from datetime import datetime, timezone
from decimal import Decimal

from tests.support.library_fixtures import LibraryFixtures

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    GenerationRunRecord,
    AssetVersionRecord,
    QualityReviewRecord,
    ResolutionPreviewRecord,
    new_id,
    utcnow,
)
from app.generation.models import (
    GenerationDraftRecord,
    ResolutionPreviewConfirmationRecord,
    GenerationRecipeSnapshotRecord,
    RunItemRecord,
    ProviderAttemptEventRecord,
    ProviderAttemptProjectionRecord,
    RunItemAssetVersionBindingRecord,
    AssetQCAttemptRecord,
    AssetQCCheckResultRecord,
    GenerationRunCommandRecord,
    ExportPackageRecord,
)
from app.platform.tenancy import TenantContext


class TestGenerationModelsExist(LibraryFixtures):
    def test_generation_draft_roundtrip(self):
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            draft = GenerationDraftRecord(
                id=new_id("gd"),
                workspace_id=ctx.workspace_id,
                product_id=self._product_id(ctx, s),
                selectors_json={},
                provider="mock",
                model="mock-v1",
                execution_mode="mock",
                combination_strategy_json={"kind": "full", "planner_version": "v1"},
                revision=1,
                state="editing",
                created_by="test-actor",
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            s.add(draft)
            s.flush()
            loaded = s.get(GenerationDraftRecord, draft.id)
            self.assertEqual(loaded.workspace_id, ctx.workspace_id)
            self.assertEqual(loaded.execution_mode, "mock")

    def test_recipe_snapshot_uniqueness(self):
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            snap1 = GenerationRecipeSnapshotRecord(
                id=new_id("rs"),
                workspace_id=ctx.workspace_id,
                canonicalization_version="v1",
                recipe_hash="abc123" * 4,
                canonical_json={},
                execution_mode="mock",
                created_at=utcnow(),
            )
            s.add(snap1)
            s.flush()
            snap2 = GenerationRecipeSnapshotRecord(
                id=new_id("rs"),
                workspace_id=ctx.workspace_id,
                canonicalization_version="v1",
                recipe_hash="abc123" * 4,
                canonical_json={},
                execution_mode="mock",
                created_at=utcnow(),
            )
            s.add(snap2)
            from sqlalchemy.exc import IntegrityError
            with self.assertRaises(IntegrityError):
                s.flush()
            s.rollback()

    def test_run_item_ordinal_uniqueness(self):
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            run_id = self._create_generation_run(ctx, s)
            item1 = RunItemRecord(
                id=new_id("ri"),
                workspace_id=ctx.workspace_id,
                generation_run_id=run_id,
                ordinal=0,
                combination_fingerprint="fp1",
                semantic_fingerprint="sf1",
                execution_mode="mock",
                state="pending",
                revision=1,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            item2 = RunItemRecord(
                id=new_id("ri"),
                workspace_id=ctx.workspace_id,
                generation_run_id=run_id,
                ordinal=0,
                combination_fingerprint="fp2",
                semantic_fingerprint="sf2",
                execution_mode="mock",
                state="pending",
                revision=1,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            s.add_all([item1, item2])
            from sqlalchemy.exc import IntegrityError
            with self.assertRaises(IntegrityError):
                s.flush()
            s.rollback()

    def test_provider_attempt_event_sequence_uniqueness(self):
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            attempt_id = self._create_provider_attempt(ctx, s)
            evt1 = ProviderAttemptEventRecord(
                id=new_id("pae"),
                workspace_id=ctx.workspace_id,
                provider_call_attempt_id=attempt_id,
                sequence=1,
                event_type="dispatch_started",
                payload_json={},
                idempotency_key="k1",
                executor="worker-1",
                created_at=utcnow(),
            )
            evt2 = ProviderAttemptEventRecord(
                id=new_id("pae"),
                workspace_id=ctx.workspace_id,
                provider_call_attempt_id=attempt_id,
                sequence=1,
                event_type="response_received",
                payload_json={},
                idempotency_key="k2",
                executor="worker-1",
                created_at=utcnow(),
            )
            s.add_all([evt1, evt2])
            from sqlalchemy.exc import IntegrityError
            with self.assertRaises(IntegrityError):
                s.flush()
            s.rollback()

    def test_cross_workspace_run_item_fk_rejected(self):
        ctx_a = self._make_context("ws_a")
        ctx_b = self._make_context("ws_b")
        with tenant_session(ctx_a) as s:
            run_id = self._create_generation_run(ctx_a, s)
        with tenant_session(ctx_b) as s:
            item = RunItemRecord(
                id=new_id("ri"),
                workspace_id=ctx_b.workspace_id,
                generation_run_id=run_id,
                ordinal=0,
                combination_fingerprint="fp",
                semantic_fingerprint="sf",
                execution_mode="mock",
                state="pending",
                revision=1,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            s.add(item)
            from sqlalchemy.exc import IntegrityError
            with self.assertRaises(IntegrityError):
                s.flush()
            s.rollback()

    def test_generation_run_extended_fields(self):
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            snap_id = self._create_recipe_snapshot(ctx, s)
            conf_id = self._create_confirmation(ctx, s)
            op_id = self._create_operation(ctx, s)
            run = GenerationRunRecord(
                id=new_id("gr"),
                workspace_id=ctx.workspace_id,
                product_id=self._product_id(ctx, s),
                plan_id=self._plan_id(ctx, s),
                provider="mock",
                model="mock-v1",
                aspect_ratio="1:1",
                image_size="1024x1024",
                variants=1,
                use_ref=False,
                status="running",
                total=4,
                done=0,
                failed=0,
                est_cost=Decimal("0"),
                currency="USD",
                revision=1,
                recipe_snapshot_id=snap_id,
                creation_preview_confirmation_id=conf_id,
                creation_operation_id=op_id,
                submission_kind="confirmed_preview",
                execution_mode="mock",
            )
            s.add(run)
            s.flush()
            loaded = s.get(GenerationRunRecord, run.id)
            self.assertEqual(loaded.recipe_snapshot_id, snap_id)
            self.assertEqual(loaded.submission_kind, "confirmed_preview")

    def test_asset_version_generation_origin(self):
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            ri_id = self._create_run_item(ctx, s)
            pa_id = self._create_provider_attempt(ctx, s, run_item_id=ri_id)
            av = AssetVersionRecord(
                id=new_id("av"),
                workspace_id=ctx.workspace_id,
                asset_id=self._asset_id(ctx, s),
                kind="generated",
                aspect_ratio="1:1",
                object_key="test/obj",
                content_sha256="a" * 64,
                byte_size=1024,
                mime_type="image/png",
                producing_run_item_id=ri_id,
                provider_attempt_id=pa_id,
                origin_kind="generation",
                execution_mode="mock",
            )
            s.add(av)
            s.flush()
            loaded = s.get(AssetVersionRecord, av.id)
            self.assertEqual(loaded.producing_run_item_id, ri_id)
            self.assertEqual(loaded.origin_kind, "generation")

    # Helper methods below are specific to this TestCase; shared setup
    # (_make_context, _product_id, _plan_id, _asset_id, _create_operation,
    # _create_job, _create_reservation, _create_generation_run,
    # _create_run_item) is provided by LibraryFixtures.

    def _create_recipe_snapshot(self, ctx, session):
        snap = GenerationRecipeSnapshotRecord(
            id=new_id("rs"),
            workspace_id=ctx.workspace_id,
            canonicalization_version="v1",
            recipe_hash="unique_" + new_id("h"),
            canonical_json={},
            execution_mode="mock",
            created_at=utcnow(),
        )
        session.add(snap)
        session.flush()
        return snap.id

    def _create_confirmation(self, ctx, session):
        preview_id = self._create_preview(ctx, session)
        conf = ResolutionPreviewConfirmationRecord(
            id=new_id("rpc"),
            workspace_id=ctx.workspace_id,
            preview_id=preview_id,
            preview_hash="ph_" + new_id("h"),
            actor_id="test-actor",
            confirmed_at=utcnow(),
            request_fingerprint="rf_" + new_id("h"),
            quoted_max_provider_amount=Decimal("1.00"),
            quoted_max_budget_amount=Decimal("1.00"),
            quoted_currency="USD",
        )
        session.add(conf)
        session.flush()
        return conf.id

    def _create_preview(self, ctx, session):
        from app.adapters.persistence.models import ResolutionPreviewRecord
        preview = ResolutionPreviewRecord(
            id=new_id("rp"),
            workspace_id=ctx.workspace_id,
            generation_draft_id=new_id("gd"),
            draft_revision=1,
            snapshot_json={},
            preview_hash="ph_" + new_id("h"),
            recipe_input_hash="rih_" + new_id("h"),
            execution_mode="mock",
            valid_until=utcnow(),
            created_by="test",
            purpose="initial_generation",
        )
        session.add(preview)
        session.flush()
        return preview.id

    def _create_provider_attempt(self, ctx, session, run_item_id=None):
        from app.entitlements.cost_models import ProviderCallAttemptRecord
        attempt = ProviderCallAttemptRecord(
            id=new_id("pca"),
            workspace_id=ctx.workspace_id,
            operation_id=self._create_operation(ctx, session),
            job_id=self._create_job(ctx, session),
            spend_reservation_id=self._create_reservation(ctx, session),
            call_semantic_key_hash="cskh_" + new_id("h"),
            attempt_no=0,
            execution_mode="mock",
            provider="mock",
            prepared_at=utcnow(),
            updated_at=utcnow(),
            run_item_id=run_item_id,
            step_kind="generation",
        )
        session.add(attempt)
        session.flush()
        return attempt.id


class TestProviderAttemptModule(LibraryFixtures):
    def test_append_event_advances_projection(self):
        from app.generation.provider_attempt import (
            prepare_attempt, append_event, _get_projection,
        )
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            attempt = prepare_attempt(
                s,
                workspace_id=ctx.workspace_id,
                operation_id=self._create_operation(ctx, s),
                job_id=self._create_job(ctx, s),
                spend_reservation_id=self._create_reservation(ctx, s),
                run_item_id=self._create_run_item(ctx, s),
                step_kind="generation",
                call_semantic_key_hash="cskh_test",
                attempt_no=0,
                execution_mode="mock",
                provider="mock",
            )
            proj = _get_projection(s, ctx.workspace_id, attempt.id)
            self.assertEqual(proj.dispatch_certainty, "not_dispatched")
            self.assertEqual(proj.outcome, "pending")
            self.assertEqual(proj.last_event_sequence, 0)

            append_event(
                s,
                workspace_id=ctx.workspace_id,
                provider_call_attempt_id=attempt.id,
                event_type="dispatch_started",
                payload={},
                executor="test-worker",
            )
            self.assertEqual(proj.dispatch_certainty, "dispatched")
            self.assertEqual(proj.last_event_sequence, 1)

            append_event(
                s,
                workspace_id=ctx.workspace_id,
                provider_call_attempt_id=attempt.id,
                event_type="response_received",
                payload={"output_ref": "test"},
                executor="test-worker",
            )
            self.assertEqual(proj.outcome, "succeeded")
            self.assertEqual(proj.last_event_sequence, 2)

    def test_rebuild_matches_incremental(self):
        from app.generation.provider_attempt import (
            prepare_attempt, append_event, _get_projection,
            rebuild_projection,
        )
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            attempt = prepare_attempt(
                s,
                workspace_id=ctx.workspace_id,
                operation_id=self._create_operation(ctx, s),
                job_id=self._create_job(ctx, s),
                spend_reservation_id=self._create_reservation(ctx, s),
                run_item_id=self._create_run_item(ctx, s),
                step_kind="generation",
                call_semantic_key_hash="cskh_rebuild",
                attempt_no=0,
                execution_mode="mock",
                provider="mock",
            )
            append_event(
                s,
                workspace_id=ctx.workspace_id,
                provider_call_attempt_id=attempt.id,
                event_type="dispatch_started",
                payload={},
                executor="test-worker",
            )
            append_event(
                s,
                workspace_id=ctx.workspace_id,
                provider_call_attempt_id=attempt.id,
                event_type="response_received",
                payload={"output_ref": "test"},
                executor="test-worker",
            )

            proj_before = _get_projection(s, ctx.workspace_id, attempt.id)
            certainty_before = proj_before.dispatch_certainty
            outcome_before = proj_before.outcome
            seq_before = proj_before.last_event_sequence

            rebuilt = rebuild_projection(s, ctx.workspace_id, attempt.id)
            self.assertEqual(rebuilt.dispatch_certainty, certainty_before)
            self.assertEqual(rebuilt.outcome, outcome_before)
            self.assertEqual(rebuilt.last_event_sequence, seq_before)


if __name__ == "__main__":
    unittest.main()
