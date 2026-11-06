"""Text translations of a :class:`CreativeRecipe` for image providers.

The recipe (``app.generation.creative_recipe``) is provider-neutral; this
module is where it becomes words. Two translations:

* :func:`compose_text_prompt` -- the text-to-image prompt. It is exactly the
  prompt the Worker composed before the recipe existed (pinned by a golden
  regression test): product name / description / audience / selling points,
  the combination's positive fragments, the dimension values that carry no
  fragments as plain scene attributes, brand constraints, an aspect note and
  an ``Avoid:`` clause for the negative fragments (GPT-Image has no
  negative-prompt parameter).
* :func:`compose_reference_prompt` -- the instruction for a reference-image
  call (OpenAI image edits, Gemini / Seedream with input images): keep the
  product exactly as the reference shows it, naming the confirmed
  must-match items, change only the scene, and output one single continuous
  photograph (no split screen, collage or side-by-side panels -- several
  references otherwise invite a pasted-together picture, Issue #194) --
  followed by the text prompt above, which describes that scene. Every
  reference-capable adapter (OpenAI edits, Gemini, Seedream) sends it.

A direction's Chinese scene description and ad copy stay in the recipe (for
QC, export and audit) and are not sent: the direction's English fragments
already describe its picture, and copy belongs to the ad, not the image.

The final text an adapter sends is recorded on the produced Asset
(``AssetRecord.prompt``) so the Asset Detail page shows exactly what was sent.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.adapters.persistence.models import GenerationRunRecord
from app.generation.creative_recipe import (
    CreativeRecipe,
    ProductBrief,
    build_creative_recipe,
    fragment_dimension_keys,
    load_product_brief,
)
from app.generation.models import RunItemRecord
from app.platform.tenancy import TenantContext

__all__ = [
    "ComposedPrompt",
    "ProductBrief",
    "compose_item_prompt",
    "compose_reference_prompt",
    "compose_text_prompt",
    "load_product_brief",
]

_ASPECT_NOTES = {
    "1:1": "Square composition",
    "4:5": "Portrait composition",
    "9:16": "Tall vertical composition for mobile screens",
    "2:3": "Portrait composition",
    "3:4": "Portrait composition",
    "16:9": "Wide landscape composition",
    "3:2": "Landscape composition",
    "4:3": "Landscape composition",
}


@dataclass(frozen=True)
class ComposedPrompt:
    text: str
    product_version_id: str | None
    positive_fragments: tuple[str, ...] = ()
    negative_fragments: tuple[str, ...] = ()
    dimension_values: dict[str, str] = field(default_factory=dict)


def compose_text_prompt(recipe: CreativeRecipe) -> str:
    """The text-to-image prompt for ``recipe`` (golden-pinned, see module doc)."""
    brief = recipe.product
    parts: list[str] = []
    headline = f"Professional advertising photograph of: {brief.name}"
    if brief.description:
        headline += f" — {brief.description.rstrip('.')}"
    parts.append(headline + ".")
    if brief.audience:
        parts.append(f"Target audience: {brief.audience.rstrip('.')}.")
    if brief.selling_points:
        parts.append("Key selling points: " + "; ".join(p.rstrip(".") for p in brief.selling_points) + ".")
    if recipe.positive_fragments:
        parts.append(". ".join(recipe.positive_fragments) + ".")
    # Dimension values that carry no fragments still shape the image: spell
    # them out as plain attributes (scalar selectors, templates without copy).
    spelled = fragment_dimension_keys(recipe)
    plain_values = [
        f"{value.dimension_id}: {value.value}"
        for value in recipe.dimension_values
        if value.dimension_key not in spelled and value.value
    ]
    if plain_values:
        parts.append("Scene attributes — " + "; ".join(plain_values) + ".")
    if brief.brand_notes:
        parts.append(f"Brand constraints: {brief.brand_notes.rstrip('.')}.")
    aspect_note = _ASPECT_NOTES.get(recipe.format.aspect_ratio or "", "")
    parts.append(
        (f"{aspect_note}, " if aspect_note else "")
        + "the product is the clear hero of the image, commercial quality, "
        "photorealistic, no watermark, no gibberish text."
    )
    if recipe.qc_feedback:
        # Issue #169: what an earlier quality check of this combination found
        # wrong, as corrections. Absent on a first attempt (golden unchanged).
        parts.append(
            "Corrections from the previous quality check: "
            + "; ".join(hint.rstrip(".") for hint in recipe.qc_feedback) + "."
        )
    if recipe.negative_constraints:
        parts.append("Avoid: " + "; ".join(recipe.negative_constraints) + ".")
    return " ".join(parts)


def compose_reference_prompt(recipe: CreativeRecipe, reference_count: int) -> str:
    """Instruction for a call that sends ``reference_count`` product images."""
    name = recipe.product.name or "the product"
    if reference_count > 1:
        lead = (
            f"The {reference_count} attached images are reference photos of the same real product, "
            f"{name}; the first one is the primary view."
        )
    else:
        lead = f"The attached image is a reference photo of the real product, {name}."
    keep = (
        "Keep the product exactly as it appears in the reference: identical shape, proportions, "
        "colors, materials, logo, markings and details. Do not redesign, restyle or replace it."
    )
    parts = [lead, keep]
    if recipe.must_match:
        parts.append(
            "It must match: " + "; ".join(item.description.rstrip(".") for item in recipe.must_match) + "."
        )
    parts.append(
        "Change only the scene around it — setting, props, people, lighting, camera angle and "
        "composition — as described next."
    )
    # Issue #194: several references invite the model to paste them side by
    # side; the output must be one picture (the QC's blocking composite check).
    parts.append(
        "Output one single continuous photograph, no split screen, no collage, no side-by-side "
        "panels, no visible seam, and no frame or border of the reference photos."
    )
    return " ".join(parts) + " " + compose_text_prompt(recipe)


def compose_item_prompt(
    session: Session,
    ctx: TenantContext,
    item: RunItemRecord,
    run: GenerationRunRecord,
) -> ComposedPrompt:
    """The text-to-image prompt for ``item`` of ``run`` (built from its recipe)."""
    recipe = build_creative_recipe(session, ctx, item, run)
    return ComposedPrompt(
        text=compose_text_prompt(recipe),
        product_version_id=recipe.product.version_id,
        positive_fragments=recipe.positive_fragments,
        negative_fragments=recipe.negative_constraints,
        dimension_values={value.dimension_key: value.value for value in recipe.dimension_values},
    )
