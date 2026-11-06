"""Railway Bucket ``BlobStore`` adapter.

Railway Bucket intentionally has no object versioning.  A staging key can
therefore be overwritten between :meth:`inspect_upload` and
:meth:`commit_upload`, so neither ``ETag`` nor a version-pinned server-side
copy can provide the protocol guarantee on which callers rely.

This adapter closes that race without routing browser uploads through the
API: the browser still uploads directly with a presigned PUT.  The Worker
fully hashes the staged object during inspection.  At commit it downloads
the then-current object into a private temporary file, hashes it again, and
only performs an atomic PutObject to the committed namespace when the bytes
still match the inspected size and SHA-256.  A different object is never
written to the committed key.

Railway does not provide lifecycle rules.  This adapter therefore leaves
staging deletion to the durable application cleanup job enqueued by
``app.platform.upload_intents``.  ``delete`` is idempotent so that job is
safe to recover and replay.
"""
from __future__ import annotations

import base64
import hashlib
import tempfile
import time
from collections.abc import AsyncIterator, Sequence
from urllib.parse import urlsplit
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

import anyio

from ..platform.blob_store import (
    BlobRef,
    BlobStoreError,
    CommittedBlobRef,
    DownloadGrant,
    DownloadSpec,
    GeneratedArtifactSpec,
    InspectedUpload,
    ObjectChangedError,
    ObjectConstraints,
    ObjectNotFoundError,
    RevisionGoneError,
    SignedPutGrant,
    StagedBlobRef,
    UploadSpec,
    content_disposition_header,
    derive_committed_key,
    validate_object_key,
)

try:
    import boto3  # type: ignore[import-untyped]
    from botocore.config import Config  # type: ignore[import-untyped]
    from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - only environments missing production dependency
    boto3 = None  # type: ignore[assignment]
    Config = None  # type: ignore[assignment, misc]

    class BotoCoreError(Exception):  # type: ignore[no-redef]
        """Placeholder used by tests that inject a client."""

    class ClientError(Exception):  # type: ignore[no-redef]
        """Placeholder used by tests that inject a client."""

        response: dict[str, Any] = {}


_NOT_FOUND_CODES = frozenset({"NoSuchKey", "404", "NotFound"})
_READ_CHUNK = 1024 * 1024
_STREAM_CHUNK = 65536
_MAGIC_BYTES_READ_LEN = 8192
_CHECKSUM_METADATA_KEY = "marketai-sha256"


def _error_code(exc: BaseException) -> str:
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))


def _safe_storage_error(operation: str, exc: BaseException) -> BlobStoreError:
    """Return an operator-useful error without endpoint URLs or credentials."""
    code = _error_code(exc)
    detail = code if code else type(exc).__name__
    return BlobStoreError(f"Railway Bucket {operation} failed ({detail})")


async def _to_thread(func, *args: Any, **kwargs: Any) -> Any:
    return await anyio.to_thread.run_sync(lambda: func(*args, **kwargs))


def _validate_browser_origins(origins: Sequence[str]) -> list[str]:
    """Accept only exact HTTPS origins (scheme + host [+ port]); no wildcards."""
    cleaned: list[str] = []
    for raw in origins:
        origin = (raw or "").strip()
        if not origin:
            continue
        if "*" in origin:
            raise ValueError("browser CORS origins must not contain wildcards")
        parts = urlsplit(origin)
        if parts.scheme != "https" or not parts.netloc:
            raise ValueError("browser CORS origins must be https://host[:port]")
        if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
            raise ValueError("browser CORS origins must not carry a path, query, fragment or credentials")
        normalized = f"https://{parts.netloc.lower()}"
        if normalized not in cleaned:
            cleaned.append(normalized)
    if not cleaned:
        raise ValueError("at least one browser CORS origin is required")
    return cleaned


def _checksum_b64(digest: Any) -> str:
    return base64.b64encode(digest.digest()).decode("ascii")


def _content_revision(size_bytes: int, checksum_sha256_b64: str) -> str:
    """Content-addressed revision token used where Railway has no VersionId."""
    token = hashlib.sha256(checksum_sha256_b64.encode("ascii")).hexdigest()
    return f"sha256:{token}:{size_bytes}"


class RailwayBucketBlobStore:
    """BlobStore over Railway's private-by-authentication S3-compatible bucket."""

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str,
        region: str | None = None,
        addressing_style: str = "virtual",
        client: Any | None = None,
    ) -> None:
        if not bucket:
            raise ValueError("RailwayBucketBlobStore requires a bucket name")
        if client is None and not endpoint_url:
            raise ValueError("RailwayBucketBlobStore requires an endpoint URL")
        if addressing_style not in {"virtual", "path"}:
            raise ValueError("Railway Bucket addressing style must be 'virtual' or 'path'")

        if client is not None:
            self._client = client
        else:
            if boto3 is None or Config is None:
                raise ImportError(
                    "boto3 is required to construct RailwayBucketBlobStore without an injected client"
                )
            self._client = boto3.client(
                "s3",
                endpoint_url=endpoint_url,
                region_name=region or "auto",
                config=Config(
                    signature_version="s3v4",
                    s3={"addressing_style": addressing_style},
                ),
            )
        self._bucket = bucket

    async def issue_upload(self, spec: UploadSpec) -> SignedPutGrant:
        """Issue a direct browser PUT; correctness is verified after upload.

        Railway documents presigned PUT support but does not document
        checksum-header enforcement.  The expected SHA-256 is therefore
        signed as object metadata for diagnostics while ``inspect_upload``
        independently hashes the received bytes.  Size bounds are likewise
        enforced by the application verification sequence.
        """
        validate_object_key(spec.staging_key)
        params = {
            "Bucket": self._bucket,
            "Key": spec.staging_key,
            "ContentType": spec.content_type,
            "Metadata": {_CHECKSUM_METADATA_KEY: spec.checksum_sha256_b64},
        }
        try:
            url = await _to_thread(
                self._client.generate_presigned_url,
                "put_object",
                Params=params,
                ExpiresIn=spec.expires_in_seconds,
            )
        except (ClientError, BotoCoreError) as exc:
            raise _safe_storage_error("presign PUT", exc) from exc
        return SignedPutGrant(
            kind="signed_put",
            url=str(url),
            headers={
                "Content-Type": spec.content_type,
                f"x-amz-meta-{_CHECKSUM_METADATA_KEY}": spec.checksum_sha256_b64,
            },
            expires_at=datetime.fromtimestamp(
                time.time() + spec.expires_in_seconds, tz=timezone.utc
            ),
        )

    async def inspect_upload(self, ref: StagedBlobRef) -> InspectedUpload:
        validate_object_key(ref.staging_key)
        try:
            obj = await _to_thread(
                self._client.get_object, Bucket=self._bucket, Key=ref.staging_key
            )
            size_bytes, checksum, magic_bytes = await _to_thread(
                self._hash_body, obj["Body"], None
            )
        except ClientError as exc:
            if _error_code(exc) in _NOT_FOUND_CODES:
                raise ObjectNotFoundError(ref.staging_key) from exc
            raise _safe_storage_error("inspect", exc) from exc
        except BotoCoreError as exc:
            raise _safe_storage_error("inspect", exc) from exc

        # Metadata and body come from one GetObject response, so inspection
        # never combines a HEAD for one revision with magic bytes from another.
        content_length = int(obj.get("ContentLength", size_bytes))
        if content_length != size_bytes:
            raise ObjectChangedError(ref.staging_key)
        return InspectedUpload(
            ref=ref,
            storage_revision=_content_revision(size_bytes, checksum),
            size_bytes=size_bytes,
            checksum_sha256_b64=checksum,
            content_type_declared=str(
                obj.get("ContentType") or "application/octet-stream"
            ),
            magic_bytes=magic_bytes,
        )

    async def commit_upload(
        self,
        inspected: InspectedUpload,
        expected: ObjectConstraints,
    ) -> CommittedBlobRef:
        """Commit only bytes whose full SHA-256 still matches inspection."""
        self._validate_expected(inspected, expected)
        staging_key = inspected.ref.staging_key
        committed_key = derive_committed_key(
            staging_key, expected.expected_magic_mime
        )

        with tempfile.TemporaryFile(mode="w+b") as snapshot:
            try:
                obj = await _to_thread(
                    self._client.get_object, Bucket=self._bucket, Key=staging_key
                )
                size_bytes, checksum, _ = await _to_thread(
                    self._hash_body, obj["Body"], snapshot
                )
            except ClientError as exc:
                if _error_code(exc) in _NOT_FOUND_CODES:
                    raise RevisionGoneError(staging_key) from exc
                raise _safe_storage_error("snapshot staging object", exc) from exc
            except BotoCoreError as exc:
                raise _safe_storage_error("snapshot staging object", exc) from exc

            current_revision = _content_revision(size_bytes, checksum)
            if (
                current_revision != expected.storage_revision
                or size_bytes != expected.expected_size
                or checksum != expected.expected_checksum_sha256_b64
            ):
                raise ObjectChangedError(staging_key)

            existing = await self._inspect_existing_committed(committed_key)
            if existing is not None:
                existing_size, existing_checksum = existing
                if (
                    existing_size != expected.expected_size
                    or existing_checksum != expected.expected_checksum_sha256_b64
                ):
                    raise BlobStoreError(
                        "Railway Bucket committed key already contains different bytes"
                    )
            else:
                snapshot.seek(0)
                try:
                    await _to_thread(
                        self._client.put_object,
                        Bucket=self._bucket,
                        Key=committed_key,
                        Body=snapshot,
                        ContentType=expected.expected_magic_mime,
                        Metadata={
                            _CHECKSUM_METADATA_KEY: expected.expected_checksum_sha256_b64
                        },
                    )
                except (ClientError, BotoCoreError) as exc:
                    raise _safe_storage_error("commit", exc) from exc

                # Read-after-write verification is not the source of atomicity
                # (PutObject is); it makes a platform incompatibility fail
                # closed before a CommittedBlobRef can escape this method.
                stored = await self._inspect_existing_committed(committed_key)
                if stored != (expected.expected_size, expected.expected_checksum_sha256_b64):
                    await self._best_effort_delete(committed_key)
                    raise BlobStoreError(
                        "Railway Bucket could not verify the committed object"
                    )

        return CommittedBlobRef(
            committed_key=committed_key,
            storage_version_id=_content_revision(
                expected.expected_size, expected.expected_checksum_sha256_b64
            ),
            size_bytes=expected.expected_size,
            checksum_sha256_b64=expected.expected_checksum_sha256_b64,
            content_type_verified=expected.expected_magic_mime,
        )

    async def issue_download(
        self, ref: CommittedBlobRef, spec: DownloadSpec
    ) -> DownloadGrant:
        validate_object_key(ref.committed_key)
        disposition = content_disposition_header(spec.filename)
        params = {
            "Bucket": self._bucket,
            "Key": ref.committed_key,
            "ResponseContentDisposition": disposition,
        }
        try:
            url = await _to_thread(
                self._client.generate_presigned_url,
                "get_object",
                Params=params,
                ExpiresIn=spec.expires_in_seconds,
            )
        except (ClientError, BotoCoreError) as exc:
            raise _safe_storage_error("presign GET", exc) from exc
        return DownloadGrant(
            url=str(url),
            expires_at=datetime.fromtimestamp(
                time.time() + spec.expires_in_seconds, tz=timezone.utc
            ),
            content_disposition=disposition,
        )

    async def delete(self, ref: BlobRef) -> None:
        validate_object_key(ref.key)
        try:
            await _to_thread(
                self._client.delete_object, Bucket=self._bucket, Key=ref.key
            )
        except ClientError as exc:
            if _error_code(exc) in _NOT_FOUND_CODES:
                return
            raise _safe_storage_error("delete", exc) from exc
        except BotoCoreError as exc:
            raise _safe_storage_error("delete", exc) from exc

    async def open_committed(
        self, ref: CommittedBlobRef
    ) -> AsyncIterator[bytes]:
        validate_object_key(ref.committed_key)
        try:
            obj = await _to_thread(
                self._client.get_object,
                Bucket=self._bucket,
                Key=ref.committed_key,
            )
        except ClientError as exc:
            if _error_code(exc) in _NOT_FOUND_CODES:
                raise ObjectNotFoundError(ref.committed_key) from exc
            raise _safe_storage_error("open committed object", exc) from exc
        except BotoCoreError as exc:
            raise _safe_storage_error("open committed object", exc) from exc
        body = obj["Body"]
        try:
            try:
                while chunk := await _to_thread(body.read, _STREAM_CHUNK):
                    yield chunk
            except (ClientError, BotoCoreError) as exc:
                raise _safe_storage_error("stream committed object", exc) from exc
        finally:
            close = getattr(body, "close", None)
            if close is not None:
                await _to_thread(close)

    async def commit_generated_artifact(
        self, source: Path, spec: GeneratedArtifactSpec
    ) -> CommittedBlobRef:
        validate_object_key(spec.committed_key)
        size_bytes, checksum = await _to_thread(self._hash_file, source)
        if checksum != spec.expected_sha256_b64:
            raise BlobStoreError("generated artifact checksum changed before commit")

        existing = await self._inspect_existing_committed(spec.committed_key)
        if existing is not None and existing != (size_bytes, checksum):
            raise BlobStoreError(
                "Railway Bucket committed key already contains different bytes"
            )
        if existing is None:
            try:
                with source.open("rb") as body:
                    await _to_thread(
                        self._client.put_object,
                        Bucket=self._bucket,
                        Key=spec.committed_key,
                        Body=body,
                        ContentType=spec.content_type,
                        Metadata={_CHECKSUM_METADATA_KEY: checksum},
                    )
            except (ClientError, BotoCoreError) as exc:
                raise _safe_storage_error("commit generated artifact", exc) from exc

            stored = await self._inspect_existing_committed(spec.committed_key)
            if stored != (size_bytes, checksum):
                await self._best_effort_delete(spec.committed_key)
                raise BlobStoreError(
                    "Railway Bucket could not verify the generated artifact"
                )

        return CommittedBlobRef(
            committed_key=spec.committed_key,
            storage_version_id=_content_revision(size_bytes, checksum),
            size_bytes=size_bytes,
            checksum_sha256_b64=checksum,
            content_type_verified=spec.content_type,
        )

    async def converge_browser_cors(self, origins: Sequence[str]) -> dict[str, Any]:
        """Install the bucket CORS rules the browser blob transfers need.

        ``BrowserBlobTransferClient`` fetches signed GET/PUT URLs straight
        from the Web origin (spec §7.4/§7.7); without a bucket CORS rule
        naming that origin every gallery thumbnail, lightbox and reference
        upload fails in the browser as a bare network error.  The rule set is
        written as a whole (``PutBucketCors`` replaces, never merges) and read
        back so a silently ignored write fails the release instead of
        shipping a broken gallery.

        Origins are exact ``https://host[:port]`` values, never wildcards --
        the same shape ``NEXT_PUBLIC_MARKETAI_STORAGE_ORIGINS`` enforces on
        the other side of the transfer.
        """
        cleaned = _validate_browser_origins(origins)
        configuration = {
            "CORSRules": [
                {
                    "AllowedOrigins": cleaned,
                    "AllowedMethods": ["GET", "HEAD", "PUT"],
                    "AllowedHeaders": ["*"],
                    "ExposeHeaders": ["ETag"],
                    "MaxAgeSeconds": 3000,
                }
            ]
        }
        try:
            await _to_thread(
                self._client.put_bucket_cors,
                Bucket=self._bucket,
                CORSConfiguration=configuration,
            )
            stored = await _to_thread(self._client.get_bucket_cors, Bucket=self._bucket)
        except (ClientError, BotoCoreError) as exc:
            raise _safe_storage_error("converge browser CORS", exc) from exc

        rules = stored.get("CORSRules") or []
        installed = {
            origin
            for rule in rules
            if "GET" in (rule.get("AllowedMethods") or [])
            for origin in (rule.get("AllowedOrigins") or [])
        }
        missing = [origin for origin in cleaned if origin not in installed]
        if missing:
            raise BlobStoreError(
                "Railway Bucket did not retain the browser CORS rule for "
                f"{len(missing)} origin(s)"
            )
        return {"origins": len(cleaned)}

    async def probe_optional_capabilities(self, run_id: str) -> dict[str, bool]:
        """Probe undocumented Railway S3 behaviors without relying on them.

        The production commit protocol does not use these capabilities.  The
        release service records the booleans so an operator can see when a
        future server-side-copy optimization becomes eligible.  Every key is
        random and deleted in ``finally``; errors are converted to ``False``
        and never stringified.
        """
        if not run_id or not run_id.replace("-", "").isalnum():
            raise ValueError("probe run_id must be path-safe")
        base = f"healthchecks/railway_bucket/{run_id}"
        checksum_key = f"{base}/checksum.bin"
        mismatch_key = f"{base}/checksum_mismatch.bin"
        source_key = f"{base}/copy_source.bin"
        destination_key = f"{base}/copy_destination.bin"
        keys = (checksum_key, mismatch_key, source_key, destination_key)
        payload_a = b"marketai-capability-A"
        payload_b = b"marketai-capability-B"
        checksum_a = base64.b64encode(hashlib.sha256(payload_a).digest()).decode(
            "ascii"
        )

        report = {
            "checksum_rejects_mismatch": False,
            "checksum_head_roundtrip": False,
            "etag_changes_on_overwrite": False,
            "conditional_copy_rejects_stale_source": False,
        }
        try:
            try:
                await _to_thread(
                    self._client.put_object,
                    Bucket=self._bucket,
                    Key=checksum_key,
                    Body=payload_a,
                    ChecksumSHA256=checksum_a,
                )
                head = await _to_thread(
                    self._client.head_object,
                    Bucket=self._bucket,
                    Key=checksum_key,
                    ChecksumMode="ENABLED",
                )
                report["checksum_head_roundtrip"] = (
                    str(head.get("ChecksumSHA256") or "") == checksum_a
                )
            except (ClientError, BotoCoreError, TypeError):
                pass

            try:
                await _to_thread(
                    self._client.put_object,
                    Bucket=self._bucket,
                    Key=mismatch_key,
                    Body=payload_b,
                    ChecksumSHA256=checksum_a,
                )
            except ClientError:
                report["checksum_rejects_mismatch"] = not await self._object_exists(
                    mismatch_key
                )
            except (BotoCoreError, TypeError):
                pass

            try:
                await _to_thread(
                    self._client.put_object,
                    Bucket=self._bucket,
                    Key=source_key,
                    Body=payload_a,
                )
                first_head = await _to_thread(
                    self._client.head_object,
                    Bucket=self._bucket,
                    Key=source_key,
                )
                first_etag = str(first_head.get("ETag") or "")
                await _to_thread(
                    self._client.put_object,
                    Bucket=self._bucket,
                    Key=source_key,
                    Body=payload_b,
                )
                second_head = await _to_thread(
                    self._client.head_object,
                    Bucket=self._bucket,
                    Key=source_key,
                )
                second_etag = str(second_head.get("ETag") or "")
                report["etag_changes_on_overwrite"] = bool(
                    first_etag and second_etag and first_etag != second_etag
                )
                try:
                    await _to_thread(
                        self._client.copy_object,
                        Bucket=self._bucket,
                        Key=destination_key,
                        CopySource={"Bucket": self._bucket, "Key": source_key},
                        CopySourceIfMatch=first_etag,
                        IfNoneMatch="*",
                    )
                except ClientError as exc:
                    report["conditional_copy_rejects_stale_source"] = (
                        _error_code(exc) in {"412", "PreconditionFailed"}
                        and not await self._object_exists(destination_key)
                    )
                except (BotoCoreError, TypeError):
                    pass
            except (ClientError, BotoCoreError, TypeError):
                pass
        finally:
            for key in keys:
                await self._best_effort_delete(key)
        return report

    @staticmethod
    def _validate_expected(
        inspected: InspectedUpload, expected: ObjectConstraints
    ) -> None:
        if (
            expected.storage_revision != inspected.storage_revision
            or expected.expected_size != inspected.size_bytes
            or expected.expected_checksum_sha256_b64
            != inspected.checksum_sha256_b64
        ):
            raise ObjectChangedError(inspected.ref.staging_key)

    @staticmethod
    def _hash_body(
        body: Any, destination: BinaryIO | None
    ) -> tuple[int, str, bytes]:
        digest = hashlib.sha256()
        size_bytes = 0
        magic_bytes = b""
        try:
            while chunk := body.read(_READ_CHUNK):
                if not magic_bytes:
                    magic_bytes = chunk[:_MAGIC_BYTES_READ_LEN]
                digest.update(chunk)
                size_bytes += len(chunk)
                if destination is not None:
                    destination.write(chunk)
        finally:
            close = getattr(body, "close", None)
            if close is not None:
                close()
        return size_bytes, _checksum_b64(digest), magic_bytes

    @staticmethod
    def _hash_file(source: Path) -> tuple[int, str]:
        digest = hashlib.sha256()
        size_bytes = 0
        with source.open("rb") as body:
            while chunk := body.read(_READ_CHUNK):
                digest.update(chunk)
                size_bytes += len(chunk)
        return size_bytes, _checksum_b64(digest)

    async def _inspect_existing_committed(
        self, key: str
    ) -> tuple[int, str] | None:
        try:
            obj = await _to_thread(
                self._client.get_object, Bucket=self._bucket, Key=key
            )
            size_bytes, checksum, _ = await _to_thread(
                self._hash_body, obj["Body"], None
            )
            return size_bytes, checksum
        except ClientError as exc:
            if _error_code(exc) in _NOT_FOUND_CODES:
                return None
            raise _safe_storage_error("verify committed object", exc) from exc
        except BotoCoreError as exc:
            raise _safe_storage_error("verify committed object", exc) from exc

    async def _best_effort_delete(self, key: str) -> None:
        try:
            await _to_thread(
                self._client.delete_object, Bucket=self._bucket, Key=key
            )
        except (ClientError, BotoCoreError):
            # The caller still receives a fail-closed BlobStoreError.  The
            # object is not returned as committed and operators can remove
            # the unreferenced key using the documented recovery procedure.
            return

    async def _object_exists(self, key: str) -> bool:
        try:
            await _to_thread(
                self._client.head_object, Bucket=self._bucket, Key=key
            )
            return True
        except ClientError as exc:
            if _error_code(exc) in _NOT_FOUND_CODES:
                return False
            return True
        except (BotoCoreError, TypeError):
            return True
