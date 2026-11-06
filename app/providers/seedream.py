"""即梦 Seedream（火山引擎方舟 Ark）适配器，支持参考图。

The wire format (:func:`seedream_size`, :func:`build_seedream_payload`) is
shared with the governed BYOK adapter ``app.generation.seedream_adapter``
(Issue #167).
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

# Seedream 4.x 官方推荐尺寸（按比例）
_AR_SIZE_2K = {"1:1": "2048x2048", "4:5": "1728x2160", "3:4": "1728x2304",
               "9:16": "1440x2560", "16:9": "2560x1440", "2:3": "1664x2496", "3:2": "2496x1664"}
_AR_SIZE_1K = {"1:1": "1328x1328", "4:5": "1088x1360", "3:4": "1104x1472",
               "9:16": "936x1664", "16:9": "1664x936", "2:3": "1056x1584", "3:2": "1584x1056"}
SEEDREAM_SIZES_2K = dict(_AR_SIZE_2K)
SEEDREAM_SIZES_1K = dict(_AR_SIZE_1K)
#: Models that only render at 2K and above (Seedream 4.5's minimum output is
#: ~3.7 MP, so the 1K table would be refused; UNVERIFIED against Ark docs).
SEEDREAM_2K_ONLY_MODELS = frozenset({"doubao-seedream-4-5-251128"})


def seedream_size(model: str, aspect_ratio: str, image_size: str) -> str:
    """The ``WxH`` Seedream renders for this ratio and size tier."""
    two_k = image_size in ("2K", "4K") or model in SEEDREAM_2K_ONLY_MODELS
    table = _AR_SIZE_2K if two_k else _AR_SIZE_1K
    return table.get(aspect_ratio, table["1:1"])


def build_seedream_payload(model: str, prompt: str, images: list[tuple[str, bytes]], size: str) -> dict:
    """``/images/generations`` body; input images travel as data URLs."""
    payload: dict = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "response_format": "b64_json",
        "watermark": False,
        "sequential_image_generation": "disabled",
    }
    if images:
        urls = [f"data:{mime};base64,{base64.b64encode(data).decode()}" for mime, data in images]
        payload["image"] = urls if len(urls) > 1 else urls[0]
    return payload


class SeedreamProvider(ImageProvider):
    name = "seedream"
    label = "即梦 Seedream（火山方舟）"
    supports_ref = True
    models = config.SEEDREAM_MODELS
    default_model = config.SEEDREAM_MODEL

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            accepts_idempotency_key=False,
            supports_result_lookup_by_request_id=False,
        )

    def available(self) -> bool:
        return bool(config.ARK_API_KEY)

    async def execute(self, request: ProviderCallRequest) -> ProviderCallResult:
        """Unified adapter entry point for ExternalCallCoordinator dispatch."""
        # Use BYOK credential when provided, falling back to config key.
        api_key = (
            bytes(request.credential).decode() if request.credential is not None
            else config.ARK_API_KEY
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
        size = seedream_size(model, aspect_ratio, image_size)
        payload = build_seedream_payload(model, prompt, load_ref_b64_parts(ref_images), size)

        async with httpx.AsyncClient(timeout=config.GEN_TIMEOUT) as client:
            r = await client.post(f"{config.ARK_BASE_URL}/images/generations",
                                  json=payload,
                                  headers={"Authorization": f"Bearer {api_key if api_key is not None else config.ARK_API_KEY}"})
        if r.status_code != 200:
            raise ProviderHTTPError("Seedream", r.status_code, r.text)
        data = r.json()
        try:
            item = data["data"][0]
        except (KeyError, IndexError):
            raise ProviderError(f"Seedream 响应结构异常: {str(data)[:300]}")
        if item.get("b64_json"):
            return GeneratedImage(base64.b64decode(item["b64_json"]), r.headers.get("x-request-id"))
        if item.get("url"):
            async with httpx.AsyncClient(timeout=60) as client:
                img = await client.get(item["url"])
                img.raise_for_status()
                return GeneratedImage(img.content, r.headers.get("x-request-id"))
        raise ProviderError("Seedream 未返回图片数据")
