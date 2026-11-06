"""Snapshot-pinned breakdown query with privacy suppression (spec §13, §22).

Issue #87 Task 3.

Reads exclusively from a sealed ``ScaleEvidenceSnapshotRecord`` and its
immutable ``ScaleSnapshotBreakdownSegmentRecord`` / ``ScaleSnapshotBreakdown
DailyRecord`` children. Never reads from evaluator results -- evaluator
Phase 7 produces ``review_delivery_mix`` advice *candidates*, not the
breakdown data a caller pages through here.

Window boundaries, the safe watermark, and the frozen Policy/Target
identity are read directly off the sealed snapshot row
(``current_window_start_local_day``, ``current_window_end_local_day``,
``safe_watermark``, ``policy_version_id``, ``target_version_id``) rather
than through ``ScaleEpisodeProjectionRecord``. That projection is a
replaceable, evaluation-scoped cache (spec §1.9) with no Policy/Target/
window columns of its own -- it exists to answer "what does this Episode
look like *now*", not "what did this specific sealed snapshot see". Every
field a breakdown page reports must stay pinned to the snapshot the caller
asked for; reading them through the projection would let a stale or
rolled-over projection row silently swap in the wrong Policy or window
whenever the Episode has moved on since this snapshot was sealed.

Enrollment scoping is proven through ``ScaleEpisodeRecord.enrollment_id``
(the sealed snapshot itself carries only ``episode_id``, per spec §1.8) --
a snapshot whose Episode belongs to a different Enrollment is reported
identically to a missing snapshot, so this function is never usable to
enumerate another Enrollment's snapshot ids within the same Workspace/mode.

Privacy-suppressed segments appear as typed placeholders with no metric
values and are never silently omitted -- the frozen ``suppression_reason``
is the only field populated. Age/gender segments show only descriptive
aggregate spend shares: CPA and ROAS are withheld even when the underlying
evidence is present, so this projection never doubles as targeting advice
across a protected demographic axis (spec §22).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.platform.pagination import decode_offset_cursor, encode_cursor
from app.platform.tenancy import TenantContext

from .models import (
    ScaleEnrollmentRecord,
    ScaleEpisodeRecord,
    ScaleEvidenceSnapshotRecord,
    ScaleSnapshotBreakdownDailyRecord,
    ScaleSnapshotBreakdownSegmentRecord,
)

__all__ = [
    "BreakdownNotAvailableError",
    "BreakdownPage",
    "BreakdownSegment",
    "query_snapshot_breakdowns",
]

_VALID_BREAKDOWN_TYPES = frozenset({"placement", "country", "age_gender"})
_DESCRIPTIVE_ONLY_TYPES = frozenset({"age_gender"})

# Money/ratio values are quantized before crossing to a string so identical
# evidence always renders identical digits, regardless of which backend
# (SQLite float affinity vs. PostgreSQL NUMERIC) produced the Decimal.
_DISPLAY_QUANT = Decimal("0.000001")

_ZERO_TOTALS: tuple[Decimal, int, int, int, Decimal] = (
    Decimal("0"), 0, 0, 0, Decimal("0"),
)


class BreakdownNotAvailableError(RuntimeError):
    """Deterministic, stably-coded rejection raised by
    :func:`query_snapshot_breakdowns`. ``code`` is the stable
    machine-readable reason; ``detail`` is optional human context."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class BreakdownSegment:
    breakdown_type: str
    segment_key: str
    state: str
    suppression_reason: str | None = None
    spend_share: str | None = None
    impressions: int | None = None
    link_clicks: int | None = None
    purchases: int | None = None
    purchase_value: str | None = None
    cpa: str | None = None
    roas: str | None = None
    insufficient_reason: str | None = None


@dataclass(frozen=True)
class BreakdownPage:
    snapshot_id: str
    episode_id: str
    breakdown_type: str
    window_start: date
    window_end: date
    safe_watermark: date
    meta_mode: str
    policy_version_id: str
    target_version_id: str | None
    segments: list[BreakdownSegment]
    next_cursor: str | None = None


def query_snapshot_breakdowns(
    session: Session,
    context: TenantContext,
    *,
    enrollment_id: str,
    snapshot_id: str,
    breakdown_type: str,
    cursor: str | None = None,
    limit: int = 50,
) -> BreakdownPage:
    """Return one cursor-paginated page of frozen breakdown segments for a
    sealed snapshot, scoped to the Enrollment whose Episode owns it.

    Raises :class:`BreakdownNotAvailableError` for: an unknown
    ``breakdown_type`` (``invalid_breakdown_type``); a missing Enrollment
    (``enrollment_not_found``); a missing snapshot, an unsealed snapshot,
    or a snapshot belonging to a different Enrollment (all
    ``snapshot_not_found``/``snapshot_not_sealed``). Raises
    :class:`~app.platform.pagination.InvalidCursorError` for a ``cursor``
    not minted by this query for this Workspace, snapshot and type -- only
    once the Enrollment and snapshot are found.
    """
    if breakdown_type not in _VALID_BREAKDOWN_TYPES:
        raise BreakdownNotAvailableError(
            "invalid_breakdown_type",
            f"{breakdown_type!r} must be one of {sorted(_VALID_BREAKDOWN_TYPES)}",
        )

    enrollment = session.execute(
        select(ScaleEnrollmentRecord).filter_by(
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            id=enrollment_id,
        )
    ).scalar_one_or_none()
    if enrollment is None:
        raise BreakdownNotAvailableError("enrollment_not_found", enrollment_id)

    snapshot = session.execute(
        select(ScaleEvidenceSnapshotRecord).filter_by(
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            id=snapshot_id,
        )
    ).scalar_one_or_none()
    if snapshot is None:
        raise BreakdownNotAvailableError("snapshot_not_found", snapshot_id)
    if snapshot.sealed_at is None or snapshot.canonical_hash is None:
        raise BreakdownNotAvailableError("snapshot_not_sealed", snapshot_id)

    episode = session.execute(
        select(ScaleEpisodeRecord).filter_by(
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            id=snapshot.episode_id,
        )
    ).scalar_one_or_none()
    if episode is None or episode.enrollment_id != enrollment_id:
        # Reported identically to a plain missing snapshot -- this must
        # never let a caller confirm that a snapshot id exists under some
        # other Enrollment.
        raise BreakdownNotAvailableError("snapshot_not_found", snapshot_id)

    segment_rows = list(
        session.execute(
            select(ScaleSnapshotBreakdownSegmentRecord)
            .filter_by(
                workspace_id=context.workspace_id,
                meta_mode=context.meta_mode,
                snapshot_id=snapshot_id,
                breakdown_type=breakdown_type,
            )
            .order_by(ScaleSnapshotBreakdownSegmentRecord.segment_key)
        ).scalars().all()
    )

    filters_key = f"breakdowns:{snapshot_id}:{breakdown_type}"
    start = decode_offset_cursor(cursor, context.workspace_id, filters_key) if cursor else 0

    page_rows = segment_rows[start:start + limit]
    next_cursor = None
    if start + limit < len(segment_rows):
        next_cursor = encode_cursor(context.workspace_id, filters_key, (start + limit,))

    # Spend share is a share of *all* observed segments of this breakdown
    # type on this snapshot, not just the segments on this page -- so the
    # denominator is computed over the full result set before slicing.
    observed_ids = [seg.id for seg in segment_rows if seg.state == "observed"]
    total_spend = _sum_spend(session, context, observed_ids)

    page_observed_ids = [seg.id for seg in page_rows if seg.state == "observed"]
    daily_totals = _daily_totals_by_segment(session, context, page_observed_ids)

    is_descriptive_only = breakdown_type in _DESCRIPTIVE_ONLY_TYPES
    segments = [
        _build_segment(seg, daily_totals.get(seg.id), total_spend, is_descriptive_only)
        for seg in page_rows
    ]

    return BreakdownPage(
        snapshot_id=snapshot.id,
        episode_id=snapshot.episode_id,
        breakdown_type=breakdown_type,
        window_start=snapshot.current_window_start_local_day,
        window_end=snapshot.current_window_end_local_day,
        safe_watermark=snapshot.safe_watermark,
        meta_mode=snapshot.meta_mode,
        policy_version_id=snapshot.policy_version_id,
        target_version_id=snapshot.target_version_id,
        segments=segments,
        next_cursor=next_cursor,
    )


def _to_decimal(value: object) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _sum_spend(session: Session, context: TenantContext, segment_ids: list[str]) -> Decimal:
    if not segment_ids:
        return Decimal("0")
    total = session.execute(
        select(func.coalesce(func.sum(ScaleSnapshotBreakdownDailyRecord.spend), 0)).filter(
            ScaleSnapshotBreakdownDailyRecord.workspace_id == context.workspace_id,
            ScaleSnapshotBreakdownDailyRecord.meta_mode == context.meta_mode,
            ScaleSnapshotBreakdownDailyRecord.breakdown_segment_id.in_(segment_ids),
        )
    ).scalar_one()
    return _to_decimal(total)


def _daily_totals_by_segment(
    session: Session, context: TenantContext, segment_ids: list[str],
) -> dict[str, tuple[Decimal, int, int, int, Decimal]]:
    if not segment_ids:
        return {}
    rows = session.execute(
        select(
            ScaleSnapshotBreakdownDailyRecord.breakdown_segment_id,
            func.coalesce(func.sum(ScaleSnapshotBreakdownDailyRecord.spend), 0),
            func.coalesce(func.sum(ScaleSnapshotBreakdownDailyRecord.impressions), 0),
            func.coalesce(func.sum(ScaleSnapshotBreakdownDailyRecord.link_clicks), 0),
            func.coalesce(func.sum(ScaleSnapshotBreakdownDailyRecord.purchases), 0),
            func.coalesce(func.sum(ScaleSnapshotBreakdownDailyRecord.purchase_value), 0),
        )
        .filter(
            ScaleSnapshotBreakdownDailyRecord.workspace_id == context.workspace_id,
            ScaleSnapshotBreakdownDailyRecord.meta_mode == context.meta_mode,
            ScaleSnapshotBreakdownDailyRecord.breakdown_segment_id.in_(segment_ids),
        )
        .group_by(ScaleSnapshotBreakdownDailyRecord.breakdown_segment_id)
    ).all()
    return {
        row[0]: (
            _to_decimal(row[1]), int(row[2]), int(row[3]), int(row[4]), _to_decimal(row[5]),
        )
        for row in rows
    }


def _build_segment(
    seg: ScaleSnapshotBreakdownSegmentRecord,
    totals: tuple[Decimal, int, int, int, Decimal] | None,
    total_spend: Decimal,
    is_descriptive_only: bool,
) -> BreakdownSegment:
    if seg.state == "privacy_suppressed":
        return BreakdownSegment(
            breakdown_type=seg.breakdown_type,
            segment_key=seg.segment_key,
            state="privacy_suppressed",
            suppression_reason=seg.suppression_reason,
        )

    spend, impressions, link_clicks, purchases, purchase_value = totals or _ZERO_TOTALS

    if total_spend > 0:
        spend_share = str((spend / total_spend).quantize(_DISPLAY_QUANT))
    else:
        spend_share = "0"

    cpa = None
    roas = None
    if not is_descriptive_only:
        if purchases > 0 and spend > 0:
            cpa = str((spend / purchases).quantize(_DISPLAY_QUANT))
        if spend > 0 and purchase_value > 0:
            roas = str((purchase_value / spend).quantize(_DISPLAY_QUANT))

    return BreakdownSegment(
        breakdown_type=seg.breakdown_type,
        segment_key=seg.segment_key,
        state="observed",
        spend_share=spend_share,
        impressions=impressions,
        link_clicks=link_clicks,
        purchases=purchases,
        purchase_value=str(purchase_value.quantize(_DISPLAY_QUANT)) if purchase_value else None,
        cpa=cpa,
        roas=roas,
    )
