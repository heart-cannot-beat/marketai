"""Localization eligibility module (Issue #24, spec §2F, §16.1).

``list_eligible_winners()`` is a discovery-only read of
``ContextualVerdictProjectionRecord`` -- the rebuildable, potentially
stale read projection maintained by the Experiments module (spec
§16.1). It exists purely to let the UI/API surface candidate Winners for
display and never gates any Localization command.

``validate_source_for_batch()`` is the sole authoritative gate. Checks
1-7 and 11 of spec §4.2 (Decision identity/verdict, candidate/version
consistency, confirmation, the exact source AssetVersion's current
human review state, Product/Offer Library version state, and the current
``ExperimentEligibilityProjectionRecord`` chain) are delegated to
``app.experiments.winner_eligibility.evaluate_winner_identity()`` --
Issue #86's shared, hardened extraction of these same checks (this module
was their original home; see that module's docstring for exactly why it
excludes the delivery-context checks 8-10). This function then adds the
two checks that remain specific to Localization: that the resolved
Winner's exact AssetVersion matches the source being localized, and that
the requested evidence tier is compatible with the Winner's own evidence
mode. Decision, confirmation and candidate establish immutable identity
and provenance; the current eligibility projection establishes *current*
eligibility -- they are deliberately different questions (spec §16.1).
The validation runs at selection, at final confirmation, before every new
external/paid step, and before publication/adoption (spec §2F) -- it is
never run once and cached.

A stale discovery projection may still display a Winner
(``verdict='win'`` in ``ContextualVerdictProjectionRecord``); only
``validate_source_for_batch`` can reject it once current eligibility,
review, or Truth state has moved on. ``list_eligible_winners`` therefore
never filters by DataSource/evidence mode -- it is a hint, not a gate.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..adapters.persistence.models import ContextualVerdictProjectionRecord, ExperimentVersionCandidateRecord
from ..experiments.winner_eligibility import WinnerIneligibleError, evaluate_winner_identity
from ..platform.tenancy import TenantContext
from .evidence_tier import tier_of_experiment, tier_of_source

# Winner verdict vocabulary shared with `ContextualVerdictProjectionRecord`
# and `ExperimentDecisionRecord.verdict` (app/experiments/service.py).
WINNER_VERDICT = "win"
INCONCLUSIVE_VERDICT = "inconclusive"

# There is no dedicated "how real was the experiment launch" field yet:
# Meta write access stays disabled in development (CLAUDE.md invariant)
# and `ExperimentVersionCandidateRecord.execution_mode` is unconditionally
# "mock" until a real-provider Launch path exists (app/experiments/service.py
# always writes execution_mode="mock" at freeze). Translating the
# candidate's execution mode into the production/sandbox/mock vocabulary
# `evidence_tier.tier_of_experiment()` expects (spec §3C) is therefore the
# accurate value today; a later task that records the real Meta launch
# environment on the candidate/experiment replaces this mapping without
# changing callers. Tier normalization itself always goes through
# `evidence_tier.py`'s `tier_of_source`/`tier_of_experiment` -- the single
# source of truth for mode->tier -- never a locally redeclared map.
_EXPERIMENT_MODE_OF_EXECUTION_MODE = {
    "byok": "production",
    "sandbox": "sandbox",
    "mock": "mock",
}


@dataclass(frozen=True)
class EligibleWinner:
    asset_version_id: str
    decision_id: str
    experiment_id: str
    experiment_version_id: str
    experiment_version_candidate_id: str
    source_generation_mode: str
    experiment_evidence_mode: str
    evidence_origin: str = "legacy_experiment"


@dataclass(frozen=True)
class ValidationReport:
    valid: bool
    findings: tuple[str, ...]


def list_eligible_winners(
    session: Session,
    ctx: TenantContext,
    *,
    requested_evidence_tier: str | None = None,
) -> list[EligibleWinner]:
    """Discovery-only listing of candidate Winners for display.

    Reads only ``ContextualVerdictProjectionRecord`` -- never the
    authoritative Decision, confirmation, or current eligibility chain --
    and is never filtered by DataSource/evidence mode (spec §2F, §16.1).
    Callers must still run ``validate_source_for_batch()`` before acting
    on any entry returned here. ``requested_evidence_tier`` is accepted
    only for interface symmetry with the authoritative call; discovery
    never narrows on it.
    """
    del requested_evidence_tier  # discovery never filters by evidence mode

    rows = session.scalars(
        select(ContextualVerdictProjectionRecord).where(
            ContextualVerdictProjectionRecord.workspace_id == ctx.workspace_id,
            ContextualVerdictProjectionRecord.verdict == WINNER_VERDICT,
        )
    ).all()

    winners: list[EligibleWinner] = []
    for row in rows:
        candidate = session.get(ExperimentVersionCandidateRecord, row.candidate_id)
        execution_mode = candidate.execution_mode if candidate is not None else ""
        from app.adapters.persistence.models import ExperimentVersionRecord
        from app.marketing.evidence_handoff import origin
        version = session.scalar(select(ExperimentVersionRecord).where(ExperimentVersionRecord.workspace_id == ctx.workspace_id,
            ExperimentVersionRecord.id == row.experiment_version_id))
        marketing = bool(version and origin(version))
        if marketing:
            try:
                _, findings = evaluate_winner_identity(session, ctx, decision_id=row.decision_id)
                if findings:
                    continue
            except WinnerIneligibleError:
                continue
        winners.append(
            EligibleWinner(
                evidence_origin="marketing_business_target" if marketing else "legacy_experiment",
                asset_version_id=row.asset_version_id,
                decision_id=row.decision_id,
                experiment_id=row.experiment_id,
                experiment_version_id=row.experiment_version_id,
                experiment_version_candidate_id=row.candidate_id,
                source_generation_mode=execution_mode,
                experiment_evidence_mode=_EXPERIMENT_MODE_OF_EXECUTION_MODE.get(
                    execution_mode, ""
                ),
            )
        )
    return winners


def validate_source_for_batch(
    session: Session,
    ctx: TenantContext,
    asset_version_id: str,
    decision_id: str,
    *,
    requested_evidence_tier: str,
) -> ValidationReport:
    """The sole authoritative gate for using a Winner as a Localization source.

    Delegates checks 1-7 and 11 of spec §4.2 (Decision identity/verdict,
    candidate/version consistency, confirmation, the AssetVersion's
    current human review state, Product/Offer Library version state, and
    the current ``ExperimentEligibilityProjectionRecord`` chain) to
    ``app.experiments.winner_eligibility.evaluate_winner_identity()`` --
    never ``ContextualVerdictProjectionRecord`` (spec §2F, §16.1). Then
    adds the two checks that remain specific to Localization: that the
    resolved Winner's exact AssetVersion matches ``asset_version_id``, and
    that ``requested_evidence_tier`` is compatible with the Winner's
    evidence mode. Findings are collected rather than short-circuited
    (except when the Decision itself cannot be resolved at all), so a
    caller sees every reason a source is ineligible in one call.
    """
    try:
        identity, shared_findings = evaluate_winner_identity(session, ctx, decision_id=decision_id)
    except WinnerIneligibleError as exc:
        return ValidationReport(valid=False, findings=tuple(f.code for f in exc.findings))

    findings: list[str] = [f.code for f in shared_findings]

    candidate = identity.candidate
    if candidate is not None and candidate.asset_version_id != asset_version_id:
        # Localization-specific: the resolved Winner's exact AssetVersion
        # (checks 1-7/11 already validated its existence and review state)
        # must be the same AssetVersion the caller is localizing.
        findings.append("asset_version_mismatch")

    if candidate is not None:
        experiment_mode = _EXPERIMENT_MODE_OF_EXECUTION_MODE.get(candidate.execution_mode)
        try:
            source_tier = tier_of_source(candidate.execution_mode)
            experiment_tier = tier_of_experiment(experiment_mode) if experiment_mode else None
        except KeyError:
            source_tier = None
            experiment_tier = None
        try:
            requested_tier = tier_of_experiment(requested_evidence_tier)
        except KeyError:
            requested_tier = None

        if source_tier is None or experiment_tier is None:
            findings.append("unknown_evidence_mode")
        elif requested_tier is None:
            findings.append("unknown_requested_evidence_tier")
        else:
            winner_tier = min(source_tier, experiment_tier)
            if requested_tier > winner_tier:
                findings.append("incompatible_evidence_tier")

    return ValidationReport(valid=not findings, findings=tuple(findings))
