"""Product intake API commands: create, read, confirm (Issue #161).

Front-flow contract §3.1: the user pastes a link, images and/or text; a
background Job drafts a product card; the user edits and confirms it, which
publishes a ProductVersion bridged to a ``prod_`` record.

Authorization: any Workspace member (Owner or Member) may create, read and
confirm an intake. Confirming publishes a ``product`` entry, which the
creative-library contract §4 lets Members publish (``product`` is not in
``OWNER_ONLY_PUBLISH``); the images it binds are ``reference_asset``
versions, also Member-publishable. Everything is Workspace-scoped: another Workspace's intake id or upload id is "not
found", never "forbidden" (no existence oracle).

Idempotency:

* ``POST /products/intake`` -- the ``Idempotency-Key`` replays the same
  intake (a different body under the same key is ``409``); independent of
  the key, the same Workspace pasting the same input (same normalized URL,
  same set of image bytes, same text) within 24 hours gets the same intake
  back, unless that intake failed (a failed intake is not worth replaying --
  the user is retrying).
* ``POST /products/intake/{id}/confirm`` -- runs as resumable steps (entry,
  image promotion + binding, draft save, publish), each idempotent; a retry
  of the same confirmation resumes or replays it, a *different*
  confirmation of an already-confirmed intake is ``409``.
"""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select, text

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    ProductIntakeRecord,
    ProductRecord,
    ReferenceObjectRecord,
    new_id,
    utcnow,
)
from app.library import service as library
from app.library.contracts import ENTRY_PRODUCT, LIFECYCLE_ARCHIVED
from app.platform import audit
from app.platform.blob_store import CommittedBlobRef
from app.platform.jobs import enqueue_in_session
from app.platform.operations import OperationResultRef, OperationState, create_operation
from app.platform.tenancy import TenantContext
from app.platform.upload_intents import UploadIntentNotFoundError, get_upload_intent
from app.providers.llm_chat import LlmChatError, reserve_llm_chat, resolve_llm_execution_mode
from app.providers.llm_registry import model_spec

from . import intake_extraction as extraction
from .intake_fetch import MAX_URL_LENGTH, FetchRefused, parse_target

JOB_TYPE = "products.intake"
COMMAND_TYPE = "product_intake"
RESOURCE_TYPE = "product_intake"

REPLAY_WINDOW = timedelta(hours=24)
REPLAYABLE_STATUSES = frozenset({
    "queued", "fetching", "extracting", "preparing_images", "ready", "confirmed",
})
IN_FLIGHT_STATUSES = frozenset({"queued", "fetching", "extracting", "preparing_images"})
FAILED_STATUSES = frozenset({"fetch_failed", "extraction_failed", "failed"})
MAX_UPLOADS = 12
MAX_TEXT_CHARS = 20_000
ACCEPTED_UPLOAD_PURPOSES = frozenset({"product_intake_image", "reference_asset"})
ACCEPTED_UPLOAD_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
MAX_BOUND_IMAGES = 8
RIGHTS_VALIDITY = timedelta(days=365)

#: Human-readable copy per failure code (the UI keys on ``code``; this is
#: the default sentence) and what the user can do next.
FAILURE_COPY: dict[str, tuple[str, str]] = {
    "access_denied": ("该网站拒绝了自动读取（反爬或需要登录）。", "paste_text_or_screenshot"),
    "not_found": ("链接对应的页面不存在。", "check_link"),
    "timeout": ("网站在 10 秒内没有响应。", "paste_text_or_screenshot"),
    "connection_failed": ("无法连接到该网站。", "paste_text_or_screenshot"),
    "dns_failed": ("无法解析该网址的域名。", "check_link"),
    "http_error": ("网站返回了错误。", "paste_text_or_screenshot"),
    "unsupported_content_type": ("链接不是商品网页或图片。", "paste_text_or_screenshot"),
    "not_product_page": ("这个页面上读不到商品信息（可能需要脚本渲染，或不是商品页）。", "paste_text_or_screenshot"),
    "too_many_redirects": ("链接跳转次数过多。", "paste_text_or_screenshot"),
    "blocked_address": ("出于安全原因，不能读取内网或本机地址。", "paste_text_or_screenshot"),
    "invalid_url": ("链接格式不正确。", "check_link"),
    "scheme_not_allowed": ("只支持 http 或 https 链接。", "check_link"),
    "port_not_allowed": ("只支持标准端口（80/443）的链接。", "check_link"),
    "credentials_in_url": ("链接里不能包含用户名或密码。", "check_link"),
    "too_large": ("页面或图片太大。", "paste_text_or_screenshot"),
    "extraction_invalid": ("AI 没能从这些内容中整理出完整的产品卡。", "add_more_detail"),
    "schema_validation_failed": ("AI 返回的结果格式不正确。", "retry"),
    "invalid_json": ("AI 返回的结果格式不正确。", "retry"),
    "model_refusal": ("AI 拒绝处理这些内容。", "add_more_detail"),
    "credential_unavailable": ("工作区的 OpenAI 密钥不可用，请在设置中重新绑定。", "bind_openai_key"),
    "credential_rejected": ("OpenAI 拒绝了工作区的密钥。", "bind_openai_key"),
    "rate_limited": ("OpenAI 暂时限流，请稍后重试。", "retry"),
    "quota_exhausted": ("工作区 OpenAI 密钥所在账户的余额或额度已用完，请充值或换一个密钥。", "bind_openai_key"),
    "guardrail_exceeded": ("本次识别会超出花费护栏。", "adjust_spend_guardrail"),
}


@dataclass
class IntakeProblem(Exception):
    """A deterministic refusal the router turns into Problem Details."""

    status: int
    code: str
    message: str
    retryable: bool = False
    params: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _require_member(ctx: TenantContext) -> None:
    if ctx.role not in ("owner", "member"):
        raise IntakeProblem(403, "product_intake_forbidden", "只有工作区成员可以识别和确认产品")


def _load(session, ctx: TenantContext, intake_id: str, *, for_update: bool = False) -> ProductIntakeRecord:
    statement = select(ProductIntakeRecord).where(
        ProductIntakeRecord.id == intake_id,
        ProductIntakeRecord.workspace_id == ctx.workspace_id,
    )
    if for_update:
        statement = statement.with_for_update()
    record = session.scalars(statement).one_or_none()
    if record is None:
        raise IntakeProblem(404, "product_intake_not_found", "产品识别记录不存在")
    return record


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def _normalize_url(url: str | None, ctx: TenantContext) -> str | None:
    if url is None or not url.strip():
        return None
    try:
        target = parse_target(url)
    except FetchRefused as refused:
        if refused.code == "blocked_address":
            with tenant_session(ctx) as session:
                audit.append_event(
                    session,
                    event_type="product_intake.fetch_refused",
                    resource_type=RESOURCE_TYPE,
                    resource_id="",
                    details={
                        "step": "admission",
                        "code": refused.code,
                        "blocked_category": refused.category,
                    },
                )
        raise IntakeProblem(422, refused.code, refused.detail) from refused
    normalized = f"{target.scheme}://{target.host_header}{target.path_query}"
    # product_intakes.source_url is varchar(MAX_URL_LENGTH). The raw link is
    # bounded by parse_target, but normalizing can lengthen it (an empty path
    # becomes "/", an IDN host becomes punycode); PostgreSQL would reject the
    # longer value with a 500, so refuse it here like any over-long link.
    if len(normalized) > MAX_URL_LENGTH:
        raise IntakeProblem(422, "invalid_url", f"the link is longer than {MAX_URL_LENGTH} characters")
    return normalized


def _upload_inputs(session, ctx: TenantContext, upload_intent_ids: list[str]) -> list[dict]:
    uploads: list[dict] = []
    for intent_id in upload_intent_ids:
        try:
            intent = get_upload_intent(session, intent_id)
        except UploadIntentNotFoundError:
            raise IntakeProblem(404, "upload_intent_not_found", f"上传 {intent_id} 不存在") from None
        if intent.purpose not in ACCEPTED_UPLOAD_PURPOSES:
            raise IntakeProblem(
                422, "upload_purpose_not_allowed",
                f"上传 {intent_id} 的用途是 {intent.purpose}，产品识别只接受 product_intake_image",
            )
        if intent.state != "finalized" or not intent.committed_blob_ref_key:
            raise IntakeProblem(409, "upload_intent_not_finalized", f"上传 {intent_id} 尚未完成校验")
        content_type = (intent.content_type or "").lower()
        if content_type not in ACCEPTED_UPLOAD_TYPES:
            raise IntakeProblem(422, "upload_type_not_allowed", "产品识别只接受 PNG、JPEG 或 WebP 图片")
        try:
            digest = base64.b64decode(intent.checksum_sha256_b64, validate=True)
        except (ValueError, TypeError):
            raise IntakeProblem(409, "upload_intent_not_finalized", f"上传 {intent_id} 缺少校验和") from None
        uploads.append({
            "upload_intent_id": intent.id,
            "committed_key": intent.committed_blob_ref_key,
            "storage_version_id": intent.committed_blob_ref_version or "",
            "sha256": digest.hex(),
            "mime_type": content_type,
            "byte_size": int(intent.content_length_max or 0),
            "filename": (intent.filename_original or "")[:200],
        })
    return uploads


def _lock_fingerprint(session, ctx: TenantContext, fingerprint: str) -> None:
    """Serialize concurrent intakes of the same input (PostgreSQL; SQLite has one writer)."""
    if session.get_bind().dialect.name != "postgresql":
        return
    key = int.from_bytes(hashlib.sha256(f"{ctx.workspace_id}:{fingerprint}".encode()).digest()[:8], "big", signed=True)
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def create_intake(
    ctx: TenantContext,
    *,
    url: str | None,
    upload_intent_ids: list[str] | None,
    text_input: str | None,
    idempotency_key: str,
) -> dict:
    """Admit an intake: row + Operation + Job + LLM reservation, one transaction."""
    _require_member(ctx)
    normalized_url = _normalize_url(url, ctx)
    uploads_requested = list(dict.fromkeys(i.strip() for i in (upload_intent_ids or []) if i and i.strip()))
    user_text = (text_input or "").strip()
    if not normalized_url and not uploads_requested and not user_text:
        raise IntakeProblem(422, "intake_input_required", "请提供商品链接、图片或一段介绍中的至少一项")
    if len(uploads_requested) > MAX_UPLOADS:
        raise IntakeProblem(422, "too_many_images", f"最多上传 {MAX_UPLOADS} 张图片", params={"limit": MAX_UPLOADS})
    if len(user_text) > MAX_TEXT_CHARS:
        raise IntakeProblem(422, "text_too_long", f"介绍最多 {MAX_TEXT_CHARS} 个字符", params={"limit": MAX_TEXT_CHARS})

    key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    request_fingerprint = _sha({
        "url": normalized_url, "uploads": sorted(uploads_requested), "text": user_text,
    })
    with tenant_session(ctx) as session:
        by_key = session.scalars(
            select(ProductIntakeRecord).where(
                ProductIntakeRecord.workspace_id == ctx.workspace_id,
                ProductIntakeRecord.idempotency_key_hash == key_hash,
            )
        ).one_or_none()
        if by_key is not None:
            if by_key.request_fingerprint != request_fingerprint:
                raise IntakeProblem(409, "idempotency_key_reused", "Idempotency-Key 已用于另一次识别请求")
            return {**intake_view(session, ctx, by_key), "replayed": True}

        uploads = _upload_inputs(session, ctx, uploads_requested)
        input_fingerprint = _sha({
            "url": normalized_url,
            "images": sorted(item["sha256"] for item in uploads),
            "text_sha256": hashlib.sha256(user_text.encode("utf-8")).hexdigest() if user_text else None,
        })
        _lock_fingerprint(session, ctx, input_fingerprint)
        recent = session.scalars(
            select(ProductIntakeRecord)
            .where(
                ProductIntakeRecord.workspace_id == ctx.workspace_id,
                ProductIntakeRecord.input_fingerprint == input_fingerprint,
                ProductIntakeRecord.created_at >= utcnow() - REPLAY_WINDOW,
                ProductIntakeRecord.status.in_(sorted(REPLAYABLE_STATUSES)),
            )
            .order_by(ProductIntakeRecord.created_at.desc())
        ).first()
        if recent is not None:
            audit.append_event(
                session, event_type="product_intake.replayed", resource_type=RESOURCE_TYPE,
                resource_id=recent.id, details={"input_fingerprint": input_fingerprint},
            )
            return {**intake_view(session, ctx, recent), "replayed": True}

        mode = resolve_llm_execution_mode(session, ctx)
        intake_id = new_id("pin")
        candidates = [
            {
                "image_id": f"img_{index}",
                "source": "upload",
                **{key: value for key, value in upload.items()},
            }
            for index, upload in enumerate(uploads, start=1)
        ]
        now = utcnow()
        record = ProductIntakeRecord(
            id=intake_id,
            workspace_id=ctx.workspace_id,
            status="queued",
            input_fingerprint=input_fingerprint,
            request_fingerprint=request_fingerprint,
            idempotency_key_hash=key_hash,
            source_url=normalized_url,
            input_text=user_text or None,
            upload_intent_ids=[item["upload_intent_id"] for item in uploads],
            inputs_json={
                "url": normalized_url,
                "text_chars": len(user_text),
                "text_sha256": hashlib.sha256(user_text.encode("utf-8")).hexdigest() if user_text else None,
                "uploads": uploads,
            },
            execution_mode=mode,
            evidence_json={},
            images_json=candidates,
            initiator_id=ctx.initiator_id,
            created_at=now,
            updated_at=now,
        )
        session.add(record)
        session.flush()
        operation = create_operation(
            session,
            command_type=COMMAND_TYPE,
            idempotency_key_hash=key_hash,
            initial_state=OperationState.ADMITTED,
            admission_contract_version=1,
            result_ref=OperationResultRef(resource_type=RESOURCE_TYPE, resource_id=intake_id),
            reason="product intake admitted",
        )
        job = enqueue_in_session(
            session,
            context=ctx,
            job_type=JOB_TYPE,
            payload={"intake_id": intake_id, "operation_id": operation.id},
            idempotency_key=f"product_intake:{intake_id}",
            resource_type=RESOURCE_TYPE,
            resource_id=intake_id,
            max_attempts=3,
        )
        job.operation_id = operation.id
        session.flush()
        try:
            reservation = reserve_llm_chat(
                session, ctx,
                operation_id=operation.id,
                job_id=job.id,
                call_name=extraction.CALL_NAME,
                execution_mode=mode,
                input_token_budget=extraction.input_token_budget(
                    model_spec("openai" if mode == "byok" else "mock", None)
                ),
                max_output_tokens=extraction.MAX_OUTPUT_TOKENS,
            )
        except LlmChatError as error:
            status = {"guardrail_exceeded": 409, "entitlement_unavailable": 403,
                      "credential_unavailable": 409}.get(error.code, 422)
            raise IntakeProblem(status, error.code, error.detail) from error
        record.operation_id = operation.id
        record.job_id = job.id
        record.evidence_json = {
            "admission": {
                "execution_mode": mode,
                "reservation_id": reservation.reservation_id,
                "quote": {
                    "provider": reservation.quote.provider,
                    "model": reservation.quote.model,
                    "max_amount": format(reservation.quote.amount, "f"),
                    "simulated_max_amount": (
                        format(reservation.quote.simulated_amount, "f")
                        if reservation.quote.simulated_amount is not None else None
                    ),
                    "currency": reservation.quote.currency,
                    "price_rule_version": reservation.quote.price_rule_version,
                },
            }
        }
        audit.append_event(
            session,
            event_type="product_intake.created",
            resource_type=RESOURCE_TYPE,
            resource_id=intake_id,
            details={
                "has_url": bool(normalized_url),
                "host": parse_target(normalized_url).host if normalized_url else None,
                "upload_count": len(uploads),
                "text_chars": len(user_text),
                "execution_mode": mode,
                "operation_id": operation.id,
                "job_id": job.id,
            },
        )
        session.flush()
        return {**intake_view(session, ctx, record), "replayed": False}


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


_PUBLIC_IMAGE_KEYS = (
    "image_id", "rank", "role", "source", "page_source", "source_url", "alt", "mime_type",
    "byte_size", "width", "height", "sha256", "gate_state", "gate_code", "reference_object_id",
)
_PUBLIC_EVIDENCE_DROP = frozenset({"digest_text"})


def _failure_view(record: ProductIntakeRecord) -> dict | None:
    if record.status not in FAILED_STATUSES:
        return None
    code = record.failure_code or record.status
    copy, action = FAILURE_COPY.get(code, ("识别失败。", "retry"))
    step = record.current_step or next(
        (step for step in ("images", "extract", "fetch") if (record.evidence_json or {}).get(step, {}).get("status") == "failed"),
        None,
    )
    fetch = (record.evidence_json or {}).get("fetch") or {}
    view = {
        "code": code,
        "step": step,
        "message": copy,
        "detail": record.failure_detail,
        "suggested_action": action,
        "retryable": bool(((record.evidence_json or {}).get(step or "", {}) or {}).get("retryable", False)),
    }
    if fetch.get("blocked_category"):
        view["blocked_category"] = fetch["blocked_category"]
    return view


def _must_match_view(item: dict) -> dict:
    return {key: item.get(key) for key in ("id", "type", "description", "severity", "proposed_by", "confirmed")}


def _card_view(record: ProductIntakeRecord) -> dict | None:
    card = record.card_json
    if not card:
        return None
    variants = [variant for variant in card.get("model_variants") or [] if isinstance(variant, dict)]
    owner = {
        image_id: variant.get("label")
        for variant in variants for image_id in variant.get("image_ids") or []
    }
    images = []
    for item in record.images_json or []:
        if "rank" not in item and record.status != "ready" and record.status != "confirmed":
            continue
        view = {key: item.get(key) for key in _PUBLIC_IMAGE_KEYS if key in item}
        view["selectable"] = item.get("gate_state") == "released" and item.get("role") != "irrelevant"
        view["variant_label"] = owner.get(item.get("image_id"))
        images.append(view)
    images.sort(key=lambda item: item.get("rank") or 10_000)
    selectable_ids = [item["image_id"] for item in images if item["selectable"]]
    default_images = selectable_ids[:MAX_BOUND_IMAGES]
    shared = [item for item in card.get("must_match") or [] if isinstance(item, dict)]
    variant_views = []
    for variant in variants:
        _composed, replaced = extraction.variant_must_match(shared, variant)
        variant_views.append({
            "label": variant.get("label"),
            "specs": dict(variant.get("specs") or {}),
            "appearance": [_must_match_view(item) for item in variant.get("appearance") or []],
            "replaces_must_match_ids": replaced,
            "image_ids": list(variant.get("image_ids") or []),
            "default_image_ids": extraction.variant_image_ids(
                variants, variant.get("label"), selectable_ids,
            )[:MAX_BOUND_IMAGES],
        })
    return {
        "name": card.get("name"),
        "description": card.get("description"),
        "specs": dict(card.get("specs") or {}),
        "model_variants": variant_views,
        "requires_variant_selection": len(variants) > 1,
        "selling_points": list(card.get("selling_points") or []),
        "audiences": list(card.get("audiences") or []),
        "must_match": [_must_match_view(item) for item in shared],
        "images": images,
        "default_image_ids": default_images,
        "source_url": card.get("source_url"),
        "dropped_commercial_specs": list(card.get("dropped_commercial_specs") or []),
    }


def _evidence_view(record: ProductIntakeRecord) -> dict:
    evidence: dict = {}
    for step, payload in (record.evidence_json or {}).items():
        if isinstance(payload, dict):
            evidence[step] = {key: value for key, value in payload.items() if key not in _PUBLIC_EVIDENCE_DROP}
    return evidence


def intake_view(session, ctx: TenantContext, record: ProductIntakeRecord) -> dict:
    confirmation = record.confirmation_json if record.status == "confirmed" else None
    return {
        "intake_id": record.id,
        "operation_id": record.operation_id,
        "status": record.status,
        "current_step": record.current_step,
        "execution_mode": record.execution_mode,
        "input": {
            "url": record.source_url,
            "text_chars": int((record.inputs_json or {}).get("text_chars") or 0),
            "upload_count": len(record.upload_intent_ids or []),
        },
        "failure": _failure_view(record),
        "evidence": _evidence_view(record),
        "card": _card_view(record),
        "confirmation": (
            {key: confirmation.get(key) for key in (
                "product_id", "entry_id", "version_id", "version_no", "confirmed_at",
                "card", "bound_images",
            )}
            if confirmation else None
        ),
        "product_id": record.product_id,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
        "finished_at": record.finished_at.isoformat() if record.finished_at else None,
    }


def get_intake(ctx: TenantContext, intake_id: str) -> dict:
    _require_member(ctx)
    with tenant_session(ctx) as session:
        return intake_view(session, ctx, _load(session, ctx, intake_id))


# ---------------------------------------------------------------------------
# Confirm
# ---------------------------------------------------------------------------


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _confirmed_content(record: ProductIntakeRecord, edits: dict, selected_variant: str | None) -> dict:
    """The ProductVersion content the confirmation publishes (minus image bindings)."""
    draft = dict(record.card_json or {})
    card = {**draft, **{key: value for key, value in (edits or {}).items() if value is not None}}
    name = _clean(card.get("name"), extraction.MAX_NAME_CHARS)
    description = " ".join(str(card.get("description") or "").split())[: extraction.MAX_DESCRIPTION_CHARS]
    selling_points = [
        _clean(point, extraction.MAX_SELLING_POINT_CHARS)
        for point in card.get("selling_points") or [] if _clean(point, extraction.MAX_SELLING_POINT_CHARS)
    ]
    audiences = [
        {
            "who": _clean(item.get("who"), 500),
            "why": _clean(item.get("why"), 500),
            "pain": _clean(item.get("pain"), 500),
        }
        for item in card.get("audiences") or [] if isinstance(item, dict) and _clean(item.get("who"), 500)
    ]
    primary = int(card.get("primary_audience_index") or 0)
    if audiences and not 0 <= primary < len(audiences):
        raise IntakeProblem(422, "primary_audience_out_of_range", "primary_audience_index 超出客群卡范围")
    audience_text = ""
    if audiences:
        chosen = audiences[primary]
        audience_text = f"{chosen['who']}：{chosen['why']}" if chosen["why"] else chosen["who"]
        audiences = [chosen] + [item for index, item in enumerate(audiences) if index != primary]

    variants = [variant for variant in draft.get("model_variants") or [] if isinstance(variant, dict)]
    specs = {str(k): str(v) for k, v in dict(card.get("specs") or {}).items()}
    chosen_variants: list[dict] = []
    if len(variants) > 1:
        if not selected_variant:
            raise IntakeProblem(
                422, "variant_selection_required",
                "该链接包含多个款式（型号或配色），请选择要建立产品的具体款式（selected_variant）",
            )
        match = next((variant for variant in variants if variant.get("label") == selected_variant), None)
        if match is None:
            raise IntakeProblem(422, "unknown_variant", f"没有名为 {selected_variant} 的款式")
        chosen_variants = [match]
    elif len(variants) == 1:
        chosen_variants = [variants[0]]
    elif selected_variant:
        raise IntakeProblem(422, "unknown_variant", f"没有名为 {selected_variant} 的款式")
    chosen = chosen_variants[0] if chosen_variants else None
    # The user's own spec list (the editor shows the shared specs merged with
    # the chosen variant's) is taken as is; without one, the variant's
    # differing specs are laid over the shared ones.
    if (edits or {}).get("specs") is None and chosen is not None:
        specs = {**specs, **{str(k): str(v) for k, v in dict(chosen.get("specs") or {}).items()}}

    # Must-match: the shared items with the chosen variant's own appearance
    # (Issue #192) unless the user sent the list; either way the variant's
    # appearance items count as AI proposals.
    shared = [item for item in draft.get("must_match") or [] if isinstance(item, dict)]
    composed, _replaced = extraction.variant_must_match(shared, chosen)
    proposed = {
        item.get("id"): item
        for item in [*shared, *((chosen or {}).get("appearance") or [])] if isinstance(item, dict)
    }
    requested = (edits or {}).get("must_match")
    must_match: list[dict] = []
    for item in requested if requested is not None else composed:
        if not isinstance(item, dict):
            continue
        description_text = _clean(item.get("description"), 500)
        if not description_text:
            continue
        kind = str(item.get("type") or "other")
        original = proposed.get(item.get("id"))
        entry = extraction.must_match_item(
            kind, description_text,
            proposed_by="ai" if original is not None else "user",
            confirmed=True,
        )
        if original is not None and _clean(original.get("description"), 500) == description_text \
                and original.get("type") == kind:
            entry["id"] = original["id"]
        if item.get("severity") in ("blocking", "warning"):
            entry["severity"] = item["severity"]
        if all(existing["id"] != entry["id"] for existing in must_match):
            must_match.append(entry)

    content: dict = {
        "name": name,
        "description": description,
        "audience": audience_text,
        "selling_points": selling_points,
        "audiences": audiences,
        "specs": specs,
        "must_match": must_match,
    }
    if chosen is not None:
        # The ProductVersion keeps the variant's name and specs; its
        # appearance is in ``must_match`` and its images are the bindings.
        content["model_variants"] = [{
            "label": chosen.get("label"),
            "specs": {str(k): str(v) for k, v in dict(chosen.get("specs") or {}).items()},
        }]
    if record.source_url:
        content["source_url"] = record.source_url
    return content


def _select_images(
    record: ProductIntakeRecord, image_ids: list[str] | None, variant_label: str | None,
) -> list[dict]:
    """The images to bind: the requested ones, or by default the chosen
    variant's own and the general images (never another variant's).

    An explicit ``image_ids`` may name any selectable image -- the user can
    tick another variant's image back on.
    """
    images = sorted(
        (dict(item) for item in record.images_json or [] if "rank" in item),
        key=lambda item: item["rank"],
    )
    selectable = {
        item["image_id"]: item for item in images
        if item.get("gate_state") == "released" and item.get("role") != "irrelevant"
        and item.get("reference_object_id")
    }
    if image_ids is None:
        variants = [v for v in (record.card_json or {}).get("model_variants") or [] if isinstance(v, dict)]
        kept = extraction.variant_image_ids(variants, variant_label, list(selectable))
        return [selectable[image_id] for image_id in kept][:MAX_BOUND_IMAGES]
    chosen: list[dict] = []
    for image_id in dict.fromkeys(image_ids):
        item = selectable.get(image_id)
        if item is None:
            raise IntakeProblem(422, "image_not_available", f"图片 {image_id} 不存在或未通过上传门禁")
        chosen.append(item)
    if len(chosen) > MAX_BOUND_IMAGES:
        raise IntakeProblem(422, "too_many_images", f"最多绑定 {MAX_BOUND_IMAGES} 张图片", params={"limit": MAX_BOUND_IMAGES})
    return chosen


def _rights_for(image: dict, source_url: str | None, today: date) -> dict:
    """The rights declaration confirming the card makes for one bound image.

    Confirming is the user's assertion that the selected images may be used
    for this product's creatives (``declared_by`` is always the confirming
    user, set server-side by ``normalize_rights_declaration``); ``scope``
    records where the image came from and ``evidence`` the page or upload.
    """
    if image.get("source") == "product_page":
        scope, evidence = "product_page", image.get("source_url") or source_url or ""
    else:
        scope, evidence = "user_upload", f"upload_intent:{image.get('upload_intent_id', '')}"
    return {"scope": scope, "valid_to": (today + RIGHTS_VALIDITY).isoformat(), "evidence": evidence}


#: A confirmation claim older than this no longer blocks a different
#: confirmation: every step is idempotent, so the newer request may take over
#: an attempt that crashed half way instead of leaving the intake stuck.
CONFIRMATION_CLAIM_TTL = timedelta(seconds=60)


def _claim_is_stale(confirmation: dict) -> bool:
    from datetime import datetime

    started = confirmation.get("started_at")
    if not started:
        return True
    try:
        moment = datetime.fromisoformat(started)
    except ValueError:
        return True
    return utcnow() - moment > CONFIRMATION_CLAIM_TTL


def _update_confirmation(ctx: TenantContext, intake_id: str, **fields) -> None:
    with tenant_session(ctx) as session:
        record = _load(session, ctx, intake_id, for_update=True)
        confirmation = dict(record.confirmation_json or {})
        confirmation.update(fields)
        record.confirmation_json = confirmation
        record.updated_at = utcnow()


def confirm_intake(
    ctx: TenantContext,
    intake_id: str,
    *,
    card: dict | None,
    selected_variant: str | None,
    image_ids: list[str] | None,
    entry_id: str | None,
    acknowledged_warnings: list[str] | None,
    idempotency_key: str,
) -> dict:
    """Create/update the product entry, bind images, publish, bridge ``prod_``."""
    _require_member(ctx)
    request_fingerprint = _sha({
        "card": card, "selected_variant": selected_variant, "image_ids": image_ids,
        "entry_id": entry_id, "acknowledged_warnings": sorted(acknowledged_warnings or []),
    })
    today = date.today()

    # Step 0: claim the confirmation and compute what it publishes.
    with tenant_session(ctx) as session:
        record = _load(session, ctx, intake_id, for_update=True)
        confirmation = dict(record.confirmation_json or {})
        if record.status == "confirmed":
            if confirmation.get("request_fingerprint") == request_fingerprint:
                return {**intake_view(session, ctx, record), "replayed": True}
            raise IntakeProblem(409, "intake_already_confirmed", "该识别结果已确认并发布为产品")
        if record.status != "ready":
            raise IntakeProblem(
                409, "intake_not_ready", f"识别尚未完成或已失败（当前状态 {record.status}），不能确认",
            )
        if (
            confirmation.get("state") == "in_progress"
            and confirmation.get("request_fingerprint") != request_fingerprint
            and not _claim_is_stale(confirmation)
        ):
            raise IntakeProblem(
                409, "intake_confirmation_in_progress", "另一次确认正在进行", retryable=True,
            )
        content = _confirmed_content(record, dict(card or {}), selected_variant)
        chosen_label = next(iter(content.get("model_variants") or []), {}).get("label")
        images = _select_images(record, image_ids, chosen_label)
        target_entry_id = entry_id or confirmation.get("entry_id")
        record.confirmation_json = {
            **confirmation,
            "state": "in_progress",
            "request_fingerprint": request_fingerprint,
            "idempotency_key_hash": hashlib.sha256(idempotency_key.encode()).hexdigest(),
            "started_at": utcnow().isoformat(),
            "entry_id": target_entry_id,
        }
        record.updated_at = utcnow()
        snapshot = {
            "source_url": record.source_url,
            "images": images,
        }

    # Step 1: the product entry (an existing one, or a new one with its prod_ bridge).
    if target_entry_id:
        if library.get_entry_type(ctx, target_entry_id) != ENTRY_PRODUCT:
            raise IntakeProblem(422, "entry_not_product", "只能把识别结果确认到产品条目")
        existing = library.get_entry(ctx, target_entry_id)
        if existing["lifecycle"] == LIFECYCLE_ARCHIVED:
            raise IntakeProblem(409, "entry_archived", "该产品条目已归档")
    else:
        created = library.create_entry(
            ctx, entry_type=ENTRY_PRODUCT, display_name=content["name"] or "未命名产品", content={},
            idempotency_key=f"product-intake:{intake_id}:entry",
            request_fingerprint=_sha({"intake_id": intake_id, "step": "entry"}),
        )
        target_entry_id = created["id"]
        _update_confirmation(ctx, intake_id, entry_id=target_entry_id)

    # Step 2: promote each chosen image to a reference_asset and bind it.
    bound: list[dict] = []
    for priority, image in enumerate(snapshot["images"]):
        promoted = library.promote_reference_object(
            ctx, image["reference_object_id"],
            display_name=f"{content['name'][:80]} · {image['image_id']}",
            rights=_rights_for(image, snapshot["source_url"], today),
            bind={
                "entry_id": target_entry_id,
                "purpose": "product_reference",
                "viewpoint": image.get("role") or "unspecified",
                "priority": priority,
            },
            idempotency_key=f"product-intake:{intake_id}:promote:{image['reference_object_id']}:{target_entry_id}",
            request_fingerprint=_sha({"intake_id": intake_id, "object": image["reference_object_id"], "entry": target_entry_id}),
        )
        bound.append({
            "image_id": image["image_id"],
            "reference_object_id": image["reference_object_id"],
            "reference_entry_id": promoted["entry_id"],
            "reference_version_id": promoted["version_id"],
            "viewpoint": image.get("role") or "unspecified",
        })

    # Step 3: write the confirmed card into the Draft (keeping its bindings).
    entry = library.get_entry(ctx, target_entry_id)
    draft = entry["draft"] or {}
    draft_content = dict(draft.get("content") or {})
    merged = {**draft_content, **content}
    merged["reference_bindings"] = list(draft_content.get("reference_bindings") or [])
    # Fields this intake owns are replaced, never inherited from an earlier
    # version of an existing product (a single-model page drops old variants).
    for key in ("model_variants", "source_url"):
        if key not in content:
            merged.pop(key, None)
    if draft_content != merged:
        saved = library.save_draft(ctx, target_entry_id, merged, int(draft["revision"]))
        revision = int(saved["revision"])
    else:
        revision = int(draft["revision"])

    # Step 4: publish and resolve the bridge.
    try:
        version = library.publish(
            ctx, target_entry_id, revision,
            acknowledged_warnings=list(acknowledged_warnings or []),
        )
    except library.LibraryConflict as conflict:
        current = (conflict.detail or {}).get("current_version_id")
        if not current:
            raise
        version = library.get_version(ctx, current)

    with tenant_session(ctx) as session:
        product = session.scalars(
            select(ProductRecord).where(
                ProductRecord.workspace_id == ctx.workspace_id,
                ProductRecord.product_profile_entry_id == target_entry_id,
            )
        ).first()
        if product is None:
            raise IntakeProblem(500, "product_bridge_missing", "产品条目缺少 prod_ 桥接记录")
        record = _load(session, ctx, intake_id, for_update=True)
        now = utcnow()
        record.status = "confirmed"
        record.confirmed_at = now
        record.updated_at = now
        record.product_entry_id = target_entry_id
        record.product_version_id = version["id"]
        record.product_id = product.id
        record.confirmation_json = {
            **dict(record.confirmation_json or {}),
            "state": "completed",
            "entry_id": target_entry_id,
            "version_id": version["id"],
            "version_no": version["version_no"],
            "product_id": product.id,
            "confirmed_at": now.isoformat(),
            "confirmed_by": ctx.initiator_id,
            "card": {key: value for key, value in content.items()},
            "bound_images": bound,
        }
        audit.append_event(
            session,
            event_type="product_intake.confirmed",
            resource_type=RESOURCE_TYPE,
            resource_id=intake_id,
            details={
                "entry_id": target_entry_id,
                "version_id": version["id"],
                "product_id": product.id,
                "bound_images": len(bound),
                "bound_image_ids": [item["image_id"] for item in bound],
                # The variant that entered the version (a single-model page's
                # only variant is chosen without the user picking it).
                "selected_variant": next(iter(content.get("model_variants") or []), {}).get("label"),
                "must_match": len(content.get("must_match") or []),
            },
        )
        session.flush()
        return {**intake_view(session, ctx, record), "replayed": False}


# ---------------------------------------------------------------------------
# Image preview (signed link to the released derivative)
# ---------------------------------------------------------------------------


def resolve_image_derivative(ctx: TenantContext, intake_id: str, image_id: str) -> dict:
    """The released, controlled derivative of one candidate image."""
    _require_member(ctx)
    with tenant_session(ctx) as session:
        record = _load(session, ctx, intake_id)
        item = next((i for i in record.images_json or [] if i.get("image_id") == image_id), None)
        if item is None or not item.get("reference_object_id"):
            raise IntakeProblem(404, "intake_image_not_found", "图片不存在或未经过上传门禁")
        reference = session.get(ReferenceObjectRecord, item["reference_object_id"])
        if (
            reference is None or reference.workspace_id != ctx.workspace_id
            or reference.state != "released" or not reference.derivative_object_key
        ):
            raise IntakeProblem(409, "intake_image_not_released", "该图片未通过上传门禁，不能预览")
        if not reference.derivative_object_key.startswith(f"committed/{ctx.workspace_id}/"):
            raise IntakeProblem(409, "intake_image_not_released", "该图片的受控派生不在对象存储中")
        return {
            "image_id": image_id,
            "reference_object_id": reference.id,
            "mime_type": reference.derivative_mime or "image/jpeg",
            "byte_size": reference.derivative_byte_size,
            "width": reference.derivative_width,
            "height": reference.derivative_height,
            "committed_ref": CommittedBlobRef(
                committed_key=reference.derivative_object_key,
                storage_version_id=reference.derivative_storage_version_id,
                size_bytes=reference.derivative_byte_size,
                checksum_sha256_b64=base64.b64encode(bytes.fromhex(reference.derivative_sha256)).decode("ascii")
                if reference.derivative_sha256 else "",
                content_type_verified=reference.derivative_mime or "image/jpeg",
            ),
        }
