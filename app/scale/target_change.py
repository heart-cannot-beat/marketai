"""Re-evaluation after a Target change (#113, #101 S7).

Publishing or withdrawing a ScaleTargetVersion (``app.scale.targets``)
enqueues one ``scale_target_change`` Job in the command's own transaction
(:func:`enqueue_target_change`), keyed by the Target lifecycle event: the
publish event, which also stands for the withdrawal of the Target it
replaces, or the withdraw event. The API's role writes no Scale evaluation
state, and a Target belongs to no mode while every Episode does, so the
Worker does the rest (:func:`run_target_change`), mode by mode, each
Episode in its own transaction under its row lock:

1. **Affected Episodes.** The open, current Episode of every active or
   paused Enrollment of the Workspace whose ProductVersion, OfferVersion,
   market and currency are the Target's: the exact context a run reads its
   Target from (``app.scale.live_trigger.context_target``). Nothing in
   another context or Workspace is read.
2. **Supersede.** Advice formed under another Target than the context's
   Target now -- or under one, when there is none now -- no longer
   applies. It is superseded citing the Target event (contract §15.2, §18;
   spec §3.8: "Policy/Target events first supersede incompatible active
   Advice"), whether or not anything is re-evaluated. Evaluations and
   occurrences are never rewritten.
3. **Re-evaluate** an active Enrollment's Episode when its latest complete
   manifest finished no more than the Policy's freshness lag before the
   Target event (36 hours in every published Policy; the lag sealing checks
   a run's freshness reference against): one ``target_change`` run on that
   manifest, enqueued as ``scale_evaluate``. Older evidence waits for the
   next scheduled evaluation, which freezes the new Target like any live
   run. Skips are audited as ``scale.target_change_evaluation.skipped``
   with their reason.

The run freezes the context's Target as it is when the Job runs, not the one
the event named, so Jobs that run out of order still leave the newest
Target on the projection. Its semantic-event sequence is one past every
earlier run of the Episode and its projection cursor, so it wins the cursor
over the live run of the same manifest (``app.scale.service``), and its
freshness reference is the event's time (never before the manifest's
``terminal_at``), persisted, so a retry names the same run.

Replays add nothing: a run is found again by its Episode and Target event,
a superseded thread is no longer active, and the Job key names the event.
Only the skip audit is written again when a replayed Job finds the same
reason.

Policy-change re-evaluation and forward replay stay deferred (#113): a new
Policy is picked up by the next scheduled evaluation.

ADR 0017: this module reads and writes Scale rows and the Job queue only.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import JobRecord
from app.platform.audit import append_event
from app.platform.jobs import enqueue_in_session
from app.platform.runtime_role import WORKER_ROLE, runs_as
from app.platform.tenancy import VALID_META_MODES, TenantContext, tenant_scope
from app.scale.advice import supersede_for_target_change
from app.scale.evaluator import CURRENT_ALGORITHM_VERSION
from app.scale.live_trigger import context_target, eligible_live_policy
from app.scale.models import (
    NO_TARGET_MARKER,
    ScaleEnrollmentRecord,
    ScaleEpisodeRecord,
    ScaleEvaluationRunRecord,
    ScaleEvidenceSourceManifestRecord,
    ScaleTargetLifecycleEventRecord,
    ScaleTargetVersionRecord,
)
from app.scale.service import (
    TARGET_CHANGE_TRIGGER_KIND,
    ScaleEvaluationRequest,
    enqueue_scale_evaluation,
    request_scale_evaluation,
)

__all__ = [
    "TARGET_CHANGE_JOB_TYPE",
    "EpisodeTargetChange",
    "TargetChangeError",
    "TargetChangeFailed",
    "TargetEvent",
    "apply_target_change",
    "enqueue_target_change",
    "run_target_change",
    "target_change_job_input",
]

TARGET_CHANGE_JOB_TYPE = "scale_target_change"

#: Why an affected Episode got no ``target_change`` run.
ENROLLMENT_NOT_ACTIVE = "enrollment_not_active"
NO_COMPLETE_MANIFEST = "no_complete_manifest"
DAY_NOT_ENDED = "day_not_ended"
NO_ELIGIBLE_POLICY = "no_eligible_policy"
EVIDENCE_STALE = "evidence_stale"

_REEVALUATED_LIFECYCLES = ("active",)
_AFFECTED_LIFECYCLES = ("active", "paused")
_RUN_RESOURCE = "scale_evaluation_run"


class TargetChangeError(RuntimeError):
    """A ``scale_target_change`` Job that cannot run as given (``code`` is stable)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class TargetChangeFailed(RuntimeError):
    """Some Episodes were not handled; a retry handles them, and the ones
    already handled change nothing twice."""

    def __init__(self, failures: Mapping[str, str]) -> None:
        self.failures = dict(failures)
        detail = "; ".join(f"{key}: {reason}" for key, reason in sorted(self.failures.items()))
        super().__init__(f"{len(self.failures)} Episode(s) not handled: {detail}")


@dataclass(frozen=True)
class TargetEvent:
    """The Target lifecycle event a Job handles, and its Target's context,
    read once."""

    event_id: str
    event_type: str
    target_version_id: str
    product_version_id: str
    offer_version_id: str
    market_code: str
    currency: str
    occurred_at: datetime


@dataclass(frozen=True)
class EpisodeTargetChange:
    """What one Target event did to one Episode."""

    meta_mode: str
    enrollment_id: str
    episode_id: str
    superseded_thread_ids: tuple[str, ...] = ()
    run_id: str | None = None
    job_id: str | None = None
    skipped: str | None = None
    replayed: bool = False


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Enqueue (the Target command's transaction)
# ---------------------------------------------------------------------------


def enqueue_target_change(
    session: Session, context: TenantContext, event: ScaleTargetLifecycleEventRecord,
) -> JobRecord:
    """Enqueue the ``scale_target_change`` Job for one Target lifecycle event,
    in the caller's transaction, or return the Job its key already names.

    The payload names only the event: the Job serves every mode, and the
    Worker reads the Target's context from the event's Target.
    """
    with tenant_scope(session, context):
        return enqueue_in_session(
            session,
            context=context,
            job_type=TARGET_CHANGE_JOB_TYPE,
            payload={"target_event_id": event.id},
            idempotency_key=f"scale-target-change:{event.id}",
            resource_type="scale_target_version",
            resource_id=event.target_version_id,
        )


def target_change_job_input(payload: Mapping[str, Any]) -> str:
    """The Target lifecycle event a ``scale_target_change`` Job names, or
    ``missing_input``."""
    target_event_id = str(payload.get("target_event_id") or "")
    if not target_event_id:
        raise TargetChangeError("missing_input", "缺少 target_event_id")
    return target_event_id


# ---------------------------------------------------------------------------
# The Job
# ---------------------------------------------------------------------------


@runs_as(WORKER_ROLE)
def run_target_change(context: TenantContext, target_event_id: str) -> tuple[EpisodeTargetChange, ...]:
    """Handle one Target lifecycle event in every mode of the Job's Workspace.

    ``context`` is the claimed Job's; each mode gets its own copy of it.
    One Episode failing does not stop the others: they are all tried, then
    :class:`TargetChangeFailed` names the ones to retry.
    """
    modes = sorted(VALID_META_MODES)
    with tenant_session(replace(context, meta_mode=modes[0])) as session:
        event = _load_event(session, context, target_event_id)
    results: list[EpisodeTargetChange] = []
    failures: dict[str, str] = {}
    for mode in modes:
        mode_context = replace(context, meta_mode=mode)
        with tenant_session(mode_context) as session:
            enrollment_ids = _affected_enrollments(session, mode_context, event)
        for enrollment_id in enrollment_ids:
            try:
                with tenant_session(mode_context) as session:
                    outcome = apply_target_change(session, mode_context, event, enrollment_id)
            except Exception as exc:  # noqa: BLE001 -- one Episode must not stop the others
                failures[f"{mode}:{enrollment_id}"] = f"{type(exc).__name__}: {exc}"[:500]
                continue
            if outcome is not None:
                results.append(outcome)
    if failures:
        raise TargetChangeFailed(failures)
    return tuple(results)


def _load_event(session: Session, context: TenantContext, target_event_id: str) -> TargetEvent:
    row = session.execute(
        select(ScaleTargetLifecycleEventRecord, ScaleTargetVersionRecord)
        .join(ScaleTargetVersionRecord, ScaleTargetVersionRecord.id == ScaleTargetLifecycleEventRecord.target_version_id)
        .where(
            ScaleTargetLifecycleEventRecord.workspace_id == context.workspace_id,
            ScaleTargetLifecycleEventRecord.id == target_event_id,
            ScaleTargetVersionRecord.workspace_id == context.workspace_id,
        )
    ).one_or_none()
    if row is None:
        raise TargetChangeError("target_event_not_found", target_event_id)
    event, target = row
    return TargetEvent(
        event_id=event.id,
        event_type=event.event_type,
        target_version_id=target.id,
        product_version_id=target.product_version_id,
        offer_version_id=target.offer_version_id,
        market_code=target.market_code,
        currency=target.currency,
        occurred_at=_as_utc(event.created_at),
    )


def _affected_enrollments(session: Session, context: TenantContext, event: TargetEvent) -> list[str]:
    """The Enrollments whose current open Episode is in the event's exact context."""
    return list(session.scalars(
        select(ScaleEnrollmentRecord.id)
        .join(ScaleEpisodeRecord, ScaleEpisodeRecord.id == ScaleEnrollmentRecord.current_episode_id)
        .where(
            ScaleEnrollmentRecord.workspace_id == context.workspace_id,
            ScaleEnrollmentRecord.meta_mode == context.meta_mode,
            ScaleEnrollmentRecord.product_version_id == event.product_version_id,
            ScaleEnrollmentRecord.offer_version_id == event.offer_version_id,
            ScaleEnrollmentRecord.market_code == event.market_code,
            ScaleEnrollmentRecord.lifecycle.in_(_AFFECTED_LIFECYCLES),
            ScaleEpisodeRecord.workspace_id == context.workspace_id,
            ScaleEpisodeRecord.meta_mode == context.meta_mode,
            ScaleEpisodeRecord.currency == event.currency,
            ScaleEpisodeRecord.phase != "closed",
        )
        .order_by(ScaleEnrollmentRecord.id)
    ))


def apply_target_change(
    session: Session, context: TenantContext, event: TargetEvent, enrollment_id: str,
) -> EpisodeTargetChange | None:
    """Supersede and re-evaluate one Enrollment's current Episode, or say why
    not; ``None`` when it is no longer affected. Runs in the caller's
    transaction (``session`` is bound to ``context``) and commits nothing."""
    enrollment = session.scalar(
        select(ScaleEnrollmentRecord).where(
            ScaleEnrollmentRecord.workspace_id == context.workspace_id,
            ScaleEnrollmentRecord.meta_mode == context.meta_mode,
            ScaleEnrollmentRecord.id == enrollment_id,
        )
    )
    if (
        enrollment is None or enrollment.lifecycle not in _AFFECTED_LIFECYCLES
        or enrollment.current_episode_id is None
    ):
        return None
    episode = session.scalar(
        select(ScaleEpisodeRecord)
        .where(
            ScaleEpisodeRecord.workspace_id == context.workspace_id,
            ScaleEpisodeRecord.meta_mode == context.meta_mode,
            ScaleEpisodeRecord.id == enrollment.current_episode_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        episode is None or episode.phase == "closed"
        or (episode.product_version_id, episode.offer_version_id, episode.market_code, episode.currency)
        != (event.product_version_id, event.offer_version_id, event.market_code, event.currency)
    ):
        return None

    target = context_target(session, episode)
    target_key = target.id if target is not None else NO_TARGET_MARKER
    superseded = tuple(supersede_for_target_change(
        session, context, episode.id, target_key=target_key, semantic_event_id=event.event_id,
    ))
    found = dict(meta_mode=context.meta_mode, enrollment_id=enrollment.id, episode_id=episode.id,
                 superseded_thread_ids=superseded)

    existing = session.scalar(
        select(ScaleEvaluationRunRecord).where(
            ScaleEvaluationRunRecord.workspace_id == context.workspace_id,
            ScaleEvaluationRunRecord.meta_mode == context.meta_mode,
            ScaleEvaluationRunRecord.episode_id == episode.id,
            ScaleEvaluationRunRecord.trigger_kind == TARGET_CHANGE_TRIGGER_KIND,
            ScaleEvaluationRunRecord.triggering_semantic_event_id == event.event_id,
        )
    )
    if existing is not None:
        # A replayed event: the run is requested; its Job may not be.
        job = enqueue_scale_evaluation(session, context, existing.id)
        return EpisodeTargetChange(**found, run_id=existing.id, job_id=job.id, replayed=True)

    if enrollment.lifecycle not in _REEVALUATED_LIFECYCLES:
        return _skip(session, event, episode, ENROLLMENT_NOT_ACTIVE, found)
    manifest = session.scalar(
        select(ScaleEvidenceSourceManifestRecord)
        .where(
            ScaleEvidenceSourceManifestRecord.workspace_id == context.workspace_id,
            ScaleEvidenceSourceManifestRecord.meta_mode == context.meta_mode,
            ScaleEvidenceSourceManifestRecord.episode_id == episode.id,
            ScaleEvidenceSourceManifestRecord.state == "complete",
        )
        .order_by(
            ScaleEvidenceSourceManifestRecord.target_local_day.desc(),
            ScaleEvidenceSourceManifestRecord.source_revision.desc(),
        )
        .limit(1)
    )
    if manifest is None:
        return _skip(session, event, episode, NO_COMPLETE_MANIFEST, found)
    terminal_at = _as_utc(manifest.terminal_at)
    if manifest.target_local_day >= terminal_at.astimezone(ZoneInfo(episode.timezone_name)).date():
        # Its live run was skipped the same way (``app.scale.live_trigger``).
        return _skip(session, event, episode, DAY_NOT_ENDED, found, manifest=manifest)
    policy = eligible_live_policy(session, context.meta_mode)
    if policy is None:
        return _skip(session, event, episode, NO_ELIGIBLE_POLICY, found, manifest=manifest)
    lag = timedelta(hours=policy.freshness_floor_maximum_lag_hours)
    if event.occurred_at - terminal_at > lag:
        return _skip(session, event, episode, EVIDENCE_STALE, found, manifest=manifest,
                     lag_hours=policy.freshness_floor_maximum_lag_hours)

    sequence = 1 + max(
        episode.projection_semantic_event_sequence,
        session.scalar(
            select(func.max(ScaleEvaluationRunRecord.semantic_event_sequence)).where(
                ScaleEvaluationRunRecord.workspace_id == context.workspace_id,
                ScaleEvaluationRunRecord.meta_mode == context.meta_mode,
                ScaleEvaluationRunRecord.episode_id == episode.id,
            )
        ) or 0,
    )
    run_id = request_scale_evaluation(session, context, ScaleEvaluationRequest(
        enrollment_id=enrollment.id,
        episode_id=episode.id,
        target_local_day=manifest.target_local_day,
        trigger_kind=TARGET_CHANGE_TRIGGER_KIND,
        source_manifest_id=manifest.id,
        source_manifest_hash=manifest.canonical_hash,
        source_revision=manifest.source_revision,
        # Persisted values only: part of the run's semantic key.
        freshness_reference_at=max(event.occurred_at, terminal_at),
        policy_version_id=policy.id,
        algorithm_version=CURRENT_ALGORITHM_VERSION,
        target_version_id=target.id if target is not None else None,
        semantic_event_sequence=sequence,
        triggering_semantic_event_id=event.event_id,
        original_initiator_id=enrollment.original_initiator_id,
    ))
    job = enqueue_scale_evaluation(session, context, run_id)
    append_event(
        session,
        event_type="scale.target_change_evaluation.requested",
        resource_type=_RUN_RESOURCE,
        resource_id=run_id,
        details={
            "episode_id": episode.id,
            "enrollment_id": enrollment.id,
            "target_event_id": event.event_id,
            "target_event_type": event.event_type,
            "target_version_id": target.id if target is not None else None,
            "source_manifest_id": manifest.id,
            "target_local_day": manifest.target_local_day.isoformat(),
            "semantic_event_sequence": sequence,
            "policy_version_id": policy.id,
            "superseded_thread_ids": list(superseded),
            "job_id": job.id,
        },
    )
    session.flush()
    return EpisodeTargetChange(**found, run_id=run_id, job_id=job.id)


def _skip(
    session: Session,
    event: TargetEvent,
    episode: ScaleEpisodeRecord,
    reason: str,
    found: dict,
    *,
    manifest: ScaleEvidenceSourceManifestRecord | None = None,
    lag_hours: int | None = None,
) -> EpisodeTargetChange:
    details: dict[str, Any] = {
        "enrollment_id": episode.enrollment_id,
        "target_event_id": event.event_id,
        "target_event_type": event.event_type,
        "reason": reason,
        "superseded_thread_ids": list(found["superseded_thread_ids"]),
    }
    if manifest is not None:
        details["source_manifest_id"] = manifest.id
        details["manifest_terminal_at"] = _as_utc(manifest.terminal_at).isoformat()
    if lag_hours is not None:
        details["freshness_lag_hours"] = lag_hours
    append_event(
        session,
        event_type="scale.target_change_evaluation.skipped",
        resource_type="scale_episode",
        resource_id=episode.id,
        details=details,
    )
    session.flush()
    return EpisodeTargetChange(**found, skipped=reason)
