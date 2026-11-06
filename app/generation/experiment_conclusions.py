"""Contextual experiment conclusion projections (Issue #49 Task 6, spec §0.7).

Winner/Learning/Loser is a conclusion about an exact ``AssetVersion`` plus
a full experiment context (experiment, market, placement, landing-page
version, and Offer version) -- never a global property collapsed onto the
AssetVersion itself. This module is the read projection over the
authoritative experiment decision records that produces that conclusion,
with its full context, for the gallery filter/badge and the detail
lightbox (spec §0.2/§0.7).

Two backing sources are read:

* ``ContextualVerdictProjectionRecord`` (``contextual_verdict_projections``)
  -- the rebuildable, always-fully-anchored current-verdict projection
  Issue #22's ``app.experiments.service.confirm_decision_proposal`` (and
  its equivalents) maintain. Every row here has a complete context by
  construction (``market``/``placement``/``offer_version_id`` are all
  ``NOT NULL``), so it never needs an anchoring check. Its ``verdict``
  column uses the domain vocabulary ``win``/``learn``/``lose``; this module
  translates that to the browser contract's ``winner``/``learning``/
  ``loser`` (spec §0.7's ``ContextualExperimentConclusion.verdict``).

* ``VerdictDecisionRecord`` (``verdict_decisions``) -- the legacy
  "append-only compatibility record until ExperimentEntry decisions land"
  (see its class docstring). ExperimentEntry decisions have since landed
  (the source above), so this table is dead for new writes, but old rows
  may still exist. Per spec §0.7, a legacy row "may be surfaced only when
  it can be anchored to an exact AssetVersion and complete context" --
  this table has no ``asset_version_id`` column of its own, only a JSON
  ``context_json`` blob, so anchoring means ``context_json`` carries
  ``asset_version_id``, ``experiment_id``, ``market`` and ``placement``,
  all non-empty. Anything less (the common case for genuinely legacy data)
  is excluded rather than guessed at.

``context_id`` (the opaque reference the browser round-trips as the
``experiment_context`` query/selector value, spec §0.4/§0.7) is the
``experiment_id`` itself: this schema's ``experiment_versions`` table has a
``UNIQUE (experiment_id)`` constraint (one frozen version per Experiment),
so market/placement/offer/landing-page context is already 1:1 with
``experiment_id`` in practice -- there is no separate "context" identity to
mint. ``metric_snapshot_id``/``rule_version_id`` (spec §0.7's immutable refs
to "the metric set that produced the verdict" / "the decision-rule version
applied") have no dedicated columns yet either; this module uses the
``ExperimentDecisionRecord`` id and the projection's ``confirmation_id`` as
the closest immutable stand-ins until Issue #22 grows dedicated columns --
see the Task 6 report for this concern.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.persistence.models import (
    ContextualVerdictProjectionRecord,
    ExperimentDecisionRecord,
    VerdictDecisionRecord,
)
from app.platform.tenancy import TenantContext

__all__ = [
    "ExperimentContextRef",
    "ContextualExperimentConclusion",
    "VERDICT_LABELS",
    "project_contextual_conclusions",
    "project_contextual_conclusion_for_context",
]

# Domain vocabulary (contextual_verdict_projections.verdict) -> browser
# contract vocabulary (ContextualExperimentConclusion.verdict, spec §0.7).
# Public so app.generation.asset_queries's SQL-level gallery filter/summary
# can restate the same mapping as a SQL CASE without duplicating the literal
# pairs.
VERDICT_LABELS = {"win": "winner", "learn": "learning", "lose": "loser"}
_VERDICT_MAP = VERDICT_LABELS

# The legacy VerdictDecisionRecord.context_json keys that must all be
# present and non-empty for a row to be "anchored to an exact AssetVersion
# and complete context" (spec §0.7).
_LEGACY_REQUIRED_CONTEXT_KEYS = ("asset_version_id", "experiment_id", "market", "placement")


@dataclass(frozen=True)
class ExperimentContextRef:
    """Spec §0.7's ``ExperimentContextRef`` -- the full context a conclusion
    is anchored to, never collapsed into just the experiment id."""

    context_id: str
    experiment_id: str
    market: str
    placement: str
    landing_page_version_id: str
    offer_version_id: str
    metric_snapshot_id: str
    rule_version_id: str

    def as_dict(self) -> dict:
        return {
            "context_id": self.context_id,
            "experiment_id": self.experiment_id,
            "market": self.market,
            "placement": self.placement,
            "landing_page_version_id": self.landing_page_version_id,
            "offer_version_id": self.offer_version_id,
            "metric_snapshot_id": self.metric_snapshot_id,
            "rule_version_id": self.rule_version_id,
        }


@dataclass(frozen=True)
class ContextualExperimentConclusion:
    """Spec §0.7's ``ContextualExperimentConclusion``."""

    decision_id: str
    asset_version_id: str
    verdict: str
    context: ExperimentContextRef
    evidence_refs: list[str] = field(default_factory=list)
    confirmed_at: str | None = None
    revoked_at: str | None = None

    def as_dict(self) -> dict:
        return {
            "decision_id": self.decision_id,
            "asset_version_id": self.asset_version_id,
            "verdict": self.verdict,
            "context": self.context.as_dict(),
            "evidence_refs": list(self.evidence_refs),
            "confirmed_at": self.confirmed_at,
            "revoked_at": self.revoked_at,
        }


def _authoritative_conclusions(
    session: Session, ctx: TenantContext, version_id: str
) -> list[ContextualExperimentConclusion]:
    """Rows from the authoritative, always-fully-anchored projection."""
    rows = session.execute(
        select(ContextualVerdictProjectionRecord).where(
            ContextualVerdictProjectionRecord.workspace_id == ctx.workspace_id,
            ContextualVerdictProjectionRecord.asset_version_id == version_id,
            ContextualVerdictProjectionRecord.revoked_at.is_(None),
        )
    ).scalars().all()

    out: list[ContextualExperimentConclusion] = []
    for row in rows:
        verdict = _VERDICT_MAP.get(row.verdict, row.verdict)

        decision = session.execute(
            select(ExperimentDecisionRecord).where(
                ExperimentDecisionRecord.workspace_id == ctx.workspace_id,
                ExperimentDecisionRecord.id == row.decision_id,
            )
        ).scalar_one_or_none()
        evidence_refs = list(decision.reason_codes_json or []) if decision else []

        context = ExperimentContextRef(
            context_id=row.experiment_id,
            experiment_id=row.experiment_id,
            market=row.market,
            placement=row.placement,
            landing_page_version_id=row.landing_page_evidence_hash,
            offer_version_id=row.offer_version_id,
            metric_snapshot_id=row.decision_id,
            rule_version_id=row.confirmation_id,
        )
        out.append(
            ContextualExperimentConclusion(
                decision_id=row.decision_id,
                asset_version_id=row.asset_version_id,
                verdict=verdict,
                context=context,
                evidence_refs=evidence_refs,
                confirmed_at=row.confirmed_at.isoformat() if row.confirmed_at else None,
                revoked_at=row.revoked_at.isoformat() if row.revoked_at else None,
            )
        )
    return out


def _legacy_conclusions(
    session: Session, ctx: TenantContext, version_id: str
) -> list[ContextualExperimentConclusion]:
    """Legacy ``VerdictDecisionRecord`` rows, surfaced only when anchored to
    this exact AssetVersion and a complete context (spec §0.7)."""
    rows = session.execute(
        select(VerdictDecisionRecord).where(
            VerdictDecisionRecord.workspace_id == ctx.workspace_id,
        )
    ).scalars().all()

    out: list[ContextualExperimentConclusion] = []
    for row in rows:
        context_json = row.context_json or {}
        if context_json.get("asset_version_id") != version_id:
            continue
        if not all(context_json.get(key) for key in _LEGACY_REQUIRED_CONTEXT_KEYS):
            continue
        if context_json.get("revoked"):
            continue

        experiment_id = context_json["experiment_id"]
        context = ExperimentContextRef(
            context_id=experiment_id,
            experiment_id=experiment_id,
            market=context_json["market"],
            placement=context_json["placement"],
            landing_page_version_id=context_json.get("landing_page_version_id", ""),
            offer_version_id=context_json.get("offer_version_id", ""),
            metric_snapshot_id=context_json.get("metric_snapshot_id", ""),
            rule_version_id=context_json.get("rule_version_id", ""),
        )
        out.append(
            ContextualExperimentConclusion(
                decision_id=row.id,
                asset_version_id=version_id,
                verdict=row.verdict or "",
                context=context,
                evidence_refs=list(context_json.get("evidence_refs", [])),
                confirmed_at=row.created_at.isoformat() if row.created_at else None,
                revoked_at=None,
            )
        )
    return out


def project_contextual_conclusions(
    session: Session, ctx: TenantContext, version_id: str
) -> list[dict]:
    """All non-revoked, fully-anchored ``ContextualExperimentConclusion``
    rows for one AssetVersion (spec §0.7) -- the detail lightbox's
    ``contextual_conclusions[]`` (spec §0.2) and the source Tasks 8/9
    consume for Loser exclusion / ``winner_only`` export selection.

    Workspace-scoped like every other projection in this module family --
    a ``version_id`` from another Workspace simply matches nothing rather
    than raising, since this is a pure read projection with no notion of
    "the version must exist" (callers that need that already resolved the
    version before calling this).
    """
    conclusions = _authoritative_conclusions(session, ctx, version_id)
    conclusions.extend(_legacy_conclusions(session, ctx, version_id))
    return [c.as_dict() for c in conclusions]


def project_contextual_conclusion_for_context(
    session: Session, ctx: TenantContext, version_id: str, context_id: str
) -> str | None:
    """The single ``verdict`` (``winner``/``learning``/``loser``) for one
    AssetVersion anchored to one exact ``context_id``, or ``None`` when no
    non-revoked, fully-anchored conclusion exists there.

    Gallery use (spec §0.2): the ``contextual_conclusion`` summary field is
    populated only for the requested ``experiment_context`` -- it is never
    a global property of the AssetVersion. Task 8 (Loser exclusion) uses
    this same function to decide whether a stable dimension signature's
    newest source AssetVersion has a confirmed Loser conclusion in the
    active generation/regeneration context.
    """
    for conclusion in project_contextual_conclusions(session, ctx, version_id):
        if conclusion["context"]["context_id"] == context_id:
            return conclusion["verdict"]
    return None
