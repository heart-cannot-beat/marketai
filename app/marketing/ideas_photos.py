"""Photos the user adds to a product of ads-first ideas (#233; ADR 0018 §2, design doc §5.2 and §5.7).

The product's reading may find no photo that shows the product's look -- the user pasted only text, or the
page's pictures did not pass -- and then every ad is made without one. The user can add their own:

1. upload it the way any reference is uploaded (an Upload Intent of purpose ``reference_asset``, then
   ``create_library_reference_object``), so it goes through the same reference upload gate in the Worker
   (``library.reference_gate``), unrelaxed;
2. once the gate released it, add it here as a photo of the product, saying what it shows (``add``).

An added photo is the product's, not one project's: every ads-first project of the product lists it among
its photos (``product_references.candidates``), drafts images with it, and checks or rejects it with the
same ``verify_marketing_ideas`` as a photo the reading found. Until the user checks it -- this is the
product's real photo and may be used in ads -- it only makes drafts. No rights are declared when adding it;
checking it declares them. Nothing is added to the product intake: that is the record of the reading.
"""
from __future__ import annotations

from sqlalchemy import select

from app.adapters.persistence.models import ReferenceObjectRecord, new_id, utcnow
from app.platform.audit import append_event
from . import service
from .models import MarketingProductPhotoRecord as Added, MarketingProjectRecord
from .service import MarketingError


def _object(session, ctx, object_id):
    return session.scalar(select(ReferenceObjectRecord).where(ReferenceObjectRecord.workspace_id == ctx.workspace_id,
                                                              ReferenceObjectRecord.id == object_id))


def _gate_refusal(obj) -> MarketingError | None:
    """Why an uploaded image cannot be a photo yet, by the gate's own state; ``None`` once it may."""
    from app.library import references
    if obj.state == references.STATE_QUARANTINED:
        return MarketingError("photo_upload_checking", "这张图还在上传检查中，检查完再加。", 409)
    if obj.state == references.STATE_REJECTED:
        code, _ = references.gate_failure(obj)
        return MarketingError("photo_upload_rejected", "这张图没有通过上传检查，不能当作产品照片。", 409,
                              params={"failure_code": code or references.FAILURE_GATE_REJECTED})
    return None


def add(session, ctx, row, reference_object_id: str, role: str) -> Added:
    """Add an uploaded image that passed the reference upload gate as a photo of the ideas' product.

    Refused while the gate is still checking it (``photo_upload_checking``) and when the gate rejected it
    (``photo_upload_rejected``, with the gate's ``failure_code``); an image of another Workspace is not found.
    The same image added again to the same product is the same photo: the first addition is returned; an
    image already listed as a photo of these ideas -- found by the reading, or added with another role -- is
    ``photo_already_listed``, naming it."""
    from .product_references import _usable, candidates
    service.ensure_active(session, ctx)
    if not row.product_id:
        raise MarketingError("product_required", "这些广告想法还没有对应的产品。", 409)
    obj = _object(session, ctx, reference_object_id)
    if obj is None:
        raise MarketingError("photo_upload_not_found", "找不到这张上传的图，请重新上传。", 404)
    refused = _gate_refusal(obj)
    if refused is not None:
        raise refused
    if not _usable(obj, ctx):
        from app.library import references
        raise MarketingError("photo_upload_rejected", "这张图没有通过上传检查，不能当作产品照片。", 409,
                             params={"failure_code": references.FAILURE_STORAGE_UNAVAILABLE})
    session.execute(select(MarketingProjectRecord.id).where(MarketingProjectRecord.workspace_id == ctx.workspace_id,
                                                            MarketingProjectRecord.id == row.id).with_for_update())
    existing = session.scalar(select(Added).where(Added.workspace_id == ctx.workspace_id, Added.product_id == row.product_id,
                                                  Added.reference_object_id == obj.id))
    if existing is not None and existing.role == role:
        return existing
    if existing is not None or any(photo["photo_id"] == obj.derivative_sha256 for photo in candidates(session, ctx, row)):
        raise MarketingError("photo_already_listed", "这张照片已经在产品照片里了。", 409,
                             params={"photo_id": obj.derivative_sha256})
    record = Added(id=new_id("mpp"), workspace_id=ctx.workspace_id, product_id=row.product_id, project_id=row.id,
                   reference_object_id=obj.id, role=role, actor_id=ctx.initiator_id or ctx.executor, created_at=utcnow())
    session.add(record)
    session.flush()
    append_event(session, event_type="marketing.ideas_photo_added", resource_type="marketing_project", resource_id=row.id,
                 details={"added_id": record.id, "product_id": row.product_id, "reference_object_id": obj.id,
                          "photo_id": obj.derivative_sha256, "role": role})
    return record


def derivative(session, ctx, row, photo_id: str) -> dict:
    """What showing one photo of these ideas needs: its released derivative (never the uploaded original).

    Any photo the ideas list (``candidates``), whatever its checking; ``photo_not_found`` otherwise."""
    from .product_references import _blob, candidates
    from app.platform.blob_store import CommittedBlobRef
    photo = next((photo for photo in candidates(session, ctx, row) if photo["photo_id"] == photo_id), None)
    if photo is None:
        raise MarketingError("photo_not_found", "这些广告想法里没有这张照片，请刷新后重试。", 404)
    # ``candidates`` lists only photos whose object is still released in this Workspace.
    obj = _object(session, ctx, photo["reference_object_id"])
    return {"reference_object_id": obj.id, "ref": CommittedBlobRef(**_blob(obj)), "mime_type": obj.derivative_mime or "image/jpeg",
            "byte_size": int(obj.derivative_byte_size or 0), "width": int(obj.derivative_width or 0),
            "height": int(obj.derivative_height or 0)}
