import unittest
import asyncio
import hashlib
import tempfile
from decimal import Decimal

from tests.support.library_fixtures import LibraryFixtures
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import new_id


class TestRunItemWorker(LibraryFixtures):
    def test_mock_output_is_committed_as_a_valid_png(self):
        from app.adapters.blob_store_local import LocalFileBlobStore
        from app.generation.worker import materialize_mock_output

        ctx = self._make_context()
        with tempfile.TemporaryDirectory() as root:
            store = LocalFileBlobStore(
                root=root,
                public_base_url="http://blob.test",
                signing_secret=b"test-secret",
            )

            async def materialize_and_read():
                output, committed = await materialize_mock_output(
                    store, ctx, "a" * 64
                )
                payload = b"".join(
                    [chunk async for chunk in store.open_committed(committed)]
                )
                return output, committed, payload

            output, committed, payload = asyncio.run(materialize_and_read())

        self.assertTrue(payload.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(hashlib.sha256(payload).hexdigest(), output["content_sha256"])
        self.assertEqual(committed.size_bytes, len(payload))
        self.assertEqual(committed.content_type_verified, "image/png")

    def test_success_path_produces_asset_version(self):
        from app.generation.draft import create_draft
        from app.generation.preview import create_preview
        from app.generation.submission import submit_generation
        from app.generation.worker import handle_run_item
        from app.generation.models import RunItemRecord
        from app.adapters.persistence.models import AssetVersionRecord
        from sqlalchemy import select

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
            result = submit_generation(
                s, ctx,
                preview_id=preview.id,
                preview_hash=preview.preview_hash,
                expected_draft_revision=draft.revision,
                idempotency_key=new_id("ik"),
            )

            items = s.execute(
                select(RunItemRecord).where(
                    RunItemRecord.generation_run_id == result["generation_run_id"]
                )
            ).scalars().all()
            self.assertTrue(len(items) > 0)

            item = items[0]
            handle_run_item(s, ctx, item.id)
            s.expire_all()

            loaded = s.get(RunItemRecord, item.id)
            self.assertEqual(loaded.state, "qc_pending")

            from app.generation.models import RunItemAssetVersionBindingRecord

            binding = s.execute(
                select(RunItemAssetVersionBindingRecord).where(
                    RunItemAssetVersionBindingRecord.run_item_id == item.id
                )
            ).scalar_one()
            version = s.get(AssetVersionRecord, binding.asset_version_id)
            self.assertIsNotNone(version)
            self.assertEqual(version.origin_kind, "generation")
            self.assertEqual(version.execution_mode, "mock")

    def test_crash_after_dispatch_creates_uncertain(self):
        from app.generation.draft import create_draft
        from app.generation.preview import create_preview
        from app.generation.submission import submit_generation
        from app.generation.worker import (
            handle_run_item_dispatch_only,
        )
        from app.generation.models import RunItemRecord

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
            result = submit_generation(
                s, ctx,
                preview_id=preview.id,
                preview_hash=preview.preview_hash,
                expected_draft_revision=draft.revision,
                idempotency_key=new_id("ik"),
            )
            from sqlalchemy import select
            items = s.execute(
                select(RunItemRecord).where(
                    RunItemRecord.generation_run_id == result["generation_run_id"]
                )
            ).scalars().all()
            item = items[0]

            handle_run_item_dispatch_only(s, ctx, item.id)
            s.expire_all()
            loaded = s.get(RunItemRecord, item.id)
            self.assertEqual(loaded.state, "uncertain")

            from app.adapters.persistence.models import GenerationRunRecord

            run = s.get(GenerationRunRecord, result["generation_run_id"])
            self.assertEqual(run.status, "uncertain")

    def test_mock_has_zero_true_payable(self):
        from app.generation.draft import create_draft
        from app.generation.preview import create_preview
        from app.generation.submission import submit_generation
        from app.generation.worker import handle_run_item
        from app.generation.models import RunItemRecord
        from app.generation.provider_attempt import _get_projection
        from app.entitlements.cost_models import (
            ProviderCallAttemptRecord,
            SpendReservationRecord,
        )
        from sqlalchemy import select

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
            result = submit_generation(
                s, ctx,
                preview_id=preview.id,
                preview_hash=preview.preview_hash,
                expected_draft_revision=draft.revision,
                idempotency_key=new_id("ik"),
            )
            items = s.execute(
                select(RunItemRecord).where(
                    RunItemRecord.generation_run_id == result["generation_run_id"]
                )
            ).scalars().all()
            handle_run_item(s, ctx, items[0].id)

            reservation = s.execute(
                select(SpendReservationRecord).where(
                    SpendReservationRecord.workspace_id == ctx.workspace_id,
                    SpendReservationRecord.run_item_id == items[0].id,
                )
            ).scalar_one()
            self.assertEqual(reservation.maximum_provider_amount, Decimal("0"))
            self.assertEqual(reservation.maximum_budget_amount, Decimal("0"))
            self.assertEqual(len(reservation.semantic_key_hash), 64)

            attempt = s.execute(
                select(ProviderCallAttemptRecord).where(
                    ProviderCallAttemptRecord.workspace_id == ctx.workspace_id,
                    ProviderCallAttemptRecord.run_item_id == items[0].id,
                )
            ).scalar_one()
            self.assertEqual(len(attempt.call_semantic_key_hash), 64)
            self.assertEqual(attempt.chargeability, "legacy_reserved")

    def test_cross_workspace_run_item_rejected(self):
        from sqlalchemy.exc import NoResultFound

        from app.generation.draft import create_draft
        from app.generation.preview import create_preview
        from app.generation.submission import submit_generation
        from app.generation.worker import handle_run_item
        from app.generation.models import RunItemRecord
        from sqlalchemy import select

        ctx_a = self._make_context("ws_worker_a")
        ctx_b = self._make_context("ws_worker_b")

        with tenant_session(ctx_a) as s:
            draft = create_draft(
                s, ctx_a,
                product_id=self._product_id(ctx_a, s),
                provider="mock", model="mock-v1",
                execution_mode="mock",
                combination_strategy={"kind": "full", "planner_version": "v1"},
            )
            preview = create_preview(s, ctx_a, draft.id, draft.revision)
            result = submit_generation(
                s, ctx_a,
                preview_id=preview.id,
                preview_hash=preview.preview_hash,
                expected_draft_revision=draft.revision,
                idempotency_key=new_id("ik"),
            )
            items = s.execute(
                select(RunItemRecord).where(
                    RunItemRecord.generation_run_id == result["generation_run_id"]
                )
            ).scalars().all()
            item_id = items[0].id

        with tenant_session(ctx_b) as s:
            with self.assertRaises(NoResultFound):
                handle_run_item(s, ctx_b, item_id)

        # The RunItem must be untouched by the rejected cross-workspace call.
        with tenant_session(ctx_a) as s:
            loaded = s.get(RunItemRecord, item_id)
            self.assertEqual(loaded.state, "pending")


if __name__ == "__main__":
    unittest.main()
