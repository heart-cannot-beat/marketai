"""Provider 适配器基类：所有生图后端实现统一接口。"""
from __future__ import annotations

import asyncio
import io
import hashlib
from pathlib import Path

from PIL import Image

from .. import config


class ProviderError(Exception):
    """带上下文的生成失败（会记录到 asset.error 展示在 UI）。"""


class InvalidReferenceError(ProviderError):
    """Local reference validation failed before any provider request."""


class ProviderHTTPError(ProviderError):
    """HTTP status is evidence; response prose is never dispatch evidence."""

    def __init__(self, provider: str, status_code: int, detail: str):
        self.status_code = status_code
        super().__init__(f"{provider} {status_code}: {detail[:300]}")

    @property
    def dispatch_certainty(self):
        return "not_dispatched" if self.status_code in (400, 401, 403, 422) else "may_have_dispatched"


class GeneratedImage(bytes):
    def __new__(cls, content, request_id=None):
        result = super().__new__(cls, content)
        result.provider_request_id = request_id
        return result


def reference_delivery_metadata(inputs):
    return {"reference_delivery_policy": "provider-byte-references-v1",
        "reference_input_sha256": [hashlib.sha256(content).hexdigest() for content in inputs]}


class ImageProvider:
    name: str = "base"
    label: str = "Base"
    supports_ref: bool = True
    models: list[str] = []
    default_model: str = ""

    def available(self) -> bool:
        raise NotImplementedError

    async def generate(self, prompt: str, ref_images: list[Path],
                       aspect_ratio: str, image_size: str, model: str) -> bytes:
        """生成一张图，返回图片 bytes（png/jpeg）。失败抛 ProviderError。"""
        raise NotImplementedError

    async def generate_with_retry(self, *args, **kwargs) -> bytes:
        last: Exception | None = None
        for attempt in range(config.GEN_RETRIES + 1):
            try:
                return await self.generate(*args, **kwargs)
            except ProviderError as e:
                last = e
                # 参数类错误不重试（重试也不会好）
                msg = str(e).lower()
                if any(k in msg for k in ("400", "invalid", "policy", "safety", "unauthorized", "401", "403")):
                    raise
                await asyncio.sleep(2 * (attempt + 1))
            except Exception as e:  # 网络抖动等
                last = e
                await asyncio.sleep(2 * (attempt + 1))
        raise ProviderError(f"重试 {config.GEN_RETRIES} 次后仍失败: {last}")


def load_ref_b64_parts(ref_images: list[Path | bytes], max_side: int = config.REF_MAX_SIDE) -> list[tuple[str, bytes]]:
    """读取参考图并压缩到 max_side，返回 [(mime, bytes)]。"""
    if len(ref_images) > 6:
        raise InvalidReferenceError("invalid reference count: maximum 6")
    out = []
    for path in ref_images:
        if isinstance(path, bytes):
            out.extend(validated_reference_inputs((path,)))
            continue
        try:
            img = Image.open(io.BytesIO(path) if isinstance(path, bytes) else path).convert("RGB")
            img.thumbnail((max_side, max_side))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=90)
            out.append(("image/jpeg", buf.getvalue()))
        except Exception as exc:
            raise InvalidReferenceError("invalid reference image") from exc
    return out


def validated_reference_inputs(inputs: tuple[bytes, ...]) -> list[tuple[str, bytes]]:
    """Keep exact source pixels, MIME and alpha; never silently drop an input."""
    if len(inputs) > 6:
        raise InvalidReferenceError("invalid reference count")
    result = []
    for content in inputs:
        try:
            if not content or len(content) > 20 * 1024 * 1024:
                raise ValueError("reference image size out of range")
            with Image.open(io.BytesIO(content)) as image:
                if image.width * image.height > 25_000_000 or getattr(image, "n_frames", 1) != 1:
                    raise ValueError("reference image dimensions out of range")
                mime = Image.MIME.get(image.format)
                if mime not in ("image/png", "image/jpeg", "image/webp"):
                    raise ValueError("reference image format not allowed")
                image.verify()
        except Exception as exc:
            raise InvalidReferenceError("invalid reference image") from exc
        result.append((mime, content))
    return result
