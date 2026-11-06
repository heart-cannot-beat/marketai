"""S3-backed `BlobStore` Adapter (spec §7.4) -- the production Adapter.

Uses a presigned POST policy for uploads (`content-length-range` and
checksum enforced at the storage layer) and copies the exact inspected
`VersionId` at commit time, so a staging key overwritten after
`inspect_upload` never contaminates the committed object.

`boto3` is imported lazily so this module stays importable in environments
where it is not installed (e.g. this dev sandbox) -- only constructing an
`S3BlobStore` without an injected `client=` requires it. All boto3 calls run
off the event loop via `anyio.to_thread.run_sync`, matching the pattern the
rest of this codebase uses for blocking I/O in async code (see
`WorkspaceAdmissionReader`).
"""
from __future__ import annotations

import time
from collections.abc import AsyncIterator
import base64
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anyio

from ..platform.blob_store import (
    BlobRef,
    CommittedBlobRef,
    DownloadGrant,
    DownloadSpec,
    GeneratedArtifactSpec,
    InspectedUpload,
    ObjectChangedError,  # noqa: F401  (re-exported for callers catching Adapter errors)
    ObjectConstraints,
    ObjectNotFoundError,
    PresignedPostGrant,
    RevisionGoneError,
    StagedBlobRef,
    UploadSpec,
    content_disposition_header,
    derive_committed_key,
    validate_object_key,
)

try:
    import boto3  # type: ignore[import-untyped]
    from botocore.exceptions import ClientError  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - exercised only where boto3 is absent
    boto3 = None  # type: ignore[assignment]

    class ClientError(Exception):  # type: ignore[no-redef]
        """Placeholder so type references resolve when botocore is absent."""

        response: dict[str, Any] = {}


#: `HeadObject`/`CopyObject` error codes that map to a terminal, non-retryable
#: BlobStore error rather than an unclassified exception.
_NOT_FOUND_CODES = frozenset({"NoSuchKey", "404", "NotFound"})
_INVALID_VERSION_CODES = frozenset({"InvalidArgument", "NoSuchVersion"})

#: Chunk size for `open_committed`'s streaming reads over `get_object`'s
#: `Body` -- matches `LocalFileBlobStore.open_committed`'s chunking shape.
_STREAM_CHUNK = 65536


def _error_code(exc: "ClientError") -> str:
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))


async def _to_thread(func, *args: Any, **kwargs: Any) -> Any:
    return await anyio.to_thread.run_sync(lambda: func(*args, **kwargs))


class S3BlobStore:
    """Production `BlobStore` Adapter over an S3 bucket with Versioning enabled.

    The bucket must have Versioning enabled (bucket-level setting -- spec
    §7.4) and lifecycle rules on the `staging/` prefix (spec §7.15). This
    Adapter does not create the bucket or its lifecycle configuration; that
    is infrastructure, not runtime behavior.
    """

    def __init__(self, *, bucket: str, region: str | None = None, client: Any | None = None) -> None:
        if client is not None:
            self._client = client
        else:
            if boto3 is None:
                raise ImportError(
                    "boto3 is required to construct S3BlobStore without an "
                    "explicit `client=`; install boto3 or inject a test double."
                )
            self._client = boto3.client("s3", region_name=region)
        self._bucket = bucket

    async def read_committed(self, ref: CommittedBlobRef, max_bytes: int) -> bytes:
        if ref.size_bytes > max_bytes:
            raise ValueError("blob limit exceeded")
        result = await _to_thread(self._client.get_object, Bucket=self._bucket,
                                  Key=ref.committed_key, VersionId=ref.storage_version_id)
        def read():
            body = result["Body"]
            try:
                content = body.read(max_bytes + 1)
            finally:
                body.close()
            if len(content) != ref.size_bytes or len(content) > max_bytes:
                raise ValueError("blob size mismatch or limit exceeded")
            if ref.checksum_sha256_b64 and base64.b64encode(hashlib.sha256(content).digest()).decode() != ref.checksum_sha256_b64:
                raise ObjectChangedError(ref.committed_key)
            return content
        return await _to_thread(read)

    async def write_generated(self, key: str, content: bytes, mime: str) -> CommittedBlobRef:
        validate_object_key(key)
        if not key.startswith("committed/"):
            raise ValueError("generated output requires a committed key")
        checksum = base64.b64encode(hashlib.sha256(content).digest()).decode()
        try:
            result = await _to_thread(self._client.put_object, Bucket=self._bucket, Key=key,
                                     Body=content, ContentType=mime, ChecksumSHA256=checksum,
                                     IfNoneMatch="*")
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code")) not in ("PreconditionFailed", "412"):
                raise
            result = await _to_thread(self._client.head_object, Bucket=self._bucket, Key=key, ChecksumMode="ENABLED")
            if result.get("ChecksumSHA256") != checksum or result.get("ContentLength") != len(content):
                raise ObjectChangedError(key) from exc
        version = result.get("VersionId")
        if not version or version == "null":
            raise ValueError("generated output requires S3 versioning")
        return CommittedBlobRef(key, version, len(content), checksum, mime)

    async def issue_upload(self, spec: UploadSpec) -> PresignedPostGrant:
        validate_object_key(spec.staging_key)
        fields = {
            "Content-Type": spec.content_type,
            "x-amz-checksum-sha256": spec.checksum_sha256_b64,
        }
        conditions: list[Any] = [
            {"Content-Type": spec.content_type},
            {"x-amz-checksum-sha256": spec.checksum_sha256_b64},
            ["content-length-range", spec.content_length_min, spec.content_length_max],
        ]
        post = await _to_thread(
            self._client.generate_presigned_post,
            Bucket=self._bucket,
            Key=spec.staging_key,
            Fields=fields,
            Conditions=conditions,
            ExpiresIn=spec.expires_in_seconds,
        )
        return PresignedPostGrant(
            kind="presigned_post",
            url=post["url"],
            fields=dict(post["fields"]),
            expires_at=datetime.fromtimestamp(time.time() + spec.expires_in_seconds, tz=timezone.utc),
        )

    async def inspect_upload(self, ref: StagedBlobRef) -> InspectedUpload:
        try:
            head = await _to_thread(self._client.head_object, Bucket=self._bucket, Key=ref.staging_key)
        except ClientError as exc:
            if _error_code(exc) in _NOT_FOUND_CODES:
                raise ObjectNotFoundError(ref.staging_key) from exc
            raise
        version_id = head.get("VersionId")
        if not version_id or version_id == "null":
            # "null" is what an un-versioned bucket returns; this Adapter
            # requires bucket Versioning (spec §7.4).
            raise ObjectNotFoundError(ref.staging_key)
        try:
            obj = await _to_thread(
                self._client.get_object,
                Bucket=self._bucket,
                Key=ref.staging_key,
                VersionId=version_id,
                Range="bytes=0-8191",
            )
        except ClientError as exc:
            if _error_code(exc) in _NOT_FOUND_CODES:
                raise ObjectNotFoundError(ref.staging_key) from exc
            raise
        magic_bytes = obj["Body"].read()
        checksum = str(head.get("ChecksumSHA256") or "")
        return InspectedUpload(
            ref=ref,
            storage_revision=version_id,
            size_bytes=int(head["ContentLength"]),
            checksum_sha256_b64=checksum,
            content_type_declared=str(head.get("ContentType") or "application/octet-stream"),
            magic_bytes=magic_bytes,
        )

    async def commit_upload(
        self,
        inspected: InspectedUpload,
        expected: ObjectConstraints,
    ) -> CommittedBlobRef:
        committed_key = derive_committed_key(inspected.ref.staging_key, expected.expected_magic_mime)
        copy_source = {
            "Bucket": self._bucket,
            "Key": inspected.ref.staging_key,
            "VersionId": expected.storage_revision,
        }
        try:
            result = await _to_thread(
                self._client.copy_object,
                Bucket=self._bucket,
                Key=committed_key,
                CopySource=copy_source,
                ContentType=expected.expected_magic_mime,
                MetadataDirective="REPLACE",
            )
        except ClientError as exc:
            code = _error_code(exc)
            if code in _NOT_FOUND_CODES or code in _INVALID_VERSION_CODES:
                raise RevisionGoneError(inspected.ref.staging_key) from exc
            raise
        version_id = str(result.get("VersionId") or result.get("CopyObjectResult", {}).get("VersionId") or "")
        return CommittedBlobRef(
            committed_key=committed_key,
            storage_version_id=version_id,
            size_bytes=inspected.size_bytes,
            checksum_sha256_b64=inspected.checksum_sha256_b64,
            content_type_verified=expected.expected_magic_mime,
        )

    async def issue_download(self, ref: CommittedBlobRef, spec: DownloadSpec) -> DownloadGrant:
        if not ref.storage_version_id:
            raise ValueError("storage_version_id is required to issue a download grant")
        disposition = content_disposition_header(spec.filename)
        params = {
            "Bucket": self._bucket,
            "Key": ref.committed_key,
            "VersionId": ref.storage_version_id,
            "ResponseContentDisposition": disposition,
        }
        url = await _to_thread(
            self._client.generate_presigned_url,
            "get_object",
            Params=params,
            ExpiresIn=spec.expires_in_seconds,
        )
        return DownloadGrant(
            url=url,
            expires_at=datetime.fromtimestamp(time.time() + spec.expires_in_seconds, tz=timezone.utc),
            content_disposition=disposition,
        )

    async def delete(self, ref: BlobRef) -> None:
        kwargs: dict[str, Any] = {"Bucket": self._bucket, "Key": ref.key}
        if ref.version_id:
            kwargs["VersionId"] = ref.version_id
        try:
            await _to_thread(self._client.delete_object, **kwargs)
        except ClientError as exc:
            if _error_code(exc) in _NOT_FOUND_CODES:
                return
            raise

    async def open_committed(self, ref: CommittedBlobRef) -> AsyncIterator[bytes]:
        try:
            obj = await _to_thread(
                self._client.get_object,
                Bucket=self._bucket,
                Key=ref.committed_key,
                VersionId=ref.storage_version_id,
            )
        except ClientError as exc:
            code = _error_code(exc)
            if code in _NOT_FOUND_CODES or code in _INVALID_VERSION_CODES:
                raise ObjectNotFoundError(ref.committed_key) from exc
            raise
        body = obj["Body"]
        while chunk := await _to_thread(body.read, _STREAM_CHUNK):
            yield chunk

    async def commit_generated_artifact(
        self, source: Path, spec: GeneratedArtifactSpec
    ) -> CommittedBlobRef:
        validate_object_key(spec.committed_key)
        size_bytes = await _to_thread(lambda: Path(source).stat().st_size)

        def _put() -> dict[str, Any]:
            with open(source, "rb") as fh:
                return self._client.put_object(
                    Bucket=self._bucket,
                    Key=spec.committed_key,
                    Body=fh,
                    ContentType=spec.content_type,
                )

        result = await _to_thread(_put)
        version_id = str(result.get("VersionId") or "")
        return CommittedBlobRef(
            committed_key=spec.committed_key,
            storage_version_id=version_id,
            size_bytes=size_bytes,
            checksum_sha256_b64=spec.expected_sha256_b64,
            content_type_verified=spec.content_type,
        )
