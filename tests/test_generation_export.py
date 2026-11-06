from tests.support.library_fixtures import LibraryFixtures
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import new_id


class TestExportPackage(LibraryFixtures):
    def test_single_mode_export(self):
        from app.generation.export import create_export_package, get_export_package
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            av_id = self._create_approved_version(ctx, s)
            result = create_export_package(
                s, ctx,
                selector={"kind": "explicit", "version_ids": [av_id]},
            )
            self.assertEqual(result["state"], "queued")
            self.assertIn("package_id", result)
            self.assertIn("operation_id", result)

            package = get_export_package(s, ctx, result["package_id"])
            self.assertEqual(package.execution_mode, "mock")
            self.assertEqual(package.state, "pending")

    def test_mixed_mode_rejected(self):
        from app.generation.export import create_export_package, ExportError
        from app.adapters.persistence.models import AssetVersionRecord
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            av_id_1 = self._create_approved_version(ctx, s)
            # A second submission with an identical recipe would hash to the
            # same semantic fingerprint and collide on the SpendReservation
            # unique (workspace_id, semantic_key_hash) constraint (recipe
            # identity is intentionally Product-independent -- see
            # app.generation.preview) -- vary planner_version so this is a
            # genuinely distinct RunItem/AssetVersion pair, same as any two
            # unrelated generations in one Workspace would be.
            av_id_2 = self._create_approved_version(
                ctx, s, planner_version="v2",
            )
            # Every fixture in this suite runs through the mock adapter, so
            # to exercise the real `len(modes) > 1` branch (rather than a
            # bypass flag) we flip one version's already-frozen
            # execution_mode column directly -- simulating a selection that
            # spans a genuinely different execution mode without needing a
            # live byok/sandbox provider in tests.
            version_2 = s.get(AssetVersionRecord, av_id_2)
            version_2.execution_mode = "sandbox"
            s.flush()

            with self.assertRaises(ExportError) as cm:
                create_export_package(
                    s, ctx,
                    selector={"kind": "explicit", "version_ids": [av_id_1, av_id_2]},
                )
            self.assertEqual(cm.exception.code, "mixed_execution_modes")

    def test_manifest_contains_provenance(self):
        from app.generation.export import create_export_package, build_manifest
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            av_id = self._create_approved_version(ctx, s)
            result = create_export_package(
                s, ctx,
                selector={"kind": "explicit", "version_ids": [av_id]},
            )
            manifest = build_manifest(s, ctx, result["package_id"])
            self.assertIn("entries", manifest)
            self.assertIn("execution_mode", manifest)
            self.assertEqual(manifest["execution_mode"], "mock")
            self.assertEqual(manifest["entries"][0]["asset_version_id"], av_id)

    def test_empty_selection_rejected(self):
        from app.generation.export import create_export_package, ExportError
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            with self.assertRaises(ExportError) as cm:
                create_export_package(s, ctx, selector={"kind": "explicit", "version_ids": []})
            self.assertEqual(cm.exception.code, "empty_selection")

    def test_cross_workspace_export_rejected(self):
        from app.generation.export import create_export_package, ExportError
        ctx_a = self._make_context()
        ctx_b = self._make_context()
        with tenant_session(ctx_a) as s:
            av_id = self._create_approved_version(ctx_a, s)
        with tenant_session(ctx_b) as s:
            with self.assertRaises(ExportError) as cm:
                create_export_package(
                    s, ctx_b, selector={"kind": "explicit", "version_ids": [av_id]},
                )
            self.assertEqual(cm.exception.code, "asset_version_not_found")

    def test_idempotent_export_replay_returns_same_package(self):
        from app.generation.export import create_export_package
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            av_id = self._create_approved_version(ctx, s)
            key = new_id("ik")
            result1 = create_export_package(
                s, ctx, selector={"kind": "explicit", "version_ids": [av_id]}, idempotency_key=key,
            )
            result2 = create_export_package(
                s, ctx, selector={"kind": "explicit", "version_ids": [av_id]}, idempotency_key=key,
            )
            self.assertEqual(result1["package_id"], result2["package_id"])
            self.assertEqual(result1["operation_id"], result2["operation_id"])

    def test_download_intent_requires_ready_package(self):
        from app.generation.export import create_export_package, resolve_export_blob_ref, ExportError
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            av_id = self._create_approved_version(ctx, s)
            result = create_export_package(
                s, ctx,
                selector={"kind": "explicit", "version_ids": [av_id]},
            )
            with self.assertRaises(ExportError) as cm:
                resolve_export_blob_ref(s, ctx, result["package_id"])
            self.assertEqual(cm.exception.code, "export_package_not_ready")

    def _create_approved_version(self, ctx, session, planner_version="v1"):
        from app.generation.draft import create_draft
        from app.generation.preview import create_preview
        from app.generation.submission import submit_generation
        from app.generation.worker import handle_run_item
        from app.generation.qc import run_qc
        from app.generation.review import create_review
        from app.generation.models import RunItemRecord, RunItemAssetVersionBindingRecord
        from sqlalchemy import select

        draft = create_draft(
            session, ctx,
            product_id=self._product_id(ctx, session),
            provider="mock", model="mock-v1",
            execution_mode="mock",
            combination_strategy={"kind": "full", "planner_version": planner_version},
        )
        preview = create_preview(session, ctx, draft.id, draft.revision)
        result = submit_generation(
            session, ctx,
            preview_id=preview.id,
            preview_hash=preview.preview_hash,
            expected_draft_revision=draft.revision,
            idempotency_key=new_id("ik"),
        )
        items = session.execute(
            select(RunItemRecord).where(
                RunItemRecord.generation_run_id == result["generation_run_id"]
            )
        ).scalars().all()
        handle_run_item(session, ctx, items[0].id)
        binding = session.execute(
            select(RunItemAssetVersionBindingRecord).where(
                RunItemAssetVersionBindingRecord.run_item_id == items[0].id,
                RunItemAssetVersionBindingRecord.kind == "produced",
            )
        ).scalar_one()
        run_qc(session, ctx, binding.asset_version_id, items[0].id)
        create_review(
            session, ctx, binding.asset_version_id,
            "approved", "Good", "test",
        )
        return binding.asset_version_id
