"""Advice lifecycle, conversion, and disposition/snooze (spec §3.5-§3.6).

This module owns everything downstream of the pure evaluator's
``AdviceCandidate`` output: thread identity resolution, occurrence
append, eligible-miss thread resolution, supersession, conversion to a
Generation ``GenerationDraft`` or a Scale-owned ``ScaleTestPlanDraft``,
append-only disposition/snooze, and freshness expiry.

Scoping decisions made for Task 13 (documented so a later task can extend
this deliberately rather than by surprise):

* **Miss-streak tracking has no dedicated column.** The Task 5 schema
  (``app.scale.models``) gives ``ScaleAdviceThreadRecord`` no running
  miss-counter or freshness-deadline field, and this task's file scope is
  ``app/scale/advice.py`` plus its tests -- not a models.py change (which
  would also need a Task 6 Alembic migration). ``lifecycle_reason`` is a
  free-text, nullable column that is otherwise unused while a thread is
  ``active`` (it only carries a real terminal reason once resolved); this
  module reuses it as scratch state -- ``"miss_streak=<n>:<local_day>"`` --
  while the thread stays active, and overwrites it with the real terminal
  reason on transition. This keeps the two-eligible-distinct-day-miss rule
  exactly recomputable without inventing persisted state elsewhere.
* **Notification decisions are out of scope.** Spec §3.5 explicitly calls
  for generalizing the existing guardrail-shaped outbox writer so Scale
  does not fill guardrail-only columns with dummy values -- that is a
  separate cross-cutting change, not one of this task's six required
  functions, and not in its required test list. ``ScaleAdviceNotification
  DecisionRecord`` is left for that later integration.
* **The freshness-deadline job is delayed to the deadline (#111).**
  ``expire_advice_thread`` is the complete, directly callable,
  lock-recheck handler; ``enqueue_advice_expiry`` enqueues its
  ``scale_advice_expire`` Job with the thread's Meta mode in the payload
  (#97) and ``not_before`` set to the deadline, and
  ``advice_expire_job_context`` reads the mode back for the Worker.
  ``app.scale.service``'s Phase C calls ``enqueue_episode_advice_expiries``
  once its run is ``evaluated``, in the transaction that wrote the thread
  changes. ``expire_overdue_connection_advice`` is the fallback, run by the
  daily source-collection Job (#109) after its Episode loop: it expires an
  active thread past its deadline whose Job a stale authorization epoch
  rejected, or whose Job ran while the Worker's clock was behind the
  database's.
* **Episode-close supersession is applied lazily, at recurrence.** The
  ``ScaleAdviceThreadEventRecord`` CHECK constraint
  (``ck_sate_superseded_shape``) requires a non-null ``successor_thread_id``
  on every ``superseded`` event. At the moment an Episode closes there is
  usually no successor yet. This module instead supersedes a still-active
  thread from a closed Episode the moment a new Episode's advice
  processing recurs the same ``(advice_type, reason_signature)`` identity
  for the same Enrollment -- the new thread is a real, concrete successor,
  which satisfies the constraint and matches "recurrence creates a new
  thread generation linked to the predecessor thread." A thread whose
  finding never recurs is instead eventually terminated by
  ``expire_advice_thread`` once its freshness deadline passes (a `expired`
  event, which the CHECK does not constrain this way).
* **Semantic-identity-change supersession** (Policy/Target/algorithm/
  metric-contract change) is applied when this same processing pass also
  resolves a same-``advice_type`` successor thread under the new
  semantics. A **Target** change needs no successor (#113): the Target
  lifecycle event that made the thread inapplicable is its trigger
  (``semantic_event_id``, which ``ck_sate_superseded_shape`` accepts in
  place of a successor). :func:`supersede_for_target_change` applies it
  when the Worker handles the event (``app.scale.target_change``), and a
  ``target_change`` run's own pass applies it again to Advice an older
  run opened under the previous Target in the meantime. A Policy,
  algorithm or metric-contract change with no same-pass successor still
  leaves the stale thread to eligible-miss counting or freshness expiry
  (Policy-change reevaluation is deferred, #113).
* **`convert_occurrence_to_draft` calls the real, already-existing**
  ``app.generation.draft.create_draft`` (an in-process Generation-owned
  interface, matching spec intent that Scale never constructs a
  ``GenerationDraftRecord`` directly) rather than the spec's literal
  ``create_draft_from_scale_advice`` seam, which does not exist in the
  Generation module. Provider/model/combination-strategy default to
  fixed placeholders (``"scale_advice_default"``) since the original
  recipe snapshot is not reachable from Scale's own tables; a later task
  can extend ``ScaleAdviceDraftSeed`` and this call site once Generation
  exposes true recipe-lineage carry-forward.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import and_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import JobRecord, LibraryVersionRecord, new_id, utcnow
from app.generation.draft import create_draft
from app.platform.audit import append_event
from app.platform.jobs import enqueue_in_session
from app.platform.tenancy import VALID_META_MODES, TenantContext, commit_and_rebind, tenant_scope
from app.scale._canonical import canonical_encode, canonical_hash
from app.scale._contracts import AdviceCandidate
from app.scale.models import (
    ScaleAdviceConversionRecord,
    ScaleAdviceDispositionRecord,
    ScaleAdviceOccurrenceRecord,
    ScaleAdviceSnoozeRecord,
    ScaleAdviceThreadEventRecord,
    ScaleAdviceThreadRecord,
    ScaleEnrollmentRecord,
    ScaleEpisodeRecord,
    ScaleEvaluationAdviceCandidateRecord,
    ScaleEvaluationRecord,
    ScaleEvaluationRunRecord,
    ScalePolicyVersionRecord,
    ScaleTestPlanDraftRecord,
)
from app.scale.types import AdviceType, DataQualityState, EvidenceMaturity
from app.platform.runtime_role import WORKER_ROLE, runs_as

_SYSTEM_EXECUTOR = "system:scale_advice_lifecycle"
_FALLBACK_SWEEP_EXECUTOR = "system:scale_advice_fallback_sweep"
_RESOLVE_AFTER_ELIGIBLE_MISSES = 2
_SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
_MISS_STREAK_PREFIX = "miss_streak="
_DRAFT_SEED_DOMAIN = "scale-advice-draft-seed-v1"
_TEST_PLAN_LINEAGE_DOMAIN = "scale-test-plan-lineage-v1"

# Spec §3.6: "the allowed mapping from Meta evidence mode to Generation
# execution mode." Not given a literal table in the read sections; this is
# this task's concretization -- documented so a later task can correct it
# deliberately.
_EXECUTION_MODE_BY_META_MODE = {
    "customer_sandbox": "sandbox",
    "controlled_mock": "mock",
    "internal_validation": "sandbox",
}


class AdviceLifecycleError(RuntimeError):
    """Deterministic, stably-coded rejection raised by this module."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class ScaleAdviceDraftSeed:
    """Frozen lineage this module carries across the Scale/Generation seam
    (spec §3.6). A reduced concretization of the spec's full seed -- Winner
    decision/confirmation, Asset/Product/Offer/Landing-Page versions, and
    market -- limited to what is reachable from an Enrollment/Occurrence
    without a deeper cross-module read than this task's scope allows."""

    occurrence_id: str
    thread_id: str
    evaluation_id: str
    snapshot_id: str
    enrollment_id: str
    winner_decision_id: str
    winner_confirmation_id: str
    asset_version_id: str
    product_version_id: str
    offer_version_id: str
    market_code: str
    landing_page_version_id: str | None
    meta_mode: str
    refresh_axis: str | None


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _ensure_utc(value: datetime | None) -> datetime | None:
    """SQLite strips tzinfo on datetime round-trip (known pitfall); a value
    read back from the database must be re-attached to UTC before use."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _for_update(session: Session, stmt):
    """PostgreSQL row locking. SQLite (tests, bounded local path) has no
    ``FOR UPDATE`` and SQLAlchemy leaves it out there, but the statement
    keeps the lock, so the SQLite grant guard (#59) sees it."""
    return stmt.with_for_update()


def _require_meta_mode(context: TenantContext) -> str:
    if context.meta_mode is None:
        raise AdviceLifecycleError("meta_mode_required")
    return context.meta_mode


def _json_scalar(value):
    return json.loads(canonical_encode(value))


def _semantic_tuple(evaluation_row: ScaleEvaluationRecord) -> tuple:
    """The identity dimensions whose change makes an existing thread's
    finding semantically stale (spec §3.5: Policy, Target, algorithm, or
    metric-contract identity change)."""
    return (
        evaluation_row.policy_version_id,
        evaluation_row.normalized_target_key,
        evaluation_row.algorithm_version,
        evaluation_row.metric_semantic_contract_set_hash,
    )


def _parse_miss_streak(lifecycle_reason: str | None) -> tuple[int, date | None]:
    if not lifecycle_reason or not lifecycle_reason.startswith(_MISS_STREAK_PREFIX):
        return 0, None
    body = lifecycle_reason[len(_MISS_STREAK_PREFIX):]
    count_str, _, day_str = body.partition(":")
    try:
        return int(count_str), date.fromisoformat(day_str)
    except ValueError:
        return 0, None


def _format_miss_streak(count: int, day: date) -> str:
    return f"{_MISS_STREAK_PREFIX}{count}:{day.isoformat()}"


def _lock_episode(session: Session, context: TenantContext, meta_mode: str, episode_id: str) -> ScaleEpisodeRecord:
    episode = session.execute(
        _for_update(
            session,
            select(ScaleEpisodeRecord).filter_by(
                workspace_id=context.workspace_id, meta_mode=meta_mode, id=episode_id,
            ),
        )
    ).scalar_one_or_none()
    if episode is None:
        raise AdviceLifecycleError("episode_not_found", episode_id)
    return episode


def _lock_enrollment(
    session: Session, context: TenantContext, meta_mode: str, enrollment_id: str,
) -> ScaleEnrollmentRecord:
    enrollment = session.execute(
        _for_update(
            session,
            select(ScaleEnrollmentRecord).filter_by(
                workspace_id=context.workspace_id, meta_mode=meta_mode, id=enrollment_id,
            ),
        )
    ).scalar_one_or_none()
    if enrollment is None:
        raise AdviceLifecycleError("enrollment_not_found", enrollment_id)
    return enrollment


def _lock_thread(session: Session, context: TenantContext, meta_mode: str, thread_id: str) -> ScaleAdviceThreadRecord:
    thread = session.execute(
        _for_update(
            session,
            select(ScaleAdviceThreadRecord).filter_by(
                workspace_id=context.workspace_id, meta_mode=meta_mode, id=thread_id,
            ),
        )
    ).scalar_one_or_none()
    if thread is None:
        raise AdviceLifecycleError("thread_not_found", thread_id)
    return thread


def _lock_occurrence(
    session: Session, context: TenantContext, meta_mode: str, occurrence_id: str,
) -> ScaleAdviceOccurrenceRecord:
    occurrence = session.execute(
        _for_update(
            session,
            select(ScaleAdviceOccurrenceRecord).filter_by(
                workspace_id=context.workspace_id, meta_mode=meta_mode, id=occurrence_id,
            ),
        )
    ).scalar_one_or_none()
    if occurrence is None:
        raise AdviceLifecycleError("occurrence_not_found", occurrence_id)
    return occurrence


def _load_active_threads(
    session: Session, context: TenantContext, meta_mode: str, episode_id: str,
) -> dict[tuple[str, str], ScaleAdviceThreadRecord]:
    rows = session.execute(
        _for_update(
            session,
            select(ScaleAdviceThreadRecord).filter_by(
                workspace_id=context.workspace_id, meta_mode=meta_mode,
                episode_id=episode_id, lifecycle="active",
            ),
        )
    ).scalars().all()
    return {(row.advice_type, row.reason_signature): row for row in rows}


# ---------------------------------------------------------------------------
# process_advice_candidates -- spec §3.5
# ---------------------------------------------------------------------------


@runs_as(WORKER_ROLE)
def process_advice_candidates(
    session: Session,
    context: TenantContext,
    episode_id: str,
    evaluation_id: str,
    candidates: Sequence[AdviceCandidate],
    *,
    trigger_kind: str,
    evaluated_local_day: date,
    projection_eligible: bool,
    occurred_at: datetime | None = None,
    executor_id: str = _SYSTEM_EXECUTOR,
    commit: bool = True,
    semantic_event_id: str | None = None,
) -> list[str]:
    """Resolve every candidate to a thread, append occurrences, apply
    eligible-miss resolution and semantic-change/recurrence supersession.

    Returns the ordered, de-duplicated list of thread ids this call
    created, appended to, resolved, or superseded.

    ``commit=False`` leaves the changes flushed in the caller's
    transaction. Phase C passes it so the thread changes, their expiry
    Jobs and the run's terminal state commit together (spec §3.4, #111).

    ``semantic_event_id`` is the Target lifecycle event a ``target_change``
    run re-evaluates for (#113): every remaining active thread formed under
    another Target than this Evaluation's is superseded by it, successor
    or not.
    """
    meta_mode = _require_meta_mode(context)
    occurred_at = _ensure_utc(occurred_at) or utcnow()

    episode = _lock_episode(session, context, meta_mode, episode_id)
    evaluation_row = session.execute(
        select(ScaleEvaluationRecord).filter_by(
            workspace_id=context.workspace_id, meta_mode=meta_mode, id=evaluation_id,
        )
    ).scalar_one_or_none()
    if evaluation_row is None:
        raise AdviceLifecycleError("evaluation_not_found", evaluation_id)
    if evaluation_row.episode_id != episode.id:
        raise AdviceLifecycleError("evaluation_episode_mismatch", evaluation_id)

    # Only accepted, projection-eligible, in-order live Evaluations that are
    # not warming/blocked participate in eligible-miss counting (spec
    # §3.5). Occurrence creation itself is unconditional on this -- every
    # AdviceCandidate the evaluator actually produced resolves a thread.
    is_live_eligible_day = (
        projection_eligible
        and trigger_kind == "live"
        and evaluation_row.maturity != EvidenceMaturity.WARMING.value
        and evaluation_row.data_quality_state != DataQualityState.BLOCKED.value
    )
    current_semantic = _semantic_tuple(evaluation_row)

    active_threads = _load_active_threads(session, context, meta_mode, episode_id)
    touched: set[tuple[str, str]] = set()
    touched_thread_ids: list[str] = []

    for candidate in candidates:
        key = (candidate.advice_type.value, candidate.reason_signature)

        candidate_row = session.execute(
            select(ScaleEvaluationAdviceCandidateRecord).filter_by(
                workspace_id=context.workspace_id, meta_mode=meta_mode,
                evaluation_id=evaluation_id, reason_signature=candidate.reason_signature,
            )
        ).scalar_one_or_none()
        if candidate_row is None:
            raise AdviceLifecycleError("candidate_not_persisted", candidate.reason_signature)

        existing_occurrence = session.execute(
            select(ScaleAdviceOccurrenceRecord).filter_by(
                workspace_id=context.workspace_id, meta_mode=meta_mode,
                evaluation_id=evaluation_id, advice_type=key[0], reason_signature=key[1],
            )
        ).scalar_one_or_none()
        if existing_occurrence is not None:
            # Idempotent replay of an already-processed evaluation.
            touched.add(key)
            touched_thread_ids.append(existing_occurrence.thread_id)
            continue

        thread = active_threads.get(key)
        occurrence_id = new_id("sao")
        evidence_json = [{"key": item.key, "value": _json_scalar(item.value)} for item in candidate.evidence]

        if thread is None:
            prior = session.execute(
                _for_update(
                    session,
                    select(ScaleAdviceThreadRecord)
                    .filter_by(
                        workspace_id=context.workspace_id, meta_mode=meta_mode,
                        enrollment_id=episode.enrollment_id,
                        advice_type=key[0], reason_signature=key[1],
                    )
                    .filter(ScaleAdviceThreadRecord.episode_id != episode_id)
                    .order_by(ScaleAdviceThreadRecord.created_at.desc())
                    .limit(1),
                )
            ).scalar_one_or_none()

            thread = ScaleAdviceThreadRecord(
                id=new_id("sat"),
                workspace_id=context.workspace_id,
                meta_mode=meta_mode,
                episode_id=episode_id,
                enrollment_id=episode.enrollment_id,
                advice_type=key[0],
                reason_signature=key[1],
                severity=candidate.severity.value,
                lifecycle="active",
                predecessor_thread_id=prior.id if prior is not None else None,
                first_occurrence_id=occurrence_id,
                latest_occurrence_id=occurrence_id,
            )
            session.add(thread)
            session.flush()
            active_threads[key] = thread

            session.add(ScaleAdviceThreadEventRecord(
                id=new_id("sate"),
                workspace_id=context.workspace_id,
                meta_mode=meta_mode,
                episode_id=episode_id,
                thread_id=thread.id,
                occurrence_id=occurrence_id,
                event_type="created",
                previous_lifecycle=None,
                current_lifecycle="active",
                successor_thread_id=None,
                reason=("recurrence" if prior is not None else None),
                deduplication_key=f"created:{thread.id}",
                executor_id=executor_id,
            ))

            # Lazy episode-close supersession: a prior thread with the same
            # identity, from a different (necessarily closed, since only
            # one Episode per Enrollment is ever open) Episode, that was
            # never superseded because it had no successor at the time --
            # this new thread is that successor.
            if prior is not None and prior.lifecycle == "active":
                prior.lifecycle = "superseded"
                prior.lifecycle_reason = "episode_closed_recurred"
                prior.revision += 1
                prior.updated_at = utcnow()
                session.add(ScaleAdviceThreadEventRecord(
                    id=new_id("sate"),
                    workspace_id=context.workspace_id,
                    meta_mode=meta_mode,
                    episode_id=prior.episode_id,
                    thread_id=prior.id,
                    occurrence_id=None,
                    event_type="superseded",
                    previous_lifecycle="active",
                    current_lifecycle="superseded",
                    successor_thread_id=thread.id,
                    reason="episode_closed_recurred",
                    deduplication_key=f"superseded:{prior.id}:{thread.id}",
                    executor_id=executor_id,
                ))
        else:
            thread.latest_occurrence_id = occurrence_id
            thread.revision += 1
            thread.updated_at = utcnow()
            thread.lifecycle_reason = None  # a hit resets any miss streak
            if _SEVERITY_RANK[candidate.severity.value] > _SEVERITY_RANK[thread.severity]:
                thread.severity = candidate.severity.value

        session.add(ScaleAdviceOccurrenceRecord(
            id=occurrence_id,
            workspace_id=context.workspace_id,
            meta_mode=meta_mode,
            episode_id=episode_id,
            enrollment_id=episode.enrollment_id,
            evaluation_id=evaluation_id,
            candidate_id=candidate_row.id,
            snapshot_id=evaluation_row.snapshot_id,
            thread_id=thread.id,
            advice_type=key[0],
            reason_signature=key[1],
            severity=candidate.severity.value,
            signal_ids=list(candidate.signal_ids),
            evidence=evidence_json,
            occurred_at=occurred_at,
        ))
        session.flush()

        touched.add(key)
        touched_thread_ids.append(thread.id)

    # Semantic-identity-change supersession: a still-active thread whose
    # latest occurrence was evaluated under a different Policy/Target/
    # algorithm/metric-contract identity than this evaluation is stale.
    # Superseded here only when this same pass resolved a same-advice_type
    # successor (the CHECK constraint requires a concrete successor).
    successor_by_type: dict[str, str] = {
        key[0]: active_threads[key].id for key in touched
    }
    for key, thread in list(active_threads.items()):
        if key in touched or thread.lifecycle != "active":
            continue
        latest_occurrence = session.get(ScaleAdviceOccurrenceRecord, thread.latest_occurrence_id)
        latest_eval = session.execute(
            select(ScaleEvaluationRecord).filter_by(
                workspace_id=context.workspace_id, meta_mode=meta_mode,
                id=latest_occurrence.evaluation_id,
            )
        ).scalar_one_or_none() if latest_occurrence is not None else None
        if latest_eval is None or _semantic_tuple(latest_eval) == current_semantic:
            continue
        successor_id = successor_by_type.get(key[0])
        if successor_id is None:
            continue
        thread.lifecycle = "superseded"
        thread.lifecycle_reason = "semantic_identity_changed"
        thread.revision += 1
        thread.updated_at = utcnow()
        session.add(ScaleAdviceThreadEventRecord(
            id=new_id("sate"), workspace_id=context.workspace_id, meta_mode=meta_mode,
            episode_id=episode_id, thread_id=thread.id, occurrence_id=None,
            event_type="superseded", previous_lifecycle="active", current_lifecycle="superseded",
            successor_thread_id=successor_id, reason="semantic_identity_changed",
            deduplication_key=f"superseded:{thread.id}:{successor_id}", executor_id=executor_id,
        ))
        touched_thread_ids.append(thread.id)

    # A Target change supersedes without a successor (#113): Advice an
    # older run opened under the previous Target after the event's own
    # pass (``supersede_for_target_change``) ran.
    if semantic_event_id is not None:
        touched_thread_ids.extend(_supersede_other_targets(
            session, context, meta_mode,
            [thread for key, thread in active_threads.items() if key not in touched],
            target_key=evaluation_row.normalized_target_key,
            semantic_event_id=semantic_event_id,
            executor_id=executor_id,
        ))

    # Eligible-miss counting for every remaining active thread this batch
    # did not touch or supersede.
    if is_live_eligible_day:
        for key, thread in list(active_threads.items()):
            if key in touched or thread.lifecycle != "active":
                continue
            streak, last_day = _parse_miss_streak(thread.lifecycle_reason)
            if last_day == evaluated_local_day:
                # Same-day Policy/Target reevaluation: never double-counts.
                continue
            streak += 1
            thread.revision += 1
            thread.updated_at = utcnow()
            if streak >= _RESOLVE_AFTER_ELIGIBLE_MISSES:
                thread.lifecycle = "resolved"
                thread.resolved_at = utcnow()
                thread.lifecycle_reason = "eligible_misses_resolved"
                session.add(ScaleAdviceThreadEventRecord(
                    id=new_id("sate"), workspace_id=context.workspace_id, meta_mode=meta_mode,
                    episode_id=episode_id, thread_id=thread.id, occurrence_id=None,
                    event_type="resolved", previous_lifecycle="active", current_lifecycle="resolved",
                    successor_thread_id=None, reason="two_eligible_misses",
                    deduplication_key=f"resolved:{thread.id}", executor_id=executor_id,
                ))
            else:
                thread.lifecycle_reason = _format_miss_streak(streak, evaluated_local_day)
            touched_thread_ids.append(thread.id)

    if commit:
        commit_and_rebind(session)
    else:
        session.flush()

    seen: set[str] = set()
    result: list[str] = []
    for thread_id in touched_thread_ids:
        if thread_id not in seen:
            seen.add(thread_id)
            result.append(thread_id)
    return result


# ---------------------------------------------------------------------------
# Target-change supersession -- contract §15.2, §18 (#113)
# ---------------------------------------------------------------------------


#: ``lifecycle_reason`` and thread-event ``reason`` of Advice a Target
#: change made inapplicable.
TARGET_CHANGED = "target_changed"


@runs_as(WORKER_ROLE)
def supersede_for_target_change(
    session: Session,
    context: TenantContext,
    episode_id: str,
    *,
    target_key: str,
    semantic_event_id: str,
    executor_id: str = _SYSTEM_EXECUTOR,
) -> list[str]:
    """Supersede the Episode's active Advice formed under another Target
    than ``target_key`` (a Target id, or ``NO_TARGET_MARKER``), citing the
    Target lifecycle event ``semantic_event_id``.

    Spec §3.8: "Policy/Target events first supersede incompatible active
    Advice". A thread's Target is the one its latest occurrence's
    Evaluation froze; every Advice identity includes it (its reason
    signature), so none survives a change. Old Evaluations and occurrences
    are not touched. Flushes into the caller's transaction, under the
    thread locks; a thread that is no longer active is left alone, so a
    replayed event supersedes nothing twice. Returns the superseded thread
    ids in thread-id order.
    """
    meta_mode = _require_meta_mode(context)
    threads = _load_active_threads(session, context, meta_mode, episode_id)
    superseded = _supersede_other_targets(
        session, context, meta_mode, sorted(threads.values(), key=lambda row: row.id),
        target_key=target_key, semantic_event_id=semantic_event_id, executor_id=executor_id,
    )
    session.flush()
    return superseded


def _supersede_other_targets(
    session: Session,
    context: TenantContext,
    meta_mode: str,
    threads: Sequence[ScaleAdviceThreadRecord],
    *,
    target_key: str,
    semantic_event_id: str,
    executor_id: str,
) -> list[str]:
    superseded: list[str] = []
    for thread in threads:
        if thread.lifecycle != "active":
            continue
        formed_under = _thread_target_key(session, context, meta_mode, thread)
        if formed_under is None or formed_under == target_key:
            continue
        thread.lifecycle = "superseded"
        thread.lifecycle_reason = TARGET_CHANGED
        thread.revision += 1
        thread.updated_at = utcnow()
        session.add(ScaleAdviceThreadEventRecord(
            id=new_id("sate"), workspace_id=context.workspace_id, meta_mode=meta_mode,
            episode_id=thread.episode_id, thread_id=thread.id, occurrence_id=None,
            event_type="superseded", previous_lifecycle="active", current_lifecycle="superseded",
            successor_thread_id=None, semantic_event_id=semantic_event_id, reason=TARGET_CHANGED,
            deduplication_key=f"superseded:{thread.id}:{semantic_event_id}", executor_id=executor_id,
        ))
        superseded.append(thread.id)
    return superseded


def _thread_target_key(
    session: Session, context: TenantContext, meta_mode: str, thread: ScaleAdviceThreadRecord,
) -> str | None:
    """The Target (or no-target marker) the thread's latest occurrence was
    evaluated under."""
    return session.execute(
        select(ScaleEvaluationRecord.normalized_target_key)
        .join(ScaleAdviceOccurrenceRecord, and_(
            ScaleAdviceOccurrenceRecord.evaluation_id == ScaleEvaluationRecord.id,
            ScaleAdviceOccurrenceRecord.workspace_id == ScaleEvaluationRecord.workspace_id,
            ScaleAdviceOccurrenceRecord.meta_mode == ScaleEvaluationRecord.meta_mode,
        ))
        .where(
            ScaleAdviceOccurrenceRecord.workspace_id == context.workspace_id,
            ScaleAdviceOccurrenceRecord.meta_mode == meta_mode,
            ScaleAdviceOccurrenceRecord.id == thread.latest_occurrence_id,
        )
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Conversion -- spec §3.6
# ---------------------------------------------------------------------------


def _recheck_conversion_eligibility(
    session: Session, context: TenantContext, meta_mode: str, occurrence: ScaleAdviceOccurrenceRecord,
) -> tuple[ScaleAdviceThreadRecord, ScaleEpisodeRecord, ScaleEnrollmentRecord]:
    thread = _lock_thread(session, context, meta_mode, occurrence.thread_id)
    if thread.lifecycle != "active":
        raise AdviceLifecycleError("thread_not_active", thread.lifecycle)
    episode = _lock_episode(session, context, meta_mode, occurrence.episode_id)
    if episode.phase == "closed":
        raise AdviceLifecycleError("episode_closed")
    enrollment = _lock_enrollment(session, context, meta_mode, occurrence.enrollment_id)
    if enrollment.lifecycle not in ("active", "paused"):
        raise AdviceLifecycleError("enrollment_not_actionable", enrollment.lifecycle)
    from app.marketing.evidence_handoff import scale_block_reason
    marketing_reason = scale_block_reason(session, context, enrollment.winner_decision_id)
    if marketing_reason:
        raise AdviceLifecycleError(marketing_reason)
    return thread, episode, enrollment


def _existing_conversion(
    session: Session, context: TenantContext, meta_mode: str, occurrence_id: str,
) -> ScaleAdviceConversionRecord | None:
    return session.execute(
        select(ScaleAdviceConversionRecord).filter_by(
            workspace_id=context.workspace_id, meta_mode=meta_mode, occurrence_id=occurrence_id,
        )
    ).scalar_one_or_none()


def _draft_product_id(session: Session, context: TenantContext, product_version_id: str) -> str:
    """The Product a refresh Draft belongs to.

    Winner lineage freezes the Library *ProductVersion*, while a
    GenerationDraft references the Product. ``create_draft`` resolves a
    Library ProductProfile entry id through
    ``products.product_profile_entry_id`` (or accepts a legacy Product id),
    but never a version id -- so hand it the version's entry.
    """
    version = session.execute(
        select(LibraryVersionRecord).filter_by(
            workspace_id=context.workspace_id, id=product_version_id,
        )
    ).scalar_one_or_none()
    return version.entry_id if version is not None else product_version_id


def _append_conversion_disposition(
    session: Session, context: TenantContext, meta_mode: str,
    thread: ScaleAdviceThreadRecord, occurrence_id: str, conversion_id: str, initiator_id: str,
) -> None:
    session.add(ScaleAdviceDispositionRecord(
        id=new_id("sad"),
        workspace_id=context.workspace_id,
        meta_mode=meta_mode,
        thread_id=thread.id,
        occurrence_id=occurrence_id,
        kind="converted_to_draft",
        linked_conversion_id=conversion_id,
        actor_id=initiator_id,
    ))
    thread.disposition_kind = "converted_to_draft"
    thread.disposition_at = utcnow()
    thread.revision += 1
    thread.updated_at = utcnow()


def convert_occurrence_to_draft(
    session: Session,
    context: TenantContext,
    occurrence_id: str,
    *,
    initiator_id: str,
    provider: str = "scale_advice_default",
    model: str = "scale_advice_default",
    refresh_axis: str | None = None,
) -> str:
    """`refresh_creative` -> an unpaid editing GenerationDraft (spec §3.6).

    Returns the `ScaleAdviceConversionRecord` id. Reusing this call for the
    same occurrence -- whether concurrently or with a fresh Idempotency-Key
    -- always returns the same winning conversion; a losing concurrent
    Draft is rolled back with it.
    """
    meta_mode = _require_meta_mode(context)
    occurrence = _lock_occurrence(session, context, meta_mode, occurrence_id)
    if occurrence.advice_type != AdviceType.REFRESH_CREATIVE.value:
        raise AdviceLifecycleError("wrong_advice_type", occurrence.advice_type)

    existing = _existing_conversion(session, context, meta_mode, occurrence_id)
    if existing is not None:
        return existing.id

    thread, episode, enrollment = _recheck_conversion_eligibility(session, context, meta_mode, occurrence)

    seed = ScaleAdviceDraftSeed(
        occurrence_id=occurrence.id,
        thread_id=thread.id,
        evaluation_id=occurrence.evaluation_id,
        snapshot_id=occurrence.snapshot_id,
        enrollment_id=enrollment.id,
        winner_decision_id=enrollment.winner_decision_id,
        winner_confirmation_id=enrollment.winner_confirmation_id,
        asset_version_id=enrollment.asset_version_id,
        product_version_id=enrollment.product_version_id,
        offer_version_id=enrollment.offer_version_id,
        market_code=enrollment.market_code,
        landing_page_version_id=enrollment.landing_page_version_id,
        meta_mode=meta_mode,
        refresh_axis=refresh_axis,
    )
    lineage_hash = canonical_hash(seed, domain=_DRAFT_SEED_DOMAIN)
    execution_mode = _EXECUTION_MODE_BY_META_MODE.get(meta_mode, "sandbox")
    draft_product_id = _draft_product_id(session, context, enrollment.product_version_id)

    try:
        with session.begin_nested():
            draft = create_draft(
                session, context,
                product_id=draft_product_id,
                provider=provider,
                model=model,
                execution_mode=execution_mode,
                # Canonical planner shape (Issue #157); ``origin`` is an
                # informational marker the planner ignores, whereas ``mode``
                # is reserved for the legacy ``full``/``sampled`` UI shape.
                combination_strategy={"kind": "full", "origin": "scale_advice_refresh"},
                source_asset_version_id=enrollment.asset_version_id,
                regeneration_intent="new_generation",
            )
            conversion = ScaleAdviceConversionRecord(
                id=new_id("sacr"),
                workspace_id=context.workspace_id,
                meta_mode=meta_mode,
                thread_id=thread.id,
                occurrence_id=occurrence.id,
                generation_draft_id=draft.id,
                scale_test_plan_draft_id=None,
                input_lineage_hash=lineage_hash,
                initiator_id=initiator_id,
            )
            session.add(conversion)
            session.flush()
    except IntegrityError:
        # `session.begin_nested()`'s context manager already rolled back
        # the SAVEPOINT (undoing both the losing Draft and this conversion
        # insert) before re-raising; the outer transaction is untouched.
        winner = _existing_conversion(session, context, meta_mode, occurrence_id)
        if winner is None:
            raise
        return winner.id

    _append_conversion_disposition(session, context, meta_mode, thread, occurrence.id, conversion.id, initiator_id)
    commit_and_rebind(session)
    return conversion.id


def convert_occurrence_to_test_plan(
    session: Session,
    context: TenantContext,
    occurrence_id: str,
    *,
    initiator_id: str,
) -> str:
    """`consider_scale_test` -> one idempotent `ScaleTestPlanDraftRecord`
    (spec §3.6). Returns the `ScaleAdviceConversionRecord` id."""
    meta_mode = _require_meta_mode(context)
    occurrence = _lock_occurrence(session, context, meta_mode, occurrence_id)
    if occurrence.advice_type != AdviceType.CONSIDER_SCALE_TEST.value:
        raise AdviceLifecycleError("wrong_advice_type", occurrence.advice_type)

    existing = _existing_conversion(session, context, meta_mode, occurrence_id)
    if existing is not None:
        return existing.id

    thread, episode, enrollment = _recheck_conversion_eligibility(session, context, meta_mode, occurrence)

    evaluation_row = session.execute(
        select(ScaleEvaluationRecord).filter_by(
            workspace_id=context.workspace_id, meta_mode=meta_mode, id=occurrence.evaluation_id,
        )
    ).scalar_one_or_none()
    if evaluation_row is None:
        raise AdviceLifecycleError("evaluation_not_found", occurrence.evaluation_id)

    generated_criteria = {
        "occurrence_id": occurrence.id,
        "evaluation_id": occurrence.evaluation_id,
        "snapshot_id": occurrence.snapshot_id,
        "policy_version_id": evaluation_row.policy_version_id,
        "target_version_id": evaluation_row.target_version_id,
        "signal_ids": list(occurrence.signal_ids),
        "baseline_evidence": list(occurrence.evidence),
        "severity": occurrence.severity,
    }
    lineage_hash = canonical_hash(generated_criteria, domain=_TEST_PLAN_LINEAGE_DOMAIN)

    conversion_id = new_id("sacr")
    draft_id = new_id("stpd")
    now = utcnow()

    # `ScaleTestPlanDraftRecord.conversion_id` and
    # `ScaleAdviceConversionRecord.scale_test_plan_draft_id` reference each
    # other (models.py's own comment on `fk_sacr_test_plan_draft`), and
    # neither is nullable-and-then-fillable -- the exclusive-output CHECK
    # requires `scale_test_plan_draft_id` set in the same insert. PostgreSQL
    # resolves this with a deferred constraint (Task 6 migration); SQLite's
    # equivalent is `defer_foreign_keys`, scoped to this transaction only
    # (auto-off at commit) so both rows can be inserted before either FK is
    # checked, mirroring the same each-references-the-other pattern.
    if session.get_bind().dialect.name != "postgresql":
        session.execute(text("PRAGMA defer_foreign_keys = ON"))

    try:
        with session.begin_nested():
            draft = ScaleTestPlanDraftRecord(
                id=draft_id,
                workspace_id=context.workspace_id,
                meta_mode=meta_mode,
                conversion_id=conversion_id,
                episode_id=episode.id,
                enrollment_id=enrollment.id,
                generated_criteria=generated_criteria,
                generated_lineage_hash=lineage_hash,
                generated_at=now,
                generated_by=initiator_id,
            )
            conversion = ScaleAdviceConversionRecord(
                id=conversion_id,
                workspace_id=context.workspace_id,
                meta_mode=meta_mode,
                thread_id=thread.id,
                occurrence_id=occurrence.id,
                generation_draft_id=None,
                scale_test_plan_draft_id=draft_id,
                input_lineage_hash=lineage_hash,
                initiator_id=initiator_id,
            )
            session.add_all([draft, conversion])
            session.flush()
    except IntegrityError:
        # See the matching comment in `convert_occurrence_to_draft`: the
        # SAVEPOINT rollback already happened.
        winner = _existing_conversion(session, context, meta_mode, occurrence_id)
        if winner is None:
            raise
        return winner.id

    _append_conversion_disposition(session, context, meta_mode, thread, occurrence.id, conversion.id, initiator_id)
    commit_and_rebind(session)
    return conversion.id


# ---------------------------------------------------------------------------
# Disposition and snooze -- spec §3.5 (orthogonal to lifecycle)
# ---------------------------------------------------------------------------


def _existing_disposition_by_key(
    session: Session, context: TenantContext, meta_mode: str, thread_id: str, idempotency_key: str,
) -> ScaleAdviceDispositionRecord | None:
    return session.execute(
        select(ScaleAdviceDispositionRecord).filter_by(
            workspace_id=context.workspace_id, meta_mode=meta_mode,
            thread_id=thread_id, idempotency_key=idempotency_key,
        )
    ).scalar_one_or_none()


def create_disposition(
    session: Session,
    context: TenantContext,
    thread_id: str,
    kind: str,
    *,
    actor_id: str,
    note: str | None = None,
    occurrence_id: str | None = None,
    idempotency_key: str | None = None,
) -> str:
    """Append-only `acknowledged`/`dismissed` disposition. Never changes
    thread lifecycle (dismissed Advice can still resolve through evidence,
    and snoozed Advice can still be superseded by Episode closure). Dismiss
    cooldown is a Policy-driven notification-suppression concern, applied
    when a future notification-decision pass reads this disposition and the
    frozen Policy's `notification_cooldown_hours` -- not stored here.

    `converted_to_draft` dispositions are created only by the conversion
    commands (they require `linked_conversion_id`); requesting that kind
    here is rejected.

    `idempotency_key` is optional (nullable column, `None` accepted for a
    caller that supplies none) but, when given, replaying the same key
    against the same thread returns the existing disposition instead of
    inserting a duplicate -- a browser timeout-retry must not double-record
    the acknowledgement/dismissal. Checked under the thread's row lock (see
    `_lock_thread`) and re-checked after a `PostgreSQL` unique-constraint
    race (`ix_sad_thread_idempotency`, PostgreSQL-only DDL -- SQLite has no
    physical guard, matching every other Scale uniqueness gate's split)."""
    if kind not in ("acknowledged", "dismissed"):
        raise AdviceLifecycleError("unsupported_disposition_kind", kind)
    meta_mode = _require_meta_mode(context)
    thread = _lock_thread(session, context, meta_mode, thread_id)

    if idempotency_key is not None:
        existing = _existing_disposition_by_key(session, context, meta_mode, thread_id, idempotency_key)
        if existing is not None:
            return existing.id

    resolved_occurrence_id = occurrence_id or thread.latest_occurrence_id

    disposition = ScaleAdviceDispositionRecord(
        id=new_id("sad"),
        workspace_id=context.workspace_id,
        meta_mode=meta_mode,
        thread_id=thread.id,
        occurrence_id=resolved_occurrence_id,
        kind=kind,
        note=note,
        linked_conversion_id=None,
        actor_id=actor_id,
        idempotency_key=idempotency_key,
    )
    try:
        with session.begin_nested():
            session.add(disposition)
            thread.disposition_kind = kind
            thread.disposition_at = utcnow()
            thread.revision += 1
            thread.updated_at = utcnow()
            session.flush()
    except IntegrityError:
        # The SAVEPOINT rollback already undid both the disposition insert
        # and the thread mutation (see the matching comment in
        # `convert_occurrence_to_draft`). A concurrent caller racing the
        # same idempotency_key won; recover its row instead of failing the
        # retry.
        if idempotency_key is not None:
            winner = _existing_disposition_by_key(session, context, meta_mode, thread_id, idempotency_key)
            if winner is not None:
                return winner.id
        raise
    commit_and_rebind(session)
    return disposition.id


def _existing_snooze_by_key(
    session: Session, context: TenantContext, meta_mode: str, thread_id: str, idempotency_key: str,
) -> ScaleAdviceSnoozeRecord | None:
    return session.execute(
        select(ScaleAdviceSnoozeRecord).filter_by(
            workspace_id=context.workspace_id, meta_mode=meta_mode,
            thread_id=thread_id, idempotency_key=idempotency_key,
        )
    ).scalar_one_or_none()


def create_snooze(
    session: Session,
    context: TenantContext,
    thread_id: str,
    until: datetime,
    *,
    actor_id: str,
    reason: str | None = None,
    idempotency_key: str | None = None,
) -> str:
    """Append-only snooze. Suppresses notification delivery, never
    occurrence creation, and never changes thread lifecycle.

    `idempotency_key` follows `create_disposition`'s exact contract (see its
    docstring): optional, but a retry with the same key against the same
    thread returns the existing snooze instead of inserting a duplicate."""
    meta_mode = _require_meta_mode(context)
    thread = _lock_thread(session, context, meta_mode, thread_id)

    if idempotency_key is not None:
        existing = _existing_snooze_by_key(session, context, meta_mode, thread_id, idempotency_key)
        if existing is not None:
            return existing.id

    started_at = utcnow()
    until = _ensure_utc(until)
    if until is None or until <= started_at:
        raise AdviceLifecycleError("snooze_until_not_future")

    snooze = ScaleAdviceSnoozeRecord(
        id=new_id("sas"),
        workspace_id=context.workspace_id,
        meta_mode=meta_mode,
        thread_id=thread.id,
        actor_id=actor_id,
        started_at=started_at,
        snooze_until=until,
        reason=reason,
        idempotency_key=idempotency_key,
    )
    try:
        with session.begin_nested():
            session.add(snooze)
            thread.snooze_until = until
            thread.revision += 1
            thread.updated_at = started_at
            session.flush()
    except IntegrityError:
        if idempotency_key is not None:
            winner = _existing_snooze_by_key(session, context, meta_mode, thread_id, idempotency_key)
            if winner is not None:
                return winner.id
        raise
    commit_and_rebind(session)
    return snooze.id


# ---------------------------------------------------------------------------
# Freshness expiry -- spec §3.5
# ---------------------------------------------------------------------------


@runs_as(WORKER_ROLE)
def expire_advice_thread(
    session: Session,
    context: TenantContext,
    thread_id: str,
    *,
    executor_id: str = _SYSTEM_EXECUTOR,
    now: datetime | None = None,
) -> bool:
    """The `scale_advice_expire` deadline-job handler. Rechecks the latest
    eligible evidence under lock and either expires the thread or no-ops
    because newer evidence already moved the deadline. Returns True iff the
    thread was expired by this call."""
    meta_mode = _require_meta_mode(context)
    thread = _lock_thread(session, context, meta_mode, thread_id)
    if thread.lifecycle != "active":
        return False

    current_time = _ensure_utc(now) or utcnow()
    if current_time < _freshness_deadline(session, context, meta_mode, thread):
        # Newer evidence already moved the deadline; nothing to do.
        return False

    thread.lifecycle = "expired"
    thread.lifecycle_reason = "freshness_deadline_exceeded"
    thread.revision += 1
    thread.updated_at = current_time
    session.add(ScaleAdviceThreadEventRecord(
        id=new_id("sate"),
        workspace_id=context.workspace_id,
        meta_mode=meta_mode,
        episode_id=thread.episode_id,
        thread_id=thread.id,
        occurrence_id=None,
        event_type="expired",
        previous_lifecycle="active",
        current_lifecycle="expired",
        successor_thread_id=None,
        reason="freshness_deadline_exceeded",
        deduplication_key=f"expired:{thread.id}",
        executor_id=executor_id,
    ))
    commit_and_rebind(session)
    return True


def _freshness_deadline(
    session: Session, context: TenantContext, meta_mode: str, thread: ScaleAdviceThreadRecord,
) -> datetime:
    """When the thread's latest eligible evidence passes its frozen freshness
    deadline (spec §3.5): the freshest evaluated live run of its Episode, or
    else its latest occurrence, plus the Policy's maximum lag."""
    latest_occurrence = session.get(ScaleAdviceOccurrenceRecord, thread.latest_occurrence_id)
    if latest_occurrence is None:
        raise AdviceLifecycleError("occurrence_not_found", thread.latest_occurrence_id)
    latest_eval = session.execute(
        select(ScaleEvaluationRecord).filter_by(
            workspace_id=context.workspace_id, meta_mode=meta_mode, id=latest_occurrence.evaluation_id,
        )
    ).scalar_one_or_none()
    if latest_eval is None:
        raise AdviceLifecycleError("evaluation_not_found", latest_occurrence.evaluation_id)
    policy = session.get(ScalePolicyVersionRecord, latest_eval.policy_version_id)
    if policy is None:
        raise AdviceLifecycleError("policy_not_found", latest_eval.policy_version_id)

    freshest_run = session.execute(
        select(ScaleEvaluationRunRecord)
        .filter_by(
            workspace_id=context.workspace_id, meta_mode=meta_mode,
            episode_id=thread.episode_id, trigger_kind="live", run_state="evaluated",
        )
        .order_by(ScaleEvaluationRunRecord.freshness_reference_at.desc())
        .limit(1)
    ).scalars().first()

    reference_at = _ensure_utc(
        freshest_run.freshness_reference_at if freshest_run is not None
        else latest_occurrence.occurred_at
    )
    return reference_at + timedelta(hours=policy.freshness_floor_maximum_lag_hours)


def enqueue_advice_expiry(
    session: Session, context: TenantContext, thread_id: str,
) -> JobRecord | None:
    """Enqueue the ``scale_advice_expire`` deadline Job for an active thread
    (spec §3.5), keyed by thread and freshness deadline, or return the Job
    that key already names. A thread that is no longer active has no
    deadline: nothing is enqueued.

    The Job is not claimable before the deadline (``not_before``, #111).
    When later evidence moves the deadline, the next call enqueues a new Job
    under the new key; the earlier one, once due, rechecks, finds the
    deadline ahead and changes nothing. The payload carries the thread's
    Meta mode (#97): the Worker's claimed context names none, and the thread
    is visible under RLS only to a context that does.
    """
    meta_mode = _require_meta_mode(context)
    thread = session.execute(
        select(ScaleAdviceThreadRecord).filter_by(
            workspace_id=context.workspace_id, meta_mode=meta_mode, id=thread_id,
        )
    ).scalar_one_or_none()
    if thread is None:
        raise AdviceLifecycleError("thread_not_found", thread_id)
    if thread.lifecycle != "active":
        return None
    # In UTC, so one instant names one key whatever the session's time zone.
    deadline = _freshness_deadline(session, context, meta_mode, thread).astimezone(timezone.utc)
    with tenant_scope(session, context):
        return enqueue_in_session(
            session,
            context=context,
            job_type="scale_advice_expire",
            payload={"thread_id": thread.id, "meta_mode": meta_mode},
            idempotency_key=f"scale-advice-expire:{thread.id}:{deadline.isoformat()}",
            resource_type="scale_advice_thread",
            resource_id=thread.id,
            not_before=deadline,
        )


def enqueue_episode_advice_expiries(
    session: Session, context: TenantContext, episode_id: str,
) -> list[JobRecord]:
    """Schedule the deadline Job of every active thread of one Episode
    (spec §3.5: "Every projection-eligible Evaluation advances the ...
    deadline for all active Episode threads").

    Phase C calls this after its run is flushed as ``evaluated``, so the
    deadline each key carries is the one ``expire_advice_thread`` will
    recompute; and before its one commit, so a thread is never committed
    without the Job for its current deadline. The threads are locked, as
    the handler locks them. Returns the Jobs in thread-id order.
    """
    meta_mode = _require_meta_mode(context)
    threads = _load_active_threads(session, context, meta_mode, episode_id)
    scheduled: list[JobRecord] = []
    for thread in sorted(threads.values(), key=lambda row: row.id):
        job = enqueue_advice_expiry(session, context, thread.id)
        if job is not None:
            scheduled.append(job)
    return scheduled


def advice_expire_job_context(context: TenantContext, payload: Mapping[str, Any]) -> TenantContext:
    """The TenantContext a ``scale_advice_expire`` Job runs under (#97).

    The claimed context names no Meta mode; the payload does. A payload
    without a valid mode is refused as ``missing_input`` before the Job
    runs, instead of failing inside the handler as ``meta_mode_required``.
    """
    meta_mode = str(payload.get("meta_mode") or "")
    if meta_mode not in VALID_META_MODES:
        raise AdviceLifecycleError("missing_input", "缺少或无效的 meta_mode")
    return replace(context, meta_mode=meta_mode)


# ---------------------------------------------------------------------------
# The fallback sweep -- #111, #101 design §5
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OverdueAdviceSweep:
    """What one fallback sweep did: the threads it expired, in id order, and
    the ones it could not check, by thread id."""

    expired: tuple[str, ...]
    failed: Mapping[str, str]


@runs_as(WORKER_ROLE)
def expire_overdue_connection_advice(
    context: TenantContext, connection_id: str, *, now: datetime | None = None,
) -> OverdueAdviceSweep:
    """Expire every ``active`` thread past its deadline on one connection: the
    fallback for a deadline Job that did not (#111; #101 design §5).

    A ``scale_advice_expire`` Job waits until its deadline, a day or more,
    and two things can leave its thread ``active`` past it:

    * the Workspace's authorization epoch moves meanwhile, and the claim
      rejects the Job as ``authorization_epoch_stale``, due or not;
    * the claim compares the deadline with the database clock, while
      :func:`expire_advice_thread` rechecks it with the Worker's: a Job
      claimed while a lagging Worker clock is still short of the deadline
      changes nothing, and its key is spent.

    The source-collection Job (``app.scale.collector``) calls this after its
    Episode loop. It runs daily for each connection with an ``active`` or
    ``paused`` Enrollment, also when the batch failed, under the epoch its
    claim checked. Every ``active`` thread of an Episode on the connection,
    in this Workspace and Meta mode, goes through
    :func:`expire_advice_thread`, which locks it, recomputes the deadline
    and expires it only once the deadline has passed, so a thread whose
    deadline is ahead, or that its Job already expired, is left as it is.

    Each thread commits on its own. A thread that cannot be checked is
    recorded as a ``scale.advice_sweep.thread_failed`` audit event and stays
    for the next collection: the sweep never fails the collection. The
    ``expired`` event names this sweep as its executor, so the thread's
    history says which path expired it.
    """
    meta_mode = _require_meta_mode(context)
    current_time = _ensure_utc(now) or utcnow()
    with tenant_session(context) as session:
        thread_ids = list(session.scalars(
            select(ScaleAdviceThreadRecord.id)
            .join(ScaleEpisodeRecord, and_(
                ScaleEpisodeRecord.workspace_id == ScaleAdviceThreadRecord.workspace_id,
                ScaleEpisodeRecord.meta_mode == ScaleAdviceThreadRecord.meta_mode,
                ScaleEpisodeRecord.id == ScaleAdviceThreadRecord.episode_id,
            ))
            .where(
                ScaleAdviceThreadRecord.workspace_id == context.workspace_id,
                ScaleAdviceThreadRecord.meta_mode == meta_mode,
                ScaleAdviceThreadRecord.lifecycle == "active",
                ScaleEpisodeRecord.meta_connection_id == connection_id,
            )
            .order_by(ScaleAdviceThreadRecord.id)
        ))
    expired: list[str] = []
    failed: dict[str, str] = {}
    for thread_id in thread_ids:
        try:
            with tenant_session(context) as session:
                if expire_advice_thread(
                    session, context, thread_id, executor_id=_FALLBACK_SWEEP_EXECUTOR, now=current_time,
                ):
                    expired.append(thread_id)
        except Exception as exc:  # noqa: BLE001 -- one thread must not stop the others or the collection
            failed[thread_id] = f"{type(exc).__name__}: {exc}"[:500]
    if failed:
        with tenant_session(context) as session:
            for thread_id, detail in failed.items():
                append_event(
                    session,
                    event_type="scale.advice_sweep.thread_failed",
                    resource_type="scale_advice_thread",
                    resource_id=thread_id,
                    details={"meta_connection_id": connection_id, "error": detail},
                )
    return OverdueAdviceSweep(expired=tuple(expired), failed=failed)
