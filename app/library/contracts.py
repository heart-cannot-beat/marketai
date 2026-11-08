"""创意库条目类型、状态维度、内容契约与校验。

契约 §2 五类入口、§3 身份/草稿/发布版本、§4 角色、§5 Product 与 Offer、
§6 Must-match、§7 Reference、§8 Person。

这里只做纯函数：内容形状校验、依赖抽取、内容 hash。
持久化与命令事务在 service.py。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable

from .fact_types import FACT_FAMILIES, fact_value_problems


# --------------------------------------------------------------------------
# 条目类型（契约 §2）。维度模板、Prompt 规则与数据源属于 #20，不在本模块。
# --------------------------------------------------------------------------
ENTRY_PRODUCT = "product"
ENTRY_OFFER = "offer"
ENTRY_PERSON = "person"
ENTRY_REFERENCE_ASSET = "reference_asset"
ENTRY_REFERENCE_GROUP = "reference_group"
ENTRY_SELECTION_RULE = "reference_selection_rule"
ENTRY_MARKET_MAPPING = "market_source_mapping"

# 维度模板/Schema、Prompt 规则、数据源、人工事实集与真值解析策略（#20）。
ENTRY_DIMENSION_TEMPLATE = "dimension_template"
ENTRY_DIMENSION_SCHEMA = "dimension_schema"
ENTRY_PROMPT_POLICY = "prompt_policy"
ENTRY_DATA_SOURCE = "data_source"
ENTRY_MANUAL_FACT_SET = "manual_fact_set"
ENTRY_TRUTH_RESOLUTION_POLICY = "truth_resolution_policy"
ENTRY_BRAND_LOCALIZATION_POLICY = "brand_localization_policy"

ENTRY_TYPES = frozenset(
    {
        ENTRY_PRODUCT,
        ENTRY_OFFER,
        ENTRY_PERSON,
        ENTRY_REFERENCE_ASSET,
        ENTRY_REFERENCE_GROUP,
        ENTRY_SELECTION_RULE,
        ENTRY_MARKET_MAPPING,
        ENTRY_DIMENSION_TEMPLATE,
        ENTRY_DIMENSION_SCHEMA,
        ENTRY_PROMPT_POLICY,
        ENTRY_DATA_SOURCE,
        ENTRY_MANUAL_FACT_SET,
        ENTRY_TRUTH_RESOLUTION_POLICY,
        ENTRY_BRAND_LOCALIZATION_POLICY,
    }
)

# --------------------------------------------------------------------------
# 状态维度（契约 §3.2）。禁止用一个 status 同时表达编辑/发布/资格。
# --------------------------------------------------------------------------
LIFECYCLE_ACTIVE = "active"
LIFECYCLE_ARCHIVED = "archived"
LIFECYCLES = frozenset({LIFECYCLE_ACTIVE, LIFECYCLE_ARCHIVED})

DRAFT_EDITING = "editing"
DRAFT_VALIDATION_FAILED = "validation_failed"
DRAFT_READY = "ready_to_publish"
DRAFT_STATES = frozenset({DRAFT_EDITING, DRAFT_VALIDATION_FAILED, DRAFT_READY})

VERSION_EFFECTIVE = "effective"
VERSION_SUPERSEDED = "superseded"
VERSION_WITHDRAWN = "withdrawn"
VERSION_STATES = frozenset({VERSION_EFFECTIVE, VERSION_SUPERSEDED, VERSION_WITHDRAWN})

ELIGIBILITY_ELIGIBLE = "eligible"
ELIGIBILITY_WARNING = "warning"
ELIGIBILITY_BLOCKED = "blocked"
ELIGIBILITIES = frozenset({ELIGIBILITY_ELIGIBLE, ELIGIBILITY_WARNING, ELIGIBILITY_BLOCKED})

# --------------------------------------------------------------------------
# 复用命令（契约 §12）。"duplicate" 不是一个动作。
# --------------------------------------------------------------------------
COMMAND_CREATE = "create"
COMMAND_EDIT = "edit"
COMMAND_FORK = "fork"
COMMAND_CLONE = "clone"
COMMAND_BIND = "bind"
COMMAND_APPLY = "apply"
ORIGIN_COMMANDS = frozenset(
    {COMMAND_CREATE, COMMAND_EDIT, COMMAND_FORK, COMMAND_CLONE, COMMAND_APPLY}
)

ROLE_OWNER = "owner"
ROLE_MEMBER = "member"
ROLES = frozenset({ROLE_OWNER, ROLE_MEMBER})

# 契约 §4：Owner 才能发布 OfferVersion。Product/Person/Reference 由 Member 发布。
# Prompt 规则、数据源、人工事实集与真值解析策略同样只允许 Owner 发布（#20）。
# 品牌本地化策略同样只允许 Owner 发布/撤销（既有 MFA 二次确认）（#24）。
OWNER_ONLY_PUBLISH = frozenset(
    {
        ENTRY_OFFER,
        ENTRY_PROMPT_POLICY,
        ENTRY_DATA_SOURCE,
        ENTRY_MANUAL_FACT_SET,
        ENTRY_TRUTH_RESOLUTION_POLICY,
        ENTRY_BRAND_LOCALIZATION_POLICY,
    }
)

#: Roles that may publish ``OWNER_ONLY_PUBLISH`` types: the Owner, and a
#: ``system`` context acting for the Workspace after a hand-off.
PUBLISH_OWNER_ROLES = frozenset({ROLE_OWNER, "system"})


def can_publish(entry_type: str, role: str) -> bool:
    """Whether ``role`` may publish a version of an ``entry_type`` entry.

    The one rule both the entry's ``publish`` eligibility and the publish
    command apply, so a button offered as allowed is never refused for role.
    """
    return entry_type not in OWNER_ONLY_PUBLISH or role in PUBLISH_OWNER_ROLES


EXECUTION_MODE_REAL = "real"
EXECUTION_MODE_MOCK = "mock"
EXECUTION_MODES = frozenset({EXECUTION_MODE_REAL, EXECUTION_MODE_MOCK})

# --------------------------------------------------------------------------
# Content schema versioning (Gate 0.2 / #47).
# Each entry type's content shape is versioned independently.
# Backfill uses LEGACY_CONTRACT_VERSION for rows that predate versioning.
# --------------------------------------------------------------------------
LEGACY_CONTRACT_VERSION = 1
CONTENT_SCHEMA_VERSIONS: dict[str, int] = {
    ENTRY_PRODUCT: 1,
    ENTRY_OFFER: 1,
    ENTRY_PERSON: 1,
    ENTRY_REFERENCE_ASSET: 1,
    ENTRY_REFERENCE_GROUP: 1,
    ENTRY_SELECTION_RULE: 1,
    ENTRY_MARKET_MAPPING: 1,
    ENTRY_DIMENSION_TEMPLATE: 1,
    ENTRY_DIMENSION_SCHEMA: 1,
    ENTRY_PROMPT_POLICY: 1,
    ENTRY_DATA_SOURCE: 1,
    ENTRY_MANUAL_FACT_SET: 1,
    ENTRY_TRUTH_RESOLUTION_POLICY: 1,
    ENTRY_BRAND_LOCALIZATION_POLICY: 1,
}


def content_schema_version(entry_type: str) -> int:
    """Return the current content schema version for an entry type."""
    if entry_type not in ENTRY_TYPES:
        raise LibraryContractError(f"未知创意库条目类型: {entry_type}", code="library_entry_type_unknown")
    return CONTENT_SCHEMA_VERSIONS[entry_type]


# --------------------------------------------------------------------------
# Navigation categories (Gate 0.5 / #47).
# Root categories appear as top-level navigation; internal types are
# discovered under a root via LibraryEntryContextRecord.
# --------------------------------------------------------------------------
ROOT_CATEGORIES = frozenset(
    {ENTRY_PRODUCT, ENTRY_PERSON, ENTRY_DIMENSION_TEMPLATE, ENTRY_PROMPT_POLICY, ENTRY_DATA_SOURCE}
)

INTERNAL_TYPES = frozenset(
    {
        ENTRY_OFFER,
        ENTRY_DIMENSION_SCHEMA,
        ENTRY_REFERENCE_ASSET,
        ENTRY_REFERENCE_GROUP,
        ENTRY_SELECTION_RULE,
        ENTRY_MARKET_MAPPING,
        ENTRY_MANUAL_FACT_SET,
        ENTRY_TRUTH_RESOLUTION_POLICY,
    }
)

CONTEXT_REGISTRY: dict[str, dict[str, str]] = {
    ENTRY_PRODUCT: {
        ENTRY_OFFER: "product_offer",
        ENTRY_REFERENCE_GROUP: "product_reference_group",
        ENTRY_SELECTION_RULE: "product_selection_rule",
        ENTRY_DIMENSION_SCHEMA: "product_dimension_schema",
        ENTRY_MARKET_MAPPING: "product_market_mapping",
    },
    ENTRY_DATA_SOURCE: {
        ENTRY_MANUAL_FACT_SET: "data_source_fact_set",
        ENTRY_TRUTH_RESOLUTION_POLICY: "data_source_truth_policy",
    },
}

SHARED_INTERNAL_TYPES = frozenset({ENTRY_REFERENCE_ASSET})


RIGHTS_GRANTED = "granted"
RIGHTS_EXPIRED = "expired"
RIGHTS_REVOKED = "revoked"
RIGHTS_MISSING = "missing"

SEVERITY_BLOCKING = "blocking"
SEVERITY_WARNING = "warning"

PERSON_FICTIONAL = "fictional"
PERSON_LICENSED = "licensed_talent"
PERSON_TYPES = frozenset({PERSON_FICTIONAL, PERSON_LICENSED})


class LibraryContractError(ValueError):
    """内容不符合创意库契约。``code`` is the stable response code (#253)."""

    def __init__(self, message: str = "", *, code: str = "library_contract_invalid") -> None:
        super().__init__(message)
        self.code = code
        self.params: dict = {}


def check_rights_eligible(rights: dict, *, today: date | None = None) -> str | None:
    """Shared rights eligibility gate used by both contract validation and runtime commands.

    Returns None when eligible, or an error message string when not.
    """
    today = today or date.today()
    state = rights.get("state", "")
    if state in (RIGHTS_REVOKED, RIGHTS_EXPIRED, RIGHTS_MISSING):
        return f"权利状态为 {state!r}，不可用"
    if state != RIGHTS_GRANTED:
        return f"权利状态必须是 granted，当前为 {state!r}"
    valid_to_raw = rights.get("valid_to")
    if not isinstance(valid_to_raw, str) or not valid_to_raw.strip():
        return "缺少授权有效期 (valid_to)"
    parsed = _parse_date(valid_to_raw)
    if parsed is None:
        return f"授权有效期格式无效: {valid_to_raw!r}"
    if parsed < today:
        return f"授权已于 {parsed} 过期"
    return None


# ---------------------------------------------------------------------------
# Withdrawal downstream event types (contract S14 / #20-#25 stubs).
# Only constants; implementation belongs to future tickets.
# ---------------------------------------------------------------------------
WITHDRAWAL_DOWNSTREAM_EVENT_TYPES = {
    "generation_draft": "library.withdrawal.affects_generation_draft",
    "generation_run": "library.withdrawal.affects_generation_run",
    "asset_version": "library.withdrawal.affects_asset_version",
    "experiment": "library.withdrawal.affects_experiment",
    "localization_batch": "library.withdrawal.affects_localization_batch",
    "truth_package": "library.withdrawal.affects_truth_package",
    "market_asset_binding": "library.withdrawal.affects_market_asset_binding",
    "launch": "library.withdrawal.affects_launch",
}


@dataclass(frozen=True)
class Finding:
    code: str
    message: str
    path: str = ""
    severity: str = SEVERITY_BLOCKING

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "path": self.path, "severity": self.severity}


@dataclass
class ValidationReport:
    entry_type: str
    content_hash: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == SEVERITY_BLOCKING]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == SEVERITY_WARNING]

    @property
    def ok(self) -> bool:
        return not self.blocking

    def as_dict(self) -> dict:
        return {
            "entry_type": self.entry_type,
            "content_hash": self.content_hash,
            "ok": self.ok,
            "blocking": [f.as_dict() for f in self.blocking],
            "warnings": [f.as_dict() for f in self.warnings],
        }


@dataclass(frozen=True)
class DependencyEdge:
    """内容对另一个 LibraryVersion 的引用。发布时冻结成依赖边。"""

    to_version_id: str
    relation: str
    path: str = ""


def canonical_hash(value: Any) -> str:
    """与 experiments.judge.canonical_hash 相同的稳定序列化口径。"""
    from ..platform.canonical import canonical_hash as _shared
    return _shared(value)


def _is_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------
# 被禁字段：契约把它们显式排除在该版本之外，静默接受会污染身份边界。
# --------------------------------------------------------------------------

# §5.1 ProductVersion 不保存当前价格、默认 Variant、Shopify Token 或可变商店映射。
PRODUCT_FORBIDDEN_KEYS = {
    "price": "价格属于商业真值，不属于 ProductVersion",
    "currency": "币种属于商业真值，不属于 ProductVersion",
    "variant_id": "Variant 属于 OfferVersion 与 MarketSourceMappingVersion",
    "default_variant": "契约禁止默认/第一个 Variant 推断",
    "shopify_token": "凭据由 CredentialVersion 拥有，绝不进入内容版本",
    "store_mapping": "外部映射属于 MarketSourceMappingVersion",
    "landing_page_url": "Landing Page 属于 OfferVersion",
    "offer": "Offer 是独立稳定身份，不能内嵌进 ProductVersion",
    "external_product_id": "外部对象身份属于 MarketSourceMappingVersion",
}

# §5.2 OfferVersion 不得通过标题或"第一个 Variant"推断外部对象。
OFFER_FORBIDDEN_KEYS = {
    "external_product_id": "外部对象身份属于 MarketSourceMappingVersion",
    "external_variant_id": "外部对象身份属于 MarketSourceMappingVersion",
    "data_source_version_id": "数据源绑定属于 MarketSourceMappingVersion",
    "price": "价格是带有效期的商业真值，不能内嵌进 OfferVersion",
    "currency": "币种随价格事实一起由商业真值提供",
    "shopify_token": "凭据由 CredentialVersion 拥有，绝不进入内容版本",
}

# §7.3 选择规则匹配稳定 ID，不匹配中文标签或文件名。
RULE_FORBIDDEN_MATCH_KEYS = {
    "label": "选择规则不得匹配展示标签",
    "filename": "选择规则不得匹配文件名",
    "name": "选择规则不得匹配名称",
    "tag": "选择规则不得匹配自由标签",
}


def _check_forbidden(content: dict, forbidden: dict[str, str], findings: list[Finding]) -> None:
    for key, why in forbidden.items():
        if key in content:
            findings.append(
                Finding(code="forbidden_field", message=f"{key}: {why}", path=key)
            )


# --------------------------------------------------------------------------
# Must-match（契约 §6）：不是字符串数组。
# --------------------------------------------------------------------------
MUST_MATCH_TYPES = frozenset({"visual", "text", "material", "color", "logo", "claim", "other"})
QC_METHODS = frozenset({"human_review", "automated_check", "reference_diff", "none"})


def validate_must_match(items: Any, findings: list[Finding], *, path: str = "must_match") -> None:
    if items in (None, []):
        return
    if not isinstance(items, list):
        findings.append(Finding("must_match_shape", "must_match 必须是结构化要求列表", path))
        return
    seen: set[str] = set()
    for index, item in enumerate(items):
        item_path = f"{path}[{index}]"
        if isinstance(item, str):
            findings.append(
                Finding(
                    "must_match_not_structured",
                    "MustMatchRequirement 不是字符串；必须保存稳定 ID、类型、严重级别、视角、证据和 QC 方法",
                    item_path,
                )
            )
            continue
        if not isinstance(item, dict):
            findings.append(Finding("must_match_shape", "MustMatchRequirement 必须是对象", item_path))
            continue
        stable_id = item.get("id")
        if not _is_str(stable_id):
            findings.append(Finding("must_match_id", "MustMatchRequirement 需要稳定 ID", item_path))
        elif stable_id in seen:
            findings.append(Finding("must_match_duplicate_id", f"重复的 must-match ID: {stable_id}", item_path))
        else:
            seen.add(str(stable_id))
        if item.get("type") not in MUST_MATCH_TYPES:
            findings.append(
                Finding("must_match_type", f"type 必须是 {sorted(MUST_MATCH_TYPES)} 之一", item_path)
            )
        if not _is_str(item.get("description")):
            findings.append(Finding("must_match_description", "description 不能为空", item_path))
        if item.get("severity") not in {SEVERITY_BLOCKING, SEVERITY_WARNING}:
            findings.append(Finding("must_match_severity", "severity 必须是 blocking 或 warning", item_path))
        viewpoints = item.get("viewpoints")
        if not isinstance(viewpoints, list) or not viewpoints:
            findings.append(Finding("must_match_viewpoints", "必须声明适用视角", item_path))
        if item.get("qc_method") not in QC_METHODS:
            findings.append(Finding("must_match_qc_method", f"qc_method 必须是 {sorted(QC_METHODS)} 之一", item_path))
        evidence = item.get("evidence_reference_version_ids")
        if evidence is not None and not isinstance(evidence, list):
            findings.append(Finding("must_match_evidence", "证据必须是 ReferenceAssetVersion ID 列表", item_path))
        # §6：模型观察不是已确认要求。
        if item.get("proposed_by") == "ai" and not item.get("confirmed"):
            findings.append(
                Finding(
                    "must_match_unconfirmed",
                    "AI 提出的 must-match 必须由用户确认后才能发布",
                    item_path,
                )
            )


def _validate_reference_bindings(items: Any, findings: list[Finding], *, path: str) -> None:
    """§7.1 ReferenceBinding 保存用途、视角、优先级、说明和适用条件。"""
    if items in (None, []):
        return
    if not isinstance(items, list):
        findings.append(Finding("binding_shape", "reference_bindings 必须是列表", path))
        return
    seen: set[tuple[str, str, str]] = set()
    for index, item in enumerate(items):
        item_path = f"{path}[{index}]"
        if not isinstance(item, dict):
            findings.append(Finding("binding_shape", "ReferenceBinding 必须是对象", item_path))
            continue
        version_id = item.get("reference_version_id")
        if not _is_str(version_id):
            findings.append(Finding("binding_target", "ReferenceBinding 需要 reference_version_id", item_path))
        if not _is_str(item.get("purpose")):
            findings.append(Finding("binding_purpose", "ReferenceBinding 需要 purpose", item_path))
        if not _is_str(item.get("viewpoint")):
            findings.append(Finding("binding_viewpoint", "ReferenceBinding 需要 viewpoint", item_path))
        priority = item.get("priority")
        if not isinstance(priority, int) or isinstance(priority, bool) or priority < 0:
            findings.append(Finding("binding_priority", "priority 必须是非负整数", item_path))
        key = (str(version_id), str(item.get("purpose")), str(item.get("viewpoint")))
        if key in seen:
            findings.append(
                Finding("binding_duplicate", "同一 purpose/viewpoint 下重复绑定同一参考版本", item_path)
            )
        seen.add(key)


# --------------------------------------------------------------------------
# 各类型内容校验
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# 产品识别（Issue #161）新增的可选字段：specs、model_variants、audiences、
# source_url。都是 ProductVersion 的生成语义（外观、型号、客群），不是商业真值：
# 价格、币种、优惠与落地页仍归 OfferVersion（§5.2），因此规格键里出现这些
# 概念同样被拒绝。
# --------------------------------------------------------------------------
PRODUCT_SPECS_MAX_ENTRIES = 60
PRODUCT_SPEC_KEY_MAX = 100
PRODUCT_SPEC_VALUE_MAX = 500
PRODUCT_VARIANTS_MAX = 20
PRODUCT_AUDIENCES_MAX = 5
PRODUCT_AUDIENCE_FIELD_MAX = 500
PRODUCT_SOURCE_URL_MAX = 2048
PRODUCT_SELLING_POINT_MAX = 500

#: 规格键里的商业真值词（小写、去空白后包含即拒绝）。
COMMERCIAL_SPEC_TERMS = (
    "price", "msrp", "currency", "discount", "coupon", "landingpage", "landing_page",
    "价格", "售价", "原价", "现价", "币种", "优惠", "折扣", "落地页",
)
#: must-match 项的提出者（契约 §6：AI 提议不等于确认）。
MUST_MATCH_PROPOSERS = frozenset({"ai", "user"})


def is_commercial_spec_key(key: str) -> bool:
    folded = "".join(str(key).lower().split())
    return any(term in folded for term in COMMERCIAL_SPEC_TERMS)


def validate_product_specs(specs: Any, findings: list[Finding], *, path: str = "specs") -> None:
    """``specs`` 是扁平的 键 → 值 字符串表（尺寸、功率、接口、材质……）。"""
    if specs is None:
        return
    if not isinstance(specs, dict):
        findings.append(Finding("product_specs_shape", "specs 必须是 键 → 值 对象", path))
        return
    if len(specs) > PRODUCT_SPECS_MAX_ENTRIES:
        findings.append(
            Finding("product_specs_too_many", f"specs 最多 {PRODUCT_SPECS_MAX_ENTRIES} 项", path)
        )
    for key, value in specs.items():
        item_path = f"{path}.{key}"
        if not _is_str(key) or len(str(key)) > PRODUCT_SPEC_KEY_MAX:
            findings.append(
                Finding("product_spec_key", f"规格名必须是 1..{PRODUCT_SPEC_KEY_MAX} 个字符", item_path)
            )
            continue
        if is_commercial_spec_key(str(key)):
            findings.append(
                Finding(
                    "product_spec_commercial_truth",
                    "价格、币种、优惠与落地页属于 OfferVersion，不能作为产品规格",
                    item_path,
                )
            )
        if not isinstance(value, str) or len(value) > PRODUCT_SPEC_VALUE_MAX:
            findings.append(
                Finding("product_spec_value", f"规格值必须是不超过 {PRODUCT_SPEC_VALUE_MAX} 字符的字符串", item_path)
            )


def _validate_model_variants(items: Any, findings: list[Finding]) -> None:
    if items is None:
        return
    if not isinstance(items, list):
        findings.append(Finding("product_variants_shape", "model_variants 必须是列表", "model_variants"))
        return
    if len(items) > PRODUCT_VARIANTS_MAX:
        findings.append(
            Finding("product_variants_too_many", f"model_variants 最多 {PRODUCT_VARIANTS_MAX} 个", "model_variants")
        )
    seen: set[str] = set()
    for index, item in enumerate(items):
        item_path = f"model_variants[{index}]"
        if not isinstance(item, dict):
            findings.append(Finding("product_variant_shape", "型号必须是对象 {label, specs}", item_path))
            continue
        label = item.get("label")
        if not _is_str(label) or len(str(label)) > PRODUCT_SPEC_KEY_MAX:
            findings.append(Finding("product_variant_label", "型号需要 label", f"{item_path}.label"))
        elif str(label).strip() in seen:
            findings.append(Finding("product_variant_duplicate", f"重复的型号: {label}", f"{item_path}.label"))
        else:
            seen.add(str(label).strip())
        validate_product_specs(item.get("specs"), findings, path=f"{item_path}.specs")
        _check_forbidden(item, PRODUCT_FORBIDDEN_KEYS, findings)


def _validate_audiences(items: Any, findings: list[Finding]) -> None:
    """客群卡：谁 / 为什么买 / 痛点。``audience`` 字符串仍是必填的主客群。"""
    if items is None:
        return
    if not isinstance(items, list):
        findings.append(Finding("product_audiences_shape", "audiences 必须是列表", "audiences"))
        return
    if len(items) > PRODUCT_AUDIENCES_MAX:
        findings.append(
            Finding("product_audiences_too_many", f"audiences 最多 {PRODUCT_AUDIENCES_MAX} 张", "audiences")
        )
    for index, item in enumerate(items):
        item_path = f"audiences[{index}]"
        if not isinstance(item, dict):
            findings.append(Finding("product_audience_card_shape", "客群卡必须是对象 {who, why, pain}", item_path))
            continue
        if not _is_str(item.get("who")):
            findings.append(Finding("product_audience_who", "客群卡需要 who", f"{item_path}.who"))
        for key in ("who", "why", "pain"):
            value = item.get(key)
            if value is not None and (not isinstance(value, str) or len(value) > PRODUCT_AUDIENCE_FIELD_MAX):
                findings.append(
                    Finding(
                        "product_audience_card_field",
                        f"{key} 必须是不超过 {PRODUCT_AUDIENCE_FIELD_MAX} 字符的字符串",
                        f"{item_path}.{key}",
                    )
                )


def _validate_source_url(value: Any, findings: list[Finding]) -> None:
    """``source_url`` 是识别来源（用户粘贴的商品链接），不是落地页（§5.2）。"""
    if value is None:
        return
    if not _is_str(value) or len(str(value)) > PRODUCT_SOURCE_URL_MAX:
        findings.append(
            Finding("product_source_url", f"source_url 必须是不超过 {PRODUCT_SOURCE_URL_MAX} 字符的链接", "source_url")
        )
        return
    lowered = str(value).strip().lower()
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        findings.append(Finding("product_source_url", "source_url 只能是 http(s) 链接", "source_url"))


def _validate_product(content: dict, findings: list[Finding]) -> None:
    _check_forbidden(content, PRODUCT_FORBIDDEN_KEYS, findings)
    if not _is_str(content.get("name")):
        findings.append(Finding("product_name", "ProductVersion 需要名称", "name"))
    if not _is_str(content.get("description")):
        findings.append(Finding("product_description", "ProductVersion 需要描述", "description"))
    if not _is_str(content.get("audience")):
        findings.append(Finding("product_audience", "ProductVersion 需要受众", "audience"))
    selling_points = content.get("selling_points")
    if selling_points is not None and not isinstance(selling_points, list):
        findings.append(Finding("product_selling_points", "selling_points 必须是列表", "selling_points"))
    validate_product_specs(content.get("specs"), findings)
    _validate_model_variants(content.get("model_variants"), findings)
    _validate_audiences(content.get("audiences"), findings)
    _validate_source_url(content.get("source_url"), findings)
    must_match = content.get("must_match")
    for index, item in enumerate(must_match if isinstance(must_match, list) else []):
        if isinstance(item, dict) and "proposed_by" in item and item.get("proposed_by") not in MUST_MATCH_PROPOSERS:
            findings.append(
                Finding(
                    "must_match_proposed_by",
                    f"proposed_by 必须是 {sorted(MUST_MATCH_PROPOSERS)} 之一",
                    f"must_match[{index}].proposed_by",
                )
            )
    validate_must_match(content.get("must_match"), findings)
    _validate_reference_bindings(content.get("reference_bindings"), findings, path="reference_bindings")
    groups = content.get("reference_group_version_ids")
    if groups is not None and not isinstance(groups, list):
        findings.append(
            Finding("product_reference_groups", "reference_group_version_ids 必须是列表", "reference_group_version_ids")
        )


def _validate_offer(content: dict, findings: list[Finding]) -> None:
    _check_forbidden(content, OFFER_FORBIDDEN_KEYS, findings)
    if not _is_str(content.get("product_version_id")):
        findings.append(Finding("offer_product", "OfferVersion 必须显式引用一个 ProductVersion", "product_version_id"))
    item = content.get("item")
    if not isinstance(item, dict):
        findings.append(Finding("offer_item", "OfferVersion 需要结构化 item（sku/variant/数量/套装）", "item"))
    else:
        if not _is_str(item.get("sku")):
            findings.append(Finding("offer_sku", "item.sku 不能为空", "item.sku"))
        quantity = item.get("quantity")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1:
            findings.append(Finding("offer_quantity", "item.quantity 必须是正整数", "item.quantity"))
        if "variant_label" in item and not _is_str(item.get("variant_label")):
            findings.append(Finding("offer_variant_label", "variant_label 不能为空字符串", "item.variant_label"))
        bundle = item.get("bundle_components")
        if bundle is not None and not isinstance(bundle, list):
            findings.append(Finding("offer_bundle", "bundle_components 必须是列表", "item.bundle_components"))
    if not _is_str(content.get("landing_page_url")):
        findings.append(Finding("offer_landing_page", "OfferVersion 必须显式声明 Landing Page", "landing_page_url"))
    markets = content.get("market_scope")
    if not isinstance(markets, list) or not markets:
        findings.append(Finding("offer_market_scope", "OfferVersion 必须显式声明市场范围", "market_scope"))
    else:
        for index, market in enumerate(markets):
            if not isinstance(market, str) or len(market.strip()) != 2:
                findings.append(
                    Finding("offer_market_code", "市场必须是 2 位 ISO 国家代码", f"market_scope[{index}]")
                )
    claims = content.get("claim_constraints")
    if claims is not None and not isinstance(claims, list):
        findings.append(Finding("offer_claims", "claim_constraints 必须是列表", "claim_constraints"))
    valid_from = _parse_date(content.get("valid_from"))
    if valid_from is None:
        findings.append(Finding("offer_valid_from", "OfferVersion 必须声明业务有效期起点", "valid_from"))
    valid_to_raw = content.get("valid_to")
    if valid_to_raw not in (None, ""):
        valid_to = _parse_date(valid_to_raw)
        if valid_to is None:
            findings.append(Finding("offer_valid_to", "valid_to 必须是 ISO 日期", "valid_to"))
        elif valid_from is not None and valid_to < valid_from:
            findings.append(Finding("offer_validity_range", "valid_to 不能早于 valid_from", "valid_to"))


def _validate_market_mapping(content: dict, findings: list[Finding]) -> None:
    if not _is_str(content.get("offer_version_id")):
        findings.append(Finding("mapping_offer", "MarketSourceMappingVersion 必须引用一个 OfferVersion", "offer_version_id"))
    # DataSourceVersion 归 #20 所有；这里只强制显式、非空的不透明 ID。
    if not _is_str(content.get("data_source_version_id")):
        findings.append(
            Finding("mapping_data_source", "必须显式声明 data_source_version_id，不存在隐式默认数据源", "data_source_version_id")
        )
    external = content.get("external_object")
    if not isinstance(external, dict):
        findings.append(Finding("mapping_external_object", "必须显式声明外部对象", "external_object"))
    else:
        kind = external.get("kind")
        if kind not in {"product", "variant", "price_list", "shipping_rule"}:
            findings.append(
                Finding("mapping_external_kind", "external_object.kind 必须是 product/variant/price_list/shipping_rule", "external_object.kind")
            )
        if not _is_str(external.get("id")):
            findings.append(
                Finding("mapping_external_id", "external_object.id 不能为空；不得从标题或第一个 Variant 推断", "external_object.id")
            )
    market = content.get("market")
    if not isinstance(market, str) or len(market.strip()) != 2:
        findings.append(Finding("mapping_market", "market 必须是 2 位 ISO 国家代码", "market"))
    if content.get("execution_mode") not in EXECUTION_MODES:
        findings.append(Finding("mapping_execution_mode", "必须显式声明 execution_mode", "execution_mode"))


def _validate_person(content: dict, findings: list[Finding], *, today: date | None = None) -> None:
    today = today or date.today()
    if not _is_str(content.get("name")):
        findings.append(Finding("person_name", "PersonVersion 需要名称", "name"))
    person_type = content.get("person_type")
    if person_type not in PERSON_TYPES:
        findings.append(
            Finding("person_type", "person_type 必须是 fictional 或 licensed_talent", "person_type")
        )
    if not _is_str(content.get("age_expression")):
        findings.append(Finding("person_age_expression", "PersonVersion 需要年龄表达", "age_expression"))
    fragments = content.get("prompt_fragments")
    if fragments is not None and not isinstance(fragments, list):
        findings.append(Finding("person_prompt_fragments", "prompt_fragments 必须是列表", "prompt_fragments"))
    _validate_reference_bindings(content.get("reference_bindings"), findings, path="reference_bindings")

    rights = content.get("rights")
    if not isinstance(rights, dict):
        findings.append(Finding("person_rights", "PersonVersion 必须声明权利范围", "rights"))
        return
    regions = rights.get("regions")
    channels = rights.get("channels")
    if not isinstance(regions, list) or not regions:
        findings.append(Finding("person_rights_regions", "必须声明地域范围", "rights.regions"))
    if not isinstance(channels, list) or not channels:
        findings.append(Finding("person_rights_channels", "必须声明渠道范围", "rights.channels"))
    valid_to = _parse_date(rights.get("valid_to"))
    if person_type == PERSON_LICENSED:
        # §8：品牌人才必须有明确授权证据与有效期。
        if not _is_str(rights.get("evidence")):
            findings.append(Finding("person_rights_evidence", "品牌人才必须提供授权证据", "rights.evidence"))
        if valid_to is None:
            findings.append(Finding("person_rights_valid_to", "品牌人才授权必须有有效期", "rights.valid_to"))
    if valid_to is not None and valid_to < today:
        findings.append(Finding("person_rights_expired", f"人物授权已于 {valid_to} 过期", "rights.valid_to"))
    state = rights.get("state", RIGHTS_GRANTED)
    if state in {RIGHTS_REVOKED, RIGHTS_EXPIRED, RIGHTS_MISSING}:
        findings.append(Finding("person_rights_state", f"人物授权状态为 {state}，不能发布", "rights.state"))


def _validate_reference_asset(content: dict, findings: list[Finding], *, today: date | None = None) -> None:
    today = today or date.today()
    if not _is_str(content.get("object_id")):
        findings.append(Finding("reference_object", "ReferenceAssetVersion 必须引用一个已放行的对象", "object_id"))
    if not _is_str(content.get("source")):
        findings.append(Finding("reference_source", "必须声明来源", "source"))
    rights = content.get("rights")
    if not isinstance(rights, dict):
        findings.append(Finding("reference_rights", "ReferenceAssetVersion 必须声明权利状态", "rights"))
        return
    reason = check_rights_eligible(rights, today=today)
    if reason is not None:
        findings.append(Finding("reference_rights_ineligible", reason, "rights"))
    if not _is_str(rights.get("scope")):
        findings.append(Finding("reference_rights_scope", "必须声明授权范围", "rights.scope"))
    if not _is_str(rights.get("declared_by")):
        findings.append(Finding("reference_rights_declared_by", "必须记录操作者声明", "rights.declared_by"))

    file_evidence = content.get("file")
    if not isinstance(file_evidence, dict):
        findings.append(Finding(
            "reference_file_evidence",
            "ReferenceAssetVersion 必须冻结文件证据 (file dict)",
            "file",
        ))
    else:
        for field in ("content_sha256", "mime_type", "byte_size"):
            if not file_evidence.get(field):
                findings.append(Finding(
                    f"reference_evidence_{field}",
                    f"ReferenceAssetVersion 必须冻结 file.{field} 证据",
                    f"file.{field}",
                ))
        for field in ("width", "height"):
            if file_evidence.get(field) is None:
                findings.append(Finding(
                    f"reference_evidence_{field}",
                    f"ReferenceAssetVersion 必须冻结 file.{field} 证据",
                    f"file.{field}",
                ))
    if not isinstance(content.get("gate_checks"), list):
        findings.append(Finding(
            "reference_gate_evidence",
            "ReferenceAssetVersion 必须冻结 gate check 证据",
            "gate_checks",
        ))
    derivative = content.get("derivative")
    if not isinstance(derivative, dict):
        findings.append(Finding(
            "reference_derivative_evidence",
            "ReferenceAssetVersion 必须冻结受控派生元数据",
            "derivative",
        ))
    else:
        for field in ("sha256", "mime_type", "byte_size"):
            if not derivative.get(field):
                findings.append(Finding(
                    f"reference_derivative_{field}",
                    f"ReferenceAssetVersion 必须冻结 derivative.{field} 证据",
                    f"derivative.{field}",
                ))
        for field in ("width", "height"):
            if derivative.get(field) is None:
                findings.append(Finding(
                    f"reference_derivative_{field}",
                    f"ReferenceAssetVersion 必须冻结 derivative.{field} 证据",
                    f"derivative.{field}",
                ))


def _validate_reference_group(content: dict, findings: list[Finding]) -> None:
    members = content.get("members")
    if not isinstance(members, list) or not members:
        findings.append(Finding("group_members", "ReferenceGroupVersion 必须有明确成员", "members"))
        return
    seen: set[str] = set()
    for index, member in enumerate(members):
        path = f"members[{index}]"
        if not isinstance(member, dict):
            findings.append(Finding("group_member_shape", "成员必须是对象", path))
            continue
        version_id = member.get("reference_version_id")
        if not _is_str(version_id):
            findings.append(Finding("group_member_target", "成员需要 reference_version_id", path))
        elif version_id in seen:
            findings.append(Finding("group_member_duplicate", "同一参考版本在组内重复", path))
        else:
            seen.add(str(version_id))
        if not _is_str(member.get("role")):
            findings.append(Finding("group_member_role", "成员需要业务角色（如整机、接口特写、白底棚拍）", path))


def _validate_selection_rule(content: dict, findings: list[Finding]) -> None:
    rules = content.get("rules")
    if not isinstance(rules, list) or not rules:
        findings.append(Finding("rule_empty", "ReferenceSelectionRuleVersion 至少需要一条规则", "rules"))
        return
    for index, rule in enumerate(rules):
        path = f"rules[{index}]"
        if not isinstance(rule, dict):
            findings.append(Finding("rule_shape", "规则必须是对象", path))
            continue
        match = rule.get("match")
        if not isinstance(match, dict) or not match:
            findings.append(Finding("rule_match", "规则需要 match 条件", f"{path}.match"))
        else:
            for key, why in RULE_FORBIDDEN_MATCH_KEYS.items():
                if key in match:
                    findings.append(Finding("rule_forbidden_match", why, f"{path}.match.{key}"))
            if not any(_is_str(match.get(k)) for k in ("dimension_id", "value_id", "aspect_ratio", "intent")):
                findings.append(
                    Finding(
                        "rule_match_stable_id",
                        "match 必须至少包含稳定 dimension_id/value_id/aspect_ratio/intent 之一",
                        f"{path}.match",
                    )
                )
        if not _is_str(rule.get("group_version_id")):
            findings.append(Finding("rule_group", "规则必须指向一个 ReferenceGroupVersion", f"{path}.group_version_id"))
        priority = rule.get("priority")
        if not isinstance(priority, int) or isinstance(priority, bool) or priority < 0:
            findings.append(Finding("rule_priority", "priority 必须是非负整数", f"{path}.priority"))


# --------------------------------------------------------------------------
# 维度模板/Schema、Prompt 规则、数据源、人工事实集与真值解析策略内容校验（#20）。
# --------------------------------------------------------------------------

# 数据源的来源类型决定执行模式（#396）：受控 Mock 是独立的来源，只能是 mock；人工和
# Shopify 只能是 real。其余组合解析 Truth 时会撞 ck_tsa_manual_adapter / ck_tsa_mock_fields。
# 草稿表单的选项（DataSourceDraftEditor.tsx 的 DATA_SOURCE_ADAPTERS）由守护测试对照本表。
ADAPTER_EXECUTION_MODES: dict[str, str] = {
    "manual": EXECUTION_MODE_REAL,
    "shopify": EXECUTION_MODE_REAL,
    "mock": EXECUTION_MODE_MOCK,
}
ADAPTER_KINDS = frozenset(ADAPTER_EXECUTION_MODES)
CARDINALITIES = frozenset({"one_per_dimension"})
CONSTRAINT_TYPES = frozenset({"requires", "excludes", "max_occurrence"})
# 人工事实集和真值解析策略只接受 Truth 事实族（#26），值结构见 fact_types.FACT_VALUE_FIELDS。
FACT_TYPES = FACT_FAMILIES
ON_FAILURE_MODES = frozenset({"block", "degrade", "skip"})
PRICE_FORBIDDEN_ON_FAILURE = frozenset({"degrade"})

_TOKEN_REF_RE = re.compile(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}")


def _validate_dimension_template(content: dict, findings: list[Finding]) -> None:
    dims = content.get("dimensions")
    if not isinstance(dims, list) or not dims:
        findings.append(Finding("template_dimensions_required", "至少需要一个维度", "dimensions"))
        return
    dim_ids: set[str] = set()
    all_value_ids: set[str] = set()
    for i, dim in enumerate(dims):
        path = f"dimensions[{i}]"
        if not isinstance(dim, dict):
            findings.append(Finding("template_dim_shape", "维度必须是对象", path))
            continue
        dim_id = dim.get("id")
        if not _is_str(dim_id):
            findings.append(Finding("template_dim_id", "维度需要稳定 ID", path))
        elif dim_id in dim_ids:
            findings.append(Finding("template_duplicate_dimension_id",
                                    f"重复维度 ID: {dim_id}", path))
        else:
            dim_ids.add(str(dim_id))
        if not _is_str(dim.get("name")):
            findings.append(Finding("template_dim_name", "维度需要名称", path))
        values = dim.get("values")
        if not isinstance(values, list) or not values:
            findings.append(Finding("template_dim_values", "维度至少需要一个取值", f"{path}.values"))
            continue
        for j, val in enumerate(values):
            vpath = f"{path}.values[{j}]"
            if not isinstance(val, dict):
                findings.append(Finding("template_val_shape", "取值必须是对象", vpath))
                continue
            val_id = val.get("id")
            if not _is_str(val_id):
                findings.append(Finding("template_val_id", "取值需要稳定 ID", vpath))
            elif val_id in all_value_ids:
                findings.append(Finding("template_duplicate_value_id",
                                        f"重复取值 ID: {val_id}", vpath))
            else:
                all_value_ids.add(str(val_id))
            if not isinstance(val.get("positive_fragments"), list):
                findings.append(Finding("template_val_pos_frag", "positive_fragments 必须是列表", vpath))
            if not isinstance(val.get("negative_fragments"), list):
                findings.append(Finding("template_val_neg_frag", "negative_fragments 必须是列表", vpath))
    # 校验约束是否引用了已知维度。
    constraints = content.get("constraints") or []
    if not isinstance(constraints, list):
        findings.append(Finding("template_constraints_shape", "constraints 必须是列表", "constraints"))
    else:
        for i, c in enumerate(constraints):
            cpath = f"constraints[{i}]"
            if not isinstance(c, dict):
                findings.append(Finding("template_constraint_shape", "约束必须是对象", cpath))
                continue
            ctype = c.get("type")
            if ctype not in CONSTRAINT_TYPES:
                findings.append(Finding("template_constraint_type",
                                        f"type 必须是 {sorted(CONSTRAINT_TYPES)} 之一", cpath))
                continue
            if ctype == "requires":
                for fld in ("from_dimension_id", "to_dimension_id"):
                    if c.get(fld) not in dim_ids:
                        findings.append(Finding("template_constraint_ref",
                                                f"{fld} 引用了未知维度", f"{cpath}.{fld}"))
            elif ctype == "excludes":
                for fld in ("dimension_id_a", "dimension_id_b"):
                    if c.get(fld) not in dim_ids:
                        findings.append(Finding("template_constraint_ref",
                                                f"{fld} 引用了未知维度", f"{cpath}.{fld}"))
            elif ctype == "max_occurrence":
                if c.get("dimension_id") not in dim_ids:
                    findings.append(Finding("template_constraint_ref",
                                            "dimension_id 引用了未知维度", f"{cpath}.dimension_id"))
                max_val = c.get("max")
                if not isinstance(max_val, int) or isinstance(max_val, bool) or max_val < 1:
                    findings.append(Finding("template_max_occurrence",
                                            "max 必须是正整数", f"{cpath}.max"))
    cardinality = content.get("combination_cardinality")
    if cardinality not in CARDINALITIES:
        findings.append(Finding("template_cardinality",
                                f"combination_cardinality 必须是 {sorted(CARDINALITIES)} 之一",
                                "combination_cardinality"))


def _validate_dimension_schema(content: dict, findings: list[Finding]) -> None:
    if not _is_str(content.get("product_entry_id")):
        findings.append(Finding("schema_product_entry_required",
                                "DimensionSchema 必须绑定 product_entry_id", "product_entry_id"))
    dims = content.get("dimensions")
    if not isinstance(dims, list) or not dims:
        findings.append(Finding("schema_dimensions_required", "至少需要一个维度", "dimensions"))
        return
    dim_ids: set[str] = set()
    for i, dim in enumerate(dims):
        path = f"dimensions[{i}]"
        if not isinstance(dim, dict):
            findings.append(Finding("schema_dim_shape", "维度必须是对象", path))
            continue
        dim_id = dim.get("id")
        if not _is_str(dim_id):
            findings.append(Finding("schema_dim_id", "维度需要稳定 ID", path))
        elif dim_id in dim_ids:
            findings.append(Finding("schema_duplicate_dim_id", f"重复维度 ID: {dim_id}", path))
        else:
            dim_ids.add(str(dim_id))
        values = dim.get("values")
        if not isinstance(values, list) or not values:
            findings.append(Finding("schema_dim_values", "维度至少需要一个取值", f"{path}.values"))
    cardinality = content.get("combination_cardinality")
    if cardinality not in CARDINALITIES:
        findings.append(Finding("schema_cardinality",
                                f"combination_cardinality 必须是 {sorted(CARDINALITIES)} 之一",
                                "combination_cardinality"))


def _validate_prompt_policy(content: dict, findings: list[Finding]) -> None:
    tokens = content.get("registered_tokens")
    if not isinstance(tokens, list):
        findings.append(Finding("policy_tokens_shape", "registered_tokens 必须是列表", "registered_tokens"))
    else:
        seen_names: set[str] = set()
        for i, tok in enumerate(tokens):
            tpath = f"registered_tokens[{i}]"
            if not isinstance(tok, dict):
                findings.append(Finding("policy_token_shape", "token 必须是对象", tpath))
                continue
            name = tok.get("name")
            if not _is_str(name):
                findings.append(Finding("policy_token_name", "token 需要 name", tpath))
            elif name in seen_names:
                findings.append(Finding("policy_duplicate_token", f"重复 token: {name}", tpath))
            else:
                seen_names.add(str(name))
    phrases = content.get("locked_phrases")
    if phrases is not None and not isinstance(phrases, list):
        findings.append(Finding("policy_phrases_shape", "locked_phrases 必须是列表", "locked_phrases"))
    # 单遍递归 token 检测：token 的 default 里不能引用另一个 token（不追多跳链）。
    if isinstance(tokens, list):
        token_names = {t.get("name") for t in tokens if isinstance(t, dict) and t.get("name")}
        for i, tok in enumerate(tokens):
            if not isinstance(tok, dict):
                continue
            default = tok.get("default")
            if isinstance(default, str):
                refs = _TOKEN_REF_RE.findall(default)
                for ref in refs:
                    if ref in token_names:
                        findings.append(Finding(
                            "policy_recursive_token",
                            f"token {tok.get('name')!r} 的 default 引用了另一个 token {{{{{ref}}}}}",
                            f"registered_tokens[{i}].default",
                        ))
    entries = content.get("allowed_template_entry_ids")
    if entries is not None and not isinstance(entries, list):
        findings.append(Finding("policy_allowed_templates_shape",
                                "allowed_template_entry_ids 必须是列表", "allowed_template_entry_ids"))


def _validate_data_source(content: dict, findings: list[Finding]) -> None:
    adapter_kind = content.get("adapter_kind")
    execution_mode = content.get("execution_mode")
    if adapter_kind not in ADAPTER_KINDS:
        findings.append(Finding("ds_adapter_kind", f"adapter_kind 必须是 {sorted(ADAPTER_KINDS)} 之一",
                                "adapter_kind"))
    if execution_mode not in EXECUTION_MODES:
        findings.append(Finding("ds_execution_mode",
                                f"必须显式声明 execution_mode，取值为 {sorted(EXECUTION_MODES)} 之一",
                                "execution_mode"))
    elif adapter_kind in ADAPTER_KINDS and execution_mode != ADAPTER_EXECUTION_MODES[adapter_kind]:
        pairs = "、".join(f"{kind}/{mode}" for kind, mode in ADAPTER_EXECUTION_MODES.items())
        findings.append(Finding(
            "ds_execution_mode_mismatch",
            f"adapter_kind {adapter_kind!r} 的 execution_mode 必须是 "
            f"{ADAPTER_EXECUTION_MODES[adapter_kind]!r}，合法组合只有 {pairs}",
            "execution_mode",
        ))
    config = content.get("config")
    if config is not None and not isinstance(config, dict):
        findings.append(Finding("ds_config_shape", "config 必须是对象", "config"))


def _fact_date(value: str) -> datetime | None:
    """A fact's ``valid_from``/``valid_to`` as Truth reads it, or None.

    Same reading as ``app.localization.truth._parse_content_date``: an ISO
    date or datetime, without an offset taken as UTC.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _validate_fact_window(fact: dict, fpath: str, findings: list[Finding]) -> None:
    """Both ends of a fact's validity are ISO dates and it starts before it ends (#23).

    Truth serves a fact while ``valid_from <= now < valid_to``, so an end on
    or before the start is a fact that is never valid.
    """
    starts = ends = None
    if not _is_str(fact.get("valid_from")):
        findings.append(Finding("fact_valid_from", "必须声明 valid_from", f"{fpath}.valid_from"))
    elif (starts := _fact_date(fact["valid_from"])) is None:
        findings.append(Finding("fact_date_format",
                                "valid_from 必须是 ISO 日期或时间，例如 2026-01-31", f"{fpath}.valid_from"))
    if not _is_str(fact.get("valid_to")):
        findings.append(Finding("fact_valid_to", "必须声明 valid_to", f"{fpath}.valid_to"))
    elif (ends := _fact_date(fact["valid_to"])) is None:
        findings.append(Finding("fact_date_format",
                                "valid_to 必须是 ISO 日期或时间，例如 2026-01-31", f"{fpath}.valid_to"))
    if starts is not None and ends is not None and starts >= ends:
        findings.append(Finding("fact_valid_range", "valid_to 必须晚于 valid_from", f"{fpath}.valid_to"))


def _validate_manual_fact_set(content: dict, findings: list[Finding]) -> None:
    if not _is_str(content.get("data_source_entry_id")):
        findings.append(Finding("fact_set_ds_required",
                                "必须显式声明 data_source_entry_id", "data_source_entry_id"))
    facts = content.get("facts")
    if not isinstance(facts, list) or not facts:
        findings.append(Finding("fact_set_empty", "至少需要一条事实", "facts"))
        return
    seen_ids: set[str] = set()
    for i, fact in enumerate(facts):
        fpath = f"facts[{i}]"
        if not isinstance(fact, dict):
            findings.append(Finding("fact_shape", "事实必须是对象", fpath))
            continue
        fact_id = fact.get("fact_id")
        if not _is_str(fact_id):
            findings.append(Finding("fact_id_required", "事实需要稳定 fact_id", f"{fpath}.fact_id"))
        elif fact_id in seen_ids:
            findings.append(Finding("fact_duplicate_id", f"重复 fact_id: {fact_id}", f"{fpath}.fact_id"))
        else:
            seen_ids.add(str(fact_id))
        fact_type = fact.get("fact_type")
        known_type = isinstance(fact_type, str) and fact_type in FACT_TYPES
        if not known_type:
            findings.append(Finding("fact_type_unknown",
                                    f"fact_type 必须是 {sorted(FACT_TYPES)} 之一", f"{fpath}.fact_type"))
        cc = fact.get("country_code")
        if not isinstance(cc, str) or len(cc) != 2 or cc != cc.upper():
            findings.append(Finding("fact_country_code", "country_code 必须是大写 2 位 ISO", f"{fpath}.country_code"))
        _validate_fact_window(fact, fpath, findings)
        if not _is_str(fact.get("evidence_source")):
            findings.append(Finding("fact_evidence_source_required",
                                    "必须声明证据来源 evidence_source", f"{fpath}.evidence_source"))
        if known_type:
            # 值结构与 Truth 适配器解析时用的是同一张表、同一套规则。
            for problem in fact_value_problems(fact_type, fact):
                findings.append(Finding(problem.code, problem.message, f"{fpath}.{problem.field}"))


def _validate_truth_resolution_policy(content: dict, findings: list[Finding]) -> None:
    rules = content.get("rules")
    if not isinstance(rules, list) or not rules:
        findings.append(Finding("trp_rules_required", "至少需要一条规则", "rules"))
        return
    seen_types: set[str] = set()
    for i, rule in enumerate(rules):
        rpath = f"rules[{i}]"
        if not isinstance(rule, dict):
            findings.append(Finding("trp_rule_shape", "规则必须是对象", rpath))
            continue
        ft = rule.get("fact_type")
        if not _is_str(ft):
            findings.append(Finding("trp_fact_type", "规则需要 fact_type", rpath))
        elif ft not in FACT_TYPES:
            findings.append(Finding("trp_fact_type_unknown",
                                    f"fact_type 必须是 {sorted(FACT_TYPES)} 之一", f"{rpath}.fact_type"))
        elif ft in seen_types:
            findings.append(Finding("trp_duplicate_fact_type",
                                    f"重复 fact_type: {ft}", rpath))
        else:
            seen_types.add(str(ft))
        sources = rule.get("source_priority")
        if not isinstance(sources, list) or not sources:
            findings.append(Finding("trp_source_priority_empty",
                                    "source_priority 不能为空", f"{rpath}.source_priority"))
        elif len(sources) != len(set(sources)):
            findings.append(Finding("trp_source_priority_dups",
                                    "source_priority 不能有重复", f"{rpath}.source_priority"))
        staleness = rule.get("max_staleness_seconds")
        if not isinstance(staleness, (int, float)) or isinstance(staleness, bool) or staleness < 0:
            findings.append(Finding("trp_staleness", "max_staleness_seconds 必须 >= 0",
                                    f"{rpath}.max_staleness_seconds"))
        on_fail = rule.get("on_failure")
        if on_fail not in ON_FAILURE_MODES:
            findings.append(Finding("trp_on_failure",
                                    f"on_failure 必须是 {sorted(ON_FAILURE_MODES)} 之一",
                                    f"{rpath}.on_failure"))
        elif ft == "price" and on_fail in PRICE_FORBIDDEN_ON_FAILURE:
            findings.append(Finding("trp_price_no_degrade",
                                    "price 类型不能使用 degrade 模式", f"{rpath}.on_failure"))


def _validate_brand_localization_policy_content(
    content: dict, *, version: int,
) -> list[Finding]:
    findings: list[Finding] = []
    for required in ("claim_switches", "style_guide"):
        if required not in content:
            findings.append(Finding(
                code=f"brand_policy.missing_{required}",
                message=f"Required field '{required}' missing",
                path=required,
            ))
    if "claim_switches" in content and not isinstance(content["claim_switches"], dict):
        findings.append(Finding(
            code="brand_policy.invalid_claim_switches",
            message="claim_switches must be an object",
            path="claim_switches",
        ))
    return findings


def _validate_brand_localization_policy(content: dict, findings: list[Finding]) -> None:
    findings.extend(
        _validate_brand_localization_policy_content(
            content, version=content_schema_version(ENTRY_BRAND_LOCALIZATION_POLICY)
        )
    )


_VALIDATORS = {
    ENTRY_PRODUCT: _validate_product,
    ENTRY_OFFER: _validate_offer,
    ENTRY_PERSON: _validate_person,
    ENTRY_REFERENCE_ASSET: _validate_reference_asset,
    ENTRY_REFERENCE_GROUP: _validate_reference_group,
    ENTRY_SELECTION_RULE: _validate_selection_rule,
    ENTRY_MARKET_MAPPING: _validate_market_mapping,
    ENTRY_DIMENSION_TEMPLATE: _validate_dimension_template,
    ENTRY_DIMENSION_SCHEMA: _validate_dimension_schema,
    ENTRY_PROMPT_POLICY: _validate_prompt_policy,
    ENTRY_DATA_SOURCE: _validate_data_source,
    ENTRY_MANUAL_FACT_SET: _validate_manual_fact_set,
    ENTRY_TRUTH_RESOLUTION_POLICY: _validate_truth_resolution_policy,
    ENTRY_BRAND_LOCALIZATION_POLICY: _validate_brand_localization_policy,
}


def validate_content(entry_type: str, content: dict, *, today: date | None = None) -> ValidationReport:
    """纯 Schema/形状校验。跨版本引用与权限由 service 层补充。"""
    if entry_type not in ENTRY_TYPES:
        raise LibraryContractError(f"未知创意库条目类型: {entry_type}", code="library_entry_type_unknown")
    findings: list[Finding] = []
    if not isinstance(content, dict):
        findings.append(Finding("content_shape", "内容必须是对象"))
        return ValidationReport(entry_type, canonical_hash(content), findings)
    validator = _VALIDATORS[entry_type]
    if entry_type in {ENTRY_PERSON, ENTRY_REFERENCE_ASSET}:
        validator(content, findings, today=today)  # type: ignore[call-arg]
    else:
        validator(content, findings)  # type: ignore[call-arg]
    return ValidationReport(entry_type, canonical_hash(content), findings)


# --------------------------------------------------------------------------
# 依赖抽取（契约 §14 DependencyIndex）
# --------------------------------------------------------------------------

REL_PRODUCT_REFERENCE = "product_reference"
REL_PRODUCT_REFERENCE_GROUP = "product_reference_group"
REL_PRODUCT_SELECTION_RULE = "product_selection_rule"
REL_MUST_MATCH_EVIDENCE = "must_match_evidence"
REL_OFFER_PRODUCT = "offer_product"
REL_MAPPING_OFFER = "mapping_offer"
REL_PERSON_REFERENCE = "person_reference"
REL_GROUP_MEMBER = "group_member"
REL_RULE_GROUP = "rule_group"

# 维度模板/Schema、Prompt 规则、数据源与真值解析策略的依赖关系（#20）。
REL_SCHEMA_TEMPLATE = "schema_template"
REL_SCHEMA_PRODUCT = "schema_product"
REL_POLICY_TEMPLATE = "policy_template"
REL_FACT_SET_DATA_SOURCE = "fact_set_data_source"
REL_TRUTH_POLICY_DATA_SOURCE = "truth_policy_data_source"


def extract_dependencies(entry_type: str, content: dict) -> list[DependencyEdge]:
    """返回该内容显式引用的 LibraryVersion。空字符串与 None 被忽略。"""
    edges: list[DependencyEdge] = []

    def items(value: Any) -> list[Any]:
        # Validation owns shape errors. Dependency extraction must still be
        # total for invalid drafts so impact previews can return those errors
        # instead of turning a client-correctable shape issue into HTTP 500.
        return value if isinstance(value, list) else []

    def add(value: Any, relation: str, path: str) -> None:
        if _is_str(value):
            edges.append(DependencyEdge(str(value).strip(), relation, path))

    if not isinstance(content, dict):
        return edges

    if entry_type == ENTRY_PRODUCT:
        for index, item in enumerate(items(content.get("reference_bindings"))):
            if isinstance(item, dict):
                add(item.get("reference_version_id"), REL_PRODUCT_REFERENCE, f"reference_bindings[{index}]")
        for index, group_id in enumerate(items(content.get("reference_group_version_ids"))):
            add(group_id, REL_PRODUCT_REFERENCE_GROUP, f"reference_group_version_ids[{index}]")
        add(
            content.get("default_reference_selection_rule_version_id"),
            REL_PRODUCT_SELECTION_RULE,
            "default_reference_selection_rule_version_id",
        )
        for index, item in enumerate(items(content.get("must_match"))):
            if isinstance(item, dict):
                for j, evidence in enumerate(items(item.get("evidence_reference_version_ids"))):
                    add(evidence, REL_MUST_MATCH_EVIDENCE, f"must_match[{index}].evidence_reference_version_ids[{j}]")
    elif entry_type == ENTRY_OFFER:
        add(content.get("product_version_id"), REL_OFFER_PRODUCT, "product_version_id")
    elif entry_type == ENTRY_MARKET_MAPPING:
        add(content.get("offer_version_id"), REL_MAPPING_OFFER, "offer_version_id")
    elif entry_type == ENTRY_PERSON:
        for index, item in enumerate(items(content.get("reference_bindings"))):
            if isinstance(item, dict):
                add(item.get("reference_version_id"), REL_PERSON_REFERENCE, f"reference_bindings[{index}]")
    elif entry_type == ENTRY_REFERENCE_GROUP:
        for index, member in enumerate(items(content.get("members"))):
            if isinstance(member, dict):
                add(member.get("reference_version_id"), REL_GROUP_MEMBER, f"members[{index}]")
    elif entry_type == ENTRY_SELECTION_RULE:
        for index, rule in enumerate(items(content.get("rules"))):
            if isinstance(rule, dict):
                add(rule.get("group_version_id"), REL_RULE_GROUP, f"rules[{index}].group_version_id")
    elif entry_type == ENTRY_DIMENSION_SCHEMA:
        add(
            content.get("derived_from_template_version_id"),
            REL_SCHEMA_TEMPLATE,
            "derived_from_template_version_id",
        )
        add(content.get("product_entry_id"), REL_SCHEMA_PRODUCT, "product_entry_id")
    elif entry_type == ENTRY_PROMPT_POLICY:
        for index, template_id in enumerate(items(content.get("allowed_template_entry_ids"))):
            add(template_id, REL_POLICY_TEMPLATE, f"allowed_template_entry_ids[{index}]")
    elif entry_type == ENTRY_MANUAL_FACT_SET:
        add(content.get("data_source_entry_id"), REL_FACT_SET_DATA_SOURCE, "data_source_entry_id")
    elif entry_type == ENTRY_TRUTH_RESOLUTION_POLICY:
        for i, rule in enumerate(items(content.get("rules"))):
            if not isinstance(rule, dict):
                continue
            for j, ds_id in enumerate(items(rule.get("source_priority"))):
                add(ds_id, REL_TRUTH_POLICY_DATA_SOURCE, f"rules[{i}].source_priority[{j}]")
    elif entry_type == ENTRY_BRAND_LOCALIZATION_POLICY:
        # Dispatched explicitly (#24). Current content shape (claim_switches,
        # style_guide) carries no Library version references yet; a later
        # ticket wires referenced Library versions (e.g. base terminology
        # sets) here once that content shape is defined.
        pass
    return edges


# 每种关系允许的被引用条目类型。跨类型乱引用是阻断错误。
RELATION_TARGET_TYPES = {
    REL_PRODUCT_REFERENCE: ENTRY_REFERENCE_ASSET,
    REL_PRODUCT_REFERENCE_GROUP: ENTRY_REFERENCE_GROUP,
    REL_PRODUCT_SELECTION_RULE: ENTRY_SELECTION_RULE,
    REL_MUST_MATCH_EVIDENCE: ENTRY_REFERENCE_ASSET,
    REL_OFFER_PRODUCT: ENTRY_PRODUCT,
    REL_MAPPING_OFFER: ENTRY_OFFER,
    REL_PERSON_REFERENCE: ENTRY_REFERENCE_ASSET,
    REL_GROUP_MEMBER: ENTRY_REFERENCE_ASSET,
    REL_RULE_GROUP: ENTRY_REFERENCE_GROUP,
    REL_SCHEMA_TEMPLATE: ENTRY_DIMENSION_TEMPLATE,
    REL_SCHEMA_PRODUCT: ENTRY_PRODUCT,
    REL_POLICY_TEMPLATE: ENTRY_DIMENSION_TEMPLATE,
    REL_FACT_SET_DATA_SOURCE: ENTRY_DATA_SOURCE,
    REL_TRUTH_POLICY_DATA_SOURCE: ENTRY_DATA_SOURCE,
}


def extract_reference_bindings(entry_type: str, content: dict) -> list[dict]:
    """把内容里的参考绑定摊平成待物化的 ReferenceBinding 行（§7.1）。"""
    rows: list[dict] = []
    if not isinstance(content, dict):
        return rows
    if entry_type in {ENTRY_PRODUCT, ENTRY_PERSON}:
        for index, item in enumerate(content.get("reference_bindings") or []):
            if not isinstance(item, dict):
                continue
            rows.append(
                {
                    "reference_version_id": str(item.get("reference_version_id") or "").strip(),
                    "purpose": str(item.get("purpose") or "").strip(),
                    "viewpoint": str(item.get("viewpoint") or "").strip(),
                    "priority": int(item.get("priority") or 0),
                    "note": str(item.get("note") or ""),
                    "conditions": dict(item.get("conditions") or {}),
                    "position": index,
                }
            )
    elif entry_type == ENTRY_REFERENCE_GROUP:
        for index, member in enumerate(content.get("members") or []):
            if not isinstance(member, dict):
                continue
            rows.append(
                {
                    "reference_version_id": str(member.get("reference_version_id") or "").strip(),
                    "purpose": "group_member",
                    "viewpoint": str(member.get("role") or "").strip(),
                    "priority": int(member.get("priority") or index),
                    "note": str(member.get("note") or ""),
                    "conditions": dict(member.get("conditions") or {}),
                    "position": index,
                }
            )
    return rows


# --------------------------------------------------------------------------
# Clone Product 安全默认（契约 §5.2 / §12）
# --------------------------------------------------------------------------

# 默认只复制低风险内容。
CLONE_PRODUCT_SAFE_KEYS = frozenset(
    {
        "name",
        "description",
        "selling_points",
        "audience",
        # Issue #161: generation semantics drafted by product intake. The
        # intake ``source_url`` is provenance of *this* product and is not
        # carried to a clone.
        "audiences",
        "specs",
        "model_variants",
        "brand_constraints",
        "must_match",
        "reference_bindings",
        "reference_group_version_ids",
        "default_reference_selection_rule_version_id",
        "default_dimension_schema_version_id",
    }
)

# 必须逐项显式选择的高风险内容。
CLONE_PRODUCT_OPTIONAL_KEYS = frozenset({"brand_sku", "prompt_policy_version_id"})

# Offer、价格、DataSource Mapping 和 Landing Page 是独立条目，Clone Product 永不携带。
CLONE_PRODUCT_NEVER_COPIED = frozenset({"offers", "market_source_mappings", "landing_page_url", "price"})


def diff_content(old: dict, new: dict) -> dict[str, dict]:
    """字段级差异，用于 revision 冲突时告诉用户到底哪里不一样（§3.3）。"""
    keys = set(old or {}) | set(new or {})
    changes: dict[str, dict] = {}
    for key in sorted(keys):
        before = (old or {}).get(key)
        after = (new or {}).get(key)
        if before != after:
            changes[key] = {"base": before, "attempted": after}
    return changes


def semantic_fields(entry_type: str) -> frozenset[str]:
    """会改变生成语义、因此必须产生新版本的字段（§5.1）。"""
    if entry_type == ENTRY_PRODUCT:
        return frozenset(
            {
                "description",
                "audience",
                "must_match",
                "reference_bindings",
                "reference_group_version_ids",
                "default_reference_selection_rule_version_id",
                "selling_points",
                "brand_constraints",
            }
        )
    return frozenset()


def iter_entry_types() -> Iterable[str]:
    return sorted(ENTRY_TYPES)
