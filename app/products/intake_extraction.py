"""The ``product_intake`` extraction: schema, prompt, mock fixture, normalization.

Issue #161 step 2. The Worker asks the governed ``llm_chat`` primitive
(``app.providers.llm_chat``) for one JSON object with a fixed schema --
``name, description, model_variants[{label, specs, appearance, image_ids}],
specs, selling_points, audiences[{who, why, pain}], visual_must_match,
image_ranking`` plus ``is_product`` -- reading the page digest and/or the
user's text and the first :data:`MAX_LLM_IMAGES` image candidates (vision).

Variants (Issue #192) are model/size/power variants *and* colour or
appearance editions of the same model. Each carries its own appearance
(colour, material, pattern: the must-match items that differ between
variants) and the candidate images that show it; an image no variant claims,
or that several claim, is "general" (it shows the shared design). Only the
variant the user picks enters the ProductVersion (front-flow contract §3.1),
with its appearance and its own plus the general images.

The schema stays inside the subset OpenAI strict structured outputs accept
(no length/count bounds; every property required; no open objects), so
``specs`` travel as ``[{name, value}]`` pairs and become the content's
``{name: value}`` map here. Bounds, de-duplication and the rule that price,
currency, offers and landing pages never enter a ProductVersion are applied
by :func:`normalize_extraction` -- a structurally valid answer that is still
unusable (no name, no selling point, no audience) is a *schema failure* of
this step: the intake ends ``extraction_failed`` and keeps the raw answer.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from app.library.contracts import (
    PRODUCT_AUDIENCE_FIELD_MAX,
    PRODUCT_SPEC_KEY_MAX,
    PRODUCT_SPEC_VALUE_MAX,
    PRODUCT_SPECS_MAX_ENTRIES,
    PRODUCT_VARIANTS_MAX,
    is_commercial_spec_key,
)
from app.providers.llm_mock import register_llm_mock_fixture, synthesize_from_schema
from app.providers.llm_registry import estimate_input_tokens
from app.providers.llm_types import ResolvedLlmChatRequest

SEMANTIC = "product_intake"
SCHEMA_NAME = "product_card"
CALL_NAME = "extract"

#: Images sent to the model with the text (the first N ranked candidates).
MAX_LLM_IMAGES = 6
#: The user prompt is cut to this many UTF-8 bytes, so the reservation made
#: at admission (before the page is known) always covers the real request.
MAX_USER_PROMPT_BYTES = 24_000
MAX_USER_TEXT_CHARS = 8_000
#: Raised from 4 000 for Issue #192: every variant now also carries its
#: appearance and image ids, which a colour-rich page multiplies.
MAX_OUTPUT_TOKENS = 6_000

MAX_SELLING_POINTS = 6
MAX_AUDIENCES = 3
MAX_MUST_MATCH = 10
#: Appearance items one variant may carry.
MAX_VARIANT_APPEARANCE = 6
#: A chosen variant's must-match list (shared items + its appearance) is
#: bounded like the confirm request's ``must_match`` edit.
MAX_COMPOSED_MUST_MATCH = 20
MAX_NAME_CHARS = 200
MAX_DESCRIPTION_CHARS = 2_000
MAX_SELLING_POINT_CHARS = 300

MUST_MATCH_KINDS = ("color", "logo", "visual", "material", "text", "other")
IMAGE_ROLES = ("main", "angle", "detail", "in_use", "packaging", "other", "irrelevant")
#: Shared must-match kinds that a variant's own appearance item of the same
#: kind replaces: "colour options: black, white, camo" is no constraint once
#: each variant states its own colour.
VARIANT_OVERRIDE_KINDS = frozenset({"color", "material"})

_MUST_MATCH_ITEMS = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": list(MUST_MATCH_KINDS)},
            "description": {"type": "string"},
        },
        "required": ["type", "description"],
        "additionalProperties": False,
    },
}

_SPEC_PAIRS = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "value": {"type": "string"}},
        "required": ["name", "value"],
        "additionalProperties": False,
    },
}

PRODUCT_INTAKE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_product": {"type": "boolean"},
        "name": {"type": "string"},
        "description": {"type": "string"},
        "model_variants": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "specs": {"$ref": "#/$defs/spec_pairs"},
                    "appearance": {"$ref": "#/$defs/must_match_items"},
                    "image_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["label", "specs", "appearance", "image_ids"],
                "additionalProperties": False,
            },
        },
        "specs": {"$ref": "#/$defs/spec_pairs"},
        "selling_points": {"type": "array", "items": {"type": "string"}},
        "audiences": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "who": {"type": "string"},
                    "why": {"type": "string"},
                    "pain": {"type": "string"},
                },
                "required": ["who", "why", "pain"],
                "additionalProperties": False,
            },
        },
        "visual_must_match": {"$ref": "#/$defs/must_match_items"},
        "image_ranking": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "image_id": {"type": "string"},
                    "role": {"type": "string", "enum": list(IMAGE_ROLES)},
                },
                "required": ["image_id", "role"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "is_product", "name", "description", "model_variants", "specs", "selling_points",
        "audiences", "visual_must_match", "image_ranking",
    ],
    "additionalProperties": False,
    "$defs": {"spec_pairs": _SPEC_PAIRS, "must_match_items": _MUST_MATCH_ITEMS},
}

SYSTEM_PROMPT = """You turn a product source into a product card for ad-creative testing.
Sources: a product page digest (JSON-LD, OpenGraph, page text), text the user pasted,
and product images (screenshots or photos) labelled image_1, image_2, ...

Rules:
- Only state what the sources support. Do not invent specifications.
- Never output prices, currencies, discounts, coupons, shipping offers or landing pages:
  they are commercial facts handled elsewhere. Leave them out of specs and selling points.
- name: the product's name without marketing slogans. description: 1-2 factual sentences.
- model_variants: one entry per distinct variant the source sells: a different model,
  size or power, AND a different colour, finish or edition of the same model
  (e.g. "Classic Black", "Avalanche White", "Camo"). An empty list when there is only one.
  For each variant:
  - label: its short name as the source calls it;
  - specs: only the specifications that differ from the shared ones;
  - appearance: how this variant looks where variants differ (colour, material, pattern,
    printed graphics), concrete enough to check an image against, e.g.
    {"type": "color", "description": "matte black housing, blue paw-print logo on the front"};
  - image_ids: the images that show this variant and no other. Use what you see, and the
    file names, URLs and alt text of images that are listed but not attached. Leave out
    images that show the shared design or several variants at once.
- specs: shared specifications as name/value pairs (dimensions, power, ports, material...).
- selling_points: 3 to 6 short, concrete benefits.
- audiences: 2 to 3 buyer groups; who buys, why they buy, the pain it solves.
- visual_must_match: what any generated image must keep identical to the real product
  (colour, shape, logo position, connectors, materials, printed text) that every variant
  shares. Never list the colour options here: each variant's own colour and material go
  into that variant's appearance.
- image_ranking: every image id you were given, best product image first, with its role;
  mark logos, banners, unrelated products and decorative images as "irrelevant".
- is_product: false when the sources do not describe one physical or digital product.
- Answer in the language of the sources; if mixed or unclear, use Simplified Chinese."""


def schema_json() -> str:
    return json.dumps(PRODUCT_INTAKE_SCHEMA, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def input_token_budget(spec) -> int:
    """Upper bound on the extraction request's input tokens, known at admission."""
    return estimate_input_tokens(
        spec,
        texts=(SYSTEM_PROMPT, "x" * MAX_USER_PROMPT_BYTES),
        schema_json=schema_json(),
        image_count=MAX_LLM_IMAGES,
    )


def _truncate_utf8(text: str, limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore")


def _image_line(label: dict) -> str:
    detail = f"{label['image_id']}: from {label['source']}"
    if label.get("url"):
        detail += f", url: {str(label['url'])[:200]}"
    if label.get("alt"):
        detail += f", alt text: {str(label['alt'])[:120]}"
    return detail


def build_user_prompt(
    *,
    user_text: str | None,
    page_url: str | None,
    page_digest: str | None,
    image_labels: list[dict],
    other_image_labels: list[dict] | None = None,
) -> str:
    """The user message: what was given, then each image's id and provenance.

    ``image_labels`` are the attached images; ``other_image_labels`` are the
    remaining candidates (not attached), listed by id, URL and alt text so
    the model can still assign them to a variant (Issue #192).
    """
    parts: list[str] = []
    if user_text:
        parts.append("Text the user pasted:\n" + user_text[:MAX_USER_TEXT_CHARS])
    if page_digest:
        parts.append(f"Product page ({page_url}):\n{page_digest}")
    if image_labels:
        parts.append("Images (attached in this order):\n" + "\n".join(_image_line(label) for label in image_labels))
    else:
        parts.append("No images were provided; return an empty image_ranking.")
    if other_image_labels:
        parts.append(
            "More images on the page (not attached: leave them out of image_ranking, but you may "
            "assign them to a variant by URL or alt text):\n"
            + "\n".join(_image_line(label) for label in other_image_labels)
        )
    prompt = "\n\n".join(parts)
    return _truncate_utf8(prompt, MAX_USER_PROMPT_BYTES)


# ---------------------------------------------------------------------------
# Normalization: model answer -> product-card draft
# ---------------------------------------------------------------------------


class ExtractionInvalid(ValueError):
    """A structurally valid answer that cannot become a product card."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(problems))


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _spec_map(pairs: Any, dropped: list[str]) -> dict[str, str]:
    specs: dict[str, str] = {}
    for pair in pairs if isinstance(pairs, list) else []:
        if not isinstance(pair, dict):
            continue
        name = _clean(pair.get("name"), PRODUCT_SPEC_KEY_MAX)
        value = _clean(pair.get("value"), PRODUCT_SPEC_VALUE_MAX)
        if not name or not value:
            continue
        if is_commercial_spec_key(name):
            dropped.append(name)
            continue
        if name not in specs and len(specs) < PRODUCT_SPECS_MAX_ENTRIES:
            specs[name] = value
    return specs


def must_match_id(kind: str, description: str) -> str:
    digest = hashlib.sha256(f"{kind}\x1f{description}".encode("utf-8")).hexdigest()
    return f"mm_{digest[:16]}"


def must_match_item(kind: str, description: str, *, proposed_by: str, confirmed: bool) -> dict:
    """A structured MustMatchRequirement (creative-library contract §6)."""
    return {
        "id": must_match_id(kind, description),
        "type": kind if kind in MUST_MATCH_KINDS else "other",
        "description": description,
        "severity": "blocking",
        "viewpoints": ["all"],
        "qc_method": "human_review",
        "evidence_reference_version_ids": [],
        "proposed_by": proposed_by,
        "confirmed": confirmed,
    }


def _proposed_items(items: Any, limit: int | None) -> list[dict]:
    """AI-proposed must-match items, cleaned and de-duplicated by stable id."""
    proposed: list[dict] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        text = _clean(item.get("description"), 500)
        if not text:
            continue
        entry = must_match_item(str(item.get("type") or "other"), text, proposed_by="ai", confirmed=False)
        if all(existing["id"] != entry["id"] for existing in proposed):
            proposed.append(entry)
    return proposed[:limit] if limit is not None else proposed


def variant_must_match(shared: list[dict], variant: dict | None) -> tuple[list[dict], list[str]]:
    """The must-match list a variant gets, and the shared item ids it replaces.

    Shared items stay, except those of a kind in
    :data:`VARIANT_OVERRIDE_KINDS` that the variant's appearance states
    itself (its own colour replaces the shared "colour options" line); the
    variant's appearance items follow. Without a variant (or appearance)
    this is the shared list unchanged -- the single-model behaviour.
    """
    appearance = [item for item in (variant or {}).get("appearance") or [] if isinstance(item, dict)]
    overridden = {item.get("type") for item in appearance} & VARIANT_OVERRIDE_KINDS
    replaced = [
        str(item.get("id")) for item in shared
        if isinstance(item, dict) and item.get("type") in overridden and item.get("id")
    ]
    composed = [item for item in shared if isinstance(item, dict) and str(item.get("id")) not in replaced]
    for item in appearance:
        if all(existing.get("id") != item.get("id") for existing in composed):
            composed.append(item)
    return composed[:MAX_COMPOSED_MUST_MATCH], replaced


def variant_image_ids(variants: list[dict], label: str | None, ranked_ids: list[str]) -> list[str]:
    """``ranked_ids`` minus the images that belong to a variant other than ``label``.

    With no variant chosen (``label`` is None) every image is kept, so a
    single-model card and a card whose variants claim no image behave as
    before Issue #192.
    """
    if label is None:
        return list(ranked_ids)
    others = {
        image_id for variant in variants if variant.get("label") != label
        for image_id in variant.get("image_ids") or []
    }
    return [image_id for image_id in ranked_ids if image_id not in others]


def normalize_extraction(data: dict, *, candidate_ids: list[str]) -> dict:
    """The product-card draft for ``data``; raises :class:`ExtractionInvalid`."""
    problems: list[str] = []
    dropped_specs: list[str] = []
    name = _clean(data.get("name"), MAX_NAME_CHARS)
    if not name:
        problems.append("name is empty")
    description = " ".join(str(data.get("description") or "").split())[:MAX_DESCRIPTION_CHARS]

    selling_points: list[str] = []
    for point in data.get("selling_points") or []:
        text = _clean(point, MAX_SELLING_POINT_CHARS)
        if text and text not in selling_points:
            selling_points.append(text)
    selling_points = selling_points[:MAX_SELLING_POINTS]
    if not selling_points:
        problems.append("no selling point")
    if not description and selling_points:
        description = "；".join(selling_points[:2])[:MAX_DESCRIPTION_CHARS]

    audiences: list[dict] = []
    for card in data.get("audiences") or []:
        if not isinstance(card, dict):
            continue
        who = _clean(card.get("who"), PRODUCT_AUDIENCE_FIELD_MAX)
        if not who or any(existing["who"] == who for existing in audiences):
            continue
        audiences.append({
            "who": who,
            "why": _clean(card.get("why"), PRODUCT_AUDIENCE_FIELD_MAX),
            "pain": _clean(card.get("pain"), PRODUCT_AUDIENCE_FIELD_MAX),
        })
    audiences = audiences[:MAX_AUDIENCES]
    if not audiences:
        problems.append("no audience")

    specs = _spec_map(data.get("specs"), dropped_specs)
    must_match = _proposed_items(data.get("visual_must_match"), MAX_MUST_MATCH)
    shared_ids = {item["id"] for item in must_match}

    known = set(candidate_ids)
    variants: list[dict] = []
    for variant in data.get("model_variants") or []:
        if not isinstance(variant, dict):
            continue
        label = _clean(variant.get("label"), PRODUCT_SPEC_KEY_MAX)
        if not label or any(existing["label"] == label for existing in variants):
            continue
        appearance = [
            item for item in _proposed_items(variant.get("appearance"), None)
            if item["id"] not in shared_ids
        ][:MAX_VARIANT_APPEARANCE]
        image_ids = list(dict.fromkeys(
            image_id for image_id in (str(raw or "").strip() for raw in variant.get("image_ids") or [])
            if image_id in known
        ))
        variants.append({
            "label": label,
            "specs": _spec_map(variant.get("specs"), dropped_specs),
            "appearance": appearance,
            "image_ids": image_ids,
        })
    variants = variants[:PRODUCT_VARIANTS_MAX]
    # An image several variants claim shows the shared design (or all of
    # them at once): it is general, not any one variant's.
    claims: dict[str, int] = {}
    for variant in variants:
        for image_id in variant["image_ids"]:
            claims[image_id] = claims.get(image_id, 0) + 1
    for variant in variants:
        variant["image_ids"] = [image_id for image_id in variant["image_ids"] if claims[image_id] == 1]

    ranking: list[dict] = []
    for item in data.get("image_ranking") or []:
        if not isinstance(item, dict):
            continue
        image_id = str(item.get("image_id") or "").strip()
        if image_id in known and all(existing["image_id"] != image_id for existing in ranking):
            role = item.get("role") if item.get("role") in IMAGE_ROLES else "other"
            ranking.append({"image_id": image_id, "role": role})

    if problems:
        raise ExtractionInvalid(problems)
    return {
        "is_product": bool(data.get("is_product", True)),
        "name": name,
        "description": description or name,
        "model_variants": variants,
        "specs": specs,
        "selling_points": selling_points,
        "audiences": audiences,
        "must_match": must_match,
        "image_ranking": ranking,
        "dropped_commercial_specs": sorted(set(dropped_specs)),
    }


# ---------------------------------------------------------------------------
# Controlled mock fixture
# ---------------------------------------------------------------------------


#: Words in the user's message that select the multi-colour example.
MOCK_COLOUR_CUES = ("配色", "颜色", "colour", "color")

#: The multi-colour example's editions: label, finish, front panel.
MOCK_COLOUR_EDITIONS = (
    ("曜石黑", "哑光曜石黑", "外壳为哑光黑色，正面深灰面板，白色 VoltNest logo"),
    ("冰川白", "亮面冰川白", "外壳为亮面白色，正面浅灰面板，黑色 VoltNest logo"),
    ("丛林迷彩", "丛林迷彩涂装", "外壳为绿褐丛林迷彩图案，正面黑色面板，白色 VoltNest logo"),
)


def _user_text(request: ResolvedLlmChatRequest) -> str:
    messages = getattr(request, "messages", None) or ()
    return "\n".join(message.text for message in messages if message.role == "user")


def _mock_variants(request: ResolvedLlmChatRequest) -> tuple[list[dict], list[dict]]:
    """``(model_variants, visual_must_match)`` for the request.

    By default the page sells two power variants (7 kW / 11 kW) that look
    alike. When the user's message talks about colours (配色 / 颜色 /
    colour) it sells three colour editions of one model instead (Issue
    #192): each with its own appearance and images -- ``img_1`` stays
    general, the others go to the editions in turn -- and a shared "colour
    options" line of the kind a real model writes, which each edition's own
    colour replaces.
    """
    text = _user_text(request).lower()
    if not any(cue in text for cue in MOCK_COLOUR_CUES):
        variants = [
            {
                "label": "7kW 单相",
                "specs": [
                    {"name": "额定功率", "value": "7 kW"},
                    {"name": "输入", "value": "单相 230 V / 32 A"},
                ],
                "appearance": [],
                "image_ids": [],
            },
            {
                "label": "11kW 三相",
                "specs": [
                    {"name": "额定功率", "value": "11 kW"},
                    {"name": "输入", "value": "三相 400 V / 16 A"},
                ],
                "appearance": [],
                "image_ids": [],
            },
        ]
        return variants, [
            {"type": "color", "description": "哑光黑外壳，正面浅灰色面板"},
            {"type": "logo", "description": "面板上方居中的 VoltNest 白色 logo"},
            {"type": "visual", "description": "底部出线的黑色螺旋线缆与 Type 2 枪头"},
            {"type": "visual", "description": "面板中央的环形 LED 状态灯"},
        ]
    variants = [
        {
            "label": label,
            # Same key as the shared "外壳" spec: choosing the edition rewrites it.
            "specs": [{"name": "外壳", "value": f"{finish} PC+ABS 阻燃外壳"}],
            "appearance": [{"type": "color", "description": colour}],
            "image_ids": [
                f"img_{index}" for index in range(2, len(request.images) + 1)
                if (index - 2) % len(MOCK_COLOUR_EDITIONS) == position
            ],
        }
        for position, (label, finish, colour) in enumerate(MOCK_COLOUR_EDITIONS)
    ]
    return variants, [
        {"type": "color", "description": "可选配色：" + "、".join(edition[0] for edition in MOCK_COLOUR_EDITIONS)},
        {"type": "logo", "description": "面板上方居中的 VoltNest logo"},
        {"type": "visual", "description": "底部出线的黑色螺旋线缆与 Type 2 枪头"},
        {"type": "visual", "description": "面板中央的环形 LED 状态灯"},
    ]


def _mock_product_intake(request: ResolvedLlmChatRequest) -> dict[str, Any]:
    """A realistic EV-charger card; ranks whatever images it was given.

    The mock registry is keyed by semantic only, so a request under this tag
    that carries a different schema (another caller, a test) gets the
    schema-synthesized answer instead of a card that would not fit it.
    """
    if request.schema_name != SCHEMA_NAME:
        return synthesize_from_schema(request.json_schema)
    ranking = [
        {"image_id": f"img_{index}", "role": "main" if index == 1 else "angle"}
        for index in range(1, len(request.images) + 1)
    ]
    variants, must_match = _mock_variants(request)
    return {
        "is_product": True,
        "name": "VoltNest 家用交流充电桩",
        "description": "壁挂式家用电动汽车交流充电桩，Type 2 接口，支持 App 预约充电与负载均衡。",
        "model_variants": variants,
        "specs": [
            {"name": "接口", "value": "Type 2（IEC 62196-2），5 米线缆"},
            {"name": "防护等级", "value": "IP55"},
            {"name": "尺寸", "value": "320 × 220 × 110 mm"},
            {"name": "联网", "value": "Wi-Fi、蓝牙、OCPP 1.6J"},
            {"name": "外壳", "value": "哑光黑 PC+ABS 阻燃外壳"},
        ],
        "selling_points": [
            "比家用插座快 3 倍，一晚充满",
            "App 预约谷电时段充电，电费更省",
            "动态负载均衡，不跳闸",
            "IP55 防护，室外车位也能装",
        ],
        "audiences": [
            {
                "who": "有固定车位的新能源车主",
                "why": "想在家过夜充满，不再排队找公共桩",
                "pain": "随车充太慢、公共桩排队且贵",
            },
            {
                "who": "注重电费的通勤族",
                "why": "用谷电时段充电降低每公里成本",
                "pain": "白天充电电价高",
            },
        ],
        "visual_must_match": must_match,
        "image_ranking": ranking,
    }


register_llm_mock_fixture(SEMANTIC, _mock_product_intake)
