"""Archiving a scan's ad media (spec §5.2 media step).

For each card's ``original_image_url`` and ``video_preview_image_url``
(video files are never archived, spec §1) the Worker downloads the signed
link, stores the bytes once per SHA-256 under purpose ``competitor_media``
(``committed/<workspace>/competitor_media/<sha256>.<ext>``, content
addressed, so the same image in two ads is one object) and derives a 512px
JPEG next to it. The observation keeps the link without its signature and
when the signed link expires (``oe=``); the download's hash, size, type and
time are the media step's evidence.

Downloads are free and never touch the provider's API, but they do reach a
host the provider named, so a real link is fetched only over HTTPS from
Meta's CDN (``*.fbcdn.net``), without redirects, within a size limit.
Controlled Mock links are answered by :func:`fetch_mock_media`. A 403 (or
410) is an expired link: the media step may refresh the ad with one paid
``ad_refresh`` call; an ad without quota left is "media unavailable".
"""
from __future__ import annotations

import base64
import hashlib
import io
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit, urlunsplit

import anyio
import httpx
from PIL import Image

from app.competitors.sources.mock import fetch_mock_media
from app.generation.thumbnails import THUMBNAIL_LONG_EDGE, build_thumbnail
from app.platform.blob_store import BlobStore, GeneratedArtifactSpec, extension_for_mime, validate_object_key
from app.providers.llm_types import sniff_image_type

MEDIA_PURPOSE = "competitor_media"
ALLOWED_HOST_SUFFIXES = (".fbcdn.net",)
MAX_MEDIA_BYTES = 10 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True)
class MediaFetch:
    """What a download found: bytes, or the reason there are none (``expired`` asks for a refresh)."""

    content: bytes | None = None
    reason: str | None = None
    http_status: int | None = None


class MediaFetcher(Protocol):
    async def fetch(self, url: str) -> MediaFetch: ...


def unsigned(url: str) -> str:
    """The link without its query (signature, expiry and tracking parameters)."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _status_reason(status: int) -> str:
    if status in (403, 410):
        return "expired"
    if status == 404:
        return "not_found"
    return "fetch_failed"


class HttpMediaFetcher:
    """Meta CDN links over HTTPS only; no redirects, no proxies from the environment."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def fetch(self, url: str) -> MediaFetch:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or not host.endswith(ALLOWED_HOST_SUFFIXES):
            return MediaFetch(reason="host_not_allowed")
        try:
            async with httpx.AsyncClient(
                timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False, transport=self._transport,
            ) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code != 200:
                        return MediaFetch(reason=_status_reason(response.status_code), http_status=response.status_code)
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_MEDIA_BYTES:
                            return MediaFetch(reason="too_large", http_status=200)
                        chunks.append(chunk)
                    return MediaFetch(content=b"".join(chunks), http_status=200)
        except httpx.HTTPError:
            return MediaFetch(reason="fetch_failed")


class MockMediaFetcher:
    """Controlled Mock placeholder links, as of ``clock()`` (an expired ``oe`` answers 403)."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def fetch(self, url: str) -> MediaFetch:
        answer = fetch_mock_media(url, now=self._clock())
        if answer.status != 200 or answer.content is None:
            return MediaFetch(reason=_status_reason(answer.status), http_status=answer.status)
        return MediaFetch(content=answer.content, http_status=200)


def default_fetcher(execution_mode: str) -> MediaFetcher:
    return HttpMediaFetcher() if execution_mode == "byok" else MockMediaFetcher()


def _key(workspace_id: str, sha256: str, mime: str) -> str:
    key = f"committed/{workspace_id}/{MEDIA_PURPOSE}/{sha256}.{extension_for_mime(mime)}"
    validate_object_key(key)
    return key


async def _commit(blob_store: BlobStore, key: str, content: bytes, mime: str) -> Any:
    digest = hashlib.sha256(content).digest()
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / f"media.{extension_for_mime(mime)}"
        source.write_bytes(content)
        return await blob_store.commit_generated_artifact(source, GeneratedArtifactSpec(
            committed_key=key, content_type=mime, expected_sha256_b64=base64.b64encode(digest).decode("ascii"),
        ))


def _dimensions(content: bytes) -> tuple[int | None, int | None]:
    try:
        with Image.open(io.BytesIO(content)) as image:
            return image.size
    except Exception:  # noqa: BLE001 -- dimensions are informative only
        return None, None


async def archive(blob_store: BlobStore, workspace_id: str, content: bytes) -> dict[str, Any]:
    """Store ``content`` and its 512px derivative; the stored refs, or ``{"reason": ...}``."""
    mime = sniff_image_type(content)
    if mime is None:
        return {"reason": "unsupported_type"}
    sha256 = hashlib.sha256(content).hexdigest()
    committed = await _commit(blob_store, _key(workspace_id, sha256, mime), content, mime)
    width, height = _dimensions(content)
    stored: dict[str, Any] = {
        "sha256": sha256, "mime_type": mime, "byte_size": len(content), "width": width, "height": height,
        "object_key": committed.committed_key, "storage_version_id": committed.storage_version_id,
        "thumbnail": None,
    }
    try:
        derivative = await anyio.to_thread.run_sync(partial(build_thumbnail, content, long_edge=THUMBNAIL_LONG_EDGE))
        thumb = await _commit(
            blob_store, _key(workspace_id, derivative.content_sha256, derivative.mime_type),
            derivative.content_bytes, derivative.mime_type,
        )
        stored["thumbnail"] = {
            "object_key": thumb.committed_key, "storage_version_id": thumb.storage_version_id,
            "byte_size": thumb.size_bytes, "sha256_b64": derivative.content_sha256_b64,
            "mime_type": derivative.mime_type, "width": derivative.width, "height": derivative.height,
        }
    except Exception:  # noqa: BLE001 -- the original is stored; the board falls back to a placeholder
        stored["thumbnail"] = None
    return stored


async def signed_derivative(blob_store: BlobStore, ref: Mapping[str, Any]) -> dict[str, Any] | None:
    """A short-lived signed GET of an archived 512px derivative (``archive()['thumbnail']``);
    ``None`` on any failure, so one tile falls back to its placeholder, not the page."""
    from app.generation.thumbnails import ThumbnailRef, issue_thumbnail_link
    from app.platform.blob_store import CommittedBlobRef

    try:
        return await issue_thumbnail_link(blob_store, ThumbnailRef(
            committed=CommittedBlobRef(
                committed_key=ref["object_key"], storage_version_id=ref["storage_version_id"],
                size_bytes=int(ref.get("byte_size") or 0), checksum_sha256_b64=ref.get("sha256_b64") or "",
                content_type_verified=ref.get("mime_type") or "image/jpeg",
            ),
            width=ref.get("width"), height=ref.get("height"),
        ))
    except Exception:  # noqa: BLE001 -- one tile, not the page
        return None
