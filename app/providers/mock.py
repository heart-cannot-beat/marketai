"""Mock provider：不联网，生成带组合信息的占位图。用于没有 key 时跑通全流程。"""
from __future__ import annotations

import asyncio
import hashlib
import io
import random
from pathlib import Path

from PIL import Image, ImageDraw

from .adapter import (
    ProviderCallRequest,
    ProviderCallResult,
    ProviderCapabilities,
    ProviderDispatchError,
)
from .base import ImageProvider

_AR = {"1:1": (1024, 1024), "4:5": (896, 1120), "9:16": (720, 1280), "16:9": (1280, 720)}

_PALETTES = [
    ((36, 44, 92), (108, 92, 231)), ((20, 66, 66), (0, 184, 148)),
    ((70, 38, 24), (225, 112, 85)), ((30, 30, 46), (116, 185, 255)),
    ((52, 31, 71), (232, 67, 147)), ((25, 42, 30), (85, 239, 196)),
]


class MockProvider(ImageProvider):
    name = "mock"
    label = "Mock 占位（免费测试）"
    supports_ref = True
    models = ["mock-v1"]
    default_model = "mock-v1"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            accepts_idempotency_key=True,
            supports_result_lookup_by_request_id=False,
        )

    def available(self) -> bool:
        return True

    async def execute(self, request: ProviderCallRequest) -> ProviderCallResult:
        """Unified adapter entry point for ExternalCallCoordinator dispatch."""
        try:
            from app.generation.image_tools import validate_request, delivery_metadata
            validate_request(request, self.name)
            output = await self.generate(
                prompt=request.prompt,
                ref_images=[],
                aspect_ratio=request.aspect_ratio,
                image_size=request.image_size,
                model=request.model,
            )
            if request.edit_operation == "remove_background":
                # An explicitly simulated transparent fixture, never a cutout.
                with Image.open(io.BytesIO(output)) as opened:
                    image = opened.convert("RGBA")
                    ImageDraw.Draw(image).rectangle((0, 0, image.width // 4, image.height // 4), fill=(0, 0, 0, 0))
                    buffer = io.BytesIO()
                    image.save(buffer, format="PNG")
                    output = buffer.getvalue()
        except Exception as exc:
            raise ProviderDispatchError(
                dispatch_certainty="not_dispatched",
                message=str(exc),
            ) from exc
        return ProviderCallResult(
            output_bytes=output,
            provider_request_id=None,
            idempotency_key_accepted=True,
            metadata=delivery_metadata(request.edit_operation, request.edit_mask) if request.edit_operation else {},
        )

    async def generate(self, prompt: str, ref_images: list[Path],
                       aspect_ratio: str, image_size: str, model: str) -> bytes:
        await asyncio.sleep(random.uniform(0.4, 1.2))  # 模拟延迟
        w, h = _AR.get(aspect_ratio, (1024, 1024))
        seed = int(hashlib.md5(prompt.encode()).hexdigest(), 16)
        c1, c2 = _PALETTES[seed % len(_PALETTES)]

        img = Image.new("RGB", (w, h))
        draw = ImageDraw.Draw(img)
        for y in range(h):  # 渐变底
            t = y / h
            draw.line([(0, y), (w, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(c1, c2)))

        # 中央"产品"占位块
        bw, bh = int(w * 0.42), int(h * 0.30)
        bx, by = (w - bw) // 2, (h - bh) // 2
        draw.rounded_rectangle([bx, by, bx + bw, by + bh], radius=24, fill=(240, 240, 245), outline=(255, 255, 255), width=3)

        # 把 prompt 里的组合标签画上去（取前几行）
        lines = ["MOCK PREVIEW"]
        words, line = prompt.split(), ""
        for wd in words:
            if len(line) + len(wd) > 34:
                lines.append(line)
                line = wd
                if len(lines) > 7:
                    break
            else:
                line = f"{line} {wd}".strip()
        if line and len(lines) <= 7:
            lines.append(line)
        ty = by + bh + 24
        for i, ln in enumerate(lines):
            draw.text((bx, ty + i * 22), ln, fill=(255, 255, 255))

        # Long shared product facts may hide the changed instruction below the
        # visible preview text. Give each full prompt a deterministic identity
        # so a corrected Mock execution cannot collide on its six-colour palette.
        identity = hashlib.sha256(prompt.encode()).hexdigest()[:24]
        draw.text((24, 24), "MOCK " + identity, fill=(255, 255, 255))

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
