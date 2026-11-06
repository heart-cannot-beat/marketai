"""Gallery / Run-tile thumbnails for generated AssetVersions (Issue #159).

A generated original is the only truth about an AssetVersion's pixels: a
GPT-Image PNG is 1.2-1.3 MB and the gallery used to load 24 of them per page
(known gap "W"), while the Run page tiles showed no image at all. This module
derives a small, cheap-to-transfer **thumbnail** from the bytes the Worker
commits -- a JPEG whose long edge is at most :data:`THUMBNAIL_LONG_EDGE`
pixels, EXIF-free, alpha flattened onto white -- and commits it through the
same ``BlobStore`` contract as the original, under its own content-addressed
key and with its own verified checksum.

Rules this module enforces for every caller (the mock Worker path, the BYOK
evidence recorder and the backfill):

* ``GeneratedArtifactSpec.expected_sha256_b64`` is the **base64** digest of
  the exact bytes handed over. The production Railway Bucket Adapter
  re-hashes the file and refuses a mismatch; the hex digest that was passed
  here once failed every production Mock item (known gap "H"). The record
  keeps the hex digest (``thumbnail_content_sha256``) like every other
  ``content_sha256`` column.
* The thumbnail is a derivative, never a version: a failure to decode the
  original or to commit the derivative is logged and leaves the
  ``thumbnail_*`` columns null (:func:`commit_thumbnail_best_effort`). The
  RunItem, its AssetVersion and the original's commit are never failed by
  it, and readers fall back to the original whenever the ref is null.
* Keys are content-addressed by the thumbnail's own digest
  (``committed/<workspace>/generation_thumbnail/<sha256>.jpg``), so a retry
  that re-derives identical bytes re-commits the identical object and a
  re-encode that differs (another Pillow build) lands under a new key rather
  than tripping the bucket's "committed key already contains different
  bytes" guard.

The read side (:func:`thumbnail_refs_for_versions`,
:func:`attach_thumbnail_links`) turns the stored ref pair back into a
short-lived signed GET the list projections embed per item, so a page of
tiles costs one list request plus one small GET per tile instead of one
download-intent round trip plus one full-size GET per tile. Originals keep
the download-intent path (Asset Detail, Lightbox, export).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
import tempfile
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Iterable, Mapping

import anyio
from PIL import Image, ImageOps
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.persistence.models import AssetVersionRecord
from app.platform.blob_store import (
    BlobStore,
    CommittedBlobRef,
    DownloadSpec,
    GeneratedArtifactSpec,
    extension_for_mime,
    validate_object_key,
)
from app.platform.tenancy import TenantContext

logger = logging.getLogger(__name__)

__all__ = [
    "THUMBNAIL_LINK_TTL_SECONDS",
    "THUMBNAIL_LONG_EDGE",
    "THUMBNAIL_MIME_TYPE",
    "CommittedThumbnail",
    "ThumbnailDerivative",
    "ThumbnailRef",
    "apply_thumbnail",
    "attach_thumbnail_links",
    "build_thumbnail",
    "commit_thumbnail",
    "commit_thumbnail_best_effort",
    "issue_thumbnail_link",
    "thumbnail_committed_key",
    "thumbnail_ref",
    "thumbnail_refs_for_versions",
]

#: Long-edge bound of a thumbnail. 512px covers a 180-260px gallery tile on a
#: 2x display; a 1024x1024 GPT-Image PNG shrinks from ~1.25 MB to ~40 KB.
THUMBNAIL_LONG_EDGE = 512
THUMBNAIL_MIME_TYPE = "image/jpeg"
THUMBNAIL_JPEG_QUALITY = 82
#: Lifetime of the signed thumbnail links a list projection embeds. They are
#: consumed as soon as a tile mounts, so this only has to outlive a page's
#: realistic lifetime while staying a short-lived bearer capability; a tile
#: whose link has lapsed falls back to the download-intent path for the
#: original rather than showing a broken image.
THUMBNAIL_LINK_TTL_SECONDS = 900


@dataclass(frozen=True)
class ThumbnailDerivative:
    """The encoded thumbnail plus both digest forms the storage seam needs."""

    content_bytes: bytes
    mime_type: str
    width: int
    height: int
    #: Hex digest -- what the ``thumbnail_content_sha256`` column stores.
    content_sha256: str
    #: Base64 digest -- what ``GeneratedArtifactSpec.expected_sha256_b64`` verifies.
    content_sha256_b64: str


@dataclass(frozen=True)
class CommittedThumbnail:
    """A thumbnail that is in the blob store, ready to be recorded on a version."""

    committed: CommittedBlobRef
    content_sha256: str
    width: int
    height: int

    @property
    def mime_type(self) -> str:
        return self.committed.content_type_verified


@dataclass(frozen=True)
class ThumbnailRef:
    """The stored ref pair of a version's thumbnail, for issuing a signed GET."""

    committed: CommittedBlobRef
    width: int | None
    height: int | None


# --------------------------------------------------------------------------
# Derivation
# --------------------------------------------------------------------------


def build_thumbnail(
    source_bytes: bytes, *, long_edge: int = THUMBNAIL_LONG_EDGE
) -> ThumbnailDerivative:
    """Decode ``source_bytes`` and encode a JPEG whose long edge is at most ``long_edge``.

    ``Image.thumbnail`` never upscales, so the Worker's 1x1 controlled-Mock
    PNG becomes a 1x1 JPEG rather than a blurry 512px square -- the
    derivative always carries the original's aspect ratio. EXIF orientation
    is applied before resizing and no metadata is written back; transparent
    sources are flattened onto white because JPEG has no alpha channel.

    Raises whatever Pillow raises on bytes it cannot decode
    (``UnidentifiedImageError``, ``OSError``, ``DecompressionBombError`` ...);
    :func:`commit_thumbnail_best_effort` is the layer that turns that into
    "no thumbnail".
    """
    if long_edge < 1:
        raise ValueError("long_edge must be at least 1 pixel")
    with Image.open(io.BytesIO(source_bytes)) as decoded:
        decoded.load()
        oriented = ImageOps.exif_transpose(decoded) or decoded
        flattened = _flatten_to_rgb(oriented)
        flattened.thumbnail((long_edge, long_edge), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        # No ``exif=`` argument: Pillow does not carry the source metadata over.
        flattened.save(
            buffer,
            format="JPEG",
            quality=THUMBNAIL_JPEG_QUALITY,
            optimize=True,
            progressive=True,
        )
        width, height = flattened.size
    content = buffer.getvalue()
    digest = hashlib.sha256(content)
    return ThumbnailDerivative(
        content_bytes=content,
        mime_type=THUMBNAIL_MIME_TYPE,
        width=width,
        height=height,
        content_sha256=digest.hexdigest(),
        content_sha256_b64=base64.b64encode(digest.digest()).decode("ascii"),
    )


def _flatten_to_rgb(image: Image.Image) -> Image.Image:
    has_alpha = image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in image.info
    )
    if has_alpha:
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    if image.mode != "RGB":
        return image.convert("RGB")
    return image


def thumbnail_committed_key(workspace_id: str, content_sha256: str, mime_type: str) -> str:
    """Content-addressed committed key for a thumbnail (spec §7.13 layout)."""
    key = (
        f"committed/{workspace_id}/generation_thumbnail/"
        f"{content_sha256}.{extension_for_mime(mime_type)}"
    )
    validate_object_key(key)
    return key


def _b64_from_hex(hex_digest: str) -> str:
    return base64.b64encode(bytes.fromhex(hex_digest)).decode("ascii")


# --------------------------------------------------------------------------
# Commit
# --------------------------------------------------------------------------


async def commit_thumbnail(
    blob_store: BlobStore,
    workspace_id: str,
    source_bytes: bytes,
    *,
    long_edge: int = THUMBNAIL_LONG_EDGE,
) -> CommittedThumbnail:
    """Derive the thumbnail and commit it with its own verified checksum.

    Decoding/encoding is CPU work and runs off the event loop. Raises on an
    undecodable source or a storage failure -- callers that must never fail
    their own unit of work use :func:`commit_thumbnail_best_effort`.
    """
    derivative = await anyio.to_thread.run_sync(
        partial(build_thumbnail, source_bytes, long_edge=long_edge)
    )
    committed_key = thumbnail_committed_key(
        workspace_id, derivative.content_sha256, derivative.mime_type
    )
    with tempfile.TemporaryDirectory() as temp_dir:
        source = Path(temp_dir) / f"thumbnail.{extension_for_mime(derivative.mime_type)}"
        source.write_bytes(derivative.content_bytes)
        committed = await blob_store.commit_generated_artifact(
            source,
            GeneratedArtifactSpec(
                committed_key=committed_key,
                content_type=derivative.mime_type,
                expected_sha256_b64=derivative.content_sha256_b64,
            ),
        )
    return CommittedThumbnail(
        committed=committed,
        content_sha256=derivative.content_sha256,
        width=derivative.width,
        height=derivative.height,
    )


async def commit_thumbnail_best_effort(
    blob_store: BlobStore | None,
    workspace_id: str,
    source_bytes: bytes | None,
    *,
    log_context: Mapping[str, Any] | None = None,
) -> CommittedThumbnail | None:
    """:func:`commit_thumbnail`, but a failure yields ``None`` instead of raising.

    The original is already committed (or about to be) and is the truth; a
    missing derivative only costs the gallery a slower tile. Every failure is
    logged with its type and the caller's context (RunItem / AssetVersion
    ids, never a signed URL) so an operator can spot a systematic problem --
    a Pillow build without JPEG support, a bucket refusing commits -- and run
    the backfill once it is fixed.
    """
    if blob_store is None or not source_bytes:
        return None
    try:
        return await commit_thumbnail(blob_store, workspace_id, source_bytes)
    except Exception as exc:  # noqa: BLE001 -- a derivative must never fail its original
        logger.warning(
            "asset_thumbnail_unavailable",
            extra={
                "workspace_id": workspace_id,
                "error_type": type(exc).__name__,
                "error": str(exc)[:300],
                **dict(log_context or {}),
            },
        )
        return None


def apply_thumbnail(version: AssetVersionRecord, thumbnail: CommittedThumbnail | None) -> None:
    """Record a committed thumbnail on its AssetVersion; a ``None`` leaves the columns alone."""
    if thumbnail is None:
        return
    version.thumbnail_object_key = thumbnail.committed.committed_key
    version.thumbnail_storage_version_id = thumbnail.committed.storage_version_id
    version.thumbnail_content_sha256 = thumbnail.content_sha256
    version.thumbnail_byte_size = thumbnail.committed.size_bytes
    version.thumbnail_mime_type = thumbnail.mime_type
    version.thumbnail_width = thumbnail.width
    version.thumbnail_height = thumbnail.height


# --------------------------------------------------------------------------
# Read side: refs and signed links for list projections
# --------------------------------------------------------------------------


def thumbnail_ref(version: AssetVersionRecord) -> ThumbnailRef | None:
    """The version's thumbnail ref, or ``None`` when it has no complete ref pair.

    Mirrors ``asset_queries.resolve_asset_blob_ref``'s rule for originals:
    both the key and the storage version must be present, or there is
    nothing a signed capability may be issued against.
    """
    if not version.thumbnail_object_key or not version.thumbnail_storage_version_id:
        return None
    checksum_b64 = ""
    if version.thumbnail_content_sha256:
        try:
            checksum_b64 = _b64_from_hex(version.thumbnail_content_sha256)
        except ValueError:
            checksum_b64 = ""
    return ThumbnailRef(
        committed=CommittedBlobRef(
            committed_key=version.thumbnail_object_key,
            storage_version_id=version.thumbnail_storage_version_id,
            size_bytes=version.thumbnail_byte_size or 0,
            checksum_sha256_b64=checksum_b64,
            content_type_verified=version.thumbnail_mime_type or THUMBNAIL_MIME_TYPE,
        ),
        width=version.thumbnail_width,
        height=version.thumbnail_height,
    )


def thumbnail_refs_for_versions(
    session: Session, ctx: TenantContext, version_ids: Iterable[str | None]
) -> dict[str, ThumbnailRef]:
    """Batch lookup of thumbnail refs for one page of versions, Workspace-scoped.

    A version id from another Workspace (or an unknown one) is simply absent
    from the result -- the caller's item keeps ``thumbnail: null`` -- so a
    crafted id can never surface a foreign thumbnail link.
    """
    ids = [version_id for version_id in dict.fromkeys(version_ids) if version_id]
    if not ids:
        return {}
    rows = session.execute(
        select(AssetVersionRecord).where(
            AssetVersionRecord.workspace_id == ctx.workspace_id,
            AssetVersionRecord.id.in_(ids),
        )
    ).scalars()
    refs: dict[str, ThumbnailRef] = {}
    for row in rows:
        ref = thumbnail_ref(row)
        if ref is not None:
            refs[row.id] = ref
    return refs


async def issue_thumbnail_link(
    blob_store: BlobStore,
    ref: ThumbnailRef,
    *,
    ttl_seconds: int = THUMBNAIL_LINK_TTL_SECONDS,
) -> dict[str, Any]:
    """A short-lived signed GET for one thumbnail, shaped as ``AssetThumbnailLink``."""
    extension = extension_for_mime(ref.committed.content_type_verified)
    grant = await blob_store.issue_download(
        ref.committed,
        DownloadSpec(filename=f"thumbnail.{extension}", expires_in_seconds=ttl_seconds),
    )
    return {
        "url": grant.url,
        "expires_at": grant.expires_at.isoformat(),
        "mime_type": ref.committed.content_type_verified,
        "width": ref.width,
        "height": ref.height,
    }


async def attach_thumbnail_links(
    blob_store: BlobStore,
    items: list[dict[str, Any]],
    refs: Mapping[str, ThumbnailRef],
    *,
    version_id_key: str,
    field: str = "thumbnail",
) -> None:
    """Set ``item[field]`` to a signed link for every item whose version has a thumbnail.

    Items without a ref (legacy rows, failed derivatives, run items that have
    produced nothing yet) get ``None`` and the browser falls back to the
    original. Signing is per item and best-effort: one adapter error nulls
    that one tile instead of failing the whole page.
    """

    async def _attach(item: dict[str, Any]) -> None:
        version_id = item.get(version_id_key)
        ref = refs.get(version_id) if isinstance(version_id, str) else None
        if ref is None:
            item[field] = None
            return
        try:
            item[field] = await issue_thumbnail_link(blob_store, ref)
        except Exception as exc:  # noqa: BLE001 -- one tile, not the page
            logger.warning(
                "asset_thumbnail_link_unavailable",
                extra={
                    "asset_version_id": version_id,
                    "object_key": ref.committed.committed_key,
                    "error_type": type(exc).__name__,
                },
            )
            item[field] = None

    await asyncio.gather(*(_attach(item) for item in items))
