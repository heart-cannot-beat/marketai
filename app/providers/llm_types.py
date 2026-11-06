"""Typed request/response values of the governed ``llm_chat`` call semantic.

Callers build an :class:`LlmChatRequest`; ``app.providers.llm_chat`` resolves
it (images loaded, model resolved, token ceiling estimated, digests taken)
into a :class:`ResolvedLlmChatRequest`, which is what an adapter receives;
the adapter answers with an :class:`LlmChatOutput`. Raw image bytes live
only in memory on the resolved request -- evidence keeps their SHA-256.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from app.platform.blob_store import CommittedBlobRef

SYSTEM_ROLE = "system"
USER_ROLE = "user"
_ROLES = (SYSTEM_ROLE, USER_ROLE)

#: Input image types OpenAI vision accepts.
SUPPORTED_IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")


class LlmOutputValidationError(ValueError):
    """An expected domain rejection eligible for the call's bounded retry."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class LlmMessage:
    role: str
    text: str

    def __post_init__(self) -> None:
        if self.role not in _ROLES:
            raise ValueError(f"llm_chat message role must be one of {_ROLES}, got {self.role!r}")


@dataclass(frozen=True)
class LlmImage:
    """An input image: inline bytes, or a committed object in this Workspace's bucket."""

    content: bytes | None = field(default=None, repr=False)
    mime_type: str | None = None
    blob: CommittedBlobRef | None = None

    @classmethod
    def from_bytes(cls, content: bytes, mime_type: str | None = None) -> "LlmImage":
        return cls(content=bytes(content), mime_type=mime_type)

    @classmethod
    def from_blob(cls, ref: CommittedBlobRef) -> "LlmImage":
        return cls(blob=ref, mime_type=ref.content_type_verified or None)


@dataclass(frozen=True)
class LlmChatRequest:
    """What a Worker job asks the model.

    ``semantic`` names the purpose (``product_intake``, ``test_plan_draft``,
    ``asset_tagging``, ``qc``, ...): it is recorded on every attempt and
    selects the controlled mock's fixture. ``json_schema`` is the object
    schema the answer must satisfy (OpenAI strict structured outputs: every
    object lists all its properties as ``required`` and sets
    ``additionalProperties: false``). Images go with the last user message.
    A pure ``output_validator`` may check domain references after schema
    validation, sharing the same bounded retry. Its versioned policy name
    enters the request digest; the callable is never sent or persisted.
    """

    semantic: str
    messages: tuple[LlmMessage, ...]
    json_schema: Mapping[str, Any]
    max_output_tokens: int
    model: str | None = None
    images: tuple[LlmImage, ...] = ()
    schema_name: str = "result"
    output_validation_policy: str | None = None
    output_validator: Callable[[dict], None] | None = field(default=None, repr=False, compare=False)

    @classmethod
    def build(
        cls,
        *,
        semantic: str,
        system: str,
        user: str,
        json_schema: Mapping[str, Any],
        max_output_tokens: int,
        model: str | None = None,
        images: tuple[LlmImage, ...] | list[LlmImage] = (),
        schema_name: str = "result",
        output_validation_policy: str | None = None,
        output_validator: Callable[[dict], None] | None = None,
    ) -> "LlmChatRequest":
        messages = tuple(
            message for message in (
                LlmMessage(SYSTEM_ROLE, system) if system else None,
                LlmMessage(USER_ROLE, user),
            ) if message is not None
        )
        return cls(
            semantic=semantic,
            messages=messages,
            json_schema=json_schema,
            max_output_tokens=max_output_tokens,
            model=model,
            images=tuple(images),
            schema_name=schema_name,
            output_validation_policy=output_validation_policy,
            output_validator=output_validator,
        )


@dataclass(frozen=True)
class ResolvedImage:
    content: bytes = field(repr=False)
    mime_type: str
    sha256: str


@dataclass(frozen=True)
class ResolvedLlmChatRequest:
    """The fully resolved call an adapter executes (never persisted as-is)."""

    semantic: str
    provider: str
    model: str
    messages: tuple[LlmMessage, ...]
    images: tuple[ResolvedImage, ...]
    json_schema: dict
    schema_name: str
    max_output_tokens: int
    estimated_input_tokens: int
    request_digest: str
    redacted_request_digest: str
    output_validation_policy: str | None = None
    output_validator: Callable[[dict], None] | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class LlmChatOutput:
    """A schema-valid answer plus the evidence of how it was obtained."""

    semantic: str
    data: dict
    usage: dict
    model_version: str
    latency_ms: int
    response_digest: str
    http_calls: int


def canonical_digest(value: Any) -> str:
    """SHA-256 of ``value``'s canonical JSON (sorted keys, no whitespace)."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def sniff_image_type(content: bytes) -> str | None:
    """The image MIME type ``content``'s magic bytes say it is, if supported."""
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if content[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None
