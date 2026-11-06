"""The product's current dimension vocabulary, as an external creative is tagged.

Three sources, all keyed by stable IDs (never by label alone):

1. **Test plans** (Issue #163) -- every ``ready`` plan's factor catalog. The
   plans of one product usually reuse factors across rounds (a next round
   is drafted from the previous one's learnings), so factors are merged by
   their key and values by their normalized label; each merged value keeps
   its *members* -- the ``(plan, factor key, value id)`` triples it stands
   for -- because learning identity stays plan-local (glossary
   「维度概念键」). The plans' active directions are listed too, for the
   optional "which direction is this" assignment.
2. **Dimension Templates** the product's generation drafts selected (the
   advanced workbench), except the plans' own AI templates (already covered
   by 1). Each template dimension is its own factor; members are
   ``(template entry, dimension id, value id)`` with any explicit concept
   mapping the effective version declares.
3. **The expression mechanism catalog** (Issue #329) --
   :mod:`app.marketing.expression_catalog`, one factor 「表达机制」 every
   product has, even before it has a plan (a marketing project's product
   anchor never gets one). Its values are exactly the catalog's mechanism
   keys (never ``other:``; "none of these" stays the tagging NONE), each
   with the catalog's ``how_to_tell`` as judging guidance; members are
   ``(catalog, catalog version, mechanism key)``. It always keeps its slot
   under :data:`MAX_FACTORS`, after the plan and template factors, and its
   labels are in the interface language the vocabulary is frozen in.

The vocabulary is frozen on the external creative at admission (its hash is
part of the tagging request), so the suggestion and the confirmation refer
to exactly what the model saw. Vocabulary value ids (``v1``, ``v2`` ...) are
local to one factor of one frozen vocabulary.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.adapters.persistence.models import (
    AdTestPlanDirectionRecord,
    AdTestPlanRecord,
    AdTestPlanRunRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
)
from app.generation.models import GenerationDraftRecord
from app.generation.planner import DIRECTION_DIMENSION_ID
from app.marketing import expression_catalog
from app.platform.canonical import canonical_hash
from app.platform.tenancy import TenantContext

VOCABULARY_VERSION = "external-tagging-vocabulary/v1"
SOURCE_TEST_PLAN = "test_plan"
SOURCE_TEMPLATE = "dimension_template"
SOURCE_CATALOG = "expression_catalog"
CATALOG_FACTOR_ID = "catalog:visual_mechanism"

#: Bounds that keep the tagging prompt small and the review cards readable.
#: The catalog factor holds one of the MAX_FACTORS slots, whatever the plans hold.
MAX_FACTORS = 12
MAX_VALUES_PER_FACTOR = 16
MAX_DIRECTIONS = 24
MAX_TEMPLATES = 4


def _norm(label: Any) -> str:
    return " ".join(str(label or "").split()).casefold()


def _clean(label: Any, limit: int = 120) -> str:
    return " ".join(str(label or "").split())[:limit]


def empty() -> dict:
    return {"version": VOCABULARY_VERSION, "factors": [], "directions": [], "plans": []}


def vocabulary_hash(vocabulary: dict) -> str:
    return canonical_hash(vocabulary)


def _plans(session, ctx: TenantContext, product_id: str) -> list[tuple[int, AdTestPlanRecord]]:
    """Every plan of the product with its round number (attribution's numbering)."""
    plans = list(session.scalars(
        select(AdTestPlanRecord)
        .where(AdTestPlanRecord.workspace_id == ctx.workspace_id, AdTestPlanRecord.product_id == product_id)
        .order_by(AdTestPlanRecord.created_at, AdTestPlanRecord.id)
    ))
    return list(enumerate(plans, start=1))


def catalog_factor(language: str) -> dict:
    """The expression mechanism catalog as one factor, labelled in ``language``."""
    return {
        "factor_id": CATALOG_FACTOR_ID,
        "label": expression_catalog.FACTOR_NAMES.get(language) or expression_catalog.FACTOR_NAMES["en"],
        "source": SOURCE_CATALOG,
        "catalog_version": expression_catalog.CATALOG_VERSION,
        "values": [
            {
                "value_id": f"v{index}",
                "label": expression_catalog.display_name(entry["visual_mechanism"], language),
                "guidance": entry["how_to_tell"],
                "members": [{
                    "catalog": SOURCE_CATALOG,
                    "catalog_version": expression_catalog.CATALOG_VERSION,
                    "visual_mechanism": entry["visual_mechanism"],
                }],
            }
            for index, entry in enumerate(expression_catalog.ENTRIES, start=1)
        ],
    }


def build_vocabulary(session, ctx: TenantContext, product_id: str, *, language: str = "en") -> dict:
    vocabulary = empty()
    factors: dict[str, dict] = {}
    order: list[str] = []
    numbered = _plans(session, ctx, product_id)
    ready = [(number, plan) for number, plan in numbered if plan.status == "ready"]

    # 1. Test plans, newest first: the newest wording of a factor wins.
    for number, plan in reversed(ready):
        vocabulary["plans"].append({
            "plan_id": plan.id,
            "round": number,
            "goal": plan.goal,
            "created_at": plan.created_at.isoformat() if plan.created_at else None,
        })
        for factor in (plan.recipe_json or {}).get("factors") or []:
            key = str(factor.get("key") or "")
            if not key or key == DIRECTION_DIMENSION_ID:
                continue
            merged = factors.get(key)
            if merged is None:
                if len(order) >= MAX_FACTORS - 1:
                    continue
                merged = {
                    "factor_id": key,
                    "label": _clean(factor.get("label") or key, 80),
                    "source": SOURCE_TEST_PLAN,
                    "values": [],
                }
                factors[key] = merged
                order.append(key)
            for value in factor.get("values") or []:
                value_id = str(value.get("id") or "")
                label = _clean(value.get("label") or value_id)
                if not value_id or not label:
                    continue
                member = {"plan_id": plan.id, "factor_key": key, "value_id": value_id}
                existing = next((item for item in merged["values"] if _norm(item["label"]) == _norm(label)), None)
                if existing is not None:
                    if not any(m.get("plan_id") == plan.id for m in existing["members"]):
                        existing["members"].append(member)
                    continue
                if len(merged["values"]) >= MAX_VALUES_PER_FACTOR:
                    continue
                merged["values"].append({
                    "value_id": f"v{len(merged['values']) + 1}",
                    "label": label,
                    "members": [member],
                })
        directions = session.scalars(
            select(AdTestPlanDirectionRecord)
            .where(
                AdTestPlanDirectionRecord.workspace_id == ctx.workspace_id,
                AdTestPlanDirectionRecord.plan_id == plan.id,
                AdTestPlanDirectionRecord.removed.is_(False),
            )
            .order_by(AdTestPlanDirectionRecord.ordinal)
        )
        for direction in directions:
            if len(vocabulary["directions"]) >= MAX_DIRECTIONS:
                break
            vocabulary["directions"].append({
                "plan_id": plan.id,
                "round": number,
                "direction_key": direction.direction_key,
                "title": _clean(direction.title, 200),
            })

    # 2. Dimension Templates the product's drafts selected (not plan templates).
    plan_templates = {plan.template_entry_id for _n, plan in numbered if plan.template_entry_id}
    if numbered:
        plan_templates.update(
            entry_id for entry_id in session.scalars(
                select(AdTestPlanRunRecord.template_entry_id).where(
                    AdTestPlanRunRecord.workspace_id == ctx.workspace_id,
                    AdTestPlanRunRecord.plan_id.in_([plan.id for _n, plan in numbered]),
                )
            ) if entry_id
        )
    template_ids: list[str] = []
    for selectors in session.scalars(
        select(GenerationDraftRecord.selectors_json)
        .where(
            GenerationDraftRecord.workspace_id == ctx.workspace_id,
            GenerationDraftRecord.product_id == product_id,
        )
        .order_by(GenerationDraftRecord.created_at.desc())
    ):
        for entry_id in (selectors or {}).get("dimension_template_ids") or []:
            if isinstance(entry_id, str) and entry_id not in plan_templates and entry_id not in template_ids:
                template_ids.append(entry_id)
    for entry_id in template_ids[:MAX_TEMPLATES]:
        entry = session.scalars(
            select(LibraryEntryRecord).where(
                LibraryEntryRecord.workspace_id == ctx.workspace_id,
                LibraryEntryRecord.id == entry_id,
                LibraryEntryRecord.entry_type == "dimension_template",
                LibraryEntryRecord.lifecycle == "active",
            )
        ).one_or_none()
        if entry is None or not entry.current_version_id:
            continue
        version = session.scalars(
            select(LibraryVersionRecord).where(
                LibraryVersionRecord.workspace_id == ctx.workspace_id,
                LibraryVersionRecord.id == entry.current_version_id,
                LibraryVersionRecord.state == "effective",
            )
        ).one_or_none()
        content = version.content_json if version is not None else None
        if not isinstance(content, dict):
            continue
        mappings = {
            (str(item.get("dimension_id") or ""), str(item.get("value_id") or "")): str(item.get("concept_key_id") or "")
            for item in content.get("concept_key_mappings") or [] if isinstance(item, dict)
        }
        for dimension in content.get("dimensions") or []:
            if not isinstance(dimension, dict) or len(order) >= MAX_FACTORS - 1:
                continue
            dimension_id = str(dimension.get("id") or "")
            if not dimension_id:
                continue
            factor_id = f"tpl:{entry.id}:{dimension_id}"
            merged = {
                "factor_id": factor_id,
                "label": _clean(dimension.get("name") or dimension_id, 80),
                "source": SOURCE_TEMPLATE,
                "template_entry_id": entry.id,
                "template_name": _clean(entry.display_name, 120),
                "values": [],
            }
            for value in dimension.get("values") or []:
                if not isinstance(value, dict) or len(merged["values"]) >= MAX_VALUES_PER_FACTOR:
                    continue
                value_id = str(value.get("id") or "")
                if not value_id:
                    continue
                merged["values"].append({
                    "value_id": f"v{len(merged['values']) + 1}",
                    "label": _clean(value.get("name") or value_id),
                    "members": [{
                        "template_entry_id": entry.id,
                        "dimension_id": dimension_id,
                        "value_id": value_id,
                        "concept_key_id": mappings.get((dimension_id, value_id)) or None,
                    }],
                })
            if merged["values"]:
                factors[factor_id] = merged
                order.append(factor_id)

    # 3. The expression mechanism catalog: every product, always kept.
    vocabulary["factors"] = [factors[key] for key in order if factors[key]["values"]]
    vocabulary["factors"].append(catalog_factor(language))
    return vocabulary


def factor(vocabulary: dict, factor_id: str) -> dict | None:
    return next((item for item in vocabulary.get("factors") or [] if item.get("factor_id") == factor_id), None)


def value(factor_entry: dict, value_id: str) -> dict | None:
    return next((item for item in factor_entry.get("values") or [] if item.get("value_id") == value_id), None)


def direction(vocabulary: dict, plan_id: str, direction_key: str) -> dict | None:
    return next(
        (
            item for item in vocabulary.get("directions") or []
            if item.get("plan_id") == plan_id and item.get("direction_key") == direction_key
        ),
        None,
    )


def public_view(vocabulary: dict) -> dict:
    """What the review cards show: factors and values (no member internals)."""
    return {
        "factors": [
            {
                "factor_id": item["factor_id"],
                "label": item["label"],
                "source": item["source"],
                "values": [{"value_id": v["value_id"], "label": v["label"]} for v in item["values"]],
            }
            for item in vocabulary.get("factors") or []
        ],
        "directions": [
            {key: item.get(key) for key in ("plan_id", "round", "direction_key", "title")}
            for item in vocabulary.get("directions") or []
        ],
    }
