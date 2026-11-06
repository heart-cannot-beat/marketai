"""Typed supplier edits and bounded, deterministic finishing of their results.

The exact source and edit recipe are frozen by Generation. Masks are advice to
the supplier; compositing here is what protects pixels outside the selection.
"""
import hashlib
import io
from typing import Literal

from PIL import Image, ImageDraw, ImageOps
from pydantic import BaseModel, ConfigDict, Field, model_validator, model_serializer

TOOL_VERSION = "supplier-image-tools-v1"
ToolKind = Literal["upscale", "remove_background", "erase"]


class EraseRegion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: float = Field(ge=0, lt=1, allow_inf_nan=False)
    y: float = Field(ge=0, lt=1, allow_inf_nan=False)
    width: float = Field(gt=0, le=1, allow_inf_nan=False)
    height: float = Field(gt=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def within_source(self):
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("选区超出原图")
        return self


class BrushPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: float = Field(ge=0, le=1, allow_inf_nan=False)
    y: float = Field(ge=0, le=1, allow_inf_nan=False)


class EraseStroke(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Diameter as a fraction of the shorter, EXIF-oriented source dimension.
    width: float = Field(ge=.002, le=.5, allow_inf_nan=False)
    points: list[BrushPoint] = Field(min_length=1, max_length=512)


class ImageToolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: ToolKind
    source_width: int = Field(strict=True, ge=1, le=8192)
    source_height: int = Field(strict=True, ge=1, le=8192)
    scale: Literal[2, 4] | None = None
    region: EraseRegion | None = None
    strokes: list[EraseStroke] | None = Field(default=None, min_length=1, max_length=128)
    algorithm: Literal["supplier-image-tools-v1"] = TOOL_VERSION

    @model_serializer(mode="wrap")
    def compatible_optional_strokes(self, handler):
        result = handler(self)
        if self.strokes is None:
            result.pop("strokes", None)
        return result

    @model_validator(mode="after")
    def parameters(self):
        if (self.kind == "upscale") != (self.scale is not None):
            raise ValueError("放大需要选择 2 倍或 4 倍，其他操作不接受放大参数")
        selections = int(self.region is not None) + int(self.strokes is not None)
        if selections != (1 if self.kind == "erase" else 0):
            raise ValueError("局部擦除需要矩形或笔画选区，其他操作不接受选区")
        if self.strokes and sum(len(s.points) for s in self.strokes) > 8192:
            raise ValueError("笔画点数超过上限，请分次编辑")
        factor = self.scale or 1
        if self.source_width * self.source_height * factor * factor > 25_000_000 or max(self.source_width, self.source_height) * factor > 8192:
            raise ValueError("目标图片超过像素上限，请先裁剪或选择较小倍数")
        if self.region:
            region_box(self.region, (self.source_width, self.source_height))
        return self


def supported_tools(provider, model):
    if provider == "mock" and model == "mock-v1":
        return ["upscale", "remove_background", "erase"]
    if provider == "openai" and model in ("gpt-image-1.5", "gpt-image-1", "gpt-image-1-mini", "gpt-image-2"):
        # GPT Image 2 transparency is still in preview; expose stable support.
        return ["upscale", "erase"] if model == "gpt-image-2" else ["upscale", "remove_background", "erase"]
    if provider in ("gemini", "seedream"):
        from app.providers.router import get_provider
        return ["upscale"] if model in get_provider(provider).models else []
    return []


def validate_tool(spec, provider, model):
    value = ImageToolSpec.model_validate(spec)
    if value.kind not in supported_tools(provider, model):
        raise ValueError("该供应商或模型尚不支持这项编辑操作")
    return value


def instruction(spec):
    tool = ImageToolSpec.model_validate(spec)
    return {
        "upscale": "Enhance the original image for enlargement. Preserve its exact composition, product shape, logos, text and claims. Do not invent detail or add objects. Return the same image with improved clarity.",
        "remove_background": "Remove only the background of the original creative. Preserve the foreground product, its geometry, colors, logos and text. Return a PNG with a genuinely transparent background, not a checkerboard or a solid color.",
        "erase": "Remove the object or marks ONLY inside the transparent area of the supplied mask. Reconstruct the background there. Preserve the image composition and do not introduce objects, text or new claims.",
    }[tool.kind]


def decode(content):
    from app.providers.base import validated_reference_inputs
    validated_reference_inputs((content,))
    with Image.open(io.BytesIO(content)) as opened:
        return ImageOps.exif_transpose(opened).convert("RGBA")


def png(image):
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    content = buffer.getvalue()
    if len(content) > 20 * 1024 * 1024:
        raise ValueError("图片超过 20MB")
    return content


def region_box(region, size):
    w, h = size
    box = (round(region.x * w), round(region.y * h), round((region.x + region.width) * w), round((region.y + region.height) * h))
    if box[2] <= box[0] or box[3] <= box[1]:
        raise ValueError("选区小于一个像素")
    return box


def selection_mask(tool, size):
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    if tool.region:
        l, t, r, b = region_box(tool.region, size)
        draw.rectangle((l, t, r - 1, b - 1), fill=255)
    else:
        for stroke in tool.strokes:
            width = max(1, round(stroke.width * min(size)))
            points = [(round(p.x * (size[0] - 1)), round(p.y * (size[1] - 1))) for p in stroke.points]
            draw.line(points, fill=255, width=width, joint="curve")
            radius = (width - 1) / 2
            for x, y in points:
                if width == 1:
                    draw.point((x, y), fill=255)
                else:
                    draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=255)
    return mask


def prepare_input(content, spec):
    tool = ImageToolSpec.model_validate(spec)
    source = decode(content)
    if source.size != (tool.source_width, tool.source_height):
        raise ValueError("原图尺寸不符，请重新载入原图并准备编辑")
    if tool.kind != "erase":
        return content, None
    mask = Image.new("RGBA", source.size, "white")
    mask.putalpha(ImageOps.invert(selection_mask(tool, source.size)))
    # PNG image and PNG alpha mask share exact dimensions and orientation.
    return png(source), png(mask)


class ImageToolOutputError(ValueError):
    pass


def finish(content, output, spec):
    from app.providers.base import InvalidReferenceError
    tool = ImageToolSpec.model_validate(spec)
    try:
        source, generated = decode(content), decode(output)
        if tool.kind == "remove_background":
            low, high = generated.getchannel("A").getextrema()
            if low == 255 or high == 0:
                raise ValueError("去背景结果没有有效透明前景")
        target = (source.width * (tool.scale or 1), source.height * (tool.scale or 1))
        # Providers render at their supported canvas sizes. Record this resize
        # honestly: an enlarged raster does not prove recovered fine detail.
        generated = generated.resize(target, Image.Resampling.LANCZOS)
        if tool.kind == "erase":
            generated = Image.composite(generated, source, selection_mask(tool, source.size))
            generated.putalpha(source.getchannel("A"))
        return png(generated), {"algorithm": TOOL_VERSION, "kind": tool.kind,
            "source_sha256": hashlib.sha256(content).hexdigest(), "provider_output_sha256": hashlib.sha256(output).hexdigest(),
            "width": target[0], "height": target[1], "outside_selection_preserved": tool.kind == "erase"}
    except (ValueError, OSError, InvalidReferenceError) as exc:
        raise ImageToolOutputError("image_tool_output_invalid") from exc


def delivery_metadata(operation, mask):
    return {"image_tool_operation": operation, "image_tool_policy": TOOL_VERSION,
        "edit_mask_sha256": hashlib.sha256(mask).hexdigest() if mask else None}


def validate_request(request, provider):
    from app.providers.base import InvalidReferenceError
    if request.edit_operation is None:
        if request.edit_mask is not None:
            raise InvalidReferenceError("mask_without_edit")
        return
    if request.edit_operation not in supported_tools(provider, request.model) or not request.reference_inputs:
        raise InvalidReferenceError("unsupported_image_tool")
    if (request.edit_operation == "erase") != (request.edit_mask is not None):
        raise InvalidReferenceError("image_tool_mask_mismatch")
    if request.edit_mask:
        try:
            decode(request.edit_mask)
            with Image.open(io.BytesIO(request.reference_inputs[0])) as source, Image.open(io.BytesIO(request.edit_mask)) as mask:
                if source.format != "PNG" or mask.format != "PNG" or source.size != mask.size or "A" not in mask.getbands():
                    raise ValueError("mask_dimensions_or_format")
        except (ValueError, OSError) as exc:
            raise InvalidReferenceError("invalid_edit_mask") from exc
