"""ResolutionPreview creation + submission-time validity check (Issue #21 Task 2).

Freezes a Draft plus its resolved execution-mode policy
(``app.generation.execution_policy.resolve_preview``) into an immutable
``ResolutionPreviewRecord`` the operator confirms before Atomic Submission.

The ``recipe_input_hash`` is deliberately computed over only the *recipe*
portion of the snapshot -- provider, model, execution mode, combination
strategy, format, exclusions, and the resolved adapter/cost/scenario
evidence. ``product_id`` and ``selectors`` are product-specific, not part of
the reusable recipe, so two Drafts that only differ by which Product they
target still resolve to the same recipe identity (this is what lets
``insert_or_reuse_snapshot`` in ``app.generation.snapshot`` share one
``GenerationRecipeSnapshotRecord`` across runs against different Products).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.persistence.models import ResolutionPreviewRecord, new_id, utcnow
from app.generation.draft import get_draft
from app.generation.execution_policy import PreviewRejection, resolve_preview
from app.platform.tenancy import TenantContext

#: How long a confirmed-but-unsubmitted preview stays eligible for submission.
PREVIEW_TTL = timedelta(minutes=30)

#: Bumped whenever the shape of the hashed "recipe" payload changes, so old
#: previews never silently compare equal to a differently-shaped new one.
CANONICALIZATION_VERSION = "v1"


from .preview import PreviewError


def create_preview(
    session: Session,
    ctx: TenantContext,
    draft_id: str,
    draft_revision: int,
    *,
    adapter_registry: dict | None = None,
    credential_evidence: dict | None = None,
) -> ResolutionPreviewRecord:
    """Resolve ``draft_id`` at ``draft_revision`` and freeze it into a Preview.

    Raises :class:`PreviewError` with code ``stale_draft_revision`` if the
    Draft has moved on, or with the rejection code returned by
    :func:`app.generation.execution_policy.resolve_preview` if the Draft's
    execution mode cannot currently be admitted (e.g. missing BYOK
    credential, unavailable sandbox adapter).
    """
    draft = get_draft(session, ctx, draft_id)
    if draft.revision != draft_revision:
        raise PreviewError(
            "stale_draft_revision",
            f"Expected revision {draft_revision}, draft is at {draft.revision}",
        )

    from .capabilities import PLATFORM_ADAPTER_REGISTRY
    registry = adapter_registry if adapter_registry is not None else PLATFORM_ADAPTER_REGISTRY

    resolution = resolve_preview(
        session,
        draft_mode=draft.execution_mode,
        provider=draft.provider,
        model=draft.model,
        adapter_registry=registry,
        credential_evidence=credential_evidence,
    )
    if isinstance(resolution, PreviewRejection):
        if draft.execution_mode != "byok" or credential_evidence is not None:
            raise PreviewError(resolution.code, resolution.detail)

    selection = None
    if draft.execution_mode == "byok" or "prompt" in (draft.selectors_json or {}):
        from .selection import resolve_selection, SelectionError
        try:
            selection = resolve_selection(session, ctx, draft)
        except SelectionError as exc:
            raise PreviewError(exc.code, exc.detail) from exc
        resolution = resolve_preview(
            session, draft_mode=draft.execution_mode, provider=draft.provider,
            model=draft.model, adapter_registry=registry,
            credential_evidence=selection["credential_evidence"],
        )
        if isinstance(resolution, PreviewRejection):
            raise PreviewError(resolution.code, resolution.detail)

    from .preview import _truth_dependency
    recipe_core = _recipe_core(draft, resolution)
    recipe_core["truth_dependency"] = _truth_dependency(draft.selectors_json or {})
    if selection:
        recipe_core["selection"] = selection
        recipe_core["format"] = selection["format"]
        recipe_core["selected_combinations"] = [
            {"fingerprint": f"variation_{i}", "variant_index": i}
            for i in range(selection["format"]["variants"])
        ]
    snapshot_content = {
        "product_id": draft.product_id,
        "selectors": draft.selectors_json,
        "recipe": recipe_core,
    }
    recipe_input_hash = _canonical_hash(
        {"recipe": recipe_core, "canonicalization_version": CANONICALIZATION_VERSION}
    )
    preview_hash = _canonical_hash(
        {
            "draft_id": draft_id,
            "draft_revision": draft_revision,
            "recipe_input_hash": recipe_input_hash,
            "mode": resolution.effective_mode,
            "adapter": resolution.adapter_key,
            "capabilities": resolution.capabilities,
            "cost_policy": resolution.cost_policy,
            "scenario": resolution.scenario_evidence,
        }
    )

    now = utcnow()
    preview = ResolutionPreviewRecord(
        id=new_id("rpv"),
        workspace_id=ctx.workspace_id,
        generation_draft_id=draft_id,
        draft_revision=draft_revision,
        snapshot_json=snapshot_content,
        preview_hash=preview_hash,
        recipe_input_hash=recipe_input_hash,
        execution_mode=resolution.effective_mode,
        valid_until=now + PREVIEW_TTL,
        created_by=ctx.initiator_id or "unknown",
        created_at=now,
        purpose="initial_generation",
    )
    session.add(preview)
    session.flush()
    return preview


def check_preview_valid(
    session: Session,
    ctx: TenantContext,
    preview_id: str,
    expected_hash: str,
) -> ResolutionPreviewRecord:
    """Submission-time gate: exists, hash matches, not invalidated/expired/consumed."""
    preview = session.execute(
        select(ResolutionPreviewRecord).where(
            ResolutionPreviewRecord.id == preview_id,
            ResolutionPreviewRecord.workspace_id == ctx.workspace_id,
        )
    ).scalar_one_or_none()
    if preview is None:
        raise PreviewError("preview_not_found", "Preview does not exist")
    if preview.preview_hash != expected_hash:
        raise PreviewError("preview_hash_mismatch", "Preview hash does not match")
    if preview.invalidated_at is not None:
        raise PreviewError("preview_invalidated", preview.invalidation_reason)
    if _as_aware(preview.valid_until) < utcnow():
        raise PreviewError("preview_expired", "Preview has expired")
    if preview.submitted_run_id is not None:
        raise PreviewError("preview_already_submitted", "Preview already consumed")
    return preview


def _recipe_core(draft, resolution) -> dict:
    return {
        "provider": draft.provider,
        "model": draft.model,
        "execution_mode": resolution.effective_mode,
        "combination_strategy": draft.combination_strategy_json,
        "format": draft.format_json,
        "exclusions": draft.exclusions_json,
        "adapter_key": resolution.adapter_key,
        "adapter_version": resolution.adapter_version,
        "capabilities": resolution.capabilities,
        "cost_policy": resolution.cost_policy,
        "scenario_evidence": resolution.scenario_evidence,
    }


def _as_aware(value: datetime) -> datetime:
    """SQLite drops tzinfo on ``DateTime(timezone=True)`` round-trips; every
    other platform module comparing a stored timestamp against ``utcnow()``
    re-attaches UTC the same way (see e.g. ``app.platform.jobs``,
    ``app.launch.service``)."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _canonical_hash(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _default_adapter_registry() -> dict:
    return {
        "mock": {
            "version": "v1",
            "capabilities": {"image_generation": True},
        },
    }
