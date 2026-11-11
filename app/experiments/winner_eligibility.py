"""Shared Winner eligibility for Scale enrollment (Issue #86, spec §4.2).

``validate_winner_eligibility()`` is the sole authoritative proof that a
confirmed Experiment Decision ("Winner") is eligible for Scale enrollment.
It runs the complete 11-check list from spec §4.2 -- Decision identity,
candidate/version consistency, confirmation, AssetVersion existence and
review, Product/Offer Library state, frozen delivery-context resolution
(market/placement/offer/landing-page/attribution), MarketAssetBinding
readiness, reference/usage-rights and localized-truth currency, and current
``ExperimentEligibilityProjection`` state -- and returns an
``EligibleWinnerLineage`` (all fields proven) or raises
``WinnerIneligibleError`` with every collected finding. Findings are
collected, never short-circuited, *except* when the Decision itself cannot
be resolved at all (workspace-scoped not-found), which raises immediately
with a single finding (spec §4.2). It deliberately knows nothing about
delivery targets (``ScaleDeliveryTargetRecord``, §4.3) -- that exact
compatibility join is ``app.scale.enrollment_lineage.
lock_and_validate_enrollment_lineage()`` (§4.4), a later task's module.

``evaluate_winner_identity()`` is the subset of these checks (1-7 and 11)
that concerns the Winner's own identity and *current* Experiment-side
eligibility -- Decision verdict, candidate/version consistency,
confirmation, AssetVersion review, Product/Offer withdrawal, and the
current eligibility projection (including QC and stale-Truth events). It
deliberately excludes the delivery-context checks (8-10: frozen
market/placement/offer/landing-page/attribution resolution,
MarketAssetBinding readiness, reference-rights/localized-truth currency)
because those concern the *target* delivery slot a Winner is bound into --
Localization is what *creates* that binding, so requiring one to already
exist would make it impossible to ever localize a fresh Winner.
``app.localization.eligibility.validate_source_for_batch`` delegates to
this narrower helper for exactly that reason, then layers its own
localization-specific checks (source AssetVersion match, evidence-tier
compatibility) on top. ``validate_winner_eligibility()`` calls this same
helper first, then layers checks 8-10 on top for the full Scale lineage.

Finding codes for checks 1-7 and 11 intentionally reuse the vocabulary
``app.localization.eligibility.validate_source_for_batch`` already shipped
and tests (``no_decision``, ``inconclusive_decision``,
``non_winner_decision``, ``missing_candidate``, ``missing_experiment_version``,
``missing_confirmation``, ``no_approved_review``/``review_rejected``/
``review_needs_changes``, ``product_withdrawn``, ``offer_withdrawn``, and the
raw ``upstream_event_type`` string for a current ineligibility event) so
delegation needs no translation layer. Checks 8-10 introduce new codes
(``judgement_insufficient_data``, ``binding_not_ready``,
``reference_rights_ineligible``) that only ``validate_winner_eligibility()``
can raise.

Landing Page: the Landing Page belongs to the OfferVersion
(creative-library contract §5.2) and has no version entity of its own. An
ExperimentVersion freezes it as ``landing_page_evidence_hash``, and
``EligibleWinnerLineage.landing_page_evidence_hash`` is that frozen hash for
every Winner, legacy experiment and marketing handoff alike (#129). A
DeliveryTarget carries the hash frozen by the ExperimentVersion its ad came
from, and the lineage gate compares the two.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.persistence.models import (
    AssetVersionRecord,
    DecisionConfirmationRecord,
    ExperimentDecisionRecord,
    ExperimentEligibilityEventRecord,
    ExperimentEligibilityProjectionRecord,
    ExperimentVersionCandidateRecord,
    ExperimentVersionRecord,
    JudgementRecord,
    LibraryVersionRecord,
    MarketAssetBindingRecord,
    TruthPackageRecord,
)
from app.generation.review import project_effective_review
from app.platform.canonical import _canonical_bytes, _sha256
from app.platform.tenancy import TenantContext

__all__ = [
    "EligibleWinnerLineage",
    "WinnerEligibilityFinding",
    "WinnerIneligibleError",
    "WinnerIdentity",
    "attribution_semantics_hash_for",
    "evaluate_winner_identity",
    "validate_winner_eligibility",
]


def _versioned_hash(version: str, mapping: object) -> str:
    """Version-prefixed SHA-256 over canonical JSON (spec §4.7)."""
    return _sha256(f"{version}:".encode("utf-8") + _canonical_bytes(mapping))


def attribution_semantics_hash_for(click_window_days: int, view_window_days: int) -> str:
    """The ``attribution-semantics-v1`` hash of a click/view window pair.

    A Winner hashes the windows its ExperimentVersion froze; a DeliveryTarget
    hashes the windows its Scale metric contracts read with (#129), so the
    lineage gate compares what the Winner was judged on with what Scale
    measures.
    """
    return _versioned_hash(
        "attribution-semantics-v1",
        {"click_window_days": click_window_days, "view_window_days": view_window_days},
    )


def _aware_utc(value: datetime) -> datetime:
    """SQLite has no real timezone type; ``DateTime(timezone=True)`` columns
    round-trip as naive there. Every value this module reads is UTC (spec
    §4.7 requires RFC 3339 timestamps normalized to UTC), so a naive value
    means UTC -- `_canonical_bytes` rejects naive datetimes outright."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class WinnerEligibilityFinding:
    code: str
    detail: str


class WinnerIneligibleError(Exception):
    def __init__(self, findings: tuple[WinnerEligibilityFinding, ...]) -> None:
        self.findings = findings
        super().__init__(f"Winner ineligible: {[f.code for f in findings]}")


@dataclass(frozen=True)
class EligibleWinnerLineage:
    """Fully proven Winner lineage (spec §4.2). All fields present and
    independently verified against append-only/authoritative records."""

    decision_id: str
    confirmation_id: str
    experiment_version_id: str
    experiment_version_candidate_id: str
    asset_version_id: str
    product_version_id: str
    offer_version_id: str
    market_code: str
    placement: str
    landing_page_evidence_hash: str
    market_asset_binding_id: str
    attribution_semantics_hash: str
    reference_rights_hash: str
    winner_fingerprint: str


@dataclass(frozen=True)
class WinnerIdentity:
    """Resolved records for checks 1-7 and 11 (spec §4.2). Any field may be
    ``None`` when its owning check failed -- callers must consult
    ``findings`` before trusting a field's presence."""

    decision: ExperimentDecisionRecord
    candidate: ExperimentVersionCandidateRecord | None
    experiment_version: ExperimentVersionRecord | None
    confirmation: DecisionConfirmationRecord | None
    asset_version: AssetVersionRecord | None
    quality_review_state: str | None
    product_version: LibraryVersionRecord | None
    offer_version: LibraryVersionRecord | None
    eligibility_projection_revision: int


def _resolve_current_ineligibility(
    session: Session,
    context: TenantContext,
    *,
    experiment_id: str,
    experiment_version_id: str,
    candidate_id: str,
) -> tuple[str | None, int]:
    """The upstream event type behind the first current ineligibility found,
    plus the projection's ``head_sequence`` (used as
    ``eligibility_projection_revision`` in the winner fingerprint, spec
    §4.7.1). Checks the candidate-level projection first, then the
    experiment-version-level projection -- either can independently make a
    Winner ineligible (candidate-precise invalidation such as
    ``stale_truth_detected``; version-wide Product/Offer withdrawal or
    QC/review events). Shared with
    ``app.localization.eligibility``'s identical original check.
    """
    checks = (
        ("candidate", ExperimentEligibilityProjectionRecord.candidate_id == candidate_id),
        ("version", ExperimentEligibilityProjectionRecord.candidate_id.is_(None)),
    )
    revision = 0
    for level, candidate_filter in checks:
        proj = session.scalar(
            select(ExperimentEligibilityProjectionRecord).where(
                ExperimentEligibilityProjectionRecord.workspace_id == context.workspace_id,
                ExperimentEligibilityProjectionRecord.experiment_id == experiment_id,
                ExperimentEligibilityProjectionRecord.experiment_version_id
                == experiment_version_id,
                ExperimentEligibilityProjectionRecord.level == level,
                candidate_filter,
            )
        )
        if proj is None:
            continue
        revision = max(revision, proj.head_sequence)
        if proj.current_eligibility == "ineligible" or proj.irreversible_active:
            event = None
            if proj.head_event_id is not None:
                event = session.get(ExperimentEligibilityEventRecord, proj.head_event_id)
            code = event.upstream_event_type if event is not None else "ineligible"
            return code, revision
    return None, revision


def evaluate_winner_identity(
    session: Session,
    context: TenantContext,
    *,
    decision_id: str,
) -> tuple[WinnerIdentity, tuple[WinnerEligibilityFinding, ...]]:
    """Checks 1-7 and 11 of spec §4.2: the Winner's own identity and
    *current* Experiment-side eligibility, independent of any delivery
    context. Raises ``WinnerIneligibleError`` with a single
    ``no_decision`` finding when the Decision itself cannot be resolved
    (early exit); every other check's finding is collected, never
    short-circuited.
    """
    findings: list[WinnerEligibilityFinding] = []

    # 1. ExperimentDecisionRecord: exists, workspace match (early exit).
    decision = session.execute(
        select(ExperimentDecisionRecord).where(
            ExperimentDecisionRecord.workspace_id == context.workspace_id,
            ExperimentDecisionRecord.id == decision_id,
        )
    ).scalar_one_or_none()
    if decision is None:
        raise WinnerIneligibleError((
            WinnerEligibilityFinding("no_decision", f"Decision {decision_id} not found"),
        ))

    # 1 (cont.). verdict == 'win'.
    if decision.verdict == "inconclusive":
        findings.append(WinnerEligibilityFinding(
            "inconclusive_decision", "Decision verdict is 'inconclusive', not 'win'",
        ))
    elif decision.verdict != "win":
        findings.append(WinnerEligibilityFinding(
            "non_winner_decision", f"Decision verdict is '{decision.verdict}', not 'win'",
        ))

    # 2. Candidate + ExperimentVersion resolution; judgement sufficiency.
    candidate: ExperimentVersionCandidateRecord | None = None
    if decision.experiment_version_candidate_id is not None:
        candidate = session.scalar(
            select(ExperimentVersionCandidateRecord).where(
                ExperimentVersionCandidateRecord.workspace_id == context.workspace_id,
                ExperimentVersionCandidateRecord.id
                == decision.experiment_version_candidate_id,
            )
        )
    if candidate is None or candidate.experiment_version_id != decision.experiment_version_id:
        findings.append(WinnerEligibilityFinding(
            "missing_candidate", "Decision has no exact winning candidate",
        ))
        candidate = None

    experiment_version: ExperimentVersionRecord | None = None
    if decision.experiment_version_id is not None:
        experiment_version = session.scalar(
            select(ExperimentVersionRecord).where(
                ExperimentVersionRecord.workspace_id == context.workspace_id,
                ExperimentVersionRecord.id == decision.experiment_version_id,
            )
        )
    if experiment_version is None:
        findings.append(WinnerEligibilityFinding(
            "missing_experiment_version", "Decision's ExperimentVersion not found",
        ))

    judgement = session.scalar(
        select(JudgementRecord).where(
            JudgementRecord.workspace_id == context.workspace_id,
            JudgementRecord.id == decision.judgement_id,
        )
    )
    if judgement is None:
        findings.append(WinnerEligibilityFinding(
            "judgement_insufficient_data",
            "Judgement backing this Decision was not found",
        ))

    # 3. DecisionConfirmationRecord: exists, is the decision's exact
    #    confirmation. (No decision-revocation event type exists yet in
    #    this schema -- that concern folds into check 11 below, matching
    #    the upstream_event_type/upstream_resource_type vocabularies.)
    confirmation: DecisionConfirmationRecord | None = None
    if decision.confirmation_id is None:
        findings.append(WinnerEligibilityFinding(
            "missing_confirmation", "Decision has no confirmation",
        ))
    else:
        confirmation = session.scalar(
            select(DecisionConfirmationRecord).where(
                DecisionConfirmationRecord.workspace_id == context.workspace_id,
                DecisionConfirmationRecord.id == decision.confirmation_id,
            )
        )
        if confirmation is None or confirmation.proposal_id != decision.proposal_id:
            findings.append(WinnerEligibilityFinding(
                "missing_confirmation",
                "Confirmation not found or does not match the Decision's proposal",
            ))
            confirmation = None

    # 4. AssetVersion: exists in this workspace.
    asset_version: AssetVersionRecord | None = None
    if candidate is not None:
        asset_version = session.scalar(
            select(AssetVersionRecord).where(
                AssetVersionRecord.workspace_id == context.workspace_id,
                AssetVersionRecord.id == candidate.asset_version_id,
            )
        )
        if asset_version is None:
            findings.append(WinnerEligibilityFinding(
                "missing_asset_version", "Winning AssetVersion not found",
            ))

    # 5. QualityReview: 'approved' (supersession-aware, spec §0.3).
    quality_review_state: str | None = None
    if asset_version is not None:
        quality_review_state = project_effective_review(session, context, asset_version.id)
        if quality_review_state != "approved":
            if quality_review_state == "rejected":
                findings.append(WinnerEligibilityFinding(
                    "review_rejected", "AssetVersion's effective review is 'rejected'",
                ))
            elif quality_review_state == "needs_changes":
                findings.append(WinnerEligibilityFinding(
                    "review_needs_changes",
                    "AssetVersion's effective review is 'needs_changes'",
                ))
            else:
                findings.append(WinnerEligibilityFinding(
                    "no_approved_review", "AssetVersion has no approved effective review",
                ))

    # 6/7. Product/Offer Library version state.
    product_version: LibraryVersionRecord | None = None
    offer_version: LibraryVersionRecord | None = None
    if experiment_version is not None:
        product_version = session.get(LibraryVersionRecord, experiment_version.product_version_id)
        offer_version = session.get(LibraryVersionRecord, experiment_version.offer_version_id)
        if product_version is None or product_version.state == "withdrawn":
            findings.append(WinnerEligibilityFinding(
                "product_withdrawn", "Product Library version is withdrawn or missing",
            ))
        if offer_version is None or offer_version.state == "withdrawn":
            findings.append(WinnerEligibilityFinding(
                "offer_withdrawn", "Offer Library version is withdrawn or missing",
            ))

    # 11. ExperimentEligibilityProjection: not 'ineligible'/'irreversible_active'.
    eligibility_projection_revision = 0
    if experiment_version is not None and candidate is not None:
        ineligibility_code, revision = _resolve_current_ineligibility(
            session, context,
            experiment_id=decision.experiment_id,
            experiment_version_id=experiment_version.id,
            candidate_id=candidate.id,
        )
        eligibility_projection_revision = revision
        if ineligibility_code is not None:
            findings.append(WinnerEligibilityFinding(
                ineligibility_code, "Current eligibility projection is ineligible",
            ))

    # Compatibility rows never confer eligibility by their legacy `win` bit.
    # Recheck the authoritative marketing input on every downstream admission.
    from app.marketing.evidence_handoff import check_projection, origin, POLICY
    from app.marketing.service import MarketingError
    if experiment_version is not None:
        if bool(origin(experiment_version)) != bool(judgement and judgement.algorithm_version == POLICY):
            findings.append(WinnerEligibilityFinding("marketing_handoff_invalid", "Marketing origin/algorithm mismatch"))
        elif origin(experiment_version):
            try:
                check_projection(session, context, experiment_version, candidate)
            except MarketingError as exc:
                findings.append(WinnerEligibilityFinding(exc.code, exc.detail))

    identity = WinnerIdentity(
        decision=decision,
        candidate=candidate,
        experiment_version=experiment_version,
        confirmation=confirmation,
        asset_version=asset_version,
        quality_review_state=quality_review_state,
        product_version=product_version,
        offer_version=offer_version,
        eligibility_projection_revision=eligibility_projection_revision,
    )
    return identity, tuple(findings)


def validate_winner_eligibility(
    session: Session,
    context: TenantContext,
    *,
    decision_id: str,
) -> EligibleWinnerLineage:
    """Returns proven lineage or raises ``WinnerIneligibleError`` (spec
    §4.2). Runs the complete 11-check list: ``evaluate_winner_identity()``
    (checks 1-7, 11) plus the delivery-context checks (8-10): frozen
    market/placement/offer/landing-page/attribution resolution, exact
    MarketAssetBinding readiness, and reference-rights/localized-truth
    currency. All findings are collected -- never short-circuited -- except
    when the Decision itself is missing (raised by
    ``evaluate_winner_identity()`` as an early exit).

    On success, computes ``winner_fingerprint`` from all verified Winner
    fields (spec §4.7.1).
    """
    identity, findings = evaluate_winner_identity(session, context, decision_id=decision_id)
    findings = list(findings)
    decision = identity.decision
    now = datetime.now(timezone.utc)

    market_code = placement = landing_page_evidence_hash = ""
    attribution_semantics_hash = ""
    binding: MarketAssetBindingRecord | None = None
    reference_rights_hash = ""

    if identity.experiment_version is not None:
        from app.marketing.evidence_handoff import check_projection
        from app.marketing.service import MarketingError
        try:
            check_projection(session, context, identity.experiment_version, identity.candidate, require_combination=True)
        except MarketingError as exc:
            findings.append(WinnerEligibilityFinding(exc.code, exc.detail))

    # 8. Resolve the exact frozen market/placement/Offer/Landing Page and
    #    attribution semantics from the frozen ExperimentVersion.
    if identity.experiment_version is not None:
        experiment_version = identity.experiment_version
        market_code = experiment_version.market
        placement = experiment_version.placement
        landing_page_evidence_hash = experiment_version.landing_page_evidence_hash
        attribution_semantics_hash = attribution_semantics_hash_for(
            experiment_version.attribution_click_window_days,
            experiment_version.attribution_view_window_days,
        )

        # 9. MarketAssetBinding: exact AssetVersion + market + offer match,
        #    truth_freshness != 'stale', active, ready_for_experiment.
        #    (No `placement` column exists on `market_asset_bindings` in
        #    the current schema -- matched by source AssetVersion, target
        #    market and Offer version only; see module docstring.)
        if identity.asset_version is not None:
            binding = session.scalar(
                select(MarketAssetBindingRecord).where(
                    MarketAssetBindingRecord.workspace_id == context.workspace_id,
                    MarketAssetBindingRecord.source_asset_version_id
                    == identity.asset_version.id,
                    MarketAssetBindingRecord.target_market == market_code,
                    MarketAssetBindingRecord.offer_version_id
                    == experiment_version.offer_version_id,
                    MarketAssetBindingRecord.active.is_(True),
                )
            )
            if binding is None:
                findings.append(WinnerEligibilityFinding(
                    "binding_not_ready",
                    "No active MarketAssetBinding for this AssetVersion/market/offer",
                ))
            elif (
                binding.truth_freshness == "stale"
                or not binding.active
                or not binding.ready_for_experiment
            ):
                findings.append(WinnerEligibilityFinding(
                    "binding_not_ready",
                    f"MarketAssetBinding {binding.id} is not experiment-ready "
                    f"(truth_freshness={binding.truth_freshness!r}, "
                    f"active={binding.active}, "
                    f"ready_for_experiment={binding.ready_for_experiment})",
                ))

            # 10. Reference/usage rights and localized-truth eligibility
            #     are currently valid.
            if binding is not None:
                truth_package = session.scalar(
                    select(TruthPackageRecord).where(
                        TruthPackageRecord.workspace_id == context.workspace_id,
                        TruthPackageRecord.id == binding.truth_package_id,
                    )
                )
                valid_until = (
                    _aware_utc(truth_package.valid_until) if truth_package is not None else None
                )
                if (
                    truth_package is None
                    or truth_package.status == "failed"
                    or valid_until < now
                ):
                    findings.append(WinnerEligibilityFinding(
                        "reference_rights_ineligible",
                        "Localized-truth package backing this binding is not currently valid",
                    ))
                else:
                    reference_rights_hash = _versioned_hash(
                        "reference-rights-v1",
                        {
                            "truth_package_id": truth_package.id,
                            "truth_package_status": truth_package.status,
                            "truth_package_canonical_hash": truth_package.canonical_hash,
                            "market_asset_binding_id": binding.id,
                        },
                    )

    if findings:
        raise WinnerIneligibleError(tuple(findings))

    assert identity.candidate is not None
    assert identity.experiment_version is not None
    assert identity.confirmation is not None
    assert identity.asset_version is not None
    assert identity.product_version is not None
    assert identity.offer_version is not None
    assert binding is not None

    winner_fingerprint = _versioned_hash(
        "winner-v1",
        {
            "decision_id": decision.id,
            "decision_verdict": decision.verdict,
            "decision_reason_codes": list(decision.reason_codes_json or []),
            "confirmation_id": identity.confirmation.id,
            "confirmation_confirmed_at": _aware_utc(identity.confirmation.confirmed_at),
            "experiment_version_id": identity.experiment_version.id,
            "experiment_version_candidate_id": identity.candidate.id,
            "asset_version_id": identity.asset_version.id,
            "quality_review_state": identity.quality_review_state,
            "product_version_id": identity.product_version.id,
            "product_version_state": identity.product_version.state,
            "offer_version_id": identity.offer_version.id,
            "offer_version_state": identity.offer_version.state,
            "market_code": market_code,
            "placement": placement,
            # Historical key name; the value is the frozen evidence hash (#129).
            "landing_page_version_id": landing_page_evidence_hash,
            "attribution_semantics_hash": attribution_semantics_hash,
            "binding_id": binding.id,
            "binding_truth_freshness": binding.truth_freshness,
            "binding_active": binding.active,
            "binding_ready_for_experiment": binding.ready_for_experiment,
            "reference_rights_hash": reference_rights_hash,
            "eligibility_projection_revision": identity.eligibility_projection_revision,
        },
    )

    return EligibleWinnerLineage(
        decision_id=decision.id,
        confirmation_id=identity.confirmation.id,
        experiment_version_id=identity.experiment_version.id,
        experiment_version_candidate_id=identity.candidate.id,
        asset_version_id=identity.asset_version.id,
        product_version_id=identity.product_version.id,
        offer_version_id=identity.offer_version.id,
        market_code=market_code,
        placement=placement,
        landing_page_evidence_hash=landing_page_evidence_hash,
        market_asset_binding_id=binding.id,
        attribution_semantics_hash=attribution_semantics_hash,
        reference_rights_hash=reference_rights_hash,
        winner_fingerprint=winner_fingerprint,
    )
