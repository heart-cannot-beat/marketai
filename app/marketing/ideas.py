"""Ads-first ideas (ADR 0018): one product's buying claims and ad drafts, before any test settings.

An ads-first project is a ``MarketingProjectRecord`` with ``flow = "ads_first"``. Its states are only
``input``, ``understanding_draft`` and ``ideas_ready``, its ``plan_json`` stays empty in step 1, and it
always has a product. Its ads are the generated images whose recipe intent names the project and the
ads-first flow (design D6); no round is involved. The plan-first endpoints never return these projects.
"""
from __future__ import annotations

from sqlalchemy import select

from app.adapters.persistence.models import (AssetVersionRecord, GenerationRunRecord, LibraryEntryRecord, ProductRecord,
                                            new_id, utcnow)
from app.generation.models import GenerationRecipeSnapshotRecord, RunItemRecord
from app.generation.selection import SelectionError
from app.platform.audit import append_event
from . import service
from .models import MarketingAdKeepRecord, MarketingProjectRecord

FLOW = "ads_first"


def _product_anchor(session, ctx, product_id):
    """The workspace product an ads-first project hangs on, given its ``prod_`` id or its Library entry id.

    The first batch builds ideas on a product draft that is published only when the user checks it (D3),
    so a draft product is accepted; an archived one is not.
    """
    product = session.scalar(select(ProductRecord).where(ProductRecord.workspace_id == ctx.workspace_id,
        (ProductRecord.id == product_id) | (ProductRecord.product_profile_entry_id == product_id))
        .order_by(ProductRecord.product_profile_entry_id.is_(None), ProductRecord.id).limit(1))
    if product is not None:
        entry = session.scalar(select(LibraryEntryRecord).where(LibraryEntryRecord.workspace_id == ctx.workspace_id,
            LibraryEntryRecord.id == product.product_profile_entry_id)) if product.product_profile_entry_id else None
        if entry is None or entry.lifecycle == "active":
            return product.id
    else:
        try:  # a published Library product that has no product anchor yet gets one
            return service.product_card(session, ctx, product_id)[0]
        except SelectionError:
            pass
    raise service.MarketingError("product_not_found", "找不到这个产品。", 404)


def create_ideas_project(session, ctx, *, product_id: str, source_text: str, market: str, ad_language: str):
    service.ensure_active(session, ctx)
    anchor = _product_anchor(session, ctx, product_id)
    if not source_text.strip() or len(source_text) > 16000:
        raise service.MarketingError("source_invalid", "请粘贴不超过 16000 字的产品资料。", params={"limit": 16000})
    row = MarketingProjectRecord(id=new_id("mp"), workspace_id=ctx.workspace_id, product_id=anchor, flow=FLOW,
        source_text=source_text.strip(), market=market.strip(), ad_language=ad_language, revision=1, state="input")
    session.add(row)
    session.flush()
    service.checkpoint(session, ctx, row, "created")
    return row


def get_ideas_project(session, ctx, project_id: str):
    return service.get_project(session, ctx, project_id, flow=FLOW)


def project_ads(session, ctx, row):
    """This project's ads, oldest first: images whose recipe intent names the project and the ads-first flow."""
    records = session.execute(select(AssetVersionRecord, GenerationRecipeSnapshotRecord)
        .join(RunItemRecord, (RunItemRecord.id == AssetVersionRecord.producing_run_item_id) & (RunItemRecord.workspace_id == ctx.workspace_id))
        .join(GenerationRunRecord, (GenerationRunRecord.id == RunItemRecord.generation_run_id) & (GenerationRunRecord.workspace_id == ctx.workspace_id))
        .join(GenerationRecipeSnapshotRecord, (GenerationRecipeSnapshotRecord.id == GenerationRunRecord.recipe_snapshot_id) & (GenerationRecipeSnapshotRecord.workspace_id == ctx.workspace_id))
        .where(AssetVersionRecord.workspace_id == ctx.workspace_id)
        .where(GenerationRecipeSnapshotRecord.canonical_json['selection']['marketing_intent']['project_id'].as_string() == row.id)
        .order_by(AssetVersionRecord.created_at, AssetVersionRecord.id)).all()
    return [(version, recipe) for version, recipe in records
            if (_intent(recipe).get("project_id"), _intent(recipe).get("flow")) == (row.id, FLOW)]


def _intent(recipe):
    return (recipe.canonical_json or {}).get("selection", {}).get("marketing_intent") or {}


def _keeps(session, ctx, row):
    return {k.asset_version_id: k.kept for k in session.scalars(select(MarketingAdKeepRecord).where(
        MarketingAdKeepRecord.workspace_id == ctx.workspace_id, MarketingAdKeepRecord.project_id == row.id)).all()}


def set_kept(session, ctx, row, asset_version_id: str, kept: bool) -> None:
    service.ensure_active(session, ctx)
    if asset_version_id not in {version.id for version, _ in project_ads(session, ctx, row)}:
        raise service.MarketingError("ad_not_found", "这张广告不属于这些广告想法。", 404)
    # Serialize marks on one project so two first marks of the same ad never race on the unique key.
    session.execute(select(MarketingProjectRecord.id).where(MarketingProjectRecord.workspace_id == ctx.workspace_id,
        MarketingProjectRecord.id == row.id).with_for_update())
    mark = session.scalar(select(MarketingAdKeepRecord).where(MarketingAdKeepRecord.workspace_id == ctx.workspace_id,
        MarketingAdKeepRecord.project_id == row.id, MarketingAdKeepRecord.asset_version_id == asset_version_id))
    actor = ctx.initiator_id or ctx.executor
    if mark is None:
        session.add(MarketingAdKeepRecord(id=new_id("mak"), workspace_id=ctx.workspace_id, project_id=row.id,
            asset_version_id=asset_version_id, kept=kept, actor_id=actor, updated_at=utcnow()))
    else:
        mark.kept, mark.actor_id, mark.updated_at = kept, actor, utcnow()
    append_event(session, event_type="marketing.ad_kept", resource_type="marketing_project", resource_id=row.id,
                 details={"asset_version_id": asset_version_id, "kept": kept})
    session.flush()


def _ad(version, recipe, keeps):
    combination = _intent(recipe).get("combination") or {}
    return {"asset_version_id": version.id, "combination_id": combination.get("id", ""),
            "angle_id": combination.get("angle_id", ""), "expression_id": combination.get("expression_id", ""),
            # Step 1 has no checking yet, so every ad is a draft (ADR 0018 §2); S10 derives the others.
            "status": "draft", "kept": keeps.get(version.id, False),
            "created_at": version.created_at.isoformat() if version.created_at else ""}


def ideas_dict(session, ctx, row) -> dict:
    """The ``IdeasProjectDetail`` of an ads-first project."""
    call = service.latest_call(session, ctx, row.id)
    keeps = _keeps(session, ctx, row)
    return {"id": row.id, "revision": row.revision, "state": row.state, "product_id": row.product_id,
            "market": row.market, "ad_language": row.ad_language, "understanding": row.understanding_json,
            "ideas": row.ideas_json, "latest_call": service.call_dict(call) if call else None,
            "ads": [_ad(version, recipe, keeps) for version, recipe in project_ads(session, ctx, row)],
            "pending": []}


def ideas_summary(session, ctx, row) -> dict:
    """The ``IdeasProjectSummary`` of an ads-first project."""
    ads = {version.id for version, _ in project_ads(session, ctx, row)}
    keeps = _keeps(session, ctx, row)
    product = session.scalar(select(ProductRecord).where(ProductRecord.workspace_id == ctx.workspace_id,
                                                         ProductRecord.id == row.product_id))
    name = (product.name if product else "") or (row.understanding_json or {}).get("product_name") or row.source_text[:80]
    return {"id": row.id, "revision": row.revision, "state": row.state, "product_id": row.product_id,
            "product_name": name, "ad_count": len(ads), "kept_count": sum(1 for ad in ads if keeps.get(ad)),
            "updated_at": row.updated_at.isoformat() if row.updated_at else ""}
