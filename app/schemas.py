"""数据模型：产品 → 维度 → 组合 → 生成任务 → 素材。"""
from __future__ import annotations

import time
import uuid
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, Field


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def now() -> float:
    return time.time()


# ---------- 产品 ----------
class ProductEconomics(BaseModel):
    price: Decimal = Decimal("0")
    discount: Decimal = Decimal("0")
    cogs: Decimal = Decimal("0")
    payment_fee: Decimal = Decimal("0")
    fulfilment_cost: Decimal = Decimal("0")
    shipping_subsidy: Decimal = Decimal("0")
    expected_refund_rate: Decimal = Decimal("0")
    target_contribution_margin: Decimal = Decimal("0")
    currency: str = "USD"


class Product(BaseModel):
    id: str = Field(default_factory=lambda: _id("prod"))
    name: str
    description: str = ""
    selling_points: list[str] = Field(default_factory=list)
    audience: str = ""
    brand_notes: str = ""            # 品牌色 / 风格约束等
    ref_images: list[str] = Field(default_factory=list)  # data/products/{id}/refs/ 下的文件名
    economics: ProductEconomics = Field(default_factory=ProductEconomics)
    created_at: float = Field(default_factory=now)


class ProductCreate(BaseModel):
    name: str
    description: str = ""
    selling_points: list[str] = Field(default_factory=list)
    audience: str = ""
    brand_notes: str = ""
    economics: ProductEconomics = Field(default_factory=ProductEconomics)


# ---------- 角度维度 ----------
class DimensionValue(BaseModel):
    id: str = Field(default_factory=lambda: _id("v"))
    label: str                       # 展示名（中文），如 "男性车主"
    prompt_fragment: str = ""        # 进入图片 prompt 的英文片段


class Dimension(BaseModel):
    id: str = Field(default_factory=lambda: _id("d"))
    label: str                       # 如 "人物" / "场景" / "汽车"
    values: list[DimensionValue] = Field(default_factory=list)


class AngleSchema(BaseModel):
    product_id: str
    dimensions: list[Dimension] = Field(default_factory=list)
    style_notes: str = ""            # 全局风格说明（英文，进入每条 prompt）
    source: Literal["llm", "template", "manual"] = "manual"
    updated_at: float = Field(default_factory=now)


# ---------- 组合 ----------
class Combo(BaseModel):
    id: str                          # 如 "C03"
    code: str                        # 如 "A2-B1-C2"（维度序号-取值序号）
    values: dict[str, str]           # dimension_id -> value_id
    tags: dict[str, str]             # 维度名 -> 取值名（人读）


class PlanRequest(BaseModel):
    selected: dict[str, list[str]]   # dimension_id -> 勾选的 value_id 列表
    mode: Literal["full", "coverage"] = "full"
    cap: int = 0                     # coverage 模式下的组合数上限；0=不限


class Plan(BaseModel):
    id: str = Field(default_factory=lambda: _id("plan"))
    product_id: str
    mode: str
    cap: int
    total_full: int                  # 全组合数（用于 UI 展示 "81 → 采样 12"）
    combos: list[Combo] = Field(default_factory=list)
    created_at: float = Field(default_factory=now)


# ---------- 生成 ----------
class GenRequest(BaseModel):
    product_id: str
    provider: str                    # gemini / openai / seedream / mock
    model: Optional[str] = None
    aspect_ratio: str = "1:1"        # 1:1 / 4:5 / 9:16 / 16:9
    image_size: str = "1K"           # 1K / 2K（provider 内部映射）
    variants: int = 1                # 每个组合出几张
    use_ref: bool = True             # 以产品参考图做保真生成
    combo_ids: Optional[list[str]] = None  # 不传 = 当前 plan 全部组合
    force: bool = False              # true 才允许绕过成功指纹复用；仍受成本硬上限约束


class Asset(BaseModel):
    id: str
    run_id: str
    combo_id: str
    combo_code: str
    tags: dict[str, str]
    filename: str                    # 相对 run 目录
    provider: str
    model: str
    prompt: str
    fingerprint: str = ""
    reused_from_asset_id: Optional[str] = None
    status: Literal["pending", "ok", "failed"] = "pending"
    error: str = ""
    created_at: float = Field(default_factory=now)


class Run(BaseModel):
    id: str = Field(default_factory=lambda: _id("run"))
    product_id: str
    plan_id: str
    provider: str
    model: str
    aspect_ratio: str
    image_size: str
    variants: int
    use_ref: bool
    status: Literal["queued", "running", "done", "cancelled", "failed"] = "queued"
    total: int = 0
    done: int = 0
    failed: int = 0
    est_cost: Decimal = Decimal("0")
    currency: str = "USD"
    assets: list[Asset] = Field(default_factory=list)
    created_at: float = Field(default_factory=now)


# ---------- 双状态与营销变量（v9：为 Meta 阶段定的数据契约） ----------
# 质量审核（quality）与投放判定（verdict）是两套独立状态，不放在 Asset 本体上，
# 分别存 quality.json / verdicts.json（见 store.py）。人只判"能不能用"，数据判"好不好卖"。
QUALITY_VALUES = ("approved", "needs_changes", "rejected")
VERDICT_VALUES = ("win", "learn", "lose")


class VerdictRecord(BaseModel):
    """一条投放判定：Meta 阶段终判、人确认后回写；带判定环境（Winner 只在环境内成立）。"""
    verdict: Literal["win", "learn", "lose"]
    context: dict = Field(default_factory=dict)  # 市场 / 版位 / landing_page / offer / 实验轮 等
    set_at: float = Field(default_factory=now)


class MarketingVars(BaseModel):
    """广告层营销变量：跟随广告（而非素材）。投放实验建轮时填写，随每条广告
    写入命名与 ad_id 映射，进入归因——只学画面维度，最后只能学到"暖光比冷光好"。
    注意：Persona ≠ 画面里的人物；同一张素材可配不同 Offer / LP 进多条广告
    （组合 → 素材 → 版本 → 广告 是 1:N 链，不要混为一个对象）。
    画面侧的营销维度（如「钩子」）走 Dimension 机制，见 angle_engine。"""
    persona: str = ""
    angle: str = ""
    hook: str = ""                   # 留空 = 投放侧按 angle 自动生成
    offer: str = ""
    landing_page: str = ""


class AssetVersion(BaseModel):
    id: str = Field(default_factory=lambda: _id("ver"))
    asset_id: str
    parent_version_id: Optional[str] = None
    kind: Literal["generated", "size", "localized", "manual"] = "generated"
    aspect_ratio: str = "1:1"
    language: str = ""
    object_key: str
    content_sha256: str
    byte_size: int
    mime_type: str = "image/png"
    currency: Optional[str] = None
    truth_package_id: Optional[str] = None
    created_at: float = Field(default_factory=now)
