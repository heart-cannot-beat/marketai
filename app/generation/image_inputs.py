"""Resolve exact, tenant-scoped source pixels for image-to-image execution."""
from dataclasses import asdict
import hashlib

from sqlalchemy import select

from app.adapters.persistence.models import AssetRecord, AssetVersionRecord
from .selection import SelectionError


def source_image(session, ctx, version_id, product_id, execution_mode):
    from .paid_execution import asset_blob_ref
    row = session.execute(select(AssetVersionRecord, AssetRecord).join(AssetRecord,
        (AssetRecord.id == AssetVersionRecord.asset_id) & (AssetRecord.workspace_id == ctx.workspace_id))
        .where(AssetVersionRecord.workspace_id == ctx.workspace_id,
               AssetVersionRecord.id == version_id, AssetRecord.product_id == product_id)).first()
    if not row or row[0].execution_mode != execution_mode:
        raise SelectionError("edit_source_unavailable", "原素材不可用或与本次产品、模式不一致。")
    version = row[0]
    if version.mime_type not in ("image/png", "image/jpeg", "image/webp") or not 0 < version.byte_size <= 20 * 1024 * 1024:
        raise SelectionError("edit_source_unavailable", "原素材不是受支持的图片。")
    return {"asset_version_id": version.id, "sha256": version.content_sha256,
            "role": "edit_source", "blob": asdict(asset_blob_ref(session, ctx, version.id))}


def check_source(session, ctx, frozen, product_id, execution_mode):
    current = source_image(session, ctx, frozen["asset_version_id"], product_id, execution_mode)
    if current != frozen:
        raise SelectionError("edit_source_changed", "原素材已变化，请重新准备修改。")


async def read_source(blob_store, frozen):
    from app.platform.blob_store import CommittedBlobRef
    from app.providers.base import validated_reference_inputs
    from app.marketing.blob_read import read_committed
    content = await read_committed(blob_store, CommittedBlobRef(**frozen["blob"]), 20 * 1024 * 1024)
    if hashlib.sha256(content).hexdigest() != frozen["sha256"]:
        raise SelectionError("edit_source_changed", "原图文件校验未通过。")
    validated_reference_inputs((content,))
    return content
