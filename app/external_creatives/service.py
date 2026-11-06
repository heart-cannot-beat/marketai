"""External-creative API commands: create (upload batch), list, read, confirm.

Authorization: any Workspace member (Owner or Member) may upload, read and
confirm, exactly who may create a generation draft or confirm a product
intake; tagging spend stays inside the Owner's Spend Guardrail (reserved at
admission). The Meta write path is untouched. Everything is
Workspace-scoped: another Workspace's creative, product or upload id is
"not found", never "forbidden" (no existence oracle).

Idempotency:

* ``POST /external-creatives`` -- the ``Idempotency-Key`` replays the same
  batch (a different body under the same key is ``409``); independent of
  the key, the same image bytes uploaded again for the same product return
  the existing creative (``replayed: true``) instead of a second asset.
* ``POST /external-creatives/{id}/confirm`` -- the same tags confirmed again
  replay the latest confirmation; different tags append a new immutable
  revision (re-tagging never rewrites a confirmation an experiment froze).
"""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    EXTERNAL_CREATIVE_MADE_BY,
    AdTestPlanRecord,
    ExternalCreativeRecord,
    ExternalCreativeTagConfirmationRecord,
    ProductRecord,
    ResultObservationRecord,
    new_id,
    utcnow,
)
from app.learnings import codes
from app.platform import audit
from app.platform.jobs import enqueue_in_session
from app.platform.operations import OperationResultRef, OperationState, create_operation
from app.platform.tenancy import TenantContext
from app.platform.upload_intents import UploadIntentNotFoundError, get_upload_intent
from app.providers.llm_chat import LlmChatError, reserve_llm_chat, resolve_llm_execution_mode

from . import identity, tagging
from . import vocabulary as vocab

JOB_TYPE = "external_creatives.process"
COMMAND_TYPE = "external_creative"
RESOURCE_TYPE = "external_creative"
UPLOAD_PURPOSE = "external_creative"
ACCEPTED_UPLOAD_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
MAX_UPLOADS = 10
MAX_NOTE_CHARS = 500
LIST_LIMIT = 100

IN_FLIGHT_STATUSES = frozenset({"queued", "gating", "preparing_asset", "tagging"})
CONFIRMABLE_STATUSES = frozenset({"ready", "tagging_failed", "confirmed"})
TERMINAL_STATUSES = frozenset({"ready", "tagging_failed", "confirmed", "rejected", "failed"})

REVIEW_REASON = "外部素材：上传者确认标签并批准用于测试"

#: Human-readable copy per failure code (the UI keys on ``code``).
FAILURE_COPY: dict[str, tuple[str, str]] = {
    "reference_gate_rejected": ("这张图片没有通过上传检查（格式、尺寸或内容安全）。", "upload_another"),
    "content_safety_unavailable": ("内容安全检查服务暂不可用，图片暂时不能使用。", "retry_later"),
    "reference_source_unavailable": ("读取上传的原图失败。", "upload_again"),
    "reference_storage_unavailable": ("保存图片时存储暂不可用。", "retry_later"),
    "image_undecodable": ("图片无法解码。", "upload_another"),
    "credential_unavailable": ("工作区的 OpenAI 密钥不可用，可以手动选择标签。", "tag_manually"),
    "credential_rejected": ("OpenAI 拒绝了工作区的密钥，可以手动选择标签。", "tag_manually"),
    "rate_limited": ("OpenAI 暂时限流，可以手动选择标签。", "tag_manually"),
    "schema_validation_failed": ("AI 返回的结果格式不正确，可以手动选择标签。", "tag_manually"),
    "invalid_json": ("AI 返回的结果格式不正确，可以手动选择标签。", "tag_manually"),
    "model_refusal": ("AI 拒绝识别这张图片，可以手动选择标签。", "tag_manually"),
}


@dataclass
class ExternalCreativeProblem(Exception):
    """A deterministic refusal the router turns into Problem Details."""

    status: int
    code: str
    message: str
    retryable: bool = False

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def require_member(ctx: TenantContext) -> None:
    if ctx.role not in ("owner", "member"):
        raise ExternalCreativeProblem(403, "external_creative_forbidden", "只有工作区成员可以上传和确认外部素材")


def _load(session, ctx: TenantContext, creative_id: str, *, for_update: bool = False) -> ExternalCreativeRecord:
    statement = select(ExternalCreativeRecord).where(
        ExternalCreativeRecord.id == creative_id,
        ExternalCreativeRecord.workspace_id == ctx.workspace_id,
    )
    if for_update:
        statement = statement.with_for_update()
    record = session.scalars(statement).one_or_none()
    if record is None:
        raise ExternalCreativeProblem(404, "external_creative_not_found", "外部素材不存在")
    return record


def _product(session, ctx: TenantContext, product_id: str) -> ProductRecord:
    product = session.scalars(
        select(ProductRecord).where(
            ProductRecord.workspace_id == ctx.workspace_id,
            (ProductRecord.id == product_id) | (ProductRecord.product_profile_entry_id == product_id),
        )
    ).first()
    if product is None:
        raise ExternalCreativeProblem(404, "product_not_found", "产品不存在")
    return product


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def _rights(raw: dict | None) -> tuple[str, dict]:
    """The uploader's declaration; ``declared_by`` is set server-side."""
    raw = dict(raw or {})
    if raw.get("permission_confirmed") is not True:
        raise ExternalCreativeProblem(
            422, "rights_confirmation_required", "请确认你有权在广告中使用这些图片",
        )
    made_by = str(raw.get("made_by") or "self")
    if made_by not in EXTERNAL_CREATIVE_MADE_BY:
        raise ExternalCreativeProblem(422, "made_by_invalid", f"made_by 必须是 {', '.join(EXTERNAL_CREATIVE_MADE_BY)} 之一")
    note = " ".join(str(raw.get("note") or "").split())[:MAX_NOTE_CHARS]
    return made_by, {"made_by": made_by, "permission_confirmed": True, "note": note or None}


def _uploads(session, ctx: TenantContext, upload_intent_ids: list[str]) -> list[dict]:
    uploads: list[dict] = []
    for intent_id in upload_intent_ids:
        try:
            intent = get_upload_intent(session, intent_id)
        except UploadIntentNotFoundError:
            raise ExternalCreativeProblem(404, "upload_intent_not_found", f"上传 {intent_id} 不存在") from None
        if intent.purpose != UPLOAD_PURPOSE:
            raise ExternalCreativeProblem(
                422, "upload_purpose_not_allowed",
                f"上传 {intent_id} 的用途是 {intent.purpose}，外部素材只接受 {UPLOAD_PURPOSE}",
            )
        if intent.state != "finalized" or not intent.committed_blob_ref_key:
            raise ExternalCreativeProblem(409, "upload_intent_not_finalized", f"上传 {intent_id} 尚未完成校验")
        content_type = (intent.content_type or "").lower()
        if content_type not in ACCEPTED_UPLOAD_TYPES:
            raise ExternalCreativeProblem(422, "upload_type_not_allowed", "外部素材只接受 PNG、JPEG 或 WebP 图片")
        try:
            digest = base64.b64decode(intent.checksum_sha256_b64, validate=True)
        except (ValueError, TypeError):
            raise ExternalCreativeProblem(409, "upload_intent_not_finalized", f"上传 {intent_id} 缺少校验和") from None
        if len(digest) != 32:
            raise ExternalCreativeProblem(409, "upload_intent_not_finalized", f"上传 {intent_id} 缺少校验和")
        uploads.append({
            "upload_intent_id": intent.id,
            "committed_key": intent.committed_blob_ref_key,
            "storage_version_id": intent.committed_blob_ref_version or "",
            "sha256": digest.hex(),
            "mime_type": content_type,
            "byte_size": int(intent.content_length_max or 0),
            "filename": (intent.filename_original or "")[:300],
        })
    return uploads


def create_external_creatives(
    ctx: TenantContext,
    *,
    product_id: str,
    upload_intent_ids: list[str],
    rights: dict | None,
    idempotency_key: str,
) -> dict:
    """Admit a batch: per image a row + Operation + Job (+ tagging reservation)."""
    require_member(ctx)
    requested = list(dict.fromkeys(i.strip() for i in upload_intent_ids or [] if i and i.strip()))
    if not requested:
        raise ExternalCreativeProblem(422, "uploads_required", "请至少上传一张图片")
    if len(requested) > MAX_UPLOADS:
        raise ExternalCreativeProblem(422, "too_many_uploads", f"一次最多上传 {MAX_UPLOADS} 张图片")
    made_by, rights_json = _rights(rights)
    key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    request_fingerprint = _sha({"product_id": product_id, "uploads": sorted(requested), "rights": rights_json})

    with tenant_session(ctx) as session:
        batch = list(session.scalars(
            select(ExternalCreativeRecord)
            .where(
                ExternalCreativeRecord.workspace_id == ctx.workspace_id,
                ExternalCreativeRecord.batch_key_hash == key_hash,
            )
            .order_by(ExternalCreativeRecord.created_at, ExternalCreativeRecord.id)
        ))
        if batch:
            if any(item.request_fingerprint != request_fingerprint for item in batch):
                raise ExternalCreativeProblem(409, "idempotency_key_reused", "Idempotency-Key 已用于另一次上传")
            return {"items": [{**creative_view(session, ctx, item), "replayed": True} for item in batch]}

        product = _product(session, ctx, product_id)
        uploads = _uploads(session, ctx, requested)
        mode = resolve_llm_execution_mode(session, ctx)
        vocabulary = vocab.build_vocabulary(session, ctx, product.id, language=ctx.ui_language)
        vocabulary_hash = vocab.vocabulary_hash(vocabulary)
        declared = {**rights_json, "declared_by": ctx.initiator_id, "declared_at": utcnow().isoformat()}
        items: list[dict] = []
        seen: set[str] = set()
        for upload in uploads:
            if upload["sha256"] in seen:
                continue
            seen.add(upload["sha256"])
            existing = session.scalars(
                select(ExternalCreativeRecord).where(
                    ExternalCreativeRecord.workspace_id == ctx.workspace_id,
                    ExternalCreativeRecord.product_id == product.id,
                    ExternalCreativeRecord.upload_sha256 == upload["sha256"],
                )
            ).one_or_none()
            if existing is not None:
                audit.append_event(
                    session, event_type="external_creative.replayed", resource_type=RESOURCE_TYPE,
                    resource_id=existing.id, details={"upload_intent_id": upload["upload_intent_id"]},
                )
                items.append({**creative_view(session, ctx, existing), "replayed": True})
                continue
            items.append({**_admit(
                session, ctx, product=product, upload=upload, made_by=made_by, rights=declared,
                mode=mode, vocabulary=vocabulary, vocabulary_hash=vocabulary_hash,
                key_hash=key_hash, request_fingerprint=request_fingerprint,
            ), "replayed": False})
        session.flush()
        return {"items": items}


def _admit(
    session,
    ctx: TenantContext,
    *,
    product: ProductRecord,
    upload: dict,
    made_by: str,
    rights: dict,
    mode: str,
    vocabulary: dict,
    vocabulary_hash: str,
    key_hash: str,
    request_fingerprint: str,
) -> dict:
    creative_id = new_id("exc")
    now = utcnow()
    record = ExternalCreativeRecord(
        id=creative_id,
        workspace_id=ctx.workspace_id,
        product_id=product.id,
        status="queued",
        upload_intent_id=upload["upload_intent_id"],
        upload_key=upload["committed_key"],
        upload_storage_version_id=upload["storage_version_id"],
        upload_sha256=upload["sha256"],
        upload_mime_type=upload["mime_type"],
        upload_byte_size=upload["byte_size"],
        filename=upload["filename"],
        made_by=made_by,
        rights_json=rights,
        execution_mode=mode,
        vocabulary_json=vocabulary,
        vocabulary_hash=vocabulary_hash,
        evidence_json={},
        confirmed_revision=0,
        batch_key_hash=key_hash,
        request_fingerprint=request_fingerprint,
        initiator_id=ctx.initiator_id,
        created_at=now,
        updated_at=now,
    )
    session.add(record)
    session.flush()
    operation = create_operation(
        session,
        command_type=COMMAND_TYPE,
        idempotency_key_hash=_sha({"batch": key_hash, "creative": creative_id}),
        initial_state=OperationState.ADMITTED,
        admission_contract_version=1,
        result_ref=OperationResultRef(resource_type=RESOURCE_TYPE, resource_id=creative_id),
        reason="external creative admitted",
    )
    job = enqueue_in_session(
        session,
        context=ctx,
        job_type=JOB_TYPE,
        payload={"external_creative_id": creative_id, "operation_id": operation.id},
        idempotency_key=f"external_creative:{creative_id}",
        resource_type=RESOURCE_TYPE,
        resource_id=creative_id,
        max_attempts=3,
    )
    job.operation_id = operation.id
    session.flush()
    admission: dict = {"execution_mode": mode, "vocabulary_factors": len(vocabulary["factors"])}
    if vocabulary["factors"]:
        try:
            reservation = reserve_llm_chat(
                session, ctx,
                operation_id=operation.id,
                job_id=job.id,
                call_name=tagging.CALL_NAME,
                execution_mode=mode,
                request=tagging.build_request(vocabulary, tagging.PLACEHOLDER_IMAGE),
            )
        except LlmChatError as error:
            status = {"guardrail_exceeded": 409, "entitlement_unavailable": 403,
                      "credential_unavailable": 409}.get(error.code, 422)
            raise ExternalCreativeProblem(status, error.code, error.detail) from error
        quote = reservation.quote
        admission["reservation_id"] = reservation.reservation_id
        admission["quote"] = {
            "provider": quote.provider,
            "model": quote.model,
            "input_tokens": quote.input_tokens,
            "max_output_tokens": quote.max_output_tokens,
            "max_amount": format(quote.amount, "f"),
            "simulated_max_amount": (
                format(quote.simulated_amount, "f") if quote.simulated_amount is not None else None
            ),
            "currency": quote.currency,
            "price_rule_version": quote.price_rule_version,
        }
    else:
        admission["tagging"] = "skipped_no_vocabulary"
    record.operation_id = operation.id
    record.job_id = job.id
    record.evidence_json = {"admission": admission}
    audit.append_event(
        session,
        event_type="external_creative.created",
        resource_type=RESOURCE_TYPE,
        resource_id=creative_id,
        details={
            "product_id": product.id,
            "upload_intent_id": upload["upload_intent_id"],
            "made_by": made_by,
            "execution_mode": mode,
            "vocabulary_hash": vocabulary_hash,
            "vocabulary_factors": len(vocabulary["factors"]),
            "operation_id": operation.id,
            "job_id": job.id,
        },
    )
    session.flush()
    return creative_view(session, ctx, record)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def _failure_view(record: ExternalCreativeRecord) -> dict | None:
    if record.status not in ("rejected", "failed", "tagging_failed") or not record.failure_code:
        return None
    copy, action = FAILURE_COPY.get(record.failure_code, ("处理失败。", "upload_again"))
    return {
        "code": record.failure_code,
        "message": copy,
        "detail": record.failure_detail,
        "suggested_action": action,
    }


def _suggestion_view(record: ExternalCreativeRecord) -> dict | None:
    suggestion = record.suggestion_json
    if not suggestion:
        return None
    call = dict(suggestion.get("call") or {})
    return {
        "factors": list(suggestion.get("factors") or []),
        "summary": suggestion.get("summary") or "",
        "provider": call.get("provider"),
        "model": call.get("model"),
        "execution_mode": call.get("execution_mode"),
        "preselect_confidence": tagging.PRESELECT_CONFIDENCE,
    }


def _latest(session, ctx: TenantContext, creative_id: str) -> ExternalCreativeTagConfirmationRecord | None:
    return session.scalars(
        select(ExternalCreativeTagConfirmationRecord)
        .where(
            ExternalCreativeTagConfirmationRecord.workspace_id == ctx.workspace_id,
            ExternalCreativeTagConfirmationRecord.external_creative_id == creative_id,
        )
        .order_by(ExternalCreativeTagConfirmationRecord.revision.desc())
        .limit(1)
    ).first()


def _confirmation_view(session, ctx: TenantContext, record: ExternalCreativeRecord,
                       confirmation: ExternalCreativeTagConfirmationRecord | None) -> dict | None:
    if confirmation is None:
        return None
    plan_round = None
    direction_title = None
    if confirmation.plan_id:
        plan_round = next(
            (item["round"] for item in (record.vocabulary_json or {}).get("plans") or []
             if item.get("plan_id") == confirmation.plan_id),
            None,
        )
        if confirmation.direction_key:
            found = vocab.direction(record.vocabulary_json or {}, confirmation.plan_id, confirmation.direction_key)
            direction_title = found["title"] if found else None
    return {
        "revision": confirmation.revision,
        "tags": list(confirmation.tags_json or []),
        "direction": (
            {"plan_id": confirmation.plan_id, "direction_key": confirmation.direction_key, "title": direction_title}
            if confirmation.direction_key else None
        ),
        "plan_id": confirmation.plan_id,
        "round": plan_round,
        "attributable": bool(confirmation.plan_id and confirmation.cells_json),
        "experiment_ready": bool(confirmation.concepts_json),
        "concept_count": len(confirmation.concepts_json or []),
        "stable_dimension_signature": confirmation.stable_dimension_signature or None,
        "review_id": confirmation.review_id,
        "confirmed_by": confirmation.confirmed_by,
        "confirmed_at": confirmation.created_at.isoformat() if confirmation.created_at else None,
    }


def creative_view(session, ctx: TenantContext, record: ExternalCreativeRecord) -> dict:
    asset = dict((record.evidence_json or {}).get("asset") or {})
    return {
        "external_creative_id": record.id,
        "product_id": record.product_id,
        "status": record.status,
        "current_step": record.current_step,
        "execution_mode": record.execution_mode,
        "filename": record.filename,
        "mime_type": asset.get("mime_type") or record.upload_mime_type,
        "byte_size": int(asset.get("byte_size") or record.upload_byte_size or 0),
        "width": asset.get("width"),
        "height": asset.get("height"),
        "aspect_ratio": asset.get("aspect_ratio"),
        "rights": {
            key: (record.rights_json or {}).get(key)
            for key in ("made_by", "permission_confirmed", "note", "declared_by", "declared_at")
        },
        "asset_id": record.asset_id,
        "asset_version_id": record.asset_version_id,
        "asset_code": codes.asset_code(ctx.workspace_id, record.asset_version_id) if record.asset_version_id else None,
        "thumbnail": None,
        "failure": _failure_view(record),
        "vocabulary": vocab.public_view(record.vocabulary_json or vocab.empty()),
        "suggestion": _suggestion_view(record),
        "confirmation": _confirmation_view(session, ctx, record, _latest(session, ctx, record.id)),
        "operation_id": record.operation_id,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
        "finished_at": record.finished_at.isoformat() if record.finished_at else None,
    }


def get_external_creative(ctx: TenantContext, creative_id: str) -> dict:
    require_member(ctx)
    with tenant_session(ctx) as session:
        return creative_view(session, ctx, _load(session, ctx, creative_id))


def list_external_creatives(ctx: TenantContext, *, product_id: str | None = None, status: str | None = None) -> dict:
    require_member(ctx)
    with tenant_session(ctx) as session:
        statement = select(ExternalCreativeRecord).where(ExternalCreativeRecord.workspace_id == ctx.workspace_id)
        if product_id:
            statement = statement.where(ExternalCreativeRecord.product_id == _product(session, ctx, product_id).id)
        if status:
            statement = statement.where(ExternalCreativeRecord.status == status)
        rows = session.scalars(
            statement.order_by(ExternalCreativeRecord.created_at.desc(), ExternalCreativeRecord.id.desc()).limit(LIST_LIMIT)
        )
        return {"items": [creative_view(session, ctx, row) for row in rows]}


# ---------------------------------------------------------------------------
# Confirm
# ---------------------------------------------------------------------------


def _resolve_tags(vocabulary: dict, tags: list[dict]) -> tuple[list[tuple[dict, dict]], dict[str, str | None]]:
    chosen: list[tuple[dict, dict]] = []
    answered: dict[str, str | None] = {}
    for tag in tags:
        factor_id = str(tag.get("factor_id") or "")
        factor = vocab.factor(vocabulary, factor_id)
        if factor is None:
            raise ExternalCreativeProblem(422, "unknown_factor", f"没有因素 {factor_id}")
        if factor_id in answered:
            raise ExternalCreativeProblem(422, "duplicate_factor", f"因素 {factor['label']} 只能选一个取值")
        value_id = tag.get("value_id")
        if value_id in (None, "", tagging.NONE):
            answered[factor_id] = None
            continue
        value = vocab.value(factor, str(value_id))
        if value is None:
            raise ExternalCreativeProblem(422, "unknown_value", f"因素 {factor['label']} 没有取值 {value_id}")
        answered[factor_id] = value["value_id"]
        chosen.append((factor, value))
    return chosen, answered


def _tags_json(vocabulary: dict, answered: dict[str, str | None], suggestion: dict | None) -> list[dict]:
    suggested = {item["factor_id"]: item for item in (suggestion or {}).get("factors") or []}
    rows = []
    for factor in vocabulary.get("factors") or []:
        factor_id = factor["factor_id"]
        value_id = answered.get(factor_id)
        value = vocab.value(factor, value_id) if value_id else None
        ai = suggested.get(factor_id) or {}
        rows.append({
            "factor_id": factor_id,
            "factor_label": factor["label"],
            "value_id": value_id,
            "value_label": value["label"] if value else None,
            "suggested_value_id": ai.get("value_id"),
            "suggested_confidence": ai.get("confidence"),
            "source": (
                "ai" if ai and ai.get("value_id") == value_id and factor_id in answered
                else ("user" if factor_id in answered else "untagged")
            ),
        })
    return rows


def _plan_entries(session, ctx: TenantContext, plan_ids: list[str]) -> dict[str, str]:
    if not plan_ids:
        return {}
    return {
        plan.id: plan.template_entry_id
        for plan in session.scalars(
            select(AdTestPlanRecord).where(
                AdTestPlanRecord.workspace_id == ctx.workspace_id, AdTestPlanRecord.id.in_(plan_ids),
            )
        )
        if plan.template_entry_id
    }


def confirm_external_creative(
    ctx: TenantContext,
    creative_id: str,
    *,
    tags: list[dict],
    direction: dict | None,
    approve: bool,
    idempotency_key: str,
) -> dict:
    """Write the confirmed tags' concept identity (and approve the image)."""
    from app.plans.service import PlanProblem
    from app.plans.runs import ensure_template_entry

    require_member(ctx)
    normalized_tags = sorted(
        ({"factor_id": str(t.get("factor_id") or ""), "value_id": t.get("value_id") or None} for t in tags or []),
        key=lambda item: item["factor_id"],
    )
    direction_ref = (
        {"plan_id": str(direction.get("plan_id") or ""), "direction_key": str(direction.get("direction_key") or "")}
        if direction else None
    )
    fingerprint = _sha({"tags": normalized_tags, "direction": direction_ref, "approve": bool(approve)})

    # Step 1: validate against the frozen vocabulary.
    with tenant_session(ctx) as session:
        record = _load(session, ctx, creative_id)
        latest = _latest(session, ctx, record.id)
        if latest is not None and latest.request_fingerprint == fingerprint:
            return {**creative_view(session, ctx, record), "replayed": True}
        if record.status not in CONFIRMABLE_STATUSES or not record.asset_version_id:
            raise ExternalCreativeProblem(
                409, "external_creative_not_ready", f"这张图片还不能确认（当前状态 {record.status}）",
                retryable=record.status in IN_FLIGHT_STATUSES,
            )
        vocabulary = dict(record.vocabulary_json or vocab.empty())
        chosen, answered = _resolve_tags(vocabulary, normalized_tags)
        chosen_direction = None
        if direction_ref is not None:
            chosen_direction = vocab.direction(vocabulary, direction_ref["plan_id"], direction_ref["direction_key"])
            if chosen_direction is None:
                raise ExternalCreativeProblem(422, "unknown_direction", "没有这个创意方向")
        placement = identity.placement(vocabulary, chosen, chosen_direction)
        member_plans = sorted({
            member["plan_id"] for _f, value in chosen for member in value.get("members") or [] if member.get("plan_id")
        } | ({chosen_direction["plan_id"]} if chosen_direction else set()))
        plan_entries = _plan_entries(session, ctx, member_plans)

    # Step 2: the home plan's AI template entry (a Draft entry, never a version).
    if placement.plan_id and placement.plan_id not in plan_entries:
        try:
            plan_entries[placement.plan_id] = ensure_template_entry(ctx, placement.plan_id)
        except PlanProblem as problem:
            raise ExternalCreativeProblem(problem.status, problem.code, problem.message) from problem

    # Step 3: concepts, review, the immutable confirmation -- one transaction.
    from app.generation.preview import PreviewError
    from app.generation.review import ReviewError, create_review, find_review_head

    with tenant_session(ctx) as session:
        record = _load(session, ctx, creative_id, for_update=True)
        latest = _latest(session, ctx, record.id)
        if latest is not None and latest.request_fingerprint == fingerprint:
            return {**creative_view(session, ctx, record), "replayed": True}
        if record.status not in CONFIRMABLE_STATUSES or not record.asset_version_id:
            raise ExternalCreativeProblem(409, "external_creative_not_ready", "这张图片还不能确认")
        try:
            concepts, signature = identity.build_concepts(
                session, ctx, chosen=chosen, direction=chosen_direction, plan_entries=plan_entries,
            )
        except PreviewError as error:
            raise ExternalCreativeProblem(409, error.code, error.detail) from error
        review_id = None
        if approve:
            head = find_review_head(session, ctx, record.asset_version_id)
            if head is None:
                try:
                    review = create_review(
                        session, ctx, record.asset_version_id, "approved", REVIEW_REASON,
                        actor_id=ctx.initiator_id or "unknown",
                    )
                except ReviewError as error:
                    raise ExternalCreativeProblem(409, error.code, error.detail) from error
                review_id = review.id
            else:
                review_id = head.id if head.review_value == "approved" else None
        revision = int(record.confirmed_revision or 0) + 1
        confirmation = ExternalCreativeTagConfirmationRecord(
            id=new_id("ectc"),
            workspace_id=ctx.workspace_id,
            external_creative_id=record.id,
            asset_version_id=record.asset_version_id,
            revision=revision,
            supersedes_id=latest.id if latest is not None else None,
            vocabulary_hash=record.vocabulary_hash,
            tags_json=_tags_json(vocabulary, answered, record.suggestion_json),
            concepts_json=concepts,
            stable_dimension_signature=signature,
            plan_id=placement.plan_id,
            direction_key=placement.direction_key,
            cells_json=placement.cells,
            review_id=review_id,
            request_fingerprint=fingerprint,
            confirmed_by=ctx.initiator_id,
            created_at=utcnow(),
        )
        session.add(confirmation)
        record.status = "confirmed"
        record.confirmed_revision = revision
        record.updated_at = utcnow()
        session.flush()
        audit.append_event(
            session,
            event_type="external_creative.confirmed",
            resource_type=RESOURCE_TYPE,
            resource_id=record.id,
            details={
                "revision": revision,
                "asset_version_id": record.asset_version_id,
                "tagged_factors": len(chosen),
                "ai_accepted": sum(1 for row in confirmation.tags_json if row["source"] == "ai"),
                "concepts": len(concepts),
                "plan_id": placement.plan_id,
                "direction_key": placement.direction_key,
                "approved": bool(approve),
                "review_id": review_id,
                "idempotency_key_hash": hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest(),
            },
        )
        _refresh_learnings(session, ctx, record)
        session.flush()
        return {**creative_view(session, ctx, record), "replayed": False}


def _refresh_learnings(session, ctx: TenantContext, record: ExternalCreativeRecord) -> None:
    """Results imported before the tags were confirmed now count (Issue #166).

    The learnings snapshot hashes every unit's placement, so a confirmation
    that places an image with committed observations yields a new snapshot;
    one with no observations changes nothing.
    """
    from app.learnings.attribution import evidence_mode_of_asset
    from app.learnings.service import compute_snapshot

    mode = evidence_mode_of_asset("upload", None)
    has_results = session.scalars(
        select(ResultObservationRecord.id).where(
            ResultObservationRecord.workspace_id == ctx.workspace_id,
            ResultObservationRecord.asset_version_id == record.asset_version_id,
            ResultObservationRecord.evidence_mode == mode,
        ).limit(1)
    ).first()
    if has_results is not None:
        compute_snapshot(session, ctx, record.product_id, mode)
