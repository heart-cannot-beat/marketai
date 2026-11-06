"""What a BYOK image adapter receives, and the plumbing every one of them shares.

:class:`ByokGenerationRequest` wraps the RunItem's :class:`CreativeRecipe`
-- the only creative input an adapter gets -- with the Run's model binding
and the item's paid-intent fingerprint. Each adapter exposes a pure
``plan(request) -> ProviderCallPlan`` (the endpoint, the exact final prompt
and the reference images it will send) and an ``execute`` that performs
exactly that plan; the Worker records the plan's prompt and references on
the produced AssetVersion, so what is audited is what was sent.

Reference bytes are read at call time through a :class:`ReferenceReader`
bound to the dispatching Workspace: a key outside
``committed/<workspace>/`` is refused before any read, and the bytes must
hash to the SHA-256 the recipe froze. A reference that cannot be read fails
the call as ``not_dispatched`` (nothing was sent, the reservation is
released) instead of silently degrading to a text-only image.
"""
from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

import httpx

from app.generation.controlled_adapter import GenerationOutput
from app.generation.creative_recipe import (
    CreativeRecipe,
    ReferenceImage,
    committed_key_in_workspace,
)
from app.platform.blob_store import BlobStore, CommittedBlobRef
from app.providers.adapter import ProviderDispatchError

#: Largest reference image read into a request (controlled derivatives are
#: far smaller; this only bounds a corrupt or hostile object).
MAX_REFERENCE_BYTES = 20 * 1024 * 1024

#: Response statuses after which no image was generated or billed.
NOT_DISPATCHED_STATUSES = frozenset({400, 401, 403, 404, 405, 409, 413, 415, 422, 429})


@dataclass(frozen=True)
class ByokGenerationRequest:
    """One real image request: the frozen recipe plus the Run's model binding."""

    recipe: CreativeRecipe
    model: str
    semantic_fingerprint: str
    #: Reads the recipe's reference images for this Workspace (set by the
    #: Worker; never part of the request's identity).
    reference_reader: "ReferenceReader | None" = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class ProviderCallPlan:
    """What an adapter will send for a request (pure, recorded as evidence)."""

    endpoint: str
    prompt: str
    references: tuple[ReferenceImage, ...] = ()


class PlanningAdapter(Protocol):
    def plan(self, request: ByokGenerationRequest) -> ProviderCallPlan: ...


class ReferenceReader:
    """Reads committed reference derivatives of one Workspace from the BlobStore."""

    def __init__(self, workspace_id: str, blob_store: BlobStore | None) -> None:
        self._workspace_id = workspace_id
        self._blob_store = blob_store

    async def read(self, reference: ReferenceImage) -> bytes:
        if not committed_key_in_workspace(reference.committed_key, self._workspace_id):
            raise ProviderDispatchError(
                dispatch_certainty="not_dispatched",
                message="a reference image is not a committed object of this Workspace",
                error_code="reference_not_in_workspace",
            )
        if self._blob_store is None:
            raise ProviderDispatchError(
                dispatch_certainty="not_dispatched",
                message="no BlobStore is available to read reference images",
                error_code="reference_unavailable",
            )
        ref = CommittedBlobRef(
            committed_key=reference.committed_key,
            storage_version_id=reference.storage_version_id,
            size_bytes=reference.byte_size,
            checksum_sha256_b64=(
                base64.b64encode(bytes.fromhex(reference.sha256)).decode("ascii")
                if _is_hex_sha256(reference.sha256) else ""
            ),
            content_type_verified=reference.mime_type,
        )
        chunks: list[bytes] = []
        size = 0
        try:
            async for chunk in self._blob_store.open_committed(ref):
                size += len(chunk)
                if size > MAX_REFERENCE_BYTES:
                    raise ProviderDispatchError(
                        dispatch_certainty="not_dispatched",
                        message=f"reference image {reference.reference_version_id} is too large",
                        error_code="reference_unavailable",
                    )
                chunks.append(chunk)
        except ProviderDispatchError:
            raise
        except Exception as exc:  # noqa: BLE001 -- storage failure: nothing was sent
            raise ProviderDispatchError(
                dispatch_certainty="not_dispatched",
                message=f"reference image {reference.reference_version_id} could not be read: "
                        f"{type(exc).__name__}",
                error_code="reference_unavailable",
            ) from exc
        content = b"".join(chunks)
        if reference.sha256 and hashlib.sha256(content).hexdigest() != reference.sha256:
            raise ProviderDispatchError(
                dispatch_certainty="not_dispatched",
                message=f"reference image {reference.reference_version_id} does not match its frozen hash",
                error_code="reference_changed",
            )
        return content


async def load_reference_images(
    request: ByokGenerationRequest, references: tuple[ReferenceImage, ...],
) -> list[tuple[str, bytes]]:
    """``[(mime, bytes)]`` for ``references``, primary first."""
    if not references:
        return []
    if request.reference_reader is None:
        raise ProviderDispatchError(
            dispatch_certainty="not_dispatched",
            message="the call needs reference images but no reader was supplied",
            error_code="reference_unavailable",
        )
    images: list[tuple[str, bytes]] = []
    for reference in references:
        content = await request.reference_reader.read(reference)
        images.append((sniff_mime(content, default=reference.mime_type), content))
    return images


def plan_for(adapter: Any, request: ByokGenerationRequest) -> ProviderCallPlan:
    """The adapter's plan; a plain text prompt for adapters that do not plan."""
    planner = getattr(adapter, "plan", None)
    if callable(planner):
        return planner(request)
    from app.generation.prompting import compose_text_prompt

    return ProviderCallPlan(endpoint="text", prompt=compose_text_prompt(request.recipe))


def require_credential(credential: memoryview | None) -> str:
    if credential is None or len(credential) == 0:
        raise ProviderDispatchError(
            dispatch_certainty="not_dispatched",
            message="no BYOK credential was supplied for the call",
            error_code="credential_unavailable",
        )
    return bytes(credential).decode("utf-8").strip()


def transport_failure(label: str, exc: Exception, timeout: float) -> ProviderDispatchError:
    """A request that left (or may have left) and got no trustworthy answer."""
    if isinstance(exc, httpx.TimeoutException):
        return ProviderDispatchError(
            dispatch_certainty="uncertain",
            message=f"{label} request timed out after {timeout:g}s",
            error_code="provider_timeout",
        )
    return ProviderDispatchError(
        dispatch_certainty="uncertain",
        message=f"{label} request failed in transit: {type(exc).__name__}",
        error_code="provider_unreachable",
    )


def status_failure(label: str, code_prefix: str, response: httpx.Response) -> ProviderDispatchError:
    detail = error_detail(response)
    status = response.status_code
    if status in NOT_DISPATCHED_STATUSES:
        return ProviderDispatchError(
            dispatch_certainty="not_dispatched",
            message=f"{label} {status}: {detail}",
            error_code=(
                "rate_limited" if status == 429
                else "credential_rejected" if status in (401, 403)
                else f"{code_prefix}_{status}"
            ),
        )
    return ProviderDispatchError(
        dispatch_certainty="uncertain",
        message=f"{label} {status}: {detail}",
        error_code=f"{code_prefix}_{status}",
    )


def error_detail(response: httpx.Response) -> str:
    try:
        error = response.json().get("error") or {}
        message = error.get("message") if isinstance(error, dict) else None
    except (ValueError, AttributeError):
        message = None
    text = str(message or response.text or "").strip()
    return text[:300]


def malformed(label: str, what: str) -> ProviderDispatchError:
    return ProviderDispatchError(
        dispatch_certainty="uncertain",
        message=f"{label} returned {what}",
        error_code="malformed_response",
    )


def image_output(provider: str, content: bytes, usage: Mapping[str, Any] | None = None) -> GenerationOutput:
    digest = hashlib.sha256(content).hexdigest()
    mime_type = sniff_mime(content)
    return GenerationOutput(
        object_key=f"generation/{provider}/{digest}.{extension(mime_type)}",
        content_sha256=digest,
        byte_size=len(content),
        mime_type=mime_type,
        usage=dict(usage or {}),
        content_bytes=content,
    )


async def download(url: str, *, transport: httpx.AsyncBaseTransport | None) -> bytes:
    async with httpx.AsyncClient(timeout=60, transport=transport) as client:
        image = await client.get(url)
        image.raise_for_status()
        return image.content


def sniff_mime(content: bytes, default: str = "image/png") -> str:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return default


def extension(mime_type: str) -> str:
    return {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(mime_type, "png")


def _is_hex_sha256(value: str) -> bool:
    if len(value or "") != 64:
        return False
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return True
