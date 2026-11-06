"""Read projections for Generation Drafts, Previews, Runs, RunItems and Assets
(Issue #27 Task 3).

Assembles server-side detail projections so the browser does not join raw
persistence records or reimplement action eligibility (spec §2). Every
function here is workspace-scoped through ``ctx.workspace_id`` -- an id that
does not resolve inside the caller's Workspace raises
:class:`GenerationResourceNotFoundError`, never falls through to another
tenant's row.

This module deliberately returns plain ``dict`` projections, not ORM
records (except the handful of ``get_*``/``list_drafts`` helpers other
Generation v1 modules need the live record for, e.g. ``run_commands``'s CAS
updates) -- ``app.generation.response_models`` validates those dicts against
the BFF-facing shape.
"""
from __future__ import annotations

import re

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.persistence.models import (
    AssetRecord,
    AssetVersionRecord,
    GenerationRunRecord,
    QualityReviewRecord,
    ResolutionPreviewRecord,
)
from app.entitlements.cost_models import ProviderCallAttemptRecord, SpendReservationRecord
from app.generation.models import (
    AssetQCAttemptRecord,
    AssetQCCheckResultRecord,
    GenerationDraftRecord,
    GenerationRunCommandRecord,
    ProviderAttemptProjectionRecord,
    RunItemAssetVersionBindingRecord,
    RunItemRecord,
)
from app.platform.action_eligibility import ActionEligibility, compute_actions
from app.platform.tenancy import TenantContext


class GenerationResourceNotFoundError(LookupError):
    """A Draft/Preview/Run/RunItem/AssetVersion id does not resolve in this Workspace."""

    def __init__(self, resource: str, resource_id: str, *, code: str):
        self.resource = resource
        self.resource_id = resource_id
        self.code = code
        super().__init__(f"{resource} {resource_id!r} not found")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _as_aware(value: datetime) -> datetime:
    """SQLite drops tzinfo on ``DateTime(timezone=True)`` round-trips; match
    the same re-attachment ``app.generation.preview`` uses."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


# --------------------------------------------------------------------------
# Drafts -- draft.py has create/update/archive/get but no list; that read
# lives here rather than growing draft.py's CAS-focused surface.
# --------------------------------------------------------------------------


def list_drafts(
    session: Session, ctx: TenantContext, *,
    product_id: str | None = None,
    state: str | None = None,
) -> list[dict]:
    stmt = select(GenerationDraftRecord).where(GenerationDraftRecord.workspace_id == ctx.workspace_id)
    if product_id:
        stmt = stmt.where(GenerationDraftRecord.product_id == product_id)
    if state:
        stmt = stmt.where(GenerationDraftRecord.state == state)
    stmt = stmt.order_by(GenerationDraftRecord.updated_at.desc(), GenerationDraftRecord.id.desc())
    rows = session.scalars(stmt).all()
    return [_draft_summary(d) for d in rows]


def _draft_summary(d: GenerationDraftRecord) -> dict:
    return {
        "id": d.id,
        "product_id": d.product_id,
        "provider": d.provider,
        "model": d.model,
        "execution_mode": d.execution_mode,
        "state": d.state,
        "revision": d.revision,
        "updated_at": _iso(d.updated_at),
    }


_DRAFT_ACTIONS = ["update", "archive", "preview"]


def draft_actions(d: GenerationDraftRecord) -> list[ActionEligibility]:
    editable = d.state == "editing"
    checks: dict[str, tuple[bool, str | None]] = {
        "update": (editable, None if editable else "draft_not_editable"),
        "archive": (d.state != "archived", None if d.state != "archived" else "already_archived"),
        "preview": (editable, None if editable else "draft_not_editable"),
    }
    return compute_actions(_DRAFT_ACTIONS, checks)


# --------------------------------------------------------------------------
# Previews -- app.generation.preview.check_preview_valid needs an
# ``expected_hash`` to compare against (the submission-time gate); a plain
# ``GET`` has no hash to compare, so this is a separate by-id read.
# --------------------------------------------------------------------------


def get_preview(session: Session, ctx: TenantContext, preview_id: str) -> ResolutionPreviewRecord:
    preview = session.get(ResolutionPreviewRecord, preview_id)
    if preview is None or preview.workspace_id != ctx.workspace_id:
        raise GenerationResourceNotFoundError("preview", preview_id, code="preview_not_found")
    return preview


_PREVIEW_ACTIONS = ["submit"]


def preview_actions(p: ResolutionPreviewRecord) -> list[ActionEligibility]:
    from app.adapters.persistence.models import utcnow

    valid = (
        p.invalidated_at is None
        and p.submitted_run_id is None
        and _as_aware(p.valid_until) >= utcnow()
    )
    checks: dict[str, tuple[bool, str | None]] = {
        "submit": (valid, None if valid else "preview_not_submittable"),
    }
    return compute_actions(_PREVIEW_ACTIONS, checks)


_RECOVERY_PREVIEW_ACTIONS = ["confirm"]


def recovery_preview_actions(p: ResolutionPreviewRecord) -> list[ActionEligibility]:
    from app.adapters.persistence.models import utcnow

    has_targets = bool(p.recovery_target_json.get("items"))
    valid = (
        p.invalidated_at is None
        and p.submitted_run_id is None
        and _as_aware(p.valid_until) >= utcnow()
        and has_targets
    )
    checks: dict[str, tuple[bool, str | None]] = {
        "confirm": (valid, None if valid else "preview_not_confirmable"),
    }
    return compute_actions(_RECOVERY_PREVIEW_ACTIONS, checks)


# --------------------------------------------------------------------------
# Runs / RunItems
# --------------------------------------------------------------------------


def get_run(session: Session, ctx: TenantContext, run_id: str) -> GenerationRunRecord:
    run = session.get(GenerationRunRecord, run_id)
    if run is None or run.workspace_id != ctx.workspace_id:
        raise GenerationResourceNotFoundError("generation_run", run_id, code="generation_run_not_found")
    return run


def list_runs(
    session: Session, ctx: TenantContext, *,
    product_id: str | None = None,
    status: str | None = None,
    execution_mode: str | None = None,
) -> list[dict]:
    stmt = select(GenerationRunRecord).where(GenerationRunRecord.workspace_id == ctx.workspace_id)
    if product_id:
        stmt = stmt.where(GenerationRunRecord.product_id == product_id)
    if status:
        stmt = stmt.where(GenerationRunRecord.status == status)
    if execution_mode:
        stmt = stmt.where(GenerationRunRecord.execution_mode == execution_mode)
    stmt = stmt.order_by(GenerationRunRecord.created_at.desc(), GenerationRunRecord.id.desc())
    rows = session.scalars(stmt).all()
    return [_run_summary(r) for r in rows]


def get_run_detail(session: Session, ctx: TenantContext, run_id: str) -> dict:
    run = get_run(session, ctx, run_id)
    commands = session.scalars(
        select(GenerationRunCommandRecord)
        .where(
            GenerationRunCommandRecord.workspace_id == ctx.workspace_id,
            GenerationRunCommandRecord.generation_run_id == run_id,
        )
        .order_by(GenerationRunCommandRecord.created_at)
    ).all()
    return {
        **_run_summary(run),
        "revision": run.revision,
        "recipe_snapshot_id": run.recipe_snapshot_id,
        "commands": [
            {
                "id": c.id,
                "command_kind": c.command_kind,
                "operation_id": c.operation_id,
                "created_at": _iso(c.created_at),
            }
            for c in commands
        ],
        "allowed_actions": run_actions(session, ctx, run),
    }


def list_run_items(
    session: Session, ctx: TenantContext, run_id: str, *,
    state: str | None = None,
) -> list[dict]:
    get_run(session, ctx, run_id)  # 404s before listing if the Run itself is not in this Workspace
    stmt = select(RunItemRecord).where(
        RunItemRecord.workspace_id == ctx.workspace_id,
        RunItemRecord.generation_run_id == run_id,
    )
    if state:
        stmt = stmt.where(RunItemRecord.state == state)
    stmt = stmt.order_by(RunItemRecord.ordinal.asc(), RunItemRecord.id.asc())
    rows = session.scalars(stmt).all()
    bound = _bound_asset_version_ids(session, ctx, [item.id for item in rows])
    return [
        _run_item_summary(item, bound_asset_version_id=bound.get(item.id))
        for item in rows
    ]


#: ``error_summary`` is ``"<code>: <diagnostic>"`` (or a bare code); the code
#: is what the run item card renders (#253).
_SUMMARY_CODE = re.compile(r"([a-z][a-z0-9_]*)(?::|$)")


def error_code_of(summary: str | None) -> str | None:
    match = _SUMMARY_CODE.match(summary or "")
    return match.group(1) if match else None


def get_run_item_detail(session: Session, ctx: TenantContext, run_id: str, run_item_id: str) -> dict:
    get_run(session, ctx, run_id)
    item = session.get(RunItemRecord, run_item_id)
    if item is None or item.workspace_id != ctx.workspace_id or item.generation_run_id != run_id:
        raise GenerationResourceNotFoundError("run_item", run_item_id, code="run_item_not_found")
    bound_asset_version_id = _bound_asset_version_id(session, ctx, run_item_id)
    return {
        **_run_item_summary(item, bound_asset_version_id=bound_asset_version_id),
        "combination_identity": item.combination_identity_json,
        "execution_mode": item.execution_mode,
        "error_summary": item.error_summary,
        "error_code": error_code_of(item.error_summary),
        "source_asset_version_id": item.source_asset_version_id,
        "source_run_item_id": item.source_run_item_id,
        "intent_kind": item.intent_kind,
        **_attempt_and_cost(session, ctx, run_item_id),
        "qc_result": _latest_qc_result(session, ctx, run_item_id),
        "bound_asset_version_id": bound_asset_version_id,
    }


def _attempt_and_cost(
    session: Session, ctx: TenantContext, run_item_id: str,
) -> dict:
    """Shared lookup for latest_attempt and cost_evidence to avoid duplicate queries."""
    attempt = session.execute(
        select(ProviderCallAttemptRecord)
        .where(
            ProviderCallAttemptRecord.workspace_id == ctx.workspace_id,
            ProviderCallAttemptRecord.run_item_id == run_item_id,
            ProviderCallAttemptRecord.step_kind == "generation",
        )
        .order_by(ProviderCallAttemptRecord.prepared_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if attempt is None:
        return {"latest_attempt": None, "cost_evidence": None}

    proj = session.execute(
        select(ProviderAttemptProjectionRecord).where(
            ProviderAttemptProjectionRecord.workspace_id == ctx.workspace_id,
            ProviderAttemptProjectionRecord.provider_call_attempt_id == attempt.id,
        )
    ).scalar_one_or_none()

    dispatch_certainty = "uncertain"
    outcome = "pending"
    if proj is not None:
        dc = proj.dispatch_certainty
        dispatch_certainty = "confirmed" if dc == "dispatched" else dc
        outcome = proj.outcome

    reservation = session.execute(
        select(SpendReservationRecord).where(
            SpendReservationRecord.workspace_id == ctx.workspace_id,
            SpendReservationRecord.id == attempt.spend_reservation_id,
        )
    ).scalar_one_or_none()

    attempt_cost = None
    if reservation is not None:
        attempt_cost = {
            "amount": str(reservation.maximum_provider_amount),
            "currency": reservation.provider_currency,
        }

    latest_attempt = {
        "dispatch_certainty": dispatch_certainty,
        "outcome": outcome,
        "provider": attempt.provider,
        "model": attempt.model,
        "version": str(attempt.attempt_no),
        "scenario": attempt.execution_mode if attempt.execution_mode != "byok" else None,
        "cost": attempt_cost,
    }

    cost_evidence = None
    if reservation is not None:
        cost_state_map = {
            "partially_settled": "settled",
            "settled": "settled",
            "uncertain": "reserved",
        }
        cost_evidence = {
            "state": cost_state_map.get(reservation.state, reservation.state),
            "amount": str(reservation.maximum_provider_amount),
            "currency": reservation.provider_currency,
        }

    return {"latest_attempt": latest_attempt, "cost_evidence": cost_evidence}


def _latest_qc_result(session: Session, ctx: TenantContext, run_item_id: str) -> dict | None:
    qc_attempt = session.execute(
        select(AssetQCAttemptRecord)
        .where(
            AssetQCAttemptRecord.workspace_id == ctx.workspace_id,
            AssetQCAttemptRecord.run_item_id == run_item_id,
        )
        .order_by(AssetQCAttemptRecord.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if qc_attempt is None:
        return None

    qc_state_map = {
        "failed_blocking": "failed",
        "failed_nonblocking": "failed",
        "running": "pending",
    }
    state = qc_state_map.get(qc_attempt.state, qc_attempt.state)

    check_rows = session.execute(
        select(AssetQCCheckResultRecord)
        .where(
            AssetQCCheckResultRecord.workspace_id == ctx.workspace_id,
            AssetQCCheckResultRecord.qc_attempt_id == qc_attempt.id,
        )
        .order_by(AssetQCCheckResultRecord.created_at)
    ).scalars().all()

    checks = [
        {
            "name": c.rule_id,
            "passed": c.outcome == "pass",
            "detail": c.feedback_detail or None,
            "verdict": (c.evidence_json or {}).get("verdict") or {
                "pass": "pass", "fail": "fail", "skip": "skip", "error": "uncertain",
            }.get(c.outcome, c.outcome),
        }
        for c in check_rows
    ]
    return {"state": state, "checks": checks}


def _bound_asset_version_id(session: Session, ctx: TenantContext, run_item_id: str) -> str | None:
    binding = session.execute(
        select(RunItemAssetVersionBindingRecord.asset_version_id)
        .where(
            RunItemAssetVersionBindingRecord.workspace_id == ctx.workspace_id,
            RunItemAssetVersionBindingRecord.run_item_id == run_item_id,
        )
        .order_by(RunItemAssetVersionBindingRecord.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return binding


def _bound_asset_version_ids(
    session: Session, ctx: TenantContext, run_item_ids: list[str],
) -> dict[str, str]:
    """Batch form of :func:`_bound_asset_version_id` for one Run's item list:
    the newest binding per RunItem in a single query rather than one query
    per tile (a 24-item Run page polls this every 1-2 s while running)."""
    if not run_item_ids:
        return {}
    rows = session.execute(
        select(
            RunItemAssetVersionBindingRecord.run_item_id,
            RunItemAssetVersionBindingRecord.asset_version_id,
        )
        .where(
            RunItemAssetVersionBindingRecord.workspace_id == ctx.workspace_id,
            RunItemAssetVersionBindingRecord.run_item_id.in_(run_item_ids),
        )
        .order_by(
            RunItemAssetVersionBindingRecord.created_at.desc(),
            RunItemAssetVersionBindingRecord.id.desc(),
        )
    ).all()
    bound: dict[str, str] = {}
    for run_item_id, asset_version_id in rows:
        bound.setdefault(run_item_id, asset_version_id)
    return bound


def _run_summary(run: GenerationRunRecord) -> dict:
    return {
        "id": run.id,
        "product_id": run.product_id,
        "provider": run.provider,
        "model": run.model,
        "status": run.status,
        "total": run.total,
        "done": run.done,
        "failed": run.failed,
        "execution_mode": run.execution_mode,
        "created_at": _iso(run.created_at),
    }


def _combination_value_tags(item: RunItemRecord) -> dict:
    """The item's combination values as ``{dimension: value}`` display tags.

    Atomic Submission never fills ``RunItemRecord.tags_json`` (it stays the
    column default ``{}``); the values a tile should label itself with live
    in the frozen ``combination_identity_json["dimensions"]`` as
    ``{"<template entry id>:<dimension id>": "<value id>"}`` for
    template-driven Runs and ``{"<dimension>": "<value>"}`` for scalar
    selectors. The template entry id is identity, not copy
    (``app.generation.prompting._short_dimension`` makes the same cut), so
    keys are shortened to the dimension id unless two templates would
    collide on it, in which case every key keeps its full form.
    """
    if item.tags_json:
        return dict(item.tags_json)
    identity = item.combination_identity_json or {}
    dimensions = identity.get("dimensions") if isinstance(identity, dict) else None
    if not isinstance(dimensions, dict) or not dimensions:
        return {}
    shortened = {
        key: (key.rsplit(":", 1)[-1] if ":" in key else key) for key in dimensions
    }
    if len(set(shortened.values())) != len(shortened):
        shortened = {key: key for key in dimensions}
    return {shortened[key]: str(value) for key, value in dimensions.items()}


def _run_item_summary(
    item: RunItemRecord, *, bound_asset_version_id: str | None = None,
) -> dict:
    return {
        "id": item.id,
        "ordinal": item.ordinal,
        "state": item.state,
        "combination_fingerprint": item.combination_fingerprint,
        "variant_index": item.variant_index,
        "tags": _combination_value_tags(item),
        # The produced/reused AssetVersion, so a tile can key its image on a
        # stable identity, plus the slot for that version's signed thumbnail
        # link (Issue #159) -- filled by the route via
        # ``app.generation.thumbnails.attach_thumbnail_links``, because
        # signing needs the process's BlobStore; ``None`` until the item has
        # produced a version with a derivative.
        "bound_asset_version_id": bound_asset_version_id,
        "thumbnail": None,
        "allowed_actions": item_actions(item),
    }


_RUN_ACTIONS = ["cancel", "retry_failed", "resume_paused", "reconcile"]
_ITEM_ACTIONS = ["review", "regenerate", "download"]


def run_actions(
    session: Session, ctx: TenantContext, run: GenerationRunRecord,
) -> list[ActionEligibility]:
    is_active = run.status in ("pending", "running", "qc_pending")

    has_failed = run.failed > 0

    has_resumable_paused = session.query(RunItemRecord).filter(
        RunItemRecord.workspace_id == ctx.workspace_id,
        RunItemRecord.generation_run_id == run.id,
        RunItemRecord.state == "paused",
        RunItemRecord.same_snapshot_resumable == True,  # noqa: E712
    ).first() is not None

    has_uncertain = session.query(RunItemRecord).filter(
        RunItemRecord.workspace_id == ctx.workspace_id,
        RunItemRecord.generation_run_id == run.id,
        RunItemRecord.state == "uncertain",
    ).first() is not None

    checks: dict[str, tuple[bool, str | None]] = {
        "cancel": (is_active, None if is_active else "run_not_active"),
        "retry_failed": (has_failed, None if has_failed else "no_failed_items"),
        "resume_paused": (
            has_resumable_paused,
            None if has_resumable_paused else "no_resumable_paused_items",
        ),
        "reconcile": (
            has_uncertain,
            None if has_uncertain else "no_uncertain_items",
        ),
    }
    return compute_actions(_RUN_ACTIONS, checks)


def item_actions(item: RunItemRecord) -> list[ActionEligibility]:
    succeeded = item.state in ("succeeded", "reused")
    checks: dict[str, tuple[bool, str | None]] = {
        "review": (succeeded, None if succeeded else "item_not_succeeded"),
        "regenerate": (succeeded, None if succeeded else "item_not_succeeded"),
        "download": (succeeded, None if succeeded else "item_not_succeeded"),
    }
    return compute_actions(_ITEM_ACTIONS, checks)


# --------------------------------------------------------------------------
# Assets / AssetVersions
# --------------------------------------------------------------------------


def list_assets(
    session: Session, ctx: TenantContext, *,
    product_id: str | None = None,
    run_id: str | None = None,
    provider: str | None = None,
    execution_mode: str | None = None,
    origin_kind: str | None = None,
) -> list[dict]:
    stmt = (
        select(AssetVersionRecord, AssetRecord)
        .join(AssetRecord, AssetVersionRecord.asset_id == AssetRecord.id)
        .where(AssetVersionRecord.workspace_id == ctx.workspace_id)
    )
    if product_id:
        stmt = stmt.where(AssetRecord.product_id == product_id)
    if run_id:
        stmt = stmt.where(AssetRecord.run_id == run_id)
    if provider:
        stmt = stmt.where(AssetRecord.provider == provider)
    if execution_mode:
        stmt = stmt.where(AssetVersionRecord.execution_mode == execution_mode)
    if origin_kind:
        stmt = stmt.where(AssetVersionRecord.origin_kind == origin_kind)
    stmt = stmt.order_by(AssetVersionRecord.created_at.desc(), AssetVersionRecord.id.desc())

    rows = session.execute(stmt).all()
    result = []
    for version, asset in rows:
        review_value = session.execute(
            select(QualityReviewRecord.review_value)
            .where(
                QualityReviewRecord.asset_version_id == version.id,
                QualityReviewRecord.workspace_id == ctx.workspace_id,
            )
            .order_by(QualityReviewRecord.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        summary = _asset_summary(version, asset, review_value)
        if version.producing_run_item_id:
            item = session.scalar(select(RunItemRecord).where(RunItemRecord.workspace_id == ctx.workspace_id,
                RunItemRecord.id == version.producing_run_item_id))
            run = session.scalar(select(GenerationRunRecord).where(GenerationRunRecord.workspace_id == ctx.workspace_id,
                GenerationRunRecord.id == item.generation_run_id)) if item else None
            if run:
                summary.update(run_id=run.id, provider=run.provider, model=run.model)
        result.append(summary)
    return result


def get_asset_version_detail(session: Session, ctx: TenantContext, version_id: str) -> dict:
    av = session.get(AssetVersionRecord, version_id)
    if av is None or av.workspace_id != ctx.workspace_id:
        raise GenerationResourceNotFoundError("asset_version", version_id, code="asset_version_not_found")

    reviews = session.scalars(
        select(QualityReviewRecord)
        .where(
            QualityReviewRecord.asset_version_id == version_id,
            QualityReviewRecord.workspace_id == ctx.workspace_id,
        )
        .order_by(QualityReviewRecord.created_at.desc())
    ).all()

    return {
        "id": av.id,
        "asset_id": av.asset_id,
        "kind": av.kind,
        "aspect_ratio": av.aspect_ratio,
        "mime_type": av.mime_type,
        "byte_size": av.byte_size,
        "origin_kind": av.origin_kind,
        "execution_mode": av.execution_mode,
        "content_sha256": av.content_sha256,
        "producing_run_item_id": av.producing_run_item_id,
        "provider_attempt_id": av.provider_attempt_id,
        "requested_seed": av.requested_seed,
        "effective_seed": av.effective_seed,
        "seed_unavailable_reason": av.seed_unavailable_reason,
        "reviews": [_review_dict(r) for r in reviews],
        "allowed_actions": asset_version_actions(av),
    }


def _asset_summary(version: AssetVersionRecord, asset: AssetRecord, review_value: str | None) -> dict:
    return {
        "id": version.id,
        "asset_id": version.asset_id,
        "product_id": asset.product_id,
        "run_id": asset.run_id or "",
        "provider": asset.provider,
        "model": asset.model,
        "status": asset.status,
        "review_value": review_value,
        "origin_kind": version.origin_kind,
        "execution_mode": version.execution_mode,
        "created_at": _iso(version.created_at),
    }


def _review_dict(r: QualityReviewRecord) -> dict:
    return {
        "id": r.id,
        "asset_version_id": r.asset_version_id,
        "review_value": r.review_value,
        "reason": r.reason,
        "actor_id": r.actor_id,
        "created_at": _iso(r.created_at),
    }


_ASSET_VERSION_ACTIONS = ["review", "regenerate", "download"]


def asset_version_actions(av: AssetVersionRecord) -> list[ActionEligibility]:
    has_blob = bool(av.object_key)
    # An external creative (Issue #168) has no generation recipe to replay.
    regenerable = av.origin_kind != "upload"
    checks: dict[str, tuple[bool, str | None]] = {
        "review": (True, None),
        "regenerate": (regenerable, None if regenerable else "external_creative_not_regenerable"),
        "download": (has_blob, None if has_blob else "asset_blob_unavailable"),
    }
    return compute_actions(_ASSET_VERSION_ACTIONS, checks)
