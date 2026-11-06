"""Railway Bucket BlobStore tests with an unversioned S3-shaped fake."""
from __future__ import annotations

import base64
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config
from app.adapters.blob_store_railway_bucket import RailwayBucketBlobStore
from app.platform.blob_store import (
    BlobRef,
    BlobStore,
    BlobStoreError,
    GeneratedArtifactSpec,
    ObjectChangedError,
    ObjectConstraints,
    RevisionGoneError,
    StagedBlobRef,
    UploadSpec,
)
from app.platform.blob_store_factory import build_blob_store


def _checksum(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._position = 0

    def read(self, amount: int | None = None) -> bytes:
        if amount is None:
            amount = len(self._data) - self._position
        chunk = self._data[self._position : self._position + amount]
        self._position += len(chunk)
        return chunk

    def close(self) -> None:
        return None


def _client_error(code: str, operation: str):
    from app.adapters import blob_store_railway_bucket as module

    if module.boto3 is None:
        exc = module.ClientError(code)
        exc.response = {"Error": {"Code": code, "Message": code}}
        return exc
    return module.ClientError(
        {"Error": {"Code": code, "Message": code}}, operation
    )


class _FakeRailwayClient:
    """No VersionId, lifecycle, checksum response, or conditional copy."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self.metadata: dict[str, dict[str, str]] = {}
        self.checksums: dict[str, str] = {}
        self.deleted: list[str] = []
        self.presigned_params: list[dict] = []

    def put_test_object(
        self, key: str, data: bytes, *, content_type: str = "image/png"
    ) -> None:
        self.objects[key] = data
        self.content_types[key] = content_type

    def generate_presigned_url(self, operation, *, Params, ExpiresIn):
        self.presigned_params.append(
            {"operation": operation, "Params": Params, "ExpiresIn": ExpiresIn}
        )
        return f"https://bucket.storage.invalid/{Params['Key']}?signature=redacted"

    def head_object(self, *, Bucket, Key, ChecksumMode=None):
        if Key not in self.objects:
            raise _client_error("NoSuchKey", "HeadObject")
        data = self.objects[Key]
        result = {
            "ContentLength": len(data),
            "ContentType": self.content_types.get(
                Key, "application/octet-stream"
            ),
            "ETag": f'"{hashlib.md5(data).hexdigest()}"',  # noqa: S324 - fake ETag only
            # Deliberately no VersionId or ChecksumSHA256.
        }
        if ChecksumMode == "ENABLED" and Key in self.checksums:
            result["ChecksumSHA256"] = self.checksums[Key]
        return result

    def get_object(self, *, Bucket, Key):
        if Key not in self.objects:
            raise _client_error("NoSuchKey", "GetObject")
        return {
            "Body": _Body(self.objects[Key]),
            "ContentLength": len(self.objects[Key]),
            "ContentType": self.content_types.get(
                Key, "application/octet-stream"
            ),
        }

    def put_object(
        self,
        *,
        Bucket,
        Key,
        Body,
        ContentType="application/octet-stream",
        Metadata=None,
        ChecksumSHA256=None,
    ):
        data = Body.read() if hasattr(Body, "read") else bytes(Body)
        if ChecksumSHA256 is not None and ChecksumSHA256 != _checksum(data):
            raise _client_error("BadDigest", "PutObject")
        self.objects[Key] = data
        self.content_types[Key] = ContentType
        self.metadata[Key] = dict(Metadata or {})
        if ChecksumSHA256 is not None:
            self.checksums[Key] = ChecksumSHA256
        return {"ETag": f'"{hashlib.md5(data).hexdigest()}"'}  # noqa: S324

    def copy_object(
        self,
        *,
        Bucket,
        Key,
        CopySource,
        CopySourceIfMatch,
        IfNoneMatch,
    ):
        source_key = CopySource["Key"]
        if source_key not in self.objects:
            raise _client_error("NoSuchKey", "CopyObject")
        current_etag = f'"{hashlib.md5(self.objects[source_key]).hexdigest()}"'  # noqa: S324
        if current_etag != CopySourceIfMatch:
            raise _client_error("PreconditionFailed", "CopyObject")
        if IfNoneMatch == "*" and Key in self.objects:
            raise _client_error("PreconditionFailed", "CopyObject")
        self.objects[Key] = self.objects[source_key]
        return {"CopyObjectResult": {"ETag": current_etag}}

    def put_bucket_cors(self, *, Bucket, CORSConfiguration):
        self.cors = {"CORSRules": list(CORSConfiguration["CORSRules"])}
        return {}

    def get_bucket_cors(self, *, Bucket):
        if not getattr(self, "cors", None):
            raise _client_error("NoSuchCORSConfiguration", "GetBucketCors")
        return self.cors

    def delete_object(self, *, Bucket, Key):
        self.deleted.append(Key)
        self.objects.pop(Key, None)
        self.content_types.pop(Key, None)
        self.metadata.pop(Key, None)
        self.checksums.pop(Key, None)
        return {}


class RailwayBucketBlobStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.client = _FakeRailwayClient()
        self.store = RailwayBucketBlobStore(
            bucket="marketai-test",
            endpoint_url="https://storage.invalid",
            client=self.client,
        )
        self.staging_key = (
            "staging/ws1/generation_output/2026/09/22/upload.dat"
        )

    def test_satisfies_protocol_without_versioning(self) -> None:
        self.assertIsInstance(self.store, BlobStore)

    async def test_issue_upload_uses_signed_put_and_declared_checksum_metadata(
        self,
    ) -> None:
        grant = await self.store.issue_upload(
            UploadSpec(
                staging_key=self.staging_key,
                content_type="image/png",
                content_length_min=1,
                content_length_max=1000,
                checksum_sha256_b64="expected==",
                expires_in_seconds=300,
            )
        )
        self.assertEqual(grant.kind, "signed_put")
        self.assertEqual(grant.headers["Content-Type"], "image/png")
        self.assertEqual(
            grant.headers["x-amz-meta-marketai-sha256"], "expected=="
        )
        self.assertEqual(
            self.client.presigned_params[0]["operation"], "put_object"
        )

    async def test_inspect_hashes_bytes_without_checksum_or_version_id(self) -> None:
        data = b"\x89PNG\r\n\x1a\n" + b"payload" * 30
        self.client.put_test_object(self.staging_key, data)
        inspected = await self.store.inspect_upload(
            StagedBlobRef(staging_key=self.staging_key)
        )
        self.assertEqual(inspected.size_bytes, len(data))
        self.assertEqual(inspected.checksum_sha256_b64, _checksum(data))
        self.assertTrue(inspected.storage_revision.startswith("sha256:"))
        self.assertEqual(inspected.magic_bytes[:8], b"\x89PNG\r\n\x1a\n")

    async def test_commit_replays_exact_inspected_bytes_without_copy_support(
        self,
    ) -> None:
        data = b"\x89PNG\r\n\x1a\n" + b"payload" * 30
        self.client.put_test_object(self.staging_key, data)
        inspected = await self.store.inspect_upload(
            StagedBlobRef(staging_key=self.staging_key)
        )
        committed = await self.store.commit_upload(
            inspected,
            ObjectConstraints(
                expected_size=inspected.size_bytes,
                expected_checksum_sha256_b64=inspected.checksum_sha256_b64,
                expected_magic_mime="image/png",
                storage_revision=inspected.storage_revision,
            ),
        )
        self.assertEqual(self.client.objects[committed.committed_key], data)
        self.assertTrue(committed.storage_version_id.startswith("sha256:"))
        # Cleanup is application-owned and runs as a durable Job.
        self.assertIn(self.staging_key, self.client.objects)

    async def test_overwrite_after_inspection_never_creates_committed_object(
        self,
    ) -> None:
        original = b"\x89PNG\r\n\x1a\n" + b"A" * 64
        replacement = b"\x89PNG\r\n\x1a\n" + b"B" * 64
        self.client.put_test_object(self.staging_key, original)
        inspected = await self.store.inspect_upload(
            StagedBlobRef(staging_key=self.staging_key)
        )
        self.client.put_test_object(self.staging_key, replacement)

        with self.assertRaises(ObjectChangedError):
            await self.store.commit_upload(
                inspected,
                ObjectConstraints(
                    expected_size=inspected.size_bytes,
                    expected_checksum_sha256_b64=inspected.checksum_sha256_b64,
                    expected_magic_mime="image/png",
                    storage_revision=inspected.storage_revision,
                ),
            )

        committed_key = (
            "committed/ws1/generation_output/2026/09/22/upload.png"
        )
        self.assertNotIn(committed_key, self.client.objects)

    async def test_deleted_staging_object_fails_closed(self) -> None:
        data = b"\x89PNG\r\n\x1a\n" + b"payload"
        self.client.put_test_object(self.staging_key, data)
        inspected = await self.store.inspect_upload(
            StagedBlobRef(staging_key=self.staging_key)
        )
        self.client.objects.pop(self.staging_key)
        with self.assertRaises(RevisionGoneError):
            await self.store.commit_upload(
                inspected,
                ObjectConstraints(
                    expected_size=inspected.size_bytes,
                    expected_checksum_sha256_b64=inspected.checksum_sha256_b64,
                    expected_magic_mime="image/png",
                    storage_revision=inspected.storage_revision,
                ),
            )

    async def test_existing_different_committed_object_is_never_overwritten(
        self,
    ) -> None:
        data = b"\x89PNG\r\n\x1a\n" + b"payload"
        self.client.put_test_object(self.staging_key, data)
        inspected = await self.store.inspect_upload(
            StagedBlobRef(staging_key=self.staging_key)
        )
        committed_key = (
            "committed/ws1/generation_output/2026/09/22/upload.png"
        )
        self.client.put_test_object(committed_key, b"different")
        with self.assertRaises(BlobStoreError):
            await self.store.commit_upload(
                inspected,
                ObjectConstraints(
                    expected_size=inspected.size_bytes,
                    expected_checksum_sha256_b64=inspected.checksum_sha256_b64,
                    expected_magic_mime="image/png",
                    storage_revision=inspected.storage_revision,
                ),
            )
        self.assertEqual(self.client.objects[committed_key], b"different")

    async def test_delete_is_idempotent(self) -> None:
        await self.store.delete(BlobRef(key=self.staging_key))
        await self.store.delete(BlobRef(key=self.staging_key))
        self.assertEqual(self.client.deleted, [self.staging_key, self.staging_key])

    async def test_generated_artifact_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "artifact.zip"
            data = b"PK\x03\x04" + b"generated" * 20
            source.write_bytes(data)
            ref = await self.store.commit_generated_artifact(
                source,
                GeneratedArtifactSpec(
                    committed_key=(
                        "committed/ws1/exports/2026/09/22/artifact.zip"
                    ),
                    content_type="application/zip",
                    expected_sha256_b64=_checksum(data),
                ),
            )
        chunks = []
        async for chunk in self.store.open_committed(ref):
            chunks.append(chunk)
        self.assertEqual(b"".join(chunks), data)

    async def test_browser_cors_is_written_as_exact_origins_and_read_back(self) -> None:
        result = await self.store.converge_browser_cors(
            ["https://App.Example.com", " https://app.example.com ", "https://other.example.com:8443"]
        )
        self.assertEqual(result, {"origins": 2})
        rules = self.client.cors["CORSRules"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(
            rules[0]["AllowedOrigins"],
            ["https://app.example.com", "https://other.example.com:8443"],
        )
        self.assertEqual(rules[0]["AllowedMethods"], ["GET", "HEAD", "PUT"])
        self.assertEqual(rules[0]["AllowedHeaders"], ["*"])
        self.assertIn("ETag", rules[0]["ExposeHeaders"])

    async def test_browser_cors_rejects_wildcards_and_insecure_origins(self) -> None:
        for origins in (["*"], ["https://*.example.com"], ["http://app.example.com"],
                        ["https://app.example.com/path"], [""], []):
            with self.subTest(origins=origins):
                with self.assertRaises(ValueError):
                    await self.store.converge_browser_cors(origins)
        self.assertFalse(getattr(self.client, "cors", None))

    async def test_browser_cors_fails_closed_when_the_rule_is_not_retained(self) -> None:
        def drop_rule(*, Bucket, CORSConfiguration):
            self.client.cors = {"CORSRules": []}
            return {}

        self.client.put_bucket_cors = drop_rule
        with self.assertRaises(BlobStoreError):
            await self.store.converge_browser_cors(["https://app.example.com"])

    async def test_browser_cors_storage_errors_are_redacted(self) -> None:
        def refuse(*, Bucket, CORSConfiguration):
            raise _client_error("AccessDenied", "PutBucketCors")

        self.client.put_bucket_cors = refuse
        with self.assertRaises(BlobStoreError) as caught:
            await self.store.converge_browser_cors(["https://app.example.com"])
        self.assertEqual(
            str(caught.exception),
            "Railway Bucket converge browser CORS failed (AccessDenied)",
        )

    async def test_optional_capability_probe_records_behavior_and_cleans_up(
        self,
    ) -> None:
        report = await self.store.probe_optional_capabilities("run123")
        self.assertEqual(
            report,
            {
                "checksum_rejects_mismatch": True,
                "checksum_head_roundtrip": True,
                "etag_changes_on_overwrite": True,
                "conditional_copy_rejects_stale_source": True,
            },
        )
        self.assertFalse(
            [key for key in self.client.objects if key.startswith("healthchecks/")]
        )


class RailwayBucketFactoryTests(unittest.TestCase):
    def test_factory_wires_railway_bucket_configuration(self) -> None:
        with (
            patch.object(config, "BLOB_STORE_ADAPTER", "railway_bucket"),
            patch.object(config, "BLOB_STORE_S3_BUCKET", "bucket"),
            patch.object(config, "BLOB_STORE_S3_ENDPOINT_URL", "https://endpoint"),
            patch.object(config, "BLOB_STORE_S3_REGION", "auto"),
            patch.object(config, "BLOB_STORE_S3_ADDRESSING_STYLE", "virtual"),
            patch(
                "app.adapters.blob_store_railway_bucket.RailwayBucketBlobStore"
            ) as adapter,
        ):
            result = build_blob_store()
        self.assertIs(result, adapter.return_value)
        adapter.assert_called_once_with(
            bucket="bucket",
            endpoint_url="https://endpoint",
            region="auto",
            addressing_style="virtual",
        )

    def test_factory_rejects_unknown_adapter(self) -> None:
        with patch.object(config, "BLOB_STORE_ADAPTER", "typo"):
            with self.assertRaises(ValueError):
                build_blob_store()


if __name__ == "__main__":
    unittest.main()
