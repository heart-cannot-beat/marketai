"""Dev-only local filesystem `BlobStore` Adapter (spec §7.4).

`LocalFileBlobStore` issues HMAC-signed URLs pointing at the separate
`LocalBlobTransferApp` process (`worker/local_blob_transfer.py`, default
`http://localhost:9000`) for the actual binary transfer, and performs the
trusted-path operations (`inspect_upload`, `commit_upload`, `delete`)
directly against the filesystem -- mirroring the shape of `S3BlobStore`.

This module also exports the filesystem/locking/signature primitives shared
with `LocalBlobTransferApp`, so the two processes agree on: how a key
resolves to a path (no symlink, no parent escape), how the opaque
`storage_revision` is computed (`st_dev:st_ino:st_mtime_ns:st_size`), which
per-key advisory lock file serializes a transfer's final rename against a
Worker's `commit_upload`, and how the local capability signature is built
and verified.
"""
from __future__ import annotations

import base64
import errno
import fcntl
import hashlib
import hmac
import json
import os
import shutil
import tempfile
import time
import urllib.parse
from collections.abc import AsyncIterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import anyio

from ..platform.blob_store import (
    BlobRef,
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

#: First N bytes captured for magic-byte MIME sniffing by the caller (the
#: Worker owns the actual sniffing; this Adapter only captures the bytes --
#: spec §7.5).
MAGIC_BYTES_READ_LEN = 8192
_READ_CHUNK = 1024 * 1024

#: Chunk size for `open_committed`'s streaming reads -- matches the 64KB
#: streaming granularity `S3BlobStore.open_committed` uses over `get_object`'s
#: `Body`, so a Worker consuming either Adapter sees the same chunking shape.
_STREAM_CHUNK = 65536

#: `os.O_NOFOLLOW` is POSIX-only; this Adapter is a dev-only local path so a
#: missing flag (non-POSIX platforms) degrades to "follow symlinks" rather
#: than failing import.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


# --------------------------------------------------------------------------
# Filesystem primitives shared with LocalBlobTransferApp
# --------------------------------------------------------------------------


def compute_revision(st: os.stat_result) -> str:
    """Opaque `storage_revision` token (spec §7.4)."""
    return f"{st.st_dev}:{st.st_ino}:{st.st_mtime_ns}:{st.st_size}"


def resolve_key_path(root: Path, key: str) -> Path:
    """Resolve `key` beneath `root`, rejecting traversal and symlink escape.

    Every path derived from a caller-controlled key routes through here --
    the sole no-symlink/no-parent-escape check (spec §7.4's transfer
    enforcement step 2, §7.16's "keys cannot escape their prefix").
    """
    validate_object_key(key)
    root = root.resolve()
    candidate = (root / key).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ValueError(f"object key escapes storage root: {key!r}") from None
    return candidate


def sidecar_path(path: Path) -> Path:
    """Path to the declared-metadata sidecar for a resolved blob path.

    The local filesystem has no object-metadata store, unlike S3's
    `Content-Type`/checksum object metadata. `LocalBlobTransferApp` writes
    this JSON sidecar (`content_type`, `checksum_sha256_b64` as declared at
    `issue_upload` time) next to the blob so `inspect_upload` can report
    `InspectedUpload.content_type_declared` the same way the S3 Adapter
    reads it from `HeadObject`.
    """
    return path.with_name(path.name + ".meta.json")


def lock_path_for(root: Path, key: str) -> Path:
    """Deterministic advisory-lock path for `key`.

    Both `LocalFileBlobStore.commit_upload` and `LocalBlobTransferApp`'s PUT
    handler take this same lock around their respective terminal rename, so
    a transfer's final write and a Worker's commit can never interleave
    (spec §7.4).
    """
    locks_dir = root.resolve() / ".locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return locks_dir / f"{digest}.lock"


@contextmanager
def advisory_lock(lock_file: Path) -> Iterator[None]:
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# --------------------------------------------------------------------------
# Local capability signature (spec §7.4)
# --------------------------------------------------------------------------


def _signature_message(
    method: str,
    key: str,
    expiry: int,
    *,
    storage_revision: str | None = None,
    content_type: str | None = None,
    content_length_min: int | None = None,
    content_length_max: int | None = None,
    checksum_sha256_b64: str | None = None,
) -> str:
    message = f"{method}\n{key}\n{expiry}"
    if method == "GET":
        message += f"\n{storage_revision}"
    elif method == "PUT":
        message += f"\n{content_type}\n{content_length_min}\n{content_length_max}\n{checksum_sha256_b64}"
    return message


def compute_local_signature(secret: bytes, method: str, key: str, expiry: int, **kwargs: object) -> str:
    message = _signature_message(method, key, expiry, **kwargs)  # type: ignore[arg-type]
    return hmac.new(secret, message.encode(), hashlib.sha256).hexdigest()


def sign_local_url(
    secret: bytes,
    public_base_url: str,
    method: str,
    key: str,
    expiry: int,
    *,
    storage_revision: str | None = None,
    content_type: str | None = None,
    content_length_min: int | None = None,
    content_length_max: int | None = None,
    checksum_sha256_b64: str | None = None,
) -> str:
    signature = compute_local_signature(
        secret,
        method,
        key,
        expiry,
        storage_revision=storage_revision,
        content_type=content_type,
        content_length_min=content_length_min,
        content_length_max=content_length_max,
        checksum_sha256_b64=checksum_sha256_b64,
    )
    query: dict[str, str | int] = {"expires": expiry}
    if method == "GET":
        query["v"] = storage_revision or ""
    elif method == "PUT":
        query.update(
            {
                "ct": content_type or "",
                "cl_min": content_length_min or 0,
                "cl_max": content_length_max or 0,
                "cs": checksum_sha256_b64 or "",
            }
        )
    qs = urllib.parse.urlencode(query)
    return f"{public_base_url.rstrip('/')}/_local_blob/{signature}/{key}?{qs}"


async def _run_sync(func, *args, **kwargs):
    return await anyio.to_thread.run_sync(lambda: func(*args, **kwargs))


# --------------------------------------------------------------------------
# LocalFileBlobStore
# --------------------------------------------------------------------------


class LocalFileBlobStore:
    """Dev-only `BlobStore` Adapter backed by the local filesystem.

    `issue_upload`/`issue_download` sign capabilities for the separate
    `LocalBlobTransferApp`; `inspect_upload`/`commit_upload`/`delete` operate
    on the filesystem directly since they run inside the trusted Worker/API
    process, not across the transfer boundary.
    """

    def __init__(self, *, root: Path | str, public_base_url: str, signing_secret: bytes) -> None:
        if not signing_secret:
            raise ValueError("LocalFileBlobStore requires a non-empty signing secret")
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._public_base_url = public_base_url.rstrip("/")
        self._signing_secret = signing_secret

    @property
    def root(self) -> Path:
        return self._root

    async def read_committed(self, ref: CommittedBlobRef, max_bytes: int) -> bytes:
        def read():
            path = resolve_key_path(self._root, ref.committed_key)
            with advisory_lock(lock_path_for(self._root, ref.committed_key)):
                with path.open("rb") as handle:
                    stat = os.fstat(handle.fileno())
                    if compute_revision(stat) != ref.storage_version_id:
                        raise ObjectChangedError(ref.committed_key)
                    if stat.st_size > max_bytes or stat.st_size != ref.size_bytes:
                        raise ValueError("blob size mismatch or limit exceeded")
                    content = handle.read(max_bytes + 1)
                if ref.checksum_sha256_b64 and base64.b64encode(hashlib.sha256(content).digest()).decode() != ref.checksum_sha256_b64:
                    raise ObjectChangedError(ref.committed_key)
                return content
        return await _run_sync(read)

    async def write_generated(self, key: str, content: bytes, mime: str) -> CommittedBlobRef:
        return await _run_sync(self._write_generated_sync, key, content, mime)

    def _write_generated_sync(self, key: str, content: bytes, mime: str) -> CommittedBlobRef:
        path = resolve_key_path(self._root, key)
        if not key.startswith("committed/"):
            raise ValueError("generated output requires a committed key")
        path.parent.mkdir(parents=True, exist_ok=True)
        with advisory_lock(lock_path_for(self._root, key)):
            if path.exists():
                if path.read_bytes() != content:
                    raise ObjectChangedError(key)
            else:
                temporary = path.with_name(path.name + ".writing")
                temporary.write_bytes(content)
                os.replace(temporary, path)
        return CommittedBlobRef(key, compute_revision(path.stat()), len(content),
                                base64.b64encode(hashlib.sha256(content).digest()).decode(), mime)

    async def issue_upload(self, spec: UploadSpec) -> SignedPutGrant:
        validate_object_key(spec.staging_key)
        expiry = int(time.time()) + spec.expires_in_seconds
        url = sign_local_url(
            self._signing_secret,
            self._public_base_url,
            "PUT",
            spec.staging_key,
            expiry,
            content_type=spec.content_type,
            content_length_min=spec.content_length_min,
            content_length_max=spec.content_length_max,
            checksum_sha256_b64=spec.checksum_sha256_b64,
        )
        headers = {
            "Content-Type": spec.content_type,
            "x-amz-checksum-sha256": spec.checksum_sha256_b64,
        }
        return SignedPutGrant(
            kind="signed_put",
            url=url,
            headers=headers,
            expires_at=datetime.fromtimestamp(expiry, tz=timezone.utc),
        )

    async def inspect_upload(self, ref: StagedBlobRef) -> InspectedUpload:
        return await _run_sync(self._inspect_upload_sync, ref)

    def _inspect_upload_sync(self, ref: StagedBlobRef) -> InspectedUpload:
        path = resolve_key_path(self._root, ref.staging_key)
        try:
            fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW)
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ELOOP, errno.ENOTDIR):
                raise ObjectNotFoundError(ref.staging_key) from exc
            raise
        try:
            st = os.fstat(fd)
            checksum = hashlib.sha256()
            magic_bytes = b""
            while True:
                chunk = os.read(fd, _READ_CHUNK)
                if not chunk:
                    break
                if not magic_bytes:
                    magic_bytes = chunk[:MAGIC_BYTES_READ_LEN]
                checksum.update(chunk)
        finally:
            os.close(fd)
        content_type_declared = self._read_declared_content_type(path)
        return InspectedUpload(
            ref=ref,
            storage_revision=compute_revision(st),
            size_bytes=st.st_size,
            checksum_sha256_b64=base64.b64encode(checksum.digest()).decode("ascii"),
            content_type_declared=content_type_declared,
            magic_bytes=magic_bytes,
        )

    @staticmethod
    def _read_declared_content_type(path: Path) -> str:
        try:
            with open(sidecar_path(path), "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return str(data.get("content_type") or "application/octet-stream")
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return "application/octet-stream"

    async def commit_upload(
        self,
        inspected: InspectedUpload,
        expected: ObjectConstraints,
    ) -> CommittedBlobRef:
        return await _run_sync(self._commit_upload_sync, inspected, expected)

    def _commit_upload_sync(
        self,
        inspected: InspectedUpload,
        expected: ObjectConstraints,
    ) -> CommittedBlobRef:
        staging_key = inspected.ref.staging_key
        committed_key = derive_committed_key(staging_key, expected.expected_magic_mime)
        staging_path = resolve_key_path(self._root, staging_key)
        committed_path = resolve_key_path(self._root, committed_key)
        lock_file = lock_path_for(self._root, staging_key)
        with advisory_lock(lock_file):
            try:
                st = os.stat(staging_path, follow_symlinks=False)
            except FileNotFoundError as exc:
                raise RevisionGoneError(staging_key) from exc
            current_revision = compute_revision(st)
            if current_revision != expected.storage_revision:
                raise ObjectChangedError(staging_key)
            committed_path.parent.mkdir(parents=True, exist_ok=True)
            os.rename(staging_path, committed_path)
            staging_sidecar = sidecar_path(staging_path)
            if staging_sidecar.exists():
                os.replace(staging_sidecar, sidecar_path(committed_path))
        committed_st = os.stat(committed_path, follow_symlinks=False)
        return CommittedBlobRef(
            committed_key=committed_key,
            storage_version_id=compute_revision(committed_st),
            size_bytes=inspected.size_bytes,
            checksum_sha256_b64=inspected.checksum_sha256_b64,
            content_type_verified=expected.expected_magic_mime,
        )

    async def issue_download(self, ref: CommittedBlobRef, spec: DownloadSpec) -> DownloadGrant:
        if not ref.storage_version_id:
            raise ValueError("storage_version_id is required to issue a download grant")
        validate_object_key(ref.committed_key)
        expiry = int(time.time()) + spec.expires_in_seconds
        url = sign_local_url(
            self._signing_secret,
            self._public_base_url,
            "GET",
            ref.committed_key,
            expiry,
            storage_revision=ref.storage_version_id,
        )
        return DownloadGrant(
            url=url,
            expires_at=datetime.fromtimestamp(expiry, tz=timezone.utc),
            content_disposition=content_disposition_header(spec.filename),
        )

    async def delete(self, ref: BlobRef) -> None:
        await _run_sync(self._delete_sync, ref)

    def _delete_sync(self, ref: BlobRef) -> None:
        path = resolve_key_path(self._root, ref.key)
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        try:
            os.remove(sidecar_path(path))
        except FileNotFoundError:
            pass

    async def open_committed(self, ref: CommittedBlobRef) -> AsyncIterator[bytes]:
        """Stream a committed blob's bytes to the caller (e.g. the export Worker).

        Dev-only local path: reads happen directly on the event loop rather
        than via `_run_sync` (streaming a generator through a worker thread
        needs a sync-to-async bridge this dev Adapter doesn't warrant).
        """
        path = resolve_key_path(self._root, ref.committed_key)
        try:
            with open(path, "rb") as f:
                while chunk := f.read(_STREAM_CHUNK):
                    yield chunk
        except FileNotFoundError as exc:
            raise ObjectNotFoundError(f"committed blob not found: {ref.committed_key}") from exc

    async def commit_generated_artifact(
        self, source: Path, spec: GeneratedArtifactSpec
    ) -> CommittedBlobRef:
        return await _run_sync(self._commit_generated_artifact_sync, source, spec)

    def _commit_generated_artifact_sync(
        self, source: Path, spec: GeneratedArtifactSpec
    ) -> CommittedBlobRef:
        validate_object_key(spec.committed_key)
        dest = resolve_key_path(self._root, spec.committed_key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Recovered exports can commit again after losing the first response.
        # Preserve that exact immutable version; never expose a partial copy or
        # replace an already committed key with different bytes.
        with advisory_lock(lock_path_for(self._root, spec.committed_key)):
            if dest.exists():
                with source.open('rb') as original, dest.open('rb') as committed:
                    if hashlib.file_digest(original, 'sha256').digest() != hashlib.file_digest(committed, 'sha256').digest():
                        raise ObjectChangedError(spec.committed_key)
            else:
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(dir=dest.parent, prefix='.generated-', delete=False) as output:
                        temporary = Path(output.name)
                        with source.open('rb') as original:
                            shutil.copyfileobj(original, output)
                    os.replace(temporary, dest)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
            st = os.stat(dest, follow_symlinks=False)
        return CommittedBlobRef(
            committed_key=spec.committed_key,
            storage_version_id=compute_revision(st),
            size_bytes=st.st_size,
            checksum_sha256_b64=spec.expected_sha256_b64,
            content_type_verified=spec.content_type,
        )
