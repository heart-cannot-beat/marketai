"""Truth 事实族：人工事实集、真值解析策略和 Truth 适配器共用的唯一事实类型词表（#26）。

以前有三套词表：人工事实集只认 price/shipping_availability/delivery_time/tax/discount，
Truth 适配器只认下面六个事实族，真值解析策略不校验 fact_type。现在三处都用这里的常量：

- ``contracts._validate_manual_fact_set``：词表外的 fact_type 报 ``fact_type_unknown``，
  每一行按所属事实族的值结构逐字段校验，发现带 ``facts[i].<字段>`` 的 path；
- ``contracts._validate_truth_resolution_policy``：词表外的 fact_type 报 ``trp_fact_type_unknown``；
- ``app/localization/fact_sources._build_fact_value``：按同一张表分派、同一套规则校验，
  不合规的行返回 FactSourceFailure（``fact_type_unsupported`` / ``fact_value_invalid``），
  交给策略的 ``on_failure`` 处理，不抛异常。

``FACT_VALUE_FIELDS[族]`` 的字段名和顺序与 fact_sources 里对应 FactValue 数据类的字段一一对应，
由守护测试 tests/test_fact_families.py 对照。旧名字（tax、shipping_availability、delivery_time）
不映射：它们的行字段和事实族的值结构对不上，映射会产生假事实；已发布的旧版本不改写。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal, Mapping

# decimal：Decimal 字符串（有限值）；currency：大写 3 位 ISO 4217；bool：JSON true/false；
# days：非负整数天数；text：非空字符串。
FieldKind = Literal["decimal", "currency", "bool", "days", "text"]

FACT_VALUE_FIELDS: dict[str, dict[str, FieldKind]] = {
    "price": {"amount": "decimal", "currency": "currency"},
    "availability": {"available": "bool"},
    "discount": {
        "discount_type": "text",
        "amount": "decimal",
        "currency": "currency",
        "conditions": "text",
    },
    "shipping": {
        "method": "text",
        "cost": "decimal",
        "currency": "currency",
        "delivery_days_min": "days",
        "delivery_days_max": "days",
    },
    "delivery": {
        "delivery_days_min": "days",
        "delivery_days_max": "days",
        "region": "text",
    },
    "product_claim": {
        "claim_type": "text",
        "claim_text": "text",
        "evidence_ref": "text",
    },
}

FACT_FAMILIES = frozenset(FACT_VALUE_FIELDS)


@dataclass(frozen=True)
class FactValueProblem:
    """一行事实里某个值字段的问题；``field`` 是行内字段名。"""

    field: str
    code: str
    message: str


def _field_problem(field: str, kind: FieldKind, value: object) -> FactValueProblem | None:
    if value is None:
        return FactValueProblem(field, "fact_value_missing", f"缺少字段 {field}")
    if kind == "decimal":
        if not isinstance(value, str):
            return FactValueProblem(field, "fact_amount_not_string", f"{field} 必须是 decimal 字符串")
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            return FactValueProblem(field, "fact_amount_parse", f"无法解析 {field}: {value!r}")
        if not parsed.is_finite():
            return FactValueProblem(field, "fact_amount_invalid", f"{field} 不能是 NaN 或 Inf")
        return None
    if kind == "currency":
        if not isinstance(value, str) or len(value) != 3 or value != value.upper():
            return FactValueProblem(field, "fact_currency", f"{field} 必须是大写 3 位 ISO 4217")
        return None
    if kind == "bool":
        if not isinstance(value, bool):
            return FactValueProblem(field, "fact_bool_invalid", f"{field} 必须是 true 或 false")
        return None
    if kind == "days":
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return FactValueProblem(field, "fact_days_invalid", f"{field} 必须是非负整数天数")
        return None
    if not isinstance(value, str) or not value.strip():
        return FactValueProblem(field, "fact_text_required", f"{field} 必须是非空字符串")
    return None


def fact_value_problems(fact_type: str, row: Mapping[str, object]) -> list[FactValueProblem]:
    """按 ``fact_type`` 的值结构检查一行事实；调用方先确认 ``fact_type in FACT_FAMILIES``。"""
    fields = FACT_VALUE_FIELDS[fact_type]
    problems = [
        problem
        for field, kind in fields.items()
        if (problem := _field_problem(field, kind, row.get(field))) is not None
    ]
    day_fields = {"delivery_days_min", "delivery_days_max"}
    if day_fields <= fields.keys() and not day_fields & {problem.field for problem in problems}:
        low, high = row["delivery_days_min"], row["delivery_days_max"]
        if low > high:  # type: ignore[operator]  # 上面已确认两者都是整数
            problems.append(FactValueProblem(
                "delivery_days_max", "fact_days_range",
                "delivery_days_max 不能小于 delivery_days_min",
            ))
    return problems
