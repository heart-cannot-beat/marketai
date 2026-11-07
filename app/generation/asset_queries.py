"""Asset query projections and blob resolution (Issue #49 Phase 0).

This module owns read-side projections for the Asset gallery:
``resolve_asset_blob_ref`` (Task 2), ``AssetGalleryPage``/``list_asset_versions``
(Task 3) and ``AssetVersionDetail``/``get_asset_version_detail`` (Task 4,
added on top of this file rather than replacing it -- it now supersedes
``run_queries.get_asset_version_detail`` as the implementation
``v1_router``'s ``GET /generation/asset-versions/{version_id}`` route
calls, though the older function stays in ``run_queries`` unchanged/unused
since removing it is outside this task's file list).

``resolve_asset_blob_ref`` is the single place that turns an ``AssetVersion``
id into the exact committed blob reference a signed download capability may
be issued against (spec §7.4/§0.1). It never falls back to the legacy
``object_key`` column -- an ``AssetVersion`` without both
``committed_object_key`` and ``committed_storage_version_id`` populated is
treated as unavailable rather than served from a reference that was never
verified through the committed-blob lifecycle.

``list_asset_versions`` (spec §0.2) replaces the generic ``Page[Any]``
asset listing (``app.generation.run_queries.list_assets`` -- still present,
now unused by any route) with a typed ``AssetGalleryPage``: a database
keyset cursor over ``(created_at DESC, id DESC)`` bound to Workspace plus the
canonical filter set (reusing ``app.platform.pagination.encode_cursor``/
``decode_keyset_cursor``, the same opaque-cursor primitive every other
Generation v1 list route uses), server-side filters, and Workspace-scoped
facets.

``effective_review_value`` now uses Task 5's supersession-aware head-review
rule (``app.generation.review.project_effective_review``, spec §0.3): the
newest review not itself superseded by another review, with a ``cleared``
head projecting to ``None`` (a tombstone, not a value). The gallery item
projection (``_version_summary``) batch-calls ``project_effective_review``
per page row; the ``review_value`` filter and the ``reviews`` facet
(``_gallery_join_and_filters``/``_compute_facets``) instead use
``_effective_review_head_subquery``, a correlated-SQL restatement of the
same head rule -- computing the head per-row in Python is not viable for a
filter/facet that must run over the full matching Workspace dataset, not
just the current page.

``latest_qc_state`` is still the latest ``AssetQCAttemptRecord.state``
(ordered by ``attempt_sequence DESC``).

``stable_dimension_signature`` and ``mock_evidence`` stay ``None`` and the
``tag`` filter is a no-op -- no column backs either of them yet.
``contextual_conclusion``/``conclusion`` (Task 6, spec §0.7) are wired
against ``app.generation.experiment_conclusions``: the ``conclusion``
filter still enforces its ``experiment_context_required`` precondition
(spec §0.2) and, given both, filters and populates the summary from the
authoritative ``contextual_verdict_projections`` projection for that exact
``experiment_context`` (treated as an opaque ``experiment_id`` -- see
``app.generation.experiment_conclusions``'s module docstring).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import and_, case, func, join, or_, select
from sqlalchemy.orm import Session

from app.adapters.persistence.models import (
    AssetRecord,
    GenerationRunRecord,
    AssetVersionRecord,
    ContextualVerdictProjectionRecord,
    QualityReviewRecord,
)
from app.generation.experiment_conclusions import (
    VERDICT_LABELS,
    project_contextual_conclusion_for_context,
    project_contextual_conclusions,
)
from app.generation.models import AssetQCAttemptRecord, AssetQCCheckResultRecord, RunItemRecord
from app.generation.review import project_effective_review
from app.generation.run_queries import (
    GenerationResourceNotFoundError,
    asset_version_actions,
)
from app.platform.action_eligibility import ActionEligibility, compute_actions
from app.platform.blob_store import CommittedBlobRef
from app.platform.denial_audit import check_and_record_denial
from app.platform.pagination import decode_keyset_cursor, encode_cursor
from app.platform.tenancy import TenantContext

__all__ = [
    "AssetBlobError",
    "GalleryQueryError",
    "get_asset_version_detail",
    "list_asset_versions",
    "resolve_asset_blob_ref",
]


class AssetBlobError(ValueError):
    """A committed blob reference could not be resolved for an AssetVersion.

    Always raised with ``code == "asset_blob_unavailable"`` (spec §0.1):
    missing, uncommitted, or cross-Workspace lookups all fail closed under
    this single stable code rather than leaking a 404-vs-422 distinction the
    browser would have to special-case.
    """

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def resolve_asset_blob_ref(
    session: Session, ctx: TenantContext, version_id: str
) -> CommittedBlobRef:
    """Resolve the exact ``CommittedBlobRef`` an AssetVersion was produced from.

    Workspace-scoped: a version that exists but belongs to another Workspace
    resolves identically to one that does not exist at all -- both raise
    ``AssetBlobError("asset_blob_unavailable", ...)`` rather than distinguishing
    "not found" from "not yours" to a caller outside the Workspace.
    """
    version = session.execute(
        select(AssetVersionRecord).where(
            AssetVersionRecord.id == version_id,
            AssetVersionRecord.workspace_id == ctx.workspace_id,
        )
    ).scalar_one_or_none()

    if version is None:
        check_and_record_denial(
            session,
            ctx,
            AssetVersionRecord,
            version_id,
            resource_kind="asset_version",
            detail="resolve_asset_blob_ref",
        )
        raise AssetBlobError(
            "asset_blob_unavailable",
            f"AssetVersion {version_id} not found in workspace {ctx.workspace_id}",
        )

    if not version.committed_object_key or not version.committed_storage_version_id:
        raise AssetBlobError(
            "asset_blob_unavailable",
            f"AssetVersion {version_id} has no committed blob reference",
        )

    return CommittedBlobRef(
        committed_key=version.committed_object_key,
        storage_version_id=version.committed_storage_version_id,
        size_bytes=version.byte_size or 0,
        checksum_sha256_b64=version.content_sha256 or "",
        content_type_verified=version.mime_type or "application/octet-stream",
    )


# --------------------------------------------------------------------------
# AssetGalleryPage (Task 3, spec §0.2)
# --------------------------------------------------------------------------


class GalleryQueryError(ValueError):
    """A gallery query parameter combination is invalid (spec §0.2).

    Always carries a stable ``code``. Currently the only producer is the
    ``conclusion`` filter's ``experiment_context_required`` precondition --
    an invalid/mismatched cursor raises ``InvalidCursorError`` instead (the
    same exception every other Generation v1 list route already surfaces as
    ``400 invalid_cursor``).
    """

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _review_head_value_subquery():
    """Correlated scalar subquery: the head review's raw ``review_value``
    for the ``AssetVersionRecord`` in the enclosing query -- the same
    supersession-aware head rule as
    ``app.generation.review.find_review_head``/``project_effective_review``,
    restated as correlated SQL so it can drive a WHERE filter and a GROUP BY
    facet over the full matching dataset (Python-per-row is only viable for
    the single-page item projection, see ``_version_summary``).

    Raw, not yet folded through the ``cleared`` tombstone rule -- callers
    wrap this in :func:`_effective_review_subquery` for that."""
    superseded_ids = (
        select(QualityReviewRecord.supersedes_review_id)
        .where(
            QualityReviewRecord.asset_version_id == AssetVersionRecord.id,
            QualityReviewRecord.workspace_id == AssetVersionRecord.workspace_id,
            QualityReviewRecord.supersedes_review_id.isnot(None),
        )
        .correlate(AssetVersionRecord)
        .scalar_subquery()
    )
    return (
        select(QualityReviewRecord.review_value)
        .where(
            QualityReviewRecord.asset_version_id == AssetVersionRecord.id,
            QualityReviewRecord.workspace_id == AssetVersionRecord.workspace_id,
            ~QualityReviewRecord.id.in_(superseded_ids),
        )
        .order_by(QualityReviewRecord.created_at.desc(), QualityReviewRecord.id.desc())
        .limit(1)
        .correlate(AssetVersionRecord)
        .scalar_subquery()
    )


def _effective_review_subquery():
    """The effective review value (spec §0.3): the head's ``review_value``,
    folded to ``NULL`` when the head is ``cleared`` -- a tombstone, not a
    value. SQL restatement of ``app.generation.review.project_effective_review``
    for use in a WHERE filter or GROUP BY facet."""
    head_value = _review_head_value_subquery()
    return case((head_value == "cleared", None), else_=head_value)


def _latest_qc_state_subquery():
    """Correlated scalar subquery: the latest QC attempt ``state`` for the
    ``AssetVersionRecord`` in the enclosing query (interim definition --
    see module docstring)."""
    return (
        select(AssetQCAttemptRecord.state)
        .where(
            AssetQCAttemptRecord.asset_version_id == AssetVersionRecord.id,
            AssetQCAttemptRecord.workspace_id == AssetVersionRecord.workspace_id,
        )
        .order_by(AssetQCAttemptRecord.attempt_sequence.desc())
        .limit(1)
        .correlate(AssetVersionRecord)
        .scalar_subquery()
    )


def _contextual_conclusion_subquery(experiment_context: str):
    """Correlated scalar subquery: the ``winner``/``learning``/``loser``
    verdict for the ``AssetVersionRecord`` in the enclosing query, anchored
    to one exact ``experiment_context`` (spec §0.7, Task 6).

    Reads only the authoritative ``contextual_verdict_projections``
    projection -- unlike ``app.generation.experiment_conclusions``'s
    per-version Python projection, this SQL restatement drives a WHERE
    filter and (via ``_version_summary``'s per-row call into that same
    Python projection) the gallery item's ``contextual_conclusion`` field
    over the full matching dataset, so it does not also scan the legacy
    ``verdict_decisions`` JSON-anchored fallback (fine: that table is dead
    for new writes and exists only for spec §0.7's completeness case in
    the single-version detail projection)."""
    verdict_case = case(
        *[
            (ContextualVerdictProjectionRecord.verdict == domain_value, label)
            for domain_value, label in VERDICT_LABELS.items()
        ],
        else_=None,
    )
    return (
        select(verdict_case)
        .where(
            ContextualVerdictProjectionRecord.workspace_id == AssetVersionRecord.workspace_id,
            ContextualVerdictProjectionRecord.asset_version_id == AssetVersionRecord.id,
            ContextualVerdictProjectionRecord.experiment_id == experiment_context,
            ContextualVerdictProjectionRecord.revoked_at.is_(None),
        )
        .correlate(AssetVersionRecord)
        .scalar_subquery()
    )


def _execution_column(name: str, fallback):
    """Resolve execution provenance per version, retaining imported legacy rows."""
    value = getattr(GenerationRunRecord, name)
    current = (select(value).join(RunItemRecord,
        (RunItemRecord.generation_run_id == GenerationRunRecord.id)
        & (RunItemRecord.workspace_id == GenerationRunRecord.workspace_id))
        .where(RunItemRecord.id == AssetVersionRecord.producing_run_item_id,
            RunItemRecord.workspace_id == AssetVersionRecord.workspace_id)
        .correlate(AssetVersionRecord).scalar_subquery())
    return func.coalesce(current, fallback)


def _execution_metadata(session, ctx, version, asset):
    current = session.execute(select(GenerationRunRecord.id, GenerationRunRecord.provider, GenerationRunRecord.model)
        .join(RunItemRecord, (RunItemRecord.generation_run_id == GenerationRunRecord.id)
            & (RunItemRecord.workspace_id == GenerationRunRecord.workspace_id))
        .where(RunItemRecord.id == version.producing_run_item_id,
            RunItemRecord.workspace_id == ctx.workspace_id)).first() if version.producing_run_item_id else None
    run_id, provider, model = current if current else (asset.run_id or "", asset.provider, asset.model)
    return {"run_id": run_id, "provider": provider, "model": model}


def _gallery_join_and_filters(
    ctx: TenantContext,
    *,
    product_id: str | None,
    run_id: str | None,
    provider: str | None,
    origin_kind: str | None,
    execution_mode: str | None,
    aspect_ratio: str | None,
    review_value: str | None,
    qc_state: str | None,
    experiment_context: str | None = None,
    conclusion: str | None = None,
) -> tuple[Any, list[Any]]:
    """A reusable ``(from_clause, wheres)`` pair -- ``AssetVersionRecord``
    joined to its owning ``AssetRecord``, scoped to the Workspace and every
    filter. Every caller (the item page query, every facet count, and the
    collection-level ``export`` eligibility check) applies both via
    ``.select_from(from_clause).where(*wheres)`` on its own fresh ``select()``
    rather than restructuring one shared ``Select`` object -- reselecting a
    different column list from a joined ``Select`` (e.g. via
    ``with_only_columns``) can silently drop the join and fall back to an
    implicit cross join once the columns clause no longer mentions both
    sides, which this sidesteps entirely."""
    from_clause = join(
        AssetVersionRecord, AssetRecord, AssetVersionRecord.asset_id == AssetRecord.id
    )
    wheres: list[Any] = [AssetVersionRecord.workspace_id == ctx.workspace_id]
    if product_id:
        wheres.append(AssetRecord.product_id == product_id)
    if run_id:
        wheres.append(_execution_column("id", AssetRecord.run_id) == run_id)
    if provider:
        wheres.append(_execution_column("provider", AssetRecord.provider) == provider)
    if origin_kind:
        wheres.append(AssetVersionRecord.origin_kind == origin_kind)
    if execution_mode:
        wheres.append(AssetVersionRecord.execution_mode == execution_mode)
    if aspect_ratio:
        wheres.append(AssetVersionRecord.aspect_ratio == aspect_ratio)
    if review_value:
        review_subq = _effective_review_subquery()
        if review_value == "unreviewed":
            wheres.append(review_subq.is_(None))
        else:
            wheres.append(review_subq == review_value)
    if qc_state:
        wheres.append(_latest_qc_state_subquery() == qc_state)
    if conclusion and experiment_context:
        conclusion_subq = _contextual_conclusion_subquery(experiment_context)
        if conclusion == "none":
            wheres.append(conclusion_subq.is_(None))
        else:
            wheres.append(conclusion_subq == conclusion)
    return from_clause, wheres


def list_asset_versions(
    session: Session,
    ctx: TenantContext,
    *,
    product_id: str | None = None,
    run_id: str | None = None,
    provider: str | None = None,
    origin_kind: str | None = None,
    execution_mode: str | None = None,
    review_value: str | None = None,
    aspect_ratio: str | None = None,
    qc_state: str | None = None,
    tag: str | None = None,
    experiment_context: str | None = None,
    conclusion: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> dict:
    """``AssetGalleryPage`` projection (spec §0.2): ``{ items, next_cursor,
    facets, allowed_actions }``.

    ``tag`` is accepted but not yet filtered on -- it names a
    ``stable_dimension_signature`` value, a column that does not exist yet.
    ``conclusion`` enforces its ``experiment_context_required`` precondition
    (spec §0.2) and, given both ``conclusion`` and ``experiment_context``,
    filters against the authoritative contextual-verdict projection for
    that exact context (Task 6, spec §0.7) -- ``"none"`` matches versions
    with no confirmed, non-revoked conclusion there.
    """
    if conclusion and not experiment_context:
        raise GalleryQueryError(
            "experiment_context_required",
            "conclusion filter requires experiment_context",
        )

    # Canonical filter key: binds the keyset cursor to this exact filter set
    # (app.platform.pagination hashes this string internally), matching the
    # ``filters_key`` convention every other Generation v1 list route in
    # app.generation.v1_router already uses.
    filters_key = "assets_gallery:" + ":".join(
        f"{name}={value or ''}"
        for name, value in sorted(
            {
                "product_id": product_id,
                "run_id": run_id,
                "provider": provider,
                "origin_kind": origin_kind,
                "execution_mode": execution_mode,
                "review_value": review_value,
                "aspect_ratio": aspect_ratio,
                "qc_state": qc_state,
                "tag": tag,
                "experiment_context": experiment_context,
                "conclusion": conclusion,
            }.items()
        )
    )

    from_clause, wheres = _gallery_join_and_filters(
        ctx,
        product_id=product_id,
        run_id=run_id,
        provider=provider,
        origin_kind=origin_kind,
        execution_mode=execution_mode,
        aspect_ratio=aspect_ratio,
        review_value=review_value,
        qc_state=qc_state,
        experiment_context=experiment_context,
        conclusion=conclusion,
    )

    query = (
        select(AssetVersionRecord, AssetRecord)
        .select_from(from_clause)
        .where(*wheres)
        .order_by(AssetVersionRecord.created_at.desc(), AssetVersionRecord.id.desc())
    )

    if cursor:
        # InvalidCursorError propagates to the caller (app.generation.v1_router
        # catches it exactly like every other Generation v1 list route's
        # ``_paginate`` cursor and turns it into ``400 invalid_cursor``).
        last_created_at, last_id = decode_keyset_cursor(cursor, ctx.workspace_id, filters_key)
        query = query.where(
            or_(
                AssetVersionRecord.created_at < last_created_at,
                and_(
                    AssetVersionRecord.created_at == last_created_at,
                    AssetVersionRecord.id < last_id,
                ),
            )
        )

    rows = session.execute(query.limit(limit + 1)).all()
    has_more = len(rows) > limit
    page_rows = rows[:limit]

    next_cursor = None
    if has_more and page_rows:
        last_version = page_rows[-1][0]
        next_cursor = encode_cursor(
            ctx.workspace_id,
            filters_key,
            (last_version.created_at.isoformat(), last_version.id),
        )

    items = [
        _version_summary(session, ctx, version, asset, experiment_context=experiment_context)
        for version, asset in page_rows
    ]
    facets = _compute_facets(session, from_clause, wheres)
    allowed_actions = _compute_collection_actions(session, ctx, from_clause, wheres)

    return {
        "items": items,
        "next_cursor": next_cursor,
        "facets": facets,
        "allowed_actions": allowed_actions,
    }


def _version_summary(
    session: Session,
    ctx: TenantContext,
    version: AssetVersionRecord,
    asset: AssetRecord,
    *,
    experiment_context: str | None = None,
) -> dict:
    effective_review_value = project_effective_review(session, ctx, version.id)

    latest_qc_state = session.execute(
        select(AssetQCAttemptRecord.state)
        .where(
            AssetQCAttemptRecord.asset_version_id == version.id,
            AssetQCAttemptRecord.workspace_id == ctx.workspace_id,
        )
        .order_by(AssetQCAttemptRecord.attempt_sequence.desc())
        .limit(1)
    ).scalar_one_or_none()

    # contextual_conclusion is populated only for the requested
    # experiment_context (spec §0.2/§0.7) -- it is never a global property
    # of the AssetVersion.
    contextual_conclusion = (
        project_contextual_conclusion_for_context(session, ctx, version.id, experiment_context)
        if experiment_context
        else None
    )

    return {
        "id": version.id,
        "asset_id": version.asset_id,
        "product_id": asset.product_id,
        **_execution_metadata(session, ctx, version, asset),
        "status": asset.status,
        "aspect_ratio": version.aspect_ratio,
        "stable_dimension_signature": None,  # No column yet
        "tags": asset.tags_json or {},
        "effective_review_value": effective_review_value,
        "latest_qc_state": latest_qc_state,
        "contextual_conclusion": contextual_conclusion,
        "origin_kind": version.origin_kind,
        "execution_mode": version.execution_mode,
        "mock_evidence": None,  # No column yet
        "byte_size": version.byte_size or 0,
        # Signed thumbnail link (Issue #159). The projection stays pure --
        # signing needs the process's BlobStore and runs after the session
        # closes -- so the route fills this in via
        # ``app.generation.thumbnails.attach_thumbnail_links``; it stays
        # ``None`` for versions without a derivative.
        "thumbnail": None,
        "created_at": version.created_at.isoformat() if version.created_at else None,
    }


def _compute_facets(session: Session, from_clause: Any, wheres: list[Any]) -> dict:
    """Facets over the full Workspace-scoped, filtered dataset -- every
    active filter applies to every facet (this is not a "remove this
    facet's own filter to show alternatives" faceted-search computation),
    matching spec §0.2's "computed by the server over the matching
    Workspace dataset, not inferred from the currently loaded page"."""

    def _facet_values(column) -> list[dict[str, Any]]:
        rows = session.execute(
            select(column, func.count())
            .select_from(from_clause)
            .where(*wheres)
            .group_by(column)
        ).all()
        return [
            {"value": str(value), "count": count}
            for value, count in rows
            if value is not None
        ]

    def _review_facet_values() -> list[dict[str, Any]]:
        """Like ``_facet_values``, but over the effective-review projection
        (spec §0.3) rather than a plain column -- a ``None`` bucket (no
        effective review, including a ``cleared`` head) surfaces as the
        ``"unreviewed"`` facet value, matching the ``review_value=unreviewed``
        filter this same function's WHERE clause already accepts."""
        effective = _effective_review_subquery()
        rows = session.execute(
            select(effective, func.count())
            .select_from(from_clause)
            .where(*wheres)
            .group_by(effective)
        ).all()
        return [
            {"value": value if value is not None else "unreviewed", "count": count}
            for value, count in rows
        ]

    return {
        "providers": _facet_values(_execution_column("provider", AssetRecord.provider)),
        "origins": _facet_values(AssetVersionRecord.origin_kind),
        "modes": _facet_values(AssetVersionRecord.execution_mode),
        "reviews": _review_facet_values(),
        "aspects": _facet_values(AssetVersionRecord.aspect_ratio),
        "qc_states": [],  # No qc_state facet computed yet
        "tags": [],  # No stable_dimension_signature column yet
    }


_COLLECTION_ACTIONS = ["download", "review", "regenerate", "export"]


def _compute_collection_actions(
    session: Session, ctx: TenantContext, from_clause: Any, wheres: list[Any]
) -> list[ActionEligibility]:
    """Collection-level ``allowed_actions`` (spec §0.2): ``export``
    eligibility for the current scope plus the same review/regenerate/
    download codes ``app.generation.run_queries.asset_version_actions``
    advertises per-item (unlike that function, ``ctx.role`` is not
    consulted anywhere else in this module -- ``system`` is the only role
    that cannot initiate a human review/regeneration command)."""
    has_match = session.execute(
        select(
            select(AssetVersionRecord.id)
            .select_from(from_clause)
            .where(*wheres)
            .limit(1)
            .exists()
        )
    ).scalar()
    can_act = ctx.role in ("owner", "member")
    checks: dict[str, tuple[bool, str | None]] = {
        "download": (True, None),
        "review": (can_act, None if can_act else "role_not_permitted"),
        "regenerate": (can_act, None if can_act else "role_not_permitted"),
        "export": (bool(has_match), None if has_match else "no_matching_assets"),
    }
    return compute_actions(_COLLECTION_ACTIONS, checks)


# --------------------------------------------------------------------------
# AssetVersionDetail (Task 4, spec §0.2)
# --------------------------------------------------------------------------


def _review_dict(review: QualityReviewRecord) -> dict:
    return {
        "id": review.id,
        "asset_version_id": review.asset_version_id,
        "review_value": review.review_value,
        "reason": review.reason,
        "supersedes_review_id": review.supersedes_review_id,
        "actor_id": review.actor_id,
        "created_at": review.created_at.isoformat() if review.created_at else None,
        "source": review.source or "human",
    }


def _qc_check_dict(check: AssetQCCheckResultRecord) -> dict:
    evidence = check.evidence_json or {}
    verdict = evidence.get("verdict") or {
        "pass": "pass", "fail": "fail", "skip": "skip", "error": "uncertain",
    }.get(check.outcome, check.outcome)
    return {
        "rule_id": check.rule_id,
        "check_type": check.check_type,
        "severity": check.severity,
        "outcome": check.outcome,
        "verdict": verdict,
        "reason": check.feedback_detail or evidence.get("reason") or None,
        "fix_hint": evidence.get("fix_hint") or None,
        "must_match_id": evidence.get("must_match_id"),
        "description": evidence.get("description") or None,
    }


def _qc_attempt_dict(
    attempt: AssetQCAttemptRecord, checks: list[AssetQCCheckResultRecord] | None = None,
) -> dict:
    """One QC attempt with its verdicts and Chinese reasons (Issue #169)."""
    summary = attempt.feedback_summary_json or {}
    return {
        "id": attempt.id,
        "state": attempt.state,
        "attempt_sequence": attempt.attempt_sequence,
        "feedback_round": attempt.feedback_round,
        "execution_mode": attempt.execution_mode,
        "created_at": attempt.created_at.isoformat() if attempt.created_at else None,
        "summary": summary.get("summary"),
        "reasons": list(summary.get("reasons") or []),
        "consecutive_failures": int(summary.get("consecutive_failures") or 0),
        "projected_review": summary.get("projected_review"),
        "model": summary.get("model"),
        "controlled_mock": bool(summary.get("controlled_mock")),
        "checks": [_qc_check_dict(check) for check in checks or []],
    }


def _lineage_entry(session: Session, version_id: str, run_item_id: str | None) -> dict:
    """One ``parent``/``children`` lineage entry: ``version_id`` plus the
    regeneration intent recorded on *that version's own* producing
    ``RunItemRecord`` (spec §0.6 -- the intent describes how ``version_id``
    was produced from its parent, so it is looked up off the child side of
    the edge in both the ``parent`` and ``children`` projections)."""
    run_item = session.get(RunItemRecord, run_item_id) if run_item_id else None
    return {
        "version_id": version_id,
        "intent": run_item.intent_kind if run_item is not None else None,
    }


def get_asset_version_detail(session: Session, ctx: TenantContext, version_id: str) -> dict:
    """``AssetVersionDetail`` projection (spec §0.2): the full-provenance
    projection Browser Task 8's ``LightboxModal`` renders.

    Extends Task 3's ``_version_summary`` fields with seed, prompt, the full
    append-only ``reviews[]``, QC attempt history, and parent/children
    lineage (``AssetVersionRecord.parent_version_id`` plus the producing
    ``RunItemRecord.intent_kind`` -- spec §0.6 requires the detail
    projection to return parent/children directly rather than the browser
    deriving lineage from ``producing_run_item_id`` itself).

    Workspace-scoped like ``resolve_asset_blob_ref``: a version that exists
    but belongs to another Workspace raises the same
    ``GenerationResourceNotFoundError`` as one that does not exist at all,
    which ``v1_router``'s existing handler turns into ``404
    asset_version_not_found``.

    ``review_head_id``/``effective_review_value`` apply the supersession-aware
    head-review rule (spec §0.3, Task 5) -- the same rule as
    ``app.generation.review.find_review_head``/``project_effective_review``,
    computed in-memory over the already-loaded ``reviews`` list rather than
    re-querying (see the inline comment below).
    ``contextual_conclusions`` (Task 6, spec §0.7) is
    ``app.generation.experiment_conclusions.project_contextual_conclusions``
    -- every non-revoked, fully-anchored conclusion for this exact
    AssetVersion, each with its own full context (never collapsed into a
    single global verdict). ``allowed_actions`` reuses
    ``run_queries.asset_version_actions`` unchanged so this pre-existing
    detail route's eligibility semantics (``object_key``-based download
    eligibility) do not regress.
    """
    row = session.execute(
        select(AssetVersionRecord, AssetRecord)
        .select_from(
            join(AssetVersionRecord, AssetRecord, AssetVersionRecord.asset_id == AssetRecord.id)
        )
        .where(
            AssetVersionRecord.id == version_id,
            AssetVersionRecord.workspace_id == ctx.workspace_id,
        )
    ).one_or_none()
    if row is None:
        raise GenerationResourceNotFoundError("asset_version", version_id, code="asset_version_not_found")
    version, asset = row

    reviews = session.scalars(
        select(QualityReviewRecord)
        .where(
            QualityReviewRecord.asset_version_id == version.id,
            QualityReviewRecord.workspace_id == ctx.workspace_id,
        )
        .order_by(QualityReviewRecord.created_at.desc(), QualityReviewRecord.id.desc())
    ).all()
    # Supersession-aware head rule (spec §0.3, Task 5) -- the same rule as
    # app.generation.review.find_review_head/project_effective_review,
    # computed in-memory over the already-loaded, already-ordered ``reviews``
    # list rather than re-querying: the head is the newest row (this list is
    # already ``created_at DESC, id DESC``) that is not itself named by
    # another row's ``supersedes_review_id``. A ``cleared`` head is still the
    # head (``review_head_id`` still points at it) but projects to a
    # ``None`` ``effective_review_value`` -- a tombstone, not a value.
    superseded_ids = {r.supersedes_review_id for r in reviews if r.supersedes_review_id}
    review_head = next((r for r in reviews if r.id not in superseded_ids), None)
    review_head_id = review_head.id if review_head else None
    effective_review_value = (
        None
        if review_head is None or review_head.review_value == "cleared"
        else review_head.review_value
    )

    qc_attempts = session.scalars(
        select(AssetQCAttemptRecord)
        .where(
            AssetQCAttemptRecord.asset_version_id == version.id,
            AssetQCAttemptRecord.workspace_id == ctx.workspace_id,
        )
        .order_by(AssetQCAttemptRecord.attempt_sequence.desc())
    ).all()
    latest_qc_attempt_id = qc_attempts[0].id if qc_attempts else None
    latest_qc_state = qc_attempts[0].state if qc_attempts else None
    checks_by_attempt: dict[str, list[AssetQCCheckResultRecord]] = {}
    if qc_attempts:
        for check in session.scalars(
            select(AssetQCCheckResultRecord)
            .where(
                AssetQCCheckResultRecord.workspace_id == ctx.workspace_id,
                AssetQCCheckResultRecord.qc_attempt_id.in_([a.id for a in qc_attempts]),
            )
            .order_by(AssetQCCheckResultRecord.created_at, AssetQCCheckResultRecord.id)
        ).all():
            checks_by_attempt.setdefault(check.qc_attempt_id, []).append(check)

    parent = None
    if version.parent_version_id:
        parent = _lineage_entry(session, version.parent_version_id, version.producing_run_item_id)

    child_rows = session.execute(
        select(AssetVersionRecord.id, AssetVersionRecord.producing_run_item_id)
        .where(
            AssetVersionRecord.parent_version_id == version.id,
            AssetVersionRecord.workspace_id == ctx.workspace_id,
        )
        .order_by(AssetVersionRecord.created_at.asc(), AssetVersionRecord.id.asc())
    ).all()
    children = [
        _lineage_entry(session, child_id, child_run_item_id)
        for child_id, child_run_item_id in child_rows
    ]

    return {
        "id": version.id,
        "asset_id": version.asset_id,
        "product_id": asset.product_id,
        **_execution_metadata(session, ctx, version, asset),
        "status": asset.status,
        "kind": version.kind,
        "aspect_ratio": version.aspect_ratio,
        "stable_dimension_signature": None,  # No column yet
        "tags": asset.tags_json or {},
        "mime_type": version.mime_type,
        "byte_size": version.byte_size or 0,
        "content_sha256": version.content_sha256,
        "origin_kind": version.origin_kind,
        "execution_mode": version.execution_mode,
        "mock_evidence": None,  # No column yet
        "seed": {
            "requested": version.requested_seed,
            "effective": version.effective_seed,
            "unavailable_reason": version.seed_unavailable_reason,
        },
        "prompt": asset.prompt,
        "reference_sources": version.reference_sources_json,
        "producing_run_item_id": version.producing_run_item_id,
        "provider_attempt_id": version.provider_attempt_id,
        "reviews": [_review_dict(r) for r in reviews],
        "review_head_id": review_head_id,
        "effective_review_value": effective_review_value,
        "qc_attempts": [_qc_attempt_dict(a, checks_by_attempt.get(a.id)) for a in qc_attempts],
        "latest_qc_attempt_id": latest_qc_attempt_id,
        "latest_qc_state": latest_qc_state,
        "contextual_conclusions": project_contextual_conclusions(session, ctx, version.id),
        "parent": parent,
        "children": children,
        "allowed_actions": asset_version_actions(version),
        "created_at": version.created_at.isoformat() if version.created_at else None,
    }
