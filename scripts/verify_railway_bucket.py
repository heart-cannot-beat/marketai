"""Release-time read/write/delete probe for the configured Railway Bucket.

The probe deliberately emits no URL, header, endpoint, bucket name, or
credential.  A random object is always deleted in ``finally`` and deletion is
idempotent, so a Release retry converges after an interrupted prior run.

When ``MARKETAI_BLOB_STORE_BROWSER_ORIGINS`` is set the probe also converges
the bucket CORS rule those origins need for direct browser transfers
(gallery thumbnails, lightbox, reference uploads) and reads it back; a
bucket that will not retain the rule fails the release.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sys
import urllib.request
import uuid
from datetime import datetime, timezone

from app import config
from app.adapters.blob_store_railway_bucket import RailwayBucketBlobStore
from app.platform.blob_store import (
    BlobRef,
    DownloadSpec,
    ObjectConstraints,
    ObjectNotFoundError,
    StagedBlobRef,
    UploadSpec,
    derive_committed_key,
)
from app.platform.blob_store_factory import build_blob_store


async def verify() -> dict[str, bool]:
    if config.BLOB_STORE_ADAPTER != "railway_bucket":
        raise RuntimeError("release bucket probe requires railway_bucket adapter")

    store = build_blob_store()
    if not isinstance(store, RailwayBucketBlobStore):
        raise RuntimeError("release bucket probe built the wrong adapter")
    now = datetime.now(timezone.utc)
    payload = b"marketai-railway-bucket-release-probe-v1"
    checksum = base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")
    staging_key = (
        "staging/platform/release_probe/"
        f"{now:%Y}/{now:%m}/{now:%d}/{uuid.uuid4().hex}.dat"
    )
    committed_key = derive_committed_key(staging_key, "application/octet-stream")
    try:
        upload = await store.issue_upload(
            UploadSpec(
                staging_key=staging_key,
                content_type="application/octet-stream",
                content_length_min=len(payload),
                content_length_max=len(payload),
                checksum_sha256_b64=checksum,
                expires_in_seconds=60,
            )
        )

        def _upload_with_presigned_url() -> None:
            request = urllib.request.Request(
                upload.url,
                data=payload,
                method="PUT",
                headers=upload.headers,
            )
            with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
                if not 200 <= response.status < 300:
                    raise RuntimeError("presigned upload returned a non-success status")

        await asyncio.to_thread(_upload_with_presigned_url)
        inspected = await store.inspect_upload(StagedBlobRef(staging_key))
        if (
            inspected.size_bytes != len(payload)
            or inspected.checksum_sha256_b64 != checksum
        ):
            raise RuntimeError("presigned upload read-back mismatch")

        ref = await store.commit_upload(
            inspected,
            ObjectConstraints(
                expected_size=len(payload),
                expected_checksum_sha256_b64=checksum,
                expected_magic_mime="application/octet-stream",
                storage_revision=inspected.storage_revision,
            ),
        )

        received = bytearray()
        async for chunk in store.open_committed(ref):
            received.extend(chunk)
        if bytes(received) != payload:
            raise RuntimeError("Railway Bucket read-back mismatch")

        # URL generation is intentionally exercised but the bearer URL is
        # neither printed nor persisted.
        grant = await store.issue_download(
            ref, DownloadSpec(filename="probe.bin", expires_in_seconds=60)
        )
        if not grant.url or grant.expires_at <= datetime.now(timezone.utc):
            raise RuntimeError("Railway Bucket presign failed")

        await store.delete(BlobRef(key=committed_key))
        try:
            async for _ in store.open_committed(ref):
                pass
        except ObjectNotFoundError:
            pass
        else:
            raise RuntimeError("Railway Bucket delete verification failed")
        capabilities = await store.probe_optional_capabilities(uuid.uuid4().hex)
        if config.BLOB_STORE_BROWSER_ORIGINS:
            await store.converge_browser_cors(config.BLOB_STORE_BROWSER_ORIGINS)
        return capabilities
    finally:
        # Covers failures after PutObject and is safe when the object is
        # already absent.
        try:
            await store.delete(BlobRef(key=committed_key))
        except Exception:
            pass
        try:
            await store.delete(BlobRef(key=staging_key))
        except Exception:
            pass


def main() -> int:
    try:
        capabilities = asyncio.run(verify())
    except Exception as exc:
        # Do not stringify unknown SDK/transport exceptions: they may contain
        # endpoint URLs or request details.  Adapter BlobStoreError messages
        # are already deliberately redacted, but a type is sufficient here.
        print(
            f"FATAL: Railway Bucket verification failed ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "railway_bucket": "connectivity_verified",
                "optional_capabilities": capabilities,
                "browser_cors": (
                    {"converged_origins": len(config.BLOB_STORE_BROWSER_ORIGINS)}
                    if config.BLOB_STORE_BROWSER_ORIGINS
                    else "skipped (MARKETAI_BLOB_STORE_BROWSER_ORIGINS unset)"
                ),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
