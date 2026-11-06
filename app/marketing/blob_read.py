"""Bounded reads over the deployed streaming BlobStore seam."""
import base64
import hashlib

async def read_committed(store, ref, maximum):
    if ref.size_bytes > maximum:
        raise ValueError("blob_too_large")
    data = bytearray()
    async for chunk in store.open_committed(ref):
        data.extend(chunk)
        if len(data) > maximum:
            raise ValueError("blob_too_large")
    if len(data) != ref.size_bytes or base64.b64encode(hashlib.sha256(data).digest()).decode() != ref.checksum_sha256_b64:
        raise ValueError("blob_integrity_mismatch")
    return bytes(data)

async def write_generated(store, key, content, mime):
    import tempfile
    from pathlib import Path
    from app.platform.blob_store import GeneratedArtifactSpec
    with tempfile.TemporaryDirectory(prefix="marketai-artifact-") as folder:
        path = Path(folder) / "artifact"
        path.write_bytes(content)
        spec = GeneratedArtifactSpec(key, mime, base64.b64encode(hashlib.sha256(content).digest()).decode())
        return await store.commit_generated_artifact(path, spec)
