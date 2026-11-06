import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

spec = importlib.util.spec_from_file_location(
    "persist_backup", Path(__file__).resolve().parents[1] / "deploy/release/persist_backup.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def files(tmp_path):
    dump = tmp_path / "domain.dump"
    dump.write_bytes(b"PGDMP" + b"private backup payload")
    meta = tmp_path / "domain.meta.json"
    meta.write_text(json.dumps({"database": "marketai_domain"}))
    return dump, meta


def test_private_persistence_verifies_both_receipts_without_signed_urls(tmp_path):
    dump, meta = files(tmp_path)
    store = AsyncMock()

    async def commit(path, artifact):
        from app.platform.blob_store import validate_object_key
        validate_object_key(artifact.committed_key)
        return SimpleNamespace(committed_key=artifact.committed_key,
            size_bytes=path.stat().st_size, checksum_sha256_b64=artifact.expected_sha256_b64)

    store.commit_generated_artifact.side_effect = commit
    result = asyncio.run(module.persist(dump, meta, store))
    assert len(result) == 2
    assert all(r["key"].startswith("committed/platform-release-backups/") for r in result)
    store.issue_download.assert_not_called()


def test_invalid_dump_is_never_uploaded(tmp_path):
    dump, meta = files(tmp_path)
    dump.write_bytes(b"failed dump")
    store = AsyncMock()
    with pytest.raises(ValueError, match="Invalid PostgreSQL"):
        asyncio.run(module.persist(dump, meta, store))
    store.commit_generated_artifact.assert_not_called()


def test_mismatching_storage_receipt_blocks_release(tmp_path):
    dump, meta = files(tmp_path)
    store = AsyncMock()
    store.commit_generated_artifact.return_value = SimpleNamespace(
        committed_key="wrong", size_bytes=0, checksum_sha256_b64="wrong",
    )
    with pytest.raises(ValueError, match="receipt mismatch"):
        asyncio.run(module.persist(dump, meta, store))
