"""Generation's server-side commits must satisfy the Railway Bucket Adapter.

``BlobStore.commit_generated_artifact`` takes ``expected_sha256_b64`` -- the
base64 digest -- and the production ``RailwayBucketBlobStore`` re-hashes the
file and refuses the commit on a mismatch. The dev ``LocalFileBlobStore``
echoes whatever it is handed, so the two Generation callers that passed the
*hex* digest (the Mock materialization and the export package build) passed
the suite and failed every production commit with "generated artifact
checksum changed before commit": every Mock RunItem stayed ``pending`` and
no export could become ``ready``.

These tests drive both callers through the real Adapter against the fake S3
client ``tests/test_blob_store_railway_bucket.py`` already uses.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib

from sqlalchemy import select

from app.adapters.blob_store_railway_bucket import RailwayBucketBlobStore
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AssetVersionRecord, JobRecord, new_id
from app.generation.models import RunItemAssetVersionBindingRecord, RunItemRecord
from tests.support.library_fixtures import LibraryFixtures
from tests.support.queue_hygiene import cancel_workspace_jobs, quarantine_foreign_jobs
from tests.test_blob_store_railway_bucket import _FakeRailwayClient


def _b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


class TestMockOutputCommit(LibraryFixtures):
    def setUp(self) -> None:
        self.client = _FakeRailwayClient()
        self.store = RailwayBucketBlobStore(
            bucket="marketai-test",
            endpoint_url="https://storage.invalid",
            client=self.client,
        )
        self.ctx = self._make_context()
        self.addCleanup(cancel_workspace_jobs, self.ctx.workspace_id)

    def test_materialize_mock_output_passes_the_adapter_checksum_gate(self):
        from app.generation.worker import materialize_mock_output

        ctx = self.ctx
        output, committed = asyncio.run(
            materialize_mock_output(self.store, ctx, "b" * 64)
        )

        stored = self.client.objects[committed.committed_key]
        self.assertEqual(stored, output["content_bytes"])
        self.assertEqual(hashlib.sha256(stored).hexdigest(), output["content_sha256"])
        self.assertEqual(committed.checksum_sha256_b64, _b64(stored))
        self.assertEqual(committed.size_bytes, len(stored))
        self.assertEqual(committed.content_type_verified, "image/png")

    def test_mock_run_item_commits_through_the_worker_and_records_a_hex_digest(self):
        from app.generation.draft import create_draft
        from app.generation.preview import create_preview
        from app.generation.submission import submit_generation
        from app.worker_runtime import WorkerRuntime

        ctx = self.ctx
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
        run_id = result["generation_run_id"]

        # Drain only this Workspace's Jobs (earlier tests' leftovers are
        # deferred first): every Job this Worker meets commits through the
        # Railway Adapter, QC included.
        quarantine_foreign_jobs(ctx.workspace_id)
        runtime = WorkerRuntime(owner="railway-commit-worker")
        runtime.blob_store = self.store
        while asyncio.run(runtime.run_once()):
            pass

        with tenant_session(ctx) as s:
            jobs = s.execute(
                select(JobRecord).where(
                    JobRecord.workspace_id == ctx.workspace_id,
                    JobRecord.type == "generation.run_item",
                )
            ).scalars().all()
            self.assertTrue(jobs)
            self.assertEqual(
                {(job.state, job.error_code) for job in jobs}, {("succeeded", "")}
            )

            items = s.execute(
                select(RunItemRecord).where(
                    RunItemRecord.generation_run_id == run_id,
                    RunItemRecord.workspace_id == ctx.workspace_id,
                )
            ).scalars().all()
            self.assertTrue(items)
            for item in items:
                self.assertNotIn(item.state, ("pending", "queued", "failed"))
                binding = s.execute(
                    select(RunItemAssetVersionBindingRecord).where(
                        RunItemAssetVersionBindingRecord.run_item_id == item.id
                    )
                ).scalar_one()
                version = s.get(AssetVersionRecord, binding.asset_version_id)
                stored = self.client.objects[version.committed_object_key]
                # Records keep the hex digest; the bucket holds the exact bytes.
                self.assertEqual(len(version.content_sha256), 64)
                self.assertEqual(version.content_sha256, hashlib.sha256(stored).hexdigest())
                self.assertEqual(version.byte_size, len(stored))
                self.assertEqual(version.mime_type, "image/png")
                self.assertTrue(version.committed_storage_version_id)


class TestExportPackageCommit(LibraryFixtures):
    def setUp(self) -> None:
        self.client = _FakeRailwayClient()
        self.store = RailwayBucketBlobStore(
            bucket="marketai-test",
            endpoint_url="https://storage.invalid",
            client=self.client,
        )
        self.ctx = self._make_context()
        self.addCleanup(cancel_workspace_jobs, self.ctx.workspace_id)

    def _seed_committed_version(self, session, *, content: bytes, seq: int) -> str:
        key = f"committed/{self.ctx.workspace_id}/assets/2026/10/06/v{seq}.png"
        self.client.put_test_object(key, content, content_type="image/png")
        asset_id = self._asset_id(self.ctx, session)
        version = AssetVersionRecord(
            id=new_id("av"),
            workspace_id=self.ctx.workspace_id,
            asset_id=asset_id,
            kind="generated",
            aspect_ratio="1:1",
            object_key=f"legacy/{asset_id}.png",
            content_sha256=hashlib.sha256(content).hexdigest(),
            byte_size=len(content),
            mime_type="image/png",
            origin_kind="generation",
            execution_mode="mock",
            committed_object_key=key,
            committed_storage_version_id=f"sv-{seq}",
        )
        session.add(version)
        session.flush()
        return version.id

    def test_export_package_passes_the_adapter_checksum_gate(self):
        from app.generation.export import create_export_package
        from app.generation.export_worker import handle_export_package
        from app.generation.models import ExportPackageRecord

        with tenant_session(self.ctx) as session:
            version_ids = [
                self._seed_committed_version(
                    session, content=f"railway-{i}".encode(), seq=i
                )
                for i in range(2)
            ]
            package_id = create_export_package(
                session, self.ctx,
                selector={"kind": "explicit", "version_ids": version_ids},
            )["package_id"]

            asyncio.run(handle_export_package(session, self.store, package_id))

            package = session.get(ExportPackageRecord, package_id)
            self.assertEqual(package.state, "ready", package.failure_evidence)
            stored = self.client.objects[package.committed_object_key]
            self.assertEqual(hashlib.sha256(stored).hexdigest(), package.zip_hash)
            self.assertEqual(len(stored), package.byte_size)
            self.assertEqual(
                self.client.content_types[package.committed_object_key],
                "application/zip",
            )
