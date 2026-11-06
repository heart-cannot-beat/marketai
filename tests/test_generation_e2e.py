"""End-to-end Generation → Assets golden path (Issue #21 Task 7).

Exercises the full vertical slice in one pass -- Draft -> Preview -> Atomic
Submission -> Worker (controlled Mock) -> QC -> Human review -> Export -- plus
targeted fault-matrix coverage that does not fit any single earlier task's
test module: GenerationRun state derivation from mixed RunItem outcomes,
cross-Workspace isolation on a Draft mutation, and RecipeSnapshot reuse for
two submissions whose canonical recipe payload is identical.
"""
from tests.support.library_fixtures import LibraryFixtures
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import GenerationRunRecord, new_id


class TestEndToEndGoldenPath(LibraryFixtures):
    def test_draft_to_approved_export(self):
        from app.generation.draft import create_draft
        from app.generation.preview import create_preview
        from app.generation.submission import submit_generation
        from app.generation.worker import handle_run_item
        from app.generation.qc import run_qc
        from app.generation.review import create_review, is_ready_for_experiment
        from app.generation.export import create_export_package, build_manifest
        from app.generation.models import RunItemRecord, RunItemAssetVersionBindingRecord
        from sqlalchemy import select

        ctx = self._make_context()
        with tenant_session(ctx) as s:
            # 1. Create Draft
            draft = create_draft(
                s, ctx,
                product_id=self._product_id(ctx, s),
                provider="mock", model="mock-v1",
                execution_mode="mock",
                combination_strategy={"kind": "full", "planner_version": "v1"},
            )
            self.assertEqual(draft.state, "editing")

            # 2. Create Preview
            preview = create_preview(s, ctx, draft.id, draft.revision)
            self.assertEqual(preview.execution_mode, "mock")

            # 3. Submit
            result = submit_generation(
                s, ctx,
                preview_id=preview.id,
                preview_hash=preview.preview_hash,
                expected_draft_revision=draft.revision,
                idempotency_key=new_id("ik"),
            )
            self.assertIn("generation_run_id", result)

            # 4. Worker executes RunItem (controlled Mock)
            items = s.execute(
                select(RunItemRecord).where(
                    RunItemRecord.generation_run_id == result["generation_run_id"]
                )
            ).scalars().all()
            self.assertTrue(len(items) > 0)

            for item in items:
                handle_run_item(s, ctx, item.id)

            # 5. Verify qc_pending: handle_run_item() advances a RunItem only
            # as far as `qc_pending` -- `succeeded` is QC's transition
            # (step 7 below), not the Worker's.
            s.expire_all()
            for item in items:
                loaded = s.get(RunItemRecord, item.id)
                self.assertEqual(loaded.state, "qc_pending")

            # 6. Get produced AssetVersion
            binding = s.execute(
                select(RunItemAssetVersionBindingRecord).where(
                    RunItemAssetVersionBindingRecord.run_item_id == items[0].id,
                    RunItemAssetVersionBindingRecord.kind == "produced",
                )
            ).scalar_one()
            av_id = binding.asset_version_id

            # 7. QC pass
            run_qc(s, ctx, av_id, items[0].id)

            # 8. Human review: Approved
            create_review(s, ctx, av_id, "approved", "Good quality", "test-actor")

            # 9. Verify ready_for_experiment
            self.assertTrue(is_ready_for_experiment(s, ctx, av_id))

            # 10. Export
            result = create_export_package(
                s, ctx,
                selector={"kind": "explicit", "version_ids": [av_id]},
                idempotency_key=new_id("ik"),
            )
            self.assertEqual(result["state"], "queued")

            manifest = build_manifest(s, ctx, result["package_id"])
            self.assertEqual(manifest["execution_mode"], "mock")
            self.assertEqual(manifest["total_versions"], 1)
            self.assertEqual(
                manifest["entries"][0]["asset_version_id"], av_id,
            )

    def test_run_state_derivation_mixed_outcomes(self):
        from app.generation.run import derive_run_state
        self.assertEqual(derive_run_state(["succeeded", "succeeded"]), "succeeded")
        self.assertEqual(derive_run_state(["succeeded", "failed"]), "partially_succeeded")
        self.assertEqual(derive_run_state(["failed", "failed"]), "failed")
        self.assertEqual(derive_run_state(["cancelled", "cancelled"]), "cancelled")
        self.assertEqual(derive_run_state(["succeeded", "uncertain"]), "uncertain")
        self.assertEqual(derive_run_state(["succeeded", "running"]), "running")
        # `paused` is one of `derive_run_state`'s `_ACTIVE_STATES` (the Run
        # stays actionable while a paused item awaits recovery), so it
        # collapses into `running` rather than surfacing as its own Run
        # status -- confirmed against `app.generation.run.derive_run_state`
        # and the Task 2 review note flagging this collapse.
        self.assertEqual(derive_run_state(["succeeded", "paused"]), "running")
        self.assertEqual(derive_run_state(["succeeded", "cancelled", "failed"]), "partially_succeeded")

    def test_cross_workspace_isolation(self):
        from app.generation.draft import create_draft, DraftNotFoundError
        from app.generation.draft import update_draft

        ctx_a = self._make_context("ws_a")
        ctx_b = self._make_context("ws_b")
        with tenant_session(ctx_a) as s:
            draft = create_draft(
                s, ctx_a,
                product_id=self._product_id(ctx_a, s),
                provider="mock", model="mock-v1",
                execution_mode="mock",
                combination_strategy={"kind": "full", "planner_version": "v1"},
            )
        with tenant_session(ctx_b) as s:
            with self.assertRaises(DraftNotFoundError):
                update_draft(s, ctx_b, draft.id, expected_revision=1, model="x")

    def test_second_equivalent_submission_reuses_snapshot(self):
        from app.generation.snapshot import insert_or_reuse_snapshot

        ctx = self._make_context()
        with tenant_session(ctx) as s:
            snap1 = insert_or_reuse_snapshot(
                s, ctx,
                canonical_json={"prompt": "identical", "provider": "mock"},
                execution_mode="mock",
            )
            snap2 = insert_or_reuse_snapshot(
                s, ctx,
                canonical_json={"prompt": "identical", "provider": "mock"},
                execution_mode="mock",
            )
            self.assertEqual(snap1.id, snap2.id)

    def test_resubmitting_the_same_draft_dispatches_a_second_run(self):
        """An unchanged Draft submitted again is a second generation.

        The recipe snapshot is content-addressed and shared between the two
        Runs (see the test above); the RunItems must not be. Before the fix
        every item of the second Run failed at dispatch with a
        ``UniqueViolation`` on ``spend_reservations`` (and would have hit the
        same wall on ``provider_call_attempts``), because the RunItem's
        semantic fingerprint -- and the reservation / provider-call keys the
        Worker derives from it -- carried no per-submission nonce.
        """
        from sqlalchemy import select

        from app.entitlements.cost_models import SpendReservationRecord
        from app.generation.draft import create_draft
        from app.generation.models import RunItemRecord
        from app.generation.preview import create_preview
        from app.generation.submission import submit_generation
        from app.generation.worker import handle_run_item

        ctx = self._make_context()
        with tenant_session(ctx) as s:
            draft = create_draft(
                s, ctx,
                product_id=self._product_id(ctx, s),
                provider="mock", model="mock-v1",
                execution_mode="mock",
                combination_strategy={"kind": "full", "planner_version": "v1"},
                format_config={"variants_per_combination": 2},
            )

            run_ids: list[str] = []
            for _ in range(2):
                preview = create_preview(s, ctx, draft.id, draft.revision)
                result = submit_generation(
                    s, ctx,
                    preview_id=preview.id,
                    preview_hash=preview.preview_hash,
                    expected_draft_revision=draft.revision,
                    idempotency_key=new_id("ik"),
                )
                run_ids.append(result["generation_run_id"])
                items = s.execute(
                    select(RunItemRecord)
                    .where(RunItemRecord.generation_run_id == result["generation_run_id"])
                    .order_by(RunItemRecord.ordinal)
                ).scalars().all()
                self.assertEqual(len(items), 2)
                for item in items:
                    handle_run_item(s, ctx, item.id)
                s.expire_all()
                self.assertEqual(
                    [s.get(RunItemRecord, item.id).state for item in items],
                    ["qc_pending", "qc_pending"],
                )

            first, second = (
                s.execute(
                    select(RunItemRecord)
                    .where(RunItemRecord.generation_run_id == run_id)
                    .order_by(RunItemRecord.ordinal)
                ).scalars().all()
                for run_id in run_ids
            )
            # Same frozen recipe, shared snapshot row ...
            self.assertEqual(
                s.get(GenerationRunRecord, run_ids[0]).recipe_snapshot_id,
                s.get(GenerationRunRecord, run_ids[1]).recipe_snapshot_id,
            )
            # ... but each Run's items are their own paid intent.
            self.assertTrue(
                {i.semantic_fingerprint for i in first}.isdisjoint(
                    {i.semantic_fingerprint for i in second}
                )
            )
            reservations = s.execute(
                select(SpendReservationRecord.run_item_id).where(
                    SpendReservationRecord.workspace_id == ctx.workspace_id
                )
            ).scalars().all()
            self.assertEqual(
                sorted(reservations), sorted(i.id for i in [*first, *second])
            )
