"""Reuse exact historical creatives without relabelling their original generation."""
from types import SimpleNamespace
from sqlalchemy import select
from pydantic import Field
from app.adapters.persistence.models import AssetRecord, AssetVersionRecord, new_id
from app.generation.paid_execution import asset_blob_ref
from .contracts import Document
from .models import MarketingRevisionRecord
from .workflow_models import MarketingCreativeReuseRecord as Reuse
from .service import MarketingError, digest


class ReuseCreative(Document):
    asset_version_id: str = Field(min_length=1, max_length=64)
    combination_id: str = Field(min_length=1, max_length=64)


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
    revisions = workflow.scoped(session, ctx, MarketingRevisionRecord, project_id=row.project_id)
    revisions = sorted((r for r in revisions if r.revision <= row.plan_revision), key=lambda r: r.revision)
    source = next((r for r in reversed(revisions) if r.reason in ("created", "revise_source")), None)
    confirmed = next((r for r in reversed(revisions) if r.reason == "confirm_understanding"), None)
    product_ids = {r.content_json.get("product_id") for r in (source, confirmed) if r}
    if not asset or asset.product_id not in product_ids:
        raise MarketingError("asset_product_mismatch", "只能复用本测试的素材；同一产品其他测试中的素材，目前不能跨测试使用。")
    if version.execution_mode != row.execution_mode:
        raise MarketingError("asset_mode_mismatch", "真实素材与模拟素材不能混用。")
    if not version.mime_type.startswith("image/") or version.byte_size > 20 * 1024 * 1024:
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
    source = {"kind": "existing_creative", "asset_version_id": version.id, "asset_id": version.asset_id,
        "product_id": asset.product_id, "origin_kind": version.origin_kind, "execution_mode": version.execution_mode,
        "content_sha256": version.content_sha256, "original_prompt": asset.prompt,
        "original_provider": asset.provider, "original_model": asset.model,
        "plan_hash": row.snapshot_json["plan_hash"], "combination": combos[body.combination_id]}
    session.add(Reuse(id=new_id("mreuse"), workspace_id=ctx.workspace_id, round_id=row.id,
        asset_version_id=version.id, combination_id=body.combination_id, source_json=source,
        source_hash=digest(source), actor_id=ctx.initiator_id or ctx.executor))
    workflow.audit(session, row, "creative_reused", {"asset_version_id": version.id,
        "combination_id": body.combination_id, "source_hash": digest(source)})


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
