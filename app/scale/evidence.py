"""Immutable evidence ingestion: snapshot sealing and read-consistent
assembly (spec §3.2-§3.3, and the persistence-assembler responsibilities of
§2.9).

This module is the sole adapter between the mutable-free ORM source records
(Tasks 2-5: ``ScaleDailyMetricSourceRecord``, ``ScaleExactWindowSourceRecord``,
``ScaleBreakdownSourceRecord``, ``ScaleDeliveryConfigSourceRecord``, their
owning ``ScaleEvidenceSourceManifestRecord``) and the pure evaluator's frozen
``ScaleEvidenceSnapshot`` value object (Task 7/9: ``app.scale._contracts``,
``app.scale.evaluator``). It exposes exactly two internal interfaces (spec
§3.1):

* :func:`seal_snapshot` -- Transaction A. Selects the run's already-frozen
  ``complete`` source manifest, rejects a ``blocked`` one, freezes Policy,
  Target-or-no-target, the metric-contract set, the predecessor
  ``PriorStreakState``, inserts the snapshot parent UNSEALED and flushes it,
  then inserts every source-link, quality-issue, and breakdown child, then
  computes the canonical hash and seals the parent as a final UPDATE -- all
  inside one SAVEPOINT, recovering idempotently from a concurrent claim-hash
  or sealed-content race. The parent must be flushed unsealed before any
  child is inserted: the Task 6 migration's PostgreSQL guard triggers on the
  child tables reject a child INSERT once the parent's `sealed_at IS NOT
  NULL`, and the FK forces the parent row to exist before a child can
  reference it, so an already-sealed parent would block its own children.
* :func:`assemble_snapshot` -- a read-consistent reconstruction of a sealed
  snapshot's frozen value objects, verifying provenance and recomputing the
  canonical hash in constant time before returning.

Known task-10 scoping decisions (the full generality is orchestration's job,
per Task 12):

* upstream-eligibility recheck (spec §3.3 step 2) and delivery-configuration
  Episode rollover (step 3) are NOT implemented here -- a run whose manifest
  is ``blocked`` simply fails sealing (pitfall #2: the run's `run_state`
  CHECK constraint forbids a `blocked` run_state without a `snapshot_id`), and
  it is orchestration's job to transition that run to a terminal outcome;
* predecessor selection uses "greatest earlier `as_of_local_day` Evaluation
  on this Episode" without full replay-generation/projection-eligibility
  filtering -- Task 12 owns that fuller selection rule;
* `ScalePolicySnapshot`/`ScaleTargetSnapshot` fields with no dedicated
  ``ScalePolicyVersionRecord``/``ScaleTargetVersionRecord`` column are read
  from the Policy's ``signal_thresholds`` JSON blob under the flat key
  convention documented on ``_POLICY_JSON_DECIMAL_FIELDS`` /
  ``_POLICY_JSON_INT_FIELDS`` below (no earlier task defined this mapping);
  Target threshold overrides are not modeled on ``ScaleTargetVersionRecord``
  and are always frozen as ``None``;
* ``DeliveryConfigSnapshot.status_history`` is always ``()`` and
  ``diagnostic_state`` defaults to ``NONE_`` -- no earlier task added a
  status-history or diagnostic-state column to
  ``ScaleDeliveryConfigSourceRecord``;
* `MetricContractRef.observation_contract_version_id` reuses
  ``ScaleMetricSemanticContractSetMemberRecord.observation_contract_generation``
  -- that record only retains the wrapped #23 physical contract generation
  and its hash (spec §1.6), not a separate per-instance version id, so this
  is the closest available identity. `_build_exact_window`'s "unavailable"
  fallback and `_assemble_exact_window`'s missing-link fallback use the
  literal string ``"unavailable"`` for the same field instead, since no
  contract instance was actually observed in that case.
"""
from __future__ import annotations

import hmac
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.persistence.models import new_id, utcnow
from app.platform.tenancy import TenantContext
from app.scale._canonical import canonical_hash
from app.scale._contracts import (
    BreakdownSegment,
    CanonicalKeyValue,
    DailyMetricValues,
    DailyObservation,
    DataQualityItem,
    DataQualityLocator,
    DeliveryConfigSnapshot,
    ExactWindowMetrics,
    MetricContractRef,
    MetricWindow,
    PriorStreakState,
    ScaleEvidenceSnapshot,
    ScalePolicySnapshot,
    ScaleTargetSnapshot,
    StreakCompatibility,
    StreakCounter,
)
from app.scale.models import (
    NO_TARGET_MARKER,
    ScaleBreakdownDailyChildRecord,
    ScaleBreakdownPrivacyRecord,
    ScaleBreakdownSourceRecord,
    ScaleDailyMetricSourceRecord,
    ScaleDeliveryConfigSourceRecord,
    ScaleEnrollmentRecord,
    ScaleEpisodeRecord,
    ScaleEvaluationRecord,
    ScaleEvaluationRunRecord,
    ScaleEvaluationStreakCounterRecord,
    ScaleEvidenceSnapshotRecord,
    ScaleEvidenceSourceLinkRecord,
    ScaleEvidenceSourceManifestRecord,
    ScaleExactWindowSourceRecord,
    ScaleManifestItemRecord,
    ScaleMetricSemanticContractSetMemberRecord,
    ScalePolicyVersionRecord,
    ScaleSnapshotBreakdownDailyRecord,
    ScaleSnapshotBreakdownSegmentRecord,
    ScaleSnapshotQualityIssueRecord,
    ScaleSourceQualityIssueRecord,
    ScaleTargetVersionRecord,
)
from app.scale.types import (
    BreakdownEvidenceState,
    BreakdownSuppressionReason,
    BreakdownType,
    DailyEvidenceState,
    DataQualityImpact,
    DataQualityIssueCode,
    DeliveryDiagnosticState,
    MetaMode,
    MetricContractType,
    PriorStreakAbsenceReason,
    ScaleHealth,
    WindowRole,
)

CANONICAL_SCHEMA_VERSION = "scale-snapshot-v1"
SNAPSHOT_HASH_DOMAIN = "scale-evidence-snapshot-v1"
CLAIM_HASH_DOMAIN = "scale-snapshot-claim-v1"
CONTRACT_SET_HASH_DOMAIN = "scale-metric-contract-set-v1"

__all__ = [
    "SnapshotSealingError",
    "SnapshotAssemblyError",
    "seal_snapshot",
    "assemble_snapshot",
]


class SnapshotSealingError(RuntimeError):
    """Deterministic, stably-coded rejection raised by :func:`seal_snapshot`."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


class SnapshotAssemblyError(RuntimeError):
    """Deterministic, stably-coded rejection raised by :func:`assemble_snapshot`."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _ensure_utc(value: datetime | None) -> datetime | None:
    """SQLite strips tzinfo on datetime round-trip; every datetime read back
    from the database that flows into a ``ScaleEvidenceSnapshot`` value
    object or its canonical hash payload must be re-attached to UTC before
    use (known pitfall #1). A value that is already offset-aware (e.g. one
    read from PostgreSQL, or a Python-native value not yet round-tripped) is
    left unchanged."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _for_update(session: Session, stmt):
    """PostgreSQL row locking; SQLite (used by tests and the bounded local
    path) does not support ``SELECT ... FOR UPDATE``."""
    if session.get_bind().dialect.name == "postgresql":
        return stmt.with_for_update()
    return stmt


def _hash_payload(snapshot: ScaleEvidenceSnapshot) -> dict[str, Any]:
    """The explicit snapshot-hash payload (spec §2.7/§3.3): every
    evaluator-relevant field, excluding ``snapshot_id``, ``canonical_hash``
    itself, and persistence-only metadata."""
    return {
        "canonical_schema_version": snapshot.canonical_schema_version,
        "episode_id": snapshot.episode_id,
        "meta_mode": snapshot.meta_mode,
        "meta_connection_id": snapshot.meta_connection_id,
        "scenario_version_id": snapshot.scenario_version_id,
        "timezone_name": snapshot.timezone_name,
        "currency": snapshot.currency,
        "policy": snapshot.policy,
        "target": snapshot.target,
        "metric_contract_set_hash": snapshot.metric_contract_set_hash,
        "metric_contracts": snapshot.metric_contracts,
        "delivery_config": snapshot.delivery_config,
        "current_7d": snapshot.current_7d,
        "baseline_7d": snapshot.baseline_7d,
        "current_exact_7d": snapshot.current_exact_7d,
        "baseline_exact_7d": snapshot.baseline_exact_7d,
        "rolling_exact_14d": snapshot.rolling_exact_14d,
        "breakdowns": snapshot.breakdowns,
        "data_quality_issues": snapshot.data_quality_issues,
        "complete_local_days_in_episode": snapshot.complete_local_days_in_episode,
        "active_delivery_days_in_episode": snapshot.active_delivery_days_in_episode,
        "data_continuity_generation": snapshot.data_continuity_generation,
        "prior_streak": snapshot.prior_streak,
        "prior_streak_absence_reason": snapshot.prior_streak_absence_reason,
        "last_complete_local_day": snapshot.last_complete_local_day,
        "last_sync_at": snapshot.last_sync_at,
        "freshness_reference_at": snapshot.freshness_reference_at,
        "as_of_local_day": snapshot.as_of_local_day,
    }


def _snapshot_hash(snapshot: ScaleEvidenceSnapshot) -> str:
    return canonical_hash(
        _hash_payload(snapshot),
        domain=SNAPSHOT_HASH_DOMAIN,
        schema_version=snapshot.canonical_schema_version,
    )


_ROUNDING_MODE_MAP = {
    "round_half_even": "ROUND_HALF_EVEN",
    "round_half_up": "ROUND_HALF_UP",
    "round_down": "ROUND_DOWN",
    "round_ceiling": "ROUND_CEILING",
}

# Task-10-defined convention (no earlier task fixed one): flat keys read out
# of ``ScalePolicyVersionRecord.signal_thresholds`` for every
# ``ScalePolicySnapshot`` field with no dedicated typed column. Decimal
# fields are stored as JSON strings (JSON has no Decimal) and parsed via
# ``Decimal(str(...))``; int fields are stored as JSON numbers.
_POLICY_JSON_DECIMAL_FIELDS = (
    "confidence_level",
    "confidence_z",
    "frequency_level_threshold",
    "frequency_growth_threshold",
    "ctr_decline_threshold",
    "ctr_stability_tolerance",
    "cvr_decline_threshold",
    "cvr_stability_tolerance",
    "cpm_increase_threshold",
    "delivery_decline_threshold",
    "cpa_worsening_threshold",
    "roas_worsening_threshold",
    "bootstrap_lower_percentile",
    "bootstrap_upper_percentile",
    "breakdown_min_spend_share",
)
_POLICY_JSON_INT_FIELDS = (
    "active_day_floor",
    "consecutive_composite_fatigue_hits_required",
    "consecutive_healthy_evaluations_required",
    "breakdown_min_sample_impressions",
    "breakdown_min_sample_clicks",
    "breakdown_min_sample_purchases",
)


def _policy_snapshot(policy: ScalePolicyVersionRecord) -> ScalePolicySnapshot:
    thresholds = policy.signal_thresholds or {}
    kwargs: dict[str, Any] = {
        "policy_version_id": policy.id,
        "algorithm_version": policy.algorithm_version,
        "decimal_precision": policy.decimal_precision,
        "decimal_rounding_mode": _ROUNDING_MODE_MAP.get(
            policy.decimal_rounding, policy.decimal_rounding
        ),
        "complete_day_floor": policy.evidence_floor_minimum_days,
        "impression_floor": policy.activity_floor_minimum_impressions,
        "click_floor": policy.sample_floor_minimum_clicks,
        "purchase_floor": policy.evidence_floor_minimum_conversions,
        "spend_floor": policy.spend_floor_minimum_daily_spend,
        "bootstrap_prng_version": policy.bootstrap_prng_algorithm,
        "bootstrap_draw_count": policy.bootstrap_draw_count,
        "bootstrap_min_valid_draw_ratio": policy.bootstrap_valid_draw_ratio,
        "fdr_limit": policy.bh_fdr_alpha,
        "quality_warning_permitted_codes": tuple(
            DataQualityIssueCode(c) for c in thresholds.get("quality_warning_permitted_codes", ())
        ),
        "streak_reset_reason_codes": tuple(thresholds.get("streak_reset_reason_codes", ())),
        "advice_construction_parameters": tuple(
            CanonicalKeyValue(key=k, value=v)
            for k, v in thresholds.get("advice_construction_parameters", ())
        ),
    }
    for field in _POLICY_JSON_DECIMAL_FIELDS:
        kwargs[field] = Decimal(str(thresholds[field]))
    for field in _POLICY_JSON_INT_FIELDS:
        kwargs[field] = int(thresholds[field])
    return ScalePolicySnapshot(**kwargs)


def _target_snapshot(target: ScaleTargetVersionRecord) -> ScaleTargetSnapshot:
    return ScaleTargetSnapshot(
        target_version_id=target.id,
        currency=target.currency,
        target_cpa=target.target_cpa,
        breakeven_roas=target.breakeven_roas,
        minimum_economic_spend=target.minimum_economic_spend or Decimal("0"),
        frequency_level_threshold_override=None,
        frequency_growth_threshold_override=None,
        ctr_decline_threshold_override=None,
        cvr_decline_threshold_override=None,
        cpm_increase_threshold_override=None,
        delivery_decline_threshold_override=None,
        cpa_worsening_threshold_override=None,
        roas_worsening_threshold_override=None,
    )


_BREAKDOWN_CONTRACT_TO_TYPE = {
    MetricContractType.BREAKDOWN_PLACEMENT: BreakdownType.PLACEMENT,
    MetricContractType.BREAKDOWN_COUNTRY: BreakdownType.COUNTRY,
    MetricContractType.BREAKDOWN_AGE_GENDER: BreakdownType.AGE_GENDER,
}
_BREAKDOWN_TYPE_TO_CONTRACT = {v: k for k, v in _BREAKDOWN_CONTRACT_TO_TYPE.items()}


def _daily_metrics(row) -> DailyMetricValues:
    return DailyMetricValues(
        impressions=row.impressions,
        link_clicks=row.link_clicks,
        purchases=Decimal(row.purchases),
        spend=row.spend,
        purchase_value=row.purchase_value,
        delivered=row.delivered,
    )


def _metric_contracts(
    members: list[ScaleMetricSemanticContractSetMemberRecord],
    *,
    connection_id: str,
    meta_mode: str,
    scenario_scope_key: str,
    timezone_name: str,
    currency: str,
) -> tuple[MetricContractRef, ...]:
    ordered = sorted(members, key=lambda m: m.contract_type)
    return tuple(
        MetricContractRef(
            contract_type=MetricContractType(m.contract_type),
            scale_contract_version="scale-contract-v1",
            observation_contract_generation=m.observation_contract_generation,
            observation_contract_version_id=m.observation_contract_generation,
            contract_hash=m.contract_hash,
            connection_id=connection_id,
            meta_mode=MetaMode(meta_mode),
            scenario_version_id=(
                None if scenario_scope_key == "__no_scenario__" else scenario_scope_key
            ),
            timezone_name=timezone_name,
            currency=currency,
        )
        for m in ordered
    )


def _contract_set_hash(contracts: tuple[MetricContractRef, ...]) -> str:
    return canonical_hash(
        tuple(c.contract_hash for c in contracts), domain=CONTRACT_SET_HASH_DOMAIN
    )


# ---------------------------------------------------------------------------
# seal_snapshot (Transaction A, spec §3.3)
# ---------------------------------------------------------------------------


def seal_snapshot(session: Session, context: TenantContext, run_id: str) -> str:
    """Seal or reuse the exact snapshot selected by the durable run.

    Scope for Task 10 (see module docstring): rejects a run whose frozen
    source manifest is not ``complete`` without mutating the run record
    (pitfall #2 -- the run's shape CHECK forbids a `blocked` run_state
    without a `snapshot_id`; terminalizing the run is Task 12's job).
    """
    run = session.execute(
        _for_update(
            session,
            select(ScaleEvaluationRunRecord).filter_by(
                workspace_id=context.workspace_id,
                meta_mode=context.meta_mode,
                id=run_id,
            ),
        )
    ).scalar_one_or_none()
    if run is None:
        raise SnapshotSealingError("run_not_found", run_id)

    if run.snapshot_id is not None:
        return run.snapshot_id

    manifest = session.execute(
        select(ScaleEvidenceSourceManifestRecord).filter_by(
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            id=run.source_manifest_id,
        )
    ).scalar_one_or_none()
    if manifest is None:
        raise SnapshotSealingError("manifest_not_found", run.source_manifest_id)
    if manifest.state != "complete":
        raise SnapshotSealingError("manifest_blocked", manifest.id)

    episode = session.execute(
        _for_update(
            session,
            select(ScaleEpisodeRecord).filter_by(
                workspace_id=context.workspace_id,
                meta_mode=context.meta_mode,
                id=run.episode_id,
            ),
        )
    ).scalar_one_or_none()
    if episode is None:
        raise SnapshotSealingError("episode_not_found", run.episode_id)

    enrollment = session.execute(
        _for_update(
            session,
            select(ScaleEnrollmentRecord).filter_by(
                workspace_id=context.workspace_id,
                meta_mode=context.meta_mode,
                id=run.enrollment_id,
            ),
        )
    ).scalar_one_or_none()
    if enrollment is None:
        raise SnapshotSealingError("enrollment_not_found", run.enrollment_id)

    policy = session.get(ScalePolicyVersionRecord, run.policy_version_id)
    if policy is None:
        raise SnapshotSealingError("policy_not_found", run.policy_version_id)

    target = None
    if run.target_version_id is not None:
        target = session.execute(
            select(ScaleTargetVersionRecord).filter_by(
                workspace_id=context.workspace_id, id=run.target_version_id,
            )
        ).scalar_one_or_none()
        if target is None:
            raise SnapshotSealingError("target_not_found", run.target_version_id)

    as_of = run.target_local_day
    current_start = as_of - timedelta(days=6)
    current_end = as_of + timedelta(days=1)
    baseline_start = current_start - timedelta(days=7)
    baseline_end = current_start

    freshness_reference_at = _ensure_utc(run.freshness_reference_at)
    last_sync_at = _ensure_utc(manifest.terminal_at)

    with session.no_autoflush:
        manifest_items = session.execute(
            select(ScaleManifestItemRecord).filter_by(manifest_id=manifest.id)
        ).scalars().all()
        item_by_key: dict[tuple, ScaleManifestItemRecord] = {
            (item.contract_type, item.window_role, item.local_day, item.segment_key): item
            for item in manifest_items
        }

        current_daily, quality_issues = _build_daily_window(
            session, item_by_key, current_start, current_end
        )
        baseline_daily, more_issues = _build_daily_window(
            session, item_by_key, baseline_start, baseline_end
        )
        quality_issues += more_issues

        current_exact, iss = _build_exact_window(
            session, item_by_key, WindowRole.CURRENT_7D
        )
        quality_issues += iss
        baseline_exact, iss = _build_exact_window(
            session, item_by_key, WindowRole.BASELINE_7D
        )
        quality_issues += iss
        rolling_exact, iss = _build_exact_window(
            session, item_by_key, WindowRole.ROLLING_14D
        )
        quality_issues += iss

        delivery_config, delivery_source = _build_delivery_config(session, item_by_key)
        if delivery_source is None:
            raise SnapshotSealingError("delivery_config_missing")

        breakdowns, breakdown_sources, iss = _build_breakdowns(session, item_by_key)
        quality_issues += iss

        for source_issue in session.execute(
            select(ScaleSourceQualityIssueRecord).filter_by(manifest_id=manifest.id)
        ).scalars().all():
            quality_issues.append(_quality_item_from_source_issue(source_issue))

        quality_issues = sorted(quality_issues, key=_quality_sort_key)

        contract_members = session.execute(
            select(ScaleMetricSemanticContractSetMemberRecord).filter_by(
                contract_set_id=episode.metric_semantic_contract_set_id
            )
        ).scalars().all()
        metric_contracts = _metric_contracts(
            contract_members,
            connection_id=episode.meta_connection_id,
            meta_mode=episode.meta_mode,
            scenario_scope_key=episode.scenario_scope_key,
            timezone_name=episode.timezone_name,
            currency=episode.currency,
        )
        metric_contract_set_hash = _contract_set_hash(metric_contracts)

        prior_streak, prior_absence_reason = _select_predecessor(
            session, context, episode.id, as_of,
        )

        policy_snapshot = _policy_snapshot(policy)
        target_snapshot = _target_snapshot(target) if target is not None else None
        normalized_target_key = target.id if target is not None else NO_TARGET_MARKER

        complete_days, active_days = _episode_day_counts(session, episode.id, as_of)
        data_continuity_generation = f"gen-{manifest.source_generation}"

        claim_payload = {
            "workspace_id": context.workspace_id,
            "meta_mode": context.meta_mode,
            "episode_id": episode.id,
            "target_local_day": as_of.isoformat(),
            "source_manifest_id": manifest.id,
            "source_revision": run.source_revision,
            "freshness_reference_at": freshness_reference_at.isoformat(),
            "policy_version_id": policy.id,
            "normalized_target_key": normalized_target_key,
            "semantic_event_sequence": run.semantic_event_sequence,
            "replay_generation": run.replay_generation,
            "predecessor_id": prior_streak.evaluation_id if prior_streak else None,
            "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
            "algorithm_version": run.algorithm_version,
        }
        claim_hash = canonical_hash(claim_payload, domain=CLAIM_HASH_DOMAIN)

        snapshot_id = new_id("sesn")
        value = ScaleEvidenceSnapshot(
            snapshot_id=snapshot_id,
            canonical_hash="",
            canonical_schema_version=CANONICAL_SCHEMA_VERSION,
            episode_id=episode.id,
            meta_mode=MetaMode(episode.meta_mode),
            meta_connection_id=episode.meta_connection_id,
            scenario_version_id=(
                None if episode.scenario_scope_key == "__no_scenario__" else episode.scenario_scope_key
            ),
            timezone_name=episode.timezone_name,
            currency=episode.currency,
            policy=policy_snapshot,
            target=target_snapshot,
            metric_contract_set_hash=metric_contract_set_hash,
            metric_contracts=metric_contracts,
            delivery_config=delivery_config,
            current_7d=MetricWindow(
                role=WindowRole.CURRENT_7D,
                start_date=current_start,
                end_date_exclusive=current_end,
                daily=current_daily,
            ),
            baseline_7d=MetricWindow(
                role=WindowRole.BASELINE_7D,
                start_date=baseline_start,
                end_date_exclusive=baseline_end,
                daily=baseline_daily,
            ),
            current_exact_7d=current_exact,
            baseline_exact_7d=baseline_exact,
            rolling_exact_14d=rolling_exact,
            breakdowns=breakdowns,
            data_quality_issues=tuple(quality_issues),
            complete_local_days_in_episode=complete_days,
            active_delivery_days_in_episode=active_days,
            data_continuity_generation=data_continuity_generation,
            prior_streak=prior_streak,
            prior_streak_absence_reason=prior_absence_reason,
            last_complete_local_day=manifest.last_complete_local_day or as_of,
            last_sync_at=last_sync_at,
            freshness_reference_at=freshness_reference_at,
            as_of_local_day=as_of,
        )
        computed_hash = _snapshot_hash(value)

        snapshot_row = ScaleEvidenceSnapshotRecord(
            id=snapshot_id,
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            meta_connection_id=episode.meta_connection_id,
            scenario_scope_key=episode.scenario_scope_key,
            ad_account_id=episode.ad_account_id,
            episode_id=episode.id,
            remote_binding_key=enrollment.remote_binding_key,
            claim_hash=claim_hash,
            source_manifest_id=manifest.id,
            source_manifest_hash=manifest.canonical_hash,
            source_revision=run.source_revision,
            safe_watermark=manifest.safe_watermark,
            policy_version_id=policy.id,
            target_version_id=target.id if target is not None else None,
            normalized_target_key=normalized_target_key,
            algorithm_version=run.algorithm_version,
            canonical_schema_version=CANONICAL_SCHEMA_VERSION,
            metric_semantic_contract_set_id=episode.metric_semantic_contract_set_id,
            metric_semantic_contract_set_hash=metric_contract_set_hash,
            delivery_config_source_id=delivery_source.id,
            delivery_evidence_hash=delivery_source.evidence_hash,
            delivery_comparability_hash=delivery_source.comparability_hash,
            timezone_name=episode.timezone_name,
            currency=episode.currency,
            attribution_semantics_hash=episode.attribution_semantics_hash,
            current_window_start_local_day=current_start,
            current_window_end_local_day=current_end,
            baseline_window_start_local_day=baseline_start,
            baseline_window_end_local_day=baseline_end,
            rolling_window_start_local_day=baseline_start,
            rolling_window_end_local_day=current_end,
            complete_local_days_in_episode=complete_days,
            active_delivery_days_in_episode=active_days,
            data_continuity_generation=data_continuity_generation,
            **_prior_streak_columns(prior_streak, prior_absence_reason),
            last_complete_local_day=manifest.last_complete_local_day or as_of,
            last_sync_at=last_sync_at,
            freshness_reference_at=freshness_reference_at,
            as_of_local_day=as_of,
            # Inserted UNSEALED (spec §3.3 step 7): the PostgreSQL guard
            # triggers on the child tables (added by the Task 6 migration)
            # reject a child INSERT once the parent's sealed_at IS NOT NULL,
            # and the FK forces the parent row to exist before any child can
            # reference it -- so an already-sealed parent would block every
            # one of its own children. `canonical_hash`/`sealed_at` are set
            # by a separate final UPDATE below, once every child is in.
            canonical_hash=None,
            sealed_at=None,
        )

        child_rows: list[Any] = []
        child_rows.extend(
            _source_link_rows(snapshot_id, context, item_by_key)
        )
        child_rows.extend(
            _quality_issue_rows(snapshot_id, context, quality_issues)
        )
        seg_rows, daily_rows = _breakdown_child_rows(
            snapshot_id, context, breakdowns, breakdown_sources
        )
        child_rows.extend(seg_rows)
        child_rows.extend(daily_rows)

    try:
        with session.begin_nested():
            # Step 1: insert the unsealed parent alone and flush, so it is
            # visible to the child-table guard triggers as unsealed before
            # any child is inserted. A claim-hash collision here means a
            # concurrent sealer already claimed this exact semantic request.
            session.add(snapshot_row)
            session.flush()

            # Step 2: insert every child now that the parent exists unsealed.
            session.add_all(child_rows)
            session.flush()

            # Step 3: seal as the final mutation (spec §3.3 step 7). A
            # sealed-content collision here means a concurrent sealer already
            # produced byte-identical content under a different claim hash.
            snapshot_row.canonical_hash = computed_hash
            snapshot_row.sealed_at = utcnow()
            session.flush()
    except IntegrityError:
        existing = session.execute(
            select(ScaleEvidenceSnapshotRecord).filter_by(
                workspace_id=context.workspace_id,
                meta_mode=context.meta_mode,
                claim_hash=claim_hash,
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = session.execute(
                select(ScaleEvidenceSnapshotRecord).filter_by(
                    workspace_id=context.workspace_id,
                    meta_mode=context.meta_mode,
                    canonical_schema_version=CANONICAL_SCHEMA_VERSION,
                    canonical_hash=computed_hash,
                )
            ).scalar_one_or_none()
        if existing is None or existing.sealed_at is None:
            raise
        snapshot_row = existing

    run.snapshot_id = snapshot_row.id
    run.run_state = "snapshot_sealed"
    run.updated_at = utcnow()

    if episode.phase_run_id is not None and episode.phase_run_id != run.id:
        raise SnapshotSealingError("episode_phase_conflict", episode.id)
    episode.phase = "evaluating"
    episode.phase_run_id = run.id
    episode.revision += 1

    session.flush()
    return snapshot_row.id


def _quality_sort_key(item: DataQualityItem):
    locator = item.locator
    return (
        item.impact.value,
        item.code.value,
        (
            locator.contract_type.value,
            locator.window_role.value if locator.window_role else "",
            locator.local_date.isoformat() if locator.local_date else "",
            locator.breakdown_type.value if locator.breakdown_type else "",
            locator.segment_key or "",
            locator.source_evidence_hash,
        ),
        tuple((d.key, str(d.value)) for d in item.details),
    )


def _build_daily_window(
    session: Session,
    item_by_key: dict[tuple, ScaleManifestItemRecord],
    start: date,
    end_exclusive: date,
) -> tuple[tuple[DailyObservation, ...], list[DataQualityItem]]:
    days: list[DailyObservation] = []
    issues: list[DataQualityItem] = []
    d = start
    while d < end_exclusive:
        item = item_by_key.get((MetricContractType.DAILY.value, None, d, None))
        if item is not None and item.state == "fulfilled":
            row = session.get(ScaleDailyMetricSourceRecord, item.source_record_id)
            days.append(
                DailyObservation(
                    local_date=d,
                    evidence_state=DailyEvidenceState.PRESENT,
                    metrics=_daily_metrics(row),
                )
            )
        else:
            days.append(
                DailyObservation(
                    local_date=d, evidence_state=DailyEvidenceState.MISSING, metrics=None,
                )
            )
            issues.append(
                DataQualityItem(
                    code=DataQualityIssueCode.DAILY_SLOT_MISSING,
                    impact=DataQualityImpact.BLOCKING,
                    locator=DataQualityLocator(
                        contract_type=MetricContractType.DAILY,
                        window_role=None,
                        local_date=d,
                        breakdown_type=None,
                        segment_key=None,
                        source_evidence_hash="",
                    ),
                    details=(),
                )
            )
        d += timedelta(days=1)
    return tuple(days), issues


def _build_exact_window(
    session: Session,
    item_by_key: dict[tuple, ScaleManifestItemRecord],
    role: WindowRole,
) -> tuple[ExactWindowMetrics, list[DataQualityItem]]:
    item = item_by_key.get((MetricContractType.EXACT_WINDOW.value, role.value, None, None))
    if item is not None and item.state == "fulfilled":
        row = session.get(ScaleExactWindowSourceRecord, item.source_record_id)
        return (
            ExactWindowMetrics(
                role=role,
                start_date=row.window_start_local_day,
                end_date_exclusive=row.window_end_local_day,
                reach=row.reach,
                frequency=row.frequency,
                observation_contract_version_id=row.contract_generation_id,
                evidence_hash=row.raw_response_hash,
            ),
            [],
        )
    # Unavailable: the contract instance identity is still known from the
    # manifest item's declared window, but there is no per-instance
    # evidence -- reach/frequency are unavailable, never invented as zero.
    issue = DataQualityItem(
        code=DataQualityIssueCode.EXACT_WINDOW_VALUE_UNAVAILABLE,
        impact=DataQualityImpact.BLOCKING,
        locator=DataQualityLocator(
            contract_type=MetricContractType.EXACT_WINDOW,
            window_role=role,
            local_date=None,
            breakdown_type=None,
            segment_key=None,
            source_evidence_hash="",
        ),
        details=(),
    )
    return (
        ExactWindowMetrics(
            role=role,
            start_date=date(1970, 1, 1),
            end_date_exclusive=date(1970, 1, 1),
            reach=None,
            frequency=None,
            observation_contract_version_id="unavailable",
            evidence_hash="unavailable",
        ),
        [issue],
    )


def _build_delivery_config(
    session: Session, item_by_key: dict[tuple, ScaleManifestItemRecord],
) -> tuple[DeliveryConfigSnapshot | None, ScaleDeliveryConfigSourceRecord | None]:
    item = item_by_key.get(
        (MetricContractType.DELIVERY_CONFIGURATION.value, None, None, None)
    )
    if item is None or item.state != "fulfilled":
        return None, None
    row = session.get(ScaleDeliveryConfigSourceRecord, item.source_record_id)
    snapshot = DeliveryConfigSnapshot(
        baseline_configuration_hash=row.comparability_hash,
        current_configuration_hash=row.comparability_hash,
        effective_campaign_status=row.campaign_status,
        effective_ad_set_status=row.ad_set_status,
        effective_ad_status=row.ad_status,
        status_history=(),
        attribution_setting=row.attribution_setting,
        diagnostic_state=DeliveryDiagnosticState.NONE_,
        diagnostic_detail=(),
    )
    return snapshot, row


def _build_breakdowns(
    session: Session, item_by_key: dict[tuple, ScaleManifestItemRecord],
) -> tuple[
    tuple[BreakdownSegment, ...],
    dict[tuple[BreakdownType, str], ScaleBreakdownSourceRecord],
    list[DataQualityItem],
]:
    segments: list[BreakdownSegment] = []
    sources: dict[tuple[BreakdownType, str], ScaleBreakdownSourceRecord] = {}
    issues: list[DataQualityItem] = []

    for (contract_type, window_role, local_day, segment_key), item in item_by_key.items():
        try:
            contract_enum = MetricContractType(contract_type)
        except ValueError:
            continue
        breakdown_type = _BREAKDOWN_CONTRACT_TO_TYPE.get(contract_enum)
        if breakdown_type is None:
            continue
        if item.state != "fulfilled":
            issues.append(
                DataQualityItem(
                    code=DataQualityIssueCode.BREAKDOWN_SEGMENT_UNKNOWN,
                    impact=DataQualityImpact.WARNING,
                    locator=DataQualityLocator(
                        contract_type=contract_enum,
                        window_role=WindowRole.CURRENT_7D,
                        local_date=None,
                        breakdown_type=breakdown_type,
                        segment_key=segment_key,
                        source_evidence_hash="",
                    ),
                    details=(),
                )
            )
            continue

        row = session.get(ScaleBreakdownSourceRecord, item.source_record_id)
        sources[(breakdown_type, row.segment_key)] = row
        if row.state == "observed":
            children = session.execute(
                select(ScaleBreakdownDailyChildRecord)
                .filter_by(breakdown_source_id=row.id)
                .order_by(ScaleBreakdownDailyChildRecord.local_day)
            ).scalars().all()
            daily = tuple(
                DailyObservation(
                    local_date=c.local_day,
                    evidence_state=(
                        DailyEvidenceState.PRESENT
                        if c.state == "present"
                        else DailyEvidenceState.MISSING
                    ),
                    metrics=(_daily_metrics(c) if c.state == "present" else None),
                )
                for c in children
            )
            segments.append(
                BreakdownSegment(
                    breakdown_type=breakdown_type,
                    segment_key=row.segment_key,
                    evidence_state=BreakdownEvidenceState.OBSERVED,
                    daily=daily,
                    suppression_reason=None,
                )
            )
        else:
            privacy = session.execute(
                select(ScaleBreakdownPrivacyRecord).filter_by(breakdown_source_id=row.id)
            ).scalar_one_or_none()
            reason = (
                BreakdownSuppressionReason(privacy.suppression_reason)
                if privacy is not None
                else BreakdownSuppressionReason.API_PRIVACY_THRESHOLD
            )
            segments.append(
                BreakdownSegment(
                    breakdown_type=breakdown_type,
                    segment_key=row.segment_key,
                    evidence_state=BreakdownEvidenceState.PRIVACY_SUPPRESSED,
                    daily=None,
                    suppression_reason=reason,
                )
            )

    segments.sort(key=lambda s: (s.breakdown_type.value, s.segment_key))
    return tuple(segments), sources, issues


def _quality_item_from_source_issue(issue: ScaleSourceQualityIssueRecord) -> DataQualityItem:
    contract_type = (
        MetricContractType(issue.contract_type)
        if issue.contract_type
        else MetricContractType.DAILY
    )
    return DataQualityItem(
        code=DataQualityIssueCode(issue.code),
        impact=DataQualityImpact.BLOCKING if issue.is_blocking else DataQualityImpact.WARNING,
        locator=DataQualityLocator(
            contract_type=contract_type,
            window_role=WindowRole(issue.window_role) if issue.window_role else None,
            local_date=issue.local_day,
            breakdown_type=None,
            segment_key=issue.segment_key,
            source_evidence_hash="",
        ),
        details=(),
    )


def _episode_day_counts(session: Session, episode_id: str, as_of: date) -> tuple[int, int]:
    rows = session.execute(
        select(
            ScaleDailyMetricSourceRecord.local_day,
            ScaleDailyMetricSourceRecord.state,
            ScaleDailyMetricSourceRecord.delivered,
        ).filter(
            ScaleDailyMetricSourceRecord.episode_id == episode_id,
            ScaleDailyMetricSourceRecord.local_day <= as_of,
        )
    ).all()
    complete_days: dict[date, bool] = {}
    for local_day, state, delivered in rows:
        if state != "present":
            continue
        complete_days[local_day] = complete_days.get(local_day, False) or bool(delivered)
    complete = len(complete_days)
    active = sum(1 for delivered in complete_days.values() if delivered)
    return complete, active


def _select_predecessor(
    session: Session, context: TenantContext, episode_id: str, as_of: date,
) -> tuple[PriorStreakState | None, PriorStreakAbsenceReason | None]:
    row = session.execute(
        select(ScaleEvaluationRecord, ScaleEvidenceSnapshotRecord.as_of_local_day)
        .join(
            ScaleEvidenceSnapshotRecord,
            ScaleEvaluationRecord.snapshot_id == ScaleEvidenceSnapshotRecord.id,
        )
        .filter(
            ScaleEvaluationRecord.workspace_id == context.workspace_id,
            ScaleEvaluationRecord.meta_mode == context.meta_mode,
            ScaleEvaluationRecord.episode_id == episode_id,
            ScaleEvidenceSnapshotRecord.as_of_local_day < as_of,
        )
        .order_by(
            ScaleEvidenceSnapshotRecord.as_of_local_day.desc(),
            ScaleEvaluationRecord.created_at.desc(),
        )
    ).first()
    if row is None:
        return None, PriorStreakAbsenceReason.NO_EARLIER_DISTINCT_DAY

    evaluation, evaluated_local_day = row
    counters = session.execute(
        select(ScaleEvaluationStreakCounterRecord).filter_by(evaluation_id=evaluation.id)
    ).scalars().all()
    streak_counters = tuple(
        sorted(
            (StreakCounter(signal_key=c.signal_key, consecutive_hits=c.consecutive_hits) for c in counters),
            key=lambda c: c.signal_key,
        )
    )
    compatibility = StreakCompatibility(
        policy_version_id=evaluation.streak_compat_policy_version_id,
        target_version_id=evaluation.streak_compat_target_version_id,
        metric_contract_set_hash=evaluation.streak_compat_metric_contract_set_hash,
        configuration_baseline_hash=evaluation.streak_compat_configuration_baseline_hash,
        data_continuity_generation=evaluation.streak_compat_data_continuity_generation,
        canonical_hash=evaluation.streak_compat_canonical_hash,
    )
    prior = PriorStreakState(
        evaluation_id=evaluation.id,
        evaluated_local_day=evaluated_local_day,
        compatibility=compatibility,
        health=ScaleHealth(evaluation.health) if evaluation.health else None,
        healthy_evaluations=evaluation.streak_healthy_evaluations,
        counters=streak_counters,
    )
    return prior, None


def _prior_streak_columns(
    prior: PriorStreakState | None, absence_reason: PriorStreakAbsenceReason | None,
) -> dict[str, Any]:
    if prior is None:
        return dict(
            prior_evaluation_id=None,
            prior_evaluated_local_day=None,
            prior_streak_compat_policy_version_id=None,
            prior_streak_compat_target_version_id=None,
            prior_streak_compat_metric_contract_set_hash=None,
            prior_streak_compat_configuration_baseline_hash=None,
            prior_streak_compat_data_continuity_generation=None,
            prior_streak_compat_canonical_hash=None,
            prior_health=None,
            prior_healthy_evaluations=None,
            prior_streak_counters=None,
            prior_streak_absence_reason=absence_reason.value if absence_reason else None,
        )
    return dict(
        prior_evaluation_id=prior.evaluation_id,
        prior_evaluated_local_day=prior.evaluated_local_day,
        prior_streak_compat_policy_version_id=prior.compatibility.policy_version_id,
        prior_streak_compat_target_version_id=prior.compatibility.target_version_id,
        prior_streak_compat_metric_contract_set_hash=prior.compatibility.metric_contract_set_hash,
        prior_streak_compat_configuration_baseline_hash=prior.compatibility.configuration_baseline_hash,
        prior_streak_compat_data_continuity_generation=prior.compatibility.data_continuity_generation,
        prior_streak_compat_canonical_hash=prior.compatibility.canonical_hash,
        prior_health=prior.health.value if prior.health else None,
        prior_healthy_evaluations=prior.healthy_evaluations,
        prior_streak_counters=[
            {"signal_key": c.signal_key, "consecutive_hits": c.consecutive_hits}
            for c in prior.counters
        ],
        prior_streak_absence_reason=None,
    )


def _source_link_rows(
    snapshot_id: str, context: TenantContext, item_by_key: dict[tuple, ScaleManifestItemRecord],
) -> list[ScaleEvidenceSourceLinkRecord]:
    rows = []
    for (contract_type, window_role, local_day, segment_key), item in item_by_key.items():
        if item.state != "fulfilled":
            continue
        rows.append(
            ScaleEvidenceSourceLinkRecord(
                id=new_id("sesl"),
                workspace_id=context.workspace_id,
                meta_mode=context.meta_mode,
                snapshot_id=snapshot_id,
                contract_type=contract_type,
                window_role=window_role,
                local_day=local_day,
                segment_key=segment_key,
                source_record_id=item.source_record_id,
            )
        )
    return rows


def _quality_issue_rows(
    snapshot_id: str, context: TenantContext, issues: list[DataQualityItem],
) -> list[ScaleSnapshotQualityIssueRecord]:
    rows = []
    for item in issues:
        loc = item.locator
        rows.append(
            ScaleSnapshotQualityIssueRecord(
                id=new_id("snqi"),
                workspace_id=context.workspace_id,
                meta_mode=context.meta_mode,
                snapshot_id=snapshot_id,
                code=item.code.value,
                severity=item.impact.value,
                is_blocking=item.impact == DataQualityImpact.BLOCKING,
                contract_type=loc.contract_type.value if loc.contract_type else None,
                window_role=loc.window_role.value if loc.window_role else None,
                local_day=loc.local_date,
                segment_key=loc.segment_key,
            )
        )
    return rows


def _breakdown_child_rows(
    snapshot_id: str,
    context: TenantContext,
    breakdowns: tuple[BreakdownSegment, ...],
    sources: dict[tuple[BreakdownType, str], ScaleBreakdownSourceRecord],
) -> tuple[list[ScaleSnapshotBreakdownSegmentRecord], list[ScaleSnapshotBreakdownDailyRecord]]:
    seg_rows = []
    daily_rows = []
    for seg in breakdowns:
        source_row = sources[(seg.breakdown_type, seg.segment_key)]
        seg_id = new_id("ssbs")
        seg_rows.append(
            ScaleSnapshotBreakdownSegmentRecord(
                id=seg_id,
                workspace_id=context.workspace_id,
                meta_mode=context.meta_mode,
                snapshot_id=snapshot_id,
                breakdown_type=seg.breakdown_type.value,
                segment_key=seg.segment_key,
                state=seg.evidence_state.value,
                suppression_reason=(
                    seg.suppression_reason.value if seg.suppression_reason else None
                ),
                source_record_id=source_row.id,
            )
        )
        if seg.daily is not None:
            for d in seg.daily:
                if d.evidence_state == DailyEvidenceState.PRESENT:
                    m = d.metrics
                    daily_rows.append(
                        ScaleSnapshotBreakdownDailyRecord(
                            id=new_id("ssbd"),
                            workspace_id=context.workspace_id,
                            meta_mode=context.meta_mode,
                            breakdown_segment_id=seg_id,
                            local_day=d.local_date,
                            state="present",
                            delivered=m.delivered,
                            spend=m.spend,
                            impressions=m.impressions,
                            link_clicks=m.link_clicks,
                            purchases=m.purchases,
                            purchase_value=m.purchase_value,
                        )
                    )
                else:
                    daily_rows.append(
                        ScaleSnapshotBreakdownDailyRecord(
                            id=new_id("ssbd"),
                            workspace_id=context.workspace_id,
                            meta_mode=context.meta_mode,
                            breakdown_segment_id=seg_id,
                            local_day=d.local_date,
                            state="missing",
                        )
                    )
    return seg_rows, daily_rows


# ---------------------------------------------------------------------------
# assemble_snapshot (read-consistent assembly, spec §3.3 / §2.9)
# ---------------------------------------------------------------------------


def assemble_snapshot(
    session: Session, context: TenantContext, snapshot_id: str,
) -> ScaleEvidenceSnapshot:
    """Load and verify one sealed snapshot as immutable value objects."""
    snapshot_row = session.execute(
        select(ScaleEvidenceSnapshotRecord).filter_by(
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            id=snapshot_id,
        )
    ).scalar_one_or_none()
    if snapshot_row is None:
        raise SnapshotAssemblyError("snapshot_not_found", snapshot_id)
    if snapshot_row.sealed_at is None or snapshot_row.canonical_hash is None:
        raise SnapshotAssemblyError("snapshot_not_sealed", snapshot_id)

    episode = session.execute(
        select(ScaleEpisodeRecord).filter_by(
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            id=snapshot_row.episode_id,
        )
    ).scalar_one_or_none()
    if episode is None:
        raise SnapshotAssemblyError("provenance_mismatch", "episode_not_found")

    if (
        episode.workspace_id != snapshot_row.workspace_id
        or episode.meta_mode != snapshot_row.meta_mode
        or episode.meta_connection_id != snapshot_row.meta_connection_id
        or episode.scenario_scope_key != snapshot_row.scenario_scope_key
        or episode.timezone_name != snapshot_row.timezone_name
        or episode.currency != snapshot_row.currency
        or episode.metric_semantic_contract_set_id != snapshot_row.metric_semantic_contract_set_id
    ):
        raise SnapshotAssemblyError("provenance_mismatch", snapshot_id)

    links = session.execute(
        select(ScaleEvidenceSourceLinkRecord).filter_by(snapshot_id=snapshot_id)
    ).scalars().all()
    link_by_key = {
        (link.contract_type, link.window_role, link.local_day, link.segment_key): link
        for link in links
    }

    current_start = snapshot_row.current_window_start_local_day
    current_end = snapshot_row.current_window_end_local_day
    baseline_start = snapshot_row.baseline_window_start_local_day
    baseline_end = snapshot_row.baseline_window_end_local_day

    try:
        current_daily = _assemble_daily_window(session, link_by_key, current_start, current_end)
        baseline_daily = _assemble_daily_window(session, link_by_key, baseline_start, baseline_end)
        current_exact = _assemble_exact_window(session, link_by_key, WindowRole.CURRENT_7D)
        baseline_exact = _assemble_exact_window(session, link_by_key, WindowRole.BASELINE_7D)
        rolling_exact = _assemble_exact_window(session, link_by_key, WindowRole.ROLLING_14D)
        delivery_config = _assemble_delivery_config(session, link_by_key)
    except SnapshotAssemblyError:
        raise
    except Exception as exc:  # defensive: any missing-child lookup failure
        raise SnapshotAssemblyError("missing_child", str(exc)) from exc

    quality_rows = session.execute(
        select(ScaleSnapshotQualityIssueRecord).filter_by(snapshot_id=snapshot_id)
    ).scalars().all()
    data_quality_issues = tuple(
        sorted(
            (_quality_item_from_snapshot_issue(r) for r in quality_rows),
            key=_quality_sort_key,
        )
    )

    breakdowns = _assemble_breakdowns(session, snapshot_id)

    contract_members = session.execute(
        select(ScaleMetricSemanticContractSetMemberRecord).filter_by(
            contract_set_id=snapshot_row.metric_semantic_contract_set_id
        )
    ).scalars().all()
    metric_contracts = _metric_contracts(
        contract_members,
        connection_id=snapshot_row.meta_connection_id,
        meta_mode=snapshot_row.meta_mode,
        scenario_scope_key=snapshot_row.scenario_scope_key,
        timezone_name=snapshot_row.timezone_name,
        currency=snapshot_row.currency,
    )

    policy = session.get(ScalePolicyVersionRecord, snapshot_row.policy_version_id)
    if policy is None:
        raise SnapshotAssemblyError("provenance_mismatch", "policy_not_found")
    policy_snapshot = _policy_snapshot(policy)

    target_snapshot = None
    if snapshot_row.target_version_id is not None:
        target = session.execute(
            select(ScaleTargetVersionRecord).filter_by(
                workspace_id=context.workspace_id, id=snapshot_row.target_version_id,
            )
        ).scalar_one_or_none()
        if target is None:
            raise SnapshotAssemblyError("provenance_mismatch", "target_not_found")
        target_snapshot = _target_snapshot(target)

    prior_streak, prior_absence_reason = _assemble_prior_streak(snapshot_row)

    value = ScaleEvidenceSnapshot(
        snapshot_id=snapshot_row.id,
        canonical_hash="",
        canonical_schema_version=snapshot_row.canonical_schema_version,
        episode_id=snapshot_row.episode_id,
        meta_mode=MetaMode(snapshot_row.meta_mode),
        meta_connection_id=snapshot_row.meta_connection_id,
        scenario_version_id=(
            None
            if snapshot_row.scenario_scope_key == "__no_scenario__"
            else snapshot_row.scenario_scope_key
        ),
        timezone_name=snapshot_row.timezone_name,
        currency=snapshot_row.currency,
        policy=policy_snapshot,
        target=target_snapshot,
        metric_contract_set_hash=snapshot_row.metric_semantic_contract_set_hash,
        metric_contracts=metric_contracts,
        delivery_config=delivery_config,
        current_7d=MetricWindow(
            role=WindowRole.CURRENT_7D,
            start_date=current_start,
            end_date_exclusive=current_end,
            daily=current_daily,
        ),
        baseline_7d=MetricWindow(
            role=WindowRole.BASELINE_7D,
            start_date=baseline_start,
            end_date_exclusive=baseline_end,
            daily=baseline_daily,
        ),
        current_exact_7d=current_exact,
        baseline_exact_7d=baseline_exact,
        rolling_exact_14d=rolling_exact,
        breakdowns=breakdowns,
        data_quality_issues=data_quality_issues,
        complete_local_days_in_episode=snapshot_row.complete_local_days_in_episode,
        active_delivery_days_in_episode=snapshot_row.active_delivery_days_in_episode,
        data_continuity_generation=snapshot_row.data_continuity_generation,
        prior_streak=prior_streak,
        prior_streak_absence_reason=prior_absence_reason,
        last_complete_local_day=snapshot_row.last_complete_local_day,
        last_sync_at=_ensure_utc(snapshot_row.last_sync_at),
        freshness_reference_at=_ensure_utc(snapshot_row.freshness_reference_at),
        as_of_local_day=snapshot_row.as_of_local_day,
    )

    recomputed_hash = _snapshot_hash(value)
    if not hmac.compare_digest(recomputed_hash, snapshot_row.canonical_hash):
        raise SnapshotAssemblyError("hash_mismatch", snapshot_id)

    return replace(value, canonical_hash=recomputed_hash)


def _assemble_daily_window(
    session: Session, link_by_key: dict[tuple, ScaleEvidenceSourceLinkRecord], start: date, end_exclusive: date,
) -> tuple[DailyObservation, ...]:
    days = []
    d = start
    while d < end_exclusive:
        link = link_by_key.get((MetricContractType.DAILY.value, None, d, None))
        if link is None:
            days.append(
                DailyObservation(local_date=d, evidence_state=DailyEvidenceState.MISSING, metrics=None)
            )
        else:
            row = session.get(ScaleDailyMetricSourceRecord, link.source_record_id)
            if row is None:
                raise SnapshotAssemblyError("missing_child", f"daily:{d.isoformat()}")
            days.append(
                DailyObservation(
                    local_date=d, evidence_state=DailyEvidenceState.PRESENT, metrics=_daily_metrics(row),
                )
            )
        d += timedelta(days=1)
    return tuple(days)


def _assemble_exact_window(
    session: Session, link_by_key: dict[tuple, ScaleEvidenceSourceLinkRecord], role: WindowRole,
) -> ExactWindowMetrics:
    link = link_by_key.get((MetricContractType.EXACT_WINDOW.value, role.value, None, None))
    if link is None:
        return ExactWindowMetrics(
            role=role,
            start_date=date(1970, 1, 1),
            end_date_exclusive=date(1970, 1, 1),
            reach=None,
            frequency=None,
            observation_contract_version_id="unavailable",
            evidence_hash="unavailable",
        )
    row = session.get(ScaleExactWindowSourceRecord, link.source_record_id)
    if row is None:
        raise SnapshotAssemblyError("missing_child", f"exact_window:{role.value}")
    return ExactWindowMetrics(
        role=role,
        start_date=row.window_start_local_day,
        end_date_exclusive=row.window_end_local_day,
        reach=row.reach,
        frequency=row.frequency,
        observation_contract_version_id=row.contract_generation_id,
        evidence_hash=row.raw_response_hash,
    )


def _assemble_delivery_config(
    session: Session, link_by_key: dict[tuple, ScaleEvidenceSourceLinkRecord],
) -> DeliveryConfigSnapshot:
    link = link_by_key.get((MetricContractType.DELIVERY_CONFIGURATION.value, None, None, None))
    if link is None:
        raise SnapshotAssemblyError("missing_child", "delivery_configuration")
    row = session.get(ScaleDeliveryConfigSourceRecord, link.source_record_id)
    if row is None:
        raise SnapshotAssemblyError("missing_child", "delivery_configuration")
    return DeliveryConfigSnapshot(
        baseline_configuration_hash=row.comparability_hash,
        current_configuration_hash=row.comparability_hash,
        effective_campaign_status=row.campaign_status,
        effective_ad_set_status=row.ad_set_status,
        effective_ad_status=row.ad_status,
        status_history=(),
        attribution_setting=row.attribution_setting,
        diagnostic_state=DeliveryDiagnosticState.NONE_,
        diagnostic_detail=(),
    )


def _assemble_breakdowns(session: Session, snapshot_id: str) -> tuple[BreakdownSegment, ...]:
    seg_rows = session.execute(
        select(ScaleSnapshotBreakdownSegmentRecord).filter_by(snapshot_id=snapshot_id)
    ).scalars().all()
    segments = []
    for seg in seg_rows:
        breakdown_type = BreakdownType(seg.breakdown_type)
        if seg.state == "observed":
            children = session.execute(
                select(ScaleSnapshotBreakdownDailyRecord)
                .filter_by(breakdown_segment_id=seg.id)
                .order_by(ScaleSnapshotBreakdownDailyRecord.local_day)
            ).scalars().all()
            daily = tuple(
                DailyObservation(
                    local_date=c.local_day,
                    evidence_state=(
                        DailyEvidenceState.PRESENT if c.state == "present" else DailyEvidenceState.MISSING
                    ),
                    metrics=_daily_metrics(c) if c.state == "present" else None,
                )
                for c in children
            )
            segments.append(
                BreakdownSegment(
                    breakdown_type=breakdown_type,
                    segment_key=seg.segment_key,
                    evidence_state=BreakdownEvidenceState.OBSERVED,
                    daily=daily,
                    suppression_reason=None,
                )
            )
        else:
            segments.append(
                BreakdownSegment(
                    breakdown_type=breakdown_type,
                    segment_key=seg.segment_key,
                    evidence_state=BreakdownEvidenceState.PRIVACY_SUPPRESSED,
                    daily=None,
                    suppression_reason=BreakdownSuppressionReason(seg.suppression_reason),
                )
            )
    segments.sort(key=lambda s: (s.breakdown_type.value, s.segment_key))
    return tuple(segments)


def _quality_item_from_snapshot_issue(row: ScaleSnapshotQualityIssueRecord) -> DataQualityItem:
    return DataQualityItem(
        code=DataQualityIssueCode(row.code),
        impact=DataQualityImpact(row.severity),
        locator=DataQualityLocator(
            contract_type=(
                MetricContractType(row.contract_type) if row.contract_type else MetricContractType.DAILY
            ),
            window_role=WindowRole(row.window_role) if row.window_role else None,
            local_date=row.local_day,
            breakdown_type=None,
            segment_key=row.segment_key,
            source_evidence_hash="",
        ),
        details=(),
    )


def _assemble_prior_streak(
    snapshot_row: ScaleEvidenceSnapshotRecord,
) -> tuple[PriorStreakState | None, PriorStreakAbsenceReason | None]:
    if snapshot_row.prior_evaluation_id is None:
        return None, (
            PriorStreakAbsenceReason(snapshot_row.prior_streak_absence_reason)
            if snapshot_row.prior_streak_absence_reason
            else None
        )
    counters = tuple(
        sorted(
            (
                StreakCounter(signal_key=c["signal_key"], consecutive_hits=c["consecutive_hits"])
                for c in (snapshot_row.prior_streak_counters or [])
            ),
            key=lambda c: c.signal_key,
        )
    )
    compatibility = StreakCompatibility(
        policy_version_id=snapshot_row.prior_streak_compat_policy_version_id,
        target_version_id=snapshot_row.prior_streak_compat_target_version_id,
        metric_contract_set_hash=snapshot_row.prior_streak_compat_metric_contract_set_hash,
        configuration_baseline_hash=snapshot_row.prior_streak_compat_configuration_baseline_hash,
        data_continuity_generation=snapshot_row.prior_streak_compat_data_continuity_generation,
        canonical_hash=snapshot_row.prior_streak_compat_canonical_hash,
    )
    prior = PriorStreakState(
        evaluation_id=snapshot_row.prior_evaluation_id,
        evaluated_local_day=snapshot_row.prior_evaluated_local_day,
        compatibility=compatibility,
        health=ScaleHealth(snapshot_row.prior_health) if snapshot_row.prior_health else None,
        healthy_evaluations=snapshot_row.prior_healthy_evaluations,
        counters=counters,
    )
    return prior, None
