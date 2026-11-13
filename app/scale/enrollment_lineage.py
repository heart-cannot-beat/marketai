"""The sole authoritative Winner/DeliveryTarget lineage gate (spec §4.4,
issue #86 Task 9).

``lock_and_validate_enrollment_lineage()`` is the one place in Scale that
proves a Winner Decision and a DeliveryTarget are *exactly* the same offer,
market, placement, landing page, and attribution context, not merely that
both independently exist. It is deliberately re-entrant and cheap to call
more than once per request: a preliminary (unlocked) validation runs first
so the expensive Winner-eligibility/DeliveryTarget reads happen before any
row lock is taken (lock ordering, spec §7.4: Journal -> Guard -> Candidate/
Enrollment), then the ``ScaleLineageGuardRecord`` for the pair is locked
(creating it on first use) and *both* reads are repeated under that lock --
discarding the preliminary result entirely -- because the Task 8 reviewer
note this task inherits is explicit: the guard's own three unenforced-at-
the-database-level FKs (``winner_decision_id``, ``winner_confirmation_id``,
``market_asset_binding_id``) mean only this authoritative re-validation, not
the guard row's mere existence, proves current lineage validity.

``lock_and_validate_enrollment_candidate()`` is a thin convenience wrapper
that resolves an already-created ``ScaleEnrollmentCandidateRecord`` back to
its ``(decision_id, delivery_target_id)`` pair and delegates to the function
above -- used by ``app.scale.enrollment.create_draft_enrollment()`` to
re-validate a candidate's lineage at draft-creation time, exactly as the
guard itself is never blindly trusted.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.experiments.winner_eligibility import (
    EligibleWinnerLineage,
    validate_winner_eligibility,
)
from app.platform.denial_audit import check_and_record_mode_denial
from app.platform.tenancy import TenantContext

from .canonical_lineage import canonical_lineage_hash
from .lineage_guard import ScaleLineageGuardRecord, get_or_create_lineage_guard
from .models import ScaleDeliveryTargetRecord, ScaleEnrollmentCandidateRecord

__all__ = [
    "ValidatedEnrollmentLineage",
    "WinnerTargetMismatchError",
    "DeliveryTargetNotFoundError",
    "DeliveryTargetExpiredError",
    "lock_and_validate_enrollment_lineage",
    "lock_and_validate_enrollment_candidate",
]

#: Schema version folded into every ``candidate_fingerprint`` this module
#: computes (spec §4.7.2). Bump whenever the hashed field set below changes.
_CANDIDATE_FINGERPRINT_VERSION = 1


@dataclass(frozen=True)
class ValidatedEnrollmentLineage:
    """The complete, currently-valid proof one Winner Decision and one
    DeliveryTarget are an exactly-compatible pair, as of this call."""

    winner: EligibleWinnerLineage
    target: ScaleDeliveryTargetRecord
    guard: ScaleLineageGuardRecord
    candidate_fingerprint: str
    guard_revision: int


class WinnerTargetMismatchError(Exception):
    """The Winner's frozen delivery context and the DeliveryTarget's own
    frozen context disagree on ``field`` -- they are not the same offer/
    market/placement/landing-page/attribution slot."""

    def __init__(self, field: str, winner_value: str, target_value: str) -> None:
        self.field = field
        self.winner_value = winner_value
        self.target_value = target_value
        super().__init__(
            f"winner_target_mismatch: {field} winner={winner_value!r} target={target_value!r}"
        )


class DeliveryTargetNotFoundError(Exception):
    pass


class DeliveryTargetExpiredError(Exception):
    pass


class EnrollmentCandidateNotFoundError(Exception):
    """Raised only by ``lock_and_validate_enrollment_candidate`` when the
    candidate id itself does not resolve. Distinct from
    ``app.scale.candidates.CandidateNotFoundError`` to avoid a circular
    import (``app.scale.candidates`` imports this module); the two are
    re-unified by ``app.scale.candidates`` re-raising this one under its own
    public name -- see that module."""


def lock_and_validate_enrollment_lineage(
    session: Session,
    context: TenantContext,
    *,
    decision_id: str,
    delivery_target_id: str,
) -> ValidatedEnrollmentLineage:
    """The sole authoritative gate joining Winner and delivery context (spec
    §4.4):

    1. Validate winner eligibility (all 11 checks; collects findings).
    2. Load and validate the DeliveryTarget (exists, not expired).
    3. Lock (or create) the pair's ``ScaleLineageGuardRecord``.
    4. Discard both preliminary reads; re-validate winner and target again,
       now under the guard's lock.
    5. Prove exact Winner/Target field-for-field compatibility.
    6. Compute the versioned ``candidate_fingerprint``.

    Raises ``WinnerIneligibleError``, ``DeliveryTargetNotFoundError``,
    ``DeliveryTargetExpiredError``, or ``WinnerTargetMismatchError``.
    """
    # Preliminary (unlocked) reads -- done before any row lock, per the lock
    # ordering the module docstring describes.
    preliminary_winner = validate_winner_eligibility(session, context, decision_id=decision_id)
    preliminary_target = _load_and_validate_target(session, context, delivery_target_id)

    guard = get_or_create_lineage_guard(
        session, context,
        winner_decision_id=decision_id,
        winner_confirmation_id=preliminary_winner.confirmation_id,
        asset_version_id=preliminary_winner.asset_version_id,
        product_version_id=preliminary_winner.product_version_id,
        offer_version_id=preliminary_winner.offer_version_id,
        market_asset_binding_id=preliminary_winner.market_asset_binding_id,
        delivery_target_id=delivery_target_id,
        meta_connection_id=preliminary_target.meta_connection_id,
        contract_generation_id=preliminary_target.contract_generation_id,
        scenario_scope_key=preliminary_target.scenario_scope_key,
    )

    # Authoritative re-validation: discard the preliminary reads entirely
    # and re-read both, now that the guard is locked (see module docstring
    # / Task 8 reviewer note).
    winner = validate_winner_eligibility(session, context, decision_id=decision_id)
    target = _load_and_validate_target(session, context, delivery_target_id)

    _assert_winner_target_compatible(winner, target)

    fingerprint = canonical_lineage_hash(
        _CANDIDATE_FINGERPRINT_VERSION,
        {
            "decision_id": winner.decision_id,
            "confirmation_id": winner.confirmation_id,
            "asset_version_id": winner.asset_version_id,
            "product_version_id": winner.product_version_id,
            "offer_version_id": winner.offer_version_id,
            "market_code": winner.market_code,
            "placement": winner.placement,
            # Historical key name; the value is the frozen evidence hash (#129).
            "landing_page_version_id": winner.landing_page_evidence_hash,
            "market_asset_binding_id": winner.market_asset_binding_id,
            "attribution_semantics_hash": winner.attribution_semantics_hash,
            "delivery_target_id": target.id,
            "target_fingerprint": target.target_fingerprint,
            "guard_id": guard.id,
            "guard_revision": guard.revision,
        },
    )

    return ValidatedEnrollmentLineage(
        winner=winner,
        target=target,
        guard=guard,
        candidate_fingerprint=fingerprint,
        guard_revision=guard.revision,
    )


def lock_and_validate_enrollment_candidate(
    session: Session,
    context: TenantContext,
    *,
    candidate_id: str,
) -> ValidatedEnrollmentLineage:
    """Resolve ``candidate_id`` to its ``(decision_id, delivery_target_id)``
    pair and delegate to :func:`lock_and_validate_enrollment_lineage` --
    the candidate row is never trusted on its own (module docstring)."""
    candidate = session.execute(
        select(ScaleEnrollmentCandidateRecord).filter_by(
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            id=candidate_id,
        )
    ).scalar_one_or_none()
    if candidate is None:
        raise EnrollmentCandidateNotFoundError(candidate_id)
    return lock_and_validate_enrollment_lineage(
        session, context,
        decision_id=candidate.winner_decision_id,
        delivery_target_id=candidate.delivery_target_id,
    )


def _load_and_validate_target(
    session: Session, context: TenantContext, target_id: str,
) -> ScaleDeliveryTargetRecord:
    target = session.execute(
        select(ScaleDeliveryTargetRecord).filter_by(
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            id=target_id,
        )
    ).scalar_one_or_none()
    if target is None:
        # Same-workspace/different-mode and cross-workspace access attempts
        # are both classified and audited here, before the not-found error
        # (issue #86 Task 9; see ``app.platform.denial_audit.
        # check_and_record_mode_denial``'s own docstring). The error the
        # caller sees is unchanged either way -- this is observability
        # only, never a different response for the requester.
        check_and_record_mode_denial(
            session, context, ScaleDeliveryTargetRecord, target_id,
            resource_kind="scale.delivery_target",
        )
        raise DeliveryTargetNotFoundError(target_id)
    expires_at = target.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        raise DeliveryTargetExpiredError(target_id)
    return target


def _assert_winner_target_compatible(
    winner: EligibleWinnerLineage,
    target: ScaleDeliveryTargetRecord,
) -> None:
    """Field-for-field proof the Winner's frozen delivery context and the
    DeliveryTarget's own frozen context are the same slot (spec §4.4)."""
    checks = (
        ("asset_version_id", winner.asset_version_id, target.creative_asset_version_id),
        ("market_code", winner.market_code, target.market_code),
        ("placement", winner.placement, target.placement),
        ("offer_version_id", winner.offer_version_id, target.offer_version_id),
        ("landing_page_evidence_hash", winner.landing_page_evidence_hash, target.landing_page_evidence_hash),
        ("attribution_semantics_hash", winner.attribution_semantics_hash, target.attribution_setting),
    )
    for field, winner_value, target_value in checks:
        if winner_value != target_value:
            raise WinnerTargetMismatchError(field, winner_value, target_value)
