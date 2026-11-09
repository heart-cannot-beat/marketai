"""Durable evaluation orchestration (spec §3.1, §3.4).

This module is the central service tying together evidence sealing (Task 10,
``app.scale.evidence``), the pure evaluator (Task 9, ``app.scale.evaluator``),
and Enrollment/Episode lifecycle (Task 11, ``app.scale.enrollment``). It
exposes exactly two public entry points:

* :func:`request_scale_evaluation` -- creates or reuses a durable
  ``ScaleEvaluationRunRecord`` for one semantic request and returns its id.
  It never seals a snapshot or calls the evaluator; that is
  :func:`run_scale_evaluation`'s job.
* :func:`run_scale_evaluation` -- resumes one durable run to a terminal
  outcome through the three-phase model (spec §3.4):

  - **Phase A** (short transaction): seal or reuse the run's snapshot via
    ``seal_snapshot()``. Returns early with the recorded outcome when the run
    is already terminal.
  - **Phase B** (no open transaction): ``assemble_snapshot()`` then
    ``evaluate_scale(snapshot)``. Pure computation, no DB writes.
  - **Phase C** (short transaction): persist the Evaluation and its children,
    CAS the Episode projection, apply seven-day no-delivery closure when
    applicable, append an audit event, and CAS the run to its terminal
    outcome.

Known Task-12 scoping decisions (the full generality is later tasks' job):

* Advice lifecycle processing (thread resolution, occurrence creation,
  dispositions/cooldowns, notification intents -- spec §3.5) is delegated to
  :func:`app.scale.advice.process_advice_candidates`, called from Phase C
  step 6 with the Evaluation's own persisted advice candidates.
* Job dispatch for the ``scale_evaluate`` job (spec §3.8) is Task 14's
  concern. ``request_scale_evaluation`` only creates/reuses the durable run
  checkpoint; the run id is already the complete job payload the future
  dispatcher needs.
* Forward-replay lineage reconciliation (spec §3.8 "Backfill lineage") is not
  implemented: a ``forward_replay`` run never wins the projection CAS here.
  The run/Evaluation records still carry every field a later task needs to
  add that generality without a schema change.
* ``ck_sevr_snapshot_shape`` (a non-``requested`` run_state requires
  ``snapshot_id``) forces every early Phase-A rejection that has no sealed
  snapshot yet (a blocked source manifest, or any other
  ``SnapshotSealingError`` raised before a snapshot row exists) to leave the
  run row untouched at ``requested`` -- matching ``seal_snapshot()``'s own
  tested contract (Task 10: "rejects blocked manifest without mutating run")
  -- and return an ephemeral, non-persisted classification instead. A fresh
  run against a new source manifest is the retry path (spec §3.2), not
  resuming this one. Episode rollover and upstream-eligibility rejections
  are therefore evaluated *after* sealing succeeds (the old Episode's final
  snapshot is real, valid, sealed evidence even though the run does not go
  on to evaluate it), which keeps every terminal transition constraint-safe.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.persistence.models import AuditEventRecord, new_id, utcnow
from app.platform.tenancy import TenantContext
from app.scale._canonical import canonical_encode, canonical_hash
from app.scale._contracts import ScaleEvaluation, ScaleEvidenceSnapshot
from app.scale.advice import process_advice_candidates
from app.scale.enrollment import (
    DeliveryConfigBaseline,
    EpisodeContext,
    apply_no_delivery_episode_closure,
    # Reused directly rather than duplicated: these build (not insert) a
    # fresh Episode/delivery-source pair and close an Episode's current one,
    # the exact two primitives Episode rollover here needs, and Task 11
    # already owns their locking/event/idempotency shape.
    _build_episode_and_delivery_source,
    _close_episode,
)
from app.scale.evaluator import evaluate_scale
from app.scale.evidence import (
    SnapshotAssemblyError,
    SnapshotSealingError,
    assemble_snapshot,
    seal_snapshot,
)
from app.scale.models import (
    NO_TARGET_MARKER,
    ScaleDeliveryConfigSourceRecord,
    ScaleEnrollmentRecord,
    ScaleEpisodeProjectionRecord,
    ScaleEpisodeRecord,
    ScaleEvaluationAdviceCandidateRecord,
    ScaleEvaluationRecord,
    ScaleEvaluationRunRecord,
    ScaleEvaluationSignalRecord,
    ScaleEvaluationStreakCounterRecord,
    ScaleEvaluationStreakResetRecord,
    ScaleEvidenceSnapshotRecord,
    ScaleManifestItemRecord,
)
from app.scale.types import RunOutcomeKind
from app.platform.runtime_role import WORKER_ROLE, runs_as

__all__ = [
    "ScaleEvaluationRequest",
    "ScaleRunOutcome",
    "ScaleOrchestrationError",
    "request_scale_evaluation",
    "run_scale_evaluation",
]

_RESULT_HASH_DOMAIN = "scale-evaluation-result-v1"
_EVALUATION_CANONICAL_SCHEMA_VERSION = "scale-canonical-v1"
# spec §3.7: "seven consecutive complete zero-delivery days" -- no Policy
# field carries this threshold (Task 7 did not define one), so it is a fixed
# constant matching the spec's literal wording.
_NO_DELIVERY_CLOSURE_THRESHOLD = 7

_RUN_STATE_BY_OUTCOME = {
    RunOutcomeKind.EVALUATED: "evaluated",
    RunOutcomeKind.ALREADY_EVALUATED: "evaluated",
    RunOutcomeKind.EPISODE_ROLLED_OVER: "episode_rolled_over",
    RunOutcomeKind.BLOCKED: "blocked",
    RunOutcomeKind.CANCELLED: "cancelled",
    RunOutcomeKind.FAILED_INTEGRITY: "failed_integrity",
}


class ScaleOrchestrationError(RuntimeError):
    """Deterministic, stably-coded rejection raised by this module."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class ScaleEvaluationRequest:
    """One frozen semantic evaluation request (spec §3.1). Everything an
    HTTP caller cannot choose: source manifest, Policy, Target, algorithm,
    replay generation, and semantic sequence are all scheduler-resolved
    before this value is built."""

    enrollment_id: str
    episode_id: str
    target_local_day: date
    trigger_kind: str
    source_manifest_id: str
    source_manifest_hash: str
    source_revision: int
    freshness_reference_at: datetime
    policy_version_id: str
    algorithm_version: str
    target_version_id: str | None = None
    semantic_event_sequence: int = 0
    replay_generation: int = 0
    replay_base_evaluation_id: str | None = None
    triggering_sync_run_id: str | None = None
    triggering_semantic_event_id: str | None = None
    original_initiator_id: str = ""


@dataclass(frozen=True)
class ScaleRunOutcome:
    """Returned by :func:`run_scale_evaluation` (spec §3.1). ``evaluation_id``
    is populated only for the ``evaluated``/``already_evaluated`` kinds."""

    kind: RunOutcomeKind
    run_id: str
    enrollment_id: str
    episode_id: str
    evaluation_id: str | None = None
    detail: str = ""


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _for_update(session: Session, stmt):
    """PostgreSQL row locking; SQLite (tests, bounded local path) is a no-op."""
    if session.get_bind().dialect.name == "postgresql":
        return stmt.with_for_update()
    return stmt


def _require_meta_mode(context: TenantContext) -> str:
    if context.meta_mode is None:
        raise ScaleOrchestrationError("meta_mode_required")
    return context.meta_mode


def _outcome_from_run(run: ScaleEvaluationRunRecord) -> ScaleRunOutcome:
    return ScaleRunOutcome(
        kind=RunOutcomeKind(run.outcome_kind),
        run_id=run.id,
        enrollment_id=run.enrollment_id,
        episode_id=run.episode_id,
        evaluation_id=run.evaluation_id,
        detail=run.outcome_detail or "",
    )


def _terminalize(
    session: Session,
    context: TenantContext,
    run: ScaleEvaluationRunRecord,
    episode: ScaleEpisodeRecord | None,
    *,
    kind: RunOutcomeKind,
    detail: str = "",
    evaluation_id: str | None = None,
) -> ScaleRunOutcome:
    """CAS the run to one terminal outcome (spec §3.4 Phase C step 9, and
    the equivalent post-seal Phase A transitions this module adds). Unless a
    closure already closed the Episode, restores its phase to `collecting`
    and clears `phase_run_id` when this run still owns it (spec §3.4 step
    8)."""
    run.run_state = _RUN_STATE_BY_OUTCOME[kind]
    run.outcome_kind = kind.value
    run.outcome_detail = detail or None
    if evaluation_id is not None:
        run.evaluation_id = evaluation_id
    run.updated_at = utcnow()
    if episode is not None and episode.phase != "closed" and episode.phase_run_id == run.id:
        episode.phase = "collecting"
        episode.phase_run_id = None
        episode.revision += 1
    session.flush()
    return ScaleRunOutcome(
        kind=kind,
        run_id=run.id,
        enrollment_id=run.enrollment_id,
        episode_id=run.episode_id,
        evaluation_id=run.evaluation_id,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# request_scale_evaluation
# ---------------------------------------------------------------------------


def request_scale_evaluation(
    session: Session, context: TenantContext, request: ScaleEvaluationRequest,
) -> str:
    """Return the durable ``evaluation_run_id`` for one semantic request
    (spec §3.1). Creates or reuses a ``ScaleEvaluationRunRecord`` by its
    semantic uniqueness key; never seals a snapshot or evaluates."""
    meta_mode = _require_meta_mode(context)
    normalized_target_key = request.target_version_id or NO_TARGET_MARKER

    def _find_existing() -> ScaleEvaluationRunRecord | None:
        return session.execute(
            select(ScaleEvaluationRunRecord).filter_by(
                workspace_id=context.workspace_id,
                meta_mode=meta_mode,
                episode_id=request.episode_id,
                target_local_day=request.target_local_day,
                trigger_kind=request.trigger_kind,
                source_manifest_hash=request.source_manifest_hash,
                source_revision=request.source_revision,
                freshness_reference_at=request.freshness_reference_at,
                policy_version_id=request.policy_version_id,
                normalized_target_key=normalized_target_key,
                algorithm_version=request.algorithm_version,
                semantic_event_sequence=request.semantic_event_sequence,
                replay_generation=request.replay_generation,
            )
        ).scalar_one_or_none()

    existing = _find_existing()
    if existing is not None:
        return existing.id

    enrollment = session.execute(
        select(ScaleEnrollmentRecord).filter_by(
            workspace_id=context.workspace_id, meta_mode=meta_mode, id=request.enrollment_id,
        )
    ).scalar_one_or_none()
    if enrollment is None:
        raise ScaleOrchestrationError("enrollment_not_found", request.enrollment_id)

    run = ScaleEvaluationRunRecord(
        id=new_id("sevr"),
        workspace_id=context.workspace_id,
        meta_mode=meta_mode,
        meta_connection_id=enrollment.meta_connection_id,
        scenario_scope_key=enrollment.scenario_scope_key,
        episode_id=request.episode_id,
        enrollment_id=request.enrollment_id,
        target_local_day=request.target_local_day,
        trigger_kind=request.trigger_kind,
        source_manifest_id=request.source_manifest_id,
        source_manifest_hash=request.source_manifest_hash,
        source_revision=request.source_revision,
        freshness_reference_at=request.freshness_reference_at,
        policy_version_id=request.policy_version_id,
        target_version_id=request.target_version_id,
        normalized_target_key=normalized_target_key,
        algorithm_version=request.algorithm_version,
        semantic_event_sequence=request.semantic_event_sequence,
        replay_generation=request.replay_generation,
        replay_base_evaluation_id=request.replay_base_evaluation_id,
        triggering_sync_run_id=request.triggering_sync_run_id,
        triggering_semantic_event_id=request.triggering_semantic_event_id,
        original_initiator_id=request.original_initiator_id or enrollment.original_initiator_id,
        cancellation_generation=enrollment.evaluation_cancellation_generation,
        run_state="requested",
    )
    try:
        with session.begin_nested():
            session.add(run)
            session.flush()
    except IntegrityError:
        recovered = _find_existing()
        if recovered is None:
            raise
        return recovered.id

    # Job enqueuing (spec §3.8): the actual `scale_evaluate` dispatch
    # mechanism, its lease, and its idempotency key
    # (`scale-eval:{run_id}:{algorithm_version}`) are Task 14's concern. The
    # durable run this function just created/reused is already the complete
    # payload a future dispatcher needs -- recording the intent here means
    # nothing more than handing back this id.
    return run.id


# ---------------------------------------------------------------------------
# run_scale_evaluation
# ---------------------------------------------------------------------------


@runs_as(WORKER_ROLE)
def run_scale_evaluation(
    session: Session, context: TenantContext, evaluation_run_id: str,
) -> ScaleRunOutcome:
    """Resume one durable Scale evaluation run to a terminal outcome (spec
    §3.4)."""
    phase_a = _phase_a(session, context, evaluation_run_id)
    if isinstance(phase_a, ScaleRunOutcome):
        return phase_a
    run_id, snapshot_id = phase_a

    try:
        snapshot = assemble_snapshot(session, context, snapshot_id)
    except SnapshotAssemblyError as exc:
        return _fail_after_seal(session, context, run_id, f"assembly:{exc.code}")

    # Phase B ends here: close the read transaction before pure computation
    # (spec §3.4 Phase B -- the deterministic bootstrap holds no database
    # lock or RLS transaction open while it runs).
    session.commit()

    evaluation = evaluate_scale(snapshot)

    return _phase_c(session, context, run_id, snapshot, evaluation)


def _fail_after_seal(session: Session, context: TenantContext, run_id: str, detail: str) -> ScaleRunOutcome:
    meta_mode = _require_meta_mode(context)
    run = session.execute(
        _for_update(
            session,
            select(ScaleEvaluationRunRecord).filter_by(
                workspace_id=context.workspace_id, meta_mode=meta_mode, id=run_id,
            ),
        )
    ).scalar_one()
    episode = session.execute(
        _for_update(
            session,
            select(ScaleEpisodeRecord).filter_by(
                workspace_id=context.workspace_id, meta_mode=meta_mode, id=run.episode_id,
            ),
        )
    ).scalar_one_or_none()
    outcome = _terminalize(
        session, context, run, episode, kind=RunOutcomeKind.FAILED_INTEGRITY, detail=detail,
    )
    session.commit()
    return outcome


# ---------------------------------------------------------------------------
# Phase A: seal or reuse (spec §3.3 Transaction A, §3.4 Phase A)
# ---------------------------------------------------------------------------


def _phase_a(
    session: Session, context: TenantContext, run_id: str,
) -> ScaleRunOutcome | tuple[str, str]:
    meta_mode = _require_meta_mode(context)

    run = session.execute(
        _for_update(
            session,
            select(ScaleEvaluationRunRecord).filter_by(
                workspace_id=context.workspace_id, meta_mode=meta_mode, id=run_id,
            ),
        )
    ).scalar_one_or_none()
    if run is None:
        raise ScaleOrchestrationError("run_not_found", run_id)

    if run.outcome_kind is not None:
        # Already terminal: return the recorded outcome without repeating
        # side effects (spec §3.4 Phase A).
        outcome = _outcome_from_run(run)
        session.commit()
        return outcome

    enrollment = session.execute(
        _for_update(
            session,
            select(ScaleEnrollmentRecord).filter_by(
                workspace_id=context.workspace_id, meta_mode=meta_mode, id=run.enrollment_id,
            ),
        )
    ).scalar_one_or_none()
    if enrollment is None:
        raise ScaleOrchestrationError("enrollment_not_found", run.enrollment_id)

    episode = session.execute(
        _for_update(
            session,
            select(ScaleEpisodeRecord).filter_by(
                workspace_id=context.workspace_id, meta_mode=meta_mode, id=run.episode_id,
            ),
        )
    ).scalar_one_or_none()
    if episode is None:
        raise ScaleOrchestrationError("episode_not_found", run.episode_id)

    try:
        snapshot_id = seal_snapshot(session, context, run.id)
    except SnapshotSealingError as exc:
        if run.snapshot_id is None:
            # No snapshot exists (manifest blocked, or any other rejection
            # `seal_snapshot()` raises before inserting a snapshot row). The
            # run's own `ck_sevr_snapshot_shape` CHECK forbids a
            # non-`requested` run_state without a `snapshot_id`, so this run
            # is left untouched -- matching Task 10's own tested sealing
            # contract -- and this classification is returned without being
            # persisted. See module docstring.
            kind = (
                RunOutcomeKind.BLOCKED
                if exc.code == "manifest_blocked"
                else RunOutcomeKind.FAILED_INTEGRITY
            )
            # Nothing was mutated by the failed sealing attempt (no snapshot
            # row, no run/Episode change), but the `SELECT ... FOR UPDATE`
            # locks above still hold an open transaction -- roll it back so
            # this call releases those locks and leaves the session exactly
            # as it found the database.
            session.rollback()
            return ScaleRunOutcome(
                kind=kind,
                run_id=run.id,
                enrollment_id=run.enrollment_id,
                episode_id=run.episode_id,
                detail=exc.code,
            )
        # The snapshot itself sealed (its content is committed, real, sealed
        # evidence); only the Episode `phase_run_id` fencing step afterward
        # failed because a different live run already owns it. Persist the
        # pending `run.snapshot_id`/`run_state` sealing already assigned in
        # memory, then terminalize -- the constraint is now satisfiable.
        session.flush()
        outcome = _terminalize(
            session, context, run, episode, kind=RunOutcomeKind.FAILED_INTEGRITY, detail=exc.code,
        )
        session.commit()
        return outcome

    snapshot_row = session.get(ScaleEvidenceSnapshotRecord, snapshot_id)

    # Episode rollover (spec §3.3 step 3): evidence.py deliberately seals
    # faithfully from the manifest's declared delivery source without
    # comparing it against the Episode's frozen baseline -- that comparison
    # is this module's job. Checked after sealing (see module docstring) so
    # the CHECK constraint on the terminal run_state is always satisfiable.
    if snapshot_row.delivery_comparability_hash != episode.delivery_comparability_hash:
        outcome = _apply_episode_rollover(session, context, run, enrollment, episode)
        session.commit()
        return outcome

    # Upstream eligibility recheck (spec §3.3 step 2 / §3.4 Phase C). A
    # historical backfill for an otherwise closed Enrollment is out of this
    # task's scope (module docstring); only a live/semantic-reevaluation run
    # is blocked here.
    from app.marketing.evidence_handoff import scale_block_reason
    marketing_reason = scale_block_reason(session, context, enrollment.winner_decision_id)
    if marketing_reason:
        outcome = _terminalize(session, context, run, episode, kind=RunOutcomeKind.BLOCKED, detail=marketing_reason)
        session.commit()
        return outcome

    if enrollment.lifecycle == "closed" and run.trigger_kind != "backfill":
        outcome = _terminalize(
            session, context, run, episode, kind=RunOutcomeKind.BLOCKED, detail="upstream_withdrawal",
        )
        session.commit()
        return outcome

    session.commit()
    return run.id, snapshot_id


def _apply_episode_rollover(
    session: Session,
    context: TenantContext,
    run: ScaleEvaluationRunRecord,
    enrollment: ScaleEnrollmentRecord,
    episode: ScaleEpisodeRecord,
) -> ScaleRunOutcome:
    manifest_item = session.execute(
        select(ScaleManifestItemRecord).filter_by(
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            manifest_id=run.source_manifest_id,
            contract_type="delivery_configuration",
        )
    ).scalar_one_or_none()
    delivery = (
        session.get(ScaleDeliveryConfigSourceRecord, manifest_item.source_record_id)
        if manifest_item is not None and manifest_item.state == "fulfilled"
        else None
    )
    if delivery is None or enrollment.current_episode_id != episode.id or episode.phase == "closed":
        # Nothing live to roll over (already closed by a concurrent path, or
        # this Episode is no longer the Enrollment's current one): fall back
        # to a plain cancellation rather than fabricating a rollover with no
        # delivery evidence.
        return _terminalize(
            session, context, run, episode, kind=RunOutcomeKind.CANCELLED, detail="episode_not_current",
        )

    baseline = DeliveryConfigBaseline(
        contract_generation_id=delivery.contract_generation_id,
        ad_creative_id=delivery.ad_creative_id,
        campaign_status=delivery.campaign_status,
        ad_set_status=delivery.ad_set_status,
        ad_status=delivery.ad_status,
        budget_source=delivery.budget_source,
        budget_amount=delivery.budget_amount,
        optimization_goal=delivery.optimization_goal,
        bid_strategy=delivery.bid_strategy,
        attribution_setting=delivery.attribution_setting,
        targeting_safety_hash=delivery.targeting_safety_hash,
        evidence_hash=delivery.evidence_hash,
        comparability_hash=delivery.comparability_hash,
        raw_response_hash=delivery.raw_response_hash,
        sync_run_id=delivery.sync_run_id,
        occurrence_id=delivery.occurrence_id,
        call_intent_id=delivery.call_intent_id,
        observed_at=delivery.observed_at,
        reconciliation_id=delivery.reconciliation_id,
        offer_version_id=delivery.offer_version_id,
        landing_page_version_id=delivery.landing_page_version_id,
    )
    episode_context = EpisodeContext(
        metric_semantic_contract_set_id=episode.metric_semantic_contract_set_id,
        timezone_name=episode.timezone_name,
        currency=episode.currency,
        attribution_semantics_hash=episode.attribution_semantics_hash,
        started_local_day=run.target_local_day,
    )
    new_episode, new_delivery_source = _build_episode_and_delivery_source(
        context, enrollment, baseline, episode_context,
    )
    _, close_event = _close_episode(
        session, context, enrollment,
        closure_reason="delivery_configuration_changed",
        idempotency_key=f"rollover:{run.id}",
    )

    enrollment.current_episode_id = new_episode.id
    enrollment.updated_at = utcnow()

    session.add(new_episode)
    session.flush()
    to_add = [new_delivery_source]
    if close_event is not None:
        to_add.append(close_event)
    session.add_all(to_add)
    session.flush()

    return _terminalize(
        session, context, run, episode,
        kind=RunOutcomeKind.EPISODE_ROLLED_OVER, detail="delivery_configuration_changed",
    )


# ---------------------------------------------------------------------------
# Phase C: persist and project atomically (spec §3.4 Phase C)
# ---------------------------------------------------------------------------


def _phase_c(
    session: Session,
    context: TenantContext,
    run_id: str,
    snapshot: ScaleEvidenceSnapshot,
    evaluation: ScaleEvaluation,
) -> ScaleRunOutcome:
    meta_mode = _require_meta_mode(context)

    run = session.execute(
        _for_update(
            session,
            select(ScaleEvaluationRunRecord).filter_by(
                workspace_id=context.workspace_id, meta_mode=meta_mode, id=run_id,
            ),
        )
    ).scalar_one_or_none()
    if run is None:
        raise ScaleOrchestrationError("run_not_found", run_id)

    if run.outcome_kind is not None:
        outcome = _outcome_from_run(run)
        session.commit()
        return outcome

    enrollment = session.execute(
        _for_update(
            session,
            select(ScaleEnrollmentRecord).filter_by(
                workspace_id=context.workspace_id, meta_mode=meta_mode, id=run.enrollment_id,
            ),
        )
    ).scalar_one_or_none()
    episode = session.execute(
        _for_update(
            session,
            select(ScaleEpisodeRecord).filter_by(
                workspace_id=context.workspace_id, meta_mode=meta_mode, id=run.episode_id,
            ),
        )
    ).scalar_one_or_none()
    if enrollment is None or episode is None:
        raise ScaleOrchestrationError("enrollment_or_episode_not_found", run_id)

    # Recheck upstream eligibility, cancellation generation, and Episode
    # phase ownership for a race that happened during Phase B's DB-free
    # window (spec §3.4 Phase C).
    from app.marketing.evidence_handoff import scale_block_reason
    marketing_reason = scale_block_reason(session, context, enrollment.winner_decision_id)
    if marketing_reason:
        outcome = _terminalize(session, context, run, episode, kind=RunOutcomeKind.BLOCKED, detail=marketing_reason)
        session.commit()
        return outcome

    if enrollment.lifecycle == "closed" and run.trigger_kind != "backfill":
        outcome = _terminalize(
            session, context, run, episode, kind=RunOutcomeKind.BLOCKED, detail="upstream_withdrawal",
        )
        session.commit()
        return outcome

    if run.cancellation_generation != enrollment.evaluation_cancellation_generation:
        outcome = _terminalize(
            session, context, run, episode,
            kind=RunOutcomeKind.CANCELLED, detail="cancellation_generation_fenced",
        )
        session.commit()
        return outcome

    # A concurrent Episode closure raced this run during Phase B's DB-free
    # window (spec §3.4 Phase C: "A concurrent configuration rollover makes
    # a live run `episode_rolled_over`; pause, close, or another closure
    # makes it `cancelled`"). Distinguish by `closure_reason` rather than
    # collapsing every closure into `cancelled`.
    if episode.phase == "closed":
        if episode.closure_reason == "delivery_configuration_changed":
            outcome = _terminalize(
                session, context, run, episode,
                kind=RunOutcomeKind.EPISODE_ROLLED_OVER, detail="delivery_configuration_changed",
            )
        else:
            outcome = _terminalize(
                session, context, run, episode,
                kind=RunOutcomeKind.CANCELLED,
                detail=f"episode_closed:{episode.closure_reason or 'unknown'}",
            )
        session.commit()
        return outcome

    if episode.phase_run_id != run.id:
        outcome = _terminalize(
            session, context, run, episode,
            kind=RunOutcomeKind.CANCELLED, detail="episode_phase_owner_changed",
        )
        session.commit()
        return outcome

    # Steps 1-2: insert or load the Evaluation by full semantic fingerprint.
    evaluation_row, reused = _insert_or_load_evaluation(session, context, episode, snapshot, evaluation)

    # Step 3: decide projection eligibility and CAS the Episode cursor.
    projection_eligible = _decide_projection_eligibility(episode, run, snapshot)

    no_delivery_closing = False
    if projection_eligible:
        _apply_projection(session, context, episode, run, snapshot, evaluation, evaluation_row)

        # Step 5: seven-consecutive-complete-no-delivery-day closure, before
        # ordinary Advice processing.
        if (
            enrollment.lifecycle == "active"
            and _no_delivery_streak(evaluation) >= _NO_DELIVERY_CLOSURE_THRESHOLD
        ):
            apply_no_delivery_episode_closure(
                session, context, enrollment.id, episode.id,
                idempotency_key=f"eval:{evaluation_row.id}",
            )
            no_delivery_closing = True

    if not no_delivery_closing:
        # Step 6: Advice lifecycle processing (spec §3.5) -- thread
        # resolution, occurrence creation, eligible-miss transitions, and
        # recurrence/semantic-change supersession. Note that
        # `process_advice_candidates` commits its own transaction
        # internally; the Evaluation/projection writes above are already
        # flushed (and visible to it within this same session) by the time
        # it runs.
        process_advice_candidates(
            session, context, episode.id, evaluation_row.id, evaluation.advice_candidates,
            trigger_kind=run.trigger_kind,
            evaluated_local_day=snapshot.as_of_local_day,
            projection_eligible=projection_eligible,
        )

    # Step 7: audit.
    _append_run_audit(session, context, run, enrollment, episode, evaluation_row, evaluation, reused)

    # Step 8: unless closure already closed the Episode, CAS its phase back
    # to `collecting`.
    if episode.phase != "closed" and episode.phase_run_id == run.id:
        episode.phase = "collecting"
        episode.phase_run_id = None
        episode.revision += 1

    # Step 9: CAS the run to its terminal outcome and commit.
    outcome_kind = RunOutcomeKind.ALREADY_EVALUATED if reused else RunOutcomeKind.EVALUATED
    run.run_state = "evaluated"
    run.outcome_kind = outcome_kind.value
    run.evaluation_id = evaluation_row.id
    run.updated_at = utcnow()
    session.flush()
    session.commit()

    return ScaleRunOutcome(
        kind=outcome_kind,
        run_id=run.id,
        enrollment_id=enrollment.id,
        episode_id=episode.id,
        evaluation_id=evaluation_row.id,
    )


def _result_payload(evaluation: ScaleEvaluation) -> dict:
    """The exact payload `evaluate_scale()`'s own Phase 10 hashes (spec
    §2.5), reconstructed here so the stored `canonical_result_payload` and a
    recomputed `result_hash` can be verified against the evaluator's own
    output rather than trusted blindly."""
    return {
        "snapshot_canonical_hash": evaluation.snapshot_canonical_hash,
        "algorithm_version": evaluation.algorithm_version,
        "maturity": evaluation.maturity,
        "data_quality_state": evaluation.data_quality_state,
        "health": evaluation.health,
        "health_reason": evaluation.health_reason,
        "signals": evaluation.signals,
        "advice_candidates": evaluation.advice_candidates,
        "streak_state": evaluation.streak_state,
    }


def _json_scalar(value):
    return json.loads(canonical_encode(value))


def _insert_or_load_evaluation(
    session: Session,
    context: TenantContext,
    episode: ScaleEpisodeRecord,
    snapshot: ScaleEvidenceSnapshot,
    evaluation: ScaleEvaluation,
) -> tuple[ScaleEvaluationRecord, bool]:
    normalized_target_key = (
        snapshot.target.target_version_id if snapshot.target is not None else NO_TARGET_MARKER
    )
    fingerprint = dict(
        workspace_id=context.workspace_id,
        meta_mode=context.meta_mode,
        episode_id=episode.id,
        snapshot_canonical_hash=snapshot.canonical_hash,
        metric_semantic_contract_set_hash=snapshot.metric_contract_set_hash,
        policy_version_id=snapshot.policy.policy_version_id,
        normalized_target_key=normalized_target_key,
        algorithm_version=evaluation.algorithm_version,
    )

    def _find_existing() -> ScaleEvaluationRecord | None:
        return session.execute(
            select(ScaleEvaluationRecord).filter_by(**fingerprint)
        ).scalar_one_or_none()

    existing = _find_existing()
    if existing is not None:
        if existing.result_hash != evaluation.result_hash:
            raise ScaleOrchestrationError(
                "evaluation_fingerprint_conflict",
                f"existing={existing.result_hash} computed={evaluation.result_hash}",
            )
        return existing, True

    payload = _result_payload(evaluation)
    recomputed_hash = canonical_hash(payload, domain=_RESULT_HASH_DOMAIN)
    if recomputed_hash != evaluation.result_hash:
        raise ScaleOrchestrationError("result_hash_mismatch")
    canonical_payload = json.loads(canonical_encode(payload))

    compat = evaluation.streak_state.compatibility
    evaluation_row = ScaleEvaluationRecord(
        id=new_id("sev"),
        workspace_id=context.workspace_id,
        meta_mode=context.meta_mode,
        episode_id=episode.id,
        snapshot_id=snapshot.snapshot_id,
        snapshot_canonical_hash=snapshot.canonical_hash,
        metric_semantic_contract_set_hash=snapshot.metric_contract_set_hash,
        policy_version_id=snapshot.policy.policy_version_id,
        target_version_id=(snapshot.target.target_version_id if snapshot.target is not None else None),
        normalized_target_key=normalized_target_key,
        algorithm_version=evaluation.algorithm_version,
        canonical_schema_version=_EVALUATION_CANONICAL_SCHEMA_VERSION,
        maturity=evaluation.maturity.value,
        data_quality_state=evaluation.data_quality_state.value,
        health=evaluation.health.value if evaluation.health is not None else None,
        health_reason=evaluation.health_reason,
        streak_compat_policy_version_id=compat.policy_version_id,
        streak_compat_target_version_id=compat.target_version_id,
        streak_compat_metric_contract_set_hash=compat.metric_contract_set_hash,
        streak_compat_configuration_baseline_hash=compat.configuration_baseline_hash,
        streak_compat_data_continuity_generation=compat.data_continuity_generation,
        streak_compat_canonical_hash=compat.canonical_hash,
        streak_healthy_evaluations=evaluation.streak_state.healthy_evaluations,
        canonical_result_payload=canonical_payload,
        result_hash=evaluation.result_hash,
    )

    signal_rows = [
        ScaleEvaluationSignalRecord(
            id=new_id("sevs"),
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            evaluation_id=evaluation_row.id,
            signal_id=s.signal_id,
            state=s.state.value,
            expected=s.expected,
            observed=s.observed,
            interval_lower=s.interval_lower,
            interval_upper=s.interval_upper,
            threshold=s.threshold,
            p_value=s.p_value,
            adjusted_q_value=s.adjusted_q_value,
            reason_code=s.reason_code,
        )
        for s in evaluation.signals
    ]
    advice_rows = [
        ScaleEvaluationAdviceCandidateRecord(
            id=new_id("seac"),
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            evaluation_id=evaluation_row.id,
            advice_type=c.advice_type.value,
            reason_signature=c.reason_signature,
            severity=c.severity.value,
            signal_ids=list(c.signal_ids),
            evidence=[{"key": e.key, "value": _json_scalar(e.value)} for e in c.evidence],
        )
        for c in evaluation.advice_candidates
    ]
    counter_rows = [
        ScaleEvaluationStreakCounterRecord(
            id=new_id("sesc"),
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            evaluation_id=evaluation_row.id,
            signal_key=c.signal_key,
            consecutive_hits=c.consecutive_hits,
        )
        for c in evaluation.streak_state.counters
    ]
    reset_rows = [
        ScaleEvaluationStreakResetRecord(
            id=new_id("sesr"),
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            evaluation_id=evaluation_row.id,
            streak_id=r.streak_id,
            previous_value=r.previous_value,
            reason_code=r.reason_code,
        )
        for r in evaluation.streak_state.resets
    ]

    try:
        with session.begin_nested():
            session.add(evaluation_row)
            session.flush()
            session.add_all(signal_rows + advice_rows + counter_rows + reset_rows)
            session.flush()
    except IntegrityError:
        recovered = _find_existing()
        if recovered is None:
            raise
        if recovered.result_hash != evaluation.result_hash:
            raise ScaleOrchestrationError(
                "evaluation_fingerprint_conflict",
                f"existing={recovered.result_hash} computed={evaluation.result_hash}",
            )
        return recovered, True

    return evaluation_row, False


def _decide_projection_eligibility(
    episode: ScaleEpisodeRecord, run: ScaleEvaluationRunRecord, snapshot: ScaleEvidenceSnapshot,
) -> bool:
    """Spec §3.4 step 3: eligibility is based on account-local day, source
    revision, semantic-event sequence, and replay lineage -- not insertion
    time or lexical Evaluation ID. Forward-replay lineage reconciliation
    (only a final head may project) is out of this task's scope: a
    `forward_replay` run is simply never eligible here."""
    if run.trigger_kind not in ("live", "policy_change", "target_change"):
        return False
    new_cursor = (
        snapshot.as_of_local_day,
        run.source_revision,
        run.semantic_event_sequence,
        episode.projection_lineage_generation,
    )
    if episode.accepted_evaluation_id is None:
        return True
    old_cursor = (
        episode.projection_as_of_local_day,
        episode.projection_source_revision,
        episode.projection_semantic_event_sequence,
        episode.projection_lineage_generation,
    )
    return new_cursor > old_cursor


def _apply_projection(
    session: Session,
    context: TenantContext,
    episode: ScaleEpisodeRecord,
    run: ScaleEvaluationRunRecord,
    snapshot: ScaleEvidenceSnapshot,
    evaluation: ScaleEvaluation,
    evaluation_row: ScaleEvaluationRecord,
) -> None:
    lineage_generation = episode.projection_lineage_generation
    episode.projection_revision += 1
    episode.projection_as_of_local_day = snapshot.as_of_local_day
    episode.projection_source_revision = run.source_revision
    episode.projection_semantic_event_sequence = run.semantic_event_sequence
    episode.projection_lineage_generation = lineage_generation
    episode.accepted_evaluation_id = evaluation_row.id
    episode.latest_source_manifest_id = run.source_manifest_id

    streak_counters_json = [
        {"signal_key": c.signal_key, "consecutive_hits": c.consecutive_hits}
        for c in evaluation.streak_state.counters
    ]

    projection = session.execute(
        select(ScaleEpisodeProjectionRecord).filter_by(
            workspace_id=context.workspace_id, meta_mode=context.meta_mode, episode_id=episode.id,
        )
    ).scalar_one_or_none()
    health_value = evaluation.health.value if evaluation.health is not None else None
    if projection is None:
        projection = ScaleEpisodeProjectionRecord(
            id=new_id("sepp"),
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            episode_id=episode.id,
            evaluation_id=evaluation_row.id,
            maturity=evaluation.maturity.value,
            health=health_value,
            health_reason=evaluation.health_reason,
            healthy_evaluations=evaluation.streak_state.healthy_evaluations,
            streak_counters=streak_counters_json,
            cursor_as_of_local_day=snapshot.as_of_local_day,
            cursor_source_revision=run.source_revision,
            cursor_semantic_event_sequence=run.semantic_event_sequence,
            cursor_lineage_generation=lineage_generation,
            revision=1,
        )
        session.add(projection)
    else:
        projection.evaluation_id = evaluation_row.id
        projection.maturity = evaluation.maturity.value
        projection.health = health_value
        projection.health_reason = evaluation.health_reason
        projection.healthy_evaluations = evaluation.streak_state.healthy_evaluations
        projection.streak_counters = streak_counters_json
        projection.cursor_as_of_local_day = snapshot.as_of_local_day
        projection.cursor_source_revision = run.source_revision
        projection.cursor_semantic_event_sequence = run.semantic_event_sequence
        projection.cursor_lineage_generation = lineage_generation
        projection.revision += 1
        projection.updated_at = utcnow()
    session.flush()


def _no_delivery_streak(evaluation: ScaleEvaluation) -> int:
    for counter in evaluation.streak_state.counters:
        if counter.signal_key == "no_delivery_complete_day":
            return counter.consecutive_hits
    return 0


def _append_run_audit(
    session: Session,
    context: TenantContext,
    run: ScaleEvaluationRunRecord,
    enrollment: ScaleEnrollmentRecord,
    episode: ScaleEpisodeRecord,
    evaluation_row: ScaleEvaluationRecord,
    evaluation: ScaleEvaluation,
    reused: bool,
) -> None:
    """Spec §3.4 step 7: audit event carrying the system executor, original
    Enrollment creator/initiator, triggering lineage, run/snapshot/Evaluation
    IDs, and result hash. Reuses the platform's generic `AuditEventRecord`
    (no Scale-specific audit table exists yet)."""
    session.add(
        AuditEventRecord(
            id=new_id("audit"),
            workspace_id=context.workspace_id,
            initiator_id=run.original_initiator_id or enrollment.original_initiator_id,
            initiator_role=context.audit_initiator_role,
            executor=context.executor,
            request_id=context.request_id,
            trace_id=context.trace_id,
            authorization_epoch=context.authorization_epoch,
            meta_mode=context.meta_mode,
            event_type="scale_evaluation_already_evaluated" if reused else "scale_evaluation_evaluated",
            resource_type="scale_evaluation_run",
            resource_id=run.id,
            details_json={
                "run_id": run.id,
                "snapshot_id": run.snapshot_id,
                "evaluation_id": evaluation_row.id,
                "result_hash": evaluation.result_hash,
                "episode_id": episode.id,
                "enrollment_id": enrollment.id,
                "triggering_sync_run_id": run.triggering_sync_run_id,
                "triggering_semantic_event_id": run.triggering_semantic_event_id,
                "original_initiator_id": enrollment.original_initiator_id,
            },
        )
    )
