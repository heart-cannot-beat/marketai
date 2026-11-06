"""Enrollment and Episode lifecycle commands (design §6, spec §3.7).

Enrollment lifecycle is ``draft -> active <-> paused -> closed`` (terminal).
Episode phase is ``collecting -> evaluating -> closed``. This module owns
every state transition for both aggregates and the append-only event log
that makes the mutable projection rows (``ScaleEnrollmentRecord.lifecycle``,
``ScaleEpisodeRecord.phase``) a CAS-maintained projection of that history,
never the source of truth on their own.

Six commands, matching the spec's public interface:

* :func:`create_draft_enrollment` -- new Draft, no binding reservation.
* :func:`activate_enrollment` -- Draft -> active; reserves the normalized
  remote binding and creates the first Episode from an exact frozen
  baseline, in the same transaction.
* :func:`pause_enrollment` -- active -> paused; retains the binding and the
  current Episode untouched, fences new evaluation requests.
* :func:`resume_enrollment` -- paused -> active; continues the current
  Episode when it is still open, or opens a fresh one from a new baseline
  when it was closed (e.g. by :func:`apply_no_delivery_episode_closure`).
* :func:`close_enrollment` -- active/paused -> closed (terminal); closes the
  current Episode and releases the binding.
* :func:`handle_upstream_withdrawal` -- active/paused -> closed with reason
  ``upstream_withdrawal`` (spec §3.9); the same shape as
  :func:`close_enrollment` but system-triggered, unconditional on revision,
  and idempotent on the upstream event's own semantic id.

Plus one orchestration-facing internal command:

* :func:`apply_no_delivery_episode_closure` -- the automatic
  ``no_delivery_7_complete_days`` transition (spec §3.7): closes the Episode
  and moves the Enrollment to `paused`, retaining the binding. Task 12's
  evaluation orchestration is the sole caller, once its own stable-signal
  day counting (distinct complete local days, so same-day reevaluation
  cannot double count) decides the condition is met; this command only
  enforces the resulting state transition, locking, and idempotency.

Scoping decisions made in this task (the full generality is later tasks'
job, per the plan):

* Winner/Approved-status/Offer/Landing-Page/Connection/rights eligibility
  gates live in other bounded contexts this task's "Consumes" list does not
  include (only "ORM records, TenantContext, tenant_session"). This module
  therefore trusts :class:`DraftEnrollmentRequest` and
  :class:`DeliveryConfigBaseline` as already gate-checked by the caller
  (Task 15's router, which owns calling those other modules) and only
  re-validates what it can determine from Scale's own persisted rows:
  remote-binding shape, scenario/mode consistency, revision, lifecycle
  legality, and binding-uniqueness against sibling Enrollments.
* "a fresh reconciled delivery-configuration source" (spec §3.7) is supplied
  by the caller as a :class:`DeliveryConfigBaseline` value, not fetched from
  Meta here -- live evidence ingestion is a separate adapter concern outside
  this task's scope (mirrors Task 10's documented evidence.py scoping).
* Resume's "stable configuration and continuous evidence can continue an
  open Episode" check is limited to "is the current Episode still open".
  Mid-resume delivery-configuration-change rollover (closing an otherwise
  open Episode because the caller's fresh delivery config no longer
  matches) is Task 12's job, per its plan ("Episode rollover during
  Phase A").
* Upstream withdrawal's "supersedes active Advice and blocks new
  conversions" (spec §3.9 step 4) is not implemented here -- the Advice
  aggregate and its lifecycle do not exist until Task 13. This command
  performs every Enrollment/Episode-owned step (1, 2, 3, 5 partially, 6
  partially) and leaves Advice superseding to Task 13's own withdrawal
  hook, which will call this command first under the same lock.
* Issue #86 Task 11 hardening: :func:`activate_enrollment` and
  :func:`resume_enrollment` no longer accept a caller-supplied
  ``delivery_config``/``episode_context`` at all -- both build their
  Episode from server-resolved lineage + evidence via
  ``_build_episode_from_evidence`` (an ``ActivationPreview``-gated
  ``DeliveryConfigBaseline``/``EpisodeContext`` synthesis), never from a
  browser value. ``DeliveryConfigBaseline``/``EpisodeContext``/
  :func:`_build_episode_and_delivery_source` themselves are unchanged and
  stay in use by ``app.scale.service``'s episode-rollover path, which
  already has a genuinely fresh ``ScaleDeliveryConfigSourceRecord`` to
  build from. ``_build_episode_from_evidence``'s own docstring documents
  a known scope gap this inherits from Task 10: the evidence this module
  can reach proves lineage identity and freshness, not genuine Meta
  campaign/ad-set/ad status, budget, optimization, bid, or targeting
  content -- those fields are filled with clearly-labeled deterministic
  non-values pending a future ticket that extends the evidence pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.persistence.models import WorkspaceRecord, new_id, utcnow
from app.meta.models import ObservationContractGenerationRecord
from app.platform.tenancy import TenantContext
from app.scale.canonical_lineage import canonical_lineage_hash
from app.scale.enrollment_lineage import ValidatedEnrollmentLineage
from app.scale.models import (
    ScaleDeliveryConfigSourceRecord,
    ScaleDeliveryEvidenceSnapshotRecord,
    ScaleEnrollmentCandidateRecord,
    ScaleEnrollmentEventRecord,
    ScaleEnrollmentRecord,
    ScaleEpisodeEventRecord,
    ScaleEpisodeRecord,
    ScaleMetricSemanticContractSetMemberRecord,
    ScaleMetricSemanticContractSetRecord,
)

NO_SCENARIO_MARKER = "__no_scenario__"

__all__ = [
    "DraftEnrollmentRequest",
    "DeliveryConfigBaseline",
    "EpisodeContext",
    "EnrollmentLifecycleError",
    "CandidateFingerprintDriftError",
    "create_draft_enrollment",
    "create_draft_enrollment_from_request",
    "activate_enrollment",
    "pause_enrollment",
    "resume_enrollment",
    "close_enrollment",
    "handle_upstream_withdrawal",
    "apply_no_delivery_episode_closure",
]


class CandidateFingerprintDriftError(RuntimeError):
    """Raised by :func:`create_draft_enrollment` when a re-validated
    Candidate's freshly computed ``candidate_fingerprint`` no longer
    matches the one frozen at Candidate-creation time -- the underlying
    lineage moved between creation and consumption. The Candidate is
    consumed regardless (it is single-use either way); the caller must
    request a fresh one."""

    def __init__(self, candidate_id: str) -> None:
        self.candidate_id = candidate_id
        super().__init__(f"candidate_fingerprint_drift: {candidate_id}")


class EnrollmentLifecycleError(RuntimeError):
    """Deterministic, stably-coded rejection raised by every command below."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


# ---------------------------------------------------------------------------
# Caller-supplied request/value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DraftEnrollmentRequest:
    """One Draft Enrollment's caller-verified intent. See module docstring
    for what this module does and does not re-validate."""

    meta_connection_id: str
    ad_account_id: str
    scenario_scope_key: str
    winner_decision_id: str
    winner_confirmation_id: str
    asset_version_id: str
    product_version_id: str
    offer_version_id: str
    market_code: str
    landing_page_version_id: str | None
    market_asset_binding_id: str | None
    campaign_remote_id: str | None
    ad_set_remote_id: str | None
    ad_remote_id: str | None
    creator_id: str


@dataclass(frozen=True)
class DeliveryConfigBaseline:
    """One already-reconciled delivery-configuration evidence observation
    (spec §1.7 ``ScaleDeliveryConfigSourceRecord`` shape), supplied by the
    caller's evidence-ingestion path. Persisted as the Episode's exact
    frozen baseline in the same transaction as activation or a fresh-episode
    resume."""

    contract_generation_id: str
    ad_creative_id: str
    campaign_status: str
    ad_set_status: str
    ad_status: str
    budget_source: str
    budget_amount: Decimal
    optimization_goal: str
    bid_strategy: str
    attribution_setting: str
    targeting_safety_hash: str
    evidence_hash: str
    comparability_hash: str
    raw_response_hash: str
    sync_run_id: str
    occurrence_id: str
    call_intent_id: str
    observed_at: datetime
    reconciliation_id: str | None = None
    offer_version_id: str | None = None
    landing_page_version_id: str | None = None


@dataclass(frozen=True)
class EpisodeContext:
    """The remaining Episode-freezing inputs beyond delivery configuration
    (spec §3.7 Episode invariants): the Scale metric-contract set,
    timezone/currency, and attribution semantics."""

    metric_semantic_contract_set_id: str
    timezone_name: str
    currency: str
    attribution_semantics_hash: str
    started_local_day: date


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _for_update(session: Session, stmt):
    """PostgreSQL row locking; SQLite (tests, bounded local path) is a no-op."""
    if session.get_bind().dialect.name == "postgresql":
        return stmt.with_for_update()
    return stmt


def _executor_id(context: TenantContext) -> str:
    return context.initiator_id or context.executor


def _require_meta_mode(context: TenantContext) -> str:
    if context.meta_mode is None:
        raise EnrollmentLifecycleError("meta_mode_required")
    return context.meta_mode


def _lock_enrollment(session: Session, context: TenantContext, enrollment_id: str) -> ScaleEnrollmentRecord:
    meta_mode = _require_meta_mode(context)
    enrollment = session.execute(
        _for_update(
            session,
            select(ScaleEnrollmentRecord).filter_by(
                workspace_id=context.workspace_id, meta_mode=meta_mode, id=enrollment_id,
            ),
        )
    ).scalar_one_or_none()
    if enrollment is None:
        raise EnrollmentLifecycleError("enrollment_not_found", enrollment_id)
    return enrollment


def _lock_episode(session: Session, context: TenantContext, episode_id: str) -> ScaleEpisodeRecord:
    meta_mode = _require_meta_mode(context)
    episode = session.execute(
        _for_update(
            session,
            select(ScaleEpisodeRecord).filter_by(
                workspace_id=context.workspace_id, meta_mode=meta_mode, id=episode_id,
            ),
        )
    ).scalar_one_or_none()
    if episode is None:
        raise EnrollmentLifecycleError("episode_not_found", episode_id)
    return episode


def _existing_enrollment_event(
    session: Session, context: TenantContext, dedup_key: str,
) -> ScaleEnrollmentEventRecord | None:
    return session.execute(
        select(ScaleEnrollmentEventRecord).filter_by(
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            deduplication_key=dedup_key,
        )
    ).scalar_one_or_none()


def _require_lifecycle(enrollment: ScaleEnrollmentRecord, allowed: tuple[str, ...]) -> None:
    if enrollment.lifecycle not in allowed:
        raise EnrollmentLifecycleError(
            "invalid_transition",
            f"enrollment {enrollment.id} is {enrollment.lifecycle}, expected one of {allowed}",
        )


def _require_revision(enrollment: ScaleEnrollmentRecord, expected_revision: int) -> None:
    if enrollment.revision != expected_revision:
        raise EnrollmentLifecycleError(
            "revision_conflict",
            f"enrollment {enrollment.id} expected revision {expected_revision}, "
            f"actual {enrollment.revision}",
        )


def _validate_scenario_scope(meta_mode: str, scenario_scope_key: str) -> None:
    if meta_mode == "controlled_mock":
        if scenario_scope_key == NO_SCENARIO_MARKER:
            raise EnrollmentLifecycleError(
                "scenario_scope_required", "controlled_mock requires a scenario_scope_key"
            )
    elif scenario_scope_key != NO_SCENARIO_MARKER:
        raise EnrollmentLifecycleError(
            "scenario_scope_forbidden",
            f"{meta_mode} must use the reserved '{NO_SCENARIO_MARKER}' marker",
        )


def _validate_remote_binding_shape(
    campaign_remote_id: str | None, ad_set_remote_id: str | None, ad_remote_id: str | None,
) -> None:
    if not campaign_remote_id:
        raise EnrollmentLifecycleError(
            "remote_binding_shape_invalid", "campaign_remote_id is required"
        )
    if ad_remote_id and not ad_set_remote_id:
        raise EnrollmentLifecycleError(
            "remote_binding_shape_invalid", "ad_remote_id requires ad_set_remote_id"
        )


def _compute_remote_binding_key(
    meta_connection_id: str,
    ad_account_id: str,
    campaign_remote_id: str | None,
    ad_set_remote_id: str | None,
    ad_remote_id: str | None,
) -> str:
    """Server-derived, normalized so a nullable deeper identifier can never
    defeat the active/paused binding-uniqueness index (spec §3.7)."""
    return "/".join(
        (
            meta_connection_id,
            ad_account_id,
            campaign_remote_id or "-",
            ad_set_remote_id or "-",
            ad_remote_id or "-",
        )
    )


def _build_episode_and_delivery_source(
    context: TenantContext,
    enrollment: ScaleEnrollmentRecord,
    delivery_config: DeliveryConfigBaseline,
    episode_context: EpisodeContext,
) -> tuple[ScaleEpisodeRecord, ScaleDeliveryConfigSourceRecord]:
    """Build (not insert) a fresh Episode plus its exact frozen delivery-
    configuration baseline. The Episode is built with the baseline source's
    pre-generated id before that row exists -- `delivery_config_baseline_
    source_id` is a plain string column with no database FK (see models.py),
    so this ordering is safe: the Episode is inserted (and flushed) first,
    then the source row referencing the now-real `episode_id` via its own
    genuine FK."""
    episode_id = new_id("sep")
    delivery_source_id = new_id("sdcs")

    episode = ScaleEpisodeRecord(
        id=episode_id,
        workspace_id=context.workspace_id,
        meta_mode=context.meta_mode,
        enrollment_id=enrollment.id,
        winner_decision_id=enrollment.winner_decision_id,
        winner_confirmation_id=enrollment.winner_confirmation_id,
        asset_version_id=enrollment.asset_version_id,
        product_version_id=enrollment.product_version_id,
        offer_version_id=enrollment.offer_version_id,
        market_code=enrollment.market_code,
        landing_page_version_id=enrollment.landing_page_version_id,
        market_asset_binding_id=enrollment.market_asset_binding_id,
        meta_connection_id=enrollment.meta_connection_id,
        scenario_scope_key=enrollment.scenario_scope_key,
        ad_account_id=enrollment.ad_account_id,
        campaign_remote_id=enrollment.campaign_remote_id,
        ad_set_remote_id=enrollment.ad_set_remote_id,
        ad_remote_id=enrollment.ad_remote_id,
        delivery_config_baseline_source_id=delivery_source_id,
        delivery_evidence_hash=delivery_config.evidence_hash,
        delivery_comparability_hash=delivery_config.comparability_hash,
        metric_semantic_contract_set_id=episode_context.metric_semantic_contract_set_id,
        timezone_name=episode_context.timezone_name,
        currency=episode_context.currency,
        attribution_semantics_hash=episode_context.attribution_semantics_hash,
        phase="collecting",
        started_local_day=episode_context.started_local_day,
        revision=1,
    )

    delivery_source = ScaleDeliveryConfigSourceRecord(
        id=delivery_source_id,
        workspace_id=context.workspace_id,
        meta_mode=context.meta_mode,
        meta_connection_id=enrollment.meta_connection_id,
        scenario_scope_key=enrollment.scenario_scope_key,
        ad_account_id=enrollment.ad_account_id,
        episode_id=episode_id,
        source_revision=1,
        contract_generation_id=delivery_config.contract_generation_id,
        ad_creative_id=delivery_config.ad_creative_id,
        campaign_remote_id=enrollment.campaign_remote_id,
        ad_set_remote_id=enrollment.ad_set_remote_id,
        ad_remote_id=enrollment.ad_remote_id,
        campaign_status=delivery_config.campaign_status,
        ad_set_status=delivery_config.ad_set_status,
        ad_status=delivery_config.ad_status,
        budget_source=delivery_config.budget_source,
        budget_amount=delivery_config.budget_amount,
        currency=episode_context.currency,
        optimization_goal=delivery_config.optimization_goal,
        bid_strategy=delivery_config.bid_strategy,
        attribution_setting=delivery_config.attribution_setting,
        targeting_safety_hash=delivery_config.targeting_safety_hash,
        offer_version_id=delivery_config.offer_version_id,
        landing_page_version_id=delivery_config.landing_page_version_id,
        evidence_hash=delivery_config.evidence_hash,
        comparability_hash=delivery_config.comparability_hash,
        raw_response_hash=delivery_config.raw_response_hash,
        sync_run_id=delivery_config.sync_run_id,
        occurrence_id=delivery_config.occurrence_id,
        call_intent_id=delivery_config.call_intent_id,
        reconciliation_id=delivery_config.reconciliation_id,
        observed_at=delivery_config.observed_at,
    )
    return episode, delivery_source


# ---------------------------------------------------------------------------
# Issue #86 Task 11: build an Episode from server-resolved evidence/lineage
# (never from a browser-supplied DeliveryConfigBaseline/EpisodeContext).
# ---------------------------------------------------------------------------

#: Clearly-labeled non-value for a delivery-config-content field this module
#: has no genuine source for (see ``_build_episode_from_evidence``'s
#: docstring). Never a value a browser could plausibly have supplied.
_UNVERIFIED = "unverified"


def _resolve_metric_semantic_contract_set_id(
    session: Session, context: TenantContext, *, contract_generation_id: str,
) -> str:
    """The single ``ScaleMetricSemanticContractSetRecord`` whose
    ``delivery_configuration`` member wraps this exact contract generation
    (spec §6.11 step 6). Zero or more than one match is a configuration
    error the caller must fix (declare, or deduplicate, the compatible
    set) -- never a lineage failure, so it is never conflated with
    ``EnrollmentLifecycleError``'s lineage-drift codes."""
    rows = session.execute(
        select(ScaleMetricSemanticContractSetRecord.id)
        .join(
            ScaleMetricSemanticContractSetMemberRecord,
            ScaleMetricSemanticContractSetMemberRecord.contract_set_id
            == ScaleMetricSemanticContractSetRecord.id,
        )
        .where(
            ScaleMetricSemanticContractSetRecord.workspace_id == context.workspace_id,
            ScaleMetricSemanticContractSetRecord.meta_mode == context.meta_mode,
            ScaleMetricSemanticContractSetMemberRecord.contract_type == "delivery_configuration",
            ScaleMetricSemanticContractSetMemberRecord.observation_contract_generation
            == contract_generation_id,
        )
    ).scalars().all()
    distinct_ids = set(rows)
    if not distinct_ids:
        raise EnrollmentLifecycleError("metric_contract_set_missing", contract_generation_id)
    if len(distinct_ids) > 1:
        raise EnrollmentLifecycleError("metric_contract_set_ambiguous", contract_generation_id)
    return rows[0]


def _load_contract_generation(
    session: Session, context: TenantContext, *, contract_generation_id: str,
) -> ObservationContractGenerationRecord:
    generation = session.execute(
        select(ObservationContractGenerationRecord).filter_by(
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            id=contract_generation_id,
        )
    ).scalar_one_or_none()
    if generation is None:
        raise EnrollmentLifecycleError("contract_generation_not_found", contract_generation_id)
    return generation


def _load_workspace_default_currency(session: Session, context: TenantContext) -> str:
    currency = session.execute(
        select(WorkspaceRecord.default_currency).filter_by(id=context.workspace_id)
    ).scalar_one_or_none()
    if currency is None:
        raise EnrollmentLifecycleError("workspace_not_found", context.workspace_id)
    return currency


def _compute_lineage_hash(
    lineage: ValidatedEnrollmentLineage, evidence: ScaleDeliveryEvidenceSnapshotRecord,
) -> str:
    """Mirrors ``app.scale.activation_preview.create_activation_preview``'s
    own ``lineage_hash`` computation exactly -- same version, key set, and
    ``str(guard_revision)`` formatting -- so activation/resume can detect
    lineage drift since preview-time by simple equality against
    ``preview.lineage_hash``."""
    return canonical_lineage_hash(1, {
        "candidate_fingerprint": lineage.candidate_fingerprint,
        "guard_revision": str(lineage.guard_revision),
        "evidence_id": evidence.id,
        "attestation_id": evidence.attestation_id,
    })


def _build_episode_from_evidence(
    session: Session,
    context: TenantContext,
    enrollment: ScaleEnrollmentRecord,
    lineage: ValidatedEnrollmentLineage,
    evidence: ScaleDeliveryEvidenceSnapshotRecord,
) -> tuple[ScaleEpisodeRecord, ScaleDeliveryConfigSourceRecord]:
    """Build (not insert) a fresh Episode plus its exact frozen delivery-
    configuration baseline from server-resolved lineage + evidence (issue
    #86 Task 11) -- never from a browser-supplied ``DeliveryConfigBaseline``/
    ``EpisodeContext``, which is exactly what let a browser bypass the
    lineage gate before this hardening.

    Every field below has a genuine, already-persisted source:
    ``timezone_name`` from the exact ``ObservationContractGenerationRecord``
    the validated DeliveryTarget was re-proven against, ``currency`` from
    the Workspace's own ``default_currency``, ``attribution_semantics_hash``
    from the re-validated Winner lineage, and
    ``metric_semantic_contract_set_id`` from the single compatible set
    declared for this contract generation (spec §6.11 step 6,
    :func:`_resolve_metric_semantic_contract_set_id`).

    Known Task 11 scope gap: the evidence this module can reach (Task 10's
    ``resolve_activation_evidence`` / ``ScaleDeliveryEvidenceSnapshotRecord``)
    proves lineage identity and evidence *freshness* only -- it does not
    carry genuine Meta campaign/ad-set/ad status, budget, optimization,
    bid, or targeting content (spec §5.1 describes a richer snapshot shape
    Task 10 did not ship; see module docstring). Every
    ``ScaleDeliveryConfigSourceRecord`` field that would need one of those
    values is filled with a clearly-labeled, deterministic non-value
    (:data:`_UNVERIFIED`, ``Decimal("0")``, or a hash/id derived only from
    this evidence snapshot's own identity) instead of trusting a
    browser-supplied one. A future ticket that extends the evidence
    pipeline to capture genuine delivery-config content should replace
    this synthesis with real values without needing to change this
    function's shape.
    """
    target = lineage.target
    generation = _load_contract_generation(
        session, context, contract_generation_id=target.contract_generation_id,
    )
    currency = _load_workspace_default_currency(session, context)
    metric_semantic_contract_set_id = _resolve_metric_semantic_contract_set_id(
        session, context, contract_generation_id=target.contract_generation_id,
    )
    attribution_semantics_hash = lineage.winner.attribution_semantics_hash

    observed_at = evidence.observed_at
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    started_local_day = observed_at.astimezone(ZoneInfo(generation.local_timezone)).date()

    evidence_hash = canonical_lineage_hash(1, {
        "evidence_snapshot_id": evidence.id,
        "delivery_target_fingerprint": evidence.delivery_target_fingerprint,
        "contract_generation_id": evidence.contract_generation_id,
        "attestation_id": evidence.attestation_id,
        "evidence_watermark": evidence.evidence_watermark,
        "observed_at": observed_at,
    })
    comparability_hash = canonical_lineage_hash(1, {
        "delivery_target_fingerprint": evidence.delivery_target_fingerprint,
        "evidence_watermark": evidence.evidence_watermark,
    })
    raw_response_hash = canonical_lineage_hash(1, {"evidence_snapshot_id": evidence.id})
    targeting_safety_hash = canonical_lineage_hash(1, {
        "target_fingerprint": target.target_fingerprint,
    })

    delivery_config = DeliveryConfigBaseline(
        contract_generation_id=target.contract_generation_id,
        ad_creative_id=target.ad_creative_id,
        campaign_status=_UNVERIFIED,
        ad_set_status=_UNVERIFIED,
        ad_status=_UNVERIFIED,
        budget_source=_UNVERIFIED,
        budget_amount=Decimal("0"),
        optimization_goal=_UNVERIFIED,
        bid_strategy=_UNVERIFIED,
        attribution_setting=target.attribution_setting,
        targeting_safety_hash=targeting_safety_hash,
        evidence_hash=evidence_hash,
        comparability_hash=comparability_hash,
        raw_response_hash=raw_response_hash,
        sync_run_id=f"unverified_sync:{evidence.id}",
        occurrence_id=f"unverified_occurrence:{evidence.id}",
        call_intent_id=f"unverified_call_intent:{evidence.id}",
        observed_at=observed_at,
        reconciliation_id=None,
        offer_version_id=target.offer_version_id,
        landing_page_version_id=target.landing_page_version_id,
    )
    episode_context = EpisodeContext(
        metric_semantic_contract_set_id=metric_semantic_contract_set_id,
        timezone_name=generation.local_timezone,
        currency=currency,
        attribution_semantics_hash=attribution_semantics_hash,
        started_local_day=started_local_day,
    )
    return _build_episode_and_delivery_source(context, enrollment, delivery_config, episode_context)


def _commit_transition(
    session: Session,
    context: TenantContext,
    *,
    dedup_key: str,
    mutated: list,
    added: list[list],
    conflict_code: str,
    conflict_detail: str = "",
) -> ScaleEnrollmentEventRecord | None:
    """Flush a lifecycle transition's already-mutated rows plus its new
    child rows inside one SAVEPOINT. Returns ``None`` on success. On a
    unique-constraint race -- the Postgres active/paused binding index, or
    this exact idempotency key already applied by a concurrent caller --
    every mutated row is expired (so its in-memory attributes cannot lie
    about the rolled-back DB state) and the existing dedup event is
    returned for idempotent-replay handling; if no such event exists, the
    race was a genuine conflict and this raises ``conflict_code``.

    ``added`` is a list of flush *stages*, each a list of new rows: staged
    rather than one flat ``add_all`` because SQLAlchemy's automatic
    flush-ordering across mapped classes with no ORM ``relationship()`` does
    not reliably topo-sort the composite, non-primary-key
    ``ForeignKeyConstraint``s every Scale table uses (known pitfall, see
    ``tests/test_scale_evidence.py``'s fixture comment) -- a child row's
    stage must be flushed strictly after the stage inserting the parent row
    it references.
    """
    try:
        with session.begin_nested():
            for stage in added:
                session.add_all(stage)
                session.flush()
    except IntegrityError:
        for obj in mutated:
            session.expire(obj)
        recovered = _existing_enrollment_event(session, context, dedup_key)
        if recovered is None:
            raise EnrollmentLifecycleError(conflict_code, conflict_detail)
        return recovered
    return None


# ---------------------------------------------------------------------------
# create_draft_enrollment
# ---------------------------------------------------------------------------


def create_draft_enrollment(
    session: Session,
    context: TenantContext,
    *,
    enrollment_candidate_id: str,
    idempotency_key: str,
) -> ScaleEnrollmentRecord:
    """The only HTTP-reachable way to create a Draft Enrollment (issue #86
    Task 9). Accepts a single opaque, already-proven
    ``ScaleEnrollmentCandidateRecord`` id -- never raw Winner/DeliveryTarget
    identifiers, which the pre-#86 :func:`create_draft_enrollment_from_request`
    accepted directly and which a browser could otherwise supply for any
    lineage it can guess (spec §4.4's whole reason for existing).

    Lock ordering (spec §7.4): claims the command journal first, then
    authoritatively re-validates and locks the Candidate's lineage (never
    trusting the row's frozen fields on their own -- see
    ``app.scale.enrollment_lineage``'s module docstring), then atomically
    consumes the Candidate, and only then builds the Enrollment row --
    every identifying field on it comes from the freshly re-validated
    lineage, not from the Candidate row nor from any caller-supplied value.
    """
    from .candidates import (
        CandidateAlreadyConsumedError,
        CandidateExpiredError,
        CandidateNotFoundError,
        consume_candidate,
    )
    from .command_journal import claim_command
    from .enrollment_lineage import (
        EnrollmentCandidateNotFoundError,
        lock_and_validate_enrollment_candidate,
    )

    enrollment_id = new_id("senr")

    scenario_scope_key = None
    if context.meta_mode == "controlled_mock":
        scenario_scope_key = session.execute(
            select(ScaleEnrollmentCandidateRecord.scenario_scope_key).filter_by(
                workspace_id=context.workspace_id,
                meta_mode=context.meta_mode,
                id=enrollment_candidate_id,
            )
        ).scalar_one_or_none()
        if scenario_scope_key is None:
            raise CandidateNotFoundError(enrollment_candidate_id)

    claim = claim_command(
        session, context,
        command_type="create_enrollment",
        deduplication_key=idempotency_key,
        result_id=enrollment_id,
        request_fingerprint=enrollment_candidate_id,
        scenario_scope_key=scenario_scope_key,
    )
    if claim.replay:
        enrollment = session.get(ScaleEnrollmentRecord, claim.record.result_resource_id)
        if enrollment is None:
            raise EnrollmentLifecycleError(
                "idempotent_replay_missing_enrollment", claim.record.result_resource_id,
            )
        session.refresh(enrollment)
        return enrollment

    try:
        lineage = lock_and_validate_enrollment_candidate(
            session, context, candidate_id=enrollment_candidate_id,
        )
    except EnrollmentCandidateNotFoundError as exc:
        raise CandidateNotFoundError(enrollment_candidate_id) from exc

    candidate = consume_candidate(session, context, enrollment_candidate_id)

    now = datetime.now(timezone.utc)
    expires_at = candidate.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < now:
        raise CandidateExpiredError(enrollment_candidate_id)
    if candidate.candidate_fingerprint != lineage.candidate_fingerprint:
        raise CandidateFingerprintDriftError(enrollment_candidate_id)

    winner = lineage.winner
    target = lineage.target
    remote_binding_key = _compute_remote_binding_key(
        target.meta_connection_id, target.ad_account_id,
        target.campaign_remote_id, target.adset_remote_id, target.ad_remote_id,
    )
    original_initiator_id = context.initiator_id or context.executor
    enrollment = ScaleEnrollmentRecord(
        id=enrollment_id,
        workspace_id=context.workspace_id,
        meta_mode=context.meta_mode,
        meta_connection_id=target.meta_connection_id,
        scenario_scope_key=target.scenario_scope_key,
        ad_account_id=target.ad_account_id,
        winner_decision_id=winner.decision_id,
        winner_confirmation_id=winner.confirmation_id,
        asset_version_id=winner.asset_version_id,
        product_version_id=winner.product_version_id,
        offer_version_id=winner.offer_version_id,
        market_code=winner.market_code,
        landing_page_version_id=winner.landing_page_version_id,
        market_asset_binding_id=winner.market_asset_binding_id,
        campaign_remote_id=target.campaign_remote_id,
        ad_set_remote_id=target.adset_remote_id,
        ad_remote_id=target.ad_remote_id,
        remote_binding_key=remote_binding_key,
        lifecycle="draft",
        creator_id=original_initiator_id,
        original_initiator_id=original_initiator_id,
        revision=1,
        enrollment_candidate_id=enrollment_candidate_id,
        delivery_target_id=target.id,
        lineage_guard_id=lineage.guard.id,
        guard_revision_at_draft=lineage.guard_revision,
        candidate_fingerprint=lineage.candidate_fingerprint,
    )
    event = ScaleEnrollmentEventRecord(
        id=new_id("sene"),
        enrollment_id=enrollment_id,
        workspace_id=context.workspace_id,
        meta_mode=context.meta_mode,
        sequence=1,
        event_type="draft_created",
        previous_lifecycle=None,
        current_lifecycle="draft",
        reason=None,
        deduplication_key=f"draft:{context.workspace_id}:{idempotency_key}",
        executor_id=_executor_id(context),
        original_initiator_id=original_initiator_id,
        revision_at_event=1,
    )
    # Two flush stages, not one ``add_all``: SQLAlchemy's automatic
    # flush-ordering across mapped classes with no ORM ``relationship()``
    # does not reliably topo-sort composite, non-primary-key
    # ``ForeignKeyConstraint``s (every Scale table uses this shape) -- the
    # event's FK into this Enrollment row requires the Enrollment's own
    # INSERT to commit first (see ``_commit_transition``'s identical note
    # above).
    session.add(enrollment)
    session.flush()
    session.add(event)
    session.flush()
    return enrollment


# ---------------------------------------------------------------------------
# create_draft_enrollment_from_request -- pre-#86 raw-ID primitive
# ---------------------------------------------------------------------------


def create_draft_enrollment_from_request(
    session: Session,
    context: TenantContext,
    request: DraftEnrollmentRequest,
    *,
    idempotency_key: str,
) -> ScaleEnrollmentRecord:
    """The pre-#86 raw-ID Draft Enrollment constructor. No HTTP route
    reaches this any longer -- :func:`create_draft_enrollment` (candidate-
    gated, issue #86 Task 9) is the only HTTP-reachable entry point, and it
    trusts a caller-supplied Winner/DeliveryTarget identifier for exactly
    zero of an Enrollment's fields. This function is kept, under this new
    name, purely as a lower-level primitive for internal composition and
    for the Task 11 lifecycle-transition test suite
    (``tests/test_scale_enrollment.py``), which exercises
    activate/pause/resume/close state-machine behavior that has nothing to
    do with lineage gating and does not need the full Winner/DeliveryTarget
    proof chain to set up a Draft to transition from.

    Re-validates only what this module can itself determine from the
    persisted Draft/request (remote-binding shape, scenario/mode
    consistency) -- see the module docstring's original scoping note; it
    still performs no Winner/DeliveryTarget eligibility check of its own,
    exactly as it never did before this task.
    """
    meta_mode = _require_meta_mode(context)
    dedup_key = f"draft:{context.workspace_id}:{idempotency_key}"

    existing = _existing_enrollment_event(session, context, dedup_key)
    if existing is not None:
        enrollment = session.get(ScaleEnrollmentRecord, existing.enrollment_id)
        if enrollment is None:
            raise EnrollmentLifecycleError("idempotent_replay_missing_enrollment", dedup_key)
        return enrollment

    _validate_scenario_scope(meta_mode, request.scenario_scope_key)
    _validate_remote_binding_shape(
        request.campaign_remote_id, request.ad_set_remote_id, request.ad_remote_id,
    )
    for field_name in (
        "winner_decision_id", "winner_confirmation_id", "asset_version_id",
        "product_version_id", "offer_version_id", "market_code",
        "meta_connection_id", "ad_account_id", "creator_id",
    ):
        if not getattr(request, field_name):
            raise EnrollmentLifecycleError("required_field_missing", field_name)

    remote_binding_key = _compute_remote_binding_key(
        request.meta_connection_id, request.ad_account_id,
        request.campaign_remote_id, request.ad_set_remote_id, request.ad_remote_id,
    )

    enrollment_id = new_id("senr")
    original_initiator_id = context.initiator_id or request.creator_id
    enrollment = ScaleEnrollmentRecord(
        id=enrollment_id,
        workspace_id=context.workspace_id,
        meta_mode=meta_mode,
        meta_connection_id=request.meta_connection_id,
        scenario_scope_key=request.scenario_scope_key,
        ad_account_id=request.ad_account_id,
        winner_decision_id=request.winner_decision_id,
        winner_confirmation_id=request.winner_confirmation_id,
        asset_version_id=request.asset_version_id,
        product_version_id=request.product_version_id,
        offer_version_id=request.offer_version_id,
        market_code=request.market_code,
        landing_page_version_id=request.landing_page_version_id,
        market_asset_binding_id=request.market_asset_binding_id,
        campaign_remote_id=request.campaign_remote_id,
        ad_set_remote_id=request.ad_set_remote_id,
        ad_remote_id=request.ad_remote_id,
        remote_binding_key=remote_binding_key,
        lifecycle="draft",
        creator_id=request.creator_id,
        original_initiator_id=original_initiator_id,
        revision=1,
    )
    event = ScaleEnrollmentEventRecord(
        id=new_id("sene"),
        enrollment_id=enrollment_id,
        workspace_id=context.workspace_id,
        meta_mode=meta_mode,
        sequence=1,
        event_type="draft_created",
        previous_lifecycle=None,
        current_lifecycle="draft",
        reason=None,
        deduplication_key=dedup_key,
        executor_id=_executor_id(context),
        original_initiator_id=original_initiator_id,
        revision_at_event=1,
    )

    recovered = _commit_transition(
        session, context,
        dedup_key=dedup_key,
        mutated=[],
        added=[[enrollment], [event]],
        conflict_code="draft_creation_conflict",
    )
    if recovered is not None:
        recovered_enrollment = session.get(ScaleEnrollmentRecord, recovered.enrollment_id)
        if recovered_enrollment is None:
            raise EnrollmentLifecycleError("idempotent_replay_missing_enrollment", dedup_key)
        return recovered_enrollment
    return enrollment


# ---------------------------------------------------------------------------
# activate_enrollment
# ---------------------------------------------------------------------------


def activate_enrollment(
    session: Session,
    context: TenantContext,
    *,
    enrollment_id: str,
    activation_preview_id: str,
    expected_revision: int,
    idempotency_key: str,
) -> tuple[ScaleEnrollmentRecord, ScaleEpisodeRecord]:
    """Draft -> active, gated entirely by a server-consumed
    ``ScaleActivationPreviewRecord`` (issue #86 Task 11). The caller
    supplies only ``activation_preview_id`` -- every lineage/evidence
    identifying field the pre-hardening ``delivery_config``/
    ``episode_context`` parameters let a browser set directly is now
    server-derived (:func:`_build_episode_from_evidence`).

    Lock ordering (spec §7.4): Journal (:func:`~app.scale.command_journal.
    claim_command`) -> Guard (:func:`~app.scale.enrollment_lineage.
    lock_and_validate_enrollment_candidate`) -> Enrollment
    (:func:`_lock_enrollment`) -> Preview (:func:`~app.scale.
    activation_preview.load_and_consume_preview`) -> Episode.
    """
    from .activation_evidence import resolve_activation_evidence
    from .activation_preview import load_and_consume_preview
    from .command_journal import claim_command
    from .enrollment_lineage import lock_and_validate_enrollment_candidate

    meta_mode = _require_meta_mode(context)
    dedup_key = f"activate:{enrollment_id}:{idempotency_key}"

    # Unlocked discovery read (spec §7.4): only to learn the candidate id
    # and scenario scope before any lock is taken -- never trusted for
    # anything beyond that (the Guard/Enrollment locks below re-prove it).
    discovery = session.execute(
        select(ScaleEnrollmentRecord).filter_by(
            workspace_id=context.workspace_id, meta_mode=meta_mode, id=enrollment_id,
        )
    ).scalar_one_or_none()
    if discovery is None:
        raise EnrollmentLifecycleError("enrollment_not_found", enrollment_id)
    if discovery.enrollment_candidate_id is None:
        raise EnrollmentLifecycleError(
            "candidate_lineage_required",
            f"enrollment {enrollment_id} has no candidate lineage (legacy raw-ID path)",
        )

    claim = claim_command(
        session, context,
        command_type="activate_enrollment",
        deduplication_key=dedup_key,
        result_id=enrollment_id,
        request_fingerprint=f"{activation_preview_id}:{expected_revision}",
        scenario_scope_key=(
            discovery.scenario_scope_key if meta_mode == "controlled_mock" else None
        ),
    )
    if claim.replay:
        enrollment = session.get(ScaleEnrollmentRecord, claim.record.result_resource_id)
        if enrollment is None:
            raise EnrollmentLifecycleError(
                "idempotent_replay_missing_enrollment", claim.record.result_resource_id,
            )
        # The unlocked discovery read happened before claim_command waited for
        # the concurrent winner. Refresh the identity-map row so the replay
        # sees that winner's lifecycle and current Episode.
        session.refresh(enrollment)
        episode = (
            session.get(ScaleEpisodeRecord, enrollment.current_episode_id)
            if enrollment.current_episode_id
            else None
        )
        return enrollment, episode

    # Guard lock + re-validated Winner/Target pair (spec §7.4 "Guard" step).
    lineage = lock_and_validate_enrollment_candidate(
        session, context, candidate_id=discovery.enrollment_candidate_id,
    )

    # Enrollment lock (spec §7.4 "Enrollment" step).
    enrollment = _lock_enrollment(session, context, enrollment_id)
    _require_lifecycle(enrollment, ("draft",))
    _require_revision(enrollment, expected_revision)

    # Repeat every gate under lock (spec §3.7): the Scale-owned invariants
    # this module can itself re-verify from the persisted Draft. See module
    # docstring for what remains the caller's responsibility.
    _validate_scenario_scope(enrollment.meta_mode, enrollment.scenario_scope_key)
    _validate_remote_binding_shape(
        enrollment.campaign_remote_id, enrollment.ad_set_remote_id, enrollment.ad_remote_id,
    )

    # Preview CAS (spec §7.4 "Preview" step).
    preview = load_and_consume_preview(
        session, context,
        preview_id=activation_preview_id,
        enrollment_id=enrollment_id,
        expected_enrollment_revision=expected_revision,
    )
    if preview.transition_kind != "activate":
        raise EnrollmentLifecycleError(
            "preview_transition_kind_mismatch",
            f"preview {activation_preview_id} is for {preview.transition_kind!r}, not activate",
        )

    now = datetime.now(timezone.utc)
    evidence = resolve_activation_evidence(session, context, lineage=lineage, reference_time=now)
    if _compute_lineage_hash(lineage, evidence) != preview.lineage_hash:
        raise EnrollmentLifecycleError(
            "preview_lineage_stale", f"lineage drifted since preview {activation_preview_id}",
        )

    conflict = session.execute(
        select(ScaleEnrollmentRecord.id).filter(
            ScaleEnrollmentRecord.workspace_id == context.workspace_id,
            ScaleEnrollmentRecord.meta_mode == context.meta_mode,
            ScaleEnrollmentRecord.meta_connection_id == enrollment.meta_connection_id,
            ScaleEnrollmentRecord.ad_account_id == enrollment.ad_account_id,
            ScaleEnrollmentRecord.remote_binding_key == enrollment.remote_binding_key,
            ScaleEnrollmentRecord.lifecycle.in_(("active", "paused")),
            ScaleEnrollmentRecord.id != enrollment.id,
        )
    ).scalar_one_or_none()
    if conflict is not None:
        raise EnrollmentLifecycleError("duplicate_binding", enrollment.remote_binding_key)

    # Episode (spec §7.4 "Episode" step).
    episode, delivery_source = _build_episode_from_evidence(
        session, context, enrollment, lineage, evidence,
    )

    new_revision = enrollment.revision + 1
    enrollment.lifecycle = "active"
    enrollment.current_episode_id = episode.id
    enrollment.transition_reason = None
    enrollment.revision = new_revision
    enrollment.updated_at = utcnow()

    enrollment_event = ScaleEnrollmentEventRecord(
        id=new_id("sene"),
        enrollment_id=enrollment.id,
        workspace_id=context.workspace_id,
        meta_mode=context.meta_mode,
        sequence=new_revision,
        event_type="activated",
        previous_lifecycle="draft",
        current_lifecycle="active",
        reason=None,
        deduplication_key=dedup_key,
        executor_id=_executor_id(context),
        original_initiator_id=enrollment.original_initiator_id,
        revision_at_event=new_revision,
    )
    episode_event = ScaleEpisodeEventRecord(
        id=new_id("sevt"),
        episode_id=episode.id,
        workspace_id=context.workspace_id,
        meta_mode=context.meta_mode,
        enrollment_id=enrollment.id,
        event_type="opened",
        previous_phase=None,
        current_phase="collecting",
        closure_reason=None,
        deduplication_key=f"open:{episode.id}:{idempotency_key}",
        executor_id=_executor_id(context),
        original_initiator_id=enrollment.original_initiator_id,
    )

    recovered = _commit_transition(
        session, context,
        dedup_key=dedup_key,
        mutated=[enrollment],
        added=[[episode], [delivery_source, episode_event, enrollment_event]],
        conflict_code="duplicate_binding",
        conflict_detail=enrollment.remote_binding_key,
    )
    if recovered is not None:
        reloaded = _lock_enrollment(session, context, enrollment_id)
        reloaded_episode = (
            session.get(ScaleEpisodeRecord, reloaded.current_episode_id)
            if reloaded.current_episode_id
            else None
        )
        return reloaded, reloaded_episode

    return enrollment, episode


# ---------------------------------------------------------------------------
# pause_enrollment
# ---------------------------------------------------------------------------


def pause_enrollment(
    session: Session,
    context: TenantContext,
    enrollment_id: str,
    *,
    expected_revision: int,
    idempotency_key: str,
    reason: str = "user_requested",
) -> ScaleEnrollmentRecord:
    dedup_key = f"pause:{enrollment_id}:{idempotency_key}"

    existing = _existing_enrollment_event(session, context, dedup_key)
    if existing is not None:
        return _lock_enrollment(session, context, enrollment_id)

    enrollment = _lock_enrollment(session, context, enrollment_id)
    _require_lifecycle(enrollment, ("active",))
    _require_revision(enrollment, expected_revision)

    new_revision = enrollment.revision + 1
    enrollment.lifecycle = "paused"
    enrollment.transition_reason = reason
    # Fences pending/in-flight live evaluation runs (spec §3.7): Task 12's
    # orchestration compares a run's frozen generation against this current
    # value and cancels a stale one rather than projecting it.
    enrollment.evaluation_cancellation_generation += 1
    enrollment.revision = new_revision
    enrollment.updated_at = utcnow()

    event = ScaleEnrollmentEventRecord(
        id=new_id("sene"),
        enrollment_id=enrollment.id,
        workspace_id=context.workspace_id,
        meta_mode=context.meta_mode,
        sequence=new_revision,
        event_type="paused",
        previous_lifecycle="active",
        current_lifecycle="paused",
        reason=reason,
        deduplication_key=dedup_key,
        executor_id=_executor_id(context),
        original_initiator_id=enrollment.original_initiator_id,
        revision_at_event=new_revision,
    )

    recovered = _commit_transition(
        session, context,
        dedup_key=dedup_key,
        mutated=[enrollment],
        added=[[event]],
        conflict_code="pause_conflict",
    )
    if recovered is not None:
        return _lock_enrollment(session, context, enrollment_id)
    return enrollment


# ---------------------------------------------------------------------------
# resume_enrollment
# ---------------------------------------------------------------------------


def resume_enrollment(
    session: Session,
    context: TenantContext,
    *,
    enrollment_id: str,
    activation_preview_id: str,
    expected_revision: int,
    idempotency_key: str,
) -> tuple[ScaleEnrollmentRecord, ScaleEpisodeRecord]:
    """Paused -> active, gated entirely by a server-consumed
    ``ScaleActivationPreviewRecord`` with ``transition_kind='resume'``
    (issue #86 Task 11) -- the same protocol as :func:`activate_enrollment`,
    never bypassable by a browser-supplied ``delivery_config``/
    ``episode_context`` (spec §5.3: "Resume cannot bypass this protocol").
    A fresh preview (and re-resolved evidence) is required even when the
    current Episode is still open and simply continues untouched: the
    preview is what proves lineage is *still* valid at resume-time, not
    only what supplies fresh evidence for a brand-new Episode.
    """
    from .activation_evidence import resolve_activation_evidence
    from .activation_preview import load_and_consume_preview
    from .command_journal import claim_command
    from .enrollment_lineage import lock_and_validate_enrollment_candidate

    meta_mode = _require_meta_mode(context)
    dedup_key = f"resume:{enrollment_id}:{idempotency_key}"

    # Unlocked discovery read (spec §7.4) -- see activate_enrollment's
    # identical comment.
    discovery = session.execute(
        select(ScaleEnrollmentRecord).filter_by(
            workspace_id=context.workspace_id, meta_mode=meta_mode, id=enrollment_id,
        )
    ).scalar_one_or_none()
    if discovery is None:
        raise EnrollmentLifecycleError("enrollment_not_found", enrollment_id)
    if discovery.enrollment_candidate_id is None:
        raise EnrollmentLifecycleError(
            "candidate_lineage_required",
            f"enrollment {enrollment_id} has no candidate lineage (legacy raw-ID path)",
        )

    claim = claim_command(
        session, context,
        command_type="resume_enrollment",
        deduplication_key=dedup_key,
        result_id=enrollment_id,
        request_fingerprint=f"{activation_preview_id}:{expected_revision}",
        scenario_scope_key=(
            discovery.scenario_scope_key if meta_mode == "controlled_mock" else None
        ),
    )
    if claim.replay:
        enrollment = session.get(ScaleEnrollmentRecord, claim.record.result_resource_id)
        if enrollment is None:
            raise EnrollmentLifecycleError(
                "idempotent_replay_missing_enrollment", claim.record.result_resource_id,
            )
        # See activate_enrollment: discovery may have cached the pre-winner
        # paused row while the journal claim was blocked.
        session.refresh(enrollment)
        episode = (
            session.get(ScaleEpisodeRecord, enrollment.current_episode_id)
            if enrollment.current_episode_id
            else None
        )
        return enrollment, episode

    # Guard lock + re-validated Winner/Target pair (spec §7.4 "Guard" step).
    lineage = lock_and_validate_enrollment_candidate(
        session, context, candidate_id=discovery.enrollment_candidate_id,
    )

    # Enrollment lock (spec §7.4 "Enrollment" step).
    enrollment = _lock_enrollment(session, context, enrollment_id)
    _require_lifecycle(enrollment, ("paused",))
    _require_revision(enrollment, expected_revision)

    # Preview CAS (spec §7.4 "Preview" step).
    preview = load_and_consume_preview(
        session, context,
        preview_id=activation_preview_id,
        enrollment_id=enrollment_id,
        expected_enrollment_revision=expected_revision,
    )
    if preview.transition_kind != "resume":
        raise EnrollmentLifecycleError(
            "preview_transition_kind_mismatch",
            f"preview {activation_preview_id} is for {preview.transition_kind!r}, not resume",
        )

    now = datetime.now(timezone.utc)
    evidence = resolve_activation_evidence(session, context, lineage=lineage, reference_time=now)
    if _compute_lineage_hash(lineage, evidence) != preview.lineage_hash:
        raise EnrollmentLifecycleError(
            "preview_lineage_stale", f"lineage drifted since preview {activation_preview_id}",
        )

    current_episode = (
        _lock_episode(session, context, enrollment.current_episode_id)
        if enrollment.current_episode_id
        else None
    )

    added_stages: list[list] = []
    if current_episode is not None and current_episode.phase != "closed":
        # Stable configuration, continuous evidence: continue the open
        # Episode untouched (no phase transition, so no episode event).
        target_episode = current_episode
    else:
        # A previously closed no-delivery (or otherwise closed) Episode
        # always causes resume to create a new Episode, now from the
        # server-resolved evidence/lineage above (spec §3.7/§5.3), never
        # from a caller-supplied delivery_config/episode_context.
        target_episode, delivery_source = _build_episode_from_evidence(
            session, context, enrollment, lineage, evidence,
        )
        episode_event = ScaleEpisodeEventRecord(
            id=new_id("sevt"),
            episode_id=target_episode.id,
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            enrollment_id=enrollment.id,
            event_type="opened",
            previous_phase=None,
            current_phase="collecting",
            closure_reason=None,
            deduplication_key=f"open:{target_episode.id}:{idempotency_key}",
            executor_id=_executor_id(context),
            original_initiator_id=enrollment.original_initiator_id,
        )
        # `target_episode` must be flushed (its own INSERT stage) before
        # `delivery_source`/`episode_event`, both of which FK-reference it.
        added_stages.append([target_episode])
        added_stages.append([delivery_source, episode_event])

    new_revision = enrollment.revision + 1
    enrollment.lifecycle = "active"
    enrollment.transition_reason = None
    enrollment.current_episode_id = target_episode.id
    enrollment.revision = new_revision
    enrollment.updated_at = utcnow()

    enrollment_event = ScaleEnrollmentEventRecord(
        id=new_id("sene"),
        enrollment_id=enrollment.id,
        workspace_id=context.workspace_id,
        meta_mode=context.meta_mode,
        sequence=new_revision,
        event_type="resumed",
        previous_lifecycle="paused",
        current_lifecycle="active",
        reason=None,
        deduplication_key=dedup_key,
        executor_id=_executor_id(context),
        original_initiator_id=enrollment.original_initiator_id,
        revision_at_event=new_revision,
    )
    added_stages.append([enrollment_event])

    recovered = _commit_transition(
        session, context,
        dedup_key=dedup_key,
        mutated=[enrollment],
        added=added_stages,
        conflict_code="resume_conflict",
    )
    if recovered is not None:
        reloaded = _lock_enrollment(session, context, enrollment_id)
        reloaded_episode = (
            session.get(ScaleEpisodeRecord, reloaded.current_episode_id)
            if reloaded.current_episode_id
            else None
        )
        return reloaded, reloaded_episode

    return enrollment, target_episode


# ---------------------------------------------------------------------------
# close_enrollment
# ---------------------------------------------------------------------------


def _close_episode(
    session: Session,
    context: TenantContext,
    enrollment: ScaleEnrollmentRecord,
    *,
    closure_reason: str,
    idempotency_key: str,
) -> tuple[ScaleEpisodeRecord | None, ScaleEpisodeEventRecord | None]:
    """Lock and close the Enrollment's current Episode, if it has one open.
    Returns ``(None, None)`` when there is nothing to close."""
    if not enrollment.current_episode_id:
        return None, None
    episode = _lock_episode(session, context, enrollment.current_episode_id)
    if episode.phase == "closed":
        return episode, None

    episode.phase = "closed"
    episode.ended_at = utcnow()
    episode.closure_reason = closure_reason
    episode.revision += 1

    episode_event = ScaleEpisodeEventRecord(
        id=new_id("sevt"),
        episode_id=episode.id,
        workspace_id=context.workspace_id,
        meta_mode=context.meta_mode,
        enrollment_id=enrollment.id,
        event_type="closed",
        previous_phase=None,
        current_phase="closed",
        closure_reason=closure_reason,
        deduplication_key=f"close:{episode.id}:{idempotency_key}",
        executor_id=_executor_id(context),
        original_initiator_id=enrollment.original_initiator_id,
    )
    return episode, episode_event


def close_enrollment(
    session: Session,
    context: TenantContext,
    enrollment_id: str,
    *,
    expected_revision: int,
    idempotency_key: str,
    reason: str = "user_requested",
) -> ScaleEnrollmentRecord:
    dedup_key = f"close:{enrollment_id}:{idempotency_key}"

    existing = _existing_enrollment_event(session, context, dedup_key)
    if existing is not None:
        return _lock_enrollment(session, context, enrollment_id)

    enrollment = _lock_enrollment(session, context, enrollment_id)
    _require_lifecycle(enrollment, ("active", "paused"))
    _require_revision(enrollment, expected_revision)

    previous_lifecycle = enrollment.lifecycle
    episode, episode_event = _close_episode(
        session, context, enrollment, closure_reason="enrollment_closed", idempotency_key=idempotency_key,
    )

    new_revision = enrollment.revision + 1
    enrollment.lifecycle = "closed"
    enrollment.transition_reason = reason
    enrollment.closed_at = utcnow()
    enrollment.evaluation_cancellation_generation += 1
    enrollment.revision = new_revision
    enrollment.updated_at = utcnow()

    enrollment_event = ScaleEnrollmentEventRecord(
        id=new_id("sene"),
        enrollment_id=enrollment.id,
        workspace_id=context.workspace_id,
        meta_mode=context.meta_mode,
        sequence=new_revision,
        event_type="closed",
        previous_lifecycle=previous_lifecycle,
        current_lifecycle="closed",
        reason=reason,
        deduplication_key=dedup_key,
        executor_id=_executor_id(context),
        original_initiator_id=enrollment.original_initiator_id,
        revision_at_event=new_revision,
    )

    stage = [enrollment_event]
    mutated = [enrollment]
    if episode_event is not None:
        stage.append(episode_event)
        mutated.append(episode)
    added = [stage]

    recovered = _commit_transition(
        session, context,
        dedup_key=dedup_key,
        mutated=mutated,
        added=added,
        conflict_code="close_conflict",
    )
    if recovered is not None:
        return _lock_enrollment(session, context, enrollment_id)
    return enrollment


# ---------------------------------------------------------------------------
# handle_upstream_withdrawal
# ---------------------------------------------------------------------------


def handle_upstream_withdrawal(
    session: Session,
    context: TenantContext,
    enrollment_id: str,
    *,
    idempotency_key: str,
    reason_detail: str | None = None,
) -> ScaleEnrollmentRecord:
    """Spec §3.9. ``idempotency_key`` should be the durable upstream event's
    own unique semantic id, so redelivery of the same upstream event is a
    pure no-op rather than a second withdrawal attempt.

    Unconditional on ``expected_revision``: this is a system-triggered
    event, not a user CAS operation, and every evaluation/conversion
    transaction is required to repeat the upstream check anyway (spec
    §3.9), so racing a concurrent user action here loses to whichever
    commits first and the other observes the closed state on its own retry.
    """
    dedup_key = f"withdraw:{enrollment_id}:{idempotency_key}"

    existing = _existing_enrollment_event(session, context, dedup_key)
    if existing is not None:
        return _lock_enrollment(session, context, enrollment_id)

    enrollment = _lock_enrollment(session, context, enrollment_id)
    if enrollment.lifecycle == "closed":
        # Already terminal (whether from this same withdrawal racing another
        # execution path, an explicit close, or an earlier withdrawal) --
        # withdrawal never reopens or re-closes a closed Enrollment.
        return enrollment
    if enrollment.lifecycle == "draft":
        raise EnrollmentLifecycleError(
            "not_withdrawable", "a draft Enrollment holds no binding reservation to withdraw"
        )

    previous_lifecycle = enrollment.lifecycle
    episode, episode_event = _close_episode(
        session, context, enrollment, closure_reason="upstream_withdrawal", idempotency_key=idempotency_key,
    )

    new_revision = enrollment.revision + 1
    enrollment.lifecycle = "closed"
    enrollment.transition_reason = "upstream_withdrawal"
    enrollment.closed_at = utcnow()
    enrollment.evaluation_cancellation_generation += 1
    enrollment.revision = new_revision
    enrollment.updated_at = utcnow()

    enrollment_event = ScaleEnrollmentEventRecord(
        id=new_id("sene"),
        enrollment_id=enrollment.id,
        workspace_id=context.workspace_id,
        meta_mode=context.meta_mode,
        sequence=new_revision,
        event_type="upstream_withdrawal",
        previous_lifecycle=previous_lifecycle,
        current_lifecycle="closed",
        reason=reason_detail or "upstream_withdrawal",
        deduplication_key=dedup_key,
        executor_id=_executor_id(context),
        original_initiator_id=enrollment.original_initiator_id,
        revision_at_event=new_revision,
    )

    stage = [enrollment_event]
    mutated = [enrollment]
    if episode_event is not None:
        stage.append(episode_event)
        mutated.append(episode)
    added = [stage]

    recovered = _commit_transition(
        session, context,
        dedup_key=dedup_key,
        mutated=mutated,
        added=added,
        conflict_code="withdrawal_conflict",
    )
    if recovered is not None:
        return _lock_enrollment(session, context, enrollment_id)
    return enrollment


# ---------------------------------------------------------------------------
# apply_no_delivery_episode_closure (Task 12's internal caller)
# ---------------------------------------------------------------------------


def apply_no_delivery_episode_closure(
    session: Session,
    context: TenantContext,
    enrollment_id: str,
    episode_id: str,
    *,
    idempotency_key: str,
) -> tuple[ScaleEnrollmentRecord, ScaleEpisodeRecord]:
    """Spec §3.7: seven consecutive complete zero-delivery days close the
    Episode (``closure_reason='no_delivery_7_complete_days'``) and move the
    Enrollment to `paused`, retaining its binding reservation. Task 12's
    orchestration is the sole caller, once its own stable-signal day
    counting decides the condition is met; this command only enforces the
    resulting state transition, locking, and idempotency."""
    dedup_key = f"no-delivery:{episode_id}:{idempotency_key}"

    existing = _existing_enrollment_event(session, context, dedup_key)
    if existing is not None:
        enrollment = _lock_enrollment(session, context, enrollment_id)
        episode = session.get(ScaleEpisodeRecord, episode_id)
        return enrollment, episode

    enrollment = _lock_enrollment(session, context, enrollment_id)
    _require_lifecycle(enrollment, ("active",))
    if enrollment.current_episode_id != episode_id:
        raise EnrollmentLifecycleError(
            "episode_mismatch",
            f"enrollment {enrollment_id} current Episode is "
            f"{enrollment.current_episode_id!r}, not {episode_id!r}",
        )

    episode = _lock_episode(session, context, episode_id)
    if episode.phase == "closed":
        raise EnrollmentLifecycleError("episode_already_closed", episode_id)

    episode.phase = "closed"
    episode.ended_at = utcnow()
    episode.closure_reason = "no_delivery_7_complete_days"
    episode.revision += 1

    episode_event = ScaleEpisodeEventRecord(
        id=new_id("sevt"),
        episode_id=episode.id,
        workspace_id=context.workspace_id,
        meta_mode=context.meta_mode,
        enrollment_id=enrollment.id,
        event_type="closed",
        previous_phase=None,
        current_phase="closed",
        closure_reason="no_delivery_7_complete_days",
        deduplication_key=f"close:{episode.id}:{idempotency_key}",
        executor_id=_executor_id(context),
        original_initiator_id=enrollment.original_initiator_id,
    )

    new_revision = enrollment.revision + 1
    enrollment.lifecycle = "paused"
    enrollment.transition_reason = "no_delivery_7_complete_days"
    enrollment.evaluation_cancellation_generation += 1
    enrollment.revision = new_revision
    enrollment.updated_at = utcnow()

    enrollment_event = ScaleEnrollmentEventRecord(
        id=new_id("sene"),
        enrollment_id=enrollment.id,
        workspace_id=context.workspace_id,
        meta_mode=context.meta_mode,
        sequence=new_revision,
        event_type="paused",
        previous_lifecycle="active",
        current_lifecycle="paused",
        reason="no_delivery_7_complete_days",
        deduplication_key=dedup_key,
        executor_id=_executor_id(context),
        original_initiator_id=enrollment.original_initiator_id,
        revision_at_event=new_revision,
    )

    recovered = _commit_transition(
        session, context,
        dedup_key=dedup_key,
        mutated=[enrollment, episode],
        added=[[episode_event, enrollment_event]],
        conflict_code="no_delivery_closure_conflict",
    )
    if recovered is not None:
        reloaded = _lock_enrollment(session, context, enrollment_id)
        reloaded_episode = session.get(ScaleEpisodeRecord, episode_id)
        return reloaded, reloaded_episode

    return enrollment, episode
