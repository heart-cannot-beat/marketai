"""Durable crop then clockwise rotation, with exact-version marketing review."""
import asyncio
import hashlib
import io
from math import gcd
from types import SimpleNamespace
from typing import Literal

from PIL import Image, ImageOps
from pydantic import Field, model_validator
from sqlalchemy import select, null

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AssetRecord, AssetVersionRecord, new_id
from app.entitlements.storage_quota import admit_generation
from app.generation.image_inputs import source_image, check_source, read_source
from app.platform.jobs import enqueue_in_session
from app.platform.operations import create_operation, transition_operation, OperationState, OperationResultRef
from .contracts import Document
from .service import MarketingError, creative_intent, digest, ensure_active, get_project
from .workflow_models import MarketingImageEditRecord as Edit
from app.platform.runtime_role import WORKER_ROLE, runs_as


class ImageCrop(Document):
    x: float = Field(ge=0, lt=1, allow_inf_nan=False)
    y: float = Field(ge=0, lt=1, allow_inf_nan=False)
    width: float = Field(gt=0, le=1, allow_inf_nan=False)
    height: float = Field(gt=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def within_image(self):
        if self.x + self.width > 1 + 1e-9 or self.y + self.height > 1 + 1e-9:
            raise ValueError("裁剪区域超出原图")
        return self


class CreateImageEdit(Document):
    asset_version_id: str = Field(min_length=1, max_length=64)
    crop: ImageCrop | None = None
    rotation: Literal[0, 90, 180, 270] = 0


def editable(session, ctx, row):
    from . import workflow
    ensure_active(session, ctx)
    project = get_project(session, ctx, row.project_id)
    if (row.state != "reviewing" or workflow.scoped(session, ctx, workflow.Handoff, round_id=row.id)
            or project.state != "plan_confirmed" or project.revision != row.plan_revision):
        raise MarketingError("image_edit_frozen", "方案或投放清单已冻结，请在新一轮修改素材。", 409)
    return project


def prepare(session, ctx, row, body):
    from . import workflow
    project = editable(session, ctx, row)
    match = next((item for item in workflow.creative_assets(session, ctx, row) if item[0].id == body.asset_version_id), None)
    if not match:
        raise MarketingError("asset_not_in_round", "请使用当前小轮的素材。", 404)
    version, _, combo = match
    inputs = {"algorithm": "crop-rotate-v1", "source_image": source_image(session, ctx, version.id, project.product_id, row.execution_mode),
        "crop": body.crop.model_dump() if body.crop else None, "rotation": body.rotation,
        "marketing_intent": creative_intent(project, combo["id"]), "execution_mode": row.execution_mode}
    hashed = digest(inputs)
    previous = workflow.scoped(session, ctx, Edit, round_id=row.id, input_hash=hashed)
    if previous and previous[0].state != "failed":
        return previous[0]
    if previous:
        raise MarketingError("image_edit_failed", "该修改未完成，请查看错误后重新调整。", 409)
    # Contract §11 step 3, after the same edit was answered: no new edited
    # version once the Workspace stores its storage.bytes.max (#158).
    admit_generation(session, ctx)
    op = create_operation(session, command_type="marketing.image_edit")
    edit = Edit(id=new_id("mied"), workspace_id=ctx.workspace_id, round_id=row.id,
        source_asset_version_id=version.id, operation_id=op.id, input_hash=hashed, input_json=inputs, state="queued")
    session.add(edit)
    session.flush()
    job = enqueue_in_session(session, context=ctx, job_type="marketing.image_edit", payload={"edit_id": edit.id},
        idempotency_key=edit.id, resource_type="marketing_image_edit", resource_id=edit.id,
        safe_to_retry_after_crash=True, max_attempts=3)
    job.operation_id = op.id
    workflow.audit(session, row, "image_edit_requested", {"edit_id": edit.id, "source_asset_version_id": version.id})
    return edit


def transform(content, inputs):
    with Image.open(io.BytesIO(content)) as original:
        image = ImageOps.exif_transpose(original).convert("RGBA" if "A" in original.getbands() or "transparency" in original.info else "RGB")
        crop = inputs["crop"]
        if crop:
            w, h = image.size
            left, top = round(crop["x"] * w), round(crop["y"] * h)
            right, bottom = round((crop["x"] + crop["width"]) * w), round((crop["y"] + crop["height"]) * h)
            if right <= left or bottom <= top:
                raise MarketingError("crop_too_small", "裁剪区域小于一个像素。")
            image = image.crop((left, top, right, bottom))
        rotations = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_90}
        if inputs["rotation"]:
            image = image.transpose(rotations[inputs["rotation"]])
        if image.size == original.size and image.tobytes() == original.convert(image.mode).tobytes():
            return content, image.size
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue(), image.size


def load_edit(session, ctx, edit_id, *, lock=False):
    query = select(Edit).where(Edit.workspace_id == ctx.workspace_id, Edit.id == edit_id)
    return session.scalar(query.with_for_update() if lock else query)


@runs_as(WORKER_ROLE)
async def execute_edit(ctx, edit_id, blob_store):
    from . import workflow
    with tenant_session(ctx) as session:
        edit = load_edit(session, ctx, edit_id)
        if not edit or edit.state != "queued":
            return
        inputs = edit.input_json
        row = workflow.get_round(session, ctx, edit.round_id)
        project = editable(session, ctx, row)
        check_source(session, ctx, inputs["source_image"], project.product_id, row.execution_mode)
    try:
        content = await read_source(blob_store, inputs["source_image"])
        output, (w, h) = await asyncio.to_thread(transform, content, inputs)
        if len(output) > 20 * 1024 * 1024:
            raise MarketingError("image_edit_too_large", "修改后的图片超过 20MB，请缩小裁剪范围。")
        hashed = hashlib.sha256(output).hexdigest()
        from .blob_read import write_generated
        blob = await write_generated(blob_store, f"committed/{ctx.workspace_id}/image_edits/{edit_id}.png", output, "image/png") if output != content else None
        from app.generation.thumbnails import apply_thumbnail, commit_thumbnail_best_effort
        thumbnail = await commit_thumbnail_best_effort(blob_store, ctx.workspace_id, output,
            log_context={"image_edit_id": edit_id}) if blob else None
        with tenant_session(ctx) as session:
            # The API serializes round mutations on this same workspace lock.
            workflow.serialize_workspace(session, ctx)
            edit = load_edit(session, ctx, edit_id, lock=True)
            if edit.state != "queued":
                return
            row = workflow.get_round(session, ctx, edit.round_id)
            project = editable(session, ctx, row)
            if digest(inputs) != edit.input_hash or inputs["marketing_intent"] != creative_intent(project, inputs["marketing_intent"]["combination"]["id"]):
                raise MarketingError("image_edit_changed", "修改依据已变化。", 409)
            check_source(session, ctx, inputs["source_image"], project.product_id, row.execution_mode)
            source = session.get(AssetVersionRecord, edit.source_asset_version_id)
            session.scalar(select(AssetRecord).where(AssetRecord.workspace_id == ctx.workspace_id,
                AssetRecord.id == source.asset_id).with_for_update())
            existing = session.scalar(select(AssetVersionRecord).where(AssetVersionRecord.workspace_id == ctx.workspace_id,
                AssetVersionRecord.asset_id == source.asset_id, AssetVersionRecord.content_sha256 == hashed))
            transition_operation(session, edit.operation_id, OperationState.RUNNING, "local image edit", "worker")
            if existing:
                edit.state, edit.result_asset_version_id = "unchanged", existing.id
            else:
                common = gcd(w, h)
                version = AssetVersionRecord(id=new_id("av"), workspace_id=ctx.workspace_id, asset_id=source.asset_id,
                    reference_sources_json=source.reference_sources_json, truth_package_id=source.truth_package_id,
                    truth_dependency_expected=source.truth_dependency_expected,
                    truth_dependency_policy_version=source.truth_dependency_policy_version,
                    truth_dependency_evidence_json=source.truth_dependency_evidence_json if source.truth_dependency_evidence_json is not None else null(),
                    truth_dependency_evidence_hash=source.truth_dependency_evidence_hash,
                    parent_version_id=source.id, kind="edited", origin_kind="edit", execution_mode=source.execution_mode,
                    aspect_ratio=f"{w // common}:{h // common}", object_key=blob.committed_key,
                    committed_object_key=blob.committed_key, committed_storage_version_id=blob.storage_version_id,
                    content_sha256=hashed, byte_size=len(output), mime_type="image/png")
                apply_thumbnail(version, thumbnail)
                session.add(version)
                session.flush()
                edit.state, edit.result_asset_version_id = "succeeded", version.id
            transition_operation(session, edit.operation_id, OperationState.SUCCEEDED, edit.state, "worker",
                result_ref=OperationResultRef("asset_version", edit.result_asset_version_id))
    except MarketingError:
        fail_edit(ctx, edit_id)
        raise


@runs_as(WORKER_ROLE)
def fail_edit(ctx, edit_id):
    with tenant_session(ctx) as session:
        edit = load_edit(session, ctx, edit_id, lock=True)
        if edit and edit.state == "queued":
            edit.state, edit.error_code = "failed", "image_edit_failed"
            transition_operation(session, edit.operation_id, OperationState.RUNNING, "local edit failed", "worker")
            transition_operation(session, edit.operation_id, OperationState.FAILED, "image_edit_failed", "worker")


def creatives(session, ctx, row, combos):
    result = []
    records = session.execute(select(Edit, AssetVersionRecord).join(AssetVersionRecord,
        (AssetVersionRecord.id == Edit.result_asset_version_id) & (AssetVersionRecord.workspace_id == ctx.workspace_id))
        .where(Edit.workspace_id == ctx.workspace_id, Edit.round_id == row.id, Edit.state == "succeeded")).all()
    for edit, version in records:
        inputs = edit.input_json
        intent = inputs["marketing_intent"]
        combo = combos.get(intent["combination"]["id"])
        if (version.execution_mode == row.execution_mode and version.parent_version_id == edit.source_asset_version_id
                and inputs["execution_mode"] == row.execution_mode and digest(inputs) == edit.input_hash
                and intent["plan_hash"] == row.snapshot_json["plan_hash"] and intent["combination"] == combo):
            result.append((version, SimpleNamespace(recipe_hash=edit.input_hash, canonical_json={
                "selection": {"marketing_intent": intent}, "image_edit": {"kind": "image_edit", "edit_id": edit.id,
                    "source_asset_version_id": edit.source_asset_version_id, "source_sha256": inputs["source_image"]["sha256"],
                    "crop": inputs["crop"], "rotation": inputs["rotation"], "algorithm": inputs["algorithm"]}}), combo))
    return result


def summaries(session, ctx, row):
    return [{"id": e.id, "operation_id": e.operation_id, "source_asset_version_id": e.source_asset_version_id,
             "result_asset_version_id": e.result_asset_version_id, "state": e.state, "error_code": e.error_code}
        for e in session.scalars(select(Edit).where(Edit.workspace_id == ctx.workspace_id, Edit.round_id == row.id)
                                 .order_by(Edit.created_at, Edit.id)).all()]
