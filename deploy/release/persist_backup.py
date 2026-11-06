"""Keep pre-release backups in the deployment's existing private bucket."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import config
from app.platform.blob_store import GeneratedArtifactSpec
from app.platform.blob_store_factory import build_blob_store


async def persist(dump: Path, metadata: Path, store) -> list[dict]:
    if not dump.is_file() or not metadata.is_file():
        raise ValueError("Backup files are missing")
    with dump.open("rb") as stream:
        header = stream.read(5)
    if header != b"PGDMP":
        raise ValueError("Invalid PostgreSQL backup")
    info = json.loads(metadata.read_text())
    if info["database"] not in ("marketai_control", "marketai_domain"):
        raise ValueError("Unexpected backup database")
    results = []
    for path, mime in ((dump, "application/octet-stream"), (metadata, "application/json")):
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").digest()
        leaf = "metadata.json" if path == metadata else "database.dump"
        key = f"committed/platform-release-backups/{dump.stem}/{leaf}"
        ref = await store.commit_generated_artifact(path, GeneratedArtifactSpec(
            key, mime, base64.b64encode(digest).decode(),
        ))
        if ref.checksum_sha256_b64 != base64.b64encode(digest).decode() or ref.size_bytes != path.stat().st_size:
            raise ValueError("Backup storage receipt mismatch")
        results.append({"key": ref.committed_key, "bytes": ref.size_bytes, "sha256": digest.hex()})
    return results


if __name__ == "__main__":
    if config.BLOB_STORE_ADAPTER != "railway_bucket":
        raise SystemExit("Production backup persistence requires the private Railway bucket")
    result = asyncio.run(persist(Path(sys.argv[1]), Path(sys.argv[2]), build_blob_store()))
    print(json.dumps({"private_backup": result}))
