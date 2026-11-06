"""Exact-version human review for generated Asset Versions (#21 Task 4,
#49 Task 5).

A review is scoped to one immutable ``AssetVersionRecord``, never to the
mutable Asset it belongs to -- ``QualityReviewRecord.asset_version_id`` is
the exact-version anchor Task 1 added. ``product_id``/``asset_id`` are still
populated (they carry ``NOT NULL`` foreign keys into ``products``/``assets``)
by resolving them from the AssetVersion, so this never falls back to the
legacy unscoped path (``legacy_unscoped=False``).

``create_review`` enforces the blocking-QC gate: an ``approved`` review
cannot be recorded while the AssetVersion carries a QC attempt in
``failed_blocking`` state. ``is_ready_for_experiment`` is the read-side of
the same rule -- true only when the *effective* review (spec §0.3 --
``project_effective_review``) is ``approved`` *and* no blocking QC failure
is outstanding (a later QC re-run can still invalidate an already-approved
version).

Head guard (spec §0.3, Issue #49 Task 5): once an AssetVersion has an
effective review head (any review row not itself superseded), a further
``create_review`` call must set ``supersedes_review_id`` to correct it --
submitting a bare/uncorrelated review while a head already exists raises
``ReviewConflictError("effective_review_exists", ...)`` (409). This applies
regardless of the head's own value, including a ``cleared`` head: the
correction chain always continues from the actual last row, not from the
last *visible* value.
"""
from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.adapters.persistence.models import (
    AssetRecord,
    AssetVersionRecord,
    QualityReviewRecord,
    new_id,
)
from app.generation.models import AssetQCAttemptRecord
from app.generation.onboarding_facts import emit_onboarding_fact
from app.platform.denial_audit import check_and_record_denial
from app.platform.tenancy import TenantContext


class ReviewError(ValueError):
    def __init__(self, code: str, detail: str, *, params: dict | None = None):
        self.code = code
        self.detail = detail
        self.params = params or {}
        super().__init__(f"{code}: {detail}")


class ReviewConflictError(Exception):
    """Raised for 409 conflict scenarios in the review correction chain.

    Distinct from ``ReviewError`` (which covers 404/422 validation of the
    reviewed AssetVersion itself): a ``ReviewConflictError`` signals that the
    requested correction conflicts with an existing correction chain --
    double supersession of the same prior review, or an attempt to correct a
    review that belongs to a different AssetVersion.
    """

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _has_blocking_qc_failure(session: Session, ctx: TenantContext, asset_version_id: str) -> bool:
    blocking = session.execute(
        select(AssetQCAttemptRecord.id).where(
            AssetQCAttemptRecord.asset_version_id == asset_version_id,
            AssetQCAttemptRecord.workspace_id == ctx.workspace_id,
            AssetQCAttemptRecord.state == "failed_blocking",
        ).limit(1)
    ).scalar_one_or_none()
    return blocking is not None


def _resolve_asset_and_product(
    session: Session, ctx: TenantContext, asset_version_id: str
) -> tuple[str, str]:
    version = session.execute(
        select(AssetVersionRecord).where(
            AssetVersionRecord.id == asset_version_id,
            AssetVersionRecord.workspace_id == ctx.workspace_id,
        ).with_for_update()
    ).scalar_one_or_none()
    if version is None:
        check_and_record_denial(
            session,
            ctx,
            AssetVersionRecord,
            asset_version_id,
            resource_kind="asset_version",
            detail="create_review",
        )
        raise ReviewError(
            "asset_version_not_found", f"AssetVersion {asset_version_id} not found"
        )

    asset = session.execute(
        select(AssetRecord).where(
            AssetRecord.id == version.asset_id,
            AssetRecord.workspace_id == ctx.workspace_id,
        )
    ).scalar_one_or_none()
    if asset is None:
        raise ReviewError("asset_not_found", f"Asset {version.asset_id} not found")

    return asset.product_id, asset.id


def find_review_head(
    session: Session, ctx: TenantContext, asset_version_id: str
) -> QualityReviewRecord | None:
    """The current review head for ``asset_version_id``: the newest review
    (``created_at DESC, id DESC``) that is not itself superseded by another
    review row. Returns ``None`` when the AssetVersion has no reviews yet.

    This is a row-presence rule, distinct from :func:`project_effective_review`'s
    *value* rule -- a ``cleared`` review is still a head (the correction
    chain must continue from it via ``supersedes_review_id``), even though
    its effective *value* projects to ``None``.
    """
    superseded_ids = (
        select(QualityReviewRecord.supersedes_review_id)
        .where(
            QualityReviewRecord.asset_version_id == asset_version_id,
            QualityReviewRecord.workspace_id == ctx.workspace_id,
            QualityReviewRecord.legacy_unscoped.is_(False),
            QualityReviewRecord.supersedes_review_id.isnot(None),
        )
        .scalar_subquery()
    )
    return session.execute(
        select(QualityReviewRecord)
        .where(
            QualityReviewRecord.asset_version_id == asset_version_id,
            QualityReviewRecord.workspace_id == ctx.workspace_id,
            QualityReviewRecord.legacy_unscoped.is_(False),
            ~QualityReviewRecord.id.in_(superseded_ids),
        )
        .order_by(desc(QualityReviewRecord.created_at), desc(QualityReviewRecord.id))
        .limit(1)
    ).scalar_one_or_none()


def project_effective_review(
    session: Session, ctx: TenantContext, asset_version_id: str
) -> str | None:
    """The effective review value for ``asset_version_id`` (spec §0.3).

    The effective review is the head's ``review_value`` -- unless the head
    is ``cleared``, which is a tombstone: a cleared head means "no effective
    review", not "reviewed as cleared".
    """
    head = find_review_head(session, ctx, asset_version_id)
    if head is None or head.review_value == "cleared":
        return None
    return head.review_value


def _validate_supersedes_target(
    session: Session,
    ctx: TenantContext,
    asset_version_id: str,
    supersedes_review_id: str,
) -> None:
    target = session.execute(
        select(QualityReviewRecord).where(
            QualityReviewRecord.id == supersedes_review_id,
            QualityReviewRecord.workspace_id == ctx.workspace_id,
        )
    ).scalar_one_or_none()
    if target is None:
        raise ReviewConflictError(
            "supersedes_review_not_found",
            f"QualityReview {supersedes_review_id} not found",
        )
    if target.asset_version_id != asset_version_id:
        raise ReviewConflictError(
            "cross_version_correction",
            f"Review {supersedes_review_id} belongs to AssetVersion "
            f"{target.asset_version_id}, not {asset_version_id}; "
            "cross-version correction is not allowed",
        )
    already_superseded_by = session.execute(
        select(QualityReviewRecord.id).where(
            QualityReviewRecord.supersedes_review_id == supersedes_review_id,
            QualityReviewRecord.workspace_id == ctx.workspace_id,
        )
    ).scalar_one_or_none()
    if already_superseded_by is not None:
        raise ReviewConflictError(
            "review_already_superseded",
            f"Review {supersedes_review_id} has already been superseded by "
            f"{already_superseded_by}",
        )


def create_review(
    session: Session,
    ctx: TenantContext,
    asset_version_id: str,
    review_value: str,
    reason: str,
    actor_id: str,
    supersedes_review_id: str | None = None,
) -> QualityReviewRecord:
    if review_value == "cleared" and not supersedes_review_id:
        raise ReviewError(
            "cleared_requires_supersedes_review_id",
            "A 'cleared' review must set supersedes_review_id to the review it corrects",
        )

    if review_value == "approved" and _has_blocking_qc_failure(session, ctx, asset_version_id):
        raise ReviewError(
            "blocking_qc_failure",
            "Cannot approve an AssetVersion with an unresolved blocking QC failure",
        )

    product_id, asset_id = _resolve_asset_and_product(session, ctx, asset_version_id)

    current_head = find_review_head(session, ctx, asset_version_id)
    if current_head is not None and not supersedes_review_id:
        raise ReviewConflictError(
            "effective_review_exists",
            f"AssetVersion {asset_version_id} already has an effective review "
            f"(head={current_head.id}); set supersedes_review_id to correct it",
        )

    if supersedes_review_id is not None:
        _validate_supersedes_target(session, ctx, asset_version_id, supersedes_review_id)

    review = QualityReviewRecord(
        id=new_id("qr"),
        workspace_id=ctx.workspace_id,
        product_id=product_id,
        asset_id=asset_id,
        asset_version_id=asset_version_id,
        review_value=review_value,
        reason=reason,
        source="human",
        actor_id=actor_id,
        legacy_unscoped=False,
        supersedes_review_id=supersedes_review_id,
    )
    session.add(review)
    session.flush()
    emit_onboarding_fact(
        session,
        ctx,
        fact_type="quality_review.human_recorded",
        source_resource_id=review.id,
    )
    return review


def is_ready_for_experiment(
    session: Session,
    ctx: TenantContext,
    asset_version_id: str,
) -> bool:
    """True iff the *effective* review (spec §0.3) is ``approved`` and no
    blocking QC failure is outstanding -- not merely "any approved review
    exists anywhere in the version's review history". A correction
    (``cleared``/``rejected``/``needs_changes`` superseding a prior
    ``approved``) revokes readiness even though the earlier ``approved`` row
    is still present (append-only history, spec §0.3/§6)."""
    if project_effective_review(session, ctx, asset_version_id) != "approved":
        return False

    return not _has_blocking_qc_failure(session, ctx, asset_version_id)
