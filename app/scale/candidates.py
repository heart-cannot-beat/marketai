"""Enrollment Candidates: the two-tier server-derived lineage gate (spec
§4.4, issue #86 Task 9).

Two tiers, matching the spec's "discovery is never a gate" rule:

* :func:`list_eligible_candidates` -- a read-only *discovery* scan over
  confirmed Winner Decisions that currently pass the lighter identity/
  eligibility checks (``app.experiments.winner_eligibility.
  evaluate_winner_identity``, checks 1-7 and 11) and have no live
  Enrollment yet. Purely advisory: nothing about a discovery result is
  persisted or trusted by any command. A caller still has to name an exact
  ``(decision_id, delivery_target_id)`` pair to actually create a
  Candidate, and that creation call re-validates everything from scratch.
* :func:`create_enrollment_candidate` -- the only writer of
  ``ScaleEnrollmentCandidateRecord``. Claims idempotency first (spec §7.1
  lock ordering: Journal -> Guard -> Candidate), then delegates the actual
  lineage proof to ``app.scale.enrollment_lineage.
  lock_and_validate_enrollment_lineage()`` (which itself locks/creates the
  ``ScaleLineageGuardRecord``), and freezes the result's
  ``candidate_fingerprint``/guard revision onto the new row.
* :func:`consume_candidate` -- atomic single-use claim via
  ``UPDATE ... WHERE consumed_at IS NULL``, no SELECT-then-UPDATE window.
  Being consumed does not by itself prove the lineage is still valid at the
  moment of consumption; ``app.scale.enrollment.create_draft_enrollment()``
  re-runs the full lineage gate under a freshly re-locked guard before
  trusting this row (see ``app.scale.enrollment_lineage``'s module
  docstring).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.experiments.winner_eligibility import (
    WinnerIneligibleError,
    evaluate_winner_identity,
)
from app.platform.tenancy import TenantContext

from .command_journal import claim_command, command_request_fingerprint
from .enrollment_lineage import (
    DeliveryTargetExpiredError,
    DeliveryTargetNotFoundError,
    lock_and_validate_enrollment_lineage,
)
from .models import (
    ScaleDeliveryTargetRecord,
    ScaleEnrollmentCandidateRecord,
    ScaleEnrollmentRecord,
)

__all__ = [
    "CandidateDiscoveryItem",
    "CandidateNotFoundError",
    "CandidateAlreadyConsumedError",
    "CandidateExpiredError",
    "DeliveryTargetNotFoundError",
    "DeliveryTargetExpiredError",
    "list_eligible_candidates",
    "create_enrollment_candidate",
    "consume_candidate",
]

#: How long a freshly created Candidate stays consumable before it must be
#: re-created (spec §4.4: short-lived proof, not a durable resource).
CANDIDATE_TTL = timedelta(hours=1)

#: Lifecycle states that mean "this Winner Decision already has a live
#: Enrollment" for discovery purposes -- draft counts too, since discovery
#: should not repeatedly surface a Decision someone has already started
#: enrolling (spec §4.4: discovery, not a race invitation).
_LIVE_ENROLLMENT_LIFECYCLES = ("draft", "active", "paused")


class CandidateNotFoundError(Exception):
    pass


class CandidateAlreadyConsumedError(Exception):
    pass


class CandidateExpiredError(Exception):
    pass


@dataclass(frozen=True)
class CandidateDiscoveryItem:
    """One advisory discovery row (spec §4.4). Never persisted, never
    accepted back as input to any command -- a caller must still name the
    exact ``(decision_id, delivery_target_id)`` pair to
    :func:`create_enrollment_candidate`, which re-validates independently."""

    decision_id: str
    confirmation_id: str | None
    experiment_version_id: str | None
    candidate_id: str | None
    asset_version_id: str | None
    evidence_origin: str = "legacy_experiment"
    delivery_targets: tuple[dict, ...] = field(default_factory=tuple)
    delivery_unavailable_reason: str | None = None


def list_eligible_candidates(
    session: Session,
    context: TenantContext,
) -> list[CandidateDiscoveryItem]:
    """Read-only discovery: confirmed Winner Decisions in this Workspace
    that currently pass the identity/eligibility checks
    (``evaluate_winner_identity``, spec §4.2 checks 1-7/11) and have no
    live Enrollment yet. Never gates any command -- purely advisory."""
    from app.adapters.persistence.models import ExperimentDecisionRecord

    live_decision_ids = set(
        session.execute(
            select(ScaleEnrollmentRecord.winner_decision_id).filter(
                ScaleEnrollmentRecord.workspace_id == context.workspace_id,
                ScaleEnrollmentRecord.lifecycle.in_(_LIVE_ENROLLMENT_LIFECYCLES),
            )
        ).scalars().all()
    )

    decisions = session.execute(
        select(ExperimentDecisionRecord).filter(
            ExperimentDecisionRecord.workspace_id == context.workspace_id,
            ExperimentDecisionRecord.verdict == "win",
        )
    ).scalars().all()

    items: list[CandidateDiscoveryItem] = []
    for decision in decisions:
        if decision.id in live_decision_ids:
            continue
        try:
            identity, findings = evaluate_winner_identity(
                session, context, decision_id=decision.id,
            )
        except WinnerIneligibleError:
            continue
        if findings:
            continue
        from app.marketing.evidence_handoff import origin, check_projection
        from app.marketing.service import MarketingError
        marketing = bool(identity.experiment_version and origin(identity.experiment_version))
        if marketing:
            try:
                check_projection(session, context, identity.experiment_version, identity.candidate, require_combination=True)
            except MarketingError:
                continue
        delivery_targets, delivery_reason = (), None
        if marketing:
            delivery_targets, delivery_reason = marketing_delivery_choices(session, context, decision.id)
        items.append(CandidateDiscoveryItem(
            evidence_origin="marketing_confirmed_combination" if marketing else "legacy_experiment",
            delivery_targets=delivery_targets, delivery_unavailable_reason=delivery_reason,
            decision_id=decision.id,
            confirmation_id=identity.confirmation.id if identity.confirmation else None,
            experiment_version_id=(
                identity.experiment_version.id if identity.experiment_version else None
            ),
            candidate_id=identity.candidate.id if identity.candidate else None,
            asset_version_id=identity.asset_version.id if identity.asset_version else None,
        ))
    return items


def create_enrollment_candidate(
    session: Session,
    context: TenantContext,
    *,
    decision_id: str,
    delivery_target_id: str,
    idempotency_key: str,
) -> ScaleEnrollmentCandidateRecord:
    """Create (or replay) one short-lived Candidate proving ``decision_id``
    and ``delivery_target_id`` are, right now, an exactly-compatible
    lineage pair. Raises ``WinnerIneligibleError``, ``DeliveryTargetNotFoundError``,
    ``DeliveryTargetExpiredError``, or ``WinnerTargetMismatchError`` when
    they are not.
    """
    candidate_id = f"secand_{uuid.uuid4().hex[:20]}"

    # ``claim_command`` requires an explicit ``scenario_scope_key`` under
    # ``controlled_mock`` and must run before any row lock (command_journal
    # module docstring): a non-locking lookup of the target's own scenario
    # scope satisfies both -- and doubles as the not-found check when the
    # target id was never issued.
    scenario_scope_key = None
    if context.meta_mode == "controlled_mock":
        scenario_scope_key = session.execute(
            select(ScaleDeliveryTargetRecord.scenario_scope_key).filter_by(
                workspace_id=context.workspace_id,
                meta_mode=context.meta_mode,
                id=delivery_target_id,
            )
        ).scalar_one_or_none()
        if scenario_scope_key is None:
            raise DeliveryTargetNotFoundError(delivery_target_id)

    claim = claim_command(
        session, context,
        command_type="create_candidate",
        deduplication_key=idempotency_key,
        result_id=candidate_id,
        request_fingerprint=command_request_fingerprint(
            decision_id=decision_id,
            delivery_target_id=delivery_target_id,
        ),
        scenario_scope_key=scenario_scope_key,
    )
    if claim.replay:
        existing = session.get(
            ScaleEnrollmentCandidateRecord, claim.record.result_resource_id,
        )
        if existing is None:
            raise CandidateNotFoundError(claim.record.result_resource_id)
        return existing

    lineage = lock_and_validate_enrollment_lineage(
        session, context,
        decision_id=decision_id,
        delivery_target_id=delivery_target_id,
    )

    now = datetime.now(timezone.utc)
    candidate = ScaleEnrollmentCandidateRecord(
        id=candidate_id,
        workspace_id=context.workspace_id,
        meta_mode=context.meta_mode,
        scenario_scope_key=lineage.target.scenario_scope_key,
        winner_decision_id=decision_id,
        delivery_target_id=delivery_target_id,
        lineage_guard_id=lineage.guard.id,
        candidate_fingerprint=lineage.candidate_fingerprint,
        guard_revision_at_creation=lineage.guard_revision,
        created_by=context.initiator_id or context.executor,
        expires_at=now + CANDIDATE_TTL,
        created_at=now,
    )
    session.add(candidate)
    session.flush()
    return candidate


def consume_candidate(
    session: Session,
    context: TenantContext,
    candidate_id: str,
) -> ScaleEnrollmentCandidateRecord:
    """Atomically claim ``candidate_id`` for one-time use: an
    ``UPDATE ... WHERE consumed_at IS NULL`` CAS, no SELECT-then-UPDATE
    window. Raises ``CandidateNotFoundError`` when the id was never issued
    to this Workspace/mode, ``CandidateAlreadyConsumedError`` when it was
    already claimed."""
    now = datetime.now(timezone.utc)
    row = session.execute(
        update(ScaleEnrollmentCandidateRecord)
        .where(
            ScaleEnrollmentCandidateRecord.workspace_id == context.workspace_id,
            ScaleEnrollmentCandidateRecord.meta_mode == context.meta_mode,
            ScaleEnrollmentCandidateRecord.id == candidate_id,
            ScaleEnrollmentCandidateRecord.consumed_at.is_(None),
        )
        .values(consumed_at=now)
        .returning(ScaleEnrollmentCandidateRecord)
    ).scalar_one_or_none()
    if row is not None:
        return row

    existing = session.execute(
        select(ScaleEnrollmentCandidateRecord).filter_by(
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            id=candidate_id,
        )
    ).scalar_one_or_none()
    if existing is None:
        raise CandidateNotFoundError(candidate_id)
    raise CandidateAlreadyConsumedError(candidate_id)


def marketing_delivery_choices(session, context, decision_id):
    """Read-only hints; creation still locks and revalidates the exact pair."""
    from app.experiments.winner_eligibility import validate_winner_eligibility
    from .enrollment_lineage import _load_and_validate_target, _assert_winner_target_compatible, WinnerTargetMismatchError
    try:
        winner = validate_winner_eligibility(session, context, decision_id=decision_id)
    except WinnerIneligibleError as exc:
        return (), exc.findings[0].code if exc.findings else "source_not_ready"
    targets = session.scalars(select(ScaleDeliveryTargetRecord).where(ScaleDeliveryTargetRecord.workspace_id == context.workspace_id,
        ScaleDeliveryTargetRecord.meta_mode == context.meta_mode,
        ScaleDeliveryTargetRecord.creative_asset_version_id == winner.asset_version_id)).all()
    choices = []
    for record in targets:
        try:
            target = _load_and_validate_target(session, context, record.id)
            _assert_winner_target_compatible(winner, target)
        except (DeliveryTargetNotFoundError, DeliveryTargetExpiredError, WinnerTargetMismatchError):
            continue
        choices.append({"id": target.id, "label": f"{target.market_code} · {target.placement} · Meta ad {target.ad_remote_id}",
            "source_type": target.source_type})
    return tuple(choices), None if choices else "compatible_delivery_target_required"
