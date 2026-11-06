"""OpenAI GPT-Image 适配器：无参考图走 /images/generations，有参考图走 /images/edits。"""
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

# GPT-Image 只有三档尺寸，按最近比例映射
_AR_SIZE = {"1:1": "1024x1024", "4:5": "1024x1536", "9:16": "1024x1536",
            "16:9": "1536x1024", "3:2": "1536x1024", "2:3": "1024x1536"}
_QUALITY = {"1K": "medium", "2K": "high", "4K": "high"}


class OpenAIImageProvider(ImageProvider):
    name = "openai"
    label = "OpenAI · GPT-Image"
    supports_ref = True
    models = config.OPENAI_IMAGE_MODELS
    default_model = config.OPENAI_IMAGE_MODEL

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            accepts_idempotency_key=True,
            supports_result_lookup_by_request_id=False,
        )

    def available(self) -> bool:
        return bool(config.OPENAI_API_KEY)

    async def execute(self, request: ProviderCallRequest) -> ProviderCallResult:
        """Unified adapter entry point for ExternalCallCoordinator dispatch."""
        # Use BYOK credential when provided, falling back to config key.
        api_key = (
            bytes(request.credential).decode() if request.credential is not None
            else config.OPENAI_API_KEY
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
                **({"edit_operation": request.edit_operation, "edit_mask": request.edit_mask} if request.edit_operation else {}),
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
            idempotency_key_accepted=True,
            metadata={**reference_delivery_metadata(request.reference_inputs),
                **(delivery_metadata(request.edit_operation, request.edit_mask) if request.edit_operation else {})},
        )

    async def generate(self, prompt: str, ref_images: list[Path | bytes],
                       aspect_ratio: str, image_size: str, model: str,
                       api_key: str | None = None, *, edit_operation: str | None = None, edit_mask: bytes | None = None) -> bytes:
        size = _AR_SIZE.get(aspect_ratio, "1024x1024")
        quality = _QUALITY.get(image_size, "medium")
        effective_key = api_key if api_key is not None else config.OPENAI_API_KEY
        headers = {"Authorization": f"Bearer {effective_key}"}
        refs = load_ref_b64_parts(ref_images)

        async with httpx.AsyncClient(timeout=config.GEN_TIMEOUT) as client:
            if refs:
                files = [("image[]", (f"ref{i}.{mime.split('/')[-1]}", data, mime)) for i, (mime, data) in enumerate(refs)]
                form = {"model": model, "prompt": prompt, "size": size,
                        "quality": quality, "n": "1"}
                if model in ("gpt-image-1", "gpt-image-1.5"):
                    form["input_fidelity"] = "high"
                if edit_operation:
                    form["output_format"] = "png"
                if edit_operation == "remove_background":
                    form["background"] = "transparent"
                if edit_mask is not None:
                    files.append(("mask", ("mask.png", edit_mask, "image/png")))
                r = await client.post(f"{config.OPENAI_BASE_URL}/images/edits",
                                      headers=headers, data=form, files=files)
            else:
                payload = {"model": model, "prompt": prompt, "size": size, "quality": quality, "n": 1}
                r = await client.post(f"{config.OPENAI_BASE_URL}/images/generations",
                                      headers=headers, json=payload)

        if r.status_code != 200:
            raise ProviderHTTPError("OpenAI", r.status_code, r.text)
        data = r.json()
        try:
            item = data["data"][0]
        except (KeyError, IndexError):
            raise ProviderError(f"OpenAI 响应结构异常: {str(data)[:300]}")
        if item.get("b64_json"):
            return GeneratedImage(base64.b64decode(item["b64_json"]), r.headers.get("x-request-id"))
        if item.get("url"):
            async with httpx.AsyncClient(timeout=60) as client:
                img = await client.get(item["url"])
                img.raise_for_status()
                return GeneratedImage(img.content, r.headers.get("x-request-id"))
        raise ProviderError("OpenAI 未返回图片数据")
