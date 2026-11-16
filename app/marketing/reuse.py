"""Reuse exact historical creatives without relabelling their original generation.

A round may reuse a creative of its own test, of the product card the test
started from, or of another test of that same card (#7, J-32 lineage in
``card_lineage``). The reuse never rewrites the original AssetVersion or its
generation snapshot and carries no delivery evidence over; the creative is
reviewed again in this round against this test's confirmed facts.
"""
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Literal

from sqlalchemy import and_, or_, select
from pydantic import Field
from app.adapters.persistence.models import AssetRecord, AssetVersionRecord, ProductRecord, new_id
from app.generation.paid_execution import asset_blob_ref
from app.platform.pagination import InvalidCursorError, decode_cursor, encode_cursor
from .contracts import Document
from .models import MarketingProjectRecord, MarketingRevisionRecord
from .workflow_models import MarketingCreativeReuseRecord as Reuse
from .service import MarketingError, digest

#: Largest image the reviewer and the AI checks accept (``asset_not_checkable``).
MAX_CHECKABLE_BYTES = 20 * 1024 * 1024

Relation = Literal["this_test", "product_card", "other_test"]


class ReuseCreative(Document):
    asset_version_id: str = Field(min_length=1, max_length=64)
    combination_id: str = Field(min_length=1, max_length=64)


class ReuseCandidate(Document):
    asset_version_id: str
    asset_id: str
    created_at: str | None
    provider: str | None
    model: str | None
    aspect_ratio: str | None
    #: Where the creative comes from: this test, the product card itself, or another test of the card.
    relation: Relation
    #: ``other_test`` only: the test that generated it and its product name.
    source_project_id: str | None = None
    source_test_name: str | None = None
    #: ``other_test`` only: whether that test confirmed the same facts as this round.
    facts_relation: Literal["same", "differs"] | None = None


class ReuseCandidatePage(Document):
    items: list[ReuseCandidate]
    next_cursor: str | None


@dataclass(frozen=True)
class Origin:
    relation: str
    card_product_id: str | None = None
    source_project_id: str | None = None


def reusable(session, ctx, row) -> dict[str, Origin]:
    """Products whose creatives this round may reuse, with where each comes from.

    Always this test's source and the understanding confirmed before the
    round's plan; for a test that stands for a product card, also that card
    and every anchor of the card's other unrevised tests.
    """
    from . import workflow
    from .card_lineage import same_card
    revisions = workflow.scoped(session, ctx, MarketingRevisionRecord, project_id=row.project_id)
    revisions = sorted((r for r in revisions if r.revision <= row.plan_revision), key=lambda r: r.revision)
    source = next((r for r in reversed(revisions) if r.reason in ("created", "revise_source")), None)
    confirmed = next((r for r in reversed(revisions) if r.reason == "confirm_understanding"), None)
    lineage = same_card(session, ctx, row.project_id)
    card = lineage[0] if lineage else None
    allowed: dict[str, Origin] = {}
    for test in lineage[1] if lineage else ():
        mine = test.project_id == row.project_id
        allowed.update({anchor: Origin("this_test" if mine else "other_test", card, None if mine else test.project_id)
                        for anchor in test.anchors})
        allowed[test.source] = Origin("product_card", card)
    if card:
        allowed[card] = Origin("product_card", card)
    if source and source.content_json.get("product_id"):
        allowed[source.content_json["product_id"]] = Origin("product_card", card)
    if confirmed and confirmed.content_json.get("product_id"):
        allowed[confirmed.content_json["product_id"]] = Origin("this_test", card)
    return allowed


def _statements(values) -> list[str]:
    return sorted({" ".join(str(value).split()).casefold() for value in values if str(value).strip()})


def facts_relation(anchor: ProductRecord | None, row) -> str:
    """Whether the generating test confirmed the same fact statements as this round's snapshot."""
    facts = (row.snapshot_json.get("understanding") or {}).get("facts") or []
    mine = [f.get("statement", "") for f in facts if f.get("confirmation", "confirmed") == "confirmed"]
    theirs = list(anchor.selling_points or []) if anchor is not None else []
    return "same" if theirs and _statements(theirs) == _statements(mine) else "differs"


def attach(session, ctx, row, body):
    from . import workflow
    if row.state != "reviewing" or workflow.scoped(session, ctx, workflow.Handoff, round_id=row.id):
        raise MarketingError("round_not_reviewable", "请在检查阶段选择已有素材。", 409)
    combos = {c["id"]: c for c in row.snapshot_json["plan"]["combinations"]}
    if body.combination_id not in combos:
        raise MarketingError("combination_not_found", "找不到本轮创意组合。")
    version = session.scalar(select(AssetVersionRecord).where(AssetVersionRecord.workspace_id == ctx.workspace_id,
        AssetVersionRecord.id == body.asset_version_id))
    if not version:
        raise MarketingError("asset_not_found", "找不到素材版本。", 404)
    asset = session.scalar(select(AssetRecord).where(AssetRecord.workspace_id == ctx.workspace_id, AssetRecord.id == version.asset_id))
    origin = reusable(session, ctx, row).get(asset.product_id) if asset else None
    if origin is None:
        raise MarketingError("asset_product_mismatch", "只能复用同一产品下的素材。")
    if version.execution_mode != row.execution_mode:
        raise MarketingError("asset_mode_mismatch", "真实素材与模拟素材不能混用。")
    if not version.mime_type.startswith("image/") or version.byte_size > MAX_CHECKABLE_BYTES:
        raise MarketingError("asset_not_checkable", "素材不是支持检查的图片。")
    asset_blob_ref(session, ctx, version.id)  # exact committed storage is required
    existing = workflow.scoped(session, ctx, Reuse, round_id=row.id, asset_version_id=version.id)
    native = [v for v, _, _ in workflow.creative_assets(session, ctx, row) if v.id == version.id]
    if existing:
        if existing[0].combination_id != body.combination_id:
            raise MarketingError("reuse_already_mapped", "该版本已归属另一组合；请另起小轮调整意图。", 409)
        return
    if native:
        raise MarketingError("creative_already_in_round", "该素材已经属于本轮。", 409)
    lineage = {"relation": origin.relation, "source_project_id": origin.source_project_id,
        "card_product_id": origin.card_product_id,
        "facts_relation": facts_relation(session.scalar(select(ProductRecord).where(
            ProductRecord.workspace_id == ctx.workspace_id, ProductRecord.id == asset.product_id)), row)
            if origin.relation == "other_test" else None}
    source = {"kind": "existing_creative", "asset_version_id": version.id, "asset_id": version.asset_id,
        "product_id": asset.product_id, "origin_kind": version.origin_kind, "execution_mode": version.execution_mode,
        "content_sha256": version.content_sha256, "original_prompt": asset.prompt,
        "original_provider": asset.provider, "original_model": asset.model,
        "plan_hash": row.snapshot_json["plan_hash"], "combination": combos[body.combination_id], **lineage}
    session.add(Reuse(id=new_id("mreuse"), workspace_id=ctx.workspace_id, round_id=row.id,
        asset_version_id=version.id, combination_id=body.combination_id, source_json=source,
        source_hash=digest(source), actor_id=ctx.initiator_id or ctx.executor))
    workflow.audit(session, row, "creative_reused", {"asset_version_id": version.id,
        "combination_id": body.combination_id, "source_hash": digest(source), **lineage})


def candidates(session, ctx, row, *, cursor: str | None = None, limit: int = 20) -> dict:
    """Creatives this round could reuse, newest first, without the ones already in it.

    Only images ``attach`` would accept: an allowed product, this round's
    execution mode and a checkable image. Each item says where it comes from.
    """
    from . import workflow
    allowed = reusable(session, ctx, row)
    in_round = [v.id for v, _, _ in workflow.creative_assets(session, ctx, row)]
    filters = f"marketing-reuse-candidates-v1:{row.id}"
    query = (select(AssetVersionRecord, AssetRecord)
        .join(AssetRecord, (AssetRecord.id == AssetVersionRecord.asset_id) & (AssetRecord.workspace_id == AssetVersionRecord.workspace_id))
        .where(AssetVersionRecord.workspace_id == ctx.workspace_id, AssetRecord.product_id.in_(list(allowed)),
               AssetVersionRecord.execution_mode == row.execution_mode, AssetVersionRecord.mime_type.like("image/%"),
               AssetVersionRecord.byte_size <= MAX_CHECKABLE_BYTES))
    if in_round:
        query = query.where(AssetVersionRecord.id.notin_(in_round))
    if cursor:
        try:
            position = decode_cursor(cursor, ctx.workspace_id, filters)
            if len(position) != 2 or not all(isinstance(v, str) for v in position) or not 1 <= len(position[1]) <= 64:
                raise ValueError("invalid position")
            stamp = datetime.fromisoformat(position[0])
        except (InvalidCursorError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            raise MarketingError("invalid_cursor", "列表位置无效或搜索条件已变化，请从第一页重新加载。", 400) from None
        query = query.where(or_(AssetVersionRecord.created_at < stamp,
            and_(AssetVersionRecord.created_at == stamp, AssetVersionRecord.id < position[1])))
    rows = session.execute(query.order_by(AssetVersionRecord.created_at.desc(), AssetVersionRecord.id.desc())
        .limit(limit + 1)).all() if allowed else []
    more, rows = len(rows) > limit, rows[:limit]
    others = {asset.product_id for _, asset in rows if allowed[asset.product_id].relation == "other_test"}
    anchors = {p.id: p for p in session.scalars(select(ProductRecord).where(
        ProductRecord.workspace_id == ctx.workspace_id, ProductRecord.id.in_(others))).all()} if others else {}
    projects = {allowed[p].source_project_id for p in others}
    names = {p.id: (p.understanding_json or {}).get("product_name") for p in session.scalars(select(MarketingProjectRecord).where(
        MarketingProjectRecord.workspace_id == ctx.workspace_id, MarketingProjectRecord.id.in_(projects))).all()} if projects else {}
    items = []
    for version, asset in rows:
        origin = allowed[asset.product_id]
        item = {"asset_version_id": version.id, "asset_id": version.asset_id,
            "created_at": version.created_at.isoformat() if version.created_at else None,
            "provider": asset.provider, "model": asset.model, "aspect_ratio": version.aspect_ratio, "relation": origin.relation}
        if origin.relation == "other_test":
            item.update(source_project_id=origin.source_project_id, source_test_name=names.get(origin.source_project_id),
                facts_relation=facts_relation(anchors.get(asset.product_id), row))
        items.append(item)
    last = rows[-1][0] if rows else None
    return {"items": items, "next_cursor": encode_cursor(ctx.workspace_id, filters, (last.created_at.isoformat(), last.id))
            if more and last is not None else None}


def editable(project, recipe) -> bool:
    """Whether a round creative can be revised or cropped in this test (#7 decision 4).

    Revisions and crops build on this test's own product anchor. A creative
    reused from another test or from the product card hangs on a different
    product, which the draft, the preview and the paid Worker step would all
    refuse, so it is not offered.
    """
    reused = recipe.canonical_json.get("reuse")
    return not reused or reused.get("product_id") == project.product_id


def ensure_editable(project, recipe):
    """Refuse a revision or crop of a reused creative before any reservation (see ``editable``)."""
    if not editable(project, recipe):
        raise MarketingError("reused_creative_not_editable",
            "从其他测试或产品卡片复用的素材，暂时不能在本测试里修改或裁剪；请在原测试里修改，或在这一轮重新生成。")


def creatives(session, ctx, row, combos):
    result = []
    records = session.execute(select(Reuse, AssetVersionRecord).join(AssetVersionRecord,
        (AssetVersionRecord.id == Reuse.asset_version_id) & (AssetVersionRecord.workspace_id == ctx.workspace_id))
        .where(Reuse.workspace_id == ctx.workspace_id, Reuse.round_id == row.id)).all()
    for link, version in records:
        source = link.source_json
        if (version and version.execution_mode == row.execution_mode and source["content_sha256"] == version.content_sha256
                and source["plan_hash"] == row.snapshot_json["plan_hash"] and link.source_hash == digest(source)
                and source["combination"] == combos.get(link.combination_id)):
            # This hash describes the reuse association, never a claimed generation recipe.
            result.append((version, SimpleNamespace(recipe_hash=link.source_hash, canonical_json={"reuse": source}),
                combos[link.combination_id]))
    return result
