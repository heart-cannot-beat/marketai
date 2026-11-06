"""Committed blob refs on AssetVersionRecord + BlobStore extensions (Issue #49 Task 1).

`AssetVersionRecord.committed_object_key`/`committed_storage_version_id`
carry the exact `CommittedBlobRef` an AssetVersion was produced from (spec
§7.4), independent of the legacy `object_key` column. `BlobStore.open_committed`
lets the export Worker (Task 9) stream a committed blob's bytes without a
signed URL round-trip; `BlobStore.commit_generated_artifact` lets it commit a
server-generated artifact (e.g. an export ZIP) directly, bypassing the
staging/inspect path meant for browser uploads.

Follows this suite's established conventions (`unittest.TestCase` +
`LibraryFixtures` for DB-backed model tests, `unittest.IsolatedAsyncioTestCase`
for the async `BlobStore` Adapter methods -- see `tests/test_blob_store.py`)
rather than pytest fixtures, since this repo's test suite does not use
pytest-asyncio or the `db_session`/`tenant_ctx`/`local_blob_store` fixture
names.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sqlalchemy import select

from tests.support.library_fixtures import LibraryFixtures

from app.adapters.blob_store_local import LocalFileBlobStore
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AssetVersionRecord, new_id
from app.platform.blob_store import (
    BlobStore,
    BlobStoreError,
    CommittedBlobRef,
    GeneratedArtifactSpec,
)


class TestAssetVersionCommittedRefColumns(LibraryFixtures):
    """`asset_versions.committed_object_key`/`committed_storage_version_id`."""

    def _make_version(self, ctx, session, **overrides) -> AssetVersionRecord:
        asset_id = self._asset_id(ctx, session)
        fields = dict(
            id=new_id("ver"),
            workspace_id=ctx.workspace_id,
            asset_id=asset_id,
            kind="generated",
            aspect_ratio="1:1",
            language="en",
            object_key=f"legacy/{asset_id}.png",
            content_sha256="c" * 64,
            byte_size=100,
            mime_type="image/png",
        )
        fields.update(overrides)
        version = AssetVersionRecord(**fields)
        session.add(version)
        session.flush()
        return version

    def test_asset_version_has_committed_ref_columns(self):
        """The ORM model exposes both columns, even with no rows present."""
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            s.execute(select(AssetVersionRecord).limit(1)).scalar_one_or_none()
        self.assertTrue(hasattr(AssetVersionRecord, "committed_object_key"))
        self.assertTrue(hasattr(AssetVersionRecord, "committed_storage_version_id"))

    def test_committed_ref_nullable_for_legacy(self):
        """Legacy AssetVersions may have null committed refs."""
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            version = self._make_version(ctx, s)
            version_id = version.id
            s.flush()
            reloaded = s.get(AssetVersionRecord, version_id)
            self.assertIsNone(reloaded.committed_object_key)
            self.assertIsNone(reloaded.committed_storage_version_id)

    def test_committed_ref_populated_for_new_versions(self):
        """New AssetVersions carry committed refs after generation commit."""
        ctx = self._make_context()
        with tenant_session(ctx) as s:
            version = self._make_version(
                ctx,
                s,
                committed_object_key="committed/ws1/assets/2026/09/08/ulid123.png",
                committed_storage_version_id="sv-abc123",
            )
            version_id = version.id
            s.flush()
            reloaded = s.get(AssetVersionRecord, version_id)
            self.assertEqual(
                reloaded.committed_object_key,
                "committed/ws1/assets/2026/09/08/ulid123.png",
            )
            self.assertEqual(reloaded.committed_storage_version_id, "sv-abc123")


class TestLocalFileBlobStoreOpenCommitted(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = LocalFileBlobStore(
            root=self.root, public_base_url="http://localhost:9000", signing_secret=b"top-secret"
        )

    async def test_satisfies_blob_store_protocol_with_new_methods(self) -> None:
        self.assertIsInstance(self.store, BlobStore)
        self.assertTrue(hasattr(self.store, "open_committed"))
        self.assertTrue(hasattr(self.store, "commit_generated_artifact"))

    async def test_recommitting_identical_generated_bytes_preserves_storage_version(self):
        first_source, second_source = self.root / 'first.zip', self.root / 'second.zip'
        content = b'PK\x03\x04same immutable package'
        first_source.write_bytes(content)
        spec = GeneratedArtifactSpec('committed/ws1/exports/recovery.zip', 'application/zip', 'test-sha256')
        first = await self.store.commit_generated_artifact(first_source, spec)
        # A different temporary file has different metadata even for identical bytes.
        second_source.write_bytes(content)
        first_mtime = (self.root / spec.committed_key).stat().st_mtime_ns
        import os
        os.utime(second_source, ns=(first_mtime + 1000000, first_mtime + 1000000))
        second = await self.store.commit_generated_artifact(second_source, spec)
        self.assertEqual(first, second)
        self.assertEqual((self.root / spec.committed_key).read_bytes(), content)

    async def test_generated_key_collision_preserves_original_bytes_and_reference(self):
        from app.platform.blob_store import ObjectChangedError
        source = self.root / 'source.zip'
        source.write_bytes(b'original')
        spec = GeneratedArtifactSpec('committed/ws1/exports/collision.zip', 'application/zip', 'test-sha256')
        original = await self.store.commit_generated_artifact(source, spec)
        source.write_bytes(b'different payload')
        with self.assertRaises(ObjectChangedError):
            await self.store.commit_generated_artifact(source, spec)
        self.assertEqual((self.root / spec.committed_key).read_bytes(), b'original')
        self.assertEqual((await self.store.read_committed(CommittedBlobRef(
            original.committed_key, original.storage_version_id, original.size_bytes, '', original.content_type_verified), 100)), b'original')

    async def test_interrupted_generated_copy_never_exposes_partial_destination(self):
        from unittest.mock import patch
        source = self.root / 'source.zip'
        source.write_bytes(b'original payload')
        spec = GeneratedArtifactSpec('committed/ws1/exports/interrupted.zip', 'application/zip', 'test-sha256')

        def interrupted(original, output):
            output.write(b'partial')
            raise OSError('controlled: disk full during copy')

        with patch('app.adapters.blob_store_local.shutil.copyfileobj', side_effect=interrupted):
            with self.assertRaises(OSError):
                await self.store.commit_generated_artifact(source, spec)
        dest = self.root / spec.committed_key
        self.assertFalse(dest.exists())
        self.assertEqual(list(dest.parent.glob('.generated-*')), [])
        await self.store.commit_generated_artifact(source, spec)
        self.assertEqual(dest.read_bytes(), b'original payload')

    async def test_concurrent_generated_commits_share_one_immutable_version(self):
        import asyncio
        source = self.root / 'source.zip'
        source.write_bytes(b'one immutable payload')
        spec = GeneratedArtifactSpec('committed/ws1/exports/concurrent.zip', 'application/zip', 'test-sha256')
        refs = await asyncio.gather(*(self.store.commit_generated_artifact(source, spec) for _ in range(4)))
        self.assertTrue(all(ref == refs[0] for ref in refs))

    async def test_open_committed_returns_bytes(self) -> None:
        """open_committed yields the exact committed blob bytes."""
        key = "committed/ws1/assets/2026/09/08/ulid123.png"
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test-image-bytes")
        ref = CommittedBlobRef(
            committed_key=key,
            storage_version_id="sv-abc123",
            size_bytes=17,
            checksum_sha256_b64="",
            content_type_verified="image/png",
        )
        chunks = []
        async for chunk in self.store.open_committed(ref):
            chunks.append(chunk)
        self.assertEqual(b"".join(chunks), b"test-image-bytes")

    async def test_open_committed_streams_large_blob_in_chunks(self) -> None:
        """Bytes beyond one internal chunk still round-trip exactly."""
        key = "committed/ws1/assets/2026/09/08/big.bin"
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        data = b"x" * (65536 * 3 + 17)
        path.write_bytes(data)
        ref = CommittedBlobRef(
            committed_key=key,
            storage_version_id="sv-big",
            size_bytes=len(data),
            checksum_sha256_b64="",
            content_type_verified="application/octet-stream",
        )
        chunks = []
        async for chunk in self.store.open_committed(ref):
            chunks.append(chunk)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(b"".join(chunks), data)

    async def test_open_committed_raises_for_missing_blob(self) -> None:
        """open_committed raises BlobStoreError for a non-existent committed ref."""
        fake_ref = CommittedBlobRef(
            committed_key="committed/ws1/assets/2026/01/01/missing.png",
            storage_version_id="sv-missing",
            size_bytes=100,
            checksum_sha256_b64="",
            content_type_verified="image/png",
        )
        with self.assertRaises(BlobStoreError):
            async for _ in self.store.open_committed(fake_ref):
                pass

    async def test_commit_generated_artifact(self) -> None:
        """commit_generated_artifact commits server-owned bytes and returns CommittedBlobRef."""
        source = Path(self._tmp.name) / "source" / "export.zip"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"PK\x03\x04" + b"\x00" * 100)

        spec = GeneratedArtifactSpec(
            committed_key="committed/ws1/exports/2026/09/08/pkg1.zip",
            content_type="application/zip",
            expected_sha256_b64="test-sha256",
        )

        ref = await self.store.commit_generated_artifact(source, spec)
        self.assertIsInstance(ref, CommittedBlobRef)
        self.assertEqual(ref.committed_key, spec.committed_key)
        self.assertEqual(ref.content_type_verified, "application/zip")
        self.assertEqual(ref.checksum_sha256_b64, "test-sha256")
        self.assertEqual(ref.size_bytes, 104)
        self.assertTrue(ref.storage_version_id)

        committed_path = self.root / spec.committed_key
        self.assertEqual(committed_path.read_bytes(), b"PK\x03\x04" + b"\x00" * 100)
        # Source is untouched (copy, not move) -- unlike commit_upload's
        # staging rename, the caller may still own/clean up its own source.
        self.assertTrue(source.exists())

    async def test_commit_generated_artifact_then_open_committed_round_trips(self) -> None:
        source = Path(self._tmp.name) / "source" / "export.zip"
        source.parent.mkdir(parents=True, exist_ok=True)
        data = b"PK\x03\x04" + b"payload-bytes" * 10
        source.write_bytes(data)
        spec = GeneratedArtifactSpec(
            committed_key="committed/ws1/exports/2026/09/08/pkg2.zip",
            content_type="application/zip",
            expected_sha256_b64="test-sha256",
        )
        ref = await self.store.commit_generated_artifact(source, spec)
        chunks = []
        async for chunk in self.store.open_committed(ref):
            chunks.append(chunk)
        self.assertEqual(b"".join(chunks), data)


if __name__ == "__main__":
    unittest.main()
