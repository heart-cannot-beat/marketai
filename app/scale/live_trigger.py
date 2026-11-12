"""Live Scale evaluations from complete source manifests (#110, #101 S4).

The collector (:mod:`app.scale.collector`) writes one immutable source
manifest per ``active`` Enrollment's current Episode once a connection's
observation batch for target day D is terminal. For a ``complete`` manifest
it calls :func:`trigger_live_evaluation` in the same transaction, so the
manifest, the run and the ``scale_evaluate`` Job commit together or not at
all. A retried collection finds its manifest by the collection key and
triggers nothing a second time. A ``blocked`` manifest gets no run: it
cannot be sealed (contract §18), and only moves the readiness projection.

In order:

1. **Baseline.** Activation cannot read the ad's configuration and freezes a
   synthetic baseline (``app.scale.enrollment.is_unverified_baseline``).
   The first complete manifest of such an Episode adopts its configuration
   source as the baseline -- before any snapshot is sealed, and only when
   the read's creative is the one the DeliveryTarget named -- so the first
   evaluation compares two real reads instead of closing an unchanged
   Episode as ``delivery_configuration_changed``. A different creative is
   not adopted: that evaluation rolls the Episode over (contract §4.3).
2. **The day.** D must have ended in the account's time zone when the
   batch finished, and must be the Episode's latest complete day (#101
   Q9). A manifest older than that -- an earlier day than one already
   collected, or than the projection cursor -- gets a ``backfill`` run
   instead (#113, #101 S7): sealed and evaluated like any run, but it
   neither takes the Episode's phase nor moves its projection, streaks or
   Advice, and it notifies no one (contract §18). It freezes the same
   Policy and Target a live run would, and skips the stuck-run check below,
   which is about the phase.
3. **A stuck run** (#101 Q10). When the run holding the Episode's phase is
   unfinished and none of its Jobs can still run, it is enqueued again
   (``scale-eval:{run_id}:{algorithm_version}:redrive-{n}``), twice at
   most. After that a critical ``scale_evaluation`` alert goes out and the
   Episode gets no new run until the held one is resolved.
4. **The Policy** is the latest published one that permits the mode
   (``mode_permissions[mode] is True``) and whose algorithm is the deployed
   :data:`~app.scale.evaluator.CURRENT_ALGORITHM_VERSION` (#101 Q8). The
   Target is the Episode context's active one, or none.
5. ``request_scale_evaluation`` (``trigger_kind="live"``, the manifest's
   ``terminal_at`` as freshness reference, the batch's last sync run as
   trigger) and ``enqueue_scale_evaluation``.

A run that finds the Episode still held by the previous day's run waits:
Phase A defers it and its Job retries (``ScaleEvaluationDeferred``).

ADR 0017: this module reads and writes Scale rows and the Job queue only.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.persistence.models import JobRecord
from app.platform.audit import append_event
from app.platform.ops_alerts import AlertCategory, Severity, emit as emit_alert
from app.platform.tenancy import TenantContext
from app.scale.enrollment import is_unverified_baseline
from app.scale.evaluator import CURRENT_ALGORITHM_VERSION
from app.scale.models import (
    ScaleDeliveryConfigSourceRecord,
    ScaleEnrollmentRecord,
    ScaleEpisodeRecord,
    ScaleEvaluationRunRecord,
    ScaleEvidenceSnapshotRecord,
    ScaleEvidenceSourceManifestRecord,
    ScaleManifestItemRecord,
    ScalePolicyVersionRecord,
    ScaleTargetVersionRecord,
)
from app.scale.service import (
    BACKFILL_TRIGGER_KIND,
    ScaleEvaluationRequest,
    enqueue_scale_evaluation,
    evaluation_job_key,
    request_scale_evaluation,
)
# The one place the active Target of an exact context is read, keyed like
# its unique index; reused rather than duplicated (as service.py reuses
# enrollment's Episode builders).
from app.scale.targets import _active_targets

__all__ = [
    "LIVE_TRIGGER_KIND",
    "MAX_REDRIVES",
    "LiveTrigger",
    "context_target",
    "eligible_live_policy",
    "trigger_live_evaluation",
]

LIVE_TRIGGER_KIND = "live"
#: How many times a stuck run is enqueued again before the Episode stops
#: getting new runs (#101 Q10).
MAX_REDRIVES = 2
#: Job states after which a Worker may still run the Job.
_LIVE_JOB_STATES = ("queued", "retry_wait", "leased", "running")
_RUN_RESOURCE = "scale_evaluation_run"

DAY_NOT_ENDED = "day_not_ended"
PHASE_RUN_STUCK = "phase_run_stuck"
NO_ELIGIBLE_POLICY = "no_eligible_policy"
CREATIVE_DIFFERS = "creative_differs"


@dataclass(frozen=True)
class LiveTrigger:
    """What one complete manifest set in motion."""

    run_id: str | None = None
    job_id: str | None = None
    skipped: str | None = None
    redriven_run_id: str | None = None
    baseline_adopted: bool = False
    #: ``live``, or ``backfill`` for a manifest older than the Episode's latest.
    trigger_kind: str | None = None


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def trigger_live_evaluation(
    session: Session,
    context: TenantContext,
    *,
    enrollment: ScaleEnrollmentRecord,
    episode: ScaleEpisodeRecord,
    manifest: ScaleEvidenceSourceManifestRecord,
    triggering_sync_run_id: str | None,
) -> LiveTrigger:
    """Request and enqueue the live run of one complete manifest, or say why not.

    Runs in the collector's transaction, with ``episode`` locked and
    ``manifest`` just written; it commits nothing itself.
    """
    if manifest.state != "complete":
        return LiveTrigger(skipped="manifest_blocked")
    adopted = _adopt_unverified_baseline(session, context, episode, manifest)

    terminal_day = _as_utc(manifest.terminal_at).astimezone(ZoneInfo(episode.timezone_name)).date()
    if manifest.target_local_day >= terminal_day:
        return _skip(session, episode, manifest, DAY_NOT_ENDED, adopted)
    if _is_historical(session, context, episode, manifest):
        return _request_backfill(
            session, context, enrollment=enrollment, episode=episode, manifest=manifest,
            triggering_sync_run_id=triggering_sync_run_id, adopted=adopted,
        )

    redriven, stuck = _redrive_stuck_run(session, context, episode)
    if stuck:
        return _skip(session, episode, manifest, PHASE_RUN_STUCK, adopted, redriven)

    policy = eligible_live_policy(session, context.meta_mode)
    if policy is None:
        return _skip(session, episode, manifest, NO_ELIGIBLE_POLICY, adopted, redriven)
    target = context_target(session, episode)

    run_id = request_scale_evaluation(session, context, ScaleEvaluationRequest(
        enrollment_id=enrollment.id,
        episode_id=episode.id,
        target_local_day=manifest.target_local_day,
        trigger_kind=LIVE_TRIGGER_KIND,
        source_manifest_id=manifest.id,
        source_manifest_hash=manifest.canonical_hash,
        source_revision=manifest.source_revision,
        # The persisted value, never the clock: it is part of the run's
        # semantic key, so a retry names the same run (#101 §4).
        freshness_reference_at=manifest.terminal_at,
        policy_version_id=policy.id,
        algorithm_version=CURRENT_ALGORITHM_VERSION,
        target_version_id=target.id if target is not None else None,
        triggering_sync_run_id=triggering_sync_run_id,
        original_initiator_id=enrollment.original_initiator_id,
    ))
    job = enqueue_scale_evaluation(session, context, run_id)
    append_event(
        session,
        event_type="scale.live_evaluation.requested",
        resource_type=_RUN_RESOURCE,
        resource_id=run_id,
        details={
            "episode_id": episode.id,
            "enrollment_id": enrollment.id,
            "source_manifest_id": manifest.id,
            "target_local_day": manifest.target_local_day.isoformat(),
            "policy_version_id": policy.id,
            "target_version_id": target.id if target is not None else None,
            "triggering_sync_run_id": triggering_sync_run_id,
            "job_id": job.id,
        },
    )
    session.flush()
    return LiveTrigger(run_id=run_id, job_id=job.id, redriven_run_id=redriven, baseline_adopted=adopted,
                       trigger_kind=LIVE_TRIGGER_KIND)


def context_target(session: Session, episode: ScaleEpisodeRecord) -> ScaleTargetVersionRecord | None:
    """The active Target of the Episode's exact context, or ``None``: the one
    a run requested now freezes. A Target belongs to no mode."""
    targets = _active_targets(
        session, episode.workspace_id, episode.product_version_id, episode.offer_version_id,
        episode.market_code, episode.currency,
    )
    return targets[0] if targets else None


def eligible_live_policy(session: Session, meta_mode: str | None) -> ScalePolicyVersionRecord | None:
    """The Policy a live run freezes (#101 Q8): the latest published one that
    permits ``meta_mode`` and runs the deployed algorithm, or ``None``.

    ``mode_permissions`` maps a mode to ``True`` when it may evaluate under
    the Policy; a mode it does not name is not permitted.
    """
    rows = session.scalars(
        select(ScalePolicyVersionRecord)
        .where(
            ScalePolicyVersionRecord.is_published.is_(True),
            ScalePolicyVersionRecord.algorithm_version == CURRENT_ALGORITHM_VERSION,
        )
        .order_by(
            ScalePolicyVersionRecord.published_at.desc(),
            ScalePolicyVersionRecord.created_at.desc(),
            ScalePolicyVersionRecord.id.desc(),
        )
    )
    for policy in rows:
        permissions = policy.mode_permissions
        if isinstance(permissions, dict) and permissions.get(meta_mode) is True:
            return policy
    return None


def _skip(
    session: Session,
    episode: ScaleEpisodeRecord,
    manifest: ScaleEvidenceSourceManifestRecord,
    reason: str,
    adopted: bool,
    redriven: str | None = None,
) -> LiveTrigger:
    append_event(
        session,
        event_type="scale.live_evaluation.skipped",
        resource_type="scale_evidence_source_manifest",
        resource_id=manifest.id,
        details={
            "episode_id": episode.id,
            "target_local_day": manifest.target_local_day.isoformat(),
            "reason": reason,
        },
    )
    session.flush()
    return LiveTrigger(skipped=reason, redriven_run_id=redriven, baseline_adopted=adopted)


# ---------------------------------------------------------------------------
# 1. The unverified baseline
# ---------------------------------------------------------------------------


def _adopt_unverified_baseline(
    session: Session,
    context: TenantContext,
    episode: ScaleEpisodeRecord,
    manifest: ScaleEvidenceSourceManifestRecord,
) -> bool:
    """Make the manifest's configuration source the Episode's baseline, once.

    Only an Episode still frozen to activation's synthetic baseline, with no
    snapshot sealed against it yet, whose real read shows the creative the
    DeliveryTarget named. The synthetic source stays where it is.
    """
    baseline = session.scalar(
        select(ScaleDeliveryConfigSourceRecord).where(
            ScaleDeliveryConfigSourceRecord.workspace_id == context.workspace_id,
            ScaleDeliveryConfigSourceRecord.meta_mode == context.meta_mode,
            ScaleDeliveryConfigSourceRecord.id == episode.delivery_config_baseline_source_id,
        )
    )
    if baseline is None or not is_unverified_baseline(baseline):
        return False
    sealed = session.scalar(
        select(ScaleEvidenceSnapshotRecord.id).where(
            ScaleEvidenceSnapshotRecord.workspace_id == context.workspace_id,
            ScaleEvidenceSnapshotRecord.meta_mode == context.meta_mode,
            ScaleEvidenceSnapshotRecord.episode_id == episode.id,
        ).limit(1)
    )
    if sealed is not None:
        return False
    observed = session.scalar(
        select(ScaleDeliveryConfigSourceRecord)
        .join(ScaleManifestItemRecord, ScaleManifestItemRecord.source_record_id == ScaleDeliveryConfigSourceRecord.id)
        .where(
            ScaleManifestItemRecord.workspace_id == context.workspace_id,
            ScaleManifestItemRecord.meta_mode == context.meta_mode,
            ScaleManifestItemRecord.manifest_id == manifest.id,
            ScaleManifestItemRecord.contract_type == "delivery_configuration",
            ScaleManifestItemRecord.state == "fulfilled",
            ScaleDeliveryConfigSourceRecord.workspace_id == context.workspace_id,
            ScaleDeliveryConfigSourceRecord.meta_mode == context.meta_mode,
        )
    )
    if observed is None:
        return False
    details = {
        "source_manifest_id": manifest.id,
        "unverified_source_id": baseline.id,
        "observed_source_id": observed.id,
        "unverified_comparability_hash": episode.delivery_comparability_hash,
        "observed_comparability_hash": observed.comparability_hash,
    }
    if observed.ad_creative_id != baseline.ad_creative_id:
        # Another creative than the one activated: not this Episode's
        # baseline. The first evaluation rolls the Episode over (§4.3).
        append_event(
            session,
            event_type="scale.episode.baseline_not_adopted",
            resource_type="scale_episode",
            resource_id=episode.id,
            details={**details, "reason": CREATIVE_DIFFERS},
        )
        return False
    episode.delivery_config_baseline_source_id = observed.id
    episode.delivery_evidence_hash = observed.evidence_hash
    episode.delivery_comparability_hash = observed.comparability_hash
    episode.revision += 1
    session.flush()
    append_event(
        session,
        event_type="scale.episode.baseline_verified",
        resource_type="scale_episode",
        resource_id=episode.id,
        details=details,
    )
    return True


# ---------------------------------------------------------------------------
# 2. A day older than the Episode's latest: backfill (#113)
# ---------------------------------------------------------------------------


def _is_historical(
    session: Session,
    context: TenantContext,
    episode: ScaleEpisodeRecord,
    manifest: ScaleEvidenceSourceManifestRecord,
) -> bool:
    """Whether a later complete day was collected for the Episode, or its
    projection cursor is already past this manifest."""
    later = session.scalar(
        select(ScaleEvidenceSourceManifestRecord.id).where(
            ScaleEvidenceSourceManifestRecord.workspace_id == context.workspace_id,
            ScaleEvidenceSourceManifestRecord.meta_mode == context.meta_mode,
            ScaleEvidenceSourceManifestRecord.episode_id == episode.id,
            ScaleEvidenceSourceManifestRecord.state == "complete",
            ScaleEvidenceSourceManifestRecord.target_local_day > manifest.target_local_day,
        ).limit(1)
    )
    if later is not None:
        return True
    return episode.projection_as_of_local_day is not None and (
        (manifest.target_local_day, manifest.source_revision)
        < (episode.projection_as_of_local_day, episode.projection_source_revision)
    )


def _request_backfill(
    session: Session,
    context: TenantContext,
    *,
    enrollment: ScaleEnrollmentRecord,
    episode: ScaleEpisodeRecord,
    manifest: ScaleEvidenceSourceManifestRecord,
    triggering_sync_run_id: str | None,
    adopted: bool,
) -> LiveTrigger:
    policy = eligible_live_policy(session, context.meta_mode)
    if policy is None:
        return _skip(session, episode, manifest, NO_ELIGIBLE_POLICY, adopted)
    target = context_target(session, episode)
    run_id = request_scale_evaluation(session, context, ScaleEvaluationRequest(
        enrollment_id=enrollment.id,
        episode_id=episode.id,
        target_local_day=manifest.target_local_day,
        trigger_kind=BACKFILL_TRIGGER_KIND,
        source_manifest_id=manifest.id,
        source_manifest_hash=manifest.canonical_hash,
        source_revision=manifest.source_revision,
        freshness_reference_at=manifest.terminal_at,
        policy_version_id=policy.id,
        algorithm_version=CURRENT_ALGORITHM_VERSION,
        target_version_id=target.id if target is not None else None,
        triggering_sync_run_id=triggering_sync_run_id,
        original_initiator_id=enrollment.original_initiator_id,
    ))
    job = enqueue_scale_evaluation(session, context, run_id)
    append_event(
        session,
        event_type="scale.backfill_evaluation.requested",
        resource_type=_RUN_RESOURCE,
        resource_id=run_id,
        details={
            "episode_id": episode.id,
            "enrollment_id": enrollment.id,
            "source_manifest_id": manifest.id,
            "target_local_day": manifest.target_local_day.isoformat(),
            "projection_as_of_local_day": (
                episode.projection_as_of_local_day.isoformat()
                if episode.projection_as_of_local_day is not None else None
            ),
            "policy_version_id": policy.id,
            "target_version_id": target.id if target is not None else None,
            "triggering_sync_run_id": triggering_sync_run_id,
            "job_id": job.id,
        },
    )
    session.flush()
    return LiveTrigger(run_id=run_id, job_id=job.id, baseline_adopted=adopted,
                       trigger_kind=BACKFILL_TRIGGER_KIND)


# ---------------------------------------------------------------------------
# 3. A stuck run (#101 Q10)
# ---------------------------------------------------------------------------


def _redrive_stuck_run(
    session: Session, context: TenantContext, episode: ScaleEpisodeRecord,
) -> tuple[str | None, bool]:
    """Enqueue the run holding the Episode again when nothing will finish it.

    Returns ``(redriven_run_id, stuck)``: the run enqueued again, and
    whether the Episode must get no new run because the held run is still
    stuck after :data:`MAX_REDRIVES` redrives.
    """
    if episode.phase_run_id is None:
        return None, False
    held = session.scalar(
        select(ScaleEvaluationRunRecord).where(
            ScaleEvaluationRunRecord.workspace_id == context.workspace_id,
            ScaleEvaluationRunRecord.meta_mode == context.meta_mode,
            ScaleEvaluationRunRecord.id == episode.phase_run_id,
        )
    )
    if held is None or held.outcome_kind is not None:
        return None, False
    jobs = list(session.execute(
        select(JobRecord.idempotency_key, JobRecord.state).where(
            JobRecord.workspace_id == context.workspace_id,
            JobRecord.resource_type == _RUN_RESOURCE,
            JobRecord.resource_id == held.id,
        )
    ))
    if any(state in _LIVE_JOB_STATES for _key, state in jobs):
        return None, False  # still running or waiting to: not stuck
    prefix = evaluation_job_key(held.id, held.algorithm_version) + ":redrive-"
    redrives = sum(1 for key, _state in jobs if key.startswith(prefix))
    details = {"episode_id": episode.id, "redrives": redrives, "target_local_day": held.target_local_day.isoformat()}
    if redrives >= MAX_REDRIVES:
        append_event(
            session,
            event_type="scale.evaluation_run.stuck",
            resource_type=_RUN_RESOURCE,
            resource_id=held.id,
            details=details,
        )
        emit_alert(
            Severity.CRITICAL,
            AlertCategory.SCALE_EVALUATION,
            "Scale evaluation run unfinished after its redrives; its Episode gets no new run",
            workspace_id=context.workspace_id,
            details={**details, "evaluation_run_id": held.id, "meta_mode": context.meta_mode},
        )
        return None, True
    job = enqueue_scale_evaluation(session, context, held.id, redrive=redrives + 1)
    append_event(
        session,
        event_type="scale.evaluation_run.redriven",
        resource_type=_RUN_RESOURCE,
        resource_id=held.id,
        details={**details, "redrive": redrives + 1, "job_id": job.id},
    )
    return held.id, False
