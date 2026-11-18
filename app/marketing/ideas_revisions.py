"""Changing an ads-first ad draft without a round (#200; ADR 0018, design doc §5.3 and §5.7).

"Change this ad" works on one ad of the ideas and always ends in a new image version; the old version
stays an ad of the ideas, so the user can compare and go back. Every new version is a draft that needs
its own check and its own checking by the user. Three ways, each with the existing machinery of a round:

- **A correction** (``prepare_feedback``, no ``image_tool``): the image is made again from the ad's
  current claim and way of telling, the facts as they are when it is previewed, the user's note and the
  findings of the source's newest check, with the source image as the reference. Its recipe intent is the
  ad's current ``ideas_intent`` plus a ``revision`` block that says what the new image was made from, so a
  correction resolves "the facts changed" (#198) and the image is checked against the facts it was made on.
- **A supplier image tool** (``prepare_feedback`` with ``image_tool``: upscale, remove the background,
  erase an area): the same picture processed. It keeps the source's own intent -- what the source stood on
  -- so it never makes an ad that needs redoing look current.
- **A crop or a rotation** (``prepare_image_edit``): a local, free operation run by the Worker; the new
  version keeps the source's intent too. No reservation, no settlement and no paid call.

The first two are paid generation drafts: the client previews (which quotes the maximum) and submits them
through the generation endpoints, so the confirmed number is the admitted one; the API admits, the Worker
executes, an uncertain call is never sent again. A draft's image prompt is the application's: the preview
writes it again from this module's record and refuses one a client changed (``ideas_prompt_changed``).

**Uploads** (``place_upload``): an external creative of the product (#168), whose uploader declared the
right to use it, is placed under one ad of the ideas. The placement freezes that ad's intent, so the
uploaded image is listed, kept, checked, verified and downloaded like a generated ad of that claim.
"""
from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import select

from app.adapters.persistence.models import AssetVersionRecord, ExternalCreativeRecord, new_id, utcnow
from app.generation.selection import SelectionError
from app.platform.audit import append_event
from . import ideas, service
from .models import MarketingIdeasUploadRecord as Upload, MarketingProjectRecord
from .service import MarketingError, digest
from .workflow_models import MarketingFeedbackDraftRecord as Feedback, MarketingImageEditRecord as Edit

#: The prompt policy of an ads-first correction or image tool.
PROMPT_POLICY = "ideas-feedback-brief-v1"
#: External-creative states whose image exists and may join ideas.
UPLOAD_READY = ("ready", "tagging_failed", "confirmed")


def _ready(session, ctx, row) -> None:
    """Changing or adding ads needs drafted ideas and no drafting call in flight."""
    service.ensure_active(session, ctx)
    if row.state != "ideas_ready" or not row.ideas_json:
        raise MarketingError("ideas_not_ready", "请先起草广告主张。", 409)
    call = service.latest_call(session, ctx, row.id)
    if call and call.state in ("queued", "running", "uncertain"):
        raise MarketingError("planning_in_progress", "请先等待起草完成或核对不确定调用。", 409)


def _lock(session, ctx, row) -> None:
    session.execute(select(MarketingProjectRecord.id).where(MarketingProjectRecord.workspace_id == ctx.workspace_id,
                                                            MarketingProjectRecord.id == row.id).with_for_update())


def _ad(session, ctx, row, asset_version_id):
    for version, recipe in ideas.project_ads(session, ctx, row):
        if version.id == asset_version_id:
            return version, recipe
    raise MarketingError("ad_not_found", "这张广告不属于这些广告想法。", 404)


def _current_content(row, intent) -> None:
    """The ad's content is still what the ideas hold under its combination, or ``ideas_changed``."""
    try:
        now = ideas.ideas_intent(row, (intent.get("combination") or {}).get("id", ""))
    except LookupError:
        now = None
    if now is None or ideas.content_of(now) != ideas.content_of(intent):
        raise MarketingError("ideas_changed", "这张广告已不是当前主张的内容：请按当前的主张重新出图。", 409)


def _not_needing_redo(session, ctx, row, version) -> None:
    """A processed image keeps what its source stood on: processing an ad that needs redoing cannot help."""
    from . import ideas_export
    status = ideas_export.statuses(session, ctx, row)[version.id]
    if status["status"] == "needs_redo":
        raise MarketingError("ad_needs_redo", "这张广告需要重出，处理图片不能让它可以导出：请按意见重出。", 409,
                             params={"reasons": [reason["code"] for reason in status["reasons"]]})


def _uploaded(recipe) -> bool:
    return "upload" in (recipe.canonical_json or {})


def _revision(record, kind: str) -> dict:
    """The ``revision`` block of a new version's intent: what it was made from, besides the ad's content."""
    frozen = record.feedback_json
    block = {"kind": kind, "feedback_id": record.id, "source_asset_version_id": frozen["source_asset_version_id"],
             "source_sha256": frozen["source_sha256"]}
    if kind == "correction":
        block.update(note=frozen["request_note"], check_id=frozen["check_id"],
                     finding_ids=[item["check_id"] for item in frozen["corrections"]])
    else:
        block["image_tool"] = frozen["image_tool"]["kind"]
    return block


def _prompt(session, ctx, row, frozen, intent, *, references: bool) -> str:
    """The image prompt the application writes for an ads-first correction or image tool: nothing a client supplies."""
    from . import brand_kits
    from .feedback import CORRECTIONS, REVISION
    from .product_references import REFERENCE_BRIEF
    doc, combo, *_ = ideas._ad_parts(row, frozen["combination_id"])
    kit = brand_kits.resolve_ref(session, ctx, doc.brand_kit) if doc.brand_kit else None
    # The source image already carries the logo; the brand lines still bound colours, type and claims.
    brand = brand_kits.brief_lines(kit.content_json, logo_supplied=False, logo_in_source=True) if kit else ()
    lines = [f"- {item['check_id']} ({item['status']}): {item['correction']}" for item in frozen["corrections"]]
    lines += ["- Note: " + " ".join(frozen["note"].split())] if frozen["note"] else []
    return (REVISION + service.image_brief(combo, intent, brand=brand) + ("\n" + REFERENCE_BRIEF if references else "")
            + "\n" + CORRECTIONS + "".join("\n" + line for line in lines))


def _corrections(session, ctx, row, version, intent) -> tuple[str | None, list]:
    """The findings of the source's newest finished check that the user has not accepted, as corrections."""
    from . import ai_qc, ideas_checks, ideas_export
    from .feedback import guidance
    call = ideas_checks.latest_by_asset(session, ctx, row.id).get(version.id)
    if call is None or call.state != "succeeded":
        return None, []
    _, accepted = ideas_export.confirmations(session, ctx, row)
    taken = accepted.get((version.id, call.id), set())
    required = ai_qc.ideas_required_checks(intent)
    corrections = [{"check_id": finding["check_id"], "status": finding["status"],
                    "correction": guidance({"check_id": finding["check_id"],
                                            "correction": finding["suggested_fix"] or finding["rationale"]},
                                           required, intent["ad_language"])[:140]}
                   for finding in ideas_checks.projection(call)["findings"] if finding["check_id"] not in taken]
    return call.id, sorted(corrections, key=lambda item: (item["status"] != "fail", item["check_id"]))


def prepare_feedback(session, ctx, row, body) -> Feedback:
    """Prepare a correction or an image tool of one ad as a generation draft; nothing is spent here.

    The client previews the draft (the quote) and submits it like any other generation draft. Preparing
    the same thing again returns the same draft."""
    from app.generation.draft import create_draft
    from app.generation.image_inputs import source_image
    from app.generation.image_tools import instruction, validate_tool
    from app.providers.router import get_provider
    from .feedback import generation_ratio
    from .languages import guidance_matches
    from .product_references import for_ideas
    _ready(session, ctx, row)
    version, recipe = _ad(session, ctx, row, body.asset_version_id)
    if _uploaded(recipe):
        raise MarketingError("external_creative_not_regenerable", "上传的图不能重出或用图片工具处理，可以裁剪旋转，或在这个讲法下重新出图。", 409)
    mode = "mock" if body.provider == "mock" else "byok"
    if body.model not in get_provider(body.provider).models or version.execution_mode != mode:
        raise MarketingError("feedback_provider_invalid", "供应商、模型与这张广告的出图模式不匹配。")
    tool = None
    if body.image_tool:
        try:
            tool = validate_tool(body.image_tool, body.provider, body.model).model_dump(mode="json")
        except ValueError as exc:
            raise MarketingError("image_tool_unsupported", str(exc)) from exc
    source_intent = ideas.intent_of(recipe)
    _current_content(row, source_intent)
    note = body.note.strip()
    if not guidance_matches(note, row.ad_language):
        raise MarketingError("feedback_note_language_mismatch", "修改说明会原样发给生图模型，请用英文或广告语言填写。",
                             params={"ad_language": row.ad_language})
    combination_id = source_intent["combination"]["id"]
    if tool:
        # Processing keeps what the source stood on, so it is offered only for an ad that may still be exported.
        _not_needing_redo(session, ctx, row, version)
        check_id, corrections = None, []
        intent = source_intent
        note = " ".join(part for part in (note, instruction(tool)) if part)
        references = (recipe.canonical_json.get("selection") or {}).get("product_references") or []
    else:
        # A claim that still cites a removed fact needs the claim changed first: a new image would need redoing too.
        facts = {fact["id"] for fact in (row.understanding_json or {}).get("facts", [])}
        removed = sorted(fid for fid in ideas.citations(row, combination_id) if fid not in facts)
        if removed:
            raise MarketingError("claim_cites_removed_fact", "这个主张引用的事实已被删掉，先改主张，再重出这张广告。", 409,
                                 params={"fact_ids": removed})
        intent = ideas.ideas_intent(row, combination_id)
        check_id, corrections = _corrections(session, ctx, row, version, source_intent)
        if not corrections and not note:
            raise MarketingError("feedback_required", "请补充希望调整的内容。")
        # A correction is made on the facts and photos as they are now (#198, #199): checked photos first, then
        # unchecked ones, never a rejected one -- so redoing an ad whose photo was rejected sends another.
        references = for_ideas(session, ctx, row)
    frozen = {"project_id": row.id, "source_asset_version_id": version.id, "source_sha256": version.content_sha256,
              "combination_id": combination_id, "check_id": check_id, "corrections": corrections, "note": note,
              "request_note": body.note.strip(), "provider": body.provider, "model": body.model,
              **({"image_tool": tool} if tool else {}), "prompt_policy": PROMPT_POLICY,
              # The same request on other photos is another correction, never the earlier draft (#199).
              **({} if tool else {"photo_ids": [ref.get("content_sha256", "") for ref in references]})}
    hashed = digest(frozen)
    _lock(session, ctx, row)  # two identical requests at once prepare one draft
    previous = session.scalar(select(Feedback).where(Feedback.workspace_id == ctx.workspace_id,
                                                     Feedback.project_id == row.id, Feedback.feedback_hash == hashed))
    if previous:
        return previous
    source_image(session, ctx, version.id, row.product_id, mode)  # the exact pixels the supplier will read
    record = Feedback(id=new_id("mfd"), workspace_id=ctx.workspace_id, project_id=row.id,
                      source_asset_version_id=version.id, feedback_hash=hashed, feedback_json=frozen)
    kind = "image_tool" if tool else "correction"
    intent = {**intent, "revision": _revision(record, kind)}
    prompt = _prompt(session, ctx, row, frozen, intent, references=bool(references))
    if len(prompt) > 4000:
        raise MarketingError("feedback_too_long", "修改意见过长，请精简后重新准备。")
    draft = create_draft(session, ctx, product_id=row.product_id, provider=body.provider, model=body.model,
        execution_mode=mode, combination_strategy={"mode": "single"},
        format_config={"aspect_ratio": generation_ratio(version.aspect_ratio), "image_size": "2K" if tool else "1K", "variants": 1},
        selectors={"prompt": prompt, "marketing_intent": intent, "product_references": references,
                   "reference_actor_id": ctx.initiator_id or ctx.executor},
        regeneration_intent="quality_feedback", source_asset_version_id=version.id, source_recipe_identity=recipe.recipe_hash)
    record.draft_id = draft.id
    session.add(record)
    session.flush()
    append_event(session, event_type="marketing.ideas_feedback_prepared", resource_type="marketing_project",
                 resource_id=row.id, details={"feedback_id": record.id, "draft_id": draft.id, "kind": kind,
                                              "source_asset_version_id": version.id, "combination_id": combination_id})
    return record


def resolve(session, ctx, record, draft) -> dict:
    """The intent a preview freezes for an ads-first correction or image-tool draft (``feedback.resolve``).

    The source must still be an ad of the ideas with the same content; a correction is frozen on the ad's
    current intent, an image tool on the source's own. The prompt must be the one this module writes."""
    frozen = record.feedback_json
    tool = frozen.get("image_tool")
    if tool and (draft.source_asset_version_id != record.source_asset_version_id
                 or draft.provider != frozen["provider"] or draft.model != frozen["model"]):
        raise MarketingError("image_tool_draft_changed", "编辑来源或供应商已变化，请重新准备编辑草稿。", 409)
    try:
        row = ideas.get_ideas_project(session, ctx, record.project_id)
    except MarketingError:
        raise MarketingError("ideas_unavailable", "找不到关联的广告想法。", 409) from None
    if draft.product_id != row.product_id or draft.source_asset_version_id != record.source_asset_version_id:
        raise MarketingError("ideas_changed", "这张广告的修改草稿已不是原来的广告，请重新准备。", 409)
    version, recipe = _ad(session, ctx, row, record.source_asset_version_id)
    if tool:
        from .feedback import generation_ratio
        if draft.format_json != {"aspect_ratio": generation_ratio(version.aspect_ratio), "image_size": "2K", "variants": 1}:
            raise MarketingError("image_tool_format_changed", "图片工具固定为一次一张 2K 处理，请恢复设置或重新准备草稿。", 409)
    source_intent = ideas.intent_of(recipe)
    _current_content(row, source_intent)
    intent = source_intent if tool else ideas.ideas_intent(row, frozen["combination_id"])
    intent = {**intent, "revision": _revision(record, "image_tool" if tool else "correction")}
    selectors = draft.selectors_json or {}
    if selectors.get("prompt") != _prompt(session, ctx, row, frozen, intent,
                                          references=bool(selectors.get("product_references"))):
        raise MarketingError("ideas_prompt_changed", "这张广告的出图说明被改过。新开头的广告只按主张和修改意见出图，请重新准备。", 409)
    ideas.assert_photos_usable(session, ctx, row, selectors.get("product_references"))
    return {"intent": intent, "feedback": {"id": record.id, "hash": record.feedback_hash, **frozen}}


def feedback_draft(record) -> dict:
    """The ``IdeasFeedbackDraft`` of a prepared correction or image tool."""
    frozen = record.feedback_json
    return {"id": record.id, "project_id": record.project_id, "draft_id": record.draft_id,
            "source_asset_version_id": record.source_asset_version_id, "combination_id": frozen["combination_id"],
            "kind": "image_tool" if frozen.get("image_tool") else "correction",
            "finding_ids": [item["check_id"] for item in frozen["corrections"]]}


def get_feedback(session, ctx, row, feedback_id: str) -> Feedback:
    record = session.scalar(select(Feedback).where(Feedback.workspace_id == ctx.workspace_id,
                                                   Feedback.project_id == row.id, Feedback.id == feedback_id))
    if record is None:
        raise MarketingError("feedback_not_found", "找不到这次修改。", 404)
    return record


# -- crop and rotate -------------------------------------------------------------------------------


def prepare_image_edit(session, ctx, row, body) -> Edit:
    """Admit a crop or rotation of one ad: a local Worker job with no reservation and no paid call.

    The new version keeps the source's intent. The same edit again returns the same record."""
    from app.entitlements.storage_quota import admit_generation
    from app.generation.image_inputs import source_image
    from app.platform.jobs import enqueue_in_session
    from app.platform.operations import create_operation
    _ready(session, ctx, row)
    version, recipe = _ad(session, ctx, row, body.asset_version_id)
    intent = ideas.intent_of(recipe)
    _current_content(row, intent)
    _not_needing_redo(session, ctx, row, version)
    if body.crop is None and body.rotation == 0:
        raise MarketingError("image_edit_empty", "请选择裁剪区域或旋转角度。")
    # The source's own mode is provenance only: an upload has none, and nothing here calls a supplier.
    mode = version.execution_mode
    inputs = {"algorithm": "crop-rotate-v1", "project_id": row.id,
              "source_image": source_image(session, ctx, version.id, row.product_id, mode),
              "crop": body.crop.model_dump() if body.crop else None, "rotation": body.rotation,
              "marketing_intent": intent,
              "product_references": (recipe.canonical_json.get("selection") or {}).get("product_references") or [],
              **({"upload": recipe.canonical_json["upload"]} if _uploaded(recipe) else {}),
              "execution_mode": mode}
    hashed = digest(inputs)
    _lock(session, ctx, row)  # two identical requests at once admit one edit
    previous = session.scalar(select(Edit).where(Edit.workspace_id == ctx.workspace_id, Edit.project_id == row.id,
                                                 Edit.input_hash == hashed))
    if previous and previous.state != "failed":
        return previous
    if previous:
        raise MarketingError("image_edit_failed", "该修改未完成，请查看错误后重新调整。", 409)
    admit_generation(session, ctx)  # no new version once the Workspace is out of storage (#158)
    op = create_operation(session, command_type="marketing.image_edit")
    edit = Edit(id=new_id("mied"), workspace_id=ctx.workspace_id, project_id=row.id,
                source_asset_version_id=version.id, operation_id=op.id, input_hash=hashed, input_json=inputs, state="queued")
    session.add(edit)
    session.flush()
    job = enqueue_in_session(session, context=ctx, job_type="marketing.image_edit", payload={"edit_id": edit.id},
                             idempotency_key=edit.id, resource_type="marketing_image_edit", resource_id=edit.id,
                             safe_to_retry_after_crash=True, max_attempts=3)
    job.operation_id = op.id
    append_event(session, event_type="marketing.ideas_image_edit_requested", resource_type="marketing_project",
                 resource_id=row.id, details={"edit_id": edit.id, "source_asset_version_id": version.id})
    return edit


def edit_subject(session, ctx, edit, inputs) -> tuple[str, str | None]:
    """The product and mode an ads-first crop's source must still have (``image_editing.execute_edit``).

    The edit's frozen input is unchanged and its ad's content is still the ideas'; otherwise
    ``image_edit_changed`` and the edit fails without a new version."""
    service.ensure_active(session, ctx)
    if digest(inputs) != edit.input_hash:
        raise MarketingError("image_edit_changed", "修改依据已变化。", 409)
    try:
        project = ideas.check_intent(session, ctx, inputs["marketing_intent"])
    except SelectionError:
        raise MarketingError("image_edit_changed", "修改依据已变化。", 409) from None
    return project.product_id, inputs["execution_mode"]


def edit_summaries(session, ctx, row) -> list[dict]:
    """``IdeasImageEdit`` of every crop or rotation of these ideas, oldest first."""
    return [{"id": e.id, "operation_id": e.operation_id, "source_asset_version_id": e.source_asset_version_id,
             "result_asset_version_id": e.result_asset_version_id, "state": e.state, "error_code": e.error_code}
            for e in session.scalars(select(Edit).where(Edit.workspace_id == ctx.workspace_id, Edit.project_id == row.id)
                                     .order_by(Edit.created_at, Edit.id)).all()]


def edited_ads(session, ctx, project_ids) -> dict:
    """The versions crops and rotations made, per project, each with a recipe-like record of its intent."""
    ads = {project_id: [] for project_id in project_ids}
    if not project_ids:
        return ads
    for edit, version in session.execute(select(Edit, AssetVersionRecord).join(AssetVersionRecord,
            (AssetVersionRecord.id == Edit.result_asset_version_id) & (AssetVersionRecord.workspace_id == ctx.workspace_id))
            .where(Edit.workspace_id == ctx.workspace_id, Edit.project_id.in_(project_ids), Edit.state == "succeeded")).all():
        inputs = edit.input_json
        if version.parent_version_id != edit.source_asset_version_id or digest(inputs) != edit.input_hash:
            continue
        canonical = {"selection": {"marketing_intent": inputs["marketing_intent"],
                                   "product_references": inputs.get("product_references") or []},
                     "image_edit": {"kind": "image_edit", "edit_id": edit.id, "source_asset_version_id": edit.source_asset_version_id,
                                    "source_sha256": inputs["source_image"]["sha256"], "crop": inputs["crop"],
                                    "rotation": inputs["rotation"], "algorithm": inputs["algorithm"]},
                     **({"upload": inputs["upload"]} if inputs.get("upload") else {})}
        ads[edit.project_id].append((version, SimpleNamespace(recipe_hash=edit.input_hash, canonical_json=canonical)))
    return ads


# -- uploads ---------------------------------------------------------------------------------------


def place_upload(session, ctx, row, external_creative_id: str, combination_id: str) -> Upload:
    """Place an uploaded image of this product under one ad of the ideas (design doc §5.3).

    The image must be an external creative of the project's product whose upload is done and whose
    uploader declared the right to use it in ads. The placement freezes the ad's intent; placing the same
    image under the same ad again returns the same placement, under another ad is refused."""
    _ready(session, ctx, row)
    creative = session.scalar(select(ExternalCreativeRecord).where(ExternalCreativeRecord.workspace_id == ctx.workspace_id,
                                                                   ExternalCreativeRecord.id == external_creative_id))
    if creative is None:
        raise MarketingError("external_creative_not_found", "找不到这张上传的图。", 404)
    if creative.product_id != row.product_id:
        raise MarketingError("upload_other_product", "这张图上传在别的产品下，不能放进这个产品的广告。", 409)
    if creative.status not in UPLOAD_READY or not creative.asset_version_id:
        raise MarketingError("upload_not_ready", "这张图还在处理或没有通过上传检查。", 409, params={"status": creative.status})
    rights = creative.rights_json or {}
    if rights.get("permission_confirmed") is not True:
        raise MarketingError("rights_confirmation_required", "请先确认你有权在广告中使用这张图。", 422)
    try:
        intent = ideas.ideas_intent(row, combination_id)
    except LookupError:
        raise MarketingError("combination_not_found", "这些广告想法里没有这张广告的位置。", 404) from None
    _lock(session, ctx, row)
    existing = session.scalar(select(Upload).where(Upload.workspace_id == ctx.workspace_id, Upload.project_id == row.id,
                                                   Upload.asset_version_id == creative.asset_version_id))
    if existing is not None:
        if existing.combination_id != combination_id:
            raise MarketingError("upload_already_placed", "这张图已经放在这些广告想法的另一个讲法下。", 409,
                                 params={"combination_id": existing.combination_id})
        return existing
    intent = {**intent, "upload": {"external_creative_id": creative.id, "made_by": creative.made_by,
              **{key: rights.get(key) for key in ("permission_confirmed", "declared_by", "declared_at")}}}
    record = Upload(id=new_id("miu"), workspace_id=ctx.workspace_id, project_id=row.id, combination_id=combination_id,
                    external_creative_id=creative.id, asset_version_id=creative.asset_version_id, intent_json=intent,
                    intent_hash=digest(intent), actor_id=ctx.initiator_id or ctx.executor, created_at=utcnow())
    session.add(record)
    session.flush()
    append_event(session, event_type="marketing.ideas_upload_placed", resource_type="marketing_project", resource_id=row.id,
                 details={"upload_id": record.id, "external_creative_id": creative.id,
                          "asset_version_id": creative.asset_version_id, "combination_id": combination_id})
    return record


def uploaded_ads(session, ctx, project_ids) -> dict:
    """The uploaded images placed under the ideas' ads, per project, each with a recipe-like record of its intent."""
    ads = {project_id: [] for project_id in project_ids}
    if not project_ids:
        return ads
    for upload, version in session.execute(select(Upload, AssetVersionRecord).join(AssetVersionRecord,
            (AssetVersionRecord.id == Upload.asset_version_id) & (AssetVersionRecord.workspace_id == ctx.workspace_id))
            .where(Upload.workspace_id == ctx.workspace_id, Upload.project_id.in_(project_ids))).all():
        if digest(upload.intent_json) != upload.intent_hash:
            continue
        canonical = {"selection": {"marketing_intent": upload.intent_json},
                     "reuse": {"kind": "uploaded_for_ideas", "external_creative_id": upload.external_creative_id},
                     "upload": upload.intent_json["upload"]}
        ads[upload.project_id].append((version, SimpleNamespace(recipe_hash=upload.intent_hash, canonical_json=canonical)))
    return ads


def origin(version, recipe) -> str:
    """How an ad's image came to be: ``generated``, ``corrected``, ``image_tool``, ``image_edit`` or ``uploaded``."""
    canonical = recipe.canonical_json or {}
    if "image_edit" in canonical:
        return "image_edit"
    if "upload" in canonical:
        return "uploaded"
    feedback = (canonical.get("selection") or {}).get("marketing_feedback")
    if feedback:
        return "image_tool" if feedback.get("image_tool") else "corrected"
    return "generated"
