"""Google Gemini / Nano Banana 适配器（generateContent，支持参考图）。

The wire format (:func:`build_generate_content_payload`,
:func:`extract_inline_image`) is shared with the governed BYOK adapter
``app.generation.gemini_adapter`` (Issue #167), which sends the recipe's
committed reference images through the same request shape.
"""
from __future__ import annotations

import base64
from pathlib import Path

import httpx

from .. import config
from .adapter import (
    ProviderCallRequest,
    ProviderCallResult,
    ProviderCapabilities,
    ProviderDispatchError,
)
from .base import ImageProvider, ProviderError, ProviderHTTPError, InvalidReferenceError, load_ref_b64_parts, GeneratedImage, reference_delivery_metadata

_AR_OK = {"1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"}
#: Aspect ratios ``imageConfig.aspectRatio`` accepts.
GEMINI_ASPECT_RATIOS = frozenset(_AR_OK)


def build_generate_content_payload(
    prompt: str, images: list[tuple[str, bytes]], aspect_ratio: str, image_size: str,
) -> dict:
    """``generateContent`` body: the prompt, then each input image inline."""
    parts: list[dict] = [{"text": prompt}]
    for mime, data in images:
        parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}})
    image_config: dict = {}
    if aspect_ratio in _AR_OK:
        image_config["aspectRatio"] = aspect_ratio
    if image_size in ("1K", "2K", "4K"):
        image_config["imageSize"] = image_size
    return {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"],
                             **({"imageConfig": image_config} if image_config else {})},
    }


def extract_inline_image(data: dict) -> tuple[bytes, str] | None:
    """The first inline image of a ``generateContent`` answer, if any."""
    for candidate in data.get("candidates") or []:
        for part in ((candidate or {}).get("content") or {}).get("parts") or []:
            inline = part.get("inline_data") or part.get("inlineData")
            if inline and inline.get("data"):
                mime = inline.get("mime_type") or inline.get("mimeType") or "image/png"
                return base64.b64decode(inline["data"]), str(mime)
    return None


def no_image_reason(data: dict) -> str:
    """Why an answer carried no image (block reason, finish reason or text)."""
    reasons: list[str] = []
    feedback = data.get("promptFeedback") or {}
    if isinstance(feedback, dict) and feedback.get("blockReason"):
        reasons.append(f"blocked: {feedback['blockReason']}")
    for candidate in data.get("candidates") or []:
        finish = (candidate or {}).get("finishReason")
        if finish and finish != "STOP":
            reasons.append(f"finish: {finish}")
        for part in ((candidate or {}).get("content") or {}).get("parts") or []:
            if part.get("text"):
                reasons.append(str(part["text"])[:200])
    return "; ".join(reasons)[:300]


class GeminiProvider(ImageProvider):
    name = "gemini"
    label = "Gemini · Nano Banana"
    supports_ref = True
    models = config.GEMINI_IMAGE_MODELS
    default_model = config.GEMINI_IMAGE_MODEL

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            accepts_idempotency_key=False,
            supports_result_lookup_by_request_id=False,
        )

    def available(self) -> bool:
        return bool(config.GEMINI_API_KEY)

    async def execute(self, request: ProviderCallRequest) -> ProviderCallResult:
        """Unified adapter entry point for ExternalCallCoordinator dispatch."""
        # Use BYOK credential when provided, falling back to config key.
        api_key = (
            bytes(request.credential).decode() if request.credential is not None
            else config.GEMINI_API_KEY
        )
        try:
            from app.generation.image_tools import validate_request, delivery_metadata
            validate_request(request, self.name)
            output = await self.generate(
                prompt=request.prompt,
                ref_images=list(request.reference_inputs),
                aspect_ratio=request.aspect_ratio,
                image_size=request.image_size,
                model=request.model,
                api_key=api_key,
            )
        except ProviderError as exc:
            certainty = ("not_dispatched" if isinstance(exc, InvalidReferenceError)
                         else exc.dispatch_certainty if isinstance(exc, ProviderHTTPError)
                         else "may_have_dispatched")
            raise ProviderDispatchError(
                dispatch_certainty=certainty,
                message=str(exc),
            ) from exc
        except (httpx.TimeoutException, httpx.ConnectError, OSError) as exc:
            raise ProviderDispatchError(
                dispatch_certainty="may_have_dispatched",
                message=str(exc),
            ) from exc
        except Exception as exc:
            raise ProviderDispatchError(
                dispatch_certainty="may_have_dispatched",
                message=str(exc),
            ) from exc
        return ProviderCallResult(
            output_bytes=output,
            provider_request_id=getattr(output, "provider_request_id", None),
            idempotency_key_accepted=False,
            metadata={**reference_delivery_metadata(request.reference_inputs),
                **(delivery_metadata(request.edit_operation, request.edit_mask) if request.edit_operation else {})},
        )

    async def generate(self, prompt: str, ref_images: list[Path | bytes],
                       aspect_ratio: str, image_size: str, model: str,
                       api_key: str | None = None) -> bytes:
        payload = build_generate_content_payload(
            prompt, load_ref_b64_parts(ref_images), aspect_ratio, image_size,
        )
        url = f"{config.GEMINI_BASE_URL}/models/{model}:generateContent"
        async with httpx.AsyncClient(timeout=config.GEN_TIMEOUT) as client:
            effective_key = api_key if api_key is not None else config.GEMINI_API_KEY
            r = await client.post(url, json=payload, headers={"x-goog-api-key": effective_key})
        if r.status_code != 200:
            raise ProviderHTTPError("Gemini", r.status_code, r.text)
        data = r.json()
        try:
            for part in data["candidates"][0]["content"]["parts"]:
                inline = part.get("inline_data") or part.get("inlineData")
                if inline and inline.get("data"):
                    return GeneratedImage(base64.b64decode(inline["data"]), r.headers.get("x-request-id") or data.get("responseId"))
        except (KeyError, IndexError) as e:
            raise ProviderError(f"Gemini 响应结构异常: {e} — {str(data)[:300]}")
        # 没有图片，多半是被安全策略拦了，把文本原因带出来
        feedback = data.get("promptFeedback") or ""
        text = ""
        try:
            text = " ".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])[:200]
        except Exception:
            pass
        raise ProviderError(f"Gemini 未返回图片。{('原因: ' + text) if text else ''} {feedback}")
