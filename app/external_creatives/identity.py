"""Confirmed tags -> the concept identity a generated combination carries.

A generated test-plan image's RunItem carries, per dimension, a concept
``{dimension_key: "<template entry>:<dimension id>", concept_key_id:
"dck_..."}`` (``app.generation.preview._materialize_dimension_template_
concepts``) plus the combination's ``stable_dimension_signature``
(``"<entry>:<dim>=<value>:..."``, ``app.generation.planner.enumerate_full``).
Experiments freeze exactly that (``app.experiments.service._candidate_
concepts``), and learnings place the image by plan, direction and factor
values (``app.learnings.attribution``).

An external creative gets the same identity from its confirmed tags:

* **Test-plan values** -- for each chosen vocabulary value, one concept per
  member plan that already has an AI template entry, keyed by that entry,
  the factor key and the plan-local value id: the same ``dck_`` key
  (:func:`app.generation.preview.template_concept_key_id`) every generated
  image with that value carries. The *home* plan (the assigned direction's
  plan, else the plan most chosen values belong to, newest on a tie) always
  gets its template entry created if it has none yet (a Draft entry only,
  ``app.plans.runs.ensure_template_entry``).
* **The direction**, when the user assigns one: ``<entry>:direction`` with
  the direction key, like every generated image of that direction.
* **Dimension Template values** -- ``<entry>:<dimension>`` with the version's
  explicit concept mapping, else the derived key.

The learning placement is the home plan, the direction (if any) and the
home plan's ``{factor key: value id}`` cells. The provenance stays distinct:
the concepts' evidence says ``external_tag_confirmation``, never a RunItem,
and an external creative is never Controlled-Mock output.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select

from app.adapters.persistence.models import (
    ExternalCreativeTagConfirmationRecord,
    LibraryEntryRecord,
)
from app.generation.planner import DIRECTION_DIMENSION_ID
from app.platform.tenancy import TenantContext

SOURCE_KIND = "external_tag_confirmation"


@dataclass
class Placement:
    plan_id: str | None
    direction_key: str | None
    cells: dict[str, str] = field(default_factory=dict)


def home_plan(vocabulary: dict, chosen: list[tuple[dict, dict]], direction: dict | None) -> str | None:
    """The plan the creative is learned in: the direction's, else the best-covered."""
    if direction is not None:
        return direction["plan_id"]
    counts: dict[str, int] = {}
    for _factor, value in chosen:
        for member in value.get("members") or []:
            if member.get("plan_id"):
                counts[member["plan_id"]] = counts.get(member["plan_id"], 0) + 1
    if not counts:
        return None
    newest_first = [item["plan_id"] for item in vocabulary.get("plans") or []]
    rank = {plan_id: index for index, plan_id in enumerate(newest_first)}
    return min(counts, key=lambda plan_id: (-counts[plan_id], rank.get(plan_id, len(rank))))


def placement(vocabulary: dict, chosen: list[tuple[dict, dict]], direction: dict | None) -> Placement:
    plan_id = home_plan(vocabulary, chosen, direction)
    cells: dict[str, str] = {}
    if plan_id:
        for _factor, value in chosen:
            for member in value.get("members") or []:
                if member.get("plan_id") == plan_id:
                    cells[member["factor_key"]] = member["value_id"]
    return Placement(
        plan_id=plan_id,
        direction_key=direction["direction_key"] if direction is not None else None,
        cells=cells,
    )


def lock_template_entries(session, ctx: TenantContext, entry_ids: set[str]) -> None:
    """Serialize concept-key creation with a concurrent preview of the same templates."""
    if not entry_ids:
        return
    session.scalars(
        select(LibraryEntryRecord.id)
        .where(LibraryEntryRecord.workspace_id == ctx.workspace_id, LibraryEntryRecord.id.in_(sorted(entry_ids)))
        .order_by(LibraryEntryRecord.id)
        .with_for_update()
    ).all()


def build_concepts(
    session,
    ctx: TenantContext,
    *,
    chosen: list[tuple[dict, dict]],
    direction: dict | None,
    plan_entries: dict[str, str],
) -> tuple[list[dict], str]:
    """``(concepts, stable_dimension_signature)`` for the confirmed tags.

    ``plan_entries`` maps plan id -> its AI template entry id (plans without
    an entry are skipped -- except the home plan, which the caller ensured).
    Runs in the caller's transaction; creates missing governed keys.
    """
    from app.generation.preview import ensure_template_concept_key

    raw: list[dict] = []
    for factor, value in chosen:
        for member in value.get("members") or []:
            if member.get("plan_id"):
                entry_id = plan_entries.get(member["plan_id"])
                if not entry_id:
                    continue
                raw.append({
                    "template_entry_id": entry_id,
                    "dimension_id": member["factor_key"],
                    "value_id": member["value_id"],
                    "mapped": None,
                    "label": value["label"],
                    "factor_label": factor["label"],
                    "test_plan_id": member["plan_id"],
                })
            elif member.get("template_entry_id"):
                raw.append({
                    "template_entry_id": member["template_entry_id"],
                    "dimension_id": member["dimension_id"],
                    "value_id": member["value_id"],
                    "mapped": member.get("concept_key_id") or None,
                    "label": value["label"],
                    "factor_label": factor["label"],
                    "test_plan_id": None,
                })
    if direction is not None and plan_entries.get(direction["plan_id"]):
        raw.append({
            "template_entry_id": plan_entries[direction["plan_id"]],
            "dimension_id": DIRECTION_DIMENSION_ID,
            "value_id": direction["direction_key"],
            "mapped": None,
            "label": direction["title"],
            "factor_label": "创意方向",
            "test_plan_id": direction["plan_id"],
        })

    lock_template_entries(session, ctx, {item["template_entry_id"] for item in raw})
    by_dimension: dict[str, dict] = {}
    for item in raw:
        dimension_key = f"{item['template_entry_id']}:{item['dimension_id']}"
        if dimension_key in by_dimension:
            continue  # one value per dimension, first (newest) wins
        key = ensure_template_concept_key(
            session, ctx,
            template_entry_id=item["template_entry_id"],
            dimension_id=item["dimension_id"],
            value_id=item["value_id"],
            mapped_concept_id=item["mapped"],
            description="Created by an external-creative tag confirmation",
        )
        by_dimension[dimension_key] = {
            "dimension_key": dimension_key,
            "concept_key_id": key.id,
            "value": {
                "value": item["value_id"],
                "template_entry_id": item["template_entry_id"],
                "dimension_id": item["dimension_id"],
                "value_id": item["value_id"],
                "label": item["label"],
                "factor_label": item["factor_label"],
                "test_plan_id": item["test_plan_id"],
                "source": SOURCE_KIND,
            },
        }
    dims = {key: concept["value"]["value"] for key, concept in by_dimension.items()}
    signature = ":".join(f"{key}={dims[key]}" for key in sorted(dims))
    concepts = []
    for key in sorted(by_dimension):
        concept = by_dimension[key]
        concept["stable_dimension_signature"] = signature
        concepts.append(concept)
    return concepts, signature


# ---------------------------------------------------------------------------
# Readers (experiments, learnings, plans)
# ---------------------------------------------------------------------------


def latest_confirmation(
    session, ctx: TenantContext, asset_version_id: str,
) -> ExternalCreativeTagConfirmationRecord | None:
    return session.scalars(
        select(ExternalCreativeTagConfirmationRecord)
        .where(
            ExternalCreativeTagConfirmationRecord.workspace_id == ctx.workspace_id,
            ExternalCreativeTagConfirmationRecord.asset_version_id == asset_version_id,
        )
        .order_by(ExternalCreativeTagConfirmationRecord.revision.desc())
        .limit(1)
    ).first()


def latest_confirmations(
    session, ctx: TenantContext, asset_version_ids: list[str],
) -> dict[str, ExternalCreativeTagConfirmationRecord]:
    """The newest confirmation per AssetVersion (one query)."""
    if not asset_version_ids:
        return {}
    latest: dict[str, ExternalCreativeTagConfirmationRecord] = {}
    for row in session.scalars(
        select(ExternalCreativeTagConfirmationRecord)
        .where(
            ExternalCreativeTagConfirmationRecord.workspace_id == ctx.workspace_id,
            ExternalCreativeTagConfirmationRecord.asset_version_id.in_(sorted(set(asset_version_ids))),
        )
        .order_by(ExternalCreativeTagConfirmationRecord.revision)
    ):
        latest[row.asset_version_id] = row
    return latest


def tag_labels(confirmation: ExternalCreativeTagConfirmationRecord) -> dict[str, str]:
    """``{factor label: value label}`` of the confirmed (non-empty) tags."""
    return {
        item["factor_label"]: item["value_label"]
        for item in confirmation.tags_json or []
        if item.get("value_id") and item.get("factor_label") and item.get("value_label")
    }


__all__ = [
    "Placement",
    "SOURCE_KIND",
    "build_concepts",
    "home_plan",
    "latest_confirmation",
    "latest_confirmations",
    "placement",
    "tag_labels",
]
