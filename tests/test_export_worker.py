"""Export package Worker tests (Issue #49 Task 9).

Exercises ``app.generation.export_worker.handle_export_package`` directly
against a real tenant-scoped session and a real ``LocalFileBlobStore``
(dev-only filesystem Adapter), following the exact conventions
``tests/test_committed_blob_refs.py`` established for this Adapter and
``tests/test_generation_export.py``/``LibraryFixtures`` established for
Generation domain tests.

This repo's test suite uses ``unittest.TestCase`` + ``LibraryFixtures``
(not pytest fixtures like ``db_session``/``tenant_ctx``/``local_blob_store``/
``make_export_package_with_blobs``), and has no pytest-asyncio plugin
installed -- ``asyncio.run`` drives the async Worker call from an ordinary
synchronous test method instead of ``unittest.IsolatedAsyncioTestCase``,
since the surrounding ``tenant_session`` context manager is itself
synchronous.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from tests.support.library_fixtures import LibraryFixtures

from app.adapters.blob_store_local import LocalFileBlobStore
from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import AssetVersionRecord, JobRecord, new_id
from app.generation.export import create_export_package, resolve_export_blob_ref
from app.generation.export_worker import handle_export_package
from app.generation.models import ExportPackageRecord


class TestExportWorker(LibraryFixtures):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.blob_store = LocalFileBlobStore(
            root=Path(self._tmp.name),
            public_base_url="http://localhost:9000",
            signing_secret=b"top-secret",
        )
        self.ctx = self._make_context()
        # create_export_package() enqueues a durable "generation.export_package"
        # Job this suite drives directly via handle_export_package(), never
        # through the real Job queue -- see test_export_async.py's setUp for
        # why a left-behind "queued" row here would leak into an unrelated
        # later test's own jobs.claim_next() call.
        self.addCleanup(self._drain_export_jobs)

    def _drain_export_jobs(self) -> None:
        with platform_session() as session:
            session.query(JobRecord).filter(
                JobRecord.workspace_id == self.ctx.workspace_id,
                JobRecord.state.in_(("queued", "retry_wait")),
            ).update({"state": "cancelled"}, synchronize_session=False)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _seed_committed_version(self, session, *, content: bytes, seq: int, mime: str = "image/png") -> str:
        """Create an AssetVersion with a real committed blob on disk (both
        the DB row's committed refs and the actual bytes the Worker streams
        via ``open_committed``)."""
        import hashlib

        content_hash = hashlib.sha256(content).hexdigest()
        ext = "png" if mime == "image/png" else "bin"
        key = f"committed/{self.ctx.workspace_id}/assets/2026/09/08/v{seq}.{ext}"
        path = self.blob_store.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

        asset_id = self._asset_id(self.ctx, session)
        version = AssetVersionRecord(
            id=new_id("av"),
            workspace_id=self.ctx.workspace_id,
            asset_id=asset_id,
            kind="generated",
            aspect_ratio="1:1",
            object_key=f"legacy/{asset_id}.{ext}",
            content_sha256=content_hash,
            byte_size=len(content),
            mime_type=mime,
            origin_kind="generation",
            execution_mode="mock",
            committed_object_key=key,
            committed_storage_version_id=f"sv-{seq}",
        )
        session.add(version)
        session.flush()
        return version.id

    def _create_package(self, session, version_ids: list[str]) -> str:
        result = create_export_package(
            session, self.ctx, selector={"kind": "explicit", "version_ids": version_ids},
        )
        return result["package_id"]

    def _run_worker(self, session, package_id: str) -> None:
        asyncio.run(handle_export_package(session, self.blob_store, package_id))

    # ------------------------------------------------------------------

    def test_worker_builds_deterministic_zip(self):
        """Worker builds ZIP with deterministic content and commits it."""
        with tenant_session(self.ctx) as session:
            version_ids = [
                self._seed_committed_version(session, content=f"content-{i}".encode(), seq=i)
                for i in range(3)
            ]
            package_id = self._create_package(session, version_ids)
            self._run_worker(session, package_id)

            reloaded = session.get(ExportPackageRecord, package_id)
            self.assertEqual(reloaded.state, "ready")
            self.assertTrue(reloaded.committed_object_key)
            self.assertTrue(reloaded.committed_storage_version_id)
            self.assertTrue(reloaded.zip_hash)
            self.assertGreater(reloaded.byte_size, 0)
            committed = resolve_export_blob_ref(session, self.ctx, package_id)
            self.assertEqual(
                committed.storage_version_id,
                reloaded.committed_storage_version_id,
            )

    def test_worker_deduplicates_by_content_hash(self):
        """Duplicate content hashes produce one physical payload per hash."""
        with tenant_session(self.ctx) as session:
            shared = b"identical-bytes"
            v1 = self._seed_committed_version(session, content=shared, seq=1)
            v2 = self._seed_committed_version(session, content=shared, seq=2)
            v3 = self._seed_committed_version(session, content=b"unique-bytes", seq=3)
            package_id = self._create_package(session, [v1, v2, v3])
            self._run_worker(session, package_id)

            reloaded = session.get(ExportPackageRecord, package_id)
            self.assertEqual(reloaded.state, "ready")
            self.assertEqual(reloaded.counts_json["logical_count"], 3)
            self.assertEqual(reloaded.counts_json["unique_blob_count"], 2)
            self.assertEqual(reloaded.counts_json["dedup_count"], 1)
            self.assertGreater(reloaded.counts_json["dedup_count"], 0)
            self.assertLess(reloaded.counts_json["unique_blob_count"], reloaded.counts_json["logical_count"])

    def test_worker_fails_on_unavailable_blob(self):
        """Worker fails the whole package when one blob is unavailable."""
        with tenant_session(self.ctx) as session:
            asset_id = self._asset_id(self.ctx, session)
            # No committed_object_key/committed_storage_version_id --
            # resolve_asset_blob_ref fails closed with asset_blob_unavailable.
            version = AssetVersionRecord(
                id=new_id("av"),
                workspace_id=self.ctx.workspace_id,
                asset_id=asset_id,
                kind="generated",
                aspect_ratio="1:1",
                object_key="legacy/missing.png",
                content_sha256="f" * 64,
                byte_size=10,
                mime_type="image/png",
                origin_kind="generation",
                execution_mode="mock",
            )
            session.add(version)
            session.flush()

            package_id = self._create_package(session, [version.id])
            self._run_worker(session, package_id)

            reloaded = session.get(ExportPackageRecord, package_id)
            self.assertEqual(reloaded.state, "failed")
            evidence = json.loads(reloaded.failure_evidence)
            self.assertEqual(evidence["code"], "asset_blob_unavailable")

    def test_worker_fails_on_missing_blob_file(self):
        """A committed ref whose bytes don't actually exist on disk also fails closed."""
        with tenant_session(self.ctx) as session:
            asset_id = self._asset_id(self.ctx, session)
            version = AssetVersionRecord(
                id=new_id("av"),
                workspace_id=self.ctx.workspace_id,
                asset_id=asset_id,
                kind="generated",
                aspect_ratio="1:1",
                object_key="legacy/ghost.png",
                content_sha256="a" * 64,
                byte_size=10,
                mime_type="image/png",
                origin_kind="generation",
                execution_mode="mock",
                committed_object_key="committed/ws/assets/2026/09/08/ghost.png",
                committed_storage_version_id="sv-ghost",
            )
            session.add(version)
            session.flush()

            package_id = self._create_package(session, [version.id])
            self._run_worker(session, package_id)

            reloaded = session.get(ExportPackageRecord, package_id)
            self.assertEqual(reloaded.state, "failed")
            evidence = json.loads(reloaded.failure_evidence)
            self.assertEqual(evidence["code"], "asset_blob_unavailable")

    def test_worker_includes_manifest_json_in_zip(self):
        """The committed ZIP contains manifest.json with full provenance."""
        with tenant_session(self.ctx) as session:
            version_ids = [
                self._seed_committed_version(session, content=f"m-{i}".encode(), seq=i)
                for i in range(2)
            ]
            package_id = self._create_package(session, version_ids)
            self._run_worker(session, package_id)
            reloaded = session.get(ExportPackageRecord, package_id)
            committed_key = reloaded.committed_object_key

        zip_path = self.blob_store.root / committed_key
        with zipfile.ZipFile(zip_path, "r") as zf:
            self.assertIn("manifest.json", zf.namelist())
            manifest = json.loads(zf.read("manifest.json"))
            self.assertEqual(manifest["package_id"], package_id)
            self.assertEqual(len(manifest["entries"]), 2)

    def test_worker_deterministic_zip_timestamps(self):
        """ZIP timestamps and permissions are fixed for determinism."""
        with tenant_session(self.ctx) as session:
            version_ids = [self._seed_committed_version(session, content=b"solo", seq=1)]
            package_id = self._create_package(session, version_ids)
            self._run_worker(session, package_id)
            reloaded = session.get(ExportPackageRecord, package_id)
            committed_key = reloaded.committed_object_key

        zip_path = self.blob_store.root / committed_key
        with zipfile.ZipFile(zip_path, "r") as zf:
            self.assertGreater(len(zf.infolist()), 0)
            for info in zf.infolist():
                self.assertEqual(info.date_time, (2020, 1, 1, 0, 0, 0))
                self.assertEqual(info.external_attr, 0o644 << 16)

    def test_worker_asset_entry_bytes_are_stable_across_packages(self):
        """The same content_sha256 produces the same asset archive member
        bytes/name across two different packages (manifest.json itself
        differs -- it carries the package_id -- but the deduplicated asset
        payloads are byte-identical, matching the fixed timestamp/permission
        contract that guarantees the archive is reproducible from the same
        frozen content, not from wall-clock time)."""
        with tenant_session(self.ctx) as session:
            content = b"stable-asset-bytes"
            v_a = self._seed_committed_version(session, content=content, seq=1)
            package_id_a = self._create_package(session, [v_a])
            self._run_worker(session, package_id_a)
            reloaded_a = session.get(ExportPackageRecord, package_id_a)
            with zipfile.ZipFile(self.blob_store.root / reloaded_a.committed_object_key) as zf:
                asset_names_a = [n for n in zf.namelist() if n.startswith("assets/")]
                asset_bytes_a = {n: zf.read(n) for n in asset_names_a}

        with tenant_session(self.ctx) as session:
            v_b = self._seed_committed_version(session, content=content, seq=2)
            package_id_b = self._create_package(session, [v_b])
            self._run_worker(session, package_id_b)
            reloaded_b = session.get(ExportPackageRecord, package_id_b)
            with zipfile.ZipFile(self.blob_store.root / reloaded_b.committed_object_key) as zf:
                asset_names_b = [n for n in zf.namelist() if n.startswith("assets/")]
                asset_bytes_b = {n: zf.read(n) for n in asset_names_b}

        self.assertEqual(asset_names_a, asset_names_b)
        self.assertEqual(asset_bytes_a, asset_bytes_b)


if __name__ == "__main__":
    unittest.main()
