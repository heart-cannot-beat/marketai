"""Render a #231 research specimen; never writes domain data or calls providers.

Run with Python/Pillow and pass --spec. The editable JSON and the photo it names
are the inputs; PNGs and hash manifests are reproducible derived files.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import platform
import zlib

from PIL import Image, ImageDraw, ImageFont, ImageOps, features, __version__ as pillow_version


def digest(value: bytes) -> str:
    return sha256(value).hexdigest()


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def font(size: int):
    # Pillow's built-in Aileron avoids redistributing an operating-system font.
    return ImageFont.load_default(size=size)


def render(spec_path: Path, output_dir: Path):
    spec = json.loads(spec_path.read_text())
    if spec["schema_version"] != 1 or spec["research_only"] is not True:
        raise ValueError("Only the explicit version-1 research specimen is supported.")
    source_path = spec_path.parent / spec["source"]["file"]
    source_bytes = source_path.read_bytes()
    source_hash = digest(source_bytes)
    if source_hash != spec["source"]["sha256"]:
        raise ValueError("Source photo changed; update its explicit provenance first.")
    output_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(source_path) as opened:
        photo = ImageOps.exif_transpose(opened).convert("RGB")
    records = []
    for layout in spec["layouts"]:
        width, height = layout["size"]
        base = Image.new("RGBA", (width, height), spec["brand_kit"]["background"])
        box = layout["photo_box"]
        x, y, box_width, box_height = box
        if min(box) < 0 or x + box_width > width or y + box_height > height:
            raise ValueError("Photo box exceeds canvas.")
        # Contain preserves the complete connector; never crop/fabricate ports.
        contained = ImageOps.contain(photo, (box_width, box_height), Image.Resampling.LANCZOS)
        base.paste(contained, (x + (box_width - contained.width) // 2,
                              y + (box_height - contained.height) // 2))
        overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        draw.rectangle((64, 54, 112, 60), fill=spec["brand_kit"]["accent"])
        boxes = []
        for layer in layout["layers"]:
            text = spec["copy"][layer["copy_key"]]
            if not text.isascii():
                raise ValueError("This English-only specimen needs a separate shaped-font policy for other scripts.")
            face = font(layer["font_size"])
            position = tuple(layer["position"])
            bounds = draw.textbbox(position, text, font=face)
            if (bounds[0] < 48 or bounds[1] < 48 or bounds[2] > width - 48
                    or bounds[3] > height - 48
                    or bounds[2] - bounds[0] > layer["max_width"]):
                raise ValueError(f"Text overflow in {layout['name']}/{layer['copy_key']}; edit the layout, never clip silently.")
            color = spec["brand_kit"][layer["color"]]
            draw.text(position, text, font=face, fill=color)
            boxes.append({"copy_key": layer["copy_key"], "text": text,
                          "bounds": list(bounds), "font_size": layer["font_size"], "color": color})
        composite = Image.alpha_composite(base, overlay).convert("RGB")
        name = layout["name"]
        final_path, overlay_path = output_dir / f"{name}.png", output_dir / f"{name}-overlay.png"
        composite.save(final_path, optimize=True)
        overlay.save(overlay_path, optimize=True)
        record = {"schema_version": 1, "research_only": True, "layout": name,
                  "ad_language": spec["ad_language"], "size": [width, height],
                  "source_sha256": source_hash, "editable_spec_sha256": digest(canonical(spec)),
                  "layout_sha256": digest(canonical(layout)),
                  "sample_brand_kit_sha256": digest(canonical(spec["brand_kit"])),
                  "font_sha256": digest(font(24).font_bytes), "font_name": list(font(24).getname()),
                  "renderer": {"library": "Pillow", "version": pillow_version,
                               "python": platform.python_version(),
                               "freetype": features.version("freetype2"),
                               "zlib": zlib.ZLIB_VERSION,
                               "policy": "research-pillow-overlay-v1"},
                  "layers": boxes, "overlay_sha256": digest(overlay_path.read_bytes()),
                  "final_sha256": digest(final_path.read_bytes()),
                  "not_a_production_manifest": True}
        (output_dir / f"{name}-manifest.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        records.append(record)
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    spec_path = args.spec.resolve()
    records = render(spec_path, args.output_dir or spec_path.parent / "renders")
    print(json.dumps({"renders": [{"layout": r["layout"], "size": r["size"],
                                  "sha256": r["final_sha256"]} for r in records]}, indent=2))


if __name__ == "__main__":
    main()
