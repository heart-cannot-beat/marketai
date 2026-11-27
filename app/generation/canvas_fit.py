"""Deliver the aspect ratio the user chose when the provider only renders a nearby canvas (#268).

GPT-Image renders 1024x1024, 1024x1536 and 1536x1024, so a 4:5 request is sent (and priced) at
1024x1536, which is 2:3. Meta's feed shows a portrait image up to 4:5 and crops anything taller, so a
headline near the top of a 2:3 picture could be cut there. The Worker therefore tells the model the
frame in the prompt (``note``) and keeps the central 4:5 of what comes back (``fit``); the provider's
own size and the hash of its bytes are recorded as evidence next to the stored image.

Only the pairs in ``RENDERED_CANVAS`` are fitted. Other providers render the ratio they are asked for,
other GPT-Image ratios keep the provider's canvas, and image-tool edits are already resized to their
source by ``image_tools.finish``.
"""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

from PIL import Image

POLICY = "center-crop-v1"

#: (provider, chosen aspect ratio) -> the canvas the provider renders for it (as sent and priced).
RENDERED_CANVAS: dict[tuple[str, str], tuple[int, int]] = {("openai", "4:5"): (1024, 1536)}


@dataclass(frozen=True)
class CanvasFit:
    aspect_ratio: str
    canvas: tuple[int, int]
    size: tuple[int, int]


def _ratio(aspect_ratio: str) -> tuple[int, int]:
    width, height = aspect_ratio.split(":")
    return int(width), int(height)


def _central(size: tuple[int, int], aspect_ratio: str) -> tuple[int, int]:
    """The largest ``aspect_ratio`` area inside ``size``."""
    width, height = size
    rw, rh = _ratio(aspect_ratio)
    if width * rh > height * rw:
        return round(height * rw / rh), height
    return width, round(width * rh / rw)


def plan(provider: str | None, aspect_ratio: str | None, image_tool: dict | None = None) -> CanvasFit | None:
    """The fit a generation needs, or ``None`` when the provider's output is delivered as it comes."""
    if image_tool:
        return None
    canvas = RENDERED_CANVAS.get((provider or "", aspect_ratio or ""))
    if canvas is None:
        return None
    return CanvasFit(aspect_ratio, canvas, _central(canvas, aspect_ratio))


def note(fit: CanvasFit) -> str:
    """The frame the image model is told about, so nothing that matters lands in the cropped edges."""
    (cw, ch), (w, h) = fit.canvas, fit.size
    if ch > h:
        edges = f"{(ch - h) // 2} px at the top and at the bottom"
    else:
        edges = f"{(cw - w) // 2} px at the left and at the right"
    return (f"Frame: the final ad is {fit.aspect_ratio}. This {cw}x{ch} canvas is cropped to its central {w}x{h}, "
            f"removing {edges}, so keep the headline, body text, logo and the whole product inside that central area.")


def fit(content: bytes, planned: CanvasFit) -> tuple[bytes, dict]:
    """Keep the central ``planned.aspect_ratio`` of the provider's image, in the provider's format.

    The crop is computed from the image actually returned, not from the canvas that was asked for;
    an image already in the ratio is kept byte for byte."""
    with Image.open(io.BytesIO(content)) as image:
        image.load()
        source_format = image.format
        provider_size = image.size
        size = _central(provider_size, planned.aspect_ratio)
        if size == provider_size:
            output = content
        else:
            left = (provider_size[0] - size[0]) // 2
            top = (provider_size[1] - size[1]) // 2
            cropped = image.crop((left, top, left + size[0], top + size[1]))
            buffer = io.BytesIO()
            if source_format == "PNG":
                cropped.save(buffer, format="PNG")
            elif source_format in ("JPEG", "WEBP"):
                cropped.save(buffer, format=source_format, quality=95)
            else:
                raise ValueError("unsupported provider output")
            output = buffer.getvalue()
    return output, {"policy": POLICY, "aspect_ratio": planned.aspect_ratio,
                    "provider_width": provider_size[0], "provider_height": provider_size[1],
                    "width": size[0], "height": size[1],
                    "provider_output_sha256": hashlib.sha256(content).hexdigest()}
