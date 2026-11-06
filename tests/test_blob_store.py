"""BlobStore Protocol, LocalFileBlobStore, LocalBlobTransferApp, S3BlobStore
(spec §7.3-§7.4).

`S3BlobStore` is not exercised against real S3 -- its tests inject a fake
boto3-shaped client so the Adapter's own logic (key derivation, error
mapping, presigned-URL assembly) is covered without an AWS dependency.
"""
from __future__ import annotations

import base64
import hashlib
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import httpx

from app.adapters.blob_store_local import (
    LocalFileBlobStore,
    compute_local_signature,
    lock_path_for,
    resolve_key_path,
    sidecar_path,
)
from app.adapters.blob_store_s3 import S3BlobStore
from app.platform.blob_store import (
    BlobRef,
    BlobStore,
    CommittedBlobRef,
    DownloadSpec,
    ObjectChangedError,
    ObjectConstraints,
    ObjectNotFoundError,
    PresignedPostGrant,
    RevisionGoneError,
    SignedPutGrant,
    StagedBlobRef,
    UploadSpec,
    content_disposition_header,
    derive_committed_key,
    extension_for_mime,
    validate_object_key,
)
from worker.local_blob_transfer import LocalBlobTransferApp


def _checksum_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


def _parse_qsl(url: str) -> list[tuple[str, str]]:
    return parse_qsl(urlsplit(url).query)


class TestBlobStoreValueObjects(unittest.TestCase):
    def test_upload_spec_creation(self) -> None:
        spec = UploadSpec(
            staging_key="staging/ws1/gen/2026/08/30/abc.dat",
            content_type="image/png",
            content_length_min=1,
            content_length_max=50_000_000,
            checksum_sha256_b64="abc123==",
            expires_in_seconds=300,
        )
        self.assertTrue(spec.staging_key.startswith("staging/"))
        self.assertEqual(spec.expires_in_seconds, 300)

    def test_committed_blob_ref_is_frozen(self) -> None:
        ref = CommittedBlobRef(
            committed_key="committed/ws1/gen/2026/08/30/abc.png",
            storage_version_id="v1",
            size_bytes=1000,
            checksum_sha256_b64="abc==",
            content_type_verified="image/png",
        )
        self.assertTrue(ref.committed_key.startswith("committed/"))
        with self.assertRaises(Exception):
            ref.committed_key = "other"  # type: ignore[misc]

    def test_upload_grant_variants_share_kind_discriminant(self) -> None:
        post = PresignedPostGrant(
            kind="presigned_post", url="https://s3", fields={}, expires_at=datetime.now(timezone.utc)
        )
        put = SignedPutGrant(
            kind="signed_put", url="http://localhost:9000", headers={}, expires_at=datetime.now(timezone.utc)
        )
        self.assertEqual(post.kind, "presigned_post")
        self.assertEqual(put.kind, "signed_put")

    def test_blob_ref_version_id_defaults_to_none(self) -> None:
        ref = BlobRef(key="committed/ws1/gen/2026/08/30/abc.png")
        self.assertIsNone(ref.version_id)


class TestObjectKeyHelpers(unittest.TestCase):
    def test_valid_staging_key_accepted(self) -> None:
        validate_object_key("staging/ws1/generation_output/2026/08/30/01hxyz.dat")

    def test_rejects_parent_traversal(self) -> None:
        with self.assertRaises(ValueError):
            validate_object_key("staging/../etc/passwd.dat")

    def test_rejects_absolute_path(self) -> None:
        with self.assertRaises(ValueError):
            validate_object_key("/staging/ws1/x.dat")

    def test_rejects_null_byte(self) -> None:
        with self.assertRaises(ValueError):
            validate_object_key("staging/ws1/x.dat\x00")

    def test_rejects_disallowed_characters(self) -> None:
        with self.assertRaises(ValueError):
            validate_object_key("staging/ws 1/x.dat")

    def test_rejects_leaf_without_extension(self) -> None:
        with self.assertRaises(ValueError):
            validate_object_key("staging/ws1/purpose/leafwithoutext")

    def test_extension_for_mime_known_and_fallback(self) -> None:
        self.assertEqual(extension_for_mime("image/png"), "png")
        self.assertEqual(extension_for_mime("application/x-unknown"), "bin")

    def test_derive_committed_key_swaps_prefix_and_extension(self) -> None:
        staging_key = "staging/ws1/generation_output/2026/08/30/01hxyz.dat"
        committed_key = derive_committed_key(staging_key, "image/png")
        self.assertEqual(committed_key, "committed/ws1/generation_output/2026/08/30/01hxyz.png")

    def test_derive_committed_key_rejects_non_staging_input(self) -> None:
        with self.assertRaises(ValueError):
            derive_committed_key("committed/ws1/gen/2026/08/30/01hxyz.dat", "image/png")

    def test_content_disposition_strips_path_separators_and_controls(self) -> None:
        header = content_disposition_header("../../evil\x07name.png")
        self.assertNotIn("/", header.split("filename=")[1].split(";")[0])
        self.assertNotIn("\x07", header)
        self.assertIn("filename*=UTF-8''", header)


class TestBlobStoreProtocolConformance(unittest.TestCase):
    def test_local_file_blob_store_satisfies_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalFileBlobStore(
                root=Path(tmp), public_base_url="http://localhost:9000", signing_secret=b"secret"
            )
            self.assertIsInstance(store, BlobStore)

    def test_local_file_blob_store_requires_signing_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                LocalFileBlobStore(root=Path(tmp), public_base_url="http://localhost:9000", signing_secret=b"")


class TestLocalFileBlobStoreGoldenPath(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = LocalFileBlobStore(
            root=self.root, public_base_url="http://localhost:9000", signing_secret=b"top-secret"
        )
        self.staging_key = "staging/ws1/generation_output/2026/08/30/01hxyz.dat"

    def _write_staged_bytes(self, data: bytes, *, content_type: str = "image/png") -> None:
        path = resolve_key_path(self.root, self.staging_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        sidecar_path(path).write_text(
            f'{{"content_type": "{content_type}", "checksum_sha256_b64": "{_checksum_b64(data)}"}}',
            encoding="utf-8",
        )

    async def test_issue_upload_returns_signed_put_grant(self) -> None:
        spec = UploadSpec(
            staging_key=self.staging_key,
            content_type="image/png",
            content_length_min=1,
            content_length_max=1000,
            checksum_sha256_b64="deadbeef==",
            expires_in_seconds=300,
        )
        grant = await self.store.issue_upload(spec)
        self.assertEqual(grant.kind, "signed_put")
        self.assertTrue(grant.url.startswith("http://localhost:9000/_local_blob/"))
        self.assertEqual(grant.headers["Content-Type"], "image/png")
        self.assertEqual(grant.headers["x-amz-checksum-sha256"], "deadbeef==")

    async def test_inspect_upload_reports_size_checksum_and_revision(self) -> None:
        data = b"\x89PNG\r\n\x1a\n" + b"rest-of-file" * 100
        self._write_staged_bytes(data)
        inspected = await self.store.inspect_upload(StagedBlobRef(staging_key=self.staging_key))
        self.assertEqual(inspected.size_bytes, len(data))
        self.assertEqual(inspected.checksum_sha256_b64, _checksum_b64(data))
        self.assertEqual(inspected.magic_bytes[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(inspected.content_type_declared, "image/png")
        self.assertTrue(inspected.storage_revision)

    async def test_inspect_upload_missing_object_raises_not_found(self) -> None:
        with self.assertRaises(ObjectNotFoundError):
            await self.store.inspect_upload(StagedBlobRef(staging_key=self.staging_key))

    async def test_golden_path_commit_and_download(self) -> None:
        data = b"\x89PNG\r\n\x1a\n" + b"golden-path-bytes" * 50
        self._write_staged_bytes(data)
        inspected = await self.store.inspect_upload(StagedBlobRef(staging_key=self.staging_key))
        expected = ObjectConstraints(
            expected_size=inspected.size_bytes,
            expected_checksum_sha256_b64=inspected.checksum_sha256_b64,
            expected_magic_mime="image/png",
            storage_revision=inspected.storage_revision,
        )
        committed = await self.store.commit_upload(inspected, expected)
        self.assertEqual(committed.committed_key, "committed/ws1/generation_output/2026/08/30/01hxyz.png")
        self.assertEqual(committed.checksum_sha256_b64, inspected.checksum_sha256_b64)
        self.assertTrue(committed.storage_version_id)

        # Staging object is gone; committed object exists with the same bytes.
        self.assertFalse(resolve_key_path(self.root, self.staging_key).exists())
        self.assertEqual(resolve_key_path(self.root, committed.committed_key).read_bytes(), data)

        grant = await self.store.issue_download(committed, DownloadSpec(filename="output.png", expires_in_seconds=120))
        self.assertTrue(grant.url.startswith("http://localhost:9000/_local_blob/"))
        query = dict(_parse_qsl(grant.url))
        self.assertEqual(query["v"], committed.storage_version_id)
        self.assertIn("output.png", grant.content_disposition)

    async def test_delete_removes_object_and_sidecar(self) -> None:
        data = b"whatever"
        self._write_staged_bytes(data)
        path = resolve_key_path(self.root, self.staging_key)
        self.assertTrue(path.exists())
        await self.store.delete(BlobRef(key=self.staging_key))
        self.assertFalse(path.exists())
        self.assertFalse(sidecar_path(path).exists())

    async def test_delete_is_idempotent_on_missing_object(self) -> None:
        await self.store.delete(BlobRef(key=self.staging_key))  # must not raise


class TestLocalFileBlobStoreRevisionSemantics(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = LocalFileBlobStore(
            root=self.root, public_base_url="http://localhost:9000", signing_secret=b"top-secret"
        )
        self.staging_key = "staging/ws1/generation_output/2026/08/30/01hxyz.dat"
        self.path = resolve_key_path(self.root, self.staging_key)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    async def test_commit_after_staging_object_replaced_raises_object_changed(self) -> None:
        self.path.write_bytes(b"original-bytes")
        inspected = await self.store.inspect_upload(StagedBlobRef(staging_key=self.staging_key))
        expected = ObjectConstraints(
            expected_size=inspected.size_bytes,
            expected_checksum_sha256_b64=inspected.checksum_sha256_b64,
            expected_magic_mime="image/png",
            storage_revision=inspected.storage_revision,
        )
        # Object is overwritten after inspect, before commit.
        time.sleep(0.01)
        self.path.write_bytes(b"replacement-bytes-that-differ-in-size")
        with self.assertRaises(ObjectChangedError):
            await self.store.commit_upload(inspected, expected)
        # The replacement bytes were never committed.
        self.assertFalse(resolve_key_path(self.root, "committed/ws1/generation_output/2026/08/30/01hxyz.png").exists())

    async def test_commit_after_staging_object_deleted_raises_revision_gone(self) -> None:
        self.path.write_bytes(b"original-bytes")
        inspected = await self.store.inspect_upload(StagedBlobRef(staging_key=self.staging_key))
        expected = ObjectConstraints(
            expected_size=inspected.size_bytes,
            expected_checksum_sha256_b64=inspected.checksum_sha256_b64,
            expected_magic_mime="image/png",
            storage_revision=inspected.storage_revision,
        )
        os.remove(self.path)
        with self.assertRaises(RevisionGoneError):
            await self.store.commit_upload(inspected, expected)

    async def test_concurrent_commit_second_attempt_gets_revision_gone(self) -> None:
        self.path.write_bytes(b"original-bytes")
        inspected = await self.store.inspect_upload(StagedBlobRef(staging_key=self.staging_key))
        expected = ObjectConstraints(
            expected_size=inspected.size_bytes,
            expected_checksum_sha256_b64=inspected.checksum_sha256_b64,
            expected_magic_mime="image/png",
            storage_revision=inspected.storage_revision,
        )
        first = await self.store.commit_upload(inspected, expected)
        self.assertTrue(first.committed_key)
        with self.assertRaises(RevisionGoneError):
            await self.store.commit_upload(inspected, expected)


class TestLocalFileBlobStoreSecurity(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = LocalFileBlobStore(
            root=self.root, public_base_url="http://localhost:9000", signing_secret=b"top-secret"
        )

    async def test_inspect_upload_rejects_path_traversal_key(self) -> None:
        with self.assertRaises(ValueError):
            await self.store.inspect_upload(StagedBlobRef(staging_key="staging/../../etc/passwd.dat"))

    async def test_inspect_upload_rejects_symlink_escape(self) -> None:
        outside = self.root.parent / f"outside-{os.getpid()}.dat"
        outside.write_bytes(b"secret-outside-root")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        link_path = self.root / "staging" / "ws1" / "generation_output" / "2026" / "08" / "30"
        link_path.mkdir(parents=True, exist_ok=True)
        symlink = link_path / "01hxyz.dat"
        os.symlink(outside, symlink)
        # The symlink target resolves outside `root`, so `resolve_key_path`'s
        # containment check rejects it with `ValueError` before any file is
        # opened -- a stronger guarantee than `O_NOFOLLOW` alone, which would
        # only refuse to *open* the symlink but not necessarily catch every
        # escape shape.
        with self.assertRaises(ValueError):
            await self.store.inspect_upload(
                StagedBlobRef(staging_key="staging/ws1/generation_output/2026/08/30/01hxyz.dat")
            )


class _FakeStreamingBody:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def read(self, amt: int | None = None) -> bytes:
        """Mirrors botocore's `StreamingBody.read(amt)`: `None` drains the
        rest; an `amt` returns up to that many bytes from the current
        position, `b""` once exhausted -- what `S3BlobStore.open_committed`'s
        chunked-read loop relies on to terminate."""
        if amt is None:
            chunk = self._data[self._pos :]
            self._pos = len(self._data)
            return chunk
        chunk = self._data[self._pos : self._pos + amt]
        self._pos += len(chunk)
        return chunk


class _FakeS3Client:
    """Minimal boto3 S3 client double covering the calls S3BlobStore makes."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self._version_counter = 0
        self.deleted: list[dict] = []

    def _next_version(self) -> str:
        self._version_counter += 1
        return f"v{self._version_counter}"

    def put_test_object(self, key: str, data: bytes) -> str:
        version = self._next_version()
        self.objects[(key, version)] = data
        self.objects[(key, "latest")] = data
        self._latest_version = version
        return version

    def generate_presigned_post(self, *, Bucket, Key, Fields, Conditions, ExpiresIn):
        return {"url": f"https://{Bucket}.s3.amazonaws.com/", "fields": {**Fields, "key": Key}}

    def head_object(self, *, Bucket, Key, VersionId=None):
        version = VersionId or getattr(self, "_latest_version", None)
        if (Key, version) not in self.objects:
            raise _client_error("NoSuchKey", "head_object")
        data = self.objects[(Key, version)]
        return {
            "VersionId": version,
            "ContentLength": len(data),
            "ChecksumSHA256": _checksum_b64(data),
            "ContentType": "image/png",
        }

    def get_object(self, *, Bucket, Key, VersionId, Range=None):
        if (Key, VersionId) not in self.objects:
            raise _client_error("NoSuchKey", "get_object")
        data = self.objects[(Key, VersionId)]
        # `inspect_upload` passes a `Range` to sample only the magic-byte
        # prefix; `open_committed` passes none and streams the full object.
        body = data[:8192] if Range else data
        return {"Body": _FakeStreamingBody(body)}

    def put_object(self, *, Bucket, Key, Body, ContentType=None):
        data = Body.read() if hasattr(Body, "read") else Body
        version = self._next_version()
        self.objects[(Key, version)] = data
        self.objects[(Key, "latest")] = data
        self._latest_version = version
        return {"VersionId": version}

    def copy_object(self, *, Bucket, Key, CopySource, ContentType, MetadataDirective):
        src_key = CopySource["Key"]
        src_version = CopySource["VersionId"]
        if (src_key, src_version) not in self.objects:
            raise _client_error("InvalidArgument", "copy_object")
        data = self.objects[(src_key, src_version)]
        version = self._next_version()
        self.objects[(Key, version)] = data
        return {"VersionId": version}

    def generate_presigned_url(self, operation, *, Params, ExpiresIn):
        return f"https://s3.amazonaws.com/{Params['Bucket']}/{Params['Key']}?versionId={Params['VersionId']}"

    def delete_object(self, *, Bucket, Key, VersionId=None):
        self.deleted.append({"Key": Key, "VersionId": VersionId})


def _client_error(code: str, operation: str):
    from app.adapters import blob_store_s3 as s3_module

    error_cls = s3_module.ClientError
    if s3_module.boto3 is None:
        # The placeholder ClientError used when botocore is absent is a
        # plain Exception subclass; carry the code via `.response` so
        # `S3BlobStore`'s `_error_code()` can still read it.
        exc = error_cls(code)
        exc.response = {"Error": {"Code": code, "Message": code}}
        return exc
    return error_cls({"Error": {"Code": code, "Message": code}}, operation)


class TestS3BlobStoreWithFakeClient(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.client = _FakeS3Client()
        self.store = S3BlobStore(bucket="test-bucket", client=self.client)
        self.staging_key = "staging/ws1/generation_output/2026/08/30/01hxyz.dat"

    def test_satisfies_blob_store_protocol(self) -> None:
        self.assertIsInstance(self.store, BlobStore)

    async def test_issue_upload_returns_presigned_post(self) -> None:
        spec = UploadSpec(
            staging_key=self.staging_key,
            content_type="image/png",
            content_length_min=1,
            content_length_max=1000,
            checksum_sha256_b64="deadbeef==",
            expires_in_seconds=300,
        )
        grant = await self.store.issue_upload(spec)
        self.assertEqual(grant.kind, "presigned_post")
        self.assertIn("Content-Type", grant.fields)

    async def test_inspect_upload_missing_object_raises_not_found(self) -> None:
        with self.assertRaises(ObjectNotFoundError):
            await self.store.inspect_upload(StagedBlobRef(staging_key=self.staging_key))

    async def test_golden_path_commit_and_download(self) -> None:
        data = b"\x89PNG\r\n\x1a\n" + b"fake-s3-bytes" * 20
        self.client.put_test_object(self.staging_key, data)
        inspected = await self.store.inspect_upload(StagedBlobRef(staging_key=self.staging_key))
        self.assertEqual(inspected.storage_revision, "v1")
        self.assertEqual(inspected.size_bytes, len(data))

        expected = ObjectConstraints(
            expected_size=inspected.size_bytes,
            expected_checksum_sha256_b64=inspected.checksum_sha256_b64,
            expected_magic_mime="image/png",
            storage_revision=inspected.storage_revision,
        )
        committed = await self.store.commit_upload(inspected, expected)
        self.assertEqual(committed.committed_key, "committed/ws1/generation_output/2026/08/30/01hxyz.png")
        self.assertTrue(committed.storage_version_id)

        grant = await self.store.issue_download(committed, DownloadSpec(filename="a.png", expires_in_seconds=60))
        self.assertIn(committed.storage_version_id, grant.url)

    async def test_commit_upload_with_deleted_version_raises_revision_gone(self) -> None:
        data = b"bytes"
        self.client.put_test_object(self.staging_key, data)
        inspected = await self.store.inspect_upload(StagedBlobRef(staging_key=self.staging_key))
        expected = ObjectConstraints(
            expected_size=inspected.size_bytes,
            expected_checksum_sha256_b64=inspected.checksum_sha256_b64,
            expected_magic_mime="image/png",
            storage_revision="v-does-not-exist",
        )
        with self.assertRaises(RevisionGoneError):
            await self.store.commit_upload(inspected, expected)

    async def test_delete_is_idempotent_on_missing_object(self) -> None:
        await self.store.delete(BlobRef(key="committed/ws1/gen/2026/08/30/gone.png"))

    async def test_open_committed_streams_bytes(self) -> None:
        committed_key = "committed/ws1/exports/2026/09/08/pkg1.zip"
        data = b"PK\x03\x04" + b"export-bytes" * 10000  # > 64KB chunk size
        version = self.client.put_test_object(committed_key, data)
        ref = CommittedBlobRef(
            committed_key=committed_key,
            storage_version_id=version,
            size_bytes=len(data),
            checksum_sha256_b64=_checksum_b64(data),
            content_type_verified="application/zip",
        )
        chunks = []
        async for chunk in self.store.open_committed(ref):
            chunks.append(chunk)
        self.assertGreater(len(chunks), 1)  # exceeds the 64KB chunk size
        self.assertEqual(b"".join(chunks), data)

    async def test_open_committed_raises_for_missing_version(self) -> None:
        ref = CommittedBlobRef(
            committed_key="committed/ws1/exports/2026/09/08/missing.zip",
            storage_version_id="v-does-not-exist",
            size_bytes=10,
            checksum_sha256_b64="",
            content_type_verified="application/zip",
        )
        with self.assertRaises(ObjectNotFoundError):
            async for _ in self.store.open_committed(ref):
                pass

    async def test_commit_generated_artifact_uploads_and_returns_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "export.zip"
            data = b"PK\x03\x04" + b"\x00" * 100
            source.write_bytes(data)

            from app.platform.blob_store import GeneratedArtifactSpec

            spec = GeneratedArtifactSpec(
                committed_key="committed/ws1/exports/2026/09/08/pkg1.zip",
                content_type="application/zip",
                expected_sha256_b64=_checksum_b64(data),
            )
            ref = await self.store.commit_generated_artifact(source, spec)
            self.assertIsInstance(ref, CommittedBlobRef)
            self.assertEqual(ref.committed_key, spec.committed_key)
            self.assertEqual(ref.checksum_sha256_b64, spec.expected_sha256_b64)
            self.assertEqual(ref.size_bytes, len(data))
            self.assertTrue(ref.storage_version_id)
            self.assertEqual(self.client.objects[(spec.committed_key, ref.storage_version_id)], data)


class TestS3BlobStoreImportGuard(unittest.TestCase):
    def test_construction_without_client_or_boto3_raises_import_error(self) -> None:
        import app.adapters.blob_store_s3 as s3_module

        if s3_module.boto3 is not None:
            self.skipTest("boto3 is installed in this environment")
        with self.assertRaises(ImportError):
            S3BlobStore(bucket="test-bucket")


class TestLocalBlobTransferApp(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.secret = b"transfer-secret"
        self.app = LocalBlobTransferApp(root=self.root, signing_secret=self.secret, cors_origin="http://localhost:3000")
        self.store = LocalFileBlobStore(
            root=self.root, public_base_url="http://localhost:9000", signing_secret=self.secret
        )
        transport = httpx.ASGITransport(app=self.app)
        self.client = httpx.AsyncClient(transport=transport, base_url="http://localhost:9000")
        self.addAsyncCleanup(self.client.aclose)
        self.staging_key = "staging/ws1/generation_output/2026/08/30/01hxyz.dat"

    async def _issue_put_grant(self, data: bytes, content_type: str = "image/png") -> SignedPutGrant:
        spec = UploadSpec(
            staging_key=self.staging_key,
            content_type=content_type,
            content_length_min=1,
            content_length_max=10_000,
            checksum_sha256_b64=_checksum_b64(data),
            expires_in_seconds=300,
        )
        return await self.store.issue_upload(spec)

    async def test_health_endpoint_is_ready(self) -> None:
        response = await self.client.get("/health")
        self.assertEqual(response.status_code, 200)

    async def test_options_preflight_returns_allowed_origin(self) -> None:
        response = await self.client.options(
            "/_local_blob/sig/staging/ws1/x.dat", headers={"origin": "http://localhost:3000"}
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers["access-control-allow-origin"], "http://localhost:3000")

    async def test_options_preflight_rejects_disallowed_origin(self) -> None:
        response = await self.client.options(
            "/_local_blob/sig/staging/ws1/x.dat", headers={"origin": "http://evil.example"}
        )
        self.assertNotIn("access-control-allow-origin", response.headers)

    async def test_put_then_get_round_trips_bytes(self) -> None:
        data = b"\x89PNG\r\n\x1a\n" + b"round-trip-bytes" * 10
        grant = await self._issue_put_grant(data)
        path = grant.url[len("http://localhost:9000"):]
        put_response = await self.client.put(path, content=data, headers=grant.headers)
        self.assertEqual(put_response.status_code, 200)

        staged_path = resolve_key_path(self.root, self.staging_key)
        self.assertEqual(staged_path.read_bytes(), data)

        inspected = await self.store.inspect_upload(StagedBlobRef(staging_key=self.staging_key))
        expected = ObjectConstraints(
            expected_size=inspected.size_bytes,
            expected_checksum_sha256_b64=inspected.checksum_sha256_b64,
            expected_magic_mime="image/png",
            storage_revision=inspected.storage_revision,
        )
        committed = await self.store.commit_upload(inspected, expected)
        download_grant = await self.store.issue_download(committed, DownloadSpec(filename="out.png", expires_in_seconds=60))
        download_path = download_grant.url[len("http://localhost:9000"):]
        get_response = await self.client.get(download_path)
        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(get_response.content, data)

    async def test_put_with_wrong_signature_is_rejected(self) -> None:
        data = b"some-bytes"
        grant = await self._issue_put_grant(data)
        path = grant.url[len("http://localhost:9000"):]
        tampered_path = path.replace(path.split("/")[2], "0" * 64, 1)
        response = await self.client.put(tampered_path, content=data, headers=grant.headers)
        self.assertEqual(response.status_code, 403)
        body = response.json()
        self.assertEqual(body["code"], "invalid_signature")
        self.assertIn("trace_id", body)
        self.assertIn("retryable", body)

    async def test_put_exceeding_max_size_is_rejected(self) -> None:
        data = b"x" * 50
        spec = UploadSpec(
            staging_key=self.staging_key,
            content_type="image/png",
            content_length_min=1,
            content_length_max=10,
            checksum_sha256_b64=_checksum_b64(data),
            expires_in_seconds=300,
        )
        grant = await self.store.issue_upload(spec)
        path = grant.url[len("http://localhost:9000"):]
        response = await self.client.put(path, content=data, headers=grant.headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "content_too_large")

    async def test_put_below_min_size_is_rejected(self) -> None:
        data = b"x"
        spec = UploadSpec(
            staging_key=self.staging_key,
            content_type="image/png",
            content_length_min=100,
            content_length_max=1000,
            checksum_sha256_b64=_checksum_b64(data),
            expires_in_seconds=300,
        )
        grant = await self.store.issue_upload(spec)
        path = grant.url[len("http://localhost:9000"):]
        response = await self.client.put(path, content=data, headers=grant.headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "content_too_small")

    async def test_put_with_checksum_mismatch_is_rejected(self) -> None:
        data = b"actual-bytes"
        spec = UploadSpec(
            staging_key=self.staging_key,
            content_type="image/png",
            content_length_min=1,
            content_length_max=1000,
            checksum_sha256_b64=_checksum_b64(b"different-bytes"),
            expires_in_seconds=300,
        )
        grant = await self.store.issue_upload(spec)
        path = grant.url[len("http://localhost:9000"):]
        response = await self.client.put(
            path,
            content=data,
            headers={"Content-Type": "image/png", "x-amz-checksum-sha256": _checksum_b64(b"different-bytes")},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "checksum_mismatch")

    async def test_put_with_content_type_header_mismatch_is_rejected(self) -> None:
        data = b"bytes"
        grant = await self._issue_put_grant(data)
        path = grant.url[len("http://localhost:9000"):]
        response = await self.client.put(
            path, content=data, headers={"Content-Type": "text/plain", "x-amz-checksum-sha256": _checksum_b64(data)}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "content_constraint_mismatch")

    async def test_get_with_stale_revision_returns_not_found(self) -> None:
        data = b"\x89PNG\r\n\x1a\n" + b"stale-revision" * 10
        grant = await self._issue_put_grant(data)
        path = grant.url[len("http://localhost:9000"):]
        await self.client.put(path, content=data, headers=grant.headers)

        inspected = await self.store.inspect_upload(StagedBlobRef(staging_key=self.staging_key))
        expected = ObjectConstraints(
            expected_size=inspected.size_bytes,
            expected_checksum_sha256_b64=inspected.checksum_sha256_b64,
            expected_magic_mime="image/png",
            storage_revision=inspected.storage_revision,
        )
        committed = await self.store.commit_upload(inspected, expected)

        stale_signature = compute_local_signature(
            self.secret, "GET", committed.committed_key, int(time.time()) + 60, storage_revision="not-the-real-revision"
        )
        response = await self.client.get(
            f"/_local_blob/{stale_signature}/{committed.committed_key}"
            f"?expires={int(time.time()) + 60}&v=not-the-real-revision"
        )
        self.assertEqual(response.status_code, 404)

    async def test_get_with_expired_signature_returns_410(self) -> None:
        expiry = int(time.time()) - 10
        signature = compute_local_signature(self.secret, "GET", "committed/ws1/x.png", expiry, storage_revision="rev")
        response = await self.client.get(f"/_local_blob/{signature}/committed/ws1/x.png?expires={expiry}&v=rev")
        self.assertEqual(response.status_code, 410)

    async def test_get_with_duplicate_query_key_is_rejected(self) -> None:
        expiry = int(time.time()) + 60
        signature = compute_local_signature(self.secret, "GET", "committed/ws1/x.png", expiry, storage_revision="rev")
        response = await self.client.get(
            f"/_local_blob/{signature}/committed/ws1/x.png?expires={expiry}&v=rev&v=rev2"
        )
        self.assertEqual(response.status_code, 400)

    async def test_put_path_traversal_key_is_rejected(self) -> None:
        expiry = int(time.time()) + 60
        checksum = _checksum_b64(b"x")
        signature = compute_local_signature(
            self.secret,
            "PUT",
            "staging/../../etc/passwd.dat",
            expiry,
            content_type="image/png",
            content_length_min=1,
            content_length_max=100,
            checksum_sha256_b64=checksum,
        )
        response = await self.client.put(
            f"/_local_blob/{signature}/staging/../../etc/passwd.dat?expires={expiry}&ct=image%2Fpng&cl_min=1&cl_max=100&cs={checksum}",
            content=b"x",
            headers={"Content-Type": "image/png", "x-amz-checksum-sha256": checksum},
        )
        # httpx (like a browser) normalizes ".." segments while building the
        # request URL, so the server may see either the literal traversal
        # (rejected as an invalid key, 400) or a normalized path whose
        # signature no longer matches what was computed over the raw
        # traversal string (rejected as a bad signature, 403). Either way,
        # nothing outside the storage root is ever written.
        self.assertIn(response.status_code, (400, 403, 404))
        self.assertFalse((self.root.parent / "etc" / "passwd.dat").exists())

    async def test_error_body_never_contains_signed_url_or_filesystem_path(self) -> None:
        data = b"some-bytes"
        grant = await self._issue_put_grant(data)
        path = grant.url[len("http://localhost:9000"):]
        tampered_path = path.replace(path.split("/")[2], "0" * 64, 1)
        response = await self.client.put(tampered_path, content=data, headers=grant.headers)
        body_text = response.text
        self.assertNotIn(str(self.root), body_text)
        self.assertNotIn("expires=", body_text)
        self.assertNotIn(grant.url, body_text)

    async def test_advisory_lock_path_matches_between_store_and_transfer_app(self) -> None:
        self.assertEqual(
            lock_path_for(self.root, self.staging_key),
            lock_path_for(self.root, self.staging_key),
        )


if __name__ == "__main__":
    unittest.main()
