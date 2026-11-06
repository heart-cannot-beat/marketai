"""Constructs the process's `BlobStore` Adapter from configuration (spec §7).

Shared by `app.main` (FastAPI) and `app.worker_runtime` (Worker) -- both
processes need the same Adapter selection logic, and each holds its own
instance rather than sharing one across process boundaries.
"""
from __future__ import annotations

import secrets

from .. import config
from .blob_store import BlobStore


def build_blob_store() -> BlobStore:
    if config.BLOB_STORE_ADAPTER == "s3":
        from ..adapters.blob_store_s3 import S3BlobStore

        return S3BlobStore(bucket=config.BLOB_STORE_S3_BUCKET, region=config.BLOB_STORE_S3_REGION)

    if config.BLOB_STORE_ADAPTER == "railway_bucket":
        from ..adapters.blob_store_railway_bucket import RailwayBucketBlobStore

        return RailwayBucketBlobStore(
            bucket=config.BLOB_STORE_S3_BUCKET,
            endpoint_url=config.BLOB_STORE_S3_ENDPOINT_URL,
            region=config.BLOB_STORE_S3_REGION,
            addressing_style=config.BLOB_STORE_S3_ADDRESSING_STYLE,
        )

    if config.BLOB_STORE_ADAPTER != "local":
        raise ValueError(
            "MARKETAI_BLOB_STORE_ADAPTER must be one of: local, s3, railway_bucket"
        )

    from ..adapters.blob_store_local import LocalFileBlobStore

    # A real deployment always sets MARKETAI_BLOB_STORE_LOCAL_SIGNING_SECRET;
    # local dev/test runs fall back to a random per-process secret so the
    # local adapter -- itself dev-only -- never blocks import on a missing
    # env var. The fallback secret is never persisted or logged.
    secret = (
        config.BLOB_STORE_LOCAL_SIGNING_SECRET.encode("utf-8")
        if config.BLOB_STORE_LOCAL_SIGNING_SECRET
        else secrets.token_bytes(32)
    )
    return LocalFileBlobStore(
        root=config.BLOB_STORE_LOCAL_ROOT,
        public_base_url=config.BLOB_STORE_LOCAL_PUBLIC_URL,
        signing_secret=secret,
    )
