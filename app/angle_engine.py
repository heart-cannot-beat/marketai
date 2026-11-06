"""角度引擎：

Legacy: ``generate_dimensions`` uses the ungoverned ``app.llm`` client
(platform env keys, no reservation or evidence). New LLM work must use the
governed ``llm_chat`` semantic in ``app.providers.llm_chat`` instead.

1. generate_dimensions —— LLM 根据产品信息产出维度 schema（无 key 时回落到通用模板）
2. compose_prompt —— 把 组合 + 产品 + 风格 确定性地拼成图片生成 prompt

营销变量的分工（v9）：画面侧的营销维度（「钩子」）在这里走 Dimension 机制进 prompt；
广告层变量（Persona / Angle / Offer / Landing Page，见 schemas.MarketingVars）不进画面，
由 Meta 投放阶段按轮记录并写入 ad_id 映射。
"""
from __future__ import annotations

from . import llm
from .schemas import AngleSchema, Combo, Dimension, DimensionValue, Product

ANGLE_SYSTEM = """你是资深的 Meta 广告创意策略师。给定一个产品，你要设计用于"创意组合测试"的维度体系。

规则：
- 输出 3~5 个维度（dimension），每个维度 2~4 个取值（value）。
- 维度之间必须相互独立、可自由组合（任何取值组合都能构成一张合理的广告图）。
- 维度要覆盖不同层面：人物有无/类型、使用场景、产品呈现方式、氛围光线；**必须包含一个「钩子」维度**（营销角度如何进画面：痛点场景 / 核心卖点直给 / 省钱实惠 / 社会证明……）——营销变量与画面变量同等重要，只测画面最后只能学到"暖光比冷光好"。
- label 用简体中文（给运营看）；prompt_fragment 用英文摄影/广告语言（直接进入图片生成 prompt），具体、可视觉化，避免抽象词。
- style_notes 是一段英文全局风格描述（光线、质感、构图基调），会附加到每条 prompt。

严格只输出 JSON，格式：
{
  "dimensions": [
    {"label": "人物", "values": [
      {"label": "无人", "prompt_fragment": "no people in frame, product-focused composition"},
      {"label": "男性车主", "prompt_fragment": "a man in his 30s using the product next to his car"}
    ]}
  ],
  "style_notes": "clean commercial photography, soft natural light ..."
}"""


def _angle_user_prompt(p: Product) -> str:
    points = "、".join(p.selling_points) if p.selling_points else "（未填写）"
    return (
        f"产品名称：{p.name}\n"
        f"产品描述：{p.description or '（未填写）'}\n"
        f"核心卖点：{points}\n"
        f"目标人群：{p.audience or '（未填写）'}\n"
        f"品牌约束：{p.brand_notes or '（无）'}\n\n"
        f"请为这个产品设计创意测试维度。"
    )


def template_dimensions() -> tuple[list[Dimension], str]:
    """离线通用模板：任何产品都适用的四个基础维度。"""
    dims = [
        Dimension(label="人物", values=[
            DimensionValue(label="无人", prompt_fragment="no people in frame, clean product-focused composition"),
            DimensionValue(label="单人使用者", prompt_fragment="one person naturally using the product, candid lifestyle moment"),
            DimensionValue(label="家庭/多人", prompt_fragment="a family or small group interacting around the product, warm authentic mood"),
        ]),
        Dimension(label="场景", values=[
            DimensionValue(label="产品棚拍", prompt_fragment="studio product shot on a minimal seamless background with soft gradient"),
            DimensionValue(label="家居生活", prompt_fragment="cozy modern home interior setting with natural daylight"),
            DimensionValue(label="户外", prompt_fragment="outdoor real-world environment, natural light, depth of field"),
            DimensionValue(label="工作/专业环境", prompt_fragment="professional working environment, practical industrial context"),
        ]),
        Dimension(label="产品呈现", values=[
            DimensionValue(label="产品特写", prompt_fragment="hero close-up of the product, crisp details, shallow depth of field"),
            DimensionValue(label="使用中", prompt_fragment="product shown in active use, hands visible, action moment"),
        ]),
        Dimension(label="钩子", values=[
            DimensionValue(label="核心卖点直给", prompt_fragment="hero-benefit hook: make the product's single strongest selling point visually unmistakable"),
            DimensionValue(label="痛点场景", prompt_fragment="problem-moment hook: show the frustrating situation this product solves, relief clearly visible"),
            DimensionValue(label="省钱实惠", prompt_fragment="value-for-money hook, savings-focused framing, practical everyday tone"),
        ]),
        Dimension(label="氛围光线", values=[
            DimensionValue(label="明亮日光", prompt_fragment="bright airy daylight, fresh and clean color palette"),
            DimensionValue(label="暖调温馨", prompt_fragment="warm golden-hour tones, inviting cozy atmosphere"),
            DimensionValue(label="冷调科技感", prompt_fragment="cool blue-toned tech ambience, sleek modern feel"),
        ]),
    ]
    style = ("professional advertising photography, high detail, realistic textures, "
             "balanced composition with clear space for ad copy, no watermark")
    return dims, style


async def generate_dimensions(p: Product) -> AngleSchema:
    """优先 LLM 生成；LLM 不可用或解析失败时回落到模板。"""
    try:
        text = await llm.chat(ANGLE_SYSTEM, _angle_user_prompt(p))
        data = llm.extract_json(text)
        dims = []
        for d in data.get("dimensions", []):
            values = [DimensionValue(label=v["label"], prompt_fragment=v.get("prompt_fragment", ""))
                      for v in d.get("values", []) if v.get("label")]
            if len(values) >= 2:
                dims.append(Dimension(label=d["label"], values=values))
        if not dims:
            raise ValueError("LLM 未返回有效维度")
        return AngleSchema(product_id=p.id, dimensions=dims,
                           style_notes=data.get("style_notes", ""), source="llm")
    except Exception:
        dims, style = template_dimensions()
        return AngleSchema(product_id=p.id, dimensions=dims, style_notes=style, source="template")


# ---------- prompt 组装（确定性模板，保证可复现、零成本） ----------

_AR_NOTES = {
    "1:1": "square 1:1 composition for feed placement",
    "4:5": "vertical 4:5 composition for feed placement",
    "9:16": "vertical 9:16 full-screen composition for stories and reels",
    "16:9": "wide 16:9 composition",
}


def compose_prompt(p: Product, combo: Combo, angles: AngleSchema,
                   aspect_ratio: str, use_ref: bool) -> str:
    dim_by_id = {d.id: d for d in angles.dimensions}
    fragments: list[str] = []
    for dim_id, val_id in combo.values.items():
        dim = dim_by_id.get(dim_id)
        if not dim:
            continue
        val = next((v for v in dim.values if v.id == val_id), None)
        if val and val.prompt_fragment:
            fragments.append(val.prompt_fragment.strip().rstrip("."))

    parts: list[str] = []
    if use_ref and p.ref_images:
        parts.append(
            "Use the exact product shown in the reference image(s): preserve its shape, "
            "proportions, ports, buttons, colors, materials and logo precisely. "
            "Place this product naturally into the scene described below."
        )
        parts.append(f"Advertising photograph featuring this product ({p.name}).")
    else:
        desc = p.description.strip() or p.name
        parts.append(f"Professional advertising photograph of: {p.name} — {desc}.")

    if fragments:
        parts.append(". ".join(fragments) + ".")
    if angles.style_notes:
        parts.append(angles.style_notes.strip().rstrip(".") + ".")
    if p.brand_notes:
        parts.append(f"Brand constraints: {p.brand_notes.strip()}.")
    parts.append(
        f"{_AR_NOTES.get(aspect_ratio, '')}, the product is the clear hero of the image, "
        "commercial quality, photorealistic, no watermark, no gibberish text."
    )
    return " ".join(x for x in parts if x)
